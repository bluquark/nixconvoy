//! nixconvoy's step 5 verdict on the targets whose running system differs
//! from the union: whose work each runs (its lineage, as nixconvoy's
//! lineage_doc wrote it), and so which of them the deploy may switch.
//!
//! A target runs another checkout's increment that this union cannot contain
//! when that checkout's push has not landed (origin's tip of its branch is
//! still the one it built on) or when it holds work of a branch
//! deploy-branches here does not list.  Switching it would revert that work
//! until its owner redeploys, so it is never switched as is - unless that
//! deploy is over: its increment's announcement (the lineage's
//! `pending.announce`, origin's pending ref) is no longer in flight
//! (Context::in_flight), the push that would have cleared it having never
//! landed; a lineage without one older than PENDING_MAX_AGE by the target's
//! clock counts as a dead deploy's likewise - or the deploy is a --reset
//! that names the target or reaches it.  When this checkout's own work does not reach the target
//! (Params::reached: no file its configuration reads changed), the target is
//! simply left running that work and the deploy goes on without it; when it
//! does, two checkouts are changing the same target and the deploy is refused
//! whole: switching the rest would leave them carrying this run's unpushed
//! claim beside the other's, with neither increment able to land cleanly.
//!
//! A target to be switched may run an increment that left the convoy since:
//! a branch tip its lineage recorded that origin's tip of the branch no
//! longer carries.  Query::Fate asks git whether this deploy's union still
//! holds it and, when not, what became of it - abandoned (reset or
//! dropped, landed nowhere) or landed and since undone - and the target's
//! verdict line names each abandoned increment the switch reverts:
//! abandon itself touches no target, so this is where the revert shows.  A
//! subagent's branch, pushed by its first deploy and deleted with its
//! worktree, leaves targets whose lineage holds a pending increment of a
//! branch origin never had as far as this run can see: the same query
//! tells a dropped branch (switched over, the increment named) from one
//! whose first push is on its way (refused, as any in-flight increment).
use super::{listed, Halt, Query, Run, Script, BRANCHES_FILE};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

/// A push follows its switch within seconds (the nightly's main, once its
/// four targets switched): a lineage without an announcement still refused
/// this long after it was written (by the target's clock) is a dead
/// deploy's, and its target is fair game again.  nixconvoy drops an
/// announcement this old from Context::in_flight the same way.
pub const PENDING_MAX_AGE: f64 = 30.0 * 60.0;
pub const RESET_FLAG: &str = "--reset";

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
    /// The pending increment's announcement on origin, cleared by the push
    /// that ends the deploy.
    pub announce: Option<String>,
    pub at: f64,
    pub machine: String,
    pub checkout: String,
    pub pid: i64,
    /// Seconds since it was written, by the target's own clock (the file's
    /// mtime): the machines' clocks drift, a WSL one after every sleep.
    pub age: Option<f64>,
}

/// A lineage file's contents, or None when absent or malformed.
pub fn parse(text: &str, age: Option<f64>) -> Option<Lineage> {
    let v: Value = serde_json::from_str(text).ok()?;
    let o = v.as_object()?;
    // Version 1 named the machine `by.host`; read, never written.
    // pending.announce came later without a new version: a reader from
    // before it ignores the field.
    let version = o.get("version").and_then(Value::as_i64)?;
    if !(1..=2).contains(&version) {
        return None;
    }
    let s = |v: Option<&Value>| v.and_then(Value::as_str).map(str::to_string);
    let by = o.get("by")?.as_object()?;
    let mut announce = None;
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
        at: o.get("at")?.as_f64()?,
        machine: s(by.get("machine")).or_else(|| s(by.get("host")))?,
        checkout: s(by.get("checkout"))?,
        pid: by.get("pid").and_then(Value::as_i64).unwrap_or(0),
        age,
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
    /// Would be refused, but this checkout's work does not reach the target:
    /// left running the other checkout's work, not switched.
    LeftAlone,
    /// Refused, but its deploy is over (its announcement cleared without
    /// the push landing) or, without an announcement, it was written more
    /// than PENDING_MAX_AGE ago.
    Abandoned,
    /// Refused, but taken over by a --reset that names or reaches the target.
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
            Kind::LeftAlone => "left-alone",
            Kind::Abandoned => "abandoned",
            Kind::Overridden => "overridden",
        }
    }

    pub fn refused(self) -> bool {
        matches!(self, Kind::InFlight | Kind::Unlisted)
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
    /// The announced increment of each branch whose deploy is under way:
    /// origin's pending refs as fetched, a dead deploy's left out.
    pub in_flight: BTreeMap<String, String>,
    pub now: f64,
    /// Seconds east of UTC, for the times shown.
    pub tz_offset: i64,
}

fn clock(at: f64, tz_offset: i64) -> String {
    let t = (at.floor() as i64).saturating_add(tz_offset).rem_euclid(86_400);
    format!("{:02}:{:02}", t / 3600, t % 3600 / 60)
}

/// Whose work a drifting target runs, relative to this deploy's union, and
/// why; `take_over` when a --reset names the target or reaches it, `reached`
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
        let why = format!("runs an increment of {branch} deployed by {who} whose push has not landed (origin/{branch} is still at {at})");
        if let Some(a) = &lin.announce {
            if cx.in_flight.get(branch) != Some(a) {
                return (Kind::Abandoned, format!("{why}; its deploy is over without one, so taken as abandoned"));
            }
        }
        (Kind::InFlight, why)
    } else {
        return (Kind::CatchUp, format!("runs pushed work deployed by {who}"));
    };
    let age = lin.age.unwrap_or(cx.now - lin.at);
    if (kind == Kind::Unlisted || lin.announce.is_none()) && age > PENDING_MAX_AGE {
        return (Kind::Abandoned, format!("{why}; {:.0} min old, so taken as abandoned", age / 60.0));
    }
    if take_over {
        return (Kind::Overridden, format!("{why}; taken over by {RESET_FLAG}"));
    }
    if !reached {
        return (Kind::LeftAlone, format!("{why}; this checkout's work does not reach it"));
    }
    (kind, why)
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
    /// --reset: this checkout's view alone - refused work is taken over on
    /// the targets it reaches, and on every candidate when `named`.
    pub reset: bool,
    /// The candidates were named on the command line.
    pub named: bool,
    /// The targets this checkout's own work reaches: those whose
    /// configuration reads a file that work changed (nixconvoy's
    /// reached_targets, by the eval memo's scan).
    pub reached: BTreeSet<String>,
    /// Each probed target's running system (None: unreachable).
    pub running: BTreeMap<String, Option<String>>,
    /// The union's toplevel of each reachable target.
    pub built: BTreeMap<String, String>,
    /// Each probed target's lineage file and its age by the target's clock.
    pub lineages: BTreeMap<String, (String, Option<f64>)>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Outcome {
    /// Targets whose running system differs from the union.
    pub affected: Vec<String>,
    /// The targets to switch, in switch order.
    pub plan: Vec<String>,
    pub drift: BTreeMap<String, (Kind, String)>,
}

/// The die message of a deploy refusing `refused` (in report order).
pub fn refusal(refused: &[(String, Kind)]) -> String {
    let targets: Vec<&str> = refused.iter().map(|(h, _)| h.as_str()).collect();
    let mut fixes = Vec::new();
    if refused.iter().any(|(_, k)| *k == Kind::InFlight) {
        fixes.push("re-run once that deploy ends".to_string());
    }
    if refused.iter().any(|(_, k)| *k == Kind::Unlisted) {
        fixes.push(format!("list its branch in {BRANCHES_FILE} (`nixconvoy pull` once that line is on main)"));
    }
    let fix = fixes.join(" or ");
    let mut c = fix.chars();
    let fix: String = c.next().map(|f| f.to_uppercase().chain(c).collect()).unwrap_or_default();
    format!(
        "nothing switched or committed: {} {} another checkout's work this union lacks, and this checkout's work reaches {} too (see above).  {fix} (an increment whose deploy ended without its push, or whose announcement is {} min old, counts as abandoned), or deploy with {RESET_FLAG} {} to revert it deliberately - along with every other branch's work there, which {RESET_FLAG} leaves out.",
        targets.join(", "),
        if targets.len() == 1 { "runs" } else { "run" },
        if targets.len() == 1 { "it" } else { "them" },
        PENDING_MAX_AGE as u64 / 60,
        targets.first().copied().unwrap_or("")
    )
}

/// Query::Fate's answer for `branch`'s increment at `tip` of the lineage
/// `lin`, asked once per tip (`fates` memoises it across targets): "kept",
/// "landed" or "abandoned".  The query names the deploying checkout when
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
/// holds, as "<branch>'s abandoned increment <sha>" (or "landed and since
/// undone").
fn reverted(s: &mut Script, lin: &Lineage, p: &Params, fates: &mut BTreeMap<(String, String), String>) -> Run<Vec<String>> {
    let mut notes = Vec::new();
    for (b, tip) in &lin.tips {
        if p.cx.tips.get(b) == Some(tip) {
            continue;
        }
        let short: String = tip.chars().take(12).collect();
        match fate(s, b, tip, lin, p, fates)?.as_str() {
            "abandoned" => notes.push(format!("{b}'s abandoned increment {short}")),
            "landed" => notes.push(format!("{b}'s increment {short}, landed and since undone")),
            _ => {}
        }
    }
    Ok(notes)
}

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    let mut affected = Vec::new();
    for h in switch_order(&p.targets, &p.router) {
        let Some(Some(running)) = p.running.get(&h) else { continue };
        let Some(built) = p.built.get(&h) else {
            return Err(Halt::Die(format!("deploy-plan was given no evaluated toplevel for {h}")));
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
        let lin = p.lineages.get(h).and_then(|(text, age)| parse(text, *age));
        let running = p.running.get(h).cloned().flatten().unwrap_or_default();
        let reached = p.reached.contains(h);
        let (mut kind, mut why) = classify(lin.as_ref(), &running, &p.cx, p.reset && (p.named || reached), reached);
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
        if kind.refused() {
            refused.push((h.clone(), kind));
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
