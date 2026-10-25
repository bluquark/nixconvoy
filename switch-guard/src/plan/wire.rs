//! nixconvoy-plan's protocol with nixconvoy: JSON lines, as nixconvoy-switch's
//! (control/wire.rs).  nixconvoy writes the core's name and parameters,
//! then one reply per query; the binary answers each with
//! `{"effects": [...], "next": {"ask": ...} | {"done": ...} | {"die": ...}}`.
use super::{drift, prune, rebase, union, Effect, Next, Query, Replay, Reply, Step};
use crate::wire::{field, flag, int, map_of, opt_text, string_lists, string_map, strings, text};
use serde_json::{json, Map, Value};

/// Bumped on any change of the messages below; nixconvoy sends the version
/// it speaks.
pub const PROTOCOL: u64 = 11;

fn pairs(v: &Value) -> Result<Vec<(String, String)>, String> {
    v.as_array()
        .ok_or("refs is not a list")?
        .iter()
        .map(|p| match p.as_array().map(Vec::as_slice) {
            Some([Value::String(a), Value::String(b)]) => Ok((a.clone(), b.clone())),
            _ => Err(format!("bad ref pair {p}")),
        })
        .collect()
}

fn drift_params(v: &Value) -> Result<drift::Params, String> {
    let me = strings(v, "me")?;
    let [machine, checkout] = me.as_slice() else { return Err("me is not [machine, checkout]".into()) };
    Ok(drift::Params {
        cx: drift::Context {
            me: (machine.clone(), checkout.clone()),
            listed: strings(v, "listed")?.into_iter().collect(),
            tips: string_map(v, "tips")?,
            in_flight: string_map(v, "in_flight")?,
            dead: strings(v, "dead")?.into_iter().collect(),
            tz_offset: int(v, "tz_offset")?,
        },
        union: text(v, "union")?,
        router: text(v, "router")?,
        targets: strings(v, "targets")?,
        candidates: strings(v, "candidates")?,
        evict: flag(v, "evict")?,
        named: flag(v, "named")?,
        work_branch: flag(v, "work_branch")?,
        reached: strings(v, "reached")?.into_iter().collect(),
        running: map_of(v, "running", opt_text)?,
        built: string_map(v, "built")?,
        held: string_lists(v, "held")?,
        provenances: string_map(v, "provenances")?,
    })
}

fn rebase_params(v: &Value) -> Result<rebase::Params, String> {
    Ok(match text(v, "mode")?.as_str() {
        "pop_tmp" => rebase::Params::PopTmp,
        "ensure_rebased" => rebase::Params::EnsureRebased,
        "sync" => rebase::Params::Sync { wb: opt_text(field(v, "wb")?)? },
        "onto" => rebase::Params::Onto { upstream: text(v, "upstream")? },
        "on_work_branch" => rebase::Params::OnWorkBranch { wb: text(v, "wb")? },
        m => return Err(format!("unknown rebase mode {m}")),
    })
}

fn prune_params(v: &Value) -> Result<prune::Params, String> {
    let stopped = match v.get("stopped").unwrap_or(&Value::Null) {
        Value::Null => None,
        st => Some(prune::Stopped {
            pid: u32::try_from(field(st, "pid")?.as_u64().ok_or("pid is not a number")?).map_err(|e| e.to_string())?,
            lock: opt_text(field(st, "lock")?)?,
        }),
    };
    Ok(prune::Params { name: text(v, "name")?, named: flag(v, "named")?, own_session_ends: flag(v, "own_session_ends")?, stopped })
}

fn union_params(v: &Value) -> Result<union::Params, String> {
    Ok(union::Params { wb: text(v, "wb")?, evict: flag(v, "evict")?, main0: opt_text(field(v, "main0")?)? })
}

pub fn reply(line: &str) -> Result<Reply, String> {
    let v = crate::wire::reply(line)?;
    let has = |k: &str| v.get(k).is_some();
    Ok(if has("flag") {
        Reply::Flag(flag(&v, "flag")?)
    } else if let Some(t) = v.get("text") {
        Reply::Text(opt_text(t)?)
    } else if has("ran") {
        Reply::Ran { ok: flag(&v, "ran")?, detail: text(&v, "detail")? }
    } else if let Some(r) = v.get("refs") {
        Reply::Refs(pairs(r)?)
    } else if has("names") {
        Reply::Names(strings(&v, "names")?)
    } else if has("merge") {
        Reply::Merge { ok: flag(&v, "merge")?, files: strings(&v, "files")?, detail: text(&v, "detail")? }
    } else {
        return Err(format!("unknown reply {v}"));
    })
}

pub fn query(q: &Query) -> Value {
    let s = |x: &str| json!(x);
    let (ask, fields): (&str, Vec<(&str, Value)>) = match q {
        Query::Contains { a, b } => ("contains", vec![("a", s(a)), ("b", s(b))]),
        Query::OpInProgress => ("op_in_progress", vec![]),
        Query::CurrentBranch => ("current_branch", vec![]),
        Query::Detach { upstream } => ("detach", vec![("upstream", s(upstream))]),
        Query::Pushed => ("pushed", vec![]),
        Query::OnOrigin => ("on_origin", vec![]),
        Query::StagedDiffers => ("staged_differs", vec![]),
        Query::Commit { msg } => ("commit", vec![("msg", s(msg))]),
        Query::AddAll => ("add_all", vec![]),
        Query::Pop { mixed } => ("pop", vec![("mixed", json!(mixed))]),
        Query::HeadSubject => ("head_subject", vec![]),
        Query::Rebase { keep_empty, aside, upstream } => {
            ("rebase", vec![("keep_empty", json!(keep_empty)), ("aside", json!(aside)), ("upstream", s(upstream))])
        }
        Query::RebaseAbort { aside } => ("rebase_abort", vec![("aside", json!(aside))]),
        Query::Rev { name } => ("rev", vec![("name", s(name))]),
        Query::Fetch => ("fetch", vec![]),
        Query::Push { branch } => ("push", vec![("branch", s(branch))]),
        Query::AddWorktree { branch } => ("add_worktree", vec![("branch", s(branch))]),
        Query::DropWorktree => ("drop_worktree", vec![]),
        Query::Switch { branch, start } => ("switch", vec![("branch", s(branch)), ("start", json!(start))]),
        Query::Upstream { branch } => ("upstream", vec![("branch", s(branch))]),
        Query::Fate { branch, tip, union, checkout } => {
            ("fate", vec![("branch", s(branch)), ("tip", s(tip)), ("union", s(union)), ("checkout", json!(checkout))])
        }
        Query::SetUpstream { branch } => ("set_upstream", vec![("branch", s(branch))]),
        Query::Listed => ("listed", vec![]),
        Query::Branches => ("branches", vec![]),
        Query::Unchanged { sha } => ("unchanged", vec![("sha", s(sha))]),
        Query::Worktree => ("worktree", vec![]),
        Query::Summary { sha } => ("summary", vec![("sha", s(sha))]),
        Query::Merge { name, sha } => ("merge", vec![("name", s(name)), ("sha", s(sha))]),
        Query::DropFile { file } => ("drop_file", vec![("file", s(file))]),
        Query::Solve { file } => ("solve", vec![("file", s(file))]),
        Query::Add { file } => ("add", vec![("file", s(file))]),
        Query::AbortMerge => ("abort_merge", vec![]),
        Query::CommitMerge => ("commit_merge", vec![]),
        Query::StripTree => ("strip_tree", vec![]),
        Query::Head => ("head", vec![]),
        Query::HeadTree => ("head_tree", vec![]),
        Query::CommitUnion { tree } => ("commit_union", vec![("tree", s(tree))]),
        Query::Checkout { union } => ("checkout", vec![("union", s(union))]),
        Query::CollectFetch => ("collect_fetch", vec![]),
        Query::Refresh => ("refresh", vec![]),
        Query::AwaitPending { union } => ("await_pending", vec![("union", s(union))]),
        Query::Stage { union, listed, branches } => {
            ("stage", vec![("union", s(union)), ("listed", json!(listed)), ("branches", json!(branches))])
        }
        Query::TakeNameLock => ("take_name_lock", vec![]),
        Query::ReleaseNameLock => ("release_name_lock", vec![]),
        Query::TakeDeployLock => ("take_deploy_lock", vec![]),
        Query::ReleaseDeployLock => ("release_deploy_lock", vec![]),
        Query::Lock => ("lock", vec![]),
        Query::Alive { pid, start, own_ends } => {
            ("alive", vec![("pid", json!(pid)), ("start", json!(start)), ("own_ends", json!(own_ends))])
        }
        Query::PushInFlight => ("push_in_flight", vec![]),
        Query::Nested => ("nested", vec![]),
        Query::OwnBranch => ("own_branch", vec![]),
        Query::Unpushed { sha } => ("unpushed", vec![("sha", s(sha))]),
        Query::Dirty => ("dirty", vec![]),
        Query::Occupied => ("occupied", vec![]),
        Query::RemoveWorktree => ("remove_worktree", vec![]),
        Query::DropBranches { wb, cur } => ("drop_branches", vec![("wb", s(wb)), ("cur", json!(cur))]),
    };
    let mut o: Map<String, Value> = fields.into_iter().map(|(k, v)| (k.to_string(), v)).collect();
    o.insert("ask".into(), json!(ask));
    Value::Object(o)
}

fn effect(e: &Effect) -> Value {
    match e {
        Effect::Print(s) => json!({ "print": s }),
        Effect::Warn(s) => json!({ "warn": s }),
        Effect::Mark(s) => json!({ "mark": s }),
    }
}

fn step<O>(s: Step<O>, done: impl FnOnce(O) -> Value) -> (String, bool) {
    let (next, more) = match s.next {
        Next::Ask(q) => (query(&q), true),
        Next::Done(o) => (json!({ "done": done(o) }), false),
        Next::Die(m) => (json!({ "die": m }), false),
    };
    let fx: Vec<Value> = s.effects.iter().map(effect).collect();
    (json!({ "effects": fx, "next": next }).to_string(), more)
}

pub enum Core {
    Drift(Box<Replay<drift::Params, drift::Outcome>>),
    Rebase(Replay<rebase::Params, rebase::Outcome>),
    Union(Replay<union::Params, union::Outcome>),
    Prune(Replay<prune::Params, prune::Outcome>),
}

pub fn start(line: &str) -> Result<Core, String> {
    let v = crate::wire::params(line, "nixconvoy-plan", PROTOCOL)?;
    Ok(match text(&v, "core")?.as_str() {
        "drift" => Core::Drift(Box::new(Replay::new(drift_params(&v)?, drift::program))),
        "rebase" => Core::Rebase(Replay::new(rebase_params(&v)?, rebase::program)),
        "union" => Core::Union(Replay::new(union_params(&v)?, union::program)),
        "prune" => Core::Prune(Replay::new(prune_params(&v)?, prune::program)),
        c => return Err(format!("unknown core {c}")),
    })
}

impl Core {
    /// The next step's line, and whether it asks a query.
    pub fn decide(&mut self, reply: Option<Reply>) -> (String, bool) {
        match self {
            Core::Drift(m) => step(m.decide(reply), |o| {
                let drift: Map<String, Value> = o.drift.into_iter().map(|(h, (k, why))| (h, json!([k.name(), why]))).collect();
                json!({ "affected": o.affected, "plan": o.plan, "drift": drift })
            }),
            Core::Rebase(m) => step(m.decide(reply), |o| match o {
                Ok(status) => json!({ "ok": true, "status": status }),
                Err(e) => json!({ "ok": false, "status": e }),
            }),
            Core::Union(m) => step(
                m.decide(reply),
                |o| json!({ "union": o.union, "branches": o.branches.iter().map(|(n, s)| json!([n, s])).collect::<Vec<_>>() }),
            ),
            Core::Prune(m) => step(m.decide(reply), |o| match o {
                prune::Outcome::Removed => json!({ "removed": true, "kept": null }),
                prune::Outcome::Kept(why) => json!({ "removed": false, "kept": why }),
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        assert!(start(r#"{"protocol":0,"core":"rebase","mode":"pop_tmp"}"#).is_err());
        let mut c = start(r#"{"protocol":11,"core":"rebase","mode":"pop_tmp"}"#).unwrap();
        assert_eq!(c.decide(None), (r#"{"effects":[],"next":{"ask":"head_subject"}}"#.to_string(), true));
        assert_eq!(reply(r#"{"text":null}"#).unwrap(), Reply::Text(None));
        let (line, more) = c.decide(Some(Reply::Text(None)));
        assert_eq!(line, r#"{"effects":[],"next":{"done":{"ok":true,"status":""}}}"#);
        assert!(!more);
        assert_eq!(rebase_params(&serde_json::json!({"mode": "sync", "wb": null})), Ok(rebase::Params::Sync { wb: None }));
        assert_eq!(
            query(&Query::Detach { upstream: super::super::MAIN.into() }).to_string(),
            r#"{"ask":"detach","upstream":"refs/remotes/origin/main"}"#
        );
        assert_eq!(
            rebase_params(&serde_json::json!({"mode": "onto", "upstream": "refs/remotes/origin/dev"})),
            Ok(rebase::Params::Onto { upstream: "refs/remotes/origin/dev".into() })
        );
        assert_eq!(reply(r#"{"refs":[["a","b"]]}"#).unwrap(), Reply::Refs(vec![("a".into(), "b".into())]));
        assert_eq!(
            reply(r#"{"merge":false,"files":["x"],"detail":"d"}"#).unwrap(),
            Reply::Merge { ok: false, files: vec!["x".into()], detail: "d".into() }
        );
        assert_eq!(reply(r#"{"ran":true,"detail":""}"#).unwrap(), Reply::Ran { ok: true, detail: String::new() });
        assert_eq!(query(&Query::Switch { branch: "b".into(), start: None }).to_string(), r#"{"ask":"switch","branch":"b","start":null}"#);
        let mut d = start(r#"{"protocol":11,"core":"drift","me":["h","/c"],"union":"u","listed":["main"],"tips":{},"in_flight":{},"dead":[],"tz_offset":0,"router":"choko","targets":["a"],"candidates":["a"],"evict":false,"named":false,"work_branch":true,"reached":[],"running":{"a":"/old"},"built":{"a":"/new"},"provenances":{"a":""},"held":{}}"#).unwrap();
        let (line, _) = d.decide(None);
        assert_eq!(
            line,
            r#"{"effects":[{"print":"  a: will switch"}],"next":{"done":{"affected":["a"],"drift":{"a":["untracked",""]},"plan":["a"]}}}"#
        );
        let mut u = start(r#"{"protocol":11,"core":"union","wb":"w","evict":true,"main0":null}"#).unwrap();
        assert_eq!(u.decide(None).0, r#"{"effects":[{"mark":"union"}],"next":{"ask":"listed"}}"#);
        let mut p =
            start(r#"{"protocol":11,"core":"prune","name":"agent-x","named":false,"own_session_ends":false,"stopped":null}"#).unwrap();
        assert_eq!(p.decide(None).0, r#"{"effects":[],"next":{"ask":"lock"}}"#);
        assert_eq!(
            p.decide(Some(Reply::Text(Some("pid 7".into())))).0,
            r#"{"effects":[],"next":{"ask":"alive","own_ends":false,"pid":7,"start":null}}"#
        );
        assert_eq!(p.decide(Some(Reply::Flag(false))).0, r#"{"effects":[],"next":{"ask":"fetch"}}"#);
        assert_eq!(p.decide(Some(Reply::Ran { ok: true, detail: String::new() })).0, r#"{"effects":[],"next":{"ask":"take_name_lock"}}"#);
        assert_eq!(p.decide(Some(Reply::Flag(true))).0, r#"{"effects":[],"next":{"ask":"take_deploy_lock"}}"#);
        assert_eq!(p.decide(Some(Reply::Flag(false))).0, r#"{"effects":[],"next":{"ask":"release_name_lock"}}"#);
        assert_eq!(
            p.decide(Some(Reply::Ran { ok: true, detail: String::new() })).0,
            r#"{"effects":[],"next":{"done":{"kept":"a nixconvoy runs in it","removed":false}}}"#
        );
        assert_eq!(query(&Query::Occupied).to_string(), r#"{"ask":"occupied"}"#);
        assert_eq!(query(&Query::ReleaseNameLock).to_string(), r#"{"ask":"release_name_lock"}"#);
        assert_eq!(
            query(&Query::Alive { pid: 7, start: None, own_ends: false }).to_string(),
            r#"{"ask":"alive","own_ends":false,"pid":7,"start":null}"#
        );
    }
}
