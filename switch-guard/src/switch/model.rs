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
use super::fake::{self, Outcome, World as _, OUTCOMES};
use crate::record::{Phase, Record};
use stateright::{Model, Property};
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
    target: fake::Target<Sys>,
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
    fn quiescent(&self) -> bool {
        !self.timer
            && self.runs.iter().all(Option::is_none)
            && self.deploys.iter().all(|d| matches!(d, Deploy::Idle | Deploy::Done { .. } | Deploy::Dead))
    }
}

impl fake::World<Sys> for World {
    fn target(&mut self) -> &mut fake::Target<Sys> {
        &mut self.target
    }

    fn timer_pending(&self) -> bool {
        self.timer
    }

    fn rollback_running(&self) -> bool {
        self.runs[TIMER].is_some()
    }

    fn start_timer(&mut self, _secs: u64) {
        self.timer = true;
    }

    fn fault(&mut self) -> bool {
        if self.faults == 0 {
            return false;
        }
        self.faults -= 1;
        true
    }
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

const DEADLINE: u64 = 10;
const GUARD: u64 = 600;
/// Bounds the check fails at rather than miss part of the space (it holds
/// about 424k states, 182k unique, depth 76).
const MAX_STATES: usize = 5_000_000;
const MAX_DEPTH: usize = 200;

fn spawn(w: &mut World, slot: usize, verb: Verb<Sys>) {
    let mut m = Machine::new(verb, DEADLINE, GUARD);
    let step = m.decide(Reply::Start);
    assert!(step.effects.is_empty());
    let Next::Ask(q) = step.next else { panic!("a run exits before its first query") };
    w.runs[slot] = Some(Run { m, q, locked: false });
}

fn end_run(w: &mut World, slot: usize, code: i32) {
    let run = w.runs[slot].take().expect("a live run");
    if run.locked {
        w.target.lock = false;
    }
    if w.stuck == Some(slot) {
        w.stuck = None;
        if matches!(&w.target.record, Some(Record { previous, phase: Phase::Armed }) if *previous != w.target.running) {
            w.exhausted = true;
        }
    }
    if let Verb::Confirm { reboot_pending: true, .. } = run.m.verb() {
        if code == 0 {
            w.staged = Some(w.target.current);
        }
    }
    if slot < 2 && w.deploys[slot] != Deploy::Dead {
        w.exits[slot] = Some(code);
    }
}

/// The world's answer to `slot`'s query under `o`, or None when `o` is not
/// a possible outcome of it (or the query must wait, for the lock): the
/// fake target's, besides a failing profile set and a rollback whose every
/// attempt fails.
fn answer(w: &mut World, slot: usize, o: Outcome) -> Option<Reply<Sys>> {
    let run = w.runs[slot].as_ref()?;
    // The activation's own switch, not the rollback's after a failed timer restart.
    let activation = matches!((run.m.verb(), &run.q), (Verb::Activate(new), Query::Switch(s, _)) if s == new);
    let r = match (run.q.clone(), o) {
        (Query::SetProfile(_), Outcome::FailBefore) if w.fault() => Reply::Flag(false),
        (Query::Switch(_, Action::Test), o) if !activation && w.stuck == Some(slot) => {
            if o != Outcome::FailBefore {
                return None;
            }
            Reply::Code(1)
        }
        (Query::Switch(_, Action::Test), Outcome::Incompatible) if !activation => {
            if !w.fault() {
                return None;
            }
            w.stuck = Some(slot);
            Reply::Code(1)
        }
        (q, o) => {
            let lock = q == Query::Lock;
            let r = fake::answer(w, q, o, activation)?;
            if lock {
                w.runs[slot].as_mut()?.locked = true;
            }
            r
        }
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
                if w.hand_fixed && r.phase == Phase::Armed {
                    w.armed_after_fix = true;
                }
                w.exhausted = false;
                w.hand_fixed = false;
                w.target.record = Some(r);
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
    if success && (w.target.running != TOPLEVEL[d] || w.target.profile() != TOPLEVEL[d]) {
        w.false_success = true;
    }
}

impl Model for Target {
    type State = World;
    type Action = Act;

    fn init_states(&self) -> Vec<World> {
        vec![World {
            target: fake::Target::new(Sys::Old),
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
                    if w.target.running == TOPLEVEL[d] {
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
                for o in OUTCOMES {
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
                let before = w.target.record.clone();
                if !step_run(&mut w, slot, o) {
                    return None;
                }
                if slot < 2
                    && matches!(w.target.record, Some(Record { phase: Phase::Armed, .. }))
                    && before != w.target.record
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
                let t = &mut w.target;
                t.gens.push(Some(t.running));
                t.current = t.gens.len() - 1;
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
                let t = &w.target;
                match &t.record {
                    None => t.running == Sys::Old,
                    Some(Record { previous, phase: Phase::Confirmed(c) }) => t.running == *c || t.running == *previous,
                    Some(Record { previous, .. }) => t.running == *previous,
                }
            }),
            Property::always("quiescent: profile matches running unless reboot-pending", |_, w: &World| {
                !w.quiescent() || w.exhausted || w.target.profile() == w.target.running || w.staged == Some(w.target.current)
            }),
            Property::always("quiescent: a timer that fired before a delayed activation undid it", |_, w: &World| {
                !w.quiescent()
                    || w.exhausted
                    || w.hand_fixed
                    || (0..2).all(|d| !(w.fired_early[d] && w.last_arm == Some(d)) || w.target.running != TOPLEVEL[d])
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
                    && matches!(w.target.record, Some(Record { phase: Phase::RolledBack, .. }))
                    && w.target.gens.len() == 1
            }),
            Property::sometimes("reboot-pending staged", |_, w: &World| w.quiescent() && w.staged == Some(w.target.current)),
            Property::sometimes("bootloader failure restored the profile", |_, w: &World| {
                w.quiescent() && w.target.gens.iter().any(Option::is_none)
            }),
            Property::sometimes("a rollback gave up", |_, w: &World| w.quiescent() && w.exhausted),
            Property::sometimes("a switch after a hand fix rolled back", |_, w: &World| {
                w.quiescent() && w.armed_after_fix && matches!(w.target.record, Some(Record { phase: Phase::RolledBack, .. }))
            }),
            Property::sometimes("a rollback attempt failed and was retried", |_, w: &World| {
                w.quiescent() && w.faults == 0 && matches!(w.target.record, Some(Record { phase: Phase::RolledBack, .. }))
            }),
        ]
    }
}

#[test]
fn model_checks() {
    fake::check(Target, "confirmed-switch model", MAX_STATES, MAX_DEPTH, |w: &World| {
        format!(
            "running {:?} profile {:?} record {:?} timer {} deploys {:?} runs {:?}",
            w.target.running,
            w.target.profile(),
            w.target.record,
            w.timer,
            w.deploys,
            w.runs.iter().map(|r| r.as_ref().map(|r| (r.m.verb().clone(), r.q.clone()))).collect::<Vec<_>>()
        )
    });
}
