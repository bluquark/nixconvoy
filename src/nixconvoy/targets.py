"""The targets: inventory, probes, builds, closure copies, and the dry
activation gate over a target's protected units."""

from __future__ import annotations

import http.client
import ipaddress
import re
import shlex
import signal
import socket
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from . import proc
from .config import builder, cfg
from .memo import Protection
from .proc import die, load_json, ok, run, stream

# Seconds one probe of a target may take, so a stalled ssh session holds
# neither a deploy's start nor verification past the deadline.
PROBE_TIMEOUT = 20
# Where a target keeps the provenance of the system it runs (switch.lineage_doc),
# claimed under the switch lock before activation, read by every probe.
# Persistent, so a reboot keeps it beside the system it describes.
LINEAGE_FILE: str = "/var/lib/nixconvoy/lineage.json"
CURRENT_SYSTEM: str = "/run/current-system"
SYSTEM_PROFILE: str = "/nix/var/nix/profiles/system"
SWITCH_LOCK: str = "/tmp/nixconvoy-switch.lock"  # noqa: S108 - every deploy shares it
LINEAGE_WRITER: str = "sudo -n"
# Restricted settings for the build phase alone, honoured only from a
# trusted user (elsewhere Nix warns and ignores them); every other nix
# invocation keeps the daemon defaults.  always-allow-substitutes false:
# Determinate's nix.conf sets it true, overriding the allowSubstitutes =
# false NixOS puts on the toplevel/etc/system-units/activate derivations,
# so every level of that chain would probe every substituter.
BUILD_OPTS = ("--option", "always-allow-substitutes", "false")
# Added by sandbox_opts when only trivial builders are left: the toplevel
# chain's derivations have the whole system as input closure, which the
# sandbox bind-mounts first, ~0.65 s per derivation.  A real package build
# must keep the sandbox: unsandboxed builds share the building machine's
# /tmp across build users.
UNSANDBOXED = ("--option", "sandbox", "false")
ALLOW_FLAG = "--allow"
# Seconds a dry-activate may take: it only computes the unit diff, so a
# longer wait means a stalled connection, not work.
DRY_ACTIVATE_TIMEOUT = 120


def flake_target_names() -> list[str]:
    """The deployable targets: <hosts_dir>/<name>/ maps 1:1 to nixosConfigurations."""
    d = cfg().hosts_path
    try:
        names = sorted(e.name for e in Path(d).iterdir() if e.is_dir())
    except OSError as error:
        die(f"cannot read the target inventory under {d}/: {error}")
    if not names:
        die(f"no targets found under {d}/")
    return names


def local_only(name: str) -> bool:
    return cfg().target(name).local_only


def remote_only(name: str) -> bool:
    """Deployable (and addressed) only from itself, and this is not it."""
    return local_only(name) and name != proc.this_machine()


def expand_targets(names: list[str], all_targets: list[str]) -> list[str]:
    """The targets `names` designate, in order and once each: a target by name,
    or `@tag` for every target nixconvoy.toml tags so (a remote-only one is
    dropped from a tag, as from the default of every target, since it cannot
    be addressed from here; named outright it is reported by the caller)."""
    found: list[str] = []
    for n in names:
        if n.startswith("@"):
            tagged = [h for h in cfg().tagged(n[1:]) if not remote_only(h)]
            if not tagged:
                die(f"no target reachable from here carries the tag {n[1:]} (nixconvoy.toml: "
                    f"targets.<name>.tags)")
            phantom = [h for h in tagged if h not in all_targets]
            if phantom:
                die(f"nixconvoy.toml tags {', '.join(phantom)} as {n[1:]}, but the inventory "
                    f"has no such target (it has: {', '.join(all_targets)})")
            found.extend(tagged)
        else:
            found.append(n)
    unknown = [h for h in found if h not in all_targets]
    if unknown:
        die(f"unknown target(s) {', '.join(unknown)}; the inventory has: {', '.join(all_targets)}")
    return list(dict.fromkeys(found))


def exec_on(names: list[str], argv: list[str]) -> None:
    """`argv` on each target `names` designate (every target addressable from
    here when none), concurrently, the output of each held and printed
    under its name once it finishes; dies naming the targets where it failed."""
    all_targets = flake_target_names()
    targets = expand_targets(names, all_targets) or [h for h in all_targets if not remote_only(h)]
    for h in targets:
        if remote_only(h):
            die(f"{h} is reachable only from itself (nixconvoy.toml: local_only)")
    def on(h: str) -> int:
        held = f"[{h}] " if len(targets) > 1 else ""
        try:
            return stream(target_cmd(h, argv), held_as=held or None)
        except FileNotFoundError:  # this machine itself, no shell to say so
            print(f"{held}{argv[0]}: command not found")
            return 127

    with ThreadPoolExecutor(len(targets)) as pool:
        codes = {h: pool.submit(on, h) for h in targets}
    failed = [f"{h} (exit {f.result()})" for h, f in codes.items() if f.result() != 0]
    if failed:
        die(f"failed on {', '.join(failed)}")


def ssh_address(name: str) -> str:
    return cfg().ssh_address(name)


def control_opts() -> list[str]:
    # One master connection per target for the several ssh sessions of a
    # deploy, kept for back-to-back deploys too.
    return ["-o", "ControlMaster=auto",
            "-o", f"ControlPath={Path.home()}/.ssh/nixconvoy-%C",
            "-o", "ControlPersist=60"]


def target_cmd(target: str, argv: list[str], sudo: bool = False,
               fresh: bool = False) -> list[str]:
    """`argv` on `target`: directly when it is this machine, else over ssh -
    the deploy's master connection, or with `fresh` a new connection of its
    own, proving sshd admits one."""
    pre = ["sudo"] if sudo else []
    if target == proc.this_machine():
        return [*pre, *argv]
    opts = ["-o", "ControlPath=none"] if fresh else control_opts()
    # The remote shell re-parses ssh's command line, hence the quoting.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", *opts,
            ssh_address(target), *pre, *map(shlex.quote, argv)]


class Probe(NamedTuple):
    running: str | None
    # LINEAGE_FILE as read ("" when absent).
    lineage: str
    # switch.lineage_digest of it, for the guard's compare.
    digest: str


def lineage_digest(text: str) -> str:
    """What the switch guard compares LINEAGE_FILE against (sha256sum of
    its bytes; an absent file reads as empty)."""
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(text.encode()).hexdigest()


def parse_probe(stdout: str) -> Probe:
    """The remote probe's output: the running system, then the lineage file
    verbatim."""
    cur, _, text = stdout.partition("\n")
    return Probe(cur.strip() or None, text, lineage_digest(text))


def probe(name: str) -> Probe:
    """The target's running system and its lineage, in one round trip;
    running None when it cannot be read."""
    if name == proc.this_machine():
        try:
            cur = str(Path(CURRENT_SYSTEM).readlink())
        except OSError:
            return Probe(None, "", "")
        try:
            text = Path(LINEAGE_FILE).read_bytes().decode()
        except (OSError, UnicodeDecodeError):
            text = ""
        return Probe(cur, text, lineage_digest(text))
    if local_only(name):
        return Probe(None, "", "")
    script = f"readlink {CURRENT_SYSTEM} && {{ cat {LINEAGE_FILE} 2>/dev/null || true; }}"
    p = run(target_cmd(name, ["sh", "-c", script]), check=False, timeout=PROBE_TIMEOUT)
    if p.returncode != 0:
        return Probe(None, "", "")
    return parse_probe(p.stdout)


def running_system(name: str, cleanup: bool = False,
                   timeout: float = PROBE_TIMEOUT, fresh: bool = False) -> str | None:
    """The target's /run/current-system (`fresh`: over a new ssh connection),
    or None if it cannot be read."""
    if name == proc.this_machine():
        try:
            return str(Path(CURRENT_SYSTEM).readlink())
        except OSError:
            return None
    if local_only(name):
        return None
    p = run(target_cmd(name, ["readlink", CURRENT_SYSTEM], fresh=fresh),
            check=False, timeout=timeout, cleanup=cleanup)
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def sandbox_opts(dry_run: list[str], drvs: list[str]) -> tuple[str, ...]:
    """The build's sandbox setting (see UNSANDBOXED): off when every
    derivation the dry-run (`dry_run`: the nix build command prefix, local
    or over ssh) leaves to build is preferLocalBuild - the marker nixpkgs
    puts on runCommandLocal/writeText/substituteAll outputs, the NixOS
    toplevel chain and fixed-output fetchers, and on no real package build.
    Anything else, including output that cannot be read, keeps the sandbox."""
    p = run([*dry_run, "--dry-run", *(f"{d}^out" for d in drvs)],
            check=False, merged=True)
    if p.returncode != 0:
        return ()
    todo = re.findall(r"^\s+(/nix/store/\S+\.drv)$", p.stdout, re.MULTILINE)
    if not todo:
        return UNSANDBOXED
    # The .drvs were evaluated here, so the classification is always local.
    p = run(["nix", "derivation", "show", *todo], check=False)
    if p.returncode != 0:
        return ()
    try:
        shown = load_json(p.stdout)
        table = shown.get("derivations", shown) if isinstance(shown, dict) else None
        if not isinstance(table, dict):
            return ()
        for drv in table.values():
            if not isinstance(drv, dict):
                return ()
            attrs = {**drv.get("env", {}), **(drv.get("structuredAttrs") or {})}
            if attrs.get("preferLocalBuild") not in ("1", True):
                return ()
    except (ValueError, TypeError):
        return ()
    return UNSANDBOXED


def build_on(builder: str, drvs: list[str], what: str,
             opts: tuple[str, ...], capture: bool) -> str | None:
    """Realise `drvs` on `builder`.  A .drv closure is self-authenticating,
    so the copy needs no signing or trusted user there."""
    tgt = ssh_address(builder)
    sshopts = " ".join(["-o", "BatchMode=yes", *control_opts()])
    p = run(["nix", "copy", "--derivation", "--to", f"ssh://{tgt}", *drvs],
            env={"NIX_SSHOPTS": sshopts}, check=False, capture=capture, merged=True)
    if p.returncode != 0:
        return f"copying derivations to {builder} failed" + (f":\n{p.stdout}" if capture else "")
    build = ["ssh", "-o", "BatchMode=yes", *control_opts(), tgt,
             "nix", "build", "--no-link", *BUILD_OPTS, *opts,
             "--option", "substituters", f"'{cfg().build_substituters(builder)}'"]
    p = run([*build, *sandbox_opts(build, drvs), *(f"{d}^out" for d in drvs)],
            check=False, capture=capture, merged=True)
    if p.returncode != 0:
        return f"{what} failed on {builder}" + (f":\n{p.stdout}" if capture else "")
    return None


def build_drvs(drvs: list[str], what: str, opts: tuple[str, ...] = (),
               capture: bool = False) -> str | None:
    """Realise `drvs` on the builder when set, else here.  The failure
    text (naming `what`, with the build output when `capture` kept it off
    the terminal) or None; the caller dies, since this also runs on the
    precheck thread."""
    b = builder()
    if b:
        return build_on(b, drvs, what, opts, capture)
    build = ["nix", "build", "--no-link", *BUILD_OPTS, *opts,
             "--option", "substituters", cfg().build_substituters(proc.this_machine())]
    p = run([*build, *sandbox_opts(build, drvs), *(f"{d}^out" for d in drvs)],
            check=False, capture=capture, merged=True)
    if p.returncode != 0:
        return f"{what} failed" + (f":\n{p.stdout}" if capture else "")
    return None


def off_lan(tgt: str) -> bool:
    """Whether `tgt` (user@name) is reached over the tailnet rather than
    the LAN: its name resolves into the CGNAT range tailscale hands out."""
    name = tgt.split("@", 1)[-1]
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)}
    except socket.gaierror:
        return False
    return any(ipaddress.ip_address(a) in ipaddress.ip_network("100.64.0.0/10")
               for a in addrs)


def copy_closure(target: str, toplevel: str, quiet: bool = False) -> None:
    """Copy `toplevel`'s closure to `target`, outside the switch lock (a union
    rebuilt after it merely leaves a few extra paths there).  `quiet` keeps
    nix's progress output for the failure message."""
    if target == proc.this_machine():
        return
    tgt = ssh_address(target)
    print(f"copying closure to {target}...")
    # Off-LAN targets (a VPS over the tailnet) substitute what they can
    # themselves, so only custom-built paths cross the slow uplink; on a
    # LAN a straight copy beats any download and per-path probe.
    subst = ["-s"] if off_lan(tgt) else []
    sshopts = " ".join(["-o", "BatchMode=yes", *control_opts()])
    b = builder()
    if b:
        # The outputs live on the builder, not here.
        p = run(["ssh", "-o", "BatchMode=yes", *control_opts(), ssh_address(b),
                 ("NIX_SSHOPTS='-o BatchMode=yes' "
                  f"nix copy {' '.join(subst)} --to ssh://{tgt} {toplevel}")],
                check=False, capture=quiet, merged=True)
    else:
        p = run(["nix", "copy", *subst, "--to", f"ssh://{tgt}", toplevel],
                env={"NIX_SSHOPTS": sshopts}, check=False, capture=quiet, merged=True)
    if p.returncode != 0:
        die(f"copying the closure to {target} failed; nothing was committed "
            f"or pushed - the changes are still staged"
            + (f"\n{p.stdout.strip()}" if p.stdout else ""))


def check_builder() -> None:
    """Fail before any nix work when a builder is set but unreachable,
    rather than on ssh's own timeout without the configured hint."""
    b = builder()
    if b and not ok(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                      *control_opts(), ssh_address(b), "true"]):
        hint = cfg().build_unreachable_hint
        die(f"builder {b} unreachable" + (f" ({hint})" if hint else "")
            + " - nothing was committed")


def rejected_generation(target: str, toplevel: str) -> str | None:
    """The ESP stub of `toplevel` on `target` when systemd-boot has spent its
    boot tries on it (the boot-assessment module: a `+0[-N]` counter), else
    None.  The ESP is root-only, hence sudo."""
    script = (
        "for s in /boot/EFI/Linux/nixos-generation-*+0*.efi; do "
        '[ -e "$s" ] || continue; n=${s##*/nixos-generation-}; g=${n%%-*}; '
        f'[ "$(readlink -f /nix/var/nix/profiles/system-$g-link 2>/dev/null)" = "{toplevel}" ] '
        '&& { echo "${s##*/}"; exit 0; }; done; exit 0'
    )
    p = run(target_cmd(target, ["sh", "-c", script], sudo=True), check=False, timeout=PROBE_TIMEOUT)
    if p.returncode != 0:
        die(f"could not scan {target}'s ESP for rejected generations (exit {p.returncode}); "
            f"nothing was switched or committed")
    return p.stdout.strip() or None


def internet_reachable(timeout: float = 8) -> bool:
    """Whether this machine reaches the internet."""
    try:
        with urllib.request.urlopen("https://www.google.com/generate_204",
                                    timeout=timeout) as r:
            return r.status == 204
    except (OSError, ValueError, http.client.HTTPException):
        return False


def unit_changes(dry_activate: str) -> dict[str, list[str]]:
    """The units dry-activate output says it would stop, restart or reload,
    keyed by verb (a pending start alone is no disruption)."""
    found: dict[str, list[str]] = {}
    for line in dry_activate.splitlines():
        m = re.match(r"would (stop|restart|reload) the following units: (.*)", line)
        if m:
            found.setdefault(m.group(1), []).extend(
                u.strip() for u in m.group(2).split(",") if u.strip())
    return found


def dry_activate(target: str, toplevel: str) -> tuple[dict[str, list[str]], str | None]:
    """Dry-activate `toplevel` on `target` (its closure already copied): the
    unit changes it reports (unit_changes), printed under the target's name,
    or why it could not be run.  Runs in the copy pool, so it only reports;
    the caller dies once the other copies have finished."""
    p = run(target_cmd(target, [f"{toplevel}/bin/switch-to-configuration", "dry-activate"],
                     sudo=True), check=False, merged=True, timeout=DRY_ACTIVATE_TIMEOUT)
    if proc.DYING:  # another thread's die() killed the child; it reports, not us
        return {}, None
    if p.returncode == -signal.SIGKILL:
        return {}, (f"dry-activating the new system on {target} did not finish within "
                    f"{DRY_ACTIVATE_TIMEOUT} s (a stalled ssh connection?); nothing was "
                    f"switched or committed")
    if p.returncode != 0:
        return {}, (f"dry-activating the new system on {target} failed (exit {p.returncode}); "
                    f"nothing was switched or committed"
                    + (f"\n{p.stdout.strip()}" if p.stdout else ""))
    changes = unit_changes(p.stdout)
    for verb, units in changes.items():
        print(f"  {target}: would {verb} {', '.join(units)}")
    if not changes:
        print(f"  {target}: would stop, restart or reload no unit")
    return changes, None


class Refusal(NamedTuple):
    """A switch the protected-units gate refused: what it would do to which
    groups' units, and the group names an --allow must name."""
    target: str
    hits: str
    names: tuple[str, ...]

    def __str__(self) -> str:
        return (f"switching {self.target} would {self.hits}, which its configuration protects "
                f"(nixconvoy.protected); nothing was switched or committed.")


def protected_units_gate(target: str, toplevel: str, protected: tuple[Protection, ...],
                         allowed: frozenset[str]) -> str | Refusal | None:
    """Dry-activate `toplevel` on `target`: why the deploy must stop (a
    Refusal: it would stop, restart or reload a unit of one of the target's
    `protected` groups whose name is not in `allowed`; or why the dry
    activation failed), else None."""
    changes, err = dry_activate(target, toplevel)
    if err:
        return err
    refused: list[Protection] = []
    hits: list[str] = []
    admitted: list[str] = []
    for p in protected:
        touched = [f"{verb} {u}" for verb, units in changes.items() for u in units if u in p.units]
        if not touched:
            continue
        if p.name in allowed:
            admitted.append(f"  {target}: {ALLOW_FLAG} {p.name} admits: {', '.join(touched)}")
            continue
        refused.append(p)
        hits.append(f"{', '.join(touched)} ({p.name}: {p.why})")
    if refused:
        return Refusal(target, "; ".join(hits), tuple(p.name for p in refused))
    for line in admitted:
        print(line)
    return None


def refusals_message(refusals: list[str | Refusal]) -> str:
    """The gate's verdicts as one message: each on its line, then the one
    command admitting every refused group (none for failures alone)."""
    names = sorted({n for r in refusals if isinstance(r, Refusal) for n in r.names})
    text = "\n".join(str(r) for r in refusals)
    if names:
        text += f"\nTo do it deliberately, re-run:\n  {allowing_command(names)}"
    return text


def allowing_command(names: list[str]) -> str:
    """The command admitting `names`: this invocation with `--allow <name>`
    for each after its subcommand - as an apply from the same directory
    for an abandon --apply-now, whose abandon is over."""
    argv = sys.argv[1:]
    flags = [a for n in names for a in (ALLOW_FLAG, n)]
    if argv[:1] == ["abandon"]:
        return shlex.join(["nixconvoy", "apply", *flags])
    return shlex.join(["nixconvoy", *argv[:1], *flags, *argv[1:]])
