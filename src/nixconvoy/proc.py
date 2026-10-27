"""Subprocesses, the exit path and the run's perf record: everything else
runs its commands through here, so die() can kill what is in flight."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import IO, NoReturn

TOOL = "nixconvoy"


def xdg_dir(var: str, default: str) -> Path:
    """The XDG base directory `var` names; `default` under HOME when it is
    unset or relative (the spec says to ignore a relative value)."""
    v = os.environ.get(var)
    return Path(v) if v and Path(v).is_absolute() else Path.home() / default


def cache_dir() -> Path:
    """Everything nixconvoy keeps on the machine between runs: the eval
    memo, the submodule mirrors and the perf log, all disposable."""
    return xdg_dir("XDG_CACHE_HOME", ".cache") / TOOL


class Perf:
    """Phase timings plus the slowest subprocess calls, appended as one JSON
    line per run (via atexit, so failed runs log too) to one log per machine
    under the XDG state dir, since timings compare across a host's checkouts."""

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
            path = cache_dir() / "perf.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(dump_json(rec) + "\n")
        except (OSError, RuntimeError):  # RuntimeError: no resolvable HOME
            return
        summary = " ".join(f"{k} {v}s" for k, v in phases.items()
                           if k not in ("startup", "end"))
        print(f"perf: total {rec['total']}s ({summary}) -> {path}")


PERF = Perf()


def load_json(text: str) -> object:
    return json.loads(text)


def dump_json(obj: object) -> str:
    return json.dumps(obj)


class Fail(Exception):
    """A failure die() has said: the run's end, unless a loop going on
    past one item's failure catches it."""


# The tail of every failure before any target is switched (the work
# branch is committed only after the switches).
NOTHING_DONE = "nothing was switched or committed"

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


def gate() -> None:
    if EXITING is not None and not getattr(_LOCAL, "finishing", False):
        raise Fail(EXITING)


def kill_children(why: str) -> None:
    with CHILDREN_LOCK:
        for c in CHILDREN:
            if c.poll() is not None:
                continue  # done already: its run() reports its own result
            with contextlib.suppress(OSError):
                c.kill()
            SWEPT[c] = why


def die(msg: str) -> NoReturn:
    global EXITING
    if threading.current_thread() is not threading.main_thread():
        EXITING = msg
    kill_children(msg)
    sys.stdout.flush()  # keep the order straight when both streams are piped
    print(f"{TOOL}: {msg}", file=sys.stderr)
    if ARMED:
        print(f"{TOOL}: {', '.join(sorted(ARMED))} still armed: each finishes verifying "
              f"and is then confirmed or rolled back", file=sys.stderr)
    raise Fail(msg)


def failure(e: Exception) -> str:
    """An exception for a verdict line: a Fail by its message, said by
    die() already; anything else with its type."""
    return str(e) if isinstance(e, Fail) else repr(e)


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
) -> subprocess.CompletedProcess[str]:
    """`merged` captures stderr into stdout, in order (p.stderr is None).
    `timeout` kills the command after that many seconds (returncode -SIGKILL).
    `input` is fed to the command's stdin; the command inherits `pass_fds`."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    t0 = time.monotonic()
    with CHILDREN_LOCK:
        gate()
        child = subprocess.Popen(
            cmd, cwd=cwd, env=full_env, text=True,
            stdin=subprocess.PIPE if input is not None else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=(subprocess.STDOUT if merged else subprocess.PIPE) if capture else None,
            pass_fds=pass_fds,
        )
        CHILDREN.add(child)
    try:
        try:
            stdout, stderr = child.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate()
    finally:
        with CHILDREN_LOCK:
            CHILDREN.discard(child)
            swept = SWEPT.pop(child, None)
    if swept is not None:
        raise Fail(swept)
    p = subprocess.CompletedProcess(cmd, child.returncode, stdout, stderr)
    PERF.call(cmd, time.monotonic() - t0)
    if check and p.returncode != 0:
        detail = (p.stderr or "").strip() if capture else ""
        die(f"`{' '.join(cmd)}` failed" + (f":\n{detail}" if detail else ""))
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
# a global post-commit auto-push or pre-push hook.  A network verb gets GIT_NET.
def git(*args: str, cwd: str | None = None, env: dict[str, str] | None = None,
        check: bool = True, capture: bool = True, timeout: float | None = None,
        pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    net = PUSH_NET if args and args[0] == "push" else GIT_NET if args and args[0] in NET_VERBS else ()
    return run(["git", "-c", "core.hooksPath=/dev/null", *net, *args], cwd=cwd, env={**GIT_ENV, **(env or {})},
               check=check, capture=capture, timeout=timeout, pass_fds=pass_fds)


def rev(ref: str) -> str | None:
    p = run(["git", "rev-parse", "-q", "--verify", ref], check=False)
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def is_ancestor(a: str, b: str, cwd: str | None = None) -> bool:
    return ok(["git", "merge-base", "--is-ancestor", a, b], cwd=cwd)


def this_machine() -> str:
    return socket.gethostname().split(".")[0]


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
            yield f"[agenix] decrypted {n} secrets (generation {m.group(1)})"


def stream(cmd: list[str], *, held_as: str | None = None,
           sink: list[str] | None = None) -> int:
    """Run `cmd` with stderr merged into stdout and its lines squashed
    (squash_agenix), printed as they arrive - or, with `held_as`, all at
    once at the end under that prefix - and appended to `sink`."""
    gate()
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    assert p.stdout is not None
    held: list[str] = []
    for line in squash_agenix(raw.rstrip("\n") for raw in p.stdout):
        if sink is not None:
            sink.append(line)
        if held_as is None:
            print(line, flush=True)
        else:
            held.append(held_as + line)
    rc = p.wait()
    PERF.call(cmd, time.monotonic() - t0)
    if held:
        print("\n".join(held), flush=True)
    return rc


def painter(stream: IO[str] | None = None) -> Callable[[str, str], str]:
    """paint(text, style) in SGR codes when `stream` (stdout) is a terminal
    (NO_COLOR unset), else the text as is: bold, dim, gray, green, yellow,
    red, cyan, magenta."""
    if not (stream or sys.stdout).isatty() or os.environ.get("NO_COLOR"):
        return lambda text, _style: text
    codes = {"bold": "1", "dim": "2", "gray": "90", "green": "32", "yellow": "33", "red": "31",
             "cyan": "36", "magenta": "35"}

    def paint(text: str, style: str) -> str:
        return "\033[" + ";".join(codes[c] for c in style.split()) + "m" + text + "\033[0m"
    return paint


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
