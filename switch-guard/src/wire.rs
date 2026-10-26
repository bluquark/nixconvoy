//! The JSON-lines protocol nixconvoy-switch (control/wire.rs) and
//! nixconvoy-plan (plan/wire.rs) speak with nixconvoy: every line is the
//! serde default encoding of one of this crate's types (an enum externally
//! tagged by its variant's snake_case name, a struct by field, Option as
//! null, tuples and sets as lists), which src/nixconvoy/wire.py mirrors
//! class for type.  nixconvoy writes the parameters, then one reply per
//! query; the binary answers each with one step until
//! it asks nothing more.
use serde::de::DeserializeOwned;
use serde::Serialize;
use std::io::{BufRead, Write};

/// The parameters line.
pub fn params<P: DeserializeOwned>(line: &str) -> Result<P, String> {
    serde_json::from_str(line).map_err(|e| e.to_string())
}

/// A reply line.
pub fn reply<R: DeserializeOwned>(line: &str) -> Result<R, String> {
    serde_json::from_str(line).map_err(|e| e.to_string())
}

/// A step's line.
pub fn line<T: Serialize>(step: &T) -> String {
    serde_json::to_string(step).expect("a step serializes")
}

/// A binary's stdin/stdout loop: `start` reads the parameters line, `decide`
/// turns the last reply (None at first) into the next line and whether it
/// asks a query, `reply` parses each answer.  Bad input exits 2.
pub fn serve<C, R>(
    binary: &str,
    start: impl FnOnce(&str) -> Result<C, String>,
    mut decide: impl FnMut(&mut C, Option<R>) -> (String, bool),
    reply: impl Fn(&str) -> Result<R, String>,
) {
    let stdin = std::io::stdin();
    let mut lines = stdin.lock().lines();
    let mut out = std::io::stdout().lock();
    let fail = |msg: String| -> ! {
        eprintln!("{binary}: {msg}");
        std::process::exit(2);
    };
    let first = lines.next().and_then(Result::ok).unwrap_or_else(|| fail("no parameters".into()));
    let mut core = start(&first).unwrap_or_else(|e| fail(e));
    let mut last = None;
    loop {
        let (line, more) = decide(&mut core, last.take());
        let _ = writeln!(out, "{line}");
        let _ = out.flush();
        if !more {
            return;
        }
        let Some(Ok(line)) = lines.next() else { return };
        last = Some(reply(&line).unwrap_or_else(|e| fail(e)));
    }
}
