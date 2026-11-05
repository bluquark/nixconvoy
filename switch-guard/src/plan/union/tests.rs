//! The union core against a fake origin whose branches may move when the
//! deploy's fetch lands and during each build, and a union worktree that
//! checks the merge protocol (no merge over an unresolved one, every
//! conflicted file resolved or the merge aborted).
use super::*;
use crate::plan::MAIN;
use crate::plan::{drive, Effect, Replay, Reply};
use proptest::prelude::*;
use std::collections::BTreeMap;

const FB: &str = "haswell-wsl-deploys4";
const LISTED: [&str; 3] = [FB, "chikuwa-deploys1", "M3-Air-deploys2"];
/// Unlisted at first; a refresh may bring a branch_prefix shaped to it.
const NEW: &str = "chikuwa-deploys2";
const UNLISTED: &str = "M3-Air-deploys9";

type Refs = Vec<(String, String)>;

#[derive(Clone, Debug, Default)]
struct Origin {
    head_has: BTreeSet<String>,
    unchanged: BTreeSet<String>,
    /// Each tip's conflicted files on merge, and whether mergiraf solves
    /// each.
    conflicts: BTreeMap<String, Vec<(String, bool)>>,
    refs: Refs,
    main: String,
    /// The work branches' shapes in the checkout, and after a refresh.
    listed: Vec<String>,
    refreshed_listed: Option<Vec<String>>,
    /// What the fetch finds, and each build after it.
    fetch: Option<(String, Refs)>,
    builds: Vec<Refs>,
    /// The deploys under way to wait for: what origin holds after each.
    waits: Vec<Refs>,
    tree_as_is: bool,
    final_stage: bool,
    /// What Query::Cuts answers.
    cuts: Vec<(String, String, String)>,
}

#[derive(Clone, Debug, Default)]
struct Trace {
    p: Option<Params>,
    fetched: bool,
    /// The fetch moved origin/main or the selection, and the checkout was
    /// not refreshed since.
    stale: bool,
    refreshed: usize,
    worktrees: usize,
    merged: Vec<String>,
    pending: Option<Vec<(String, bool, bool)>>,
    checked_out: Option<String>,
    /// Each staged union, the refs and main when it was staged.
    stages: Vec<(String, Vec<String>)>,
    /// The first union of this attempt once a lane's worktree replaced
    /// its own as the one merged in: what it merged and checked out.
    first: Option<(Vec<String>, Option<String>)>,
    /// The last stage's lanes.
    lanes: Vec<Lane>,
    /// What each select pass saw, and the shapes then.
    passes: Vec<(Refs, Vec<String>)>,
    listed_reads: usize,
}

fn select_of(o: &Origin, p: &Params) -> Refs {
    if p.evict {
        return vec![];
    }
    o.refs
        .iter()
        .filter(|(n, s)| *n != p.fb && !o.head_has.contains(s) && o.listed.contains(n) && !o.unchanged.contains(s))
        .cloned()
        .collect()
}

fn answer(o: &mut Origin, t: &mut Trace, q: &Query) -> Reply {
    let ran = |ok| Reply::Ran { ok, detail: String::new() };
    match q {
        Query::Listed => {
            t.listed_reads += 1;
            Reply::Names(o.listed.clone())
        }
        Query::Branches => {
            assert!(t.listed_reads > t.passes.len(), "a pass over the branches without reading the shapes");
            t.passes.push((o.refs.clone(), o.listed.clone()));
            Reply::Refs(o.refs.clone())
        }
        Query::Contains { a, b } => {
            assert_eq!(b, "HEAD");
            Reply::Flag(o.head_has.contains(a))
        }
        Query::Unchanged { sha } => Reply::Flag(o.unchanged.contains(sha)),
        Query::Worktree => {
            assert!(t.pending.is_none(), "a new worktree over an unconcluded merge");
            assert!(!t.stale, "a union built on a checkout the fetch moved");
            t.worktrees += 1;
            t.merged.clear();
            t.checked_out = None;
            t.first = None;
            ran(true)
        }
        Query::LaneWorktree => {
            assert!(t.pending.is_none(), "a lane's worktree over an unconcluded merge");
            assert!(t.fetched && !t.stale, "a lane built before the fetch was collected and followed");
            assert!(t.checked_out.is_some(), "a lane before the first union");
            if t.first.is_none() {
                t.first = Some((t.merged.clone(), t.checked_out.clone()));
            }
            t.merged.clear();
            t.checked_out = None;
            ran(true)
        }
        Query::Cuts { union, branches } => {
            assert!(t.first.is_none(), "the cuts asked for once per union");
            assert_eq!(t.checked_out.as_ref(), Some(union), "cuts scanned over a union not checked out");
            assert_eq!(branches.iter().map(|b| b.0.clone()).collect::<Vec<_>>(), t.merged);
            Reply::Cuts(o.cuts.clone())
        }
        Query::Summary { sha } => Reply::Text(Some(format!("summary of {sha}"))),
        Query::Merge { name, sha } => {
            assert!(t.pending.is_none(), "merging over an unresolved merge");
            assert!(o.listed.contains(name), "merging {name}, not a work branch by the shapes now");
            let files = o.conflicts.get(sha).cloned().unwrap_or_default();
            if files.is_empty() {
                t.merged.push(name.clone());
                return Reply::Merged { ok: true, files: vec![], detail: String::new() };
            }
            t.pending = Some(files.iter().map(|(f, ok)| (f.clone(), *ok, false)).collect());
            t.merged.push(name.clone());
            Reply::Merged { ok: false, files: files.into_iter().map(|f| f.0).collect(), detail: "CONFLICT".into() }
        }
        Query::DropFile { file } | Query::Add { file } => {
            assert!(matches!(q, Query::DropFile { .. }) == (file == MSG_FILE));
            let pending = t.pending.as_mut().expect("no merge to resolve");
            let f = pending.iter_mut().find(|f| f.0 == *file).expect("not conflicted");
            assert!(f.1 || file == MSG_FILE, "added an unsolved file");
            f.2 = true;
            ran(true)
        }
        Query::Solve { file } => {
            assert_ne!(file, MSG_FILE);
            let pending = t.pending.as_ref().expect("no merge to resolve");
            Reply::Flag(pending.iter().find(|f| f.0 == *file).expect("not conflicted").1)
        }
        Query::AbortMerge => {
            t.pending.take().expect("no merge to abort");
            t.merged.pop();
            ran(true)
        }
        Query::CommitMerge => {
            let pending = t.pending.take().expect("no merge to commit");
            assert!(pending.iter().all(|f| f.2), "committing with unresolved files");
            ran(true)
        }
        Query::StripTree => {
            assert!(t.pending.is_none());
            Reply::Text(Some(format!("tree {}", t.merged.join("+"))))
        }
        Query::HeadTree => Reply::Text(Some(if o.tree_as_is { "tree ".into() } else { "tree of the snapshot".into() })),
        Query::Head => Reply::Text(Some("head".into())),
        Query::CommitUnion { tree } => Reply::Text(Some(format!("union of {tree}"))),
        Query::Checkout { union } => {
            t.checked_out = Some(union.clone());
            ran(true)
        }
        Query::CollectFetch => {
            assert!(!t.fetched, "the fetch is collected once");
            t.fetched = true;
            if let Some((main, refs)) = o.fetch.take() {
                let p = t.p.as_ref().unwrap();
                let before = select_of(o, p);
                o.main = main;
                o.refs = refs;
                t.stale = o.main != "m0" || select_of(o, p) != before;
            }
            ran(true)
        }
        Query::Rev { name } => {
            assert_eq!(name, MAIN);
            Reply::Text(Some(o.main.clone()))
        }
        Query::Refresh => {
            assert!(t.fetched);
            t.refreshed += 1;
            t.stale = false;
            if let Some(l) = o.refreshed_listed.take() {
                o.listed = l;
            }
            ran(true)
        }
        Query::AwaitPending { union } => {
            assert!(!t.p.as_ref().unwrap().evict, "a --evict waits for nobody");
            assert_eq!(t.checked_out.as_ref(), Some(union), "waiting on a union not checked out");
            assert!(t.fetched && !t.stale, "waited before the fetch was collected and followed");
            if o.waits.is_empty() {
                return Reply::Flag(false);
            }
            o.refs = o.waits.remove(0);
            t.stale = true;
            Reply::Flag(true)
        }
        Query::Stage { union, listed, branches, lanes } => {
            assert_eq!(listed, &o.listed, "drift judged against other shapes than the checkout's");
            let (merged, checked_out) = t.first.clone().unwrap_or((t.merged.clone(), t.checked_out.clone()));
            assert_eq!(branches.iter().map(|b| b.0.clone()).collect::<Vec<_>>(), merged, "staging other branches than the union merged");
            assert_eq!(checked_out.as_ref(), Some(union), "staging what is not checked out");
            assert!(t.fetched && !t.stale, "staged before the fetch was collected and followed");
            assert_eq!(lanes.is_empty(), t.first.is_none(), "a lane's worktree per lane staged");
            t.stages.push((union.clone(), merged));
            t.lanes = lanes.clone();
            if !o.builds.is_empty() {
                o.refs = o.builds.remove(0);
            }
            Reply::Flag(o.final_stage)
        }
        q => panic!("the union core asked {q:?}"),
    }
}

fn params(evict: bool) -> Params {
    Params { names: Names::default(), fb: FB.into(), evict, main0: Some("m0".into()) }
}

fn arb_refs() -> impl Strategy<Value = Refs> {
    let names = [FB, LISTED[1], LISTED[2], NEW, UNLISTED];
    prop::collection::vec(prop::sample::select(vec!["s1", "s2", "s3", "s4"]), 5).prop_flat_map(move |shas| {
        let all: Refs = names.iter().zip(shas).map(|(n, s)| (n.to_string(), s.to_string())).collect();
        prop::sample::subsequence(all, 0..=5)
    })
}

fn arb_origin() -> impl Strategy<Value = Origin> {
    let shas = || prop::collection::btree_set(prop::sample::select(vec!["s1", "s2", "s3", "s4"]).prop_map(str::to_string), 0..3);
    let file = prop::sample::select(vec![MSG_FILE, "a.nix", "b.nix"]).prop_map(str::to_string);
    (
        shas(),
        shas(),
        prop::collection::btree_map(
            prop::sample::select(vec!["s1", "s2", "s3", "s4"]).prop_map(str::to_string),
            prop::collection::btree_map(file, any::<bool>(), 0..3).prop_map(|m| m.into_iter().collect::<Vec<_>>()),
            0..3,
        ),
        arb_refs(),
        proptest::option::of((prop::sample::select(vec!["m0", "m1"]).prop_map(str::to_string), arb_refs())),
        prop::collection::vec(arb_refs(), 0..4),
        prop::collection::vec(arb_refs(), 0..3),
        any::<bool>(),
        prop::bool::weighted(0.1),
        any::<bool>(),
        prop::collection::vec(
            (
                prop::sample::select(vec!["a", "b", "c"]).prop_map(str::to_string),
                prop::sample::select(vec![FB, LISTED[1], LISTED[2], NEW, UNLISTED]).prop_map(str::to_string),
                prop::sample::select(vec!["", "s1", "s2", "c1"]).prop_map(str::to_string),
            ),
            0..4,
        ),
    )
        .prop_map(|(head_has, unchanged, conflicts, refs, fetch, builds, waits, tree_as_is, final_stage, lists_new, cuts)| Origin {
            cuts,
            listed: LISTED.iter().map(|s| s.to_string()).collect(),
            refreshed_listed: lists_new.then(|| LISTED.iter().chain(&[NEW]).map(|s| s.to_string()).collect()),
            head_has,
            unchanged,
            conflicts,
            refs,
            main: "m0".into(),
            fetch,
            builds,
            waits,
            tree_as_is,
            final_stage,
        })
}

fn run(o: &Origin, p: &Params) -> (Origin, Trace, Vec<Query>, Vec<Effect>, Result<Outcome, String>) {
    let mut o = o.clone();
    let mut t = Trace { p: Some(p.clone()), ..Trace::default() };
    let (asked, fx, out) = drive(Replay::new(p.clone(), program), 300, |q| answer(&mut o, &mut t, q));
    (o, t, asked, fx, out)
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 6000, failure_persistence: None, ..ProptestConfig::default() })]

    #[test]
    fn unions(o in arb_origin(), evict in prop::bool::weighted(0.1)) {
        let p = params(evict);
        let (end, t, asked, fx, out) = run(&o, &p);
        // A wait is another deploy's progress: it costs no attempt.
        let waits = o.waits.len() - end.waits.len();
        prop_assert!(t.stages.len() <= ATTEMPTS && t.worktrees <= ATTEMPTS + waits);
        if evict {
            let merged = asked.iter().any(|q| matches!(q, Query::Branches | Query::Merge { .. } | Query::AwaitPending { .. } | Query::Cuts { .. }));
            prop_assert!(!merged);
        }
        for q in &asked {
            if let Query::Merge { name, sha } = q {
                prop_assert!(name != FB);
                // A lane's commit of a branch is whatever its targets run.
                let cut = o.cuts.iter().any(|c| c.1 == *name && c.2 == *sha);
                prop_assert!(cut || !o.head_has.contains(sha) && !o.unchanged.contains(sha));
            }
        }
        // Each branch a pass saw unlisted (and HEAD lacks) is warned of once.
        let head_has = &o.head_has;
        let want: BTreeSet<String> = t.passes.iter()
            .flat_map(|(refs, listed)| refs.iter().filter(move |(n, s)| n != FB && !listed.contains(n) && !head_has.contains(s)))
            .map(|(n, _)| format!("warning: ignoring origin/{n}: not shaped like nixconvoy.toml's branch_prefix"))
            .collect();
        let warned: Vec<String> = fx.iter().filter_map(|e| match e { Effect::Warn(w) => Some(w.clone()), _ => None }).collect();
        prop_assert_eq!(warned.len(), want.len());
        prop_assert_eq!(warned.into_iter().collect::<BTreeSet<_>>(), want);
        match out {
            Ok(done) => {
                let (union, merged) = t.stages.last().unwrap().clone();
                prop_assert_eq!(&done.union, &union);
                prop_assert_eq!(done.branches.iter().map(|b| b.0.clone()).collect::<Vec<_>>(), merged);
                if !o.final_stage {
                    prop_assert_eq!(&done.branches, &select_of(&end, &p));
                }
                if done.branches.is_empty() && o.tree_as_is {
                    prop_assert_eq!(done.union, "head");
                }
                // The lanes are what the cuts call for of the branches merged.
                let want = lanes_of(&done.branches, &o.cuts);
                prop_assert_eq!(t.lanes.iter().map(|l| (l.targets.clone(), l.branches.clone())).collect::<Vec<_>>(), want);
                let mut seen = BTreeSet::new();
                for l in &t.lanes {
                    prop_assert!(!l.targets.is_empty() && l.branches != done.branches);
                    prop_assert!(l.targets.iter().all(|h| seen.insert(h.clone())), "a target in two lanes");
                    for (name, sha) in &l.branches {
                        let tip = &done.branches.iter().find(|b| b.0 == *name).expect("a lane merging a branch the union lacks").1;
                        prop_assert!(sha == tip || o.cuts.iter().any(|c| c.1 == *name && c.2 == *sha));
                    }
                }
            }
            Err(msg) if msg.starts_with("cannot union work branch") => {
                // Only a conflict with what origin holds once fetched.
                prop_assert!(t.fetched);
                let Some(Query::Merge { name, sha }) = asked.iter().rev().find(|q| matches!(q, Query::Merge { .. })) else { panic!() };
                let unsolved: Vec<String> = o.conflicts[sha].iter().filter(|f| f.0 != MSG_FILE && !f.1).map(|f| f.0.clone()).collect();
                prop_assert!(msg.contains(&format!("\n  {}\n", unsolved.join(", "))), "{}", msg);
                if t.first.is_some() {
                    // A lane's: of a commit a cut names, or a tip merged after another branch was left out.
                    prop_assert!(msg.contains(&format!("cannot union work branch {name} as ")), "{}", msg);
                } else {
                    prop_assert!(msg.contains(&format!("cannot union work branch {name};")), "{}", msg);
                    prop_assert!(select_of(&end, &p).contains(&(name.clone(), sha.clone())));
                }
            }
            Err(msg) => {
                prop_assert_eq!(msg, format!("origin kept moving during {ATTEMPTS} attempts; re-run"));
                prop_assert_eq!(t.worktrees, ATTEMPTS + waits);
            }
        }
    }
}

#[test]
fn prefixes_match_names_starting_with_them() {
    let pats = ["work/haswell-wsl/", "main"];
    let listed = |n| crate::plan::listed(pats.iter().copied(), n);
    assert!(listed("work/haswell-wsl/4"));
    assert!(listed("work/haswell-wsl/4-fix-kettle"));
    assert!(listed("work/haswell-wsl/"));
    assert!(listed("main"));
    assert!(!listed("work/M3-Air/1"));
    assert!(!listed("xwork/haswell-wsl/4"));
    assert!(!listed("(detached)"));
    assert!(!crate::plan::listed([], "main"));
}

#[test]
fn a_moved_origin_redoes_the_union() {
    let o = Origin {
        refs: vec![(LISTED[1].into(), "s1".into())],
        main: "m0".into(),
        fetch: Some(("m1".into(), vec![(LISTED[1].into(), "s2".into()), (NEW.into(), "s3".into())])),
        listed: LISTED.iter().map(|s| s.to_string()).collect(),
        refreshed_listed: Some(LISTED.iter().chain(&[NEW]).map(|s| s.to_string()).collect()),
        ..Origin::default()
    };
    let (_, t, asked, fx, out) = run(&o, &params(false));
    let done = out.unwrap();
    // The refresh brought a shape fitting NEW: it is unioned too.
    assert_eq!(done.branches, [(LISTED[1].to_string(), "s2".to_string()), (NEW.to_string(), "s3".to_string())]);
    assert_eq!(t.refreshed, 1);
    assert_eq!(t.stages.len(), 1);
    assert!(fx.contains(&Effect::Print("origin moved since the last fetch; redoing the union".into())));
    let marks: Vec<&Effect> = fx.iter().filter(|e| matches!(e, Effect::Mark(_))).collect();
    assert_eq!(marks, [&Effect::Mark("union".into()), &Effect::Mark("fetch".into()), &Effect::Mark("union".into())]);
    assert_eq!(asked.iter().filter(|q| **q == Query::Worktree).count(), 2);
}

#[test]
fn a_deploy_under_way_is_waited_for() {
    // Before anything is evaluated, the wait; then the union is redone on
    // the refreshed checkout with the branch that deploy pushed.
    let o = Origin {
        refs: vec![(LISTED[1].into(), "s1".into())],
        main: "m0".into(),
        waits: vec![vec![(LISTED[1].into(), "s1".into()), (LISTED[2].into(), "s2".into())]],
        listed: LISTED.iter().map(|s| s.to_string()).collect(),
        ..Origin::default()
    };
    let (_, t, asked, fx, out) = run(&o, &params(false));
    let done = out.unwrap();
    assert_eq!(done.branches, [(LISTED[1].to_string(), "s1".to_string()), (LISTED[2].to_string(), "s2".to_string())]);
    assert_eq!((t.refreshed, t.stages.len(), t.worktrees), (1, 1, 2));
    assert!(fx.contains(&Effect::Print("redoing the union with what that deploy pushed".into())));
    let waits: Vec<&Query> = asked.iter().filter(|q| matches!(q, Query::AwaitPending { .. })).collect();
    assert_eq!(waits.len(), 2);
    let stage = asked.iter().position(|q| matches!(q, Query::Stage { .. })).unwrap();
    assert!(asked.iter().position(|q| matches!(q, Query::AwaitPending { .. })).unwrap() < stage);
}

#[test]
fn a_merge_failing_without_conflicts_dies() {
    let mut replies = vec![
        Reply::Names(LISTED.iter().map(|s| s.to_string()).collect()),
        Reply::Refs(vec![(LISTED[1].into(), "s1".into())]),
        Reply::Flag(false),
        Reply::Flag(false),
        Reply::Ran { ok: true, detail: String::new() },
        Reply::Text(Some("s1 summary".into())),
        Reply::Merged { ok: false, files: vec![], detail: "fatal: refusing to merge unrelated histories".into() },
    ]
    .into_iter();
    let (_, _, out) = drive(Replay::new(params(false), program), 20, |_| replies.next().unwrap());
    assert_eq!(out, Err(format!("merging {} into the union failed:\nfatal: refusing to merge unrelated histories", LISTED[1])));
}

#[test]
fn targets_take_the_commits_deployed_there() {
    let s = |v: &str| v.to_string();
    let branches = vec![(s("x"), s("x2")), (s("y"), s("y2"))];
    // a and b take x at x1; c takes nothing of x and y at y1; d's cut of a
    // branch not merged counts for nothing, e's at the tip is no cut.
    let cuts = vec![
        (s("b"), s("x"), s("x1")),
        (s("a"), s("x"), s("x1")),
        (s("c"), s("x"), s("")),
        (s("c"), s("y"), s("y1")),
        (s("d"), s("z"), s("z1")),
        (s("e"), s("y"), s("y2")),
    ];
    assert_eq!(
        lanes_of(&branches, &cuts),
        [(vec![s("a"), s("b")], vec![(s("x"), s("x1")), (s("y"), s("y2"))]), (vec![s("c")], vec![(s("y"), s("y1"))])]
    );
    assert!(lanes_of(&branches, &[]).is_empty() && lanes_of(&[], &cuts).is_empty());

    let o = Origin {
        refs: vec![(LISTED[1].into(), "s1".into()), (LISTED[2].into(), "s2".into())],
        main: "m0".into(),
        listed: LISTED.iter().map(|s| s.to_string()).collect(),
        cuts: vec![(s("choko"), LISTED[1].into(), s("")), (s("side1"), LISTED[2].into(), s("c1"))],
        ..Origin::default()
    };
    let (_, t, asked, fx, out) = run(&o, &params(false));
    let done = out.unwrap();
    assert_eq!(done.branches, [(LISTED[1].to_string(), s("s1")), (LISTED[2].to_string(), s("s2"))]);
    assert_eq!(
        t.lanes,
        [
            Lane {
                targets: vec![s("choko")],
                union: format!("union of tree {}", LISTED[2]),
                branches: vec![(LISTED[2].to_string(), s("s2"))]
            },
            Lane {
                targets: vec![s("side1")],
                union: format!("union of tree {}+{}", LISTED[1], LISTED[2]),
                branches: vec![(LISTED[1].to_string(), s("s1")), (LISTED[2].to_string(), s("c1"))]
            },
        ]
    );
    assert_eq!(asked.iter().filter(|q| **q == Query::LaneWorktree).count(), 2);
    assert!(fx.contains(&Effect::Print("union for choko, the other branches as deployed there:".into())), "{fx:?}");
    assert!(asked.contains(&Query::Merge { name: LISTED[2].into(), sha: s("c1") }));

    // A lane that does not merge ends the run, naming its targets.
    let mut o = o;
    o.conflicts.insert(s("c1"), vec![(s("a.nix"), false)]);
    let (_, _, _, _, out) = run(&o, &params(false));
    assert_eq!(
        out.unwrap_err().lines().next().unwrap(),
        format!("cannot union work branch {} as side1 runs it (c1); mergiraf could not resolve:", LISTED[2])
    );
}

/// A main checkout's deploy leaves a lane that does not merge behind
/// rather than end: its targets are staged with no union.
#[test]
fn a_main_checkout_leaves_an_unmerged_lane_behind() {
    let s = |v: &str| v.to_string();
    let mut o = Origin {
        refs: vec![(LISTED[1].into(), "s1".into())],
        main: "m0".into(),
        listed: LISTED.iter().map(|s| s.to_string()).collect(),
        cuts: vec![(s("side1"), LISTED[1].into(), s("c1")), (s("choko"), LISTED[1].into(), s(""))],
        ..Origin::default()
    };
    o.conflicts.insert(s("c1"), vec![(s("a.nix"), false)]);
    let p = Params { fb: String::new(), ..params(false) };
    let (_, t, _, fx, out) = run(&o, &p);
    assert_eq!(out.unwrap().branches, [(LISTED[1].to_string(), s("s1"))]);
    assert_eq!(
        t.lanes,
        [
            Lane { targets: vec![s("choko")], union: s("union of tree "), branches: vec![] },
            Lane { targets: vec![s("side1")], union: String::new(), branches: vec![(LISTED[1].to_string(), s("c1"))] },
        ]
    );
    let warned: Vec<&Effect> = fx.iter().filter(|e| matches!(e, Effect::Warn(_))).collect();
    assert!(
        matches!(warned[..], [Effect::Warn(w)] if w.starts_with(&format!("warning: not deploying to side1: cannot union work branch {} as side1 runs it (c1);", LISTED[1]))),
        "{warned:?}"
    );
}
