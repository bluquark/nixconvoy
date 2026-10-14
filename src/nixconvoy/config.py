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
class Cache:
    """A binary cache of the fleet's own builds, asked before the public
    substituters - except when building on `served_by`, the builder whose store
    it serves (there its name may not even resolve to the cache)."""
    url: str
    served_by: str = ""


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
    # A subagent worktree of the main checkout owns "<prefix>-<name>";
    # {machine} is this machine's hostname.
    branch_prefix: str = "{machine}-deploys"
    # Where `open <name>` puts a task worktree, relative to the checkout
    # (gitignored: a nested worktree); any worktree nested in a checkout
    # is one of its task worktrees, wherever it was made.
    worktrees: str = "worktrees"
    # A flake check (checks.<system>.<attr>) proven before any build,
    # memoized on the git object ids of the files matching `globs`.
    precheck_attr: str = ""
    precheck_globs: tuple[str, ...] = ()
    # The overlay whose attributes map one-to-one onto files, so a target's
    # eval memo keys only on the attribute files its modules name:
    # `<x> = ... ./<file>.nix` lines, named as pkgs.<namespace>.<x>.
    overlay: str = ""
    overlay_namespace: str = ""
    # Determinate Nix's parallel evaluation (builtins.parallel, --eval-cores).
    parallel_eval: bool = False
    # The builder the closures are realised on, by sys.platform ("darwin",
    # "linux"); absent means here.
    builder: dict[str, str] = field(default_factory=dict)
    build_unreachable_hint: str = ""
    caches: tuple[Cache, ...] = ()
    public_substituters: str = "https://cache.nixos.org"
    targets: dict[str, TargetConfig] = field(default_factory=dict)
    # The flake inputs developed in place (annex.py): input name -> the
    # repository's checkout on this machine, whose origin the lock fetches.
    annexes: dict[str, str] = field(default_factory=dict)

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

    def build_substituters(self, builder: str) -> str:
        own = [c.url for c in self.caches if c.served_by != builder]
        return " ".join([*own, self.public_substituters])


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


def _strs(t: dict[str, object], k: str, where: str) -> tuple[str, ...]:
    v = t.get(k, [])
    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
        raise ConfigError(f"{where}.{k} must be a list of strings")
    return tuple(v)


# The keys the target/builder/machine vocabulary renamed, for the error a
# config still using one gets.
_RENAMED = {"target": "ssh_address (over {target}, not {host})", "hosts": "targets",
            "build.host": "build.builder"}


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
    known = {"version", "flake", "hosts_dir", "system", "ssh_address", "branch_prefix", "worktrees",
             "precheck", "memo", "eval", "build", "targets", "annexes"}
    for k in doc:
        if k not in known:
            raise ConfigError(f"{CONFIG_FILE}: unknown key {k}{_renamed(k)}")
    pre = _table(doc.get("precheck", {}), "precheck")
    memo = _table(doc.get("memo", {}), "memo")
    ev = _table(doc.get("eval", {}), "eval")
    build = _table(doc.get("build", {}), "build")
    if "host" in build:
        raise ConfigError(f"{CONFIG_FILE}: unknown key build.host{_renamed('build.host')}")
    raw_builder = build.get("builder", {})
    if isinstance(raw_builder, str):
        builder = {"linux": raw_builder, "darwin": raw_builder}
    else:
        builder = {k: v for k, v in _table(raw_builder, "build.builder").items() if isinstance(v, str)}
    caches = []
    raw_caches = build.get("caches", [])
    if not isinstance(raw_caches, list):
        raise ConfigError("build.caches must be an array of tables")
    for i, c in enumerate(raw_caches):
        t = _table(c, f"build.caches[{i}]")
        caches.append(Cache(_str(t, "url", "build.caches"), _str(t, "served_by", "build.caches")))
    targets: dict[str, TargetConfig] = {}
    for name, h in _table(doc.get("targets", {}), "targets").items():
        t = _table(h, f"targets.{name}")
        for k in t:
            if k not in ("local_only", "ssh_address", "tags"):
                raise ConfigError(f"{CONFIG_FILE}: unknown key targets.{name}.{k}{_renamed(k)}")
        targets[name] = TargetConfig(local_only=_bool(t, "local_only", f"targets.{name}"),
                                     ssh_address=_str(t, "ssh_address", f"targets.{name}"),
                                     tags=_strs(t, "tags", f"targets.{name}"))
    annexes: dict[str, str] = {}
    for name, a in _table(doc.get("annexes", {}), "annexes").items():
        t = _table(a, f"annexes.{name}")
        for k in t:
            if k != "repo":
                raise ConfigError(f"{CONFIG_FILE}: unknown key annexes.{name}.{k}")
        repo = _str(t, "repo", f"annexes.{name}")
        if not repo:
            raise ConfigError(f"{CONFIG_FILE}: annexes.{name}.repo is required")
        annexes[name] = repo
    overlay = _str(memo, "overlay", "memo")
    namespace = _str(memo, "namespace", "memo")
    if bool(overlay) != bool(namespace):
        raise ConfigError("memo.overlay and memo.namespace go together")
    worktrees = _str(doc, "worktrees", CONFIG_FILE, "worktrees").strip("/")
    if not worktrees or ".." in worktrees.split("/"):
        raise ConfigError(f"{CONFIG_FILE}: worktrees is a directory inside the checkout")
    return Config(
        flake=_str(doc, "flake", CONFIG_FILE, "."),
        hosts_dir=_str(doc, "hosts_dir", CONFIG_FILE),
        system=_str(doc, "system", CONFIG_FILE, "x86_64-linux"),
        ssh_template=_template(doc, "ssh_address", "{target}", "target"),
        branch_prefix=_template(doc, "branch_prefix", "{machine}-deploys", "machine"),
        worktrees=worktrees,
        precheck_attr=_str(pre, "attr", "precheck"),
        precheck_globs=_strs(pre, "globs", "precheck"),
        overlay=overlay, overlay_namespace=namespace,
        parallel_eval=_bool(ev, "parallel", "eval"),
        builder=builder,
        build_unreachable_hint=_str(build, "unreachable_hint", "build"),
        caches=tuple(caches),
        public_substituters=_str(build, "public_substituters", "build", "https://cache.nixos.org"),
        targets=targets,
        annexes=annexes,
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
