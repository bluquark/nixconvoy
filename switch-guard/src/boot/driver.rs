//! boot-assessment's effects: the profile and the ESP for the planner,
//! systemd, the watchdog and the notice command for the judge.
use super::gate::{self, Bless, Gate, GateUnit, Watch};
use super::plan::{self, Boot, Ctx, Esp, File, Gen, Generation, Policy, Profile, Stub};
use crate::config::Config;
use crate::proc::{self, say, warn};
use crate::record::Record;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

const BOOTED: &str = "/run/booted-system";
const CURRENT: &str = "/run/current-system";
const MANAGER: [&str; 3] = ["org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager"];
const GATE: &str = "boot-assessment.service";
const BLESS: &str = "systemd-bless-boot.service";

pub struct Settings {
    esp: PathBuf,
    profile_dir: PathBuf,
    boot_log: PathBuf,
    state_dir: Option<PathBuf>,
    policy: Policy,
    keep_newest: usize,
    lzbt: String,
    nix_env: String,
    systemctl: String,
    busctl: String,
    date: String,
    notice: String,
    notice_timeout: u64,
    hostname: String,
    deadline: u64,
    runtime_watchdog: u64,
    panic: u64,
    status_file: PathBuf,
    count_path: PathBuf,
    checks: String,
    switch_check: Option<String>,
    initial_tries: u32,
    reported: PathBuf,
}

impl Settings {
    fn load(path: &Path) -> Result<Settings, String> {
        let c = Config::load(path)?;
        let p = |k: &str| c.str(k).map(PathBuf::from);
        let s = |k: &str| c.str(k).map(str::to_string);
        Ok(Settings {
            esp: p("esp")?,
            profile_dir: p("profile_dir")?,
            boot_log: p("boot_log")?,
            state_dir: c.opt("state_dir").map(PathBuf::from),
            policy: Policy { bootable: c.num("bootable")?, tiers: c.nums("tiers")?, reserve: c.num("reserve")? },
            keep_newest: c.num("keep_newest")?,
            lzbt: s("lzbt")?,
            nix_env: s("nix_env")?,
            systemctl: s("systemctl")?,
            busctl: s("busctl")?,
            date: s("date")?,
            notice: s("notice")?,
            notice_timeout: c.num("notice_timeout")?,
            hostname: s("hostname")?,
            deadline: c.num("deadline")?,
            runtime_watchdog: c.num("runtime_watchdog")?,
            panic: c.num("panic")?,
            status_file: p("status_file")?,
            count_path: p("count_path")?,
            checks: s("checks")?,
            switch_check: c.opt("switch_check").map(str::to_string),
            initial_tries: c.num("initial_tries")?,
            reported: p("reported")?,
        })
    }

    fn profile(&self) -> PathBuf {
        self.profile_dir.join("system")
    }

    fn stub_dir(&self) -> PathBuf {
        self.esp.join("EFI/Linux")
    }
}

fn canonical(p: impl AsRef<Path>) -> Option<String> {
    std::fs::canonicalize(p).ok().map(|p| p.to_string_lossy().into_owned())
}

fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64)
}

fn uptime() -> u64 {
    std::fs::read_to_string("/proc/uptime").ok().and_then(|s| s.split('.').next().and_then(|n| n.parse().ok())).unwrap_or(0)
}

fn read_profile(s: &Settings) -> Profile<String> {
    let mut gens = Vec::new();
    if let Ok(dir) = std::fs::read_dir(&s.profile_dir) {
        for e in dir.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let Some(num) = crate::switch::driver::link_generation(&name) else { continue };
            let link = e.path();
            let Ok(meta) = std::fs::symlink_metadata(&link) else { continue };
            let path =
                canonical(&link).unwrap_or_else(|| std::fs::read_link(&link).map(|t| t.to_string_lossy().into_owned()).unwrap_or_default());
            gens.push(Generation { num, mtime: meta.mtime(), path });
        }
    }
    Profile::new(gens)
}

fn read_boots(s: &Settings) -> Vec<Boot> {
    let text = std::fs::read_to_string(&s.boot_log).unwrap_or_default();
    text.lines()
        .filter_map(|l| {
            let mut w = l.split_whitespace();
            let time = w.next()?.parse().ok()?;
            Some(Boot { time, gen: w.next().and_then(|g| g.parse().ok()) })
        })
        .collect()
}

fn stub_names(s: &Settings) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(s.stub_dir())
        .map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect())
        .unwrap_or_default();
    v.sort();
    v
}

fn read_stubs(s: &Settings) -> Vec<(Stub, String)> {
    stub_names(s).into_iter().filter_map(|n| Stub::parse(&n).map(|st| (st, n))).collect()
}

struct RealEsp<'a> {
    s: &'a Settings,
    profile: Profile<String>,
}

impl Esp for RealEsp<'_> {
    type P = String;
    type F = String;

    fn profile(&mut self) -> Profile<String> {
        self.profile = read_profile(self.s);
        self.profile.clone()
    }

    fn delete_generation(&mut self, g: Gen) -> bool {
        proc::run(&self.s.nix_env, &["-p", &self.s.profile().to_string_lossy(), "--delete-generations", &g.to_string()]) == 0
    }

    fn stubs(&mut self) -> Vec<(Stub, String)> {
        read_stubs(self.s)
    }

    fn files(&mut self, g: Gen) -> Vec<File<String>> {
        let Some(top) = self.profile.path(g) else { return vec![] };
        ["kernel", "initrd"]
            .iter()
            .filter_map(|f| {
                let p = std::fs::canonicalize(Path::new(top).join(f)).ok()?;
                let size = std::fs::metadata(&p).ok()?.len();
                Some(File { id: p.to_string_lossy().into_owned(), size })
            })
            .collect()
    }

    fn install(&mut self, gens: &[Gen]) -> i32 {
        let links: Vec<String> =
            gens.iter().map(|g| self.s.profile_dir.join(format!("system-{g}-link")).to_string_lossy().into_owned()).collect();
        let args: Vec<&str> = links.iter().map(String::as_str).collect();
        proc::run(&self.s.lzbt, &args)
    }

    fn usage(&mut self) -> (u64, u64) {
        let Ok(c) = std::ffi::CString::new(self.s.esp.to_string_lossy().as_bytes()) else { return (0, 0) };
        // SAFETY: statvfs writes only into the zeroed struct it is given.
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
            return (0, 0);
        }
        let frsize = st.f_frsize as u64;
        let blocks = st.f_blocks as u64;
        (blocks * frsize, (blocks - st.f_bfree as u64) * frsize)
    }

    fn say(&mut self, msg: &str) {
        say(msg);
    }

    fn warn(&mut self, msg: &str) {
        warn(msg);
    }
}

/// system.build.installBootLoader: `<toplevel>` as installed by
/// switch-to-configuration boot (or a confirmed switch's confirm).
fn install(s: &Settings, target: &str) -> i32 {
    let Some(target) = canonical(target) else {
        warn(&format!("boot-assessment: {target} does not exist"));
        return 1;
    };
    let ctx = ctx(s, target);
    let mut esp = RealEsp { s, profile: read_profile(s) };
    let done = plan::install(&mut esp, &read_boots(s), &ctx, &s.policy);
    esp_notice(s, done.shortfall);
    done.code
}

fn ctx(s: &Settings, target: String) -> Ctx<String> {
    let record = match s.state_dir.as_deref().map(Record::load) {
        Some(Err(e)) => {
            warn(&format!(
                "boot-assessment: {e}; not sparing a pending rollback's system - remove that file once no confirmed switch is in flight"
            ));
            None
        }
        r => r.and_then(Result::ok).flatten(),
    };
    let previous = record.and_then(|r| canonical(&r.previous));
    Ctx { target, booted: canonical(BOOTED), running: canonical(CURRENT), previous, now: now() }
}

/// The ESP-full notice, set when even the generations the failover needs
/// do not fit (lanzaboote's install then fails at every switch until space
/// is made), cleared by the next install that fits.  The marker beside the
/// boot log keeps a fitting install from paying a notice call.
fn esp_notice(s: &Settings, shortfall: Option<u64>) {
    let marker = s.boot_log.with_file_name("esp-full");
    match shortfall {
        Some(bytes) => {
            let body = format!(
                "{host}'s ESP lacks {mib} MiB for the generations its failover needs (target, booted, running, previous): every switch fails to install its boot entry until space is made on {esp}.\n",
                host = s.hostname,
                mib = bytes.div_ceil(1 << 20),
                esp = s.esp.display(),
            );
            warn(&format!("boot-assessment: the ESP lacks {bytes} bytes even for the generations the failover needs"));
            notice(s, "set", "esp-full", Some(&body));
            let _ = std::fs::write(&marker, "");
        }
        None if marker.exists() => {
            notice(s, "clear", "esp-full", None);
            let _ = std::fs::remove_file(&marker);
        }
        None => {}
    }
}

/// nix-gc's ExecStartPre (with nix.gc's own options cleared): prunes the
/// system profile down to what an install for its generation would make
/// bootable, and the newest generations.
fn prune(s: &Settings, dry_run: bool) -> i32 {
    let Some(target) = canonical(s.profile()) else {
        warn(&format!("boot-assessment: {} does not exist, not pruning", s.profile().display()));
        return 1;
    };
    if let Err(e) = std::fs::read_dir(s.stub_dir()) {
        warn(&format!("boot-assessment: cannot read {}, not pruning: {e}", s.stub_dir().display()));
        return 1;
    }
    let profile = read_profile(s);
    let stubs: Vec<Stub> = read_stubs(s).into_iter().map(|(st, _)| st).collect();
    let boots = read_boots(s);
    let ctx = ctx(s, target);
    let drop = plan::prune(&profile, &boots, &stubs, &ctx, &s.policy, s.keep_newest);
    let list = |v: &[Gen]| v.iter().map(Gen::to_string).collect::<Vec<_>>().join(" ");
    let anchors = plan::anchors(&profile, &boots, &stubs, ctx.now, &s.policy.tiers);
    let kept: Vec<Gen> = profile.gens().iter().map(|x| x.num).filter(|g| !drop.contains(g)).collect();
    say(&format!(
        "boot-assessment: {} generations {} (keeping {}: the bootable set, the stubbed, anchors {}, and the newest {})",
        if dry_run { "would prune" } else { "pruning" },
        if drop.is_empty() { "none".to_string() } else { list(&drop) },
        list(&kept),
        list(&anchors),
        s.keep_newest
    ));
    if drop.is_empty() || dry_run {
        return 0;
    }
    let mut args = vec!["-p".to_string(), s.profile().to_string_lossy().into_owned(), "--delete-generations".to_string()];
    args.extend(drop.iter().map(Gen::to_string));
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    proc::run(&s.nix_env, &args)
}

fn date(s: &Settings) -> String {
    proc::output(&s.date, &["-Iseconds"]).map_or_else(|| "?".to_string(), |d| d.trim().to_string())
}

/// `notice set|clear <name>` under a timeout: a publisher's network
/// timeouts must not keep a box about to reboot down longer.
fn notice(s: &Settings, verb: &str, name: &str, body: Option<&str>) {
    let mut cmd = Command::new(&s.notice);
    cmd.args([verb, name]);
    proc::run_limited(cmd, body, Duration::from_secs(s.notice_timeout));
}

/// Once per boot, before any switch: log the boot for the anchors, then
/// flag a boot that did not land on the profile's newest generation.
fn report(s: &Settings) -> i32 {
    if let Some(d) = s.reported.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let _ = std::fs::write(&s.reported, "");
    let profile = read_profile(s);
    let booted = canonical(BOOTED);
    let bgen = profile.gen_of(booted.as_ref());
    let line = format!("{} {}\n", now() - uptime() as i64, bgen.map_or(String::new(), |g| g.to_string()));
    {
        use std::io::Write;
        let appended =
            std::fs::OpenOptions::new().create(true).append(true).open(&s.boot_log).and_then(|mut f| f.write_all(line.as_bytes()));
        if let Err(e) = appended {
            warn(&format!("boot-assessment: {}: {e}", s.boot_log.display()));
        }
    }
    if canonical(CURRENT) != booted {
        return 0;
    }
    if canonical(s.profile()) == booted {
        notice(s, "clear", "boot-failover", None);
        return 0;
    }
    let ngen = std::fs::read_link(s.profile())
        .ok()
        .and_then(|l| crate::switch::driver::link_generation(&l.to_string_lossy()))
        .map_or_else(|| "?".to_string(), |g| g.to_string());
    let rejected: Vec<String> = stub_names(s).into_iter().filter(|n| Stub::parse(n).is_some_and(|st| st.tries_left == Some(0))).collect();
    let rejected = if rejected.is_empty() { "none".to_string() } else { rejected.join("\n") };
    let body = format!(
        "{host} booted generation {bgen} at {date}, not the newest ({ngen}).\n\
         Rejected entries on the ESP: {rejected}\n\
         nixconvoy refuses to switch back onto a rejected generation: change the configuration (a new generation gets its own tries), or re-arm the stub to retry it as is (sudo mv of the +0-N stub to +{tries}, then deploy).\n\
         The boot-assessment notice, if set, says why the newest one failed.\n\
         Log: journalctl -u boot-assessment-watch -u boot-assessment\n",
        host = s.hostname,
        bgen = bgen.map_or_else(|| "?".to_string(), |g| g.to_string()),
        date = date(s),
        tries = s.initial_tries,
    );
    notice(s, "set", "boot-failover", Some(&body));
    0
}

/// Seconds since stage 2 began.
struct Clock {
    since: u64,
}

impl Clock {
    fn new(s: &Settings) -> Clock {
        let usec = proc::output(&s.systemctl, &["show", "-p", "UserspaceTimestampMonotonic", "--value"])
            .and_then(|o| o.trim().parse::<u64>().ok())
            .unwrap_or(0);
        Clock { since: usec / 1_000_000 }
    }

    fn elapsed(&self) -> u64 {
        uptime().saturating_sub(self.since)
    }
}

/// One poll: the target's own checks (`fail <reason>` lines on stdout, in order,
/// and a non-zero exit: a fatal error or a kill fails the poll too), then
/// the confirmed-switch checks' last stderr line.
fn failures(s: &Settings) -> Vec<String> {
    let mut fails: Vec<String> = match Command::new(&s.checks).stderr(std::process::Stdio::inherit()).output() {
        Ok(o) => {
            let mut f: Vec<String> = String::from_utf8_lossy(&o.stdout).lines().filter(|l| !l.is_empty()).map(str::to_string).collect();
            if !o.status.success() {
                f.push(format!("the boot-assessment checks exited {}", proc::code(Ok(o.status))));
            }
            f
        }
        Err(e) => vec![format!("cannot run the checks: {e}")],
    };
    if let Some(c) = &s.switch_check {
        match Command::new(c).stdout(std::process::Stdio::null()).output() {
            Ok(o) if o.status.success() => {}
            Ok(o) => {
                let err = String::from_utf8_lossy(&o.stderr);
                let last = err.lines().rev().find(|l| !l.trim().is_empty());
                fails.push(last.map_or_else(|| "switch checks failed".to_string(), str::to_string));
            }
            Err(e) => fails.push(format!("cannot run the switch checks: {e}")),
        }
    }
    fails
}

/// Whether the boot is still starting something besides the gate: its own
/// job, and boot-complete.target's behind it, hold is-system-running at
/// "starting" until it returns, and every other waiting job waits on a
/// running one.  A unit activating without a job is one waiting out its
/// RestartSec.
fn busy(s: &Settings) -> bool {
    let jobs = proc::output(&s.systemctl, &["list-jobs", "--no-legend", "--plain"]);
    let units = proc::output(
        &s.systemctl,
        &["list-units", "--all", "--no-legend", "--plain", "--state=activating,deactivating,reloading,refreshing"],
    );
    let (Some(jobs), Some(units)) = (jobs, units) else { return true };
    let running_job = jobs.lines().any(|l| {
        let w: Vec<&str> = l.split_whitespace().collect();
        w.get(3) == Some(&"running") && w.get(1) != Some(&GATE)
    });
    running_job || units.lines().filter_map(|l| l.split_whitespace().next()).any(|u| u != GATE)
}

/// Judge this boot (boot-assessment.service, required by
/// boot-complete.target): reachable on the LAN, the target's own checks pass.
fn run_gate(s: &Settings) -> i32 {
    let clock = Clock::new(s);
    if let Some(d) = s.status_file.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let mut last_busy = clock.elapsed();
    loop {
        // Sampled before the checks: a failure gathered while a unit was
        // still starting must not count against an idle seen after.
        let busy = busy(s);
        let fails = failures(s);
        let t = clock.elapsed();
        if busy {
            last_busy = t;
        }
        let pending = t.saturating_sub(last_busy) < gate::SETTLE_SECS;
        match gate::gate(&fails, pending, t, s.deadline) {
            Gate::Pass => {
                say("reachable: sshd, tailscaled, the boot-assessment checks pass");
                let _ = std::fs::remove_file(&s.status_file);
                return 0;
            }
            Gate::Retry { status } => {
                let _ = std::fs::write(&s.status_file, format!("{status}\n"));
                std::thread::sleep(Duration::from_secs(gate::POLL_SECS));
            }
            Gate::Fail { status } => {
                let _ = std::fs::write(&s.status_file, format!("{status}\n"));
                if pending {
                    say(&format!("not reachable by the {}s deadline: {status}", s.deadline));
                } else {
                    say(&format!("not reachable with nothing left starting, {t}s into stage 2: {status}"));
                }
                return 1;
            }
        }
    }
}

/// A unit's ActiveState, and whether it has a job queued.
fn unit_state(s: &Settings, unit: &str) -> (String, bool) {
    let out = proc::output(&s.systemctl, &["show", "-p", "ActiveState", "-p", "Job", unit]).unwrap_or_default();
    let prop = |k: &str| out.lines().find_map(|l| l.strip_prefix(k)?.strip_prefix('=')).unwrap_or("").trim().to_string();
    (prop("ActiveState"), !prop("Job").is_empty())
}

fn gate_unit(s: &Settings) -> GateUnit {
    match unit_state(s, GATE) {
        (a, _) if a == "active" => GateUnit::Passed,
        (a, _) if a == "failed" => GateUnit::Failed,
        (a, _) if a == "activating" => GateUnit::Running,
        (a, false) if a == "inactive" || a == "deactivating" => GateUnit::Gone,
        _ => GateUnit::Queued,
    }
}

fn set_manager(s: &Settings, prop: &str, usec: u64) -> bool {
    let v = usec.to_string();
    let mut args: Vec<&str> = vec!["set-property"];
    args.extend(MANAGER);
    args.extend([prop, "t", &v]);
    proc::run(&s.busctl, &args) == 0
}

fn get_manager(s: &Settings, prop: &str) -> Option<u64> {
    let mut args: Vec<&str> = vec!["get-property"];
    args.extend(MANAGER);
    args.push(prop);
    proc::output(&s.busctl, &args)?.split_whitespace().nth(1)?.parse().ok()
}

/// The entry systemd-boot booted, counted: its EFI variable is UTF-16LE
/// after four attribute bytes.
fn entry(s: &Settings) -> String {
    let raw = std::fs::read(&s.count_path).unwrap_or_default();
    let units: Vec<u16> = raw.get(4..).unwrap_or_default().as_chunks::<2>().0.iter().map(|c| u16::from_le_bytes(*c)).collect();
    String::from_utf16_lossy(&units).trim_end_matches('\0').to_string()
}

/// The judge: arms the hardware watchdog and reboot-on-panic as soon as
/// D-Bus is up, reboots when the gate fails or will never run, and disarms
/// once systemd-bless-boot has removed the counter.
fn run_watch(s: &Settings) -> i32 {
    let clock = Clock::new(s);
    let entry = entry(s);
    if unit_state(s, BLESS).0 == "active" {
        say(&format!("{entry} already blessed this boot"));
        return 0;
    }
    let reboot_wd = get_manager(s, "RebootWatchdogUSec").unwrap_or(600_000_000);
    let panic = std::fs::read_to_string("/proc/sys/kernel/panic").unwrap_or_else(|_| "0".to_string());
    let disarm = || {
        set_manager(s, "RuntimeWatchdogUSec", 0);
        set_manager(s, "RebootWatchdogUSec", reboot_wd);
        let _ = std::fs::write("/proc/sys/kernel/panic", panic.trim());
    };
    if !set_manager(s, "RuntimeWatchdogUSec", s.runtime_watchdog * 1_000_000) {
        warn("hardware watchdog not armed");
    }
    set_manager(s, "RebootWatchdogUSec", 120_000_000);
    let _ = std::fs::write("/proc/sys/kernel/panic", s.panic.to_string());
    say(&format!("assessing {entry}: watchdog {}s, panic reboot, deadline {}s", s.runtime_watchdog, s.deadline));
    loop {
        let bless = match unit_state(s, BLESS) {
            (a, _) if a == "active" => Bless::Active,
            (a, _) if a == "failed" => Bless::Failed,
            (a, false) if a == "inactive" => Bless::Gone,
            _ => Bless::Pending,
        };
        match gate::watch(bless, gate_unit(s), clock.elapsed(), s.deadline) {
            Watch::Wait => std::thread::sleep(Duration::from_secs(gate::POLL_SECS)),
            Watch::Blessed => {
                disarm();
                if canonical(BOOTED) == canonical(s.profile()) {
                    notice(s, "clear", "boot-assessment", None);
                }
                say(&format!("passed: {entry} is blessed"));
                return 0;
            }
            Watch::BlessFailed => {
                disarm();
                let body = format!(
                    "{}'s boot of {entry} passed its assessment at {} but systemd-bless-boot failed, hangs or never ran: the stub keeps its tries counter, and each reboot spends one until the ESP takes the rename.\n\
                     Log: journalctl -u systemd-bless-boot\n",
                    s.hostname,
                    date(s)
                );
                notice(s, "set", "boot-assessment", Some(&body));
                say("reachable, but not blessed: systemd-bless-boot failed or never ran");
                return 0;
            }
            Watch::Reboot => break,
        }
    }
    let status = std::fs::read_to_string(&s.status_file).map_or_else(|_| "the gate never ran".to_string(), |t| t.trim().to_string());
    let body = format!(
        "{}'s boot of {entry} failed its assessment at {}: {status}\n\
         Rebooting: systemd-boot retries the entry until its tries are spent, then boots the generation deployed before it (if it was never booted), then the last blessed one.\n\
         Log: journalctl -u boot-assessment-watch -u boot-assessment\n",
        s.hostname,
        date(s)
    );
    notice(s, "set", "boot-assessment", Some(&body));
    // SAFETY: sync has no preconditions.
    unsafe { libc::sync() };
    proc::run(&s.systemctl, &["reboot"])
}

pub const USAGE: &str = "usage: nixconvoy-boot-assessment --config <file> install <toplevel> | prune [--dry-run] | report | gate | watch";

pub fn main(args: &[String]) -> i32 {
    let (config, rest) = match args {
        [flag, path, rest @ ..] if flag == "--config" => (Path::new(path), rest),
        _ => {
            warn(USAGE);
            return 2;
        }
    };
    let s = match Settings::load(config) {
        Ok(s) => s,
        Err(e) => {
            warn(&format!("boot-assessment: {e}"));
            return 1;
        }
    };
    let rest: Vec<&str> = rest.iter().map(String::as_str).collect();
    match rest.as_slice() {
        ["install", target] => install(&s, target),
        ["prune"] => prune(&s, false),
        ["prune", "--dry-run"] => prune(&s, true),
        ["report"] => report(&s),
        ["gate"] => run_gate(&s),
        ["watch"] => run_watch(&s),
        _ => {
            warn(USAGE);
            2
        }
    }
}
