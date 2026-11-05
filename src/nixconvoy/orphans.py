"""`nixconvoy orphans`: this machine's subagent work branches on origin
that nobody holds (cli docstring: Orphans)."""

from __future__ import annotations

import sys
from pathlib import Path

from . import strings
from .checkout import (
    SESSIONLESS,
    branch_at,
    fetch_args,
    identity_of,
    listed,
    lock_pid_alive,
    origin_tips,
    sibling_checkouts,
    sub_parent,
    this_prefix,
    worktree_lock,
)
from .config import MSG_FILE, cfg, remote_ref
from .proc import git_remote, run


def held(d: Path) -> bool:
    """Whether the worktree `d` is held: any checkout but a subagent
    worktree; a subagent worktree unless its lock names a claude session
    that is gone.  A hand lock, a sessionless one (`open --child` by hand)
    and none at all are a person's, as the sweeps take them."""
    if sub_parent(d) is None:
        return True
    reason = worktree_lock(d)
    if reason is None or reason.endswith(SESSIONLESS):
        return True
    return lock_pid_alive(reason) is not False


def find() -> list[tuple[str, Path | None, str]]:
    """(branch, worktree owning it or None, COMMIT_MSG.md's subject) for
    each orphan: one of this machine's listed subagent branches
    (<prefix><branch-suffix>-<name>) on origin with commits beyond origin/main and
    every other listed branch, which no checkout here owns or whose owner
    is a subagent worktree no live session holds."""
    fetched = git_remote(*fetch_args("--no-auto-gc"), check=False)
    if fetched.returncode != 0:
        print(strings.ORPHANS_FETCH_FAILED(stderr=(fetched.stderr or "").strip()), file=sys.stderr)
    prefix = this_prefix()
    tips = origin_tips()
    work = [b for b in tips if listed(b)]
    here = Path.cwd().resolve()
    owners: dict[str, Path] = {}
    for d in [here, *sibling_checkouts(here)]:
        try:
            wb = identity_of(d, branch_at(d)).wb
        except FileNotFoundError:  # removed since it was listed
            continue
        if wb is not None:
            owners.setdefault(wb, d)
    found = []
    for b in sorted(work):
        if not b.startswith(prefix) or "-" not in b[len(prefix):]:
            continue
        d = owners.get(b)
        if d is not None and held(d):
            continue
        others = [remote_ref(o) for o in [cfg().main_branch, *work] if o != b and o in tips]
        own = run(["git", "rev-list", "-n", "1", remote_ref(b), "--not", *others],
                  check=False).stdout.strip()
        if not own:
            continue
        msg = run(["git", "show", f"{remote_ref(b)}:{MSG_FILE}"], check=False).stdout
        found.append((b, d, msg.partition("\n")[0].replace("\t", " ").strip()))
    return found


def main() -> None:
    """One tab-separated line per orphan: branch, worktree path or
    "(no worktree)", subject."""
    for b, d, subject in find():
        print(f"{b}\t{d if d is not None else strings.ORPHANS_NO_WORKTREE}\t{subject}")
