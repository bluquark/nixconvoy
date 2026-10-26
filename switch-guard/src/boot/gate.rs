//! A counted boot's judgement.  The gate polls the checks until they pass,
//! or fails them as soon as the boot has nothing left starting: every job
//! is bounded by its unit's own start timeout, so checks still failing then
//! will not pass.  The deadline (seconds since stage 2 began: an initrd that
//! waited for a manual LUKS unlock must not eat into it) is only the
//! backstop for a unit with an infinite start timeout.  The watch reboots on
//! the gate unit's failure, or when the gate will never run (its job gone,
//! or still queued at the deadline), and stands down once
//! systemd-bless-boot has removed the counter - the target being reached is
//! not enough, as a failed rename would leave the tries to be spent.

pub const POLL_SECS: u64 = 5;

/// How long the boot must have had nothing starting before the gate trusts
/// a failure: a check can trail the unit it observes (an address networkd
/// raises after the daemon is up, a bind-dynamic socket), and a job made
/// runnable waits on systemd's run queue before it shows as running.  A
/// passing boot never waits it out.
pub const SETTLE_SECS: u64 = 30;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Gate {
    Pass,
    /// Record `status` and poll again.
    Retry {
        status: String,
    },
    Fail {
        status: String,
    },
}

pub fn gate(failures: &[String], jobs_pending: bool, elapsed: u64, deadline: u64) -> Gate {
    if failures.is_empty() {
        return Gate::Pass;
    }
    let status = failures.join(", ");
    if !jobs_pending || elapsed >= deadline {
        Gate::Fail { status }
    } else {
        Gate::Retry { status }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Bless {
    Active,
    Failed,
    Pending,
    /// Inactive with no job: boot-complete.target failed, or its job was
    /// dropped.
    Gone,
}

/// boot-assessment.service as systemd reports it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GateUnit {
    /// Its start job is queued behind its ordering dependencies.
    Queued,
    /// Polling, bounded by its own deadline and start timeout.
    Running,
    Passed,
    Failed,
    /// Inactive with no job: it will never run.
    Gone,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Watch {
    Blessed,
    /// Reachable, but the counter stays: the tries keep being spent.
    BlessFailed,
    Reboot,
    Wait,
}

pub fn watch(bless: Bless, gate: GateUnit, elapsed: u64, deadline: u64) -> Watch {
    match (bless, gate) {
        (Bless::Active, _) => Watch::Blessed,
        (Bless::Failed, _) => Watch::BlessFailed,
        (_, GateUnit::Failed | GateUnit::Gone) => Watch::Reboot,
        (Bless::Gone, GateUnit::Passed) => Watch::BlessFailed,
        // A hung systemd-bless-boot (or a unit before boot-complete.target
        // with no start timeout) behind a passed gate: the generation is
        // sound, so noticed rather than rebooted out of.
        (Bless::Pending, GateUnit::Passed) if elapsed >= deadline => Watch::BlessFailed,
        (_, GateUnit::Queued) if elapsed >= deadline => Watch::Reboot,
        _ => Watch::Wait,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fails() -> Vec<String> {
        vec!["x".to_string()]
    }

    #[test]
    fn gate_decides_on_events() {
        assert_eq!(gate(&[], true, 0, 900), Gate::Pass);
        assert_eq!(gate(&[], false, 1000, 900), Gate::Pass);
        assert_eq!(gate(&fails(), true, 30, 900), Gate::Retry { status: "x".to_string() });
        assert_eq!(gate(&fails(), false, 30, 900), Gate::Fail { status: "x".to_string() });
        assert_eq!(gate(&fails(), true, 900, 900), Gate::Fail { status: "x".to_string() });
        let two = vec!["a".to_string(), "b".to_string()];
        assert_eq!(gate(&two, false, 30, 900), Gate::Fail { status: "a, b".to_string() });
    }

    #[test]
    fn watch_follows_the_gate_unit() {
        use GateUnit::*;
        for g in [Queued, Running, Passed, Failed, Gone] {
            assert_eq!(watch(Bless::Active, g, 0, 900), Watch::Blessed);
            assert_eq!(watch(Bless::Failed, g, 0, 900), Watch::BlessFailed);
        }
        assert_eq!(watch(Bless::Pending, Failed, 0, 900), Watch::Reboot);
        assert_eq!(watch(Bless::Pending, Gone, 0, 900), Watch::Reboot);
        assert_eq!(watch(Bless::Pending, Queued, 899, 900), Watch::Wait);
        assert_eq!(watch(Bless::Pending, Queued, 900, 900), Watch::Reboot);
        // A running gate brings its own verdict, a passed one the blessing.
        assert_eq!(watch(Bless::Pending, Running, 5000, 900), Watch::Wait);
        assert_eq!(watch(Bless::Pending, Passed, 899, 900), Watch::Wait);
        assert_eq!(watch(Bless::Pending, Passed, 900, 900), Watch::BlessFailed);
        // A failed gate fails boot-complete.target, and so the blessing.
        assert_eq!(watch(Bless::Gone, Failed, 0, 900), Watch::Reboot);
        assert_eq!(watch(Bless::Gone, Passed, 0, 900), Watch::BlessFailed);
        assert_eq!(watch(Bless::Gone, Running, 0, 900), Watch::Wait);
    }

    /// The gate and the watch as one boot whose jobs finish at `settle`
    /// (None: one never does): checks that pass by then are blessed, checks
    /// that do not are rebooted out of within a poll of the settling, or of
    /// the deadline when the boot never settles.
    #[test]
    fn a_boot_ends_blessed_or_rebooted() {
        let deadline = 900;
        for settle in [Some(0), Some(40), Some(300), None] {
            for pass_at in [None, Some(0), Some(30), Some(295), Some(890)] {
                let mut unit = GateUnit::Running;
                let mut t = 0;
                let end = loop {
                    if unit == GateUnit::Running {
                        let f = if pass_at.is_some_and(|p| t >= p) { vec![] } else { fails() };
                        let pending = settle.is_none_or(|s| t < s);
                        match gate(&f, pending, t, deadline) {
                            Gate::Pass => unit = GateUnit::Passed,
                            Gate::Fail { .. } => unit = GateUnit::Failed,
                            Gate::Retry { .. } => {}
                        }
                    }
                    let b = if unit == GateUnit::Passed { Bless::Active } else { Bless::Pending };
                    match watch(b, unit, t, deadline) {
                        Watch::Wait => t += POLL_SECS,
                        v => break (v, t),
                    }
                };
                let decided = settle.unwrap_or(deadline);
                match pass_at {
                    Some(p) if p <= decided => assert_eq!(end.0, Watch::Blessed, "{settle:?} {pass_at:?}"),
                    _ => {
                        assert_eq!(end.0, Watch::Reboot, "{settle:?} {pass_at:?}");
                        assert!(end.1 <= decided + POLL_SECS, "{settle:?} {pass_at:?} {}", end.1);
                    }
                }
            }
        }
    }
}
