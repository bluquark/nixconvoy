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
from .checkout import MACHINE_ENV, GitQueries, add_worktree, allowed_branches, drop_worktree, ran, summarize
from .config import MSG_FILE, cfg
from .proc import git, ok, out, rev, run, solve_with_mergiraf


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

    def __call__(self, ask: dict[str, object]) -> dict[str, object]:
        def arg(k: str) -> str:
            return str(ask.get(k))

        kind, wt = ask.get("ask"), self.wt
        if kind == "listed":
            return {"names": sorted(allowed_branches())}
        if kind == "branches":
            # Every work-branch-shaped ref: the listed ones, and the ones
            # shaped like this machine's (any machine's), so the core can warn
            # of a pushed work branch nobody registered.
            shape = cfg().branch_prefix.replace("{machine}", "*") + "*"
            pats = {*allowed_branches(), shape}
            listing = out(["git", "for-each-ref", "--format=%(refname:strip=3)%09%(objectname)",
                           "refs/remotes/origin/"])
            refs = [ln.split("\t", 1) for ln in listing.splitlines()]
            return {"refs": [r for r in refs if any(fnmatchcase(r[0], p) for p in pats)]}
        if kind == "unchanged":
            base = out(["git", "merge-base", "origin/main", arg("sha")])
            return {"flag": ok(["git", "diff", "--quiet", base, arg("sha"), "--", ".",
                                f":(exclude){MSG_FILE}"])}
        if kind == "worktree":
            self.drop()
            self.wt = add_worktree(self.base())
            self.wts.append(self.wt)
            return {"ran": True, "detail": ""}
        if kind == "summary":
            return {"text": summarize(arg("sha"))}
        if kind == "merge":
            p = git("merge", "--quiet", "--no-edit", "-m", f"union {arg('name')}", arg("sha"),
                    cwd=wt, env=MACHINE_ENV, check=False)
            files: list[str] = [] if p.returncode == 0 else out(
                ["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt).splitlines()
            return {"merge": p.returncode == 0, "files": files,
                    "detail": (p.stderr or "").strip()}
        if kind == "drop_file":
            return ran(git("rm", "--quiet", "-f", arg("file"), cwd=wt, check=False))
        if kind == "solve":
            if submodule.owner(Path(wt), arg("file")) is not None:
                return {"flag": submodule.solve_union(Path(wt), arg("file"))}
            return {"flag": solve_with_mergiraf(wt, arg("file"))}
        if kind == "add":
            return ran(git("add", "--", arg("file"), cwd=wt, check=False))
        if kind == "abort_merge":
            return ran(git("merge", "--abort", cwd=wt, check=False))
        if kind == "commit_merge":
            return ran(git("commit", "--quiet", "--no-edit", "--no-verify", cwd=wt,
                           env=MACHINE_ENV, check=False))
        if kind == "strip_tree":
            git("rm", "--quiet", "--ignore-unmatch", "-f", MSG_FILE, cwd=wt)
            p = run(["git", "write-tree"], cwd=wt, check=False)
            return {"text": p.stdout.strip() if p.returncode == 0 else None}
        if kind == "head":
            return {"text": out(["git", "rev-parse", "HEAD"], cwd=wt)}
        if kind == "head_tree":
            return {"text": out(["git", "rev-parse", "HEAD^{tree}"], cwd=wt)}
        if kind == "commit_union":
            p = run(["git", "commit-tree", arg("tree"), "-p", rev("refs/remotes/origin/main") or "HEAD",
                     "-m", "deploy union of work branches"], env=MACHINE_ENV, check=False)
            return {"text": p.stdout.strip() if p.returncode == 0 else None}
        if kind == "checkout":
            return ran(git("reset", "--hard", "--quiet", arg("union"), cwd=wt, check=False))
        if kind == "collect_fetch":
            self.fetch.result()
            return {"ran": True, "detail": ""}
        if kind == "refresh":
            self.refresh()
            return {"ran": True, "detail": ""}
        if kind == "await_pending":
            if self.await_pending is None:
                return {"flag": False}
            return {"flag": self.await_pending(wt, arg("union"))}
        if kind == "stage":
            listed, branches = ask.get("listed"), ask.get("branches")
            assert isinstance(listed, list) and isinstance(branches, list)
            return {"flag": self.stage(wt, arg("union"), [str(b) for b in listed],
                                       [(str(n), str(t)) for n, t in branches])}
        return super().__call__(ask)
