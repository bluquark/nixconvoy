//! deploy-switch's protocol with nixconvoy: JSON lines.  nixconvoy writes
//! the parameters, then one reply per query; the binary answers each with
//! `{"effects": [...], "next": {...}}`.
use super::{Effect, Guard, Next, Params, Query, Reply};
use serde_json::{json, Map, Value};

/// Bumped on any change of the messages below; nixconvoy sends the version
/// it speaks.
pub const PROTOCOL: u64 = 1;

fn field<'a>(v: &'a Value, k: &str) -> Result<&'a Value, String> {
    v.get(k).ok_or_else(|| format!("missing {k} in {v}"))
}

fn text(v: &Value, k: &str) -> Result<String, String> {
    field(v, k)?.as_str().map(str::to_string).ok_or_else(|| format!("{k} is not a string"))
}

fn flag(v: &Value, k: &str) -> Result<bool, String> {
    field(v, k)?.as_bool().ok_or_else(|| format!("{k} is not a boolean"))
}

pub fn params(line: &str) -> Result<Params, String> {
    let v: Value = serde_json::from_str(line).map_err(|e| e.to_string())?;
    let protocol = field(&v, "protocol")?.as_u64();
    if protocol != Some(PROTOCOL) {
        return Err(format!("deploy-switch speaks protocol {PROTOCOL}, nixconvoy {protocol:?}"));
    }
    Ok(Params {
        target: text(&v, "target")?,
        toplevel: text(&v, "toplevel")?,
        expected: text(&v, "expected")?,
        place: text(&v, "place")?,
        quiet: flag(&v, "quiet")?,
        router: flag(&v, "router")?,
        fail_verify: flag(&v, "fail_verify")?,
    })
}

pub fn reply(line: &str) -> Result<Reply, String> {
    let v: Value = serde_json::from_str(line).map_err(|e| e.to_string())?;
    let o = v.as_object().ok_or("a reply is an object")?;
    let int = |k: &str| -> Result<i64, String> { field(&v, k)?.as_i64().ok_or_else(|| format!("{k} is not an integer")) };
    let opt = |k: &str| -> Result<Option<String>, String> {
        match field(&v, k)? {
            Value::Null => Ok(None),
            Value::String(s) => Ok(Some(s.clone())),
            _ => Err(format!("{k} is not a string or null")),
        }
    };
    Ok(if let Some(g) = o.get("guard") {
        Reply::Guard(match g.as_str() {
            Some("locked") => Guard::Locked,
            Some("moved") => Guard::Moved(text(&v, "running")?),
            Some("failed") => Guard::Failed(text(&v, "line")?),
            _ => return Err(format!("bad guard reply {v}")),
        })
    } else if o.contains_key("to") {
        Reply::Said { code: int("code")? as i32, to: opt("to")? }
    } else if o.contains_key("code") {
        Reply::Code(int("code")? as i32)
    } else if o.contains_key("text") {
        Reply::Text(text(&v, "text")?)
    } else if o.contains_key("now") {
        Reply::Now(int("now")?.max(0) as u64)
    } else if o.contains_key("running") {
        Reply::Running(opt("running")?)
    } else if o.contains_key("check") {
        Reply::Check { code: int("check")? as i32, last: opt("last")? }
    } else if o.contains_key("flag") {
        Reply::Flag(flag(&v, "flag")?)
    } else if o.contains_key("done") {
        Reply::Done
    } else {
        return Err(format!("unknown reply {v}"));
    })
}

fn effect(e: &Effect) -> Value {
    match e {
        Effect::Print(s) => json!({ "print": s }),
        Effect::Armed => json!({ "armed": true }),
        Effect::Disarmed => json!({ "armed": false }),
        Effect::Mark(s) => json!({ "mark": s }),
    }
}

fn next(n: &Next) -> Value {
    match n {
        Next::Done => json!({ "done": true }),
        Next::Die(m) => json!({ "die": m }),
        Next::Ask(q) => {
            let mut o = Map::new();
            let ask = match q {
                Query::Guard => "guard",
                Query::ProbeHelper => "probe_helper",
                Query::SetProfile => "set_profile",
                Query::Activate(a) => {
                    o.insert("action".into(), json!(a));
                    "activate"
                }
                Query::Deadline => "deadline",
                Query::Arm => "arm",
                Query::Helper(args) => {
                    o.insert("args".into(), json!(args));
                    "helper"
                }
                Query::Now => "now",
                Query::Running { timeout_ms } => {
                    o.insert("timeout_ms".into(), json!(timeout_ms));
                    "running"
                }
                Query::Check { timeout_ms } => {
                    o.insert("timeout_ms".into(), json!(timeout_ms));
                    "check"
                }
                Query::Internet { timeout_ms } => {
                    o.insert("timeout_ms".into(), json!(timeout_ms));
                    "internet"
                }
                Query::Sleep { ms } => {
                    o.insert("ms".into(), json!(ms));
                    "sleep"
                }
            };
            o.insert("ask".into(), json!(ask));
            Value::Object(o)
        }
    }
}

pub fn step(s: &super::Step) -> String {
    json!({ "effects": s.effects.iter().map(effect).collect::<Vec<_>>(), "next": next(&s.next) }).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let p = params(
            r#"{"protocol":1,"target":"h","toplevel":"/t","expected":"/e","place":"local","quiet":false,"router":true,"fail_verify":false}"#,
        )
        .unwrap();
        assert!(p.router && p.place == "local");
        assert!(params(r#"{"protocol":0}"#).is_err());
        assert_eq!(reply(r#"{"guard":"moved","running":"/x"}"#).unwrap(), Reply::Guard(Guard::Moved("/x".into())));
        assert_eq!(reply(r#"{"check":-9,"last":null}"#).unwrap(), Reply::Check { code: -9, last: None });
        assert_eq!(reply(r#"{"running":null}"#).unwrap(), Reply::Running(None));
        assert_eq!(reply(r#"{"code":0,"to":"/p"}"#).unwrap(), Reply::Said { code: 0, to: Some("/p".into()) });
        let s = super::super::Step { effects: vec![Effect::Armed], next: Next::Ask(Query::Check { timeout_ms: 3000 }) };
        assert_eq!(step(&s), r#"{"effects":[{"armed":true}],"next":{"ask":"check","timeout_ms":3000}}"#);
    }
}
