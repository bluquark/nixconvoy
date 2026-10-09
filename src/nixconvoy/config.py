"""nixconvoy.toml at the repo root: the fleet-level settings the git-side
verbs need before any Nix runs, and the per-host facts a deploy needs before
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
    substituters - except when building on `served_by`, the host whose store
    it serves (there its name may not even resolve to the cache)."""
    url: str
    served_by: str = ""


@dataclass(frozen=True)
class HostConfig:
    # Deployable only from itself: never probed or named from elsewhere.
    local_only: bool = False
    # ssh target, overriding Config.target's template.
    target: str = ""


@dataclass(frozen=True)
class Config:
    # The directory holding flake.nix, relative to the repo root.
    flake: str = "."
    # The directory whose subdirectories name the nixosConfigurations.
    hosts_dir: str = ""
    system: str = "x86_64-linux"
    # The ssh target of a host: a template over {host}.
    target: str = "{host}"
    # A subagent worktree of the main checkout owns "<prefix>-<name>".
    branch_prefix: str = "{host}-deploys"
    # A flake check (checks.<system>.<attr>) proven before any host build,
    # memoized on the git object ids of the files matching `globs`.
    precheck_attr: str = ""
    precheck_globs: tuple[str, ...] = ()
    # The overlay whose attributes map one-to-one onto files, so a host's
    # eval memo keys only on the attribute files its modules name:
    # `<x> = ... ./<file>.nix` lines, named as pkgs.<namespace>.<x>.
    overlay: str = ""
    overlay_namespace: str = ""
    # Determinate Nix's parallel evaluation (builtins.parallel, --eval-cores).
    parallel_eval: bool = False
    # Where the closures are realised, by sys.platform ("darwin", "linux");
    # absent means here.
    build_host: dict[str, str] = field(default_factory=dict)
    build_unreachable_hint: str = ""
    caches: tuple[Cache, ...] = ()
    public_substituters: str = "https://cache.nixos.org"
    hosts: dict[str, HostConfig] = field(default_factory=dict)

    def host(self, name: str) -> HostConfig:
        return self.hosts.get(name, HostConfig())

    def ssh_target(self, name: str) -> str:
        return self.host(name).target or self.target.format(host=name)

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


def parse(text: str) -> Config:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{CONFIG_FILE}: {e}") from e
    version = doc.get("version", 1)
    if version != 1:
        raise ConfigError(f"{CONFIG_FILE}: version {version} is not supported (1 is)")
    known = {"version", "flake", "hosts_dir", "system", "target", "branch_prefix",
             "precheck", "memo", "eval", "build", "hosts"}
    for k in doc:
        if k not in known:
            raise ConfigError(f"{CONFIG_FILE}: unknown key {k}")
    pre = _table(doc.get("precheck", {}), "precheck")
    memo = _table(doc.get("memo", {}), "memo")
    ev = _table(doc.get("eval", {}), "eval")
    build = _table(doc.get("build", {}), "build")
    raw_host = build.get("host", {})
    if isinstance(raw_host, str):
        build_host = {"linux": raw_host, "darwin": raw_host}
    else:
        build_host = {k: v for k, v in _table(raw_host, "build.host").items() if isinstance(v, str)}
    caches = []
    raw_caches = build.get("caches", [])
    if not isinstance(raw_caches, list):
        raise ConfigError("build.caches must be an array of tables")
    for i, c in enumerate(raw_caches):
        t = _table(c, f"build.caches[{i}]")
        caches.append(Cache(_str(t, "url", "build.caches"), _str(t, "served_by", "build.caches")))
    hosts: dict[str, HostConfig] = {}
    for name, h in _table(doc.get("hosts", {}), "hosts").items():
        t = _table(h, f"hosts.{name}")
        hosts[name] = HostConfig(local_only=_bool(t, "local_only", f"hosts.{name}"),
                                 target=_str(t, "target", f"hosts.{name}"))
    overlay = _str(memo, "overlay", "memo")
    namespace = _str(memo, "namespace", "memo")
    if bool(overlay) != bool(namespace):
        raise ConfigError("memo.overlay and memo.namespace go together")
    return Config(
        flake=_str(doc, "flake", CONFIG_FILE, "."),
        hosts_dir=_str(doc, "hosts_dir", CONFIG_FILE),
        system=_str(doc, "system", CONFIG_FILE, "x86_64-linux"),
        target=_str(doc, "target", CONFIG_FILE, "{host}"),
        branch_prefix=_str(doc, "branch_prefix", CONFIG_FILE, "{host}-deploys"),
        precheck_attr=_str(pre, "attr", "precheck"),
        precheck_globs=_strs(pre, "globs", "precheck"),
        overlay=overlay, overlay_namespace=namespace,
        parallel_eval=_bool(ev, "parallel", "eval"),
        build_host=build_host,
        build_unreachable_hint=_str(build, "unreachable_hint", "build"),
        caches=tuple(caches),
        public_substituters=_str(build, "public_substituters", "build", "https://cache.nixos.org"),
        hosts=hosts,
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


def build_host() -> str:
    """Where the closures are realised: NIXCONVOY_BUILD_HOST, else the config's
    entry for this platform; empty means here."""
    env = os.environ.get("NIXCONVOY_BUILD_HOST")
    if env is not None:
        return env
    return cfg().build_host.get("darwin" if sys.platform == "darwin" else "linux", "")
