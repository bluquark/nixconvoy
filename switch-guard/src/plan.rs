//! nixconvoy's decisions about the checkout and the targets as Sans-I/O cores
//! (nixconvoy-plan): which targets a deploy may switch (drift), how a checkout is
//! brought onto origin/main (rebase), how the union of the work branches
//! is built and kept current (union) and whether a subagent worktree is
//! removed (prune).  nixconvoy performs each query - git,
//! a worktree, the eval/build/copy of a union - and feeds the result back.
//!
//! A core is a straight-line program over `Script`: each `ask` returns the
//! reply recorded for it, or halts the program with that query when there is
//! none yet.  `Replay` re-runs the program from the start on every reply, so
//! the program reads like the procedure it decides and holds no state of its
//! own; it is pure, so each re-run retraces the same queries.
pub mod drift;
pub mod prune;
pub mod rebase;
pub mod union;
pub mod wire;

pub const MSG_FILE: &str = "COMMIT_MSG.md";
pub const BRANCHES_FILE: &str = "deploy-branches";
pub const MAIN: &str = "refs/remotes/origin/main";

/// Whether `name` matches one of deploy-branches' patterns, where `*`
/// spans anything.
pub fn listed<'a>(patterns: impl IntoIterator<Item = &'a str>, name: &str) -> bool {
    fn glob(p: &[u8], n: &[u8]) -> bool {
        match p.split_first() {
            None => n.is_empty(),
            Some((b'*', rest)) => (0..=n.len()).any(|i| glob(rest, &n[i..])),
            Some((c, rest)) => n.first() == Some(c) && glob(rest, &n[1..]),
        }
    }
    patterns.into_iter().any(|p| glob(p.as_bytes(), name.as_bytes()))
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Effect {
    Print(String),
    /// To stderr.
    Warn(String),
    /// nixconvoy's PERF phase.
    Mark(String),
}

/// What nixconvoy does for the cores: git in the checkout (or in the
/// worktree a query names), and the union core's callbacks into the deploy.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Query {
    /// Whether `a` is an ancestor of `b` (or equal) -> Flag.
    Contains {
        a: String,
        b: String,
    },
    /// The rebase, merge, cherry-pick or revert in progress -> Text.
    OpInProgress,
    /// The checked-out branch -> Text (None: detached).
    CurrentBranch,
    /// Whether the index differs from HEAD -> Flag.
    StagedDiffers,
    /// Commit the index with this subject -> Ran.
    Commit {
        msg: String,
    },
    /// Stage everything, untracked files included -> Ran.
    AddAll,
    /// Move HEAD to HEAD~1, keeping the index (soft) or not (mixed) -> Ran.
    Pop {
        mixed: bool,
    },
    /// HEAD's subject -> Text.
    HeadSubject,
    /// Rebase onto origin/main, keeping or dropping commits that become
    /// empty, in the checkout or the aside worktree -> Ran.
    Rebase {
        keep_empty: bool,
        aside: bool,
    },
    RebaseAbort {
        aside: bool,
    },
    /// A ref's commit -> Text (None: no such ref).
    Rev {
        name: String,
    },
    Fetch,
    /// Push the local work branch, leased on origin's tip as fetched -> Ran.
    Push {
        branch: String,
    },
    /// A throwaway worktree with `branch` checked out -> Ran.
    AddWorktree {
        branch: String,
    },
    /// Remove the aside worktree, if any -> Ran.
    DropWorktree,
    /// Switch to `branch`, creating it at `start` when given -> Ran.
    Switch {
        branch: String,
        start: Option<String>,
    },
    /// Move the detached HEAD onto origin/main, local changes carried along
    /// -> Ran.
    Detach,
    /// Whether HEAD is already on origin: an ancestor of origin/main or of
    /// origin's tip of a listed work branch -> Flag.
    Pushed,
    /// Whether HEAD holds nothing of its own beyond origin: Pushed, or on a
    /// tip fetched before (a review checkout of a work branch since
    /// squashed or reset), or - that tip since rebased or squashed - its
    /// tree less MSG_FILE an origin/main commit's, or each of its commits
    /// beyond a tip patch-equivalent to one of the tip's -> Flag.
    OnOrigin,
    /// The branch's configured merge ref -> Text.
    Upstream {
        branch: String,
    },
    /// What became of `branch`'s increment at `tip`, which origin's tip of
    /// the branch no longer is: "kept" when origin's branch still carries
    /// it, or `checkout` (the deploying one, on this machine) still has the
    /// branch checked out - its first push on its way, or its switch
    /// failed - or merging it into `union` changes nothing (rebased,
    /// redeployed, landed intact), else "landed" (an origin/main commit
    /// squashed a later tip of it) or "abandoned" -> Text.
    Fate {
        branch: String,
        tip: String,
        union: String,
        checkout: Option<String>,
    },
    /// Point the branch's upstream at origin/main -> Ran.
    SetUpstream {
        branch: String,
    },

    /// deploy-branches as the checkout now has it -> Names.
    Listed,
    /// origin's *-deploys* branches and their tips, in ref order -> Refs.
    Branches,
    /// Whether `sha` changes nothing but MSG_FILE since its merge base with
    /// origin/main -> Flag.
    Unchanged {
        sha: String,
    },
    /// A fresh union worktree on the deploy's snapshot (or HEAD), any
    /// earlier one dropped -> Ran.
    Worktree,
    /// One line naming `sha` for a person -> Text.
    Summary {
        sha: String,
    },
    /// Merge `sha` into the union worktree -> Merge.
    Merge {
        name: String,
        sha: String,
    },
    /// Resolve a conflicted file by deleting it -> Ran.
    DropFile {
        file: String,
    },
    /// mergiraf on a conflicted file -> Flag.
    Solve {
        file: String,
    },
    /// Mark a resolved file -> Ran.
    Add {
        file: String,
    },
    AbortMerge,
    /// Conclude the resolved merge -> Ran.
    CommitMerge,
    /// Delete MSG_FILE in the union worktree and write its index's tree ->
    /// Text.
    StripTree,
    /// The union worktree's HEAD commit, and its tree -> Text.
    Head,
    HeadTree,
    /// The deterministic union commit of `tree` on origin/main -> Text.
    CommitUnion {
        tree: String,
    },
    /// Hard-reset the union worktree onto `union` -> Ran.
    Checkout {
        union: String,
    },
    /// Wait for the fetch the deploy started -> Ran.
    CollectFetch,
    /// Bring the checkout onto the fetched origin/main and retake the
    /// staged snapshot -> Ran.
    Refresh,
    /// Wait while another checkout's deploy under way (its announcement on
    /// origin, as fetched) reaches a target this checkout's work reaches
    /// too, probing the targets again after -> Flag: waited (origin may
    /// have moved).
    AwaitPending {
        union: String,
    },
    /// Evaluate, build and copy `union` (the deploy's steps 5-7 up to the
    /// switch), `listed` the branches it may contain and `branches` the
    /// ones it merged, re-fetching meanwhile -> Flag: true when the deploy
    /// ends there.
    Stage {
        union: String,
        listed: Vec<String>,
        branches: Vec<(String, String)>,
    },

    /// The prune core's, in the subagent worktree it judges.  Take the
    /// lock on its name the create hook also takes, without waiting ->
    /// Flag: taken.
    TakeNameLock,
    ReleaseNameLock,
    /// Take its deploy lock, without waiting -> Flag: taken.
    TakeDeployLock,
    ReleaseDeployLock,
    /// Its git worktree lock's reason -> Text (None: unlocked).
    Lock,
    /// Whether the claude session `pid` (started at `start`, where /proc
    /// tells) runs - not counting the one running this when `own_ends`
    /// -> Flag.
    Alive {
        pid: u32,
        start: Option<u64>,
        own_ends: bool,
    },
    /// Whether a background push of its work branch runs -> Flag.
    PushInFlight,
    /// Whether another worktree lives under its directory -> Flag.
    Nested,
    /// The work branch it owns -> Text.
    OwnBranch,
    /// Whether `sha` has commits on no origin ref -> Flag.
    Unpushed {
        sha: String,
    },
    /// Whether it has staged, unstaged or untracked changes -> Flag.
    Dirty,
    /// Whether a process other than nixconvoy's own has its working
    /// directory in it -> Flag.
    Occupied,
    /// A stop's prune: whether its agent took a turn since the stop or a
    /// prompt awaits one (it runs again) -> Flag.
    RanAgain,

    /// Rename its lock aside, if it still holds `expect` (None: if it is
    /// still unlocked) -> Flag: claimed.
    ClaimLock {
        expect: Option<String>,
    },
    /// `git worktree remove` without force: refused for a locked or
    /// unclean worktree -> Ran.
    RemoveWorktree,
    /// The claimed lock back in place, unless a new one took it -> Ran.
    RestoreLock,
    /// Delete its branches - `wb`, and `cur` or its fresh branch when its
    /// own - here and on origin -> Ran.
    DropBranches {
        wb: String,
        cur: Option<String>,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Reply {
    Flag(bool),
    Text(Option<String>),
    /// A command's success and its stderr.
    Ran {
        ok: bool,
        detail: String,
    },
    Refs(Vec<(String, String)>),
    Names(Vec<String>),
    /// A merge's success, its conflicted files and its stderr.
    Merge {
        ok: bool,
        files: Vec<String>,
        detail: String,
    },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Halt {
    Ask(Query),
    Die(String),
}

pub type Run<T> = Result<T, Halt>;

pub struct Script<'a> {
    replies: &'a [Reply],
    pos: usize,
    effects: Vec<Effect>,
}

impl Script<'_> {
    pub fn ask(&mut self, q: Query) -> Run<Reply> {
        match self.replies.get(self.pos) {
            Some(r) => {
                self.pos += 1;
                Ok(r.clone())
            }
            None => Err(Halt::Ask(q)),
        }
    }

    pub fn flag(&mut self, q: Query) -> Run<bool> {
        match self.ask(q.clone())? {
            Reply::Flag(b) => Ok(b),
            r => Err(mismatch(&q, &r)),
        }
    }

    pub fn text(&mut self, q: Query) -> Run<Option<String>> {
        match self.ask(q.clone())? {
            Reply::Text(t) => Ok(t),
            r => Err(mismatch(&q, &r)),
        }
    }

    /// The command's stderr when it failed.
    pub fn ran(&mut self, q: Query) -> Run<Result<(), String>> {
        match self.ask(q.clone())? {
            Reply::Ran { ok: true, .. } => Ok(Ok(())),
            Reply::Ran { ok: false, detail } => Ok(Err(detail)),
            r => Err(mismatch(&q, &r)),
        }
    }

    /// A command whose failure changes nothing, like git's own cleanups.
    pub fn tolerate(&mut self, q: Query) -> Run<()> {
        self.ran(q).map(|_| ())
    }

    /// A command that must succeed: its failure ends the run.
    pub fn must(&mut self, q: Query, what: &str) -> Run<()> {
        self.ran(q)?.map_err(|detail| Halt::Die(failed(what, &detail)))
    }

    pub fn refs(&mut self, q: Query) -> Run<Vec<(String, String)>> {
        match self.ask(q.clone())? {
            Reply::Refs(v) => Ok(v),
            r => Err(mismatch(&q, &r)),
        }
    }

    pub fn names(&mut self, q: Query) -> Run<Vec<String>> {
        match self.ask(q.clone())? {
            Reply::Names(v) => Ok(v),
            r => Err(mismatch(&q, &r)),
        }
    }

    /// A merge's conflicted files and stderr when it failed.
    pub fn merge(&mut self, q: Query) -> Run<Result<(), (Vec<String>, String)>> {
        match self.ask(q.clone())? {
            Reply::Merge { ok: true, .. } => Ok(Ok(())),
            Reply::Merge { ok: false, files, detail } => Ok(Err((files, detail))),
            r => Err(mismatch(&q, &r)),
        }
    }

    pub fn print(&mut self, s: impl Into<String>) {
        self.effects.push(Effect::Print(s.into()));
    }

    pub fn warn(&mut self, s: impl Into<String>) {
        self.effects.push(Effect::Warn(s.into()));
    }

    pub fn mark(&mut self, s: impl Into<String>) {
        self.effects.push(Effect::Mark(s.into()));
    }
}

pub fn failed(what: &str, detail: &str) -> String {
    if detail.trim().is_empty() {
        format!("{what} failed")
    } else {
        format!("{what} failed:\n{}", detail.trim())
    }
}

fn mismatch(q: &Query, r: &Reply) -> Halt {
    Halt::Die(format!("nixconvoy-plan asked {q:?} and got {r:?}"))
}

/// A stderr without git's hint: lines.
pub fn no_hints(detail: &str) -> String {
    detail.trim().lines().filter(|l| !l.starts_with("hint:")).collect::<Vec<_>>().join("\n")
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Next<O> {
    Ask(Query),
    Done(O),
    Die(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Step<O> {
    pub effects: Vec<Effect>,
    pub next: Next<O>,
}

/// A core's program re-run over the replies so far; each step carries only
/// the effects the previous steps did not.
pub struct Replay<P, O> {
    params: P,
    program: fn(&mut Script, &P) -> Run<O>,
    replies: Vec<Reply>,
    emitted: usize,
}

impl<P, O> Replay<P, O> {
    pub fn new(params: P, program: fn(&mut Script, &P) -> Run<O>) -> Self {
        Replay { params, program, replies: Vec::new(), emitted: 0 }
    }

    /// The next step: the first with `None`, then one per reply to the
    /// query the previous step asked.
    pub fn decide(&mut self, reply: Option<Reply>) -> Step<O> {
        self.replies.extend(reply);
        let mut s = Script { replies: &self.replies, pos: 0, effects: Vec::new() };
        let result = (self.program)(&mut s, &self.params);
        let consumed = s.pos;
        let effects = s.effects.split_off(self.emitted.min(s.effects.len()));
        self.emitted += effects.len();
        let next = match result {
            Ok(o) => Next::Done(o),
            Err(Halt::Die(m)) => Next::Die(m),
            Err(Halt::Ask(q)) => {
                debug_assert_eq!(consumed, self.replies.len(), "a replay retraced fewer queries");
                Next::Ask(q)
            }
        };
        Step { effects, next }
    }
}

/// A core's program run once over `replies`: what it asks next, or how
/// it ends (the stateright models keep the replies, not a `Replay`).
#[cfg(test)]
pub fn resume<P, O>(params: &P, program: fn(&mut Script, &P) -> Run<O>, replies: &[Reply]) -> Next<O> {
    let mut s = Script { replies, pos: 0, effects: Vec::new() };
    match program(&mut s, params) {
        Ok(o) => Next::Done(o),
        Err(Halt::Die(m)) => Next::Die(m),
        Err(Halt::Ask(q)) => Next::Ask(q),
    }
}

/// Drives a core against `answer` to its end: the effects and the outcome
/// (Err: its dying message).  `limit` bounds the queries.
#[cfg(test)]
pub fn drive<P, O>(
    mut m: Replay<P, O>,
    limit: usize,
    mut answer: impl FnMut(&Query) -> Reply,
) -> (Vec<Query>, Vec<Effect>, Result<O, String>) {
    let mut asked = Vec::new();
    let mut fx = Vec::new();
    let mut reply = None;
    loop {
        let step = m.decide(reply.take());
        fx.extend(step.effects);
        match step.next {
            Next::Done(o) => return (asked, fx, Ok(o)),
            Next::Die(msg) => return (asked, fx, Err(msg)),
            Next::Ask(q) => {
                assert!(asked.len() < limit, "more than {limit} queries: {asked:?}");
                reply = Some(answer(&q));
                asked.push(q);
            }
        }
    }
}
