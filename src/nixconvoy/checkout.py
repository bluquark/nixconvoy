"""The checkouts of the repo on this machine and their work branches:
identity, locks, the staged snapshot, pushes, and the git queries
deploy-plan's rebase core asks."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import IO, NamedTuple

from . import proc
from .config import BRANCHES_FILE, MSG_FILE, cfg
from .guard import decide
from .proc import die, git, is_ancestor, ok, out, rev, run

# Identity for machinery commits (snapshots, union merges); the fixed date
# makes an unchanged union re-create the same commit hash.  Work-branch and
# squash commits use the user's identity.
MACHINE_ENV = {
    "GIT_AUTHOR_NAME": "nixconvoy",
    "GIT_AUTHOR_EMAIL": "deploy@nixconvoy",
    "GIT_COMMITTER_NAME": "nixconvoy",
    "GIT_COMMITTER_EMAIL": "deploy@nixconvoy",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}
# Claude Code's subagent worktrees live under a checkout here.
SUB_DIR = Path(".claude/worktrees")
# A throwaway worktree of a run (the union's, a rebase aside): never a
# checkout, whatever `git worktree list` says.
TMP_PREFIX = "nixconvoy-"


def allowed_branches() -> set[str]:
    """The patterns of the work branches nixconvoy may union or own, from this
    checkout's working-tree copy of deploy-branches: `*` spans anything, the
    one wildcard every matcher (here, the plan cores) agrees on."""
    try:
        lines = Path(BRANCHES_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        die(f"{BRANCHES_FILE} is missing from the repo root: list the work branches "
            f"(patterns, `*` the wildcard) one per line, e.g. `{this_prefix()}*`")
    pats = {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}
    bad = [p for p in pats if not re.fullmatch(r"[\w.*-]+", p)]
    if bad:
        die(f"{BRANCHES_FILE}: a pattern may hold letters, digits, '.', '-', '_' and '*' "
            f"only: {', '.join(bad)}")
    return pats


def listed(name: str, pats: set[str] | None = None) -> bool:
    return any(fnmatchcase(name, p) for p in (allowed_branches() if pats is None else pats))


def this_prefix() -> str:
    return cfg().branch_prefix.format(host=proc.this_host())


class Checkout(NamedTuple):
    # "main" (no work branch of its own), "agent" (a work branch) or "sub"
    # (a subagent worktree under another checkout's SUB_DIR).
    kind: str
    wb: str | None


def sub_parent(d: Path) -> Path | None:
    """The checkout a subagent worktree at `d` (<checkout>/.claude/worktrees/<name>)
    belongs to; None when `d` is not one."""
    top = d.parent.parent.parent
    if (d.parent.name == SUB_DIR.name and d.parent.parent.name == SUB_DIR.parent.name
            and (top / ".git").exists() and re.fullmatch(r"[\w.-]+", d.name)):
        return top
    return None


def is_checkout(d: Path) -> bool:
    """Whether `d` is a checkout rather than a run's throwaway worktree."""
    return not d.name.startswith(TMP_PREFIX)


def branch_at(d: Path) -> str | None:
    p = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=str(d), check=False)
    return p.stdout.strip() or None


def note_path(d: Path) -> Path:
    """The note in a checkout's git dir naming the work branch it owns:
    the listed branch it last deployed or committed from, so switching to
    main to look around does not orphan the branch (pull still rebases it
    aside, status still shows it as this checkout's)."""
    return d / out(["git", "rev-parse", "--git-path", "nixconvoy-work-branch"], cwd=str(d))


def noted_branch(d: Path) -> str | None:
    try:
        return note_path(d).read_text().strip() or None
    except OSError:
        return None


def note_branch(d: Path, wb: str) -> None:
    if noted_branch(d) != wb:
        with contextlib.suppress(OSError):
            note_path(d).write_text(wb + "\n")


def identity_of(here: Path, cur: str | None, pats: set[str] | None = None) -> Checkout:
    """What the checkout at `here` with `cur` checked out is: on a listed
    work branch it owns that branch (a subagent's when it lives under
    another checkout's SUB_DIR); a subagent worktree on any other branch
    owns <parent's work branch>-<name> (<branch_prefix>-<name> under the
    main checkout); a checkout that owned a listed branch before (its note)
    still does; anything else is the main checkout, with no work branch of
    its own."""
    parent = sub_parent(here)
    if cur and listed(cur, pats):
        return Checkout("sub" if parent is not None else "agent", cur)
    if parent is not None:
        pb = branch_at(parent) or noted_branch(parent)
        base = pb if pb and listed(pb, pats) else this_prefix()
        return Checkout("sub", f"{base}-{here.name}")
    noted = noted_branch(here)
    if noted and listed(noted, pats):
        return Checkout("agent", noted)
    return Checkout("main", None)


def checkout_identity() -> Checkout:
    here = Path.cwd().resolve()
    ident = identity_of(here, current_branch())
    if ident.wb is not None and ident.kind == "agent":
        note_branch(here, ident.wb)
    return ident


def work_branch_name() -> str | None:
    """The work branch of the current checkout; None for the main checkout."""
    return checkout_identity().wb


def work_branch() -> str | None:
    wb = work_branch_name()
    if wb is not None and not listed(wb):
        die(f"this checkout's work branch {wb} matches no pattern in {BRANCHES_FILE}; "
            "add this machine's line there (and land it on main so other "
            "checkouts union its branches) before deploying")
    return wb


def require_work_branch(what: str) -> str:
    wb = work_branch()
    if wb is None:
        die(f"{what} needs a work branch, and this checkout has none: "
            f"`git switch -c {this_prefix()}N` (a branch {BRANCHES_FILE} lists) and re-run")
    return wb


def current_branch() -> str | None:
    p = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], check=False)
    return p.stdout.strip() or None


def staged_differs() -> bool:
    return not ok(["git", "diff", "--cached", "--quiet"])


def lock_checkout() -> IO[str] | None:
    """The current checkout's deploy lock: a flock on a file in its git dir,
    held by the returned open file, or None when another run holds it.  It
    serialises nixconvoy runs in one checkout: a deploy commits the snapshot tree
    onto HEAD at its end, so `pull` must not rebase HEAD under it."""
    f = Path(out(["git", "rev-parse", "--git-path", "nixconvoy-deploy.lock"])).open("w")  # noqa: SIM115
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def deploy_running(d: Path) -> bool:
    """Whether an nixconvoy holds the checkout's lock (lock_checkout): a shared
    lock, tried and dropped at once, conflicts only with its exclusive one.
    No lock file yet means no run ever took it."""
    lock = d / out(["git", "rev-parse", "--git-path", "nixconvoy-deploy.lock"], cwd=str(d))
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


def ran(p: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {"ran": p.returncode == 0, "detail": (p.stderr or "").strip()}


class GitQueries:
    """Performs deploy-plan's git queries in the current checkout, and in
    the throwaway worktree a rebase of a branch not checked out here
    (`aside`) runs in."""

    def __init__(self) -> None:
        self.aside = ""

    def where(self, ask: dict[str, object]) -> str | None:
        return self.aside if ask.get("aside") else None

    def __call__(self, ask: dict[str, object]) -> dict[str, object]:
        def arg(k: str) -> str:
            return str(ask.get(k))

        kind = ask.get("ask")
        if kind == "contains":
            return {"flag": is_ancestor(arg("a"), arg("b"))}
        if kind == "op_in_progress":
            return {"text": git_op_in_progress()}
        if kind == "current_branch":
            return {"text": current_branch()}
        if kind == "staged_differs":
            return {"flag": staged_differs()}
        if kind == "commit":
            return ran(git("commit", "--quiet", "-m", arg("msg"), check=False))
        if kind == "add_all":
            return ran(git("add", "-A", check=False))
        if kind == "pop":
            mode = "--mixed" if ask.get("mixed") else "--soft"
            return ran(git("reset", "--quiet", mode, "HEAD~1", check=False))
        if kind == "head_subject":
            return {"text": git("log", "-1", "--format=%s", check=False).stdout.strip() or None}
        if kind == "rebase":
            empty = "--empty=keep" if ask.get("keep_empty") else "--empty=drop"
            return ran(git("rebase", "--quiet", empty, "origin/main", cwd=self.where(ask),
                           check=False))
        if kind == "rebase_abort":
            return ran(git("rebase", "--abort", cwd=self.where(ask), check=False))
        if kind == "rev":
            return {"text": rev(arg("name"))}
        if kind == "fetch":
            return ran(git("fetch", "--quiet", "--no-auto-gc", "origin", check=False))
        if kind == "push":
            return ran(git(*push_cmd(arg("branch")), check=False))
        if kind == "add_worktree":
            self.aside = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}rebase-wt.", dir="/tmp")
            return ran(git("worktree", "add", "--quiet", self.aside, arg("branch"), check=False))
        if kind == "drop_worktree":
            if self.aside:
                git("worktree", "remove", "--force", self.aside, check=False)
                shutil.rmtree(self.aside, ignore_errors=True)
                self.aside = ""
            return {"ran": True, "detail": ""}
        if kind == "detach":
            return ran(git("switch", "--quiet", "--detach", "origin/main", check=False))
        if kind == "pushed":
            return {"flag": any(is_ancestor("HEAD", t) for t in listed_tips(allowed_branches()).values())}
        if kind == "on_origin":
            return {"flag": on_origin()}
        if kind == "switch":
            start = ask.get("start")
            where = ["-c", arg("branch"), str(start)] if start else [arg("branch")]
            return ran(git("switch", "--quiet", *where, check=False))
        if kind == "upstream":
            merge = git("config", f"branch.{arg('branch')}.merge", check=False).stdout.strip()
            return {"text": merge or None}
        if kind == "set_upstream":
            return ran(git("branch", "--set-upstream-to=origin/main", arg("branch"), check=False))
        raise ValueError(f"deploy-plan asked {ask!r}")


def pop_tmp_commits() -> None:
    """Unwind the tmp commits a run that died mid-rebase left on the
    checked-out branch (never pushed)."""
    decide("rebase", mode="pop_tmp")


def ensure_rebased() -> None:
    """Make HEAD contain origin/main, its staged and unstaged changes intact,
    or die leaving the checkout as it was."""
    decide("rebase", mode="ensure_rebased")


def sibling_checkouts() -> list[Path]:
    """The other checkouts of this repo on this machine: its worktrees (not
    a run's throwaway ones) and the clones beside it with the same origin."""
    here = Path.cwd().resolve()
    origin = out(["git", "remote", "get-url", "origin"])
    found: set[Path] = set()

    def worktrees(of: Path) -> None:
        for ln in run(["git", "worktree", "list", "--porcelain"], cwd=str(of),
                      check=False).stdout.splitlines():
            if ln.startswith("worktree "):
                found.add(Path(ln[len("worktree "):]).resolve())

    worktrees(here)
    for d in (sub_parent(here) or here).parent.iterdir():
        if d != here and d.is_dir() and (d / ".git").exists():
            url = run(["git", "remote", "get-url", "origin"], cwd=str(d), check=False).stdout
            if url.strip() == origin:
                worktrees(d)
    return [d for d in sorted(found) if d != here and d.is_dir() and is_checkout(d)]


def update_checkout(path: Path) -> tuple[bool, str]:
    """Fetch and bring the checkout at `path` onto origin/main under its
    deploy lock, pushing its work branch (origin's copy is what every other
    deploy unions): (ok, what happened).  A checkout that cannot be is left
    exactly as it was."""
    prev = Path.cwd()
    os.chdir(path)
    try:
        lock = lock_checkout()
        if lock is None:
            return False, "skipped: an nixconvoy is running there"
        try:
            done = decide("rebase", mode="sync", wb=work_branch_name())
        finally:
            lock.close()
    finally:
        os.chdir(prev)
    assert isinstance(done, dict)
    return done.get("ok") is True, str(done.get("status"))


def catch_up(paths: list[Path]) -> bool:
    """update_checkout each of `paths`, printing one status line each;
    whether all succeeded."""
    good = True
    for d in paths:
        done, status = update_checkout(d)
        print(f"{d.name}: {status}", file=sys.stdout if done else sys.stderr,
              flush=True)
        good &= done
    return good


def ensure_on_work_branch(wb: str) -> None:
    prev = current_branch()
    decide("rebase", mode="on_work_branch", wb=wb)
    note_branch(Path.cwd().resolve(), wb)
    if (prev is not None and prev not in (wb, "main") and current_branch() == wb
            and checkout_identity().kind == "sub"):
        # The fresh branch a subagent's worktree was created on, replaced.
        git("branch", "--quiet", "-D", prev, check=False)


def warn_no_msgfile(wb: str) -> None:
    if rev(f"HEAD:{MSG_FILE}") is None:
        print(f"note: {wb} carries work but no {MSG_FILE} yet - it is required "
              "to squash", file=sys.stderr)


def staged_snapshot() -> str:
    """Ephemeral deterministic commit of the staged state on top of HEAD:
    what gets built and switched, never what lands on a branch.  Built from a
    copy of the index, since write-tree on the real one would rewrite its
    cache-tree extension."""
    fd, tmpidx = tempfile.mkstemp()
    os.close(fd)
    try:
        shutil.copy(out(["git", "rev-parse", "--git-path", "index"]), tmpidx)
        p = run(["git", "write-tree"], env={"GIT_INDEX_FILE": tmpidx}, check=False)
        if p.returncode != 0:
            die("cannot snapshot the index (unmerged paths?)")
        tree = p.stdout.strip()
    finally:
        Path(tmpidx).unlink()
    return out(["git", "commit-tree", tree, "-p", "HEAD",
                "-m", "staged snapshot (not yet committed)"], env=MACHINE_ENV)


def commit_deployed(wb: str, snap: str, msg: str) -> None:
    """The work-branch commit of an increment that just deployed: exactly the
    snapshot's tree, with the user's identity.  The soft reset leaves the
    index alone, so anything staged mid-deploy stays staged on top."""
    tree = out(["git", "rev-parse", f"{snap}^{{tree}}"])
    c = out(["git", "commit-tree", tree, "-p", "HEAD", "-m", msg])
    git("reset", "--quiet", "--soft", c)
    warn_no_msgfile(wb)


def push_state(kind: str) -> str:
    """The background push's pid/status/log file (in the git dir)."""
    return out(["git", "rev-parse", "--git-path", f"nixconvoy-push.{kind}"])


def await_background_push() -> None:
    """Collect the previous run's background push (push_work_branch) before
    anything fetches or pushes: wait for it, report a failure (this run's
    push retries it) and clear its files."""
    pidf, statusf, logf = (Path(push_state(k)) for k in ("pid", "status", "log"))
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
        status = "timed out" if pid > 0 else "no pid"
        if pid > 0:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGTERM)
    log = logf.read_text().strip() if logf.exists() else ""
    for f in (pidf, statusf, logf):
        with contextlib.suppress(OSError):
            f.unlink()
    if status != "0":
        print(f"note: the previous run's background push failed ({status}); this run's "
              f"push retries it" + (f":\n{log}" if log else ""), file=sys.stderr)


def push_cmd(wb: str) -> list[str]:
    """Push the work branch's local tip, CAS against the tip we last fetched."""
    old = rev(f"refs/remotes/origin/{wb}")
    return ["push", "--quiet", "origin", f"{wb}:refs/heads/{wb}",
            f"--force-with-lease=refs/heads/{wb}:{old or ''}"]


def push_work_branch(wb: str, background: bool = False) -> None:
    """Push the work branch.  A background push outlives this process, its
    outcome collected by the next run (await_background_push)."""
    head = rev(f"refs/heads/{wb}")
    if rev(f"refs/remotes/origin/{wb}") == head:
        return
    args = push_cmd(wb)
    if background:
        pidf, statusf, logf = (Path(push_state(k)) for k in ("pid", "status", "log"))
        hold = os.environ.get("NIXCONVOY_HOLD_PUSH", "")
        script = '"$@"; echo $? >"$0"'
        if hold:
            script = f"sleep {shlex.quote(hold)}; {script}"
        with logf.open("w") as log:
            child = subprocess.Popen(
                ["sh", "-c", script, str(statusf),
                 "git", "-c", "core.hooksPath=/dev/null", *args],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        pidf.write_text(str(child.pid))
        print(f"pushing {wb} ({head[:12] if head else '?'}) in the background")
        return
    p = git(*args, check=False)
    if p.returncode != 0:
        die(f"pushing {wb} failed - it moved on origin, but this checkout is its "
            f"only writer.  Investigate:\n{p.stderr.strip()}")
    print(f"pushed {wb} ({head[:12] if head else '?'})")


def origin_tips(cwd: str | None = None) -> dict[str, str]:
    """origin's branches as last fetched: name -> tip."""
    listing = out(["git", "for-each-ref", "--format=%(refname:strip=3)%09%(objectname)",
                   "refs/remotes/origin"], cwd=cwd)
    return dict(ln.split("\t", 1) for ln in listing.splitlines() if not ln.startswith("HEAD\t"))


def listed_tips(pats: set[str], cwd: str | None = None) -> dict[str, str]:
    """origin_tips narrowed to main and the work branches `pats` list."""
    return {b: t for b, t in origin_tips(cwd).items()
            if b == "main" or any(fnmatchcase(b, p) for p in pats)}


def summarize(ref: str) -> str:
    return out(["git", "log", "-1", "--format=%h %ad %s", "--date=format:%Y-%m-%d %H:%M", ref])


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


# The last pushed tip of each work branch remove_sub deleted on origin,
# <branch> -> tip, with a reflog: the remote-tracking ref's goes with the
# branch, and on_origin needs the memory.
PAST_REFS = "refs/nixconvoy/past"


def on_origin(cwd: str | None = None, pats: set[str] | None = None) -> bool:
    """Whether HEAD holds nothing of its own beyond origin: it is on
    origin/main or a pushed listed work branch - as fetched now, or at a
    tip fetched here before (the remote-tracking refs' reflogs, PAST_REFS
    for a branch since deleted: a review subagent's checkout of a work
    branch that was squashed or reset since)
    - or, that tip since rebased or squashed, its tree less MSG_FILE is
    that of an origin/main commit past their merge base, or each commit it
    has beyond a tip is patch-equivalent to one of the tip's (git cherry,
    which skips merges: a merge among them is its own)."""
    tips = listed_tips(allowed_branches() if pats is None else pats, cwd)
    past = set(tips.values())
    gone = out(["git", "for-each-ref", "--format=%(refname)", PAST_REFS], cwd=cwd).split()
    refs = [f"refs/remotes/origin/{b}" for b in tips] + gone
    past.update(run(["git", "log", "-g", "--format=%H", *refs], cwd=cwd, check=False).stdout.split())
    # Excluded as ^sha lines: --not does not reach revisions read from stdin.
    if not out(["git", "rev-list", "-n", "1", "HEAD", "--stdin"], cwd=cwd,
               input="".join(f"^{c}\n" for c in sorted(past))):
        return True
    base = run(["git", "merge-base", "HEAD", "refs/remotes/origin/main"], cwd=cwd,
               check=False).stdout.strip()
    if not base:
        return False  # no common history: nothing of origin's to be on
    landed = out(["git", "log", "--format=%T", f"{base}..refs/remotes/origin/main"], cwd=cwd)
    if tree_less_msg("HEAD", cwd) in landed.split():
        return True
    for t in tips.values():
        marks = out(["git", "cherry", t, "HEAD"], cwd=cwd).splitlines()
        n = int(out(["git", "rev-list", "--count", f"{t}..HEAD"], cwd=cwd) or 0)
        if marks and len(marks) == n and all(m.startswith("-") for m in marks):
            return True
    return False


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


def ancestor_pids() -> set[int]:
    """The pids of this process's ancestors (via /proc, else ps)."""
    found: set[int] = set()
    pid = os.getpid()
    while pid > 1:
        stat = proc_stat(pid)
        if stat is not None:
            pid = int(stat.split()[1])
        else:
            parent = run(["ps", "-o", "ppid=", "-p", str(pid)], check=False).stdout.strip()
            if not parent.isdigit():
                break
            pid = int(parent)
        if pid > 1:
            found.add(pid)
    return found


def lock_live(reason: str) -> bool:
    """Whether a worktree lock still protects an agent at work: the claude
    session its reason names runs, and is not the one this nixconvoy runs under
    (that session cleaning up after its own agents)."""
    if lock_pid_alive(reason) is False:
        return False
    m = re.search(r"\bpid (\d+)\b", reason)
    return m is None or int(m.group(1)) not in ancestor_pids()


def require_unlocked(here: Path) -> None:
    """Die unless the subagent worktree `here` may be removed: Claude Code
    locks it for the lifetime of the session that spawned the agent."""
    reason = worktree_lock(here)
    if reason is not None and lock_live(reason):
        die(f"this worktree is locked ({reason or 'no reason given'}): the claude session that "
            "spawned its agent still runs; wait for it to exit, or `git worktree unlock` it, "
            "then re-run")


def add_worktree(ref: str) -> str:
    wt = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}deploy-wt.", dir="/tmp")
    git("worktree", "add", "--detach", "--quiet", wt, ref)
    return wt


def drop_worktree(wt: str) -> None:
    git("worktree", "remove", "--force", wt, check=False, cleanup=True)
    shutil.rmtree(wt, ignore_errors=True)
