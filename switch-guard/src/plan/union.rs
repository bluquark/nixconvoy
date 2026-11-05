//! nixconvoy's union of the work branches (its steps 3-4 and the retries
//! around 5-7): every other listed work branch on origin that still
//! declares changes is merged onto this checkout's snapshot, structural
//! conflicts resolved by mergiraf and COMMIT_MSG.md dropped, into a
//! deterministic commit on origin/main - so the eval memo stays valid
//! across retries and no-op redeploys.
//!
//! The union is first built from the previous fetch's refs, while this
//! run's fetch is still in flight; once it lands, a moved origin/main or
//! work branch redoes the union (and so does a conflict, which the fetch
//! may have resolved).  Then, before anything is evaluated, a deploy under
//! way elsewhere whose increment reaches a target this checkout's work
//! reaches too is waited for (Query::AwaitPending), and the union redone
//! with what it pushed: building without it would only end refused, or
//! rebuilt, once it switched.  A work branch that moves while the union
//! builds would be reverted on its targets by switching without it, so
//! the build is redone then too; ATTEMPTS such moves of origin end the
//! run (a wait is another deploy's progress, not a move, and counts for
//! nothing).
//!
//! That union holds every other branch at its tip.  A target of the deploy
//! takes less of a branch when its deploy record holds an older commit of
//! it (or none) and the work past that commit is something the target's
//! evaluation reads (Query::Cuts): that branch's own checkout has not
//! deployed it there, and no other checkout's deploy rolls it out.  The
//! targets taking the same commits of the branches share a lane, a union
//! of its own built the same way from those commits, staged beside the
//! first; a lane that does not merge ends the run - or, for a main
//! checkout, whose work is main's advance, leaves its targets out of the
//! deploy (a Lane with no union).
use super::{failed, listed, Halt, Names, Query, Run, Script, BRANCHES_KEY, MSG_FILE};
use crate::text;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const ATTEMPTS: usize = 3;
/// Deploys waited for in one run before giving up: each wait ends with
/// another deploy's progress, so this many means something is wrong.
pub const WAITS: usize = 10;

#[derive(Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct Params {
    pub names: Names,
    /// This checkout's work branch.
    pub wb: String,
    /// --evict: no other branch.
    pub evict: bool,
    /// origin/main before this run's fetch.
    pub main0: Option<String>,
}

#[derive(Serialize, Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    pub union: String,
    /// The branches merged into it and their tips.
    pub branches: Vec<(String, String)>,
}

/// A union of its own for `targets`: `branches`, each at the commit those
/// targets take of it.  No `union`: they did not merge, and the targets are
/// left out of the deploy.
#[derive(Serialize, Clone, Debug, PartialEq, Eq, Hash)]
pub struct Lane {
    pub targets: Vec<String>,
    pub union: String,
    pub branches: Vec<(String, String)>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Conflict {
    name: String,
    sha: String,
    files: Vec<String>,
}

/// What the passes over origin's branches carry between them: the
/// work branches' shapes the last one read, and the unlisted branches
/// already named.
struct Seen {
    listed: Vec<String>,
    warned: BTreeSet<String>,
}

/// The other listed branches on origin that still declare changes not in
/// HEAD.  One not shaped so is skipped aloud: the checked-in shape, not
/// the branch name, is the credential.  The shapes are read on every pass, since
/// a refresh may bring a new one; the stage's drift verdicts get the same.
fn select(s: &mut Script, p: &Params, seen: &mut Seen) -> Run<Vec<(String, String)>> {
    seen.listed = s.names(Query::Listed)?;
    if p.evict {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    for (name, sha) in s.refs(Query::Branches)? {
        if name == p.wb || s.flag(Query::Contains { a: sha.clone(), b: "HEAD".into() })? {
            continue;
        }
        if !listed(seen.listed.iter().map(String::as_str), &name) {
            if seen.warned.insert(name.clone()) {
                s.warn(format!(text!(union_unlisted), remote = p.names.remote, name = name, BRANCHES_KEY = BRANCHES_KEY));
            }
            continue;
        }
        // Everything it had was squashed or committed: nothing to union.
        if s.flag(Query::Unchanged { sha: sha.clone() })? {
            continue;
        }
        out.push((name, sha));
    }
    Ok(out)
}

fn build(s: &mut Script, branches: &[(String, String)]) -> Run<Result<String, Conflict>> {
    for (name, sha) in branches {
        let summary = s.text(Query::Summary { sha: sha.clone() })?.unwrap_or_default();
        s.print(format!(text!(union_merging), name = name, summary = summary));
        let Err((files, detail)) = s.merge(Query::Merge { name: name.clone(), sha: sha.clone() })? else {
            continue;
        };
        if files.is_empty() {
            return Err(Halt::Die(failed(&format!(text!(union_merge_what), name = name), &detail)));
        }
        let mut unresolved = Vec::new();
        for file in files {
            if file == MSG_FILE {
                s.must(Query::DropFile { file }, &format!(text!(union_dropping_msg_file), MSG_FILE = MSG_FILE))?;
            } else if s.flag(Query::Solve { file: file.clone() })? {
                s.must(Query::Add { file: file.clone() }, &format!(text!(union_adding), file = file))?;
            } else {
                unresolved.push(file);
            }
        }
        if !unresolved.is_empty() {
            s.tolerate(Query::AbortMerge)?;
            return Ok(Err(Conflict { name: name.clone(), sha: sha.clone(), files: unresolved }));
        }
        s.must(Query::CommitMerge, &format!(text!(union_committing_with), name = name))?;
    }
    let Some(tree) = s.text(Query::StripTree)? else {
        return Err(Halt::Die(text!(union_tree_failed).into()));
    };
    let as_is = branches.is_empty() && s.text(Query::HeadTree)?.as_ref() == Some(&tree);
    let union = if as_is { s.text(Query::Head)? } else { s.text(Query::CommitUnion { tree })? };
    let Some(union) = union else {
        return Err(Halt::Die(text!(union_commit_failed).into()));
    };
    s.must(Query::Checkout { union: union.clone() }, text!(union_checking_out))?;
    Ok(Ok(union))
}

fn guidance(s: &mut Script, n: &Names, c: &Conflict) -> Run<String> {
    let summary = s.text(Query::Summary { sha: c.sha.clone() })?.unwrap_or_default();
    let name = &c.name;
    Ok(format!(
        text!(union_conflict),
        name = name,
        files = c.files.join(", "),
        summary = summary,
        remote = n.remote,
        main_short = n.main_short()
    ))
}

/// Branches, each at a commit of it.
type Taken = Vec<(String, String)>;

/// The lanes `cuts` (Query::Cuts) call for among `branches` (their tips):
/// each lane's targets and the branches at the commits they take, a branch
/// they take nothing of left out; a cut of a branch not among `branches`
/// counts for nothing, and a target taking every tip has no lane.
pub fn lanes_of(branches: &[(String, String)], cuts: &[(String, String, String)]) -> Vec<(Vec<String>, Taken)> {
    let mut by_target: BTreeMap<&str, BTreeMap<&str, &str>> = BTreeMap::new();
    for (target, branch, commit) in cuts {
        by_target.entry(target).or_default().insert(branch, commit);
    }
    let mut by_set: BTreeMap<Taken, Vec<String>> = BTreeMap::new();
    for (target, cut) in by_target {
        let set: Taken = branches
            .iter()
            .filter_map(|(name, tip)| match cut.get(name.as_str()) {
                None => Some((name.clone(), tip.clone())),
                Some(&"") => None,
                Some(c) => Some((name.clone(), c.to_string())),
            })
            .collect();
        if set != branches {
            by_set.entry(set).or_default().push(target.to_string());
        }
    }
    by_set.into_iter().map(|(set, targets)| (targets, set)).collect()
}

fn lane_guidance(targets: &[String], c: &Conflict) -> String {
    format!(
        text!(union_lane_conflict),
        name = c.name,
        targets = targets.join(", "),
        run = if targets.len() == 1 { "runs" } else { "run" },
        short = c.sha.chars().take(12).collect::<String>(),
        files = c.files.join(", "),
        args = targets.join(" ")
    )
}

/// Collects this run's fetch, once: whether it moved origin/main or a work
/// branch, in which case the checkout is brought onto it first.
fn origin_moved(s: &mut Script, p: &Params, branches: &[(String, String)], fetched: &mut bool, seen: &mut Seen) -> Run<bool> {
    if *fetched {
        return Ok(false);
    }
    *fetched = true;
    s.mark("fetch");
    s.must(Query::CollectFetch, &format!(text!(union_fetching), remote = p.names.remote))?;
    if s.text(Query::Rev { name: p.names.main_ref() })? == p.main0 && select(s, p, seen)? == branches {
        return Ok(false);
    }
    s.print(format!(text!(union_origin_moved), remote = p.names.remote));
    s.must(Query::Refresh, &format!(text!(union_refreshing), main_short = p.names.main_short()))?;
    Ok(true)
}

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    let mut fetched = false;
    let mut seen = Seen { listed: Vec::new(), warned: BTreeSet::new() };
    let mut moves = 0;
    let mut waits = 0;
    while moves < ATTEMPTS {
        s.mark("union");
        let branches = select(s, p, &mut seen)?;
        s.must(Query::Worktree, text!(union_creating_worktree))?;
        let union = match build(s, &branches)? {
            Ok(union) => union,
            Err(c) => {
                if origin_moved(s, p, &branches, &mut fetched, &mut seen)? {
                    moves += 1;
                    continue;
                }
                return Err(Halt::Die(guidance(s, &p.names, &c)?));
            }
        };
        if origin_moved(s, p, &branches, &mut fetched, &mut seen)? {
            moves += 1;
            continue;
        }
        if !p.evict && s.flag(Query::AwaitPending { union: union.clone() })? {
            waits += 1;
            if waits > WAITS {
                return Err(Halt::Die(format!(text!(union_waited_too_often), WAITS = WAITS)));
            }
            s.print(text!(union_redo_after_wait));
            s.must(Query::Refresh, &format!(text!(union_refreshing), main_short = p.names.main_short()))?;
            continue;
        }
        let mut lanes = Vec::new();
        if !branches.is_empty() {
            let cuts = s.cuts(Query::Cuts { union: union.clone(), branches: branches.clone() })?;
            for (targets, taken) in lanes_of(&branches, &cuts) {
                s.print(format!(text!(union_lane), targets = targets.join(", ")));
                s.must(Query::LaneWorktree, text!(union_creating_worktree))?;
                match build(s, &taken)? {
                    Ok(union) => lanes.push(Lane { targets, union, branches: taken }),
                    Err(c) if p.wb.is_empty() => {
                        s.warn(format!(text!(union_lane_left), targets = targets.join(", "), why = lane_guidance(&targets, &c)));
                        lanes.push(Lane { targets, union: String::new(), branches: taken });
                    }
                    Err(c) => return Err(Halt::Die(lane_guidance(&targets, &c))),
                }
            }
        }
        let stage = Query::Stage { union: union.clone(), listed: seen.listed.clone(), branches: branches.clone(), lanes };
        if s.flag(stage)? || select(s, p, &mut seen)? == branches {
            return Ok(Outcome { union, branches });
        }
        s.print(text!(union_moved_during_build));
        moves += 1;
    }
    Err(Halt::Die(format!(text!(union_kept_moving), remote = p.names.remote, ATTEMPTS = ATTEMPTS)))
}

#[cfg(test)]
mod tests;
