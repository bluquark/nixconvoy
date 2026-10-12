//! nixconvoy's switch step, one target per process: see control/wire.rs.
use std::io::{BufRead, Write};
use switch_guard::control::{wire, Machine, Next, Reply};

fn main() {
    let stdin = std::io::stdin();
    let mut lines = stdin.lock().lines();
    let mut out = std::io::stdout().lock();
    let fail = |msg: String| -> ! {
        eprintln!("deploy-switch: {msg}");
        std::process::exit(2);
    };
    let first = lines.next().and_then(Result::ok).unwrap_or_else(|| fail("no parameters".into()));
    let mut m = Machine::new(wire::params(&first).unwrap_or_else(|e| fail(e)));
    let mut reply = Reply::Start;
    loop {
        let step = m.decide(reply);
        let _ = writeln!(out, "{}", wire::step(&step));
        let _ = out.flush();
        if !matches!(step.next, Next::Ask(_)) {
            return;
        }
        let Some(Ok(line)) = lines.next() else { return };
        reply = wire::reply(&line).unwrap_or_else(|e| fail(e));
    }
}
