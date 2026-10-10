//! Whether a subagent worktree (<checkout>/.claude/worktrees/<name>) is
//! removed, and its removal.  Claude Code never removes the worktrees
//! nixconvoy's WorktreeCreate hook makes, and an agent can be resumed while
//! the session that spawned it runs, which needs its worktree; so a
//! worktree goes only once the session its lock names has exited, or its
//! agent stopped and neither a resume relocked it nor anything woke the
//! agent since, and only when it holds nothing of its own.  A sweep (no
//! name given) keeps an unlocked one, which no session's exit tells the
//! end of.

//!
//! A live session or a hand lock is told before origin is fetched, which
//! may be offline; every check after the fetch is taken under the
//! worktree's name lock - a file in the common git dir that the create hook
//! also holds while it adds or reuses a worktree of that name, as do the
//! other removals - and under its deploy lock, so neither a create
//! nor a deploy moves it meanwhile; the agent's session is dead, so its
//! agent does not either.  `git worktree remove` checks the lock, then
//! cleanliness, then deletes the tree and the admin directory by name, so
//! without the name lock a reuse could relock and hand out the worktree
//! after git's check, or a fresh add recreate the admin directory that
//! git then deletes.  The removal claims the lock it judged - renames it
//! aside, only if it still holds that reason - and leaves git to refuse a
//! locked or unclean worktree; a process working in it without its lock
//! (a shell cd'd there) keeps it too.
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
    /// A stop's (`prune --stopped`).
    pub stopped: Option<Stopped>,
}

/// The session whose agent stopped, by pid, and the worktree's lock then:
/// that session counts as ended for it while the lock is still that one.
/// A resume relocks it, and each lock the create hook writes naming a
/// session carries a token of its own.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Stopped {
    pub pid: u32,
    pub lock: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Outcome {
    Removed,
    /// Why it was kept, a clause after "kept, ".
    Kept(String),
}

/// The claude session a lock names: Claude Code's and the create hook's
/// "... (pid P start S)", S the process's start time where /proc has one.
/// As nixconvoy's lock_pid_alive reads it: `\bpid (\d+)\b(?: start (\d+)\b)?`.
pub fn session(reason: &str) -> Option<(u32, Option<u64>)> {
    let word = |c: u8| c.is_ascii_alphanumeric() || c == b'_';
    let b = reason.as_bytes();
    let number = |at: usize| -> Option<(u64, usize)> {
        let n = b[at..].iter().take_while(|c| c.is_ascii_digit()).count();
        if n == 0 || b.get(at + n).is_some_and(|&c| word(c)) {
            return None;
        }
        Some((reason[at..at + n].parse().ok()?, at + n))
    };
    let (pid, end) = reason.match_indices("pid ").filter(|(i, _)| *i == 0 || !word(b[i - 1])).find_map(|(i, _)| number(i + 4))?;
    let pid = u32::try_from(pid).ok()?;
    let start = reason[end..].strip_prefix(" start ").and_then(|_| number(end + 7)).map(|(s, _)| s);
    Some((pid, start))
}

/// The create hook's lock when it found no claude session to name.
pub const SESSIONLESS: &str = "(no claude session found)";

fn kept<T>(why: impl Into<String>) -> Run<Result<T, Outcome>> {
    Ok(Err(Outcome::Kept(why.into())))
}

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    if let Err(o) = holder(s, p)? {
        return Ok(o);
    }
    // Before any lock: a slow fetch holds up no create of this name.
    if let Err(detail) = s.ran(Query::Fetch)? {
        return Ok(Outcome::Kept(format!("origin could not be fetched to judge it:\n{}", detail.trim())));
    }
    if !s.flag(Query::TakeNameLock)? {
        return Ok(Outcome::Kept("its name is locked: a create or another removal is at it; re-run".into()));
    }
    let outcome = if s.flag(Query::TakeDeployLock)? {
        let o = match judge(s, p)? {
            Err(o) => o,
            Ok((lock, wb, cur)) => remove(s, p, lock, &wb, cur)?,
        };
        s.tolerate(Query::ReleaseDeployLock)?;
        o
    } else {
        Outcome::Kept("a nixconvoy runs in it".into())
    };
    s.tolerate(Query::ReleaseNameLock)?;
    Ok(outcome)
}

/// The lock it holds, or why it is kept: a session it outlives, or none.
fn holder(s: &mut Script, p: &Params) -> Run<Result<Option<String>, Outcome>> {
    let lock = s.text(Query::Lock)?;
    match lock.as_deref() {
        None if !p.named => return kept(format!("unlocked: no session to outlive; `nixconvoy prune {}` judges it", p.name)),
        None => {}
        // Abandon ends the session that would have unlocked it.
        Some(reason) if p.own_session_ends && reason.ends_with(SESSIONLESS) => {}
        Some(reason) => match session(reason) {
            None if reason.ends_with(SESSIONLESS) => {
                return kept(format!(
                    "its create found no claude session to outlive: `nixconvoy abandon {}`, or `git worktree unlock` it to let prune judge it",
                    p.name
                ));
            }
            None => {
                let why = if reason.is_empty() { "no reason given" } else { reason };
                return kept(format!("locked by hand ({why}): `git worktree unlock` it to let prune judge it"));
            }
            Some((pid, start)) => {
                let ended = p.stopped.as_ref().is_some_and(|st| st.pid == pid && st.lock.as_deref() == Some(reason));
                if !ended && s.flag(Query::Alive { pid, start, own_ends: p.own_session_ends })? {
                    return kept("the claude session that spawned its agent still runs");
                }
            }
        },
    }
    Ok(Ok(lock))
}

/// The lock it holds, its work branch and its branch, or why it is kept.
type Judged = (Option<String>, String, Option<String>);

fn judge(s: &mut Script, p: &Params) -> Run<Result<Judged, Outcome>> {
    let lock = match holder(s, p)? {
        Ok(lock) => lock,
        Err(o) => return Ok(Err(o)),
    };
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

fn remove(s: &mut Script, p: &Params, lock: Option<String>, wb: &str, cur: Option<String>) -> Run<Outcome> {
    if !s.flag(Query::ClaimLock { expect: lock })? {
        return Ok(Outcome::Kept("its lock changed meanwhile: a session took it over".into()));
    }
    if s.flag(Query::Occupied)? {
        s.tolerate(Query::RestoreLock)?;
        return Ok(Outcome::Kept("a process works in it".into()));
    }
    // Woken past the resume hook, whose relock would have kept it.
    if p.stopped.is_some() && s.flag(Query::RanAgain)? {
        s.tolerate(Query::RestoreLock)?;
        return Ok(Outcome::Kept("its agent ran again since it stopped".into()));
    }

    if let Err(detail) = s.ran(Query::RemoveWorktree)? {
        s.tolerate(Query::RestoreLock)?;
        return Ok(Outcome::Kept(format!("git refused to remove it:\n{}", detail.trim())));
    }
    if let Err(detail) = s.ran(Query::DropBranches { wb: wb.into(), cur })? {
        s.warn(detail);
    }
    Ok(Outcome::Removed)
}

#[cfg(test)]
mod model;
#[cfg(test)]
mod tests;
