"""`nixconvoy init`: nixconvoy.toml for a fresh install.  The targets are
the flake's nixosConfigurations as nix evaluates them, one
[targets.<name>] table each (so nix is needed); every other key is
detected from the repository - the flake directory, the targets' system,
this machine's work-branch prefix, the remote and its main branch - or
starts from this machine (the ssh address template, as this user); the
optional sections are written as commented examples filled with what was
found (the flake's checks).  git is only read: nothing is staged or
committed."""

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
    hosts = nix.json(ref, "nixosConfigurations", "the nixosConfigurations' names", apply="builtins.attrNames")
    if hosts is None and not nix.missing and (alt := committed_ref(root, flake)) is not None:
        # The working tree may not evaluate (a submodule's directory still
        # empty): the committed tree, whose submodules nix fetches itself.
        hosts = nix.json(alt, "nixosConfigurations", "HEAD's nixosConfigurations' names", apply="builtins.attrNames")
        if hosts is not None:
            ref = alt
    targets = sorted(h for h in hosts if isinstance(h, str)) if isinstance(hosts, list) else None
    if targets is None:
        die("the targets are the flake's nixosConfigurations, which could not be read:\n  "
            + "\n  ".join(notes) + "\nfix that (nix on PATH, a flake that evaluates) and re-run")
    if not targets:
        die("the flake has no nixosConfigurations yet: nothing to deploy to; add one and re-run")
    # Where the hosts' files are, for the system literal they may hold.
    hosts_dir = find_hosts_dir(root, flake, targets)
    system, system_how = detect_system(root, ref, hosts_dir, targets, nix)
    prefix = DEFAULT_BRANCH_PREFIX
    checks = nix.json(ref, f"checks.{system}", "the flake's checks",
                      apply="builtins.attrNames") if system_how != "guessed" else None
    check_names = sorted(c for c in checks if isinstance(c, str)) if isinstance(checks, list) else []
    ssh = f"{this_user()}@{{target}}"
    remote, main = detect_names(notes)
    text = render(flake=flake, system=system, ssh=ssh, prefix=prefix,
                  branches=[prefix.format(machine=proc.this_machine())], checks=check_names, targets=targets,
                  remote=remote, main=main, hosts_dir=hosts_dir)
    try:
        config.parse(text)
    except config.ConfigError as e:  # a bug in render, never the user's
        die(f"generated an invalid {CONFIG_FILE}: {e}")
    (root / CONFIG_FILE).write_text(text, encoding="utf-8")
    print(f"wrote {CONFIG_FILE}:")
    print(f"  flake_dir = {flake!r}, targets: {', '.join(targets)}")
    print(f"  targets_arch = {system!r} ({system_how}), ssh_address_template = {ssh!r}, branch_prefix = {prefix!r}")
    print(f"  remote = {remote!r}, main_branch = {main!r}")
    for n in notes:
        print(f"  note: {n}")


def detect_names(notes: list[str]) -> tuple[str, str]:
    """The remote main lives on and main's name there: origin, else the one
    remote; its HEAD's branch as fetched, else main or master as fetched,
    else main (noted)."""
    remotes = out(["git", "remote"]).split()
    if "origin" in remotes or len(remotes) != 1 or not config.valid_remote(remotes[0]):
        remote = "origin"
        if "origin" not in remotes:
            notes.append(f"remote = \"origin\" assumed ({', '.join(remotes) or 'no remote yet'}); "
                         "nixconvoy fetches and pushes it")
    else:
        remote = remotes[0]
    head = run(["git", "symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD"], check=False).stdout.strip()
    if head.startswith(f"{remote}/") and config.valid_branch(head[len(remote) + 1:], remote):
        return remote, head[len(remote) + 1:]
    for b in ("main", "master"):
        if run(["git", "rev-parse", "-q", "--verify", f"refs/remotes/{remote}/{b}"], check=False).returncode == 0:
            return remote, b
    notes.append(f"main_branch = \"main\" assumed ({remote} has no HEAD, main or master as fetched)")
    return remote, "main"


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
    """The best-effort nix evals, each a note when nix is missing or fails.
    A flake's inputs are fetched first (`nix flake archive`, under nix's
    own transfer handling), so the evals run offline: none waits on the
    network, and none needs a time limit."""

    def __init__(self, notes: list[str]) -> None:
        self.notes = notes
        self.missing = False
        self.fetched: dict[str, bool] = {}

    def nix(self, cmd: list[str], what: str, doing: str) -> str | None:
        """`cmd`'s output, or None (noted) when nix is missing or it fails."""
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            self.missing = True
            self.notes.append(f"no nix on PATH: {what} not read")
            return None
        if p.returncode != 0:
            lines = [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
            errors = [ln for ln in lines if ln.startswith("error:")]
            self.notes.append(f"{doing} failed, so {what} not read: {(errors or lines or ['(no output)'])[-1]}")
            return None
        return p.stdout

    def json(self, ref: str, attr: str, what: str, apply: str | None = None) -> object | None:
        """`ref#attr` evaluated (through `apply`) as JSON: offline once the
        inputs are fetched, fetching what it forces when the archive (every
        input, needed or not) failed."""
        if self.missing:
            self.notes.append(f"{what} not read: nix is not on PATH")
            return None
        if ref not in self.fetched:
            self.fetched[ref] = self.nix(["nix", "flake", "archive", ref], what,
                                         f"fetching the inputs of {ref}") is not None
        got = self.nix(["nix", "eval", *(["--offline"] if self.fetched[ref] else []), "--json", f"{ref}#{attr}",
                        *(["--apply", apply] if apply else [])], what, "nix eval")
        if got is None:
            return None
        try:
            return json.loads(got)
        except ValueError:
            self.notes.append(f"nix eval printed no JSON for {what}")
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


def find_hosts_dir(root: Path, flake: str, host_names: list[str]) -> str | None:
    """The directory whose subdirectories name the nixosConfigurations:
    under the flake directory (else anywhere near the root), the one whose
    subdirectories best match the names nix gave (a hosts/, machines/ or
    nodes/ before another of the same score); None when none do."""
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
        return (len(subs & set(host_names)), confs)

    # Best match first; under the flake directory before elsewhere, a
    # conventional name before another, shallow before deep.
    ranked = sorted((tuple(-n for n in score(d)), 0 if flake == "." or d.startswith(flake + "/") else 1,
                     0 if d.rsplit("/", 1)[-1] in ("hosts", "machines", "nodes") else 1, d.count("/"), d)
                    for d in dirs)
    if not ranked or ranked[0][0] == (0, 0):
        return None
    return ranked[0][-1]


def detect_system(root: Path, ref: str, hosts_dir: str | None, targets: list[str], nix: NixProbe) -> tuple[str, str]:
    """The targets' system: the one system literal the hosts' .nix files
    agree on where they set a platform (hardware-configuration.nix's
    nixpkgs.hostPlatform, a `system =`; not binfmt's emulated systems or a
    comment), else the first target's evaluated hostPlatform, else this
    machine's architecture as a Linux guess."""
    seen: set[str] = set()
    if hosts_dir and (root / hosts_dir).is_dir():
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
        got = nix.json(ref, f'nixosConfigurations."{targets[0]}".config.nixpkgs.hostPlatform.system',
                       f"{targets[0]}'s system")
        if isinstance(got, str) and got:
            return got, f"evaluated from {targets[0]}"
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine(), platform.machine()) or "x86_64"
    several = f" ({', '.join(sorted(seen))} all appear under {hosts_dir})" if seen else ""
    nix.notes.append(f"system guessed from this machine's architecture{several}: verify it")
    return f"{arch}-linux", "guessed"


def render(*, flake: str, system: str, ssh: str, prefix: str, branches: list[str],
           checks: list[str], targets: list[str], remote: str = "origin", main: str = "main",
           hosts_dir: str | None = None) -> str:
    """The file, the `targets` as [targets.<name>] tables, `hosts_dir`
    (where their files were found) in the `dir` examples."""
    q = json.dumps  # a TOML basic string
    lines = [
        "config_version = 1",
        f"flake_dir = {q(flake)}",
        f"targets_arch = {q(system)}",
        f"ssh_address_template = {q(ssh)}",
        "# The remote the work branches are pushed to and main is landed on, and main's name there.",
        f"remote = {q(remote)}",
        f"main_branch = {q(main)}",
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
        "# How the targets are switched (every target's defaults; [targets.<name>.switch] overrides one's).",
        "# Confirmed, unless `confirmed = false`: a rollback timer is armed on the target, the new system",
        "# activated, verified over a new ssh connection (and its `checks`, bash run as root), and only then",
        "# set as the profile and boot entry; otherwise it rolls back within `deadline` seconds.",
        "# [switch]",
        "# deadline = 30",
        "# internet_check = false   # the verification also wants the internet reachable from the target",
        "# checks = \"\"\"",
        "# systemctl is-active -q nginx.service",
        "# \"\"\"",
        "",
        *target_tables(targets, hosts_dir if hosts_dir is not None else os.path.normpath(f"{flake}/hosts")),
        "",
        "# Optional support for git submodules. Feel free to ignore this if you don't have any submodules in your git repo.",
        "# [submodules.<input>]",
        "# repo = \"~/src/<input>\"",
    ]
    return "\n".join(lines) + "\n"


def target_tables(targets: list[str], hosts_dir: str) -> list[str]:
    """One [targets.<name>] table per nixosConfiguration - the inventory -
    each with the optional keys as comments, `dir`'s example under
    `hosts_dir`."""
    q = json.dumps
    lines = ["# The targets: one table per nixosConfiguration (the inventory, as there is no targets_dir);",
             "# a nixosConfiguration added later needs its table here.",
             "# A deploy re-evaluates a target only when flake.nix, a file it names (transitively) or the",
             "# target's own directory changed: a directory named after it beside the other targets' is",
             "# found on its own, `dir` names one elsewhere."]
    for i, t in enumerate(targets):
        lines += [f"[targets.{t if re.fullmatch(r'[A-Za-z0-9_-]+', t) else q(t)}]",
                  f"# dir = {q(f'{hosts_dir}/{t}')}"]
        if i == 0:
            lines += ["# # overrides ssh_address_template, if one target has an unusual username or domain-name",
                      "# ssh_address = \"root@10.0.0.9\"",
                      "# # For larger fleets, tags can be used to refer to several machines as a group in `nixconvoy deploy`",
                      "# tags = [\"vps\"]"]
    return lines
