"""`nixconvoy init`: nixconvoy.toml for a fresh install.  Every required
key is detected from the repository - the flake directory, the hosts
directory, the targets' system, this machine's work-branch prefix - or
starts from this machine (the ssh address template, as this user); the
optional sections are written as commented examples filled with what was
found (the flake's checks, the targets).  git is only read: nothing is
staged or committed."""

from __future__ import annotations

import getpass
import json
import os
import platform
import re
import subprocess
from pathlib import Path

from . import config, proc
from .config import CONFIG_FILE
from .proc import die, out, run

# A nix probe is a convenience: past this, the heuristics stand alone.
NIX_TIMEOUT = 60
SYSTEM_RE = re.compile(r'"((?:x86_64|aarch64|i686|riscv64|armv7l|powerpc64le)-(?:linux|darwin))"')
PLATFORM_LINE_RE = re.compile(r"hostPlatform|\bsystem\s*=")
MAX_DEPTH = 3  # how deep under the root the flake and hosts directories are looked for
DEFAULT_BRANCH_PREFIX = "nixconvoy/"


def main(flake_arg: str | None, start: Path) -> None:
    root = Path.cwd()
    if (root / CONFIG_FILE).exists():
        die(f"{CONFIG_FILE} is already there; move it aside to generate a new one")
    notes: list[str] = []
    flake = find_flake(root, start / flake_arg if flake_arg else None)
    nix = NixProbe(notes)
    ref = "." if flake == "." else f"./{flake}"
    hosts = nix.json(["nix", "eval", "--json", f"{ref}#nixosConfigurations", "--apply", "builtins.attrNames"],
                     "the nixosConfigurations' names")
    if hosts is None and not (nix.timed_out or nix.missing) and (alt := committed_ref(root, flake)) is not None:
        # The working tree may not evaluate (a submodule's directory still
        # empty): the committed tree, whose submodules nix fetches itself.
        hosts = nix.json(["nix", "eval", "--json", f"{alt}#nixosConfigurations", "--apply", "builtins.attrNames"],
                         "HEAD's nixosConfigurations' names")
        if hosts is not None:
            ref = alt
    host_names = sorted(h for h in hosts if isinstance(h, str)) if isinstance(hosts, list) else None
    hosts_dir = find_hosts_dir(root, flake, host_names)
    if hosts_dir is None:
        hosts_dir = os.path.normpath(f"{flake}/hosts")
        notes.append(f"no hosts directory found: {hosts_dir}/<name>/ per nixosConfiguration is where "
                     "the targets are enumerated")
    targets = sorted(p.name for p in (root / hosts_dir).iterdir() if p.is_dir()) \
        if (root / hosts_dir).is_dir() else []
    if host_names is not None:
        targets = [t for t in targets if t in host_names]
    system, system_how = detect_system(root, ref, hosts_dir, targets, nix)
    prefix = DEFAULT_BRANCH_PREFIX
    checks = nix.json(["nix", "eval", "--json", f"{ref}#checks.{system}", "--apply", "builtins.attrNames"],
                      "the flake's checks") if system_how != "guessed" else None
    check_names = sorted(c for c in checks if isinstance(c, str)) if isinstance(checks, list) else []
    ssh = f"{this_user()}@{{target}}"
    text = render(flake=flake, hosts_dir=hosts_dir, system=system, ssh=ssh, prefix=prefix,
                  branches=[prefix.format(machine=proc.this_machine())], checks=check_names, targets=targets)
    try:
        config.parse(text)
    except config.ConfigError as e:  # a bug in render, never the user's
        die(f"generated an invalid {CONFIG_FILE}: {e}")
    (root / CONFIG_FILE).write_text(text, encoding="utf-8")
    print(f"wrote {CONFIG_FILE}:")
    print(f"  flake_dir = {flake!r}, targets_dir = {hosts_dir!r} ({', '.join(targets) or 'no target directories yet'})")
    print(f"  targets_arch = {system!r} ({system_how}), ssh_address_template = {ssh!r}, branch_prefix = {prefix!r}")
    for n in notes:
        print(f"  note: {n}")


def this_user() -> str:
    try:
        return getpass.getuser()
    except OSError:
        return "root"


def committed_ref(root: Path, flake: str) -> str | None:
    """HEAD's tree as a flake reference, when HEAD holds the flake."""
    flake_nix = os.path.normpath(f"{flake}/flake.nix")
    if run(["git", "rev-parse", "--verify", "-q", f"HEAD:{flake_nix}"], check=False).returncode != 0:
        return None
    head = out(["git", "rev-parse", "HEAD"])
    return f"git+file://{root.resolve()}?rev={head}" + (f"&dir={flake}" if flake != "." else "")


class NixProbe:
    """The best-effort nix evals: each a note when nix is missing or
    fails, and none attempted once one took longer than NIX_TIMEOUT."""

    def __init__(self, notes: list[str]) -> None:
        self.notes = notes
        self.timed_out = False
        self.missing = False

    def json(self, cmd: list[str], what: str) -> object | None:
        if self.timed_out or self.missing:
            self.notes.append(f"{what} not read: nix is " + ("too slow here" if self.timed_out else "not on PATH"))
            return None
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=NIX_TIMEOUT, check=False)
        except FileNotFoundError:
            self.missing = True
            self.notes.append(f"no nix on PATH: {what} not read")
            return None
        except subprocess.TimeoutExpired:
            self.timed_out = True
            self.notes.append(f"nix eval of {what} took over {NIX_TIMEOUT}s: skipped, as every further probe")
            return None
        if p.returncode != 0:
            lines = [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
            errors = [ln for ln in lines if ln.startswith("error:")]
            self.notes.append(f"nix eval of {what} failed: {(errors or lines or ['(no output)'])[-1]}")
            return None
        try:
            return json.loads(p.stdout)
        except ValueError:
            return None


def repo_files(root: Path) -> list[str]:
    """Tracked and untracked unignored paths, as the repository holds them
    (a submodule is one gitlink, its contents never listed)."""
    return out(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=str(root)) \
        .split("\0")


def find_flake(root: Path, given: Path | None) -> str:
    """The directory holding flake.nix, relative to the root: the one
    given, the root, else the one flake.nix no deeper than any other."""
    if given is not None:
        d, top = given.resolve(), root.resolve()
        if not (d / "flake.nix").is_file():
            die(f"{given}: no flake.nix there")
        if not d.is_relative_to(top):
            die(f"{given} is outside the repository")
        return os.path.relpath(d, top) if d != top else "."
    if (root / "flake.nix").is_file():
        return "."
    found = sorted((f.count("/"), f[: -len("/flake.nix")]) for f in repo_files(root)
                   if f.endswith("/flake.nix") and f.count("/") <= MAX_DEPTH)
    if not found:
        die("no flake.nix in this repository (nixconvoy init [<flake-dir>])")
    top = [d for n, d in found if n == found[0][0]]
    if len(top) > 1:
        die(f"several flakes: {', '.join(top)}; name one: nixconvoy init <flake-dir>")
    return top[0]


def find_hosts_dir(root: Path, flake: str, host_names: list[str] | None) -> str | None:
    """The directory whose subdirectories name the nixosConfigurations:
    under the flake directory (else anywhere near the root), the one whose
    subdirectories best match the names nix gave, or without them hold the
    most configuration.nix (a hosts/, machines/ or nodes/ before another
    of the same score); None when nothing matches."""
    dirs: set[str] = set()
    for f in repo_files(root):
        parts = f.split("/")
        for i in range(1, min(len(parts) - 1, MAX_DEPTH + 2)):
            dirs.add("/".join(parts[:i]))

    def score(d: str) -> tuple[int, int]:
        p = root / d
        if not p.is_dir():
            return (0, 0)
        subs = {c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")}
        confs = sum((p / s / "configuration.nix").is_file() for s in subs)
        return (len(subs & set(host_names)) if host_names is not None else confs, confs)

    # Best match first; under the flake directory before elsewhere, a
    # conventional name before another, shallow before deep.
    ranked = sorted((tuple(-n for n in score(d)), 0 if flake == "." or d.startswith(flake + "/") else 1,
                     0 if d.rsplit("/", 1)[-1] in ("hosts", "machines", "nodes") else 1, d.count("/"), d)
                    for d in dirs)
    if not ranked or ranked[0][0] == (0, 0):
        return None
    return ranked[0][-1]


def detect_system(root: Path, ref: str, hosts_dir: str, targets: list[str], nix: NixProbe) -> tuple[str, str]:
    """The targets' system: the one system literal the hosts' .nix files
    agree on where they set a platform (hardware-configuration.nix's
    nixpkgs.hostPlatform, a `system =`; not binfmt's emulated systems or a
    comment), else the first target's evaluated hostPlatform, else this
    machine's architecture as a Linux guess."""
    seen: set[str] = set()
    if (root / hosts_dir).is_dir():
        for f in (root / hosts_dir).rglob("*.nix"):
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.lstrip().startswith("#") and PLATFORM_LINE_RE.search(line):
                    seen.update(SYSTEM_RE.findall(line))
    if len(seen) == 1:
        return seen.pop(), f"from {hosts_dir}'s .nix files"
    if targets:
        attr = f'{ref}#nixosConfigurations."{targets[0]}".config.nixpkgs.hostPlatform.system'
        got = nix.json(["nix", "eval", "--json", attr], f"{targets[0]}'s system")
        if isinstance(got, str) and got:
            return got, f"evaluated from {targets[0]}"
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine(), platform.machine()) or "x86_64"
    several = f" ({', '.join(sorted(seen))} all appear under {hosts_dir})" if seen else ""
    nix.notes.append(f"system guessed from this machine's architecture{several}: verify it")
    return f"{arch}-linux", "guessed"


def render(*, flake: str, hosts_dir: str, system: str, ssh: str, prefix: str, branches: list[str],
           checks: list[str], targets: list[str]) -> str:
    q = json.dumps  # a TOML basic string
    lines = [
        "config_version = 1",
        f"flake_dir = {q(flake)}",
        f"targets_dir = {q(hosts_dir)}",
        f"targets_arch = {q(system)}",
        f"ssh_address_template = {q(ssh)}",
        "",
        "# A checkout's work branch is <branch_prefix><leaf>, `open <path>` naming it after the path.",
        "# {machine} in the prefix is this machine's hostname, a namespace per machine when you want one.",
        f"branch_prefix = {q(prefix)}",
        "# The work branches nixconvoy may union into a deploy (and own, when named with the prefix), by",
        "# the prefixes of their names; a pushed branch starting with none is ignored.",
        f"work_branches = [{', '.join(q(b) for b in branches)}]",
        "# Child worktrees are an optional feature of nixconvoy that can be used by hand if desired,",
        "# but is primarily intended to hook into worktrees that harnesses automatically create to isolate their subagents.",
        "# The default directory is set to Claude Code's, so that nixconvoy's Claude Code plugin works out of the box.",
        "child_worktrees = \".claude/worktrees\"",
        "",
        "[eval]",
        "parallel = \"auto\"  # Determinate Nix's parallel evaluation: true, false or \"auto\" (on when nix has it)",
        "# Optional: a flake check (checks.<targets_arch>.<name>) proven before any build"
        + (f"; this flake's: {', '.join(checks)}" if checks else "") + ".",
        f"# precheck = {q(checks[0] if len(checks) == 1 else 'lint')}",
        "",
        "# Optional: realise the closures in another machine's store, keyed by the hostname nixconvoy",
        "# runs on (\"*\": any other machine; a machine mapped to itself realises locally); that",
        "# machine's nix daemon builds them or hands them to its own remote builders.",
        "# [build]",
        "# store = { \"*\" = \"bigbox\" }",
        "# skip_substituters = [\"https://slow.cachix.org\"]",
        "",
        "# Optional per-target settings" + (f" (autodetected targets: {', '.join(targets)})" if targets else ""),
        f"# [targets.{targets[0] if targets else '<name>'}]",
        "# # overrides ssh_address_template, if one target has an unusual username or domain-name",
        "# ssh_address = \"root@10.0.0.9\"",
        "# # For larger fleets, tags can be used to refer to several machines as a group in `nixconvoy deploy`",
        "# tags = [\"vps\"]",
        "",
        "# Optional support for git submodules. Feel free to ignore this if you don't have any submodules in your git repo.",
        "# [submodules.<input>]",
        "# repo = \"~/src/<input>\"",
    ]
    return "\n".join(lines) + "\n"
