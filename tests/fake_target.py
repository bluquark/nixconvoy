"""The fake target switch_test.py's switch cases drive: every ssh call of
the switch step lands here (--fake-target), playing the confirmed-switch
helper, systemd-run and nix-env on the JSON state file FAKE_TARGET_STATE
names; --fake-guard plays switch_guard_cmd's contract.  A script of its own,
importing nothing of the suite: the switch cases run it a hundred times."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

NEW = "/nix/store/new-system"
KIT = "/nix/store/new-kit"


def load(path: Path) -> dict[str, object]:
    state = json.loads(path.read_text())
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
    helper = f"{KIT}/confirmed-switch"
    match argv:
        case ["sh", "-c", script] if f"--add-root /var/lib/nixconvoy/kits/new-system --indirect -r {KIT}" in script:
            state["rooted"] = True
        case ["sh", "-c", script] if script.startswith("rm -f /var/lib/nixconvoy/kits/new-system;"):
            state["unrooted"] = True
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
                path.write_text(json.dumps(state))
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
    path.write_text(json.dumps(state))
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
        path.write_text(json.dumps(state))
    print("WROTE" if rec else "SKIPPED", flush=True)
    sys.stdin.read()
    state = load(path)
    if toplevel not in (state["running"], state["profile"]):
        state.pop("provenance", None)
        path.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-target"]:
        sys.exit(fake_target(sys.argv[2:]))
    if sys.argv[1:2] == ["--fake-guard"]:
        sys.exit(fake_guard(sys.argv[2], sys.argv[3]))
    sys.exit(2)
