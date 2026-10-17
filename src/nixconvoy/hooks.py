"""`nixconvoy hook <event>`: the Claude Code plugin's hooks (cli docstring:
Hook), each run by integrations/claude-code/hooks/<event>.sh with the
hook's JSON input on stdin, under the claude session it belongs to - the
nearest claude ancestor of this process.  worktree-create opens the
subagent worktree the session asks for, locked with the session's identity;
subagent-stop and session-start remove the worktrees the session's agents
leave with nothing of their own, detached, logging to nixconvoy-prune.log
in the common git dir."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from . import proc
from .checkout import SESSIONLESS, ancestors, lock_pid_alive, proc_stat, sub_parent
from .commands import (
    PruneQueries,
    Stop,
    close_worktrees,
    open_worktree,
    outermost,
    prune_one,
    sub_label,
    transcript_cwd,
    transcript_turns,
)
from .config import BRANCHES_FILE
from .proc import die, out, run

EVENTS = ("session-start", "subagent-stop", "worktree-create")


def run_hook(event: str) -> None:
    """The hook `event` on the input read from stdin, in the repository
    Claude Code runs it for (CLAUDE_PROJECT_DIR, else the input's cwd).
    The prunes do nothing outside a nixconvoy repository or a claude
    session; a create outside a repository fails."""
    try:
        given = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        given = {}
    if not isinstance(given, dict):
        given = {}
    where = os.environ.get("CLAUDE_PROJECT_DIR") or str(given.get("cwd") or "") or "."
    p = run(["git", "-C", where, "rev-parse", "--show-toplevel"], check=False)
    root = p.stdout.strip()
    if p.returncode != 0 or not root:
        if event == "worktree-create":
            die(f"hook worktree-create: {where} is not in a git repository")
        return
    os.chdir(root)
    session = claude_session()
    if event == "worktree-create":
        worktree_create(str(given.get("name") or ""), session)
        return
    if session is None or not Path(BRANCHES_FILE).is_file():
        return
    if event == "session-start":
        session_start(session)
    else:
        subagent_stop(session, given)


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
    """`open <name>` for the session's agent: the worktree locked for the
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


def subagent_stop(session: int, stop: dict[str, object]) -> None:
    """The claude session's subagent stopped, its SubagentStop hook input
    `stop`.  Detached, the agent's worktree (where its transcript starts)
    goes when it holds nothing of its own, the session counted as ended
    for it while the worktree's lock is still the one it had at the stop:
    a resume relocks or recreates it first (worktree-resume.sh).  Kept
    while a process, a background task the agent started (which wakes it,
    bypassing that hook), or another running agent of the session (a skill
    forked there) works in it, or once its transcript records a turn since
    the stop or a prompt awaiting one (it runs again); the agent stops
    again once that work has woken it, and is judged again."""

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
