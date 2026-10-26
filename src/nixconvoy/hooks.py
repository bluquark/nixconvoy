"""`nixconvoy hook <event>`: the Claude Code plugin's hooks (cli docstring:
Hook), each run by integrations/claude-code/hooks/run.sh <event> with the
hook's JSON input on stdin, under the claude session it belongs to - the
nearest claude ancestor of this process.  worktree-create opens the
subagent worktree the session asks for, locked with the session's identity;
worktree-resume does so again for an agent a message resumes;
subagent-stop and session-start remove the worktrees the session's agents
leave with nothing of their own, detached, logging to nixconvoy-prune.log
in the common git dir; worktree-remove removes the one Claude Code itself
lets go of (a `claude --worktree` session's, at its exit)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

from . import config, names
from .checkout import (
    SESSIONLESS,
    ancestors,
    branch_at,
    lock_live,
    lock_pid_alive,
    proc_stat,
    sub_parent,
    worktree_lock,
)
from .commands import (
    PruneQueries,
    Stop,
    close_worktrees,
    hold_lock,
    name_lock_path,
    open_worktree,
    outermost,
    prune_one,
    sub_label,
    transcript_cwd,
)
from .config import CONFIG_FILE
from .proc import die, failure, git, ok, out, run

EVENTS = ("session-start", "subagent-stop", "worktree-create", "worktree-remove", "worktree-resume")


def run_hook(event: str) -> None:
    """The hook `event` on the input read from stdin, in the repository
    Claude Code runs it for (CLAUDE_PROJECT_DIR, else the input's cwd; the
    worktree's own for worktree-remove and worktree-resume).  The prunes
    do nothing outside a nixconvoy repository or a claude session; a
    create outside a repository fails."""
    try:
        given = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        given = {}
    if not isinstance(given, dict):
        given = {}
    if event == "worktree-remove":
        worktree_remove(str(given.get("worktree_path") or ""))
        return
    if event == "worktree-resume":
        worktree_resume(given)
        return
    where = os.environ.get("CLAUDE_PROJECT_DIR") or str(given.get("cwd") or "") or "."
    root = toplevel(where)
    if root is None:
        if event == "worktree-create":
            die(f"hook worktree-create: {where} is not in a git repository")
        return
    os.chdir(root)
    config.set_root(root)
    session = claude_session()
    if event == "worktree-create":
        worktree_create(str(given.get("name") or ""), session)
        return
    if session is None or not Path(CONFIG_FILE).is_file():
        return
    if event == "session-start":
        session_start(session)
    else:
        subagent_stop(session, given)


def toplevel(where: str | Path) -> Path | None:
    """names.toplevel of `where`, None outside a work tree."""
    if not ok(["git", "-C", str(where), "rev-parse", "--show-toplevel"]):
        return None
    return names.toplevel(Path(where))


def claude_session() -> int | None:
    """The claude session this process runs under: its nearest ancestor
    whose command name says claude; None outside one."""
    for pid in ancestors():
        if "claude" in command_name(pid):
            return pid
    return None


def command_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return run(["ps", "-o", "comm=", "-p", str(pid)], check=False).stdout.strip()


def worktree_create(name: str, session: int | None) -> None:
    """`open --child <name>` for the session's agent: the worktree locked for the
    session's lifetime, its reason naming the pid and start time close and
    status read, then a token of this create, so a lock taken again (a
    reuse, a resume) never reads as one a close judged before.  With no
    session found the lock names none, which the sweeps keep like a hand
    lock.  Prints the path, as the hook's output."""
    if not name:
        die("hook worktree-create: the input names no worktree")
    if session is None:
        reason = f"claude agent {name} {SESSIONLESS}"
    else:
        stat = proc_stat(session)
        start = f" start {stat.split()[19]}" if stat else ""
        reason = f"claude agent {name} (pid {session}{start}) {int(time.time())}.{os.getpid()}"
    open_worktree(name, reason)


def worktree_resume(given: dict[str, object]) -> None:
    """PreToolUse on SendMessage: a message to a stopped agent resumes it in
    the worktree its transcript started in, which its stop may have
    removed - recreated here before the resume, or relocked when it is
    still there, so the stop's prune sees a lock taken since and keeps it.
    Any failure refuses the resume (exit 2: a PreToolUse hook's exit 1
    would let the agent resume in a deleted directory)."""
    tool = given.get("tool_input")
    to = str(tool.get("to") or "") if isinstance(tool, dict) else ""
    transcript = str(given.get("transcript_path") or "")
    if not re.fullmatch(r"a[0-9a-f]+", to) or not transcript.endswith(".jsonl"):
        return
    try:
        d = transcript_cwd(Path(transcript.removesuffix(".jsonl")) / "subagents" / f"agent-{to}.jsonl")
        if d is None or d.name != f"agent-{to}":
            return
        # The checkout it is (or was) nested in, which its removal may have
        # taken with it.
        up = d
        while not up.is_dir() and up != up.parent:
            up = up.parent
        root = toplevel(up)
        if root is None or not (root / CONFIG_FILE).is_file():
            return
        os.chdir(root)
        with contextlib.redirect_stdout(sys.stderr):
            worktree_create(d.name, claude_session())
    except Exception as e:  # noqa: BLE001 - every failure blocks the resume
        print(f"worktree-resume: could not recreate or relock agent {to}'s worktree ({failure(e)}), "
              "so it is not resumed; retry", file=sys.stderr)
        sys.exit(2)


def worktree_remove(path: str) -> None:
    """WorktreeRemove: Claude Code lets go of a worktree the create hook
    made (a `claude --worktree` session's, at its exit; never a finished
    subagent's), which it leaves to this hook to remove.  Removed with its
    branch unless removal_keeps keeps it or, in a nixconvoy repository,
    the prune core does (named, the exiting session's lock ended for it,
    its branches dropped here and on origin): a failure, which leaves it
    on disk.  Its name lock is waited for while its holder runs, within
    the hook's timeout (hooks.json): a kill meanwhile leaves nothing
    taken."""
    if not path:
        return
    d = Path(path).resolve()
    top = toplevel(d.parent) if d.is_dir() else None
    if top is None:
        return
    os.chdir(top)
    nixconvoy = Path(CONFIG_FILE).is_file()
    held = hold_lock(name_lock_path(d), wait=True, what=str(d))
    assert held is not None
    try:
        if not d.is_dir():
            return
        why = removal_keeps(d)
        if why is None and not nixconvoy:
            branch = branch_at(d)
            git("worktree", "unlock", str(d), check=False)
            git("worktree", "remove", "--force", str(d), pass_fds=(held.fileno(),))
            if branch:
                git("branch", "-D", branch, check=False)
            return
    finally:
        held.close()
    if why is None:
        why = "it is no subagent worktree" if sub_parent(d) is None else prune_one(
            d, named=True, own_session_ends=True)
    if why is not None:
        die(f"hook worktree-remove: {d} kept: {why}")


def removal_keeps(d: Path) -> str | None:
    """Why the worktree-remove hook keeps `d` in any repository: another
    live claude session holds its lock (a `claude --worktree` reuse), it
    has changes (ignored files included: the session's builds and notes)
    or commits not on origin/main, or a submodule worktree is planted in it
    (`nixconvoy close` judges and removes both)."""
    reason = worktree_lock(d)
    if reason is not None and lock_pid_alive(reason) and lock_live(reason):
        return f"another claude session holds it ({reason})"
    if out(["git", "status", "--porcelain", "--ignored"], cwd=str(d)):
        return "it has uncommitted, untracked or ignored changes"
    main = run(["git", "rev-parse", "-q", "--verify", "refs/remotes/origin/main"], cwd=str(d), check=False)
    landed = "refs/remotes/origin/main" if main.returncode == 0 else "--remotes"
    if out(["git", "rev-list", "-n", "1", "HEAD", "--not", landed], cwd=str(d)):
        return "it carries commits not on origin/main"
    for ln in out(["git", "ls-files", "-s"], cwd=str(d)).splitlines():
        meta, _, sub = ln.partition("\t")
        if meta.startswith("160000") and (d / sub / ".git").exists():
            return f"it holds a submodule worktree ({sub}), left to nixconvoy close"
    return None


def session_start(pid: int) -> None:
    """Detach, prune what dead sessions left, wait for the claude session
    `pid` to exit, then prune again - its own agents' worktrees among them.
    It runs from the checkout the session's agents hang off (a `claude
    --worktree` session's own root goes at its exit).  One waiter per
    session (a lock file named after it in the common git dir)."""
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
        except Exception as e:  # noqa: BLE001 - the waiter outlives a failed sweep
            print(f"prune failed: {failure(e)}", flush=True)

    try:
        sweep("started: pruning dead sessions'")
        while lock_pid_alive(session) is not False:
            time.sleep(5)
        sweep("exited: pruning its own")
    finally:
        waiter.unlink(missing_ok=True)
        sys.stdout.flush()
    os._exit(0)


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


# How long after an agent's stop it is judged: a resume sent right after
# the stop relocks its worktree meanwhile, which keeps it.
STOP_GRACE = 10


def subagent_stop(session: int, stop: dict[str, object]) -> None:
    """The claude session's subagent stopped, its SubagentStop hook input
    `stop`.  Detached, the agent's worktree (where its transcript starts)
    goes when it holds nothing of its own, the session counted as ended
    for it while the worktree's lock is still the one it had at the stop:
    a resume relocks or recreates it first (worktree_resume).  Kept
    while a process, a background task the agent started (which wakes it,
    bypassing that hook), or another running agent of the session (a skill
    forked there) works in it; the agent stops again once that work has
    woken it, and is judged again."""

    agent = str(stop.get("agent_id") or "")
    transcript = Path(str(stop.get("agent_transcript_path") or ""))
    d = transcript_cwd(transcript)
    if not agent or d is None or d.name != f"agent-{agent}" or not d.is_dir() or sub_parent(d) is None:
        return

    tasks = stop.get("background_tasks")
    running = [t for t in tasks if isinstance(t, dict) and t.get("status") == "running"
               and t.get("id") != agent] if isinstance(tasks, list) else []
    top = outermost(d)
    os.chdir(top)
    common = Path(out(["git", "rev-parse", "--git-common-dir"])).resolve()
    locked = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "locked"], cwd=str(d)))
    stop_lock = PruneQueries.read(locked)
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
            time.sleep(STOP_GRACE)
            with (common / "nixconvoy-prune.lock").open("w") as serial:
                fcntl.flock(serial, fcntl.LOCK_EX)
                gone = not d.is_dir()
                why = None if gone else prune_one(d, named=True, stopped=Stop(session, stop_lock), wait_name=False)
            verdict = "removed meanwhile" if gone else "empty, removed" if why is None else f"kept, {why}"
    except Exception as e:  # noqa: BLE001 - reported in the log
        verdict = f"kept, its check failed: {failure(e)}"
    print(f"{time.strftime('%F %T')} agent {agent} stopped: {sub_label(d)}: {verdict}", flush=True)
    os._exit(0)
