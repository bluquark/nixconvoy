//! How nixconvoy brings a checkout onto origin/main and onto its feature
//! branch.  A detached HEAD (made by hand) is moved along when it is
//! behind origin/main; one at commits already on origin - a review
//! subagent's checkout of a feature branch's tip, which stays where the
//! review reads it - is left alone; one carrying commits of its own is
//! refused, since switching to the feature branch would strand them.
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
use super::{failed, no_hints, Halt, Names, Query, Run, Script};
use crate::text;
use serde::Deserialize;

pub const TMP_STAGED: &str = "tmp: staged (ensure_rebased)";
pub const TMP_UNSTAGED: &str = "tmp: unstaged (ensure_rebased)";

#[derive(Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct Params {
    pub names: Names,
    pub op: Op,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Op {
    /// Unwind a dead run's tmp commits.
    PopTmp,
    /// Make HEAD contain origin/main, dying when that isn't clean.
    EnsureRebased,
    /// Make HEAD contain `upstream` (a submodule worktree, its repository's
    /// default branch as fetched), a dead run's tmp commits unwound first;
    /// Err says why it was left as it was.
    Onto { upstream: String },
    /// Fetch, then bring the checkout and its feature branch (None: the main
    /// checkout has none) onto origin/main and push the branch.
    Sync { fb: Option<String> },
    /// Leave the checkout on its feature branch, coming from main, from a
    /// detached HEAD or from a branch with nothing of its own.
    OnFeatureBranch { fb: String },
}

/// What happened, or (Err) why the checkout was left as it was.
pub type Outcome = Result<String, String>;

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    let n = &p.names;
    match &p.op {
        Op::PopTmp => pop_tmp(s).map(|()| Ok(String::new())),
        Op::EnsureRebased => match onto_main(s, n)? {
            Ok(status) if status == LEFT => Err(Halt::Die(format!(text!(rebase_not_on_main), LEFT = LEFT, main = n.main_short()))),
            Ok(status) => {
                if status != text!(rebase_up_to_date) {
                    s.print(status.clone());
                }
                Ok(Ok(status))
            }
            Err(e) => Err(Halt::Die(e)),
        },
        Op::Sync { fb } => sync(s, n, fb.as_deref()),
        Op::Onto { upstream } => {
            pop_tmp(s)?;
            onto(s, n, upstream)
        }
        Op::OnFeatureBranch { fb } => on_feature_branch(s, n, fb).map(|()| Ok(String::new())),
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
        s.must(Query::Pop { mixed }, text!(rebase_unwinding_tmp))?;
    }
}

fn moved(ff: bool, branch: &str, up: &str) -> String {
    if ff {
        format!(text!(rebase_fast_forwarded), branch = branch, up = up)
    } else {
        format!(text!(rebase_rebased), branch = branch, up = up)
    }
}

/// `upstream` as a person names it: origin/main.
fn short(upstream: &str) -> &str {
    upstream.strip_prefix("refs/remotes/").unwrap_or(upstream)
}

pub fn onto_main(s: &mut Script, n: &Names) -> Run<Outcome> {
    onto(s, n, &n.main_ref())
}

pub fn onto(s: &mut Script, n: &Names, upstream: &str) -> Run<Outcome> {
    let up = short(upstream);
    if contains(s, upstream, "HEAD")? {
        return Ok(Ok(text!(rebase_up_to_date).into()));
    }
    if let Some(op) = s.text(Query::OpInProgress)? {
        return Ok(Err(format!(text!(rebase_op_in_progress), op = op)));
    }
    let Some(br) = branch(s)? else {
        return detached_onto(s, n, upstream);
    };
    let ff = contains(s, "HEAD", upstream)?;
    let staged = s.flag(Query::StagedDiffers)?;
    if staged {
        s.must(Query::Commit { msg: TMP_STAGED.into() }, text!(rebase_committing_staged))?;
    }
    s.must(Query::AddAll, text!(rebase_staging_unstaged))?;
    let unstaged = s.flag(Query::StagedDiffers)?;
    if unstaged {
        s.must(Query::Commit { msg: TMP_UNSTAGED.into() }, text!(rebase_committing_unstaged))?;
    }
    let rebased = s.ran(Query::Rebase { keep_empty: true, aside: false, upstream: upstream.into() })?;
    if rebased.is_err() {
        s.tolerate(Query::RebaseAbort { aside: false })?;
    }
    if unstaged {
        s.must(Query::Pop { mixed: true }, text!(rebase_restoring_unstaged))?;
    }
    if staged {
        s.must(Query::Pop { mixed: false }, text!(rebase_restoring_staged))?;
    }
    Ok(match rebased {
        Ok(()) => Ok(moved(ff, &br, up)),
        Err(detail) => Err(format!(text!(rebase_not_clean), branch = br, up = up, detail = no_hints(&detail))),
    })
}

pub const LEFT: &str = text!(rebase_left);

/// Why a detached HEAD with commits of its own is left alone.
fn stranded(n: &Names, upstream: &str) -> String {
    let up = short(upstream);
    let branch = up.strip_prefix(&format!("{}/", n.remote)).unwrap_or(up);
    format!(text!(rebase_stranded), up = up, remote = n.remote, branch = branch)
}

/// A detached HEAD behind `upstream` moved onto it; one at commits that
/// are on the remote anyway left where it is.
fn detached_onto(s: &mut Script, n: &Names, upstream: &str) -> Run<Outcome> {
    let up = short(upstream);
    if !contains(s, "HEAD", upstream)? {
        return Ok(if s.flag(Query::OnOrigin)? { Ok(LEFT.into()) } else { Err(stranded(n, upstream)) });
    }
    Ok(match s.ran(Query::Detach { upstream: upstream.into() })? {
        Ok(()) => Ok(format!(text!(rebase_detached_moved), up = up)),
        Err(detail) => Err(format!(text!(rebase_detached_stuck), up = up, detail = no_hints(&detail))),
    })
}

/// The local branch `fb`, not checked out here, onto origin/main in a
/// throwaway worktree: no tmp commits to protect, so commits that become
/// empty are dropped.
pub fn aside(s: &mut Script, n: &Names, fb: &str) -> Run<Outcome> {
    let local = format!("refs/heads/{fb}");
    let main = n.main_ref();
    let up = n.main_short();
    if contains(s, &main, &local)? {
        return Ok(Ok(text!(rebase_up_to_date).into()));
    }
    let ff = contains(s, &local, &main)?;
    let result = match s.ran(Query::AddWorktree { branch: fb.into() })? {
        Err(detail) => Err(format!(text!(rebase_cannot_check_out), branch = fb, detail = detail.trim())),
        Ok(()) => match s.ran(Query::Rebase { keep_empty: false, aside: true, upstream: main })? {
            Ok(()) => Ok(moved(ff, fb, &up)),
            Err(detail) => {
                s.tolerate(Query::RebaseAbort { aside: true })?;
                Err(format!(text!(rebase_not_clean), branch = fb, up = up, detail = no_hints(&detail)))
            }
        },
    };
    s.tolerate(Query::DropWorktree)?;
    Ok(result)
}

fn sync(s: &mut Script, n: &Names, fb: Option<&str>) -> Run<Outcome> {
    if let Err(detail) = s.ran(Query::Fetch)? {
        return Ok(Err(failed(text!(rebase_fetch), &detail)));
    }
    pop_tmp(s)?;
    let mut done = vec![match onto_main(s, n)? {
        Ok(d) => d,
        Err(e) => return Ok(Err(e)),
    }];
    let Some(fb) = fb else {
        return Ok(Ok(done.remove(0)));
    };
    let local = format!("refs/heads/{fb}");
    if rev(s, local.clone())?.is_none() {
        return Ok(Ok(done.remove(0)));
    }
    if branch(s)?.as_deref() != Some(fb) {
        match aside(s, n, fb)? {
            Ok(d) => done.push(d),
            Err(e) => return Ok(Err(e)),
        }
    }
    if rev(s, local)? != rev(s, n.remote_ref(fb))? {
        if let Err(detail) = s.ran(Query::Push { branch: fb.into() })? {
            return Ok(Err(format!(text!(rebase_push_failed), branch = fb, detail = detail.trim())));
        }
        done.push(format!(text!(rebase_pushed), branch = fb));
    }
    let steps: Vec<String> = done.into_iter().filter(|d| d != text!(rebase_up_to_date)).collect();
    Ok(Ok(if steps.is_empty() { text!(rebase_up_to_date).into() } else { steps.join(text!(rebase_steps_joiner)) }))
}

/// Plain `git pull --rebase` on the feature branch then means the same
/// catch-up deploys do.  Pushes are unaffected: each names its refspec.
fn ensure_upstream(s: &mut Script, n: &Names, fb: &str) -> Run<()> {
    if s.text(Query::Upstream { branch: fb.into() })?.as_deref() != Some(format!("{}\trefs/heads/{}", n.remote, n.main).as_str()) {
        s.must(Query::SetUpstream { branch: fb.into() }, &format!(text!(rebase_setting_upstream), branch = fb))?;
    }
    Ok(())
}

fn on_feature_branch(s: &mut Script, n: &Names, fb: &str) -> Run<()> {
    let cur = branch(s)?;
    if cur.as_deref() == Some(fb) {
        return ensure_upstream(s, n, fb);
    }
    // Nothing of its own to strand: detached at or behind origin/main, or
    // the fresh branch a subagent's worktree was created with - at its
    // parent's tip, whose increments are pushed on the parent's branch.
    let main = n.main_ref();
    let safe = cur.as_deref() != Some(n.main.as_str()) && (contains(s, "HEAD", &main)? || s.flag(Query::Pushed)?);
    match cur.as_deref() {
        Some(b) if b == n.main => {}
        _ if safe => {}
        None => return Err(Halt::Die(stranded(n, &main))),
        Some(other) => {
            return Err(Halt::Die(format!(text!(rebase_other_branch), other = other, main_short = n.main_short(), fb = fb, main = n.main)));
        }
    }
    let start = if rev(s, format!("refs/heads/{fb}"))?.is_some() {
        None
    } else {
        // Recover the branch from the remote if an earlier clone pushed one.
        let remote = n.remote_ref(fb);
        Some(if rev(s, remote.clone())?.is_some() { remote } else { "HEAD".into() })
    };
    if let Err(detail) = s.ran(Query::Switch { branch: fb.into(), start })? {
        return Err(Halt::Die(format!(text!(rebase_switch_failed), fb = fb, detail = detail.trim())));
    }
    ensure_upstream(s, n, fb)?;
    s.print(format!(text!(rebase_switched), fb = fb));
    Ok(())
}

#[cfg(test)]
mod tests;
