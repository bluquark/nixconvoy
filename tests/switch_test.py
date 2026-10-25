"""nixconvoy's test suite, run by the flake's lint check (nix/lint.nix)
against $NIXCONVOY_GUARD_DIR's build of this tree's switch-guard:

- the switch step end to end against a fake target: the real
  nixconvoy-switch binary decides, the switch module's effects loop
  performs, and every ssh call lands in this file's --fake-target mode,
  which plays the confirmed-switch helper, systemd-run and nix-env on a JSON
  state file;
- the batch's die() gate, the targets and nixconvoy.toml, the protected-units
  gate, the provenance records and the drift classification other deploys read
  from them, and the real on-target switch guard;
- on scratch git repos: which targets a change reaches, nixconvoy-plan's
  rebase and union cores, pull, close, prune and undeploy, the deploy's
  announcements and records, submodules, open/review/land and the Claude
  Code hooks, and init.

The suite's own progress goes to stderr; what nixconvoy prints is
swallowed unless a case captures it."""

from __future__ import annotations

import atexit
import contextlib
import getpass
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest import mock

from nixconvoy import (
    checkout,
    cli,
    commands,
    config,
    deploy,
    guard,
    hooks,
    memo,
    names,
    proc,
    review,
    switch,
    targets,
)
from nixconvoy.union import UnionQueries

PROGRESS = sys.stderr

OLD, NEW = "/nix/store/old-system", "/nix/store/new-system"
# The helper's rollback exit when the confirm landed after all.
ALREADY_CONFIRMED = 3

BRANCH, BASE, LANDED = "work/haswell-wsl/2", "a" * 40, "b" * 40
ME = ("haswell-wsl", "/home/blu/projects/homelab3")
OTHER = ("haswell-wsl", "/home/blu/projects/homelab2")
LISTED = {"main", BRANCH, "work/haswell-wsl/3", "work/chikuwa/1"}


def provenance_line(top: str, *, by: tuple[str, str] = OTHER,
                 pending: tuple[str, str | None] | None = (BRANCH, BASE), announce: str = "",
                 others: tuple[str, ...] = ("work/chikuwa/1",), at: float = 1e6,
                 key: str = "") -> str:
    own = pending[0] if pending else BRANCH
    doc = switch.provenance_doc(
        union="u" * 40, main="m" * 40, branches={b: "c" * 40 for b in others},
        own=own, head="h" * 40, base=pending[1] if pending else "h" * 40,
        snap="s" * 40 if pending else "", announce=announce, at=at, machine=by[0], checkout=by[1],
        pid=4242, key=key)
    return proc.dump_json({**doc, "toplevel": top})


PROVENANCE = provenance_line(NEW)


def say(line: str) -> None:
    print(line, file=PROGRESS, flush=True)


def printed(f: Callable[[], object]) -> str:
    """What `f` prints to stdout."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        f()
    return out.getvalue()


def warned(f: Callable[[], object]) -> str:
    """What `f` prints to stderr."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        f()
    return err.getvalue()


def load(path: Path) -> dict[str, object]:
    state = proc.load_json(path.read_text())
    assert isinstance(state, dict)
    return {str(k): v for k, v in state.items()}


def fake_target(argv: list[str]) -> int:
    """One ssh call to the fake target."""
    path = Path(os.environ["FAKE_TARGET_STATE"])
    state = load(path)
    calls = state.setdefault("calls", [])
    assert isinstance(calls, list)
    calls.append(argv)
    if argv[0] == "systemd-run":
        argv = [a for a in argv[1:] if not a.startswith("--")]
    rc = 0
    helper = f"{NEW}/confirmed-switch"
    match argv:
        case ["test", "-e", p]:
            rc = 0 if state["helper"] and p == helper else 1
        case ["readlink", "/run/current-system"]:
            print(state["running"])
        case [h, "deadline"] if h == helper:
            print(20)
        case [h, "arm"] if h == helper:
            state["armed"] = True
            print(f"confirmed-switch: armed, rolling back to {state['rollback_to']} in 20s unless confirmed")
        case [h, "activate", top] if h == helper:
            rc = int(str(state["activate_rc"]))
            if rc != 100:
                state["running"] = top
        case [h, "check"] if h == helper:
            if state["check_killed"]:
                path.write_text(proc.dump_json(state))
                os.kill(os.getpid(), signal.SIGKILL)
            if not state["check_ok"]:
                print("dnsmasq is not active", file=sys.stderr)
                rc = 1
        case [h, "confirm", top, *rest] if h == helper:
            if state["confirm_rc"]:
                rc = int(str(state["confirm_rc"]))
            elif rest or state["running"] == top:
                state["profile"] = top
            else:
                rc = 1
        case [h, "rollback"] if h == helper:
            rc = int(str(state["rollback_rc"]))
            if rc == 0:
                state["running"] = state["rollback_to"]
                print(f"confirmed-switch: rolled back to {state['rollback_to']}")
        case ["nix-env", "-p", _, "--set", top]:
            state["profile"] = top
        case [p, "switch"] if p == f"{NEW}/bin/switch-to-configuration":
            state["running"] = NEW
        case _:
            print(f"fake target: unexpected {argv}", file=sys.stderr)
            rc = 99
    path.write_text(proc.dump_json(state))
    return rc


def fake_guard(expected: str, toplevel: str) -> int:
    """switch_guard_cmd's contract: claim the provenance once locked, put it
    back at the end unless the target runs or boots `toplevel`."""
    path = Path(os.environ["FAKE_TARGET_STATE"])
    running = load(path)["running"]
    if running != expected:
        print(f"MOVED {running}", flush=True)
        return 1
    print("LOCKED", flush=True)
    rec = sys.stdin.readline().strip()
    if rec:
        state = load(path)
        state["provenance"] = rec
        path.write_text(proc.dump_json(state))
    print("WROTE" if rec else "SKIPPED", flush=True)
    sys.stdin.read()
    state = load(path)
    if toplevel not in (state["running"], state["profile"]):
        state.pop("provenance", None)
        path.write_text(proc.dump_json(state))
    return 0


class Clock:
    """Each reading a second after the last: verification rounds advance
    without sleeping."""

    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> int:
        self.ms += 1000
        return self.ms


def scenario(name: str, *, helper: bool = True, check_ok: bool = True, activate_rc: int = 0,
             running: str = OLD, rollback_to: str = OLD, check_killed: bool = False,
             confirm_rc: int = 0, rollback_rc: int = 0,
             nixconvoy_switch: str = "") -> tuple[str | None, dict[str, object]]:
    """Run one switch; (the die message or None, the target's final state)."""
    state_file = Path(os.environ["FAKE_TARGET_STATE"])
    state_file.write_text(proc.dump_json({
        "helper": helper, "check_ok": check_ok, "activate_rc": activate_rc,
        "running": running, "profile": running, "armed": False, "rollback_to": rollback_to,
        "check_killed": check_killed, "confirm_rc": confirm_rc, "rollback_rc": rollback_rc,
    }))
    proc.ARMED.clear()
    died: str | None = None
    try:
        switch.switch("testhost", OLD, NEW, nixconvoy_switch or guard.binary("nixconvoy-switch"),
                      provenance=PROVENANCE)
    except proc.Fail as e:
        died = str(e).splitlines()[0]
    say(f"{name}: {died or 'switched'}")
    return died, load(state_file)


def switch_cases(d: Path) -> None:
    """The switch step against the fake target, on a clock that advances a
    second a reading and sleeps that return at once."""
    me = [sys.executable, str(Path(__file__).resolve())]

    def target_cmd(target: str, argv: list[str], sudo: bool = False,
                   fresh: bool = False) -> list[str]:
        del target, sudo, fresh
        return [*me, "--fake-target", *argv]

    def switch_guard_cmd(target: str, expected: str, toplevel: str, digest: str) -> list[str]:
        del target, digest
        return [*me, "--fake-guard", expected, toplevel]

    with contextlib.ExitStack() as patched:
        for obj, name, value in ((targets, "target_cmd", target_cmd), (switch, "switch_guard_cmd", switch_guard_cmd),
                                 (targets, "internet_reachable", lambda timeout=8.0: timeout > 0),
                                 (proc, "monotonic_ms", Clock()), (time, "sleep", lambda _secs: None)):
            patched.enter_context(mock.patch.object(obj, name, value))
        patched.enter_context(mock.patch.dict(os.environ, FAKE_TARGET_STATE=str(d / "target.json")))
        switch_scenarios(d)


def switch_scenarios(d: Path) -> None:
    died, s = scenario("confirmed")
    assert died is None and s["running"] == NEW and s["profile"] == NEW, (died, s)
    assert not proc.ARMED
    assert s.get("provenance") == PROVENANCE, s

    # A batch sibling's die() sweeps this switch's verification probes:
    # those rounds fail and are retried, the switch still confirmed.
    running_system, run = targets.running_system, switch.run
    swept: list[str] = []

    def swept_running(*args: Any, **kwargs: Any) -> str | None:
        if "running" not in swept:
            swept.append("running")
            raise proc.Fail("another target's switch failed")
        return running_system(*args, **kwargs)

    def swept_check(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[-1] == "check" and "check" not in swept:
            swept.append("check")
            raise proc.Fail("another target's switch failed")
        return run(cmd, **kwargs)
    with mock.patch.object(targets, "running_system", swept_running), mock.patch.object(switch, "run", swept_check):
        died, s = scenario("probes swept")
    assert died is None and s["running"] == NEW and swept == ["running", "check"], (died, s, swept)

    # A batch sibling's die() on its worker thread: this switch, armed,
    # still verifies and is confirmed; one not armed yet stops there.
    def sibling_died(*args: Any, **kwargs: Any) -> str | None:
        proc.EXITING = "a sibling's switch failed"
        return running_system(*args, **kwargs)
    with mock.patch.object(targets, "running_system", sibling_died), mock.patch.object(proc, "EXITING", None):
        died, s = scenario("sibling died while armed")
    assert died is None and s["running"] == NEW, (died, s)
    with mock.patch.object(proc, "EXITING", "a sibling's switch failed"):
        died, s = scenario("sibling died before arming")
    assert died == "a sibling's switch failed" and s["running"] == OLD and not s["armed"], (died, s)

    died, s = scenario("verification fails", check_ok=False)
    assert "provenance" not in s, s
    assert died is not None and "did not verify (testhost: dnsmasq is not active" in died, died
    assert f"rolled back to {OLD}" in died, died
    assert s["running"] == OLD and s["profile"] == OLD, s

    died, s = scenario("reboot needed", activate_rc=100)
    assert died is not None and "reboot testhost" in died, died
    assert s["running"] == OLD and s["profile"] == NEW, s
    assert s.get("provenance") == PROVENANCE, s  # the reboot takes the target to it

    died, s = scenario("plain", helper=False)
    assert died is None and s["running"] == NEW and s["profile"] == NEW, (died, s)
    assert s.get("provenance") == PROVENANCE, s

    died, s = scenario("moved", running="/nix/store/other")
    assert died is not None and "was switched by another deploy" in died, died
    assert s["running"] == "/nix/store/other" and "provenance" not in s, s

    # A dead deploy's pending rollback, taken over by this arm.
    died, s = scenario("inherited target", check_ok=False, rollback_to="/nix/store/older")
    assert died is not None and "rolled back to /nix/store/older" in died, died
    assert s["running"] == "/nix/store/older", s

    died, s = scenario("check killed", check_killed=True)
    assert died is not None and "check timed out" in died and f"rolled back to {OLD}" in died, died
    assert s["running"] == OLD and "provenance" not in s, s

    died, s = scenario("confirm lost, already confirmed", confirm_rc=1, rollback_rc=ALREADY_CONFIRMED)
    assert died is None and s.get("provenance") == PROVENANCE, (died, s)

    died, s = scenario("confirm fails", confirm_rc=1)
    assert died is not None and "verified but could not be confirmed" in died, died
    assert s["running"] == OLD, s

    crash = d / "crash"
    crash.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n")
    crash.chmod(0o755)
    died, s = scenario("nixconvoy-switch crashes", nixconvoy_switch=str(crash))
    assert died is not None and "nixconvoy-switch stopped answering" in died, died
    assert s["running"] == OLD and s["armed"] is False, s


def report(text: str, *, tips: dict[str, str] | None = None,
           evict: bool = False, named: bool = True, reached: bool = True, running: str = NEW,
           in_flight: dict[str, str] | None = None, dead: list[str] | None = None,
           held: dict[str, list[str]] | None = None) -> tuple[str, str]:
    """nixconvoy-plan's drift verdict on one drifting target carrying `text` as
    its provenance file: its report line, and the deploy's die message."""
    died = ""

    def drift() -> None:
        nonlocal died
        try:
            guard.decide(
                "drift", me=list(ME), union="u" * 40, listed=sorted(LISTED),
                tips={BRANCH: BASE} if tips is None else tips, in_flight=in_flight or {},
                dead=dead or [], tz_offset=0, router="choko", targets=["side1"], candidates=["side1"],
                evict=evict, named=named, work_branch=True, reached=["side1"] if reached else [],
                running={"side1": running},
                built={"side1": "/nix/store/union-system"}, held=held or {},
                provenances={"side1": text})
        except proc.Fail as e:
            died = str(e).strip()
    return printed(drift).strip(), died


def target_cases() -> None:
    """expand_targets: names and @tags from nixconvoy.toml, once each; the
    builder by the machine nixconvoy runs on."""
    config.set_cfg(config.parse(toml(
        '[targets.a]\ntags = ["vps"]\n[targets.b]\ntags = ["vps", "lab"]\n'
        '[targets.c]\ntags = ["lab"]\n')))
    inv = ["a", "b", "c"]
    assert targets.expand_targets([], inv) == []
    assert targets.expand_targets(["@vps"], inv) == ["a", "b"]
    assert targets.expand_targets(["b", "@vps"], inv) == ["b", "a"]
    assert targets.expand_targets(["@lab", "c"], inv) == ["b", "c"]
    env_builder = os.environ.pop("NIXCONVOY_BUILDER", None)
    try:
        for build, want in (('[build]\nbuilder = "big"\n', "big"),
                            ('[build]\nbuilder = "tester"\n', ""),
                            ('[build]\nbuilder = ""\n', ""),
                            ('[build]\nbuilder = { }\n', ""),
                            ("", ""),
                            ('[build]\nbuilder = { "*" = "big" }\n', "big"),
                            ('[build]\nbuilder = { "*" = "big", tester = "tester" }\n', ""),
                            ('[build]\nbuilder = { "*" = "big", tester = "other" }\n', "other"),
                            ('[build]\nbuilder = { other = "big" }\n', "")):
            config.set_cfg(config.parse(toml(build)))
            assert config.builder() == want, (build, config.builder())
        config.set_cfg(config.parse(toml('[build]\nbuilder = { "*" = "big" }\n')))
        os.environ["NIXCONVOY_BUILDER"] = ""
        assert config.builder() == ""
        os.environ["NIXCONVOY_BUILDER"] = "env"
        assert config.builder() == "env"
    finally:
        os.environ.pop("NIXCONVOY_BUILDER", None)
        if env_builder is not None:
            os.environ["NIXCONVOY_BUILDER"] = env_builder
    config.set_cfg(config.parse(toml('[targets.a]\ntags = ["vps"]\n[targets.zz]\ntags = ["vps"]\n')))
    for bad in (["@nope"], ["zz"], ["@vps"]):
        try:
            targets.expand_targets(bad, inv)
        except proc.Fail:
            pass
        else:
            raise AssertionError(bad)
    for bad_cfg, hint in ((toml('[targets.a]\ntag = ["vps"]\n'), "unknown key targets.a.tag"),
                          (toml('[build]\nbuilder = { big = 3 }\n'), "build.builder.big must be a string"),
                          (toml('[build]\nbuilder = 3\n'), "build.builder is a machine name"),
                          (toml().replace("config_version = 1", "config_version = true"), "not supported"),
                          (toml('[build]\nunreachable_hint = "zzz"\n'), "unknown key build.unreachable_hint"),
                          (toml('[targets.a]\nlocal_only = true\n'), "unknown key targets.a.local_only"),
                          (toml(ssh_address_template="x@{host}"), "over {target} alone"),
                          (toml(branch_prefix="{host}-d"), "over {machine} alone"),
                          # No defaults: every scalar key is required.
                          ("", "config_version is required"),
                          (toml().replace('flake_dir = "."\n', ""), "flake_dir is required"),
                          (toml().replace('targets_dir = "hosts"\n', ""), "targets_dir is required"),
                          (toml().replace('targets_arch = "x86_64-linux"\n', ""), "targets_arch is required"),
                          (toml().replace('ssh_address_template = "{target}"\n', ""), "ssh_address_template is required"),
                          (toml().replace('branch_prefix = "work/{machine}/"\n', ""), "branch_prefix is required"),
                          (toml().replace('work_branches = ["work/tester/*"]\n', ""), "work_branches is required"),
                          (toml(branches=("work/tester/*", "bad branch")), "work_branches: a pattern may hold"),
                          (toml().replace('child_worktrees = ".claude/worktrees"\n', ""), "child_worktrees is required"),
                          (toml().replace('[eval]\nparallel = "auto"\n', ""), "eval is required"),
                          (toml().replace('parallel = "auto"\n', ""), "eval.parallel is required"),
                          (toml().replace('parallel = "auto"\n', 'parallel = "auto"\ncores = 2\n'), "unknown key eval.cores")):
        try:
            config.parse(bad_cfg)
        except config.ConfigError as e:
            assert hint in str(e), (bad_cfg, e)
        else:
            raise AssertionError(f"accepted: {bad_cfg!r}")
    with tempfile.TemporaryDirectory() as empty:
        try:
            config.load(Path(empty))
        except config.ConfigError as e:
            assert "nixconvoy init" in str(e), e
        else:
            raise AssertionError("loaded without a file")
    # The builder's substituters less the skipped ones, each cache once.
    assert targets.kept_substituters(
        "https://a/ https://slow.cachix.org/?priority=40 https://a?priority=1 https://b\n",
        ("https://slow.cachix.org",)) == "https://a/ https://b"
    say("targets: ok")


def gate_cases() -> None:
    """A worker thread's die() stops every later command but a finishing
    thread's; a main-thread die() stops nothing."""
    def worker() -> None:
        with contextlib.suppress(proc.Fail):
            proc.die("worker failed")
    with contextlib.redirect_stderr(io.StringIO()) as err:
        with contextlib.suppress(proc.Fail):
            proc.die("main failed")
        assert proc.EXITING is None and proc.ok(["true"])
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert err.getvalue() == "nixconvoy: main failed\nnixconvoy: worker failed\n", err.getvalue()
    try:
        try:
            proc.run(["true"])
        except proc.Fail as e:
            assert str(e) == "worker failed", e
        else:
            raise AssertionError("ran past a worker's die()")
        with proc.finishing():
            assert proc.ok(["true"])
    finally:
        proc.EXITING = None
    say("gate: ok")


def protected_cases() -> None:
    """The protected-units gate: a group's units touched refuse the switch
    with its why and the exact --allow command (one for every refused target,
    a deploy for an undeploy, whose reset is over); --allow <name> admits them; the
    memo entry round-trips."""
    net = memo.Protection("risky-network-reload-or-restart",
                          ("systemd-networkd.service", "nftables.service"),
                          "risk of breaking essential LAN/WAN functionality")
    db = memo.Protection("database", ("postgresql.service",), "drops every connection")
    top = memo.Top("/nix/store/new", "/nix/store/new.drv", (db, net), True, False)
    ent = memo.memo_entry(json.loads(json.dumps({
        "out": top.out, "drv": top.drv, "last": True, "assessed": False,
        "protected": {p.name: {"units": list(p.units), "why": p.why} for p in top.protected}})))
    assert ent == top, ent
    assert memo.memo_entry({"out": "o", "drv": "d", "protected": ["nftables.service"]}) is None
    assert memo.memo_entry({"out": "o", "drv": "d"}) == memo.Top("o", "d", (), False, False)

    report = ("would restart the following units: nftables.service, nginx.service\n"
              "would reload the following units: postgresql.service\n")

    def dry_activate(target: str, toplevel: str) -> tuple[dict[str, list[str]], str | None]:
        del target, toplevel
        return targets.unit_changes(report), None

    with mock.patch.object(targets, "dry_activate", dry_activate), \
            mock.patch.object(sys, "argv", ["nixconvoy", "deploy", "-m", "a change", "router"]):
        gate = targets.protected_units_gate
        r = gate("router", top.out, top.protected, frozenset())
        assert isinstance(r, targets.Refusal), r
        assert r.target == "router" and r.names == ("database", net.name), r
        assert "reload postgresql.service (database: " in r.hits and "restart nftables.service (" in r.hits, r
        both = "nixconvoy deploy --allow database --allow risky-network-reload-or-restart -m 'a change' router"
        assert targets.refusals_message([r]).startswith(f"{r}\n") and \
            targets.refusals_message([r]).endswith(f"\n  {both}"), targets.refusals_message([r])
        r = gate("router", top.out, top.protected, frozenset({"database"}))
        assert isinstance(r, targets.Refusal) and targets.refusals_message([r]).endswith(
            "\n  nixconvoy deploy --allow risky-network-reload-or-restart -m 'a change' router"), r
        assert gate("router", top.out, top.protected, frozenset({"database", net.name})) is None
        assert gate("router", top.out, (), frozenset()) is None
        r2 = gate("db", top.out, (db,), frozenset())
        assert isinstance(r2, targets.Refusal) and r2.names == ("database",), r2
        m = targets.refusals_message([r, "dry-activating the new system on x failed", r2])
        assert m.startswith(f"{r}\ndry-activating") and f"on x failed\n{r2}\n" in m and m.endswith(f"\n  {both}"), m
        assert "re-run" not in targets.refusals_message(["dry-activating the new system on x failed"])
        sys.argv = ["nixconvoy", "undeploy", "task"]
        assert targets.refusals_message([r2]).endswith("\n  nixconvoy deploy --allow database")
    say("protected: ok")


def provenance_cases() -> None:
    """The drift core (switch-guard/src/plan/drift.rs, whose own tests hold
    the cases) on the provenances provenance_doc writes."""
    line, died = report(provenance_line(NEW))
    assert line.startswith(f"side1: refused: runs an increment of {BRANCH}") and \
        f"{OTHER[0]}:{OTHER[1]} (pid 4242) at 13:46" in line, line
    assert f"origin/{BRANCH} is still at {BASE[:12]}" in line, line
    assert "side1 runs another" in died and "--evict side1" in died, died
    line, died = report(provenance_line(NEW), reached=False)
    assert line.startswith(f"side1: left alone: runs an increment of {BRANCH}") and \
        "does not reach it" in line and not died, (line, died)
    line, died = report(provenance_line(NEW), tips={BRANCH: LANDED})
    assert line.startswith("side1: will switch (catch-up: ") and not died, (line, died)
    # An announced increment: refused while origin's pending ref of its
    # branch is uncleared - the refusal naming the deploy as dead when this
    # run found it so - and abandoned once it was cleared without the push.
    announced = provenance_line(NEW, announce="p" * 40)
    assert proc.load_json(announced)["pending"]["announce"] == "p" * 40  # type: ignore[index]
    line, died = report(announced, in_flight={BRANCH: "q" * 40})
    assert line.startswith("side1: refused: ") and "dead" not in line and \
        "push its branch from its checkout" in died and "--evict side1" in died, (line, died)
    line, died = report(announced, in_flight={BRANCH: "q" * 40}, dead=[BRANCH])
    assert "that deploy looks dead" in line and \
        f"The deploy of {BRANCH} looks dead" in died and \
        f"git push --force origin origin/main:{checkout.PENDING_REFS}/{BRANCH}" in died, (line, died)
    line, died = report(announced)
    assert line.startswith("side1: will switch, reverting it: ") and "its deploy is over" in line and \
        not died, (line, died)
    # A main checkout's, under its own ref.
    nightly = provenance_line(NEW, by=("chikuwa", "/var/lib/homelab"), pending=("main", BASE),
                           announce="p" * 40, key="main@chikuwa")
    assert proc.load_json(nightly)["pending"]["key"] == "main@chikuwa"  # type: ignore[index]
    line, died = report(nightly, tips={"main": BASE}, in_flight={"main@chikuwa": "q" * 40}, dead=["main@chikuwa"])
    assert line.startswith("side1: refused: ") and "The deploy of main looks dead" in died and \
        f"{checkout.PENDING_REFS}/main@chikuwa" in died, (line, died)
    line, died = report(nightly, tips={"main": BASE}, in_flight={"main@haswell-wsl": "q" * 40})
    assert line.startswith("side1: will switch, reverting it: ") and not died, (line, died)
    line, died = report(provenance_line(NEW), in_flight={BRANCH: "q" * 40}, dead=[BRANCH])
    assert "dead" not in line and "push its branch from its checkout" in died, (line, died)
    line, _ = report(provenance_line(NEW), evict=True)
    assert line.endswith("taken over by --evict"), line
    line, _ = report(provenance_line(NEW), evict=True, named=False, reached=False)
    assert line.startswith("side1: left alone: "), line
    line, _ = report(provenance_line(NEW), evict=True, named=False)
    assert line.endswith("taken over by --evict"), line
    line, _ = report(provenance_line(NEW, pending=None, others=("work/M3-Air/9",)))
    assert "side1: refused: runs work of work/M3-Air/9" in line, line
    assert report(provenance_line(NEW, by=ME))[0].startswith("side1: will switch (this-branch: ")
    assert report("") == ("side1: will switch", "")
    assert report("{not json") == ("side1: will switch", "")
    assert report(provenance_line(OLD)) == ("side1: will switch", "")
    # A provenance of another version is none.
    assert report(json.dumps({**json.loads(provenance_line(NEW)), "version": 1})) == ("side1: will switch", "")
    assert report(provenance_line(NEW), running=OLD)[0] == "side1: will switch"
    assert report(provenance_line(NEW), running="/nix/store/union-system")[0] == "side1: up to date"
    # Held back by another branch: refused when reached, else left alone.
    line, died = report("", held={"side1": ["work/chikuwa/1"]})
    assert line.startswith("side1: refused: held back by work/chikuwa/1"), line
    assert "side1 is held back" in died and "nixconvoy deploy side1" in died, died
    line, died = report("", held={"side1": ["work/chikuwa/1"]}, reached=False)
    assert line.startswith("side1: left alone: held back by") and not died, (line, died)

    text = provenance_line(NEW)
    p = targets.parse_probe(f"{NEW}\n{text}")
    assert p.running == NEW and p.provenance == text, p
    assert p.digest == hashlib.sha256(text.encode()).hexdigest(), p
    p = targets.parse_probe(f"{OLD}\n")
    assert p == targets.Probe(OLD, "", hashlib.sha256(b"").hexdigest()), p

    with mock.patch.object(switch, "rev"), mock.patch.object(switch, "current_branch"):
        def record(branch: str | None, head: str, origin: str | None, snap: str) -> object:
            switch.current_branch = lambda: branch
            r = {"HEAD": head, f"refs/remotes/origin/{branch}": origin, "refs/remotes/origin/main": "m"}
            switch.rev = lambda ref: r.get(ref)
            return switch.provenance_record("u", [("work/chikuwa/1", "c")], snap)["pending"]

        assert record("work/haswell-wsl/3", "h", "h", "") is None
        assert record("work/haswell-wsl/3", "h", "h", "s") == {
            "branch": "work/haswell-wsl/3", "base": "h", "head": "s"}
        switch.current_branch = lambda: "work/haswell-wsl/3"
        assert switch.provenance_record("u", [], "s", "p")["pending"] == {
            "branch": "work/haswell-wsl/3", "base": "h", "head": "s", "announce": "p",
            "key": "work/haswell-wsl/3"}
        assert switch.provenance_record("u", [], "s", "p", "k")["pending"] == {
            "branch": "work/haswell-wsl/3", "base": "h", "head": "s", "announce": "p", "key": "k"}
        assert record("work/haswell-wsl/3", "h2", "h", "") == {
            "branch": "work/haswell-wsl/3", "base": "h", "head": "h2"}
        assert record("main", "lock", "m", "") == {"branch": "main", "base": "m", "head": "lock"}
        # Detached at origin/main counts as main's; a hand commit there is pending.
        assert record(None, "m", None, "") is None
        assert record(None, "h", None, "") == {"branch": "main", "base": "m", "head": "h"}
    say("provenance classification: ok")


def guard_cases(d: Path) -> None:
    """The real switch guard (switch_guard_cmd's script, flock and all) in a
    scratch directory, through the window a provenance written only after the
    switch left open: B probes while A's switch is between activation and
    confirmation."""
    cur, profile, provenance = d / "current-system", d / "profile", d / "deploy" / "provenance.json"
    targets.CURRENT_SYSTEM, targets.SYSTEM_PROFILE = str(cur), str(profile)
    targets.SWITCH_LOCK, targets.PROVENANCE_FILE, targets.PROVENANCE_WRITER = str(d / "lock"), str(provenance), ""

    def point(link: Path, to: str) -> None:
        link.unlink(missing_ok=True)
        link.symlink_to(to)

    def guard(expected: str, top: str, digest: str) -> subprocess.Popen[str]:
        return subprocess.Popen(switch.switch_guard_cmd("tester", expected, top, digest),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def until_eof(g: subprocess.Popen[str]) -> str:
        assert g.stdin is not None and g.stdout is not None
        g.stdin.close()
        g.wait(10)
        return g.stdout.read().strip()

    def lock(g: subprocess.Popen[str], line: str) -> str:
        assert g.stdin is not None and g.stdout is not None
        first = g.stdout.readline().strip()
        if first != "LOCKED":
            return first
        g.stdin.write(line + "\n")
        g.stdin.flush()
        return g.stdout.readline().strip()

    def drift(p: targets.Probe) -> str:
        return report(p.provenance, running=p.running or "")[0]

    point(cur, OLD)
    point(profile, OLD)
    b_early = targets.probe("tester")
    assert b_early.running == OLD and b_early.provenance == "", b_early
    a = guard(OLD, NEW, targets.probe("tester").digest)
    assert lock(a, provenance_line(NEW)) == "WROTE"
    b_claimed = targets.probe("tester")  # A claimed, not yet activated
    point(cur, NEW)  # A's test activation: verification starts
    b_mid = targets.probe("tester")
    assert b_mid.running == NEW and "refused: runs an increment" in drift(b_mid), b_mid
    # Probes from before the activation wait on A's lock and then abort.
    late = [guard(b.running or "", "/nix/store/b-system", b.digest) for b in (b_early, b_claimed)]
    assert until_eof(a) == "", "A's claim stands: the target runs NEW"
    assert [lock(g, provenance_line("/nix/store/b-system")) for g in late] == [f"MOVED {NEW}"] * 2
    for g in late:
        until_eof(g)
    assert targets.probe("tester").digest == b_mid.digest
    # A third switch that rolls back puts A's provenance back.
    c = guard(NEW, "/nix/store/c-system", b_mid.digest)
    assert lock(c, provenance_line("/nix/store/c-system")) == "WROTE"
    until_eof(c)
    assert targets.probe("tester").digest == b_mid.digest
    # One staged for a reboot keeps its claim: the profile names it.
    c = guard(NEW, "/nix/store/c-system", b_mid.digest)
    assert lock(c, provenance_line("/nix/store/c-system")) == "WROTE"
    point(profile, "/nix/store/c-system")
    until_eof(c)
    staged = targets.probe("tester")
    assert '"toplevel": "/nix/store/c-system"' in staged.provenance, staged
    # Same running system, rewritten provenance: B's compare fails.
    g = guard(NEW, "/nix/store/b-system", b_mid.digest)
    assert lock(g, "") == f"MOVED {NEW}, its provenance rewritten by another deploy"
    until_eof(g)
    # A rolled-back switch onto a target with no provenance leaves none.
    provenance.unlink()
    point(profile, NEW)
    g = guard(NEW, "/nix/store/d-system", targets.probe("tester").digest)
    assert lock(g, provenance_line("/nix/store/d-system")) == "WROTE"
    until_eof(g)
    assert not provenance.exists()
    say("switch guard: ok")


def toml(extra: str = "", precheck: str = "", branches: tuple[str, ...] = ("work/tester/*",), **over: str) -> str:
    """A nixconvoy.toml with every required key (the defaults the scratch
    repos use, `over` overriding, `branches` the work_branches), `extra`
    appended before the [eval] table and `precheck` in it."""
    keys = {"flake_dir": ".", "targets_dir": "hosts", "targets_arch": "x86_64-linux", "ssh_address_template": "{target}",
            "branch_prefix": "work/{machine}/", "child_worktrees": ".claude/worktrees", **over}
    return ("config_version = 1\n" + "".join(f'{k} = "{v}"\n' for k, v in keys.items())
            + f"work_branches = {json.dumps(list(branches))}\n"
            + extra + '[eval]\nparallel = "auto"\n' + (f'precheck = "{precheck}"\n' if precheck else ""))


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def put(where: Path, name: str, text: str, msg: str = "") -> None:
    (where / name).parent.mkdir(parents=True, exist_ok=True)
    (where / name).write_text(text)
    sh(where, "add", name)
    if msg:
        sh(where, "commit", "--quiet", "-m", msg)


def scratch_env() -> None:
    os.environ.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": os.devnull})
    # The scratch repos keep their flake under nixos/, with a repo lint as
    # the precheck.
    config.set_cfg(config.parse(toml(precheck="lint", flake_dir="nixos", targets_dir="nixos/hosts")))
    clear_memo()


def clear_memo() -> None:
    for cached in (memo.union_objects, memo.scan_sound, memo.target_eval_reads, memo.path_inputs, memo.file_facts):
        cached.cache_clear()


@contextlib.contextmanager
def scratch_origin(d: Path, branches: str, files: dict[str, str], *, wb: str | None = None,
                   seeded: Callable[[Path], object] = lambda _seed: None) -> Iterator[tuple[Path, Path, Path]]:
    """(origin, seed, co): a bare origin whose main holds a nixconvoy.toml
    listing `branches` (patterns, one per line), `files` and what `seeded`
    adds in the seed clone, and the checkout co cloned from it, on `wb`
    when given and the cwd throughout."""
    origin, seed, co = d / "origin.git", d / "seed", d / "homelab9"
    sh(d, "init", "--quiet", "--bare", "-b", "main", str(origin))
    sh(d, "clone", "--quiet", str(origin), str(seed))
    put(seed, "nixconvoy.toml", toml(precheck="lint", flake_dir="nixos", targets_dir="nixos/hosts",
                                     branches=tuple(ln.strip() for ln in branches.splitlines() if ln.strip())))
    for name, text in files.items():
        put(seed, name, text)
    seeded(seed)
    sh(seed, "commit", "--quiet", "-m", "seed")
    sh(seed, "push", "--quiet", "origin", "HEAD:main")
    sh(d, "clone", "--quiet", str(origin), str(co))
    prev = Path.cwd()
    os.chdir(co)
    config.set_root(co)
    try:
        if wb is not None:
            checkout.ensure_on_work_branch(wb)
        yield origin, seed, co
    finally:
        os.chdir(prev)
        config.set_root(prev)


def reach_cases(d: Path) -> None:
    """reached_targets on a scratch repo of two targets: which of them this
    checkout's work changes a read file or lock entry of, measured from
    what each target's provenance recorded of the branch."""
    scratch_env()
    wb = "work/tester/9"
    lock = {"version": 7, "root": "root", "nodes": {
        "root": {"inputs": {"nixpkgs": "nixpkgs", "p": "p", "q": "q", "r": "r"}},
        "nixpkgs": {"locked": {"rev": "n1"}}, "p": {"locked": {"rev": "p1"}}, "q": {"locked": {"rev": "q1"}},
        "r": {"locked": {"rev": "r1"}}}}
    files = {
        "nixos/flake.nix": "{\n  outputs = { self, nixpkgs, p, q, r }: {\n"
        "    hosts = [ ./hosts/a/configuration.nix ./hosts/b/configuration.nix ];\n"
        "    specialArgs = { inherit p q; };\n"
        "    checks.x86_64-linux.lint = import ./lint/check.nix {\n      inherit r;\n      root = ../.;\n    };\n"
        "    lib.lint = import ./lib/lint.nix { };\n  };\n}\n",
        "nixos/flake.lock": proc.dump_json(lock),
        # The lint reads the whole flake directory; skipped as the precheck.
        "nixos/lint/check.nix": "{ }: { src = ../.; }\n",
        "nixos/pkgs/tool.nix": "1\n",
        "nixos/pkgs/other.nix": "1\n",
        # Named like the precheck attribute, but under `lib`: flake.nix reads it.
        "nixos/lib/lint.nix": "1\n",
        "nixos/hosts/a/configuration.nix": "{ p, ... }: { imports = [ ../../modules/x.nix ]; }\n",
        "nixos/hosts/b/configuration.nix": "{ q, ... }: { imports = [ ../../modules/y.nix ]; }\n",
        "nixos/modules/x.nix": "{ pkgs, ... }: { environment.systemPackages = [ (import ../pkgs/tool.nix) ]; }\n",
        "nixos/modules/y.nix": "{ ... }: { }\n",
        "README.md": "docs\n",
    }
    with scratch_origin(d, f"{wb}*\n", files, wb=wb) as (_origin, _seed, co):
        targets = ["a", "b"]

        def reached(provenances: dict[str, str] | None = None) -> set[str]:
            tip = sh(co, "rev-parse", "HEAD")
            clear_memo()
            return memo.reached_targets(str(co), tip, targets, tip, provenances or {})

        def provenance(**tips: str) -> str:
            return proc.dump_json({"version": 2, "main": sh(co, "rev-parse", "origin/main"),
                                     "branches": tips})

        main0 = sh(co, "rev-parse", "HEAD")
        assert reached() == set()
        put(co, "README.md", "docs, more\n", "docs")
        assert reached() == set()
        put(co, "nixos/modules/x.nix", "{ pkgs, ... }: { environment.systemPackages = [ (import ../pkgs/tool.nix) ]; x = 1; }\n", "x")
        c_x = sh(co, "rev-parse", "HEAD")
        assert reached() == {"a"}
        # A package file counts for the targets importing it; one nothing
        # imports reaches no target, though the precheck's lint names its
        # whole directory.
        put(co, "nixos/pkgs/tool.nix", "2\n", "tool")
        put(co, "nixos/pkgs/other.nix", "2\n", "other")
        assert reached({"a": provenance(**{wb: c_x})}) == {"a"}
        c_tool = sh(co, "rev-parse", "HEAD")
        put(co, "nixos/pkgs/other.nix", "3\n", "other again")
        assert reached({"a": provenance(**{wb: c_tool}), "b": provenance(**{wb: c_tool})}) == set()
        c_tool = sh(co, "rev-parse", "HEAD")
        put(co, "nixos/lib/lint.nix", "2\n", "lib lint")
        assert reached({"a": provenance(**{wb: c_tool}), "b": provenance(**{wb: c_tool})}) == {"a", "b"}
        c_tool = sh(co, "rev-parse", "HEAD")
        # A module naming a directory that holds the flake keys its target
        # on the whole tree, and the scan says so.
        put(co, "nixos/modules/y.nix", "{ ... }: { all = ../.; }\n", "y whole")
        c_whole = sh(co, "rev-parse", "HEAD")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert reached({"a": provenance(**{wb: c_tool}), "b": provenance(**{wb: c_tool})}) == {"b"}
        assert "b keys on the whole nixos/ tree: nixos/modules/y.nix" in err.getvalue(), err.getvalue()
        # ...so an edit nothing imports reaches it from then on.
        put(co, "nixos/pkgs/other.nix", "4\n", "other under whole")
        assert reached({"a": provenance(**{wb: c_whole}), "b": provenance(**{wb: c_whole})}) == {"b"}
        # The repository root likewise: the flake's source is the whole repo.
        put(co, "nixos/modules/y.nix", "{ ... }: { all = ../..; }\n", "y root")
        c_root = sh(co, "rev-parse", "HEAD")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert reached({"a": provenance(**{wb: c_whole}), "b": provenance(**{wb: c_whole})}) == {"b"}
        assert "b keys on the whole repository: nixos/modules/y.nix" in err.getvalue(), err.getvalue()
        put(co, "README.md", "docs, under root\n", "docs under root")
        assert reached({"a": provenance(**{wb: c_root}), "b": provenance(**{wb: c_root})}) == {"b"}
        put(co, "nixos/modules/y.nix", "{ ... }: { }\n", "y plain")
        c_tool = sh(co, "rev-parse", "HEAD")
        # A lock bump reaches the targets whose files name the input.
        lock["nodes"]["q"]["locked"]["rev"] = "q2"
        put(co, "nixos/flake.lock", proc.dump_json(lock), "bump q")
        c_q = sh(co, "rev-parse", "HEAD")
        assert reached() == {"a", "b"}
        assert reached({"a": provenance(**{wb: c_tool})}) == {"b"}
        # An input only the precheck's stanza names reaches no target.
        lock["nodes"]["r"]["locked"]["rev"] = "r2"
        put(co, "nixos/flake.lock", proc.dump_json(lock), "bump r")
        assert reached({"a": provenance(**{wb: c_q}), "b": provenance(**{wb: c_q})}) == set()
        c_q = sh(co, "rev-parse", "HEAD")
        # What a target already runs of this branch is not new work for it:
        # a recorded tip between the merge base and HEAD narrows the delta,
        # one at HEAD empties it, an unknown or foreign one is ignored.
        assert reached({"a": provenance(**{wb: c_x}), "b": provenance(**{wb: c_q})}) == {"a"}
        assert reached({"a": provenance(**{wb: c_q}), "b": provenance(**{wb: c_q})}) == set()
        assert reached({"a": provenance(**{wb: "f" * 40}), "b": provenance(**{"work/other/1": c_q})}) == {"a", "b"}
        assert reached({"a": "", "b": "{not json"}) == {"a", "b"}
        # A file moved out of a directory a target reads whole changes that
        # target, though the move lists no path it reads any more.
        put(co, "nixos/hosts/a/extra.nix", "{ }\n", "extra")
        c_extra = sh(co, "rev-parse", "HEAD")
        sh(co, "mv", "nixos/hosts/a/extra.nix", "nixos/modules/extra.nix")
        sh(co, "commit", "--quiet", "-m", "moved")
        assert reached({"a": provenance(**{wb: c_extra}), "b": provenance(**{wb: c_extra})}) == {"a"}
        # An input taken as a relative path (a submodule's gitlink) is read
        # from the tree: moving it reaches the targets naming the input.
        lock["nodes"]["p"] = {"locked": {"path": "./vendor/p", "type": "path"}}
        sh(co, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},nixos/vendor/p")
        put(co, "nixos/flake.lock", proc.dump_json(lock), "p as a path input")
        c_p = sh(co, "rev-parse", "HEAD")
        sh(co, "update-index", "--add", "--cacheinfo", f"160000,{'2' * 40},nixos/vendor/p")
        sh(co, "commit", "--quiet", "-m", "p moved")
        assert reached({"a": provenance(**{wb: c_p}), "b": provenance(**{wb: c_p})}) == {"a"}
        # A recorded tip on main's side of the merge base (the branch had
        # nothing then; main moved since) is the merge base.
        upstream = sh(co, "commit-tree", f"{main0}^{{tree}}", "-p", main0, "-m", "upstream")
        sh(co, "push", "--quiet", "origin", f"{upstream}:main")
        sh(co, "fetch", "--quiet")
        sh(co, "rebase", "--quiet", "origin/main")
        assert reached({"a": provenance(**{wb: main0}), "b": provenance(**{wb: main0})}) == {"a", "b"}
        # Between two commits, as the wait for a deploy under way measures
        # its increment (reached_between): x.nix's change reaches a alone.
        tip = sh(co, "rev-parse", "HEAD")
        clear_memo()
        assert memo.reached_between(str(co), tip, targets, f"{c_x}^", c_x) == {"a"}
        assert memo.reached_between(str(co), tip, targets, c_x, c_x) == set()
        assert memo.reached_between(str(co), tip, ["b"], f"{c_x}^", c_x) == set()
        # The staged increment a deploy decides on: docs reach nothing,
        # a module its target.
        (co / "README.md").write_text("docs, staged\n")
        sh(co, "add", "README.md")
        assert not deploy.increment_reaches(checkout.staged_snapshot(), targets)
        (co / "nixos/modules/x.nix").write_text("{ ... }: { x = 2; }\n")
        sh(co, "add", "nixos/modules/x.nix")
        assert deploy.increment_reaches(checkout.staged_snapshot(), targets)
        sh(co, "reset", "--quiet", "--hard", "HEAD")
    say("reach: ok")


def git_cases(d: Path) -> None:
    """The rebase and union cores (switch-guard/src/plan/) driven through
    nixconvoy's git queries in scratch clones of a scratch origin; pull,
    close, prune and undeploy on them."""
    scratch_env()

    def state(where: Path) -> tuple[str, ...]:
        return (sh(where, "rev-parse", "HEAD"), sh(where, "diff", "--cached"), sh(where, "diff"),
                sh(where, "status", "--porcelain"))

    wb = "work/tester/9"
    with scratch_origin(d, f"{wb}*\nwork/other/1\n", {"a.txt": "one\ntwo\nthree\n"}, wb=wb) as (origin, seed, co):
        assert checkout.current_branch() == wb
        assert sh(co, "config", f"branch.{wb}.merge") == "refs/heads/main"
        put(co, "b.txt", "local\n", "local")
        put(co, "COMMIT_MSG.md", "mine\n", "msg")
        put(seed, "a.txt", "ONE\ntwo\nthree\n", "upstream")
        sh(seed, "push", "--quiet", "origin", "HEAD:main")
        sh(co, "fetch", "--quiet")

        # Staged, unstaged and untracked changes ride the rebase.
        put(co, "a.txt", "one\ntwo\nTHREE\n")
        (co / "b.txt").write_text("local, unstaged\n")
        (co / "u.txt").write_text("untracked\n")
        checkout.ensure_rebased()
        assert proc.is_ancestor("refs/remotes/origin/main", "HEAD")
        assert sh(co, "diff", "--cached", "--name-only") == "a.txt"
        assert sh(co, "diff", "--name-only") == "b.txt"
        assert (co / "a.txt").read_text() == "ONE\ntwo\nTHREE\n"
        assert "?? u.txt" in sh(co, "status", "--porcelain")
        assert sh(co, "log", "-1", "--format=%s") == "msg"

        # A conflicting staged change: left exactly as it was.
        put(seed, "a.txt", "ONE\nTWO-upstream\nthree\n", "upstream 2")
        sh(seed, "push", "--quiet", "origin", "HEAD:main")
        sh(co, "fetch", "--quiet")
        put(co, "a.txt", "one\nTWO-local\nTHREE\n")
        before = state(co)
        err = dies(checkout.ensure_rebased)
        assert f"rebasing {wb} onto origin/main is not clean" in err, err
        assert state(co) == before and checkout.git_op_in_progress() is None
        sh(co, "restore", "--staged", "--worktree", "a.txt")
        checkout.ensure_rebased()

        # A dead run's tmp commit is popped back into the index.
        sh(co, "add", "b.txt")
        sh(co, "commit", "--quiet", "-m", "tmp: staged (ensure_rebased)")
        checkout.pop_tmp_commits()
        assert sh(co, "log", "-1", "--format=%s") == "msg"
        assert sh(co, "diff", "--cached", "--name-only") == "b.txt"
        sh(co, "commit", "--quiet", "-m", "b")

        # The union: another branch merged, COMMIT_MSG.md dropped.
        sh(seed, "switch", "--quiet", "-c", "work/other/1")
        put(seed, "o.txt", "other\n", "other")
        put(seed, "COMMIT_MSG.md", "theirs\n", "their msg")
        sh(seed, "push", "--quiet", "origin", "work/other/1")
        sh(co, "fetch", "--quiet")
        fetched: Future[subprocess.CompletedProcess[str]] = Future()
        fetched.set_result(subprocess.CompletedProcess([], 0))
        staged: list[list[str]] = []
        refreshes: list[str] = []

        def stage(wt: str, union: str, listed: list[str], branches: list[tuple[str, str]]) -> bool:
            assert sh(Path(wt), "rev-parse", "HEAD") == union
            assert listed == ["work/other/1", f"{wb}*"], listed
            assert [b for b, _ in branches] == ["work/other/1"], branches
            staged.append(sorted(p.name for p in Path(wt).iterdir()))
            return False

        def union(main0: str | None = None) -> dict[str, object]:
            q = UnionQueries(lambda: "HEAD", fetched, lambda: refreshes.append("r"), stage)
            try:
                with contextlib.redirect_stdout(io.StringIO()) as said:
                    done = guard.decide("union", q, wb=wb, evict=False,
                                         main0=main0 or proc.rev("refs/remotes/origin/main"))
            finally:
                q.drop()
            assert isinstance(done, dict)
            done["printed"] = said.getvalue()
            return done

        done = union()
        assert done["branches"] == [["work/other/1", proc.rev("refs/remotes/origin/work/other/1")]], done
        assert sh(co, "rev-parse", f"{done['union']}^") == proc.rev("refs/remotes/origin/main")
        assert staged == [[".git", "a.txt", "b.txt", "nixconvoy.toml", "o.txt"]], staged
        # A fetch that moved origin/main redoes the union on the refreshed
        # checkout.
        redo = union(main0="0" * 40)
        assert refreshes == ["r"] and redo["union"] == done["union"], (refreshes, redo)
        assert "redoing the union" in str(redo["printed"]), redo

        # A conflict mergiraf cannot solve names the branch's checkout.
        put(seed, "b.txt", "theirs\n", "their b")
        sh(seed, "push", "--quiet", "origin", "work/other/1")
        sh(co, "fetch", "--quiet")
        err = dies(union)
        assert "cannot union work branch work/other/1;" in err, err
        assert "\n  b.txt\n" in err, err

        # A sync from main: main fast-forwarded, the work branch rebased
        # aside and pushed.
        sh(co, "switch", "--quiet", "main")
        sh(seed, "switch", "--quiet", "main")
        put(seed, "a.txt", "ONE\nTWO-upstream\nthree\nfour\n", "upstream 3")
        sh(seed, "push", "--quiet", "origin", "HEAD:main")
        good, status = checkout.update_checkout(co)
        assert good and status.startswith("fast-forwarded main") and f"rebased {wb}" in status and \
            status.endswith(f"pushed {wb}"), status
        assert proc.is_ancestor("refs/remotes/origin/main", f"refs/remotes/origin/{wb}")
        assert checkout.update_checkout(co) == (True, "up to date")

        # A review subagent's worktree: a detached HEAD at another work
        # branch's tip.  Nothing of its own, so a pull leaves it alone...
        def left_alone(got: tuple[bool, str]) -> bool:
            return got[0] and got[1].startswith("detached HEAD left as is")
        rv = co / "worktrees" / "review"
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/work/other/1")
        assert left_alone(got := checkout.update_checkout(rv)), got
        # ...or at a tip since landed (its tree less COMMIT_MSG.md is
        # main's)...
        sh(seed, "switch", "--quiet", "work/other/1")
        sh(seed, "rebase", "--quiet", "origin/main")
        put(seed, "o.txt", "other, more\n", "more")
        sh(seed, "push", "--quiet", "--force", "origin", "work/other/1")
        sh(co, "fetch", "--quiet")
        more = sh(seed, "rev-parse", "HEAD")
        sh(seed, "rm", "--quiet", "COMMIT_MSG.md")
        sh(seed, "commit", "--quiet", "-m", "squashed")
        landed = sh(seed, "commit-tree", "HEAD^{tree}", "-p", "origin/main", "-m", "squashed")
        sh(seed, "push", "--quiet", "--force", "origin", f"{landed}:main", f"{landed}:work/other/1")
        sh(co, "fetch", "--quiet")
        sh(rv, "checkout", "--quiet", "--detach", more)
        assert left_alone(got := checkout.update_checkout(rv)), got
        # ...but a commit of its own strands.
        put(rv, "r.txt", "review\n", "by hand")
        good, status = checkout.update_checkout(rv)
        assert not good and "carries commits" in status, status
        sh(rv, "reset", "--quiet", "--hard", "HEAD~1")
        out = printed(commands.status)
        line = next(ln for ln in out.splitlines() if "review" in ln)
        assert "nothing of its own" in line and "close review removes it" in line, line
        # close keeps a worktree with anything of its own, saying why, and
        # removes an empty one; a sweep (no names) keeps an unlocked one.
        out = printed(lambda: commands.close_worktrees([]))
        assert "/review: kept, unlocked" in out, out
        put(rv, "r.txt", "review\n", "by hand")
        out = printed(lambda: commands.close_worktrees(["rev"]))
        assert "/review: kept, it carries commits not on origin" in out, out
        sh(rv, "reset", "--quiet", "--hard", "HEAD~1")
        (rv / "u.txt").write_text("untracked\n")
        out = printed(lambda: commands.close_worktrees(["rev"]))
        assert "/review: kept, it has uncommitted" in out, out
        assert rv.exists() and Path.cwd().resolve() == co.resolve()
        (rv / "u.txt").unlink()
        # A sweep removes one whose session is dead, but not while it owns a
        # local work branch carrying commits origin lacks.
        exited = subprocess.Popen(["true"])
        exited.wait()
        sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {exited.pid})", str(rv))
        own = checkout.identity_of(rv.resolve(), None).wb
        assert own is not None
        put(co, "unpushed.txt", "x\n", "unpushed")
        sh(co, "branch", own, "HEAD")
        sh(co, "reset", "--quiet", "--hard", "HEAD~1")
        out = printed(lambda: commands.close_worktrees([]))
        assert f"/review: kept, it owns {own}, which carries commits not on origin" in out, out
        sh(co, "branch", "-D", own)
        out = printed(lambda: commands.close_worktrees([]))
        assert out.endswith("/review: empty, removed\n"), out
        assert not rv.exists() and Path.cwd().resolve() == co.resolve()
        out = printed(lambda: commands.close_worktrees([]))
        assert out.startswith("no subagent worktree under"), out
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/main")
        # The shell that ran a prune from inside it occupies it; only
        # a named close, ending its own session, discounts its invoker.
        probe = ("import sys; from pathlib import Path; from nixconvoy.commands import occupied; "
                 "d = Path(sys.argv[1]); print(occupied(d), occupied(d, invoker=True))")
        got = subprocess.run(["sh", "-c", 'cd "$1" && "$2" -c "$3" "$1"; :', "sh", str(rv.resolve()),
                              sys.executable, probe], env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
                             capture_output=True, text=True, check=True).stdout
        assert got == "True False\n", got
        # Unlike a named close, prune keeps a worktree whose session still runs even
        # when that session is the one asking: it may resume the agent.
        if (parent := os.getppid()) > 1:
            pstart = Path(f"/proc/{parent}/stat").read_text().rpartition(")")[2].split()[19]
            sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {parent} start {pstart})", str(rv))
            out = printed(lambda: commands.close_worktrees([]))
            assert "/review: kept, the claude session" in out, out
            # ...unless its agent stopped: that session ended for it, while
            # nothing relocked it since the stop (a resume does).
            held = f"claude agent review (pid {parent} start {pstart})"
            why = commands.prune_one(rv.resolve(), named=True, stopped=commands.Stop(parent, f"{held} 1.2"))
            assert why is not None and why.startswith("the claude session"), why
            # A lock naming no session tells no relock apart: kept.
            sh(co, "worktree", "unlock", str(rv))
            sessionless = f"claude agent review {checkout.SESSIONLESS}"
            sh(co, "worktree", "lock", "--reason", sessionless, str(rv))
            why = commands.prune_one(rv.resolve(), named=True, stopped=commands.Stop(parent, sessionless))
            assert why is not None and "no claude session" in why, why
            sh(co, "worktree", "unlock", str(rv))
            sh(co, "worktree", "lock", "--reason", held, str(rv))
            exclude = Path(sh(rv, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
            exclude.parent.mkdir(exist_ok=True)
            kept_out = exclude.read_text() if exclude.exists() else ""
            exclude.write_text(kept_out + "target/\n")
            (rv / "target").mkdir()
            (rv / "target" / "build").write_text("x\n")
            why = commands.prune_one(rv.resolve(), named=True, stopped=commands.Stop(parent, held))
            assert why is not None and why.startswith("it has uncommitted"), why
            (rv / "target" / "build").unlink()
            (rv / "target").rmdir()
            exclude.write_text(kept_out)
            # A removal git refuses leaves the lock as it was.
            (rv / "u.txt").write_text("untracked\n")
            os.chdir(rv)
            queries = commands.PruneQueries(rv.resolve(), [])
            try:
                refused = queries({"ask": "remove_worktree"})
            finally:
                queries.close()
                os.chdir(co)
            assert refused.get("ran") is False and rv.exists(), refused
            assert checkout.worktree_lock(rv) == held
            (rv / "u.txt").unlink()
            assert commands.prune_one(rv.resolve(), named=True, stopped=commands.Stop(parent, held)) is None
            assert not rv.exists() and Path.cwd().resolve() == co.resolve()
            sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/main")
        # Where an agent's transcript says it started, and is now.
        log = Path(tempfile.mkdtemp()) / "agent-a1.jsonl"
        log.write_bytes(b'[1]\n\xff\xfe\n{"type":"user","cwd":"/a"}\nnot json\n{"type":"x"}\n{"cwd":"/b"}\n')
        assert commands.transcript_cwd(log) == Path("/a") and commands.transcript_cwd(log, last=True) == Path("/b")
        assert commands.transcript_cwd(log.with_name("none.jsonl")) is None
        # Claude Code's lock names the spawning session's process and its start
        # time: a named close keeps the worktree while that lives, lifted
        # once it is gone (or its pid reused by a process started at another
        # time); undeploy, which removes nothing, does not mind.
        me = os.getpid()
        start = Path(f"/proc/{me}/stat").read_text().rpartition(")")[2].split()[19]
        sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {me} start {start})", str(rv))
        out = printed(lambda: commands.close_worktrees(["rev"]))
        assert "/review: kept, the claude session" in out, out
        assert rv.exists()
        sh(co, "worktree", "unlock", str(rv))
        # ...unless that session is the one running this: an ancestor process.
        if (parent := os.getppid()) > 1:
            pstart = Path(f"/proc/{parent}/stat").read_text().rpartition(")")[2].split()[19]
            sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {parent} start {pstart})", str(rv))
            assert not checkout.lock_live(f"claude agent review (pid {parent} start {pstart})")
            sh(co, "worktree", "unlock", str(rv))
        sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {me} start 1)", str(rv))
        # As the CLI runs it: under the worktree's deploy lock.  A never
        # deployed worktree has nothing to undeploy; a named close removes
        # it once empty (its lock's session counted as ended for it).
        commands.enter_sub("rev")
        (rv / "e.txt").write_text("edit\n")
        sh(rv, "add", "e.txt")
        held = checkout.lock_checkout()
        assert held is not None
        err = dies(lambda: commands.undeploy("origin/main"))
        held.close()
        assert "homelab9/review is not on its work branch" in err and "nothing to undeploy" in err, err
        sh(rv, "reset", "--quiet", "--hard")
        assert rv.exists() and not sh(rv, "status", "--porcelain")
        os.chdir(co)
        out = printed(lambda: commands.close_worktrees(["rev"]))
        assert out.endswith("/review: empty, removed\n"), out
        assert not rv.exists() and Path.cwd().resolve() == co.resolve()
        # The create hook's lock naming no session: the sweep keeps it, a
        # named close removes it.
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/main")
        sh(co, "worktree", "lock", "--reason", f"claude agent review {checkout.SESSIONLESS}", str(rv))
        out = printed(lambda: commands.close_worktrees([]))
        assert "/review: kept, its create found no claude session" in out, out
        out = printed(lambda: commands.close_worktrees(["rev"]))
        assert out.endswith("/review: empty, removed\n"), out
        assert not rv.exists()
        # --force removes whatever it holds, with its branches.
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/main")
        put(rv, "r.txt", "review\n", "by hand")
        (rv / "u.txt").write_text("untracked\n")
        out = printed(lambda: commands.close_worktrees(["rev"]))
        assert "/review: kept, it has uncommitted" in out, out
        out = printed(lambda: commands.close_worktrees(["rev"], force=True))
        assert "/review: removed (--force)" in out, out
        assert not rv.exists() and Path.cwd().resolve() == co.resolve()

        # A subagent's pushed branch, undeployed and deleted with its
        # worktree: a review checkout at its old tip holds commits of its own.
        sub_wb = f"{wb}-review"
        sh(co, "worktree", "add", "--quiet", "-b", sub_wb, str(rv), "origin/main")
        put(rv, "s.txt", "sub\n", "sub work\n\nReaches: a\nDeployed: a")
        sh(rv, "push", "--quiet", "origin", sub_wb)
        sh(co, "fetch", "--quiet")
        tip = proc.rev(f"refs/remotes/origin/{sub_wb}")
        commands.enter_sub("rev")
        assert commands.undeploy("origin/main")
        # Undeployed: reset onto origin/main here and there, the worktree kept.
        assert rv.exists() and sh(rv, "branch", "--show-current") == sub_wb
        assert proc.rev(f"refs/remotes/origin/{sub_wb}") == proc.rev("refs/remotes/origin/main")
        os.chdir(co)
        commands.close_worktrees(["rev"])
        assert not rv.exists() and proc.rev(f"refs/remotes/origin/{sub_wb}") is None
        sh(seed, "fetch", "--quiet")
        sh(seed, "switch", "--quiet", "--detach", "origin/main")
        put(seed, "a.txt", "ONE\nTWO-upstream\nthree\nfour\nfive\n", "upstream 4")
        sh(seed, "push", "--quiet", "origin", "HEAD:main")
        sh(co, "fetch", "--quiet")
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), str(tip))
        good, status = checkout.update_checkout(rv)
        assert not good and "carries commits" in status, status
        commands.enter_sub("rev")
        assert "nothing to undeploy" in dies(lambda: commands.undeploy("origin/main"))  # detached: not on its branch
        assert rv.exists()
        os.chdir(co)
        commands.close_worktrees(["rev"], force=True)
        assert not rv.exists()

        # undeploy alone: the latest increment off the branch, here and on
        # origin, its deployed targets named for the deploy that follows;
        # unstaged edits elsewhere and untracked files stay.
        co9, wb9 = d / "homelab90", "work/tester/90"
        sh(d, "clone", "--quiet", str(origin), str(co9))
        os.chdir(co9)
        checkout.ensure_on_work_branch(wb9)
        assert "nothing to undeploy: no increment beyond origin/main" in dies(commands.undeploy)
        put(co9, "i1.txt", "one\n", "inc 1\n\nReaches: a\nApplied: a")  # Applied: the trailer key before the rename, still read
        put(co9, "i2.txt", "two\n", "inc 2\n\nReaches: a, b\nDeployed: a, b")
        sh(co9, "push", "--quiet", "origin", wb9)
        sh(co9, "fetch", "--quiet")
        tip, parent = sh(co9, "rev-parse", "HEAD"), sh(co9, "rev-parse", "HEAD^")
        assert "--back-to nope: no such commit" in dies(lambda: commands.undeploy("nope"))
        assert "nothing to undeploy: HEAD is HEAD" in dies(lambda: commands.undeploy("HEAD"))
        (co9 / "i1.txt").write_text("one, edited\n")
        (co9 / "u.txt").write_text("untracked\n")
        put(co9, "s.txt", "staged\n")
        assert "staged but undeployed changes present" in dies(commands.undeploy)
        sh(co9, "rm", "--cached", "--quiet", "s.txt")
        (co9 / "s.txt").unlink()
        put(co9, "h.txt", "hand\n", "a hand commit, no deploy's")
        assert "a hand commit, no deploy's is not a deploy's increment" in dies(commands.undeploy)
        assert "is not a deploy's increment" in dies(lambda: commands.undeploy("origin/main"))
        sh(co9, "reset", "--quiet", "--keep", "HEAD^")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert commands.undeploy()
        said = out.getvalue()
        assert f"undeployed {wb9}'s 1 increment(s)" in said and f"\n  {tip[:7]}" in said and \
            "deployed to a, b" in said and f"reset onto {parent[:12]}" in said, said
        assert sh(co9, "rev-parse", "HEAD") == parent and proc.rev(f"refs/remotes/origin/{wb9}") == parent
        assert not (co9 / "i2.txt").exists() and (co9 / "i1.txt").read_text() == "one, edited\n"
        assert (co9 / "u.txt").exists() and sh(co9, "status", "--porcelain") == "M i1.txt\n?? u.txt"  # stripped
        # An unstaged edit to the increment's own file: nothing moves.
        before = state(co9)
        err = dies(commands.undeploy)
        assert "unstaged edits" in err and "i1.txt" in err, err
        assert state(co9) == before and proc.rev(f"refs/remotes/origin/{wb9}") == parent
        sh(co9, "restore", "i1.txt")
        # --back-to origin/main: every increment, their targets together.
        put(co9, "i3.txt", "three\n", "inc 3\n\nReaches: c\nDeployed: c")
        assert f"is not on {wb9}" in dies(lambda: commands.undeploy(tip[:12]))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert commands.undeploy("origin/main")
        said = out.getvalue()
        assert f"undeployed {wb9}'s 2 increment(s)" in said and "deployed to a, c" in said \
            and "reset onto origin/main" in said, said
        assert sh(co9, "rev-parse", "HEAD") == proc.rev("refs/remotes/origin/main") == proc.rev(f"refs/remotes/origin/{wb9}")
        assert (co9 / "u.txt").exists() and not (co9 / "i1.txt").exists()
        assert "nothing to undeploy: no increment beyond origin/main" in dies(commands.undeploy)
        assert "nothing to undeploy: no increment beyond origin/main" in dies(lambda: commands.undeploy("origin/main"))
    say("git cores: ok")


def announce_cases(d: Path) -> None:
    """The announcement of a deploy's pending state and its renewals, the
    deploy records and the push that ends a deploy, on a scratch origin:
    what a deploy elsewhere sees of a deploy under way (checkout.announced),
    what it leaves behind."""
    scratch_env()
    wb = "work/tester/9"
    with mock.patch.object(checkout, "HEARTBEAT", 0.2), \
            scratch_origin(d, f"{wb}*\nwork/other/1\n", {"a.txt": "one\n"}, wb=wb) as (_origin, seed, co):
        put(co, "a.txt", "one\ntwo\n")
        snap = checkout.staged_snapshot()
        t0 = int(time.time())
        # Announced: listed as under way by a fetch anywhere, the author
        # date the deploy's start, this run's own branch left out.
        ann = checkout.Announce(wb, snap)
        assert ann.collect() and ann.key == wb
        first = ann.sha
        sh(co, *checkout.fetch_args())
        assert sh(co, "rev-parse", f"{first}^{{tree}}") == sh(co, "rev-parse", f"{snap}^{{tree}}")
        assert sh(co, "rev-parse", f"{first}^") == sh(co, "rev-parse", "HEAD")
        assert sh(co, "log", "-1", "--format=%s", first) == \
            f"pending increment of {wb} from tester:{co} (pid {os.getpid()})"
        (pend,) = checkout.pending_announcements()
        assert pend.key == pend.branch == wb and pend.sha == first and not pend.settled and \
            t0 <= pend.since <= pend.at <= t0 + 60, pend
        assert checkout.announced(wb) == {}
        assert checkout.announced(None) == {wb: first}
        # Renewed while the deploy runs: a fresh commit of the same tree on
        # origin, the author date kept (the rank), the committer date new.
        deadline = time.monotonic() + 10
        while ann.sha == first and time.monotonic() < deadline:
            time.sleep(0.1)
        sh(co, *checkout.fetch_args())
        (renewed,) = checkout.pending_announcements()
        assert renewed.sha != first and renewed.since == pend.since and \
            sh(co, "rev-parse", f"{renewed.sha}^{{tree}}") == sh(co, "rev-parse", f"{snap}^{{tree}}"), renewed
        # A re-run announces with the rank of the announcement it renews.
        again = checkout.Announce(wb, snap)
        assert again.collect() and again.since == pend.since
        again.stop()
        # Withdrawn (the deploy ended without a switch): cleared onto
        # origin/main, the renewals over.
        ann.withdraw()
        sh(co, *checkout.fetch_args())
        assert checkout.pending_announcements() == []
        assert sh(co, "rev-parse", f"{checkout.PENDING_REFS}/{wb}") == proc.rev("refs/remotes/origin/main")
        time.sleep(0.5)
        sh(co, *checkout.fetch_args())
        assert checkout.pending_announcements() == []

        # The deploy records: the union's tree, chained on the target's
        # previous record first, then main, the branches and the increment;
        # a parent not here is left out.  Pushed before the switch.
        ann = checkout.Announce(wb, snap)
        assert ann.collect()
        committed = checkout.deployed_commit(snap, "two\n\nReaches: a\nDeployed: a")
        main = proc.rev("refs/remotes/origin/main")
        assert main is not None
        # Holds replay oldest first: reached and not deployed holds, a later
        # deploy of the target ends the hold, a commit without trailers is
        # nothing.
        chain = checkout.deployed_commit(snap, "h1\n\nReaches: a, b, c\nDeployed: a")
        chain = out_at(co, chain, "plain")
        chain = out_at(co, chain, "h2\n\nReaches: b\nApplied: b")  # the key before the rename
        chain = out_at(co, chain, "h3\n\nReaches: none\nDeployed: none")
        assert checkout.held_targets(f"{main}..{chain}") == {"c"}
        assert checkout.held_targets(f"{main}..{chain}^") == {"c"}
        assert checkout.held_targets(f"{main}..{chain}~2") == {"b", "c"}
        assert checkout.held_targets(f"{main}..{chain}~3") == {"b", "c"}
        rec1 = checkout.record_commit("a", snap, ["", main, "f" * 40, committed], "two", '{"doc": 1}')
        assert sh(co, "log", "-1", "--format=%P", rec1).split() == [main, committed]
        assert sh(co, "log", "-1", "--format=%s%n%b", rec1) == 'deploy a: two\n{"doc": 1}'
        rec2 = checkout.record_commit("a", snap, [rec1, main, committed, main], "three", "{}")
        assert sh(co, "log", "-1", "--format=%P", rec2).split() == [rec1, main, committed]
        assert checkout.push_records({"a": rec1, "b": rec2}) is None
        assert checkout.push_records({}) is None
        assert sh(seed, "ls-remote", "origin", f"{checkout.DEPLOYING_REFS}/a").split()[0] == rec1
        # The push that ends the deploy: the work branch, then the records
        # onto refs/heads/deployed/<target> and the announcement cleared.
        checkout.adopt_commit(wb, committed)
        checkout.push_deployed(wb, ann, {"a": rec1, "b": rec2})
        checkout.await_background_push()
        sh(co, *checkout.fetch_args())
        assert proc.rev(f"refs/remotes/origin/{wb}") == committed
        assert proc.rev(f"refs/remotes/origin/{checkout.DEPLOYED_PREFIX}/a") == rec1
        assert proc.rev(f"refs/remotes/origin/{checkout.DEPLOYED_PREFIX}/b") == rec2
        assert checkout.pending_announcements() == []
        # A main checkout's deploy: the records alone.
        checkout.push_deployed(None, None, {"a": rec2})
        checkout.await_background_push()
        sh(co, *checkout.fetch_args())
        assert proc.rev(f"refs/remotes/origin/{checkout.DEPLOYED_PREFIX}/a") == rec2
        assert proc.rev(f"refs/remotes/origin/{checkout.DEPLOYED_PREFIX}/b") == rec2
        # Nothing to push: no background push started.
        checkout.push_deployed(wb, None, {})
        assert not Path(checkout.push_state("pid")).exists()
        # A main checkout's announcement (its HEAD origin lacks), under
        # main@<machine>, is settled by the push that ends its deploy (over,
        # not waited for) and cleared by the push of main that lands that
        # HEAD, not by nixconvoy.
        ann = checkout.Announce("main", "HEAD")
        assert ann.collect() and ann.key == "main@tester"
        checkout.push_deployed(None, ann, {"a": rec2})
        checkout.await_background_push()
        sh(co, *checkout.fetch_args())
        (pend,) = checkout.pending_announcements()
        assert (pend.key, pend.branch, pend.settled) == ("main@tester", "main", True), pend
        sh(co, "push", "--quiet", "origin", "HEAD:main")
        sh(co, *checkout.fetch_args())
        assert checkout.pending_announcements() == []
        # The branch not pushed (its lease failed): the announcement stays,
        # so the deploy still counts as under way (dead, once its renewals
        # stop) until a push of the branch clears it.
        ann = checkout.Announce(wb, snap)
        assert ann.collect()
        put(co, "a.txt", "one\ntwo\nthree\n", "three")
        sh(seed, "fetch", "--quiet", "origin", f"+refs/heads/{wb}:refs/heads/{wb}")
        sh(seed, "switch", "--quiet", wb)
        put(seed, "b.txt", "theirs\n", "theirs")
        sh(seed, "push", "--quiet", "origin", wb)
        checkout.push_deployed(wb, ann, {})
        err = warned(checkout.await_background_push)
        assert "background push failed" in err, err
        sh(co, *checkout.fetch_args())
        assert list(checkout.announced(None)) == [wb]
        assert checkout.clear_refspec(wb) == [f"+{proc.rev('refs/remotes/origin/main')}:{checkout.PENDING_REFS}/{wb}"]
    say("announcements: ok")


def out_at(co: Path, parent: str, msg: str) -> str:
    """A commit of `parent`'s tree on `parent` with the message `msg`."""
    return sh(co, "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", msg)


def dies(f: Callable[[], object]) -> str:
    """Run `f`, which must die() (cli.main: exit 1 after one): its stderr."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            f()
        except proc.Fail:
            return err.getvalue()
        except SystemExit as e:
            assert e.code == 1, e.code
            return err.getvalue()
    raise AssertionError("did not die")


def submodule_cases(d: Path) -> None:
    """The submodule flow on scratch repos: the planted worktree, a
    deploy's sync (the branch pushed, the gitlink pinned and staged), the
    union of two branches, a landing (squashed onto the submodule's main,
    pinned there), the catch-up onto a moved main and the removal."""
    scratch_env()
    from nixconvoy import submodule  # noqa: PLC0415 - after scratch_env's config
    dep_origin, dep_seed = d / "dep.git", d / "dep-seed"
    sh(d, "init", "--quiet", "--bare", "-b", "main", str(dep_origin))
    sh(d, "clone", "--quiet", str(dep_origin), str(dep_seed))
    put(dep_seed, "tool.py", "v1\n", "dep v1")
    sh(dep_seed, "push", "--quiet", "origin", "HEAD:main")
    dep_repo = d / "dep"  # the repository's checkout here, never deployed from
    sh(d, "clone", "--quiet", str(dep_origin), str(dep_repo))
    config.set_cfg(config.parse(toml(f'[submodules.dep]\nrepo = "{dep_repo}"\n', flake_dir="nixos", targets_dir="nixos/hosts")))

    wb = "work/tester/9"
    path = "nixos/submodules/dep"
    v1 = sh(dep_seed, "rev-parse", "HEAD")
    lock = {"nodes": {"dep": {"flake": False, "locked": {"path": "./submodules/dep", "type": "path"},
                              "original": {"path": "./submodules/dep", "type": "path"}, "parent": []},
                      "root": {"inputs": {"dep": "dep"}}},
            "root": "root", "version": 7}

    def pinned(top: Path | None = None) -> str | None:
        sub = submodule.at_path(top or co, path)
        assert sub is not None
        return submodule.pin(sub, top or co)

    def dep(top: Path | None = None) -> submodule.Sub:
        return submodule.present_one(top or co, "dep")

    def dep_ref(ref: str) -> str:
        """The submodule origin's `ref` (the first match of a pattern), "" for none."""
        return (sh(dep_seed, "ls-remote", str(dep_origin), ref).split() or [""])[0]

    files = {".gitmodules": f'[submodule "dep"]\n\tpath = {path}\n\turl = {dep_origin}\n',
             "nixos/flake.lock": proc.dump_json(lock) + "\n"}
    with scratch_origin(d, "work/tester/*\n", files,
                        seeded=lambda seed: sh(seed, "update-index", "--add", "--cacheinfo", f"160000,{v1},{path}")) \
            as (origin, _seed, co):
        assert (co / path).is_dir() and not any((co / path).iterdir())  # unpopulated, as cloned
        checkout.ensure_on_work_branch(wb)
        ax = submodule.create("dep", wb)
        assert ax == co / path and submodule.branch_at(ax) == wb
        assert sh(ax, "rev-parse", "HEAD") == v1
        assert sh(ax, "config", f"branch.{wb}.merge") == "refs/heads/main"
        assert (dep_repo / ".git" / submodule.MARKER).exists()
        assert not sh(co, "status", "--porcelain")  # planted at the pinned rev
        assert submodule.create("dep", wb) == ax  # idempotent
        assert submodule.sync(wb, None) == []  # nothing of its own: no push, the pin as it was
        assert not submodule.own(co) and submodule.notes(co) == ["submodule dep"]

        # Staged in the worktree: committed on its branch, pushed, the gitlink pinned and staged here.
        put(ax, "tool.py", "v2\n")
        assert "submodule dep has staged changes" in dies(lambda: submodule.sync(wb, None))
        assert "dry activation cannot carry" in dies(lambda: submodule.sync(wb, None, dry=True))
        told = submodule.sync(wb, "dep: v2")
        v2 = sh(ax, "rev-parse", "HEAD")
        assert v2 != v1 and sh(ax, "log", "-1", "--format=%s") == "dep: v2"
        assert dep_ref(f"refs/heads/{wb}") == v2
        assert len(told) == 2 and "pushed" in told[0] and "pinned" in told[1], told
        assert sh(co, "diff", "--cached", "--name-only") == path
        assert pinned() == v2
        assert submodule.sync(wb, "again") == []  # settled: nothing to do
        assert submodule.sync(wb, None, dry=True) == []  # deployed state: a dry run may carry it
        assert submodule.own(co)
        assert submodule.notes(co) == ["submodule dep +1"]
        # A worktree reset below what the tree deploys: refused rather than
        # pinned away; a hand rebase of it (the same patch) passes.
        sh(ax, "reset", "--quiet", "--hard", v1)
        err = dies(lambda: submodule.sync(wb, "again"))
        assert "was reset below the work the tree deploys" in err and "dep: v2" in err, err
        sh(ax, "reset", "--quiet", "--hard", v2)
        sh(ax, "commit", "--quiet", "--amend", "-m", "dep: v2, amended")
        told = submodule.sync(wb, "same patch")
        v2b = sh(ax, "rev-parse", "HEAD")
        assert v2b != v2 and pinned() == v2b and len(told) == 2, told
        sh(ax, "reset", "--quiet", "--hard", v2)
        noted = warned(lambda: submodule.sync(wb, "back"))  # the same patch again: nothing to say
        assert pinned() == v2 and not noted, noted
        # An amend that changes the patch: pinned, the deployed commit it
        # leaves behind named.
        (ax / "tool.py").write_text("v2, changed\n")
        sh(ax, "add", "tool.py")
        sh(ax, "commit", "--quiet", "--amend", "--no-edit")
        noted = warned(lambda: submodule.sync(wb, "changed"))
        v2c = sh(ax, "rev-parse", "HEAD")
        assert pinned() == v2c
        assert "leaves 1 deployed commit(s) behind" in noted and v2[:7] in noted, noted
        sh(ax, "reset", "--quiet", "--hard", v2)
        submodule.sync(wb, "back")
        assert pinned() == v2
        assert dep_ref(f"refs/heads/{wb}") == v2

        # A landing refuses what it would lose or never deployed.
        (ax / "tool.py").write_text("v2 edited\n")
        assert "unstaged edits" in dies(lambda: submodule.land_one(dep(), wb, "m"))
        sh(ax, "checkout", "--", "tool.py")
        (ax / "loose.txt").write_text("x\n")  # untracked: harmless, the worktree stays
        put(ax, "tool.py", "v3\n", "hand commit, never deployed")
        assert "not what the tree pins" in dies(lambda: submodule.land_one(dep(), wb, "m"))
        assert "not deployed yet" in dies(lambda: submodule.sync(wb, None, dry=True))
        sh(ax, "reset", "--quiet", "--hard", v2)

        # The deploy's commit of the pin, then the landing: the branch squashed onto its main.
        sh(co, "commit", "--quiet", "-m", "dep: v2")

        # undeploy: the pin the latest increment moved is put back, its
        # worktree reset below it and the branch pushed so, what stops it
        # (staged changes, an unstaged edit the reset would lose) checked
        # ahead of the checkout's own reset.
        tip = sh(co, "rev-parse", "HEAD")
        moved = submodule.moved_pins(co, wb, tip, f"{tip}^")
        assert [(s.name, was) for s, was in moved] == [("dep", v1)], moved
        put(ax, "tool.py", "staged\n")
        assert "submodule dep has staged changes" in dies(lambda: submodule.moved_pins(co, wb, tip, f"{tip}^"))
        sh(ax, "reset", "--quiet", "--hard", v2)
        (ax / "tool.py").write_text("v2 edited\n")
        err = dies(lambda: submodule.moved_pins(co, wb, tip, f"{tip}^"))
        assert "unstaged edits, which the reset would lose" in err and "tool.py" in err, err
        err = dies(lambda: submodule.undeploy_pins(moved, wb))
        assert "the reset onto" in err and sh(ax, "rev-parse", "HEAD") == v2, err
        sh(ax, "checkout", "--", "tool.py")
        submodule.undeploy_pins(moved, wb)
        assert sh(ax, "rev-parse", "HEAD") == v1 and (ax / "loose.txt").exists()
        assert dep_ref(f"refs/heads/{wb}") == v1
        assert dep_ref(f"{submodule.PAST_REFS}/{wb}/{v2}") == v2
        sh(ax, "reset", "--quiet", "--hard", v2)  # ...and back, as a deploy would carry it again
        submodule.sync(wb, "again")
        assert pinned() == v2 and dep_ref(f"refs/heads/{wb}") == v2

        # Another checkout pins the submodule on its own branch: the union
        # merges the two branches, pushed as the submodule origin's
        # refs/nixconvoy/union/dep, and pins the gitlink there.
        co8, wb8 = d / "homelab8", "work/tester/8"
        sh(d, "clone", "--quiet", str(origin), str(co8))
        os.chdir(co8)
        checkout.ensure_on_work_branch(wb8)
        ax8 = submodule.create("dep", wb8)
        put(ax8, "tool2.py", "b\n")
        submodule.sync(wb8, "dep: b")
        b1 = sh(ax8, "rev-parse", "HEAD")
        sh(co8, "commit", "--quiet", "-m", "dep: b")
        sh(co8, "push", "--quiet", "origin", wb8)
        os.chdir(co)
        sh(co, "fetch", "--quiet")
        settled: Future[subprocess.CompletedProcess[str]] = Future()
        settled.set_result(subprocess.CompletedProcess([], 0))

        def union() -> dict[str, object]:
            q = UnionQueries(lambda: "HEAD", settled, lambda: None, lambda *_: False)
            try:
                done = guard.decide("union", q, wb=wb, evict=False, main0=proc.rev("refs/remotes/origin/main"))
            finally:
                q.drop()
            assert isinstance(done, dict)
            return done

        done = union()
        u = sh(co, "rev-parse", f"{done['union']}:{path}")
        assert dep_ref(f"{submodule.UNION_REFS}/dep/{u}") == u
        assert sh(dep_repo, "rev-parse", f"{u}^1") == v2 and sh(dep_repo, "rev-parse", f"{u}^2") == b1
        assert sh(dep_repo, "show", f"{u}:tool.py") == "v2" and sh(dep_repo, "show", f"{u}:tool2.py") == "b"
        assert union()["union"] == done["union"]  # deterministic
        assert not sh(dep_repo, "worktree", "list", "--porcelain").count("submodule-merge")
        # The branches conflicting is the other branch's conflict.
        put(ax8, "tool.py", "vB\n", "dep: vB")
        sh(ax8, "push", "--quiet", "--force", "origin", wb8)
        os.chdir(co8)
        submodule.set_pin(dep(co8), sh(ax8, "rev-parse", "HEAD"), co8)
        sh(co8, "commit", "--quiet", "-m", "dep: vB")
        sh(co8, "push", "--quiet", "origin", wb8)
        os.chdir(co)
        sh(co, "fetch", "--quiet")
        err = dies(union)
        assert f"cannot union work branch {wb8};" in err and path in err, err
        sh(ax8, "reset", "--quiet", "--hard", b1)
        sh(ax8, "push", "--quiet", "--force", "origin", wb8)
        sh(co8, "reset", "--quiet", "--hard", "HEAD~1")
        sh(co8, "push", "--quiet", "--force", "origin", wb8)
        sh(co, "fetch", "--quiet")
        assert union()["union"] == done["union"]
        # Union refs are kept, pinned or not.
        sh(dep_repo, "push", "--quiet", "origin", f"{v1}:{submodule.UNION_REFS}/dep/{v1}")
        submodule.FETCHED.clear()
        assert union()["union"] == done["union"]
        assert sh(dep_seed, "ls-remote", str(dep_origin), f"{submodule.UNION_REFS}/dep/*").split()[1::2] == sorted(
            [f"{submodule.UNION_REFS}/dep/{u}", f"{submodule.UNION_REFS}/dep/{v1}"])

        put(co, "COMMIT_MSG.md", "dep v2 landed\n", "msg")
        (ax / "COMMIT_MSG.md").write_text("dep v2 landed\n\nthe submodule's own message\n")  # untracked
        put(dep_seed, "other.txt", "x\n", "upstream meanwhile")
        sh(dep_seed, "push", "--quiet", "origin", "HEAD:main")
        # Past refs: the landing deletes those no ref of origin pins (wb8 pins b1).
        put(dep_seed, "junk.txt", "j\n", "a tip nothing pins")
        junk = sh(dep_seed, "rev-parse", "HEAD")
        sh(dep_seed, "reset", "--quiet", "--hard", "HEAD~1")
        sh(dep_seed, "push", "--quiet", "origin", f"{junk}:{submodule.PAST_REFS}/gone/{junk}")
        sh(dep_repo, "push", "--quiet", "origin", f"{b1}:{submodule.PAST_REFS}/keep/{b1}")
        # The repository's own checkout, idle on main since the clone, follows
        # in the sweep after the landing (the landing itself leaves it alone:
        # nothing of its may stop main's commit), like the main checkout
        # follows main, an untracked file of its riding along; one on
        # another branch is left where it is.
        assert sh(dep_repo, "rev-parse", "HEAD") == v1 and submodule.branch_at(dep_repo) == "main"
        (dep_repo / "scratch.txt").write_text("s\n")
        submodule.land_all(wb, "dep v2 landed")
        assert not dep_ref(f"{submodule.PAST_REFS}/gone/*")
        assert dep_ref(f"{submodule.PAST_REFS}/keep/{b1}") == b1
        # The branch's deployed tip v2, pinned by nothing once the landing moved wb: not kept.
        assert not dep_ref(f"{submodule.PAST_REFS}/{wb}/*")
        dep_main = dep_ref("refs/heads/main")
        assert sh(ax, "rev-parse", "HEAD") == dep_main
        assert sh(dep_repo, "rev-parse", "HEAD") == v1
        sh(dep_repo, "switch", "--quiet", "-c", "aside")
        with contextlib.redirect_stdout(io.StringIO()) as noted:
            assert submodule.catch_up_repos()
        assert sh(dep_repo, "rev-parse", "HEAD") == v1 and not noted.getvalue()
        sh(dep_repo, "switch", "--quiet", "main")
        with contextlib.redirect_stdout(io.StringIO()) as noted:
            assert submodule.catch_up_repos()
        assert sh(dep_repo, "rev-parse", "HEAD") == dep_main and submodule.branch_at(dep_repo) == "main"
        assert f"{dep_repo} (submodule dep): fast-forwarded main" in noted.getvalue(), noted.getvalue()
        assert sh(dep_repo, "status", "--porcelain") == "?? scratch.txt"
        (dep_repo / "scratch.txt").unlink()
        assert sh(ax, "log", "-1", "--format=%B", dep_main) == "dep v2 landed\n\nthe submodule's own message"
        assert not (ax / "COMMIT_MSG.md").exists()
        assert sh(ax, "show", f"{dep_main}:tool.py") == "v2" and sh(ax, "show", f"{dep_main}:other.txt") == "x"
        assert sh(ax, "rev-list", "--count", f"{v1}..{dep_main}") == "2"  # upstream's, then the squash
        assert pinned() == dep_main
        after_land = sh(co, "rev-parse", "HEAD")
        assert not sh(co, "diff", "--cached") and sh(co, "log", "-1", "--format=%s").startswith("submodule dep:")
        assert (ax / "loose.txt").exists() and submodule.own(co)  # untracked: kept, and the worktree's own
        (ax / "loose.txt").unlink()
        assert not submodule.own(co)
        submodule.land_all(wb, "dep v2 landed")  # a retry: nothing left to do
        assert sh(co, "log", "-1", "--format=%s").startswith("submodule dep:")

        # Landed, the worktree stays on its branch at the landed commit, pushed so.
        assert ax.exists() and submodule.branch_at(ax) == wb
        assert dep_ref(f"refs/heads/{wb}") == dep_main
        assert submodule.keeps(dep(), co) is None
        # The removal (close <input>): worktree and branch gone, here and
        # on the origin, the branch's last tip kept under PAST_REFS there;
        # the directory stays, empty, so the gitlink is not deleted.
        submodule.remove_all(co)
        assert ax.is_dir() and not any(ax.iterdir()) and not sh(co, "status", "--porcelain")
        assert not dep_ref(f"refs/heads/{wb}")
        assert dep_ref(f"{submodule.PAST_REFS}/{wb}/{dep_main}") == dep_main
        assert submodule.present(co) == []
        assert f"refs/heads/{wb}" not in sh(dep_repo, "for-each-ref", "--format=%(refname)")

        # close <input> --force's pieces: reset onto the submodule's main,
        # the deployed pin put back to origin/main's, staged; close refuses until then.
        sh(co, "push", "--quiet", "origin", f"HEAD:refs/heads/{wb}")
        ax = submodule.create("dep", wb)  # from origin/main (the branch went)
        put(ax, "tool.py", "v4\n")
        submodule.sync(wb, "dep: v4")
        v4 = sh(ax, "rev-parse", "HEAD")
        assert pinned() == v4
        sh(co, "commit", "--quiet", "-m", "dep: v4")
        assert submodule.orphaned(co) == []
        why = submodule.keeps(dep(), co)
        assert why is not None and why.startswith("it carries commits not on origin/main"), why
        submodule.reset_one(dep(), wb)
        assert submodule.restore(dep(), co)
        assert ax.exists() and sh(ax, "rev-parse", "HEAD") == dep_main
        assert dep_ref(f"refs/heads/{wb}") == dep_main
        assert pinned() == v1  # origin/main's
        assert sh(co, "diff", "--cached", "--name-only") == path
        assert submodule.keeps(dep(), co) is None
        sh(co, "reset", "--quiet", "--hard", "HEAD")  # the pin back on the branch's...
        why = submodule.keeps(dep(), co)
        assert why is not None and why.startswith("the tree still pins"), why
        submodule.remove_all(co)  # ...what --force does, the pin then restored
        assert submodule.orphaned(co) == ["dep"]  # the worktree gone, the pin still the branch's

        # The landing on main: the other checkout's rebase onto it meets the
        # landed gitlink, rebases its branch onto the landed default branch
        # and pins that - once, every later pin of the same submodule (here
        # the deploy's staged one, replayed as a tmp commit that resolves to
        # nothing) pinning the same tip; the branch is pushed once the
        # rebase is through.
        sh(co, "push", "--quiet", "--force", "origin", f"{after_land}:refs/heads/main")
        os.chdir(co8)
        put(ax8, "tool3.py", "c\n", "dep: c")  # a deploy's increment from before the landing...
        t2 = sh(ax8, "rev-parse", "HEAD")
        sh(ax8, "push", "--quiet", "--force", "origin", wb8)
        submodule.set_pin(dep(co8), t2, co8)  # ...its pin staged, as that deploy's sync left it
        sh(co8, "fetch", "--quiet")
        # The submodule's worktree cannot follow (edits): the rebase's
        # failure says why git could not do it alone and what to do.
        (ax8 / "tool2.py").write_text("b, edited\n")
        err = dies(checkout.ensure_rebased)
        assert f"rebasing {wb8} onto origin/main is not clean" in err and "submodule dep's pin" in err and \
            f"its worktree ({ax8}) has edits" in err and "Could not read" not in err, err
        assert sh(ax8, "rev-parse", "HEAD") == t2 and pinned(co8) == t2
        sh(ax8, "checkout", "--", "tool2.py")
        said = printed(checkout.ensure_rebased)
        assert proc.is_ancestor("refs/remotes/origin/main", "HEAD")
        b2 = sh(ax8, "rev-parse", "HEAD")
        assert b2 not in (b1, t2) and proc.is_ancestor(dep_main, b2, cwd=str(ax8))
        assert sh(ax8, "show", f"{b2}:tool.py") == "v2" and sh(ax8, "show", f"{b2}:tool3.py") == "c"
        assert pinned(co8) == b2
        assert sh(co8, "log", "--format=%s", "origin/main..HEAD").split("\n") == ["dep: b"], sh(co8, "log", "--format=%s", "origin/main..HEAD")
        assert not sh(co8, "status", "--porcelain")
        assert said.count("rebased onto origin/main") == 1, said
        assert dep_ref(f"refs/heads/{wb8}") == b2
        assert dep_ref(f"{submodule.PAST_REFS}/{wb8}/{t2}") == t2
        assert submodule.PENDING == {}

        # The default branch moving again, with no conflict to meet it (no
        # deploy pinned anything since): the catch-up every pull, landing
        # and deploy runs brings each worktree onto the new tip as it brings
        # the checkout onto main.  One with commits of its own (co8's,
        # pinned in its branch) is rebased, pushed and pinned, staged; one
        # with nothing of its own is fast-forwarded with its unstaged edits
        # intact, nothing pushed or pinned; one whose edits touch what
        # landed is left as it was and named, failing the pull.
        sh(dep_seed, "fetch", "--quiet")
        sh(dep_seed, "reset", "--quiet", "--hard", "origin/main")
        put(dep_seed, "more.txt", "m\n", "upstream once more")
        sh(dep_seed, "push", "--quiet", "origin", "HEAD:main")
        new_main = sh(dep_seed, "rev-parse", "HEAD")
        submodule.FETCHED.clear()
        os.chdir(co)
        # The repository's own checkout follows in the sweep's last pass, a
        # worktree of the submodule present or not; an edit there that the
        # new tip touches leaves it as it was, named.
        assert sh(dep_repo, "rev-parse", "HEAD") == dep_main
        (dep_repo / "more.txt").write_text("mine\n")
        with contextlib.redirect_stderr(io.StringIO()) as said:
            assert not submodule.catch_up_repos()
        assert f"{dep_repo} (submodule dep): rebasing main" in said.getvalue() and "not clean" in said.getvalue(), \
            said.getvalue()
        assert sh(dep_repo, "rev-parse", "HEAD") == dep_main and (dep_repo / "more.txt").read_text() == "mine\n"
        assert not sh(dep_repo, "status", "--porcelain", "-uno")
        (dep_repo / "more.txt").unlink()
        with contextlib.redirect_stdout(io.StringIO()) as noted:
            assert submodule.catch_up_repos()
        assert sh(dep_repo, "rev-parse", "HEAD") == new_main
        assert f"{dep_repo} (submodule dep): fast-forwarded main" in noted.getvalue(), noted.getvalue()
        with contextlib.redirect_stdout(io.StringIO()) as noted:
            assert submodule.catch_up_repos()  # settled: silent
        assert not noted.getvalue()
        good, status = checkout.update_checkout(co8)
        assert good, status
        b3 = sh(ax8, "rev-parse", "HEAD")
        assert b3 != b2 and proc.is_ancestor(new_main, b3, cwd=str(ax8))
        assert sh(ax8, "show", f"{b3}:tool3.py") == "c" and sh(ax8, "show", f"{b3}:more.txt") == "m"
        assert dep_ref(f"refs/heads/{wb8}") == b3
        os.chdir(co8)
        assert pinned(co8) == b3
        assert sh(co8, "diff", "--cached", "--name-only") == path
        sh(co8, "commit", "--quiet", "-m", "dep: b on the new main")
        os.chdir(co)
        ax = submodule.create("dep", wb)  # from origin/main: nothing of its own
        assert submodule.restore(dep(), co)  # the orphaned pin back to main's
        sh(ax, "reset", "--quiet", "--hard", dep_main)
        (ax / "tool.py").write_text("edited\n")  # unstaged, a file the landing left alone
        pin_before = pinned()
        assert submodule.catch_up(co, wb) == []
        assert sh(ax, "rev-parse", "HEAD") == new_main
        assert (ax / "tool.py").read_text() == "edited\n" and not sh(ax, "diff", "--cached", "--name-only")
        assert pin_before == dep_main and pinned() == new_main  # the pin follows the fast-forward
        assert not dep_ref(f"refs/heads/{wb}")
        sh(ax, "reset", "--quiet", "--hard", dep_main)
        (ax / "more.txt").write_text("mine\n")  # what the landing added
        problems = submodule.catch_up(co, wb)
        assert len(problems) == 1 and "is not clean, left as it was" in problems[0], problems
        assert sh(ax, "rev-parse", "HEAD") == dep_main and (ax / "more.txt").read_text() == "mine\n"
        assert not sh(ax, "status", "--porcelain", "-uno")
        os.chdir(co8)
        good, status = checkout.update_checkout(co)
        assert not good and "submodule dep: rebasing" in status and "is not clean" in status, status
        (ax / "more.txt").unlink()
        os.chdir(co)
        assert submodule.catch_up(co, wb) == []
        assert sh(ax, "rev-parse", "HEAD") == new_main
        # A dry activation leaves the submodule where it is; the deploy's
        # own submodule step brings it along.
        sh(ax, "reset", "--quiet", "--hard", dep_main)
        assert submodule.sync(wb, None, dry=True) == [] and sh(ax, "rev-parse", "HEAD") == dep_main
        assert submodule.sync(wb, None) == []
        assert sh(ax, "rev-parse", "HEAD") == new_main
        submodule.remove_all(co)
        # pin_file: every pin also writes the rev and its committer date,
        # staged; a union merge is dated a second past its parents' newest.
        config.set_cfg(config.parse(toml(f'[submodules.dep]\nrepo = "{dep_repo}"\npin_file = "nixos/submodules/dep.json"\n',
                                         flake_dir="nixos", targets_dir="nixos/hosts")))
        s = submodule.sub_of("dep", submodule.entries(co)["dep"], co)
        pin_json = co / "nixos/submodules/dep.json"
        date = lambda rev: int(sh(dep_repo, "show", "-s", "--format=%ct", rev))
        submodule.set_pin(s, new_main, co)
        assert json.loads(pin_json.read_text()) == {"committed": date(new_main), "rev": new_main}
        assert "nixos/submodules/dep.json" in sh(co, "diff", "--cached", "--name-only").split()
        sh(dep_seed, "checkout", "--quiet", dep_main)
        put(dep_seed, "side.txt", "s\n", "side")  # diverging from new_main
        side = sh(dep_seed, "rev-parse", "HEAD")
        sh(dep_seed, "push", "--quiet", "origin", f"{side}:refs/heads/side")
        sh(dep_repo, "fetch", "--quiet", "origin")
        merge = submodule.merge_commit("dep", dep_repo, new_main, side, "union")
        assert merge is not None and merge not in (new_main, side)
        assert date(merge) == max(date(new_main), date(side)) + 1
        assert submodule.merge_commit("dep", dep_repo, new_main, side, "union") == merge  # deterministic
        submodule.set_pin(s, merge, co)
        assert json.loads(pin_json.read_text()) == {"committed": date(merge), "rev": merge}
        # A stale file (a hand-moved gitlink) is rewritten by a deploy on a
        # work branch, only said in a main checkout.
        pin_json.write_text('{"committed": 1, "rev": "0"}\n')
        assert warned(lambda: submodule.heal_pin_files(co, None, False)) and json.loads(pin_json.read_text())["rev"] == "0"
        assert submodule.heal_pin_files(co, wb, False) == [f"submodule dep: nixos/submodules/dep.json rewritten for {merge[:12]}"]
        assert json.loads(pin_json.read_text())["rev"] == merge
    say("submodules: ok")


def open_cases(d: Path) -> None:
    """`nixconvoy open --child <name>`: the subagent worktree under the checkout, its
    branch and lock, reused on a second open; a worktree nested anywhere
    in a checkout is its subagent's, another repository's is not.  `open
    <path>` / `close <path>`: an agent checkout beside the others on a
    free listed work branch.  `land --with-children`: the checkout and its
    subagent worktree landed as one commit."""
    scratch_env()
    with scratch_origin(d, "work/tester/*\n", {"a.txt": "one\n"}, wb="work/tester/9") as (origin, _seed, co):
        w = commands.open_worktree("task", "claude agent task (pid 1 start 2) 3.4")
        assert w == co / ".claude/worktrees/task" and checkout.branch_at(w) == "worktree-task"
        # Nothing ignored the directory: open excluded it for the clone.
        assert "/.claude/worktrees/\n" in (co / ".git/info/exclude").read_text()
        assert not sh(co, "status", "--porcelain")
        assert sh(w, "rev-parse", "HEAD") == sh(co, "rev-parse", "origin/main")
        assert checkout.worktree_lock(w) == "claude agent task (pid 1 start 2) 3.4"
        assert checkout.identity_of(w.resolve(), "worktree-task") == checkout.Checkout("sub", "work/tester/9-task")
        assert checkout.sub_parent(w.resolve()) == co.resolve() and checkout.sub_parent(co.resolve()) is None
        # Nested deeper, still this checkout's; a worktree of another
        # repository nested here (a submodule's) is nobody's subagent.
        deep = co / "elsewhere/deeper/other"
        sh(co, "worktree", "add", "--quiet", "--detach", str(deep), "origin/main")
        assert checkout.sub_parent(deep.resolve()) == co.resolve()
        sh(co, "worktree", "remove", "--force", str(deep))
        foreign = d / "foreign"
        sh(d, "init", "--quiet", "-b", "main", str(foreign))
        put(foreign, "f.txt", "f\n", "foreign")
        sh(foreign, "worktree", "add", "--quiet", "--detach", str(co / ".claude/worktrees/foreign"))
        assert checkout.sub_parent((co / ".claude/worktrees/foreign").resolve()) is None
        assert commands.subs_of(co.resolve()) == [w.resolve()]
        # review: a name is a subagent worktree (exact, or a unique prefix)
        # or a branch (the worktree on it), never a diff argument.
        assert review.targets(["task", "--stat"], co) == ([w], ["--stat"])
        assert review.targets(["ta"], co) == ([w], [])
        assert review.targets(["worktree-task", "a.txt"], co) == ([w], ["a.txt"])
        assert review.targets(["--with-children", "--stat"], co) == ([co, w], ["--stat"])
        assert "no subagent worktree, submodule, branch or path named nope" in dies(lambda: review.targets(["nope"], co))
        assert "named foreign" in dies(lambda: review.targets(["foreign"], co))  # no configured submodule
        # The same names for land, undeploy and close (names.worktree): a
        # branch stands for the worktree on it, which land and undeploy
        # take whatever checkout it is, close only as a subagent worktree.
        assert names.resolve("work/tester/9", co) == co.resolve() and names.resolve("main", co) == "main"  # seed is a clone
        assert names.resolve("origin/main", co) == "origin/main"  # a remote branch no checkout is on
        assert names.worktree("ta", co, "land", subagent=False) == w.resolve()
        assert names.worktree("work/tester/9", co, "land", subagent=False) == co.resolve()
        assert "not a subagent worktree" in dies(lambda: names.worktree("work/tester/9", co, "close"))
        assert "is on branch origin/main" in dies(lambda: names.worktree("origin/main", co, "land", subagent=False))
        err = dies(lambda: names.worktree("nope", co, "close"))
        assert "named nope" in err and "homelab9/task" in err, err
        # An exact worktree name beats a branch, a branch beats a prefix;
        # a branch is this repository's, never a sibling clone's.
        sh(co, "branch", "--quiet", "ta", "origin/main")
        assert names.resolve("ta", co) == "ta" and names.resolve("t", co) == w.resolve()
        sh(co, "worktree", "add", "--quiet", "--detach", str(co / ".claude/worktrees/maintenance"), "origin/main")
        assert names.resolve("main", co) == "main" and names.resolve("mai", co) == (co / ".claude/worktrees/maintenance").resolve()
        sh(co, "worktree", "remove", "--force", str(co / ".claude/worktrees/maintenance"))
        sh(d, "clone", "--quiet", str(origin), str(d / "aclone"))
        sh(d / "aclone", "checkout", "--quiet", "-b", "work/tester/9", "origin/main")
        assert names.resolve("work/tester/9", co) == co.resolve()
        sh(co, "branch", "-D", "ta")
        shutil.rmtree(d / "aclone")
        # close takes a branch with slashes as a branch, a path as a path;
        # a name is never a path, a directory of that name or not.
        assert not cli.is_path("work/tester/9") and cli.is_path("sub/new")
        assert cli.is_path("../homelab5") and cli.is_path("worktrees/")
        assert not cli.is_path("worktrees") and not cli.is_path("task")
        # A submodule's name (declared in .gitmodules, configured or not)
        # never names a worktree, whatever worktree it prefixes.
        (co / ".gitmodules").write_text('[submodule "ta"]\n\tpath = deps/ta\n\turl = x\n'
                                        '[submodule "tb"]\n\tpath = deps/tb\n\turl = x\n')
        assert "ta is a submodule" in dies(lambda: names.worktree("ta", co, "land"))
        assert "ta is a submodule" in dies(lambda: commands.open_worktree("ta", None))
        # close: a declared submodule's name means the submodule, a path a
        # checkout, anything else a subagent worktree - one kind per call;
        # several submodules each get their say.
        argv0, cwd0 = sys.argv, Path.cwd()
        try:
            os.chdir(co)
            for argv, want in ((["close", "ta", "task"], "usage:"), (["close", "ta", "../x/"], "usage:"),
                               (["close", "task", "../x/"], "usage:"),
                               (["close", "ta"], "ta is not a configured submodule"),
                               (["close", "ta", "tb"], "close failed for submodule ta, tb")):
                sys.argv = ["nixconvoy", *argv]
                assert want in dies(cli.main), argv
                for held in proc.HELD:  # the checkout lock a close takes, until exit
                    held.close()
                proc.HELD.clear()
        finally:
            sys.argv = argv0
            os.chdir(cwd0)
        (co / ".gitmodules").unlink()
        put(w, "COMMIT_MSG.md", "task done\n")
        put(w, "b.txt", "two\n")
        with (d / "review.out").open("w") as f, contextlib.redirect_stdout(f):
            assert review.main(["task", "--stat"]) == 0
        shown = (d / "review.out").read_text()
        assert shown.startswith(f"══ {co.name}/task ══\n") and "task done" in shown.partition("\n\n")[0] and "b.txt" in shown, shown
        assert "COMMIT_MSG.md" not in shown.partition("\n\n")[2]  # the diff leaves the message out
        sh(w, "reset", "--quiet", "--hard", "HEAD")
        # An agent checkout beside the others, on the branch named after its path.
        five = commands.open_checkout(d / "homelab5", None)
        one = commands.open_checkout(d / "other", None)
        assert five == d / "homelab5" and checkout.branch_at(five) == "work/tester/homelab5"
        assert one == d / "other" and checkout.branch_at(one) == "work/tester/other"
        # A sibling whose tree cannot even be parsed as a configuration is
        # caught up on this checkout's listing, not its own.
        (one / "nixconvoy.toml").write_text("config_version = 1\n")
        assert checkout.catch_up([one]) and (one / "nixconvoy.toml").read_text() == "config_version = 1\n"
        sh(one, "checkout", "--", "nixconvoy.toml")
        assert sh(five, "rev-parse", "HEAD") == sh(co, "rev-parse", "origin/main")
        assert sh(five, "config", "branch.work/tester/homelab5.merge") == "refs/heads/main"
        assert checkout.identity_of(five.resolve(), "work/tester/homelab5") == checkout.Checkout("agent", "work/tester/homelab5")
        assert "exists already" in dies(lambda: commands.open_checkout(d / "homelab5", None))
        assert "nested" in dies(lambda: commands.open_checkout(co / "inside", None))
        assert "not a work branch" in dies(lambda: commands.open_checkout(d / "x", "work/other/1"))
        # Removed once it holds nothing of its own, its branch with it.
        put(five, "b.txt", "b\n")
        assert "holds edits" in dies(lambda: commands.close_checkout(d / "homelab5"))
        commands.close_checkout(d / "homelab5", force=True)
        commands.close_checkout(d / "other")
        assert not five.exists() and not one.exists()
        assert not sh(co, "branch", "--list", "work/tester/homelab5", "work/tester/other")
        assert "no worktree of this repository" in dies(lambda: commands.close_checkout(foreign))
        os.chdir(w)
        assert "primary worktree" in dies(lambda: commands.close_checkout(co))
        os.chdir(co)
        # land --with-children: the parent's increment and the sub's, each deployed
        # (committed on its work branch and pushed), land as one commit
        # under the parent's COMMIT_MSG.md with the sub's appended; every
        # branch resets onto it.
        put(co, "p.txt", "parent\n")
        put(co, "COMMIT_MSG.md", "the union\n- the sub's piece\n", "parent increment")
        sh(co, "push", "--quiet", "origin", "work/tester/9")
        sh(w, "switch", "--quiet", "-c", "work/tester/9-task")
        (w / "COMMIT_MSG.md").write_text("the sub: s.txt\n- its detail\n")
        sh(w, "add", "COMMIT_MSG.md")
        put(w, "s.txt", "sub\n", "sub increment\n\nReaches: a, b\nDeployed: a")
        sh(w, "push", "--quiet", "origin", "work/tester/9-task")
        put(w, "n.txt", "unstaged\n")
        sh(w, "reset", "--quiet", "n.txt")
        # The sub holds b back: its increment reached b, deployed to a alone.
        assert checkout.held_targets("origin/main..HEAD", str(w)) == {"b"}
        # A held target refuses the landing unless --not-yet-deployed.
        assert "work/tester/9-task holds back b:" in dies(commands.land_with_children)
        told = printed(lambda: commands.land_with_children(not_yet_deployed=True))
        assert "landing with b not yet deployed:" in told
        assert not sh(co, "worktree", "list").count("deploy-wt")
        main = sh(co, "rev-parse", "origin/main")
        assert sh(co, "ls-tree", "--name-only", main).split() == ["a.txt", "nixconvoy.toml", "p.txt", "s.txt"]
        assert sh(co, "log", "-1", "--format=%B", main).strip() == (
            "the union\n\n- the sub's piece\n\nthe sub: s.txt\n- its detail")  # the sub's appended
        assert sh(co, "rev-parse", "HEAD") == main and checkout.branch_at(co) == "work/tester/9"
        assert sh(w, "rev-parse", "HEAD") == main and checkout.branch_at(w) == "work/tester/9-task"
        assert sh(co, "rev-parse", "origin/work/tester/9") == main
        assert sh(co, "rev-parse", "origin/work/tester/9-task") == main
        assert not sh(co, "status", "--porcelain") and sh(w, "status", "--porcelain") == "?? n.txt"
        assert f"has no {config.MSG_FILE}" in dies(commands.land_with_children)  # consumed by the landing
        # land -m: a quick landing with no COMMIT_MSG.md, folded like one;
        # beside the file, committed or a draft on disk, it is refused.
        put(co, "q.txt", "quick\n", "quick increment")
        sh(co, "push", "--quiet", "origin", "work/tester/9")
        put(w, "s2.txt", "sub\n", "sub increment 2")
        sh(w, "push", "--quiet", "origin", "work/tester/9-task")
        commands.land_with_children(message="quick land\n- by -m\n")
        main = sh(co, "rev-parse", "origin/main")
        assert sh(co, "log", "-1", "--format=%B", main).strip() == "quick land\n\n- by -m"
        landed = sh(co, "ls-tree", "--name-only", main).split()
        assert sh(co, "rev-parse", "HEAD") == main and "q.txt" in landed and "s2.txt" in landed
        assert sh(w, "rev-parse", "HEAD") == main
        put(co, "r.txt", "r\n")
        put(co, "COMMIT_MSG.md", "from the file\n", "r increment")
        sh(co, "push", "--quiet", "origin", "work/tester/9")
        tip = sh(co, "rev-parse", "HEAD")
        assert f"work/tester/9 has a {config.MSG_FILE} and -m was given" in dies(lambda: commands.land(message="from -m"))
        assert sh(co, "rev-parse", "HEAD") == tip and sh(co, "rev-parse", "origin/main") == main
        assert not sh(co, "status", "--porcelain")
        commands.land()
        main = sh(co, "rev-parse", "origin/main")
        assert sh(co, "log", "-1", "--format=%B", main).strip() == "from the file"
        put(co, "r2.txt", "r\n", "r2 increment")
        sh(co, "push", "--quiet", "origin", "work/tester/9")
        (co / "COMMIT_MSG.md").write_text("a draft\n")  # on disk alone: refused too
        assert f"has a {config.MSG_FILE} and -m was given" in dies(lambda: commands.land(message="from -m"))
        assert (co / "COMMIT_MSG.md").read_text() == "a draft\n" and sh(co, "rev-parse", "origin/main") == main
        (co / "COMMIT_MSG.md").unlink()
        commands.land(message="from -m")
        main = sh(co, "rev-parse", "origin/main")
        assert sh(co, "log", "-1", "--format=%B", main).strip() == "from -m"
        assert sh(co, "rev-parse", "HEAD") == main and not sh(co, "status", "--porcelain")
        # Which message a landing commits under.
        assert checkout.landing_message(None, "s\n- b\n", "", "x") == "s\n\n- b"
        assert checkout.landing_message(None, "", "draft\n", "x") == ""  # a draft is not HEAD's
        assert checkout.landing_message("m\n- b", "", "", "x") == "m\n\n- b"
        assert checkout.landing_message(" m ", "", " \n", "x") == "m"  # a blank file is none
        assert f"x has a {config.MSG_FILE} and -m was given" in dies(lambda: checkout.landing_message("m", "f\n", "f\n", "x"))
        assert f"sub y has a {config.MSG_FILE}" in dies(lambda: checkout.landing_message("m", "", "d\n", "sub y"))
        assert f"x has a {config.MSG_FILE}" in dies(lambda: checkout.landing_message("m", "f\n", "d\n", "x"))
        # The CLI: -m is land's alone, one non-empty message for one branch,
        # never a flag's name.
        argv0 = sys.argv
        try:
            for argv in (["undeploy", "-m", "x"], ["undeploy", "--submodule", "dep"], ["close", "--submodule", "dep"],
                         ["open", "--child", "--branch", "b", "x"], ["open", "--child", "--submodule", "x"], ["open", "--child"],
                         ["close", "--after", "1"], ["close", "--stopped", "1"], ["open", "--lock", "x", "y"],
                         ["hook"], ["hook", "nope"], ["hook", "session-start", "x"],
                         ["undeploy", "--back-to"], ["undeploy", "--back-to", "--with-children"], ["land", "--back-to", "x"],
                         ["land", "-m", ""], ["land", "-m", " ", "--with-children"], ["land", "-m", "--with-children"],
                         ["land", "-m", "x", "a", "b"], ["land", "-m"]):
                sys.argv = ["nixconvoy", *argv]
                err = dies(cli.main)
                assert err.startswith("usage:"), (argv, err)
        finally:
            sys.argv = argv0
            atexit.unregister(proc.PERF.write)
        assert commands.open_worktree("task", "claude agent task (pid 1 start 2) 5.6") == w  # reused, relocked
        assert checkout.worktree_lock(w) == "claude agent task (pid 1 start 2) 5.6"
        assert commands.open_worktree("task", None) == w  # by hand: the lock kept
        assert checkout.worktree_lock(w) == "claude agent task (pid 1 start 2) 5.6"
        h = commands.open_worktree("hand", None)
        assert checkout.worktree_lock(h) == f"nixconvoy open --child hand {checkout.SESSIONLESS}"
        assert "letters, digits" in dies(lambda: commands.open_worktree("bad name", None))
        # The create hook: the worktree locked with the session's pid and
        # start time, then a token of the create; sessionless without one.
        got = printed(lambda: hooks.worktree_create("hooked", 1))
        assert got.strip() == str(co / ".claude/worktrees/hooked")
        lock = checkout.worktree_lock(co / ".claude/worktrees/hooked") or ""
        assert lock.startswith("claude agent hooked (pid 1" + (" start " if Path("/proc/1/stat").exists() else ")")), lock
        assert checkout.lock_pid_alive(lock) is True
        stdin0, sys.stdin, sys.argv = sys.stdin, io.StringIO(json.dumps({"name": "viahook", "cwd": str(co)})), ["nixconvoy", "hook", "worktree-create"]
        project0 = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            got = printed(cli.main)
        finally:
            sys.stdin, sys.argv = stdin0, argv0
            if project0 is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = project0
        assert got.strip() == str(co / ".claude/worktrees/viahook")
        assert (checkout.worktree_lock(co / ".claude/worktrees/viahook") or "").startswith("claude agent viahook ")
        # A message resuming a stopped agent: the worktree its transcript
        # started in recreated first; anything else sent is let through.
        (d / "sess/subagents").mkdir(parents=True)
        gone = co / ".claude/worktrees/agent-a1b2"
        (d / "sess/subagents/agent-a1b2.jsonl").write_text(json.dumps({"cwd": str(gone)}) + "\n")
        for to in ("main", "a1b2"):
            hooks.worktree_resume({"tool_input": {"to": to}, "transcript_path": str(d / "sess.jsonl")})
        assert (checkout.worktree_lock(gone) or "").startswith("claude agent agent-a1b2 ")
        assert Path.cwd().resolve() == co.resolve()
        # Whatever fails, the resume is refused (exit 2), never let through.
        create0 = hooks.worktree_create

        def broken(name: str, session: int | None) -> None:
            raise OSError(f"no room for {name} (session {session})")
        hooks.worktree_create = broken
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    hooks.worktree_resume({"tool_input": {"to": "a1b2"}, "transcript_path": str(d / "sess.jsonl")})
                except SystemExit as e:
                    assert e.code == 2, e.code
                else:
                    raise AssertionError("resumed")
            assert "no room for agent-a1b2" in err.getvalue(), err.getvalue()
        finally:
            hooks.worktree_create = create0
        # Claude Code letting go of a worktree (worktree-remove): kept while
        # another live session holds it or it holds anything, else removed
        # with its branch.
        sh(co, "worktree", "unlock", str(gone))
        sh(co, "worktree", "lock", "--reason", f"claude agent agent-a1b2 (pid {os.getpid()})", str(gone))
        assert "kept: another claude session holds it" in dies(lambda: hooks.worktree_remove(str(gone)))
        sh(co, "worktree", "unlock", str(gone))
        (gone / "u.txt").write_text("u\n")
        assert "kept: it has uncommitted" in dies(lambda: hooks.worktree_remove(str(gone)))
        (gone / "u.txt").unlink()
        (gone / ".claude/worktrees").mkdir(parents=True)
        (gone / ".claude/worktrees/build.log").write_text("ignored\n")  # excluded, as nested worktrees are
        assert "kept: it has uncommitted" in dies(lambda: hooks.worktree_remove(str(gone)))
        shutil.rmtree(gone / ".claude")
        put(gone, "g.txt", "g\n", "by hand")
        assert "kept: it carries commits" in dies(lambda: hooks.worktree_remove(str(gone)))
        sh(gone, "reset", "--quiet", "--hard", "HEAD~1")
        hooks.worktree_remove(str(gone))
        assert not gone.exists() and proc.rev("refs/heads/worktree-agent-a1b2") is None
        assert Path.cwd().resolve() == co.resolve()
        # ...in a repository nixconvoy does not manage, by those rules alone.
        bare, plain = d / "plain.git", d / "plain"
        sh(d, "init", "--quiet", "--bare", "-b", "main", str(bare))
        sh(d, "clone", "--quiet", str(bare), str(plain))
        put(plain, "p.txt", "p\n", "plain")
        sh(plain, "push", "--quiet", "origin", "HEAD:main")
        pw = plain / ".claude/worktrees/x"
        sh(plain, "worktree", "add", "--quiet", "--lock", "-b", "worktree-x", str(pw), "origin/main")
        hooks.worktree_remove(str(pw))
        assert not pw.exists() and not sh(plain, "branch", "--list", "worktree-x")
        os.chdir(co)
        # The session a hook acts for: the nearest ancestor named claude.
        if Path("/proc/self").exists():
            fake = d / "claude"
            fake.symlink_to(sh_path := subprocess.run(["sh", "-c", "command -v sh"], capture_output=True, text=True, check=True).stdout.strip())
            assert sh_path
            env = {**os.environ, "PYTHONPATH": str(Path(hooks.__file__).parent.parent)}
            # The no-op after python keeps the shell from exec'ing it in place.
            told = subprocess.run([str(fake), "-c", f'echo $$; {sys.executable} -c "from nixconvoy import hooks; print(hooks.claude_session())"; :'],
                                  capture_output=True, text=True, check=True, env=env).stdout.split()
            assert len(told) == 2 and told[0] == told[1], told
            assert hooks.claude_session() is None or hooks.claude_session() in checkout.ancestor_pids()
    say("open: ok")


def require_guard_of_this_tree() -> None:
    """Die unless $NIXCONVOY_GUARD_DIR holds nix/switch-guard.nix's build of
    this tree's switch-guard/ ($NIXCONVOY_GUARD_SRC where the lint copies
    it apart), by its source-hash: a build of another tree fails cases on
    wording or protocol in ways that look like bugs."""
    crate = Path(os.environ.get("NIXCONVOY_GUARD_SRC") or Path(__file__).resolve().parent.parent / "switch-guard")
    tracked = subprocess.run(["git", "-C", str(crate), "ls-files", "-z", "--", "Cargo.toml", "Cargo.lock", "src"],
                             capture_output=True, check=False)
    if tracked.returncode == 0:  # a checkout: what a flake build copies, no stray file
        files = [Path(f.decode()) for f in tracked.stdout.split(b"\0") if f]
    else:  # the build's source as nix copied it
        files = [p.relative_to(crate) for p in [crate / "Cargo.toml", crate / "Cargo.lock", *(crate / "src").rglob("*")]]
    files = sorted(f"./{f}" for f in files if (crate / f).is_file() and not (crate / f).is_symlink())
    listing = "".join(f"{hashlib.sha256((crate / f).read_bytes()).hexdigest()}  {f}\n" for f in files)
    expected = hashlib.sha256(listing.encode()).hexdigest()
    where = os.environ.get("NIXCONVOY_GUARD_DIR")
    if not where:
        sys.exit("switch_test: set NIXCONVOY_GUARD_DIR to `nix build .#switch-guard`'s bin/ for this tree")
    try:
        built = (Path(where).parent / "source-hash").read_text().strip()
    except OSError:
        built = None
    if built != expected:
        sys.exit(f"switch_test: NIXCONVOY_GUARD_DIR ({where}) is {'no nix build of switch-guard' if built is None else 'a build of another tree'}: "
                 f"point it at `nix build .#switch-guard`'s bin/ for this tree")


def init_cases(d: Path) -> None:
    """`nixconvoy init`: nixconvoy.toml detected from the repository (the
    flake under nixos/, its hosts directory, the one system literal), the
    ssh address template as this user, this machine's work-branch pattern,
    nothing staged; a second init refused."""
    scratch_env()

    repo = d / "repo"

    def run_cli(argv: list[str]) -> None:
        os.chdir(repo / "nixos" / "hosts")  # main chdirs to the root and stays
        sys.argv = ["nixconvoy", *argv]
        cli.main()

    sh(d, "init", "--quiet", "-b", "main", str(repo))
    put(repo, "nixos/flake.nix", "{ outputs = _: { }; }\n")
    put(repo, "nixos/hosts/a/configuration.nix", "{ }\n")
    put(repo, "nixos/hosts/b/configuration.nix", '{ nixpkgs.hostPlatform = "aarch64-linux"; }\n')
    put(repo, "nixos/hosts/b/emu.nix", '{ boot.binfmt.emulatedSystems = [ "riscv64-linux" ]; }\n')  # not a platform
    put(repo, "nixos/modules/m.nix", '{ x = "x86_64-darwin"; }\n')  # outside the hosts directory
    put(repo, "nixos/submodules/dep/.keep", "")
    put(repo, "nixos/pkgs/p1/default.nix", "{ }\n")  # packages, not hosts
    put(repo, "nixos/pkgs/p2/default.nix", "{ }\n")
    put(repo, "nixos/pkgs/p3/default.nix", "{ }\n")
    prev = Path.cwd()
    me = getpass.getuser()
    try:
        assert "usage:" in dies(lambda: run_cli(["init", "--ssh-domain", "lan"]))
        assert not (repo / "nixconvoy.toml").exists()
        run_cli(["init"])
        c = config.load(repo)
        assert (c.flake_dir, c.targets_dir, c.targets_arch) == ("nixos", "nixos/hosts", "aarch64-linux"), c
        assert c.ssh_template == me + "@{target}" and c.branch_prefix == "nixconvoy/{machine}/", c
        assert c.work_branches == ("nixconvoy/tester/*",), c
        assert c.child_worktrees == ".claude/worktrees" and c.parallel_eval is None and not c.precheck
        assert not c.submodules and not c.targets
        text = (repo / "nixconvoy.toml").read_text()
        assert "# [targets.a]" in text and "# precheck = " in text and "# [submodules.<input>]" in text, text
        assert "?? nixconvoy.toml" in sh(repo, "status", "--porcelain")  # written, not staged
        assert "already there" in dies(lambda: run_cli(["init"]))
        (repo / "nixconvoy.toml").unlink()
        # The flake named, relative to where init runs.
        run_cli(["init", ".."])
        c = config.load(repo)
        assert (c.flake_dir, c.branch_prefix) == ("nixos", "nixconvoy/{machine}/"), c
    finally:
        os.chdir(prev)
    # A flake at the root with no hosts directory yet.
    bare = d / "bare"
    sh(d, "init", "--quiet", "-b", "main", str(bare))
    put(bare, "flake.nix", "{ outputs = _: { }; }\n")
    os.chdir(bare)
    try:
        sys.argv = ["nixconvoy", "init"]
        cli.main()
        c = config.load(bare)
        assert (c.flake_dir, c.targets_dir, c.ssh_template) == (".", "hosts", me + "@{target}"), c
    finally:
        os.chdir(prev)
    say("init: ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-target"]:
        sys.exit(fake_target(sys.argv[2:]))
    if sys.argv[1:2] == ["--fake-guard"]:
        sys.exit(fake_guard(sys.argv[2], sys.argv[3]))
    require_guard_of_this_tree()
    proc.this_machine = lambda: "tester"
    config.set_cfg(config.parse(toml()))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        with tempfile.TemporaryDirectory() as d:
            switch_cases(Path(d))
        for cases in (gate_cases, provenance_cases, target_cases, protected_cases):
            cases()
        for scratch_cases in (guard_cases, reach_cases, git_cases, announce_cases, submodule_cases, open_cases,
                              init_cases):
            with tempfile.TemporaryDirectory() as d:
                scratch_cases(Path(d))
