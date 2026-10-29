"""What a name on the command line means - one vocabulary for review,
land, undeploy and close: a submodule of this checkout (review alone), a
subagent worktree (its exact name, this checkout's first, else a unique
prefix of one, likewise), or a branch of this repository (the worktree
here on it, else the branch itself).  An exact worktree name wins over a
branch, a branch over a prefix."""

from __future__ import annotations

from pathlib import Path

from . import strings, submodule
from .checkout import branch_at, git_common, sibling_checkouts, sub_parent
from .proc import die, run
from .submodule import Sub


def toplevel(d: Path) -> Path:
    return Path(run(["git", "rev-parse", "--show-toplevel"], cwd=str(d)).stdout.strip()).resolve()


def resolve(name: str, here: Path, *, submodules: bool = True) -> Path | Sub | str | None:
    """What `name` means from the checkout `here` (or a directory in it):
    a submodule (`submodules`: those of this checkout count), a worktree, a
    branch name no worktree is on, or None."""
    top = toplevel(here)
    if submodules:
        for sub in submodule.present(top):
            if sub.name == name:
                return sub
    checkouts = sorted({top, *sibling_checkouts(top)}, key=lambda d: (d.name, str(d)))
    subs = [d for d in checkouts if sub_parent(d) is not None]
    own = [d for d in subs if top in d.parents]
    found = unique(name, [d for d in own if d.name == name]) or unique(name, [d for d in subs if d.name == name])
    if found is not None:
        return found
    for ref in (f"refs/heads/{name}", f"refs/remotes/{name}"):
        if run(["git", "rev-parse", "-q", "--verify", ref], cwd=str(top), check=False).returncode == 0:
            # The branch is this repository's: a clone's of the same name is another.
            mine = git_common(top)
            for d in checkouts:
                if git_common(d) == mine and branch_at(d) == name:
                    return d
            return name
    return (unique(name, [d for d in own if d.name.startswith(name)])
            or unique(name, [d for d in subs if d.name.startswith(name)]))


def unique(name: str, hits: list[Path]) -> Path | None:
    if len(hits) > 1:
        die(strings.NAMES_AMBIGUOUS(name=name, worktrees=", ".join(label(d) for d in hits)))
    return hits[0] if hits else None


def worktree(name: str, here: Path, verb: str, *, subagent: bool = True) -> Path:
    """The worktree `name` names, for `verb` to act in; `subagent`: a
    subagent worktree alone.  Anything else dies saying what `name` is."""
    if name in gitmodule_names(toplevel(here)):
        die(strings.NAMES_IS_SUBMODULE(name=name, verb=verb))
    got = resolve(name, here, submodules=False)
    if isinstance(got, str):
        die(strings.NAMES_NO_WORKTREE_ON_BRANCH(name=name, verb=verb))
    if got is None or isinstance(got, Sub):
        subs = [d for d in sibling_checkouts(here) if sub_parent(d) is not None]
        listing = strings.NAMES_NOT_FOUND_LISTING(worktrees=", ".join(label(d) for d in subs)) if subs else ""
        die(strings.NAMES_NOT_FOUND(name=name, listing=listing))
    if subagent and sub_parent(got) is None:
        die(strings.NAMES_NOT_SUBAGENT(name=name, path=got))
    return got


def gitmodule_names(top: Path) -> set[str]:
    """The submodule names .gitmodules declares under `top` (read without
    nixconvoy.toml, which belongs to the checkout acted in)."""
    p = run(["git", "config", "-f", str(top / ".gitmodules"), "--name-only", "--get-regexp", r"^submodule\..*\.path$"],
            check=False)
    return {ln[len("submodule."):-len(".path")] for ln in p.stdout.splitlines() if ln.startswith("submodule.")}


def label(d: Path) -> str:
    parent = sub_parent(d)
    return f"{parent.name}/{d.name}" if parent is not None else str(d)
