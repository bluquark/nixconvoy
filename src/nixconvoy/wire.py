"""The messages nixconvoy-plan and nixconvoy-switch exchange with nixconvoy
(switch-guard/src/wire.rs).  Each class below mirrors, by name, one variant
of an enum or one struct of the crate's plan and control modules, its
fields typed as there; `encode` and `decode` are serde's default encoding
of them: a variant holding nothing is its name in snake_case, a `Newtype`
one {name: value}, any other {name: {field: value, ...}}; a struct is
{field: value, ...}; None is null, tuples and sets are lists.  `decode`
refuses anything the types do not admit."""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass
from functools import cache
from typing import Any, ClassVar


class WireError(ValueError):
    """A line the types do not admit."""


@dataclass(frozen=True)
class Variant:
    """An enum variant: tagged by its class name in snake_case, unless `tag`
    says otherwise."""

    tag: ClassVar[str] = ""


@dataclass(frozen=True)
class Newtype(Variant):
    """A variant holding one unnamed value, its `value` field."""


def tag_of(cls: type[Variant]) -> str:
    if cls.tag:
        return cls.tag
    name = cls.__name__
    return "".join(f"_{c.lower()}" if c.isupper() and i else c.lower() for i, c in enumerate(name))


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def encode(v: object) -> object:
    """`v` as the JSON value serde reads."""
    if isinstance(v, Variant):
        fields = dataclasses.fields(v)
        tag = tag_of(type(v))
        if not fields:
            return tag
        if isinstance(v, Newtype):
            return {tag: encode(getattr(v, fields[0].name))}
        return {tag: {f.name: encode(getattr(v, f.name)) for f in fields}}
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: encode(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, (list, tuple)):
        return [encode(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(encode(x) for x in v)  # type: ignore[type-var] - sets of one ordered type
    if isinstance(v, dict):
        return {str(k): encode(x) for k, x in v.items()}
    if v is None or isinstance(v, (bool, int, str)):
        return v
    raise WireError(f"cannot encode {v!r}")


def _tagged(v: object, at: str) -> tuple[str, object]:
    if isinstance(v, str):
        return v, None
    if isinstance(v, dict) and len(v) == 1:
        ((tag, payload),) = v.items()
        return str(tag), payload
    raise WireError(f"{at}: {v!r} is not a variant")


def _fields(cls: type, v: object, at: str) -> dict[str, object]:
    if not isinstance(v, dict):
        raise WireError(f"{at}: {v!r} is not an object")
    hints = _hints(cls)
    got = {}
    for f in dataclasses.fields(cls):
        if f.name not in v:
            raise WireError(f"{at}: no {f.name}")
        got[f.name] = decode(hints[f.name], v[f.name], f"{at}.{f.name}")
    return got


def _variant(cls: type[Variant], payload: object, at: str) -> Variant:
    fields = dataclasses.fields(cls)
    if not fields:
        if payload is not None:
            raise WireError(f"{at}: {tag_of(cls)} holds nothing")
        return cls()
    if issubclass(cls, Newtype):
        return cls(**{fields[0].name: decode(_hints(cls)[fields[0].name], payload, at)})
    return cls(**_fields(cls, payload, at))


def decode(t: object, v: object, at: str = "value") -> Any:
    """The JSON value `v` as a `t`: a class above, or a str/bool/int, None,
    list[T], tuple[A, B], dict[str, T] or union of them."""
    origin = typing.get_origin(t)
    if origin in (types.UnionType, typing.Union):
        members = typing.get_args(t)
        if v is None and type(None) in members:
            return None
        rest = [m for m in members if m is not type(None)]
        if len(rest) == 1:
            return decode(rest[0], v, at)
        variants = [m for m in rest if isinstance(m, type) and issubclass(m, Variant)]
        if len(variants) != len(rest):
            raise WireError(f"{at}: unsupported union {t}")
        tag, payload = _tagged(v, at)
        for m in variants:
            if tag_of(m) == tag:
                return _variant(m, payload, at)
        raise WireError(f"{at}: unknown variant {tag!r}")
    if origin is list:
        (item,) = typing.get_args(t)
        if not isinstance(v, list):
            raise WireError(f"{at}: {v!r} is not a list")
        return [decode(item, x, f"{at}[{i}]") for i, x in enumerate(v)]
    if origin is tuple:
        items = typing.get_args(t)
        if not isinstance(v, list) or len(v) != len(items):
            raise WireError(f"{at}: {v!r} is not a {len(items)}-tuple")
        return tuple(decode(item, x, f"{at}[{i}]") for i, (item, x) in enumerate(zip(items, v, strict=True)))
    if origin is dict:
        key, item = typing.get_args(t)
        if key is not str or not isinstance(v, dict):
            raise WireError(f"{at}: {v!r} is not an object")
        return {str(k): decode(item, x, f"{at}.{k}") for k, x in v.items()}
    if t is bool or t is int:
        if not isinstance(v, int) or isinstance(v, bool) != (t is bool):
            raise WireError(f"{at}: {v!r} is not a {t.__name__}")  # type: ignore[attr-defined]
        return v
    if t is str:
        if not isinstance(v, str):
            raise WireError(f"{at}: {v!r} is not a string")
        return v
    if t is type(None):
        if v is not None:
            raise WireError(f"{at}: {v!r} is not null")
        return None
    if isinstance(t, type) and issubclass(t, Variant):
        tag, payload = _tagged(v, at)
        if tag != tag_of(t):
            raise WireError(f"{at}: {tag!r} is not {tag_of(t)}")
        return _variant(t, payload, at)
    if isinstance(t, type) and dataclasses.is_dataclass(t):
        return t(**_fields(t, v, at))
    raise WireError(f"{at}: cannot decode a {t}")


# A core's step: what it printed, then what it asks, ends with or dies of.


@dataclass(frozen=True)
class Print(Newtype):
    value: str


@dataclass(frozen=True)
class Warn(Newtype):
    """To stderr."""

    value: str


@dataclass(frozen=True)
class Mark(Newtype):
    """nixconvoy's PERF phase."""

    value: str


@dataclass(frozen=True)
class Armed(Variant):
    """The target is armed: a die() elsewhere leaves it to its timer."""


@dataclass(frozen=True)
class Disarmed(Variant):
    pass


Effect = Print | Warn | Mark | Armed | Disarmed


def step(v: object, query: object, outcome: object) -> tuple[list[Effect], str, object]:
    """A step line decoded: its effects, then "ask" with the query (one of
    the union `query`), "done" with the outcome (an `outcome`) or "die"
    with the message."""
    if not isinstance(v, dict) or "effects" not in v or "next" not in v:
        raise WireError(f"{v!r} is not a step")
    effects: list[Effect] = decode(list[Effect], v["effects"], "effects")
    tag, payload = _tagged(v["next"], "next")
    if tag == "ask":
        return effects, tag, decode(query, payload, "ask")
    if tag == "done":
        return effects, tag, decode(outcome, payload, "done")
    if tag == "die":
        return effects, tag, decode(str, payload, "die")
    raise WireError(f"next: unknown {tag!r}")


# nixconvoy-plan (switch-guard/src/plan.rs): the parameters of each core.


@dataclass(frozen=True)
class RepoNames:
    """The repository's remote and its main branch there (plan.rs's Names):
    nixconvoy.toml's `remote` and `main_branch`."""

    remote: str
    main: str

    @property
    def main_ref(self) -> str:
        """main as fetched, in full: refs/remotes/origin/main."""
        return self.remote_ref(self.main)

    @property
    def main_short(self) -> str:
        """main as fetched, as a person names it: origin/main."""
        return f"{self.remote}/{self.main}"

    def remote_ref(self, branch: str) -> str:
        """`branch` as fetched, in full: refs/remotes/origin/<branch>."""
        return f"refs/remotes/{self.remote}/{branch}"



@dataclass(frozen=True)
class Context:
    """What the deploy knows when it classifies."""

    names: RepoNames
    me: tuple[str, str]
    listed: list[str]
    tips: dict[str, str]
    in_flight: dict[str, str]
    dead: list[str]
    tz_offset: int


@dataclass(frozen=True)
class DriftParams:
    cx: Context
    union: str
    router: str
    targets: list[str]
    candidates: list[str]
    evict: bool
    named: bool
    work_branch: bool
    reached: list[str]
    running: dict[str, str | None]
    built: dict[str, str]
    held: dict[str, list[str]]
    provenances: dict[str, str]


@dataclass(frozen=True)
class DriftOutcome:
    affected: list[str]
    plan: list[str]
    drift: dict[str, tuple[str, str]]


@dataclass(frozen=True)
class PopTmp(Variant):
    pass


@dataclass(frozen=True)
class EnsureRebased(Variant):
    pass


@dataclass(frozen=True)
class Onto(Variant):
    upstream: str


@dataclass(frozen=True)
class Sync(Variant):
    wb: str | None


@dataclass(frozen=True)
class OnWorkBranch(Variant):
    wb: str


RebaseOp = PopTmp | EnsureRebased | Onto | Sync | OnWorkBranch


@dataclass(frozen=True)
class RebaseParams:
    """The rebase core's parameters: the operation, under the repository's names."""

    names: RepoNames
    op: RebaseOp


@dataclass(frozen=True)
class Ok(Newtype):
    tag: ClassVar[str] = "Ok"
    value: str


@dataclass(frozen=True)
class Err(Newtype):
    tag: ClassVar[str] = "Err"
    value: str


RebaseOutcome = Ok | Err


@dataclass(frozen=True)
class UnionParams:
    names: RepoNames
    wb: str
    evict: bool
    main0: str | None


@dataclass(frozen=True)
class UnionOutcome:
    union: str
    branches: list[tuple[str, str]]


@dataclass(frozen=True)
class Stopped:
    pid: int
    lock: str | None


@dataclass(frozen=True)
class PruneParams:
    names: RepoNames
    name: str
    named: bool
    own_session_ends: bool
    stopped: Stopped | None


@dataclass(frozen=True)
class Removed(Variant):
    pass


@dataclass(frozen=True)
class Kept(Newtype):
    value: str


PruneOutcome = Removed | Kept


# nixconvoy-plan's queries (plan.rs's Query, its comments) and replies.


@dataclass(frozen=True)
class Contains(Variant):
    a: str
    b: str


@dataclass(frozen=True)
class OpInProgress(Variant):
    pass


@dataclass(frozen=True)
class CurrentBranch(Variant):
    pass


@dataclass(frozen=True)
class StagedDiffers(Variant):
    pass


@dataclass(frozen=True)
class Commit(Variant):
    msg: str


@dataclass(frozen=True)
class AddAll(Variant):
    pass


@dataclass(frozen=True)
class Pop(Variant):
    mixed: bool


@dataclass(frozen=True)
class HeadSubject(Variant):
    pass


@dataclass(frozen=True)
class Rebase(Variant):
    keep_empty: bool
    aside: bool
    upstream: str


@dataclass(frozen=True)
class RebaseAbort(Variant):
    aside: bool


@dataclass(frozen=True)
class Rev(Variant):
    name: str


@dataclass(frozen=True)
class Fetch(Variant):
    pass


@dataclass(frozen=True)
class Push(Variant):
    branch: str


@dataclass(frozen=True)
class AddWorktree(Variant):
    branch: str


@dataclass(frozen=True)
class DropWorktree(Variant):
    pass


@dataclass(frozen=True)
class Switch(Variant):
    branch: str
    start: str | None


@dataclass(frozen=True)
class Detach(Variant):
    upstream: str


@dataclass(frozen=True)
class Pushed(Variant):
    pass


@dataclass(frozen=True)
class OnOrigin(Variant):
    pass


@dataclass(frozen=True)
class Upstream(Variant):
    branch: str


@dataclass(frozen=True)
class Fate(Variant):
    branch: str
    tip: str
    union: str
    checkout: str | None


@dataclass(frozen=True)
class SetUpstream(Variant):
    branch: str


@dataclass(frozen=True)
class Listed(Variant):
    pass


@dataclass(frozen=True)
class Branches(Variant):
    pass


@dataclass(frozen=True)
class Unchanged(Variant):
    sha: str


@dataclass(frozen=True)
class Worktree(Variant):
    pass


@dataclass(frozen=True)
class Summary(Variant):
    sha: str


@dataclass(frozen=True)
class Merge(Variant):
    name: str
    sha: str


@dataclass(frozen=True)
class DropFile(Variant):
    file: str


@dataclass(frozen=True)
class Solve(Variant):
    file: str


@dataclass(frozen=True)
class Add(Variant):
    file: str


@dataclass(frozen=True)
class AbortMerge(Variant):
    pass


@dataclass(frozen=True)
class CommitMerge(Variant):
    pass


@dataclass(frozen=True)
class StripTree(Variant):
    pass


@dataclass(frozen=True)
class Head(Variant):
    pass


@dataclass(frozen=True)
class HeadTree(Variant):
    pass


@dataclass(frozen=True)
class CommitUnion(Variant):
    tree: str


@dataclass(frozen=True)
class Checkout(Variant):
    union: str


@dataclass(frozen=True)
class CollectFetch(Variant):
    pass


@dataclass(frozen=True)
class Refresh(Variant):
    pass


@dataclass(frozen=True)
class AwaitPending(Variant):
    union: str


@dataclass(frozen=True)
class Stage(Variant):
    union: str
    listed: list[str]
    branches: list[tuple[str, str]]


@dataclass(frozen=True)
class TakeNameLock(Variant):
    pass


@dataclass(frozen=True)
class ReleaseNameLock(Variant):
    pass


@dataclass(frozen=True)
class TakeDeployLock(Variant):
    pass


@dataclass(frozen=True)
class ReleaseDeployLock(Variant):
    pass


@dataclass(frozen=True)
class Lock(Variant):
    pass


@dataclass(frozen=True)
class Alive(Variant):
    pid: int
    start: int | None
    own_ends: bool


@dataclass(frozen=True)
class PushInFlight(Variant):
    pass


@dataclass(frozen=True)
class Nested(Variant):
    pass


@dataclass(frozen=True)
class OwnBranch(Variant):
    pass


@dataclass(frozen=True)
class Unpushed(Variant):
    sha: str


@dataclass(frozen=True)
class Dirty(Variant):
    pass


@dataclass(frozen=True)
class Occupied(Variant):
    pass


@dataclass(frozen=True)
class RemoveWorktree(Variant):
    pass


@dataclass(frozen=True)
class DropBranches(Variant):
    wb: str
    cur: str | None


PlanQuery = (
    Contains | OpInProgress | CurrentBranch | StagedDiffers | Commit | AddAll | Pop | HeadSubject | Rebase
    | RebaseAbort | Rev | Fetch | Push | AddWorktree | DropWorktree | Switch | Detach | Pushed | OnOrigin
    | Upstream | Fate | SetUpstream | Listed | Branches | Unchanged | Worktree | Summary | Merge | DropFile
    | Solve | Add | AbortMerge | CommitMerge | StripTree | Head | HeadTree | CommitUnion | Checkout
    | CollectFetch | Refresh | AwaitPending | Stage | TakeNameLock | ReleaseNameLock | TakeDeployLock
    | ReleaseDeployLock | Lock | Alive | PushInFlight | Nested | OwnBranch | Unpushed | Dirty | Occupied
    | RemoveWorktree | DropBranches
)


@dataclass(frozen=True)
class Flag(Newtype):
    value: bool


@dataclass(frozen=True)
class Text(Newtype):
    """nixconvoy-switch's never holds None."""

    value: str | None


@dataclass(frozen=True)
class Ran(Variant):
    """A command's success and its stderr."""

    ok: bool
    detail: str


@dataclass(frozen=True)
class Refs(Newtype):
    value: list[tuple[str, str]]


@dataclass(frozen=True)
class Names(Newtype):
    value: list[str]


@dataclass(frozen=True)
class Merged(Variant):
    """A merge's success, its conflicted files and its stderr."""

    ok: bool
    files: list[str]
    detail: str


PlanReply = Flag | Text | Ran | Refs | Names | Merged


# nixconvoy-switch (switch-guard/src/control.rs): its parameters, queries
# and replies.


@dataclass(frozen=True)
class SwitchParams:
    target: str
    toplevel: str
    expected: str
    place: str
    quiet: bool
    router: bool
    fail_verify: bool


@dataclass(frozen=True)
class TakeGuard(Variant):
    pass


@dataclass(frozen=True)
class ProbeHelper(Variant):
    pass


@dataclass(frozen=True)
class SetProfile(Variant):
    pass


@dataclass(frozen=True)
class Activate(Variant):
    pass


@dataclass(frozen=True)
class Deadline(Variant):
    pass


@dataclass(frozen=True)
class Arm(Variant):
    pass


@dataclass(frozen=True)
class Helper(Variant):
    args: list[str]


@dataclass(frozen=True)
class Clock(Variant):
    pass


@dataclass(frozen=True)
class Running(Variant):
    timeout_ms: int | None


@dataclass(frozen=True)
class Check(Variant):
    timeout_ms: int


@dataclass(frozen=True)
class Internet(Variant):
    timeout_ms: int


@dataclass(frozen=True)
class Sleep(Variant):
    ms: int


SwitchQuery = (
    TakeGuard | ProbeHelper | SetProfile | Activate | Deadline | Arm | Helper | Clock | Running | Check
    | Internet | Sleep
)


@dataclass(frozen=True)
class Locked(Variant):
    pass


@dataclass(frozen=True)
class Moved(Newtype):
    """The target runs something else than what was probed."""

    value: str


@dataclass(frozen=True)
class Failed(Newtype):
    value: str


@dataclass(frozen=True)
class Guard(Newtype):
    value: Locked | Moved | Failed


@dataclass(frozen=True)
class Code(Newtype):
    value: int


@dataclass(frozen=True)
class Said(Variant):
    """An arm's or rollback's exit code and the system the helper said its
    rollback goes or went to."""

    code: int
    to: str | None


@dataclass(frozen=True)
class Now(Newtype):
    value: int


@dataclass(frozen=True)
class System(Newtype):
    value: str | None


@dataclass(frozen=True)
class Checked(Variant):
    """`code` < 0: timed out.  `last`: its last stderr line."""

    code: int
    last: str | None


@dataclass(frozen=True)
class Done(Variant):
    pass


SwitchReply = Guard | Code | Said | Text | Now | System | Checked | Flag | Done
