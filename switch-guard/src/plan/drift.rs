//! nixconvoy's step 5 verdict on the targets whose running system differs
//! from the union: whose work each runs (its provenance, as nixconvoy's
//! provenance_doc wrote it), and so which of them the deploy may switch.
//!
//! A target runs another checkout's increment that this union cannot contain
//! when that checkout's push has not landed (origin's tip of its branch is
//! still the one it built on) or when it holds work of a branch not
//! shaped like branch_prefix here.  Switching it would revert that work
//! until its owner redeploys, so it is never switched as is - unless that
//! deploy is over: the provenance names its increment's announcement
//! (`pending.announce`, its ref `pending.key`) and that ref is cleared
//! (Context::in_flight lacks it), which only the deploy's own end does -
//! its push, or its withdrawal having switched nothing - or the removal
//! of its branch, or a hand clear; or the deploy is a --evict that names
//! the target or reaches it.  Work of a branch not shaped like
//! branch_prefix here stays refused until it is.  A deploy that died leaves its announcement up
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
//! a branch tip its provenance recorded that origin's tip of the branch no
//! longer carries.  Query::Fate asks git whether this deploy's union still
//! holds it ("kept") or not ("abandoned": reset, dropped, force-pushed
//! over, or landed and since undone), and the target's verdict line names
//! each abandoned increment the switch reverts:
//! undeploy runs that switch itself; a branch reset by hand waits for the
//! next deploy by anyone, and this is where the revert shows.  A
//! subagent's branch, pushed by its first deploy and deleted with its
//! worktree, leaves targets whose provenance holds a pending increment of a
//! branch origin never had as far as this run can see: the same query
//! tells a dropped branch (switched over, the increment named) from one
//! whose first push is on its way (refused, as any in-flight increment).
//!
//! Each target is judged against the union of its own lane (Params::unions;
//! the union core's Lane): the other branches at the commits its deploy
//! record holds of them, so a switch never rolls out what a branch's own
//! checkout has not deployed there.
use super::{listed, Halt, Names, Query, Run, Script, BRANCHES_KEY};
use crate::text;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

pub const EVICT_FLAG: &str = "--evict";
/// Origin's announcements, by branch (nixconvoy's PENDING_REFS).
pub const PENDING_REFS: &str = "refs/nixconvoy/deploying/branch";

#[derive(Clone, Debug, PartialEq)]
pub struct Provenance {
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

impl Provenance {
    /// The ref of the pending increment's announcement, when it has one.
    pub fn announce_key(&self) -> Option<&str> {
        self.announce.as_ref()?;
        let (branch, _) = self.pending.as_ref()?;
        Some(self.key.as_deref().unwrap_or(branch))
    }
}

/// An optional string field: None when it is neither absent, null nor a
/// string.
fn opt_str(o: &serde_json::Map<String, Value>, key: &str) -> Option<Option<String>> {
    match o.get(key) {
        None | Some(Value::Null) => Some(None),
        Some(Value::String(s)) => Some(Some(s.clone())),
        Some(_) => None,
    }
}

/// A provenance file's contents, or None when absent or malformed.
pub fn parse(text: &str) -> Option<Provenance> {
    let v: Value = serde_json::from_str(text).ok()?;
    let o = v.as_object()?;
    if o.get("version").and_then(Value::as_i64)? != 2 {
        return None;
    }
    let s = |v: Option<&Value>| v.and_then(Value::as_str).map(str::to_string);
    let by = o.get("by")?.as_object()?;
    let mut announce = None;
    let mut key = None;
    let pending = match o.get("pending") {
        None | Some(Value::Null) => None,
        Some(Value::Object(p)) => {
            let base = opt_str(p, "base")?;
            announce = opt_str(p, "announce")?;
            key = opt_str(p, "key")?;
            Some((s(p.get("branch"))?, base))
        }
        Some(_) => return None,
    };
    let held = o.get("branches")?.as_object()?;
    let mut branches: Vec<String> = held.keys().cloned().collect();
    branches.sort();
    let tips = held.iter().filter_map(|(b, t)| Some((b.clone(), t.as_str()?.to_string()))).collect();
    Some(Provenance {
        toplevel: s(o.get("toplevel"))?,
        union: s(o.get("union"))?,
        branches,
        tips,
        pending,
        announce,
        key,
        at: o.get("at")?.as_f64()?,
        machine: s(by.get("machine"))?,
        checkout: s(by.get("checkout"))?,
        pid: by.get("pid").and_then(Value::as_i64).unwrap_or(0),
    })
}

#[derive(Serialize)]
#[serde(rename_all = "kebab-case")]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Kind {
    /// No provenance for the system it runs: as before provenances.
    Untracked,
    /// This checkout deployed it, so its union carries it.
    ThisBranch,
    /// Another checkout's, all of it pushed (or its pending push landed
    /// since): contained in the refs this union reads.
    CatchUp,
    /// Another checkout's increment whose push has not landed - refused.
    InFlight,
    /// Work of a branch not shaped like branch_prefix here - refused.
    Unlisted,
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
#[derive(Deserialize, Clone, Debug, PartialEq)]
pub struct Context {
    pub names: Names,
    /// This checkout: (machine, checkout directory).
    pub me: (String, String),
    /// The branches its union may contain besides main: the work
    /// branches' shapes (branch_prefix).
    pub listed: BTreeSet<String>,
    /// This run's fetched origin tip of each branch.
    pub tips: BTreeMap<String, String>,
    /// The uncleared announcements by ref name (a provenance's `key`):
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
/// why, before `demote`.
pub fn classify(prov: Option<&Provenance>, running: &str, cx: &Context) -> (Kind, String) {
    let Some(prov) = prov.filter(|l| l.toplevel == running) else {
        return (Kind::Untracked, String::new());
    };
    let when = clock(prov.at, cx.tz_offset);
    if (&prov.machine, &prov.checkout) == (&cx.me.0, &cx.me.1) {
        return (Kind::ThisBranch, format!(text!(drift_this_branch), when = when));
    }
    let who = format!(text!(drift_who), machine = prov.machine, checkout = prov.checkout, pid = prov.pid, when = when);
    let unknown: Vec<&str> = prov
        .branches
        .iter()
        .filter(|b| **b != cx.names.main && !listed(cx.listed.iter().map(String::as_str), b))
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        return (Kind::Unlisted, format!(text!(drift_unlisted), branches = unknown.join(", "), who = who, BRANCHES_KEY = BRANCHES_KEY));
    }
    let Some((branch, base)) = &prov.pending else {
        return (Kind::CatchUp, format!(text!(drift_catch_up), who = who));
    };
    if cx.tips.get(branch) != base.as_ref() {
        return (Kind::CatchUp, format!(text!(drift_pushed_since), branch = branch, who = who));
    }
    let at: String = base.as_deref().unwrap_or(text!(drift_no_base)).chars().take(12).collect();
    let mut why = format!(text!(drift_in_flight), branch = branch, who = who, remote = cx.names.remote, at = at);
    if let Some(key) = prov.announce_key() {
        if !cx.in_flight.contains_key(key) {
            return (Kind::Abandoned, format!(text!(drift_abandoned), why = why));
        }
        if cx.dead.contains(key) {
            why = format!(text!(drift_looks_dead), why = why);
        }
    }
    (Kind::InFlight, why)
}

/// `classify`'s verdict for this deploy: a refusal is taken over when
/// `take_over` (a --evict names the target or reaches it), else left alone
/// unless `reached` (this checkout's own work changes something the
/// target's configuration reads).
pub fn demote(kind: Kind, why: String, take_over: bool, reached: bool) -> (Kind, String) {
    if !kind.refused() {
        return (kind, why);
    }
    if take_over {
        return (Kind::Overridden, format!(text!(drift_taken_over), why = why, EVICT_FLAG = EVICT_FLAG));
    }
    if !reached {
        return (Kind::LeftAlone, format!(text!(drift_not_reached), why = why));
    }
    (kind, why)
}

/// The order targets are reported in.
pub fn by_name(names: &[String]) -> Vec<String> {
    let mut v = names.to_vec();
    v.sort();
    v
}

#[derive(Deserialize, Clone, Debug, PartialEq)]
pub struct Params {
    pub cx: Context,
    /// The union commit this deploy builds.
    pub union: String,
    /// The targets of a lane of their own, each with that lane's union.
    pub unions: BTreeMap<String, String>,
    pub targets: Vec<String>,
    pub candidates: Vec<String>,
    /// --evict: this checkout's view alone - refused work is taken over on
    /// the targets it reaches, and on every candidate when `named`.
    pub evict: bool,
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
    /// Each probed target's provenance file.
    pub provenances: BTreeMap<String, String>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Outcome {
    /// Targets whose running system differs from the union.
    pub affected: Vec<String>,
    /// The targets to switch, in switch order.
    pub plan: Vec<String>,
    pub drift: BTreeMap<String, (Kind, String)>,
}

/// The die message of a deploy refusing `refused` (in report order), each
/// with the announcement ref of its dead deploy when the refusal is one.
pub fn refusal(refused: &[(String, Kind, Option<String>)], remote: &str) -> String {
    let targets: Vec<&str> = refused.iter().map(|(h, _, _)| h.as_str()).collect();
    let mut fixes = Vec::new();
    if refused.iter().any(|(_, k, d)| *k == Kind::InFlight && d.is_none()) {
        fixes.push(text!(drift_fix_in_flight).to_string());
    }
    let dead: BTreeSet<&str> = refused.iter().filter_map(|(_, _, d)| d.as_deref()).collect();
    for key in dead {
        let branch = key.split('@').next().unwrap_or(key);
        fixes.push(format!(text!(drift_fix_dead), branch = branch, remote = remote, PENDING_REFS = PENDING_REFS, key = key));
    }
    if refused.iter().any(|(_, k, _)| *k == Kind::Unlisted) {
        fixes.push(format!(text!(drift_fix_unlisted), BRANCHES_KEY = BRANCHES_KEY));
    }
    let fix = fixes.join(text!(drift_fix_joiner));
    let mut c = fix.chars();
    let fix: String = c.next().map(|f| f.to_uppercase().chain(c).collect()).unwrap_or_default();
    format!(
        text!(drift_refused),
        targets = targets.join(", "),
        run = if targets.len() == 1 { "runs" } else { "run" },
        them = if targets.len() == 1 { "it" } else { "them" },
        fix = fix,
        EVICT_FLAG = EVICT_FLAG,
        first = targets.first().copied().unwrap_or("")
    )
}

type Fates = BTreeMap<(String, String, String), String>;

/// Query::Fate's answer for `branch`'s increment at `tip` of the provenance
/// `prov` in `union`, asked once per tip and union (`fates` memoises it
/// across targets): "kept" or "abandoned".  The query names the deploying
/// checkout when it is on this machine: one still on the branch keeps the
/// increment.
fn fate(s: &mut Script, branch: &str, tip: &str, union: &str, prov: &Provenance, p: &Params, fates: &mut Fates) -> Run<String> {
    let key = (branch.to_string(), tip.to_string(), union.to_string());
    if let Some(f) = fates.get(&key) {
        return Ok(f.clone());
    }
    let checkout = (prov.machine == p.cx.me.0).then(|| prov.checkout.clone());
    let f = s.text(Query::Fate { branch: branch.into(), tip: tip.into(), union: union.into(), checkout })?.unwrap_or_default();
    fates.insert(key, f.clone());
    Ok(f)
}

/// The increments a switch of a target onto `union` reverts: each branch tip
/// its provenance recorded that is not origin's tip now and that the union
/// no longer holds, as "<branch>'s abandoned increment <sha>".
fn reverted(s: &mut Script, union: &str, prov: &Provenance, p: &Params, fates: &mut Fates) -> Run<Vec<String>> {
    let mut notes = Vec::new();
    for (b, tip) in &prov.tips {
        if p.cx.tips.get(b) == Some(tip) {
            continue;
        }
        let short: String = tip.chars().take(12).collect();
        if fate(s, b, tip, union, prov, p, fates)? == "abandoned" {
            notes.push(format!(text!(drift_abandoned_increment), branch = b, short = short));
        }
    }
    Ok(notes)
}

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    let mut affected = Vec::new();
    for h in by_name(&p.targets) {
        let Some(Some(running)) = p.running.get(&h) else { continue };
        let Some(built) = p.built.get(&h) else {
            return Err(Halt::Die(format!(text!(drift_no_toplevel), target = h)));
        };
        if built != running {
            affected.push(h);
        }
    }
    let mut drift = BTreeMap::new();
    let mut plan = Vec::new();
    let mut refused = Vec::new();
    let mut fates = Fates::new();
    let mut routine = BTreeSet::new();
    for h in affected.iter().filter(|h| p.candidates.contains(h)) {
        let union = p.unions.get(h).unwrap_or(&p.union);
        let prov = p.provenances.get(h).and_then(|text| parse(text));
        let running = p.running.get(h).cloned().flatten().unwrap_or_default();
        let reached = p.reached.contains(h);
        let take_over = p.evict && (p.named || reached);
        let (base, why) = classify(prov.as_ref(), &running, &p.cx);
        let (mut kind, mut why) = demote(base, why, take_over, reached);
        // The pending increment of a branch origin has no tip of: its
        // first push on its way (in flight), or the branch deleted since
        // with its subagent worktree - then whatever the union lacks of
        // it is abandoned, and the target catches up.
        let in_flight = base == Kind::InFlight;
        if let Some(prov) = prov.as_ref().filter(|_| in_flight) {
            if let Some((b, None)) = &prov.pending {
                if !p.cx.tips.contains_key(b) {
                    if let Some(tip) = prov.tips.get(b) {
                        if fate(s, b, tip, union, prov, p, &mut fates)? != "kept" {
                            let who = format!(text!(drift_who_untimed), machine = prov.machine, checkout = prov.checkout, pid = prov.pid);
                            (kind, why) = (Kind::CatchUp, format!(text!(drift_branch_gone), branch = b, who = who));
                        }
                    }
                }
            }
        }
        if kind.refused() {
            let dead = prov
                .as_ref()
                .and_then(Provenance::announce_key)
                .filter(|k| kind == Kind::InFlight && p.cx.dead.contains(*k))
                .map(str::to_string);
            refused.push((h.clone(), kind, dead));
        } else if kind != Kind::LeftAlone {
            plan.push(h.clone());
        }
        // A catch-up to pushed work may be the revert of an abandoned
        // increment; the other kinds revert or refuse in so many words.
        if let (Kind::ThisBranch | Kind::CatchUp, Some(prov)) = (kind, prov.as_ref()) {
            let notes = reverted(s, union, prov, p, &mut fates)?;
            if notes.is_empty() {
                routine.insert(h.clone());
            } else {
                why = format!(text!(drift_reverting), why = why, notes = notes.join(", "));
            }
        }
        drift.insert(h.clone(), (kind, why));
    }
    for h in by_name(&p.candidates) {
        // A switch over work the union contains, reverting nothing, is the
        // ordinary case: the switch itself is its report.
        if routine.contains(&h) {
            continue;
        }
        s.print(match drift.get(&h) {
            Some((k, why)) if k.refused() => format!(text!(drift_line_refused), target = h, why = why),
            None => format!(text!(drift_line_up_to_date), target = h),
            Some((Kind::Abandoned | Kind::Overridden, why)) => format!(text!(drift_line_reverting), target = h, why = why),
            Some((Kind::LeftAlone, why)) => format!(text!(drift_line_left_alone), target = h, why = why),
            Some((k, why)) if !why.is_empty() => format!(text!(drift_line_switch_kind), target = h, kind = k.name(), why = why),
            Some(_) => format!(text!(drift_line_switch), target = h),
        });
    }
    for h in &affected {
        if !drift.contains_key(h) {
            s.print(format!(text!(drift_line_excluded), target = h));
        }
    }
    if !refused.is_empty() {
        return Err(Halt::Die(refusal(&refused, &p.cx.names.remote)));
    }
    Ok(Outcome { affected, plan, drift })
}

#[cfg(test)]
mod tests;
