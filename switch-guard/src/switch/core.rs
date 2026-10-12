//! The pure helper: one `Machine` per invocation, fed the reply to its last
//! query and answering with effects plus the next query or its exit code.
//!
//! arm, activate, confirm and rollback serialize on the helper's lock and
//! read the record under it: the timer's rollback firing during an
//! activation or a confirm waits for it, and one that fired before a delayed
//! activation has recorded RolledBack, which activate then refuses.  Only
//! an Armed record admits an activation or a confirm, so a delayed helper
//! run of a deploy that died cannot act on the record of the deploy that
//! re-armed after it.
use crate::record::{Phase, Record};
use std::fmt::Display;

pub const ROLLBACK_ATTEMPTS: u32 = 5;
pub const RETRY_SLEEP: u64 = 5;
/// Exit code of `rollback` when the record is confirmed: nixconvoy reads
/// it as "the confirm landed, only its relay died".
pub const ALREADY_CONFIRMED: i32 = 3;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Verb<S> {
    Arm,
    Activate(S),
    /// `reboot_pending`: the activation exited 100 (an init incompatible
    /// with the running one) without changing anything; the new system is
    /// staged for a manual reboot, as a plain switch would leave it.
    Confirm {
        new: S,
        reboot_pending: bool,
    },
    Rollback,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Action {
    Test,
    Boot,
}

impl Action {
    pub fn as_str(self) -> &'static str {
        match self {
            Action::Test => "test",
            Action::Boot => "boot",
        }
    }
}

/// Effects whose outcome the machine does not branch on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Effect<S> {
    Say(String),
    Warn(String),
    Log(String),
    StopTimer,
    ResetFailedRollback,
    /// None removes the record.  A failure to persist ends the run (exit 1).
    Persist(Option<Record<S>>),
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Query<S> {
    /// Whether the timer's rollback service is running -> Flag.
    RollbackActive,
    /// Whether the rollback timer is pending or its service running -> Flag.
    TimerLive,
    /// The system the profile's current generation names -> Running.
    ProfileSystem,
    /// Take the helper's lock (blocking), then read the record -> Locked.
    Lock,
    /// /run/current-system -> Running.
    Running,
    /// Start the transient rollback timer -> Flag.
    StartTimer,
    /// The system profile's current generation -> Generation.
    Generation,
    /// `nix-env --set` -> Flag.
    SetProfile(S),
    /// `<system>/bin/switch-to-configuration <action>` -> Code.
    Switch(S, Action),
    /// Wait (bounded) for switch-to-configuration's own lock -> Done.
    WaitSwitchLock,
    Sleep(u64),
    /// -> Flag.
    SwitchGeneration(u64),
    /// -> Flag.
    DeleteGeneration(u64),
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Reply<S> {
    Start,
    Flag(bool),
    Locked(Option<Record<S>>),
    Running(Option<S>),
    Generation(Option<u64>),
    Code(i32),
    Done,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Next<S> {
    Ask(Query<S>),
    Exit(i32),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Step<S> {
    pub effects: Vec<Effect<S>>,
    pub next: Next<S>,
}

/// Where the run is: each variant awaits the reply to one query.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Pc<S> {
    Start,
    ArmActive,
    ArmLocked,
    ArmTimerLive(S),
    ArmProfile(S),
    ArmRunning(Option<S>),
    ArmTimer(S),
    ActLocked,
    ActSwitched,
    ConfLocked,
    ConfRunning {
        previous: S,
        want: S,
    },
    ConfBefore {
        previous: S,
    },
    ConfProfile {
        previous: S,
        before: Option<u64>,
    },
    ConfBoot {
        previous: S,
        before: Option<u64>,
    },
    ConfAfter {
        previous: S,
        before: Option<u64>,
    },
    ConfRestored {
        previous: S,
        after: u64,
    },
    ConfDeleted {
        previous: S,
    },
    RbLocked,
    RbSettled {
        previous: S,
    },
    /// The rollback loop, attempt `n` (1-based).
    RbWaited {
        previous: S,
        n: u32,
        then: Then,
    },
    RbRunning {
        previous: S,
        n: u32,
        then: Then,
    },
    RbSwitched {
        previous: S,
        n: u32,
        then: Then,
    },
    RbSlept {
        previous: S,
        n: u32,
        then: Then,
    },
    RbFinal {
        previous: S,
        then: Then,
    },
    Finished,
}

/// What a finished rollback loop exits with: the rollback verb reports it,
/// a failed confirm exits 1 either way.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Then {
    Report,
    Fail,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Machine<S> {
    verb: Verb<S>,
    deadline: u64,
    pc: Pc<S>,
}

const P: &str = "confirmed-switch";

fn opt<S: Display>(s: &Option<S>) -> String {
    s.as_ref().map_or_else(|| "nothing".to_string(), |s| s.to_string())
}

impl<S: Clone + Eq + Display + std::fmt::Debug> Machine<S> {
    pub fn new(verb: Verb<S>, deadline: u64) -> Self {
        Machine { verb, deadline, pc: Pc::Start }
    }

    pub fn verb(&self) -> &Verb<S> {
        &self.verb
    }

    pub fn finished(&self) -> bool {
        self.pc == Pc::Finished
    }

    /// The machine's transition: `reply` answers the query the previous
    /// step asked (Reply::Start for the first step).
    pub fn decide(&mut self, reply: Reply<S>) -> Step<S> {
        let mut fx = Vec::new();
        let pc = std::mem::replace(&mut self.pc, Pc::Finished);
        let next = self.transition(pc, reply, &mut fx);
        Step { effects: fx, next }
    }

    fn ask(&mut self, pc: Pc<S>, q: Query<S>) -> Next<S> {
        self.pc = pc;
        Next::Ask(q)
    }

    fn transition(&mut self, pc: Pc<S>, reply: Reply<S>, fx: &mut Vec<Effect<S>>) -> Next<S> {
        use Pc::*;
        match (pc, reply) {
            (Start, Reply::Start) => match &self.verb {
                Verb::Arm => self.ask(ArmActive, Query::RollbackActive),
                Verb::Activate(_) => self.ask(ActLocked, Query::Lock),
                Verb::Confirm { .. } => self.ask(ConfLocked, Query::Lock),
                Verb::Rollback => self.ask(RbLocked, Query::Lock),
            },

            (ArmActive, Reply::Flag(active)) => {
                if active {
                    fx.push(Effect::Say(format!("{P}: waiting for an earlier rollback to finish")));
                }
                self.ask(ArmLocked, Query::Lock)
            }
            // An Armed record here is a switch whose deploy died before
            // confirming: its activation may have landed or may still be
            // pending, so this switch takes over its timer's duty and rolls
            // back to the system it would have, never to an unconfirmed one.
            // Only while that duty is live - its timer pending or firing, or
            // the profile still naming its previous system: a record left
            // by an exhausted rollback outlives a target fixed by hand since.
            (ArmLocked, Reply::Locked(rec)) => match rec {
                Some(Record { previous, phase: Phase::Armed }) => self.ask(ArmTimerLive(previous), Query::TimerLive),
                _ => self.ask(ArmRunning(None), Query::Running),
            },
            (ArmTimerLive(previous), Reply::Flag(live)) => {
                if live {
                    return self.ask(ArmRunning(Some(previous)), Query::Running);
                }
                self.ask(ArmProfile(previous), Query::ProfileSystem)
            }
            (ArmProfile(previous), Reply::Running(profile)) => {
                let inherited = (profile.as_ref() == Some(&previous)).then_some(previous);
                self.ask(ArmRunning(inherited), Query::Running)
            }
            (ArmRunning(inherited), Reply::Running(cur)) => {
                if let Some(prev) = inherited.as_ref().filter(|p| cur.as_ref() != Some(*p)) {
                    fx.push(Effect::Say(format!("{P}: an unconfirmed switch from {prev} is still pending; taking over its rollback")));
                }
                let Some(previous) = inherited.or(cur) else {
                    fx.push(Effect::Warn(format!("{P}: cannot read /run/current-system")));
                    return Next::Exit(1);
                };
                fx.push(Effect::StopTimer);
                fx.push(Effect::ResetFailedRollback);
                self.ask(ArmTimer(previous), Query::StartTimer)
            }
            // Armed only with the timer pending: an Armed record admits a
            // dead deploy's delayed activation too.
            (ArmTimer(previous), Reply::Flag(ok)) => {
                if !ok {
                    fx.push(Effect::Warn(format!("{P}: could not start the rollback timer")));
                    return self.rollback(previous, Then::Fail);
                }
                fx.push(Effect::Persist(Some(Record { previous: previous.clone(), phase: Phase::Armed })));
                fx.push(Effect::Say(format!("{P}: armed, rolling back to {previous} in {}s unless confirmed", self.deadline)));
                Next::Exit(0)
            }

            (ActLocked, Reply::Locked(rec)) => match rec.map(|r| r.phase) {
                Some(Phase::RolledBack) => {
                    fx.push(Effect::Warn(format!("{P}: already rolled back (the deadline passed first?), not activating")));
                    Next::Exit(1)
                }
                Some(Phase::Armed) => {
                    let Verb::Activate(new) = &self.verb else { unreachable!() };
                    let q = Query::Switch(new.clone(), Action::Test);
                    self.ask(ActSwitched, q)
                }
                Some(Phase::Confirmed(_)) | None => {
                    fx.push(Effect::Warn(format!("{P}: not armed")));
                    Next::Exit(1)
                }
            },
            (ActSwitched, Reply::Code(c)) => Next::Exit(c),

            (ConfLocked, Reply::Locked(rec)) => {
                let Verb::Confirm { new, reboot_pending } = &self.verb else { unreachable!() };
                match rec {
                    Some(Record { phase: Phase::RolledBack, .. }) => {
                        fx.push(Effect::Warn(format!("{P}: already rolled back (the deadline passed first?), not confirming")));
                        Next::Exit(1)
                    }
                    Some(Record { previous, phase: Phase::Armed }) => {
                        let want = if *reboot_pending { previous.clone() } else { new.clone() };
                        self.ask(ConfRunning { previous, want }, Query::Running)
                    }
                    Some(Record { phase: Phase::Confirmed(_), .. }) | None => {
                        fx.push(Effect::Warn(format!("{P}: not armed")));
                        Next::Exit(1)
                    }
                }
            }
            (ConfRunning { previous, want }, Reply::Running(cur)) => {
                if cur.as_ref() != Some(&want) {
                    fx.push(Effect::Warn(format!("{P}: running {}, not {want}", opt(&cur))));
                    return Next::Exit(1);
                }
                self.ask(ConfBefore { previous }, Query::Generation)
            }
            (ConfBefore { previous }, Reply::Generation(before)) => {
                let q = Query::SetProfile(self.target().clone());
                self.ask(ConfProfile { previous, before }, q)
            }
            (ConfProfile { previous, before }, Reply::Flag(ok)) => {
                if !ok {
                    return self.confirm_failed(previous, fx);
                }
                let q = Query::Switch(self.target().clone(), Action::Boot);
                self.ask(ConfBoot { previous, before }, q)
            }
            (ConfBoot { previous, before }, Reply::Code(c)) => {
                if c == 0 {
                    let new = self.target().clone();
                    fx.push(Effect::Persist(Some(Record { previous, phase: Phase::Confirmed(new.clone()) })));
                    fx.push(Effect::StopTimer);
                    fx.push(Effect::Say(format!("{P}: confirmed {new}")));
                    return Next::Exit(0);
                }
                // The generation --set made never got a bootloader entry:
                // back to the previous one, so the profile matches the boot
                // menu.
                self.ask(ConfAfter { previous, before }, Query::Generation)
            }
            (ConfAfter { previous, before }, Reply::Generation(after)) => match (before, after) {
                (Some(b), Some(a)) if a != b => self.ask(ConfRestored { previous, after: a }, Query::SwitchGeneration(b)),
                _ => self.confirm_failed(previous, fx),
            },
            (ConfRestored { previous, after }, Reply::Flag(_)) => self.ask(ConfDeleted { previous }, Query::DeleteGeneration(after)),
            (ConfDeleted { previous }, Reply::Flag(_)) => self.confirm_failed(previous, fx),

            (RbLocked, Reply::Locked(rec)) => match rec {
                Some(Record { phase: Phase::Confirmed(_), .. }) => {
                    fx.push(Effect::Say(format!("{P}: already confirmed, nothing to roll back")));
                    Next::Exit(ALREADY_CONFIRMED)
                }
                None => {
                    fx.push(Effect::Warn(format!("{P}: not armed")));
                    Next::Exit(1)
                }
                // The record alone is not enough: an activation outside the
                // helper after a timer that found nothing to undo still
                // needs undoing.
                Some(Record { previous, phase: Phase::RolledBack }) => self.ask(RbSettled { previous }, Query::Running),
                Some(Record { previous, phase: Phase::Armed }) => self.rollback(previous, Then::Report),
            },
            (RbSettled { previous }, Reply::Running(cur)) => {
                if cur.as_ref() == Some(&previous) {
                    fx.push(Effect::Say(format!("{P}: already rolled back to {previous}")));
                    return Next::Exit(0);
                }
                self.rollback(previous, Then::Report)
            }

            // An activation may still be running at the deadline
            // (switch-to-configuration refuses to start while another holds
            // its lock), so each attempt first waits for that lock; `test`
            // suffices since the profile and the bootloader were never
            // touched.
            (RbWaited { previous, n, then }, Reply::Done) => self.ask(RbRunning { previous, n, then }, Query::Running),
            (RbRunning { previous, n, then }, Reply::Running(cur)) => {
                if cur.as_ref() == Some(&previous) {
                    return self.rolled_back(previous, then, fx);
                }
                fx.push(Effect::Say(format!("{P}: rolling back {} to {previous}", opt(&cur))));
                let q = Query::Switch(previous.clone(), Action::Test);
                self.ask(RbSwitched { previous, n, then }, q)
            }
            (RbSwitched { previous, n, then }, Reply::Code(c)) => {
                if c != 0 {
                    return self.ask(RbSlept { previous, n, then }, Query::Sleep(RETRY_SLEEP));
                }
                self.attempt(previous, n + 1, then)
            }
            (RbSlept { previous, n, then }, Reply::Done) => self.attempt(previous, n + 1, then),
            (RbFinal { previous, then }, Reply::Running(cur)) => {
                if cur.as_ref() == Some(&previous) {
                    return self.rolled_back(previous, then, fx);
                }
                fx.push(Effect::Warn(format!("{P}: rollback failed, still running {}", opt(&cur))));
                Next::Exit(1)
            }

            (pc, reply) => panic!("{P}: reply {reply:?} does not answer {pc:?}"),
        }
    }

    fn target(&self) -> &S {
        match &self.verb {
            Verb::Confirm { new, .. } | Verb::Activate(new) => new,
            _ => unreachable!(),
        }
    }

    fn confirm_failed(&mut self, previous: S, fx: &mut Vec<Effect<S>>) -> Next<S> {
        fx.push(Effect::Warn(format!("{P}: could not confirm {}, rolling back", self.target())));
        self.rollback(previous, Then::Fail)
    }

    fn rollback(&mut self, previous: S, then: Then) -> Next<S> {
        self.attempt(previous, 1, then)
    }

    fn attempt(&mut self, previous: S, n: u32, then: Then) -> Next<S> {
        if n > ROLLBACK_ATTEMPTS {
            return self.ask(Pc::RbFinal { previous, then }, Query::Running);
        }
        self.ask(Pc::RbWaited { previous, n, then }, Query::WaitSwitchLock)
    }

    /// Recorded, and the timer stopped, only once the target demonstrably runs
    /// the previous system again, so a still-pending timer gets its retry.
    fn rolled_back(&mut self, previous: S, then: Then, fx: &mut Vec<Effect<S>>) -> Next<S> {
        fx.push(Effect::Persist(Some(Record { previous: previous.clone(), phase: Phase::RolledBack })));
        fx.push(Effect::StopTimer);
        fx.push(Effect::Log(format!("rolled back to {previous}")));
        fx.push(Effect::Say(format!("{P}: rolled back to {previous}")));
        Next::Exit(match then {
            Then::Report => 0,
            Then::Fail => 1,
        })
    }
}
