//! The prune core against the world it runs in: two claude sessions, each
//! with its waiter (`prune --after`: a sweep at start, another once the
//! session exits), spawning an agent into a worktree through the create
//! hook - a fresh worktree, or `claude --worktree <name>` reusing a dead
//! session's, relocking it (with no claude ancestor found, by a lock
//! naming no session) - and resuming it while it runs; the agent dirtying, committing,
//! deploying (a background push that may fail) and leaving its work
//! branch, a few times; a hand lock; a reboot killing sessions, waiters,
//! pushes and a sweep mid-removal; and, once all is quiet, a later
//! session's start sweep.  Each query the real core asks is one step, so
//! every other actor interleaves between any two of them.  Sweeps are
//! serialised (prune's flock), as the create hook's steps are per session.
//! The core judges each worktree on its own, so one is modelled; a manual
//! prune is a waiter's first sweep at another moment.
use super::{program, Outcome, Params};
use crate::plan::{resume, Next, Query, Reply, MAIN};
use stateright::{Checker, Model, Property};

const S: usize = 2;
const W: usize = 1;
/// Agent actions on the worktree, all told.
const WORK: u8 = 3;

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
    Hand,
}

const HAND: &str = "kept for a look";

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

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct Slot {
    exists: bool,
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
    fresh: bool,
    deploy_lock: bool,
    /// The session whose agent works in it, and may resume it.
    agent: Option<usize>,
}

const EMPTY: Slot = Slot {
    exists: false,
    lock: None,
    claimed: None,
    dirty: false,
    commits: false,
    on_wb: false,
    local: Local::None,
    increments: false,
    push: false,
    fresh: false,
    deploy_lock: false,
    agent: None,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum CStep {
    /// worktree-create.sh: does the worktree exist (reuse), else does its
    /// branch (check out as is, else add -b).
    Check {
        reuse: bool,
        ancestor: bool,
    },
    Add {
        on_branch: bool,
    },
    /// Rename the new lock over the old one: the session's, or with no
    /// claude ancestor found one naming none, which prune keeps as a hand
    /// lock.
    Relock {
        ancestor: bool,
    },
    Verify,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct Create {
    slot: usize,
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
enum Who {
    First(usize),
    Last(usize),
    Later,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct Sweep {
    who: Who,
    todo: Vec<usize>,
    core: Option<(usize, Vec<Reply>)>,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct World {
    sessions: [Session; S],
    waiters: [Waiter; S],
    creating: [Option<Create>; S],
    slots: [Slot; W],
    sweep: Option<Sweep>,
    later: Option<bool>,
    work: u8,
    reboots: u8,
    stale_reuse: u8,
    hand: u8,
    // Ghosts.
    stranded: bool,
    lost: bool,
    died: bool,
    removed: bool,
    raced: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum Act {
    Start(usize),
    Spawn(usize, usize),
    Reuse(usize, usize, bool),
    CreateStep(usize),
    Dirty(usize),
    Commit(usize),
    Deploy(usize),
    Detach(usize),
    PushDone(usize, bool),
    Exit(usize),
    Reboot,
    /// `git worktree lock` by hand on a worktree its session left.
    HandLock(usize),
    Begin(Who),
    Step,
}

fn wb(k: usize) -> String {
    format!("p-agent-{k}")
}

fn fresh(k: usize) -> String {
    format!("worktree-agent-{k}")
}

fn flag(b: bool) -> Reply {
    Reply::Flag(b)
}

fn ran(ok: bool) -> Reply {
    Reply::Ran { ok, detail: if ok { String::new() } else { "refused".into() } }
}

impl World {
    fn alive(&self, s: usize) -> bool {
        self.sessions[s] == Session::Alive
    }

    fn agent_of(&self, k: usize) -> Option<usize> {
        self.slots[k].agent.filter(|&s| self.alive(s))
    }

    fn quiet(&self) -> bool {
        self.sessions.iter().all(|s| *s == Session::Dead)
            && self.waiters.iter().all(|w| matches!(w, Waiter::Done | Waiter::Killed | Waiter::Unstarted))
            && self.creating.iter().all(Option::is_none)
            && self.sweep.is_none()
            && self.slots.iter().all(|x| !x.push)
    }

    /// What the real git would answer the core judging slot `k`.
    fn answer(&mut self, k: usize, q: &Query) -> Reply {
        let live = self.sessions.map(|s| s == Session::Alive);
        let x = &mut self.slots[k];
        match q {
            Query::TakeDeployLock => {
                let took = x.exists && !x.deploy_lock;
                x.deploy_lock |= took;
                flag(took)
            }
            Query::ReleaseDeployLock => {
                x.deploy_lock = false;
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
            Query::CurrentBranch => Reply::Text(if x.on_wb { Some(wb(k)) } else { x.fresh.then(|| fresh(k)) }),
            Query::OwnBranch => Reply::Text(Some(wb(k))),
            Query::Fetch => ran(true),
            Query::Rev { name } if *name == format!("refs/remotes/origin/{}", wb(k)) => Reply::Text(x.increments.then(|| "tip".into())),
            Query::Rev { name } if *name == format!("refs/heads/{}", wb(k)) => {
                Reply::Text((x.local != Local::None).then(|| "local".into()))
            }
            Query::Contains { a, b } if a == "tip" && b == MAIN => flag(!x.increments),
            Query::Unpushed { sha } if sha == "local" => flag(x.local == Local::Unpushed),
            Query::Dirty => flag(x.dirty),
            Query::OnOrigin => flag(!x.commits),
            Query::ClaimLock { expect } => {
                let expect = expect.as_deref().map(parse);
                if x.lock.is_some() && x.lock == expect {
                    x.claimed = x.lock.take();
                    flag(true)
                } else {
                    // Still unlocked, or claimed already by a run that died.
                    flag(x.lock.is_none() && x.claimed == expect)
                }
            }
            Query::RemoveWorktree => {
                if !x.exists || x.lock.is_some() || x.dirty {
                    return ran(false);
                }
                let lost = x.commits || x.on_wb || x.local == Local::Unpushed || x.increments || x.push;
                let stranded = x.agent.is_some_and(|s| live[s]);
                *x = Slot { local: x.local, increments: x.increments, fresh: x.fresh, ..EMPTY };
                self.lost |= lost;
                self.stranded |= stranded;
                self.removed = true;
                ran(true)
            }
            Query::RestoreLock => {
                if x.lock.is_none() {
                    x.lock = x.claimed;
                }
                x.claimed = None;
                ran(true)
            }
            Query::DropBranches { .. } => {
                // git refuses to delete a branch a worktree has checked out.
                if !x.exists {
                    let lost = x.local == Local::Unpushed || x.increments;
                    x.local = Local::None;
                    x.increments = false;
                    x.fresh = false;
                    self.lost |= lost;
                }
                ran(true)
            }
            q => panic!("prune asked the model {q:?}"),
        }
    }

    fn step_create(&mut self, s: usize) {
        let Some(c) = self.creating[s] else { return };
        let k = c.slot;
        let next = match c.step {
            CStep::Check { reuse, ancestor } => {
                if reuse && self.slots[k].exists {
                    Some(CStep::Relock { ancestor })
                } else if self.slots[k].exists {
                    // A fresh agent's name is unique; another create for
                    // this slot would have to have raced it.
                    panic!("fresh create over an existing worktree");
                } else {
                    Some(CStep::Add { on_branch: self.slots[k].fresh })
                }
            }
            CStep::Add { on_branch } => {
                if on_branch && !self.slots[k].fresh || self.slots[k].exists {
                    // The branch went (or the worktree came) meanwhile: again.
                    self.raced = true;
                    Some(CStep::Check { reuse: false, ancestor: true })
                } else {
                    let keep = (self.slots[k].local, self.slots[k].increments);
                    self.slots[k] = Slot {
                        exists: true,
                        lock: Some(Lock::Of(s)),
                        fresh: true,
                        local: keep.0,
                        increments: keep.1,
                        agent: Some(s),
                        ..EMPTY
                    };
                    None
                }
            }
            CStep::Relock { ancestor } => {
                let x = &mut self.slots[k];
                if !x.exists {
                    self.raced = true;
                    Some(CStep::Check { reuse: false, ancestor })
                } else {
                    x.lock = Some(if ancestor { Lock::Of(s) } else { Lock::Hand });
                    Some(CStep::Verify)
                }
            }
            CStep::Verify => {
                if self.slots[k].exists {
                    self.slots[k].agent = Some(s);
                    None
                } else {
                    self.raced = true;
                    Some(CStep::Check { reuse: false, ancestor: true })
                }
            }
        };
        self.creating[s] = next.map(|step| Create { slot: k, step });
    }

    fn step_sweep(&mut self) {
        let Some(mut sw) = self.sweep.take() else { return };
        match sw.core.take() {
            None => match sw.todo.pop() {
                Some(k) if self.slots[k].exists => sw.core = Some((k, Vec::new())),
                Some(_) => {}
                None => {
                    match sw.who {
                        Who::First(s) => self.waiters[s] = Waiter::Waiting,
                        Who::Last(s) => self.waiters[s] = Waiter::Done,
                        Who::Later => self.later = Some(true),
                    }
                    return;
                }
            },
            Some((k, mut replies)) => {
                let p = Params { name: format!("agent-{k}"), named: false, own_session_ends: false };
                match resume(&p, program, &replies) {
                    Next::Ask(q) => {
                        let r = self.answer(k, &q);
                        replies.push(r);
                        sw.core = Some((k, replies));
                    }
                    Next::Done(Outcome::Removed | Outcome::Kept(_)) => {}
                    Next::Die(_) => self.died = true,
                }
            }
        }
        self.sweep = Some(sw);
    }
}

struct Prune;

impl Model for Prune {
    type State = World;
    type Action = Act;

    fn init_states(&self) -> Vec<World> {
        vec![World {
            sessions: [Session::Unstarted; S],
            waiters: [Waiter::Unstarted; S],
            creating: [None; S],
            slots: [EMPTY; W],
            sweep: None,
            later: None,
            work: WORK,
            reboots: 1,
            stale_reuse: 1,
            hand: 1,
            stranded: false,
            lost: false,
            died: false,
            removed: false,
            raced: false,
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
        for s in 0..S {
            match w.sessions[s] {
                Session::Unstarted if s == 0 || w.sessions[0] != Session::Unstarted => acts.push(Act::Start(s)),
                Session::Alive => {
                    acts.push(Act::Exit(s));
                    if w.creating[s].is_some() {
                        acts.push(Act::CreateStep(s));
                    } else {
                        for k in 0..W {
                            let busy = w.creating.iter().flatten().any(|c| c.slot == k) || w.agent_of(k).is_some();
                            if busy {
                                continue;
                            }
                            if !w.slots[k].exists {
                                acts.push(Act::Spawn(s, k));
                            } else {
                                acts.push(Act::Reuse(s, k, true));
                                if w.stale_reuse > 0 {
                                    acts.push(Act::Reuse(s, k, false));
                                }
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        for k in 0..W {
            let x = &w.slots[k];
            if x.agent.is_some_and(|s| w.alive(s)) && x.exists && w.work > 0 {
                if !x.dirty {
                    acts.push(Act::Dirty(k));
                } else {
                    acts.push(Act::Commit(k));
                }
                if !x.deploy_lock && !x.push && (x.commits || x.dirty) {
                    acts.push(Act::Deploy(k));
                }
                if x.on_wb && !x.dirty {
                    acts.push(Act::Detach(k));
                }
            }
            if x.push {
                acts.push(Act::PushDone(k, true));
                acts.push(Act::PushDone(k, false));
            }
            if w.hand > 0 && x.exists && matches!(x.lock, Some(Lock::Of(s)) if !w.alive(s)) {
                acts.push(Act::HandLock(k));
            }
        }
        if w.sweep.is_none() {
            for s in 0..S {
                match w.waiters[s] {
                    Waiter::WantFirst => acts.push(Act::Begin(Who::First(s))),
                    Waiter::Waiting if w.sessions[s] == Session::Dead => acts.push(Act::Begin(Who::Last(s))),
                    _ => {}
                }
            }
        } else {
            acts.push(Act::Step);
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
            Act::Spawn(s, k) => w.creating[s] = Some(Create { slot: k, step: CStep::Check { reuse: false, ancestor: true } }),
            Act::Reuse(s, k, ancestor) => {
                if !ancestor {
                    w.stale_reuse -= 1;
                }
                w.creating[s] = Some(Create { slot: k, step: CStep::Check { reuse: true, ancestor } });
            }
            Act::CreateStep(s) => w.step_create(s),
            Act::Dirty(k) => {
                w.work -= 1;
                w.slots[k].dirty = true;
            }
            Act::Commit(k) => {
                w.work -= 1;
                w.slots[k].dirty = false;
                w.slots[k].commits = true;
            }
            Act::Deploy(k) => {
                w.work -= 1;
                let x = &mut w.slots[k];
                x.dirty = false;
                x.commits = true;
                x.on_wb = true;
                x.local = Local::Unpushed;
                x.push = true;
            }
            Act::Detach(k) => {
                w.work -= 1;
                let x = &mut w.slots[k];
                x.on_wb = false;
                x.commits = false;
            }
            Act::PushDone(k, ok) => {
                let x = &mut w.slots[k];
                x.push = false;
                if ok {
                    x.local = Local::Pushed;
                    x.increments = true;
                }
            }
            Act::Exit(s) => {
                w.sessions[s] = Session::Dead;
                w.creating[s] = None;
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
                    w.creating[s] = None;
                }
                w.sweep = None;
                for x in &mut w.slots {
                    x.push = false;
                    x.deploy_lock = false;
                }
            }
            Act::HandLock(k) => {
                w.hand -= 1;
                w.slots[k].lock = Some(Lock::Hand);
            }
            Act::Begin(who) => {
                if let Who::First(s) | Who::Last(s) = who {
                    w.waiters[s] = Waiter::Waiting;
                }
                w.sweep = Some(Sweep { who, todo: (0..W).filter(|&k| w.slots[k].exists).collect(), core: None });
            }
            Act::Step => w.step_sweep(),
        }
        Some(w)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        vec![
            Property::always("a running or resumable agent keeps its worktree", |_, w: &World| {
                !w.stranded
                    && (0..W).all(|k| w.agent_of(k).is_none() || w.slots[k].exists || w.creating.iter().flatten().any(|c| c.slot == k))
            }),
            Property::always("no work is removed", |_, w: &World| !w.lost),
            Property::always("the core never dies", |_, w: &World| !w.died),
            Property::always("once all is quiet, a later session's sweep leaves nothing removable", |_, w: &World| {
                w.later != Some(true)
                    || w.slots.iter().all(|x| {
                        let dead = matches!(x.lock.or(x.claimed), Some(Lock::Of(_)));
                        !(x.exists && dead && !x.dirty && !x.commits && !x.on_wb && x.local != Local::Unpushed && !x.increments)
                    })
            }),
            Property::sometimes("a worktree is removed", |_, w: &World| w.removed),
            Property::sometimes("a create races a removal and retries", |_, w: &World| w.raced),
            Property::sometimes("deployed work survives to the end", |_, w: &World| {
                w.later == Some(true) && w.slots.iter().any(|x| x.exists && x.increments)
            }),
        ]
    }
}

#[test]
fn prune_model() {
    let checker = Prune.checker().spawn_bfs().join();
    eprintln!("prune model: {} unique states, depth {}", checker.unique_state_count(), checker.max_depth());
    checker.assert_properties();
}
