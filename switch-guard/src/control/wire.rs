//! nixconvoy-switch's protocol with nixconvoy: JSON lines.  nixconvoy writes
//! the parameters, then one reply per query; the binary answers each with
//! `{"effects": [...], "next": {...}}`.
use super::{Effect, Guard, Next, Params, Query, Reply};
use crate::wire::{field, flag, int, opt_text, text};
use serde_json::{json, Map, Value};

/// Bumped on any change of the messages below; nixconvoy sends the version
/// it speaks.
pub const PROTOCOL: u64 = 2;

pub fn params(line: &str) -> Result<Params, String> {
    let v = crate::wire::params(line, "nixconvoy-switch", PROTOCOL)?;
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
    let v = crate::wire::reply(line)?;
    let has = |k: &str| v.get(k).is_some();
    let opt = |k: &str| opt_text(field(&v, k)?);
    Ok(if let Some(g) = v.get("guard") {
        Reply::Guard(match g.as_str() {
            Some("locked") => Guard::Locked,
            Some("moved") => Guard::Moved(text(&v, "running")?),
            Some("failed") => Guard::Failed(text(&v, "line")?),
            _ => return Err(format!("bad guard reply {v}")),
        })
    } else if has("to") {
        Reply::Said { code: int(&v, "code")? as i32, to: opt("to")? }
    } else if has("code") {
        Reply::Code(int(&v, "code")? as i32)
    } else if has("text") {
        Reply::Text(text(&v, "text")?)
    } else if has("now") {
        Reply::Now(int(&v, "now")?.max(0) as u64)
    } else if has("running") {
        Reply::Running(opt("running")?)
    } else if has("check") {
        Reply::Check { code: int(&v, "check")? as i32, last: opt("last")? }
    } else if has("flag") {
        Reply::Flag(flag(&v, "flag")?)
    } else if has("done") {
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
                Query::Activate => "activate",
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

/// The step's line, and whether it asks a query.
pub fn step(s: &super::Step) -> (String, bool) {
    let line = json!({ "effects": s.effects.iter().map(effect).collect::<Vec<_>>(), "next": next(&s.next) }).to_string();
    (line, matches!(s.next, Next::Ask(_)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let p = params(
            r#"{"protocol":2,"target":"h","toplevel":"/t","expected":"/e","place":"local","quiet":false,"router":true,"fail_verify":false}"#,
        )
        .unwrap();
        assert!(p.router && p.place == "local");
        assert!(params(r#"{"protocol":0}"#).is_err());
        assert_eq!(reply(r#"{"guard":"moved","running":"/x"}"#).unwrap(), Reply::Guard(Guard::Moved("/x".into())));
        assert_eq!(reply(r#"{"check":-9,"last":null}"#).unwrap(), Reply::Check { code: -9, last: None });
        assert_eq!(reply(r#"{"running":null}"#).unwrap(), Reply::Running(None));
        assert_eq!(reply(r#"{"code":0,"to":"/p"}"#).unwrap(), Reply::Said { code: 0, to: Some("/p".into()) });
        let s = super::super::Step { effects: vec![Effect::Armed], next: Next::Ask(Query::Check { timeout_ms: 3000 }) };
        assert_eq!(step(&s), (r#"{"effects":[{"armed":true}],"next":{"ask":"check","timeout_ms":3000}}"#.to_string(), true));
        let s = super::super::Step { effects: vec![], next: Next::Ask(Query::Activate) };
        assert_eq!(step(&s).0, r#"{"effects":[],"next":{"ask":"activate"}}"#);
    }
}
