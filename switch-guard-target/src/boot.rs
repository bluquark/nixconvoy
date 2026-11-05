//! boot-assessment's effects: the profile and the ESP for the planner,
//! systemd, the watchdog and the notice command for the judge.
use crate::config::Config;
use crate::proc::{self, say, warn};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;
use switch_guard::boot::gate::{self, Bless, Gate, GateUnit, Watch};
use switch_guard::boot::plan::{self, Boot, Ctx, Esp, File, Gen, Generation, Policy, Profile, Stub};
use switch_guard::record::Record;
use switch_guard::text;

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
    kits_dir: Option<PathBuf>,
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
            kits_dir: c.opt("kits_dir").map(PathBuf::from),
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
            let Some(num) = crate::switch::link_generation(&name) else { continue };
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
        warn(&format!(text!(boot_target_missing), target = target));
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
            warn(&format!(text!(boot_bad_record), e = e));
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
            let body = format!(text!(boot_esp_full_notice), host = s.hostname, mib = bytes.div_ceil(1 << 20), esp = s.esp.display());
            warn(&format!(text!(boot_esp_full), bytes = bytes));
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
        warn(&format!(text!(boot_prune_no_profile), profile = s.profile().display()));
        return 1;
    };
    if let Err(e) = std::fs::read_dir(s.stub_dir()) {
        warn(&format!(text!(boot_prune_no_esp), dir = s.stub_dir().display(), e = e));
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
    let dropped = if drop.is_empty() { text!(boot_none).to_string() } else { list(&drop) };
    let (kept, anchors) = (list(&kept), list(&anchors));
    say(&if dry_run {
        format!(text!(boot_would_prune), drop = dropped, kept = kept, anchors = anchors, newest = s.keep_newest)
    } else {
        format!(text!(boot_pruning), drop = dropped, kept = kept, anchors = anchors, newest = s.keep_newest)
    });
    if drop.is_empty() || dry_run {
        return 0;
    }
    let mut args = vec!["-p".to_string(), s.profile().to_string_lossy().into_owned(), "--delete-generations".to_string()];
    args.extend(drop.iter().map(Gen::to_string));
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    proc::run(&s.nix_env, &args)
}

fn date(s: &Settings) -> String {
    proc::output(&s.date, &["-Iseconds"]).map_or_else(|| text!(boot_unknown).to_string(), |d| d.trim().to_string())
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
            warn(&format!(text!(boot_log_failed), path = s.boot_log.display(), e = e));
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
        .and_then(|l| crate::switch::link_generation(&l.to_string_lossy()))
        .map_or_else(|| text!(boot_unknown).to_string(), |g| g.to_string());
    let rejected: Vec<String> = stub_names(s).into_iter().filter(|n| Stub::parse(n).is_some_and(|st| st.tries_left == Some(0))).collect();
    let rejected = if rejected.is_empty() { text!(boot_none).to_string() } else { rejected.join("\n") };
    let body = format!(
        text!(boot_failover_notice),
        host = s.hostname,
        bgen = bgen.map_or_else(|| text!(boot_unknown).to_string(), |g| g.to_string()),
        date = date(s),
        ngen = ngen,
        rejected = rejected,
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
                f.push(format!(text!(boot_checks_exited), code = proc::code(Ok(o.status))));
            }
            f
        }
        Err(e) => vec![format!(text!(boot_checks_cannot_run), e = e)],
    };
    // The booted system's kit's checks; none rooted for it (switched
    // plainly), or a kit without checks, is nothing more to prove.
    let switch_check = s.kits_dir.as_ref().and_then(|d| {
        let booted = std::fs::read_link(BOOTED).ok()?;
        Some(d.join(booted.file_name()?).join("checks")).filter(|c| c.exists())
    });
    if let Some(c) = switch_check {
        match Command::new(&c).stdout(std::process::Stdio::null()).output() {
            Ok(o) if o.status.success() => {}
            Ok(o) => {
                let err = String::from_utf8_lossy(&o.stderr);
                let last = err.lines().rev().find(|l| !l.trim().is_empty());
                fails.push(last.map_or_else(|| text!(boot_switch_checks_failed).to_string(), str::to_string));
            }
            Err(e) => fails.push(format!(text!(boot_switch_checks_cannot_run), e = e)),
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
                say(text!(boot_gate_passed));
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
                    say(&format!(text!(boot_gate_deadline), deadline = s.deadline, status = status));
                } else {
                    say(&format!(text!(boot_gate_settled), t = t, status = status));
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

/// Whether the ESP shows the counter taken off the booted entry: nothing
/// under its counted name, the uncounted name in its place.  The rename
/// read back rather than taken from the unit that did it, which a switch
/// stops and starts again.
fn blessed_on_esp(s: &Settings, entry: &str) -> bool {
    let counted = count_file(&s.esp, entry);
    let Some(good) = counted.file_name().and_then(|n| n.to_str()).and_then(Stub::uncounted) else { return false };
    !counted.exists() && counted.with_file_name(good).exists()
}

/// The file LoaderBootCountPath names (`\EFI\Linux\<stub>`), on the ESP.
fn count_file(esp: &Path, entry: &str) -> PathBuf {
    esp.join(entry.trim_start_matches('\\').replace('\\', "/"))
}

/// The judge: arms the hardware watchdog and reboot-on-panic as soon as
/// D-Bus is up, reboots when the gate fails or will never run, and disarms
/// once systemd-bless-boot has removed the counter.
fn run_watch(s: &Settings) -> i32 {
    let clock = Clock::new(s);
    let entry = entry(s);
    if unit_state(s, BLESS).0 == "active" || blessed_on_esp(s, &entry) {
        say(&format!(text!(boot_already_blessed), entry = entry));
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
        warn(text!(boot_no_watchdog));
    }
    set_manager(s, "RebootWatchdogUSec", 120_000_000);
    let _ = std::fs::write("/proc/sys/kernel/panic", s.panic.to_string());
    say(&format!(text!(boot_assessing), entry = entry, watchdog = s.runtime_watchdog, deadline = s.deadline));
    loop {
        let bless = match unit_state(s, BLESS) {
            _ if blessed_on_esp(s, &entry) => Bless::Active,
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
                say(&format!(text!(boot_blessed), entry = entry));
                return 0;
            }
            Watch::BlessFailed => {
                disarm();
                let body = format!(text!(boot_bless_failed_notice), host = s.hostname, entry = entry, date = date(s));
                notice(s, "set", "boot-assessment", Some(&body));
                say(text!(boot_not_blessed));
                return 0;
            }
            Watch::Reboot => break,
        }
    }
    let status = std::fs::read_to_string(&s.status_file).map_or_else(|_| text!(boot_gate_never_ran).to_string(), |t| t.trim().to_string());
    let body = format!(text!(boot_failed_notice), host = s.hostname, entry = entry, date = date(s), status = status);
    notice(s, "set", "boot-assessment", Some(&body));
    // SAFETY: sync has no preconditions.
    unsafe { libc::sync() };
    proc::run(&s.systemctl, &["reboot"])
}

pub const USAGE: &str = text!(boot_usage);

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
            warn(&format!(text!(boot_bad_config), e = e));
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn count_file_is_the_variable_on_the_esp() {
        let esp = Path::new("/boot");
        assert_eq!(count_file(esp, r"\EFI\Linux\x+1-1.efi"), PathBuf::from("/boot/EFI/Linux/x+1-1.efi"));
        assert_eq!(count_file(esp, ""), PathBuf::from("/boot"));
    }
}
