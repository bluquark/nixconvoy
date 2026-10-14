use super::*;
use crate::plan::{drive, Effect, Query, Replay, Reply};
use proptest::prelude::*;
use serde_json::json;

const BRANCH: &str = "work/haswell-wsl/2";
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
/// deploying checkout's own branch and origin's tip of it, `announce` its
/// increment's announcement.
fn doc_ann(top: &str, by: (&str, &str), pending: Option<(&str, Option<&str>)>, announce: Option<&str>, others: &[&str], at: f64) -> String {
    let own = pending.map(|p| p.0).unwrap_or(BRANCH);
    let mut branches = serde_json::Map::new();
    for b in others {
        branches.insert(b.to_string(), json!("c".repeat(40)));
    }
    branches.insert(own.to_string(), json!("h".repeat(40)));
    let pending = pending.map(|(b, base)| {
        let mut p = json!({"branch": b, "base": base, "head": "s".repeat(40)});
        if let Some(a) = announce {
            p["announce"] = json!(a);
            if b == "main" {
                p["key"] = json!("main@chikuwa");
            }
        }
        p
    });
    json!({
        "version": 2, "union": "u".repeat(40), "main": "m".repeat(40),
        "branches": branches,
        "pending": pending,
        "at": at, "by": {"machine": by.0, "checkout": by.1, "pid": 4242},
        "toplevel": top,
    })
    .to_string()
}

fn doc(top: &str, by: (&str, &str), pending: Option<(&str, Option<&str>)>, others: &[&str], at: f64) -> String {
    doc_ann(top, by, pending, None, others, at)
}

fn cx(tips: &[(&str, &str)]) -> Context {
    Context {
        me: (ME.0.into(), ME.1.into()),
        listed: ["main", BRANCH, "work/haswell-wsl/3", "work/chikuwa/1"].iter().map(|s| s.to_string()).collect(),
        tips: tips.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect(),
        in_flight: BTreeMap::new(),
        dead: BTreeSet::new(),
        tz_offset: 0,
    }
}

fn lin_ann(
    top: &str,
    by: (&str, &str),
    pending: Option<(&str, Option<&str>)>,
    announce: Option<&str>,
    others: &[&str],
    at: f64,
) -> Lineage {
    let l = parse(&doc_ann(top, by, pending, announce, others, at)).expect("a lineage_doc parses");
    assert_eq!(l.pending, pending.map(|(b, base)| (b.to_string(), base.map(str::to_string))));
    assert_eq!(l.announce.as_deref(), announce.filter(|_| pending.is_some()));
    l
}

fn lin(top: &str, by: (&str, &str), pending: Option<(&str, Option<&str>)>, others: &[&str], at: f64) -> Lineage {
    lin_ann(top, by, pending, None, others, at)
}

fn std_lin() -> Lineage {
    let b = base();
    lin(NEW, OTHER, Some((BRANCH, Some(&b))), &["work/chikuwa/1"], T0)
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

    // A lineage without an announcement (nothing tells its deploy's end)
    // stays in flight however old; the writer's `at` is display only.
    let old = lin(NEW, OTHER, Some((BRANCH, Some(&b))), &["work/chikuwa/1"], T0 - 7200.0);
    assert_eq!(k(Some(&old), &at_base, false).0, Kind::InFlight);

    // An announced increment is in flight while origin's pending ref of
    // its branch is uncleared - whatever commit the renewals left there,
    // dead or not (then said so) - and abandoned the moment it is cleared:
    // its deploy ended without the push.
    let announced = lin_ann(NEW, OTHER, Some((BRANCH, Some(&b))), Some("p"), &["work/chikuwa/1"], T0);
    let mut live = at_base.clone();
    live.in_flight.insert(BRANCH.into(), "p".into());
    let (kind, why) = k(Some(&announced), &live, false);
    assert!(kind == Kind::InFlight && !why.contains("dead"), "{why}");
    live.in_flight.insert(BRANCH.into(), "q".into());
    assert_eq!(k(Some(&announced), &live, false).0, Kind::InFlight);
    live.dead.insert(BRANCH.into());
    let (kind, why) = k(Some(&announced), &live, false);
    assert!(kind == Kind::InFlight && why.contains("that deploy looks dead"), "{why}");
    assert_eq!(classify(Some(&announced), NEW, &live, false, false).0, Kind::LeftAlone);
    let (kind, why) = k(Some(&announced), &at_base, false);
    assert!(kind == Kind::Abandoned && why.contains("the announcement cleared without the push"), "{why}");
    assert_eq!(classify(Some(&announced), NEW, &at_base, false, false).0, Kind::Abandoned);
    assert_eq!(k(Some(&announced), &cx(&[(BRANCH, &landed())]), false).0, Kind::CatchUp);
    // A dead deploy's lineage without an announcement: nothing to go by.
    let mut dead = at_base.clone();
    dead.dead.insert(BRANCH.into());
    let (kind, why) = k(Some(&std_lin()), &dead, false);
    assert!(kind == Kind::InFlight && !why.contains("dead"), "{why}");

    let (kind, why) = k(Some(&std_lin()), &at_base, true);
    assert!(kind == Kind::Overridden && why.contains(RESET_FLAG), "{why}");

    // Refused work on a target this checkout's work does not reach is left
    // there; --reset still reverts it.
    let (kind, why) = classify(Some(&std_lin()), NEW, &at_base, false, false);
    assert!(kind == Kind::LeftAlone && !kind.refused() && why.contains("does not reach it"), "{why}");
    assert_eq!(classify(Some(&old), NEW, &at_base, false, false).0, Kind::LeftAlone);
    assert_eq!(classify(Some(&std_lin()), NEW, &at_base, true, false).0, Kind::Overridden);
    assert_eq!(classify(Some(&std_lin()), NEW, &cx(&[(BRANCH, &landed())]), false, false).0, Kind::CatchUp);

    let unl = lin(NEW, OTHER, Some(("work/M3-Air/9", Some(&b))), &["work/chikuwa/1"], T0);
    let (kind, why) = k(Some(&unl), &cx(&[("work/M3-Air/9", &b)]), false);
    assert!(kind == Kind::Unlisted && why.contains("work/M3-Air/9"), "{why}");
    assert_eq!(k(Some(&unl), &cx(&[("work/M3-Air/9", &landed())]), false).0, Kind::Unlisted);
    // Pushed work of a branch not listed here is refused too.
    let pushed_unl = lin(NEW, OTHER, None, &["work/M3-Air/9"], T0);
    assert_eq!(k(Some(&pushed_unl), &at_base, false).0, Kind::Unlisted);

    assert_eq!(k(None, &at_base, false), (Kind::Untracked, String::new()));
    assert_eq!(parse(""), None);
    assert_eq!(parse("{not json"), None);
    assert_eq!(parse(r#"{"version": 3}"#), None);
    assert_eq!(parse(&doc(NEW, OTHER, None, &[], T0).replace(r#""version":2"#, r#""version":3"#)), None);
    // A version-1 file, from before by.host became by.machine, still counts.
    let v1 = doc(NEW, OTHER, None, &[], T0).replace(r#""version":2"#, r#""version":1"#).replace(r#""machine":"#, r#""host":"#);
    assert_eq!(parse(&v1).map(|l| l.machine), Some(OTHER.0.to_string()));
    assert_eq!(k(Some(&lin(OLD, OTHER, None, &[], T0)), &at_base, false).0, Kind::Untracked);
    assert_eq!(k(Some(&lin(NEW, ME, Some((BRANCH, Some(&b))), &[], T0)), &at_base, false).0, Kind::ThisBranch);
    assert_eq!(k(Some(&lin(NEW, OTHER, None, &["work/chikuwa/1"], T0)), &at_base, false).0, Kind::CatchUp);

    // The nightly's unpushed lock commit on main.
    let nightly = lin(NEW, ("chikuwa", "/var/lib/homelab"), Some(("main", Some(&b))), &[], T0);
    assert_eq!(k(Some(&nightly), &cx(&[("main", &b)]), false).0, Kind::InFlight);
    assert_eq!(k(Some(&nightly), &cx(&[("main", &landed())]), false).0, Kind::CatchUp);
    // Announced under its own ref (main@<machine>): judged by that ref,
    // not by another main checkout's.
    let nightly = lin_ann(NEW, ("chikuwa", "/var/lib/homelab"), Some(("main", Some(&b))), Some("p"), &[], T0);
    assert_eq!(nightly.key.as_deref(), Some("main@chikuwa"));
    let mut live = cx(&[("main", &b)]);
    live.in_flight.insert("main@chikuwa".into(), "q".into());
    assert_eq!(k(Some(&nightly), &live, false).0, Kind::InFlight);
    let mut other = cx(&[("main", &b)]);
    other.in_flight.insert("main@haswell-wsl".into(), "q".into());
    assert_eq!(k(Some(&nightly), &other, false).0, Kind::Abandoned);
    // A branch whose very first push has not landed.
    let first = lin(NEW, OTHER, Some((BRANCH, None)), &[], T0);
    assert_eq!(k(Some(&first), &cx(&[]), false).0, Kind::InFlight);
}

#[test]
fn refusal_advice() {
    let a = refusal(&[("side1".into(), Kind::InFlight, None)]);
    assert!(
        a.contains("side1 runs another")
            && a.contains("reaches it too")
            && a.contains("Re-run once that deploy ends, or push its branch from its checkout; or deploy with"),
        "{a}"
    );
    assert!(a.contains(&format!("{RESET_FLAG} side1")) && !a.contains(BRANCHES_FILE) && !a.contains("dead"), "{a}");
    let a = refusal(&[("side1".into(), Kind::Unlisted, None), ("side6".into(), Kind::InFlight, None)]);
    assert!(a.contains("side1, side6 run another") && a.contains("reaches them too"), "{a}");
    assert!(a.contains("Re-run once that deploy ends, or push its branch from its checkout; or list its branch in deploy-branches"), "{a}");
    assert!(a.contains(&format!("{RESET_FLAG} side1 ")), "{a}");
    // A dead deploy's: the clear that ends it, named once per branch.
    let a = refusal(&[("side1".into(), Kind::InFlight, Some(BRANCH.into())), ("side6".into(), Kind::InFlight, Some(BRANCH.into()))]);
    assert!(a.starts_with(&format!("nothing switched or committed: side1, side6 run another checkout's work this union lacks, and this checkout's work reaches them too (see above).  The deploy of {BRANCH} looks dead")), "{a}");
    assert_eq!(a.matches("looks dead").count(), 1);
    assert!(
        a.contains(&format!("git push --force origin origin/main:{PENDING_REFS}/{BRANCH} - or push {BRANCH} from its checkout")),
        "{a}"
    );
    assert!(!a.contains("Re-run once"), "{a}");
    let a = refusal(&[("side1".into(), Kind::InFlight, Some("main@chikuwa".into())), ("side6".into(), Kind::InFlight, None)]);
    assert!(
        a.contains("from its checkout; or the deploy of main looks dead")
            && a.contains(&format!("{PENDING_REFS}/main@chikuwa - or push main from")),
        "{a}"
    );
}

#[test]
fn clock_is_local() {
    assert_eq!(clock(3600.0 * 5.0 + 60.0 * 7.0 + 59.0, 0), "05:07");
    assert_eq!(clock(0.0, -3600), "23:00");
}

fn params(targets: &[&str], candidates: &[&str], reset: (bool, bool), drifting: &[(&str, Option<String>)]) -> Params {
    let s = |v: &[&str]| v.iter().map(|h| h.to_string()).collect::<Vec<_>>();
    let b = base();
    Params {
        cx: cx(&[(BRANCH, &b)]),
        union: "u".repeat(40),
        router: "choko".into(),
        targets: s(targets),
        candidates: s(candidates),
        reset: reset.0,
        named: reset.1,
        reached: targets.iter().map(|h| h.to_string()).collect(),
        running: targets
            .iter()
            .map(|h| (h.to_string(), Some(if drifting.iter().any(|d| d.0 == *h) { OLD } else { NEW }.to_string())))
            .collect(),
        built: targets.iter().map(|h| (h.to_string(), NEW.to_string())).collect(),
        lineages: drifting.iter().filter_map(|(h, l)| l.clone().map(|l| (h.to_string(), l))).collect(),
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
    let pushed = doc(OLD, OTHER, None, &["work/chikuwa/1"], T0);
    let mut p =
        params(&["chikuwa", "choko"], &["chikuwa", "choko"], (false, false), &[("chikuwa", Some(pushed.clone())), ("choko", Some(pushed))]);
    // The lineage holds BRANCH at h..h and work/chikuwa/1 at c..c; origin
    // has BRANCH reset (at `b`) and work/chikuwa/1 still at c..c.
    p.cx = cx(&[(BRANCH, &b), ("work/chikuwa/1", &"c".repeat(40))]);
    let (asked, fx, out) = run_with(p.clone(), "abandoned");
    assert_eq!(out.unwrap().plan, ["chikuwa", "choko"]);
    assert_eq!(asked, [Query::Fate { branch: BRANCH.into(), tip: "h".repeat(40), union: "u".repeat(40), checkout: Some(OTHER.1.into()) }]);
    let line = format!("  chikuwa: will switch (catch-up: runs pushed work deployed by haswell-wsl:/home/blu/projects/homelab2 (pid 4242) at 13:46; reverting {BRANCH}'s abandoned increment hhhhhhhhhhhh)");
    assert_eq!(fx[0], Effect::Print(line));
    assert!(
        matches!(&fx[1], Effect::Print(l) if l.ends_with("reverting work/haswell-wsl/2's abandoned increment hhhhhhhhhhhh)")),
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
    let sub = "work/haswell-wsl/2-agent-x";
    let first = doc(OLD, OTHER, Some((sub, None)), &[], T0);
    let mut p = params(&["chikuwa"], &["chikuwa"], (false, false), &[("chikuwa", Some(first))]);
    p.cx = cx(&[(BRANCH, &b)]);
    p.cx.listed.insert(sub.into());
    let (asked, _, out) = run_with(p.clone(), "kept");
    assert_eq!(asked.len(), 1);
    assert!(out.unwrap_err().contains("Re-run once that deploy ends"));
    // Deployed from another machine: no checkout here to look at.
    let elsewhere = doc(OLD, ("chikuwa", "/var/lib/homelab"), Some((sub, None)), &[], T0);
    let mut q = p.clone();
    q.lineages.insert("chikuwa".into(), elsewhere);
    let (asked, _, _) = run_with(q, "kept");
    assert!(matches!(&asked[0], Query::Fate { checkout: None, .. }), "{asked:?}");
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

    // A refused or left-alone target, and one whose lineage tips origin
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
    let (fx, out) = run(p.clone());
    let msg = out.unwrap_err();
    assert!(msg.starts_with("nothing switched or committed: side1 runs another"), "{msg}");
    assert_eq!(fx[0], Effect::Print("  side1: refused: runs an increment of work/haswell-wsl/2 deployed by haswell-wsl:/home/blu/projects/homelab2 (pid 4242) at 13:46 whose push has not landed (origin/work/haswell-wsl/2 is still at aaaaaaaaaaaa)".into()));
    assert_eq!(fx[1], Effect::Print("  choko: will switch".into()));
    assert_eq!(fx[2], Effect::Print("  chikuwa: differs but excluded by the target filter".into()));
    assert_eq!(fx.len(), 3);
    // The announced increment of a deploy found dead: refused still, the
    // refusal naming the deploy and the clear that ends it.
    let mut q = p.clone();
    q.lineages.insert("side1".into(), doc_ann(OLD, OTHER, Some((BRANCH, Some(&b))), Some("p"), &[], T0));
    q.cx.in_flight.insert(BRANCH.into(), "q".into());
    q.cx.dead.insert(BRANCH.into());
    let (fx, out) = run(q);
    let msg = out.unwrap_err();
    assert!(msg.contains(&format!("The deploy of {BRANCH} looks dead")) && !msg.contains("Re-run once"), "{msg}");
    assert!(
        matches!(&fx[0], Effect::Print(l) if l.starts_with("  side1: refused: ") && l.ends_with("that deploy looks dead, its announcement no longer renewed")),
        "{fx:?}"
    );

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

    // The in-flight target is outside this checkout's work: left alone, the
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

    // An unnamed --reset leaves it alone too; one naming the targets takes it over.
    p.reset = true;
    let (_, out) = run(p.clone());
    assert_eq!(out.unwrap().drift["side1"].0, Kind::LeftAlone);
    p.named = true;
    let (_, out) = run(p.clone());
    let out = out.unwrap();
    assert_eq!(out.drift["side1"].0, Kind::Overridden);
    assert_eq!(out.plan, ["side1", "choko"]);

    // Named or not, a --reset never takes over a target outside the filter.
    p.candidates = vec!["choko".into()];
    let (fx, out) = run(p);
    assert_eq!(out.unwrap().plan, ["choko"]);
    assert!(fx.contains(&Effect::Print("  side1: differs but excluded by the target filter".into())), "{fx:?}");
}

fn arb_name() -> impl Strategy<Value = String> {
    prop::sample::select(vec!["main", BRANCH, "work/haswell-wsl/3", "work/chikuwa/1", "work/M3-Air/9", "x"]).prop_map(str::to_string)
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
            proptest::option::of(prop::sample::select(vec!["p", "q"])),
            prop::collection::vec(arb_name(), 0..3),
            0.0..1e7f64,
        )
            .prop_map(|(top, mine, pending, announce, others, at)| {
                let others: Vec<&str> = others.iter().map(String::as_str).collect();
                Some(doc_ann(
                    &top,
                    if mine { ME } else { OTHER },
                    pending.as_ref().map(|(b, base)| (b.as_str(), *base)),
                    announce,
                    &others,
                    at,
                ))
            }),
    ]
}

const TARGETS: [&str; 5] = ["chikuwa", "choko", "haswell-wsl", "side1", "side6"];

fn arb_params() -> impl Strategy<Value = Params> {
    let target = || prop::sample::select(TARGETS.to_vec()).prop_map(str::to_string);
    (
        prop::collection::btree_set(target(), 0..5),
        prop::collection::btree_set(target(), 0..5),
        (any::<bool>(), any::<bool>()),
        prop::collection::btree_set(target(), 0..5),
        prop::collection::vec((proptest::option::of(arb_top()), arb_top(), arb_lineage()), 5),
        prop::collection::btree_map(arb_name(), prop::sample::select(vec!["a", "b"]).prop_map(str::to_string), 0..4),
        prop::collection::btree_map(arb_name(), prop::sample::select(vec!["p", "q"]).prop_map(str::to_string), 0..3),
        prop::collection::btree_set(arb_name(), 0..3),
    )
        .prop_map(|(targets, cands, (reset, named), reached, facts, tips, in_flight, dead)| {
            let targets: Vec<String> = targets.into_iter().collect();
            let mut p = Params {
                cx: Context { tips, in_flight, dead, ..cx(&[]) },
                union: "u".repeat(40),
                router: "choko".into(),
                candidates: cands.into_iter().filter(|c| targets.contains(c)).collect(),
                targets: targets.clone(),
                reset,
                named,
                reached,
                running: BTreeMap::new(),
                built: BTreeMap::new(),
                lineages: BTreeMap::new(),
            };
            for (h, (running, built, lineage)) in targets.iter().zip(facts) {
                if running.is_some() {
                    p.built.insert(h.clone(), built);
                }
                p.running.insert(h.clone(), running);
                if let Some(l) = lineage {
                    p.lineages.insert(h.clone(), l);
                }
            }
            p
        })
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 4000, failure_persistence: None, ..ProptestConfig::default() })]

    #[test]
    fn parse_never_panics(text in any::<String>()) {
        let _ = parse(&text);
    }

    /// The verdicts follow from the lineage, and only refusals are
    /// demoted by a cleared announcement or --reset.
    #[test]
    fn verdicts(text in arb_lineage(), running in arb_top(), p in arb_params()) {
        let lin = text.as_deref().and_then(parse);
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
            let mine = (l.machine.as_str(), l.checkout.as_str()) == ME;
            prop_assert_eq!(kind == Kind::ThisBranch, mine);
            let unlisted = l.branches.iter().any(|b| !p.cx.listed.contains(b));
            if kind == Kind::Unlisted { prop_assert!(unlisted); }
            if !mine && unlisted { prop_assert!(matches!(kind, Kind::Unlisted | Kind::Abandoned)); }
            // An announced increment is judged by its branch's announcement
            // alone: uncleared, in flight (dead or not); cleared, abandoned.
            // One without is in flight until its push lands or a --reset.
            let announced_live = l.announce.is_some() && l.pending.as_ref().is_some_and(|(b, _)| p.cx.in_flight.contains_key(l.key.as_deref().unwrap_or(b)));
            if kind == Kind::InFlight {
                let (b, base) = l.pending.clone().unwrap();
                prop_assert_eq!(p.cx.tips.get(&b), base.as_ref());
                prop_assert!(l.announce.is_none() || announced_live);
            }
            prop_assert_eq!(kind == Kind::Abandoned, !mine && !unlisted && l.announce.is_some() && !announced_live && l.pending.as_ref().is_some_and(|(b, base)| p.cx.tips.get(b) == base.as_ref()));
        }
        if kind.refused() {
            prop_assert_eq!(over, Kind::Overridden);
        } else {
            prop_assert_eq!(over, kind);
        }
    }

    /// The plan follows classify, except that a pending increment of a
    /// branch origin has no tip of is caught up (never refused or left
    /// alone) once its fate is not "kept".
    #[test]
    fn plans(p in arb_params(), fate in prop::sample::select(vec!["kept", "landed", "abandoned"])) {
        let (_, fx, out) = run_with(p.clone(), fate);
        let affected: Vec<String> = switch_order(&p.targets, &p.router).into_iter()
            .filter(|h| matches!((p.running.get(h), p.built.get(h)), (Some(Some(r)), Some(b)) if r != b))
            .collect();
        let verdicts: Vec<(String, Kind)> = affected.iter().filter(|h| p.candidates.contains(h)).map(|h| {
            let lin = p.lineages.get(h).and_then(|t| parse(t));
            let running = p.running[h].clone().unwrap();
            let reached = p.reached.contains(h);
            let gone = lin.as_ref().is_some_and(|l| matches!(&l.pending, Some((b, None)) if !p.cx.tips.contains_key(b) && l.tips.contains_key(b)));
            if fate != "kept" && gone && classify(lin.as_ref(), &running, &p.cx, false, true).0 == Kind::InFlight {
                return (h.clone(), Kind::CatchUp);
            }
            (h.clone(), classify(lin.as_ref(), &running, &p.cx, p.reset && (p.named || reached), reached).0)
        }).collect();
        let refused: Vec<(String, Kind, Option<String>)> = verdicts.iter().filter(|v| v.1.refused()).map(|(h, k)| {
            let lin = p.lineages.get(h).and_then(|t| parse(t));
            let dead = lin.filter(|l| l.announce.is_some()).and_then(|l| l.pending.as_ref().map(|(b, _)| l.key.clone().unwrap_or_else(|| b.clone()))).filter(|key| *k == Kind::InFlight && p.cx.dead.contains(key));
            (h.clone(), *k, dead)
        }).collect();
        // A refusal needs a reached target, which a --reset takes over.
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
        // One report line per candidate and per excluded drifting target.
        let printed = fx.iter().filter(|e| matches!(e, Effect::Print(_))).count();
        prop_assert_eq!(printed, p.candidates.len() + affected.iter().filter(|h| !p.candidates.contains(h)).count());
        prop_assert!(!fx.iter().any(|e| matches!(e, Effect::Warn(_))));
    }
}
