//! A counted boot's judgement.  The gate polls the checks until they pass
//! or the deadline (seconds since stage 2 began: an initrd that waited for a
//! manual LUKS unlock must not eat into it) passes; the watch reboots on the
//! gate's verdict, its own deadline (`grace` later) only covering a gate
//! that never ran, and stands down once systemd-bless-boot has removed the
//! counter - the target being reached is not enough, as a failed rename
//! would leave the tries to be spent.

pub const POLL_SECS: u64 = 5;

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

pub fn gate(failures: &[String], elapsed: u64, deadline: u64) -> Gate {
    if failures.is_empty() {
        return Gate::Pass;
    }
    let status = failures.join(", ");
    if elapsed >= deadline {
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
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Watch {
    Blessed,
    /// Reachable, but the rename failed: the tries keep being spent.
    BlessFailed,
    Reboot,
    Wait,
}

pub fn watch(bless: Bless, gate_failed: bool, elapsed: u64, limit: u64) -> Watch {
    match bless {
        Bless::Active => Watch::Blessed,
        Bless::Failed => Watch::BlessFailed,
        Bless::Pending if gate_failed || elapsed >= limit => Watch::Reboot,
        Bless::Pending => Watch::Wait,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The gate and the watch as one boot: with checks that never pass the
    /// box reboots by deadline + grace, and with checks that pass it never
    /// reboots.
    #[test]
    fn a_boot_ends_blessed_or_rebooted() {
        for pass_at in [None, Some(0), Some(100), Some(295), Some(300)] {
            let (deadline, grace) = (300, 60);
            let (mut gate_failed, mut gate_done, mut blessed) = (false, false, false);
            let mut t = 0;
            let end = loop {
                if !gate_done {
                    let fails = if pass_at.is_some_and(|p| t >= p) { vec![] } else { vec!["x".to_string()] };
                    match gate(&fails, t, deadline) {
                        Gate::Pass => {
                            gate_done = true;
                            blessed = true;
                        }
                        Gate::Fail { .. } => {
                            gate_done = true;
                            gate_failed = true;
                        }
                        Gate::Retry { .. } => {}
                    }
                }
                let b = if blessed { Bless::Active } else { Bless::Pending };
                match watch(b, gate_failed, t, deadline + grace) {
                    Watch::Wait => t += POLL_SECS,
                    v => break (v, t),
                }
            };
            match pass_at {
                Some(p) if p <= deadline => assert_eq!(end.0, Watch::Blessed),
                _ => {
                    assert_eq!(end.0, Watch::Reboot);
                    assert!(end.1 <= deadline + POLL_SECS);
                }
            }
        }
    }
}
