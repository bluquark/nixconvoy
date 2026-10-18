//! nixconvoy's step 5 verdict on the targets whose running system differs
//! from the union: whose work each runs (its lineage, as nixconvoy's
//! lineage_doc wrote it), and so which of them the deploy may switch.
//!
//! A target runs another checkout's increment that this union cannot contain
//! when that checkout's push has not landed (origin's tip of its branch is
//! still the one it built on) or when it holds work of a branch
//! deploy-branches here does not list.  Switching it would revert that work
//! until its owner redeploys, so it is never switched as is - unless that
//! deploy is over: the lineage names its increment's announcement
//! (`pending.announce`, its ref `pending.key`) and that ref is cleared
//! (Context::in_flight lacks it), which only the deploy's own end does -
//! its push, or its withdrawal having switched nothing - or the removal
//! of its branch, or a hand clear; or the deploy is a --evict that names
//! the target or reaches it.  Work of a branch deploy-branches here does
//! not list stays refused until listed.  A deploy that died leaves its announcement up
//! (nixconvoy tells one from its stopped renewals: Context::dead), so
//! its target stays refused, the refusal naming the deploy and the
//! clear that ends it; nothing is taken over on a clock.  When this
//! checkout's own work does not reach the target (Params::reached: no
//! file its configuration reads changed), the target is simply left
//! running that work and the deploy goes on without it; when it does,
//! two checkouts are changing the same target and the deploy is refused
//! whole: switching the rest would leave them carrying this run's
//! unpushed claim beside the other's, with neither increment able to
//! land cleanly.
//!
//! A target to be switched may run an increment that left the convoy since:
//! a branch tip its lineage recorded that origin's tip of the branch no
//! longer carries.  Query::Fate asks git whether this deploy's union still
//! holds it ("kept") or not ("abandoned": reset, dropped, force-pushed
//! over, or landed and since undone), and the target's verdict line names
//! each abandoned increment the switch reverts:
//! unapply runs that switch itself; a branch reset by hand waits for the
//! next deploy by anyone, and this is where the revert shows.  A
//! subagent's branch, pushed by its first deploy and deleted with its
//! worktree, leaves targets whose lineage holds a pending increment of a
//! branch origin never had as far as this run can see: the same query
//! tells a dropped branch (switched over, the increment named) from one
//! whose first push is on its way (refused, as any in-flight increment).
//!
//! A branch holds a target back when an increment of it reached the target
//! but was deployed to other targets alone (an apply naming them): its
//! Reaches: trailer names the target, its Applied: one does not, and no
//! later increment's deploy on that branch switched the target since
//! (nixconvoy's held_targets).  The union carries that work, so switching
//! the target would roll the increment out from a checkout that chose not
//! to: a held target is left alone, or the deploy refused when this
//! checkout's own work reaches it (Params::held), as for an in-flight
//! increment - until the holding checkout deploys the target or lands with
//! --not-yet-applied.  A main checkout's own work is main's advance, which
//! a hold must not stop fleet-wide (the nightly's lock): it is left behind on
//! the held target instead (Params::work_branch).  A --evict unions no
//! other branch, so nothing is held under it: the target gets this
//! checkout's view alone, the hold standing.
use super::{listed, Halt, Query, Run, Script, BRANCHES_FILE};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

pub const EVICT_FLAG: &str = "--evict";
/// Origin's announcements, by branch (nixconvoy's PENDING_REFS).
pub const PENDING_REFS: &str = "refs/nixconvoy/pending";

#[derive(Clone, Debug, PartialEq)]
pub struct Lineage {
    pub toplevel: String,
    pub union: String,
    /// Every branch the union holds work of (the deploying checkout's own included),
    /// sorted.
    pub branches: Vec<String>,
    /// The tip of each of those the union held (the deploying checkout's own: the
    /// work-branch commit its pending increment became).
    pub tips: BTreeMap<String, String>,
    /// The deploying checkout's own branch when what it unioned of it was not on
    /// origin yet, with the origin tip that push moves.
    pub pending: Option<(String, Option<String>)>,
    /// The pending increment's announcement on origin as the deploy made
    /// it (renewed since; what counts is whether its ref is cleared) and
    /// that ref's name (nixconvoy's announce_key; the branch when absent).
    pub announce: Option<String>,
    pub key: Option<String>,
    pub at: f64,
    pub machine: String,
    pub checkout: String,
    pub pid: i64,
}

/// A lineage file's contents, or None when absent or malformed.
pub fn parse(text: &str) -> Option<Lineage> {
    let v: Value = serde_json::from_str(text).ok()?;
    let o = v.as_object()?;
    // Version 1 names the machine `by.host`; pending.announce is optional
    // in version 2.
    let version = o.get("version").and_then(Value::as_i64)?;
    if !(1..=2).contains(&version) {
        return None;
    }
    let s = |v: Option<&Value>| v.and_then(Value::as_str).map(str::to_string);
    let by = o.get("by")?.as_object()?;
    let mut announce = None;
    let mut key = None;
    let pending = match o.get("pending") {
        None | Some(Value::Null) => None,
        Some(Value::Object(p)) => {
            let base = match p.get("base") {
                None | Some(Value::Null) => None,
                Some(Value::String(b)) => Some(b.clone()),
                Some(_) => return None,
            };
            announce = match p.get("announce") {
                None | Some(Value::Null) => None,
                Some(Value::String(a)) => Some(a.clone()),
                Some(_) => return None,
            };
            key = match p.get("key") {
                None | Some(Value::Null) => None,
                Some(Value::String(k)) => Some(k.clone()),
                Some(_) => return None,
            };
            Some((s(p.get("branch"))?, base))
        }
        Some(_) => return None,
    };
    let held = o.get("branches")?.as_object()?;
    let mut branches: Vec<String> = held.keys().cloned().collect();
    branches.sort();
    let tips = held.iter().filter_map(|(b, t)| Some((b.clone(), t.as_str()?.to_string()))).collect();
    Some(Lineage {
        toplevel: s(o.get("toplevel"))?,
        union: s(o.get("union"))?,
        branches,
        tips,
        pending,
        announce,
        key,
        at: o.get("at")?.as_f64()?,
        machine: s(by.get("machine")).or_else(|| s(by.get("host")))?,
        checkout: s(by.get("checkout"))?,
        pid: by.get("pid").and_then(Value::as_i64).unwrap_or(0),
    })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Kind {
    /// No lineage for the system it runs: as before lineages.
    Untracked,
    /// This checkout deployed it, so its union carries it.
    ThisBranch,
    /// Another checkout's, all of it pushed (or its pending push landed
    /// since): contained in the refs this union reads.
    CatchUp,
    /// Another checkout's increment whose push has not landed - refused.
    InFlight,
    /// Work of a branch deploy-branches does not list here - refused.
    Unlisted,
    /// Held back by another branch whose work the union carries - refused.
    Held,
    /// Would be refused, but this checkout's work does not reach the target:
    /// left running the other checkout's work, not switched.
    LeftAlone,
    /// Refused, but its deploy is over: its announcement cleared without
    /// the push landing.
    Abandoned,
    /// Refused, but taken over by a --evict that names or reaches the target.
    Overridden,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Untracked => "untracked",
            Kind::ThisBranch => "this-branch",
            Kind::CatchUp => "catch-up",
            Kind::InFlight => "in-flight",
            Kind::Unlisted => "unlisted",
            Kind::Held => "held",
            Kind::LeftAlone => "left-alone",
            Kind::Abandoned => "abandoned",
            Kind::Overridden => "overridden",
        }
    }

    pub fn refused(self) -> bool {
        matches!(self, Kind::InFlight | Kind::Unlisted | Kind::Held)
    }
}

/// What the deploy knows when it classifies.
#[derive(Clone, Debug, PartialEq)]
pub struct Context {
    /// This checkout: (machine, checkout directory).
    pub me: (String, String),
    /// The branches its union may contain: deploy-branches' patterns plus
    /// main.
    pub listed: BTreeSet<String>,
    /// This run's fetched origin tip of each branch.
    pub tips: BTreeMap<String, String>,
    /// The uncleared announcements by ref name (a lineage's `key`):
    /// origin's pending refs as fetched, this run's own left out.
    pub in_flight: BTreeMap<String, String>,
    /// The announcements (by ref name) whose deploy nixconvoy found dead:
    /// its process gone on this machine, or no renewal while waited for.
    pub dead: BTreeSet<String>,
    /// Seconds east of UTC, for the times shown.
    pub tz_offset: i64,
}

fn clock(at: f64, tz_offset: i64) -> String {
    let t = (at.floor() as i64).saturating_add(tz_offset).rem_euclid(86_400);
    format!("{:02}:{:02}", t / 3600, t % 3600 / 60)
}

/// Whose work a drifting target runs, relative to this deploy's union, and
/// why; `take_over` when a --evict names the target or reaches it, `reached`
/// when this checkout's own work changes something the target's configuration
/// reads.
pub fn classify(lin: Option<&Lineage>, running: &str, cx: &Context, take_over: bool, reached: bool) -> (Kind, String) {
    let Some(lin) = lin.filter(|l| l.toplevel == running) else {
        return (Kind::Untracked, String::new());
    };
    let when = clock(lin.at, cx.tz_offset);
    if (&lin.machine, &lin.checkout) == (&cx.me.0, &cx.me.1) {
        return (Kind::ThisBranch, format!("over this checkout's deploy of {when}"));
    }
    let who = format!("{}:{} (pid {}) at {when}", lin.machine, lin.checkout, lin.pid);
    let unknown: Vec<&str> = lin.branches.iter().filter(|b| !listed(cx.listed.iter().map(String::as_str), b)).map(String::as_str).collect();
    let (kind, why) = if !unknown.is_empty() {
        (
            Kind::Unlisted,
            format!(
                "runs work of {} deployed by {who}, which {BRANCHES_FILE} here does not list, so no union here contains it",
                unknown.join(", ")
            ),
        )
    } else if let Some((branch, base)) = &lin.pending {
        if cx.tips.get(branch) != base.as_ref() {
            return (Kind::CatchUp, format!("runs {branch}'s increment deployed by {who}, pushed since"));
        }
        let at: String = base.as_deref().unwrap_or("nothing").chars().take(12).collect();
        let mut why =
            format!("runs an increment of {branch} deployed by {who} whose push has not landed (origin/{branch} is still at {at})");
        if lin.announce.is_some() {
            let key = lin.key.as_deref().unwrap_or(branch);
            if !cx.in_flight.contains_key(key) {
                return (
                    Kind::Abandoned,
                    format!("{why}; its deploy is over, the announcement cleared without the push, so taken as abandoned"),
                );
            }
            if cx.dead.contains(key) {
                why = format!("{why}; that deploy looks dead, its announcement no longer renewed");
            }
        }
        (Kind::InFlight, why)
    } else {
        return (Kind::CatchUp, format!("runs pushed work deployed by {who}"));
    };
    if take_over {
        return (Kind::Overridden, format!("{why}; taken over by {EVICT_FLAG}"));
    }
    if !reached {
        return (Kind::LeftAlone, format!("{why}; this checkout's work does not reach it"));
    }
    (kind, why)
}

/// The verdict on a target the branches `by` hold back (Params::held):
/// refused when `reached` by a work branch's own work, left alone
/// otherwise (a main checkout's own work being main's advance).
pub fn held(by: &[String], reached: bool, work_branch: bool) -> (Kind, String) {
    let why = format!("held back by {}: the union carries its work, deployed from that checkout to other targets alone", by.join(", "));
    if !reached {
        return (Kind::LeftAlone, format!("{why}; this checkout's work does not reach it"));
    }
    if !work_branch {
        return (Kind::LeftAlone, format!("{why}; left behind on main's advance"));
    }
    (Kind::Held, why)
}

/// The order targets are switched and reported in: the router last, so
/// everything else still has connectivity while it copies.
pub fn switch_order(names: &[String], router: &str) -> Vec<String> {
    let mut v = names.to_vec();
    v.sort_by(|a, b| (a == router, a).cmp(&(b == router, b)));
    v
}

#[derive(Clone, Debug, PartialEq)]
pub struct Params {
    pub cx: Context,
    /// The union commit this deploy builds.
    pub union: String,
    pub router: String,
    pub targets: Vec<String>,
    pub candidates: Vec<String>,
    /// --evict: this checkout's view alone - refused work is taken over on
    /// the targets it reaches, and on every candidate when `named`.
    pub evict: bool,
    /// The candidates were named on the command line.
    pub named: bool,
    /// This checkout deploys a work branch of its own: a hold refuses it
    /// when its work reaches the target.  A main checkout is left behind
    /// on the target instead.
    pub work_branch: bool,
    /// The targets this checkout's own work reaches: those whose
    /// configuration reads a file that work changed (nixconvoy's
    /// reached_targets, by the eval memo's scan).
    pub reached: BTreeSet<String>,
    /// Each probed target's running system (None: unreachable).
    pub running: BTreeMap<String, Option<String>>,
    /// The union's toplevel of each reachable target.
    pub built: BTreeMap<String, String>,
    /// Each target held back, by the other branches holding it (sorted).
    pub held: BTreeMap<String, Vec<String>>,
    /// Each probed target's lineage file.
    pub lineages: BTreeMap<String, String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Outcome {
    /// Targets whose running system differs from the union.
    pub affected: Vec<String>,
    /// The targets to switch, in switch order.
    pub plan: Vec<String>,
    pub drift: BTreeMap<String, (Kind, String)>,
}

/// The die message of a deploy refusing `refused` (in report order), each
/// with the announcement ref of its dead deploy when the refusal is one.
pub fn refusal(refused: &[(String, Kind, Option<String>)]) -> String {
    let (held, refused): (Vec<_>, Vec<_>) = refused.iter().cloned().partition(|(_, k, _)| *k == Kind::Held);
    let held: Vec<&str> = held.iter().map(|(h, _, _)| h.as_str()).collect();
    let targets: Vec<&str> = refused.iter().map(|(h, _, _)| h.as_str()).collect();
    if targets.is_empty() {
        let one = held.len() == 1;
        return format!(
            "nothing switched or committed: {} {} held back by another branch (see above): the union carries work that checkout deployed to other targets alone, and this checkout's work reaches {} too.  Deploy it there (nixconvoy apply {} from that checkout) or land it with --not-yet-applied; or deploy with {EVICT_FLAG} {} to switch {} to this checkout's view alone, without the held work (and without every other branch's), the hold standing.",
            held.join(", "),
            if one { "is" } else { "are" },
            if one { "it" } else { "them" },
            held.join(" "),
            held.join(" "),
            if one { "it" } else { "them" }
        );
    }
    let mut fixes = Vec::new();
    if refused.iter().any(|(_, k, d)| *k == Kind::InFlight && d.is_none()) {
        fixes.push("re-run once that deploy ends, or push its branch from its checkout".to_string());
    }
    let dead: BTreeSet<&str> = refused.iter().filter_map(|(_, _, d)| d.as_deref()).collect();
    for key in dead {
        let branch = key.split('@').next().unwrap_or(key);
        fixes.push(format!(
            "the deploy of {branch} looks dead (its announcement is no longer renewed): clear it to have the next deploy revert its increment - git push --force origin origin/main:{PENDING_REFS}/{key} - or push {branch} from its checkout to keep it"
        ));
    }
    if refused.iter().any(|(_, k, _)| *k == Kind::Unlisted) {
        fixes.push(format!("list its branch in {BRANCHES_FILE} (`nixconvoy pull` once that line is on main)"));
    }
    if !held.is_empty() {
        fixes.push(format!(
            "{} held back by another branch: deploy it there (nixconvoy apply {} from that checkout) or land it with --not-yet-applied",
            held.join(", "),
            held.join(" ")
        ));
    }
    let fix = fixes.join("; or ");
    let mut c = fix.chars();
    let fix: String = c.next().map(|f| f.to_uppercase().chain(c).collect()).unwrap_or_default();
    format!(
        "nothing switched or committed: {} {} another checkout's work this union lacks, and this checkout's work reaches {} too (see above).  {fix}; or deploy with {EVICT_FLAG} {} to revert it deliberately - along with every other branch's work there, which {EVICT_FLAG} leaves out.",
        targets.join(", "),
        if targets.len() == 1 { "runs" } else { "run" },
        if targets.len() == 1 { "it" } else { "them" },
        targets.first().copied().unwrap_or("")
    )
}

/// Query::Fate's answer for `branch`'s increment at `tip` of the lineage
/// `lin`, asked once per tip (`fates` memoises it across targets): "kept"
/// or "abandoned".  The query names the deploying checkout when
/// it is on this machine: one still on the branch keeps the increment.
fn fate(s: &mut Script, branch: &str, tip: &str, lin: &Lineage, p: &Params, fates: &mut BTreeMap<(String, String), String>) -> Run<String> {
    let key = (branch.to_string(), tip.to_string());
    if let Some(f) = fates.get(&key) {
        return Ok(f.clone());
    }
    let checkout = (lin.machine == p.cx.me.0).then(|| lin.checkout.clone());
    let f = s.text(Query::Fate { branch: branch.into(), tip: tip.into(), union: p.union.clone(), checkout })?.unwrap_or_default();
    fates.insert(key, f.clone());
    Ok(f)
}

/// The increments a switch of a target reverts: each branch tip its lineage
/// recorded that is not origin's tip now and that the union no longer
/// holds, as "<branch>'s abandoned increment <sha>".
fn reverted(s: &mut Script, lin: &Lineage, p: &Params, fates: &mut BTreeMap<(String, String), String>) -> Run<Vec<String>> {
    let mut notes = Vec::new();
    for (b, tip) in &lin.tips {
        if p.cx.tips.get(b) == Some(tip) {
            continue;
        }
        let short: String = tip.chars().take(12).collect();
        if fate(s, b, tip, lin, p, fates)? == "abandoned" {
            notes.push(format!("{b}'s abandoned increment {short}"));
        }
    }
    Ok(notes)
}

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    let mut affected = Vec::new();
    for h in switch_order(&p.targets, &p.router) {
        let Some(Some(running)) = p.running.get(&h) else { continue };
        let Some(built) = p.built.get(&h) else {
            return Err(Halt::Die(format!("nixconvoy-plan was given no evaluated toplevel for {h}")));
        };
        if built != running {
            affected.push(h);
        }
    }
    let mut drift = BTreeMap::new();
    let mut plan = Vec::new();
    let mut refused = Vec::new();
    let mut fates = BTreeMap::new();
    for h in affected.iter().filter(|h| p.candidates.contains(h)) {
        let lin = p.lineages.get(h).and_then(|text| parse(text));
        let running = p.running.get(h).cloned().flatten().unwrap_or_default();
        let reached = p.reached.contains(h);
        let take_over = p.evict && (p.named || reached);
        let (mut kind, mut why) = classify(lin.as_ref(), &running, &p.cx, take_over, reached);
        // The pending increment of a branch origin has no tip of: its
        // first push on its way (in flight), or the branch deleted since
        // with its subagent worktree - then whatever the union lacks of
        // it is abandoned, and the target catches up.
        let in_flight = classify(lin.as_ref(), &running, &p.cx, false, true).0 == Kind::InFlight;
        if let Some(lin) = lin.as_ref().filter(|_| in_flight) {
            if let Some((b, None)) = &lin.pending {
                if !p.cx.tips.contains_key(b) {
                    if let Some(tip) = lin.tips.get(b) {
                        if fate(s, b, tip, lin, p, &mut fates)? != "kept" {
                            let who = format!("{}:{} (pid {})", lin.machine, lin.checkout, lin.pid);
                            (kind, why) = (Kind::CatchUp, format!("runs {b}'s increment deployed by {who}, the branch gone since"));
                        }
                    }
                }
            }
        }
        // A hold (never under --evict: no other branch is unioned) over
        // any verdict but a refusal of its own, which says more.
        let hold = p.held.get(h).filter(|by| !by.is_empty() && !kind.refused() && !p.evict);
        if let Some(by) = hold {
            (kind, why) = held(by, reached, p.work_branch);
        }
        if kind.refused() {
            let dead = lin
                .as_ref()
                .filter(|l| l.announce.is_some())
                .and_then(|l| l.pending.as_ref().map(|(b, _)| l.key.clone().unwrap_or_else(|| b.clone())))
                .filter(|k| kind == Kind::InFlight && p.cx.dead.contains(k));
            refused.push((h.clone(), kind, dead));
        } else if kind != Kind::LeftAlone {
            plan.push(h.clone());
        }
        // A catch-up to pushed work may be the revert of an abandoned
        // increment; the other kinds revert or refuse in so many words.
        if let (Kind::ThisBranch | Kind::CatchUp, Some(lin)) = (kind, lin.as_ref()) {
            let notes = reverted(s, lin, p, &mut fates)?;
            if !notes.is_empty() {
                why = format!("{why}; reverting {}", notes.join(", "));
            }
        }
        drift.insert(h.clone(), (kind, why));
    }
    for h in switch_order(&p.candidates, &p.router) {
        s.print(match drift.get(&h) {
            Some((k, why)) if k.refused() => format!("  {h}: refused: {why}"),
            None => format!("  {h}: up to date"),
            Some((Kind::Abandoned | Kind::Overridden, why)) => format!("  {h}: will switch, reverting it: {why}"),
            Some((Kind::LeftAlone, why)) => format!("  {h}: left alone: {why}"),
            Some((k, why)) if !why.is_empty() => format!("  {h}: will switch ({}: {why})", k.name()),
            Some(_) => format!("  {h}: will switch"),
        });
    }
    for h in &affected {
        if !drift.contains_key(h) {
            s.print(format!("  {h}: differs but excluded by the target filter"));
        }
    }
    if !refused.is_empty() {
        return Err(Halt::Die(refusal(&refused)));
    }
    Ok(Outcome { affected, plan, drift })
}

#[cfg(test)]
mod tests;
