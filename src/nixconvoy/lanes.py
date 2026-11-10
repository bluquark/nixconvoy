"""What each target runs of each work branch, read off its deploy
records, and so what a deploy's union for it may hold of the branches of
other checkouts.

A target's record (checkout.record_commit, on its history branch) is written
by the deploy that switched it: its tree is the configuration the target
runs, its provenance names the commit of each branch that went into it.  A
branch's work reaches a target through that branch's own checkout deploying
it there, or by landing: a deploy from another checkout unions the branch
at the commit the target's record holds (carried), unless nothing of the
branch past that commit is something the target's evaluation reads
(lagging) - then the tip, which the target cannot tell from it.  A deploy
that evaluates a target to the system it already runs records that without
a switch (an attested record: deploy.stage), so the branch's later work
counts as deployed there."""

from __future__ import annotations

from . import memo
from .checkout import DEPLOYED_PREFIX, read_rebase_map
from .config import main_ref, remote_ref
from .proc import is_ancestor, load_json, out, rev, run
from .targets import flake_target_names

# How far up a target's history its attested records are looked for.
ATTESTED_DEPTH = 50
# How far up a commit's first parents a rebase's rewrite of one is looked for.
REBASE_MAP_DEPTH = 200


def record_doc(sha: str) -> dict[str, object] | None:
    """The provenance a deploy record's message carries."""
    p = run(["git", "log", "-1", "--format=%b", sha], check=False)
    if p.returncode != 0:
        return None
    try:
        doc = load_json(p.stdout)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def history_tip(target: str) -> str | None:
    """`target`'s history branch as fetched."""
    return rev(remote_ref(f"{DEPLOYED_PREFIX}/{target}"))


def running_record(target: str, provenance: str, running: str | None) -> tuple[dict[str, object], str] | None:
    """The record `target` runs and its commit ("" for a provenance naming
    none): the one its provenance file names - or the newest attested
    record chained on that one (first parents) whose toplevel is the system
    it runs; None when the provenance is not that system's."""
    try:
        doc = load_json(provenance)
    except ValueError:
        return None
    if not isinstance(doc, dict) or running is None or doc.get("toplevel") != running:
        return None
    rec = doc.get("record")
    if not isinstance(rec, str) or rev(f"{rec}^{{commit}}") is None:
        return doc, ""
    tip = history_tip(target)
    if tip is None or tip == rec:
        return doc, rec
    p = run(["git", "rev-list", "--first-parent", f"--max-count={ATTESTED_DEPTH + 1}", tip], check=False)
    chain = p.stdout.split() if p.returncode == 0 else []
    if rec not in chain:
        return doc, rec
    for sha in chain[:chain.index(rec)]:
        attested = record_doc(sha)
        if attested is not None and attested.get("attested") is True and attested.get("toplevel") == running:
            return attested, sha
    return doc, rec


def history_doc(target: str) -> dict[str, object] | None:
    """The newest record of `target`'s history branch as fetched: what it
    runs as far as origin knows, with no probe of it."""
    tip = history_tip(target)
    return record_doc(tip) if tip is not None else None


def carried_commit(branch: str, commit: str, tip: str, *, local: bool = False) -> str | None:
    """The commit of `branch` as it is now (at `tip`) that stands for
    `commit`, the one a record holds of it: itself while the branch holds
    it; the tip when the record is ahead of it (an increment whose push is
    on its way, or one the branch was reset below); else the rewrite of
    its nearest first-parent ancestor a rebase replayed, itself first (the
    branch's rebase map; `local`: this repository's own copy) - one a rebase
    dropped, its change upstream already, is in the one before it; where
    the two part, when that lies within the branch's increments (reset
    below it, then grown again); None when the branch dropped it - reset
    to nothing of its own, or landed - or nothing here places it."""
    here = rev(f"{commit}^{{commit}}") is not None
    if here and is_ancestor(commit, tip):
        return commit
    if here and is_ancestor(tip, commit):
        return tip
    known = read_rebase_map(branch, local=local)
    line = [commit]
    if here and known:
        line = run(["git", "rev-list", "--first-parent", f"--max-count={REBASE_MAP_DEPTH}", commit],
                   check=False).stdout.split()
    rewrite = next((known[c] for c in line if c in known), None)
    if rewrite is not None and rev(f"{rewrite}^{{commit}}") is not None:
        commit, here = rewrite, True
        if is_ancestor(commit, tip):
            return commit
    if not here:
        return None
    p = run(["git", "merge-base", commit, tip], check=False)
    mb = p.stdout.strip() if p.returncode == 0 else ""
    return mb if mb and not is_ancestor(mb, main_ref()) else None


def lagging(wt: str, union: str, branch: str, tip: str, docs: dict[str, dict[str, object] | None], *,
            local: bool = False) -> dict[str, str]:
    """The targets of `docs` (each with the record it runs, None for none)
    behind `branch` at `tip`, each with the commit of the branch it runs
    ("" for none of its increments): those whose evaluation reads
    something the branch changed past that commit (memo.reached_between
    over `union`, checked out in `wt`).  A commit on main's side of the
    branch's increments (the branch had none then) is none of them."""
    p = run(["git", "merge-base", tip, main_ref()], check=False)
    floor = p.stdout.strip() if p.returncode == 0 else ""
    carried: dict[str, str | None] = {}
    by_base: dict[str, list[str]] = {}
    for h, doc in docs.items():
        branches = doc.get("branches") if doc is not None else None
        held = branches.get(branch) if isinstance(branches, dict) else None
        if held == tip:
            continue
        base = floor
        if isinstance(held, str):
            if held not in carried:
                c = carried_commit(branch, held, tip, local=local)
                carried[held] = None if c is not None and c != tip and floor and is_ancestor(c, floor) else c
            base = carried[held] or floor
        if base != tip:
            by_base.setdefault(base, []).append(h)
    found: dict[str, str] = {}
    for base, hs in by_base.items():
        # No merge base with main: nothing to measure from.
        behind = memo.reached_between(wt, union, hs, base, tip) if base else set(hs)
        found.update({h: "" if base == floor else base for h in behind})
    return found


def cuts(wt: str, union: str, branches: list[tuple[str, str]],
         docs: dict[str, dict[str, object] | None]) -> list[tuple[str, str, str]]:
    """The union core's Query::Cuts over the targets of `docs`."""
    return sorted((h, name, commit) for name, tip in branches
                  for h, commit in lagging(wt, union, name, tip, docs).items())


def undeployed(branch: str, tip: str, wt: str) -> list[str]:
    """The targets the local `branch`, at `tip` (checked out in `wt`), has
    work not deployed to: by their history branches as fetched."""
    sha = out(["git", "rev-parse", f"{tip}^{{commit}}"])
    return sorted(lagging(wt, sha, branch, sha, {h: history_doc(h) for h in flake_target_names()}, local=True))
