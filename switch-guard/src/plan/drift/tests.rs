use super::*;
use crate::plan::{drive, Effect, Query, Replay, Reply};
use proptest::prelude::*;
use serde_json::json;

const BRANCH: &str = "haswell-wsl-deploys2";
const NEW: &str = "/nix/store/new-system";
const OLD: &str = "/nix/store/old-system";
const ME: (&str, &str) = ("haswell-wsl", "/home/blu/projects/homelab3");
const OTHER: (&str, &str) = ("haswell-wsl", "/home/blu/projects/homelab2");
const T0: f64 = 1e6;

fn base() -> String {
    "a".repeat(40)
}

fn landed() -> String {
    "b".repeat(40)
}

/// nixconvoy's lineage_doc with the toplevel added: `pending` is the
/// deployer's own branch and origin's tip of it.
fn doc(top: &str, by: (&str, &str), pending: Option<(&str, Option<&str>)>, others: &[&str], at: f64) -> String {
    let own = pending.map(|p| p.0).unwrap_or(BRANCH);
    let mut branches = serde_json::Map::new();
    for b in others {
        branches.insert(b.to_string(), json!("c".repeat(40)));
    }
    branches.insert(own.to_string(), json!("h".repeat(40)));
    json!({
        "version": 1, "union": "u".repeat(40), "main": "m".repeat(40),
        "branches": branches,
        "pending": pending.map(|(b, base)| json!({"branch": b, "base": base, "head": "s".repeat(40)})),
        "at": at, "by": {"host": by.0, "checkout": by.1, "pid": 4242},
        "toplevel": top,
    })
    .to_string()
}

fn cx(tips: &[(&str, &str)]) -> Context {
    Context {
        me: (ME.0.into(), ME.1.into()),
        listed: ["main", BRANCH, "haswell-wsl-deploys3", "chikuwa-deploys1"].iter().map(|s| s.to_string()).collect(),
        tips: tips.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect(),
        now: T0 + 60.0,
        tz_offset: 0,
    }
}

fn lin(top: &str, by: (&str, &str), pending: Option<(&str, Option<&str>)>, others: &[&str], age: f64, at: f64) -> Lineage {
    let l = parse(&doc(top, by, pending, others, at), Some(age)).expect("a lineage_doc parses");
    assert_eq!(l.pending, pending.map(|(b, base)| (b.to_string(), base.map(str::to_string))));
    l
}

fn std_lin() -> Lineage {
    let b = base();
    lin(NEW, OTHER, Some((BRANCH, Some(&b))), &["chikuwa-deploys1"], 60.0, T0)
}

#[test]
fn classifies_lineages() {
    let b = base();
    let at_base = cx(&[(BRANCH, &b)]);
    let k = |l: Option<&Lineage>, c: &Context, over: bool| classify(l, NEW, c, over, true);

    let (kind, why) = k(Some(&std_lin()), &at_base, false);
    assert_eq!(kind, Kind::InFlight);
    assert!(kind.refused());
    assert!(why.contains(&format!("{BRANCH} deployed by haswell-wsl:/home/blu/projects/homelab2 (pid 4242)")), "{why}");
    assert!(why.contains(&format!("origin/{BRANCH} is still at {}", &b[..12])), "{why}");
    assert_eq!(k(Some(&std_lin()), &cx(&[(BRANCH, &landed())]), false).0, Kind::CatchUp);

    // Age is the host's (the file's mtime); the writer's `at` is display only.
    let old = lin(NEW, OTHER, Some((BRANCH, Some(&b))), &["chikuwa-deploys1"], PENDING_MAX_AGE + 60.0, T0 + 7200.0);
    let (kind, why) = k(Some(&old), &at_base, false);
    assert!(kind == Kind::Abandoned && why.contains("taken as abandoned"), "{why}");
    let young = lin(NEW, OTHER, Some((BRANCH, Some(&b))), &["chikuwa-deploys1"], 10.0, T0 - 7200.0);
    assert_eq!(k(Some(&young), &at_base, false).0, Kind::InFlight);

    let (kind, why) = k(Some(&std_lin()), &at_base, true);
    assert!(kind == Kind::Overridden && why.contains(RESET_FLAG), "{why}");

    // Refused work on a host this checkout's work does not reach is left
    // there; age and --reset still revert it.
    let (kind, why) = classify(Some(&std_lin()), NEW, &at_base, false, false);
    assert!(kind == Kind::LeftAlone && !kind.refused() && why.contains("does not reach it"), "{why}");
    assert_eq!(classify(Some(&old), NEW, &at_base, false, false).0, Kind::Abandoned);
    assert_eq!(classify(Some(&std_lin()), NEW, &at_base, true, false).0, Kind::Overridden);
    assert_eq!(classify(Some(&std_lin()), NEW, &cx(&[(BRANCH, &landed())]), false, false).0, Kind::CatchUp);

    let unl = lin(NEW, OTHER, Some(("M3-Air-deploys9", Some(&b))), &["chikuwa-deploys1"], 60.0, T0);
    let (kind, why) = k(Some(&unl), &cx(&[("M3-Air-deploys9", &b)]), false);
    assert!(kind == Kind::Unlisted && why.contains("M3-Air-deploys9"), "{why}");
    assert_eq!(k(Some(&unl), &cx(&[("M3-Air-deploys9", &landed())]), false).0, Kind::Unlisted);
    // Pushed work of a branch not listed here is refused too.
    let pushed_unl = lin(NEW, OTHER, None, &["M3-Air-deploys9"], 60.0, T0);
    assert_eq!(k(Some(&pushed_unl), &at_base, false).0, Kind::Unlisted);

    assert_eq!(k(None, &at_base, false), (Kind::Untracked, String::new()));
    assert_eq!(parse("", None), None);
    assert_eq!(parse("{not json", None), None);
    assert_eq!(parse(r#"{"version": 2}"#, None), None);
    assert_eq!(k(Some(&lin(OLD, OTHER, None, &[], 60.0, T0)), &at_base, false).0, Kind::Untracked);
    assert_eq!(k(Some(&lin(NEW, ME, Some((BRANCH, Some(&b))), &[], 60.0, T0)), &at_base, false).0, Kind::ThisBranch);
    assert_eq!(k(Some(&lin(NEW, OTHER, None, &["chikuwa-deploys1"], 60.0, T0)), &at_base, false).0, Kind::CatchUp);

    // The nightly's unpushed lock commit on main.
    let nightly = lin(NEW, ("chikuwa", "/var/lib/homelab"), Some(("main", Some(&b))), &[], 60.0, T0);
    assert_eq!(k(Some(&nightly), &cx(&[("main", &b)]), false).0, Kind::InFlight);
    assert_eq!(k(Some(&nightly), &cx(&[("main", &landed())]), false).0, Kind::CatchUp);
    // A branch whose very first push has not landed.
    let first = lin(NEW, OTHER, Some((BRANCH, None)), &[], 60.0, T0);
    assert_eq!(k(Some(&first), &cx(&[]), false).0, Kind::InFlight);
}

#[test]
fn refusal_advice() {
    let a = refusal(&[("side1".into(), Kind::InFlight)]);
    assert!(
        a.contains("side1 runs another") && a.contains("reaches it too") && a.contains("Re-run once its push lands (after 30 min"),
        "{a}"
    );
    assert!(a.contains(&format!("{RESET_FLAG} side1")) && !a.contains(BRANCHES_FILE), "{a}");
    let a = refusal(&[("side1".into(), Kind::Unlisted), ("side6".into(), Kind::InFlight)]);
    assert!(a.contains("side1, side6 run another") && a.contains("reaches them too"), "{a}");
    assert!(a.contains("Re-run once its push lands or list its branch in deploy-branches"), "{a}");
    assert!(a.contains(&format!("{RESET_FLAG} side1 ")), "{a}");
}

#[test]
fn clock_is_local() {
    assert_eq!(clock(3600.0 * 5.0 + 60.0 * 7.0 + 59.0, 0), "05:07");
    assert_eq!(clock(0.0, -3600), "23:00");
}

fn params(hosts: &[&str], candidates: &[&str], reset: (bool, bool), drifting: &[(&str, Option<String>)]) -> Params {
    let s = |v: &[&str]| v.iter().map(|h| h.to_string()).collect::<Vec<_>>();
    let b = base();
    Params {
        cx: cx(&[(BRANCH, &b)]),
        union: "u".repeat(40),
        router: "choko".into(),
        hosts: s(hosts),
        candidates: s(candidates),
        reset: reset.0,
        named: reset.1,
        reached: hosts.iter().map(|h| h.to_string()).collect(),
        running: hosts
            .iter()
            .map(|h| (h.to_string(), Some(if drifting.iter().any(|d| d.0 == *h) { OLD } else { NEW }.to_string())))
            .collect(),
        built: hosts.iter().map(|h| (h.to_string(), NEW.to_string())).collect(),
        lineages: drifting.iter().filter_map(|(h, l)| l.clone().map(|l| (h.to_string(), (l, Some(60.0))))).collect(),
    }
}

/// The core run with every increment that left origin's tips `fate`
/// ("kept", "landed" or "abandoned"); it asks nothing else.
fn run_with(p: Params, fate: &str) -> (Vec<Query>, Vec<Effect>, Result<Outcome, String>) {
    drive(Replay::new(p, program), 64, |q| match q {
        Query::Fate { .. } => Reply::Text(Some(fate.into())),
        q => panic!("drift asked {q:?}"),
    })
}

fn run(p: Params) -> (Vec<Effect>, Result<Outcome, String>) {
    let (_, fx, out) = run_with(p, "kept");
    (fx, out)
}

/// A switch to pushed work names the abandoned increments it reverts: the
/// lineage's branch tips origin no longer has, whose fate git reports
/// abandoned - asked once per tip, never for a tip origin still has.
#[test]
fn names_the_abandoned_increments() {
    let b = base();
    let pushed = doc(OLD, OTHER, None, &["chikuwa-deploys1"], T0);
    let mut p =
        params(&["chikuwa", "choko"], &["chikuwa", "choko"], (false, false), &[("chikuwa", Some(pushed.clone())), ("choko", Some(pushed))]);
    // The lineage holds BRANCH at h..h and chikuwa-deploys1 at c..c; origin
    // has BRANCH reset (at `b`) and chikuwa-deploys1 still at c..c.
    p.cx = cx(&[(BRANCH, &b), ("chikuwa-deploys1", &"c".repeat(40))]);
    let (asked, fx, out) = run_with(p.clone(), "abandoned");
    assert_eq!(out.unwrap().plan, ["chikuwa", "choko"]);
    assert_eq!(asked, [Query::Fate { branch: BRANCH.into(), tip: "h".repeat(40), union: "u".repeat(40) }]);
    let line = format!("  chikuwa: will switch (catch-up: runs pushed work deployed by haswell-wsl:/home/blu/projects/homelab2 (pid 4242) at 13:46; reverting {BRANCH}'s abandoned increment hhhhhhhhhhhh)");
    assert_eq!(fx[0], Effect::Print(line));
    assert!(
        matches!(&fx[1], Effect::Print(l) if l.ends_with("reverting haswell-wsl-deploys2's abandoned increment hhhhhhhhhhhh)")),
        "{fx:?}"
    );

    // Still in the union (rebased, redeployed, landed intact): nothing is
    // reverted, so nothing is said; landed but undone since is a revert too.
    let (asked, fx, _) = run_with(p.clone(), "kept");
    assert_eq!(asked.len(), 1);
    assert!(matches!(&fx[0], Effect::Print(l) if l.ends_with("at 13:46)")), "{fx:?}");
    let (_, fx, _) = run_with(p.clone(), "landed");
    assert!(
        matches!(&fx[0], Effect::Print(l) if l.ends_with(&format!("; reverting {BRANCH}'s increment hhhhhhhhhhhh, landed and since undone)"))),
        "{fx:?}"
    );

    // A pending increment of a branch origin has no tip of: in flight
    // while its first push is on its way, caught up once the branch is
    // gone with its subagent worktree and the union lacks the increment.
    let sub = "haswell-wsl-deploys2-agent-x";
    let first = doc(OLD, OTHER, Some((sub, None)), &[], T0);
    let mut p = params(&["chikuwa"], &["chikuwa"], (false, false), &[("chikuwa", Some(first))]);
    p.cx = cx(&[(BRANCH, &b)]);
    p.cx.listed.insert(sub.into());
    let (asked, _, out) = run_with(p.clone(), "kept");
    assert_eq!(asked.len(), 1);
    assert!(out.unwrap_err().contains("Re-run once its push lands"));
    let (asked, fx, out) = run_with(p.clone(), "abandoned");
    assert_eq!(asked.len(), 1, "one fate per tip: {asked:?}");
    assert_eq!(out.unwrap().plan, ["chikuwa"]);
    assert_eq!(fx[0], Effect::Print(format!("  chikuwa: will switch (catch-up: runs {sub}'s increment deployed by haswell-wsl:/home/blu/projects/homelab2 (pid 4242), the branch gone since; reverting {sub}'s abandoned increment hhhhhhhhhhhh)")));
    p.reached.clear();
    let (_, _, out) = run_with(p, "abandoned");
    assert_eq!(out.unwrap().plan, ["chikuwa"]);

    // This checkout's own deploy, abandoned here since.
    let mine = doc(OLD, ME, Some((BRANCH, Some(&b))), &[], T0);
    let mut p = params(&["chikuwa"], &["chikuwa"], (false, false), &[("chikuwa", Some(mine))]);
    p.cx = cx(&[(BRANCH, &b)]);
    let (asked, fx, _) = run_with(p, "abandoned");
    assert_eq!(asked.len(), 1);
    assert!(
        matches!(&fx[0], Effect::Print(l) if l.starts_with("  chikuwa: will switch (this-branch: over this checkout's deploy of 13:46; reverting")),
        "{fx:?}"
    );

    // A refused or left-alone host, and one whose lineage tips origin
    // still has, asks nothing.
    let in_flight = doc(OLD, OTHER, Some((BRANCH, Some(&b))), &[], T0);
    let mut p = params(&["chikuwa"], &["chikuwa"], (false, false), &[("chikuwa", Some(in_flight))]);
    p.cx = cx(&[(BRANCH, &b)]);
    let (asked, _, out) = run_with(p.clone(), "abandoned");
    assert!(out.is_err() && asked.is_empty());
    p.reached.clear();
    let (asked, _, out) = run_with(p, "abandoned");
    assert!(out.is_ok() && asked.is_empty());
    let pushed = doc(OLD, OTHER, None, &[], T0);
    let mut p = params(&["chikuwa"], &["chikuwa"], (false, false), &[("chikuwa", Some(pushed))]);
    p.cx = cx(&[(BRANCH, &"h".repeat(40))]);
    let (asked, fx, _) = run_with(p, "abandoned");
    assert!(asked.is_empty());
    assert!(matches!(&fx[0], Effect::Print(l) if l.ends_with("at 13:46)")), "{fx:?}");
}

#[test]
fn reports_the_plan() {
    let b = base();
    let in_flight = doc(OLD, OTHER, Some((BRANCH, Some(&b))), &[], T0);
    let p = params(
        &["chikuwa", "choko", "side1"],
        &["choko", "side1"],
        (false, true),
        &[("choko", None), ("chikuwa", None), ("side1", Some(in_flight.clone()))],
    );
    let (fx, out) = run(p);
    let msg = out.unwrap_err();
    assert!(msg.starts_with("nothing switched or committed: side1 runs another"), "{msg}");
    assert_eq!(fx[0], Effect::Print("  side1: refused: runs an increment of haswell-wsl-deploys2 deployed by haswell-wsl:/home/blu/projects/homelab2 (pid 4242) at 13:46 whose push has not landed (origin/haswell-wsl-deploys2 is still at aaaaaaaaaaaa)".into()));
    assert_eq!(fx[1], Effect::Print("  choko: will switch".into()));
    assert_eq!(fx[2], Effect::Print("  chikuwa: differs but excluded by the host filter".into()));
    assert_eq!(fx.len(), 3);

    let p = params(
        &["chikuwa", "choko", "side1"],
        &["chikuwa", "choko", "side1"],
        (true, false),
        &[("choko", None), ("side1", Some(in_flight.clone()))],
    );
    let (fx, out) = run(p);
    let out = out.unwrap();
    assert_eq!(out.plan, ["side1", "choko"]);
    assert_eq!(out.affected, ["side1", "choko"]);
    assert_eq!(fx[0], Effect::Print("  chikuwa: up to date".into()));
    assert!(matches!(&fx[1], Effect::Print(l) if l.starts_with("  side1: will switch, reverting it: runs an increment")), "{fx:?}");
    assert_eq!(fx.len(), 3);

    // The in-flight host is outside this checkout's work: left alone, the
    // rest switched.
    let mut p = params(
        &["chikuwa", "choko", "side1"],
        &["chikuwa", "choko", "side1"],
        (false, false),
        &[("choko", None), ("side1", Some(in_flight.clone()))],
    );
    p.reached = ["chikuwa".to_string(), "choko".to_string()].into_iter().collect();
    let (fx, out) = run(p.clone());
    let out = out.unwrap();
    assert_eq!(out.plan, ["choko"]);
    assert_eq!(out.affected, ["side1", "choko"]);
    assert_eq!(out.drift["side1"].0, Kind::LeftAlone);
    assert!(
        matches!(&fx[1], Effect::Print(l) if l.starts_with("  side1: left alone: runs an increment") && l.ends_with("this checkout's work does not reach it")),
        "{fx:?}"
    );
    assert_eq!(fx[2], Effect::Print("  choko: will switch".into()));

    // An unnamed --reset leaves it alone too; one naming the hosts takes it over.
    p.reset = true;
    let (_, out) = run(p.clone());
    assert_eq!(out.unwrap().drift["side1"].0, Kind::LeftAlone);
    p.named = true;
    let (_, out) = run(p.clone());
    let out = out.unwrap();
    assert_eq!(out.drift["side1"].0, Kind::Overridden);
    assert_eq!(out.plan, ["side1", "choko"]);

    // Named or not, a --reset never takes over a host outside the filter.
    p.candidates = vec!["choko".into()];
    let (fx, out) = run(p);
    assert_eq!(out.unwrap().plan, ["choko"]);
    assert!(fx.contains(&Effect::Print("  side1: differs but excluded by the host filter".into())), "{fx:?}");
}

fn arb_name() -> impl Strategy<Value = String> {
    prop::sample::select(vec!["main", BRANCH, "haswell-wsl-deploys3", "chikuwa-deploys1", "M3-Air-deploys9", "x"]).prop_map(str::to_string)
}

fn arb_top() -> impl Strategy<Value = String> {
    prop::sample::select(vec![NEW, OLD, "/nix/store/other"]).prop_map(str::to_string)
}

fn arb_lineage() -> impl Strategy<Value = Option<String>> {
    prop_oneof![
        Just(None),
        any::<String>().prop_map(Some),
        (
            arb_top(),
            any::<bool>(),
            proptest::option::of((arb_name(), proptest::option::of(prop::sample::select(vec!["a", "b"])))),
            prop::collection::vec(arb_name(), 0..3),
            0.0..1e7f64,
        )
            .prop_map(|(top, mine, pending, others, at)| {
                let others: Vec<&str> = others.iter().map(String::as_str).collect();
                Some(doc(&top, if mine { ME } else { OTHER }, pending.as_ref().map(|(b, base)| (b.as_str(), *base)), &others, at))
            }),
    ]
}

const HOSTS: [&str; 5] = ["chikuwa", "choko", "haswell-wsl", "side1", "side6"];

fn arb_params() -> impl Strategy<Value = Params> {
    let host = || prop::sample::select(HOSTS.to_vec()).prop_map(str::to_string);
    (
        prop::collection::btree_set(host(), 0..5),
        prop::collection::btree_set(host(), 0..5),
        (any::<bool>(), any::<bool>()),
        prop::collection::btree_set(host(), 0..5),
        prop::collection::vec((proptest::option::of(arb_top()), arb_top(), arb_lineage(), proptest::option::of(0.0..4000.0f64)), 5),
        prop::collection::btree_map(arb_name(), prop::sample::select(vec!["a", "b"]).prop_map(str::to_string), 0..4),
        0.0..1e7f64,
    )
        .prop_map(|(hosts, cands, (reset, named), reached, facts, tips, now)| {
            let hosts: Vec<String> = hosts.into_iter().collect();
            let mut p = Params {
                cx: Context { tips, now, ..cx(&[]) },
                union: "u".repeat(40),
                router: "choko".into(),
                candidates: cands.into_iter().filter(|c| hosts.contains(c)).collect(),
                hosts: hosts.clone(),
                reset,
                named,
                reached,
                running: BTreeMap::new(),
                built: BTreeMap::new(),
                lineages: BTreeMap::new(),
            };
            for (h, (running, built, lineage, age)) in hosts.iter().zip(facts) {
                if running.is_some() {
                    p.built.insert(h.clone(), built);
                }
                p.running.insert(h.clone(), running);
                if let Some(l) = lineage {
                    p.lineages.insert(h.clone(), (l, age));
                }
            }
            p
        })
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 4000, failure_persistence: None, ..ProptestConfig::default() })]

    #[test]
    fn parse_never_panics(text in any::<String>(), age in proptest::option::of(any::<f64>())) {
        let _ = parse(&text, age);
    }

    /// The verdicts follow from the lineage, and only refusals are
    /// demoted by age or --reset.
    #[test]
    fn verdicts(text in arb_lineage(), running in arb_top(), p in arb_params(), age in proptest::option::of(0.0..4000.0f64)) {
        let lin = text.as_deref().and_then(|t| parse(t, age));
        let (kind, _) = classify(lin.as_ref(), &running, &p.cx, false, true);
        let (over, _) = classify(lin.as_ref(), &running, &p.cx, true, true);
        let (alone, _) = classify(lin.as_ref(), &running, &p.cx, false, false);
        let (over_alone, _) = classify(lin.as_ref(), &running, &p.cx, true, false);
        prop_assert_eq!(over_alone, over);
        if kind.refused() {
            prop_assert_eq!(alone, Kind::LeftAlone);
        } else {
            prop_assert_eq!(alone, kind);
        }
        prop_assert_ne!(kind, Kind::LeftAlone);
        let tracked = lin.as_ref().filter(|l| l.toplevel == running);
        prop_assert_eq!(kind == Kind::Untracked, tracked.is_none());
        if let Some(l) = tracked {
            let mine = (l.host.as_str(), l.checkout.as_str()) == ME;
            prop_assert_eq!(kind == Kind::ThisBranch, mine);
            let unlisted = l.branches.iter().any(|b| !p.cx.listed.contains(b));
            if kind == Kind::Unlisted { prop_assert!(unlisted); }
            if !mine && unlisted { prop_assert!(matches!(kind, Kind::Unlisted | Kind::Abandoned)); }
            if kind == Kind::InFlight {
                let (b, base) = l.pending.clone().unwrap();
                prop_assert_eq!(p.cx.tips.get(&b), base.as_ref());
            }
            let stale = l.age.unwrap_or(p.cx.now - l.at) > PENDING_MAX_AGE;
            if kind == Kind::Abandoned { prop_assert!(stale); }
            if kind.refused() { prop_assert!(!stale); }
        }
        if kind.refused() {
            prop_assert_eq!(over, Kind::Overridden);
        } else {
            prop_assert_eq!(over, kind);
        }
    }

    #[test]
    fn plans(p in arb_params()) {
        let (fx, out) = run(p.clone());
        let affected: Vec<String> = switch_order(&p.hosts, &p.router).into_iter()
            .filter(|h| matches!((p.running.get(h), p.built.get(h)), (Some(Some(r)), Some(b)) if r != b))
            .collect();
        let verdicts: Vec<(String, Kind)> = affected.iter().filter(|h| p.candidates.contains(h)).map(|h| {
            let lin = p.lineages.get(h).and_then(|(t, a)| parse(t, *a));
            let running = p.running[h].clone().unwrap();
            let reached = p.reached.contains(h);
            (h.clone(), classify(lin.as_ref(), &running, &p.cx, p.reset && (p.named || reached), reached).0)
        }).collect();
        let refused: Vec<(String, Kind)> = verdicts.iter().filter(|v| v.1.refused()).cloned().collect();
        // A refusal needs a reached host, which a --reset takes over.
        if p.reset { prop_assert!(refused.is_empty()); }
        match out {
            Ok(o) => {
                prop_assert!(refused.is_empty());
                prop_assert_eq!(&o.affected, &affected);
                prop_assert_eq!(o.plan.clone(), verdicts.iter().filter(|v| v.1 != Kind::LeftAlone).map(|v| v.0.clone()).collect::<Vec<_>>());
                if let Some(i) = o.plan.iter().position(|h| h == "choko") { prop_assert_eq!(i, o.plan.len() - 1); }
            }
            Err(msg) => {
                prop_assert_eq!(msg, refusal(&refused));
            }
        }
        // One report line per candidate and per excluded drifting host.
        let printed = fx.iter().filter(|e| matches!(e, Effect::Print(_))).count();
        prop_assert_eq!(printed, p.candidates.len() + affected.iter().filter(|h| !p.candidates.contains(h)).count());
        prop_assert!(!fx.iter().any(|e| matches!(e, Effect::Warn(_))));
    }
}
