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

PERF_FILE = "nixconvoy-perf.jsonl"
TOOL = "nixconvoy"


class Perf:
    """Phase timings plus the slowest subprocess calls, appended as one JSON
    line per run to PERF_FILE (via atexit, so failed runs log too)."""

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

    def call(self, cmd: list[str], secs: float) -> None:
        if secs >= 0.2:
            self.calls.append((secs, self.phase, " ".join(cmd[:8])))

    def write(self, path: Path) -> None:
        self.mark("end")
        merged: dict[str, float] = {}
        for name, secs in self.phases:
            merged[name] = merged.get(name, 0.0) + secs
        phases = {k: round(v, 2) for k, v in merged.items() if v >= 0.05}
        rec = {
            "ts": self.ts,
            "argv": sys.argv[1:],
            "total": round(time.monotonic() - self.t0, 2),
            "phases": phases,
            "slow_calls": [{"secs": round(s, 2), "phase": p, "cmd": c}
                           for s, p, c in sorted(self.calls, reverse=True)[:15]],
        }
        try:
            with path.open("a") as f:
                f.write(dump_json(rec) + "\n")
        except OSError:
            return
        summary = " ".join(f"{k} {v}s" for k, v in phases.items()
                           if k not in ("startup", "end"))
        print(f"perf: total {rec['total']}s ({summary}) -> {PERF_FILE}")


PERF = Perf()


def load_json(text: str) -> object:
    return json.loads(text)


def dump_json(obj: object) -> str:
    return json.dumps(obj)


# Every run() child in flight, killed by die(): an exit must not wait for a
# nix eval or precheck build another thread started (executors join their
# workers at interpreter shutdown) nor let their output trail the error.
# DYING, set under the lock run() spawns under, stops a thread between two
# commands from starting the next one after the sweep.
CHILDREN: set[subprocess.Popen[str]] = set()
CHILDREN_LOCK = threading.Lock()
DYING = False
# Targets armed for a confirmed switch and neither confirmed nor rolled back
# yet: a die() for another target in their batch leaves them to their timers.
ARMED: set[str] = set()


def abort_children() -> None:
    """die()'s kill of the children, for a signal handler: it takes no lock,
    since the frame it interrupted may hold CHILDREN_LOCK."""
    global DYING
    DYING = True
    try:
        for c in list(CHILDREN):
            with contextlib.suppress(OSError):
                c.kill()
    except RuntimeError:  # the set changed under the copy
        pass


def die(msg: str) -> NoReturn:
    global DYING
    if DYING and threading.current_thread() is not threading.main_thread():
        raise SystemExit(1)  # a worker's child killed by the die under way: its report is noise
    with CHILDREN_LOCK:
        DYING = True
        for c in list(CHILDREN):
            c.kill()
    sys.stdout.flush()  # keep the order straight when both streams are piped
    print(f"{TOOL}: {msg}", file=sys.stderr)
    if ARMED:
        print(f"{TOOL}: {', '.join(sorted(ARMED))} still armed: each finishes verifying "
              f"and is then confirmed or rolled back", file=sys.stderr)
    sys.exit(1)


def run(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
    merged: bool = False,
    cleanup: bool = False,
    timeout: float | None = None,
    input: str | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """`merged` captures stderr into stdout, in order (p.stderr is None).
    `cleanup` marks the commands that must still run after die().  `timeout`
    kills the command after that many seconds (returncode -SIGKILL).
    `input` is fed to the command's stdin; the command inherits `pass_fds`."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    t0 = time.monotonic()
    with CHILDREN_LOCK:
        if DYING and not cleanup:
            raise RuntimeError(f"{TOOL} is exiting")
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
    p = subprocess.CompletedProcess(cmd, child.returncode, stdout, stderr)
    PERF.call(cmd, time.monotonic() - t0)
    if check and p.returncode != 0:
        detail = (p.stderr or "").strip() if capture else ""
        die(f"`{' '.join(cmd)}` failed" + (f":\n{detail}" if detail else ""))
    return p


def out(cmd: list[str], *, cwd: str | None = None,
        env: dict[str, str] | None = None, input: str | None = None) -> str:
    return run(cmd, cwd=cwd, env=env, input=input).stdout.strip()


def ok(cmd: list[str], *, cwd: str | None = None, timeout: float | None = None,
       cleanup: bool = False) -> bool:
    return run(cmd, cwd=cwd, check=False, timeout=timeout, cleanup=cleanup).returncode == 0


# git with user hooks disabled: machinery pushes/commits must never trigger
# a global post-commit auto-push or pre-push hook.
def git(*args: str, cwd: str | None = None, env: dict[str, str] | None = None,
        check: bool = True, capture: bool = True,
        cleanup: bool = False, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    return run(["git", "-c", "core.hooksPath=/dev/null", *args],
               cwd=cwd, env=env, check=check, capture=capture, cleanup=cleanup, pass_fds=pass_fds)


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
    (NO_COLOR unset), else the text as is: bold, dim, green, yellow, red,
    cyan, magenta."""
    if not (stream or sys.stdout).isatty() or os.environ.get("NO_COLOR"):
        return lambda text, _style: text
    codes = {"bold": "1", "dim": "2", "green": "32", "yellow": "33", "red": "31",
             "cyan": "36", "magenta": "35"}

    def paint(text: str, style: str) -> str:
        return "\033[" + ";".join(codes[c] for c in style.split()) + "m" + text + "\033[0m"
    return paint


HELD: list[IO[str]] = []  # locks held until exit


def solve_with_mergiraf(wt: str, path: str) -> bool:
    """Resolve the conflicted `path` in the worktree `wt` structurally."""
    tool = ["mergiraf"] if shutil.which("mergiraf") else ["nix", "run", "nixpkgs#mergiraf", "--"]
    return ok([*tool, "solve", path], cwd=wt)
