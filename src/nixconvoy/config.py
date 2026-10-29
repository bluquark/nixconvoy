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


CHECK_KINDS = ("ssh", "internet", "routing", "bash")


@dataclass(frozen=True)
class Check:
    """One auto_rollback_checks item: `ssh` (each verification round's
    read of the running system opens a new ssh connection, so sshd must
    admit a login; without it the deploy's master connection serves),
    `internet` (an HTTPS
    probe from the target), `routing` (one from this machine: the target
    is the deploy's own way out, the router) or `bash` (`script`, run as
    root on the target with errexit by the helper's `check` verb after its
    probe, its last stderr line what a failed verification reports)."""

    kind: str
    script: str = ""


@dataclass(frozen=True)
class SwitchPolicy:
    """How a target is switched: nixconvoy.toml's `[switch]` table, the
    fleet's defaults, under a target's `[targets.<name>.switch]`."""

    # Seconds after arming before an unconfirmed switch rolls back: the
    # activation plus the verification must fit.
    deadline: int = 30
    # auto_rollback_checks: the fleet's, then the target's own appended (a
    # `false` item of a kind strikes the fleet's of it).  Any: the switch
    # is confirmed (nix/kit.nix's helper arms a rollback timer, the
    # toplevel is activated with `test`, verified from here by each check,
    # then the profile set); none: plain, as nixos-rebuild switch would.
    checks: tuple[Check, ...] = (Check("ssh"),)

    @property
    def confirmed(self) -> bool:
        return bool(self.checks)

    def has(self, kind: str) -> bool:
        return any(c.kind == kind for c in self.checks)

    @property
    def script(self) -> str:
        """The bash checks, in order, as the one errexit script the kit
        carries: each in a subshell of its own, so one's early `exit 0` or
        `set +e` reaches no other."""
        return "\n".join(f"(\n{c.script}\n)" for c in self.checks if c.kind == "bash")


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
class HostnameConfig:
    """A `[hostnames.<hostname>]` table: what nixconvoy does differently
    when run on that machine (proc.this_machine)."""

    # The target this machine is, when its hostname is not the target's
    # name: that target is probed, built for and switched here, without
    # ssh.  None: the hostname itself.
    target: str | None = None
    # The store host for this machine, over `[build] store`.
    store: str | None = None
    # Determinate Nix's parallel evaluation (builtins.parallel, --eval-cores):
    # None is "auto", on when the nix here knows the feature.
    parallel_eval: bool | None = None


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
    # The remote origin/main and the work branches live on, and main's name
    # there: `remote` and `main_branch` (origin and main unless set).
    remote: str = "origin"
    main_branch: str = "main"
    # precheck: a flake check (checks.<targets_arch>.<precheck>) proven
    # before any build; empty is none.
    precheck: str = ""
    # [build] store: the machine whose store the closures are realised in,
    # unless the hostname's table says otherwise; a machine mapped to
    # itself, or to nothing, realises them here.  Its nix daemon builds
    # them or hands them on to its own remote builders (nix.conf's
    # builders), and the closures are copied to the targets from it.
    # Addressed as a target (ssh_address).
    store: str = ""
    hostnames: dict[str, HostnameConfig] = field(default_factory=dict)
    # [switch]: every target's switch policy unless its table overrides.
    switch: SwitchPolicy = field(default_factory=SwitchPolicy)
    targets: dict[str, TargetConfig] = field(default_factory=dict)

    def target(self, name: str) -> TargetConfig:
        return self.targets.get(name, TargetConfig())

    def hostname(self, name: str) -> HostnameConfig:
        return self.hostnames.get(name, HostnameConfig())

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


def _checks(v: object, where: str, base: tuple[Check, ...]) -> tuple[Check, ...]:
    """An auto_rollback_checks list appended to `base` (the fleet's; the
    built-in default for `[switch]` itself, which it replaces): each item
    a table with one key of CHECK_KINDS - true (bash: the script) adds the
    check, false strikes `base`'s of that kind."""
    if not isinstance(v, list):
        raise ConfigError(strings.CONFIG_CHECKS(where=where))
    kept = list(base)
    own: list[Check] = []
    for i, item in enumerate(v):
        at = f"{where}[{i}]"
        if not isinstance(item, dict) or len(item) != 1:
            raise ConfigError(strings.CONFIG_CHECK_ITEM(where=at))
        ((kind, val),) = item.items()
        if kind not in CHECK_KINDS:
            raise ConfigError(strings.CONFIG_CHECK_ITEM(where=at))
        if val is False:
            kept = [c for c in kept if c.kind != kind]
        elif kind == "bash" and isinstance(val, str):
            own.append(Check(kind, val))
        elif kind != "bash" and val is True:
            own.append(Check(kind))
        else:
            raise ConfigError(strings.CONFIG_CHECK_ITEM(where=at))
    return (*kept, *own)


def _switch(t: dict[str, object], where: str, base: SwitchPolicy | None) -> SwitchPolicy:
    """A `switch` table: `[switch]` (`base` None: the fleet's defaults
    over the built-in ones) or a `[targets.<name>.switch]` over the
    fleet's."""
    for k in t:
        if k not in ("deadline_secs", "auto_rollback_checks"):
            raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"{where}.{k}"))
    fleet = base or SwitchPolicy()
    d = t.get("deadline_secs", fleet.deadline)
    if isinstance(d, bool) or not isinstance(d, int) or d <= 0:
        raise ConfigError(strings.CONFIG_DEADLINE(where=where))
    checks = fleet.checks
    if "auto_rollback_checks" in t:
        checks = _checks(t["auto_rollback_checks"], f"{where}.auto_rollback_checks", fleet.checks if base else ())
    return SwitchPolicy(deadline=d, checks=checks)


def _strs(t: dict[str, object], k: str, where: str) -> tuple[str, ...]:
    v = t.get(k, [])
    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
        raise ConfigError(strings.CONFIG_NOT_STRINGS(where=where, key=k))
    return tuple(v)


_noted_work_branches: list[bool] = []
_noted_submodules: list[bool] = []


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
             "child_worktrees", "remote", "main_branch", "precheck", "build", "hostnames", "switch", "targets",
             "submodules", "work_branches"}
    if "work_branches" in doc and not _noted_work_branches:
        # The allowlist this once had, superseded by branch_prefix's shape:
        # a file from before still loads, told so once per run.
        _noted_work_branches.append(True)
        print(strings.CONFIG_WORK_BRANCHES_IGNORED(), file=sys.stderr)
    if "submodules" in doc and not _noted_submodules:
        # The repositories this once named by path: now the main
        # checkout's submodules, every .gitmodules entry one (submodule.py).
        _noted_submodules.append(True)
        print(strings.CONFIG_SUBMODULES_IGNORED(), file=sys.stderr)
    if "eval" in doc:
        raise ConfigError(strings.CONFIG_EVAL_GONE())
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
    build = _table(doc.get("build", {}), "build")
    for k in build:
        if k not in ("store",):
            raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"build.{k}"))
    if not isinstance(build.get("store", ""), str):
        raise ConfigError(strings.CONFIG_STORE_TYPE())
    hostnames: dict[str, HostnameConfig] = {}
    for name, h in _table(doc.get("hostnames", {}), "hostnames").items():
        t = _table(h, f"hostnames.{name}")
        for k in t:
            if k not in ("target", "store", "parallel_eval"):
                raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"hostnames.{name}.{k}"))
        hostnames[name] = HostnameConfig(
            target=_str(t, "target", f"hostnames.{name}") or None,
            store=_str(t, "store", f"hostnames.{name}") if "store" in t else None,
            parallel_eval=_auto_bool(t, "parallel_eval", f"hostnames.{name}") if "parallel_eval" in t else None)
    switch = _switch(_table(doc.get("switch", {}), "switch"), "switch", None)
    targets: dict[str, TargetConfig] = {}
    for name, h in _table(doc.get("targets", {}), "targets").items():
        t = _table(h, f"targets.{name}")
        for k in t:
            if k not in ("ssh_address", "tags", "dir", "switch"):
                raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"targets.{name}.{k}"))
        own = (_switch(_table(t["switch"], f"targets.{name}.switch"), f"targets.{name}.switch", switch)
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
    return Config(
        flake_dir=flake_dir,
        targets_dir=targets_dir,
        targets_arch=targets_arch,
        ssh_template=ssh_template,
        branch_prefix=branch_prefix,
        child_worktrees=children,
        remote=remote,
        main_branch=main_branch,
        precheck=_str(doc, "precheck", CONFIG_FILE),
        store=_str(build, "store", "build"),
        hostnames=hostnames,
        switch=switch,
        targets=targets,
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


def this_target() -> str:
    """The target this machine is: its hostname's `target`, else the
    hostname itself (a machine that is no target matches none)."""
    me = proc.this_machine()
    return cfg().hostname(me).target or me


def store_host() -> str:
    """The machine whose store the closures are realised in: this
    hostname's `store`, else `[build] store`; empty means here."""
    me = proc.this_machine()
    h = cfg().hostname(me)
    b = cfg().store if h.store is None else h.store
    return "" if b in (me, this_target()) else b


def parallel_eval() -> bool | None:
    """This hostname's parallel_eval; None is auto."""
    return cfg().hostname(proc.this_machine()).parallel_eval
