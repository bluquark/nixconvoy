//! nixconvoy's step 5 verdict on the hosts whose running system differs
//! from the union: whose work each runs (its lineage, as nixconvoy's
//! lineage_doc wrote it), and so which of them the deploy may switch.
//!
//! A host runs another deployer's increment that this union cannot contain
//! when that deployer's push has not landed (origin's tip of its branch is
//! still the one it built on) or when it holds work of a branch
//! deploy-branches here does not list.  Switching it would revert that work
//! until its owner redeploys, so it is never switched as is - unless the
//! lineage is older than PENDING_MAX_AGE by the host's clock (a dead
//! deployer's) or the deploy is a --reset that names the host or reaches
//! it.  When this checkout's own work does not reach the host
//! (Params::reached: no file its configuration reads changed), the host is
//! simply left running that work and the deploy goes on without it; when it
//! does, two checkouts are changing the same host and the deploy is refused
//! whole: switching the rest would leave them carrying this run's unpushed
//! claim beside the other's, with neither increment able to land cleanly.
//!
//! A host to be switched may run an increment that left the convoy since:
//! a branch tip its lineage recorded that origin's tip of the branch no
//! longer carries.  Query::Fate asks git what became of it - abandoned
//! (reset or dropped, landed nowhere) or not - and the host's verdict line
//! names each abandoned increment the switch reverts: abandon itself
//! touches no host, so this is where the revert shows.
use super::{listed, Halt, Query, Run, Script, BRANCHES_FILE};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

/// A push follows its switch within seconds (the nightly's main, once its
/// four hosts switched): a lineage still refused this long after it was
/// written (by the host's clock) is a dead deployer's, and its host is fair
/// game again.
pub const PENDING_MAX_AGE: f64 = 30.0 * 60.0;
pub const RESET_FLAG: &str = "--reset";

#[derive(Clone, Debug, PartialEq)]
pub struct Lineage {
    pub toplevel: String,
    pub union: String,
    /// Every branch the union holds work of (the deployer's own included),
    /// sorted.
    pub branches: Vec<String>,
    /// The tip of each of those the union held (the deployer's own: its
    /// staged snapshot when an increment was pending).
    pub tips: BTreeMap<String, String>,
    /// The deployer's own branch when what it unioned of it was not on
    /// origin yet, with the origin tip that push moves.
    pub pending: Option<(String, Option<String>)>,
    pub at: f64,
    pub host: String,
    pub checkout: String,
    pub pid: i64,
    /// Seconds since it was written, by the host's own clock (the file's
    /// mtime): deployers' clocks drift, a WSL one after every sleep.
    pub age: Option<f64>,
}

/// A lineage file's contents, or None when absent or malformed.
pub fn parse(text: &str, age: Option<f64>) -> Option<Lineage> {
    let v: Value = serde_json::from_str(text).ok()?;
    let o = v.as_object()?;
    if o.get("version").and_then(Value::as_i64) != Some(1) {
        return None;
    }
    let s = |v: Option<&Value>| v.and_then(Value::as_str).map(str::to_string);
    let by = o.get("by")?.as_object()?;
    let pending = match o.get("pending") {
        None | Some(Value::Null) => None,
        Some(Value::Object(p)) => {
            let base = match p.get("base") {
                None | Some(Value::Null) => None,
                Some(Value::String(b)) => Some(b.clone()),
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
        at: o.get("at")?.as_f64()?,
        host: s(by.get("host"))?,
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
    /// Another deployer's, all of it pushed (or its pending push landed
    /// since): contained in the refs this union reads.
    CatchUp,
    /// Another deployer's increment whose push has not landed - refused.
    InFlight,
    /// Work of a branch deploy-branches does not list here - refused.
    Unlisted,
    /// Would be refused, but this checkout's work does not reach the host:
    /// left running the other deployer's work, not switched.
    LeftAlone,
    /// Refused, but written more than PENDING_MAX_AGE ago.
    Abandoned,
    /// Refused, but taken over by a --reset that names or reaches the host.
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
    /// This deployer's (host, checkout).
    pub me: (String, String),
    /// The branches its union may contain: deploy-branches' patterns plus
    /// main.
    pub listed: BTreeSet<String>,
    /// This run's fetched origin tip of each branch.
    pub tips: BTreeMap<String, String>,
    pub now: f64,
    /// Seconds east of UTC, for the times shown.
    pub tz_offset: i64,
}

fn clock(at: f64, tz_offset: i64) -> String {
    let t = (at.floor() as i64).saturating_add(tz_offset).rem_euclid(86_400);
    format!("{:02}:{:02}", t / 3600, t % 3600 / 60)
}

/// Whose work a drifting host runs, relative to this deploy's union, and
/// why; `take_over` when a --reset names the host or reaches it, `reached`
/// when this checkout's own work changes something the host's configuration
/// reads.
pub fn classify(lin: Option<&Lineage>, running: &str, cx: &Context, take_over: bool, reached: bool) -> (Kind, String) {
    let Some(lin) = lin.filter(|l| l.toplevel == running) else {
        return (Kind::Untracked, String::new());
    };
    let when = clock(lin.at, cx.tz_offset);
    if (&lin.host, &lin.checkout) == (&cx.me.0, &cx.me.1) {
        return (Kind::ThisBranch, format!("over this checkout's deploy of {when}"));
    }
    let who = format!("{}:{} (pid {}) at {when}", lin.host, lin.checkout, lin.pid);
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
        (
            Kind::InFlight,
            format!("runs an increment of {branch} deployed by {who} whose push has not landed (origin/{branch} is still at {at})"),
        )
    } else {
        return (Kind::CatchUp, format!("runs pushed work deployed by {who}"));
    };
    let age = lin.age.unwrap_or(cx.now - lin.at);
    if age > PENDING_MAX_AGE {
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

/// The order hosts are switched and reported in: the router last, so
/// everything else still has connectivity while it copies.
pub fn switch_order(names: &[String], router: &str) -> Vec<String> {
    let mut v = names.to_vec();
    v.sort_by(|a, b| (a == router, a).cmp(&(b == router, b)));
    v
}

#[derive(Clone, Debug, PartialEq)]
pub struct Params {
    pub cx: Context,
    pub router: String,
    pub hosts: Vec<String>,
    pub candidates: Vec<String>,
    /// --reset: this checkout's view alone - refused work is taken over on
    /// the hosts it reaches, and on every candidate when `named`.
    pub reset: bool,
    /// The candidates were named on the command line.
    pub named: bool,
    /// The hosts this checkout's own work reaches: those whose
    /// configuration reads a file that work changed (nixconvoy's
    /// reached_hosts, by the eval memo's scan).
    pub reached: BTreeSet<String>,
    /// Each probed host's running system (None: unreachable).
    pub running: BTreeMap<String, Option<String>>,
    /// The union's toplevel of each reachable host.
    pub built: BTreeMap<String, String>,
    /// Each probed host's lineage file and its age by the host's clock.
    pub lineages: BTreeMap<String, (String, Option<f64>)>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Outcome {
    /// Hosts whose running system differs from the union.
    pub affected: Vec<String>,
    /// The hosts to switch, in switch order.
    pub plan: Vec<String>,
    pub drift: BTreeMap<String, (Kind, String)>,
}

/// The die message of a deploy refusing `refused` (in report order).
pub fn refusal(refused: &[(String, Kind)]) -> String {
    let hosts: Vec<&str> = refused.iter().map(|(h, _)| h.as_str()).collect();
    let mut fixes = Vec::new();
    if refused.iter().any(|(_, k)| *k == Kind::InFlight) {
        fixes.push("re-run once its push lands".to_string());
    }
    if refused.iter().any(|(_, k)| *k == Kind::Unlisted) {
        fixes.push(format!("list its branch in {BRANCHES_FILE} (`nixconvoy pull` once that line is on main)"));
    }
    let fix = fixes.join(" or ");
    let mut c = fix.chars();
    let fix: String = c.next().map(|f| f.to_uppercase().chain(c).collect()).unwrap_or_default();
    format!(
        "nothing switched or committed: {} {} another deployer's work this union lacks, and this checkout's work reaches {} too (see above).  {fix} (after {} min it counts as abandoned), or deploy with {RESET_FLAG} {} to revert it deliberately - along with every other branch's work there, which {RESET_FLAG} leaves out.",
        hosts.join(", "),
        if hosts.len() == 1 { "runs" } else { "run" },
        if hosts.len() == 1 { "it" } else { "them" },
        PENDING_MAX_AGE as u64 / 60,
        hosts.first().copied().unwrap_or("")
    )
}

/// The abandoned increments a switch of a host reverts: each branch tip
/// its lineage recorded that is not origin's tip now and that Query::Fate
/// reports abandoned, as "<branch>'s abandoned increment <sha>"; `fates`
/// memoises the answers across hosts.
fn reverted(s: &mut Script, lin: &Lineage, cx: &Context, fates: &mut BTreeMap<(String, String), bool>) -> Run<Vec<String>> {
    let mut notes = Vec::new();
    for (b, tip) in &lin.tips {
        if cx.tips.get(b) == Some(tip) {
            continue;
        }
        let key = (b.clone(), tip.clone());
        let abandoned = match fates.get(&key) {
            Some(a) => *a,
            None => {
                let fate = s.text(Query::Fate { branch: b.clone(), tip: tip.clone() })?;
                let a = fate.as_deref() == Some("abandoned");
                fates.insert(key, a);
                a
            }
        };
        if abandoned {
            let short: String = tip.chars().take(12).collect();
            notes.push(format!("{b}'s abandoned increment {short}"));
        }
    }
    Ok(notes)
}

pub fn program(s: &mut Script, p: &Params) -> Run<Outcome> {
    let mut affected = Vec::new();
    for h in switch_order(&p.hosts, &p.router) {
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
        let (kind, mut why) = classify(lin.as_ref(), &running, &p.cx, p.reset && (p.named || reached), reached);
        if kind.refused() {
            refused.push((h.clone(), kind));
        } else if kind != Kind::LeftAlone {
            plan.push(h.clone());
        }
        // A catch-up to pushed work may be the revert of an abandoned
        // increment; the other kinds revert or refuse in so many words.
        if let (Kind::ThisBranch | Kind::CatchUp, Some(lin)) = (kind, lin.as_ref()) {
            let notes = reverted(s, lin, &p.cx, &mut fates)?;
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
            s.print(format!("  {h}: differs but excluded by the host filter"));
        }
    }
    if !refused.is_empty() {
        return Err(Halt::Die(refusal(&refused)));
    }
    Ok(Outcome { affected, plan, drift })
}

#[cfg(test)]
mod tests;
