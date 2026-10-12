"""One host's switch: the lineage it claims, the on-host compare-and-swap
guard, and the deploy-switch core's protocol performed over ssh."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import hosts, proc
from .checkout import current_branch
from .guard import DEPLOYER_PROTOCOL, drive
from .hosts import PROBE_TIMEOUT, control_opts, lineage_digest
from .proc import out, rev, run, stream


def lineage_doc(*, union: str, main: str | None, branches: dict[str, str | None],
                own: str, head: str | None, base: str | None, snap: str,
                at: float, host: str, checkout: str, pid: int) -> dict[str, object]:
    """The lineage of a deploy's union, before its toplevel is added:
    `branches` the other work branches' unioned tips, `own` the checked-out
    branch the union was built on (`head`, plus `snap` when an increment is
    staged), `base` origin's tip of it."""
    pending = bool(snap) or head != base
    return {
        "version": 1, "union": union, "main": main,
        "branches": {**branches, own: snap or head},
        "pending": {"branch": own, "base": base, "head": snap or head} if pending else None,
        "at": round(at), "by": {"host": host, "checkout": checkout, "pid": pid},
    }


def lineage_record(union: str, branches: list[tuple[str, str]], snap: str) -> dict[str, object]:
    """lineage_doc for this deploy, from the refs it built its union on."""
    own = current_branch() or "main"  # detached: at origin/main (ensure_rebased)
    return lineage_doc(
        union=union, main=rev("refs/remotes/origin/main"), branches=dict(branches),
        own=own, head=rev("HEAD"), base=rev(f"refs/remotes/origin/{own}"), snap=snap,
        at=time.time(), host=proc.this_host(), checkout=str(Path.cwd()), pid=os.getpid())


def switch_guard_cmd(host: str, expected: str, toplevel: str, digest: str) -> list[str]:
    """The on-host lock of a switch: a process holding fd 9 that first
    re-verifies the host still runs `expected` with the lineage whose
    lineage_digest the probe read (else MOVED, so a deploy that probed in
    another's switch-to-verify window aborts instead of reverting it), prints
    LOCKED and holds the lock until its stdin closes.  The first line sent
    is the lineage of `toplevel`, claimed in LINEAGE_FILE at once (WROTE):
    from then on a probe that finds `toplevel` running reads it.  At the
    end, unless the host runs `toplevel` or its profile names it (a reboot
    takes it there), the previous file is put back."""
    f, cur_sys = hosts.LINEAGE_FILE, hosts.CURRENT_SYSTEM
    claim = (f"umask 022; mkdir -p {Path(f).parent} && "
             f"{{ cp -p {f} {f}.prev 2>/dev/null || rm -f {f}.prev; }} && "
             f"cat >{f}.tmp && mv -f {f}.tmp {f}")
    restore = f"if [ -e {f}.prev ]; then mv -f {f}.prev {f}; else rm -f {f}; fi"
    w = hosts.LINEAGE_WRITER
    script = (
        f"exec 9>{hosts.SWITCH_LOCK}; "
        "flock -w 900 9 || { echo TIMEOUT; exit 1; }; "
        f"cur=$(readlink {cur_sys}); "
        f'[ "$cur" = "{expected}" ] || {{ echo "MOVED $cur"; exit 1; }}; '
        f"lin=$(cat {f} 2>/dev/null | sha256sum); "
        f'[ "${{lin%% *}}" = "{digest}" ] || '
        '{ echo "MOVED $cur, its lineage rewritten by another deploy"; exit 1; }; '
        "echo LOCKED; IFS= read -r rec || exit 0; claimed=; "
        'if [ -z "$rec" ]; then echo SKIPPED; '
        f"elif printf '%s\\n' \"$rec\" | {w} sh -c {shlex.quote(claim)}; then echo WROTE; claimed=1; "
        "else echo UNWRITTEN; fi; "
        "read -r _ || true; "
        '[ -n "$claimed" ] || exit 0; '
        f'[ "$(readlink {cur_sys})" = "{toplevel}" ] || '
        f'[ "$(readlink -f {hosts.SYSTEM_PROFILE})" = "{toplevel}" ] || '
        f"{w} sh -c {shlex.quote(restore)}"
    )
    if host == proc.this_host():
        return ["bash", "-c", script]
    return ["ssh", "-o", "BatchMode=yes", *control_opts(), hosts.target(host), script]


def switch(host: str, expected: str, toplevel: str, deployer: str,
           quiet: bool = False, lineage: str = "", digest: str = "",
           needs_internet: bool = False) -> None:
    """Switch `host` to `toplevel` as deploy-switch decides.  `quiet`
    (several switches at once) prints the activation output afterwards,
    prefixed with the host.  Activation, confirm and rollback run in a
    transient unit on the host (as nixos-rebuild does), so they complete
    even if this ssh session dies.  `lineage` (one JSON line) is claimed on
    the host under the switch lock, `digest` the lineage_digest the probe
    read (switch_guard_cmd).  `needs_internet`: verification also requires
    this machine to reach the internet (the router)."""
    local = host == proc.this_host()
    held = f"[{host}] " if quiet else None
    helper = f"{toplevel}/confirmed-switch"
    guard: subprocess.Popen[str] | None = None

    def detached(argv: list[str], unit: str | None = None,
                 sink: list[str] | None = None) -> int:
        # A named unit refuses a second activation while one runs; the
        # helper's runs follow each other closely, so theirs are generated.
        return stream(hosts.host_cmd(host, [
            "systemd-run", "--quiet", "--collect", "--wait", "--pipe",
            "--service-type=exec", *([f"--unit={unit}"] if unit else []), *argv],
            sudo=True), held_as=held, sink=sink)

    def said(lines: list[str], pattern: str) -> str | None:
        # The system the helper names as its rollback's, for the report.
        found = [m.group(1) for ln in lines if (m := re.search(pattern, ln))]
        return found[-1] if found else None

    def secs(ms: object) -> float:
        return ms / 1000 if isinstance(ms, int) else float(PROBE_TIMEOUT)

    def perform(ask: dict[str, object]) -> dict[str, object]:
        nonlocal guard
        kind = ask.get("ask")
        if kind == "guard":
            guard = subprocess.Popen(switch_guard_cmd(host, expected, toplevel, digest),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert guard.stdin is not None and guard.stdout is not None
            line = guard.stdout.readline().strip()
            if line == "LOCKED":
                try:
                    guard.stdin.write(lineage + "\n")
                    guard.stdin.flush()
                    claimed = guard.stdout.readline().strip()
                except OSError:
                    claimed = ""
                if lineage and claimed != "WROTE":
                    print(f"note: could not claim {host}'s lineage in {hosts.LINEAGE_FILE}; until "
                          f"its next switch, other deploys treat its system as untracked",
                          file=sys.stderr)
                return {"guard": "locked"}
            if line.startswith("MOVED"):
                now_running = line.removeprefix("MOVED").strip() or "nothing readable"
                return {"guard": "moved", "running": now_running}
            return {"guard": "failed", "line": line}
        if kind == "probe_helper":
            return {"code": run(hosts.host_cmd(host, ["test", "-e", helper]), check=False,
                                timeout=PROBE_TIMEOUT).returncode}
        if kind == "set_profile":
            return {"code": stream(hosts.host_cmd(host, ["nix-env", "-p", hosts.SYSTEM_PROFILE,
                                                         "--set", toplevel], sudo=True), held_as=held)}
        if kind == "activate":
            action = str(ask.get("action"))
            return {"code": detached([f"{toplevel}/bin/switch-to-configuration", action],
                                     unit="nixconvoy-switch-to-configuration")}
        if kind == "deadline":
            return {"text": out(hosts.host_cmd(host, [helper, "deadline"]))}
        if kind == "arm":
            lines: list[str] = []
            rc = stream(hosts.host_cmd(host, [helper, "arm"], sudo=True), held_as=held, sink=lines)
            return {"code": rc, "target": said(lines, r"armed, rolling back to (\S+) in ")}
        if kind == "helper":
            args = ask.get("args")
            assert isinstance(args, list)
            if args != ["rollback"]:
                return {"code": detached([helper, *map(str, args)])}
            rb_lines: list[str] = []
            rc = detached([helper, "rollback"], sink=rb_lines)
            return {"code": rc, "target": said(rb_lines, r"rolled back to (\S+)$")}
        if kind == "now":
            return {"now": proc.monotonic_ms()}
        if kind == "running":
            return {"running": hosts.running_system(host, cleanup=True,
                                                    timeout=secs(ask.get("timeout_ms")))}
        if kind == "check":
            t = secs(ask.get("timeout_ms"))
            p = run(hosts.host_cmd(host, [helper, "check"], sudo=True), check=False, timeout=t,
                    cleanup=True)
            lines = (p.stderr or "").strip().splitlines()
            return {"check": p.returncode, "last": lines[-1] if lines else None}
        if kind == "internet":
            return {"flag": hosts.internet_reachable(secs(ask.get("timeout_ms")))}
        if kind == "sleep":
            time.sleep(secs(ask.get("ms")))
            return {"done": True}
        raise ValueError(f"deploy-switch asked {ask!r}")

    def effect(e: dict[str, object]) -> None:
        if e.get("armed") is True:
            proc.ARMED.add(host)
        elif e.get("armed") is False:
            proc.ARMED.discard(host)

    try:
        drive(deployer, {
            "protocol": DEPLOYER_PROTOCOL, "host": host, "toplevel": toplevel,
            "expected": expected,
            "place": "local" if local else hosts.target(host),
            "quiet": quiet, "router": needs_internet,
            "fail_verify": os.environ.get("NIXCONVOY_FAIL_VERIFY") == "1"},
            perform, "deploy-switch", f" while switching {host}", effect)
    finally:
        if guard is not None:
            if guard.stdin is not None:
                with contextlib.suppress(OSError):
                    guard.stdin.close()
            try:
                guard.wait(timeout=15)
            except subprocess.TimeoutExpired:
                guard.kill()


__all__ = ["lineage_digest", "lineage_doc", "lineage_record", "switch",
           "switch_guard_cmd"]
