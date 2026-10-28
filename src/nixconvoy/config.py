"""nixconvoy.toml at the repo root: the fleet-level settings the git-side
verbs need before any Nix runs, and the per-target facts a deploy needs before
probing.  The scalar keys are required - `nixconvoy init` writes them,
detected - and the sections are optional: absent means none."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import proc, strings
from .strings import CONFIG_FILE, MSG_FILE  # noqa: F401 - other modules import MSG_FILE from here
from .wire import RepoNames

# Where Claude Code puts a subagent worktree, and the plugin's run.sh one
# in a repository without nixconvoy.toml: the place `open --child` and
# `hook` take there when run by hand.
CLAUDE_WORKTREES = ".claude/worktrees"


@dataclass(frozen=True)
class SwitchPolicy:
    """How a target is switched: nixconvoy.toml's `[switch]` table, the
    fleet's defaults, under a target's `[targets.<name>.switch]`."""

    # Confirmed (nix/kit.nix's helper arms a rollback timer, the toplevel
    # is activated with `test`, verified from here, then the profile set),
    # or plainly, as nixos-rebuild switch would.
    confirmed: bool = True
    # Seconds after arming before an unconfirmed switch rolls back: the
    # activation plus the verification must fit.
    deadline: int = 30
    # The verification begins with an HTTPS probe of the internet from the
    # target.
    internet_check: bool = False
    # Bash the helper's `check` verb runs as root (errexit) after that
    # probe: the target's own assertions on the new system, whose last
    # stderr line is what a failed verification reports.
    checks: str = ""
    # Switched alone after every other target, verified only while this
    # machine still reaches the internet (the router).  Per target only.
    last: bool = False


@dataclass(frozen=True)
class TargetConfig:
    # ssh address, overriding Config.ssh_address's template.
    ssh_address: str = ""
    # `@tag` on the command line names every target carrying it.
    tags: tuple[str, ...] = ()
    # The repo directory the target's configuration lives in, the eval
    # memo's scan root for it (Config.target_root); empty: <targets_dir>/<name>,
    # or the memo's own inference (memo.hosts_candidates).
    dir: str = ""
    # Its [targets.<name>.switch] over the [switch] defaults; None: the defaults.
    switch: SwitchPolicy | None = None



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
    # relative to the repo root; empty: the `[targets.<name>]` tables name
    # them instead.
    targets_dir: str
    # The targets' system (checks.<targets_arch>.<precheck> is the precheck).
    targets_arch: str
    # A target's ssh address (user@name): nixconvoy.toml's
    # ssh_address_template, over {target}; the store host is addressed through it too.
    ssh_template: str
    # A checkout owns "<prefix><leaf>", its subagent worktrees "<prefix><leaf>-<name>"
    # (the main checkout's "<prefix>main-<name>"); {machine}, when the
    # template has it, is this machine's hostname.  Also the shape of every
    # work branch nixconvoy unions, {machine} standing for any hostname
    # (checkout.listed, the plan cores' `listed`): a pushed branch not
    # shaped so is ignored.
    branch_prefix: str
    # Where `open --child <name>` puts a child (subagent) worktree, relative
    # to the checkout.  A nested worktree must be ignored (open writes it
    # into .git/info/exclude); any worktree nested in a checkout is one of
    # its children, wherever it was made.
    child_worktrees: str
    # Determinate Nix's parallel evaluation (builtins.parallel, --eval-cores):
    # None is "auto", on when the nix here knows the feature.
    parallel_eval: bool | None
    # The remote origin/main and the work branches live on, and main's name
    # there: `remote` and `main_branch` (origin and main unless set).
    remote: str = "origin"
    main_branch: str = "main"
    # eval.precheck: a flake check (checks.<targets_arch>.<precheck>) proven
    # before any build; empty is none.
    precheck: str = ""
    # The machine whose store the closures are realised in, by the hostname
    # nixconvoy runs on ("*": any other); a machine mapped to itself, or to
    # nothing, realises them here.  Its nix daemon builds them or hands them
    # on to its own remote builders (nix.conf's builders), and the closures
    # are copied to the targets from it.  Addressed as a target (ssh_address).
    store: dict[str, str] = field(default_factory=dict)
    # Substituters a deploy build leaves out of the store host's own
    # configured list (targets.build_cmd): the caches themselves are each
    # store host's nix.conf's business.
    skip_substituters: tuple[str, ...] = ()
    # [switch]: every target's switch policy unless its table overrides.
    switch: SwitchPolicy = field(default_factory=SwitchPolicy)
    targets: dict[str, TargetConfig] = field(default_factory=dict)
    # The flake inputs developed in place (submodule.py), by input name.
    submodules: dict[str, SubmoduleConfig] = field(default_factory=dict)

    def target(self, name: str) -> TargetConfig:
        return self.targets.get(name, TargetConfig())

    def ssh_address(self, name: str) -> str:
        return self.target(name).ssh_address or self.ssh_template.format(target=name)

    def switch_policy(self, name: str) -> SwitchPolicy:
        return self.target(name).switch or self.switch

    def tagged(self, tag: str) -> list[str]:
        return [n for n, h in self.targets.items() if tag in h.tags]

    @property
    def targets_path(self) -> str:
        return os.path.normpath(self.targets_dir) if self.targets_dir else ""

    @property
    def inventory(self) -> list[str]:
        """The targets `[targets.<name>]` names; empty with targets_dir set
        (targets.flake_target_names lists its subdirectories then).  A
        store host that is no target has no table: the ssh address
        template (or ~/.ssh/config) addresses it."""
        return [] if self.targets_dir else sorted(self.targets)

    def target_root(self, name: str) -> str | None:
        """The repo directory holding `name`'s configuration, where the eval
        memo's scan of the target starts: its `dir`, else
        <targets_dir>/<name>; None when neither names one (the memo infers
        one, else starts at flake.nix alone)."""
        t = self.target(name)
        if t.dir:
            return os.path.normpath(t.dir)
        return f"{self.targets_path}/{name}" if self.targets_dir else None

    @property
    def flake_path(self) -> str:
        return os.path.normpath(self.flake_dir)

    def flake_file(self, name: str) -> str:
        """A repo path inside the flake directory (flake.nix, flake.lock)."""
        return os.path.normpath(f"{self.flake_dir}/{name}")

    @property
    def names(self) -> RepoNames:
        return RepoNames(self.remote, self.main_branch)


class ConfigError(Exception):
    pass


def valid_remote(name: str) -> bool:
    """A git remote's name as nixconvoy addresses it: letters, digits, '.',
    '-' and '_', not starting with '-' (an option) and not '.' or '..'."""
    return bool(re.fullmatch(r"\w[\w.-]*", name)) and name not in (".", "..")


def valid_branch(name: str, remote: str) -> bool:
    """A branch's name, not a ref or a remote-tracking name (<remote>/x)."""
    return bool(re.fullmatch(r"\w[\w./-]*", name)) and ".." not in name and "//" not in name \
        and not name.endswith("/") and not name.startswith(("refs/", f"{remote}/"))


def _table(v: object, where: str) -> dict[str, object]:
    if not isinstance(v, dict):
        raise ConfigError(strings.CONFIG_NOT_A_TABLE(where=where))
    return v


def _required(t: dict[str, object], k: str, where: str) -> object:
    if k not in t:
        key = k if where == CONFIG_FILE else f"{where}.{k}"
        raise ConfigError(strings.CONFIG_REQUIRED(key=key))
    return t[k]


def _str(t: dict[str, object], k: str, where: str, default: str | None = "") -> str:
    """A string key; default None makes it required."""
    v = _required(t, k, where) if default is None else t.get(k, default)
    if not isinstance(v, str):
        raise ConfigError(strings.CONFIG_NOT_A_STRING(where=where, key=k))
    return v


def _auto_bool(t: dict[str, object], k: str, where: str) -> bool | None:
    v = _required(t, k, where)
    if v == "auto":
        return None
    if not isinstance(v, bool):
        raise ConfigError(strings.CONFIG_NOT_AUTO_BOOL(where=where, key=k))
    return v


def _bool(t: dict[str, object], k: str, where: str, default: bool) -> bool:
    v = t.get(k, default)
    if not isinstance(v, bool):
        raise ConfigError(strings.CONFIG_NOT_A_BOOL(where=where, key=k))
    return v


def _switch(t: dict[str, object], where: str, base: SwitchPolicy, per_target: bool) -> SwitchPolicy:
    """A `switch` table over `base`: `[switch]` (the defaults; `last` is a
    target's alone) or a `[targets.<name>.switch]`."""
    keys = ("confirmed", "deadline", "internet_check", "checks", *(("last",) if per_target else ()))
    for k in t:
        if k not in keys:
            unknown = strings.CONFIG_UNKNOWN_KEY_LAST if k == "last" else strings.CONFIG_UNKNOWN_KEY
            raise ConfigError(unknown(key=f"{where}.{k}"))
    d = t.get("deadline", base.deadline)
    if isinstance(d, bool) or not isinstance(d, int) or d <= 0:
        raise ConfigError(strings.CONFIG_DEADLINE(where=where))
    return SwitchPolicy(confirmed=_bool(t, "confirmed", where, base.confirmed), deadline=d,
                        internet_check=_bool(t, "internet_check", where, base.internet_check),
                        checks=_str(t, "checks", where, base.checks),
                        last=_bool(t, "last", where, base.last))


def _strs(t: dict[str, object], k: str, where: str) -> tuple[str, ...]:
    v = t.get(k, [])
    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
        raise ConfigError(strings.CONFIG_NOT_STRINGS(where=where, key=k))
    return tuple(v)


def _bare_url(u: str) -> str:
    """A substituter URL as nix.conf's spellings of it compare: without its
    query (?priority=...) and trailing slashes."""
    return u.split("?")[0].rstrip("/")


_noted_work_branches: list[bool] = []


def _template(t: dict[str, object], k: str, placeholder: str) -> str:
    """A required template setting, proven here to format with
    `placeholder` alone, so a stale placeholder fails as a ConfigError
    rather than in a probe."""
    v = _str(t, k, CONFIG_FILE, None)
    try:
        v.format(**{placeholder: "x"})
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError(strings.CONFIG_NOT_A_TEMPLATE(key=k, placeholder=placeholder, error=e)) from e
    return v


def parse(text: str) -> Config:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(strings.CONFIG_TOML(error=e)) from e
    version = _required(doc, "config_version", CONFIG_FILE)
    if isinstance(version, bool) or version != 1:
        raise ConfigError(strings.CONFIG_VERSION(version=version))
    known = {"config_version", "flake_dir", "targets_dir", "targets_arch", "ssh_address_template", "branch_prefix",
             "child_worktrees", "remote", "main_branch", "eval", "build", "switch", "targets", "submodules",
             "work_branches"}
    if "work_branches" in doc and not _noted_work_branches:
        # The allowlist this once had, superseded by branch_prefix's shape:
        # a file from before still loads, told so once per run.
        _noted_work_branches.append(True)
        print(strings.CONFIG_WORK_BRANCHES_IGNORED(), file=sys.stderr)
    for k in doc:
        if k not in known:
            raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=k))
    # The required keys in the file's order, so a hand-written file is
    # told of its first gap first.
    flake_dir = _str(doc, "flake_dir", CONFIG_FILE, None)
    targets_dir = _str(doc, "targets_dir", CONFIG_FILE)
    targets_arch = _str(doc, "targets_arch", CONFIG_FILE, None)
    ssh_template = _template(doc, "ssh_address_template", "target")
    branch_prefix = _template(doc, "branch_prefix", "machine")
    # ASCII, and braces only as {machine}: the nix side (a deploy-time
    # regex over it) and the shape matchers read it verbatim.
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch_prefix.replace("{machine}", "")):
        raise ConfigError(strings.CONFIG_BRANCH_PREFIX_CHARS(branch_prefix=branch_prefix))
    children = _str(doc, "child_worktrees", CONFIG_FILE, None).strip("/")
    if not children or ".." in children.split("/"):
        raise ConfigError(strings.CONFIG_CHILD_WORKTREES())
    remote = _str(doc, "remote", CONFIG_FILE, "origin")
    if not valid_remote(remote):
        raise ConfigError(strings.CONFIG_REMOTE(remote=remote))
    main_branch = _str(doc, "main_branch", CONFIG_FILE, "main")
    if not valid_branch(main_branch, remote):
        raise ConfigError(strings.CONFIG_MAIN_BRANCH(remote=remote, main_branch=main_branch))
    ev = _table(_required(doc, "eval", CONFIG_FILE), "eval")
    for k in ev:
        if k not in ("parallel", "precheck"):
            raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"eval.{k}"))
    build = _table(doc.get("build", {}), "build")
    for k in build:
        if k not in ("store", "builder", "skip_substituters"):
            raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"build.{k}"))
    # `builder` is the key's older name, still read.
    if "store" in build and "builder" in build:
        raise ConfigError(strings.CONFIG_STORE_AND_BUILDER())
    key = "builder" if "builder" in build else "store"
    raw_store = build.get(key, {})
    if isinstance(raw_store, str):
        store = {"*": raw_store}
    elif not isinstance(raw_store, dict):
        raise ConfigError(strings.CONFIG_STORE_TYPE(key=key))
    else:
        store = {}
        for k, v in raw_store.items():
            if not isinstance(v, str):
                raise ConfigError(strings.CONFIG_STORE_ENTRY(key=key, name=k))
            store[k] = v
    skip = []
    for s in _strs(build, "skip_substituters", "build"):
        if s.split() != [s]:
            raise ConfigError(strings.CONFIG_SKIP_SUBSTITUTER(entry=s))
        skip.append(_bare_url(s))
    switch = _switch(_table(doc.get("switch", {}), "switch"), "switch", SwitchPolicy(), per_target=False)
    targets: dict[str, TargetConfig] = {}
    for name, h in _table(doc.get("targets", {}), "targets").items():
        t = _table(h, f"targets.{name}")
        for k in t:
            if k not in ("ssh_address", "tags", "dir", "switch"):
                raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"targets.{name}.{k}"))
        own = (_switch(_table(t["switch"], f"targets.{name}.switch"), f"targets.{name}.switch", switch, per_target=True)
               if "switch" in t else None)
        d = _str(t, "dir", f"targets.{name}")
        if d.startswith("/") or ".." in d.split("/"):
            raise ConfigError(strings.CONFIG_TARGET_DIR(name=name, dir=d))
        d = os.path.normpath(d) if d else ""
        if d == ".":
            raise ConfigError(strings.CONFIG_TARGET_DIR_ROOT(name=name))
        targets[name] = TargetConfig(ssh_address=_str(t, "ssh_address", f"targets.{name}"),
                                     tags=_strs(t, "tags", f"targets.{name}"), dir=d, switch=own)
    if not targets_dir and not targets:
        raise ConfigError(strings.CONFIG_NO_TARGETS())
    submodules: dict[str, SubmoduleConfig] = {}
    for name, a in _table(doc.get("submodules", {}), "submodules").items():
        t = _table(a, f"submodules.{name}")
        for k in t:
            if k not in ("repo", "pin_file"):
                raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"submodules.{name}.{k}"))
        repo = _str(t, "repo", f"submodules.{name}")
        if not repo:
            raise ConfigError(strings.CONFIG_SUBMODULE_REPO(name=name))
        pin_file = _str(t, "pin_file", f"submodules.{name}")
        if pin_file.startswith("/") or ".." in pin_file.split("/"):
            raise ConfigError(strings.CONFIG_SUBMODULE_PIN_FILE(name=name))
        submodules[name] = SubmoduleConfig(repo=repo, pin_file=pin_file)
    return Config(
        flake_dir=flake_dir,
        targets_dir=targets_dir,
        targets_arch=targets_arch,
        ssh_template=ssh_template,
        branch_prefix=branch_prefix,
        child_worktrees=children,
        remote=remote,
        main_branch=main_branch,
        precheck=_str(ev, "precheck", "eval"),
        parallel_eval=_auto_bool(ev, "parallel", "eval"),
        store=store,
        skip_substituters=tuple(skip),
        switch=switch,
        targets=targets,
        submodules=submodules,
    )


def load(root: Path) -> Config:
    try:
        text = (root / CONFIG_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(strings.CONFIG_MISSING(root=root)) from None
    return parse(text)


_CFG: Config | None = None
# The invoking checkout's root (set_root, by cli.main's chdir), the one
# tree the configuration is read from: a run that moves into other
# checkouts (pull, land, close) keeps reading its own.
_ROOT: Path | None = None


def set_root(root: Path) -> None:
    global _ROOT
    _ROOT = root


def root() -> Path:
    return _ROOT if _ROOT is not None else Path.cwd()


def cfg() -> Config:
    """The current repo's configuration, read once from root()."""
    global _CFG
    if _CFG is None:
        try:
            _CFG = load(root())
        except ConfigError as e:
            proc.die(str(e))
    return _CFG


def set_cfg(c: Config) -> None:
    global _CFG
    _CFG = c


def repo_names() -> RepoNames:
    """The remote and main branch of the repository the cores and the git
    calls address (nixconvoy.toml's `remote` and `main_branch`)."""
    return cfg().names


def names_at(root: Path) -> RepoNames:
    """The names of the repository at `root`: its nixconvoy.toml's, else
    origin and main (a submodule's repository, any plain one)."""
    try:
        return load(root).names
    except (ConfigError, OSError, UnicodeDecodeError):
        return RepoNames("origin", "main")


def main_ref() -> str:
    return cfg().names.main_ref


def main_short() -> str:
    return cfg().names.main_short


def remote_ref(branch: str) -> str:
    return cfg().names.remote_ref(branch)


def store_host() -> str:
    """The machine whose store the closures are realised in: NIXCONVOY_STORE,
    else the config's entry for this machine; empty means here."""
    env = os.environ.get("NIXCONVOY_STORE")
    if env is not None:
        return env
    me = proc.this_machine()
    b = cfg().store.get(me, cfg().store.get("*", ""))
    return "" if b == me else b
