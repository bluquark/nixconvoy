"""The union core's queries: git in the union worktree on the deploy's
snapshot, the fetch the deploy started, and `stage` - the deploy's eval,
build and copy of a union (deploy.deploy)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from pathlib import Path

from . import strings, submodule
from .checkout import (
    MACHINE_ENV,
    GitQueries,
    add_worktree,
    drop_worktree,
    feature_shape,
    listed,
    ran,
    summarize,
)
from .config import MSG_FILE, cfg, main_ref, main_short
from .proc import git, is_ancestor, ok, out, rev, run, solve_with_mergiraf
from .wire import (
    AbortMerge,
    Add,
    AwaitPending,
    Branches,
    Checkout,
    CollectFetch,
    CommitMerge,
    CommitUnion,
    Contains,
    Cut,
    Cuts,
    DropFile,
    Flag,
    Head,
    HeadTree,
    Lane,
    LaneWorktree,
    Listed,
    Merge,
    Merged,
    Names,
    PlanQuery,
    PlanReply,
    Ran,
    Refresh,
    Refs,
    Solve,
    Stage,
    StripTree,
    Summary,
    Text,
    Unchanged,
    Worktree,
)


class UnionQueries(GitQueries):
    def __init__(self, base: Callable[[], str], fetch: Future[subprocess.CompletedProcess[str]],
                 refresh: Callable[[], None],
                 stage: Callable[[str, str, list[str], list[tuple[str, str]], list[tuple[str, Lane]]], bool],
                 await_pending: Callable[[str, str], bool] | None = None,
                 cuts: Callable[[str, str, list[tuple[str, str]]], list[tuple[str, str, str]]] | None = None,
                 as_if: Mapping[str, str] | None = None) -> None:
        super().__init__(as_if=as_if)
        self.base, self.fetch, self.refresh, self.stage = base, fetch, refresh, stage
        self.await_pending, self.cuts = await_pending, cuts
        self.wt = ""
        self.wts: list[str] = []
        # The worktree each union of this attempt is checked out in.
        self.at: dict[str, str] = {}

    def drop(self) -> None:
        while self.wts:
            drop_worktree(self.wts.pop())

    def __call__(self, ask: PlanQuery) -> PlanReply:
        wt = self.wt
        match ask:
            case Listed():
                return Names([feature_shape()])
            case Branches():
                listing = out(["git", "for-each-ref", "--format=%(refname:strip=3)%09%(objectname)",
                               f"refs/remotes/{cfg().remote}/"])
                refs = [(n, self.as_if.get(n, t)) for n, t in (ln.split("\t", 1) for ln in listing.splitlines())]
                return Refs([r for r in refs if listed(r[0])])
            case Contains(a, "HEAD"):  # the checkout's state is the union's base
                return Flag(is_ancestor(a, self.base()))
            case Unchanged(sha):
                base = out(["git", "merge-base", main_short(), sha])
                return Flag(ok(["git", "diff", "--quiet", base, sha, "--", ".", f":(exclude){MSG_FILE}"]))
            case Worktree():
                self.drop()
                self.at = {}
                self.wt = add_worktree(self.base())
                self.wts.append(self.wt)
                return Ran(True, "")
            case LaneWorktree():
                self.wt = add_worktree(self.base())
                self.wts.append(self.wt)
                return Ran(True, "")
            case Summary(sha):
                return Text(summarize(sha))
            case Merge(name, sha):
                p = git("merge", "--quiet", "--no-edit", "-m", strings.UNION_MERGE_SUBJECT(name=name), sha, cwd=wt,
                        env=MACHINE_ENV, check=False)
                files: list[str] = [] if p.returncode == 0 else out(
                    ["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt).splitlines()
                return Merged(p.returncode == 0, files, (p.stderr or "").strip())
            case DropFile(file):
                return ran(git("rm", "--quiet", "-f", file, cwd=wt, check=False))
            case Solve(file):
                if submodule.is_pin_path(Path(wt), file):
                    return Flag(submodule.solve_union(Path(wt), file))
                return Flag(solve_with_mergiraf(wt, file))
            case Add(file):
                return ran(git("add", "--", file, cwd=wt, check=False))
            case AbortMerge():
                return ran(git("merge", "--abort", cwd=wt, check=False))
            case CommitMerge():
                return ran(git("commit", "--quiet", "--no-edit", "--no-verify", cwd=wt, env=MACHINE_ENV, check=False))
            case StripTree():
                git("rm", "--quiet", "--ignore-unmatch", "-f", MSG_FILE, cwd=wt)
                p = run(["git", "write-tree"], cwd=wt, check=False)
                return Text(p.stdout.strip() if p.returncode == 0 else None)
            case Head():
                return Text(out(["git", "rev-parse", "HEAD"], cwd=wt))
            case HeadTree():
                return Text(out(["git", "rev-parse", "HEAD^{tree}"], cwd=wt))
            case CommitUnion(tree):
                p = run(["git", "commit-tree", tree, "-p", rev(main_ref()) or "HEAD",
                         "-m", strings.UNION_COMMIT_SUBJECT], env=MACHINE_ENV, check=False)
                return Text(p.stdout.strip() if p.returncode == 0 else None)
            case Checkout(union):
                self.at.setdefault(union, wt)
                return ran(git("reset", "--hard", "--quiet", union, cwd=wt, check=False))
            case CollectFetch():
                self.fetch.result()
                return Ran(True, "")
            case Refresh():
                self.refresh()
                return Ran(True, "")
            case AwaitPending(union):
                if self.await_pending is None:
                    return Flag(False)
                return Flag(self.await_pending(self.at[union], union))
            case Cuts(union, branches):
                return Cut(self.cuts(self.at[union], union, branches) if self.cuts is not None else [])
            case Stage(union, names, branches, lanes):
                return Flag(self.stage(self.at[union], union, names, branches,
                                       [(self.at[lane.union], lane) for lane in lanes]))
            case _:
                return super().__call__(ask)
