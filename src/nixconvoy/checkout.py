"""The checkouts of the repo on this machine and their work branches:
identity, locks, the staged snapshot, pushes, and the git queries
nixconvoy-plan's rebase core asks."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, NamedTuple

from . import config, proc, strings
from .config import (
    CLAUDE_WORKTREES,
    CONFIG_FILE,
    MSG_FILE,
    ConfigError,
    cfg,
    load,
    main_ref,
    remote_ref,
)
from .guard import decide
from .proc import PERF, die, git, is_ancestor, ok, out, rev, run, solve_with_mergiraf
from .wire import (
    AddAll,
    AddWorktree,
    Commit,
    Contains,
    CurrentBranch,
    Detach,
    DropWorktree,
    EnsureRebased,
    Fate,
    Fetch,
    Flag,
    HeadSubject,
    Ok,
    OnOrigin,
    OnWorkBranch,
    OpInProgress,
    PlanQuery,
    PlanReply,
    Pop,
    PopTmp,
    Push,
    Pushed,
    Ran,
    Rebase,
    RebaseAbort,
    RebaseParams,
    RepoNames,
    Rev,
    SetUpstream,
    StagedDiffers,
    Switch,
    Sync,
    Text,
    Upstream,
)

# Identity for machinery commits (snapshots, union merges); the fixed date
# makes an unchanged union re-create the same commit hash.  Work-branch and
# landing commits use the user's identity.
MACHINE_ENV = {
    "GIT_AUTHOR_NAME": "nixconvoy",
    "GIT_AUTHOR_EMAIL": "deploy@nixconvoy",
    "GIT_COMMITTER_NAME": "nixconvoy",
    "GIT_COMMITTER_EMAIL": "deploy@nixconvoy",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}
# A throwaway worktree of a run (the union's, a rebase aside): never a
# checkout, whatever `git worktree list` says.
TMP_PREFIX = "nixconvoy-"
# Origin's ref beside the branches for a deploy under way, deleted by the
# push that ends it: the deploy's announcement of its pending state
# (PENDING_REFS/<branch>, pushed before anything is built - or, with
# nothing pending, once there are targets to switch; a main checkout's,
# cleared by the push of main that lands its announced HEAD, is swept by
# the next deploy's end), which carries each target's deploy record as a
# parent from before its switch until refs/heads/<DEPLOYED_PREFIX>/<target>
# holds it.  A dead deploy's announcement stays, its records with it,
# until a deploy there ends well.  Fetches prune, so a deletion reaches every clone; local copies
# are never deleted by hand, since every worktree on the machine reads
# the same refs.
PENDING_REFS = "refs/nixconvoy/deploying/branch"
DEPLOYED_PREFIX = "nixconvoy-history"
# A deploy renews its announcement every HEARTBEAT seconds while it runs; a
# deploy waiting for it (deploy.await_pending) takes it for dead and goes
# on without it - to be refused by the drift core, which names the dead
# deploy, if a target it reaches still runs that increment - when its last
# renewal (the commit's committer date, the announcer's clock) was already
# STALE_STAMP seconds old at the fetch that first brought it (the fetch's
# start by the waiter's clock), or once the waiter has seen no renewal for
# WAIT_STALE seconds of its own clock.  The stamp rule spares every later
# deploy the WAIT_STALE wait on a key a deploy left behind long ago; it is
# the one place two machines' clocks meet, so its margin is minutes, more
# than any clock still being set drifts, and a key seen renewed between
# two looks is alive whatever its stamp.
HEARTBEAT = 10
WAIT_STALE = 30
STALE_STAMP = 600
# A renewal push slower than this is given up (the next renewal retries):
# one hanging past WAIT_STALE would have the deploy taken for dead.  A
# withdrawal too: one that never lands costs other deploys at most WAIT_STALE.
PUSH_TIMEOUT = 15


# Wall time at the start of the last fetch of origin that succeeded (the
# refs are at least that fresh), 0.0 before any: what an announcement's
# stamp is judged against (deploy.news_of).
FETCHED_AT = 0.0


def fetch_origin(*flags: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """`git fetch origin` (fetch_args), stamping FETCHED_AT on success."""
    global FETCHED_AT
    started = time.time()
    p = git(*fetch_args(*flags), check=check)
    if p.returncode == 0:
        FETCHED_AT = started
    return p


def fetch_args(*flags: str) -> list[str]:
    """`git fetch origin` of the branches and nixconvoy's own refs."""
    return ["fetch", "--quiet", "--prune", *flags, cfg().remote,
            f"+refs/heads/*:refs/remotes/{cfg().remote}/*", f"+{PENDING_REFS}/*:{PENDING_REFS}/*"]


def work_shape() -> str:
    """The shape of the work branches nixconvoy may union or own: the
    invoking checkout's working-tree copy of nixconvoy.toml's
    branch_prefix (config.root(), wherever the run has moved to), read
    afresh each time (a rebase may have changed it)."""
    try:
        return load(config.root()).branch_prefix
    except ConfigError as e:
        die(str(e))


@functools.cache
def shape_re(shape: str) -> re.Pattern[str]:
    """`shape` as a prefix pattern, {machine} in it any one path segment."""
    return re.compile("[^/]+".join(re.escape(p) for p in shape.format(machine="\0").split("\0")))


def listed(name: str, shape: str | None = None) -> bool:
    """Whether `name` is a work branch: shaped like `shape` (the work shape)."""
    return shape_re(work_shape() if shape is None else shape).match(name) is not None


def this_prefix() -> str:
    return cfg().branch_prefix.format(machine=proc.this_machine())


def owned(name: str) -> bool:
    """A work branch this machine may write: named with its prefix (another
    machine's is only ever unioned)."""
    return name.startswith(this_prefix())


class Checkout(NamedTuple):
    # "main" (no work branch of its own), "agent" (a work branch) or "sub"
    # (a subagent worktree nested in another checkout).
    kind: str
    wb: str | None


def sub_dir() -> Path:
    """Where `open --child <name>` puts a child worktree, under the
    checkout: nixconvoy.toml's child_worktrees, or Claude Code's own place
    in a repository nixconvoy does not configure (the plugin's run.sh
    makes one there without nixconvoy; by hand, nixconvoy does the same)."""
    if not Path(CONFIG_FILE).is_file():
        return Path(CLAUDE_WORKTREES)
    return Path(cfg().child_worktrees)


def git_common(d: Path) -> Path | None:
    """The common git dir of the worktree at `d`, read off its .git (a
    directory in the primary worktree, a gitdir: file in a linked one)
    without running git; None when `d` is no worktree."""
    dotgit = d / ".git"
    try:
        if dotgit.is_dir():
            return dotgit.resolve()
        text = dotgit.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    g = Path(text[len("gitdir:"):].strip())
    if not g.is_absolute():
        g = d / g
    return (g.parent.parent if g.parent.name == "worktrees" else g).resolve()


def sub_parent(d: Path) -> Path | None:
    """The checkout the subagent worktree at `d` is nested in - the nearest
    directory above it holding a .git, when that is a worktree of the
    same repository (a planted submodule worktree is another repository's);
    None when `d` is not one."""
    if not re.fullmatch(r"[\w.-]+", d.name) or (mine := git_common(d)) is None:
        return None
    up = d.parent
    while up != up.parent:
        if (up / ".git").exists():
            return up if git_common(up) == mine else None
        up = up.parent
    return None


def is_checkout(d: Path) -> bool:
    """Whether `d` is a checkout rather than a run's throwaway worktree."""
    return not d.name.startswith(TMP_PREFIX)


def branch_at(d: Path) -> str | None:
    p = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=str(d), check=False)
    return p.stdout.strip() or None


def branch_of(d: Path) -> str | None:
    """The branch the checkout at `d` is on, or is rebasing in place (HEAD
    detached meanwhile, the branch named in the rebase's state)."""
    if (b := branch_at(d)) is not None:
        return b
    for state in ("rebase-merge", "rebase-apply"):
        try:
            name = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", f"{state}/head-name"],
                            cwd=str(d))).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        return name.removeprefix("refs/heads/") or None
    return None


STATE_DIR = "nixconvoy"


def state_path(name: str, cwd: Path | None = None) -> Path:
    """nixconvoy's file `name` in the checkout's (`cwd`, else the current
    one's) own git dir: .git/nixconvoy/<name>, under .git/worktrees/<it>/
    for a linked worktree, as `git rev-parse --git-path` places it; the
    directory made where it can be (a read of a missing file in a git dir
    that cannot be written fails as the read)."""
    return state_dir(Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", STATE_DIR],
                             cwd=None if cwd is None else str(cwd)))) / name


def common_state_path(name: str, common: Path) -> Path:
    """nixconvoy's file `name` under `common`, a repository's common git
    dir: .git/nixconvoy/<name>, shared by its worktrees."""
    return state_dir(common / STATE_DIR) / name


def state_dir(d: Path) -> Path:
    with contextlib.suppress(OSError):
        d.mkdir(exist_ok=True)
    return d


def note_prefix(top: Path) -> None:
    """This machine's expanded branch_prefix, and origin/main by its
    configured names, written into the repository's common git dir for the
    shell prompt (integrations/shell/prompt.bash), which shows a work
    branch by its leaf and its lead over main: bash reads a line there
    where it could not read nixconvoy.toml at every prompt.  Written by
    `init` and refreshed by every later command; a repository without a
    readable nixconvoy.toml (a submodule's, `review` works there) gets
    none, and nothing else changes."""
    common = git_common(top)
    if common is None:
        return
    try:
        c = config.load(top)
    except config.ConfigError:
        return
    for name, text in (("branch-prefix", c.branch_prefix.format(machine=proc.this_machine())),
                       ("main-ref", c.names.main_short)):
        with contextlib.suppress(OSError):
            p = common_state_path(name, common)
            if not p.exists() or p.read_text() != text + "\n":
                p.write_text(text + "\n")


def identity_of(here: Path, cur: str | None) -> Checkout:
    """What the checkout at `here` with `cur` checked out is: on one of
    this machine's work branches it owns that branch (a subagent's
    when it is nested in another checkout); a subagent worktree on
    any other branch owns <parent's work branch>-<name> (<branch_prefix>main-<name>
    under the main checkout); anything else is the main checkout, with no
    work branch of its own."""
    parent = sub_parent(here)
    if cur and owned(cur):
        return Checkout("sub" if parent is not None else "agent", cur)
    if parent is not None:
        pb = branch_of(parent)
        base = pb if pb and owned(pb) else this_prefix() + "main"
        return Checkout("sub", f"{base}-{here.name}")
    return Checkout("main", None)


def checkout_identity() -> Checkout:
    return identity_of(Path.cwd().resolve(), current_branch())


def work_branch_name() -> str | None:
    """The work branch of the current checkout; None for the main checkout."""
    return checkout_identity().wb


def require_work_branch(what: str) -> str:
    wb = work_branch_name()
    if wb is None:
        die(strings.CHECKOUT_NEEDS_WB(what=what, prefix=this_prefix()))
    return wb


def current_branch() -> str | None:
    p = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], check=False)
    return p.stdout.strip() or None


def staged_differs(cwd: str | None = None, *, less_msg: bool = False) -> bool:
    """Whether the index differs from HEAD (`less_msg`: MSG_FILE aside,
    which a landing reads from the worktree and never deploys - unless
    unmerged, which the excluding diff would pass over)."""
    if less_msg and out(["git", "ls-files", "-u", "--", f":(top){MSG_FILE}"], cwd=cwd):
        return True
    return not ok(["git", "diff", "--cached", "--quiet", "--ignore-submodules=none",
                   *(["--", f":(top,exclude){MSG_FILE}"] if less_msg else [])], cwd=cwd)


def message_on_file(cwd: str | None = None) -> str:
    """MSG_FILE as a landing reads it: the worktree's, tracked or not (a
    staged one need not have been deployed); "" without one."""
    try:
        return (Path(cwd or ".") / MSG_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def unstage_msg(cwd: str | None = None) -> bool:
    """MSG_FILE's index entry back to HEAD's, the file itself staying: a
    landing reads it off the worktree, and the commits it puts on the
    branch (a submodule's pin) and the resets a failure answers with
    take the index for clean.  Whether it was staged: a landing refused
    past this point puts it back (restage_msg), staged for the deploy
    the refusal asks for."""
    had = bool(out(["git", "diff", "--cached", "--name-only", "--", f":(top){MSG_FILE}"], cwd=cwd))
    git("reset", "--quiet", "--", MSG_FILE, cwd=cwd)
    return had


def restage_msg(cwd: str | None = None) -> None:
    """The draft back on the index as the worktree has it (its deletion too)."""
    git("add", "--all", "--", f":(top){MSG_FILE}", cwd=cwd, check=False)


LOCK_FILE = "deploy.lock"


def lock_checkout() -> IO[str] | None:
    """The current checkout's deploy lock: a flock on a file in its git dir,
    held by the returned open file, or None when another run holds it.  It
    serialises nixconvoy runs in one checkout: a deploy commits the snapshot tree
    onto HEAD at its end, so `pull` must not rebase HEAD under it.  The
    holder's pid, start time and arguments go in the file, for lock_holder."""
    # "a+", so that a contender does not truncate the holder's line.
    f = state_path(LOCK_FILE).open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    write_holder(f)
    return f


def write_holder(f: IO[str]) -> None:
    """This run as a lock's holder line (the deploy lock's, a worktree name
    lock's): pid, start time, the pid's /proc start time (0 without /proc)
    and the arguments."""
    stat = proc_stat(os.getpid())
    start = stat.split()[19] if stat is not None else "0"
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()} {int(time.time())} {start} {shlex.join(sys.argv[1:])}\n")
    f.flush()


def lock_holder(d: Path | None = None) -> str:
    """The run holding the checkout's (`d`, else the current one) lock, for
    the refusal: its arguments, pid and age as write_holder wrote them.  A
    recorded pid that is gone (or reused, by its /proc start time) left the
    lock to a child of its own (a finished run's background push); an
    older build wrote nothing."""
    where = d or Path.cwd()
    try:
        line = state_path(LOCK_FILE, where).read_text()
    except OSError:
        line = ""
    held = holder_of(line)
    if held is None:
        return strings.CHECKOUT_LOCK_UNKNOWN_HOLDER()
    what, alive, _ = held
    return what if alive else strings.CHECKOUT_LOCK_CHILD_OF(what=what)


def flock_holder(path: Path) -> int | None:
    """A live process other than this one with `path` open - a child the
    lock was passed to, once the run that wrote the holder line is gone:
    the one /proc/locks names as the flock's holder where it lists it (not
    in WSL2's pid namespace), else the first found with the descriptor (a
    waiter's counts: it waits on a live holder in turn).  None without
    /proc, or when none has it."""
    try:
        target, ino = str(path.resolve()), str(path.stat().st_ino)
        pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
        locks = Path("/proc/locks").read_text().splitlines()
    except OSError:
        return None
    named = [int(f[4]) for f in map(str.split, locks)
             if len(f) >= 6 and f[1] == "FLOCK" and f[4].isdigit() and f[5].rsplit(":", 1)[-1] == ino]
    for pid in [*named, *pids]:
        if pid <= 0 or pid == os.getpid():
            continue
        with contextlib.suppress(OSError):
            if any(str(fd.readlink()) == target for fd in Path(f"/proc/{pid}/fd").iterdir()):
                return pid
    return None


def holder_of(line: str) -> tuple[str, bool, int] | None:
    """The run a write_holder line names, as the refusals say it (its
    arguments, pid and age), whether its pid is alive (by /proc start
    time, so a reused pid does not pass), and the pid; None for no such
    line."""
    fields = line.strip().split(" ", 3)
    if len(fields) < 3 or not all(x.isdigit() for x in fields[:3]):
        return None
    pid, started, start = int(fields[0]), int(fields[1]), fields[2]
    age = max(0, int(time.time()) - started)
    for_ = (strings.CHECKOUT_AGE_SECONDS(n=age) if age < 90 else strings.CHECKOUT_AGE_MINUTES(n=round(age / 60))
            if age < 5400 else strings.CHECKOUT_AGE_HOURS(h=age / 3600))
    what = (strings.CHECKOUT_HOLDER_CMD(args=fields[3]) if len(fields) == 4 and fields[3]
            else strings.CHECKOUT_HOLDER_CMD_BARE())
    if lock_pid_alive(f"pid {pid}" + (f" start {start}" if start != "0" else "")) is False:
        return strings.CHECKOUT_HOLDER_GONE(what=what, pid=pid, age=for_), False, pid
    return strings.CHECKOUT_HOLDER_RUNNING(what=what, pid=pid, age=for_), True, pid


def deploy_running(d: Path) -> bool:
    """Whether an nixconvoy holds the checkout's lock (lock_checkout): a shared
    lock, tried and dropped at once, conflicts only with its exclusive one.
    No lock file yet means no run ever took it."""
    lock = state_path(LOCK_FILE, d)
    if not lock.exists():
        return False
    try:
        with lock.open("r") as f:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    return False


def git_op_in_progress(cwd: Path | None = None) -> str | None:
    """The rebase, merge, cherry-pick or revert the checkout (`cwd`, else the
    current one) is in the middle of, if any."""
    for marker, name in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                         ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                         ("REVERT_HEAD", "revert")):
        where = cwd or Path.cwd()
        if (where / out(["git", "rev-parse", "--git-path", marker], cwd=str(where))).exists():
            return name
    return None


def ran(p: subprocess.CompletedProcess[str]) -> Ran:
    return Ran(p.returncode == 0, (p.stderr or "").strip())


class GitQueries:
    """Performs nixconvoy-plan's git queries in the current checkout, and in
    the throwaway worktree a rebase of a branch not checked out here
    (`aside`) runs in."""

    def __init__(self, names: RepoNames | None = None) -> None:
        self.aside = ""
        self.aside_branch = ""
        # The repository's names (a submodule's own, when the core runs there).
        self.names = names or cfg().names

    def __call__(self, ask: PlanQuery) -> PlanReply:
        match ask:
            case Contains(a, b):
                return Flag(is_ancestor(a, b))
            case OpInProgress():
                return Text(git_op_in_progress())
            case CurrentBranch():
                return Text(current_branch())
            case StagedDiffers():
                return Flag(staged_differs())
            case Commit(msg):
                return ran(git("commit", "--quiet", "-m", msg, check=False))
            case AddAll():
                return ran(git("add", "-A", check=False))
            case Pop(mixed):
                return ran(git("reset", "--quiet", "--mixed" if mixed else "--soft", "HEAD~1", check=False))
            case HeadSubject():
                return Text(git("log", "-1", "--format=%s", check=False).stdout.strip() or None)
            case Rebase(keep_empty, aside, upstream):
                return self.rebase(keep_empty, self.aside if aside else None, upstream)
            case RebaseAbort(aside):
                return ran(git("rebase", "--abort", cwd=self.aside if aside else None, check=False))
            case Rev(name):
                return Text(rev(name))
            case Fetch():
                return ran(fetch_origin("--no-auto-gc", check=False))
            case Push(branch):
                problem = sole_writer_problem(branch)
                if problem is not None:
                    return Ran(False, problem)
                head = rev(f"refs/heads/{branch}")
                p = git(*push_cmd(branch, rev(remote_ref(branch)) or ""), check=False)
                if p.returncode == 0 and head:
                    note_pushed(branch, head)
                return ran(p)
            case AddWorktree(branch):
                self.aside = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}rebase-wt.", dir="/tmp")
                self.aside_branch = branch
                return ran(git("worktree", "add", "--quiet", self.aside, branch, check=False))
            case DropWorktree():
                if self.aside:
                    git("worktree", "remove", "--force", self.aside, check=False)
                    shutil.rmtree(self.aside, ignore_errors=True)
                    self.aside = ""
                return Ran(True, "")
            case Detach(upstream):
                return ran(git("switch", "--quiet", "--detach", upstream, check=False))
            case Pushed():
                return Flag(any(is_ancestor("HEAD", t) for t in listed_tips(names=self.names).values()))
            case OnOrigin():
                return Flag(on_origin(names=self.names))
            case Switch(branch, start):
                return ran(git("switch", "--quiet", *(["-c", branch, start] if start else [branch]), check=False))
            case Upstream(branch):
                remote = git("config", f"branch.{branch}.remote", check=False).stdout.strip()
                merge = git("config", f"branch.{branch}.merge", check=False).stdout.strip()
                return Text(f"{remote}\t{merge}" if remote and merge else None)
            case SetUpstream(branch):
                return ran(git("branch", f"--set-upstream-to={self.names.main_short}", branch, check=False))
            case Fate(branch, tip, union, checkout):
                return Text(increment_fate(branch, tip, union, checkout))
            case _:
                raise ValueError(f"nixconvoy-plan asked {ask!r}")

    def rebase(self, keep_empty: bool, where: str | None, upstream: str) -> Ran:
        from . import submodule  # noqa: PLC0415 - submodule builds on this module

        empty = "--empty=keep" if keep_empty else "--empty=drop"
        p = git("rebase", "--quiet", empty, *rebase_range(upstream, where), cwd=where, check=False)
        # Stopped: on text conflicts mergiraf solves, or on gitlinks,
        # where main pins a submodule this branch pinned too (its
        # landing elsewhere) - the submodule follows (solve_stop).
        why = ""
        met: list[str] = []  # the gitlinks a hand rebase meets again, nixconvoy's solutions undone with the abort
        while p.returncode != 0:
            met = list(dict.fromkeys(met + unmerged_gitlinks(where)))
            why = solve_stop(where, upstream)
            if why is not None:
                break
            if ok(["git", "diff", "--cached", "--quiet"], cwd=where):
                # Resolved to what HEAD has already (a pin whose
                # counterpart HEAD pins: a submodule commit its rebase
                # dropped): kept as an empty commit where the core
                # counts on its commits (its tmp commits), else skipped.
                if not keep_empty:
                    p = git("rebase", "--skip", cwd=where, env={"GIT_EDITOR": "true"}, check=False)
                    continue
                stopped = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path",
                                    "rebase-merge/stopped-sha"], cwd=where)).read_text().strip()
                git("commit", "--quiet", "--allow-empty", "--no-verify", "-C", stopped, cwd=where)
            p = git("rebase", "--continue", cwd=where, env={"GIT_EDITOR": "true"}, check=False)
        if p.returncode == 0:
            submodule.push_pending()
            return ran(p)
        submodule.undo_pending()
        if why:  # a gitlink the submodule could not follow: the failure explained, git's noise dropped
            noise = ("error: Could not read ", "error: could not apply ", "Could not apply ")
            rest = "\n".join(ln for ln in (p.stderr or "").splitlines()
                             if ln.strip() and not ln.startswith(noise) and not ln.startswith("hint:"))
            return Ran(False, why + (f"\n{rest}" if rest else ""))
        there = f"git {rebase_hint(upstream, where)}" + (f" {self.aside_branch}" if where else "")
        hint = strings.CHECKOUT_REBASE_HINT(command=there)
        if met:  # git checkout --theirs leaves a gitlink unmerged: the stage is written outright
            wt = Path(where or Path.cwd()).resolve()
            fixes = []
            for g in met:
                fixes.append(f"git update-index --cacheinfo {submodule.GITLINK},$(git rev-parse :3:{g}),{g}")
                sub = submodule.owner(wt, g)
                if sub is not None:  # both moved its pin file too; the next deploy rewrites it
                    fixes.append(f"git checkout --theirs -- {sub.pin_file}")
            hint += strings.CHECKOUT_REBASE_GITLINK_HINT(gitlinks=", ".join(met), fixes="\n  ".join(fixes))
        return Ran(False, strings.CHECKOUT_REBASE_FAILED(hint=hint, stderr=(p.stderr or "").strip()))


def pop_tmp_commits() -> None:
    """Unwind the tmp commits a run that died mid-rebase left on the
    checked-out branch (never pushed)."""
    decide(RebaseParams(cfg().names, PopTmp()))


def ensure_rebased() -> None:
    """Make HEAD contain origin/main, its staged and unstaged changes intact,
    or die leaving the checkout as it was."""
    decide(RebaseParams(cfg().names, EnsureRebased()))


def sibling_checkouts(here: Path | None = None) -> list[Path]:
    """The other checkouts of this repo on this machine (`here`'s, the
    current directory's by default): its worktrees (not a run's throwaway
    ones) and the clones beside it with the same origin."""
    here = (here or Path.cwd()).resolve()
    origin = out(["git", "remote", "get-url", cfg().remote], cwd=str(here))
    found: set[Path] = set()

    def worktrees(of: Path) -> None:
        for ln in run(["git", "worktree", "list", "--porcelain"], cwd=str(of),
                      check=False).stdout.splitlines():
            if ln.startswith("worktree "):
                found.add(Path(ln[len("worktree "):]).resolve())

    def has_git(d: Path) -> bool:
        try:  # a sibling of the repo may be root-only (/var/lib/private)
            return d.is_dir() and (d / ".git").exists()
        except OSError:
            return False

    worktrees(here)
    for d in (sub_parent(here) or here).parent.iterdir():
        if d != here and has_git(d):
            url = run(["git", "remote", "get-url", cfg().remote], cwd=str(d), check=False).stdout
            if url.strip() == origin:
                worktrees(d)
    return [d for d in sorted(found) if d != here and d.is_dir() and is_checkout(d)]


def update_checkout(path: Path) -> tuple[bool, str]:
    """Fetch and bring the checkout at `path` onto origin/main under its
    deploy lock, pushing its work branch (origin's copy is what every other
    deploy unions), then its submodule worktrees onto their default
    branches (submodule.catch_up): (ok, what happened).  A checkout that
    cannot be is left exactly as it was; a submodule that cannot be is
    named, the checkout itself brought along."""
    from . import submodule  # noqa: PLC0415 - submodule builds on this module

    prev = Path.cwd()
    os.chdir(path)
    try:
        lock = lock_checkout()
        if lock is None:
            return False, strings.CHECKOUT_SKIPPED_LOCKED(holder=lock_holder())
        try:
            wb = work_branch_name()
            done = decide(RebaseParams(cfg().names, Sync(wb)))
            good, status = isinstance(done, Ok), done.value
            if good and wb is not None:
                problems = submodule.catch_up(path, wb)
                if problems:
                    good, status = False, "; ".join([status, *problems])
        finally:
            lock.close()
    finally:
        os.chdir(prev)
    return good, status


def catch_up(paths: list[Path]) -> bool:
    """update_checkout each of `paths`, printing one status line each;
    whether all succeeded."""
    good = True
    for d in paths:
        try:
            done, status = update_checkout(d)
        except FileNotFoundError as e:
            if not d.is_dir():
                continue  # a subagent worktree pruned meanwhile
            done, status = False, strings.CHECKOUT_NOT_CAUGHT_UP(error=e)
        if not done and not d.is_dir():
            continue
        print(strings.CHECKOUT_CATCH_UP_LINE(name=d.name, status=status), file=sys.stdout if done else sys.stderr,
              flush=True)
        good &= done
    return good


def ensure_on_work_branch(wb: str) -> None:
    prev = current_branch()
    decide(RebaseParams(cfg().names, OnWorkBranch(wb)))
    if (prev is not None and prev not in (wb, cfg().main_branch) and current_branch() == wb
            and checkout_identity().kind == "sub"):
        # The fresh branch a subagent's worktree was created on, replaced.
        git("branch", "--quiet", "-D", prev, check=False)


def fold_subject(msg: str) -> str:
    """A blank line after the subject, or git folds the whole first
    paragraph (subject + bullets) into %s."""
    lines = msg.splitlines()
    if len(lines) > 1 and lines[1].strip():
        return "\n".join([lines[0], "", *lines[1:]])
    return msg


def landing_message(given: str | None, on_file: str, where: str) -> str:
    """What a landing of `where` commits under: `given` (land -m) when
    passed, else `on_file` (its MSG_FILE as the landing reads it,
    message_on_file) - "" when neither says anything.  -m beside a
    MSG_FILE is refused: it is for a branch without one."""
    if given is None:
        return fold_subject(on_file.strip())
    if on_file.strip():
        die(strings.CHECKOUT_LAND_MSG_AND_M(where=where))
    return fold_subject(given.strip())


KEPT_EDIT = "landmsg"  # in the git dir (state_path): a landing's edited message, until it lands


def editor() -> str | None:
    """The command edit_message would run (`git var GIT_EDITOR`): None off a
    terminal (an agent's shell: `! nixconvoy land` in a Claude Code
    session) or with the editor set to `:`."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("TERM") == "dumb":
        return None
    cmd = run(["git", "var", "GIT_EDITOR"], check=False).stdout.strip()
    return cmd if cmd and cmd != ":" else None


def edit_message(draft: str, wb: str, tips: list[tuple[str, str]] | None = None) -> str | None:
    """`draft` (the landing's message so far, "" for none) as the user leaves
    it in git's editor (editor()), on a terminal: a landing run by hand gets
    to adjust what an agent wrote; None when no editor runs, the draft
    standing.  Below a scissors line, dropped with everything under it as
    git's scissors cleanup does (so a `#` heading in the draft survives) -
    and each note line dropped wherever it is, for a user who deleted the
    scissors line instead - the increments and changed paths of each branch
    landing (`tips`: label and rev; this checkout's HEAD when None); a
    message left empty aborts.  SIGINT and SIGQUIT are caught and ignored
    here while it runs, as under git, so a Ctrl-C is the editor's alone
    (a handler, not SIG_IGN, which the editor would inherit)."""
    cmd = editor()
    if cmd is None:
        return None
    main = main_ref()
    notes = [strings.CHECKOUT_NOTE_LANDING(wb=wb, main_branch=cfg().main_branch)]
    for label, tip in tips or [("", "HEAD")]:
        of = strings.CHECKOUT_NOTE_OF(label=label) if label else ""
        notes += [strings.CHECKOUT_NOTE_INCREMENTS(of=of),
                  *(f"  {ln}" for ln in out(["git", "log", "--format=%s", f"{main}..{tip}"]).splitlines()),
                  strings.CHECKOUT_NOTE_CHANGES(of=of),
                  *(f"  {ln}" for ln in out(["git", "diff", "--name-status", f"{main}...{tip}", "--", ".",
                                              f":!{MSG_FILE}"]).splitlines())]
    note_lines = {f"# {n}" for n in notes}
    tmpdir = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}editmsg.", dir="/tmp")
    path = Path(tmpdir) / "COMMIT_EDITMSG"  # the name editors style as a commit message
    handlers = {sig: signal.signal(sig, lambda *_: None) for sig in (signal.SIGINT, signal.SIGQUIT)}
    try:
        path.write_text("".join([draft.rstrip("\n"), "\n\n", strings.CHECKOUT_SCISSORS, "\n",
                                 *(f"# {n}\n" for n in notes)]), encoding="utf-8")
        with PERF.excluded():
            p = run(["sh", "-c", f'{cmd} "$@"', cmd, str(path)], capture=False, check=False)
        if p.returncode != 0:
            die(strings.CHECKOUT_EDITOR_FAILED(cmd=cmd, rc=p.returncode))
        lines = [ln.rstrip() for ln in path.read_text(encoding="utf-8").splitlines()]
    finally:
        for sig, h in handlers.items():
            signal.signal(sig, h)
        shutil.rmtree(tmpdir, ignore_errors=True)
    if strings.CHECKOUT_SCISSORS in lines:
        del lines[lines.index(strings.CHECKOUT_SCISSORS):]
    lines = [ln for ln in lines if ln not in note_lines]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        die(strings.CHECKOUT_EMPTY_MESSAGE)
    return fold_subject("\n".join(lines))


def warn_no_msgfile(wb: str) -> None:
    if not message_on_file().strip():
        print(strings.CHECKOUT_NO_MSG_FILE_YET(wb=wb), file=sys.stderr)


def index_tree(cwd: str | None = None) -> str:
    """The tree of the index, through a copy, since write-tree on the real
    one would rewrite its cache-tree extension."""
    fd, tmpidx = tempfile.mkstemp()
    os.close(fd)
    try:
        shutil.copy(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "index"], cwd=cwd), tmpidx)
        p = run(["git", "write-tree"], env={"GIT_INDEX_FILE": tmpidx}, cwd=cwd, check=False)
        if p.returncode != 0:
            die(strings.CHECKOUT_INDEX_UNMERGED(of=strings.CHECKOUT_INDEX_OF(cwd=cwd) if cwd else ""))
        return p.stdout.strip()
    finally:
        Path(tmpidx).unlink()


def staged_snapshot() -> str:
    """Ephemeral deterministic commit of the staged state on top of HEAD:
    what gets built and switched, never what lands on a branch."""
    return out(["git", "commit-tree", index_tree(), "-p", "HEAD",
                "-m", strings.CHECKOUT_SNAPSHOT_SUBJECT], env=MACHINE_ENV)


def deployed_commit(snap: str, msg: str) -> str:
    """The work-branch commit of the increment `snap` deploys: exactly the
    snapshot's tree on HEAD, with the user's identity.  Made before the
    switch, so the provenance the targets get names the commit origin will hold
    (a later deploy anywhere can then tell what became of it); dangling if
    the switch fails."""
    tree = out(["git", "rev-parse", f"{snap}^{{tree}}"])
    return out(["git", "commit-tree", tree, "-p", "HEAD", "-m", msg])


def adopt_commit(wb: str, c: str) -> None:
    """`c` (deployed_commit) onto the work branch once its increment runs.
    The soft reset leaves the index alone, so anything staged mid-deploy
    stays staged on top."""
    git("reset", "--quiet", "--soft", c)
    warn_no_msgfile(wb)


def commit_deployed(wb: str, snap: str, msg: str) -> None:
    adopt_commit(wb, deployed_commit(snap, msg))


def push_state(kind: str) -> Path:
    """The background push's pid/status/log file (in the git dir)."""
    return state_path(f"push.{kind}")


def await_background_push() -> None:
    """Collect the previous run's background push (push_deployed) before
    anything fetches or pushes: wait for it, report a failure (this run's
    push retries it) and clear its files."""
    pidf, statusf, logf = (push_state(k) for k in ("pid", "status", "log"))
    if not pidf.exists():
        return
    try:
        pid = int(pidf.read_text().strip())
    except ValueError:
        pid = 0
    deadline = time.monotonic() + 60
    while pid > 0 and not statusf.exists() and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:  # gone without a status
            break
        time.sleep(0.05)
    if statusf.exists():
        status = statusf.read_text().strip()
    else:
        status = strings.CHECKOUT_PUSH_TIMED_OUT if pid > 0 else strings.CHECKOUT_PUSH_NO_PID
        if pid > 0:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGTERM)
    log = logf.read_text().strip() if logf.exists() else ""
    for f in (pidf, statusf, logf):
        with contextlib.suppress(OSError):
            f.unlink()
    if status != "0":
        print(strings.CHECKOUT_PUSH_BG_FAILED(status=status, log=strings.CHECKOUT_PUSH_BG_LOG(log=log) if log else ""),
              file=sys.stderr)


# The tip this repository last pushed onto a work branch (or took from
# origin's copy at open): the proof that origin's copy is ours to push over.
PUSHED_REFS = "refs/nixconvoy/pushed"


def pushed_tip(wb: str) -> str | None:
    return rev(f"{PUSHED_REFS}/{wb}")


def note_pushed(wb: str, sha: str) -> None:
    git("update-ref", f"{PUSHED_REFS}/{wb}", sha, check=False)


def forget_pushed(wb: str) -> None:
    git("update-ref", "-d", f"{PUSHED_REFS}/{wb}", check=False)


def adopt_origin_tip(wb: str) -> None:
    """Origin's copy of the work branch, taken as this checkout's (open by
    name), noted as pushed."""
    up = rev(remote_ref(wb))
    if up is not None:
        note_pushed(wb, up)


def sole_writer_problem(wb: str) -> str | None:
    """Why origin's tip of the work branch is not this checkout's to push
    over, or None: it is, when it is on origin/main (reset, landed or
    undone from elsewhere), in the local branch, the tip this repository
    last pushed or took from origin (PUSHED_REFS), or a tip the local
    branch has held (its reflog: a push the note missed, from before the
    note or cut off after the push).  Otherwise another checkout commits
    onto it.  The deploy decides before its rebase moves the local
    branch, so the message's remedy, which brings origin's commits into
    it, is seen; after a rebase the note or the reflog decides."""
    up = rev(remote_ref(wb))
    local = rev(f"refs/heads/{wb}")
    if up is None or local is None or is_ancestor(up, main_ref()) or is_ancestor(up, local) \
            or pushed_tip(wb) == up:
        return None
    held = run(["git", "reflog", "show", "--format=%H", f"refs/heads/{wb}"], check=False).stdout.split()
    if not held or up in held:
        return None
    return strings.CHECKOUT_SOLE_WRITER(wb=wb, remote=cfg().remote, sha=up[:12], up_ref=remote_ref(wb),
                                        prefix=this_prefix())


def ensure_sole_writer(wb: str) -> str:
    """Die unless origin's tip of the work branch is this checkout's
    (sole_writer_problem): before a deploy rebases or switches, a land
    pushes main or an undeploy resets anything, and before every push of
    the branch.  Returns that tip ("" for none), the lease the push takes
    (push_cmd), so a fetch between the check and the push changes nothing."""
    problem = sole_writer_problem(wb)
    if problem is not None:
        die(problem)
    return rev(remote_ref(wb)) or ""


def push_refspecs(wb: str, lease: str) -> list[str]:
    """The work branch's local tip, CAS against `lease`: origin's tip as
    ensure_sole_writer proved it this checkout's ("" for no branch)."""
    return [f"{wb}:refs/heads/{wb}", f"--force-with-lease=refs/heads/{wb}:{lease}"]


def push_cmd(wb: str, lease: str) -> list[str]:
    return ["push", "--quiet", cfg().remote, *push_refspecs(wb, lease)]


def push_work_branch(wb: str) -> None:
    """Push the work branch (a deploy's end pushes in the background instead:
    push_deployed)."""
    head = rev(f"refs/heads/{wb}")
    if head is None or rev(remote_ref(wb)) == head:
        return
    p = git(*push_cmd(wb, ensure_sole_writer(wb)), check=False)
    if p.returncode != 0:
        die(strings.CHECKOUT_PUSH_WB_FAILED(wb=wb, remote=cfg().remote, stderr=p.stderr.strip()))
    note_pushed(wb, head)
    clear = clear_refspec(wb)
    if clear:
        git("push", "--quiet", cfg().remote, *clear, check=False)
    print(strings.CHECKOUT_PUSHED(wb=wb, sha=head[:12] if head else "?"))


DATELESS_ENV = {k: v for k, v in MACHINE_ENV.items() if not k.endswith("_DATE")}


class Announce:
    """A deploy's announcement of its pending state - the staged increment,
    or a HEAD origin lacks: a commit of its tree on origin's
    PENDING_REFS/<branch>, pushed before anything is evaluated and renewed
    every HEARTBEAT seconds on a thread for as long as the deploy runs.  A
    deploy starting elsewhere waits for this one while it lives when their
    changes reach a common target (deploy.await_pending), and takes it for
    dead once the renewals stop.  The author date is the deploy's start,
    its rank among the deploys under way (the older one is waited for);
    the subject names this machine, checkout and pid.  Once the deploy
    knows what each target will run, the renewals carry the targets'
    deploy records as further parents (carry), so origin holds them before
    any target is switched; a deploy with nothing pending announces then,
    for that alone.  The push that ends
    the deploy clears it (push_deployed; for main, the owner's push of
    main does, landing the announced HEAD: pending_announcements);
    withdraw clears one whose deploy ended without switching anything."""

    def __init__(self, branch: str, base: str) -> None:
        self.branch = branch
        self.key = announce_key(branch)
        self.target = ("", "")
        # A re-run keeps carrying the records its last run left (until the
        # stage carries its own), and the rank of an announcement whose
        # increment a target runs or was being switched onto (the records:
        # other deploys are refused there until this one ends); one that
        # switched nothing ranks as the fresh run it is, waiting its turn
        # behind the deploys that outlived it.
        prev = next((p for p in pending_announcements() if p.key == self.key), None)
        self.records: tuple[str, ...] = prev.records if prev is not None else ()
        self.since = prev.since if prev is not None and prev.carried else int(time.time())
        self._lock = threading.Lock()
        self.retarget(base)
        self.sha = ""
        self.detail = ""
        # The targets running this checkout's pending state, or whose
        # switch onto it was started: deploy keeps the announcement when it
        # ends without its push with one.
        self.claimed: list[str] = []
        self._ok: bool | None = None
        self._landed = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()

    def retarget(self, base: str) -> None:
        """What the renewals commit, resolved on the main thread: HEAD as
        the parent and `base`'s tree (the snapshot's, or HEAD's) - never
        read by the thread itself, since a rebase under way has temporary
        commits on HEAD.  The records stay carried until a stage carries its
        own (carry, which a redone stage drops the stale ones through)."""
        target = (rev("HEAD") or "", rev(f"{base}^{{tree}}") or "")
        with self._lock:
            self.target = target

    def carry(self, records: dict[str, str]) -> str | None:
        """Renew at once with each target's deploy record as a further
        parent (the renewals keep them): what failed, or None once origin
        holds them."""
        self.records = tuple(records.values())
        return None if self._push() else self.detail

    def _commit(self, settled: bool) -> str:
        """The announcement's commit as it stands: "" when it failed
        (self.detail says why).  Under the lock."""
        parent, tree = self.target
        subject = (strings.CHECKOUT_ANNOUNCE_SUBJECT(branch=self.branch, machine=proc.this_machine(), cwd=Path.cwd(),
                                                     pid=os.getpid()) + (SETTLED if settled else ""))
        parents = [a for p in (parent, *self.records) for a in ("-p", p)]
        commit = run(["git", "commit-tree", tree, *parents, "-m", subject],
                     env={**DATELESS_ENV, "GIT_AUTHOR_DATE": f"{self.since} +0000"}, check=False)
        if commit.returncode != 0:
            self.detail = (commit.stderr or "").strip() or strings.CHECKOUT_COMMIT_TREE_FAILED
            return ""
        return commit.stdout.strip()

    def _push(self) -> bool:
        # Off the main thread: nothing here may die().  Serialised: a
        # renewal and a carry at once would race for self.sha, the lease
        # withdraw deletes on.
        with self._lock:
            sha = self._commit(settled=False)
            if not sha:
                return False
            p = git("push", "--quiet", "--force", cfg().remote, f"{sha}:{PENDING_REFS}/{self.key}",
                    check=False, timeout=PUSH_TIMEOUT)
            if p.returncode != 0:
                self.detail = (p.stderr or "").strip() or strings.CHECKOUT_PUSH_FAILED
                return False
            self.sha = sha
            return True

    def _beat(self) -> None:
        try:
            self._ok = self._push()
        except Exception:  # noqa: BLE001 - die() killed the push
            self._ok = False
        finally:
            self._landed.set()
        while self._ok and not self._stop.wait(HEARTBEAT):
            try:
                if not self._push():
                    print(strings.CHECKOUT_RENEW_FAILED(detail=self.detail), file=sys.stderr)
            except Exception:  # noqa: BLE001
                return

    def collect(self) -> bool:
        """Whether the announcement reached origin, waiting for its first
        push (a provenance names only an announcement origin got)."""
        self._landed.wait(70)
        return bool(self._ok)

    def stop(self) -> None:
        """End the renewals (the announcement stays as it is)."""
        self._stop.set()
        self._thread.join(70)

    def settled_commit(self) -> str:
        """The renewals ended, a last one marked settled for the push that
        ends the deploy to carry (push_deployed, with the rest of it): the
        deploy is over, its push pending (a main checkout's, which its
        owner makes), so nobody waits for it or takes it for dead; its
        targets stay refused until then.  "" when the announcement never
        reached origin."""
        self.stop()
        with self._lock:
            return self._commit(settled=True) if self._ok else ""

    def withdraw(self) -> None:
        """Delete the announcement: the deploy ended without a switch, so
        the next deploy anywhere need not wait for it; after die() too.
        Leased on the last renewal that landed; unleased when none did,
        since a push that landed but timed out here must not strand it."""
        self.stop()
        with proc.finishing():
            git("push", "--quiet", cfg().remote, *delete_args({f"{PENDING_REFS}/{self.key}": self.sha}),
                check=False, timeout=PUSH_TIMEOUT)


def announce_key(branch: str) -> str:
    """The pending ref's name for `branch`'s announcement: the branch - its
    one checkout is the ref's one writer - or, for main,
    main@<machine>-<checkout> (the checkout by a hash of its path): every
    main checkout has a pending state of its own, and two on one machine
    (a nightly's and a hand deploy's) must not renew over each other's
    carried records."""
    if branch != cfg().main_branch:
        return branch
    return f"{branch}@{proc.this_machine()}-{hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:8]}"


class Pending(NamedTuple):
    # The ref's name (announce_key) and the branch it announces.
    key: str
    branch: str
    sha: str
    # The deploy's start (the author date) and its last renewal (the
    # committer date), by the announcing machine's clock.
    since: int
    at: int
    # Its deploy is over, its push pending (Announce.settled_commit).
    settled: bool
    # The deploy records it carries (its parents past the announcer's
    # HEAD): a switch onto its increment was started.
    records: tuple[str, ...]

    @property
    def carried(self) -> bool:
        return bool(self.records)


SETTLED = " settled"


def announcements() -> tuple[list[Pending], dict[str, str]]:
    """Origin's announcements as fetched: the pending ones, and the cleared
    ones (key -> commit) a deploy's ending push sweeps away: main's whose
    announced HEAD origin/main holds (a main checkout's push, outside
    nixconvoy, is what ends its pending state; a work branch's is deleted
    by its own deploy's end), once settled or carrying no record - a main
    checkout's deploy with nothing pending announces a HEAD main holds
    already, for its records alone, and is under way until settled; one
    that died carrying records keeps them on origin until the next deploy
    from its checkout renews the key, pushing them to their history
    branches."""
    depth = PENDING_REFS.count("/") + 1
    fmt = (f"%(refname:strip={depth})%09%(objectname)%09%(authordate:unix)"
           "%09%(committerdate:unix)%09%(parent)%09%(subject)")
    listing = out(["git", "for-each-ref", f"--format={fmt}", PENDING_REFS])
    found = []
    cleared = {}
    for ln in listing.splitlines():
        key, sha, since, at, parents, subject = ln.split("\t", 5)
        branch = key.partition("@")[0]
        settled = subject.endswith(SETTLED)
        if branch == cfg().main_branch and (settled or " " not in parents) \
                and is_ancestor(f"{sha}^", main_ref()):
            cleared[key] = sha
            continue
        found.append(Pending(key, branch, sha, int(since), int(at), settled, tuple(parents.split()[1:])))
    return found, cleared


def pending_announcements() -> list[Pending]:
    """Origin's announcements as fetched, the cleared ones left out."""
    return announcements()[0]


def announced(own: str | None) -> dict[str, str]:
    """The uncleared announcements, key -> commit, this run's own (by its
    key) left out: the drift core's in_flight.  A provenance naming an
    announcement whose key has none is abandoned (its deploy is over, the
    push never landed); one whose key is still announced is in flight,
    dead or not."""
    return {p.key: p.sha for p in pending_announcements() if p.key != own}


def delete_args(refs: dict[str, str]) -> list[str]:
    """`git push origin` arguments deleting `refs` there, each leased on
    the commit it held as fetched ("" for no lease)."""
    return [*(f"--force-with-lease={ref}:{sha}" for ref, sha in refs.items() if sha),
            *(f":{ref}" for ref in refs)]


def clear_refspec(wb: str) -> list[str]:
    """The push arguments deleting `wb`'s announcement, when one is here
    as fetched (a retry of a failed push clears it too)."""
    sha = rev(f"{PENDING_REFS}/{wb}")
    return delete_args({f"{PENDING_REFS}/{wb}": sha}) if sha else []


def record_commit(target: str, union: str, parents: list[str], subject: str, doc: str) -> str:
    """`target`'s deploy record: the union's tree under a commit whose message
    carries the provenance the target gets, on `parents` (first the target's
    previous record, for `git log --first-parent`), each one that exists
    here, once."""
    tree = out(["git", "rev-parse", f"{union}^{{tree}}"])
    ps: list[str] = []
    for p in parents:
        if p and p not in ps and rev(f"{p}^{{commit}}") is not None:
            ps.extend(["-p", p])
    return out(["git", "commit-tree", tree, *ps, "-m", strings.CHECKOUT_RECORD_MESSAGE(target=target, subject=subject,
                                                                                     doc=doc)],
               env=DATELESS_ENV)


def push_deployed(wb: str | None, ann: Announce | None, records: dict[str, str]) -> str | None:
    """The push that ends a deploy, in the background, atomic - all of it
    lands or none, so a deploy waiting on the announcement finds the
    branch moved when it clears, and a settled announcement never outlives
    records its history refs lack: the work branch, leased on origin's tip
    as fetched; each switched target's record onto
    refs/heads/DEPLOYED_PREFIX/<target>; the branch's announcement deleted
    (this run's, or an earlier run's whose push failed; unleased, a
    missing one being nothing) - or, under another key than the branch's
    (a main checkout's), settled.  Then,
    once that landed, the cleared announcements as fetched are deleted
    (not atomically), each leased on what it held, a refusal tolerated (a
    hand clear or another deploy got there first; what stays is swept by
    the next deploy ending well).  A background push outlives this
    process, its outcome collected by the next run
    (await_background_push).  Another writer of the branch since the
    check before the switches (a later fetch brought its tip) is the
    problem returned: the branch is not pushed and its announcement is
    left to the caller, as for a deploy that ended without its push; the
    records and the sweep go anyway."""
    refspecs: list[str] = []
    what: list[str] = []
    problem: str | None = None
    pushed_note: list[str] = []
    head = rev(f"refs/heads/{wb}") if wb else None
    if wb and head and current_branch() == wb and rev(remote_ref(wb)) != head:
        problem = sole_writer_problem(wb)
        if problem is None:
            refspecs += push_refspecs(wb, rev(remote_ref(wb)) or "")
            pushed_note = ["git", "update-ref", f"{PUSHED_REFS}/{wb}", head]
            what.append(strings.CHECKOUT_PUSHING_BRANCH(wb=wb, sha=head[:12]))
    refspecs += [f"+{sha}:refs/heads/{DEPLOYED_PREFIX}/{h}" for h, sha in records.items()]
    what += [f"{DEPLOYED_PREFIX}/{h}" for h in records]
    if ann is not None and ann.key == wb:
        if problem is None:
            ann.stop()
            refspecs.append(f":{PENDING_REFS}/{wb}")
    elif ann is not None:
        settled = ann.settled_commit()
        if settled:
            refspecs.append(f"+{settled}:{PENDING_REFS}/{ann.key}")
    elif wb and rev(f"{PENDING_REFS}/{wb}"):
        refspecs.append(f":{PENDING_REFS}/{wb}")
    sweep = {f"{PENDING_REFS}/{k}": sha for k, sha in announcements()[1].items()}
    if not refspecs and not sweep:
        return problem
    pidf, statusf, logf = (push_state(k) for k in ("pid", "status", "log"))
    hold = os.environ.get("NIXCONVOY_HOLD_PUSH", "")
    push = ["git", "-c", "core.hooksPath=/dev/null", *proc.PUSH_NET, "push", "--quiet", cfg().remote]
    script = "export GIT_TERMINAL_PROMPT=0; " + (shlex.join([*push, "--atomic", *refspecs]) if refspecs else "true") + "; rc=$?"
    if pushed_note:
        script += f'; [ "$rc" = 0 ] && {shlex.join(pushed_note)}'
    if sweep:
        script += f'; [ "$rc" = 0 ] && {shlex.join([*push, *delete_args(sweep)])}'
    script += '; echo "$rc" >"$0"'
    if hold:
        script = f"sleep {shlex.quote(hold)}; {script}"
    with logf.open("w") as log:
        child = subprocess.Popen(["sh", "-c", script, str(statusf)], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pidf.write_text(str(child.pid))
    print(strings.CHECKOUT_PUSHING_BG(what=", ".join(what) or strings.CHECKOUT_PUSHING_CLEARED))
    return problem


def origin_tips(cwd: str | None = None, names: RepoNames | None = None) -> dict[str, str]:
    """origin's branches as last fetched: name -> tip."""
    names = names or cfg().names
    listing = out(["git", "for-each-ref", "--format=%(refname:strip=3)%09%(objectname)",
                   f"refs/remotes/{names.remote}"], cwd=cwd)
    return dict(ln.split("\t", 1) for ln in listing.splitlines() if not ln.startswith("HEAD\t"))


def listed_tips(cwd: str | None = None, names: RepoNames | None = None) -> dict[str, str]:
    """origin_tips narrowed to main and the work branches."""
    names = names or cfg().names
    return {b: t for b, t in origin_tips(cwd, names).items() if b == names.main or listed(b)}


# The Deployed: trailer's value in a git log format; Applied: is the same
# trailer's key in increments from before the rename, so both are read (a
# commit carries one of them; a hand commit neither, its value the bare comma).
DEPLOYED_TRAILER = ("%(trailers:key=Deployed,valueonly,separator=%x2C)%x2C"
                    "%(trailers:key=Applied,valueonly,separator=%x2C)")


def trailer_targets(value: str) -> set[str]:
    """The targets a Reaches:/Deployed: trailer value names."""
    return {h.strip() for h in value.split(",") if h.strip() and h.strip() != "none"}


def held_targets(commits: str, cwd: str | None = None) -> set[str]:
    """The targets the increments in the range `commits` hold back: reached
    by one (its Reaches: trailer) that was deployed to other targets alone
    (its Deployed: one), and not running the branch's tip after any later
    increment's deploy (the Deployed: of each, oldest first)."""
    fmt = f"%(trailers:key=Reaches,valueonly,separator=%x2C)%x09{DEPLOYED_TRAILER}"
    held: set[str] = set()
    log = run(["git", "log", f"--format={fmt}", commits], cwd=cwd).stdout  # unstripped: a tab may lead
    for ln in reversed(log.splitlines()):
        reaches, _, deployed = ln.partition("\t")
        held = (held | trailer_targets(reaches)) - trailer_targets(deployed)
    return held


def summarize(ref: str) -> str:
    return out(["git", "log", "-1", "--format=%h %ad %s", "--date=format:%Y-%m-%d %H:%M", ref])


def landed_prefix(upstream: str, cwd: str | None = None) -> str | None:
    """The newest of HEAD's commits `upstream` lacks whose tree, less
    MSG_FILE, is the tree of a commit `upstream` has since the fork: a
    landing squashed it and all before it, so a rebase onto `upstream` has
    only what follows to replay (`--onto upstream <it>`, rebase_range);
    None when no landing matches.  The rebase core's tmp commits (the
    state set aside, "tmp: " subjects atop HEAD) never count: its pops
    after the rebase count on them being replayed."""
    own = []
    for ln in out(["git", "rev-list", "--format=%H %T %s", f"{upstream}..HEAD"], cwd=cwd).splitlines():
        if ln.startswith("commit "):
            continue
        fields = ln.split(" ", 2)
        if len(fields) < 3 or not fields[2].startswith("tmp: "):
            own.append((fields[0], fields[1]))
    if not own:
        return None
    landed = set(out(["git", "log", "--format=%T", f"HEAD..{upstream}"], cwd=cwd).split())
    for sha, tree in own:  # newest first
        if tree in landed:
            return sha
        if ok(["git", "cat-file", "-e", f"{tree}:{MSG_FILE}"], cwd=cwd) and tree_less_msg(sha, cwd) in landed:
            return sha
    return None


def rebase_range(upstream: str, cwd: str | None = None) -> list[str]:
    """`git rebase`'s arguments for HEAD onto `upstream`: past the landed
    prefix (landed_prefix) when there is one."""
    base = landed_prefix(upstream, cwd)
    return ["--onto", upstream, base] if base else [upstream]


def rebase_hint(upstream: str, cwd: str | None = None) -> str:
    """The rebase to run by hand, as rebase_range would: `rebase [--onto]
    origin/main [<base>]`."""
    return "rebase " + " ".join(a.removeprefix("refs/remotes/") for a in rebase_range(upstream, cwd))


def unmerged_gitlinks(where: str | None) -> list[str]:
    """The gitlinks among a stopped rebase's conflicts in `where`."""
    from . import submodule  # noqa: PLC0415 - submodule builds on this module

    lines = out(["git", "ls-files", "-u"], cwd=where).splitlines()
    return list(dict.fromkeys(path for meta, _, path in (ln.partition("\t") for ln in lines)
                              if meta.startswith(f"{submodule.GITLINK} ")))


def solve_stop(where: str | None, upstream: str) -> str | None:
    """A stopped rebase's conflicts solved where nixconvoy can: the text
    files both sides have (conflict markers) structurally (mergiraf), then
    the gitlinks and pin files by the submodule's own rebase
    (submodule.solve_rebase).  None once every conflict is solved; "" for
    a file beyond both (a side's deletion, a language mergiraf lacks), a
    hand's; else why a submodule could not follow."""
    from . import submodule  # noqa: PLC0415 - submodule builds on this module

    wt = Path(where or Path.cwd()).resolve()
    stages: dict[str, set[int]] = {}
    for ln in out(["git", "ls-files", "-u"], cwd=where).splitlines():
        meta, _, path = ln.partition("\t")
        stages.setdefault(path, set()).add(int(meta.split()[2]))
    if not stages:
        return ""
    for f, had in stages.items():
        if submodule.is_pin_path(wt, f):
            continue
        if not had >= {2, 3} or not solve_with_mergiraf(str(wt), f):
            return ""
        git("add", "--", f, cwd=where, check=False)
    if not out(["git", "diff", "--name-only", "--diff-filter=U"], cwd=where).strip():
        return None
    return submodule.solve_rebase(wt, Path.cwd().resolve(), upstream)


def rebase_solving(d: Path, upstream: str, base: str | None, rewritten: dict[str, str]) -> bool:
    """`git rebase upstream` in `d` (a submodule worktree), past the landed
    prefix (`base`, landed_prefix's) and through the conflicts solve_stop
    solves, a replayed commit solved to nothing skipped; whether it went
    through - else aborted, `d` as it was.  `rewritten` gets each replayed
    commit's rewrite, old -> new, as git's post-rewrite hook reports them:
    the one hook run, as no user hook runs under git()."""
    where = state_path("rewrite", d)
    shutil.rmtree(where, ignore_errors=True)
    where.mkdir(parents=True)
    try:
        log = where / "rewritten"
        hook = where / "post-rewrite"
        # Builtins alone: the rebase's PATH may lack coreutils.
        hook.write_text(f'#!/bin/sh\nwhile read -r old new _; do printf \'%s %s\\n\' "$old" "$new"; done >> "{log}"\n')
        hook.chmod(0o755)
        hooked = ["-c", f"core.hooksPath={where}"]
        onto = ["--onto", upstream, base] if base else [upstream]
        p = git(*hooked, "rebase", "--quiet", *onto, cwd=str(d), check=False)
        while p.returncode != 0 and solve_stop(str(d), upstream) is None:
            step = "--skip" if ok(["git", "diff", "--cached", "--quiet"], cwd=str(d)) else "--continue"
            p = git(*hooked, "rebase", step, cwd=str(d), env={"GIT_EDITOR": "true"}, check=False)
        if p.returncode != 0:
            git("rebase", "--abort", cwd=str(d), check=False)
            return False
        if log.exists():
            for ln in log.read_text().splitlines():
                old, new = ln.split()
                rewritten[old] = new
    finally:
        shutil.rmtree(where, ignore_errors=True)
    return True


def tree_less_msg(ref: str, cwd: str | None = None) -> str:
    """`ref`'s tree without MSG_FILE, through a throwaway index (write-tree
    on the real one would rewrite its cache-tree extension)."""
    fd, tmpidx = tempfile.mkstemp()
    os.close(fd)
    try:
        idx = {"GIT_INDEX_FILE": tmpidx}
        git("read-tree", ref, env=idx, cwd=cwd)
        git("update-index", "--force-remove", MSG_FILE, env=idx, cwd=cwd)
        return out(["git", "write-tree"], env=idx, cwd=cwd)
    finally:
        Path(tmpidx).unlink()


def on_origin(cwd: str | None = None, names: RepoNames | None = None) -> bool:
    """Whether HEAD holds nothing of its own beyond origin: it is an
    ancestor of origin/main or of origin's tip of a listed work branch, or
    its tree less MSG_FILE is that of an origin/main commit past their
    merge base (a review checkout of a tip since landed).  `names`: the
    repository's, a submodule's own where the core runs there."""
    names = names or cfg().names
    tips = set(listed_tips(cwd, names).values())
    # Excluded as ^sha lines: --not does not reach revisions read from stdin.
    if not out(["git", "rev-list", "-n", "1", "HEAD", "--stdin"], cwd=cwd,
               input="".join(f"^{c}\n" for c in sorted(tips))):
        return True
    main = names.main_ref
    base = run(["git", "merge-base", "HEAD", main], cwd=cwd, check=False).stdout.strip()
    if not base:
        return False
    landed = out(["git", "log", "--format=%T", f"{base}..{main}"], cwd=cwd).split()
    return tree_less_msg("HEAD", cwd) in landed


def increment_fate(branch: str, tip: str, union: str, checkout: str | None) -> str:
    """What became of `branch`'s increment at `tip`, a tip a target's provenance
    recorded that origin's tip of the branch no longer is, for the deploy
    of `union` (the drift core's Query::Fate; `checkout` the deploying one, when
    it is on this machine).  "kept" when the switch reverts none of
    it: origin's branch still carries it, or origin has no such branch yet
    while the deploying checkout has it checked out (its first push on
    its way - the commit reaches the branch only after every switch - or
    that first switch failed and the increment stays staged there; close
    removes a subagent's worktree, and an agent checkout's branch is reset
    on origin, not absent), or merging it into the union changes nothing but MSG_FILE
    (rebased, redeployed after a failed switch, landed intact).  A tip this
    repository never fetched, or one conflicting with the union (its lines
    changed since, by whatever), is "kept" too: nothing to announce.
    Otherwise the union lacks it: "abandoned" (reset, dropped with its
    subagent worktree, force-pushed over, or landed and since undone)."""
    if rev(f"{tip}^{{commit}}") is None:
        return "kept"
    up = remote_ref(branch)
    if rev(up) is not None:
        if is_ancestor(tip, up):
            return "kept"
    elif checkout and Path(checkout).is_dir() and branch_at(Path(checkout)) == branch:
        return "kept"
    p = run(["git", "merge-tree", "--write-tree", "--messages", union, tip], check=False)
    if p.returncode != 0:
        return "kept"
    merged = p.stdout.split("\n", 1)[0].strip()
    if not merged or tree_less_msg(merged) == rev(f"{union}^{{tree}}"):
        return "kept"
    return "abandoned"


def worktree_lock(d: Path) -> str | None:
    """The reason the worktree `d` is locked (Claude Code locks a subagent's
    for the agent's lifetime, naming its pid); None when it is not."""
    target, at = d.resolve(), None
    for ln in run(["git", "worktree", "list", "--porcelain"], cwd=str(d),
                  check=False).stdout.splitlines():
        if ln.startswith("worktree "):
            at = Path(ln[len("worktree "):]).resolve()
        elif at == target and (ln == "locked" or ln.startswith("locked ")):
            return ln[len("locked "):]
    return None


def proc_stat(pid: int) -> str | None:
    """/proc/<pid>/stat's fields after the parenthesised command name (state
    first, parent pid second, start time 20th); None without /proc (macOS)
    or for a pid that is gone."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2]
    except OSError:
        return None


def pid_started(pid: int) -> float | None:
    """When the process `pid` started, in epoch seconds to within a second
    (/proc/stat's btime is whole seconds); None without /proc (macOS) or
    for a pid that is gone."""
    stat = proc_stat(pid)
    if stat is None:
        return None
    try:
        btime = next(int(ln.split()[1]) for ln in Path("/proc/stat").read_text().splitlines()
                     if ln.startswith("btime "))
    except (OSError, StopIteration, ValueError):
        return None
    return btime + int(stat.split()[19]) / os.sysconf("SC_CLK_TCK")


def lock_pid_alive(reason: str) -> bool | None:
    """Whether the process a lock reason names (`pid N`, Claude Code's with
    `start S`, its /proc start time, so a reused pid does not pass) is
    alive; None when it names none."""
    m = re.search(r"\bpid (\d+)\b(?: start (\d+)\b)?", reason)
    if m is None:
        return None
    pid, start = int(m.group(1)), m.group(2)
    if Path("/proc/self").exists():
        stat = proc_stat(pid)
        if stat is None:
            return False
        if start is not None:
            return stat.split()[19] == start
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ancestors() -> Iterator[int]:
    """This process's ancestors, nearest first (via /proc, else ps)."""
    pid = os.getpid()
    while pid > 1:
        stat = proc_stat(pid)
        if stat is not None:
            pid = int(stat.split()[1])
        else:
            parent = run(["ps", "-o", "ppid=", "-p", str(pid)], check=False).stdout.strip()
            if not parent.isdigit():
                return
            pid = int(parent)
        if pid > 1:
            yield pid


def ancestor_pids() -> set[int]:
    return set(ancestors())


def lock_live(reason: str) -> bool:
    """Whether a worktree lock still protects an agent at work: the claude
    session its reason names runs, and is not the one this nixconvoy runs under
    (that session cleaning up after its own agents)."""
    if lock_pid_alive(reason) is False:
        return False
    m = re.search(r"\bpid (\d+)\b", reason)
    return m is None or int(m.group(1)) not in ancestor_pids()


# The create hook's lock when it found no claude session to name: no
# session's exit lifts it, so land and undeploy do.
SESSIONLESS = "(no claude session found)"


def require_unlocked(here: Path) -> None:
    """Die unless the subagent worktree `here` may be removed: Claude Code
    locks it for the lifetime of the session that spawned the agent."""
    reason = worktree_lock(here)
    if reason is not None and reason.endswith(SESSIONLESS):
        return
    if reason is not None and lock_pid_alive(reason) is None:
        die(strings.CHECKOUT_LOCKED_BY_HAND(reason=reason or strings.CHECKOUT_NO_LOCK_REASON))
    if reason is not None and lock_live(reason):
        die(strings.CHECKOUT_LOCKED_LIVE(reason=reason or strings.CHECKOUT_NO_LOCK_REASON))


def add_worktree(ref: str) -> str:
    wt = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}deploy-wt.", dir="/tmp")
    git("worktree", "add", "--detach", "--quiet", wt, ref)
    return wt


def drop_worktree(wt: str) -> None:
    with proc.finishing():
        git("worktree", "remove", "--force", wt, check=False)
    shutil.rmtree(wt, ignore_errors=True)
