"""The switch-guard binaries (the crate under switch-guard/): nixconvoy-plan
makes the git and drift decisions, nixconvoy-switch the switch's.  Both are
Sans-I/O cores speaking JSON lines of wire.py's messages (the crate's
wire.rs); this module runs one to its end, performing what it asks."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, overload

from . import proc, strings, term, wire
from .proc import die, dump_json, load_json
from .wire import (
    DriftOutcome,
    DriftParams,
    Effect,
    Mark,
    PlanQuery,
    PlanReply,
    Print,
    PruneOutcome,
    PruneParams,
    RebaseOutcome,
    RebaseParams,
    UnionOutcome,
    UnionParams,
    Variant,
    Warn,
)

Perform = Callable[[Any], Variant]
PlanPerform = Callable[[PlanQuery], PlanReply]


# The crate build this package carries (nix/package.nix links its binaries
# here), nowhere else; the test suite points a source-tree run at its own.
BIN = Path(__file__).resolve().parent / "bin"


def binary(name: str) -> str:
    """nixconvoy-plan or nixconvoy-switch, under BIN."""
    exe = BIN / name
    if not exe.is_file():
        die(strings.GUARD_MISSING(exe=exe))
    return str(exe)


def drive(exe: str, params: object, perform: Perform, query: object, outcome: object,
          name: str, during: str, effect: Callable[[Effect], None] | None = None,
          say: Callable[[str], None] | None = None) -> Any:
    """Run one of the cores (`exe`) to its end: send `params` (encoded),
    perform each query it asks (one of the union `query`, answered by what
    `perform` returns), print what it says to (stdout's lines given to
    `say` instead), and return its outcome (an `outcome`); its die message
    dies here."""
    try:
        core = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=term.child_stderr(),
                                text=True)
    except OSError as e:
        die(strings.GUARD_CANNOT_RUN(name=name, exe=exe, error=e))
    said = term.relay(core.stderr)
    assert core.stdin is not None and core.stdout is not None
    core_in, core_out = core.stdin, core.stdout

    def lost(what: str) -> NoReturn:
        said()
        die(strings.GUARD_LOST(name=name, what=what, during=during))

    def send(msg: object) -> None:
        try:
            core_in.write(dump_json(msg) + "\n")
            core_in.flush()
        except OSError:
            lost(strings.GUARD_STOPPED(code=core.poll()))

    try:
        send(params)
        while True:
            line = core_out.readline()
            if not line:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    core.wait(timeout=5)
                lost(strings.GUARD_STOPPED(code=core.poll()))
            try:
                effects, kind, payload = wire.step(load_json(line), query, outcome)
            except ValueError as e:  # not JSON, or not a step (WireError)
                lost(strings.GUARD_SENT(line=line.strip(), error=e))
            for e in effects:
                match e:
                    case Print(s) if say is not None:
                        say(s)
                    case Print(s):
                        print(s, flush=True)
                    case Warn(s):
                        print(s, file=sys.stderr, flush=True)
                    case Mark(s):
                        proc.PERF.mark(s)
                    case _:
                        if effect is not None:
                            effect(e)
            if kind == "done":
                return payload
            if kind == "die":
                die(str(payload))
            send(wire.encode(perform(payload)))
    finally:
        with contextlib.suppress(OSError):
            core_in.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            core.wait(timeout=5)
        if core.poll() is None:
            core.kill()


# Each core by its parameters: its name on the wire and its outcome's type.
CORES: dict[type, tuple[str, object]] = {
    DriftParams: ("drift", DriftOutcome),
    UnionParams: ("union", UnionOutcome),
    PruneParams: ("prune", PruneOutcome),
    RebaseParams: ("rebase", RebaseOutcome),
}


@overload
def decide(params: DriftParams, perform: PlanPerform | None = None,
           say: Callable[[str], None] | None = None) -> DriftOutcome: ...
@overload
def decide(params: RebaseParams, perform: PlanPerform | None = None) -> RebaseOutcome: ...
@overload
def decide(params: UnionParams, perform: PlanPerform | None = None,
           say: Callable[[str], None] | None = None) -> UnionOutcome: ...
@overload
def decide(params: PruneParams, perform: PlanPerform | None = None) -> PruneOutcome: ...
def decide(params: DriftParams | RebaseParams | UnionParams | PruneParams, perform: PlanPerform | None = None,
           say: Callable[[str], None] | None = None) -> object:
    """The outcome of nixconvoy-plan's core `params` are of, its git queries
    performed in the current checkout unless `perform` says otherwise, the
    lines it prints given to `say` when set."""
    from .checkout import GitQueries  # noqa: PLC0415 - the queries decide through here too

    core, outcome = CORES[type(params)]
    return drive(binary("nixconvoy-plan"), {core: wire.encode(params)}, perform or GitQueries(), PlanQuery, outcome,
                 "nixconvoy-plan", strings.GUARD_DECIDING(core=core), say=say)
