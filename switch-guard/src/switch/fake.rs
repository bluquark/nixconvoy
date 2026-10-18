//! The target as the helper's model checks fake it, and the checker run
//! both models share.
use super::core::{Action, Query, Reply};
use crate::record::Record;
use stateright::{Checker, Expectation, Model};
use std::fmt::Debug;
use std::hash::Hash;

/// How the target answers a query: normally, or with one of the failures
/// that query admits.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Outcome {
    Ok,
    /// switch-to-configuration exits nonzero after the system changed.
    FailAfter,
    /// It exits nonzero having changed nothing (or the bootloader install,
    /// the timer start fails).
    FailBefore,
    /// It exits 100: an incompatible init, nothing changed.
    Incompatible,
}

pub const OUTCOMES: [Outcome; 4] = [Outcome::Ok, Outcome::FailAfter, Outcome::FailBefore, Outcome::Incompatible];

/// What the helper's queries read and change on the target.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Target<S> {
    pub running: S,
    /// The system profile's generations (None: deleted) and the current one.
    pub gens: Vec<Option<S>>,
    pub current: usize,
    pub record: Option<Record<S>>,
    /// The helper's lock is held.
    pub lock: bool,
}

impl<S: Clone> Target<S> {
    pub fn new(running: S) -> Self {
        Target { running: running.clone(), gens: vec![Some(running)], current: 0, record: None, lock: false }
    }

    pub fn profile(&self) -> S {
        self.gens[self.current].clone().expect("the current generation exists")
    }
}

/// A model's world around the target: the rollback timer and the faults.
pub trait World<S> {
    fn target(&mut self) -> &mut Target<S>;
    /// The rollback timer is pending.
    fn timer_pending(&self) -> bool;
    /// The timer's rollback service is running.
    fn rollback_running(&self) -> bool;
    fn start_timer(&mut self);
    /// Spends a fault: false when none is left.
    fn fault(&mut self) -> bool;
}

/// The target's answer to a helper run's query `q` under `o` (`activation`:
/// the run is an activate), or None when `o` is not a possible outcome of it
/// or the query must wait for the lock.  A rollback's switch fails only by a
/// fault, and never as incompatible.  Taking the lock marks it held; the
/// caller marks its run.
pub fn answer<S: Clone + Eq, W: World<S>>(w: &mut W, q: Query<S>, o: Outcome, activation: bool) -> Option<Reply<S>> {
    let r = match (q, o) {
        (Query::Lock, Outcome::Ok) => {
            let t = w.target();
            if t.lock {
                return None;
            }
            t.lock = true;
            Reply::Locked(t.record.clone())
        }
        (Query::RollbackActive, Outcome::Ok) => Reply::Flag(w.rollback_running()),
        (Query::TimerLive, Outcome::Ok) => Reply::Flag(w.timer_pending() || w.rollback_running()),
        (Query::ProfileSystem, Outcome::Ok) => Reply::Running(Some(w.target().profile())),
        (Query::Running, Outcome::Ok) => Reply::Running(Some(w.target().running.clone())),
        // systemd-run refuses while the unit's service still exists.
        (Query::StartTimer, Outcome::Ok) => {
            if w.timer_pending() || w.rollback_running() {
                Reply::Flag(false)
            } else {
                w.start_timer();
                Reply::Flag(true)
            }
        }
        (Query::StartTimer, Outcome::FailBefore) if w.fault() => Reply::Flag(false),
        (Query::Generation, Outcome::Ok) => Reply::Generation(Some(w.target().current as u64)),
        (Query::SetProfile(s), Outcome::Ok) => {
            let t = w.target();
            t.gens.push(Some(s));
            t.current = t.gens.len() - 1;
            Reply::Flag(true)
        }
        (Query::Switch(s, Action::Test), o) => {
            if !activation && (o == Outcome::Incompatible || (o != Outcome::Ok && !w.fault())) {
                return None;
            }
            match o {
                Outcome::Ok => {
                    w.target().running = s;
                    Reply::Code(0)
                }
                Outcome::FailAfter => {
                    w.target().running = s;
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
            let t = w.target();
            let g = g as usize;
            let ok = t.gens.get(g).is_some_and(Option::is_some);
            if ok {
                t.current = g;
            }
            Reply::Flag(ok)
        }
        (Query::DeleteGeneration(g), Outcome::Ok) => {
            let t = w.target();
            let g = g as usize;
            let ok = g != t.current && g < t.gens.len();
            if ok {
                t.gens[g] = None;
            }
            Reply::Flag(ok)
        }
        _ => return None,
    };
    Some(r)
}

/// Checks `model`'s properties, failing past `max_states` or `max_depth`
/// rather than missing part of the space; each counterexample's last steps
/// are printed by `show`.
pub fn check<M>(model: M, name: &str, max_states: usize, max_depth: usize, show: impl Fn(&M::State) -> String)
where
    M: Model + Send + Sync + 'static,
    M::State: Debug + Hash + Send + Sync + Clone + PartialEq + 'static,
    M::Action: Debug + Clone + PartialEq,
{
    let props = model.properties();
    let checker = model
        .checker()
        .threads(std::thread::available_parallelism().map_or(1, |n| n.get()).min(4))
        .target_state_count(max_states)
        .target_max_depth(max_depth)
        .spawn_bfs()
        .join();
    eprintln!("{name}: {} states ({} unique), depth {}", checker.state_count(), checker.unique_state_count(), checker.max_depth());
    for p in props {
        if let (Expectation::Always, Some(path)) = (&p.expectation, checker.discovery(p.name)) {
            eprintln!("counterexample to {:?}:", p.name);
            let v = path.into_vec();
            let skip = v.len().saturating_sub(30);
            for (state, action) in v.into_iter().skip(skip) {
                eprintln!("  {}", show(&state));
                if let Some(a) = action {
                    eprintln!("-> {a:?}");
                }
            }
        }
    }
    assert!(checker.state_count() < max_states, "the state space outgrew MAX_STATES");
    assert!(checker.max_depth() < max_depth, "the model ran deeper than MAX_DEPTH");
    checker.assert_properties();
}
