//! Whether a subagent worktree (one nested in a checkout, <checkout>/<worktrees>/<name>) is
//! removed, and its removal.  Claude Code never removes the worktrees
//! nixconvoy's WorktreeCreate hook makes, and an agent can be resumed while
//! the session that spawned it runs, which needs its worktree; so a
//! worktree goes only once the session its lock names has exited, or its
//! agent stopped and no resume relocked it since, and only when it holds
//! nothing of its own.  A sweep (no name given) keeps an unlocked one,
//! which no session's exit tells the end of.
//!
//! A live session or a hand lock is told before origin is fetched, which
//! may be offline; every check after the fetch is taken under the
//! worktree's name lock - a file in the common git dir that the create hook
//! (a create, a reuse, a resume's relock) also holds while it adds or
//! relocks a worktree of that name, as do the other removals - and under
//! its deploy lock, so neither a create nor a deploy moves it meanwhile;
//! the agent's session is dead, so its agent does not either.  The lock
//! judged under the name lock therefore stands until the removal, which
//! lifts it right before `git worktree remove` and puts it back when git
//! refuses; a process working in it without its lock (a shell cd'd there)
//! keeps it too.
use super::{Names, Query, Run, Script};
use crate::text;
use serde::{Deserialize, Serialize};

#[derive(Deserialize, Clone, Debug, PartialEq, Eq, Hash)]
pub struct Params {
    pub names: Names,
    /// The worktree's directory name.
    pub name: String,
    /// Named on the command line, rather than found by a sweep.
    pub named: bool,
    /// The session running this may judge its own agents' worktrees dead
    /// (a named close); prune never does, the session may yet resume them.
    pub own_session_ends: bool,
    /// A stop's (`hook subagent-stop`).
    pub stopped: Option<Stopped>,
}

/// The session whose agent stopped, by pid, and the worktree's lock then:
/// that session counts as ended for it while the lock is still that one.
/// A resume relocks it, and each lock the create hook writes naming a
/// session carries a token of its own.
#[derive(Deserialize, Clone, Debug, PartialEq, Eq, Hash)]
pub struct Stopped {
    pub pid: u32,
    pub lock: Option<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
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
        return Ok(Outcome::Kept(format!(text!(prune_fetch_failed), remote = p.names.remote, detail = detail.trim())));
    }
    if !s.flag(Query::TakeNameLock)? {
        return Ok(Outcome::Kept(text!(prune_name_locked).into()));
    }
    let outcome = if s.flag(Query::TakeDeployLock)? {
        let o = match judge(s, p)? {
            Err(o) => o,
            Ok((wb, cur)) => remove(s, &wb, cur)?,
        };
        s.tolerate(Query::ReleaseDeployLock)?;
        o
    } else {
        Outcome::Kept(text!(prune_deploy_lock_held).into())
    };
    s.tolerate(Query::ReleaseNameLock)?;
    Ok(outcome)
}

/// Why it is kept for its lock, if it is: a session it does not outlive,
/// or none to outlive.
fn holder(s: &mut Script, p: &Params) -> Run<Result<(), Outcome>> {
    let lock = s.text(Query::Lock)?;
    match lock.as_deref() {
        None if !p.named => return kept(format!(text!(prune_unlocked), name = p.name)),
        None => {}
        // Undeploy ends the session that would have unlocked it.
        Some(reason) if p.own_session_ends && reason.ends_with(SESSIONLESS) => {}
        Some(reason) => match session(reason) {
            None if reason.ends_with(SESSIONLESS) => {
                return kept(format!(text!(prune_sessionless), name = p.name, main_short = p.names.main_short()));
            }
            None => {
                let why = if reason.is_empty() { text!(prune_no_reason) } else { reason };
                return kept(format!(text!(prune_hand_lock), why = why));
            }
            Some((pid, start)) => {
                let ended = p.stopped.as_ref().is_some_and(|st| st.pid == pid && st.lock.as_deref() == Some(reason));
                if !ended && s.flag(Query::Alive { pid, start, own_ends: p.own_session_ends })? {
                    return kept(text!(prune_session_runs));
                }
            }
        },
    }
    Ok(Ok(()))
}

/// Its work branch and its branch, or why it is kept.
fn judge(s: &mut Script, p: &Params) -> Run<Result<(String, Option<String>), Outcome>> {
    if let Err(o) = holder(s, p)? {
        return Ok(Err(o));
    }
    if let Some(op) = s.text(Query::OpInProgress)? {
        return kept(format!(text!(prune_op_in_progress), op = op));
    }
    if s.flag(Query::Nested)? {
        return kept(text!(prune_nested));
    }
    let cur = s.text(Query::CurrentBranch)?;
    let Some(wb) = s.text(Query::OwnBranch)? else {
        return kept(text!(prune_no_work_branch));
    };
    // On its own work branch it is judged like any checkout: idle once the
    // branch is reset onto main (a land, or an undeploy back to it), holding work before.
    let on_wb = cur.as_deref() == Some(wb.as_str());
    if on_wb && !wb.ends_with(&format!("-{}", p.name)) {
        return kept(format!(text!(prune_foreign_branch), wb = wb));
    }
    if let Some(tip) = s.text(Query::Rev { name: p.names.remote_ref(&wb) })? {
        if !s.flag(Query::Contains { a: tip, b: p.names.main_ref() })? {
            return kept(if on_wb {
                format!(text!(prune_pushed_increments), wb = wb)
            } else {
                format!(text!(prune_off_pushed_branch), wb = wb)
            });
        }
    }
    if let Some(local) = s.text(Query::Rev { name: format!("refs/heads/{wb}") })? {
        if s.flag(Query::Unpushed { sha: local })? {
            return kept(format!(text!(prune_unpushed), wb = wb, remote = p.names.remote, main_short = p.names.main_short()));
        }
    }
    if s.flag(Query::Dirty)? {
        return kept(text!(prune_dirty));
    }
    if !s.flag(Query::OnOrigin)? {
        return kept(format!(text!(prune_not_on_origin), remote = p.names.remote, main = p.names.main));
    }
    Ok(Ok((wb, cur)))
}

fn remove(s: &mut Script, wb: &str, cur: Option<String>) -> Run<Outcome> {
    if s.flag(Query::Occupied)? {
        return Ok(Outcome::Kept(text!(prune_occupied).into()));
    }
    if let Err(detail) = s.ran(Query::RemoveWorktree)? {
        return Ok(Outcome::Kept(format!(text!(prune_remove_refused), detail = detail.trim())));
    }
    if let Err(detail) = s.ran(Query::DropBranches { wb: wb.into(), cur })? {
        s.warn(detail);
    }
    Ok(Outcome::Removed)
}

#[cfg(test)]
mod tests;
