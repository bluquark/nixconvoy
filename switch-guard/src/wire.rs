//! The JSON-lines protocol nixconvoy-switch (control/wire.rs) and
//! nixconvoy-plan (plan/wire.rs) speak with nixconvoy: nixconvoy writes the
//! parameters, then one reply per query; the binary answers each with one
//! line until it asks nothing more.
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::io::{BufRead, Write};

pub fn field<'a>(v: &'a Value, k: &str) -> Result<&'a Value, String> {
    v.get(k).ok_or_else(|| format!("missing {k} in {v}"))
}

pub fn text(v: &Value, k: &str) -> Result<String, String> {
    field(v, k)?.as_str().map(str::to_string).ok_or_else(|| format!("{k} is not a string"))
}

pub fn opt_text(v: &Value) -> Result<Option<String>, String> {
    match v {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s.clone())),
        _ => Err(format!("{v} is not a string or null")),
    }
}

pub fn flag(v: &Value, k: &str) -> Result<bool, String> {
    field(v, k)?.as_bool().ok_or_else(|| format!("{k} is not a boolean"))
}

pub fn int(v: &Value, k: &str) -> Result<i64, String> {
    field(v, k)?.as_i64().ok_or_else(|| format!("{k} is not an integer"))
}

fn string_list(v: &Value, k: &str) -> Result<Vec<String>, String> {
    v.as_array()
        .ok_or_else(|| format!("{k} is not a list"))?
        .iter()
        .map(|x| x.as_str().map(str::to_string).ok_or_else(|| format!("{k} holds a non-string")))
        .collect()
}

pub fn strings(v: &Value, k: &str) -> Result<Vec<String>, String> {
    string_list(field(v, k)?, k)
}

/// The object `v[k]` with each value read by `each`.
pub fn map_of<T>(v: &Value, k: &str, each: impl Fn(&Value) -> Result<T, String>) -> Result<BTreeMap<String, T>, String> {
    let o: &Map<String, Value> = field(v, k)?.as_object().ok_or_else(|| format!("{k} is not an object"))?;
    o.iter().map(|(n, x)| Ok((n.clone(), each(x)?))).collect()
}

pub fn string_map(v: &Value, k: &str) -> Result<BTreeMap<String, String>, String> {
    map_of(v, k, |x| x.as_str().map(str::to_string).ok_or_else(|| format!("{k} holds a non-string")))
}

pub fn string_lists(v: &Value, k: &str) -> Result<BTreeMap<String, Vec<String>>, String> {
    map_of(v, k, |x| string_list(x, k))
}

/// A line of the protocol, which is an object: the parameters or a reply.
pub fn object(line: &str, what: &str) -> Result<Value, String> {
    let v: Value = serde_json::from_str(line).map_err(|e| e.to_string())?;
    if !v.is_object() {
        return Err(format!("{what} is not an object: {line}"));
    }
    Ok(v)
}

pub fn params(line: &str) -> Result<Value, String> {
    object(line, "the parameters")
}

pub fn reply(line: &str) -> Result<Value, String> {
    object(line, "a reply")
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
