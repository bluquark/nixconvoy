//! nixconvoy-plan's protocol with nixconvoy: JSON lines, as nixconvoy-switch's
//! (control/wire.rs; the encoding is crate::wire's).  nixconvoy writes
//! `{"<core>": <its Params>}`, then one `Reply` per query; the binary
//! answers each with a `Step`.
use super::{drift, prune, rebase, union, Next, Replay, Reply, Step};
use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Params {
    Drift(Box<drift::Params>),
    Rebase(rebase::Params),
    Union(union::Params),
    Prune(prune::Params),
}

pub enum Core {
    Drift(Box<Replay<drift::Params, drift::Outcome>>),
    Rebase(Replay<rebase::Params, rebase::Outcome>),
    Union(Replay<union::Params, union::Outcome>),
    Prune(Replay<prune::Params, prune::Outcome>),
}

pub fn start(line: &str) -> Result<Core, String> {
    Ok(match crate::wire::params(line)? {
        Params::Drift(p) => Core::Drift(Box::new(Replay::new(*p, drift::program))),
        Params::Rebase(p) => Core::Rebase(Replay::new(p, rebase::program)),
        Params::Union(p) => Core::Union(Replay::new(p, union::program)),
        Params::Prune(p) => Core::Prune(Replay::new(p, prune::program)),
    })
}

pub fn reply(line: &str) -> Result<Reply, String> {
    crate::wire::reply(line)
}

fn step<O: Serialize>(s: Step<O>) -> (String, bool) {
    (crate::wire::line(&s), matches!(s.next, Next::Ask(_)))
}

impl Core {
    /// The next step's line, and whether it asks a query.
    pub fn decide(&mut self, reply: Option<Reply>) -> (String, bool) {
        match self {
            Core::Drift(m) => step(m.decide(reply)),
            Core::Rebase(m) => step(m.decide(reply)),
            Core::Union(m) => step(m.decide(reply)),
            Core::Prune(m) => step(m.decide(reply)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::Query;
    use super::*;

    fn enc<T: Serialize>(v: &T) -> String {
        crate::wire::line(v)
    }

    #[test]
    fn round_trip() {
        assert!(start(r#"{"nope":"pop_tmp"}"#).is_err() && start(r#"{"rebase":{"op":"pop_tmp"}}"#).is_err() && start("[]").is_err());
        let mut c = start(r#"{"rebase":{"names":{"remote":"origin","main":"main"},"op":"pop_tmp"}}"#).unwrap();
        assert_eq!(c.decide(None), (r#"{"effects":[],"next":{"ask":"head_subject"}}"#.to_string(), true));
        assert_eq!(reply(r#"{"text":null}"#).unwrap(), Reply::Text(None));
        let (line, more) = c.decide(Some(Reply::Text(None)));
        assert_eq!(line, r#"{"effects":[],"next":{"done":{"Ok":""}}}"#);
        assert!(!more);
        let p: Params = serde_json::from_value(
            serde_json::json!({"rebase": {"names": {"remote": "origin", "main": "main"}, "op": {"sync": {"wb": null}}}}),
        )
        .unwrap();
        assert!(matches!(p, Params::Rebase(rebase::Params { op: rebase::Op::Sync { wb: None }, .. })));
        let p: Params =
            serde_json::from_value(serde_json::json!({"rebase": {"names": {"remote": "origin", "main": "main"}, "op": {"onto": {"upstream": "refs/remotes/origin/dev"}}}})).unwrap();
        assert!(
            matches!(p, Params::Rebase(rebase::Params { op: rebase::Op::Onto { upstream }, .. }) if upstream == "refs/remotes/origin/dev")
        );
        let p: Params = serde_json::from_value(serde_json::json!({"prune": {"names": {"remote": "up", "main": "trunk"}, "name": "x", "named": true, "own_session_ends": false, "stopped": null}})).unwrap();
        assert!(matches!(p, Params::Prune(prune::Params { names, .. }) if names.main_ref() == "refs/remotes/up/trunk"));
        assert_eq!(enc(&Query::Detach { upstream: super::super::MAIN.into() }), r#"{"detach":{"upstream":"refs/remotes/origin/main"}}"#);
        assert_eq!(reply(r#"{"refs":[["a","b"]]}"#).unwrap(), Reply::Refs(vec![("a".into(), "b".into())]));
        assert_eq!(
            reply(r#"{"merged":{"ok":false,"files":["x"],"detail":"d"}}"#).unwrap(),
            Reply::Merged { ok: false, files: vec!["x".into()], detail: "d".into() }
        );
        assert_eq!(reply(r#"{"ran":{"ok":true,"detail":""}}"#).unwrap(), Reply::Ran { ok: true, detail: String::new() });
        assert!(reply(r#"{"ran":true,"detail":""}"#).is_err());
        assert!(reply(r#"{"flag":"yes"}"#).is_err());
        assert_eq!(enc(&Query::Switch { branch: "b".into(), start: None }), r#"{"switch":{"branch":"b","start":null}}"#);
        let mut d = start(
            r#"{"drift":{"cx":{"names":{"remote":"origin","main":"main"},"me":["h","/c"],"listed":["main"],"tips":{},"in_flight":{},"dead":[],"tz_offset":0},"union":"u","targets":["a"],"candidates":["a"],"evict":false,"named":false,"work_branch":true,"reached":[],"running":{"a":"/old"},"built":{"a":"/new"},"provenances":{"a":""},"held":{}}}"#,
        )
        .unwrap();
        let (line, _) = d.decide(None);
        assert_eq!(
            line,
            r#"{"effects":[{"print":"  a: will switch"}],"next":{"done":{"affected":["a"],"plan":["a"],"drift":{"a":["untracked",""]}}}}"#
        );
        let mut u = start(r#"{"union":{"names":{"remote":"origin","main":"main"},"wb":"w","evict":true,"main0":null}}"#).unwrap();
        assert_eq!(u.decide(None).0, r#"{"effects":[{"mark":"union"}],"next":{"ask":"listed"}}"#);
        let mut p = start(r#"{"prune":{"names":{"remote":"origin","main":"main"},"name":"agent-x","named":false,"own_session_ends":false,"stopped":null}}"#).unwrap();
        assert_eq!(p.decide(None).0, r#"{"effects":[],"next":{"ask":"lock"}}"#);
        assert_eq!(
            p.decide(Some(Reply::Text(Some("pid 7".into())))).0,
            r#"{"effects":[],"next":{"ask":{"alive":{"pid":7,"start":null,"own_ends":false}}}}"#
        );
        assert_eq!(p.decide(Some(Reply::Flag(false))).0, r#"{"effects":[],"next":{"ask":"fetch"}}"#);
        assert_eq!(p.decide(Some(Reply::Ran { ok: true, detail: String::new() })).0, r#"{"effects":[],"next":{"ask":"take_name_lock"}}"#);
        assert_eq!(p.decide(Some(Reply::Flag(true))).0, r#"{"effects":[],"next":{"ask":"take_deploy_lock"}}"#);
        assert_eq!(p.decide(Some(Reply::Flag(false))).0, r#"{"effects":[],"next":{"ask":"release_name_lock"}}"#);
        assert_eq!(
            p.decide(Some(Reply::Ran { ok: true, detail: String::new() })).0,
            r#"{"effects":[],"next":{"done":{"kept":"a nixconvoy runs in it"}}}"#
        );
        assert_eq!(enc(&Query::Occupied), r#""occupied""#);
    }
}
