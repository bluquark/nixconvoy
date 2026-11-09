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


# nixconvoy.toml's [checks] table: the pre_switch and post_switch phases,
# each a table of check kinds (a shell kind's list of statements, `flake`'s
# list of the flake's check names, `internet = true`), the fleet's under
# [checks.<phase>], a target's own appended under
# [targets.<name>.checks.<phase>] (a kind set to false there strikes the
# fleet's).  A kind belongs to the phase that can act on it: `flake` to
# pre_switch (its result is the tree's alone), `internet` to post_switch.
PRE, POST = "pre_switch", "post_switch"
SHELL_KINDS = ("target_shell", "controller_shell")
PRE_KINDS = ("flake", *SHELL_KINDS)
POST_KINDS = ("internet", *SHELL_KINDS)
# The pre-switch kinds that see the tree alone: run once per deploy beside
# eval, so only the fleet's table may hold them.
TREE_KINDS = ("flake", "controller_shell")
DEADLINE_KEY = "ssh_deadline_secs"
# The deadline of a nixconvoy.toml without [checks.post_switch]; nixconvoy init writes it out.
DEFAULT_DEADLINE = 10
# What every shell statement runs under: a failing command, an unset
# variable or a failing pipe stage fails the check.
SHELL_PRELUDE = "set -eu -o pipefail"
# Former kinds, each with what replaces it.
GONE_KINDS = {
    "routing": "a controller_shell statement reaching the internet from this machine, such as "
               f"controller_shell = ['{strings.PROBE}']",
    "bash": 'target_shell = ["..."] (a list of statements)',
}


def shell_script(statement: str) -> str:
    """`statement` as the script its shell runs: SHELL_PRELUDE first."""
    return f"{SHELL_PRELUDE}\n{statement}\n"


def controller_shell() -> str:
    """The shell this machine's checks run in: the user's own."""
    return os.environ.get("SHELL") or "/bin/sh"


@dataclass(frozen=True)
class Check:
    """One check of a phase: `flake` (`script`: a flake check's name, built
    beside eval and proven per tree), `internet` (an HTTPS probe from the
    target), `target_shell` (one statement, run as root on the target - by
    the helper's `check` verb in root's shell after the switch, under sh
    over ssh before it) or `controller_shell` (one statement, run on this machine in
    its shell).  A phase's statements of a kind run at once, each in a
    shell of its own, any one's non-zero exit failing the phase; the last
    stderr line of each failing one is what the report says.  Every shell
    statement sees NIXCONVOY_TARGET, NIXCONVOY_TOPLEVEL (the system being
    switched to) and NIXCONVOY_PREVIOUS (the one it replaces), except a
    pre-switch controller statement, which runs beside eval with the union's
    tree as its cwd, before any toplevel is known."""

    kind: str
    script: str = ""


@dataclass(frozen=True)
class Checks:
    """The checks of one phase, the fleet's then the target's own."""

    checks: tuple[Check, ...] = ()

    def has(self, kind: str) -> bool:
        return any(c.kind == kind for c in self.checks)

    def statements(self, kind: str) -> tuple[str, ...]:
        """The checks of `kind`, in order: a shell kind's statements (the
        kit carries the target's, this machine runs its own, shell_script
        each), `flake`'s check names."""
        return tuple(c.script for c in self.checks if c.kind == kind)


@dataclass(frozen=True)
class PreSwitch(Checks):
    """[checks.pre_switch]: what must hold before a target is switched.
    The flake checks and the controller statements see the tree alone, so
    they run beside eval (the flake checks proven per tree, the statements
    every deploy); the target statements run after eval, before any build,
    with the toplevel in their environment.  A failure ends the deploy with
    nothing built, switched or committed."""

    @property
    def flakes(self) -> tuple[str, ...]:
        return self.statements("flake")


@dataclass(frozen=True)
class SwitchPolicy(Checks):
    """[checks.post_switch]: how a target is switched, and what must hold
    afterwards."""

    # ssh_deadline_secs (a target's over the fleet's): the switch is
    # confirmed - nix/kit.nix's helper arms a rollback timer, the toplevel
    # is activated with `test`, and within N seconds of the activation's
    # end this machine must read the new system running over a new ssh
    # connection (so sshd must admit a login), pass each check and
    # confirm, the profile set only then; otherwise the target rolls back
    # on its own.  None: post_switch = false - a plain switch, as
    # nixos-rebuild switch would.
    deadline: int | None = DEFAULT_DEADLINE

    @property
    def confirmed(self) -> bool:
        return self.deadline is not None


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
    # Its [targets.<name>.checks.<phase>] over the fleet's; None: the fleet's.
    pre_switch: PreSwitch | None = None
    post_switch: SwitchPolicy | None = None


@dataclass(frozen=True)
class Config:
    # The directory holding flake.nix, relative to the repo root.
    flake_dir: str
    # The directory whose subdirectories name the nixosConfigurations,
    # relative to the repo root; empty: the `[targets.<name>]` tables name
    # them instead.
    targets_dir: str
    # The targets' system (checks.<targets_arch>.<name> are the flake checks).
    targets_arch: str
    # A target's ssh address (user@name): nixconvoy.toml's
    # ssh_address_template, over {target}; the builder is addressed through it too.
    ssh_template: str
    # A checkout owns "<prefix><branch-suffix>", its subagent worktrees "<prefix><branch-suffix>-<name>"
    # (the main checkout's "<prefix>main-<name>"); {local_hostname}, when the
    # template has it, is this machine's hostname.  Also the shape of every
    # work branch nixconvoy unions, {local_hostname} standing for any hostname
    # (checkout.listed, the plan cores' `listed`): a pushed branch not
    # shaped so is ignored.
    branch_prefix: str
    # Where `open --child <name>` puts a child (subagent) worktree, relative
    # to the checkout.  A nested worktree must be ignored (open writes it
    # into .git/info/exclude); any worktree nested in a checkout is one of
    # its children, wherever it was made.
    child_worktrees: str
    # The machines the closures may be realised on, tried in order: the
    # first found usable (reachable, its daemon building for this user or
    # root, and building targets_arch) is the run's builder
    # (targets.builder_host); {local_hostname} is this machine's
    # hostname, which realises them here.  The builder's nix daemon builds
    # them or hands them on to its own remote builders (nix.conf's
    # builders), and the closures are copied to the targets from it.
    # Addressed as a target (ssh_address).
    builders: tuple[str, ...]
    # The remote origin/main and the work branches live on, and main's name
    # there: `remote` and `main_branch`.
    remote: str
    main_branch: str
    # [checks.pre_switch] and [checks.post_switch]: every target's unless
    # its own tables add to them.
    pre_switch: PreSwitch = field(default_factory=PreSwitch)
    post_switch: SwitchPolicy = field(default_factory=SwitchPolicy)
    targets: dict[str, TargetConfig] = field(default_factory=dict)

    def target(self, name: str) -> TargetConfig:
        return self.targets.get(name, TargetConfig())

    def ssh_address(self, name: str) -> str:
        return self.target(name).ssh_address or self.ssh_template.format(target=name)

    def switch_policy(self, name: str) -> SwitchPolicy:
        return self.target(name).post_switch or self.post_switch

    def pre_switch_checks(self, name: str) -> PreSwitch:
        return self.target(name).pre_switch or self.pre_switch

    @property
    def flakes(self) -> tuple[str, ...]:
        """The flake checks (checks.<targets_arch>.<name>) every deploy proves."""
        return self.pre_switch.flakes

    @property
    def tree_statements(self) -> tuple[str, ...]:
        """The pre-switch controller statements, run beside eval."""
        return self.pre_switch.statements("controller_shell")

    def tagged(self, tag: str) -> list[str]:
        return [n for n, h in self.targets.items() if tag in h.tags]

    @property
    def targets_path(self) -> str:
        return os.path.normpath(self.targets_dir) if self.targets_dir else ""

    @property
    def inventory(self) -> list[str]:
        """The targets `[targets.<name>]` names; empty with targets_dir set
        (targets.flake_target_names lists its subdirectories then).  A
        builder that is no target has no table: the ssh address
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


def _kinds(t: dict[str, object], where: str, phase: str, base: tuple[Check, ...] | None,
           kinds: tuple[str, ...]) -> tuple[Check, ...]:
    """The checks a phase table's kind keys make over `base` (the fleet's
    checks; None for the fleet's own table): a shell kind's or `flake`'s
    list appends one check per entry, internet = true appends the probe,
    false strikes `base`'s of that kind.  Keys that are no kind of `phase`
    are told which phase they belong to, when any."""
    other = PRE if phase == POST else POST
    other_kinds = PRE_KINDS if phase == POST else POST_KINDS
    kept = list(base or ())
    own: list[Check] = []
    for kind, val in t.items():
        at = f"{where}.{kind}"
        if kind == DEADLINE_KEY:
            continue
        if kind in GONE_KINDS:
            raise ConfigError(strings.CONFIG_CHECK_KIND_GONE(where=at, kind=kind, instead=GONE_KINDS[kind]))
        if kind == "auto_rollback_checks":
            raise ConfigError(strings.CONFIG_SWITCH_GONE(where=at))
        if kind not in kinds:
            if kind in other_kinds:
                raise ConfigError(strings.CONFIG_CHECK_OTHER_PHASE(where=at, key=kind, other=other, phase=phase))
            raise ConfigError(strings.CONFIG_CHECK_KIND(where=at, phase=phase, kinds=", ".join(kinds)))
        if phase == PRE and kind in TREE_KINDS and base is not None:
            raise ConfigError(strings.CONFIG_CHECK_FLEET_ONLY(where=at, kind=kind))
        if val is False:
            if base is None:
                raise ConfigError(strings.CONFIG_CHECK_STRIKE(where=at, phase=phase))
            kept = [c for c in kept if c.kind != kind]
        elif kind == "internet":
            if val is not True:
                raise ConfigError(strings.CONFIG_CHECK_INTERNET(where=at))
            if not any(c.kind == kind for c in kept):
                own.append(Check(kind))
        elif isinstance(val, list) and all(isinstance(st, str) for st in val):
            if kind == "flake" and len(set(val)) != len(val):
                raise ConfigError(strings.CONFIG_CHECK_FLAKE_TWICE(where=at))
            own.extend(Check(kind, st) for st in val)
        elif kind == "flake":
            raise ConfigError(strings.CONFIG_CHECK_FLAKE(where=at))
        else:
            raise ConfigError(strings.CONFIG_CHECK_SHELL(where=at, kind=kind))
    return (*kept, *own)


def _pre(v: object, where: str, base: PreSwitch | None) -> PreSwitch:
    """A pre_switch table over `base` (the fleet's; None for the fleet's own)."""
    if not isinstance(v, dict):
        raise ConfigError(strings.CONFIG_PRE_NOT_TABLE(where=where))
    if DEADLINE_KEY in v:
        raise ConfigError(strings.CONFIG_CHECK_OTHER_PHASE(where=f"{where}.{DEADLINE_KEY}", key=DEADLINE_KEY,
                                                           other=POST, phase=PRE))
    return PreSwitch(_kinds(v, where, PRE, base.checks if base else None, PRE_KINDS))


def _post(v: object, where: str, base: SwitchPolicy | None) -> SwitchPolicy:
    """A post_switch value over `base` (the fleet's; None for the fleet's
    own): false, a plain switch; else a table, its ssh_deadline_secs the
    deadline (over the fleet's) and its kind keys the checks (_kinds).
    Checks need a deadline from one or the other."""
    if v is False:
        return SwitchPolicy(deadline=None)
    if not isinstance(v, dict):
        raise ConfigError(strings.CONFIG_POST_NOT_TABLE(where=where))
    deadline = base.deadline if base else None
    if DEADLINE_KEY in v:
        val = v[DEADLINE_KEY]
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise ConfigError(strings.CONFIG_DEADLINE(where=f"{where}.{DEADLINE_KEY}"))
        deadline = val
    checks = _kinds(v, where, POST, base.checks if base else None, POST_KINDS)
    if checks and deadline is None:
        raise ConfigError(strings.CONFIG_CHECK_NO_DEADLINE(where=where))
    return SwitchPolicy(checks=checks, deadline=deadline)


def _checks(t: dict[str, object], where: str,
            base: tuple[PreSwitch, SwitchPolicy] | None) -> tuple[PreSwitch | None, SwitchPolicy | None]:
    """A `checks` table: `[checks]` (`base` None: the fleet's phases, the
    built-in defaults unless a phase says) or a `[targets.<name>.checks]`
    over the fleet's; a phase left out is None."""
    for k in t:
        if k not in (PRE, POST):
            raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"{where}.{k}"))
    pre = _pre(t[PRE], f"{where}.{PRE}", base[0] if base else None) if PRE in t else None
    post = _post(t[POST], f"{where}.{POST}", base[1] if base else None) if POST in t else None
    return pre, post


def _strs(t: dict[str, object], k: str, where: str) -> tuple[str, ...]:
    v = t.get(k, [])
    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
        raise ConfigError(strings.CONFIG_NOT_STRINGS(where=where, key=k))
    return tuple(v)


_noted_work_branches: list[bool] = []
_noted_submodules: list[bool] = []


def _template(t: dict[str, object], k: str, placeholder: str, where: str = CONFIG_FILE) -> str:
    """A required, non-empty template setting, proven here to format
    with `placeholder` alone, so a stale placeholder fails as a
    ConfigError rather than in a probe."""
    key = k if where == CONFIG_FILE else f"{where}.{k}"
    return _checked_template(_str(t, k, where, None), key, placeholder)


def _templates(t: dict[str, object], k: str, placeholder: str) -> tuple[str, ...]:
    """A required, non-empty list of templates (_template's), a top-level key."""
    v = _required(t, k, CONFIG_FILE)
    if not isinstance(v, list) or not v or not all(isinstance(s, str) for s in v):
        raise ConfigError(strings.CONFIG_NOT_TEMPLATES(key=k, placeholder=placeholder))
    return tuple(_checked_template(s, f"{k}[{i}]", placeholder) for i, s in enumerate(v))


def _checked_template(v: str, key: str, placeholder: str) -> str:
    if not v:
        raise ConfigError(strings.CONFIG_EMPTY_TEMPLATE(key=key, placeholder=placeholder))
    try:
        v.format(**{placeholder: "x"})
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError(strings.CONFIG_NOT_A_TEMPLATE(key=key, placeholder=placeholder, error=e)) from e
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
             "child_worktrees", "builders", "remote", "main_branch", "checks",
             "targets", "submodules", "work_branches"}
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
    if "builder" in doc:
        raise ConfigError(strings.CONFIG_BUILDER_GONE())
    if "hostnames" in doc:
        raise ConfigError(strings.CONFIG_HOSTNAMES_GONE())
    if "parallel_eval" in doc:
        raise ConfigError(strings.CONFIG_PARALLEL_EVAL_GONE())
    if "precheck" in doc:
        raise ConfigError(strings.CONFIG_PRECHECK_GONE())
    if "switch" in doc:
        raise ConfigError(strings.CONFIG_SWITCH_GONE(where="[switch]"))
    for k in doc:
        if k not in known:
            raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=k))
    # The required keys in the file's order, so a hand-written file is
    # told of its first gap first.
    flake_dir = _str(doc, "flake_dir", CONFIG_FILE, None)
    targets_dir = _str(doc, "targets_dir", CONFIG_FILE)
    targets_arch = _str(doc, "targets_arch", CONFIG_FILE, None)
    ssh_template = _template(doc, "ssh_address_template", "target")
    remote = _str(doc, "remote", CONFIG_FILE, None)
    if not valid_remote(remote):
        raise ConfigError(strings.CONFIG_REMOTE(remote=remote))
    main_branch = _str(doc, "main_branch", CONFIG_FILE, None)
    if not valid_branch(main_branch, remote):
        raise ConfigError(strings.CONFIG_MAIN_BRANCH(remote=remote, main_branch=main_branch))
    branch_prefix = _template(doc, "branch_prefix", "local_hostname")
    # ASCII, and braces only as {local_hostname}: the nix side (a deploy-time
    # regex over it) and the shape matchers read it verbatim.
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch_prefix.replace("{local_hostname}", "")):
        raise ConfigError(strings.CONFIG_BRANCH_PREFIX_CHARS(branch_prefix=branch_prefix))
    children = _str(doc, "child_worktrees", CONFIG_FILE, None).strip("/")
    if not children or ".." in children.split("/"):
        raise ConfigError(strings.CONFIG_CHILD_WORKTREES())
    builders = _templates(doc, "builders", "local_hostname")
    fleet_pre, fleet_post = _checks(_table(doc.get("checks", {}), "checks"), "checks", None)
    fleet = (fleet_pre or PreSwitch(), fleet_post or SwitchPolicy())
    targets: dict[str, TargetConfig] = {}
    for name, h in _table(doc.get("targets", {}), "targets").items():
        t = _table(h, f"targets.{name}")
        for k in t:
            if k == "switch":
                raise ConfigError(strings.CONFIG_SWITCH_GONE(where=f"[targets.{name}.switch]"))
            if k not in ("ssh_address", "tags", "dir", "checks"):
                raise ConfigError(strings.CONFIG_UNKNOWN_KEY(key=f"targets.{name}.{k}"))
        own_pre, own_post = (_checks(_table(t["checks"], f"targets.{name}.checks"), f"targets.{name}.checks", fleet)
                             if "checks" in t else (None, None))
        d = _str(t, "dir", f"targets.{name}")
        if d.startswith("/") or ".." in d.split("/"):
            raise ConfigError(strings.CONFIG_TARGET_DIR(name=name, dir=d))
        d = os.path.normpath(d) if d else ""
        if d == ".":
            raise ConfigError(strings.CONFIG_TARGET_DIR_ROOT(name=name))
        targets[name] = TargetConfig(ssh_address=_str(t, "ssh_address", f"targets.{name}"),
                                     tags=_strs(t, "tags", f"targets.{name}"), dir=d,
                                     pre_switch=own_pre, post_switch=own_post)
    if not targets_dir and not targets:
        raise ConfigError(strings.CONFIG_NO_TARGETS())
    return Config(
        flake_dir=flake_dir,
        targets_dir=targets_dir,
        targets_arch=targets_arch,
        ssh_template=ssh_template,
        branch_prefix=branch_prefix,
        child_worktrees=children,
        builders=builders,
        remote=remote,
        main_branch=main_branch,
        pre_switch=fleet[0],
        post_switch=fleet[1],
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
