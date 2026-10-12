//! Performs a Machine's effects: `Host` is the I/O boundary, `System` the
//! real one (systemd, nix-env, switch-to-configuration), and `drive` the
//! loop that feeds replies back.
use super::core::{Action, Effect, Machine, Next, Query, Reply, Verb};
use crate::config::Config;
use crate::proc;
use crate::record::Record;
use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const UNIT: &str = "confirmed-switch-rollback";
pub const PROFILE: &str = "/nix/var/nix/profiles/system";
const SWITCH_LOCK: &str = "/run/nixos/switch-to-configuration.lock";
const SWITCH_LOCK_WAIT: Duration = Duration::from_secs(600);

pub trait Host {
    fn rollback_active(&mut self) -> bool;
    fn timer_live(&mut self) -> bool;
    fn profile_system(&mut self) -> Option<String>;
    /// Held until the run ends.
    fn lock(&mut self) -> Result<Option<Record<String>>, String>;
    fn running(&mut self) -> Option<String>;
    fn start_timer(&mut self) -> bool;
    fn stop_timer(&mut self);
    fn reset_failed(&mut self);
    fn generation(&mut self) -> Option<u64>;
    fn set_profile(&mut self, system: &str) -> bool;
    fn switch(&mut self, system: &str, action: Action) -> i32;
    fn wait_switch_lock(&mut self);
    fn sleep(&mut self, secs: u64);
    fn switch_generation(&mut self, g: u64) -> bool;
    fn delete_generation(&mut self, g: u64) -> bool;
    fn persist(&mut self, rec: Option<&Record<String>>) -> Result<(), String>;
    fn say(&mut self, msg: &str);
    fn warn(&mut self, msg: &str);
    fn log(&mut self, msg: &str);
    fn state_file(&self) -> PathBuf;
}

/// Runs `m` to completion against `host`: its exit code.
pub fn drive(host: &mut impl Host, mut m: Machine<String>) -> i32 {
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
                    if let Err(e) = host.persist(r.as_ref()) {
                        host.warn(&format!("confirmed-switch: cannot record the state: {e}"));
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
                        host.warn(&format!(
                            "confirmed-switch: {e}; a state file it cannot parse refuses every verb: once no switch is in flight, remove it (sudo rm {})",
                            host.state_file().display()
                        ));
                        return 1;
                    }
                },
                Query::Running => Reply::Running(host.running()),
                Query::StartTimer => Reply::Flag(host.start_timer()),
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

/// The generation number of a `system-<n>-link` name.
pub fn link_generation(name: &str) -> Option<u64> {
    name.strip_prefix("system-")?.strip_suffix("-link")?.parse().ok()
}

impl Host for System<'_> {
    fn rollback_active(&mut self) -> bool {
        proc::quiet(&self.s.systemctl, &["is-active", "--quiet", &format!("{UNIT}.service")]) == 0
    }

    fn timer_live(&mut self) -> bool {
        proc::quiet(&self.s.systemctl, &["is-active", "--quiet", &format!("{UNIT}.timer"), &format!("{UNIT}.service")]) == 0
    }

    fn profile_system(&mut self) -> Option<String> {
        std::fs::canonicalize(PROFILE).ok().map(|p| p.to_string_lossy().into_owned())
    }

    fn lock(&mut self) -> Result<Option<Record<String>>, String> {
        let dir = &self.s.state_dir;
        std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        let path = dir.join("lock");
        let f = OpenOptions::new().create(true).truncate(false).write(true).open(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        f.lock().map_err(|e| format!("lock {}: {e}", path.display()))?;
        self.held = Some(f);
        Record::load(dir)
    }

    fn running(&mut self) -> Option<String> {
        std::fs::read_link("/run/current-system").ok().map(|p| p.to_string_lossy().into_owned())
    }

    fn start_timer(&mut self) -> bool {
        let Ok(me) = std::env::current_exe() else { return false };
        let me = me.to_string_lossy().into_owned();
        let config = self.s.config.to_string_lossy().into_owned();
        proc::run(
            &self.s.systemd_run,
            &[
                "--quiet",
                "--collect",
                &format!("--unit={UNIT}"),
                &format!("--on-active={}", self.s.deadline),
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

    fn wait_switch_lock(&mut self) {
        let _ = std::fs::create_dir_all("/run/nixos");
        let Ok(f) = OpenOptions::new().create(true).truncate(false).write(true).open(SWITCH_LOCK) else {
            return;
        };
        let start = Instant::now();
        while start.elapsed() < SWITCH_LOCK_WAIT {
            if f.try_lock().is_ok() {
                return;
            }
            std::thread::sleep(Duration::from_millis(200));
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

    fn persist(&mut self, rec: Option<&Record<String>>) -> Result<(), String> {
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

pub const USAGE: &str =
    "usage: confirmed-switch arm | activate <toplevel> | confirm <toplevel> [reboot-pending] | rollback | check | deadline";

/// The `check` verb: nixconvoy reports its last stderr line.  Root, so the
/// target's checks can attribute sockets (ss -p) and read state.
fn check(s: &Settings) -> i32 {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        proc::warn("check must run as root");
        return 1;
    }
    let probe = ["-fsS", "--max-time", "10", "-o", "/dev/null", "https://www.google.com/generate_204"];
    if proc::run(&s.curl, &probe) != 0 {
        proc::warn("cannot reach the internet");
        return 1;
    }
    use std::os::unix::process::CommandExt;
    let e = std::process::Command::new(&s.checks).exec();
    proc::warn(&format!("cannot run {}: {e}", s.checks));
    1
}

/// `confirmed-switch --config <file> <verb> ...`
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
            proc::warn(&format!("confirmed-switch: {e}"));
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
    drive(&mut System::new(&s), Machine::new(verb, s.deadline))
}
