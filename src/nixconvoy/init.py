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

from . import config, strings
from .config import CONFIG_FILE
from .proc import die, out, run

SYSTEM_RE = re.compile(r'"((?:x86_64|aarch64|i686|riscv64|armv7l|powerpc64le)-(?:linux|darwin))"')
PLATFORM_LINE_RE = re.compile(r"hostPlatform|\bsystem\s*=")
MAX_DEPTH = 3  # how deep under the root the flake and hosts directories are looked for
DEFAULT_BRANCH_PREFIX = "nixconvoy/"


def main(flake_arg: str | None, start: Path) -> None:
    root = Path.cwd()
    if (root / CONFIG_FILE).exists():
        die(strings.INIT_EXISTS())
    notes: list[str] = []
    flake = find_flake(root, start / flake_arg if flake_arg else None)
    nix = NixProbe(notes)
    ref = "." if flake == "." else f"./{flake}"
    hosts = nix.json(ref, "nixosConfigurations", strings.INIT_WHAT_HOSTS, apply="builtins.attrNames")
    if hosts is None and not nix.missing and (alt := committed_ref(root, flake)) is not None:
        # The working tree may not evaluate (a submodule's directory still
        # empty): the committed tree, whose submodules nix fetches itself.
        hosts = nix.json(alt, "nixosConfigurations", strings.INIT_WHAT_HEAD_HOSTS, apply="builtins.attrNames")
        if hosts is not None:
            ref = alt
    targets = sorted(h for h in hosts if isinstance(h, str)) if isinstance(hosts, list) else None
    if targets is None:
        die(strings.INIT_NO_TARGETS(notes="\n  ".join(notes)))
    if not targets:
        die(strings.INIT_NO_HOSTS)
    # Where the hosts' files are, for the system literal they may hold.
    hosts_dir = find_hosts_dir(root, flake, targets)
    system, system_how = detect_system(root, ref, hosts_dir, targets, nix)
    prefix = DEFAULT_BRANCH_PREFIX
    checks = nix.json(ref, f"checks.{system}", strings.INIT_WHAT_CHECKS,
                      apply="builtins.attrNames") if system_how != strings.INIT_SYSTEM_GUESSED else None
    check_names = sorted(c for c in checks if isinstance(c, str)) if isinstance(checks, list) else []
    ssh = f"{this_user()}@{{target}}"
    remote, main = detect_names(notes)
    text = render(flake=flake, system=system, ssh=ssh, prefix=prefix,
                  checks=check_names, targets=targets,
                  remote=remote, main=main, hosts_dir=hosts_dir)
    try:
        config.parse(text)
    except config.ConfigError as e:  # a bug in render, never the user's
        die(strings.INIT_INVALID(error=e))
    (root / CONFIG_FILE).write_text(text, encoding="utf-8")
    print(strings.INIT_WROTE())
    print(strings.INIT_WROTE_FLAKE(flake=flake, targets=", ".join(targets)))
    print(strings.INIT_WROTE_SYSTEM(system=system, how=system_how, ssh=ssh, prefix=prefix))
    print(strings.INIT_WROTE_REMOTE(remote=remote, main=main))
    for n in notes:
        print(strings.INIT_WROTE_NOTE(note=n))


def detect_names(notes: list[str]) -> tuple[str, str]:
    """The remote main lives on and main's name there: origin, else the one
    remote; its HEAD's branch as fetched, else main or master as fetched,
    else main (noted)."""
    remotes = out(["git", "remote"]).split()
    if "origin" in remotes or len(remotes) != 1 or not config.valid_remote(remotes[0]):
        remote = "origin"
        if "origin" not in remotes:
            notes.append(strings.INIT_REMOTE_ASSUMED(remotes=", ".join(remotes) or strings.INIT_NO_REMOTE_YET))
    else:
        remote = remotes[0]
    head = run(["git", "symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD"], check=False).stdout.strip()
    if head.startswith(f"{remote}/") and config.valid_branch(head[len(remote) + 1:], remote):
        return remote, head[len(remote) + 1:]
    for b in ("main", "master"):
        if run(["git", "rev-parse", "-q", "--verify", f"refs/remotes/{remote}/{b}"], check=False).returncode == 0:
            return remote, b
    notes.append(strings.INIT_MAIN_ASSUMED(remote=remote))
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
            self.notes.append(strings.INIT_NIX_MISSING(what=what))
            return None
        if p.returncode != 0:
            lines = [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
            errors = [ln for ln in lines if ln.startswith("error:")]
            self.notes.append(strings.INIT_NIX_FAILED(doing=doing, what=what,
                                                     error=(errors or lines or [strings.INIT_NIX_NO_OUTPUT])[-1]))
            return None
        return p.stdout

    def json(self, ref: str, attr: str, what: str, apply: str | None = None) -> object | None:
        """`ref#attr` evaluated (through `apply`) as JSON: offline once the
        inputs are fetched, fetching what it forces when the archive (every
        input, needed or not) failed."""
        if self.missing:
            self.notes.append(strings.INIT_NIX_NOT_ON_PATH(what=what))
            return None
        if ref not in self.fetched:
            self.fetched[ref] = self.nix(["nix", "flake", "archive", ref], what,
                                         strings.INIT_DOING_FETCH(ref=ref)) is not None
        got = self.nix(["nix", "eval", *(["--offline"] if self.fetched[ref] else []), "--json", f"{ref}#{attr}",
                        *(["--apply", apply] if apply else [])], what, strings.INIT_DOING_EVAL)
        if got is None:
            return None
        try:
            return json.loads(got)
        except ValueError:
            self.notes.append(strings.INIT_NIX_NO_JSON(what=what))
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
            die(strings.INIT_NO_FLAKE_THERE(given=given))
        if not d.is_relative_to(top):
            die(strings.INIT_FLAKE_OUTSIDE(given=given))
        return os.path.relpath(d, top) if d != top else "."
    if (root / "flake.nix").is_file():
        return "."
    found = sorted((f.count("/"), f[: -len("/flake.nix")]) for f in repo_files(root)
                   if f.endswith("/flake.nix") and f.count("/") <= MAX_DEPTH)
    if not found:
        die(strings.INIT_NO_FLAKE())
    top = [d for n, d in found if n == found[0][0]]
    if len(top) > 1:
        die(strings.INIT_SEVERAL_FLAKES(flakes=", ".join(top)))
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
        return seen.pop(), strings.INIT_SYSTEM_FROM_FILES(hosts_dir=hosts_dir)
    if targets:
        got = nix.json(ref, f'nixosConfigurations."{targets[0]}".config.nixpkgs.hostPlatform.system',
                       strings.INIT_WHAT_SYSTEM(target=targets[0]))
        if isinstance(got, str) and got:
            return got, strings.INIT_SYSTEM_EVALUATED(target=targets[0])
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine(), platform.machine()) or "x86_64"
    several = strings.INIT_SYSTEM_SEVERAL(systems=", ".join(sorted(seen)), hosts_dir=hosts_dir) if seen else ""
    nix.notes.append(strings.INIT_SYSTEM_GUESS_NOTE(several=several))
    return f"{arch}-linux", strings.INIT_SYSTEM_GUESSED


def render(*, flake: str, system: str, ssh: str, prefix: str,
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
        strings.INIT_TOML_REMOTE,
        f"remote = {q(remote)}",
        f"main_branch = {q(main)}",
        "",
        strings.INIT_TOML_BRANCH_PREFIX(),
        f"branch_prefix = {q(prefix)}",
        strings.INIT_TOML_CHILD_WORKTREES(),
        "child_worktrees = \".claude/worktrees\"",
        strings.INIT_TOML_PRECHECK(
            these=strings.INIT_TOML_PRECHECK_THESE(checks=", ".join(checks)) if checks else ""),
        f"# precheck = {q(checks[0] if len(checks) == 1 else 'lint')}",
        "",
        strings.INIT_TOML_BUILD,
        "",
        strings.INIT_TOML_PARALLEL_EVAL,
        "",
        strings.INIT_TOML_SWITCH(deadline=config.DEFAULT_DEADLINE),
        "",
        *target_tables(targets, hosts_dir if hosts_dir is not None else os.path.normpath(f"{flake}/hosts")),
    ]
    return "\n".join(lines) + "\n"


def target_tables(targets: list[str], hosts_dir: str) -> list[str]:
    """One [targets.<name>] table per nixosConfiguration - the inventory -
    each with the optional keys as comments, `dir`'s example under
    `hosts_dir`."""
    q = json.dumps
    lines = [strings.INIT_TOML_TARGETS]
    for i, t in enumerate(targets):
        lines += [f"[targets.{t if re.fullmatch(r'[A-Za-z0-9_-]+', t) else q(t)}]",
                  f"# dir = {q(f'{hosts_dir}/{t}')}"]
        if i == 0:
            lines += [strings.INIT_TOML_FIRST_TARGET()]
    return lines
