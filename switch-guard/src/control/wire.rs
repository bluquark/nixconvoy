//! nixconvoy-switch's protocol with nixconvoy: JSON lines (the encoding is
//! crate::wire's).  nixconvoy writes the `Params`, then one `Reply` per
//! query; the binary answers each with a `Step`.
use super::{Next, Params, Reply, Step};

pub fn params(line: &str) -> Result<Params, String> {
    crate::wire::params(line)
}

pub fn reply(line: &str) -> Result<Reply, String> {
    crate::wire::reply(line)
}

/// The step's line, and whether it asks a query.
pub fn step(s: &Step) -> (String, bool) {
    (crate::wire::line(s), matches!(s.next, Next::Ask(_)))
}

#[cfg(test)]
mod tests {
    use super::super::{Effect, Guard, Query};
    use super::*;

    #[test]
    fn round_trip() {
        let p = params(
            r#"{"target":"h","toplevel":"/t","expected":"/e","place":"local","quiet":false,"routing_check":true,"fail_verify":false}"#,
        )
        .unwrap();
        assert!(p.routing_check && p.place == "local");
        assert!(params(r#"{}"#).is_err() && params("[]").is_err() && reply("1").is_err());
        assert_eq!(reply(r#"{"guard":{"moved":"/x"}}"#).unwrap(), Reply::Guard(Guard::Moved("/x".into())));
        assert_eq!(reply(r#"{"guard":"locked"}"#).unwrap(), Reply::Guard(Guard::Locked));
        assert_eq!(reply(r#"{"checked":{"code":-9,"last":null}}"#).unwrap(), Reply::Checked { code: -9, last: None });
        assert_eq!(reply(r#"{"system":{"system":null,"timed_out":true}}"#).unwrap(), Reply::System { system: None, timed_out: true });
        assert_eq!(reply(r#"{"reached":{"ok":false,"timed_out":false}}"#).unwrap(), Reply::Reached { ok: false, timed_out: false });
        assert_eq!(reply(r#"{"said":{"code":0,"to":"/p"}}"#).unwrap(), Reply::Said { code: 0, to: Some("/p".into()) });
        assert_eq!(reply(r#""done""#).unwrap(), Reply::Done);
        assert!(reply(r#""start""#).is_err());
        let s = Step { effects: vec![Effect::Armed], next: Next::Ask(Query::Check { timeout_ms: 3000 }) };
        assert_eq!(step(&s), (r#"{"effects":["armed"],"next":{"ask":{"check":{"timeout_ms":3000}}}}"#.to_string(), true));
        let s = Step { effects: vec![], next: Next::Ask(Query::Activate) };
        assert_eq!(step(&s).0, r#"{"effects":[],"next":{"ask":"activate"}}"#);
        let s = Step { effects: vec![Effect::Print("p".into())], next: Next::Done };
        assert_eq!(step(&s), (r#"{"effects":[{"print":"p"}],"next":"done"}"#.to_string(), false));
    }
}
