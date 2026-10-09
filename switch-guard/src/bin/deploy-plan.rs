//! nixconvoy's checkout and drift decisions, one core per process: see
//! plan/wire.rs.
use std::io::{BufRead, Write};
use switch_guard::plan::wire;

fn main() {
    let stdin = std::io::stdin();
    let mut lines = stdin.lock().lines();
    let mut out = std::io::stdout().lock();
    let fail = |msg: String| -> ! {
        eprintln!("deploy-plan: {msg}");
        std::process::exit(2);
    };
    let first = lines.next().and_then(Result::ok).unwrap_or_else(|| fail("no parameters".into()));
    let mut core = wire::start(&first).unwrap_or_else(|e| fail(e));
    let mut reply = None;
    loop {
        let (line, more) = core.decide(reply.take());
        let _ = writeln!(out, "{line}");
        let _ = out.flush();
        if !more {
            return;
        }
        let Some(Ok(line)) = lines.next() else { return };
        reply = Some(wire::reply(&line).unwrap_or_else(|e| fail(e)));
    }
}
