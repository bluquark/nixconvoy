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
//! may have resolved).  A work branch that moves while the union builds
//! would be reverted on its hosts by switching without it, so the build is
//! redone then too, up to ATTEMPTS times.
use super::{failed, listed, Halt, Query, Run, Script, BRANCHES_FILE, MAIN, MSG_FILE};
use std::collections::BTreeSet;

pub const ATTEMPTS: usize = 3;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Params {
    /// This checkout's work branch.
    pub wb: String,
    /// --reset: no other branch.
    pub reset: bool,
    /// origin/main before this run's fetch.
    pub main0: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    pub union: String,
    /// The branches merged into it and their tips.
    pub branches: Vec<(String, String)>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Conflict {
    name: String,
    sha: String,
    files: Vec<String>,
}

/// What the passes over origin's branches carry between them: the
/// deploy-branches the last one read, and the unlisted branches already
/// named.
struct Seen {
    listed: Vec<String>,
    warned: BTreeSet<String>,
}

/// The other listed branches on origin that still declare changes not in
/// HEAD.  An unlisted one is skipped aloud: the checked-in patterns, not
/// the branch name, are the credential.  The list is read on every pass, since
/// a refresh may bring a new one; the stage's drift verdicts get the same.
fn select(s: &mut Script, p: &Params, seen: &mut Seen) -> Run<Vec<(String, String)>> {
    seen.listed = s.names(Query::Listed)?;
    if p.reset {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    for (name, sha) in s.refs(Query::Branches)? {
        if name == p.wb || s.flag(Query::Contains { a: sha.clone(), b: "HEAD".into() })? {
            continue;
        }
        if !listed(seen.listed.iter().map(String::as_str), &name) {
            if seen.warned.insert(name.clone()) {
                s.warn(format!("warning: ignoring origin/{name}: not listed in {BRANCHES_FILE}"));
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
        s.print(format!("union: merging {name}: {summary}"));
        let Err((files, detail)) = s.merge(Query::Merge { name: name.clone(), sha: sha.clone() })? else {
            continue;
        };
        if files.is_empty() {
            return Err(Halt::Die(failed(&format!("merging {name} into the union"), &detail)));
        }
        let mut unresolved = Vec::new();
        for file in files {
            if file == MSG_FILE {
                s.must(Query::DropFile { file }, &format!("dropping {MSG_FILE} from the union"))?;
            } else if s.flag(Query::Solve { file: file.clone() })? {
                s.must(Query::Add { file: file.clone() }, &format!("adding {file}"))?;
            } else {
                unresolved.push(file);
            }
        }
        if !unresolved.is_empty() {
            s.tolerate(Query::AbortMerge)?;
            return Ok(Err(Conflict { name: name.clone(), sha: sha.clone(), files: unresolved }));
        }
        s.must(Query::CommitMerge, &format!("committing the union with {name}"))?;
    }
    let Some(tree) = s.text(Query::StripTree)? else {
        return Err(Halt::Die("writing the union's tree failed".into()));
    };
    let as_is = branches.is_empty() && s.text(Query::HeadTree)?.as_ref() == Some(&tree);
    let union = if as_is { s.text(Query::Head)? } else { s.text(Query::CommitUnion { tree })? };
    let Some(union) = union else {
        return Err(Halt::Die("committing the union failed".into()));
    };
    s.must(Query::Checkout { union: union.clone() }, "checking out the union")?;
    Ok(Ok(union))
}

fn guidance(s: &mut Script, c: &Conflict) -> Run<String> {
    let summary = s.text(Query::Summary { sha: c.sha.clone() })?.unwrap_or_default();
    let name = &c.name;
    Ok(format!(
        "cannot union work branch {name}; mergiraf could not resolve:\n  {}\nits last deploy: {summary}\noptions:\n  - fix or rebase that branch in its own checkout, redeploy from there, re-run here\n  - if that work is abandoned: git push --force-with-lease origin origin/main:{name}\n  - --reset deploys ignoring all other branches (one-off; they return next deploy)",
        c.files.join(", ")
    ))
}

/// Collects this run's fetch, once: whether it moved origin/main or a work
/// branch, in which case the checkout is brought onto it first.
fn origin_moved(s: &mut Script, p: &Params, branches: &[(String, String)], fetched: &mut bool, seen: &mut Seen) -> Run<bool> {
    if *fetched {
        return Ok(false);
    }
    *fetched = true;
    s.mark("fetch");
    s.must(Query::CollectFetch, "fetching origin")?;
    if s.text(Query::Rev { name: MAIN.into() })? == p.main0 && select(s, p, seen)? == branches {
        return Ok(false);
    }
    s.print("origin moved since the last fetch; redoing the union");
    s.must(Query::Refresh, "bringing the checkout onto origin/main")?;
    Ok(true)
}

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    let mut fetched = false;
    let mut seen = Seen { listed: Vec::new(), warned: BTreeSet::new() };
    for _ in 0..ATTEMPTS {
        s.mark("union");
        let branches = select(s, p, &mut seen)?;
        s.must(Query::Worktree, "creating the union worktree")?;
        let union = match build(s, &branches)? {
            Ok(union) => union,
            Err(c) => {
                if origin_moved(s, p, &branches, &mut fetched, &mut seen)? {
                    continue;
                }
                return Err(Halt::Die(guidance(s, &c)?));
            }
        };
        if origin_moved(s, p, &branches, &mut fetched, &mut seen)? {
            continue;
        }
        if s.flag(Query::Stage { union: union.clone(), listed: seen.listed.clone() })? || select(s, p, &mut seen)? == branches {
            return Ok(Outcome { union, branches });
        }
        s.print("work branches moved during the build; rebuilding the union");
    }
    Err(Halt::Die(format!("work branches kept moving during {ATTEMPTS} build attempts; re-run")))
}

#[cfg(test)]
mod tests;
