//! The prune core against the world it runs in: two claude sessions, each
//! with its waiter (`prune --after`: a sweep at start, another once the
//! session exits), spawning an agent into a fresh worktree or, through
//! `claude --worktree <name>`, reusing a dead session's (relocking it, by a
//! lock naming no session when no claude ancestor is found) or recreating
//! it, each under the create hook's name lock, and resuming the agent while
//! it runs; the agent dirtying, committing, deploying (a background push
//! that may fail) and leaving its work branch, a few times; a process
//! working in it without its lock; a hand lock; a reboot killing sessions,
//! waiters, creates, pushes and sweeps mid-removal; and, once all is quiet,
//! a later session's start sweep.  Each query the real core asks is one
//! step, so every other actor interleaves between any two of them;
//! `git worktree remove` is three: its lock and cleanliness check, the
//! tree's deletion, the admin directory's (by name, whichever worktree of
//! that name it then belongs to).  The core judges each worktree on its
//! own, so one name is modelled (a fresh agent's unique name takes it only
//! once it is empty); a manual prune is a waiter's first sweep at another
//! moment.  Two configurations: the whole world with sweeps one at a time,
//! as prune's flock runs them; and sweeps fully concurrent against the
//! sessions and creates alone (no agent work, hand lock, stale reuse,
//! lockless process or reboot), since the name and deploy locks alone have
//! to keep them apart.
//!
//! Under the name lock no create meets a removal half done (a property), so
//! the hook's retries are for failures this world leaves out.  Not
//! modelled: the lockless process writing to
//! the worktree after the
//! occupancy check (a removal can always race a shell entering it at that
//! instant), ignored files (removed with the worktree), and git operations
//! left in progress or nested worktrees, which only make prune keep it.
use super::{program, Outcome, Params};
use crate::plan::{resume, Next, Query, Reply, MAIN};
use stateright::{Checker, Model, Property};

const S: usize = 2;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Session {
    Unstarted,
    Alive,
    Dead,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Lock {
    /// The create hook's, naming session `.0`.
    Of(usize),
    /// By hand, or the hook's when it found no claude ancestor.
    Hand,
}

const HAND: &str = "claude agent x (no claude session found)";

fn reason(l: Lock) -> String {
    match l {
        Lock::Of(s) => format!("claude agent x (pid {} start 1)", 100 + s),
        Lock::Hand => HAND.into(),
    }
}

fn parse(r: &str) -> Lock {
    if r == HAND {
        return Lock::Hand;
    }
    let (pid, _) = super::session(r).expect("a session's lock");
    Lock::Of(pid as usize - 100)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Local {
    None,
    Pushed,
    Unpushed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Who {
    First(usize),
    Last(usize),
    Later,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Holder {
    Sweep(Who),
    Create(usize),
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct Slot {
    /// The checked-out tree (with its .git file), and the admin directory
    /// under .git/worktrees holding its lock and deploy lock files.
    tree: bool,
    admin: bool,
    lock: Option<Lock>,
    /// The lock a removal renamed aside (ClaimLock).
    claimed: Option<Lock>,
    dirty: bool,
    /// HEAD carries commits not on origin.
    commits: bool,
    on_wb: bool,
    local: Local,
    /// origin's tip of its work branch carries increments.
    increments: bool,
    push: bool,
    /// Its fresh branch exists.
    fresh: bool,
    /// In the admin directory: gone with it.
    deploy_lock: Option<Holder>,
    /// In the common git dir: outlives the worktree.
    name_lock: Option<Holder>,
    /// The session whose agent works in it, and may resume it.
    agent: Option<usize>,
    /// A process working in it without its lock (a shell cd'd there).
    occupant: bool,
}

impl Slot {
    fn exists(&self) -> bool {
        self.tree && self.admin
    }
}

const EMPTY: Slot = Slot {
    tree: false,
    admin: false,
    lock: None,
    claimed: None,
    dirty: false,
    commits: false,
    on_wb: false,
    local: Local::None,
    increments: false,
    push: false,
    fresh: false,
    deploy_lock: None,
    name_lock: None,
    agent: None,
    occupant: false,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum CStep {
    NameLock,
    /// Does the worktree exist (reuse it), else `git worktree prune` and
    /// does its branch (check out as is, else add -b).
    Check,
    Add {
        on_branch: bool,
    },
    /// Rename the new lock over the old one.
    Relock,
    Verify,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct Create {
    reuse: bool,
    ancestor: bool,
    step: CStep,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Waiter {
    Unstarted,
    WantFirst,
    Waiting,
    Done,
    Killed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Removing {
    No,
    /// git found it unlocked and clean.
    Checked,
    /// The tree is gone; the admin directory is next.
    TreeGone,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct Sweep {
    who: Who,
    listed: bool,
    core: Option<Vec<Reply>>,
    removing: Removing,
    /// A process was in the worktree when its lock was claimed.
    occupied_at_claim: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct World {
    sessions: [Session; S],
    waiters: [Waiter; S],
    creating: [Option<Create>; S],
    x: Slot,
    sweeps: Vec<Sweep>,
    later: Option<bool>,
    work: u8,
    reboots: u8,
    stale_reuse: u8,
    hand: u8,
    enters: u8,
    // Ghosts.
    stranded: bool,
    lost: bool,
    died: bool,
    evicted: bool,
    removed: bool,
    raced: bool,
    deferred: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Act {
    Start(usize),
    /// A fresh agent (a unique name).
    Spawn(usize),
    /// `claude --worktree <name>`; with or without a claude ancestor.
    Reuse(usize, bool),
    CreateStep(usize),
    Dirty,
    Commit,
    Deploy,
    Detach,
    PushDone(bool),
    Exit(usize),
    Reboot,
    HandLock,
    Enter,
    Leave,
    Begin(Who),
    Step(usize),
}

const WB: &str = "p-agent-x";
const FRESH: &str = "worktree-agent-x";

fn flag(b: bool) -> Reply {
    Reply::Flag(b)
}

fn ran(ok: bool) -> Reply {
    Reply::Ran { ok, detail: if ok { String::new() } else { "refused".into() } }
}

fn take(l: &mut Option<Holder>, h: Holder) -> bool {
    let took = l.is_none();
    if took {
        *l = Some(h);
    }
    took
}

fn release(l: &mut Option<Holder>, h: Holder) {
    if *l == Some(h) {
        *l = None;
    }
}

impl World {
    fn alive(&self, s: usize) -> bool {
        self.sessions[s] == Session::Alive
    }

    fn agent_alive(&self) -> bool {
        self.x.agent.is_some_and(|s| self.alive(s))
    }

    fn quiet(&self) -> bool {
        self.sessions.iter().all(|s| *s == Session::Dead)
            && self.waiters.iter().all(|w| matches!(w, Waiter::Done | Waiter::Killed | Waiter::Unstarted))
            && self.creating.iter().all(Option::is_none)
            && self.sweeps.is_empty()
            && !self.x.push
            && !self.x.occupant
    }

    /// What git and the system would answer sweep `who`'s core.
    fn answer(&mut self, who: Who, q: &Query) -> Reply {
        let live = self.sessions.map(|s| s == Session::Alive);
        let me = Holder::Sweep(who);
        let x = &mut self.x;
        match q {
            Query::TakeNameLock => {
                let took = take(&mut x.name_lock, me);
                self.deferred |= !took && matches!(x.name_lock, Some(Holder::Create(_)));
                flag(took)
            }
            Query::ReleaseNameLock => {
                release(&mut x.name_lock, me);
                ran(true)
            }
            Query::TakeDeployLock => flag(x.admin && take(&mut x.deploy_lock, me)),
            Query::ReleaseDeployLock => {
                release(&mut x.deploy_lock, me);
                ran(true)
            }
            Query::Lock => Reply::Text(x.lock.or(x.claimed).map(reason)),
            Query::Alive { pid, own_ends, .. } => {
                assert!(!own_ends, "prune never lets its own session end");
                flag(live[*pid as usize - 100])
            }
            Query::OpInProgress => Reply::Text(None),
            Query::PushInFlight => flag(x.push),
            Query::Nested => flag(false),
            Query::CurrentBranch => Reply::Text(if x.on_wb { Some(WB.into()) } else { x.fresh.then(|| FRESH.into()) }),
            Query::OwnBranch => Reply::Text(Some(WB.into())),
            Query::Fetch => ran(true),
            Query::Rev { name } if *name == format!("refs/remotes/origin/{WB}") => Reply::Text(x.increments.then(|| "tip".into())),
            Query::Rev { name } if *name == format!("refs/heads/{WB}") => Reply::Text((x.local != Local::None).then(|| "local".into())),
            Query::Contains { a, b } if a == "tip" && b == MAIN => flag(!x.increments),
            Query::Unpushed { sha } if sha == "local" => flag(x.local == Local::Unpushed),
            Query::Dirty => flag(x.dirty),
            Query::OnOrigin => flag(!x.commits),
            Query::ClaimLock { expect } => {
                let expect = expect.as_deref().map(parse);
                let claimed = if x.lock.is_some() && x.lock == expect {
                    x.claimed = x.lock.take();
                    true
                } else {
                    // Still unlocked, or claimed already by a run that died.
                    x.lock.is_none() && x.claimed == expect
                };
                let occupied = x.occupant;
                if let Some(sw) = self.sweeps.iter_mut().find(|sw| sw.who == who) {
                    sw.occupied_at_claim = occupied;
                }
                flag(claimed)
            }
            Query::Occupied => flag(x.occupant),
            Query::RestoreLock => {
                if x.lock.is_none() {
                    x.lock = x.claimed;
                }
                x.claimed = None;
                ran(true)
            }
            Query::DropBranches { .. } => {
                let lost = x.local == Local::Unpushed || x.increments;
                x.local = Local::None;
                x.increments = false;
                // git refuses to delete the branch a worktree has checked out.
                x.fresh &= x.tree && !x.on_wb;
                self.lost |= lost;
                ran(true)
            }
            q => panic!("prune asked the model {q:?}"),
        }
    }

    /// `git worktree remove` for sweep `i`, one of its three steps: the
    /// reply once it is done.
    fn remove(&mut self, i: usize) -> Option<Reply> {
        let at_claim = self.sweeps[i].occupied_at_claim;
        let live = self.agent_alive();
        let x = &mut self.x;
        match self.sweeps[i].removing {
            Removing::No => {
                if !x.exists() || x.lock.is_some() || x.dirty {
                    return Some(ran(false));
                }
                self.sweeps[i].removing = Removing::Checked;
                None
            }
            Removing::Checked => {
                self.lost |= x.dirty || x.commits || x.on_wb || x.local == Local::Unpushed || x.increments || x.push;
                self.stranded |= live;
                self.evicted |= x.occupant && at_claim;
                x.tree = false;
                x.dirty = false;
                x.commits = false;
                x.on_wb = false;
                x.agent = None;
                x.occupant = false;
                self.sweeps[i].removing = Removing::TreeGone;
                self.removed = true;
                None
            }
            Removing::TreeGone => {
                // By name: a worktree of that name added meanwhile loses its.
                self.stranded |= x.tree && live;
                x.admin = false;
                x.lock = None;
                x.claimed = None;
                x.deploy_lock = None;
                self.sweeps[i].removing = Removing::No;
                Some(ran(true))
            }
        }
    }

    fn step_create(&mut self, s: usize) {
        let Some(c) = self.creating[s] else { return };
        let me = Holder::Create(s);
        let lock = if c.ancestor { Lock::Of(s) } else { Lock::Hand };
        let again = Some(CStep::Check);
        let x = &mut self.x;
        let next = match c.step {
            CStep::NameLock => take(&mut x.name_lock, me).then_some(CStep::Check),
            CStep::Check => {
                if c.reuse && x.exists() {
                    Some(CStep::Relock)
                } else {
                    // `git worktree prune`: a registration whose tree is
                    // gone, unless locked.
                    if !x.tree && x.admin && x.lock.is_none() {
                        x.admin = false;
                        x.claimed = None;
                        x.deploy_lock = None;
                    }
                    Some(CStep::Add { on_branch: x.fresh })
                }
            }
            CStep::Add { on_branch } => {
                if x.tree || x.admin || on_branch && !x.fresh {
                    // In the way, or the branch went: again.
                    self.raced = true;
                    again
                } else {
                    *x = Slot {
                        tree: true,
                        admin: true,
                        lock: Some(lock),
                        fresh: true,
                        local: x.local,
                        increments: x.increments,
                        name_lock: x.name_lock,
                        agent: Some(s),
                        ..EMPTY
                    };
                    None
                }
            }
            CStep::Relock => {
                if x.admin {
                    x.lock = Some(lock);
                    Some(CStep::Verify)
                } else {
                    self.raced = true;
                    again
                }
            }
            CStep::Verify => {
                if x.exists() {
                    x.agent = Some(s);
                    None
                } else {
                    self.raced = true;
                    again
                }
            }
        };
        if next.is_none() {
            release(&mut self.x.name_lock, me);
        }
        self.creating[s] = next.map(|step| Create { step, ..c });
    }

    fn end_create(&mut self, s: usize) {
        self.creating[s] = None;
        release(&mut self.x.name_lock, Holder::Create(s));
    }

    fn step_sweep(&mut self, i: usize) {
        let who = self.sweeps[i].who;
        if !self.sweeps[i].listed {
            self.sweeps[i].listed = true;
            if self.x.exists() {
                self.sweeps[i].core = Some(Vec::new());
                return;
            }
        }
        let Some(mut replies) = self.sweeps[i].core.take() else {
            self.sweeps.remove(i);
            match who {
                Who::First(s) => self.waiters[s] = Waiter::Waiting,
                Who::Last(s) => self.waiters[s] = Waiter::Done,
                Who::Later => self.later = Some(true),
            }
            return;
        };
        let p = Params { name: "agent-x".into(), named: false, own_session_ends: false };
        match resume(&p, program, &replies) {
            Next::Ask(Query::RemoveWorktree) => {
                if let Some(r) = self.remove(i) {
                    replies.push(r);
                }
                self.sweeps[i].core = Some(replies);
            }
            Next::Ask(q) => {
                let first = replies.is_empty();
                let r = self.answer(who, &q);
                replies.push(r);
                // What the core asks before the name lock only ever keeps
                // it, and is asked again under the lock: one step.
                if first {
                    loop {
                        match resume(&p, program, &replies) {
                            Next::Ask(q @ (Query::Lock | Query::Alive { .. } | Query::Fetch)) => {
                                let r = self.answer(who, &q);
                                replies.push(r);
                            }
                            Next::Ask(Query::TakeNameLock) => {
                                // Passed, what it read steers nothing after: one
                                // form, so states differing only there merge.
                                replies = vec![Reply::Text(Some("pid 1".into())), flag(false), ran(true)];
                                break;
                            }
                            _ => break,
                        }
                    }
                }
                self.sweeps[i].core = Some(replies);
            }
            Next::Done(Outcome::Removed | Outcome::Kept(_)) => {}
            Next::Die(_) => self.died = true,
        }
    }
}

struct Prune {
    /// Sweeps may overlap.
    concurrent: bool,
    /// Agent actions on the worktree, all told, and the other actors' turns.
    work: u8,
    reboots: u8,
    others: u8,
}

impl Model for Prune {
    type State = World;
    type Action = Act;

    fn init_states(&self) -> Vec<World> {
        vec![World {
            sessions: [Session::Unstarted; S],
            waiters: [Waiter::Unstarted; S],
            creating: [None; S],
            x: EMPTY,
            sweeps: Vec::new(),
            later: None,
            work: self.work,
            reboots: self.reboots,
            stale_reuse: self.others,
            hand: self.others,
            enters: self.others,
            stranded: false,
            lost: false,
            died: false,
            evicted: false,
            removed: false,
            raced: false,
            deferred: false,
        }]
    }

    fn actions(&self, w: &World, acts: &mut Vec<Act>) {
        if w.died || w.later.is_some() {
            return;
        }
        if w.quiet() {
            acts.push(Act::Begin(Who::Later));
            return;
        }
        let x = &w.x;
        let creating = w.creating.iter().any(Option::is_some);
        for s in 0..S {
            match w.sessions[s] {
                Session::Unstarted if s == 0 || w.sessions[0] != Session::Unstarted => acts.push(Act::Start(s)),
                Session::Alive => {
                    acts.push(Act::Exit(s));
                    match w.creating[s] {
                        Some(c) if c.step == CStep::NameLock && x.name_lock.is_some() => {}
                        Some(_) => acts.push(Act::CreateStep(s)),
                        None if creating || w.agent_alive() => {}
                        None => {
                            let idle = !x.tree && !x.admin && x.name_lock.is_none() && w.sweeps.iter().all(|sw| sw.core.is_none());
                            if idle {
                                acts.push(Act::Spawn(s));
                            }
                            acts.push(Act::Reuse(s, true));
                            if w.stale_reuse > 0 {
                                acts.push(Act::Reuse(s, false));
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        if w.agent_alive() && x.exists() && w.work > 0 {
            acts.push(if x.dirty { Act::Commit } else { Act::Dirty });
            if x.deploy_lock.is_none() && !x.push && (x.commits || x.dirty) {
                acts.push(Act::Deploy);
            }
            if x.on_wb && !x.dirty {
                acts.push(Act::Detach);
            }
        }
        if x.push {
            acts.push(Act::PushDone(true));
            acts.push(Act::PushDone(false));
        }
        if w.hand > 0 && x.exists() && matches!(x.lock, Some(Lock::Of(s)) if !w.alive(s)) {
            acts.push(Act::HandLock);
        }
        // The later sweep is the final check; a shell entering during it
        // would only make it keep the worktree.
        let last = w.sweeps.iter().any(|sw| sw.who == Who::Later);
        if w.enters > 0 && x.exists() && !x.occupant && !last {
            acts.push(Act::Enter);
        }
        if x.occupant {
            acts.push(Act::Leave);
        }
        for s in 0..S {
            let running = |who| w.sweeps.iter().any(|sw| sw.who == who) || !self.concurrent && !w.sweeps.is_empty();
            match w.waiters[s] {
                Waiter::WantFirst if !running(Who::First(s)) => acts.push(Act::Begin(Who::First(s))),
                Waiter::Waiting if w.sessions[s] == Session::Dead && !running(Who::Last(s)) => acts.push(Act::Begin(Who::Last(s))),
                _ => {}
            }
        }
        for i in 0..w.sweeps.len() {
            acts.push(Act::Step(i));
        }
        if w.reboots > 0 && w.sessions.contains(&Session::Alive) {
            acts.push(Act::Reboot);
        }
    }

    fn next_state(&self, w: &World, a: Act) -> Option<World> {
        let mut w = w.clone();
        match a {
            Act::Start(s) => {
                w.sessions[s] = Session::Alive;
                w.waiters[s] = Waiter::WantFirst;
            }
            Act::Spawn(s) => {
                // A new name: its own branches, none yet.
                w.x = EMPTY;
                w.creating[s] = Some(Create { reuse: false, ancestor: true, step: CStep::NameLock });
            }
            Act::Reuse(s, ancestor) => {
                if !ancestor {
                    w.stale_reuse -= 1;
                }
                w.creating[s] = Some(Create { reuse: true, ancestor, step: CStep::NameLock });
            }
            Act::CreateStep(s) => w.step_create(s),
            Act::Dirty => {
                w.work -= 1;
                w.x.dirty = true;
            }
            Act::Commit => {
                w.work -= 1;
                w.x.dirty = false;
                w.x.commits = true;
            }
            Act::Deploy => {
                w.work -= 1;
                let x = &mut w.x;
                x.dirty = false;
                x.commits = true;
                x.on_wb = true;
                x.local = Local::Unpushed;
                x.push = true;
            }
            Act::Detach => {
                w.work -= 1;
                w.x.on_wb = false;
                w.x.commits = false;
            }
            Act::PushDone(ok) => {
                w.x.push = false;
                if ok {
                    w.x.local = Local::Pushed;
                    w.x.increments = true;
                }
            }
            Act::Exit(s) => {
                w.sessions[s] = Session::Dead;
                w.end_create(s);
            }
            Act::Reboot => {
                w.reboots -= 1;
                for s in 0..S {
                    if w.sessions[s] == Session::Alive {
                        w.sessions[s] = Session::Dead;
                    }
                    if !matches!(w.waiters[s], Waiter::Unstarted | Waiter::Done) {
                        w.waiters[s] = Waiter::Killed;
                    }
                    w.end_create(s);
                }
                w.sweeps.clear();
                w.x.push = false;
                w.x.deploy_lock = None;
                w.x.name_lock = None;
                w.x.occupant = false;
            }
            Act::HandLock => {
                w.hand -= 1;
                w.x.lock = Some(Lock::Hand);
            }
            Act::Enter => {
                w.enters -= 1;
                w.x.occupant = true;
            }
            Act::Leave => w.x.occupant = false,
            Act::Begin(who) => {
                if let Who::First(s) | Who::Last(s) = who {
                    w.waiters[s] = Waiter::Waiting;
                }
                w.sweeps.push(Sweep { who, listed: false, core: None, removing: Removing::No, occupied_at_claim: false });
            }
            Act::Step(i) => w.step_sweep(i),
        }
        Some(w)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        let mut ps = vec![
            Property::always("a running or resumable agent keeps its worktree", |_, w: &World| {
                !w.stranded && (!w.agent_alive() || w.x.exists() || w.creating.iter().any(Option::is_some))
            }),
            Property::always("no work is removed", |_, w: &World| !w.lost),
            Property::always("the core never dies", |_, w: &World| !w.died),
            Property::always("no worktree is removed under a process that was in it at the claim", |_, w: &World| !w.evicted),
            Property::always("no create meets a removal half done", |_, w: &World| !w.raced),
            Property::always("once all is quiet, a later session's sweep leaves nothing removable", |_, w: &World| {
                let x = &w.x;
                let dead = matches!(x.lock.or(x.claimed), Some(Lock::Of(_)));
                w.later != Some(true)
                    || !(x.exists()
                        && dead
                        && !x.occupant
                        && !x.dirty
                        && !x.commits
                        && !x.on_wb
                        && x.local != Local::Unpushed
                        && !x.increments)
            }),
            Property::sometimes("a worktree is removed", |_, w: &World| w.removed),
            Property::sometimes("a sweep defers to a create holding the name", |_, w: &World| w.deferred),
        ];
        if self.concurrent {
            ps.push(Property::sometimes("two sweeps judge the worktree at once", |_, w: &World| {
                w.sweeps.iter().filter(|sw| sw.core.as_ref().is_some_and(|r| r.len() > 2)).count() > 1
            }));
        } else {
            ps.push(Property::sometimes("deployed work survives to the end", |_, w: &World| {
                w.later == Some(true) && w.x.exists() && w.x.increments
            }));
        }
        ps
    }
}

fn check(m: Prune, name: &str) {
    let checker = m.checker().spawn_dfs().join();
    eprintln!("{name}: {} unique states, depth {}", checker.unique_state_count(), checker.max_depth());
    checker.assert_properties();
}

#[test]
fn prune_model() {
    check(Prune { concurrent: false, work: 3, reboots: 1, others: 1 }, "prune model");
}

#[test]
fn prune_model_concurrent() {
    check(Prune { concurrent: true, work: 0, reboots: 0, others: 0 }, "prune model, concurrent sweeps");
}
