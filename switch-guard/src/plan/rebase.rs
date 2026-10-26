//! How nixconvoy brings a checkout onto origin/main and onto its work
//! branch.  A detached HEAD (made by hand) is moved along when it is
//! behind origin/main; one at commits already on origin - a review
//! subagent's checkout of a work branch's tip, which stays where the
//! review reads it - is left alone; one carrying commits of its own is
//! refused, since switching to the work branch would strand them.
//!
//! The same routine (`onto`) brings a submodule worktree onto its repository's
//! default branch, under the Onto params: nixconvoy runs it in that
//! worktree, naming the upstream.
//!
//! The checked-out branch is rebased with its staged and unstaged/untracked
//! changes riding along as two tmp commits, so they get the same merge
//! machinery (mergiraf) as real commits; two resets then restore the exact
//! split.  That rebase keeps commits that become empty: a tmp commit
//! already in main must not be dropped, or the resets would pop the wrong
//! commits.  A rebase that isn't clean is aborted and the split restored,
//! the checkout left exactly as found.  A run that dies between the rebase
//! and the restore leaves the tmp commits behind (never pushed); the next
//! run pops them first.
use super::{failed, no_hints, Halt, Query, Run, Script, MAIN};
use serde::Deserialize;

pub const TMP_STAGED: &str = "tmp: staged (ensure_rebased)";
pub const TMP_UNSTAGED: &str = "tmp: unstaged (ensure_rebased)";

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Params {
    /// Unwind a dead run's tmp commits.
    PopTmp,
    /// Make HEAD contain origin/main, dying when that isn't clean.
    EnsureRebased,
    /// Make HEAD contain `upstream` (a submodule worktree, its repository's
    /// default branch as fetched), a dead run's tmp commits unwound first;
    /// Err says why it was left as it was.
    Onto { upstream: String },
    /// Fetch, then bring the checkout and its work branch (None: the main
    /// checkout has none) onto origin/main and push the branch.
    Sync { wb: Option<String> },
    /// Leave the checkout on its work branch, coming from main, from a
    /// detached HEAD or from a branch with nothing of its own.
    OnWorkBranch { wb: String },
}

/// What happened, or (Err) why the checkout was left as it was.
pub type Outcome = Result<String, String>;

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    match p {
        Params::PopTmp => pop_tmp(s).map(|()| Ok(String::new())),
        Params::EnsureRebased => match onto_main(s)? {
            Ok(status) if status == LEFT => Err(Halt::Die(format!("{LEFT}, not on origin/main; switch to a branch to deploy"))),
            Ok(status) => {
                if status != "up to date" {
                    s.print(status.clone());
                }
                Ok(Ok(status))
            }
            Err(e) => Err(Halt::Die(e)),
        },
        Params::Sync { wb } => sync(s, wb.as_deref()),
        Params::Onto { upstream } => {
            pop_tmp(s)?;
            onto(s, upstream)
        }
        Params::OnWorkBranch { wb } => on_work_branch(s, wb).map(|()| Ok(String::new())),
    }
}

fn branch(s: &mut Script) -> Run<Option<String>> {
    s.text(Query::CurrentBranch)
}

fn contains(s: &mut Script, a: &str, b: &str) -> Run<bool> {
    s.flag(Query::Contains { a: a.into(), b: b.into() })
}

fn rev(s: &mut Script, name: String) -> Run<Option<String>> {
    s.text(Query::Rev { name })
}

pub fn pop_tmp(s: &mut Script) -> Run<()> {
    loop {
        let subject = s.text(Query::HeadSubject)?.unwrap_or_default();
        let mixed = match subject.as_str() {
            TMP_UNSTAGED => true,
            TMP_STAGED => false,
            _ => return Ok(()),
        };
        s.must(Query::Pop { mixed }, "unwinding a dead run's tmp commit")?;
    }
}

fn verb(ff: bool) -> &'static str {
    if ff {
        "fast-forwarded"
    } else {
        "rebased"
    }
}

/// `upstream` as a person names it: origin/main.
fn short(upstream: &str) -> &str {
    upstream.strip_prefix("refs/remotes/").unwrap_or(upstream)
}

pub fn onto_main(s: &mut Script) -> Run<Outcome> {
    onto(s, MAIN)
}

pub fn onto(s: &mut Script, upstream: &str) -> Run<Outcome> {
    let up = short(upstream);
    if contains(s, upstream, "HEAD")? {
        return Ok(Ok("up to date".into()));
    }
    if let Some(op) = s.text(Query::OpInProgress)? {
        return Ok(Err(format!("a {op} is in progress; finish or abort it first")));
    }
    let Some(br) = branch(s)? else {
        return detached_onto(s, upstream);
    };
    let ff = contains(s, "HEAD", upstream)?;
    let staged = s.flag(Query::StagedDiffers)?;
    if staged {
        s.must(Query::Commit { msg: TMP_STAGED.into() }, "committing the staged changes aside")?;
    }
    s.must(Query::AddAll, "staging the unstaged changes aside")?;
    let unstaged = s.flag(Query::StagedDiffers)?;
    if unstaged {
        s.must(Query::Commit { msg: TMP_UNSTAGED.into() }, "committing the unstaged changes aside")?;
    }
    let rebased = s.ran(Query::Rebase { keep_empty: true, aside: false, upstream: upstream.into() })?;
    if rebased.is_err() {
        s.tolerate(Query::RebaseAbort { aside: false })?;
    }
    if unstaged {
        s.must(Query::Pop { mixed: true }, "restoring the unstaged changes")?;
    }
    if staged {
        s.must(Query::Pop { mixed: false }, "restoring the staged changes")?;
    }
    Ok(match rebased {
        Ok(()) => Ok(format!("{} {br} onto {up}", verb(ff))),
        Err(detail) => Err(format!("rebasing {br} onto {up} is not clean, left as it was: {}", no_hints(&detail))),
    })
}

pub const LEFT: &str = "detached HEAD left as is: nothing of its own, its commits are already on origin";

/// Why a detached HEAD with commits of its own is left alone.
fn stranded(upstream: &str) -> String {
    let up = short(upstream);
    let branch = up.split_once('/').map_or(up, |(_, b)| b);
    format!("the detached HEAD carries commits not on {up}; push them (git push origin HEAD:{branch}) or put them on a branch, then re-run")
}

/// A detached HEAD behind `upstream` moved onto it; one at commits that
/// are on origin anyway left where it is.
fn detached_onto(s: &mut Script, upstream: &str) -> Run<Outcome> {
    let up = short(upstream);
    if !contains(s, "HEAD", upstream)? {
        return Ok(if s.flag(Query::OnOrigin)? { Ok(LEFT.into()) } else { Err(stranded(upstream)) });
    }
    Ok(match s.ran(Query::Detach { upstream: upstream.into() })? {
        Ok(()) => Ok(format!("fast-forwarded the detached HEAD onto {up}")),
        Err(detail) => Err(format!("cannot move the detached HEAD onto {up}, left as it was:\n{}", no_hints(&detail))),
    })
}

/// The local branch `wb`, not checked out here, onto origin/main in a
/// throwaway worktree: no tmp commits to protect, so commits that become
/// empty are dropped.
pub fn aside(s: &mut Script, wb: &str) -> Run<Outcome> {
    let local = format!("refs/heads/{wb}");
    if contains(s, MAIN, &local)? {
        return Ok(Ok("up to date".into()));
    }
    let ff = contains(s, &local, MAIN)?;
    let result = match s.ran(Query::AddWorktree { branch: wb.into() })? {
        Err(detail) => Err(format!("cannot check out {wb} to rebase it:\n{}", detail.trim())),
        Ok(()) => match s.ran(Query::Rebase { keep_empty: false, aside: true, upstream: MAIN.into() })? {
            Ok(()) => Ok(format!("{} {wb} onto origin/main", verb(ff))),
            Err(detail) => {
                s.tolerate(Query::RebaseAbort { aside: true })?;
                Err(format!("rebasing {wb} onto origin/main is not clean, left as it was: {}", no_hints(&detail)))
            }
        },
    };
    s.tolerate(Query::DropWorktree)?;
    Ok(result)
}

fn sync(s: &mut Script, wb: Option<&str>) -> Run<Outcome> {
    if let Err(detail) = s.ran(Query::Fetch)? {
        return Ok(Err(failed("fetch", &detail)));
    }
    pop_tmp(s)?;
    let mut done = vec![match onto_main(s)? {
        Ok(d) => d,
        Err(e) => return Ok(Err(e)),
    }];
    let Some(wb) = wb else {
        return Ok(Ok(done.remove(0)));
    };
    let local = format!("refs/heads/{wb}");
    if rev(s, local.clone())?.is_none() {
        return Ok(Ok(done.remove(0)));
    }
    if branch(s)?.as_deref() != Some(wb) {
        match aside(s, wb)? {
            Ok(d) => done.push(d),
            Err(e) => return Ok(Err(e)),
        }
    }
    if rev(s, local)? != rev(s, format!("refs/remotes/origin/{wb}"))? {
        if let Err(detail) = s.ran(Query::Push { branch: wb.into() })? {
            return Ok(Err(format!("pushing {wb} failed:\n{}", detail.trim())));
        }
        done.push(format!("pushed {wb}"));
    }
    let steps: Vec<String> = done.into_iter().filter(|d| d != "up to date").collect();
    Ok(Ok(if steps.is_empty() { "up to date".into() } else { steps.join("; ") }))
}

/// Plain `git pull --rebase` on the work branch then means the same
/// catch-up deploys do.  Pushes are unaffected: each names its refspec.
fn ensure_upstream(s: &mut Script, wb: &str) -> Run<()> {
    if s.text(Query::Upstream { branch: wb.into() })?.as_deref() != Some("refs/heads/main") {
        s.must(Query::SetUpstream { branch: wb.into() }, &format!("setting {wb}'s upstream"))?;
    }
    Ok(())
}

fn on_work_branch(s: &mut Script, wb: &str) -> Run<()> {
    let cur = branch(s)?;
    if cur.as_deref() == Some(wb) {
        return ensure_upstream(s, wb);
    }
    // Nothing of its own to strand: detached at or behind origin/main, or
    // the fresh branch a subagent's worktree was created with - at its
    // parent's tip, whose increments are pushed on the parent's branch.
    let safe = cur.as_deref() != Some("main") && (contains(s, "HEAD", MAIN)? || s.flag(Query::Pushed)?);
    match cur.as_deref() {
        Some("main") => {}
        _ if safe => {}
        None => return Err(Halt::Die(stranded(MAIN))),
        Some(other) => {
            return Err(Halt::Die(format!(
                "on branch {other}, which carries commits not on origin/main; deploys run from {wb} or from main"
            )));
        }
    }
    let start = if rev(s, format!("refs/heads/{wb}"))?.is_some() {
        None
    } else {
        // Recover the branch from origin if an earlier clone pushed one.
        let remote = format!("refs/remotes/origin/{wb}");
        Some(if rev(s, remote.clone())?.is_some() { remote } else { "HEAD".into() })
    };
    if let Err(detail) = s.ran(Query::Switch { branch: wb.into(), start })? {
        return Err(Halt::Die(format!(
            "cannot switch to {wb} with the current staged/working-tree changes:\n{}\nresolve by hand, then re-run",
            detail.trim()
        )));
    }
    ensure_upstream(s, wb)?;
    s.print(format!("switched to work branch {wb}"));
    Ok(())
}

#[cfg(test)]
mod tests;
