"""The switch-guard binaries (the crate under switch-guard/): nixconvoy-plan
makes the git and drift decisions, nixconvoy-switch the switch's.  Both are
Sans-I/O cores speaking JSON lines (src/plan/wire.rs, src/control/wire.rs);
this module runs one to its end, performing what it asks."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from . import proc
from .proc import die, dump_json, load_json

PLAN_PROTOCOL = 10
CONTROL_PROTOCOL = 1

Perform = Callable[[dict[str, object]], dict[str, object]]


def binary(name: str) -> str:
    """nixconvoy-plan or nixconvoy-switch: under NIXCONVOY_GUARD_DIR (the nix
    wrapper sets it to the crate build this package was made with), else on
    PATH."""
    d = os.environ.get("NIXCONVOY_GUARD_DIR")
    if d:
        return str(Path(d) / name)
    found = shutil.which(name)
    if found is None:
        die(f"{name} not found: run nixconvoy through its nix package (or set NIXCONVOY_GUARD_DIR)")
    return found


def drive(exe: str, params: dict[str, object], perform: Perform, name: str,
          during: str, effect: Callable[[dict[str, object]], None] | None = None) -> object:
    """Run one of the cores (`exe`) to its end: perform each query it asks,
    print what it says to, and return its outcome; its die message dies
    here."""
    try:
        core = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    except OSError as e:
        die(f"cannot run {name} ({exe}): {e}; nothing was committed")
    assert core.stdin is not None and core.stdout is not None
    core_in, core_out = core.stdin, core.stdout

    def lost(what: str) -> NoReturn:
        die(f"{name} {what}{during}; nothing was committed")

    def send(msg: dict[str, object]) -> None:
        try:
            core_in.write(dump_json(msg) + "\n")
            core_in.flush()
        except OSError:
            lost(f"stopped answering (exit {core.poll()})")

    try:
        send(params)
        while True:
            line = core_out.readline()
            try:
                step = load_json(line) if line else None
            except ValueError:
                step = None
            if not isinstance(step, dict):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    core.wait(timeout=5)
                lost(f"stopped answering (exit {core.poll()})")
            effects, ask = step.get("effects"), step.get("next")
            if not isinstance(effects, list) or not isinstance(ask, dict):
                lost(f"sent {line.strip()!r}")
            for e in effects:
                if not isinstance(e, dict):
                    continue
                if "print" in e:
                    print(e["print"], flush=True)
                elif "warn" in e:
                    print(e["warn"], file=sys.stderr, flush=True)
                elif "mark" in e:
                    proc.PERF.mark(str(e["mark"]))
                elif effect is not None:
                    effect(e)
            if "done" in ask:
                return ask["done"]
            if "die" in ask:
                die(str(ask["die"]))
            send(perform(ask))
    finally:
        with contextlib.suppress(OSError):
            core_in.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            core.wait(timeout=5)
        if core.poll() is None:
            core.kill()


def decide(core: str, perform: Perform | None = None, **params: object) -> object:
    """The outcome of nixconvoy-plan's `core` with `params`, its git queries
    performed in the current checkout unless `perform` says otherwise."""
    from .checkout import GitQueries  # noqa: PLC0415 - the queries decide through here too

    return drive(binary("nixconvoy-plan"), {"protocol": PLAN_PROTOCOL, "core": core, **params},
                 perform or GitQueries(), "nixconvoy-plan", f" deciding {core}")
