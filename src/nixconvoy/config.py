"""nixconvoy.toml at the repo root: the fleet-level settings the git-side
verbs need before any Nix runs, and the per-target facts a deploy needs before
probing.  The scalar keys are required - `nixconvoy init` writes them,
detected - and the sections are optional: absent means none."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import proc

CONFIG_FILE = "nixconvoy.toml"
MSG_FILE = "COMMIT_MSG.md"
# Where Claude Code puts a subagent worktree: the one place a repository
# without nixconvoy.toml has for one (the plugin's create hook runs in every
# repository).
CLAUDE_WORKTREES = ".claude/worktrees"


@dataclass(frozen=True)
class TargetConfig:
    # ssh address, overriding Config.ssh_address's template.
    ssh_address: str = ""
    # `@tag` on the command line names every target carrying it.
    tags: tuple[str, ...] = ()



@dataclass(frozen=True)
class SubmoduleConfig:
    # The repository's checkout on this machine, whose origin nix fetches.
    repo: str
    # A repo-relative file nixconvoy keeps beside the gitlink, staged with
    # every pin it sets: the pinned rev and its committer date as JSON, for
    # a flake that wants the commit's identity or age (a version number).
    pin_file: str = ""

@dataclass(frozen=True)
class Config:
    # The directory holding flake.nix, relative to the repo root.
    flake_dir: str
    # The directory whose subdirectories name the nixosConfigurations,
    # relative to the repo root.
    targets_dir: str
    # The targets' system (checks.<targets_arch>.<precheck> is the precheck).
    targets_arch: str
    # A target's ssh address (user@name): nixconvoy.toml's
    # ssh_address_template, over {target}; the builder is addressed through it too.
    ssh_template: str
    # A checkout owns "<prefix><leaf>", its subagent worktrees "<prefix><leaf>-<name>"
    # (the main checkout's "<prefix>main-<name>"); {machine} is this machine's hostname.
    branch_prefix: str
    # The patterns of the work branches nixconvoy may union or own (`*`
    # spans anything, the one wildcard every matcher - here, the plan cores
    # - agrees on): a pushed branch none matches is ignored with a warning.
    work_branches: tuple[str, ...]
    # Where `open --child <name>` puts a child (subagent) worktree, relative
    # to the checkout.  A nested worktree must be ignored (open writes it
    # into .git/info/exclude); any worktree nested in a checkout is one of
    # its children, wherever it was made.
    child_worktrees: str
    # Determinate Nix's parallel evaluation (builtins.parallel, --eval-cores):
    # None is "auto", on when the nix here knows the feature.
    parallel_eval: bool | None
    # eval.precheck: a flake check (checks.<targets_arch>.<precheck>) proven
    # before any build; empty is none.
    precheck: str = ""
    # The builder the closures are realised on, by the hostname nixconvoy
    # runs on ("*": any other); a machine mapped to itself, or to nothing,
    # builds here.  The builder is addressed as a target (ssh_address).
    builder: dict[str, str] = field(default_factory=dict)
    # Substituters a deploy build leaves out of the builder's own configured
    # list (targets.build_cmd): the caches themselves are each builder's
    # nix.conf's business.
    skip_substituters: tuple[str, ...] = ()
    targets: dict[str, TargetConfig] = field(default_factory=dict)
    # The flake inputs developed in place (submodule.py), by input name.
    submodules: dict[str, SubmoduleConfig] = field(default_factory=dict)

    def target(self, name: str) -> TargetConfig:
        return self.targets.get(name, TargetConfig())

    def ssh_address(self, name: str) -> str:
        return self.target(name).ssh_address or self.ssh_template.format(target=name)

    def tagged(self, tag: str) -> list[str]:
        return [n for n, h in self.targets.items() if tag in h.tags]

    @property
    def targets_path(self) -> str:
        return os.path.normpath(self.targets_dir)

    @property
    def flake_path(self) -> str:
        return os.path.normpath(self.flake_dir)

    def flake_file(self, name: str) -> str:
        """A repo path inside the flake directory (flake.nix, flake.lock)."""
        return os.path.normpath(f"{self.flake_dir}/{name}")


class ConfigError(Exception):
    pass


def _table(v: object, where: str) -> dict[str, object]:
    if not isinstance(v, dict):
        raise ConfigError(f"{where} must be a table")
    return v


def _required(t: dict[str, object], k: str, where: str) -> object:
    if k not in t:
        key = k if where == CONFIG_FILE else f"{where}.{k}"
        raise ConfigError(f"{CONFIG_FILE}: {key} is required (`nixconvoy init` writes a complete file)")
    return t[k]


def _str(t: dict[str, object], k: str, where: str, default: str | None = "") -> str:
    """A string key; default None makes it required."""
    v = _required(t, k, where) if default is None else t.get(k, default)
    if not isinstance(v, str):
        raise ConfigError(f"{where}.{k} must be a string")
    return v


def _auto_bool(t: dict[str, object], k: str, where: str) -> bool | None:
    v = _required(t, k, where)
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


def _template(t: dict[str, object], k: str, placeholder: str) -> str:
    """A required template setting, proven here to format with
    `placeholder` alone, so a stale placeholder fails as a ConfigError
    rather than in a probe."""
    v = _str(t, k, CONFIG_FILE, None)
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
    version = _required(doc, "config_version", CONFIG_FILE)
    if isinstance(version, bool) or version != 1:
        raise ConfigError(f"{CONFIG_FILE}: config_version {version} is not supported (1 is)")
    known = {"config_version", "flake_dir", "targets_dir", "targets_arch", "ssh_address_template", "branch_prefix",
             "work_branches", "child_worktrees",
             "eval", "build", "targets", "submodules"}
    for k in doc:
        if k not in known:
            raise ConfigError(f"{CONFIG_FILE}: unknown key {k}")
    # The required keys in the file's order, so a hand-written file is
    # told of its first gap first.
    flake_dir = _str(doc, "flake_dir", CONFIG_FILE, None)
    targets_dir = _str(doc, "targets_dir", CONFIG_FILE, None)
    targets_arch = _str(doc, "targets_arch", CONFIG_FILE, None)
    ssh_template = _template(doc, "ssh_address_template", "target")
    branch_prefix = _template(doc, "branch_prefix", "machine")
    if "work_branches" not in doc:
        raise ConfigError(f"{CONFIG_FILE}: work_branches is required: the patterns of the work branches "
                          f"(`*` the wildcard), e.g. [\"{branch_prefix}*\"]")
    pats = doc["work_branches"]
    if not isinstance(pats, list) or not all(isinstance(s, str) for s in pats) or not pats:
        raise ConfigError(f"{CONFIG_FILE}: work_branches must be a non-empty list of strings")
    bad = [p for p in pats if not re.fullmatch(r"[\w.*/-]+", p)]
    if bad:
        raise ConfigError(f"{CONFIG_FILE}: work_branches: a pattern may hold letters, digits, '.', '-', '_', '/' "
                          f"and '*' only: {', '.join(bad)}")
    children = _str(doc, "child_worktrees", CONFIG_FILE, None).strip("/")
    if not children or ".." in children.split("/"):
        raise ConfigError(f"{CONFIG_FILE}: child_worktrees is a directory inside the checkout")
    ev = _table(_required(doc, "eval", CONFIG_FILE), "eval")
    for k in ev:
        if k not in ("parallel", "precheck"):
            raise ConfigError(f"{CONFIG_FILE}: unknown key eval.{k}")
    build = _table(doc.get("build", {}), "build")
    for k in build:
        if k not in ("builder", "skip_substituters"):
            raise ConfigError(f"{CONFIG_FILE}: unknown key build.{k}")
    raw_builder = build.get("builder", {})
    if isinstance(raw_builder, str):
        builder = {"*": raw_builder}
    elif not isinstance(raw_builder, dict):
        raise ConfigError(f"{CONFIG_FILE}: build.builder is a machine name, or a table of them keyed by "
                          "the hostname nixconvoy runs on")
    else:
        builder = {}
        for k, v in raw_builder.items():
            if not isinstance(v, str):
                raise ConfigError(f"{CONFIG_FILE}: build.builder.{k} must be a string")
            builder[k] = v
    skip = []
    for s in _strs(build, "skip_substituters", "build"):
        if s.split() != [s]:
            raise ConfigError(f"{CONFIG_FILE}: build.skip_substituters entry {s!r} is not a substituter URL")
        skip.append(_bare_url(s))
    targets: dict[str, TargetConfig] = {}
    for name, h in _table(doc.get("targets", {}), "targets").items():
        t = _table(h, f"targets.{name}")
        for k in t:
            if k not in ("ssh_address", "tags"):
                raise ConfigError(f"{CONFIG_FILE}: unknown key targets.{name}.{k}")
        targets[name] = TargetConfig(ssh_address=_str(t, "ssh_address", f"targets.{name}"),
                                     tags=_strs(t, "tags", f"targets.{name}"))
    submodules: dict[str, SubmoduleConfig] = {}
    for name, a in _table(doc.get("submodules", {}), "submodules").items():
        t = _table(a, f"submodules.{name}")
        for k in t:
            if k not in ("repo", "pin_file"):
                raise ConfigError(f"{CONFIG_FILE}: unknown key submodules.{name}.{k}")
        repo = _str(t, "repo", f"submodules.{name}")
        if not repo:
            raise ConfigError(f"{CONFIG_FILE}: submodules.{name}.repo is required")
        pin_file = _str(t, "pin_file", f"submodules.{name}")
        if pin_file.startswith("/") or ".." in pin_file.split("/"):
            raise ConfigError(f"{CONFIG_FILE}: submodules.{name}.pin_file must be a path inside the repository")
        submodules[name] = SubmoduleConfig(repo=repo, pin_file=pin_file)
    return Config(
        flake_dir=flake_dir,
        targets_dir=targets_dir,
        targets_arch=targets_arch,
        ssh_template=ssh_template,
        branch_prefix=branch_prefix,
        work_branches=tuple(pats),
        child_worktrees=children,
        precheck=_str(ev, "precheck", "eval"),
        parallel_eval=_auto_bool(ev, "parallel", "eval"),
        builder=builder,
        skip_substituters=tuple(skip),
        targets=targets,
        submodules=submodules,
    )


def load(root: Path) -> Config:
    try:
        text = (root / CONFIG_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"no {CONFIG_FILE} at {root}: `nixconvoy init` writes one") from None
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
            proc.die(str(e))
    return _CFG


def set_cfg(c: Config) -> None:
    global _CFG
    _CFG = c


def builder() -> str:
    """Where the closures are realised: NIXCONVOY_BUILDER, else the config's
    entry for this machine; empty means here."""
    env = os.environ.get("NIXCONVOY_BUILDER")
    if env is not None:
        return env
    me = proc.this_machine()
    b = cfg().builder.get(me, cfg().builder.get("*", ""))
    return "" if b == me else b
