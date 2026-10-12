//! nixconvoy's switch of one target (its module docstring, step 7) as a pure
//! machine: nixconvoy performs each query - the on-target guard, ssh, the
//! clock - and feeds the result back, printing and dying as told.
//!
//! In a confirmed switch the helper activates under its own lock, so a timer
//! that fired first refuses the activation and one firing meanwhile waits
//! for it.  An activation that reports failures is still confirmed when the
//! target verifies (a plain switch leaves such a target on the new system too),
//! and the deploy then fails; one that exits 100 (an init incompatible with
//! the running one: nothing activated, a reboot needed) stages the new
//! system as a plain switch would, and fails.
#[cfg(test)]
mod model;
pub mod wire;

/// No verification round starts, nor pause ends, within this long of the
/// target's deadline: the confirm need only take the helper's lock before the
/// timer's rollback does.
pub const VERIFY_MARGIN_MS: u64 = 5_000;
/// What one probe may take (nixconvoy's PROBE_TIMEOUT), so a stalled ssh
/// session holds no verification past the deadline.
pub const PROBE_TIMEOUT_MS: u64 = 20_000;
/// Every probe ends this long before the target's deadline, whenever its
/// round began: the confirm's ssh round trip and systemd-run start fit in
/// it.  Each probe's timeout is cut to that hard stop, so the probes of the
/// last round (running system, check, the router's internet) cannot add up
/// past the timer.
pub const CONFIRM_RESERVE_MS: u64 = 2_000;
pub const INTERNET_TIMEOUT_MS: u64 = 8_000;
pub const VERIFY_PAUSE_MS: u64 = 3_000;
/// The rollback exit code that means the confirm landed after all.
pub const ALREADY_CONFIRMED: i32 = crate::switch::core::ALREADY_CONFIRMED;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Params {
    pub target: String,
    pub toplevel: String,
    /// What the target ran when the deploy probed it.
    pub expected: String,
    /// "local", or the ssh address.
    pub place: String,
    pub quiet: bool,
    /// The router: verification also needs this machine on the internet.
    pub router: bool,
    /// DEPLOY_FAIL_VERIFY=1: fail verification at once.
    pub fail_verify: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Guard {
    Locked,
    /// The target runs something else than what was probed.
    Moved(String),
    Failed(String),
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Query {
    /// Take the on-target switch guard (compare-and-swap on the running
    /// system) -> Guard.
    Guard,
    /// Whether the toplevel carries /confirmed-switch -> Code (0/1).
    ProbeHelper,
    /// nix-env --set of the toplevel -> Code.
    SetProfile,
    /// switch-to-configuration <action>, detached -> Code.
    Activate(&'static str),
    /// The helper's `deadline` -> Text.
    Deadline,
    /// The helper's `arm` over the session -> Said (Code when its output
    /// is lost).
    Arm,
    /// The helper with these arguments, detached -> Code, or Said for a
    /// rollback.
    Helper(Vec<String>),
    /// Monotonic milliseconds -> Now.
    Now,
    /// /run/current-system -> Running.
    Running {
        timeout_ms: Option<u64>,
    },
    /// The helper's `check` -> Check.
    Check {
        timeout_ms: u64,
    },
    /// Whether this machine reaches the internet -> Flag.
    Internet {
        timeout_ms: u64,
    },
    Sleep {
        ms: u64,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Reply {
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
    Running(Option<String>),
    /// `code` < 0: timed out.  `last`: its last stderr line.
    Check {
        code: i32,
        last: Option<String>,
    },
    Flag(bool),
    Done,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Effect {
    Print(String),
    /// The target is armed: a die() elsewhere leaves it to its timer.
    Armed,
    Disarmed,
    /// nixconvoy's PERF phase.
    Mark(String),
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Next {
    Ask(Query),
    Done,
    Die(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
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
    Clocking { deadline_ms: u64 },
    Arming { w: Window },
    Activating { w: Window },
    Staging,
    VerifyNow { w: Window, rc: i32 },
    VerifyRunning { w: Window, rc: i32, tmo: u64 },
    CheckNow { w: Window, rc: i32 },
    VerifyCheck { w: Window, rc: i32, tmo: u64 },
    InternetNow { w: Window, rc: i32, problems: Vec<String> },
    VerifyInternet { w: Window, rc: i32, problems: Vec<String>, tmo: u64 },
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
    /// deadline cut.
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
        format!(
            "the build succeeded but the switch of {} failed; nothing was committed or pushed - the changes are still staged.  Fix the problem and re-run the deploy from this checkout.",
            self.p.target
        )
    }

    fn helper(&self, args: &[&str]) -> Query {
        Query::Helper(args.iter().map(|a| a.to_string()).collect())
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
            (Pc::Start, Reply::Start) => self.ask(Pc::Guarding, Query::Guard),
            (Pc::Guarding, Reply::Guard(g)) => match g {
                Guard::Locked => self.ask(Pc::Probing, Query::ProbeHelper),
                Guard::Moved(cur) => Next::Die(format!(
                    "{target} was switched by another deploy while this one ran (now running {cur}).  Nothing was committed; re-run to converge with it."
                )),
                Guard::Failed(line) => Next::Die(format!(
                    "could not take {target}'s switch lock ({}); nothing was committed",
                    if line.is_empty() { "connection failed" } else { &line }
                )),
            },
            (Pc::Probing, Reply::Code(rc)) => {
                if rc != 0 && rc != 1 {
                    return Next::Die(format!(
                        "could not probe {target} for its confirmed-switch helper (exit {rc}); nothing was switched or committed"
                    ));
                }
                let confirmed = rc == 0;
                fx.push(Effect::Print(format!(
                    "switching {target} ({}){}...",
                    self.p.place,
                    if confirmed { ", confirmed" } else { "" }
                )));
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
                self.ask(Pc::Switching, Query::Activate("switch"))
            }
            (Pc::Switching, Reply::Code(rc)) => {
                if rc != 0 {
                    return Next::Die(self.failed());
                }
                Next::Done
            }
            (Pc::ReadingDeadline, Reply::Text(t)) => match t.trim().parse::<u64>() {
                Ok(secs) => self.ask(Pc::Clocking { deadline_ms: secs * 1000 }, Query::Now),
                Err(_) => Next::Die(format!("{target}'s confirmed-switch helper gave no deadline ({t:?})")),
            },
            // Taken before arming, so the target's timer, started later, is
            // never due before this side's reckoning of it.
            (Pc::Clocking { deadline_ms }, Reply::Now(now)) => {
                let end = now + deadline_ms;
                let w = Window { until: end.saturating_sub(VERIFY_MARGIN_MS), hard: end.saturating_sub(CONFIRM_RESERVE_MS) };
                self.ask(Pc::Arming { w }, Query::Arm)
            }
            (pc @ (Pc::Arming { .. } | Pc::RollingBack { .. } | Pc::ConfirmRollingBack { .. }), Reply::Said { code, to }) => {
                if let Some(t) = to.filter(|_| code == 0) {
                    self.rollback_to = t;
                }
                self.transition(pc, Reply::Code(code), fx)
            }
            (Pc::Arming { w }, Reply::Code(rc)) => {
                if rc != 0 {
                    return Next::Die(format!(
                        "could not arm {target}'s rollback timer; nothing was switched or committed"
                    ));
                }
                fx.push(Effect::Armed);
                let q = self.helper(&["activate", &self.p.toplevel]);
                self.ask(Pc::Activating { w }, q)
            }
            (Pc::Activating { w }, Reply::Code(rc)) => {
                if !self.p.quiet {
                    fx.push(Effect::Mark(format!("verify:{target}")));
                }
                if rc == 100 {
                    fx.push(Effect::Print(format!(
                        "{target}'s new system takes effect only at a reboot; staging it"
                    )));
                    let q = self.helper(&["confirm", &self.p.toplevel, "reboot-pending"]);
                    return self.ask(Pc::Staging, q);
                }
                fx.push(Effect::Print(format!("verifying {target}...")));
                if self.p.fail_verify {
                    return self.verify_failed("DEPLOY_FAIL_VERIFY=1".to_string(), fx);
                }
                self.ask(Pc::VerifyNow { w, rc }, Query::Now)
            }
            (Pc::Staging, Reply::Code(c)) => {
                fx.push(Effect::Disarmed);
                Next::Die(format!(
                    "the new system of {target} has an init incompatible with the running one, so it takes effect only at a reboot, which a confirmed switch never does.  {}Nothing was committed or pushed.",
                    if c == 0 {
                        format!("Its profile and bootloader entry are set: reboot {target}, then re-run.  ")
                    } else {
                        "Staging it failed (see above).  ".to_string()
                    }
                ))
            }

            // One round of verification: the target runs the new system and
            // passes its check (internet + homelab.confirmedSwitch.checks),
            // and for the router this machine reaches the internet too.
            (Pc::VerifyNow { w, rc }, Reply::Now(now)) => {
                self.cut = false;
                match self.budget(w, now) {
                    None => self.out_of_time_end(w, rc, Vec::new()),
                    Some(tmo) => self.ask(Pc::VerifyRunning { w, rc, tmo }, Query::Running { timeout_ms: Some(tmo) }),
                }
            }
            (Pc::VerifyRunning { w, rc, tmo }, Reply::Running(cur)) => {
                if cur.is_none() && tmo < PROBE_TIMEOUT_MS {
                    self.cut = true;
                }
                if cur.as_deref() != Some(self.p.toplevel.as_str()) {
                    let problem = format!(
                        "{target} runs {}, not the new system",
                        cur.as_deref().unwrap_or("nothing reachable")
                    );
                    return self.verify_internet(w, rc, vec![problem]);
                }
                self.ask(Pc::CheckNow { w, rc }, Query::Now)
            }
            (Pc::CheckNow { w, rc }, Reply::Now(now)) => match self.budget(w, now) {
                None => self.out_of_time_end(w, rc, Vec::new()),
                Some(tmo) => self.ask(Pc::VerifyCheck { w, rc, tmo }, Query::Check { timeout_ms: tmo }),
            },
            (Pc::VerifyCheck { w, rc, tmo }, Reply::Check { code, last }) => {
                let mut problems = Vec::new();
                if code < 0 {
                    self.cut |= tmo < PROBE_TIMEOUT_MS;
                    problems.push(format!("{target}: check timed out after {:.0}s", tmo as f64 / 1000.0));
                } else if code != 0 {
                    problems.push(format!("{target}: {}", last.as_deref().unwrap_or("check failed")));
                }
                self.verify_internet(w, rc, problems)
            }
            (Pc::InternetNow { w, rc, problems }, Reply::Now(now)) => match self.budget(w, now) {
                None => self.out_of_time_end(w, rc, problems),
                Some(tmo) => {
                    let tmo = INTERNET_TIMEOUT_MS.min(tmo);
                    self.ask(Pc::VerifyInternet { w, rc, problems, tmo }, Query::Internet { timeout_ms: tmo })
                }
            },
            (Pc::VerifyInternet { w, rc, mut problems, tmo }, Reply::Flag(ok)) => {
                if !ok {
                    self.cut |= tmo < INTERNET_TIMEOUT_MS;
                    problems.push("this machine cannot reach the internet".to_string());
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
            (Pc::VerifyPause { w, rc }, Reply::Done) => self.ask(Pc::VerifyNow { w, rc }, Query::Now),

            (Pc::RollingBack { problem }, Reply::Code(c)) => {
                fx.push(Effect::Disarmed);
                if c != 0 {
                    let pc = Pc::Reporting { fallout: Fallout::VerifyFailed, problem };
                    return self.ask(pc, Query::Running { timeout_ms: None });
                }
                Next::Die(format!(
                    "the switch of {target} did not verify ({problem}); rolled back to {}.  Nothing was committed or pushed - the changes are still staged.",
                    self.rollback_to
                ))
            }
            (Pc::Confirming { rc }, Reply::Code(c)) => {
                if c == 0 {
                    fx.push(Effect::Disarmed);
                    return if rc != 0 { Next::Die(self.failed()) } else { Next::Done };
                }
                // The helper rolled back itself if persisting failed; after
                // a timer that fired first, this undoes the activation it
                // may have missed.  Exit 3: the confirm did land, only its
                // relay died.
                fx.push(Effect::Print(format!("{target} could not be confirmed; rolling back")));
                let q = self.helper(&["rollback"]);
                self.ask(Pc::ConfirmRollingBack { rc }, q)
            }
            (Pc::ConfirmRollingBack { rc }, Reply::Code(r)) => {
                fx.push(Effect::Disarmed);
                if r == ALREADY_CONFIRMED {
                    return if rc != 0 { Next::Die(self.failed()) } else { Next::Done };
                }
                if r == 0 {
                    return Next::Die(format!(
                        "{target} verified but could not be confirmed (see above: its deadline passed first, or persisting the new system failed); rolled back to {}.  Nothing was committed or pushed.",
                        self.rollback_to
                    ));
                }
                let pc = Pc::Reporting { fallout: Fallout::ConfirmFailed, problem: String::new() };
                self.ask(pc, Query::Running { timeout_ms: None })
            }
            (Pc::Reporting { fallout, problem }, Reply::Running(cur)) => {
                let cur = cur.unwrap_or_else(|| "nothing reachable".to_string());
                Next::Die(match fallout {
                    Fallout::VerifyFailed => format!(
                        "the switch of {target} did not verify ({problem}) and rolling it back failed too (see above): it runs {cur}; its timer retries if still pending.  Nothing was committed or pushed."
                    ),
                    Fallout::ConfirmFailed => format!(
                        "{target} verified but could not be confirmed (see above: its deadline passed first, or persisting the new system failed); rolling it back failed too, it runs {cur}.  Nothing was committed or pushed."
                    ),
                })
            }
            (pc, reply) => panic!("deploy-switch: reply {reply:?} does not answer {pc:?}"),
        }
    }

    /// The timeout of a probe starting at `now`: None once the hard stop
    /// has come.
    fn budget(&self, w: Window, now: u64) -> Option<u64> {
        let left = w.hard.saturating_sub(now);
        (left > 0).then(|| PROBE_TIMEOUT_MS.min(left))
    }

    fn out_of_time(&self) -> String {
        format!("{}: no time left to verify before its rollback deadline", self.p.target)
    }

    fn verify_internet(&mut self, w: Window, rc: i32, problems: Vec<String>) -> Next {
        if self.p.router {
            return self.ask(Pc::InternetNow { w, rc, problems }, Query::Now);
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
        self.ask(Pc::VerifyEnd { w, rc, problems }, Query::Now)
    }

    fn verify_failed(&mut self, problem: String, fx: &mut Vec<Effect>) -> Next {
        fx.push(Effect::Print(format!("{} failed verification ({problem}); rolling back", self.p.target)));
        let q = self.helper(&["rollback"]);
        self.ask(Pc::RollingBack { problem }, q)
    }
}
