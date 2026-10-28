"""One target's switch: the provenance it claims, the on-target compare-and-swap
guard, and the nixconvoy-switch core's protocol performed over ssh."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import proc, strings, targets
from .checkout import current_branch
from .config import cfg, main_ref, remote_ref
from .guard import drive
from .proc import Fail, out, rev, run, stream
from .targets import PROBE_TIMEOUT, provenance_digest, ssh_opts
from .wire import (
    Activate,
    Arm,
    Armed,
    Check,
    Checked,
    Clock,
    Code,
    Deadline,
    Disarmed,
    Done,
    Effect,
    Failed,
    Flag,
    Guard,
    Helper,
    Internet,
    Locked,
    Now,
    ProbeHelper,
    Running,
    Said,
    SetProfile,
    Sleep,
    SwitchParams,
    SwitchQuery,
    SwitchReply,
    System,
    TakeGuard,
    Text,
    encode,
)


def provenance_doc(*, union: str, main: str | None, branches: dict[str, str | None],
                own: str, head: str | None, base: str | None, snap: str, announce: str,
                at: float, machine: str, checkout: str, pid: int, key: str = "") -> dict[str, object]:
    """The provenance of a deploy's union, before its toplevel and record are
    added: `branches` the other work branches' unioned tips, `own` the
    checked-out branch the union was built on (`head`, or `snap`, the commit
    the branch gets once the staged increment runs), `base` origin's tip of
    it, `announce` the increment's announcement and `key` its ref's name
    (checkout.Announce: the drift core takes the increment as abandoned
    once that ref is cleared; a version-2 reader may ignore both)."""
    pending = bool(snap) or head != base
    doc: dict[str, object] = {
        "version": 2, "union": union, "main": main,
        "branches": {**branches, own: snap or head},
        "pending": {"branch": own, "base": base, "head": snap or head} if pending else None,
        "at": round(at), "by": {"machine": machine, "checkout": checkout, "pid": pid},
    }
    if pending and announce:
        p = doc["pending"]
        assert isinstance(p, dict)
        p["announce"] = announce
        p["key"] = key or own
    return doc


def provenance_record(union: str, branches: list[tuple[str, str]], snap: str,
                   announce: str = "", key: str = "") -> dict[str, object]:
    """provenance_doc for this deploy, from the refs it built its union on;
    `snap` the increment's work-branch commit (deployed_commit), or ""."""
    own = current_branch() or cfg().main_branch  # detached: at origin/main (ensure_rebased)
    return provenance_doc(
        union=union, main=rev(main_ref()), branches=dict(branches),
        own=own, head=rev("HEAD"), base=rev(remote_ref(own)), snap=snap,
        announce=announce, key=key, at=time.time(), machine=proc.this_machine(),
        checkout=str(Path.cwd()), pid=os.getpid())


class SwitchedMeanwhile(Exception):
    """The guard found `target` running `running`, not what was probed:
    another deploy switched it since.  Raised before anything is activated,
    for the deploy to probe again and redo its plan."""

    def __init__(self, target: str, running: str) -> None:
        super().__init__(strings.SWITCH_MOVED_MEANWHILE(target=target, running=running))
        self.target = target
        self.running = running


def switch_guard_cmd(target: str, expected: str, toplevel: str, digest: str) -> list[str]:
    """The on-target lock of a switch: a process holding fd 9 that first
    re-verifies the target still runs `expected` with the provenance whose
    provenance_digest the probe read (else MOVED, so a deploy that probed in
    another's switch-to-verify window aborts instead of reverting it), prints
    LOCKED and holds the lock until its stdin closes.  The first line sent
    is the provenance of `toplevel`, claimed in PROVENANCE_FILE at once (WROTE):
    from then on a probe that finds `toplevel` running reads it.  At the
    end, unless the target runs `toplevel` or its profile names it (a reboot
    takes it there), the previous file is put back."""
    f, cur_sys = targets.PROVENANCE_FILE, targets.CURRENT_SYSTEM
    claim = (f"umask 022; mkdir -p {Path(f).parent} && "
             f"{{ cp -p {f} {f}.prev 2>/dev/null || rm -f {f}.prev; }} && "
             f"cat >{f}.tmp && mv -f {f}.tmp {f}")
    restore = f"if [ -e {f}.prev ]; then mv -f {f}.prev {f}; else rm -f {f}; fi"
    w = targets.PROVENANCE_WRITER
    script = (
        f"exec 9>{targets.SWITCH_LOCK}; "
        "flock -w 900 9 || { echo TIMEOUT; exit 1; }; "
        f"cur=$(readlink {cur_sys}); "
        f'[ "$cur" = "{expected}" ] || {{ echo "MOVED $cur"; exit 1; }}; '
        f"lin=$(cat {f} 2>/dev/null | sha256sum); "
        f'[ "${{lin%% *}}" = "{digest}" ] || '
        f'{{ echo "MOVED $cur"{shlex.quote(strings.SWITCH_PROVENANCE_REWRITTEN)}; exit 1; }}; '
        "echo LOCKED; IFS= read -r rec || exit 0; claimed=; "
        'if [ -z "$rec" ]; then echo SKIPPED; '
        f"elif printf '%s\\n' \"$rec\" | {w} sh -c {shlex.quote(claim)}; then echo WROTE; claimed=1; "
        "else echo UNWRITTEN; fi; "
        "read -r _ || true; "
        '[ -n "$claimed" ] || exit 0; '
        f'[ "$(readlink {cur_sys})" = "{toplevel}" ] || '
        f'[ "$(readlink -f {targets.SYSTEM_PROFILE})" = "{toplevel}" ] || '
        f"{w} sh -c {shlex.quote(restore)}"
    )
    if target == proc.this_machine():
        return ["bash", "-c", script]
    return ["ssh", *ssh_opts(), targets.ssh_address(target), script]


def switch(target: str, expected: str, toplevel: str, kit: str, nixconvoy_switch: str,
           quiet: bool = False, provenance: str = "", digest: str = "",
           needs_internet: bool = False) -> None:
    """Switch `target` to `toplevel` as nixconvoy-switch decides, confirmed
    through `kit`'s helper (nix/kit.nix, already copied) or plainly when
    `kit` is "".  `quiet`
    (several switches at once) prints the activation output afterwards,
    prefixed with the target.  Activation, confirm and rollback run in a
    transient unit on the target (as nixos-rebuild does), so they complete
    even if this ssh session dies.  Each verification round reads the running
    system over a new ssh connection, not the deploy's master connection: a
    switch that stopped sshd admitting logins fails it.  `provenance` (one JSON line) is claimed on
    the target under the switch lock, `digest` the provenance_digest the probe
    read (switch_guard_cmd).  `needs_internet`: verification also requires
    this machine to reach the internet (the router)."""
    local = target == proc.this_machine()
    held = f"[{target}] " if quiet else None
    helper = f"{kit}/confirmed-switch"
    guard: subprocess.Popen[str] | None = None

    def detached(argv: list[str], unit: str | None = None,
                 sink: list[str] | None = None) -> int:
        # A named unit refuses a second activation while one runs; the
        # helper's runs follow each other closely, so theirs are generated.
        return stream(targets.target_cmd(target, [
            "systemd-run", "--quiet", "--collect", "--wait", "--pipe",
            "--service-type=exec", *([f"--unit={unit}"] if unit else []), *argv],
            sudo=True), held_as=held, sink=sink)

    def said(lines: list[str], pattern: str) -> str | None:
        # The system the helper names as its rollback's, for the report.
        found = [m.group(1) for ln in lines if (m := re.search(pattern, ln))]
        return found[-1] if found else None

    def secs(ms: int | None) -> float:
        return ms / 1000 if ms is not None else float(PROBE_TIMEOUT)

    def perform(ask: SwitchQuery) -> SwitchReply:
        nonlocal guard
        match ask:
            case TakeGuard():
                guard = subprocess.Popen(switch_guard_cmd(target, expected, toplevel, digest),
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                assert guard.stdin is not None and guard.stdout is not None
                line = guard.stdout.readline().strip()
                if line == "LOCKED":
                    try:
                        guard.stdin.write(provenance + "\n")
                        guard.stdin.flush()
                        claimed = guard.stdout.readline().strip()
                    except OSError:
                        claimed = ""
                    if provenance and claimed != "WROTE":
                        print(strings.SWITCH_UNCLAIMED(target=target, file=targets.PROVENANCE_FILE),
                              file=sys.stderr)
                    return Guard(Locked())
                if line.startswith("MOVED"):
                    raise SwitchedMeanwhile(target, line.removeprefix("MOVED").strip()
                                            or strings.SWITCH_NOTHING_READABLE)
                return Guard(Failed(line))
            case ProbeHelper():
                # The kit (copied with the toplevel) rooted for it before
                # the helper runs: a failure is the core's to end (Code 2).
                # Switched plainly, a kit an earlier policy rooted for this
                # toplevel is dropped, so no boot runs its checks.
                p = run(targets.target_cmd(target, ["sh", "-c", targets.kit_root_script(toplevel, kit)],
                                           sudo=True), check=False, timeout=PROBE_TIMEOUT, merged=True)
                if not kit:
                    if p.returncode != 0:
                        print(strings.SWITCH_STALE_KIT_DROP_FAILED(target=target, code=p.returncode),
                              file=sys.stderr)
                    return Code(1)
                if p.returncode != 0:
                    detail = strings.PROC_DETAIL(output=p.stdout.strip()) if p.stdout else ""
                    print(strings.SWITCH_KIT_ROOT_FAILED(kit=kit, target=target, code=p.returncode, detail=detail),
                          file=sys.stderr)
                    return Code(2)
                return Code(0)
            case SetProfile():
                return Code(stream(targets.target_cmd(target, ["nix-env", "-p", targets.SYSTEM_PROFILE,
                                                               "--set", toplevel], sudo=True), held_as=held))
            case Activate():
                return Code(detached([f"{toplevel}/bin/switch-to-configuration", "switch"],
                                     unit="nixconvoy-switch-to-configuration"))
            case Deadline():
                return Text(out(targets.target_cmd(target, [helper, "deadline"])))
            case Arm():
                lines: list[str] = []
                rc = stream(targets.target_cmd(target, [helper, "arm"], sudo=True), held_as=held, sink=lines)
                return Said(rc, said(lines, r"armed, rolling back to (\S+) in "))
            case Helper(args):
                if args != ["rollback"]:
                    return Code(detached([helper, *args]))
                rb_lines: list[str] = []
                rc = detached([helper, "rollback"], sink=rb_lines)
                return Said(rc, said(rb_lines, r"rolled back to (\S+)$"))
            case Clock():
                return Now(proc.monotonic_ms())
            # A verification probe killed by another target's die() is a failed
            # round, retried until the deadline.
            case Running(timeout_ms):
                try:
                    return System(targets.running_system(target, timeout=secs(timeout_ms), fresh=True))
                except Fail:
                    return System(None)
            case Check(timeout_ms):
                t = secs(timeout_ms)
                try:
                    p = run(targets.target_cmd(target, [helper, "check"], sudo=True), check=False, timeout=t)
                except Fail as e:
                    return Checked(1, strings.SWITCH_CHECK_INTERRUPTED(error=e))
                lines = (p.stderr or "").strip().splitlines()
                return Checked(p.returncode, lines[-1] if lines else None)
            case Internet(timeout_ms):
                return Flag(targets.internet_reachable(secs(timeout_ms)))
            case Sleep(ms):
                time.sleep(secs(ms))
                return Done()
            case _:
                raise ValueError(f"nixconvoy-switch asked {ask!r}")

    def effect(e: Effect) -> None:
        match e:
            case Armed():
                # Armed, it is confirmed or rolled back on its own merits
                # whatever fails elsewhere.
                proc.set_finishing(True)
                proc.ARMED.add(target)
            case Disarmed():
                proc.ARMED.discard(target)
            case _:
                pass

    was_finishing = proc.set_finishing(False)
    try:
        params = SwitchParams(target, toplevel, expected, "local" if local else targets.ssh_address(target), quiet,
                              needs_internet, os.environ.get("NIXCONVOY_FAIL_VERIFY") == "1")
        drive(nixconvoy_switch, encode(params), perform, SwitchQuery, type(None),
              "nixconvoy-switch", strings.SWITCH_DURING(target=target), effect)
    finally:
        proc.set_finishing(was_finishing)
        if guard is not None:
            if guard.stdin is not None:
                with contextlib.suppress(OSError):
                    guard.stdin.close()
            try:
                guard.wait(timeout=15)
            except subprocess.TimeoutExpired:
                guard.kill()


__all__ = ["provenance_digest", "provenance_doc", "provenance_record", "switch",
           "switch_guard_cmd"]
