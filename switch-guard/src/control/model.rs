//! nixconvoy's switch step and the target's helper composed: the real
//! control `Machine`s drive the real helper `Machine`s through ssh calls
//! that may be delayed or lost (before or after reaching the target), against a
//! one-second clock, the dead-man timer due exactly at its arming plus the
//! deadline, probe timeouts, a crash of the deploying machine and a crash
//! (reboot) of the target.  A probe of the target answers at once or at its
//! timeout.  Two deploys in turn, the second taking the guard
//! once the first finished or died, each its own toplevel.
use super::{Guard, Machine as Control, Next as CNext, Params, Query as CQuery, Reply as CReply};
use crate::record::{Phase, Record};
use crate::switch::core::{Action, Effect as HEffect, Machine as Helper, Next as HNext, Query as HQuery, Reply as HReply, Verb};
use crate::switch::fake::{self, Outcome, World as _, OUTCOMES};
use stateright::{Model, Property};

const DEADLINE: u64 = 10;
const HORIZON: u64 = 40;
/// Bounds the check fails at rather than miss part of the space (it holds
/// about 11.3M unique states, 32.2M visited, depth 132).
const MAX_STATES: usize = 40_000_000;
const MAX_DEPTH: usize = 400;
/// Seconds a confirm may spend between leaving nixconvoy and taking the
/// helper's lock in a timely run.
const ROUND_TRIP: u64 = 1;
const SYS: [&str; 3] = ["old", "a", "b"];
const TOPLEVEL: [usize; 2] = [1, 2];

type Sys = usize;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Owner {
    /// Deploy `d`'s request; it still waits for the exit code.
    Deploy(usize),
    /// Its deploy stopped waiting (died, or the ssh session was lost).
    Orphan,
    Timer,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct Run {
    owner: Owner,
    m: Helper<Sys>,
    q: HQuery<Sys>,
    locked: bool,
    /// A confirm sent once verification passed.
    confirm: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Wait {
    /// A local query, or a probe of the target, asked at this second.
    Local(CQuery, u64),
    /// A helper call not yet on the target.
    Sending(CQuery, u64),
    /// On the target, as a run.
    Remote,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
// Model state, cloned on every step: the padding is cheaper than an allocation.
#[allow(clippy::large_enum_variant)]
enum Status {
    Idle,
    Active { m: Control, wait: Wait },
    Done { success: bool },
    Dead,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct World {
    clock: u64,
    target: fake::Target<Sys>,
    timer_due: Option<u64>,
    runs: Vec<Run>,
    deploys: [Status; 2],
    guard: Option<usize>,
    faults: u8,
    // Ghosts.
    staged: Option<usize>,
    last_arm: Option<usize>,
    success: [bool; 2],
    false_success: bool,
    /// nixconvoy reported rolling back to a system the target does not run.
    wrong_rollback: bool,
    timely: bool,
    confirm_sent: Option<u64>,
    lost_race: bool,
}

impl World {
    fn quiescent(&self) -> bool {
        self.timer_due.is_none() && self.runs.is_empty() && self.deploys.iter().all(|d| !matches!(d, Status::Active { .. }))
    }
}

impl fake::World<Sys> for World {
    fn target(&mut self) -> &mut fake::Target<Sys> {
        &mut self.target
    }

    fn timer_pending(&self) -> bool {
        self.timer_due.is_some()
    }

    fn rollback_running(&self) -> bool {
        self.runs.iter().any(|r| r.owner == Owner::Timer)
    }

    fn start_timer(&mut self) {
        self.timer_due = Some(self.clock + DEADLINE);
    }

    fn fault(&mut self) -> bool {
        if self.faults == 0 {
            return false;
        }
        self.faults -= 1;
        self.timely = false;
        true
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Act {
    Start(usize),
    /// Answer deploy `d`'s local query or probe; `.1` picks a failure.
    Answer(usize, bool),
    Deliver(usize),
    /// The ssh call is lost: before reaching the target if still sending,
    /// else its reply.
    Lose(usize),
    CrashDeploy(usize),
    CrashTarget,
    Step(usize, Outcome),
    Fire,
    Tick,
}

fn verb_of(args: &[String]) -> Verb<Sys> {
    let sys = |s: &str| SYS.iter().position(|x| *x == s).expect("a known system");
    match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["activate", t] => Verb::Activate(sys(t)),
        ["confirm", t] => Verb::Confirm { new: sys(t), reboot_pending: false },
        ["confirm", t, "reboot-pending"] => Verb::Confirm { new: sys(t), reboot_pending: true },
        ["rollback"] => Verb::Rollback,
        a => panic!("unexpected helper call {a:?}"),
    }
}

fn spawn(w: &mut World, owner: Owner, verb: Verb<Sys>, confirm: bool) {
    let mut m = Helper::new(verb, DEADLINE);
    let step = m.decide(HReply::Start);
    let HNext::Ask(q) = step.next else { unreachable!() };
    w.runs.push(Run { owner, m, q, locked: false, confirm });
}

/// Feeds deploy `d` its reply and records what it asks next.
fn advance(w: &mut World, d: usize, reply: CReply) {
    let Status::Active { m, .. } = &mut w.deploys[d] else { return };
    let step = m.decide(reply);
    match step.next {
        CNext::Done => finish(w, d, true),
        CNext::Die(msg) => {
            if let Some(t) = msg.split("rolled back to ").nth(1).and_then(|r| r.split(".  ").next()) {
                if t != SYS[w.target.running] {
                    w.wrong_rollback = true;
                }
            }
            finish(w, d, false)
        }
        CNext::Ask(q) => {
            if let CQuery::Helper { args } = &q {
                if args.len() == 2 && args[0] == "confirm" {
                    w.confirm_sent = Some(w.clock);
                }
            }
            let wait = match &q {
                CQuery::Arm | CQuery::Helper { .. } => Wait::Sending(q, w.clock),
                _ => Wait::Local(q, w.clock),
            };
            if let Status::Active { wait: wt, .. } = &mut w.deploys[d] {
                *wt = wait;
            }
        }
    }
}

fn finish(w: &mut World, d: usize, success: bool) {
    w.deploys[d] = Status::Done { success };
    if w.guard == Some(d) {
        w.guard = None;
    }
    for r in &mut w.runs {
        if r.owner == Owner::Deploy(d) {
            r.owner = Owner::Orphan;
        }
    }
    if success {
        w.success[d] = true;
        if w.target.running != TOPLEVEL[d] || w.target.profile() != TOPLEVEL[d] {
            w.false_success = true;
        }
    }
}

fn end_run(w: &mut World, i: usize, code: i32) {
    let run = w.runs.remove(i);
    if run.locked {
        w.target.lock = false;
    }
    if let Verb::Confirm { reboot_pending: true, .. } = run.m.verb() {
        if code == 0 {
            w.staged = Some(w.target.current);
        }
    }
    if let Owner::Deploy(d) = run.owner {
        // What the helper prints: an arm's record, the system a rollback goes to.
        let reply = match (run.m.verb(), &w.target.record) {
            (Verb::Arm | Verb::Rollback, Some(Record { previous, phase: Phase::Armed | Phase::RolledBack })) if code == 0 => {
                CReply::Said { code, to: Some(SYS[*previous].into()) }
            }
            _ => CReply::Code(code),
        };
        advance(w, d, reply);
    }
}

/// The fake target's answer to run `i`'s query under `o`, or None when `o`
/// is not possible.
fn target_answer(w: &mut World, i: usize, o: Outcome) -> Option<HReply<Sys>> {
    let activation = matches!(w.runs[i].m.verb(), Verb::Activate(_));
    let q = w.runs[i].q.clone();
    let lock = q == HQuery::Lock;
    let r = fake::answer(w, q, o, activation)?;
    if lock {
        w.runs[i].locked = true;
        if w.runs[i].owner == Owner::Timer && w.confirm_sent.is_some() {
            w.lost_race = true;
        }
        if w.runs[i].confirm {
            w.confirm_sent = None;
        }
    }
    Some(r)
}

fn target_step(w: &mut World, i: usize, o: Outcome) -> bool {
    let mut probe = w.clone();
    let Some(reply) = target_answer(&mut probe, i, o) else { return false };
    *w = probe;
    let step = w.runs[i].m.decide(reply);
    for e in step.effects {
        match e {
            HEffect::StopTimer => w.timer_due = None,
            HEffect::Persist(r) => {
                if r.phase == Phase::Armed {
                    if let Owner::Deploy(d) = w.runs[i].owner {
                        w.last_arm = Some(d);
                    }
                }
                w.target.record = Some(r);
            }
            _ => {}
        }
    }
    match step.next {
        HNext::Ask(q) => w.runs[i].q = q,
        HNext::Exit(c) => end_run(w, i, c),
    }
    true
}

/// A probe or sleep must be answered at its deadline, the timer fire when
/// due, a request reach the target within its round trip: the clock waits for
/// them.  Target steps other than an activation or a wait for the lock take no
/// time.
fn due(w: &World) -> bool {
    if w.timer_due.is_some_and(|t| w.clock >= t) {
        return true;
    }
    if w.runs.iter().any(|r| !matches!(r.q, HQuery::Switch(_, Action::Test) | HQuery::Lock)) {
        return true;
    }
    w.deploys.iter().any(|s| match s {
        Status::Active { wait: Wait::Local(q, at), .. } => match q {
            CQuery::Running { timeout_ms: Some(t) } | CQuery::Check { timeout_ms: t } | CQuery::Internet { timeout_ms: t } => {
                w.clock >= at + t / 1000
            }
            CQuery::Sleep { ms } => w.clock >= at + ms / 1000,
            _ => true,
        },
        Status::Active { wait: Wait::Sending(_, at), .. } => w.clock >= at + ROUND_TRIP,
        _ => false,
    })
}

struct Composed {
    second: bool,
    faults: u8,
}

impl Model for Composed {
    type State = World;
    type Action = Act;

    fn init_states(&self) -> Vec<World> {
        vec![World {
            clock: 0,
            target: fake::Target::new(0),
            timer_due: None,
            runs: vec![],
            deploys: [Status::Idle, Status::Idle],
            guard: None,
            faults: self.faults,
            staged: None,
            last_arm: None,
            success: [false, false],
            false_success: false,
            wrong_rollback: false,
            timely: true,
            confirm_sent: None,
            lost_race: false,
        }]
    }

    fn actions(&self, w: &World, acts: &mut Vec<Act>) {
        // The second deploy probes and takes the guard once the first died
        // or failed, as a re-run after it would; after a success it would
        // only repeat the first.
        if w.deploys[0] == Status::Idle {
            acts.push(Act::Start(0));
        } else if self.second && w.deploys[1] == Status::Idle && matches!(w.deploys[0], Status::Dead | Status::Done { success: false }) {
            acts.push(Act::Start(1));
        }
        for d in 0..2 {
            if let Status::Active { wait, .. } = &w.deploys[d] {
                acts.push(Act::CrashDeploy(d));
                match wait {
                    Wait::Local(..) => {
                        acts.push(Act::Answer(d, false));
                        acts.push(Act::Answer(d, true));
                    }
                    Wait::Sending(..) => {
                        acts.push(Act::Deliver(d));
                        acts.push(Act::Lose(d));
                    }
                    Wait::Remote => acts.push(Act::Lose(d)),
                }
            }
        }
        for i in 0..w.runs.len() {
            for o in OUTCOMES {
                acts.push(Act::Step(i, o));
            }
        }
        if w.timer_due.is_some_and(|t| w.clock >= t) {
            acts.push(Act::Fire);
        }
        if w.faults > 0 {
            acts.push(Act::CrashTarget);
        }
        let busy = w.timer_due.is_some() || !w.runs.is_empty() || w.deploys.iter().any(|d| matches!(d, Status::Active { .. }));
        if busy && w.clock < HORIZON && !due(w) {
            acts.push(Act::Tick);
        }
    }

    fn next_state(&self, w: &World, act: Act) -> Option<World> {
        let mut w = w.clone();
        match act {
            Act::Start(d) => {
                // Only the first deploy's leftovers on the target (a pending
                // timer) reach past its end: time restarts with the second,
                // which merges every state its start could be reached from.
                if d == 1 {
                    let offset = w.clock;
                    w.clock = 0;
                    w.timer_due = w.timer_due.map(|t| t - offset);
                    w.confirm_sent = w.confirm_sent.map(|t| t.saturating_sub(offset));
                }
                let p = Params {
                    target: "choko".into(),
                    toplevel: SYS[TOPLEVEL[d]].into(),
                    expected: SYS[w.target.running].into(),
                    place: "local".into(),
                    quiet: false,
                    routing_check: true,
                    fail_verify: false,
                };
                w.deploys[d] = Status::Active { m: Control::new(p), wait: Wait::Local(CQuery::Clock, 0) };
                advance(&mut w, d, CReply::Start);
            }
            Act::Answer(d, fail) => {
                let Status::Active { m, wait: Wait::Local(q, at) } = &w.deploys[d] else { return None };
                let expected = SYS.iter().position(|s| *s == m.params().expected).unwrap();
                let (q, at, clock) = (q.clone(), *at, w.clock);
                let timed_out = |t: u64| clock >= at + t / 1000;
                // A probe answers at once or takes its whole timeout: the
                // durations in between start the next probe in between.
                if let CQuery::Running { timeout_ms: Some(t) } | CQuery::Check { timeout_ms: t } | CQuery::Internet { timeout_ms: t } = q {
                    if clock != at && !timed_out(t) {
                        return None;
                    }
                }
                let reply = match (q, fail) {
                    (CQuery::TakeGuard, false) => {
                        if w.guard.is_some() {
                            return None;
                        }
                        if w.target.running != expected {
                            CReply::Guard(Guard::Moved(SYS[w.target.running].into()))
                        } else {
                            w.guard = Some(d);
                            CReply::Guard(Guard::Locked)
                        }
                    }
                    (CQuery::ProbeHelper, false) => CReply::Code(0),
                    (CQuery::ProbeHelper, true) if w.fault() => CReply::Code(255),
                    (CQuery::Deadline, false) => CReply::Text(DEADLINE.to_string()),
                    (CQuery::Clock, false) => CReply::Now(w.clock * 1000),
                    (CQuery::Running { .. }, false) => CReply::System(Some(SYS[w.target.running].into())),
                    (CQuery::Running { timeout_ms }, true) if timeout_ms.is_none_or(timed_out) => CReply::System(None),
                    // The target's own checks pass or not, at any moment.
                    (CQuery::Check { .. }, false) => CReply::Checked { code: 0, last: None },
                    (CQuery::Check { .. }, true) => CReply::Checked { code: 1, last: Some("x".into()) },
                    (CQuery::Internet { .. }, false) => CReply::Flag(true),
                    (CQuery::Internet { timeout_ms }, true) if timed_out(timeout_ms) => CReply::Flag(false),
                    (CQuery::Sleep { ms }, false) if timed_out(ms) => CReply::Done,
                    _ => return None,
                };
                advance(&mut w, d, reply);
            }
            Act::Deliver(d) => {
                let Status::Active { wait, .. } = &mut w.deploys[d] else { return None };
                let Wait::Sending(q, _) = std::mem::replace(wait, Wait::Remote) else { return None };
                match q {
                    CQuery::Arm => spawn(&mut w, Owner::Deploy(d), Verb::Arm, false),
                    CQuery::Helper { args } => {
                        let confirm = args.len() == 2 && args[0] == "confirm";
                        spawn(&mut w, Owner::Deploy(d), verb_of(&args), confirm);
                    }
                    _ => unreachable!(),
                }
            }
            Act::Lose(d) => {
                if !w.fault() {
                    return None;
                }
                for r in &mut w.runs {
                    if r.owner == Owner::Deploy(d) {
                        r.owner = Owner::Orphan;
                    }
                }
                w.confirm_sent = None;
                advance(&mut w, d, CReply::Code(255));
            }
            Act::CrashDeploy(d) => {
                w.deploys[d] = Status::Dead;
                w.timely = false;
                if w.guard == Some(d) {
                    w.guard = None;
                }
                for r in &mut w.runs {
                    if r.owner == Owner::Deploy(d) {
                        r.owner = Owner::Orphan;
                    }
                }
            }
            Act::CrashTarget => {
                if !w.fault() {
                    return None;
                }
                // A reboot boots the profile; /run is gone, with the record,
                // the lock, the timer and every run, and the ssh sessions.
                w.target.running = w.target.profile();
                w.target.record = None;
                w.target.lock = false;
                w.timer_due = None;
                w.guard = None;
                let waiting: Vec<usize> =
                    w.runs.iter().filter_map(|r| if let Owner::Deploy(d) = r.owner { Some(d) } else { None }).collect();
                w.runs.clear();
                w.confirm_sent = None;
                for d in waiting {
                    advance(&mut w, d, CReply::Code(255));
                }
            }
            Act::Step(i, o) => {
                if i >= w.runs.len() || !target_step(&mut w, i, o) {
                    return None;
                }
            }
            Act::Fire => {
                w.timer_due = None;
                spawn(&mut w, Owner::Timer, Verb::Rollback, false);
            }
            Act::Tick => {
                w.clock += 1;
                if w.confirm_sent.is_some_and(|t| w.clock > t + ROUND_TRIP) {
                    w.timely = false;
                }
            }
        }
        Some(w)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        vec![
            Property::always("quiescent: runs the confirmed toplevel or previous", |_, w: &World| {
                let t = &w.target;
                !w.quiescent()
                    || match &t.record {
                        None => t.running == t.profile(),
                        Some(Record { previous, phase: Phase::Confirmed(c) }) => t.running == *c || t.running == *previous,
                        Some(Record { previous, .. }) => t.running == *previous,
                    }
            }),
            Property::always("quiescent: profile matches running unless reboot-pending", |_, w: &World| {
                !w.quiescent() || w.target.profile() == w.target.running || w.staged == Some(w.target.current)
            }),
            Property::always("nixconvoy reports success only when the target runs and boots its toplevel", |_, w: &World| !w.false_success),
            Property::always("a reported rollback names the system the target runs", |_, w: &World| !w.wrong_rollback),
            Property::always("a reported success is never rolled back", |_, w: &World| {
                !w.quiescent()
                    || (0..2).all(|d| {
                        !w.success[d] || w.last_arm != Some(d) || (w.target.running == TOPLEVEL[d] && w.target.profile() == TOPLEVEL[d])
                    })
            }),
            Property::always("a timely confirm after verification never races the timer", |_, w: &World| !(w.timely && w.lost_race)),
            Property::sometimes("a deploy succeeds after a dead one", |_, w: &World| w.success[1] && w.deploys[0] == Status::Dead),
            Property::sometimes("a failed verification rolls back", |_, w: &World| {
                w.quiescent() && matches!(w.target.record, Some(Record { phase: Phase::RolledBack, .. })) && w.faults == 1
            }),
            Property::sometimes("the target crashed mid-switch", |_, w: &World| {
                w.quiescent() && w.faults == 0 && w.target.record.is_none() && w.last_arm.is_some()
            }),
        ]
    }
}

fn show(s: &World) -> String {
    let deploys: Vec<String> = s
        .deploys
        .iter()
        .map(|d| match d {
            Status::Active { wait, .. } => format!("waiting {wait:?}"),
            other => format!("{other:?}"),
        })
        .collect();
    format!(
        "t={} running {} profile {} record {:?} timer {:?} runs {:?} deploys {:?}",
        s.clock,
        SYS[s.target.running],
        SYS[s.target.profile()],
        s.target.record,
        s.timer_due,
        s.runs.iter().map(|r| (r.owner.clone(), r.m.verb().clone(), r.q.clone())).collect::<Vec<_>>(),
        deploys
    )
}

#[test]
fn composed_model_checks() {
    fake::check(Composed { second: true, faults: 1 }, "composed model", MAX_STATES, MAX_DEPTH, show);
}
