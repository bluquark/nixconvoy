"""nixconvoy.toml at the repo root: the fleet-level settings the git-side
verbs need before any Nix runs, and the per-target facts a deploy needs before
probing.  Every key is optional; a missing file is the defaults."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "nixconvoy.toml"
MSG_FILE = "COMMIT_MSG.md"
BRANCHES_FILE = "deploy-branches"


@dataclass(frozen=True)
class TargetConfig:
    # Deployable only from itself: never probed or named from elsewhere.
    local_only: bool = False
    # ssh address, overriding Config.ssh_address's template.
    ssh_address: str = ""
    # `@tag` on the command line names every target carrying it.
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    # The directory holding flake.nix, relative to the repo root.
    flake: str = "."
    # The directory whose subdirectories name the nixosConfigurations.
    hosts_dir: str = ""
    system: str = "x86_64-linux"
    # A target's ssh address (user@name): nixconvoy.toml's ssh_address, a
    # template over {target}; the builder is addressed through it too.
    ssh_template: str = "{target}"
    # A checkout owns "<prefix><leaf>", its subagent worktrees "<prefix><leaf>-<name>"
    # (the main checkout's "<prefix>main-<name>"); {machine} is this machine's hostname.
    branch_prefix: str = "work/{machine}/"
    # Where `open --child <name>` puts a child (subagent) worktree, relative
    # to the checkout: Claude Code's own place for one by default.  A nested
    # worktree must be ignored (open writes it into .git/info/exclude); any
    # worktree nested in a checkout is one of its children, wherever it was made.
    child_worktrees: str = ".claude/worktrees"
    # A flake check (checks.<system>.<check>) proven before any build.
    precheck: str = ""
    # Determinate Nix's parallel evaluation (builtins.parallel, --eval-cores):
    # None is "auto", on when the nix here knows the feature.
    parallel_eval: bool | None = None
    # The builder the closures are realised on, by sys.platform ("darwin",
    # "linux"); absent means here.
    builder: dict[str, str] = field(default_factory=dict)
    build_unreachable_hint: str = ""
    # Substituters a deploy build leaves out of the builder's own configured
    # list (targets.build_cmd): the caches themselves are each builder's
    # nix.conf's business.
    skip_substituters: tuple[str, ...] = ()
    targets: dict[str, TargetConfig] = field(default_factory=dict)
    # The flake inputs developed in place (submodule.py): input name -> the
    # repository's checkout on this machine, whose origin nix fetches.
    submodules: dict[str, str] = field(default_factory=dict)

    def target(self, name: str) -> TargetConfig:
        return self.targets.get(name, TargetConfig())

    def ssh_address(self, name: str) -> str:
        return self.target(name).ssh_address or self.ssh_template.format(target=name)

    def tagged(self, tag: str) -> list[str]:
        return [n for n, h in self.targets.items() if tag in h.tags]

    @property
    def hosts_path(self) -> str:
        return self.hosts_dir or os.path.normpath(f"{self.flake}/hosts")

    @property
    def flake_path(self) -> str:
        return os.path.normpath(self.flake)

    def flake_file(self, name: str) -> str:
        """A repo path inside the flake directory (flake.nix, flake.lock)."""
        return os.path.normpath(f"{self.flake}/{name}")
    


class ConfigError(Exception):
    pass


def _table(v: object, where: str) -> dict[str, object]:
    if not isinstance(v, dict):
        raise ConfigError(f"{where} must be a table")
    return v


def _str(t: dict[str, object], k: str, where: str, default: str = "") -> str:
    v = t.get(k, default)
    if not isinstance(v, str):
        raise ConfigError(f"{where}.{k} must be a string")
    return v


def _bool(t: dict[str, object], k: str, where: str, default: bool = False) -> bool:
    v = t.get(k, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{where}.{k} must be true or false")
    return v


def _auto_bool(t: dict[str, object], k: str, where: str) -> bool | None:
    v = t.get(k, "auto")
    if v == "auto":
        return None
    if not isinstance(v, bool):
        raise ConfigError(f'{where}.{k} must be true, false or "auto"')
    return v


def _strs(t: dict[str, object], k: str, where: str) -> tuple[str, ...]:
    v = t.get(k, [])
    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
        raise ConfigError(f"{where}.{k} must be a list of strings")
    return tuple(v)


def _bare_url(u: str) -> str:
    """A substituter URL as nix.conf's spellings of it compare: without its
    query (?priority=...) and trailing slashes."""
    return u.split("?")[0].rstrip("/")


# The keys the target/builder/machine vocabulary renamed, for the error a
# config still using one gets.
_RENAMED = {"target": "ssh_address (over {target}, not {host})", "hosts": "targets",
            "worktrees": "child_worktrees", "precheck.attr": "precheck.check",
            "build.host": "build.builder",
            "build.caches": "build.skip_substituters: the builder's nix.conf lists the caches",
            "build.public_substituters": "build.skip_substituters: the builder's nix.conf lists the caches"}


def _renamed(k: str) -> str:
    new = _RENAMED.get(k)
    return f" (now {new})" if new else ""


def _template(t: dict[str, object], k: str, default: str, placeholder: str) -> str:
    """A template setting, proven here to format with `placeholder` alone,
    so a stale placeholder fails as a ConfigError rather than in a probe."""
    v = _str(t, k, CONFIG_FILE, default)
    try:
        v.format(**{placeholder: "x"})
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError(f"{CONFIG_FILE}: {k} is a template over {{{placeholder}}} alone ({e!r})") from e
    return v


def parse(text: str) -> Config:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{CONFIG_FILE}: {e}") from e
    version = doc.get("version", 1)
    if version != 1:
        raise ConfigError(f"{CONFIG_FILE}: version {version} is not supported (1 is)")
    known = {"version", "flake", "hosts_dir", "system", "ssh_address", "branch_prefix", "child_worktrees",
             "precheck", "eval", "build", "targets", "submodules"}
    if "memo" in doc:
        raise ConfigError(f"{CONFIG_FILE}: [memo] is gone: the eval memo needs no settings, it keys "
                          "each target on the files its configuration reaches by path literal")
    for k in doc:
        if k not in known:
            raise ConfigError(f"{CONFIG_FILE}: unknown key {k}{_renamed(k)}")
    pre = _table(doc.get("precheck", {}), "precheck")
    for k in pre:
        if k != "check":
            raise ConfigError(f"{CONFIG_FILE}: unknown key precheck.{k}{_renamed(f'precheck.{k}')}")
    ev = _table(doc.get("eval", {}), "eval")
    build = _table(doc.get("build", {}), "build")
    for k in build:
        if k not in ("builder", "unreachable_hint", "skip_substituters"):
            raise ConfigError(f"{CONFIG_FILE}: unknown key build.{k}{_renamed(f'build.{k}')}")
    raw_builder = build.get("builder", {})
    if isinstance(raw_builder, str):
        builder = {"linux": raw_builder, "darwin": raw_builder}
    else:
        builder = {k: v for k, v in _table(raw_builder, "build.builder").items() if isinstance(v, str)}
    skip = []
    for s in _strs(build, "skip_substituters", "build"):
        if s.split() != [s]:
            raise ConfigError(f"{CONFIG_FILE}: build.skip_substituters entry {s!r} is not a substituter URL")
        skip.append(_bare_url(s))
    targets: dict[str, TargetConfig] = {}
    for name, h in _table(doc.get("targets", {}), "targets").items():
        t = _table(h, f"targets.{name}")
        for k in t:
            if k not in ("local_only", "ssh_address", "tags"):
                raise ConfigError(f"{CONFIG_FILE}: unknown key targets.{name}.{k}{_renamed(k)}")
        targets[name] = TargetConfig(local_only=_bool(t, "local_only", f"targets.{name}"),
                                     ssh_address=_str(t, "ssh_address", f"targets.{name}"),
                                     tags=_strs(t, "tags", f"targets.{name}"))
    submodules: dict[str, str] = {}
    for name, a in _table(doc.get("submodules", {}), "submodules").items():
        t = _table(a, f"submodules.{name}")
        for k in t:
            if k != "repo":
                raise ConfigError(f"{CONFIG_FILE}: unknown key submodules.{name}.{k}")
        repo = _str(t, "repo", f"submodules.{name}")
        if not repo:
            raise ConfigError(f"{CONFIG_FILE}: submodules.{name}.repo is required")
        submodules[name] = repo
    children = _str(doc, "child_worktrees", CONFIG_FILE, ".claude/worktrees").strip("/")
    if not children or ".." in children.split("/"):
        raise ConfigError(f"{CONFIG_FILE}: child_worktrees is a directory inside the checkout")
    return Config(
        flake=_str(doc, "flake", CONFIG_FILE, "."),
        hosts_dir=_str(doc, "hosts_dir", CONFIG_FILE),
        system=_str(doc, "system", CONFIG_FILE, "x86_64-linux"),
        ssh_template=_template(doc, "ssh_address", "{target}", "target"),
        branch_prefix=_template(doc, "branch_prefix", "work/{machine}/", "machine"),
        child_worktrees=children,
        precheck=_str(pre, "check", "precheck"),
        parallel_eval=_auto_bool(ev, "parallel", "eval"),
        builder=builder,
        build_unreachable_hint=_str(build, "unreachable_hint", "build"),
        skip_substituters=tuple(skip),
        targets=targets,
        submodules=submodules,
    )


def load(root: Path) -> Config:
    try:
        text = (root / CONFIG_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    return parse(text)


_CFG: Config | None = None


def cfg() -> Config:
    """The current repo's configuration, read once from the repo root (the
    working directory after cli.main's chdir)."""
    global _CFG
    if _CFG is None:
        try:
            _CFG = load(Path.cwd())
        except ConfigError as e:
            print(f"nixconvoy: {e}", file=sys.stderr)
            sys.exit(1)
    return _CFG


def set_cfg(c: Config) -> None:
    global _CFG
    _CFG = c


def builder() -> str:
    """Where the closures are realised: NIXCONVOY_BUILDER, else the config's
    entry for this platform; empty means here."""
    env = os.environ.get("NIXCONVOY_BUILDER")
    if env is not None:
        return env
    return cfg().builder.get("darwin" if sys.platform == "darwin" else "linux", "")
