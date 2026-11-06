"""Subprocesses, the exit path and the run's perf record: everything else
runs its commands through here, so die() can kill what is in flight."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO, NoReturn

from . import strings, term
from .strings import TOOL


def xdg_dir(var: str, default: str) -> Path:
    """The XDG base directory `var` names; `default` under HOME when it is
    unset or relative (the spec says to ignore a relative value)."""
    v = os.environ.get(var)
    return Path(v) if v and Path(v).is_absolute() else Path.home() / default


def cache_dir() -> Path:
    """Everything nixconvoy keeps on the machine between runs: the eval
    memo, the submodule mirrors, the perf log and the prune logs, all
    disposable."""
    return xdg_dir("XDG_CACHE_HOME", ".cache") / TOOL


class Perf:
    """Phase timings plus the slowest subprocess calls, appended as one JSON
    line per run (via atexit, so failed runs log too) to one log per machine
    in the cache dir's logs/, since timings compare across a host's checkouts."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.phase = "startup"
        self._phase_t0 = self.t0
        self.phases: list[tuple[str, float]] = []
        self.calls: list[tuple[float, str, str]] = []

    def mark(self, phase: str) -> None:
        now = time.monotonic()
        self.phases.append((self.phase, now - self._phase_t0))
        self.phase, self._phase_t0 = phase, now
        term.phase(phase)

    @contextlib.contextmanager
    def excluded(self) -> Iterator[None]:
        """A wait on the user (an editor): its time kept out of the phase
        and the total, its subprocesses out of the slow calls."""
        t0, n = time.monotonic(), len(self.calls)
        try:
            yield
        finally:
            secs = time.monotonic() - t0
            self.t0 += secs
            self._phase_t0 += secs
            del self.calls[n:]

    def call(self, cmd: list[str], secs: float) -> None:
        if secs >= 0.2:
            # git's leading -c settings left out, so the verb shows.
            i = 1
            while cmd[0] == "git" and i + 1 < len(cmd) and cmd[i] == "-c":
                i += 2
            self.calls.append((secs, self.phase, " ".join([cmd[0], *cmd[i:]][:8])))

    def write(self, cwd: Path) -> None:
        self.mark("end")
        term.stop()  # the perf line goes where the last row was
        merged: dict[str, float] = {}
        for name, secs in self.phases:
            merged[name] = merged.get(name, 0.0) + secs
        phases = {k: round(v, 2) for k, v in merged.items() if v >= 0.05}
        rec = {
            "ts": self.ts,
            "cwd": str(cwd),
            "argv": sys.argv[1:],
            "total": round(time.monotonic() - self.t0, 2),
            "phases": phases,
            "slow_calls": [{"secs": round(s, 2), "phase": p, "cmd": c}
                           for s, p, c in sorted(self.calls, reverse=True)[:15]],
        }
        try:
            path = cache_dir() / "logs" / "perf.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(dump_json(rec) + "\n")
        except (OSError, RuntimeError):  # RuntimeError: no resolvable HOME
            return
        phases = {k: v for k, v in phases.items() if k not in ("startup", "end")}
        if term.LIVE:
            brief: dict[str, float] = {}
            for k, v in phases.items():
                brief[k.partition(":")[0]] = round(brief.get(k.partition(":")[0], 0.0) + v, 2)
            summary = " ".join(strings.PROC_PERF_PHASE(phase=k, secs=v) for k, v in brief.items() if v >= 1)
            print(term.paint(strings.PROC_PERF_BRIEF(total=rec["total"], summary=summary) if summary
                             else strings.PROC_PERF_TOTAL(total=rec["total"]), "dim"))
            return
        summary = " ".join(strings.PROC_PERF_PHASE(phase=k, secs=v) for k, v in phases.items())
        print(strings.PROC_PERF(total=rec["total"], summary=summary, path=path))


PERF = Perf()


def load_json(text: str) -> object:
    return json.loads(text)


def dump_json(obj: object) -> str:
    return json.dumps(obj)


class Fail(Exception):
    """A failure die() has said: the run's end, unless a loop going on
    past one item's failure catches it."""


# Every run() child in flight, killed by die(): an exit must not wait for a
# nix eval or precheck build another thread started (executors join their
# workers at interpreter shutdown) nor let their output trail the error.
# SWEPT holds the message each killed child's run() fails with instead of
# its own report of the kill.  Reentrant: a signal handler sweeps too.
CHILDREN: set[subprocess.Popen[str]] = set()
SWEPT: dict[subprocess.Popen[str], str] = {}
CHILDREN_LOCK = threading.RLock()
# A worker thread's die(): every later run() or stream() fails with it, but
# in a thread finishing what must still run (a deploy's cleanup, an armed
# switch).  A main-thread die() leaves it unset, so a loop that goes
# on past one item's failure is not stopped by it.
EXITING: str | None = None
_LOCAL = threading.local()
# Targets armed for a confirmed switch and neither confirmed nor rolled back
# yet: a die() for another target in their batch leaves them to their timers.
ARMED: set[str] = set()


def set_finishing(on: bool) -> bool:
    """Whether this thread's commands run past a worker's die() under way
    (EXITING); the previous setting."""
    was = getattr(_LOCAL, "finishing", False)
    _LOCAL.finishing = on
    return was


@contextlib.contextmanager
def finishing() -> Iterator[None]:
    was = set_finishing(True)
    try:
        yield
    finally:
        set_finishing(was)


def is_finishing() -> bool:
    """Whether this thread finishes (set_finishing)."""
    return bool(getattr(_LOCAL, "finishing", False))


def gate() -> None:
    if EXITING is not None and not getattr(_LOCAL, "finishing", False):
        raise Fail(EXITING)


# Seconds a sudo gets to relay a SIGTERM and see its command out before
# it is killed itself (nix answers one within a second or two).
SUDO_GRACE = 5


def stop(c: subprocess.Popen[str]) -> None:
    """End `c`: SIGKILL - its whole process group when it leads one (run's
    `session`), so no grandchild holds its pipes open past it - or SIGTERM
    for a sudo, which relays it to root's command (a grandchild no signal
    of our own reaches), killed itself after SUDO_GRACE should that not
    end it."""
    with contextlib.suppress(OSError):
        if isinstance(c.args, list) and c.args[:1] == ["sudo"]:
            c.terminate()
            threading.Timer(SUDO_GRACE, lambda: c.poll() is None and c.kill()).start()
        elif os.getpgid(c.pid) == c.pid:
            os.killpg(c.pid, signal.SIGKILL)
        else:
            c.kill()


def kill_children(why: str) -> None:
    with CHILDREN_LOCK:
        for c in CHILDREN:
            if c.poll() is not None:
                continue  # done already: its run() reports its own result
            stop(c)
            SWEPT[c] = why


def die(msg: str) -> NoReturn:
    global EXITING
    if threading.current_thread() is not threading.main_thread():
        EXITING = msg
    kill_children(msg)
    term.fail()
    sys.stdout.flush()  # keep the order straight when both streams are piped
    print(strings.PROC_DIE(msg=msg), file=sys.stderr)
    if ARMED:
        print(strings.PROC_STILL_ARMED(targets=", ".join(sorted(ARMED))), file=sys.stderr)
    raise Fail(msg)


def failure(e: Exception) -> str:
    """An exception for a verdict line: a Fail by its message, said by
    die() already; anything else with its type."""
    return str(e) if isinstance(e, Fail) else repr(e)


class Completed(subprocess.CompletedProcess[str]):
    """run's result: a CompletedProcess that also says whether `timeout`
    ended the command (its returncode then depends on what caught the
    signal: nix exits 1 on SIGTERM), and whether it is git_remote's
    retry of a command that failed."""
    timed_out: bool = False
    retried: bool = False


def run(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
    merged: bool = False,
    timeout: float | None = None,
    input: str | None = None,
    pass_fds: tuple[int, ...] = (),
    session: bool = False,
) -> Completed:
    """`merged` captures stderr into stdout, in order (p.stderr is None):
    on a terminal each line shown as it comes in the calling thread's step
    (term.Step.show), when it has one and the command neither `timeout`
    nor `input`.
    `timeout` ends the command after that many seconds (stop; p.timed_out).
    `input` is fed to the command's stdin; the command inherits `pass_fds`.
    `session` starts it in a session of its own, ended as a whole."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    t0 = time.monotonic()
    timed_out = False
    step = term.current() if capture and merged and timeout is None and input is None else None
    with contextlib.ExitStack() as shown:
        if not capture:
            shown.enter_context(term.paused())  # the command has the terminal
        with CHILDREN_LOCK:
            gate()
            child = subprocess.Popen(
                cmd, cwd=cwd, env=full_env, text=True,
                stdin=subprocess.PIPE if input is not None else None,
                stdout=subprocess.PIPE if capture else None,
                stderr=(subprocess.STDOUT if merged else subprocess.PIPE) if capture else None,
                pass_fds=pass_fds, start_new_session=session,
            )
            CHILDREN.add(child)
        try:
            try:
                if step is not None and child.stdout is not None:
                    lines: list[str] = []
                    with child.stdout:
                        for raw in child.stdout:
                            lines.append(raw)
                            step.show(raw)
                    child.wait()
                    stdout, stderr = "".join(lines), None
                else:
                    stdout, stderr = child.communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop(child)
                try:
                    stdout, stderr = child.communicate(timeout=SUDO_GRACE + 1)
                except subprocess.TimeoutExpired:
                    # A sudo's command outlived it holding the pipes: not waited for.
                    child.wait()
                    stdout, stderr = "", ""
                    for f in (child.stdout, child.stderr):
                        if f:
                            f.close()
        finally:
            with CHILDREN_LOCK:
                CHILDREN.discard(child)
                swept = SWEPT.pop(child, None)
    if swept is not None:
        raise Fail(swept)
    p = Completed(cmd, child.returncode, stdout, stderr)
    p.timed_out = timed_out
    PERF.call(cmd, time.monotonic() - t0)
    if check and p.returncode != 0:
        detail = (p.stderr or "").strip() if capture else ""
        die(strings.PROC_CMD_FAILED(cmd=" ".join(cmd), detail=strings.PROC_DETAIL(output=detail) if detail else ""))
    return p


def out(cmd: list[str], *, cwd: str | None = None,
        env: dict[str, str] | None = None, input: str | None = None) -> str:
    return run(cmd, cwd=cwd, env=env, input=input).stdout.strip()


def ok(cmd: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
       timeout: float | None = None) -> bool:
    return run(cmd, cwd=cwd, env=env, check=False, timeout=timeout).returncode == 0


# A git transfer under 1 kB/s for 10 s aborts (HTTPS remotes; ssh ones have
# no such limit): a stall ends quickly whatever the repository's size, which
# no total limit could promise without cutting a big clone short.
GIT_NET = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=10")
# A push's server goes quiet while it takes the pack in; cut off then, the
# push has landed but is reported failed.
PUSH_NET = (*GIT_NET[:-1], "http.lowSpeedTime=30")
NET_VERBS = frozenset({"fetch", "push", "clone", "ls-remote"})
# No credential prompt: a helper that has none to give fails at once.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


# git with user hooks disabled: machinery pushes/commits must never trigger
# a global post-commit auto-push or pre-push hook.
GIT = ("git", "-c", "core.hooksPath=/dev/null")


def git(*args: str, cwd: str | None = None, env: dict[str, str] | None = None,
        check: bool = True, capture: bool = True, timeout: float | None = None,
        pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    """A git command that stays on this machine (git_remote runs the rest)."""
    assert not args or (args[0] not in NET_VERBS and args[:2] != ("submodule", "update")), \
        f"git {args[0]} talks to a remote: git_remote"
    return run([*GIT, *args], cwd=cwd, env={**GIT_ENV, **(env or {})},
               check=check, capture=capture, timeout=timeout, pass_fds=pass_fds)


# The remote failing on its own side: an HTTP 5xx, or what GitHub says
# in-band under a 200 when its own machinery fails (commit_refs is its
# ref update's last-ditch error).  Nothing a client, a network or another
# writer of the ref can cause is among them.
SERVER_ERROR = re.compile(
    r"^.*(?:returned error: 5\d\d\b|HTTP 5\d\d\b|Internal Server Error|fatal error in commit_refs"
    r"|Unexpected system error|Storage server temporarily offline).*$", re.MULTILINE | re.IGNORECASE)
# The remote answering a push with a refusal: ref by ref, or of the whole
# request (an HTTP 4xx, but the two that ask for another try).
REFUSED = re.compile(r"^\s*! \[(?:remote )?rejected\]|returned error: 4(?!08|29)\d\d\b"
                     r"|^fatal: repository .* not found|^fatal: Authentication failed", re.MULTILINE)


def refused(p: Completed) -> bool:
    """Whether the remote refused the push `p` for good: it took nothing
    of it, and will not when asked again.  Not so where its own failure
    is the reason given (SERVER_ERROR), nor for a retry - the attempt
    before may be what stood in its way, and go through yet."""
    said = p.stderr or ""
    return not p.retried and bool(REFUSED.search(said)) and not SERVER_ERROR.search(said)


# Seconds waited before each retry of a command the remote failed so.
RETRY_WAITS = (1, 3)


def undone(args: list[str], cwd: str | None, env: dict[str, str],
           timeout: float | None) -> tuple[list[str], bool]:
    """The push `args` less its leased deletions of refs the remote no
    longer holds - nothing when that leaves no ref to push - and whether
    the remote said so: asked for those refs, it may fail again.  `args`
    itself for anything else.  A leased deletion is the one push made
    here that fails when repeated after it landed (the ref gone is not at
    its lease)."""
    if args[0] != "push":
        return args, True
    leases = {a.split("=", 1)[1].split(":", 1)[0]: a for a in args if a.startswith("--force-with-lease=")}
    deletes = [a[1:] for a in args if a.startswith(":") and a[1:] in leases]
    if not deletes:
        return args, True
    remote = next(a for a in args[1:] if not a.startswith("-"))
    p = run([*GIT, *GIT_NET, "ls-remote", remote, *deletes], cwd=cwd, env=env, check=False, timeout=timeout)
    if p.returncode != 0:
        return args, False
    held = {ln.split("\t", 1)[1] for ln in p.stdout.splitlines() if "\t" in ln}
    gone = {x for ref in deletes if ref not in held for x in (f":{ref}", leases[ref])}
    left = [a for a in args if a not in gone]
    return (left if any(not a.startswith("-") for a in left[left.index(remote) + 1:]) else []), True


def git_remote(*args: str, cwd: str | None = None, env: dict[str, str] | None = None,
               check: bool = True, timeout: float | None = None, streamed: bool = False) -> Completed:
    """A git command that talks to a remote (NET_VERBS, `submodule
    update`), under GIT_NET and retried (RETRY_WAITS) while the remote
    fails on its own side (SERVER_ERROR): whether such a command landed
    is unknown, so each must leave the remote the same when repeated after
    landing, which undone sees to for the one that does not.  `timeout`
    covers the retries too, each given at least the time the attempt
    before took.  `streamed` prints the command's output as it comes
    (stream; the environment and directory this process's own), all of it
    then the result's stderr."""
    assert not streamed or (cwd, env, timeout) == (None, None, None)
    net = PUSH_NET if args[0] == "push" else GIT_NET
    full_env = {**GIT_ENV, **(env or {})}
    deadline = None if timeout is None else time.monotonic() + timeout

    def left() -> float | None:
        return None if deadline is None else max(deadline - time.monotonic(), 0.0)

    def attempt(todo: list[str]) -> tuple[Completed, float, re.Match[str] | None]:
        """`todo` run: its result, the seconds it took, its server error."""
        cmd, t0 = [*GIT, *net, *todo], time.monotonic()
        if streamed:
            lines: list[str] = []
            p = Completed(cmd, stream(cmd, sink=lines), "", "\n".join(lines))
        else:
            p = run(cmd, cwd=cwd, env=full_env, check=False, timeout=left())
        failed = p.returncode != 0 and not p.timed_out
        return p, time.monotonic() - t0, SERVER_ERROR.search(p.stderr or "") if failed else None
    todo = list(args)
    p, took, error = attempt(todo)
    for wait in RETRY_WAITS:
        if error is None or (deadline is not None and time.monotonic() + wait + took > deadline):
            break
        print(strings.PROC_REMOTE_RETRY(verb=args[0], error=error.group().strip(), secs=wait), file=sys.stderr, flush=True)
        until = time.monotonic() + wait
        while time.monotonic() < until:
            gate()
            time.sleep(0.05)
        todo, asked = undone(todo, cwd, full_env, left())
        if not todo:
            p = Completed(p.args, 0, "", "")
            break
        if deadline is not None and time.monotonic() + took > deadline:
            break
        # The remote failing to say what is left of the push is this attempt failed.
        if asked:
            p, took, error = attempt(todo)
            p.retried = True
    if check and p.returncode != 0:
        detail = (p.stderr or "").strip()
        die(strings.PROC_CMD_FAILED(cmd=" ".join(p.args), detail=strings.PROC_DETAIL(output=detail) if detail else ""))
    return p


# git_remote as a command of its own (its arguments git's), for a script
# that outlives this process: this package's directory is its first argument.
GIT_REMOTE_CMD = (sys.executable, "-I", "-c",
                  "import sys; sys.path.insert(0, sys.argv.pop(1)); from nixconvoy import proc; proc.git_remote_main()",
                  str(Path(__file__).resolve().parents[1]))


def git_remote_main() -> None:
    os.environ.update(GIT_ENV)
    sys.exit(git_remote(*sys.argv[1:], check=False, streamed=True).returncode)


def rev(ref: str) -> str | None:
    p = run(["git", "rev-parse", "-q", "--verify", ref], check=False)
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def is_ancestor(a: str, b: str, cwd: str | None = None) -> bool:
    return ok(["git", "merge-base", "--is-ancestor", a, b], cwd=cwd)


def this_machine() -> str:
    return socket.gethostname().split(".")[0]


def cgroup_unit(cgroup: str) -> str | None:
    """The systemd service a /proc/<pid>/cgroup listing puts the process
    in: the innermost .service on its path, or None."""
    for ln in cgroup.splitlines():
        for seg in reversed(ln.split(":", 2)[-1].split("/")):
            if seg.endswith(".service"):
                return seg
    return None


def ancestors(pid: int) -> set[int]:
    """The pids of `pid`'s ancestors, up to init."""
    found: set[int] = set()
    while pid > 1:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            break
        found.add(pid)
    return found


def oneshot_run() -> bool:
    """Whether this process's exit ends the systemd service its cgroup
    names: this process is the unit's main process, or the unit is
    Type=oneshot with its main process among this one's ancestors.  The
    unit's end kills what its cgroup still holds, so nothing started here
    outlives the run (push_deployed waits for its push instead)."""
    try:
        unit = cgroup_unit(Path("/proc/self/cgroup").read_text())
        if unit is None:
            return False
        p = run(["systemctl", "show", "-p", "Type", "-p", "MainPID", unit], check=False)
    except OSError:
        return False
    props = dict(ln.split("=", 1) for ln in p.stdout.splitlines() if "=" in ln)
    main_pid = props.get("MainPID", "")
    if not main_pid.isdigit():
        return False
    me = os.getpid()
    return int(main_pid) == me or (props.get("Type") == "oneshot" and int(main_pid) in ancestors(me))


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def squash_agenix(lines: Iterable[str]) -> Iterator[str]:
    """Collapse agenix's activation chatter (a `decrypting ...` line per
    secret, `[agenix] ...` bookkeeping) into one line; WARNINGs pass."""
    n = 0
    for line in lines:
        if line.startswith("decrypting '") and line.endswith("'..."):
            n += 1
        elif not line.startswith("[agenix] ") or "WARNING" in line:
            yield line
        elif m := re.search(r"symlinking new secrets .*\(generation (\d+)\)", line):
            yield strings.PROC_AGENIX_DECRYPTED(n=n, generation=m.group(1))


def stream(cmd: list[str], *, held_as: str | None = None,
           sink: list[str] | None = None) -> int:
    """Run `cmd` with stderr merged into stdout and its lines squashed
    (squash_agenix), printed as they arrive - or, with `held_as`, all at
    once at the end under that prefix - and appended to `sink`.  On a
    terminal they are the calling thread's step's instead, when it has
    one (term.Step.log)."""
    gate()
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    assert p.stdout is not None
    held: list[str] = []
    step = term.current()
    for line in squash_agenix(raw.rstrip("\n") for raw in p.stdout):
        if sink is not None:
            sink.append(line)
        if step is not None:
            step.log(line)
        elif held_as is None:
            print(line, flush=True)
        else:
            held.append(held_as + line)
    rc = p.wait()
    PERF.call(cmd, time.monotonic() - t0)
    if held:
        print("\n".join(held), flush=True)
    return rc


HELD: list[IO[str]] = []  # locks held until exit


def solve_with_mergiraf(wt: str, path: str) -> bool:
    """Resolve the conflicted `path` in the worktree `wt` structurally.
    mergiraf first probes PATH for jj (Jujutsu), which it never needs here,
    and a shim directory that fetches a command on its first use (omnibin)
    answers that probe with a 20 s fetch: it runs on a PATH of its own and
    git's directories alone (git check-attr is what it calls)."""
    tool = shutil.which("mergiraf")
    if tool is None:
        return ok(["nix", "run", "nixpkgs#mergiraf", "--", "solve", "--keep-backup=false", path], cwd=wt)
    dirs = dict.fromkeys(str(Path(t).parent) for t in (tool, shutil.which("git")) if t)
    return ok([tool, "solve", "--keep-backup=false", path], cwd=wt, env={"PATH": os.pathsep.join(dirs)})
