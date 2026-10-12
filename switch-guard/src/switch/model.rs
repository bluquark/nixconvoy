//! Exhaustive model of confirmed switches on one target: two deploys (each
//! nixconvoy's switch() for its own toplevel) racing for the target's switch
//! guard, either of which may die at any point (its detached helper runs
//! carry on, and so does its attached `arm`, which ignores the session's
//! end), the dead-man timer firing
//! whenever it is pending, every activation outcome, a failing bootloader
//! install, and one further fault (a failed rollback attempt, a rollback
//! whose every attempt fails, a profile set or a timer start).  After an
//! exhausted rollback the target may be fixed by hand (its profile set to what
//! it runs) before the next deploy.  The helper runs are the real `Machine`s, stepped one query
//! at a time so every interleaving under the helper's lock is explored.
use super::core::{Action, Machine, Next, Query, Reply, Verb, ALREADY_CONFIRMED};
use crate::record::{Phase, Record};
use stateright::{Checker, Model, Property};
use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Sys {
    Old,
    A,
    B,
}

impl fmt::Display for Sys {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}

const TOPLEVEL: [Sys; 2] = [Sys::A, Sys::B];
const TIMER: usize = 2;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct Run {
    m: Machine<Sys>,
    q: Query<Sys>,
    locked: bool,
}

/// nixconvoy's switch() for one target, from taking the guard to its verdict.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Deploy {
    Idle,
    Arming,
    Activating,
    Verifying { clean: bool },
    Staging,
    Confirming { clean: bool },
    RollingBack,
    ConfirmRollingBack { clean: bool },
    Done { success: bool },
    Dead,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct World {
    running: Sys,
    /// The system profile's generations (None: deleted) and the current one.
    gens: Vec<Option<Sys>>,
    current: usize,
    record: Option<Record<Sys>>,
    lock: Option<usize>,
    timer: bool,
    /// Slots 0 and 1: each deploy's helper run (at most one at a time);
    /// slot 2: the timer's rollback service.
    runs: [Option<Run>; 3],
    exits: [Option<i32>; 2],
    deploys: [Deploy; 2],
    guard: Option<usize>,
    faults: u8,
    // Ghosts, for the properties.
    /// The generation a reboot-pending confirm set: the profile may lead
    /// the running system while it is current.
    staged: Option<usize>,
    /// The run whose rollback attempts all fail.
    stuck: Option<usize>,
    /// A rollback gave up with the target off its record's previous system,
    /// not handled since.
    exhausted: bool,
    /// Fixed by hand after that, and not re-armed since.
    hand_fixed: bool,
    /// A switch armed after a hand fix.
    armed_after_fix: bool,
    last_arm: Option<usize>,
    fired_early: [bool; 2],
    false_success: bool,
}

impl World {
    fn profile(&self) -> Sys {
        self.gens[self.current].expect("the current generation exists")
    }

    fn quiescent(&self) -> bool {
        !self.timer
            && self.runs.iter().all(Option::is_none)
            && self.deploys.iter().all(|d| matches!(d, Deploy::Idle | Deploy::Done { .. } | Deploy::Dead))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Outcome {
    Ok,
    /// switch-to-configuration exits nonzero after the system changed.
    FailAfter,
    /// It exits nonzero having changed nothing.
    FailBefore,
    /// It exits 100: an incompatible init, nothing changed.
    Incompatible,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Act {
    Start(usize),
    Die(usize),
    Fire,
    Answer(usize, Outcome),
    Proceed(usize),
    VerifyPass(usize),
    VerifyFail(usize),
    HandFix,
}

struct Target;

const DEADLINE: u64 = 120;
/// Bounds the check fails at rather than miss part of the space (it holds
/// about 424k states, 182k unique, depth 76).
const MAX_STATES: usize = 5_000_000;
const MAX_DEPTH: usize = 200;

fn spawn(w: &mut World, slot: usize, verb: Verb<Sys>) {
    let mut m = Machine::new(verb, DEADLINE);
    let step = m.decide(Reply::Start);
    assert!(step.effects.is_empty());
    let Next::Ask(q) = step.next else { panic!("a run exits before its first query") };
    w.runs[slot] = Some(Run { m, q, locked: false });
}

fn end_run(w: &mut World, slot: usize, code: i32) {
    let run = w.runs[slot].take().expect("a live run");
    if run.locked {
        w.lock = None;
    }
    if w.stuck == Some(slot) {
        w.stuck = None;
        if matches!(&w.record, Some(Record { previous, phase: Phase::Armed }) if *previous != w.running) {
            w.exhausted = true;
        }
    }
    if let Verb::Confirm { reboot_pending: true, .. } = run.m.verb() {
        if code == 0 {
            w.staged = Some(w.current);
        }
    }
    if slot < 2 && w.deploys[slot] != Deploy::Dead {
        w.exits[slot] = Some(code);
    }
}

/// The world's answer to `slot`'s query under `o`, or None when `o` is not
/// a possible outcome of it (or the query must wait, for the lock).
fn answer(w: &mut World, slot: usize, o: Outcome) -> Option<Reply<Sys>> {
    let run = w.runs[slot].as_ref()?;
    let activation = matches!(run.m.verb(), Verb::Activate(_));
    let fault = |w: &mut World| {
        if w.faults == 0 {
            return false;
        }
        w.faults -= 1;
        true
    };
    let r = match (run.q.clone(), o) {
        (Query::Lock, Outcome::Ok) => {
            if w.lock.is_some() {
                return None;
            }
            w.lock = Some(slot);
            w.runs[slot].as_mut()?.locked = true;
            Reply::Locked(w.record.clone())
        }
        (Query::RollbackActive, Outcome::Ok) => Reply::Flag(w.runs[TIMER].is_some()),
        (Query::TimerLive, Outcome::Ok) => Reply::Flag(w.timer || w.runs[TIMER].is_some()),
        (Query::ProfileSystem, Outcome::Ok) => Reply::Running(Some(w.profile())),
        (Query::Running, Outcome::Ok) => Reply::Running(Some(w.running)),
        // systemd-run refuses while the unit's service still exists.
        (Query::StartTimer, Outcome::Ok) => {
            if w.timer || w.runs[TIMER].is_some() {
                Reply::Flag(false)
            } else {
                w.timer = true;
                Reply::Flag(true)
            }
        }
        (Query::StartTimer, Outcome::FailBefore) if fault(w) => Reply::Flag(false),
        (Query::Generation, Outcome::Ok) => Reply::Generation(Some(w.current as u64)),
        (Query::SetProfile(s), Outcome::Ok) => {
            w.gens.push(Some(s));
            w.current = w.gens.len() - 1;
            Reply::Flag(true)
        }
        (Query::SetProfile(_), Outcome::FailBefore) if fault(w) => Reply::Flag(false),
        (Query::Switch(_, Action::Test), o) if !activation && w.stuck == Some(slot) => {
            if o != Outcome::FailBefore {
                return None;
            }
            Reply::Code(1)
        }
        (Query::Switch(_, Action::Test), Outcome::Incompatible) if !activation => {
            if !fault(w) {
                return None;
            }
            w.stuck = Some(slot);
            Reply::Code(1)
        }
        (Query::Switch(s, Action::Test), o) => {
            let allowed = activation || matches!(o, Outcome::Ok) || fault(w);
            if !allowed || (!activation && o == Outcome::Incompatible) {
                return None;
            }
            match o {
                Outcome::Ok => {
                    w.running = s;
                    Reply::Code(0)
                }
                Outcome::FailAfter => {
                    w.running = s;
                    Reply::Code(4)
                }
                Outcome::FailBefore => Reply::Code(1),
                Outcome::Incompatible => Reply::Code(100),
            }
        }
        (Query::Switch(_, Action::Boot), Outcome::Ok) => Reply::Code(0),
        (Query::Switch(_, Action::Boot), Outcome::FailBefore) => Reply::Code(1),
        (Query::WaitSwitchLock | Query::Sleep(_), Outcome::Ok) => Reply::Done,
        (Query::SwitchGeneration(g), Outcome::Ok) => {
            let g = g as usize;
            if w.gens.get(g).copied().flatten().is_some() {
                w.current = g;
                Reply::Flag(true)
            } else {
                Reply::Flag(false)
            }
        }
        (Query::DeleteGeneration(g), Outcome::Ok) => {
            let g = g as usize;
            if g != w.current && g < w.gens.len() {
                w.gens[g] = None;
                Reply::Flag(true)
            } else {
                Reply::Flag(false)
            }
        }
        _ => return None,
    };
    Some(r)
}

fn step_run(w: &mut World, slot: usize, o: Outcome) -> bool {
    let mut probe = w.clone();
    let Some(reply) = answer(&mut probe, slot, o) else { return false };
    *w = probe;
    let run = w.runs[slot].as_mut().expect("a live run");
    let step = run.m.decide(reply);
    for e in step.effects {
        use super::core::Effect::*;
        match e {
            Say(_) | Warn(_) | Log(_) | ResetFailedRollback => {}
            StopTimer => w.timer = false,
            Persist(r) => {
                if w.hand_fixed && matches!(r, Some(Record { phase: Phase::Armed, .. })) {
                    w.armed_after_fix = true;
                }
                w.exhausted = false;
                w.hand_fixed = false;
                w.record = r;
            }
        }
    }
    match step.next {
        Next::Ask(q) => w.runs[slot].as_mut().expect("a live run").q = q,
        Next::Exit(c) => end_run(w, slot, c),
    }
    true
}

fn finish(w: &mut World, d: usize, success: bool) {
    w.deploys[d] = Deploy::Done { success };
    w.guard = None;
    if success && (w.running != TOPLEVEL[d] || w.profile() != TOPLEVEL[d]) {
        w.false_success = true;
    }
}

impl Model for Target {
    type State = World;
    type Action = Act;

    fn init_states(&self) -> Vec<World> {
        vec![World {
            running: Sys::Old,
            gens: vec![Some(Sys::Old)],
            current: 0,
            record: None,
            lock: None,
            timer: false,
            runs: [None, None, None],
            exits: [None, None],
            deploys: [Deploy::Idle, Deploy::Idle],
            guard: None,
            faults: 1,
            staged: None,
            stuck: None,
            exhausted: false,
            hand_fixed: false,
            armed_after_fix: false,
            last_arm: None,
            fired_early: [false, false],
            false_success: false,
        }]
    }

    fn actions(&self, w: &World, acts: &mut Vec<Act>) {
        for (d, deploy) in w.deploys.iter().enumerate() {
            match *deploy {
                Deploy::Idle if w.guard.is_none() => acts.push(Act::Start(d)),
                Deploy::Idle | Deploy::Done { .. } => {}
                Deploy::Dead => {}
                Deploy::Verifying { .. } => {
                    acts.push(Act::Die(d));
                    acts.push(Act::VerifyFail(d));
                    if w.running == TOPLEVEL[d] {
                        acts.push(Act::VerifyPass(d));
                    }
                }
                _ => {
                    acts.push(Act::Die(d));
                    if w.exits[d].is_some() {
                        acts.push(Act::Proceed(d));
                    }
                }
            }
        }
        if w.timer {
            acts.push(Act::Fire);
        }
        if w.exhausted && w.quiescent() {
            acts.push(Act::HandFix);
        }
        for slot in 0..3 {
            if w.runs[slot].is_some() {
                for o in [Outcome::Ok, Outcome::FailAfter, Outcome::FailBefore, Outcome::Incompatible] {
                    acts.push(Act::Answer(slot, o));
                }
            }
        }
    }

    fn next_state(&self, w: &World, act: Act) -> Option<World> {
        let mut w = w.clone();
        match act {
            Act::Start(d) => {
                w.guard = Some(d);
                w.deploys[d] = Deploy::Arming;
                spawn(&mut w, d, Verb::Arm);
            }
            Act::Die(d) => {
                w.deploys[d] = Deploy::Dead;
                w.exits[d] = None;
                if w.guard == Some(d) {
                    w.guard = None;
                }
            }
            Act::Fire => {
                w.timer = false;
                for d in 0..2 {
                    if let Some(r) = &w.runs[d] {
                        if matches!(r.m.verb(), Verb::Activate(_)) && !r.locked {
                            w.fired_early[d] = true;
                        }
                    }
                }
                spawn(&mut w, TIMER, Verb::Rollback);
            }
            Act::Answer(slot, o) => {
                let before = w.record.clone();
                if !step_run(&mut w, slot, o) {
                    return None;
                }
                if slot < 2
                    && matches!(w.record, Some(Record { phase: Phase::Armed, .. }))
                    && before != w.record
                    && matches!(before.map(|r| r.phase), None | Some(Phase::Confirmed(_)) | Some(Phase::RolledBack) | Some(Phase::Armed))
                    && w.runs[slot].as_ref().map(|r| r.m.verb()) == Some(&Verb::Arm)
                {
                    w.last_arm = Some(slot);
                    w.fired_early[slot] = false;
                }
            }
            Act::VerifyPass(d) => {
                let Deploy::Verifying { clean } = w.deploys[d] else { return None };
                w.deploys[d] = Deploy::Confirming { clean };
                spawn(&mut w, d, Verb::Confirm { new: TOPLEVEL[d], reboot_pending: false });
            }
            Act::HandFix => {
                w.gens.push(Some(w.running));
                w.current = w.gens.len() - 1;
                w.exhausted = false;
                w.hand_fixed = true;
            }
            Act::VerifyFail(d) => {
                w.deploys[d] = Deploy::RollingBack;
                spawn(&mut w, d, Verb::Rollback);
            }
            Act::Proceed(d) => {
                let c = w.exits[d].take()?;
                match w.deploys[d] {
                    Deploy::Arming if c == 0 => {
                        w.deploys[d] = Deploy::Activating;
                        spawn(&mut w, d, Verb::Activate(TOPLEVEL[d]));
                    }
                    Deploy::Arming => finish(&mut w, d, false),
                    Deploy::Activating if c == 100 => {
                        w.deploys[d] = Deploy::Staging;
                        spawn(&mut w, d, Verb::Confirm { new: TOPLEVEL[d], reboot_pending: true });
                    }
                    Deploy::Activating => w.deploys[d] = Deploy::Verifying { clean: c == 0 },
                    Deploy::Staging | Deploy::RollingBack => finish(&mut w, d, false),
                    Deploy::Confirming { clean } if c == 0 => finish(&mut w, d, clean),
                    Deploy::Confirming { clean } => {
                        w.deploys[d] = Deploy::ConfirmRollingBack { clean };
                        spawn(&mut w, d, Verb::Rollback);
                    }
                    // Exit 3: the confirm landed, only its relay died.
                    Deploy::ConfirmRollingBack { clean } => finish(&mut w, d, clean && c == ALREADY_CONFIRMED),
                    _ => return None,
                }
            }
        }
        Some(w)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        vec![
            Property::always("quiescent: runs the confirmed toplevel or previous", |_, w: &World| {
                if !w.quiescent() || w.exhausted || w.hand_fixed {
                    return true;
                }
                match &w.record {
                    None => w.running == Sys::Old,
                    Some(Record { previous, phase: Phase::Confirmed(t) }) => w.running == *t || w.running == *previous,
                    Some(Record { previous, .. }) => w.running == *previous,
                }
            }),
            Property::always("quiescent: profile matches running unless reboot-pending", |_, w: &World| {
                !w.quiescent() || w.exhausted || w.profile() == w.running || w.staged == Some(w.current)
            }),
            Property::always("quiescent: a timer that fired before a delayed activation undid it", |_, w: &World| {
                !w.quiescent()
                    || w.exhausted
                    || w.hand_fixed
                    || (0..2).all(|d| !(w.fired_early[d] && w.last_arm == Some(d)) || w.running != TOPLEVEL[d])
            }),
            Property::always("nixconvoy reports success only when the target runs and boots its toplevel", |_, w: &World| !w.false_success),
            Property::sometimes("both deploys confirmed in turn", |_, w: &World| {
                matches!(w.deploys, [Deploy::Done { success: true }, Deploy::Done { success: true }])
            }),
            Property::sometimes("timer fired before a delayed activation", |_, w: &World| {
                w.quiescent() && w.fired_early.iter().any(|f| *f)
            }),
            Property::sometimes("timer rolled back a dead deploy's activation", |_, w: &World| {
                w.quiescent()
                    && w.deploys.contains(&Deploy::Dead)
                    && matches!(w.record, Some(Record { phase: Phase::RolledBack, .. }))
                    && w.gens.len() == 1
            }),
            Property::sometimes("reboot-pending staged", |_, w: &World| w.quiescent() && w.staged == Some(w.current)),
            Property::sometimes("bootloader failure restored the profile", |_, w: &World| {
                w.quiescent() && w.gens.iter().any(Option::is_none)
            }),
            Property::sometimes("a rollback gave up", |_, w: &World| w.quiescent() && w.exhausted),
            Property::sometimes("a switch after a hand fix rolled back", |_, w: &World| {
                w.quiescent() && w.armed_after_fix && matches!(w.record, Some(Record { phase: Phase::RolledBack, .. }))
            }),
            Property::sometimes("a rollback attempt failed and was retried", |_, w: &World| {
                w.quiescent() && w.faults == 0 && matches!(w.record, Some(Record { phase: Phase::RolledBack, .. }))
            }),
        ]
    }
}

#[test]
fn model_checks() {
    let checker = Target
        .checker()
        .threads(std::thread::available_parallelism().map_or(1, |n| n.get()).min(4))
        .target_state_count(MAX_STATES)
        .target_max_depth(MAX_DEPTH)
        .spawn_bfs()
        .join();
    eprintln!(
        "confirmed-switch model: {} states ({} unique), depth {}",
        checker.state_count(),
        checker.unique_state_count(),
        checker.max_depth()
    );
    for p in Target.properties() {
        if let (stateright::Expectation::Always, Some(path)) = (&p.expectation, checker.discovery(p.name)) {
            let v = path.into_vec();
            let skip = v.len().saturating_sub(30);
            for (state, action) in v.into_iter().skip(skip) {
                eprintln!(
                    "  running {:?} profile {:?} record {:?} timer {} deploys {:?} runs {:?}",
                    state.running,
                    state.profile(),
                    state.record,
                    state.timer,
                    state.deploys,
                    state.runs.iter().map(|r| r.as_ref().map(|r| (r.m.verb().clone(), r.q.clone()))).collect::<Vec<_>>()
                );
                if let Some(a) = action {
                    eprintln!("-> {a:?}");
                }
            }
        }
    }
    assert!(checker.state_count() < MAX_STATES, "the state space outgrew MAX_STATES");
    assert!(checker.max_depth() < MAX_DEPTH, "the model ran deeper than MAX_DEPTH");
    checker.assert_properties();
}
