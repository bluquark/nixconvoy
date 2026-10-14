//! The rebase core against a fake checkout: commits are snapshots of a tiny
//! tree, and a rebase replays each commit's change onto origin/main as git
//! does - a conflict when the base changed the same file differently, an
//! empty commit kept or dropped as asked - refusing a dirty checkout.
use super::*;
use crate::plan::{drive, Effect, Replay, Reply};
use proptest::prelude::*;
use std::collections::BTreeMap;

type Tree = BTreeMap<u8, u8>;

#[derive(Clone, Debug, PartialEq, Eq)]
struct Commit {
    id: u32,
    subject: String,
    tree: Tree,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Head {
    On(String),
    Detached(Vec<Commit>),
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Repo {
    branches: BTreeMap<String, Vec<Commit>>,
    head: Head,
    index: Tree,
    work: Tree,
    op: Option<String>,
    /// The branch a conflicted rebase left (HEAD is detached meanwhile)
    /// and whether it was the aside worktree's.
    rebasing: Option<(String, bool)>,
    /// origin's refs as fetched, and as a fetch would now find them.
    origin: BTreeMap<String, Vec<Commit>>,
    remote: BTreeMap<String, Vec<Commit>>,
    aside: Option<String>,
    next_id: u32,
    /// Worktrees added and not dropped.
    open_worktrees: u32,
}

fn tree_of(h: &[Commit]) -> Tree {
    h.last().map(|c| c.tree.clone()).unwrap_or_default()
}

fn prefix(a: &[Commit], b: &[Commit]) -> bool {
    a.len() <= b.len() && a.iter().zip(b).all(|(x, y)| x.id == y.id)
}

/// `chain`'s changes (each against its predecessor, the first against
/// `from`) replayed onto `onto`'s tip; None on a conflict.
fn replay(onto: &[Commit], from: &Tree, chain: &[Commit], keep_empty: bool, next_id: &mut u32) -> Option<Vec<Commit>> {
    let mut out = onto.to_vec();
    let mut parent = from.clone();
    for c in chain {
        let mut cur = tree_of(&out);
        let before = cur.clone();
        let files: Vec<u8> = parent.keys().chain(c.tree.keys()).copied().collect();
        for f in files {
            let (p, n) = (parent.get(&f), c.tree.get(&f));
            if p == n {
                continue;
            }
            let have = cur.get(&f);
            if have != p && have != n {
                return None;
            }
            match n {
                Some(v) => cur.insert(f, *v),
                None => cur.remove(&f),
            };
        }
        parent = c.tree.clone();
        if cur == before && !keep_empty {
            continue;
        }
        *next_id += 1;
        out.push(Commit { id: *next_id, subject: c.subject.clone(), tree: cur });
    }
    Some(out)
}

impl Repo {
    fn commits(&self) -> &Vec<Commit> {
        match &self.head {
            Head::On(b) => &self.branches[b],
            Head::Detached(c) => c,
        }
    }

    fn commits_mut(&mut self) -> &mut Vec<Commit> {
        match &mut self.head {
            Head::On(b) => self.branches.get_mut(b).unwrap(),
            Head::Detached(c) => c,
        }
    }

    fn resolve(&self, name: &str) -> Option<Vec<Commit>> {
        if name == "HEAD" {
            return Some(self.commits().clone());
        }
        if let Some(b) = name.strip_prefix("refs/heads/") {
            return self.branches.get(b).cloned();
        }
        name.strip_prefix("refs/remotes/origin/").and_then(|b| self.origin.get(b).cloned())
    }

    fn ran(ok: bool, detail: &str) -> Reply {
        Reply::Ran { ok, detail: detail.into() }
    }

    fn rebase(&mut self, keep_empty: bool, aside: bool, upstream: &str) -> Reply {
        let name = if aside {
            match &self.aside {
                Some(b) => b.clone(),
                None => return Repo::ran(false, "no worktree"),
            }
        } else {
            match &self.head {
                Head::On(b) => b.clone(),
                Head::Detached(_) => return Repo::ran(false, "detached"),
            }
        };
        if !aside && (self.index != tree_of(self.commits()) || self.work != self.index) {
            return Repo::ran(false, "error: cannot rebase: You have unstaged changes.");
        }
        let Some(main) = self.resolve(upstream) else {
            return Repo::ran(false, "invalid upstream");
        };
        let mine = self.branches[&name].clone();
        if prefix(&main, &mine) {
            return Repo::ran(true, "");
        }
        let fork = main.iter().zip(&mine).take_while(|(a, b)| a.id == b.id).count();
        let from = tree_of(&mine[..fork]);
        match replay(&main, &from, &mine[fork..], keep_empty, &mut self.next_id) {
            Some(out) => {
                self.branches.insert(name, out);
                if !aside {
                    self.index = tree_of(self.commits());
                    self.work = self.index.clone();
                }
                Repo::ran(true, "")
            }
            None => {
                self.op = Some("rebase".into());
                self.rebasing = Some((name.clone(), aside));
                if !aside {
                    self.head = Head::Detached(main);
                }
                Repo::ran(false, "CONFLICT (content): Merge conflict in a.nix\nhint: Resolve all conflicts manually")
            }
        }
    }

    fn answer(&mut self, q: &Query) -> Reply {
        match q {
            Query::Contains { a, b } => match (self.resolve(a), self.resolve(b)) {
                (Some(x), Some(y)) => Reply::Flag(prefix(&x, &y)),
                _ => Reply::Flag(false),
            },
            Query::OpInProgress => Reply::Text(self.op.clone()),
            Query::CurrentBranch => Reply::Text(match &self.head {
                Head::On(b) => Some(b.clone()),
                Head::Detached(_) => None,
            }),
            Query::StagedDiffers => Reply::Flag(self.index != tree_of(self.commits())),
            Query::Commit { msg } => {
                if self.index == tree_of(self.commits()) {
                    return Repo::ran(false, "nothing to commit");
                }
                self.next_id += 1;
                let c = Commit { id: self.next_id, subject: msg.clone(), tree: self.index.clone() };
                self.commits_mut().push(c);
                Repo::ran(true, "")
            }
            Query::AddAll => {
                self.index = self.work.clone();
                Repo::ran(true, "")
            }
            Query::Pop { mixed } => {
                if self.commits().len() < 2 {
                    return Repo::ran(false, "ambiguous argument 'HEAD~1'");
                }
                self.commits_mut().pop();
                if *mixed {
                    self.index = tree_of(self.commits());
                }
                Repo::ran(true, "")
            }
            Query::HeadSubject => Reply::Text(self.commits().last().map(|c| c.subject.clone())),
            Query::Rebase { keep_empty, aside, upstream } => self.rebase(*keep_empty, *aside, upstream),
            Query::RebaseAbort { aside } => match self.rebasing.take() {
                Some((b, a)) if a == *aside => {
                    self.op = None;
                    if !a {
                        self.head = Head::On(b);
                    }
                    Repo::ran(true, "")
                }
                other => {
                    self.rebasing = other;
                    Repo::ran(false, "No rebase in progress?")
                }
            },
            Query::Rev { name } => Reply::Text(self.resolve(name).and_then(|c| c.last().map(|c| c.id.to_string()))),
            Query::Fetch => {
                self.origin = self.remote.clone();
                Repo::ran(true, "")
            }
            Query::Push { branch } => {
                let tip = self.branches[branch].clone();
                self.origin.insert(branch.clone(), tip.clone());
                self.remote.insert(branch.clone(), tip);
                Repo::ran(true, "")
            }
            Query::AddWorktree { branch } => {
                self.open_worktrees += 1;
                if self.head == Head::On(branch.clone()) || !self.branches.contains_key(branch) {
                    return Repo::ran(false, "fatal: already checked out");
                }
                self.aside = Some(branch.clone());
                Repo::ran(true, "")
            }
            Query::DropWorktree => {
                self.open_worktrees = self.open_worktrees.saturating_sub(1);
                self.aside = None;
                Repo::ran(true, "")
            }
            Query::Switch { branch, start } => {
                if let Some(s) = start {
                    let c = self.resolve(s).unwrap();
                    self.branches.insert(branch.clone(), c);
                }
                let target = tree_of(&self.branches[branch]);
                if self.work != tree_of(self.commits()) && target != tree_of(self.commits()) {
                    return Repo::ran(false, "error: Your local changes would be overwritten");
                }
                self.head = Head::On(branch.clone());
                Repo::ran(true, "")
            }
            Query::Detach { upstream } => {
                let Some(main) = self.resolve(upstream) else {
                    return Repo::ran(false, "invalid upstream");
                };
                let cur = tree_of(self.commits());
                if self.work != cur && tree_of(&main) != cur {
                    return Repo::ran(false, "error: Your local changes would be overwritten");
                }
                if self.work == cur {
                    self.index = tree_of(&main);
                    self.work = self.index.clone();
                }
                self.head = Head::Detached(main);
                Repo::ran(true, "")
            }
            Query::Pushed | Query::OnOrigin => Reply::Flag(self.origin.values().any(|c| prefix(self.commits(), c))),
            Query::Upstream { .. } => Reply::Text(None),
            Query::SetUpstream { .. } => Repo::ran(true, ""),
            q => panic!("the rebase core asked {q:?}"),
        }
    }
}

fn run(repo: &mut Repo, p: Params, limit: usize) -> (Vec<Query>, Vec<Effect>, Option<Result<Outcome, String>>) {
    let mut asked = Vec::new();
    let mut fx = Vec::new();
    let mut m = Replay::new(p, program);
    let mut reply = None;
    loop {
        let step = m.decide(reply.take());
        fx.extend(step.effects);
        match step.next {
            crate::plan::Next::Done(o) => return (asked, fx, Some(Ok(o))),
            crate::plan::Next::Die(e) => return (asked, fx, Some(Err(e))),
            crate::plan::Next::Ask(q) => {
                if asked.len() == limit {
                    return (asked, fx, None);
                }
                reply = Some(repo.answer(&q));
                asked.push(q);
            }
        }
    }
}

type Edits = Vec<(u8, Option<u8>)>;

fn apply(t: &Tree, e: &Edits) -> Tree {
    let mut t = t.clone();
    for (f, v) in e {
        match v {
            Some(v) => t.insert(*f, *v),
            None => t.remove(f),
        };
    }
    t
}

fn edits() -> impl Strategy<Value = Edits> {
    prop::collection::vec((0u8..4, proptest::option::of(0u8..3)), 0..3)
}

#[derive(Clone, Debug)]
struct Shape {
    root: Edits,
    main: Vec<Edits>,
    fork: usize,
    local: Vec<Edits>,
    staged: Edits,
    unstaged: Edits,
    op: bool,
    detached: bool,
}

fn shape() -> impl Strategy<Value = Shape> {
    (
        edits(),
        prop::collection::vec(edits(), 0..4),
        any::<prop::sample::Index>(),
        prop::collection::vec(edits(), 0..3),
        edits(),
        edits(),
        prop::bool::weighted(0.1),
        prop::bool::weighted(0.1),
    )
        .prop_map(|(root, main, fork, local, staged, unstaged, op, detached)| Shape {
            fork: fork.index(main.len() + 1),
            root,
            main,
            local,
            staged,
            unstaged,
            op,
            detached,
        })
}

const WB: &str = "haswell-wsl-deploys4";

fn commit(next_id: &mut u32, subject: &str, tree: Tree) -> Commit {
    *next_id += 1;
    Commit { id: *next_id, subject: subject.into(), tree }
}

/// The checkout on WB (forked from origin/main at `fork`), its staged and
/// unstaged changes, and the local main at origin's.
fn build(s: &Shape) -> Repo {
    let mut id = 0;
    let mut main = vec![commit(&mut id, "root", apply(&Tree::new(), &s.root))];
    for (i, e) in s.main.iter().enumerate() {
        let t = apply(&tree_of(&main), e);
        main.push(commit(&mut id, &format!("main {i}"), t));
    }
    let mut mine = main[..=s.fork].to_vec();
    for (i, e) in s.local.iter().enumerate() {
        let t = apply(&tree_of(&mine), e);
        mine.push(commit(&mut id, &format!("local {i}"), t));
    }
    let index = apply(&tree_of(&mine), &s.staged);
    let work = apply(&index, &s.unstaged);
    let origin: BTreeMap<String, Vec<Commit>> = [("main".to_string(), main.clone())].into();
    Repo {
        branches: [("main".to_string(), main.clone()), (WB.to_string(), mine.clone())].into(),
        head: if s.detached { Head::Detached(mine) } else { Head::On(WB.into()) },
        index,
        work,
        op: s.op.then(|| "merge".to_string()),
        rebasing: None,
        remote: origin.clone(),
        origin,
        aside: None,
        next_id: id,
        open_worktrees: 0,
    }
}

/// What a rebase that carries the staged and unstaged changes yields: the
/// branch's commits on origin/main, then the index's and the worktree's
/// trees; None when it conflicts.
fn ideal(r: &Repo) -> Option<(Vec<Commit>, Tree, Tree)> {
    let main = &r.origin["main"];
    let mine = r.commits();
    let fork = main.iter().zip(mine).take_while(|(a, b)| a.id == b.id).count();
    let mut chain = mine[fork..].to_vec();
    let head = tree_of(mine);
    let staged = r.index != head;
    let unstaged = r.work != r.index;
    if staged {
        chain.push(Commit { id: 0, subject: "S".into(), tree: r.index.clone() });
    }
    if unstaged {
        chain.push(Commit { id: 0, subject: "W".into(), tree: r.work.clone() });
    }
    let mut id = 10_000;
    let out = replay(main, &tree_of(&mine[..fork]), &chain, true, &mut id)?;
    let n = out.len() - staged as usize - unstaged as usize;
    let commits = out[..n].to_vec();
    let index = if staged { out[n].tree.clone() } else { tree_of(&commits) };
    let work = if unstaged { tree_of(&out) } else { index.clone() };
    Some((commits, index, work))
}

fn subjects(c: &[Commit]) -> Vec<String> {
    c.iter().map(|c| c.subject.clone()).collect()
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 6000, failure_persistence: None, ..ProptestConfig::default() })]

    /// The rebase carries the commits and the exact staged/unstaged split
    /// onto origin/main, or leaves the checkout exactly as it was.
    #[test]
    fn rebase_keeps_the_split(s in shape()) {
        let mut r = build(&s);
        let before = r.clone();
        let (_, fx, out) = run(&mut r, Params::EnsureRebased, 100);
        let out = out.unwrap();
        let main = &before.origin["main"];
        if prefix(main, before.commits()) {
            prop_assert_eq!(out, Ok(Ok("up to date".into())));
            prop_assert_eq!(&r, &before);
            return Ok(());
        }
        if s.op {
            prop_assert!(out.is_err());
            prop_assert_eq!(&r, &before);
            return Ok(());
        }
        if s.detached {
            // Moved along when behind origin/main, refused when it holds
            // commits of its own.
            if !prefix(before.commits(), main) {
                prop_assert_eq!(out, Err(STRANDED.into()));
                prop_assert_eq!(&r, &before);
            } else if before.work == tree_of(before.commits()) || tree_of(main) == tree_of(before.commits()) {
                prop_assert_eq!(out, Ok(Ok("fast-forwarded the detached HEAD onto origin/main".into())));
                prop_assert_eq!(&r.head, &Head::Detached(main.clone()));
                prop_assert!(!fx.is_empty());
            } else {
                let msg = out.unwrap_err();
                prop_assert!(msg.starts_with("cannot move the detached HEAD onto origin/main, left as it was"), "{}", msg);
                prop_assert_eq!(&r, &before);
            }
            return Ok(());
        }
        match ideal(&before) {
            None => {
                let msg = out.unwrap_err();
                prop_assert!(msg.starts_with(&format!("rebasing {WB} onto origin/main is not clean, left as it was")), "{}", msg);
                prop_assert!(!msg.contains("hint:"), "{}", msg);
                prop_assert_eq!(&r.branches, &before.branches);
                prop_assert_eq!(&r.head, &before.head);
                prop_assert_eq!(&r.index, &before.index);
                prop_assert_eq!(&r.work, &before.work);
                prop_assert_eq!(&r.op, &None);
            }
            Some((commits, index, work)) => {
                let ff = prefix(before.commits(), main);
                let status = format!("{} {WB} onto origin/main", if ff { "fast-forwarded" } else { "rebased" });
                prop_assert_eq!(out, Ok(Ok(status.clone())));
                prop_assert_eq!(fx, vec![Effect::Print(status)]);
                prop_assert!(prefix(main, r.commits()));
                prop_assert_eq!(subjects(r.commits()), subjects(&commits));
                prop_assert_eq!(tree_of(r.commits()), tree_of(&commits));
                prop_assert_eq!(&r.index, &index);
                prop_assert_eq!(&r.work, &work);
                prop_assert_eq!(&r.head, &before.head);
            }
        }
    }

    /// A run that died anywhere but between staging the unstaged changes
    /// and committing them is undone by the next run's pop: the checkout as
    /// it was, or as the finished rebase left it.
    #[test]
    fn pop_recovers_a_dead_run(s in shape(), at in 0usize..30) {
        let s = Shape { op: false, detached: false, ..s };
        let mut r = build(&s);
        let before = r.clone();
        let (asked, _, out) = run(&mut r, Params::EnsureRebased, at);
        if out.is_some() || r.op.is_some() {
            return Ok(());
        }
        let after_add = asked.iter().position(|q| *q == Query::AddAll);
        let tmp_unstaged = asked.iter().any(|q| *q == Query::Commit { msg: TMP_UNSTAGED.into() });
        if after_add.is_some() && !tmp_unstaged && before.work != before.index {
            return Ok(());
        }
        let rebased = asked.iter().any(|q| matches!(q, Query::Rebase { .. }));
        let work = r.work.clone();
        let (_, _, popped) = run(&mut r, Params::PopTmp, 100);
        prop_assert_eq!(popped, Some(Ok(Ok(String::new()))));
        prop_assert_eq!(&r.work, &work);
        let top = r.commits().last().unwrap().subject.clone();
        prop_assert!(top != TMP_STAGED && top != TMP_UNSTAGED);
        if !rebased {
            prop_assert_eq!(&r, &Repo { next_id: r.next_id, ..before });
        } else if let Some((commits, index, work)) = ideal(&before) {
            prop_assert_eq!(subjects(r.commits()), subjects(&commits));
            prop_assert_eq!(&r.index, &index);
            prop_assert_eq!(&r.work, &work);
        }
    }

    /// Sync from main: the work branch is rebased aside (its worktree
    /// always dropped) and pushed when origin's copy differs, or reported
    /// and left as it was.
    #[test]
    fn sync_aside(s in shape(), moved in edits(), pushed in any::<bool>()) {
        let s = Shape { op: false, detached: false, staged: vec![], unstaged: vec![], ..s };
        let mut r = build(&s);
        r.head = Head::On("main".into());
        r.index = tree_of(&r.branches["main"]);
        r.work = r.index.clone();
        if pushed {
            r.origin.insert(WB.into(), r.branches[WB].clone());
            r.remote.insert(WB.into(), r.branches[WB].clone());
        }
        let mut upstream = r.remote["main"].clone();
        let t = apply(&tree_of(&upstream), &moved);
        upstream.push(commit(&mut r.next_id, "upstream", t));
        r.remote.insert("main".into(), upstream.clone());
        let before = r.clone();
        let (asked, _, out) = run(&mut r, Params::Sync { wb: Some(WB.into()) }, 100);
        prop_assert_eq!(r.open_worktrees, 0);
        let out = out.unwrap().unwrap();
        prop_assert!(prefix(&upstream, &r.branches["main"]));
        let mut fetched = before.clone();
        fetched.origin = fetched.remote.clone();
        let mine = &before.branches[WB];
        let fork = upstream.iter().zip(mine).take_while(|(a, b)| a.id == b.id).count();
        let mut id = 0;
        match replay(&upstream, &tree_of(&mine[..fork]), &mine[fork..], false, &mut id) {
            _ if prefix(&upstream, mine) => {
                prop_assert_eq!(&r.branches[WB], mine);
                let added = asked.iter().any(|q| matches!(q, Query::AddWorktree { .. }));
                prop_assert!(!added);
            }
            None => {
                let e = out.unwrap_err();
                prop_assert!(e.contains(&format!("`git rebase origin/main {WB}`")), "{}", e);
                prop_assert_eq!(&r.branches[WB], mine);
                prop_assert_eq!(&r.origin.get(WB), &before.origin.get(WB));
                return Ok(());
            }
            Some(want) => {
                prop_assert_eq!(subjects(&r.branches[WB]), subjects(&want));
            }
        }
        let pushes = asked.iter().filter(|q| matches!(q, Query::Push { .. })).count();
        let differed = before.origin.get(WB).map(|c| c.last().unwrap().id) != r.branches[WB].last().map(|c| c.id);
        prop_assert_eq!(pushes, differed as usize);
        prop_assert_eq!(r.origin.get(WB), Some(&r.branches[WB]));
        let status = out.unwrap();
        prop_assert_eq!(status.contains(&format!("pushed {WB}")), pushes == 1);
    }
}

#[test]
fn pops_only_tmp_commits() {
    let s = Shape {
        root: vec![(0, Some(0))],
        main: vec![],
        fork: 0,
        local: vec![vec![(1, Some(1))]],
        staged: vec![],
        unstaged: vec![],
        op: false,
        detached: false,
    };
    let mut r = build(&s);
    r.index.insert(2, 2);
    r.work = r.index.clone();
    r.work.insert(3, 1);
    let before = r.clone();
    run(&mut r, Params::PopTmp, 100);
    assert_eq!(r, before);
    for (msg, add) in [(TMP_STAGED, false), (TMP_UNSTAGED, true)] {
        if add {
            r.index = r.work.clone();
        }
        r.answer(&Query::Commit { msg: msg.into() });
    }
    let (_, _, out) = run(&mut r, Params::PopTmp, 100);
    assert_eq!(out, Some(Ok(Ok(String::new()))));
    assert_eq!(r, Repo { next_id: r.next_id, ..before.clone() });
    // A dead run's tmp commit on a detached HEAD is popped too.
    r.head = Head::Detached(r.commits().clone());
    let parked = r.clone();
    r.answer(&Query::Commit { msg: TMP_STAGED.into() });
    let (_, _, out) = run(&mut r, Params::PopTmp, 100);
    assert_eq!(out, Some(Ok(Ok(String::new()))));
    assert_eq!(r, Repo { next_id: r.next_id, ..parked });
}

#[test]
fn moves_onto_the_work_branch() {
    let s = Shape {
        root: vec![(0, Some(0))],
        main: vec![],
        fork: 0,
        local: vec![],
        staged: vec![],
        unstaged: vec![],
        op: false,
        detached: false,
    };
    let mut r = build(&s);
    r.branches.remove(WB);
    r.head = Head::On("main".into());
    let (asked, fx, out) = run(&mut r.clone(), Params::OnWorkBranch { wb: WB.into() }, 100);
    assert_eq!(out, Some(Ok(Ok(String::new()))));
    assert!(asked.contains(&Query::Switch { branch: WB.into(), start: Some("HEAD".into()) }), "{asked:?}");
    assert_eq!(fx, [Effect::Print(format!("switched to work branch {WB}"))]);
    r.origin.insert(WB.into(), r.branches["main"].clone());
    let (asked, _, _) = run(&mut r.clone(), Params::OnWorkBranch { wb: WB.into() }, 100);
    assert!(asked.contains(&Query::Switch { branch: WB.into(), start: Some(format!("refs/remotes/origin/{WB}")) }), "{asked:?}");
    // From a HEAD detached at origin/main.
    r.head = Head::Detached(r.branches["main"].clone());
    let (asked, _, out) = run(&mut r.clone(), Params::OnWorkBranch { wb: WB.into() }, 100);
    assert_eq!(out, Some(Ok(Ok(String::new()))));
    assert!(asked.contains(&Query::Switch { branch: WB.into(), start: Some(format!("refs/remotes/origin/{WB}")) }), "{asked:?}");
    // A detached HEAD with a commit of its own would be stranded by the switch.
    r.index.insert(3, 3);
    r.answer(&Query::Commit { msg: "by hand".into() });
    let (_, _, out) = run(&mut r.clone(), Params::OnWorkBranch { wb: WB.into() }, 100);
    assert_eq!(out, Some(Err(STRANDED.into())));
    // A fresh branch with nothing of its own (a subagent worktree's) is fine too.
    r.head = Head::On("feature".into());
    r.branches.insert("feature".into(), r.branches["main"].clone());
    let (asked, _, out) = run(&mut r.clone(), Params::OnWorkBranch { wb: WB.into() }, 100);
    assert_eq!(out, Some(Ok(Ok(String::new()))));
    assert!(asked.iter().any(|q| matches!(q, Query::Switch { branch, .. } if branch == WB)), "{asked:?}");
    r.index.insert(3, 3);
    r.work = r.index.clone();
    r.answer(&Query::Commit { msg: "feature work".into() });
    let (_, _, out) = run(&mut r.clone(), Params::OnWorkBranch { wb: WB.into() }, 100);
    assert_eq!(out, Some(Err(format!("on branch feature, which carries commits not on origin/main; deploys run from {WB} or from main"))));
    // ...unless those commits are pushed on a work branch (its parent's).
    r.origin.insert("haswell-wsl-deploys2".into(), r.branches["feature"].clone());
    let (asked, _, out) = run(&mut r, Params::OnWorkBranch { wb: WB.into() }, 100);
    assert_eq!(out, Some(Ok(Ok(String::new()))));
    assert!(asked.contains(&Query::Pushed), "{asked:?}");
}

#[test]
fn leaves_a_review_checkout_alone() {
    // A review subagent checks out a work branch's tip: a detached HEAD with
    // commits beyond origin/main that are all on origin.  It stays put
    // (the review reads it), and is no checkout to deploy from.
    let s = Shape {
        root: vec![(0, Some(0))],
        main: vec![vec![(2, Some(2))]],
        fork: 0,
        local: vec![vec![(1, Some(1))]],
        staged: vec![],
        unstaged: vec![],
        op: false,
        detached: true,
    };
    let mut r = build(&s);
    let (_, _, out) = run(&mut r.clone(), Params::Sync { wb: Some(WB.into()) }, 100);
    assert_eq!(out, Some(Ok(Err(STRANDED.into()))));
    // On origin, as fetched and as the fetch will find it.
    r.origin.insert("haswell-wsl-deploys2".into(), r.commits().clone());
    r.remote.insert("haswell-wsl-deploys2".into(), r.commits().clone());
    let before = r.clone();
    let (asked, _, out) = run(&mut r, Params::Sync { wb: Some("tester-deploys9-review".into()) }, 100);
    assert!(asked.contains(&Query::OnOrigin), "{asked:?}");
    assert_eq!(out, Some(Ok(Ok(LEFT.into()))));
    assert_eq!(r, before);
    let (_, _, out) = run(&mut r, Params::EnsureRebased, 100);
    assert_eq!(out, Some(Err(format!("{LEFT}, not on origin/main; switch to a branch to deploy"))));
}

#[test]
fn reports_what_blocks_a_rebase() {
    let ask = |replies: Vec<Reply>| {
        let mut it = replies.into_iter();
        drive(Replay::new(Params::Sync { wb: Some(WB.into()) }, program), 20, move |_| it.next().unwrap()).2
    };
    let ran = |ok| Reply::Ran { ok, detail: "boom".into() };
    assert_eq!(ask(vec![ran(false)]), Ok(Err("fetch failed:\nboom".into())));
    let some = |s: &str| Reply::Text(Some(s.into()));
    // fetch, pop's subject, then onto_main.
    let pre = || vec![ran(true), some("x"), Reply::Flag(false)];
    assert_eq!(ask([pre(), vec![some("merge")]].concat()), Ok(Err("a merge is in progress; finish or abort it first".into())));
    assert_eq!(
        ask([pre(), vec![Reply::Text(None), Reply::Text(None), Reply::Flag(false), Reply::Flag(false)]].concat()),
        Ok(Err(STRANDED.into()))
    );
    assert_eq!(
        ask([pre(), vec![Reply::Text(None), Reply::Text(None), Reply::Flag(false), Reply::Flag(true), Reply::Text(None)]].concat()),
        Ok(Ok(LEFT.into()))
    );
    // The main checkout syncs without a work branch.
    let mut it = vec![ran(true), some("x"), Reply::Flag(true)].into_iter();
    let out = drive(Replay::new(Params::Sync { wb: None }, program), 20, move |_| it.next().unwrap()).2;
    assert_eq!(out, Ok(Ok("up to date".into())));
}

#[test]
fn onto_brings_an_annex_onto_its_default_branch() {
    // An annex worktree: nothing of its own, unstaged edits to a file the
    // landing did not touch.  Onto fast-forwards it, the edits intact; the
    // same edits to a file the landing changed leave it exactly as it was,
    // saying so.
    let s = Shape {
        root: vec![(0, Some(0)), (1, Some(1))],
        main: vec![vec![(2, Some(2))]],
        fork: 0,
        local: vec![],
        staged: vec![],
        unstaged: vec![(1, Some(9))],
        op: false,
        detached: false,
    };
    let mut r = build(&s);
    r.origin.insert("dev".into(), r.origin["main"].clone());
    let dev = "refs/remotes/origin/dev";
    let before = r.clone();
    let (asked, _, out) = run(&mut r, Params::Onto { upstream: dev.into() }, 100);
    assert_eq!(out, Some(Ok(Ok(format!("fast-forwarded {WB} onto origin/dev")))));
    assert!(asked.iter().any(|q| matches!(q, Query::Rebase { upstream, .. } if upstream == dev)), "{asked:?}");
    assert_eq!(r.commits(), &r.origin["dev"]);
    assert_eq!(r.index, tree_of(r.commits()));
    assert_eq!(r.work.get(&1), Some(&9));
    assert_eq!(r.work.get(&2), Some(&2));
    let mut r = before;
    r.work.insert(2, 9); // the landing's file, edited here too
    let before = r.clone();
    let (_, _, out) = run(&mut r, Params::Onto { upstream: dev.into() }, 100);
    let msg = out.unwrap().unwrap().unwrap_err();
    assert!(msg.starts_with(&format!("rebasing {WB} onto origin/dev is not clean, left as it was; run `git rebase origin/dev`")), "{msg}");
    r.next_id = before.next_id; // the tmp commit's id, spent
    assert_eq!(r, before);
}
