//! nixconvoy's switch step and the host's helper composed: the real
//! deployer `Machine`s drive the real helper `Machine`s through ssh calls
//! that may be delayed or lost (before or after reaching the host), against a
//! one-second clock, the dead-man timer due exactly at its arming plus the
//! deadline, probe timeouts, a crash of the deploying machine and a crash
//! (reboot) of the host.  A probe of the host answers at once or at its
//! timeout.  Two deploys in turn, the second taking the guard
//! once the first finished or died, each its own toplevel.
use super::{Guard, Machine as Deployer, Next as DNext, Params, Query as DQuery, Reply as DReply};
use crate::record::{Phase, Record};
use crate::switch::core::{Action, Effect as HEffect, Machine as Helper, Next as HNext, Query as HQuery, Reply as HReply, Verb};
use stateright::{Checker, Model, Property};

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
    /// A local query, or a probe of the host, asked at this second.
    Local(DQuery, u64),
    /// A helper call not yet on the host.
    Sending(DQuery, u64),
    /// On the host, as a run.
    Remote,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
// Model state, cloned on every step: the padding is cheaper than an allocation.
#[allow(clippy::large_enum_variant)]
enum Status {
    Idle,
    Active { m: Deployer, wait: Wait },
    Done { success: bool },
    Dead,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct World {
    clock: u64,
    running: Sys,
    gens: Vec<Option<Sys>>,
    current: usize,
    record: Option<Record<Sys>>,
    lock: bool,
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
    /// nixconvoy reported rolling back to a system the host does not run.
    wrong_target: bool,
    timely: bool,
    confirm_sent: Option<u64>,
    lost_race: bool,
}

impl World {
    fn profile(&self) -> Sys {
        self.gens[self.current].expect("current generation")
    }

    fn quiescent(&self) -> bool {
        self.timer_due.is_none() && self.runs.is_empty() && self.deploys.iter().all(|d| !matches!(d, Status::Active { .. }))
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
    /// The ssh call is lost: before reaching the host if still sending,
    /// else its reply.
    Lose(usize),
    CrashDeploy(usize),
    CrashHost,
    Step(usize, u8),
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
fn advance(w: &mut World, d: usize, reply: DReply) {
    let Status::Active { m, .. } = &mut w.deploys[d] else { return };
    let step = m.decide(reply);
    match step.next {
        DNext::Done => finish(w, d, true),
        DNext::Die(msg) => {
            if let Some(t) = msg.split("rolled back to ").nth(1).and_then(|r| r.split(".  ").next()) {
                if t != SYS[w.running] {
                    w.wrong_target = true;
                }
            }
            finish(w, d, false)
        }
        DNext::Ask(q) => {
            if let DQuery::Helper(args) = &q {
                if args.len() == 2 && args[0] == "confirm" {
                    w.confirm_sent = Some(w.clock);
                }
            }
            let wait = match &q {
                DQuery::Arm | DQuery::Helper(_) => Wait::Sending(q, w.clock),
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
        if w.running != TOPLEVEL[d] || w.profile() != TOPLEVEL[d] {
            w.false_success = true;
        }
    }
}

fn end_run(w: &mut World, i: usize, code: i32) {
    let run = w.runs.remove(i);
    if run.locked {
        w.lock = false;
    }
    if let Verb::Confirm { reboot_pending: true, .. } = run.m.verb() {
        if code == 0 {
            w.staged = Some(w.current);
        }
    }
    if let Owner::Deploy(d) = run.owner {
        // What the helper prints: an arm's record, a rollback's target.
        let reply = match (run.m.verb(), &w.record) {
            (Verb::Arm | Verb::Rollback, Some(Record { previous, phase: Phase::Armed | Phase::RolledBack })) if code == 0 => {
                DReply::Said { code, target: Some(SYS[*previous].into()) }
            }
            _ => DReply::Code(code),
        };
        advance(w, d, reply);
    }
}

/// The host's answer to run `i`'s query under outcome `o` (0: normal, 1-3:
/// the failures that query admits), or None when `o` is not possible.
fn host_answer(w: &mut World, i: usize, o: u8) -> Option<HReply<Sys>> {
    let activation = matches!(w.runs[i].m.verb(), Verb::Activate(_));
    let q = w.runs[i].q.clone();
    Some(match (q, o) {
        (HQuery::Lock, 0) => {
            if w.lock {
                return None;
            }
            w.lock = true;
            w.runs[i].locked = true;
            if w.runs[i].owner == Owner::Timer && w.confirm_sent.is_some() {
                w.lost_race = true;
            }
            if w.runs[i].confirm {
                w.confirm_sent = None;
            }
            HReply::Locked(w.record.clone())
        }
        (HQuery::RollbackActive, 0) => HReply::Flag(w.runs.iter().any(|r| r.owner == Owner::Timer)),
        (HQuery::TimerLive, 0) => HReply::Flag(w.timer_due.is_some() || w.runs.iter().any(|r| r.owner == Owner::Timer)),
        (HQuery::ProfileSystem, 0) => HReply::Running(Some(w.profile())),
        (HQuery::Running, 0) => HReply::Running(Some(w.running)),
        (HQuery::StartTimer, 0) => {
            if w.timer_due.is_some() || w.runs.iter().any(|r| r.owner == Owner::Timer) {
                HReply::Flag(false)
            } else {
                w.timer_due = Some(w.clock + DEADLINE);
                HReply::Flag(true)
            }
        }
        (HQuery::StartTimer, 1) if w.fault() => HReply::Flag(false),
        (HQuery::Generation, 0) => HReply::Generation(Some(w.current as u64)),
        (HQuery::SetProfile(s), 0) => {
            w.gens.push(Some(s));
            w.current = w.gens.len() - 1;
            HReply::Flag(true)
        }
        (HQuery::Switch(s, Action::Test), o) => {
            if !activation && o != 0 && !w.fault() {
                return None;
            }
            match o {
                0 => {
                    w.running = s;
                    HReply::Code(0)
                }
                1 => {
                    w.running = s;
                    HReply::Code(4)
                }
                2 => HReply::Code(1),
                3 if activation => HReply::Code(100),
                _ => return None,
            }
        }
        (HQuery::Switch(_, Action::Boot), 0) => HReply::Code(0),
        (HQuery::Switch(_, Action::Boot), 1) => HReply::Code(1),
        (HQuery::WaitSwitchLock | HQuery::Sleep(_), 0) => HReply::Done,
        (HQuery::SwitchGeneration(g), 0) => {
            let g = g as usize;
            let ok = w.gens.get(g).copied().flatten().is_some();
            if ok {
                w.current = g;
            }
            HReply::Flag(ok)
        }
        (HQuery::DeleteGeneration(g), 0) => {
            let g = g as usize;
            let ok = g != w.current && g < w.gens.len();
            if ok {
                w.gens[g] = None;
            }
            HReply::Flag(ok)
        }
        _ => return None,
    })
}

fn host_step(w: &mut World, i: usize, o: u8) -> bool {
    let mut probe = w.clone();
    let Some(reply) = host_answer(&mut probe, i, o) else { return false };
    *w = probe;
    let step = w.runs[i].m.decide(reply);
    for e in step.effects {
        match e {
            HEffect::StopTimer => w.timer_due = None,
            HEffect::Persist(r) => {
                if matches!(r, Some(Record { phase: Phase::Armed, .. })) {
                    if let Owner::Deploy(d) = w.runs[i].owner {
                        w.last_arm = Some(d);
                    }
                }
                w.record = r;
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
/// due, a request reach the host within its round trip: the clock waits for
/// them.  Host steps other than an activation or a wait for the lock take no
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
            DQuery::Running { timeout_ms: Some(t) } | DQuery::Check { timeout_ms: t } | DQuery::Internet { timeout_ms: t } => {
                w.clock >= at + t / 1000
            }
            DQuery::Sleep { ms } => w.clock >= at + ms / 1000,
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
            running: 0,
            gens: vec![Some(0)],
            current: 0,
            record: None,
            lock: false,
            timer_due: None,
            runs: vec![],
            deploys: [Status::Idle, Status::Idle],
            guard: None,
            faults: self.faults,
            staged: None,
            last_arm: None,
            success: [false, false],
            false_success: false,
            wrong_target: false,
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
            for o in 0..4 {
                acts.push(Act::Step(i, o));
            }
        }
        if w.timer_due.is_some_and(|t| w.clock >= t) {
            acts.push(Act::Fire);
        }
        if w.faults > 0 {
            acts.push(Act::CrashHost);
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
                // Only the first deploy's leftovers on the host (a pending
                // timer) reach past its end: time restarts with the second,
                // which merges every state its start could be reached from.
                if d == 1 {
                    let offset = w.clock;
                    w.clock = 0;
                    w.timer_due = w.timer_due.map(|t| t - offset);
                    w.confirm_sent = w.confirm_sent.map(|t| t.saturating_sub(offset));
                }
                let p = Params {
                    host: "choko".into(),
                    toplevel: SYS[TOPLEVEL[d]].into(),
                    expected: SYS[w.running].into(),
                    place: "local".into(),
                    quiet: false,
                    router: true,
                    fail_verify: false,
                };
                w.deploys[d] = Status::Active { m: Deployer::new(p), wait: Wait::Local(DQuery::Now, 0) };
                advance(&mut w, d, DReply::Start);
            }
            Act::Answer(d, fail) => {
                let Status::Active { m, wait: Wait::Local(q, at) } = &w.deploys[d] else { return None };
                let expected = SYS.iter().position(|s| *s == m.params().expected).unwrap();
                let (q, at, clock) = (q.clone(), *at, w.clock);
                let timed_out = |t: u64| clock >= at + t / 1000;
                // A probe answers at once or takes its whole timeout: the
                // durations in between start the next probe in between.
                if let DQuery::Running { timeout_ms: Some(t) } | DQuery::Check { timeout_ms: t } | DQuery::Internet { timeout_ms: t } = q {
                    if clock != at && !timed_out(t) {
                        return None;
                    }
                }
                let reply = match (q, fail) {
                    (DQuery::Guard, false) => {
                        if w.guard.is_some() {
                            return None;
                        }
                        if w.running != expected {
                            DReply::Guard(Guard::Moved(SYS[w.running].into()))
                        } else {
                            w.guard = Some(d);
                            DReply::Guard(Guard::Locked)
                        }
                    }
                    (DQuery::ProbeHelper, false) => DReply::Code(0),
                    (DQuery::ProbeHelper, true) if w.fault() => DReply::Code(255),
                    (DQuery::Deadline, false) => DReply::Text(DEADLINE.to_string()),
                    (DQuery::Now, false) => DReply::Now(w.clock * 1000),
                    (DQuery::Running { .. }, false) => DReply::Running(Some(SYS[w.running].into())),
                    (DQuery::Running { timeout_ms }, true) if timeout_ms.is_none_or(timed_out) => DReply::Running(None),
                    // The host's own checks pass or not, at any moment.
                    (DQuery::Check { .. }, false) => DReply::Check { code: 0, last: None },
                    (DQuery::Check { .. }, true) => DReply::Check { code: 1, last: Some("x".into()) },
                    (DQuery::Internet { .. }, false) => DReply::Flag(true),
                    (DQuery::Internet { timeout_ms }, true) if timed_out(timeout_ms) => DReply::Flag(false),
                    (DQuery::Sleep { ms }, false) if timed_out(ms) => DReply::Done,
                    _ => return None,
                };
                advance(&mut w, d, reply);
            }
            Act::Deliver(d) => {
                let Status::Active { wait, .. } = &mut w.deploys[d] else { return None };
                let Wait::Sending(q, _) = std::mem::replace(wait, Wait::Remote) else { return None };
                match q {
                    DQuery::Arm => spawn(&mut w, Owner::Deploy(d), Verb::Arm, false),
                    DQuery::Helper(args) => {
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
                advance(&mut w, d, DReply::Code(255));
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
            Act::CrashHost => {
                if !w.fault() {
                    return None;
                }
                // A reboot boots the profile; /run is gone, with the record,
                // the lock, the timer and every run, and the ssh sessions.
                w.running = w.profile();
                w.record = None;
                w.lock = false;
                w.timer_due = None;
                w.guard = None;
                let waiting: Vec<usize> =
                    w.runs.iter().filter_map(|r| if let Owner::Deploy(d) = r.owner { Some(d) } else { None }).collect();
                w.runs.clear();
                w.confirm_sent = None;
                for d in waiting {
                    advance(&mut w, d, DReply::Code(255));
                }
            }
            Act::Step(i, o) => {
                if i >= w.runs.len() || !host_step(&mut w, i, o) {
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
                !w.quiescent()
                    || match &w.record {
                        None => w.running == w.profile(),
                        Some(Record { previous, phase: Phase::Confirmed(t) }) => w.running == *t || w.running == *previous,
                        Some(Record { previous, .. }) => w.running == *previous,
                    }
            }),
            Property::always("quiescent: profile matches running unless reboot-pending", |_, w: &World| {
                !w.quiescent() || w.profile() == w.running || w.staged == Some(w.current)
            }),
            Property::always("nixconvoy reports success only when the host runs and boots its toplevel", |_, w: &World| !w.false_success),
            Property::always("a reported rollback names the system the host runs", |_, w: &World| !w.wrong_target),
            Property::always("a reported success is never rolled back", |_, w: &World| {
                !w.quiescent()
                    || (0..2).all(|d| !w.success[d] || w.last_arm != Some(d) || (w.running == TOPLEVEL[d] && w.profile() == TOPLEVEL[d]))
            }),
            Property::always("a timely confirm after verification never races the timer", |_, w: &World| !(w.timely && w.lost_race)),
            Property::sometimes("a deploy succeeds after a dead one", |_, w: &World| w.success[1] && w.deploys[0] == Status::Dead),
            Property::sometimes("a failed verification rolls back", |_, w: &World| {
                w.quiescent() && matches!(w.record, Some(Record { phase: Phase::RolledBack, .. })) && w.faults == 1
            }),
            Property::sometimes("the host crashed mid-switch", |_, w: &World| {
                w.quiescent() && w.faults == 0 && w.record.is_none() && w.last_arm.is_some()
            }),
        ]
    }
}

fn check(model: Composed) {
    let props = model.properties();
    let checker = model
        .checker()
        .threads(std::thread::available_parallelism().map_or(1, |n| n.get()).min(4))
        .target_state_count(MAX_STATES)
        .target_max_depth(MAX_DEPTH)
        .spawn_bfs()
        .join();
    eprintln!("composed model: {} states ({} unique), depth {}", checker.state_count(), checker.unique_state_count(), checker.max_depth());
    for p in props {
        if let (stateright::Expectation::Always, Some(path)) = (&p.expectation, checker.discovery(p.name)) {
            eprintln!("counterexample to {:?}:", p.name);
            for (s, a) in path.into_vec() {
                let deploys: Vec<String> = s
                    .deploys
                    .iter()
                    .map(|d| match d {
                        Status::Active { wait, .. } => format!("waiting {wait:?}"),
                        other => format!("{other:?}"),
                    })
                    .collect();
                eprintln!(
                    "  t={} running {} profile {} record {:?} timer {:?} runs {:?} deploys {:?}",
                    s.clock,
                    SYS[s.running],
                    SYS[s.profile()],
                    s.record,
                    s.timer_due,
                    s.runs.iter().map(|r| (r.owner.clone(), r.m.verb().clone(), r.q.clone())).collect::<Vec<_>>(),
                    deploys
                );
                if let Some(a) = a {
                    eprintln!("-> {a:?}");
                }
            }
        }
    }
    assert!(checker.state_count() < MAX_STATES, "the state space outgrew MAX_STATES");
    assert!(checker.max_depth() < MAX_DEPTH, "the model ran deeper than MAX_DEPTH");
    checker.assert_properties();
}

#[test]
fn composed_model_checks() {
    check(Composed { second: true, faults: 1 });
}
