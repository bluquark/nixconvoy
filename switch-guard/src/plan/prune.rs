//! Whether a subagent worktree (<checkout>/.claude/worktrees/<name>) is
//! removed, and its removal.  Claude Code never removes the worktrees
//! nixconvoy's WorktreeCreate hook makes, and an agent can be resumed while
//! the session that spawned it runs, which needs its worktree; so a
//! worktree goes only once the session its lock names has exited, and only
//! when it holds nothing of its own.  A sweep (no name given) keeps an
//! unlocked one, which no session's exit tells the end of.
//!
//! Every check is taken under the worktree's deploy lock, so no deploy
//! moves it meanwhile; the agent's session is dead, so nothing else writes
//! to it.  What can still move is its lock: the create hook hands a dead
//! session's worktree to a new session (`claude --worktree <name>`) by
//! renaming a new lock over the old one.  So the removal claims the lock
//! it judged - renames it aside, only if it still holds that reason - and
//! then removes the worktree with git's own refusal of a locked or unclean
//! one; a lock that appeared meanwhile is left alone and the worktree kept.
use super::{Query, Run, Script, MAIN};

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Params {
    /// The worktree's directory name.
    pub name: String,
    /// Named on the command line, rather than found by a sweep.
    pub named: bool,
    /// The session running this may judge its own agents' worktrees dead
    /// (abandon); prune never does, the session may yet resume them.
    pub own_session_ends: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Outcome {
    Removed,
    /// Why it was kept, a clause after "kept, ".
    Kept(String),
}

/// The claude session a lock names: Claude Code's and the create hook's
/// "... (pid P start S)", S the process's start time where /proc has one.
pub fn session(reason: &str) -> Option<(u32, Option<u64>)> {
    let digits = |s: &str| -> Option<(u64, usize)> {
        let n = s.bytes().take_while(u8::is_ascii_digit).count();
        (n > 0).then(|| s[..n].parse().ok().map(|v| (v, n)))?
    };
    let at = reason.match_indices("pid ").find(|(i, _)| *i == 0 || !reason.as_bytes()[i - 1].is_ascii_alphanumeric())?.0;
    let rest = &reason[at + 4..];
    let (pid, n) = digits(rest)?;
    let pid = u32::try_from(pid).ok()?;
    let start = rest[n..].strip_prefix(" start ").and_then(digits).map(|(s, _)| s);
    Some((pid, start))
}

fn kept<T>(why: impl Into<String>) -> Run<Result<T, Outcome>> {
    Ok(Err(Outcome::Kept(why.into())))
}

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    if !s.flag(Query::TakeDeployLock)? {
        return Ok(Outcome::Kept("a nixconvoy runs in it".into()));
    }
    let outcome = match judge(s, p)? {
        Err(o) => o,
        Ok((lock, wb, cur)) => remove(s, lock, &wb, cur)?,
    };
    s.tolerate(Query::ReleaseDeployLock)?;
    Ok(outcome)
}

/// The lock it holds, its work branch and its branch, or why it is kept.
type Judged = (Option<String>, String, Option<String>);

fn judge(s: &mut Script, p: &Params) -> Run<Result<Judged, Outcome>> {
    let lock = s.text(Query::Lock)?;
    match lock.as_deref() {
        None if !p.named => return kept(format!("unlocked: no session to outlive; `nixconvoy prune {}` judges it", p.name)),
        None => {}
        Some(reason) => match session(reason) {
            None => return kept(format!("locked by hand ({})", if reason.is_empty() { "no reason given" } else { reason })),
            Some((pid, start)) => {
                if s.flag(Query::Alive { pid, start, own_ends: p.own_session_ends })? {
                    return kept("the claude session that spawned its agent still runs");
                }
            }
        },
    }
    if let Some(op) = s.text(Query::OpInProgress)? {
        return kept(format!("a {op} is in progress"));
    }
    if s.flag(Query::PushInFlight)? {
        return kept("a push of its work branch is in flight");
    }
    if s.flag(Query::Nested)? {
        return kept("another worktree lives inside it");
    }
    let cur = s.text(Query::CurrentBranch)?;
    let Some(wb) = s.text(Query::OwnBranch)? else {
        return kept("it owns no work branch");
    };
    if cur.as_deref() == Some(wb.as_str()) {
        return kept(if wb.ends_with(&format!("-{}", p.name)) {
            format!("on its work branch {wb}: land or abandon decides it")
        } else {
            format!("it has {wb}, another checkout's work branch, checked out")
        });
    }
    if let Err(detail) = s.ran(Query::Fetch)? {
        return kept(format!("origin could not be fetched to judge it:\n{}", detail.trim()));
    }
    if let Some(tip) = s.text(Query::Rev { name: format!("refs/remotes/origin/{wb}") })? {
        if !s.flag(Query::Contains { a: tip, b: MAIN.into() })? {
            return kept(format!("it is not on {wb}, which carries pushed increments; `git switch {wb}` there to discard them"));
        }
    }
    if let Some(local) = s.text(Query::Rev { name: format!("refs/heads/{wb}") })? {
        if s.flag(Query::Unpushed { sha: local })? {
            return kept(format!(
                "it owns {wb}, which carries commits not on origin (a deploy whose push failed?); push it or `git switch {wb}` there and abandon"
            ));
        }
    }
    if s.flag(Query::Dirty)? {
        return kept("it has uncommitted or untracked changes, which the removal would lose; commit or discard them (git status there)");
    }
    if !s.flag(Query::OnOrigin)? {
        return kept("it carries commits not on origin, which the removal would lose; push them (git push origin HEAD:main) or deploy from the work branch");
    }
    Ok(Ok((lock, wb, cur)))
}

fn remove(s: &mut Script, lock: Option<String>, wb: &str, cur: Option<String>) -> Run<Outcome> {
    if !s.flag(Query::ClaimLock { expect: lock })? {
        return Ok(Outcome::Kept("its lock changed meanwhile: a session took it over".into()));
    }
    if let Err(detail) = s.ran(Query::RemoveWorktree)? {
        s.tolerate(Query::RestoreLock)?;
        return Ok(Outcome::Kept(format!("git refused to remove it:\n{}", detail.trim())));
    }
    s.tolerate(Query::DropBranches { wb: wb.into(), cur })?;
    Ok(Outcome::Removed)
}

#[cfg(test)]
mod model;
#[cfg(test)]
mod tests;
