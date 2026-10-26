"""The union core's queries: git in the union worktree on the deploy's
snapshot, the fetch the deploy started, and `stage` - the deploy's eval,
build and copy of a union (deploy.deploy)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from concurrent.futures import Future
from fnmatch import fnmatchcase
from pathlib import Path

from . import submodule
from .checkout import (
    MACHINE_ENV,
    GitQueries,
    add_worktree,
    allowed_branches,
    drop_worktree,
    listed,
    ran,
    summarize,
)
from .config import MSG_FILE, cfg
from .proc import git, ok, out, rev, run, solve_with_mergiraf
from .wire import (
    AbortMerge,
    Add,
    AwaitPending,
    Branches,
    Checkout,
    CollectFetch,
    CommitMerge,
    CommitUnion,
    DropFile,
    Flag,
    Head,
    HeadTree,
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
                 stage: Callable[[str, str, list[str], list[tuple[str, str]]], bool],
                 await_pending: Callable[[str, str], bool] | None = None) -> None:
        super().__init__()
        self.base, self.fetch, self.refresh, self.stage = base, fetch, refresh, stage
        self.await_pending = await_pending
        self.wt = ""
        self.wts: list[str] = []

    def drop(self) -> None:
        while self.wts:
            drop_worktree(self.wts.pop())

    def __call__(self, ask: PlanQuery) -> PlanReply:
        wt = self.wt
        match ask:
            case Listed():
                return Names(sorted(allowed_branches()))
            case Branches():
                # Every work-branch-shaped ref: the listed ones, and the ones
                # shaped like this machine's (any machine's), so the core can warn
                # of a pushed work branch nobody registered.
                shape = cfg().branch_prefix.replace("{machine}", "*") + "*"
                pats = allowed_branches()
                listing = out(["git", "for-each-ref", "--format=%(refname:strip=3)%09%(objectname)",
                               "refs/remotes/origin/"])
                refs = [(n, t) for n, t in (ln.split("\t", 1) for ln in listing.splitlines())]
                return Refs([r for r in refs if listed(r[0], pats) or fnmatchcase(r[0], shape)])
            case Unchanged(sha):
                base = out(["git", "merge-base", "origin/main", sha])
                return Flag(ok(["git", "diff", "--quiet", base, sha, "--", ".", f":(exclude){MSG_FILE}"]))
            case Worktree():
                self.drop()
                self.wt = add_worktree(self.base())
                self.wts.append(self.wt)
                return Ran(True, "")
            case Summary(sha):
                return Text(summarize(sha))
            case Merge(name, sha):
                p = git("merge", "--quiet", "--no-edit", "-m", f"union {name}", sha, cwd=wt, env=MACHINE_ENV, check=False)
                files: list[str] = [] if p.returncode == 0 else out(
                    ["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt).splitlines()
                return Merged(p.returncode == 0, files, (p.stderr or "").strip())
            case DropFile(file):
                return ran(git("rm", "--quiet", "-f", file, cwd=wt, check=False))
            case Solve(file):
                if submodule.owner(Path(wt), file) is not None:
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
                p = run(["git", "commit-tree", tree, "-p", rev("refs/remotes/origin/main") or "HEAD",
                         "-m", "deploy union of work branches"], env=MACHINE_ENV, check=False)
                return Text(p.stdout.strip() if p.returncode == 0 else None)
            case Checkout(union):
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
                return Flag(self.await_pending(wt, union))
            case Stage(union, names, branches):
                return Flag(self.stage(wt, union, names, branches))
            case _:
                return super().__call__(ask)
