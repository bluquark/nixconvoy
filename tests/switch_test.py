"""nixconvoy's switch step end to end against a fake host: the real
deploy-switch binary ($NIXCONVOY_SWITCH_BIN) decides, the switch module's
effects loop performs, and every ssh call lands in this file's --fake-host
mode, which plays the confirmed-switch helper, systemd-run and nix-env on a
JSON state file.  Then the lineage records and the drift classification
other deploys read from them, and deploy-plan's rebase and union cores on
scratch git repos.  Run by the flake's lint check (nix/lint.nix)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path

from nixconvoy import checkout, commands, config, guard, hosts, memo, proc, switch
from nixconvoy.union import UnionQueries

REAL_GUARD = switch.switch_guard_cmd

OLD, NEW = "/nix/store/old-system", "/nix/store/new-system"
# The helper's rollback exit when the confirm landed after all.
ALREADY_CONFIRMED = 3
LINEAGE = '{"version": 1, "toplevel": "/nix/store/new-system"}'


def load(path: Path) -> dict[str, object]:
    state = proc.load_json(path.read_text())
    assert isinstance(state, dict)
    return {str(k): v for k, v in state.items()}


def fake_host(argv: list[str]) -> int:
    """One ssh call to the fake host."""
    path = Path(os.environ["FAKE_HOST_STATE"])
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
            print(f"fake host: unexpected {argv}", file=sys.stderr)
            rc = 99
    path.write_text(proc.dump_json(state))
    return rc


def fake_guard(expected: str, toplevel: str) -> int:
    """switch_guard_cmd's contract: claim the lineage once locked, put it
    back at the end unless the host runs or boots `toplevel`."""
    path = Path(os.environ["FAKE_HOST_STATE"])
    running = load(path)["running"]
    if running != expected:
        print(f"MOVED {running}", flush=True)
        return 1
    print("LOCKED", flush=True)
    rec = sys.stdin.readline().strip()
    if rec:
        state = load(path)
        state["lineage"] = rec
        path.write_text(proc.dump_json(state))
    print("WROTE" if rec else "SKIPPED", flush=True)
    sys.stdin.read()
    state = load(path)
    if toplevel not in (state["running"], state["profile"]):
        state.pop("lineage", None)
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
             deployer: str = "") -> tuple[str | None, dict[str, object]]:
    """Run one switch; (the die message or None, the host's final state)."""
    state_file = Path(os.environ["FAKE_HOST_STATE"])
    state_file.write_text(proc.dump_json({
        "helper": helper, "check_ok": check_ok, "activate_rc": activate_rc,
        "running": running, "profile": running, "armed": False, "rollback_to": rollback_to,
        "check_killed": check_killed, "confirm_rc": confirm_rc, "rollback_rc": rollback_rc,
    }))
    proc.DYING = False
    proc.ARMED.clear()
    err = io.StringIO()
    died: str | None = None
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            switch.switch("testhost", OLD, NEW, deployer or os.environ["NIXCONVOY_SWITCH_BIN"],
                          lineage=LINEAGE)
        except SystemExit:
            died = err.getvalue().strip().splitlines()[0].removeprefix("nixconvoy: ")
    print(f"{name}: {died or 'switched'}")
    return died, load(state_file)


def main() -> None:
    me = [sys.executable, str(Path(__file__).resolve())]

    def host_cmd(host: str, argv: list[str], sudo: bool = False) -> list[str]:
        del host, sudo
        return [*me, "--fake-host", *argv]

    def switch_guard_cmd(host: str, expected: str, toplevel: str, digest: str) -> list[str]:
        del host, digest
        return [*me, "--fake-guard", expected, toplevel]

    hosts.host_cmd = host_cmd
    switch.switch_guard_cmd = switch_guard_cmd
    proc.this_host = lambda: "tester"
    hosts.internet_reachable = lambda timeout=8.0: timeout > 0
    proc.monotonic_ms = Clock()
    time.sleep = lambda _secs: None

    died, s = scenario("confirmed")
    assert died is None and s["running"] == NEW and s["profile"] == NEW, (died, s)
    assert not proc.ARMED
    assert s.get("lineage") == LINEAGE, s

    died, s = scenario("verification fails", check_ok=False)
    assert "lineage" not in s, s
    assert died is not None and "did not verify (testhost: dnsmasq is not active" in died, died
    assert died.endswith(f"rolled back to {OLD}.  Nothing was committed or pushed - the changes are still staged."), died
    assert s["running"] == OLD and s["profile"] == OLD, s

    died, s = scenario("reboot needed", activate_rc=100)
    assert died is not None and "Its profile and bootloader entry are set: reboot testhost" in died, died
    assert s["running"] == OLD and s["profile"] == NEW, s
    assert s.get("lineage") == LINEAGE, s  # the reboot takes the host to it

    died, s = scenario("plain", helper=False)
    assert died is None and s["running"] == NEW and s["profile"] == NEW, (died, s)
    assert s.get("lineage") == LINEAGE, s

    died, s = scenario("moved", running="/nix/store/other")
    assert died is not None and "was switched by another deploy" in died, died
    assert s["running"] == "/nix/store/other" and "lineage" not in s, s

    # A dead deploy's pending rollback, taken over by this arm.
    died, s = scenario("inherited target", check_ok=False, rollback_to="/nix/store/older")
    assert died is not None and died.endswith(
        "rolled back to /nix/store/older.  Nothing was committed or pushed - the changes are still staged."), died
    assert s["running"] == "/nix/store/older", s

    died, s = scenario("check killed", check_killed=True)
    assert died is not None and "check timed out" in died and f"rolled back to {OLD}" in died, died
    assert s["running"] == OLD and "lineage" not in s, s

    died, s = scenario("confirm lost, already confirmed", confirm_rc=1, rollback_rc=ALREADY_CONFIRMED)
    assert died is None and s.get("lineage") == LINEAGE, (died, s)

    died, s = scenario("confirm fails", confirm_rc=1)
    assert died is not None and "verified but could not be confirmed" in died, died
    assert s["running"] == OLD, s

    with tempfile.TemporaryDirectory() as d:
        crash = Path(d) / "crash"
        crash.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n")
        crash.chmod(0o755)
        died, s = scenario("deploy-switch crashes", deployer=str(crash))
    assert died is not None and "deploy-switch stopped answering" in died, died
    assert s["running"] == OLD and s["armed"] is False, s



BRANCH, BASE, LANDED = "haswell-wsl-deploys2", "a" * 40, "b" * 40
ME = ("haswell-wsl", "/home/blu/projects/homelab3")
OTHER = ("haswell-wsl", "/home/blu/projects/homelab2")
LISTED = {"main", BRANCH, "haswell-wsl-deploys3", "chikuwa-deploys1"}


def lineage_line(top: str, *, by: tuple[str, str] = OTHER,
                 pending: tuple[str, str | None] | None = (BRANCH, BASE),
                 others: tuple[str, ...] = ("chikuwa-deploys1",), at: float = 1e6) -> str:
    own = pending[0] if pending else BRANCH
    doc = switch.lineage_doc(
        union="u" * 40, main="m" * 40, branches={b: "c" * 40 for b in others},
        own=own, head="h" * 40, base=pending[1] if pending else "h" * 40,
        snap="s" * 40 if pending else "", at=at, host=by[0], checkout=by[1], pid=4242)
    return proc.dump_json({**doc, "toplevel": top})


def report(text: str, age: float | None, *, tips: dict[str, str] | None = None,
           reset: bool = False, named: bool = True, reached: bool = True,
           running: str = NEW) -> tuple[str, str]:
    """deploy-plan's drift verdict on one drifting host carrying `text` as
    its lineage file: its report line, and the deploy's die message."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            contextlib.suppress(SystemExit):
        guard.decide(
            "drift", me=list(ME), union="u" * 40, listed=sorted(LISTED),
            tips={BRANCH: BASE} if tips is None else tips,
            now=1e6 + 60, tz_offset=0, router="choko", hosts=["side1"], candidates=["side1"],
            reset=reset, named=named, reached=["side1"] if reached else [],
            running={"side1": running},
            built={"side1": "/nix/store/union-system"},
            lineages={"side1": {"text": text, "age": age}})
    proc.DYING = False
    return out.getvalue().strip(), err.getvalue().strip().removeprefix("nixconvoy: ")


def target_cases() -> None:
    """expand_targets: names and @tags from nixconvoy.toml, once each, a
    remote-only host dropped from a tag but not from a name."""
    proc.this_host = lambda: "tester"
    config.set_cfg(config.parse(
        '[hosts.a]\ntags = ["vps"]\n[hosts.b]\ntags = ["vps", "lab"]\n'
        '[hosts.c]\nlocal_only = true\ntags = ["lab"]\n'))
    inv = ["a", "b", "c"]
    assert hosts.expand_targets([], inv) == []
    assert hosts.expand_targets(["@vps"], inv) == ["a", "b"]
    assert hosts.expand_targets(["b", "@vps"], inv) == ["b", "a"]
    assert hosts.expand_targets(["@lab", "c"], inv) == ["b", "c"]
    assert hosts.remote_only("c") and not hosts.remote_only("a")
    config.set_cfg(config.parse('[hosts.a]\ntags = ["vps"]\n[hosts.zz]\ntags = ["vps"]\n'))
    for bad in (["@nope"], ["zz"], ["@vps"]):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                hosts.expand_targets(bad, inv)
        except SystemExit:
            pass
        else:
            raise AssertionError(bad)
    try:
        config.parse('[hosts.a]\ntag = ["vps"]\n')
    except config.ConfigError:
        pass
    else:
        raise AssertionError("unknown host key accepted")
    print("targets: ok")


def lineage_cases() -> None:
    """The drift core (nixos/pkgs/switch-guard/src/plan/drift.rs, whose own
    tests hold the cases) on the lineages lineage_doc writes."""
    line, died = report(lineage_line(NEW), 60)
    assert line.startswith("side1: refused: runs an increment of haswell-wsl-deploys2 deployed by "
                           "haswell-wsl:/home/blu/projects/homelab2 (pid 4242) at 13:46"), line
    assert f"origin/{BRANCH} is still at {BASE[:12]}" in line, line
    assert died.startswith("nothing switched or committed: side1 runs another") and \
        "--reset side1" in died, died
    line, died = report(lineage_line(NEW), 60, reached=False)
    assert line.startswith("side1: left alone: runs an increment of haswell-wsl-deploys2") and \
        line.endswith("this checkout's work does not reach it") and not died, (line, died)
    line, _ = report(lineage_line(NEW), 31 * 60, reached=False)
    assert line.startswith("side1: will switch, reverting it: "), line
    line, died = report(lineage_line(NEW), 60, tips={BRANCH: LANDED})
    assert line.startswith("side1: will switch (catch-up: ") and not died, (line, died)
    line, _ = report(lineage_line(NEW), 31 * 60)
    assert line.startswith("side1: will switch, reverting it: ") and "taken as abandoned" in line, line
    line, _ = report(lineage_line(NEW), 60, reset=True)
    assert line.endswith("taken over by --reset"), line
    line, _ = report(lineage_line(NEW), 60, reset=True, named=False, reached=False)
    assert line.startswith("side1: left alone: "), line
    line, _ = report(lineage_line(NEW), 60, reset=True, named=False)
    assert line.endswith("taken over by --reset"), line
    line, _ = report(lineage_line(NEW, pending=None, others=("M3-Air-deploys9",)), 60)
    assert "side1: refused: runs work of M3-Air-deploys9" in line, line
    assert report(lineage_line(NEW, by=ME), 60)[0].startswith("side1: will switch (this-branch: ")
    assert report("", None) == ("side1: will switch", "")
    assert report("{not json", None) == ("side1: will switch", "")
    assert report(lineage_line(OLD), 60) == ("side1: will switch", "")
    assert report(lineage_line(NEW), 60, running=OLD)[0] == "side1: will switch"
    assert report(lineage_line(NEW), 60, running="/nix/store/union-system")[0] == "side1: up to date"

    text = lineage_line(NEW)
    p = hosts.parse_probe(f"{NEW}\n1000100\n1000000\n{text}")
    assert p.running == NEW and p.lineage == text and p.age == 100, p
    assert p.digest == hashlib.sha256(text.encode()).hexdigest(), p
    p = hosts.parse_probe(f"{OLD}\n1000100\n-\n")
    assert p == hosts.Probe(OLD, "", None, hashlib.sha256(b"").hexdigest()), p

    saved = (switch.rev, switch.current_branch)
    try:
        def record(branch: str | None, head: str, origin: str | None, snap: str) -> object:
            switch.current_branch = lambda: branch
            r = {"HEAD": head, f"refs/remotes/origin/{branch}": origin, "refs/remotes/origin/main": "m"}
            switch.rev = lambda ref: r.get(ref)
            return switch.lineage_record("u", [("chikuwa-deploys1", "c")], snap)["pending"]

        assert record("haswell-wsl-deploys3", "h", "h", "") is None
        assert record("haswell-wsl-deploys3", "h", "h", "s") == {
            "branch": "haswell-wsl-deploys3", "base": "h", "head": "s"}
        assert record("haswell-wsl-deploys3", "h2", "h", "") == {
            "branch": "haswell-wsl-deploys3", "base": "h", "head": "h2"}
        assert record("main", "lock", "m", "") == {"branch": "main", "base": "m", "head": "lock"}
        # Detached at origin/main counts as main's; a hand commit there is pending.
        assert record(None, "m", None, "") is None
        assert record(None, "h", None, "") == {"branch": "main", "base": "m", "head": "h"}
    finally:
        switch.rev, switch.current_branch = saved
    print("lineage classification: ok")


def guard_cases(d: Path) -> None:
    """The real switch guard (switch_guard_cmd's script, flock and all) in a
    scratch directory, through the window a lineage written only after the
    switch left open: B probes while A's switch is between activation and
    confirmation."""
    cur, profile, lineage = d / "current-system", d / "profile", d / "deploy" / "lineage.json"
    hosts.CURRENT_SYSTEM, hosts.SYSTEM_PROFILE = str(cur), str(profile)
    hosts.SWITCH_LOCK, hosts.LINEAGE_FILE, hosts.LINEAGE_WRITER = str(d / "lock"), str(lineage), ""
    proc.this_host = lambda: "tester"

    def point(link: Path, to: str) -> None:
        link.unlink(missing_ok=True)
        link.symlink_to(to)

    def guard(expected: str, top: str, digest: str) -> subprocess.Popen[str]:
        return subprocess.Popen(REAL_GUARD("tester", expected, top, digest),
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

    def drift(p: hosts.Probe) -> str:
        return report(p.lineage, p.age, running=p.running or "")[0]

    point(cur, OLD)
    point(profile, OLD)
    b_early = hosts.probe("tester")
    assert b_early.running == OLD and b_early.lineage == "", b_early
    a = guard(OLD, NEW, hosts.probe("tester").digest)
    assert lock(a, lineage_line(NEW)) == "WROTE"
    b_claimed = hosts.probe("tester")  # A claimed, not yet activated
    point(cur, NEW)  # A's test activation: verification starts
    b_mid = hosts.probe("tester")
    assert b_mid.running == NEW and "refused: runs an increment" in drift(b_mid), b_mid
    # Probes from before the activation wait on A's lock and then abort.
    late = [guard(b.running or "", "/nix/store/b-system", b.digest) for b in (b_early, b_claimed)]
    assert until_eof(a) == "", "A's claim stands: the host runs NEW"
    assert [lock(g, lineage_line("/nix/store/b-system")) for g in late] == [f"MOVED {NEW}"] * 2
    for g in late:
        until_eof(g)
    assert hosts.probe("tester").digest == b_mid.digest
    # A third switch that rolls back puts A's lineage back.
    c = guard(NEW, "/nix/store/c-system", b_mid.digest)
    assert lock(c, lineage_line("/nix/store/c-system")) == "WROTE"
    until_eof(c)
    assert hosts.probe("tester").digest == b_mid.digest
    # One staged for a reboot keeps its claim: the profile names it.
    c = guard(NEW, "/nix/store/c-system", b_mid.digest)
    assert lock(c, lineage_line("/nix/store/c-system")) == "WROTE"
    point(profile, "/nix/store/c-system")
    until_eof(c)
    staged = hosts.probe("tester")
    assert '"toplevel": "/nix/store/c-system"' in staged.lineage, staged
    # Same running system, rewritten lineage: B's compare fails.
    g = guard(NEW, "/nix/store/b-system", b_mid.digest)
    assert lock(g, "") == f"MOVED {NEW}, its lineage rewritten by another deploy"
    until_eof(g)
    # A rolled-back switch onto a host with no lineage leaves none.
    lineage.unlink()
    point(profile, NEW)
    g = guard(NEW, "/nix/store/d-system", hosts.probe("tester").digest)
    assert lock(g, lineage_line("/nix/store/d-system")) == "WROTE"
    until_eof(g)
    assert not lineage.exists()
    print("switch guard: ok")


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
    proc.this_host = lambda: "tester"
    # The scratch repos keep their flake under nixos/ with a pkgs overlay.
    config.set_cfg(config.parse('flake = "nixos"\n[memo]\noverlay = "nixos/pkgs/default.nix"\n'
                                'namespace = "homelab"\n'))
    memo.union_objects.cache_clear()
    memo.eval_key_shape.cache_clear()
    memo.host_eval_reads.cache_clear()


def reach_cases(d: Path) -> None:
    """reached_hosts on a scratch repo of two hosts: which of them this
    checkout's work changes a read file or lock entry of, measured from
    what each host's lineage recorded of the branch."""
    scratch_env()
    origin, co = d / "origin.git", d / "homelab9"
    wb = "tester-deploys9"
    sh(d, "init", "--quiet", "--bare", "-b", "main", str(origin))
    sh(d, "clone", "--quiet", str(origin), str(co))
    lock = {"version": 7, "root": "root", "nodes": {
        "root": {"inputs": {"nixpkgs": "nixpkgs", "p": "p", "q": "q"}},
        "nixpkgs": {"locked": {"rev": "n1"}}, "p": {"locked": {"rev": "p1"}}, "q": {"locked": {"rev": "q1"}}}}
    put(co, "nixos/flake.nix", "{\n  outputs = { self, nixpkgs, p, q }: {\n"
        "    hosts = [ ./hosts/a/configuration.nix ./hosts/b/configuration.nix ];\n"
        "    specialArgs = { inherit p q; };\n  };\n}\n")
    put(co, "nixos/flake.lock", proc.dump_json(lock))
    put(co, "nixos/pkgs/default.nix", "final: prev: {\n  tool = import ./tool.nix;\n  other = import ./other.nix;\n}\n")
    put(co, "nixos/pkgs/tool.nix", "1\n")
    put(co, "nixos/pkgs/other.nix", "1\n")
    put(co, "nixos/hosts/a/configuration.nix", "{ p, ... }: { imports = [ ../../modules/x.nix ]; }\n")
    put(co, "nixos/hosts/b/configuration.nix", "{ q, ... }: { imports = [ ../../modules/y.nix ]; }\n")
    put(co, "nixos/modules/x.nix", "{ pkgs, ... }: { environment.systemPackages = [ pkgs.homelab.tool ]; }\n")
    put(co, "nixos/modules/y.nix", "{ ... }: { }\n")
    put(co, "deploy-branches", f"{wb}*\n")
    put(co, "README.md", "docs\n", "seed")
    sh(co, "push", "--quiet", "origin", "HEAD:main")
    prev = Path.cwd()
    os.chdir(co)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            checkout.ensure_on_work_branch(wb)
        hosts = ["a", "b"]

        def reached(lineages: dict[str, str] | None = None) -> set[str]:
            tip = sh(co, "rev-parse", "HEAD")
            memo.union_objects.cache_clear()
            memo.host_eval_reads.cache_clear()
            return memo.reached_hosts(str(co), tip, hosts, tip, lineages or {})

        def lineage(**tips: str) -> str:
            return proc.dump_json({"version": 1, "main": sh(co, "rev-parse", "origin/main"),
                                     "branches": tips})

        main0 = sh(co, "rev-parse", "HEAD")
        assert reached() == set()
        put(co, "README.md", "docs, more\n", "docs")
        assert reached() == set()
        put(co, "nixos/modules/x.nix", "{ pkgs, ... }: { environment.systemPackages = [ pkgs.homelab.tool ]; x = 1; }\n", "x")
        c_x = sh(co, "rev-parse", "HEAD")
        assert reached() == {"a"}
        # The overlay's attribute files count for the hosts naming them.
        put(co, "nixos/pkgs/tool.nix", "2\n", "tool")
        put(co, "nixos/pkgs/other.nix", "2\n", "other")
        assert reached({"a": lineage(**{wb: c_x})}) == {"a"}
        c_tool = sh(co, "rev-parse", "HEAD")
        # A lock bump reaches the hosts whose files name the input.
        lock["nodes"]["q"]["locked"]["rev"] = "q2"
        put(co, "nixos/flake.lock", proc.dump_json(lock), "bump q")
        c_q = sh(co, "rev-parse", "HEAD")
        assert reached() == {"a", "b"}
        assert reached({"a": lineage(**{wb: c_tool})}) == {"b"}
        # What a host already runs of this branch is not new work for it:
        # a recorded tip between the merge base and HEAD narrows the delta,
        # one at HEAD empties it, an unknown or foreign one is ignored.
        assert reached({"a": lineage(**{wb: c_x}), "b": lineage(**{wb: c_q})}) == {"a"}
        assert reached({"a": lineage(**{wb: c_q}), "b": lineage(**{wb: c_q})}) == set()
        assert reached({"a": lineage(**{wb: "f" * 40}), "b": lineage(**{"other-deploys1": c_q})}) == {"a", "b"}
        assert reached({"a": "", "b": "{not json"}) == {"a", "b"}
        # A file moved out of a directory a host reads whole changes that
        # host, though the move lists no path it reads any more.
        put(co, "nixos/hosts/a/extra.nix", "{ }\n", "extra")
        c_extra = sh(co, "rev-parse", "HEAD")
        sh(co, "mv", "nixos/hosts/a/extra.nix", "nixos/modules/extra.nix")
        sh(co, "commit", "--quiet", "-m", "moved")
        assert reached({"a": lineage(**{wb: c_extra}), "b": lineage(**{wb: c_extra})}) == {"a"}
        # A recorded tip on main's side of the merge base (the branch had
        # nothing then; main moved since) is the merge base.
        upstream = sh(co, "commit-tree", f"{main0}^{{tree}}", "-p", main0, "-m", "upstream")
        sh(co, "push", "--quiet", "origin", f"{upstream}:main")
        sh(co, "fetch", "--quiet")
        sh(co, "rebase", "--quiet", "origin/main")
        assert reached({"a": lineage(**{wb: main0}), "b": lineage(**{wb: main0})}) == {"a", "b"}
    finally:
        os.chdir(prev)
    print("reach: ok")


def git_cases(d: Path) -> None:
    """The rebase and union cores (nixos/pkgs/switch-guard/src/plan/) driven
    through nixconvoy's git queries in scratch clones of a scratch origin."""
    scratch_env()

    def state(where: Path) -> tuple[str, ...]:
        return (sh(where, "rev-parse", "HEAD"), sh(where, "diff", "--cached"), sh(where, "diff"),
                sh(where, "status", "--porcelain"))

    def dies(f: Callable[[], object]) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                f()
            except SystemExit:
                proc.DYING = False
                return err.getvalue()
        raise AssertionError("did not die")

    origin, seed, co = d / "origin.git", d / "seed", d / "homelab9"
    wb = "tester-deploys9"
    sh(d, "init", "--quiet", "--bare", "-b", "main", str(origin))
    sh(d, "clone", "--quiet", str(origin), str(seed))
    put(seed, "deploy-branches", f"{wb}*\nother-deploys1\n")
    put(seed, "a.txt", "one\ntwo\nthree\n", "seed")
    sh(seed, "push", "--quiet", "origin", "HEAD:main")
    sh(d, "clone", "--quiet", str(origin), str(co))
    prev = Path.cwd()
    os.chdir(co)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            checkout.ensure_on_work_branch(wb)
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
        with contextlib.redirect_stdout(io.StringIO()):
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
        assert f"rebasing {wb} onto origin/main is not clean, left as it was" in err, err
        assert state(co) == before and checkout.git_op_in_progress() is None
        sh(co, "restore", "--staged", "--worktree", "a.txt")
        with contextlib.redirect_stdout(io.StringIO()):
            checkout.ensure_rebased()

        # A dead run's tmp commit is popped back into the index.
        sh(co, "add", "b.txt")
        sh(co, "commit", "--quiet", "-m", "tmp: staged (ensure_rebased)")
        checkout.pop_tmp_commits()
        assert sh(co, "log", "-1", "--format=%s") == "msg"
        assert sh(co, "diff", "--cached", "--name-only") == "b.txt"
        sh(co, "commit", "--quiet", "-m", "b")

        # The union: another branch merged, COMMIT_MSG.md dropped.
        sh(seed, "switch", "--quiet", "-c", "other-deploys1")
        put(seed, "o.txt", "other\n", "other")
        put(seed, "COMMIT_MSG.md", "theirs\n", "their msg")
        sh(seed, "push", "--quiet", "origin", "other-deploys1")
        sh(co, "fetch", "--quiet")
        fetched: Future[subprocess.CompletedProcess[str]] = Future()
        fetched.set_result(subprocess.CompletedProcess([], 0))
        staged: list[list[str]] = []
        refreshes: list[str] = []

        def stage(wt: str, union: str, listed: list[str]) -> bool:
            assert sh(Path(wt), "rev-parse", "HEAD") == union
            assert listed == ["other-deploys1", f"{wb}*"], listed
            staged.append(sorted(p.name for p in Path(wt).iterdir()))
            return False

        def union(main0: str | None = None) -> dict[str, object]:
            q = UnionQueries(lambda: "HEAD", fetched, lambda: refreshes.append("r"), stage)
            printed = io.StringIO()
            try:
                with contextlib.redirect_stdout(printed):
                    done = guard.decide("union", q, wb=wb, reset=False,
                                         main0=main0 or proc.rev("refs/remotes/origin/main"))
            finally:
                q.drop()
            assert isinstance(done, dict)
            done["printed"] = printed.getvalue()
            return done

        done = union()
        assert done["branches"] == [["other-deploys1", proc.rev("refs/remotes/origin/other-deploys1")]], done
        assert sh(co, "rev-parse", f"{done['union']}^") == proc.rev("refs/remotes/origin/main")
        assert staged == [[".git", "a.txt", "b.txt", "deploy-branches", "o.txt"]], staged
        # A fetch that moved origin/main redoes the union on the refreshed
        # checkout.
        redo = union(main0="0" * 40)
        assert refreshes == ["r"] and redo["union"] == done["union"], (refreshes, redo)
        assert "origin moved since the last fetch; redoing the union" in str(redo["printed"]), redo

        # A conflict mergiraf cannot solve names the branch's checkout.
        put(seed, "b.txt", "theirs\n", "their b")
        sh(seed, "push", "--quiet", "origin", "other-deploys1")
        sh(co, "fetch", "--quiet")
        err = dies(union)
        assert "cannot union work branch other-deploys1;" in err, err
        assert "\n  b.txt\n" in err, err

        # A sync from main: main fast-forwarded, the work branch rebased
        # aside and pushed.
        sh(co, "switch", "--quiet", "main")
        sh(seed, "switch", "--quiet", "main")
        put(seed, "a.txt", "ONE\nTWO-upstream\nthree\nfour\n", "upstream 3")
        sh(seed, "push", "--quiet", "origin", "HEAD:main")
        good, status = checkout.update_checkout(co)
        assert good and status == (f"fast-forwarded main onto origin/main; rebased {wb} onto "
                                   f"origin/main; pushed {wb}"), status
        assert proc.is_ancestor("refs/remotes/origin/main", f"refs/remotes/origin/{wb}")
        assert checkout.update_checkout(co) == (True, "up to date")

        # A review subagent's worktree: a detached HEAD at another work
        # branch's tip.  Nothing of its own, so a pull leaves it alone...
        left = (True, "detached HEAD left as is: nothing of its own, its commits are already on origin")
        rv = co / checkout.SUB_DIR / "review"
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/other-deploys1")
        assert (got := checkout.update_checkout(rv)) == left, got
        # ...also once that branch was rebased, the old tip forgotten (its
        # commits patch-equivalent)...
        sh(seed, "switch", "--quiet", "other-deploys1")
        sh(seed, "rebase", "--quiet", "origin/main")
        sh(seed, "push", "--quiet", "--force", "origin", "other-deploys1")
        sh(co, "reflog", "expire", "--expire=all", "refs/remotes/origin/other-deploys1")
        sh(co, "fetch", "--quiet")
        assert sh(rv, "rev-parse", "HEAD") != proc.rev("refs/remotes/origin/other-deploys1")
        assert (got := checkout.update_checkout(rv)) == left, got
        # ...or at that tip while the branch moved on and was landed (the
        # remote-tracking ref's reflog remembers the tip as once pushed)...
        sh(rv, "checkout", "--quiet", "--detach", "origin/other-deploys1")
        put(seed, "o.txt", "other, more\n", "more")
        sh(seed, "push", "--quiet", "origin", "other-deploys1")
        sh(co, "fetch", "--quiet")
        more = sh(seed, "rev-parse", "HEAD")
        sh(seed, "rm", "--quiet", "COMMIT_MSG.md")
        sh(seed, "commit", "--quiet", "-m", "squashed")
        landed = sh(seed, "commit-tree", "HEAD^{tree}", "-p", "origin/main", "-m", "squashed")
        sh(seed, "push", "--quiet", "--force", "origin", f"{landed}:main", f"{landed}:other-deploys1")
        sh(co, "fetch", "--quiet")
        assert (got := checkout.update_checkout(rv)) == left, got
        # ...or at the landed tip itself, forgotten (its tree less
        # COMMIT_MSG.md is main's)...
        sh(co, "reflog", "expire", "--expire=all", "refs/remotes/origin/other-deploys1")
        sh(rv, "checkout", "--quiet", "--detach", more)
        assert (got := checkout.update_checkout(rv)) == left, got
        # ...but a commit of its own strands.
        put(rv, "r.txt", "review\n", "by hand")
        good, status = checkout.update_checkout(rv)
        assert not good and status.startswith("the detached HEAD carries commits not on origin/main"), status
        sh(rv, "reset", "--quiet", "--hard", "HEAD~1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.status()
        line = next(ln for ln in out.getvalue().splitlines() if "review" in ln)
        assert "nothing of its own" in line and "prune review removes it" in line, line
        # prune keeps a worktree with anything of its own, saying why, and
        # removes an empty one; a sweep (no names) keeps an unlocked one.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.prune([])
        assert "/review: kept, unlocked: no session to outlive" in out.getvalue(), out.getvalue()
        put(rv, "r.txt", "review\n", "by hand")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.prune(["rev"])
        assert out.getvalue().endswith("/review: kept, it carries commits not on origin, which the "
                                       "removal would lose; push them (git push origin HEAD:main) "
                                       "or deploy from the work branch\n"), out.getvalue()
        sh(rv, "reset", "--quiet", "--hard", "HEAD~1")
        (rv / "u.txt").write_text("untracked\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.prune(["rev"])
        assert "/review: kept, it has uncommitted or untracked changes" in out.getvalue(), out.getvalue()
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
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.prune([])
        assert f"/review: kept, it owns {own}, which carries commits not on origin" in out.getvalue(), out.getvalue()
        sh(co, "branch", "-D", own)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.prune([])
        assert out.getvalue().endswith("/review: empty, removed\n"), out.getvalue()
        assert not rv.exists() and Path.cwd().resolve() == co.resolve()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.prune([])
        assert out.getvalue().startswith("no subagent worktree under"), out.getvalue()
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/main")
        # The shell that ran a prune from inside it occupies it; only
        # abandon, ending its own session, discounts its invoker.
        probe = ("import sys; from pathlib import Path; from nixconvoy.commands import occupied; "
                 "d = Path(sys.argv[1]); print(occupied(d), occupied(d, invoker=True))")
        got = subprocess.run(["sh", "-c", 'cd "$1" && "$2" -c "$3" "$1"; :', "sh", str(rv.resolve()),
                              sys.executable, probe], env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
                             capture_output=True, text=True, check=True).stdout
        assert got == "True False\n", got
        # Unlike abandon, prune keeps a worktree whose session still runs even
        # when that session is the one asking: it may resume the agent.
        if (parent := os.getppid()) > 1:
            pstart = Path(f"/proc/{parent}/stat").read_text().rpartition(")")[2].split()[19]
            sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {parent} start {pstart})", str(rv))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                commands.prune([])
            assert "/review: kept, the claude session that spawned its agent still runs" in out.getvalue(), out.getvalue()
            # ...unless its agent stopped: that session ended for it, while
            # nothing relocked it since the stop (a resume does).
            held = f"claude agent review (pid {parent} start {pstart})"
            transcript = Path(tempfile.mkdtemp()) / "agent-review.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                why = commands.prune_one(rv.resolve(), named=True,
                                         stopped=commands.Stop(parent, f"{held} 1.2", transcript, None))
                assert why == "the claude session that spawned its agent still runs", why
                # A lock naming no session tells no relock apart: kept.
                sh(co, "worktree", "unlock", str(rv))
                sessionless = f"claude agent review {checkout.SESSIONLESS}"
                sh(co, "worktree", "lock", "--reason", sessionless, str(rv))
                why = commands.prune_one(rv.resolve(), named=True,
                                         stopped=commands.Stop(parent, sessionless, transcript, None))
                assert why is not None and why.startswith("its create found no claude session to outlive"), why
                sh(co, "worktree", "unlock", str(rv))
                sh(co, "worktree", "lock", "--reason", held, str(rv))
                exclude = Path(sh(rv, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
                exclude.parent.mkdir(exist_ok=True)
                kept_out = exclude.read_text() if exclude.exists() else ""
                exclude.write_text(kept_out + "target/\n")
                (rv / "target").mkdir()
                (rv / "target" / "build").write_text("x\n")
                why = commands.prune_one(rv.resolve(), named=True, stopped=commands.Stop(parent, held, transcript, None))
                assert why is not None and why.startswith("it has uncommitted or untracked changes"), why
                (rv / "target" / "build").unlink()
                (rv / "target").rmdir()
                exclude.write_text(kept_out)
                # A turn recorded since the stop, or a prompt awaiting one:
                # it runs again.  Its tool's result, or an error recorded in
                # its place, is neither.
                turn = '{"type":"assistant","message":{"model":"m","content":[]}}\n'
                result = '{"type":"user","message":{"content":[{"type":"tool_result"}]}}\n'
                error = '{"type":"assistant","message":{"model":"<synthetic>","content":[]}}\n'
                transcript.write_text(turn + result + "[]\n" + error)
                assert commands.transcript_turns(transcript) == (1, False)
                why = commands.prune_one(rv.resolve(), named=True, stopped=commands.Stop(parent, held, transcript, 0))
                assert why == "its agent ran again since it stopped", why
                with transcript.open("a") as f:
                    f.write('{"type":"user","message":{"content":"go on"}}\n')
                assert commands.transcript_turns(transcript) == (1, True)
                why = commands.prune_one(rv.resolve(), named=True, stopped=commands.Stop(parent, held, transcript, 1))
                assert why == "its agent ran again since it stopped", why
                transcript.write_text(turn + result)
                assert commands.prune_one(rv.resolve(), named=True, stopped=commands.Stop(parent, held, transcript, 1)) is None
            assert not rv.exists() and Path.cwd().resolve() == co.resolve()
            sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/main")
        # Where an agent's transcript says it started, and is now.
        log = Path(tempfile.mkdtemp()) / "agent-a1.jsonl"
        log.write_bytes(b'[1]\n\xff\xfe\n{"type":"user","cwd":"/a"}\nnot json\n{"type":"x"}\n{"cwd":"/b"}\n')
        assert commands.transcript_cwd(log) == Path("/a") and commands.transcript_cwd(log, last=True) == Path("/b")
        assert commands.transcript_cwd(log.with_name("none.jsonl")) is None
        # Claude Code's lock names the spawning session's process and its start
        # time: refused while that lives, lifted once it is gone (or its pid
        # reused by a process started at another time).
        me = os.getpid()
        start = Path(f"/proc/{me}/stat").read_text().rpartition(")")[2].split()[19]
        sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {me} start {start})", str(rv))
        commands.enter_sub("rev")
        err = dies(commands.abandon)
        assert "the claude session that spawned its agent still runs" in err, err
        sh(co, "worktree", "unlock", str(rv))
        # ...unless that session is the one running this: an ancestor process.
        if (parent := os.getppid()) > 1:
            pstart = Path(f"/proc/{parent}/stat").read_text().rpartition(")")[2].split()[19]
            sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {parent} start {pstart})", str(rv))
            assert not checkout.lock_live(f"claude agent review (pid {parent} start {pstart})")
            sh(co, "worktree", "unlock", str(rv))
        sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {me} start 1)", str(rv))
        # As the CLI runs it: under the worktree's deploy lock.
        commands.enter_sub("rev")
        held = checkout.lock_checkout()
        assert held is not None
        with contextlib.redirect_stdout(io.StringIO()):
            commands.abandon()
        held.close()
        assert not rv.exists() and Path.cwd().resolve() == co.resolve()
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/main")
        exited = subprocess.Popen(["true"])
        exited.wait()
        dead = exited.pid
        sh(co, "worktree", "lock", "--reason", f"claude agent review (pid {dead})", str(rv))
        commands.enter_sub("rev")
        with contextlib.redirect_stdout(io.StringIO()):
            commands.abandon()
        assert not rv.exists() and Path.cwd().resolve() == co.resolve()
        # The create hook's lock naming no session: prune keeps it, abandon
        # does not.
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), "origin/main")
        sh(co, "worktree", "lock", "--reason", f"claude agent review {checkout.SESSIONLESS}", str(rv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.prune(["rev"])
        assert "/review: kept, its create found no claude session" in out.getvalue(), out.getvalue()
        commands.enter_sub("rev")
        with contextlib.redirect_stdout(io.StringIO()):
            commands.abandon()
        assert not rv.exists() and Path.cwd().resolve() == co.resolve()

        # A subagent's pushed branch, deleted with its worktree, stays in
        # on_origin's memory (PAST_REFS): a review checkout at its tip still
        # holds nothing of its own.
        sub_wb = f"{wb}-review"
        sh(co, "worktree", "add", "--quiet", "-b", sub_wb, str(rv), "origin/main")
        put(rv, "s.txt", "sub\n", "sub work")
        sh(rv, "push", "--quiet", "origin", sub_wb)
        sh(co, "fetch", "--quiet")
        tip = proc.rev(f"refs/remotes/origin/{sub_wb}")
        commands.enter_sub("rev")
        with contextlib.redirect_stdout(io.StringIO()):
            commands.abandon()
        assert not rv.exists() and proc.rev(f"refs/remotes/origin/{sub_wb}") is None
        assert proc.rev(f"{checkout.PAST_REFS}/{sub_wb}") == tip
        sh(seed, "fetch", "--quiet")
        sh(seed, "switch", "--quiet", "--detach", "origin/main")
        put(seed, "a.txt", "ONE\nTWO-upstream\nthree\nfour\nfive\n", "upstream 4")
        sh(seed, "push", "--quiet", "origin", "HEAD:main")
        sh(co, "fetch", "--quiet")
        sh(co, "worktree", "add", "--quiet", "--detach", str(rv), str(tip))
        assert (got := checkout.update_checkout(rv)) == left, got
        commands.enter_sub("rev")
        with contextlib.redirect_stdout(io.StringIO()):
            commands.abandon()
        assert not rv.exists()
    finally:
        os.chdir(prev)
    print("git cores: ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-host"]:
        sys.exit(fake_host(sys.argv[2:]))
    if sys.argv[1:2] == ["--fake-guard"]:
        sys.exit(fake_guard(sys.argv[2], sys.argv[3]))
    config.set_cfg(config.Config())
    with tempfile.TemporaryDirectory() as d:
        os.environ["FAKE_HOST_STATE"] = str(Path(d) / "host.json")
        main()
    lineage_cases()
    target_cases()
    with tempfile.TemporaryDirectory() as d:
        guard_cases(Path(d))
    with tempfile.TemporaryDirectory() as d:
        reach_cases(Path(d))
    with tempfile.TemporaryDirectory() as d:
        git_cases(Path(d))
