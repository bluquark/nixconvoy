//! Performs a Machine's effects: `System` is the I/O boundary (systemd,
//! nix-env, switch-to-configuration), and `drive` the loop that feeds
//! replies back.
use super::core::{Action, Effect, Machine, Next, Query, Reply, Verb, ACTIVATION_GUARD_SECS, P};
use crate::config::Config;
use crate::proc;
use crate::record::Record;
use crate::text;
use std::fs::{File, OpenOptions};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const UNIT: &str = "confirmed-switch-rollback";
pub const PROFILE: &str = "/nix/var/nix/profiles/system";
const SWITCH_LOCK: &str = "/run/nixos/switch-to-configuration.lock";
/// How long a lock no live process is seen holding is waited for before
/// rolling back anyway (switch-to-configuration then refuses if it is still held).
const UNSEEN_HOLDER_WAIT: Duration = Duration::from_secs(5);
const LOCK_POLL: Duration = Duration::from_millis(200);
/// A wait on a live holder is uncapped; this often, the journal says it goes on.
const HELD_REMINDER: Duration = Duration::from_secs(60);

/// Runs `m` to completion against `host`: its exit code.
pub fn drive(host: &mut System<'_>, mut m: Machine<String>) -> i32 {
    let mut reply = Reply::Start;
    loop {
        let step = m.decide(reply);
        for e in step.effects {
            match e {
                Effect::Say(s) => host.say(&s),
                Effect::Warn(s) => host.warn(&s),
                Effect::Log(s) => host.log(&s),
                Effect::StopTimer => host.stop_timer(),
                Effect::ResetFailedRollback => host.reset_failed(),
                Effect::Persist(r) => {
                    if let Err(e) = host.persist(&r) {
                        host.warn(&format!(text!(switch_cannot_record), P = P, e = e));
                        return 1;
                    }
                }
            }
        }
        reply = match step.next {
            Next::Exit(c) => return c,
            Next::Ask(q) => match q {
                Query::RollbackActive => Reply::Flag(host.rollback_active()),
                Query::TimerLive => Reply::Flag(host.timer_live()),
                Query::ProfileSystem => Reply::Running(host.profile_system()),
                Query::Lock => match host.lock() {
                    Ok(r) => Reply::Locked(r),
                    Err(e) => {
                        host.warn(&format!(text!(switch_unparsable_state), P = P, e = e, path = host.state_file().display()));
                        return 1;
                    }
                },
                Query::Running => Reply::Running(host.running()),
                Query::StartTimer(secs) => Reply::Flag(host.start_timer(secs)),
                Query::Generation => Reply::Generation(host.generation()),
                Query::SetProfile(s) => Reply::Flag(host.set_profile(&s)),
                Query::Switch(s, a) => Reply::Code(host.switch(&s, a)),
                Query::WaitSwitchLock => {
                    host.wait_switch_lock();
                    Reply::Done
                }
                Query::Sleep(n) => {
                    host.sleep(n);
                    Reply::Done
                }
                Query::SwitchGeneration(g) => Reply::Flag(host.switch_generation(g)),
                Query::DeleteGeneration(g) => Reply::Flag(host.delete_generation(g)),
            },
        };
    }
}

pub struct Settings {
    pub config: PathBuf,
    pub deadline: u64,
    pub internet_check: bool,
    pub checks: String,
    pub state_dir: PathBuf,
    pub systemctl: String,
    pub systemd_run: String,
    pub nix_env: String,
    pub logger: String,
    pub curl: String,
}

impl Settings {
    pub fn load(path: &Path) -> Result<Settings, String> {
        let c = Config::load(path)?;
        Ok(Settings {
            config: path.to_path_buf(),
            deadline: c.num("deadline")?,
            internet_check: c.str("internet_check")? == "true",
            checks: c.str("checks")?.to_string(),
            state_dir: PathBuf::from(c.str("state_dir")?),
            systemctl: c.str("systemctl")?.to_string(),
            systemd_run: c.str("systemd_run")?.to_string(),
            nix_env: c.str("nix_env")?.to_string(),
            logger: c.str("logger")?.to_string(),
            curl: c.str("curl")?.to_string(),
        })
    }
}

pub struct System<'a> {
    pub s: &'a Settings,
    held: Option<File>,
}

impl<'a> System<'a> {
    pub fn new(s: &'a Settings) -> Self {
        System { s, held: None }
    }
}

/// The pid /proc/locks names as holding a flock on the file `(major, minor,
/// inode)`; it may have exited, the lock living on in a child that inherited
/// the descriptor (never switch-to-configuration's own, opened close-on-exec).
fn flock_holder(locks: &str, id: (u32, u32, u64)) -> Option<i32> {
    locks.lines().find_map(|l| {
        let [_, "FLOCK", _, _, pid, file, ..] = l.split_whitespace().collect::<Vec<_>>()[..] else { return None };
        let mut f = file.split(':');
        let file = (u32::from_str_radix(f.next()?, 16).ok()?, u32::from_str_radix(f.next()?, 16).ok()?, f.next()?.parse().ok()?);
        pid.parse().ok().filter(|p| file == id && *p > 0)
    })
}

/// Whether `pid` has the file `id` open: /proc/locks keeps naming a holder
/// that exited while a child keeps the descriptor, and its pid may be reused.
fn has_open(pid: i32, id: (u32, u32, u64)) -> bool {
    let Ok(fds) = std::fs::read_dir(format!("/proc/{pid}/fd")) else { return false };
    fds.flatten().any(|e| std::fs::metadata(e.path()).is_ok_and(|m| (libc::major(m.dev()), libc::minor(m.dev()), m.ino()) == id))
}

/// The generation number of a `system-<n>-link` name.
pub fn link_generation(name: &str) -> Option<u64> {
    name.strip_prefix("system-")?.strip_suffix("-link")?.parse().ok()
}

impl System<'_> {
    fn rollback_active(&mut self) -> bool {
        proc::quiet(&self.s.systemctl, &["is-active", "--quiet", &format!("{UNIT}.service")]) == 0
    }

    fn timer_live(&mut self) -> bool {
        proc::quiet(&self.s.systemctl, &["is-active", "--quiet", &format!("{UNIT}.timer"), &format!("{UNIT}.service")]) == 0
    }

    fn profile_system(&mut self) -> Option<String> {
        std::fs::canonicalize(PROFILE).ok().map(|p| p.to_string_lossy().into_owned())
    }

    /// Held until the run ends.
    fn lock(&mut self) -> Result<Option<Record<String>>, String> {
        let dir = &self.s.state_dir;
        std::fs::create_dir_all(dir).map_err(|e| format!(text!(record_path_error), path = dir.display(), e = e))?;
        let path = dir.join("lock");
        let f = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(&path)
            .map_err(|e| format!(text!(record_path_error), path = path.display(), e = e))?;
        f.lock().map_err(|e| format!(text!(switch_lock_error), path = path.display(), e = e))?;
        self.held = Some(f);
        Record::load(dir)
    }

    fn running(&mut self) -> Option<String> {
        std::fs::read_link("/run/current-system").ok().map(|p| p.to_string_lossy().into_owned())
    }

    fn start_timer(&mut self, secs: u64) -> bool {
        let Ok(me) = std::env::current_exe() else { return false };
        let me = me.to_string_lossy().into_owned();
        let config = self.s.config.to_string_lossy().into_owned();
        proc::run(
            &self.s.systemd_run,
            &[
                "--quiet",
                "--collect",
                &format!("--unit={UNIT}"),
                &format!("--on-active={secs}"),
                "--timer-property=AccuracySec=1s",
                &me,
                "--config",
                &config,
                "rollback",
            ],
        ) == 0
    }

    fn stop_timer(&mut self) {
        proc::quiet(&self.s.systemctl, &["stop", &format!("{UNIT}.timer")]);
    }

    fn reset_failed(&mut self) {
        proc::quiet(&self.s.systemctl, &["reset-failed", &format!("{UNIT}.service")]);
    }

    fn generation(&mut self) -> Option<u64> {
        let l = std::fs::read_link(PROFILE).ok()?;
        link_generation(&l.file_name()?.to_string_lossy())
    }

    fn set_profile(&mut self, system: &str) -> bool {
        proc::run(&self.s.nix_env, &["-p", PROFILE, "--set", system]) == 0
    }

    fn switch(&mut self, system: &str, action: Action) -> i32 {
        proc::run(&format!("{system}/bin/switch-to-configuration"), &[action.as_str()])
    }

    /// Before each rollback attempt (the timer's, or a failed confirm's): as
    /// long as a live process holds the lock, since a switch outlasting any
    /// fixed bound would otherwise have this one refused and an attempt wasted.
    fn wait_switch_lock(&mut self) {
        let _ = std::fs::create_dir_all("/run/nixos");
        let Ok(f) = OpenOptions::new().create(true).truncate(false).write(true).open(SWITCH_LOCK) else {
            return;
        };
        let id = f.metadata().ok().map(|m| (libc::major(m.dev()), libc::minor(m.dev()), m.ino()));
        let start = Instant::now();
        let (mut seen, mut unseen_since, mut reminded) = (None, None, Instant::now());
        while f.try_lock().is_err() {
            let holder = id.and_then(|id| {
                flock_holder(&std::fs::read_to_string("/proc/locks").unwrap_or_default(), id).filter(|pid| has_open(*pid, id))
            });
            if let Some(pid) = holder {
                unseen_since = None;
                let comm = || std::fs::read_to_string(format!("/proc/{pid}/comm")).unwrap_or_default().trim().to_owned();
                if seen != Some(pid) {
                    self.say(&format!(text!(switch_lock_waiting), P = P, pid = pid, comm = comm()));
                    seen = Some(pid);
                    reminded = Instant::now();
                } else if reminded.elapsed() >= HELD_REMINDER {
                    self.say(&format!(
                        text!(switch_lock_still_waiting),
                        P = P,
                        pid = pid,
                        comm = comm(),
                        secs = start.elapsed().as_secs_f64()
                    ));
                    reminded = Instant::now();
                }
            } else if unseen_since.get_or_insert_with(Instant::now).elapsed() >= UNSEEN_HOLDER_WAIT {
                self.warn(&format!(text!(switch_lock_unseen), P = P));
                return;
            }
            std::thread::sleep(LOCK_POLL);
        }
        if seen.is_some() {
            self.say(&format!(text!(switch_lock_released), P = P, secs = start.elapsed().as_secs_f64()));
        }
    }

    fn sleep(&mut self, secs: u64) {
        std::thread::sleep(Duration::from_secs(secs));
    }

    fn switch_generation(&mut self, g: u64) -> bool {
        proc::run(&self.s.nix_env, &["-p", PROFILE, "--switch-generation", &g.to_string()]) == 0
    }

    fn delete_generation(&mut self, g: u64) -> bool {
        proc::run(&self.s.nix_env, &["-p", PROFILE, "--delete-generations", &g.to_string()]) == 0
    }

    fn persist(&mut self, rec: &Record<String>) -> Result<(), String> {
        Record::store(&self.s.state_dir, rec).map_err(|e| e.to_string())
    }

    fn say(&mut self, msg: &str) {
        crate::proc::say(msg);
    }

    fn warn(&mut self, msg: &str) {
        crate::proc::warn(msg);
    }

    fn log(&mut self, msg: &str) {
        proc::quiet(&self.s.logger, &["-t", "confirmed-switch", msg]);
    }

    fn state_file(&self) -> PathBuf {
        self.s.state_dir.join(crate::record::FILE)
    }
}

pub const USAGE: &str = text!(switch_usage);

/// The `check` verb: the internet probe when configured, then the target's
/// checks; nixconvoy reports its last stderr line.  Root, so the checks can
/// attribute sockets (ss -p) and read state.
fn check(s: &Settings) -> i32 {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        proc::warn(text!(switch_check_not_root));
        return 1;
    }
    let probe = ["-fsS", "--max-time", "10", "-o", "/dev/null", "https://www.google.com/generate_204"];
    if s.internet_check && proc::run(&s.curl, &probe) != 0 {
        proc::warn(text!(switch_check_no_internet));
        return 1;
    }
    use std::os::unix::process::CommandExt;
    let e = std::process::Command::new(&s.checks).exec();
    proc::warn(&format!(text!(switch_check_cannot_run), checks = s.checks, e = e));
    1
}

/// `nixconvoy-confirmed-switch --config <file> <verb> ...`
pub fn main(args: &[String]) -> i32 {
    // nixconvoy runs `arm` over a plain ssh session: losing it must not
    // stop the run between stopping the old timer and recording the new.
    // SAFETY: setting a signal's disposition to SIG_IGN has no preconditions.
    unsafe { libc::signal(libc::SIGHUP, libc::SIG_IGN) };
    let (config, rest) = match args {
        [flag, path, rest @ ..] if flag == "--config" => (Path::new(path), rest),
        _ => {
            proc::warn(USAGE);
            return 2;
        }
    };
    let s = match Settings::load(config) {
        Ok(s) => s,
        Err(e) => {
            proc::warn(&format!(text!(switch_bad_config), P = P, e = e));
            return 1;
        }
    };
    let rest: Vec<&str> = rest.iter().map(String::as_str).collect();
    let verb = match rest.as_slice() {
        ["arm"] => Verb::Arm,
        ["activate", t] => Verb::Activate(t.to_string()),
        ["confirm", t] => Verb::Confirm { new: t.to_string(), reboot_pending: false },
        ["confirm", t, "reboot-pending"] => Verb::Confirm { new: t.to_string(), reboot_pending: true },
        ["rollback"] => Verb::Rollback,
        ["check"] => return check(&s),
        ["deadline"] => {
            proc::say(&s.deadline.to_string());
            return 0;
        }
        _ => {
            proc::warn(USAGE);
            return 2;
        }
    };
    drive(&mut System::new(&s), Machine::new(verb, s.deadline, ACTIVATION_GUARD_SECS))
}

#[cfg(test)]
mod tests {
    use super::flock_holder;

    const LOCKS: &str = "\
1: POSIX  ADVISORY  WRITE 577 00:1b:42 0 EOF
2: FLOCK  ADVISORY  WRITE 4105054 00:1b:1256931 0 EOF
2: -> FLOCK  ADVISORY  WRITE 99 00:1b:1256931 0 EOF
3: OFDLCK ADVISORY  WRITE -1 00:1b:7 0 EOF
4: FLOCK  ADVISORY  WRITE 0 00:1b:8 0 EOF
5: FLOCK  ADVISORY  WRITE 31 08:30:9 0 EOF
6: FLOCK
";

    #[test]
    fn names_the_flock_holder_not_its_waiters() {
        assert_eq!(flock_holder(LOCKS, (0, 0x1b, 1256931)), Some(4105054));
        assert_eq!(flock_holder(LOCKS, (0, 0x1c, 1256931)), None);
        assert_eq!(flock_holder(LOCKS, (0, 0x1b, 42)), None);
        assert_eq!(flock_holder(LOCKS, (0, 0x1b, 7)), None);
        assert_eq!(flock_holder(LOCKS, (0, 0x1b, 8)), None);
        assert_eq!(flock_holder(LOCKS, (8, 0x30, 9)), Some(31));
    }
}
