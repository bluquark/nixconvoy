"""The targets: inventory, probes, builds, closure copies and dry activations."""

from __future__ import annotations

import functools
import ipaddress
import os
import re
import shlex
import socket
import sys
from pathlib import Path
from typing import NamedTuple

from . import proc, strings, term
from .config import cfg, shell_script
from .proc import Fail, die, load_json, run

# Seconds one probe of a target may take, so a stalled ssh session holds
# neither a deploy's start nor verification past the deadline.
PROBE_TIMEOUT = 20
PROBE_ERROR_LINES = 6
SSH_FAILED = 255  # ssh's exit status for a failure of its own; any other is the remote command's
# Where a target keeps the provenance of the system it runs (switch.provenance_doc),
# claimed under the switch lock before activation, read by every probe.
# Persistent, so a reboot keeps it beside the system it describes.
PROVENANCE_FILE: str = "/var/lib/nixconvoy/provenance.json"
# Where a target's switch kits (nix/kit.nix) are rooted with the store, one
# link per system named after its store path (kit_link): a collection in the
# rollback window cannot take the helper the timer runs.  A deploy prunes
# the links of systems the store no longer holds.
KIT_DIR: str = "/var/lib/nixconvoy/kits"
CURRENT_SYSTEM: str = "/run/current-system"
SYSTEM_PROFILE: str = "/nix/var/nix/profiles/system"
SWITCH_LOCK: str = "/tmp/nixconvoy-switch.lock"  # noqa: S108 - every deploy shares it
PROVENANCE_WRITER: str = "sudo -n"
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
# A peer silent for 15 s ends the session (a master connection, and so every
# session riding on it), however long its command may rightly run.
SSH_KEEPALIVE = ("-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3")
# Seconds `nix store info` or `nix config show` may take on the builder
# (well under 0.1 s): a longer wait means a stuck daemon or connection.
BUILDER_TIMEOUT = 10
# What the first ssh to a builder may add: it opens the deploy's master
# connection (ConnectTimeout 8, the key exchange, nix's own start).
CONNECT_GRACE = 20
# How the build runs where the daemon does not trust the user: as root,
# which every daemon trusts.  -k ignores a cached password, so the probe
# proves what the build gets; NIX_CONFIG (a unit's `builders =`, say) is
# carried through sudo's environment reset when the shell has it.
SUDO = ("sudo", "-n", "-k")
# nix's own arguments under SUDO: root's nix would otherwise open the
# store itself and build in the sudo session, outside the daemon unit's
# scheduling and limits; as a client it is trusted all the same, and the
# daemon drops its build when it goes.  Root reads no user nix.conf, so
# the command set is enabled by hand.
ROOT_NIX = ("--store", "daemon", "--extra-experimental-features", "nix-command")


def flake_target_names() -> list[str]:
    """The deployable targets: <targets_dir>/<name>/ maps 1:1 to
    nixosConfigurations, or without targets_dir the `[targets.<name>]`
    tables do."""
    names = cfg().inventory
    if not names:
        d = cfg().targets_path
        try:
            names = sorted(e.name for e in Path(d).iterdir() if e.is_dir())
        except OSError as error:
            die(strings.TARGETS_INVENTORY_UNREADABLE(dir=d, error=error))
        if not names:
            die(strings.TARGETS_NONE_FOUND(dir=d))
    return names


def expand_targets(names: list[str], all_targets: list[str]) -> list[str]:
    """The targets `names` designate, in order and once each: a target by name,
    or `@tag` for every target nixconvoy.toml tags so."""
    found: list[str] = []
    for n in names:
        if n.startswith("@"):
            tagged = cfg().tagged(n[1:])
            if not tagged:
                die(strings.TARGETS_NO_TAG(tag=n[1:]))
            phantom = [h for h in tagged if h not in all_targets]
            if phantom:
                die(strings.TARGETS_PHANTOM_TAG(phantom=", ".join(phantom), tag=n[1:],
                                                inventory=", ".join(all_targets)))
            found.extend(tagged)
        else:
            found.append(n)
    unknown = [h for h in found if h not in all_targets]
    if unknown:
        die(strings.TARGETS_UNKNOWN(unknown=", ".join(unknown), inventory=", ".join(all_targets)))
    return list(dict.fromkeys(found))


def ssh_address(name: str) -> str:
    return cfg().ssh_address(name)


def ssh_opts(fresh: bool = False) -> list[str]:
    """The options of every ssh to a target or the builder (NIX_SSHOPTS
    too): the deploy's master connection, one per target for its several
    sessions and kept for back-to-back deploys, or with `fresh` a new
    connection of its own."""
    conn = ["-o", "ControlPath=none"] if fresh else [
        "-o", "ControlMaster=auto", "-o", f"ControlPath={Path.home()}/.ssh/nixconvoy-%C",
        "-o", "ControlPersist=60"]
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", *SSH_KEEPALIVE, *conn]


def target_cmd(target: str, argv: list[str], sudo: bool = False,
               fresh: bool = False) -> list[str]:
    """`argv` on `target` over ssh (this machine too, over loopback: the
    switch's verification then proves sshd admits a login) - the deploy's
    master connection, or with `fresh` a new connection of its own."""
    pre = ["sudo"] if sudo else []
    # The remote shell re-parses ssh's command line, hence the quoting.
    return ["ssh", *ssh_opts(fresh), ssh_address(target), *pre, *map(shlex.quote, argv)]


class Probe(NamedTuple):
    running: str | None
    # PROVENANCE_FILE as read ("" when absent).
    provenance: str
    # switch.provenance_digest of it, for the guard's compare.
    digest: str
    # Why `running` is None: what ssh or the target said.
    error: str = ""


def kit_link(toplevel: str) -> str:
    return f"{KIT_DIR}/{Path(toplevel).name}"


def kit_root_script(toplevel: str, kit: str) -> str:
    """Root `kit` for `toplevel` (kit_link) as root, through nix-store's
    indirect root, and drop the links of systems the store no longer
    holds; `kit` "" drops `toplevel`'s link instead (switched plainly now)."""
    prune = (f'for l in {KIT_DIR}/*; do [ -e "$l" ] && [ -e "/nix/store/${{l##*/}}" ] || rm -f "$l"; done')
    if not kit:
        return f"rm -f {kit_link(toplevel)}; {prune}"
    return (f"mkdir -p {KIT_DIR} && nix-store --add-root {kit_link(toplevel)} --indirect -r {kit} >/dev/null && "
            f"{prune}")


def provenance_digest(text: str) -> str:
    """What the switch guard compares PROVENANCE_FILE against (sha256sum of
    its bytes; an absent file reads as empty)."""
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(text.encode()).hexdigest()


def parse_probe(stdout: str) -> Probe:
    """The remote probe's output: the running system, then the provenance
    file verbatim."""
    cur, _, text = stdout.partition("\n")
    return Probe(cur.strip() or None, text, provenance_digest(text))


def probe_script() -> str:
    """The shell of a probe, printing what parse_probe reads."""
    return (f"readlink {CURRENT_SYSTEM} && "
            f"{{ cat {PROVENANCE_FILE} 2>/dev/null || true; }}")


def probe(name: str) -> Probe:
    """The target's running system and its provenance, in one round trip;
    running None when it cannot be read, error saying why."""
    p = run(target_cmd(name, ["sh", "-c", probe_script()]), check=False, timeout=PROBE_TIMEOUT)
    if p.returncode != 0:
        # The cause is ssh's last lines; a login banner comes before them.
        said = "\n".join((p.stderr or "").strip().splitlines()[-PROBE_ERROR_LINES:])
        if p.timed_out:
            lead = strings.TARGETS_PROBE_TIMED_OUT(secs=PROBE_TIMEOUT)
        elif p.returncode != SSH_FAILED:
            lead = strings.TARGETS_PROBE_UNREAD(path=CURRENT_SYSTEM, code=p.returncode)
        else:
            lead = "" if said else strings.TARGETS_PROBE_SILENT(code=p.returncode)
        return Probe(None, "", "", error="\n".join(filter(None, [lead, said])))
    return parse_probe(p.stdout)


def running_system(name: str, timeout: float = PROBE_TIMEOUT, fresh: bool = False) -> str | None:
    """The target's /run/current-system (`fresh`: over a new ssh connection),
    or None if it cannot be read."""
    return running_system_read(name, timeout, fresh)[0]


def running_system_read(name: str, timeout: float = PROBE_TIMEOUT, fresh: bool = False) -> tuple[str | None, bool]:
    """running_system, and whether the read timed out rather than failed."""
    p = run(target_cmd(name, ["readlink", CURRENT_SYSTEM], fresh=fresh),
            check=False, timeout=timeout)
    if p.returncode != 0:
        return None, p.timed_out
    return p.stdout.strip() or None, False


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


def nix_json(prefix: tuple[str, ...], via: tuple[str, ...], *verbs: tuple[str, ...]) -> dict[str, object] | str:
    """What `nix <verbs[0]> --json` reports through `via` (the ssh prefix
    reaching the builder, empty here) with `prefix` (SUDO or nothing)
    before nix, or the failure text.  A nix without the verb (before 2.19
    or Lix, whose names differ) is asked by each later spelling in turn."""
    t = BUILDER_TIMEOUT + (CONNECT_GRACE if via else 0)
    err = ""
    for verb in verbs:
        p = run([*via, *prefix, "nix", *(ROOT_NIX if prefix else ROOT_NIX[2:]), *verb, "--json"],
                check=False, timeout=t)
        if p.timed_out:
            return strings.TARGETS_NIX_TIMEOUT(cmd=" ".join(verb), secs=t)
        if p.returncode == 0:
            try:
                info = load_json(p.stdout)
            except ValueError:
                info = None
            if not isinstance(info, dict):
                return strings.TARGETS_NIX_NO_JSON(cmd=" ".join(verb), output=p.stdout.strip())
            return info
        err = p.stderr.strip()
        if "not a recognised command" not in err:
            break
    return strings.TARGETS_NIX_FAILED(cmd=" ".join(verb), error=err)


def store_info(prefix: tuple[str, ...], via: tuple[str, ...]) -> dict[str, object] | str:
    """`nix store info` (`nix store ping` before 2.19)."""
    return nix_json(prefix, via, ("store", "info"), ("store", "ping"))


def platforms(via: tuple[str, ...]) -> set[str] | str:
    """The systems the nix through `via` builds: its `system` and
    `extra-platforms` settings (`nix config show`; `nix show-config`
    before 2.20), or the failure text."""
    info = nix_json((), via, ("config", "show"), ("show-config",))
    if isinstance(info, str):
        return info
    found: set[str] = set()
    for key in ("system", "extra-platforms"):
        setting = info.get(key)
        v = setting.get("value") if isinstance(setting, dict) else None
        if isinstance(v, str):
            found.update(v.split())
        elif isinstance(v, list):
            found.update(x for x in v if isinstance(x, str))
    return found


def trusts(info: dict[str, object]) -> bool:
    """Whether the store trusts the user: its `trusted` (a bool from
    Determinate Nix, 0/1 from upstream; absent from a store that cannot say,
    which nothing restricts)."""
    t = info.get("trusted")
    return t is None or bool(t)


@functools.cache
def builder_prefix(via: tuple[str, ...]) -> list[str] | str:
    """What the build phase's `nix` runs under through `via`: nothing when
    the daemon trusts the user (store_info), BUILD_OPTS being honoured from
    a trusted user alone, else SUDO, proven by the same probe as root.  The
    failure text when the user is neither (or the probe fails: a builder
    unreachable, its nix unusable); once per builder for the run."""
    info = store_info((), via)
    if isinstance(info, str):
        return info
    if trusts(info):
        return []
    pre = (*SUDO, "--preserve-env=NIX_CONFIG") if not via and os.environ.get("NIX_CONFIG") else SUDO
    rooted = store_info(pre, via)
    if isinstance(rooted, str):
        return strings.TARGETS_UNTRUSTED_ROOT_FAILED(sudo=shlex.join(pre), error=rooted)
    if not trusts(rooted):
        return strings.TARGETS_UNTRUSTED_NOR_ROOT
    return list(pre)


def build_cmd(opts: tuple[str, ...], via: list[str]) -> list[str] | str:
    """The `nix build` prefix the .drvs follow, run through `via` (the ssh
    prefix reaching the builder, empty here) under builder_prefix, or
    the failure text."""
    pre = builder_prefix(tuple(via))
    if isinstance(pre, str):
        return pre
    return [*pre, "nix", *(ROOT_NIX if pre else ()), "build", "--no-link", *BUILD_OPTS, *opts]


def build_on(host: str, drvs: list[str], what: str,
             opts: tuple[str, ...], capture: bool) -> str | None:
    """Realise `drvs` in `host`'s store.  A .drv closure is self-authenticating,
    so the copy needs no signing or trusted user there."""
    tgt = ssh_address(host)
    p = run(["nix", "copy", "--derivation", "--to", f"ssh://{tgt}", *drvs],
            env={"NIX_SSHOPTS": " ".join(ssh_opts())}, check=False, capture=capture, merged=True)
    if p.returncode != 0:
        return strings.TARGETS_COPY_DRVS_FAILED(host=host, detail=strings.PROC_DETAIL(output=p.stdout) if capture else "")
    via = ["ssh", *ssh_opts(), tgt]
    cmd = build_cmd(opts, via)
    if isinstance(cmd, str):
        return strings.TARGETS_BUILD_FAILED_ON_BECAUSE(what=what, host=host, error=cmd)
    build = [*via, shlex.join(cmd)]
    p = run([*build, *sandbox_opts(build, drvs), *(f"{d}^out" for d in drvs)],
            check=False, capture=capture, merged=True)
    if p.returncode != 0:
        return strings.TARGETS_BUILD_FAILED_ON(what=what, host=host,
                                               detail=strings.PROC_DETAIL(output=p.stdout) if capture else "")
    return None


def build_drvs(drvs: list[str], what: str, opts: tuple[str, ...] = (),
               capture: bool = False) -> str | None:
    """Realise `drvs` in the builder's store when set, else here.  The failure
    text (naming `what`, with the build output when `capture` kept it off
    the terminal - as a terminal taken for the run's rows always does, the
    calling thread's step showing its latest line) or None; the caller
    dies, since this also runs on the tree gate's thread."""
    capture = capture or term.LIVE
    b = builder_host()
    if b:
        return build_on(b, drvs, what, opts, capture)
    build = build_cmd(opts, [])
    if isinstance(build, str):
        return strings.TARGETS_BUILD_FAILED_BECAUSE(what=what, error=build)
    p = run([*build, *sandbox_opts(build, drvs), *(f"{d}^out" for d in drvs)],
            check=False, capture=capture, merged=True)
    if p.returncode != 0:
        return strings.TARGETS_BUILD_FAILED(what=what, detail=strings.PROC_DETAIL(output=p.stdout) if capture else "")
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


def built_on(target: str) -> bool:
    """Whether `target` is the machine the closures are realised on, which
    is copied nothing."""
    return target == (builder_host() or proc.this_machine())


def copy_closure(target: str, paths: list[str], quiet: bool = False, fatal: bool = True) -> str | None:
    """Copy `paths`' closures (the toplevel, the switch kit) to `target`,
    outside the switch lock (a union rebuilt after it merely leaves a few
    extra paths there).  `quiet` (and a terminal taken for the run's rows)
    keeps nix's progress output for the failure message, which ends the
    deploy, or is returned with `fatal` off."""
    quiet = quiet or term.LIVE
    def failed(msg: str) -> str | None:
        if fatal:
            die(msg)
        return msg
    b = builder_host()
    sshopts = " ".join(ssh_opts())
    if built_on(target):
        return None
    tgt = ssh_address(target)
    with term.step(strings.TARGETS_COPYING_CLOSURE(target=target), part=True) as step:
        # Off-LAN targets (a VPS over the tailnet) substitute what they can
        # themselves, so only custom-built paths cross the slow uplink; on a
        # LAN a straight copy beats any download and per-path probe.
        subst = ["-s"] if off_lan(tgt) else []
        if b:
            # The outputs live on the builder, not here.
            there = shlex.quote(" ".join(["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", *SSH_KEEPALIVE]))
            p = run(["ssh", *ssh_opts(), ssh_address(b),
                     f"NIX_SSHOPTS={there} nix copy {' '.join(subst)} --to ssh://{tgt} {' '.join(paths)}"],
                    check=False, capture=quiet, merged=True)
        else:
            p = run(["nix", "copy", *subst, "--to", f"ssh://{tgt}", *paths],
                    env={"NIX_SSHOPTS": sshopts}, check=False, capture=quiet, merged=True)
        if p.returncode != 0:
            step.ok = False
            return failed(strings.TARGETS_COPY_CLOSURE_FAILED(
                target=target,
                detail=strings.PROC_DETAIL_LINES(output=p.stdout.strip()) if p.stdout else ""))
    return None


def builder_problem(host: str) -> str | None:
    """Why `host` ("" for this machine) cannot realise the closures, or
    None: unreachable, its nix not building targets_arch (platforms, asked
    first: the reason a Darwin control machine never builds, before any sudo),
    or its daemon neither trusting the user nor running nix as root
    (builder_prefix)."""
    via = ("ssh", *ssh_opts(), ssh_address(host)) if host else ()
    systems = platforms(via)
    if isinstance(systems, str):
        return systems
    arch = cfg().targets_arch
    if arch not in systems:
        return strings.TARGETS_BUILDER_ARCH(arch=arch, systems=", ".join(sorted(systems)) or "nothing")
    pre = builder_prefix(via)
    return pre if isinstance(pre, str) else None


@functools.cache
def builder_host() -> str:
    """The machine the closures are realised on, chosen once per run
    before any nix work: the first of nixconvoy.toml's `builders` without
    a builder_problem, each skipped one told; "" is this machine.  Dies
    when none is usable, rather than on ssh's own timeout or nix's
    ignored-setting warning deep in a build."""
    me = proc.this_machine()
    names = list(dict.fromkeys(b.format(local_hostname=me) for b in cfg().builders))
    for name in names:
        problem = builder_problem("" if name == me else name)
        if problem is None:
            return "" if name == me else name
        print(strings.TARGETS_BUILDER_SKIPPED(host=name, problem=problem), file=sys.stderr)
    return die(strings.TARGETS_NO_BUILDER(names=", ".join(names)))


def check_builder() -> None:
    """The builder chosen (builder_host), so a deploy with none usable
    ends before any nix work."""
    builder_host()


def pre_switch_check(target: str, statements: tuple[str, ...], toplevel: str, previous: str) -> str | None:
    """The pre-switch target statements on `target`, as root over ssh, one
    after the other (sh, under config.SHELL_PRELUDE, switch.check_env in the
    environment): the first failing one's report - its last stderr line,
    or that it exited so or timed out - else None.  Before the build, so
    the kit is not there yet: the statements run unparsed by root's shell."""
    from .switch import check_env  # noqa: PLC0415 - switch imports this module

    env = [f"{k}={v}" for k, v in check_env(target, toplevel, previous).items()]
    for statement in statements:
        first = statement.strip().splitlines()
        head = first[0] if first else ""
        try:
            p = run(target_cmd(target, ["env", *env, "sh", "-c", shell_script(statement)], sudo=True),
                    check=False, timeout=PROBE_TIMEOUT)
        except Fail as e:
            return strings.SWITCH_CHECK_INTERRUPTED(error=e)
        if p.timed_out:
            return strings.SWITCH_CHECK_TIMED_OUT(statement=head)
        if p.returncode != 0:
            lines = (p.stderr or "").strip().splitlines()
            return lines[-1] if lines else strings.SWITCH_CHECK_EXITED(statement=head, code=p.returncode)
    return None


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


def dry_activate(target: str, toplevel: str) -> str | None:
    """Dry-activate `toplevel` on `target` (its closure already copied),
    printing the unit changes it reports (unit_changes) under the target's
    name: why it could not be run, else None.  Runs in the copy pool, so it
    only reports; the caller dies once the other copies have finished."""
    p = run(target_cmd(target, [f"{toplevel}/bin/switch-to-configuration", "dry-activate"],
                     sudo=True), check=False, merged=True)
    if p.returncode != 0:
        return strings.TARGETS_DRY_ACTIVATE_FAILED(
            target=target, code=p.returncode,
            detail=strings.PROC_DETAIL_LINES(output=p.stdout.strip()) if p.stdout else "")
    changes = unit_changes(p.stdout)
    for verb, units in changes.items():
        print(strings.TARGETS_WOULD(target=target, verb=verb, units=", ".join(units)))
    if not changes:
        print(strings.TARGETS_WOULD_NOTHING(target=target))
    return None

