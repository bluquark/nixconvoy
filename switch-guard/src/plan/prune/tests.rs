//! The prune core's decisions one by one, against scripted answers.
use super::*;
use crate::plan::{drive, Replay, Reply};

fn params(named: bool) -> Params {
    Params { name: "agent-x".into(), named, own_session_ends: false }
}

/// Answers for a worktree holding nothing of its own, locked by dead
/// session 7, with `over` overriding some.
fn run(p: Params, over: impl Fn(&Query) -> Option<Reply>) -> (Vec<Query>, Outcome) {
    let (asked, _, out) = drive(Replay::new(p, program), 40, |q| {
        over(q).unwrap_or_else(|| match q {
            Query::Lock => Reply::Text(Some("claude agent agent-x (pid 7 start 9)".into())),
            Query::CurrentBranch => Reply::Text(Some("worktree-agent-x".into())),
            Query::OwnBranch => Reply::Text(Some("h-deploys3-agent-x".into())),
            Query::OpInProgress | Query::Rev { .. } => Reply::Text(None),
            Query::TakeDeployLock | Query::OnOrigin | Query::ClaimLock { .. } => Reply::Flag(true),
            Query::Fetch | Query::RemoveWorktree | Query::RestoreLock | Query::DropBranches { .. } | Query::ReleaseDeployLock => {
                Reply::Ran { ok: true, detail: String::new() }
            }
            _ => Reply::Flag(false),
        })
    });
    (asked, out.expect("prune never dies"))
}

fn kept(o: &Outcome) -> &str {
    match o {
        Outcome::Kept(why) => why,
        Outcome::Removed => panic!("removed"),
    }
}

#[test]
fn parses_the_session() {
    assert_eq!(session("claude agent a (pid 12 start 34)"), Some((12, Some(34))));
    assert_eq!(session("claude agent a (pid 12)"), Some((12, None)));
    assert_eq!(session("pid 5"), Some((5, None)));
    assert_eq!(session("rapid 5"), None);
    assert_eq!(session("kept for a look"), None);
    assert_eq!(session("pid 99999999999"), None);
}

#[test]
fn removes_an_empty_dead_one() {
    let (asked, o) = run(params(false), |_| None);
    assert_eq!(o, Outcome::Removed);
    assert_eq!(asked.first(), Some(&Query::TakeDeployLock));
    assert_eq!(asked.last(), Some(&Query::ReleaseDeployLock));
    assert!(asked.contains(&Query::Alive { pid: 7, start: Some(9), own_ends: false }));
    assert!(asked.contains(&Query::ClaimLock { expect: Some("claude agent agent-x (pid 7 start 9)".into()) }));
    assert!(asked.contains(&Query::DropBranches { wb: "h-deploys3-agent-x".into(), cur: Some("worktree-agent-x".into()) }));
}

#[test]
fn keeps_and_says_why() {
    let cases: Vec<(Query, Reply, &str)> = vec![
        (Query::TakeDeployLock, Reply::Flag(false), "a nixconvoy runs in it"),
        (Query::Lock, Reply::Text(Some("kept for a look".into())), "locked by hand (kept for a look)"),
        (Query::Lock, Reply::Text(None), "unlocked: no session to outlive"),
        (
            Query::Alive { pid: 7, start: Some(9), own_ends: false },
            Reply::Flag(true),
            "the claude session that spawned its agent still runs",
        ),
        (Query::OpInProgress, Reply::Text(Some("rebase".into())), "a rebase is in progress"),
        (Query::PushInFlight, Reply::Flag(true), "a push of its work branch is in flight"),
        (Query::Nested, Reply::Flag(true), "another worktree lives inside it"),
        (Query::OwnBranch, Reply::Text(None), "it owns no work branch"),
        (Query::CurrentBranch, Reply::Text(Some("h-deploys3-agent-x".into())), "on its work branch h-deploys3-agent-x"),
        (Query::CurrentBranch, Reply::Text(Some("h-deploys3".into())), "SKIP"),
        (Query::Fetch, Reply::Ran { ok: false, detail: "offline".into() }, "origin could not be fetched"),
        (Query::Dirty, Reply::Flag(true), "it has uncommitted or untracked changes"),
        (Query::OnOrigin, Reply::Flag(false), "it carries commits not on origin"),
        (
            Query::ClaimLock { expect: Some("claude agent agent-x (pid 7 start 9)".into()) },
            Reply::Flag(false),
            "its lock changed meanwhile",
        ),
        (Query::RemoveWorktree, Reply::Ran { ok: false, detail: "is locked".into() }, "git refused to remove it:\nis locked"),
    ];
    for (q, r, why) in cases {
        if why == "SKIP" {
            continue;
        }
        let (asked, o) = run(params(false), |x| (*x == q).then(|| r.clone()));
        assert!(kept(&o).starts_with(why), "{q:?}: {o:?}");
        assert!(!asked.contains(&Query::RemoveWorktree) || matches!(q, Query::RemoveWorktree), "{q:?} still removed");
        let took = asked.first() == Some(&Query::TakeDeployLock) && q != Query::TakeDeployLock;
        assert_eq!(asked.last() == Some(&Query::ReleaseDeployLock), took, "{q:?}: {asked:?}");
        if q == Query::RemoveWorktree {
            assert!(asked.contains(&Query::RestoreLock));
        }
    }
}

#[test]
fn origin_and_the_local_work_branch() {
    let wb = "h-deploys3-agent-x";
    let tip = |q: &Query| match q {
        Query::Rev { name } if name == &format!("refs/remotes/origin/{wb}") => Some(Reply::Text(Some("t".into()))),
        _ => None,
    };
    let (_, o) = run(params(false), |q| tip(q).or_else(|| matches!(q, Query::Contains { .. }).then_some(Reply::Flag(false))));
    assert!(kept(&o).contains("carries pushed increments"));
    let (_, o) = run(params(false), |q| tip(q).or_else(|| matches!(q, Query::Contains { .. }).then_some(Reply::Flag(true))));
    assert_eq!(o, Outcome::Removed);
    let local = |q: &Query| match q {
        Query::Rev { name } if name == &format!("refs/heads/{wb}") => Some(Reply::Text(Some("l".into()))),
        Query::Unpushed { sha } if sha == "l" => Some(Reply::Flag(true)),
        _ => None,
    };
    let (_, o) = run(params(false), local);
    assert!(kept(&o).starts_with(&format!("it owns {wb}, which carries commits not on origin")));
    let (_, o) = run(params(false), |q| (*q == Query::CurrentBranch).then(|| Reply::Text(Some("other-deploys1".into()))));
    assert_eq!(o, Outcome::Removed);
}

#[test]
fn named_and_own_session() {
    let (asked, o) = run(params(true), |q| (*q == Query::Lock).then_some(Reply::Text(None)));
    assert_eq!(o, Outcome::Removed);
    assert!(asked.contains(&Query::ClaimLock { expect: None }));
    let p = Params { own_session_ends: true, ..params(true) };
    let (asked, _) = run(p, |_| None);
    assert!(asked.contains(&Query::Alive { pid: 7, start: Some(9), own_ends: true }));
}
