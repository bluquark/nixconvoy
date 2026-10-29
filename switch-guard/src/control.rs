//! nixconvoy's switch of one target (its module docstring, step 7) as a pure
//! machine: nixconvoy performs each query - the on-target guard, ssh, the
//! clock - and feeds the result back, printing and dying as told.
//!
//! In a confirmed switch the helper activates under its own lock, so a timer
//! that fired first refuses the activation and one firing meanwhile waits
//! for it.  An activation that reports failures is still confirmed when the
//! target verifies (a plain switch leaves such a target on the new system too),
//! and the deploy then fails; one whose ssh session was lost (its unit on
//! the target runs on) is decided by the verification and confirm alone;
//! one that exits 100 (an init incompatible with the running one: nothing
//! activated, a reboot needed) stages the new system as a plain switch
//! would, and fails.
#[cfg(test)]
mod model;
pub mod wire;

use crate::text;
use serde::{Deserialize, Serialize};

/// No verification round starts, nor pause ends, within this long of the
/// target's deadline (counted from the activation's end): a round (1.8 s
/// at the median) started later would be cut by the hard stop.
pub const VERIFY_MARGIN_MS: u64 = 3_000;
/// What one probe may take (nixconvoy's PROBE_TIMEOUT), so a stalled ssh
/// session holds no verification past the deadline.
pub const PROBE_TIMEOUT_MS: u64 = 20_000;
/// Every probe ends this long before the target's deadline, whenever its
/// round began: the confirm's ssh round trip and systemd-run start (90 ms
/// warm, 600 ms cold, measured) fit in it, as does the activate call's
/// return trip, by which the target's restarted timer leads the window
/// (Pc::Clocking).  Each probe's timeout is cut to that hard stop, so the
/// probes of the last round (running system, the target's check, this
/// machine's) cannot add up past the timer.
pub const CONFIRM_RESERVE_MS: u64 = 2_000;
pub const VERIFY_PAUSE_MS: u64 = 1_000;
/// The rollback exit code that means the confirm landed after all.
pub const ALREADY_CONFIRMED: i32 = crate::switch::core::ALREADY_CONFIRMED;
/// ssh's own exit code: the session died (the keepalives cut a stalled
/// connection), telling nothing of the activation's outcome.
pub const SESSION_LOST: i32 = 255;

#[derive(Deserialize, Clone, Debug, PartialEq, Eq, Hash)]
pub struct Params {
    pub target: String,
    pub toplevel: String,
    /// What the target ran when the deploy probed it.
    pub expected: String,
    /// The ssh address.
    pub place: String,
    pub quiet: bool,
    /// Verification also runs the policy's controller_shell checks on
    /// this machine (Query::Controller): the router, the deploy's own way
    /// out, is proven by one reaching the internet through it.
    pub controller_check: bool,
    /// NIXCONVOY_FAIL_VERIFY=1: fail verification at once.
    pub fail_verify: bool,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Guard {
    Locked,
    /// The target runs something else than what was probed.
    Moved(String),
    Failed(String),
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Query {
    /// Take the on-target switch guard (compare-and-swap on the running
    /// system) -> Guard.
    TakeGuard,
    /// Root the target's switch kit for the toplevel, when it has one -> Code
    /// (0: confirmed, 1: none, switched plainly; else a failure).
    ProbeHelper,
    /// nix-env --set of the toplevel -> Code.
    SetProfile,
    /// switch-to-configuration switch, detached -> Code.
    Activate,
    /// The helper's `deadline` -> Text.
    Deadline,
    /// The helper's `arm` over the session -> Said (Code when its output
    /// is lost).
    Arm,
    /// The helper with these arguments, detached -> Code, or Said for a
    /// rollback.
    Helper {
        args: Vec<String>,
    },
    /// Monotonic milliseconds -> Now.
    Clock,
    /// /run/current-system -> System: a verification round's read (a
    /// timeout) over a new ssh connection, the proof that sshd admits one;
    /// the report's after a failed rollback (None) over the deploy's.
    Running {
        timeout_ms: Option<u64>,
    },
    /// The helper's `check` -> Checked.
    Check {
        timeout_ms: u64,
    },
    /// This machine's controller_shell checks -> Checked.
    Controller {
        timeout_ms: u64,
    },
    Sleep {
        ms: u64,
    },
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Reply {
    /// Never on the wire: the machine's first step answers it.
    #[serde(skip)]
    Start,
    Guard(Guard),
    Code(i32),
    /// An arm's or rollback's exit code and the system the helper said
    /// its rollback goes or went to.
    Said {
        code: i32,
        to: Option<String>,
    },
    Text(String),
    Now(u64),
    /// The running system, or None with whether the read timed out
    /// (a refusal is a failure of record, a timeout on a cut budget not).
    System {
        system: Option<String>,
        timed_out: bool,
    },
    /// `code` < 0: timed out.  `last`: its last stderr line (the target's
    /// check), or the report of every failing statement (this machine's).
    Checked {
        code: i32,
        last: Option<String>,
    },
    Done,
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Effect {
    Print(String),
    /// The target is armed: a die() elsewhere leaves it to its timer.
    Armed,
    Disarmed,
    /// nixconvoy's PERF phase.
    Mark(String),
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Next {
    Ask(Query),
    Done,
    Die(String),
}

#[derive(Serialize, Clone, Debug, PartialEq, Eq)]
pub struct Step {
    pub effects: Vec<Effect>,
    pub next: Next,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Fallout {
    VerifyFailed,
    ConfirmFailed,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Pc {
    Start,
    Guarding,
    Probing,
    SettingProfile,
    Switching,
    ReadingDeadline,
    Arming { deadline_ms: u64 },
    Activating { deadline_ms: u64 },
    Clocking { deadline_ms: u64, rc: i32 },
    Staging,
    VerifyNow { w: Window, rc: i32 },
    VerifyRunning { w: Window, rc: i32, tmo: u64 },
    CheckNow { w: Window, rc: i32 },
    VerifyCheck { w: Window, rc: i32, tmo: u64 },
    ControllerNow { w: Window, rc: i32, problems: Vec<String> },
    VerifyController { w: Window, rc: i32, problems: Vec<String>, tmo: u64 },
    VerifyEnd { w: Window, rc: i32, problems: Vec<String> },
    VerifyPause { w: Window, rc: i32 },
    RollingBack { problem: String },
    Confirming { rc: i32 },
    ConfirmRollingBack { rc: i32 },
    Reporting { fallout: Fallout, problem: String },
    Finished,
}

/// Monotonic milliseconds: no verification round starts at or after
/// `until`, no probe runs past `hard`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct Window {
    until: u64,
    hard: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Machine {
    p: Params,
    pc: Pc,
    /// Where a rollback takes the target: the probed system, unless the
    /// helper said otherwise (an arm taking over a dead deploy's pending
    /// rollback, a rollback finding another arm's record).
    rollback_to: String,
    /// The last round's problems that the deadline did not cut short.
    last: Vec<String>,
    /// This round ran out of time, or a probe timed out on a budget the
    /// deadline cut (a refusal within one is a failure of record).
    cut: bool,
}

impl Machine {
    pub fn new(p: Params) -> Self {
        let rollback_to = p.expected.clone();
        Machine { p, pc: Pc::Start, rollback_to, last: Vec::new(), cut: false }
    }

    pub fn params(&self) -> &Params {
        &self.p
    }

    fn failed(&self) -> String {
        format!(text!(control_switch_failed), target = self.p.target)
    }

    /// The verified, confirmed target: the activation's own report decides
    /// the run, unless its session was lost.
    fn confirmed(&self, rc: i32, fx: &mut Vec<Effect>) -> Next {
        if rc == SESSION_LOST {
            fx.push(Effect::Print(format!(text!(control_session_lost), target = self.p.target)));
            return Next::Done;
        }
        if rc != 0 {
            Next::Die(self.failed())
        } else {
            Next::Done
        }
    }

    fn helper(&self, args: &[&str]) -> Query {
        Query::Helper { args: args.iter().map(|a| a.to_string()).collect() }
    }

    pub fn decide(&mut self, reply: Reply) -> Step {
        let mut fx = Vec::new();
        let pc = std::mem::replace(&mut self.pc, Pc::Finished);
        let next = self.transition(pc, reply, &mut fx);
        Step { effects: fx, next }
    }

    fn ask(&mut self, pc: Pc, q: Query) -> Next {
        self.pc = pc;
        Next::Ask(q)
    }

    fn transition(&mut self, pc: Pc, reply: Reply, fx: &mut Vec<Effect>) -> Next {
        let target = self.p.target.clone();
        match (pc, reply) {
            (Pc::Start, Reply::Start) => self.ask(Pc::Guarding, Query::TakeGuard),
            (Pc::Guarding, Reply::Guard(g)) => match g {
                Guard::Locked => self.ask(Pc::Probing, Query::ProbeHelper),
                Guard::Moved(cur) => Next::Die(format!(text!(control_moved), target = target, cur = cur)),
                Guard::Failed(line) => Next::Die(format!(
                    text!(control_guard_failed),
                    target = target,
                    reason = if line.is_empty() { text!(control_connection_failed) } else { &line }
                )),
            },
            (Pc::Probing, Reply::Code(rc)) => {
                if rc != 0 && rc != 1 {
                    return Next::Die(format!(text!(control_probe_failed), target = target, rc = rc));
                }
                let confirmed = rc == 0;
                fx.push(Effect::Print(if confirmed {
                    format!(text!(control_switching_confirmed), target = target, place = self.p.place)
                } else {
                    format!(text!(control_switching), target = target, place = self.p.place)
                }));
                if confirmed {
                    self.ask(Pc::ReadingDeadline, Query::Deadline)
                } else {
                    self.ask(Pc::SettingProfile, Query::SetProfile)
                }
            }
            (Pc::SettingProfile, Reply::Code(rc)) => {
                if rc != 0 {
                    return Next::Die(self.failed());
                }
                self.ask(Pc::Switching, Query::Activate)
            }
            (Pc::Switching, Reply::Code(rc)) => {
                if rc != 0 {
                    return Next::Die(self.failed());
                }
                Next::Done
            }
            (Pc::ReadingDeadline, Reply::Text(t)) => match t.trim().parse::<u64>() {
                Ok(secs) => self.ask(Pc::Arming { deadline_ms: secs * 1000 }, Query::Arm),
                Err(_) => Next::Die(format!(text!(control_no_deadline), target = target, text = t)),
            },
            (pc @ (Pc::Arming { .. } | Pc::RollingBack { .. } | Pc::ConfirmRollingBack { .. }), Reply::Said { code, to }) => {
                if let Some(t) = to.filter(|_| code == 0) {
                    self.rollback_to = t;
                }
                self.transition(pc, Reply::Code(code), fx)
            }
            (Pc::Arming { deadline_ms }, Reply::Code(rc)) => {
                if rc != 0 {
                    return Next::Die(format!(text!(control_arm_failed), target = target));
                }
                fx.push(Effect::Armed);
                let q = self.helper(&["activate", &self.p.toplevel]);
                self.ask(Pc::Activating { deadline_ms }, q)
            }
            (Pc::Activating { deadline_ms }, Reply::Code(rc)) => {
                if !self.p.quiet {
                    fx.push(Effect::Mark(format!("verify:{target}")));
                }
                if rc == 100 {
                    fx.push(Effect::Print(format!(text!(control_staging), target = target)));
                    let q = self.helper(&["confirm", &self.p.toplevel, "reboot-pending"]);
                    return self.ask(Pc::Staging, q);
                }
                fx.push(Effect::Print(format!(text!(control_verifying), target = target)));
                if self.p.fail_verify {
                    return self.verify_failed(text!(control_fail_verify).to_string(), fx);
                }
                self.ask(Pc::Clocking { deadline_ms, rc }, Query::Clock)
            }
            // The target's timer restarted at the activation's end, before
            // its exit code travelled here: its due time leads this
            // reckoning by that trip (the return leg of a 90 ms round
            // trip), which CONFIRM_RESERVE_MS covers twenty times over.
            (Pc::Clocking { deadline_ms, rc }, Reply::Now(now)) => {
                let end = now + deadline_ms;
                let w = Window { until: end.saturating_sub(VERIFY_MARGIN_MS), hard: end.saturating_sub(CONFIRM_RESERVE_MS) };
                self.round(w, rc, now)
            }
            (Pc::Staging, Reply::Code(c)) => {
                fx.push(Effect::Disarmed);
                Next::Die(if c == 0 {
                    format!(text!(control_staged), target = target)
                } else {
                    format!(text!(control_staging_failed), target = target)
                })
            }

            // One round of verification: a read of the running system (over
            // a new ssh connection) finds the new one, the
            // target's check passes (the internet probe when configured,
            // then the policy's target_shell checks) and, with
            // controller_check, this machine's controller_shell checks too.
            (Pc::VerifyNow { w, rc }, Reply::Now(now)) => self.round(w, rc, now),
            (Pc::VerifyRunning { w, rc, tmo }, Reply::System { system: cur, timed_out }) => {
                if cur.is_none() && timed_out && tmo < PROBE_TIMEOUT_MS {
                    self.cut = true;
                }
                if cur.as_deref() != Some(self.p.toplevel.as_str()) {
                    let problem = format!(
                        text!(control_runs_other),
                        target = target,
                        cur = cur.as_deref().unwrap_or(text!(control_nothing_reachable))
                    );
                    return self.verify_controller(w, rc, vec![problem]);
                }
                self.ask(Pc::CheckNow { w, rc }, Query::Clock)
            }
            (Pc::CheckNow { w, rc }, Reply::Now(now)) => match self.budget(w, now) {
                None => self.out_of_time_end(w, rc, Vec::new()),
                Some(tmo) => self.ask(Pc::VerifyCheck { w, rc, tmo }, Query::Check { timeout_ms: tmo }),
            },
            (Pc::VerifyCheck { w, rc, tmo }, Reply::Checked { code, last }) => {
                let mut problems = Vec::new();
                if code < 0 {
                    self.cut |= tmo < PROBE_TIMEOUT_MS;
                    problems.push(format!(text!(control_check_timed_out), target = target, secs = tmo as f64 / 1000.0));
                } else if code != 0 {
                    problems.push(format!(
                        text!(control_check_problem),
                        target = target,
                        last = last.as_deref().unwrap_or(text!(control_check_failed))
                    ));
                }
                self.verify_controller(w, rc, problems)
            }
            (Pc::ControllerNow { w, rc, problems }, Reply::Now(now)) => match self.budget(w, now) {
                None => self.out_of_time_end(w, rc, problems),
                Some(tmo) => self.ask(Pc::VerifyController { w, rc, problems, tmo }, Query::Controller { timeout_ms: tmo }),
            },
            // A timed-out statement is named in `last` with the others
            // failing; a run that itself timed out leaves none.
            (Pc::VerifyController { w, rc, mut problems, tmo }, Reply::Checked { code, last }) => {
                if code < 0 {
                    self.cut |= tmo < PROBE_TIMEOUT_MS;
                }
                if code < 0 && last.is_none() {
                    problems.push(format!(text!(control_controller_timed_out), target = target, secs = tmo as f64 / 1000.0));
                } else if code != 0 {
                    problems.push(format!(
                        text!(control_controller_problem),
                        target = target,
                        last = last.as_deref().unwrap_or(text!(control_check_failed))
                    ));
                }
                self.verify_end(w, rc, problems)
            }
            // A pause that would end inside the margin leaves no round
            // worth starting: the report is the last full round's.
            (Pc::VerifyEnd { w, rc, problems }, Reply::Now(now)) => {
                if now + VERIFY_PAUSE_MS >= w.until {
                    let report = if self.cut && !self.last.is_empty() { self.last.clone() } else { problems };
                    return self.verify_failed(report.join("; "), fx);
                }
                self.ask(Pc::VerifyPause { w, rc }, Query::Sleep { ms: VERIFY_PAUSE_MS })
            }
            (Pc::VerifyPause { w, rc }, Reply::Done) => self.ask(Pc::VerifyNow { w, rc }, Query::Clock),

            (Pc::RollingBack { problem }, Reply::Code(c)) => {
                fx.push(Effect::Disarmed);
                if c != 0 {
                    let pc = Pc::Reporting { fallout: Fallout::VerifyFailed, problem };
                    return self.ask(pc, Query::Running { timeout_ms: None });
                }
                Next::Die(format!(text!(control_did_not_verify), target = target, problem = problem, previous = self.rollback_to))
            }
            (Pc::Confirming { rc }, Reply::Code(c)) => {
                if c == 0 {
                    fx.push(Effect::Disarmed);
                    return self.confirmed(rc, fx);
                }
                // The helper rolled back itself if persisting failed; after
                // a timer that fired first, this undoes the activation it
                // may have missed.  Exit 3: the confirm did land, only its
                // relay died.
                fx.push(Effect::Print(format!(text!(control_unconfirmed), target = target)));
                let q = self.helper(&["rollback"]);
                self.ask(Pc::ConfirmRollingBack { rc }, q)
            }
            (Pc::ConfirmRollingBack { rc }, Reply::Code(r)) => {
                fx.push(Effect::Disarmed);
                if r == ALREADY_CONFIRMED {
                    return self.confirmed(rc, fx);
                }
                if r == 0 {
                    return Next::Die(format!(text!(control_confirm_rolled_back), target = target, previous = self.rollback_to));
                }
                let pc = Pc::Reporting { fallout: Fallout::ConfirmFailed, problem: String::new() };
                self.ask(pc, Query::Running { timeout_ms: None })
            }
            (Pc::Reporting { fallout, problem }, Reply::System { system: cur, .. }) => {
                let cur = cur.unwrap_or_else(|| text!(control_nothing_reachable).to_string());
                Next::Die(match fallout {
                    Fallout::VerifyFailed => format!(text!(control_rollback_failed), target = target, problem = problem, cur = cur),
                    Fallout::ConfirmFailed => format!(text!(control_confirm_rollback_failed), target = target, cur = cur),
                })
            }
            (pc, reply) => panic!("nixconvoy-switch: reply {reply:?} does not answer {pc:?}"),
        }
    }

    /// The timeout of a probe starting at `now`: None once the hard stop
    /// has come.
    fn budget(&self, w: Window, now: u64) -> Option<u64> {
        let left = w.hard.saturating_sub(now);
        (left > 0).then(|| PROBE_TIMEOUT_MS.min(left))
    }

    /// A verification round starting at `now`.
    fn round(&mut self, w: Window, rc: i32, now: u64) -> Next {
        self.cut = false;
        match self.budget(w, now) {
            None => self.out_of_time_end(w, rc, Vec::new()),
            Some(tmo) => self.ask(Pc::VerifyRunning { w, rc, tmo }, Query::Running { timeout_ms: Some(tmo) }),
        }
    }

    fn out_of_time(&self) -> String {
        format!(text!(control_out_of_time), target = self.p.target)
    }

    fn verify_controller(&mut self, w: Window, rc: i32, problems: Vec<String>) -> Next {
        if self.p.controller_check {
            return self.ask(Pc::ControllerNow { w, rc, problems }, Query::Clock);
        }
        self.verify_end(w, rc, problems)
    }

    fn out_of_time_end(&mut self, w: Window, rc: i32, mut problems: Vec<String>) -> Next {
        self.cut = true;
        problems.push(self.out_of_time());
        self.verify_end(w, rc, problems)
    }

    fn verify_end(&mut self, w: Window, rc: i32, problems: Vec<String>) -> Next {
        if problems.is_empty() {
            let q = self.helper(&["confirm", &self.p.toplevel]);
            return self.ask(Pc::Confirming { rc }, q);
        }
        if !self.cut {
            self.last = problems.clone();
        }
        self.ask(Pc::VerifyEnd { w, rc, problems }, Query::Clock)
    }

    fn verify_failed(&mut self, problem: String, fx: &mut Vec<Effect>) -> Next {
        fx.push(Effect::Print(format!(text!(control_verify_failed), target = self.p.target, problem = problem)));
        let q = self.helper(&["rollback"]);
        self.ask(Pc::RollingBack { problem }, q)
    }
}
