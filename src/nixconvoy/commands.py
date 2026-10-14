"""The verbs beside deploy: commit, land, pull, abandon, open, close and status."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import IO, NamedTuple

from . import proc, submodule
from .checkout import (
    MACHINE_ENV,
    PAST_REFS,
    SESSIONLESS,
    WAIT_STALE,
    GitQueries,
    add_worktree,
    allowed_branches,
    ancestor_pids,
    announcer_dead,
    await_background_push,
    branch_at,
    catch_up,
    checkout_identity,
    clear_refspec,
    current_branch,
    deploy_running,
    drop_worktree,
    ensure_rebased,
    fetch_args,
    git_common,
    git_op_in_progress,
    held_targets,
    identity_of,
    landing_message,
    listed_tips,
    lock_checkout,
    lock_live,
    lock_pid_alive,
    on_origin,
    origin_tips,
    owned,
    pending_announcements,
    pop_tmp_commits,
    proc_stat,
    push_state,
    push_work_branch,
    ran,
    require_work_branch,
    sibling_checkouts,
    staged_differs,
    sub_dir,
    sub_parent,
    summarize,
    this_prefix,
    trailer_targets,
    tree_less_msg,
    work_branch_name,
    worktree_lock,
)
from .config import MSG_FILE
from .guard import binary, decide
from .proc import die, git, is_ancestor, ok, out, painter, rev, run, solve_with_mergiraf


def holds(wb: str, tip: str, main: str, release: bool, cwd: str | None = None) -> None:
    """The targets `wb`'s increments (`main..tip`) hold back: a landing is
    refused on them unless `release`, which tells where the landed work
    rolls out from."""
    held = sorted(held_targets(f"{main}..{tip}", cwd))
    if not held:
        return
    them = ", ".join(held)
    if not release:
        die(f"{wb} holds back {them}: increments of it reach {'it' if len(held) == 1 else 'them'} "
            f"but were deployed to other targets alone.  `nixconvoy apply --catch-up {' '.join(held)}` from its "
            f"checkout deploys them there first; `land --release` lands them as they are, for the "
            f"next deploy anywhere to roll out")
    print(f"releasing {them}: the next deploy anywhere rolls the landed work out there")


def main_message(wb: str, given: str | None) -> str:
    """The main commit's message (checkout.landing_message over HEAD's
    MSG_FILE and the one on disk), refused when there is none."""
    on_file = out(["git", "show", f"HEAD:{MSG_FILE}"]) if rev(f"HEAD:{MSG_FILE}") else ""
    try:
        draft = Path(MSG_FILE).read_text(encoding="utf-8")
    except OSError:
        draft = ""
    msg = landing_message(given, on_file, draft, wb)
    if not msg:
        die(f"{wb} has no {MSG_FILE}; write one (it becomes the main commit message), or pass -m <msg>")
    return msg


def land(release: bool = False, message: str | None = None) -> None:
    await_background_push()
    git(*fetch_args())
    pop_tmp_commits()
    wb = require_work_branch("land")
    if current_branch() != wb:
        die(f"land runs from the work branch ({wb}); currently on "
            f"{current_branch() or '(detached HEAD)'}")
    if staged_differs():
        die("staged but undeployed changes present; deploy or unstage them first")
    ensure_rebased()
    main = rev("refs/remotes/origin/main")
    if main is None or tree_less_msg("HEAD") == out(["git", "rev-parse", "origin/main^{tree}"]):
        die("no changes vs origin/main to land")
    msg = main_message(wb, message)
    # A branch forked from another's tip (a subagent worktree made by hand at
    # its parent's) would land that branch's increments under this message.
    carried = [b for b, tip in listed_tips(allowed_branches()).items()
               if b != wb and is_ancestor(tip, "HEAD")
               and not is_ancestor(tip, "refs/remotes/origin/main")]
    if carried:
        die(f"{wb} contains the unlanded increments of {', '.join(carried)}; land "
            "that branch first, then re-run")
    holds(wb, "HEAD", main, release)
    # The submodules first: main must never pin a branch about to go.
    gone = submodule.orphaned(Path.cwd())
    if gone:
        die(f"the tree pins {wb}'s rev of submodule {', '.join(gone)}, whose worktree is gone: "
            f"`nixconvoy open --submodule <input>` recreates it from origin's branch to land it, or "
            f"`nixconvoy abandon --submodule <input>` drops it")
    submodule.land_all(wb, msg)
    tree = tree_less_msg("HEAD")

    commit = out(["git", "commit-tree", tree, "-p", main, "-m", msg])

    p = git("push", "--quiet", "origin", f"{commit}:refs/heads/main", check=False)
    if p.returncode != 0:
        die(f"pushing main failed (moved since fetch?); re-run:\n{p.stderr.strip()}")
    # Refused where main is checked out (the main worktree): catch_up moves it.
    git("branch", "-f", "main", commit, check=False)

    git("reset", "--quiet", commit)  # move wb to main; working tree untouched
    Path(MSG_FILE).unlink(missing_ok=True)  # now main's commit message
    print(f"landed on main: {summarize(commit)}")
    siblings = sibling_checkouts()  # the parent of a subagent worktree included
    finish(wb)
    if not (catch_up(siblings) & submodule.catch_up_repos()):
        die("some checkouts could not be brought onto the new main (see above); "
            "fix them and `nixconvoy pull`")


def pull() -> None:
    """This checkout and its siblings onto origin/main (cli docstring)."""
    await_background_push()
    binary("nixconvoy-plan")  # resolved before update_checkout moves into the others
    if not (catch_up([Path.cwd(), *sibling_checkouts()]) & submodule.catch_up_repos()):
        sys.exit(1)


def deployed_targets(commits: str) -> list[str]:
    """The targets the deploys of the commits in the range `commits` switched
    (their Deployed: trailers), sorted."""
    names: set[str] = set()
    for ln in out(["git", "log", "--format=%(trailers:key=Deployed,valueonly)", commits]).splitlines():
        names |= trailer_targets(ln)
    return sorted(names)


def abandon(apply_now: bool = False) -> None:
    """Discard the work branch (cli docstring: Abandon).  The targets keep
    running its increments until a deploy reverts them: `apply_now` when
    the caller deploys right after, for the message."""
    await_background_push()
    git(*fetch_args())
    pop_tmp_commits()
    ident = checkout_identity()
    here = Path.cwd().resolve()
    if ident.kind == "sub" and ident.wb and current_branch() != ident.wb and sub_parent(here):
        # Never deployed: still on its fresh branch, reset onto origin/main
        # (untracked files survive); close removes the worktree.
        main = rev("refs/remotes/origin/main")
        if main is None:
            die("origin/main not found")
        prev = rev("HEAD") or ""
        submodule.reset_all(here, ident.wb)
        git("reset", "--quiet", "--hard", main)
        Path(MSG_FILE).unlink(missing_ok=True)
        print(f"nothing deployed from {sub_label(here)}; reset onto origin/main (was {prev[:12]}, "
              f"see reflog); `nixconvoy close {here.name}` removes the worktree")
        return
    wb = require_work_branch("abandon")
    if current_branch() != wb:
        die(f"abandon runs from the work branch ({wb}); currently on "
            f"{current_branch() or '(detached HEAD)'}")
    main = rev("refs/remotes/origin/main")
    if main is None:
        die("origin/main not found")
    prev = rev("HEAD") or ""
    if prev == main and ok(["git", "diff", "--quiet", "HEAD"]) and checkout_identity().kind != "sub":
        die("nothing to abandon")

    deployed = deployed_targets(f"{main}..{prev}")
    submodule.reset_all(here, wb)
    git("reset", "--quiet", "--hard", main)
    Path(MSG_FILE).unlink(missing_ok=True)  # an uncommitted copy survives the hard reset
    git("branch", "-f", "main", main, check=False)
    print(f"abandoned {wb} (was {prev[:12]}, see reflog): {summarize(main)}")
    if deployed:
        where = ", ".join(deployed)
        if apply_now:
            print(f"deployed to {where}: deploying now to revert it")
        else:
            print(f"deployed to {where}; whichever targets run it keep it until the next deploy "
                  f"reverts it (`nixconvoy apply --catch-up`, or `abandon --apply-now` for both at once)")
    finish(wb)


def close_worktrees(names: list[str], *, force: bool = False) -> None:
    """Remove the task worktrees that hold nothing of their own (cli
    docstring: Close), as nixconvoy-plan's prune core decides: the named
    ones (their lock's session counted as ended for them, as abandon from
    inside counts it), or every one under this checkout.  `force` removes
    the named ones whatever they hold, with their branches.  Runs are
    serialised per repository."""
    here = Path.cwd().resolve()
    top = sub_parent(here) or here
    if force and not names:
        die("--force needs the worktrees named")
    serial = Path(out(["git", "rev-parse", "--git-common-dir"])).resolve() / "nixconvoy-prune.lock"
    with serial.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        subs = ([find_sub(n) for n in names] if names
                else sorted(d for d in sibling_checkouts() if sub_parent(d) == top))
        if not subs:
            print(f"no subagent worktree under {top}", flush=True)
        fetched: list[dict[str, object]] = []
        for d in subs:
            label = sub_label(d)
            try:
                if not d.is_dir():
                    continue  # removed since it was listed
                if force:
                    if deploy_running(d):
                        die(f"a nixconvoy is running in {sub_label(d)}")
                    remove_sub(d, [identity_of(d, branch_at(d)).wb or ""])
                    verdict = "removed (--force)"
                else:
                    why = prune_one(d, named=bool(names), own_session_ends=bool(names), fetched=fetched)
                    verdict = "empty, removed" if why is None else f"kept, {why}"
            except (Exception, SystemExit) as e:  # noqa: BLE001 - one worktree's failure, reported
                proc.DYING = False
                verdict = f"kept, its check failed: {e!r}"
            finally:
                os.chdir(top)
            print(f"{label}: {verdict}", flush=True)


def close_submodule(name: str, *, force: bool = False) -> None:
    """`close --submodule <input>`: the submodule worktree removed with
    its branch, unless that would lose work (`force`: removed anyway, the
    pin put back to origin/main's when it named the branch's work, staged)."""
    git(*fetch_args("--no-auto-gc"), check=False)
    top = Path.cwd()
    sub = submodule.present_one(top, name)
    why = submodule.keeps(sub, top)
    if why is not None and not force:
        die(f"submodule {name} is kept: {why} (--force removes it anyway)")
    if work_branch_name() and submodule.off_default(sub, top):
        submodule.restore(sub, top)
    submodule.remove_one(sub)


class Stop(NamedTuple):
    """A stop's prune: the claude session (pid) whose agent stopped, the
    worktree's lock then (see the core's Stopped), and the agent's
    transcript with its turns then."""

    pid: int
    lock: str | None
    transcript: Path
    turns: int | None


def prune_one(d: Path, *, named: bool, own_session_ends: bool = False,
              fetched: list[dict[str, object]] | None = None, prefetched: bool = False,
              deploy_held: bool = False, dropped_failed: list[str] | None = None,
              stopped: Stop | None = None) -> str | None:
    """nixconvoy-plan's prune core on the subagent worktree `d`: None once
    removed (the process then in its parent), else why it was kept.
    `fetched` shares one fetch across a run (`prefetched`: the caller
    fetched); `deploy_held`: the caller holds its deploy lock already;
    `dropped_failed` collects the origin branch deletions that failed.
    `own_session_ends`: the session the lock names ends for it - this
    one's, abandon from inside it (the invoking command no occupant).
    `stopped`: a stop's prune.  A worktree named waits for its name lock, a
    sweep does not."""
    os.chdir(d)
    memo: list[dict[str, object]] = [] if fetched is None else fetched
    if prefetched and not memo:
        memo.append({"ran": True, "detail": ""})
    perform = PruneQueries(d, memo, deploy_held=deploy_held, invoker=own_session_ends,
                           stop=stopped, name_wait=60 if named else 0)
    try:
        done = decide("prune", perform=perform, name=d.name, named=named, own_session_ends=own_session_ends,
                      stopped=None if stopped is None else {"pid": stopped.pid, "lock": stopped.lock})
    finally:
        perform.close()
    if dropped_failed is not None:
        dropped_failed.extend(perform.dropped_failed)
    assert isinstance(done, dict)
    if done.get("removed"):
        return None
    return str(done.get("kept"))


def name_lock_path(d: Path) -> Path:
    """The lock on the subagent worktree `d`'s path that the create and
    remove hooks take too: in the common git dir, so it outlives the
    worktree; named after the path with each / a %."""
    common = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]))
    return common / "nixconvoy-worktree-locks" / f"{str(d).replace('/', '%')}.lock"


def hold_lock(path: Path, wait: float = 0) -> IO[str] | None:
    """The lock file `path`, waiting up to `wait` seconds; None when
    another holds it still."""
    path.parent.mkdir(exist_ok=True)
    f = path.open("a")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(0.2)
        else:
            return f


def occupied(d: Path, *, invoker: bool = False) -> bool:
    """Whether a process other than this one or its children has its
    working directory in `d` (via /proc, else lsof).  `invoker`: nor its
    ancestors, nor the process groups of those working in `d` - the session
    abandoning its own worktree, its shell and pipeline there with it
    (`timeout` gives its command a group of its own)."""
    me = os.getpid()
    mine = {me, *ancestor_pids()} if invoker else {me}
    cwds: dict[int, str] = {}
    stats: dict[int, tuple[int, int]] = {}  # pid -> (ppid, pgid)
    if Path("/proc/self").exists():
        for e in Path("/proc").iterdir():
            if not e.name.isdigit():
                continue
            pid = int(e.name)
            try:
                cwds[pid] = str((e / "cwd").readlink())
            except OSError:
                continue  # gone, or another user's
            stat = proc_stat(pid)
            if stat is not None:
                f = stat.split()
                stats[pid] = (int(f[1]), int(f[2]))
    else:
        pid = 0
        for ln in run(["lsof", "-d", "cwd", "-F", "pn"], check=False).stdout.splitlines():
            if ln.startswith("p"):
                pid = int(ln[1:])
            elif ln.startswith("n") and pid:
                cwds[pid] = ln[1:]
        for ln in run(["ps", "-A", "-o", "pid=,ppid=,pgid="], check=False).stdout.splitlines():
            f = ln.split()
            if len(f) == 3 and all(x.isdigit() for x in f):
                stats[int(f[0])] = (int(f[1]), int(f[2]))
    def inside(cwd: str | None) -> bool:
        return cwd is not None and (Path(cwd) == d or d in Path(cwd).parents)

    groups = {stats[p][1] for p in mine if p in stats and inside(cwds.get(p))} if invoker else set()
    for pid, cwd in cwds.items():
        ppid, pgid = stats.get(pid, (0, 0))
        if pid in mine or ppid == me or pgid in groups:
            continue
        if inside(cwd):
            return True
    return False


class PruneQueries(GitQueries):
    """The prune core's queries on the subagent worktree `d`: its name lock
    (the create hook's too) and deploy lock held until close(), its lock
    file claimed by renaming it aside (git refuses to remove a locked
    worktree, so a lock renamed in meanwhile keeps it); `fetched` shares one
    fetch across a run; `invoker`: the invoking command is abandon's, its
    processes in `d` no occupants; `stop`: a stop's prune, its agent's
    ignored files counted as its own, its transcript telling whether it ran
    again."""

    def __init__(self, d: Path, fetched: list[dict[str, object]], *, deploy_held: bool = False,
                 invoker: bool = False, stop: Stop | None = None,
                 name_wait: float = 0) -> None:
        super().__init__()
        self.d = d
        self.invoker = invoker
        self.stop = stop
        self.name_wait = name_wait
        # The origin branch deletions that failed after the removal.
        self.dropped_failed: list[str] = []
        parent = sub_parent(d)
        assert parent is not None
        self.parent = parent
        self.fetched = fetched
        self.deploy_held = deploy_held
        self.locked = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "locked"]))
        self.claimed = self.locked.with_name("locked.prune")
        self.held: list[IO[str]] = []
        self.name_fd: int | None = None
        # A claimed lock not yet removed with the worktree or restored.
        self.claim_out = False

    def close(self) -> None:
        """Put back a claimed lock a failure or interrupt left aside, then
        release both locks."""
        if self.claim_out and self.d.is_dir():
            self.restore()
        for f in self.held:
            f.close()
        self.held.clear()

    def take(self, path: Path, wait: float = 0) -> IO[str] | None:
        f = hold_lock(path, wait)
        if f is not None:
            self.held.append(f)
        return f

    @staticmethod
    def read(f: Path) -> str | None:
        try:
            return f.read_text().removesuffix("\n")
        except FileNotFoundError:
            return None

    @staticmethod
    def current_lock(locked: Path) -> str | None:
        """The worktree's lock, at `locked`, or as a removal claimed it
        (one that died before restoring it still counts)."""
        lock = PruneQueries.read(locked)
        return lock if lock is not None else PruneQueries.read(locked.with_name("locked.prune"))

    def restore(self) -> None:
        # Not onto a registration whose tree git deleted: locked, it would
        # stay, and the name with it.
        if self.d.is_dir():
            with contextlib.suppress(FileExistsError, FileNotFoundError):
                os.link(self.claimed, self.locked)
        self.claimed.unlink(missing_ok=True)
        self.claim_out = False

    def claim(self, expect: object) -> bool:
        if expect is None:
            return not self.locked.exists() and not self.claimed.exists()
        if not self.locked.exists():
            return self.read(self.claimed) == expect  # claimed by a run that died
        if self.read(self.locked) != expect:
            return False
        with contextlib.suppress(FileNotFoundError):
            self.locked.rename(self.claimed)
        if self.read(self.claimed) == expect:
            return True
        self.restore()  # a new lock was renamed in between
        return False

    def __call__(self, ask: dict[str, object]) -> dict[str, object]:
        kind = ask.get("ask")
        done: dict[str, object] = {"ran": True, "detail": ""}
        if kind == "take_name_lock":
            f = self.take(name_lock_path(self.d), self.name_wait)
            if f is None:
                return {"flag": False}
            self.name_fd = f.fileno()
            return {"flag": True}
        if kind in ("release_name_lock", "release_deploy_lock"):
            return done  # close() releases both once the core is done
        if kind == "take_deploy_lock":
            if self.deploy_held:
                return {"flag": True}
            return {"flag": self.take(Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path",
                                                "nixconvoy-deploy.lock"]))) is not None}
        if kind == "lock":
            return {"text": self.current_lock(self.locked)}
        if kind == "alive":
            start = ask.get("start")
            reason = f"pid {ask.get('pid')}" + (f" start {start}" if start is not None else "")
            alive = lock_live(reason) if ask.get("own_ends") else lock_pid_alive(reason) is not False
            return {"flag": alive}
        if kind == "push_in_flight":
            return {"flag": Path(push_state("pid")).exists()}
        if kind == "nested":
            inside = [Path(ln[len("worktree "):]).resolve() for ln in out(
                ["git", "worktree", "list", "--porcelain"]).splitlines() if ln.startswith("worktree ")]
            return {"flag": any(w != self.d and self.d in w.parents for w in inside)}
        if kind == "own_branch":
            return {"text": identity_of(self.d, current_branch()).wb}
        if kind == "unpushed":
            return {"flag": bool(out(["git", "rev-list", "-n", "1", str(ask.get("sha")), "--not", "--remotes=origin"]))}
        if kind == "dirty":
            # An agent that stopped may be resumed: what git ignores (its
            # builds, notes) is kept for it too.
            ignored = ["--ignored"] if self.stop is not None else []
            return {"flag": bool(out(["git", "status", "--porcelain", *ignored])) or submodule.own(self.d)}
        if kind == "occupied":
            return {"flag": occupied(self.d, invoker=self.invoker)}
        if kind == "ran_again":
            now = None if self.stop is None else transcript_turns(self.stop.transcript)
            return {"flag": self.stop is not None and (now is None or now[0] != self.stop.turns or now[1])}
        if kind == "fetch":
            if not self.fetched:
                self.fetched.append(super().__call__(ask))
            return self.fetched[0]
        if kind == "claim_lock":
            claimed = self.claim(ask.get("expect"))
            self.claim_out = claimed and ask.get("expect") is not None
            return {"flag": claimed}
        if kind == "remove_worktree":
            submodule.remove_all(self.d)  # nested in it: their registrations go first
            os.chdir(self.parent)
            # git holds the name lock too, so a prune killed meanwhile
            # leaves it deleting under the lock.
            removed = git("worktree", "remove", str(self.d), check=False,
                          pass_fds=() if self.name_fd is None else (self.name_fd,))
            if removed.returncode != 0 and self.d.is_dir():
                os.chdir(self.d)
                return ran(removed)
            self.claim_out = False
            if removed.returncode != 0:
                # The tree went but not its registration, unlocked now.
                git("worktree", "prune", check=False)
            return done
        if kind == "restore_lock":
            self.restore()
            return done
        if kind == "drop_branches":
            cur = ask.get("cur")
            failed = drop_branches(sub_branches(self.d, cur if isinstance(cur, str) else None, [str(ask.get("wb"))]))
            self.dropped_failed = failed
            return {"ran": not failed, "detail": "\n".join(failed)}
        return super().__call__(ask)


def prune_after(pid: int) -> None:
    """`prune --after PID`: detach, prune what dead sessions left, wait for
    the claude session PID to exit, then prune again - its own agents'
    worktrees among them.  It runs from the checkout the session's agents
    hang off (a `claude --worktree` session's own root goes at its exit).
    One waiter per session (a lock file named after it in the common git
    dir); the output goes to nixconvoy-prune.log there, which starts over
    past 1 MiB."""
    stat = proc_stat(pid)
    session = f"pid {pid}" + (f" start {stat.split()[19]}" if stat else "")  # as a lock names it
    top = outermost(Path.cwd().resolve())
    common = Path(out(["git", "rev-parse", "--git-common-dir"])).resolve()
    if not detached(top):
        return
    waiter = common / f"nixconvoy-prune-after.{pid}"
    held = waiter.open("w")
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os._exit(0)
    log_to(common)

    def sweep(what: str) -> None:
        print(f"{time.strftime('%F %T')} claude session {pid} {what} ({top})", flush=True)
        try:
            close_worktrees([])
        except (Exception, SystemExit) as e:  # noqa: BLE001 - the waiter outlives a failed sweep
            proc.DYING = False
            print(f"prune failed: {e!r}", flush=True)

    try:
        sweep("started: pruning dead sessions'")
        while lock_pid_alive(session) is not False:
            time.sleep(5)
        sweep("exited: pruning its own")
    finally:
        waiter.unlink(missing_ok=True)
        sys.stdout.flush()
    os._exit(0)


def outermost(d: Path) -> Path:
    """The checkout `d`'s subagent worktrees hang off, `d` if it is none."""
    while (up := sub_parent(d)) is not None:
        d = up
    return d


def detached(top: Path) -> bool:
    """Fork a process of its own session in `top`, stdio on /dev/null:
    True in it, False in the caller."""
    if os.fork():
        return False
    os.setsid()
    if os.fork():
        os._exit(0)
    os.chdir(top)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    return True


def log_to(common: Path) -> None:
    """stdout and stderr onto nixconvoy-prune.log in the common git dir,
    started over past 1 MiB."""
    logf = common / "nixconvoy-prune.log"
    with contextlib.suppress(OSError):
        if logf.stat().st_size > 1 << 20:
            logf.replace(logf.with_suffix(".log.old"))
    log = logf.open("a")
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)


# How long after an agent's stop what continues it past the resume hook
# (a message sent it before then, Claude Code's own follow-up turn) has it
# take a turn.
STOP_GRACE = 10


def transcript_turns(path: Path) -> tuple[int, bool] | None:
    """How many turns a Claude Code transcript records its agent taking, and
    whether a prompt awaits one: an agent running again records its prompt
    at once, its turn only once a block of it is done.  Its tools' results
    (written after it stopped, too) are no prompt; an error Claude Code
    records in its place is no turn."""
    turns, pending = 0, False
    try:
        with path.open("rb") as f:
            for raw in f:
                if b'"assistant"' not in raw and b'"user"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not isinstance(msg := rec.get("message"), dict):
                    continue
                if rec.get("type") == "assistant":
                    turns += msg.get("model") != "<synthetic>"
                    pending = False
                elif rec.get("type") == "user":
                    content = msg.get("content")
                    pending |= not isinstance(content, list) or any(
                        not isinstance(b, dict) or b.get("type") != "tool_result" for b in content)
    except OSError:
        return None
    return turns, pending


def transcript_cwd(path: Path, *, last: bool = False) -> Path | None:
    """The working directory a Claude Code transcript records first (where
    its agent started), or `last` (within its final 256 KiB)."""
    def cwd(raw: bytes) -> str | None:
        try:
            rec = json.loads(raw)
        except ValueError:
            return None
        found = rec.get("cwd") if isinstance(rec, dict) else None
        return found if isinstance(found, str) else None

    try:
        with path.open("rb") as f:
            if last:
                f.seek(max(0, f.seek(0, os.SEEK_END) - (256 << 10)))
                found = next(filter(None, map(cwd, reversed(f.read().splitlines()))), None)
            else:
                found = next(filter(None, map(cwd, f)), None)
    except OSError:
        return None
    return Path(found).resolve() if found else None


def prune_stopped(session: int, stop: dict[str, object]) -> None:
    """`prune --stopped PID`: claude session PID's subagent stopped, its
    SubagentStop hook input `stop`.  Detached, logged as --after logs, the
    agent's worktree (where its transcript starts) goes when it holds
    nothing of its own, the session counted as ended for it while the
    worktree's lock is still the one it had at the stop: a resume relocks or
    recreates it first (worktree-resume.sh).  Kept while a process, a
    background task the agent started (which wakes it, bypassing that hook),
    or another running agent of the session (a skill forked there) works in
    it, or once its transcript records a turn since the stop or a prompt
    awaiting one (it runs again); the agent stops again once that work has
    woken it, and is judged again."""

    agent = str(stop.get("agent_id") or "")
    transcript = Path(str(stop.get("agent_transcript_path") or ""))
    d = transcript_cwd(transcript)
    if not agent or d is None or d.name != f"agent-{agent}" or not d.is_dir() or sub_parent(d) is None:
        return
    counted = transcript_turns(transcript)
    turns = None if counted is None else counted[0]

    tasks = stop.get("background_tasks")
    running = [t for t in tasks if isinstance(t, dict) and t.get("status") == "running"
               and t.get("id") != agent] if isinstance(tasks, list) else []
    top = outermost(d)
    os.chdir(top)
    common = Path(out(["git", "rev-parse", "--git-common-dir"])).resolve()
    locked = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "locked"], cwd=str(d)))
    stop_lock = PruneQueries.current_lock(locked)
    if not detached(top):
        return
    log_to(common)
    try:
        try:
            told = transcript.read_bytes()
        except OSError:
            told = b""
        started = [str(t.get("id")) for t in running if str(t.get("id")).encode() in told]
        forked = [str(t.get("id")) for t in running if t.get("type") == "subagent"
                  and (cwd := transcript_cwd(transcript.with_name(f"agent-{t.get('id')}.jsonl"), last=True))
                  and (cwd == d or d in cwd.parents)]
        if started:
            verdict = f"kept, its background task {started[0]} still runs"
        elif forked:
            verdict = f"kept, agent {forked[0]} works in it"
        else:
            # What continues it at once, past the resume hook (a message
            # sent it after its last tool round, which relocked it before
            # the stop): the core then keeps it, a turn recorded since.
            time.sleep(STOP_GRACE)
            with (common / "nixconvoy-prune.lock").open("w") as serial:
                fcntl.flock(serial, fcntl.LOCK_EX)
                gone = not d.is_dir()
                why = None if gone else prune_one(d, named=True, stopped=Stop(session, stop_lock, transcript, turns))
            verdict = "removed meanwhile" if gone else "empty, removed" if why is None else f"kept, {why}"
    except (Exception, SystemExit) as e:  # noqa: BLE001 - reported in the log
        verdict = f"kept, its check failed: {e!r}"
    print(f"{time.strftime('%F %T')} agent {agent} stopped: {sub_label(d)}: {verdict}", flush=True)
    os._exit(0)


def finish(wb: str) -> None:
    """The checked-out work branch after a land or abandon, now at
    origin/main's commit, moved there on origin too.  A subagent worktree
    stays, idle: `close` removes it (the session-exit sweep does so on its
    own)."""
    old = rev(f"refs/remotes/origin/{wb}") or ""
    git("push", "--quiet", "origin", f"{wb}:refs/heads/{wb}",
        f"--force-with-lease=refs/heads/{wb}:{old}")
    print(f"{wb} reset onto origin/main, here and on origin")
    here = Path.cwd().resolve()
    if sub_parent(here) is not None:
        print(f"the worktree stays; `nixconvoy close {here.name}` removes it")


def remove_sub(here: Path, branches: list[str]) -> None:
    """`close --force`: the subagent worktree `here` removed with its
    branches (`branches` and whatever it has checked out) and its submodules,
    here and on origin, whatever they hold; the process ends up in the
    parent."""
    parent = sub_parent(here)
    assert parent is not None
    names = sub_branches(here, branch_at(here), [b for b in branches if b])
    # Under the name lock, so no create reuses it between the check and the
    # removal; git holds it too, through a kill of this process.
    held = hold_lock(name_lock_path(here), wait=60)
    if held is None:
        die(f"{here} stayed locked for 60s by a create or a close; re-run")
    if worktree_lock(here) is not None:
        git("worktree", "unlock", str(here))
    submodule.remove_all(here)
    os.chdir(parent)
    git("worktree", "remove", "--force", str(here), pass_fds=(held.fileno(),))
    # Still held: a create of the name reuses no branch about to go.
    failed = drop_branches(names)
    held.close()
    if failed:
        die("\n".join(failed))
    print(f"subagent worktree {here.relative_to(parent)} removed, with "
          f"{', '.join(names) or 'no branch of its own'} here and on origin")


def sub_branches(here: Path, cur: str | None, branches: list[str]) -> list[str]:
    """The subagent worktree `here`'s own branches only - its work branch
    (`branches`), the fresh one it was created on (a hand commit's
    post-commit hook pushes it) and the checked-out `cur` when named after
    it - never another checkout's it may have checked out to read.  The
    fresh one goes when it holds nothing beyond origin/main."""
    fresh = f"worktree-{here.name}"
    names = [b for b in {cur, *branches} if b is not None and (b in branches or b == fresh or b.endswith(f"-{here.name}"))]
    if fresh not in names and rev(f"refs/heads/{fresh}") and is_ancestor(fresh, "refs/remotes/origin/main"):
        names.append(fresh)
    return names


def drop_branches(names: list[str]) -> list[str]:
    """Delete `names` here and on origin, remembering origin's tips in
    PAST_REFS (on_origin's memory): the origin deletions that failed."""
    gone = {b: rev(f"refs/remotes/origin/{b}") for b in names}
    failed = []
    for b in names:
        git("branch", "--quiet", "-D", b, check=False)
    on_origin = {b: old for b, old in gone.items() if old}
    for b, old in on_origin.items():
        # Every tip the remote-tracking ref's reflog remembers (a branch
        # reset onto main by a land or abandon before this), oldest first,
        # so the deletion of that ref with its reflog forgets none.
        past = out(["git", "log", "-g", "--format=%H", f"refs/remotes/origin/{b}"]).split()[::-1] or [old]
        git("update-ref", "--create-reflog", f"{PAST_REFS}/{b}", past[0], check=False)
        for tip in past[1:]:
            git("update-ref", "-m", "past tip", f"{PAST_REFS}/{b}", tip, check=False)
    # A gone branch's announcement is cleared with it (never deleted: a
    # deleted ref lingers uncleared in every other clone).
    clears = [spec for b in names for spec in clear_refspec(b)]
    if on_origin or clears:
        # One push, bounded: prune holds the worktree's name lock meanwhile.
        # Retried once: origin has rejected a deletion out of the blue.
        cmd = ["git", "-c", "core.hooksPath=/dev/null", "push", "--quiet", "origin",
               *(f":refs/heads/{b}" for b in on_origin), *clears,
               *(f"--force-with-lease=refs/heads/{b}:{old}" for b, old in on_origin.items())]
        pushed = run(cmd, check=False, timeout=30)
        if pushed.returncode != 0:
            time.sleep(2)
            pushed = run(cmd, check=False, timeout=30)
        if pushed.returncode != 0:
            failed.append(f"deleting {', '.join(on_origin)} on origin failed: "
                          f"{(pushed.stderr or '').strip() or 'timed out'}")
    # A review of a branch gone this long is no longer expected.
    cutoff = time.time() - 90 * 86400
    past = run(["git", "for-each-ref", "--format=%(refname) %(committerdate:unix)", PAST_REFS], check=False)
    for ln in past.stdout.splitlines():
        ref, _, when = ln.partition(" ")
        if when and int(when) < cutoff:
            git("update-ref", "-d", ref, check=False)
    return failed


def sub_label(d: Path) -> str:
    parent = sub_parent(d)
    return f"{parent.name}/{d.name}" if parent is not None else str(d)


def subs_of(top: Path) -> list[Path]:
    """The subagent worktrees nested in the checkout `top`."""
    return sorted(d for d in sibling_checkouts() if sub_parent(d) == top)


def holds_work(d: Path) -> bool:
    """Whether the checkout `d` has anything an abandon would discard:
    commits beyond origin/main, or staged or unstaged edits."""
    head = run(["git", "rev-parse", "HEAD"], cwd=str(d), check=False).stdout.strip()
    return bool(head and not is_ancestor(head, "refs/remotes/origin/main")) \
        or bool(run(["git", "status", "--porcelain", "-uno"], cwd=str(d), check=False).stdout)


def enter_sub(name: str) -> None:
    """Act in the subagent worktree `name` - or the one whose name `name`
    is a unique prefix of - of any checkout on this machine (land/abandon
    run from elsewhere, as status suggests)."""
    os.chdir(find_sub(name))


def find_sub(name: str) -> Path:
    here = Path.cwd().resolve()
    subs = sorted((d for d in [here, *sibling_checkouts()] if sub_parent(d) is not None),
                  key=lambda d: (d.name, str(d)))
    own = [d for d in subs if sub_parent(d) == here]
    # The exact name anywhere, else a prefix: this checkout's first.
    found = ([d for d in subs if d.name == name]
             or [d for d in own if d.name.startswith(name)]
             or [d for d in subs if d.name.startswith(name)])
    if not found:
        die(f"no subagent worktree {name}* in any checkout here" + (
            f"; there are: {', '.join(map(sub_label, subs))}" if subs else ""))
    if len(found) > 1:
        die(f"{name} is ambiguous among the subagent worktrees {', '.join(map(sub_label, found))}")
    return found[0]


def status() -> None:
    """Every checkout on this machine as a tree, and the work branches only
    origin holds (cli docstring: Status)."""
    paint = painter()
    git(*fetch_args("--no-auto-gc"), check=False)
    main = rev("refs/remotes/origin/main")
    tips = origin_tips()
    pats = allowed_branches()
    here = Path.cwd().resolve()
    checkouts = [here, *sibling_checkouts()]

    def g(d: Path, *args: str) -> str:
        try:
            return run(["git", *args], cwd=str(d), check=False).stdout.strip()
        except FileNotFoundError:  # a subagent worktree pruned since it was listed
            return ""

    def count(d: Path, *args: str) -> int:
        return len(g(d, *args).splitlines())

    def line(d: Path) -> tuple[str, bool]:
        """The checkout's state after its name, and whether it is quiet -
        nothing of its own, nothing pending, no agent at work - and so
        rendered gray as a whole.  A submodule holding anything keeps it lit."""
        cur = g(d, "symbolic-ref", "--short", "-q", "HEAD") or None
        head = g(d, "rev-parse", "HEAD")
        ident = identity_of(d, cur, pats)
        # A subagent's commits of its own: beyond main and beyond the parent's
        # (any listed branch's) pushed tip it forked from.
        beyond = ["origin/main"] + ([f"refs/remotes/origin/{b}" for b in tips
                                     if b != cur and any(fnmatchcase(b, p) for p in pats)]
                                    if ident.kind == "sub" else [])
        ahead = int(g(d, "rev-list", "--count", "HEAD", "--not", *beyond) or 0)
        behind = int(g(d, "rev-list", "--count", "HEAD..origin/main") or 0)
        own = ahead  # commits of its own: a review checkout's are on origin
        if cur is None and ahead and on_origin(str(d), pats):
            own = 0
        counts = {label: count(d, *args) for label, args in (
            ("staged", ["diff", "--cached", "--name-only"]),
            ("unstaged", ["diff", "--name-only"]),
            ("untracked", ["ls-files", "--others", "--exclude-standard"]))}
        live = False  # a subagent worktree's lock: its session alive, or a hand lock
        alive: bool | None = False
        sessionless = False
        if ident.kind == "sub":
            reason = worktree_lock(d)
            sessionless = reason is not None and reason.endswith(SESSIONLESS)
            alive = lock_pid_alive(reason) if reason is not None else False
            live = alive is not False
        op = git_op_in_progress(d)
        deploying = deploy_running(d)
        quiet = (not own and not any(counts.values()) and not live and op is None and not deploying
                 and not submodule.own(d))
        p = (lambda text, _style: text) if quiet else paint
        if cur is None:
            where = (p("detached at origin/main", "green") if head == main
                     else p(f"detached at {head[:12]}, nothing of its own", "green")
                     if not own else p(f"detached, {ahead} ahead of origin/main", "red"))
        elif cur == "main":
            where = p("main", "green") + (p(f", {ahead} unpushed", "red") if ahead else "")
        elif ahead:
            tip = tips.get(cur)
            pushed = (p("pushed", "green") if tip == head
                      else p("unpushed" if tip else "not on origin", "red"))
            where = f"{p(cur, 'yellow')} +{ahead} {pushed}"
        else:
            where = p(f"{cur} = origin/main (idle)", "green")
        if behind:
            where += p(f", {behind} behind main", "magenta")
        notes: list[str] = []
        if ident.wb and cur != ident.wb and ident.kind != "main":
            notes.append(p(f"owns {ident.wb}", "dim"))
        notes += [p(f"{label} {n}", "cyan") for label, n in counts.items() if n]
        if own and cur not in (None, "main"):
            held = sorted(held_targets("origin/main..HEAD", str(d)))
            if held:
                notes.append(p(f"holds back {', '.join(held)}", "yellow"))
        notes += [p(a, "cyan") for a in submodule.notes(d)]
        if ident.kind == "sub" and not own and not any(counts.values()) and cur != ident.wb:
            if sessionless:
                notes.append(p(f"empty, its create found no claude session: nixconvoy close {d.name} "
                               "removes it", "dim"))
            elif alive is None:
                notes.append(p("empty, locked by hand: the sweep keeps it", "dim"))
            elif live:
                notes.append(p("empty, locked by its agent's claude session: closed when its agent "
                               "stops or that session exits", "dim"))
            else:
                notes.append(p(f"empty: nixconvoy close {d.name} removes it", "dim"))
        msg = g(d, "show", f"HEAD:{MSG_FILE}").partition("\n")[0] if cur not in (None, "main") else ""
        if msg:
            notes.append(p(f'"{msg[:60]}"', "dim"))
        if op:
            notes.append(p(f"[{op} in progress]", "bold red"))
        if deploying:
            notes.append(p("[deploying]", "bold red"))
        text = "  ".join([where, *notes])
        return (paint(text, "dim") if quiet else text), quiet

    def parent(d: Path) -> Path:
        return sub_parent(d) or d

    def is_main(d: Path) -> bool:
        return identity_of(d, g(d, "symbolic-ref", "--short", "-q", "HEAD") or None, pats).kind == "main"

    tops = sorted({parent(d) for d in checkouts}, key=lambda p: (not is_main(p), p.name))
    width = max(len(d.name) + 4 for d in checkouts)

    def show(indent: str, d: Path, pad: int) -> None:
        try:
            text, quiet = line(d)
        except FileNotFoundError:
            return  # a subagent worktree pruned since it was listed
        label = d.name if indent else d.name + "/"
        print(f"{indent}{paint(label.ljust(pad), 'dim' if quiet else 'bold')}  {text}")

    for top in tops:
        if top in checkouts:
            show("", top, width)
        else:
            name = paint(f"{top.name + '/':<{width}}", "bold")
            print(f"{name}  {paint('(not a checkout here)', 'dim')}")
        for d in sorted(d for d in checkouts if d != top and parent(d) == top):
            show("  └ ", d, width - 4)
    local = {g(d, "symbolic-ref", "--short", "-q", "HEAD") for d in checkouts}
    local |= set(out(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]).splitlines())
    elsewhere = []
    for name, tip in sorted(tips.items()):
        if name == "main" or name in local or not any(fnmatchcase(name, p) for p in pats):
            continue
        if main and is_ancestor(tip, main):
            continue
        n = out(["git", "rev-list", "--count", f"origin/main..{tip}"])
        elsewhere.append(f"{name} +{n}: {summarize(tip)}")
    if elsewhere:
        print(paint("on origin only (other machines' checkouts):", "dim"))
        for e in elsewhere:
            print(f"  {paint(e, 'yellow')}")
    repos = submodule.configured()
    if repos:
        print(paint(f"submodules: {', '.join(sorted(repos))} (nixconvoy open --submodule <input>)", "dim"))
    stray = submodule.repo_notes()
    if stray:
        print(paint("submodule repositories edited outside any checkout:", "dim"))
        for e in stray:
            print(f"  {paint(e, 'red')}")
    pending = pending_announcements()
    if pending:
        print(paint("deploys under way (announced increments):", "dim"))
        now = time.time()
        for a in pending:
            entry = f"{a.key}: {summarize(a.sha)}" + (" (settled, awaiting its push)" if a.settled else "")
            # Display only: the announcer's clock, not this machine's.
            silent = now - a.at
            if a.settled:
                print(f"  {paint(entry, 'yellow')}")
            elif announcer_dead(a):
                print(f"  {paint(f'{entry} - its process (pid {a.pid} here) is gone', 'red')}")
            elif silent > WAIT_STALE:
                print(f"  {paint(f'{entry} - not renewed for {silent / 60:.0f} min: dead?', 'red')}")
            else:
                print(f"  {paint(entry, 'yellow')}")


def open_worktree(name: str, lock_reason: str | None) -> Path:
    """`nixconvoy open <name>`: the task worktree <root>/<worktrees>/<name>
    (nixconvoy.toml's `worktrees`; root: the checkout this one's subagent
    worktrees hang off) on the fresh branch worktree-<name> off origin's
    default branch - a worktree of that name is reused - owning <root's
    branch>-<name> from its first deploy.  Under the name lock prune holds
    while judging a worktree of the name.  `lock_reason` is the worktree
    lock Claude Code's create hook names its session with (replacing a
    reused one's); without it a hand-opened worktree is locked as
    sessionless, which prune keeps and land or abandon lift."""
    if not re.fullmatch(r"[\w.-]+", name):
        die(f"{name}: a worktree name holds letters, digits, '.', '-' and '_' only")
    root = outermost(Path.cwd().resolve())
    d = root / sub_dir() / name
    # A nested worktree not ignored is an untracked directory `git add -A`
    # would stage as a gitlink.
    if not ok(["git", "check-ignore", "-q", "--no-index", str(d)], cwd=str(root)):
        die(f"{sub_dir()}/ must be gitignored (a nested worktree): add it to .gitignore, "
            f"or point nixconvoy.toml's `worktrees` at a directory that is")
    branch = f"worktree-{name}"
    base = default_base(root)
    reason = lock_reason or f"nixconvoy open {name} {SESSIONLESS}"
    held = hold_lock(name_lock_path(d), wait=60)
    if held is None:
        die(f"{d} stayed locked for 60s by a close or another open; re-run")
    try:
        for attempt in range(3):
            if attempt:
                time.sleep(1)
            if d.is_dir() and run(["git", "rev-parse", "--show-toplevel"], cwd=str(d),
                                  check=False).stdout.strip() == str(d):
                if lock_reason is not None:
                    # Reused: its lock replaced in one rename.
                    locked = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "locked"],
                                      cwd=str(d)))
                    locked.with_suffix(".tmp").write_text(lock_reason)
                    locked.with_suffix(".tmp").replace(locked)
                print(d)
                return d
            git("worktree", "prune", cwd=str(root), check=False)
            d.parent.mkdir(parents=True, exist_ok=True)
            add = ([str(d), branch] if rev(f"refs/heads/{branch}") else ["-b", branch, str(d), base])
            p = git("worktree", "add", "--quiet", "--lock", "--reason", reason, *add, cwd=str(root),
                    check=False, pass_fds=(held.fileno(),))
            if p.returncode == 0:
                print(d)
                return d
            print(f"open: git worktree add failed: {(p.stderr or '').strip()}", file=sys.stderr)
    finally:
        held.close()
    return die(f"could not create or reuse {d} (git's errors above)")


def default_base(root: Path) -> str:
    """origin's default branch as fetched (origin/HEAD, else origin/main),
    else HEAD."""
    return next((r for r in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main")
                 if run(["git", "rev-parse", "-q", "--verify", r], cwd=str(root), check=False).returncode == 0),
                "HEAD")


def open_checkout(path: Path, branch: str | None) -> Path:
    """`nixconvoy open <path>`: an agent checkout - a worktree of this
    repository at `path`, outside every checkout, on a work branch of this
    machine's: `branch`, else <branch_prefix><N> for the digits `path`'s
    name ends with when that branch is listed and free, else the first
    listed free number.  The branch is taken as origin holds it (a removed
    checkout's), else new off origin's default branch; its upstream is
    origin/main as nixconvoy keeps it.  Prints the path."""
    here = Path.cwd().resolve()
    d = path.expanduser().resolve()
    if d.exists():
        die(f"{d} exists already")
    git(*fetch_args("--no-auto-gc"), check=False)
    mine = git_common(here)
    up = d.parent
    while up != up.parent:
        if (up / ".git").exists():
            if git_common(up) == mine:
                die(f"{d} would be nested in the checkout {up}: `nixconvoy open <name>` makes a task "
                    f"worktree there; an agent checkout goes beside the checkouts")
            break
        up = up.parent
    pats = allowed_branches()
    tips = origin_tips()
    local = set(out(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]).split())
    prefix = this_prefix()

    def free(b: str) -> bool:
        return b not in local and b not in tips

    if branch is None:
        m = re.search(r"(\d+)$", d.name)
        if m and owned(f"{prefix}{m.group(1)}", pats) and free(f"{prefix}{m.group(1)}"):
            branch = f"{prefix}{m.group(1)}"
        else:
            branch = next((f"{prefix}{n}" for n in range(1, 1000)
                           if owned(f"{prefix}{n}", pats) and free(f"{prefix}{n}")), None)
        if branch is None:
            die(f"no free work branch {prefix}N is listed in deploy-branches; name one with --branch")
    elif not owned(branch, pats):
        die(f"{branch} is not a work branch of this machine's: one named {prefix}... that "
            f"deploy-branches lists")
    elif branch.removeprefix(prefix) == "main" or "-" in branch.removeprefix(prefix):
        # "<leaf>-<name>" is a subagent worktree's branch, "main-<name>" the main checkout's.
        die(f"{branch}: a checkout's branch is {prefix}<leaf> with a leaf that is not 'main' "
            f"and holds no '-'")
    d.parent.mkdir(parents=True, exist_ok=True)
    if branch in local:
        p = git("worktree", "add", "--quiet", str(d), branch, check=False)
    elif branch in tips:
        p = git("worktree", "add", "--quiet", "-b", branch, str(d), f"refs/remotes/origin/{branch}", check=False)
    else:
        p = git("worktree", "add", "--quiet", "-b", branch, str(d), default_base(here), check=False)
    if p.returncode != 0:
        die(f"git worktree add failed:\n{(p.stderr or '').strip()}")
    git("branch", "--quiet", "--set-upstream-to=origin/main", branch, check=False)
    print(d)
    print(f"checkout {d.name} on {branch}" + (" (origin's copy)" if branch in tips and branch not in local
                                               else ""), file=sys.stderr)
    return d


def close_checkout(path: Path, *, force: bool = False) -> None:
    """`nixconvoy close <path>`: the agent checkout at `path` removed with
    its work branch, here and on origin (the tip kept under PAST_REFS),
    when it holds nothing of its own - no edits, nothing beyond
    origin/main, no submodule holding anything - or `force`."""
    here = Path.cwd().resolve()
    d = path.expanduser().resolve()
    if d == here:
        die("run close from another checkout")
    if git_common(d) is None or git_common(d) != git_common(here):
        die(f"{d} is no worktree of this repository")
    if git_common(d) == (d / ".git").resolve():
        die(f"{d} is the primary worktree, which holds the repository")
    if sub_parent(d) is not None:
        die(f"{d} is a subagent worktree: `nixconvoy close {d.name}`")
    git(*fetch_args("--no-auto-gc"), check=False)
    nested = subs_of(d)
    if nested:
        die(f"{d} holds the subagent worktrees {', '.join(w.name for w in nested)}: close them first")
    os.chdir(d)
    lock = lock_checkout()
    os.chdir(here)
    if lock is None:
        die(f"a nixconvoy is running in {d}")
    cur = branch_at(d)
    wb = identity_of(d, cur).wb
    head = out(["git", "rev-parse", "HEAD"], cwd=str(d))
    # Its submodule worktrees are its own (removed with it): a pin they moved is not an edit.
    edits = [ln for ln in run(["git", "status", "--porcelain"], cwd=str(d), check=False).stdout.splitlines()
             if ln[3:] not in submodule.paths(d)]
    tip = rev(f"refs/heads/{wb}") if wb else None
    kept = ("edits or untracked files" if edits
            else "commits not on origin/main" if not is_ancestor(head, "refs/remotes/origin/main")
            else f"its work branch {wb}, with commits not on origin/main"
            if tip and not is_ancestor(tip, "refs/remotes/origin/main")
            else "a submodule holding something" if submodule.own(d) else None)
    if kept and not force:
        die(f"{d} holds {kept}: land or abandon it first (--force removes it anyway)")
    if worktree_lock(d) is not None:
        git("worktree", "unlock", str(d), check=False)
    submodule.remove_all(d)
    lock.close()
    git("worktree", "remove", "--force", str(d))
    names = [b for b in {cur, wb} if b and owned(b)]
    failed = drop_branches(names)
    if failed:
        die("\n".join(failed))
    print(f"checkout {d} removed, with {', '.join(sorted(names)) or 'no branch of its own'} here and on origin")


def land_union(release: bool = False, message: str | None = None) -> None:
    """`nixconvoy land --union`: this checkout and its subagent worktrees
    landed as one commit on main - each sub's branch merged into this
    branch's tree (mergiraf on the conflicts, as a deploy's union) under
    this checkout's MSG_FILE (or `message`) with each sub's MSG_FILE
    appended - every branch reset onto the new main, here and on origin,
    each worktree staying."""
    await_background_push()
    git(*fetch_args())
    pop_tmp_commits()
    here = Path.cwd().resolve()
    if sub_parent(here) is not None:
        die("land --union runs from the checkout the worktrees hang off")
    wb = require_work_branch("land --union")
    if current_branch() != wb:
        die(f"land runs from the work branch ({wb}); currently on {current_branch() or '(detached HEAD)'}")
    if staged_differs():
        die("staged but undeployed changes present; deploy or unstage them first")
    ensure_rebased()
    main = rev("refs/remotes/origin/main")
    if main is None:
        die("origin/main not found")
    main_tree = out(["git", "rev-parse", f"{main}^{{tree}}"])
    msg = main_message(wb, message)

    # The participants: each sub on its work branch with work beyond main.
    parts: list[tuple[Path, str]] = []
    locks: list[IO[str]] = []
    try:
        for d in subs_of(here):
            label = sub_label(d)
            cur = branch_at(d)
            ident = identity_of(d, cur)
            head = out(["git", "rev-parse", "HEAD"], cwd=str(d))
            if not ok(["git", "diff", "--cached", "--quiet"], cwd=str(d)):
                die(f"{label} has staged but undeployed changes; deploy or unstage them first")
            if is_ancestor(head, main) or tree_less_msg("HEAD", str(d)) == main_tree:
                continue
            if cur != ident.wb or not ident.wb:
                die(f"{label} holds commits on {cur or 'a detached HEAD'} that were never deployed: "
                    f"deploy or abandon them first")
            if git_op_in_progress(d):
                die(f"{label} has a {git_op_in_progress(d)} in progress")
            os.chdir(d)
            lock = lock_checkout()
            os.chdir(here)
            if lock is None:
                die(f"another nixconvoy is running in {label}")
            locks.append(lock)
            parts.append((d, ident.wb))
        if not parts:
            die(f"no subagent worktree under {here.name} holds work to land; `nixconvoy land` "
                f"lands this checkout alone")
        # Each sub's MSG_FILE, as its branch carries it, appended in turn.
        for d, _ in parts:
            if ok(["git", "cat-file", "-e", f"HEAD:{MSG_FILE}"], cwd=str(d)):
                sub_msg = out(["git", "show", f"HEAD:{MSG_FILE}"], cwd=str(d)).strip()
                if sub_msg:
                    msg = f"{msg}\n\n{sub_msg}"
        own = {wb, *(b for _, b in parts)}
        for d, b in [(here, wb), *parts]:
            carried = [x for x, tip in listed_tips(allowed_branches()).items()
                       if x not in own and is_ancestor(tip, "HEAD" if d == here else f"refs/heads/{b}")
                       and not is_ancestor(tip, main)]
            if carried:
                die(f"{b} contains the unlanded increments of {', '.join(carried)}; land that branch "
                    f"first, then re-run")
            gone = submodule.orphaned(d)
            if gone:
                die(f"the tree pins {b}'s rev of submodule {', '.join(gone)}, whose worktree is gone: "
                    f"`nixconvoy open --submodule <input>` or `abandon --submodule <input>` in {d.name} first")
        for d, b in [(here, wb), *parts]:
            holds(b, "HEAD" if d == here else f"refs/heads/{b}", main, release)

        def merged_tree() -> str:
            """The branches merged in a throwaway worktree on this branch's
            tip (a gitlink both moved on the union of its branches, as a
            deploy's union): its tree."""
            wt = add_worktree("HEAD")
            try:
                for d, b in parts:
                    tip = out(["git", "rev-parse", "HEAD"], cwd=str(d))
                    p = git("merge", "--quiet", "--no-edit", "-m", f"union {b}", tip, cwd=wt, env=MACHINE_ENV,
                            check=False)
                    if p.returncode == 0:
                        continue
                    for f in out(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt).splitlines():
                        if f == MSG_FILE:
                            git("rm", "--quiet", "-f", f, cwd=wt, check=False)
                        elif (submodule.solve_union(Path(wt), f) if submodule.at_path(Path(wt), f)
                              else solve_with_mergiraf(wt, f)):
                            git("add", "--", f, cwd=wt, check=False)
                        else:
                            git("merge", "--abort", cwd=wt, check=False)
                            die(f"{b} conflicts with the rest in {f}: resolve it (rebase {b} onto {wb} in "
                                f"{sub_label(d)}), or `nixconvoy land {d.name}` alone first")
                    git("commit", "--quiet", "--no-edit", "--no-verify", cwd=wt, env=MACHINE_ENV)
                return tree_less_msg("HEAD", wt)
            finally:
                drop_worktree(wt)

        def clashes(tree: str) -> None:
            """Each worktree is reset onto the landed commit with its unstaged
            edits kept (reset --keep), which an edited or untracked file the
            others changed would stop: told now, before anything moves."""
            for d in [here, *(d for d, _ in parts)]:
                loose = set(out(["git", "diff", "--name-only"], cwd=str(d)).splitlines()) - {MSG_FILE}
                loose |= set(out(["git", "ls-files", "--others", "--exclude-standard"], cwd=str(d)).splitlines())
                moved = set(out(["git", "diff", "--name-only", "HEAD", tree], cwd=str(d)).splitlines())
                if clash := sorted(loose & moved):
                    die(f"{d.name if d == here else sub_label(d)} has unstaged edits or untracked files "
                        f"the landing changes: {', '.join(clash)}; stage and deploy them, or put them aside")

        # Proven to merge before any submodule lands (which cannot be undone).
        clashes(merged_tree())
        for d, b in [(here, wb), *parts]:
            os.chdir(d)
            submodule.land_all(b, msg)
            os.chdir(here)
        tree = merged_tree()
        if tree == main_tree:
            die("no changes vs origin/main to land")
        clashes(tree)

        commit = out(["git", "commit-tree", tree, "-p", main, "-m", msg])
        p = git("push", "--quiet", "origin", f"{commit}:refs/heads/main", check=False)
        if p.returncode != 0:
            die(f"pushing main failed (moved since fetch?); re-run:\n{p.stderr.strip()}")
        git("branch", "-f", "main", commit, check=False)
        print(f"landed on main: {summarize(commit)}")
        for d, b in [(here, wb), *parts]:
            os.chdir(d)
            git("checkout", "--quiet", "--", MSG_FILE, check=False)  # consumed: the commit's message
            if git("reset", "--quiet", "--keep", commit, check=False).returncode != 0:
                # Landed all the same: the branch moves, the working tree
                # showing the others' changes until `git checkout -- .`.
                git("reset", "--quiet", commit)
                print(f"note: {d.name}'s working tree kept as it was (a file in the way); "
                      f"`git checkout -- .` there brings it onto the landing", file=sys.stderr)
            Path(MSG_FILE).unlink(missing_ok=True)
            finish(b)
            os.chdir(here)
    finally:
        for lock in locks:
            lock.close()
    landed = {d for d, _ in parts}
    if not (catch_up([d for d in sibling_checkouts() if d not in landed]) & submodule.catch_up_repos()):
        die("some checkouts could not be brought onto the new main (see above); "
            "fix them and `nixconvoy pull`")


def land_submodule(name: str, message: str | None = None) -> None:
    """`land --submodule <name>`: the one submodule landed on its
    repository's default branch and pinned there as a commit on the work
    branch, pushed; its worktree stays, and the submodule repositories'
    own checkouts follow (submodule.catch_up_repos).  The checkout's own
    landing is untouched."""
    await_background_push()
    git(*fetch_args())
    pop_tmp_commits()
    wb = require_work_branch("land --submodule")
    if current_branch() != wb:
        die(f"land runs from the work branch ({wb}); currently on {current_branch() or '(detached HEAD)'}")
    if staged_differs():
        die("staged but undeployed changes present; deploy or unstage them first")
    ensure_rebased()
    sub = submodule.present_one(Path.cwd(), name)
    on_file = out(["git", "show", f"HEAD:{MSG_FILE}"]) if rev(f"HEAD:{MSG_FILE}") else ""
    submodule.land_one(sub, wb, on_file.partition("\n")[0] or f"{name} landed", message)
    push_work_branch(wb)
    if not submodule.catch_up_repos():
        sys.exit(1)


def abandon_submodule(name: str) -> None:
    """`abandon --submodule <name>`: the one submodule reset onto its
    repository's default branch, the pin restored to origin/main's and
    staged for the next deploy; its worktree stays."""
    await_background_push()
    git(*fetch_args())
    pop_tmp_commits()
    wb = require_work_branch("abandon --submodule")
    top = Path.cwd()
    sub = submodule.present_one(top, name)
    submodule.main_pin(sub, top)  # nothing reset for a restore that cannot follow
    submodule.reset_one(sub, wb)
    if not submodule.restore(sub, top):
        print(f"submodule {name}: the tree still pins what origin/main pins; nothing to revert")


__all__ = [
    "abandon",
    "abandon_submodule",
    "close_checkout",
    "close_submodule",
    "close_worktrees",
    "enter_sub",
    "holds_work",
    "land",
    "land_submodule",
    "land_union",
    "open_checkout",
    "open_worktree",
    "prune_after",
    "pull",
    "status",
    "subs_of",
    "work_branch_name",
]
