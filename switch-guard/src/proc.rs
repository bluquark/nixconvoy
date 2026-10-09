//! Running the external programs the drivers' effects are made of.
use std::os::unix::process::ExitStatusExt;
use std::process::{Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

/// A shell-style exit code: 128 + the signal for a killed child, 127 when
/// it could not be started.
pub fn code(status: std::io::Result<ExitStatus>) -> i32 {
    match status {
        Ok(s) => s.code().unwrap_or_else(|| 128 + s.signal().unwrap_or(0)),
        Err(_) => 127,
    }
}

/// Inherited stdio.
pub fn run(prog: &str, args: &[&str]) -> i32 {
    code(Command::new(prog).args(args).status())
}

/// Output discarded.
pub fn quiet(prog: &str, args: &[&str]) -> i32 {
    code(Command::new(prog).args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status())
}

/// Stdout, when the program succeeds.
pub fn output(prog: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(prog).args(args).stderr(Stdio::inherit()).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

/// `cmd` killed after `limit`, as coreutils' timeout(1); `input` on stdin.
pub fn run_limited(mut cmd: Command, input: Option<&str>, limit: Duration) -> i32 {
    use std::io::Write;
    cmd.stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() });
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(_) => return 127,
    };
    if let (Some(text), Some(mut stdin)) = (input, child.stdin.take()) {
        let _ = stdin.write_all(text.as_bytes());
    }
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(s)) => return code(Ok(s)),
            Ok(None) if start.elapsed() < limit => std::thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return 124;
            }
        }
    }
}

/// Stdout lines that a vanished reader (nixconvoy's ssh session) cannot
/// turn into a panic.
pub fn say(msg: &str) {
    use std::io::Write;
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{msg}");
    let _ = out.flush();
}

pub fn warn(msg: &str) {
    use std::io::Write;
    let _ = writeln!(std::io::stderr(), "{msg}");
}
