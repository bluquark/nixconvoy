"""`nixconvoy review`: a pending landing shown as its COMMIT_MSG.md, then
its diff - `git diff --cached`, from the merge-base with origin's default
branch when the checkout is on a work branch (one deploy-branches lists),
a subagent worktree or a worktree beside the primary one on a branch of
its own (a submodule worktree reviewed from its repository), so it shows
everything pending against main (COMMIT_MSG.md itself left out unless
paths are given); then each submodule worktree under the checkout, from
the merge-base with its origin's default branch.  Leading arguments name
what to show instead of this checkout (names.resolve): a subagent
worktree, a submodule of this checkout (its worktree against its origin's
default branch) or a branch (the worktree checked out on it, else the
branch's tip against its merge-base, the message from its tree); one that
is none of these, nor a path here, is an error, never a diff argument.  --with-subagents shows this
checkout and then each subagent worktree nested in it.  The rest goes to
git diff.  One pager stream on a terminal keeps the messages visible on
long diffs; colors are per-line, since less -R drops attributes at line
boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from typing import IO

from . import names, submodule
from .checkout import sub_parent
from .config import BRANCHES_FILE, MSG_FILE
from .proc import die, painter, run
from .submodule import Sub

EXCLUDE_MSG = f":(top,exclude){MSG_FILE}"


def worktrees(here: Path) -> dict[Path, str | None]:
    """Every worktree of the repository at `here` with its branch."""
    found: dict[Path, str | None] = {}
    cur: Path | None = None
    for ln in run(["git", "worktree", "list", "--porcelain"], cwd=str(here)).stdout.splitlines():
        if ln.startswith("worktree "):
            cur = Path(ln[len("worktree "):])
            found[cur] = None
        elif ln.startswith("branch ") and cur is not None:
            found[cur] = ln[len("branch "):].removeprefix("refs/heads/")
    return found


def toplevel(d: Path) -> Path:
    return Path(run(["git", "rev-parse", "--show-toplevel"], cwd=str(d)).stdout.strip())


def default_of(d: Path) -> str:
    """origin's default branch of the repository at `d`, as a remote ref
    name (origin/HEAD, else main)."""
    head = run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd=str(d),
               check=False).stdout.strip()
    return head or "origin/main"


def targets(args: list[str], here: Path) -> tuple[list[Path | Sub | str], list[str]]:
    """The leading names resolved (names.resolve; --with-subagents: this
    checkout and its subagent worktrees), and the arguments left for git
    diff."""
    top = toplevel(here)
    found: list[Path | Sub | str] = []
    with_subs = False
    rest = list(args)
    while rest:
        a = rest[0]
        if a == "--with-subagents":
            with_subs = True
            rest.pop(0)
            continue
        if a.startswith("-") or (here / a).exists():
            break
        got = names.resolve(a, here)
        if got is None:
            die(f"review: no subagent worktree, submodule, branch or path named {a}")
        found.append(got)
        rest.pop(0)
    if with_subs:
        found = [top, *sorted(w for w in worktrees(here) if top in w.parents)]
    return found, rest


def listed_in(top: Path, branch: str) -> bool:
    """Whether `top`'s deploy-branches lists `branch`."""
    try:
        pats = [ln.strip() for ln in (top / BRANCHES_FILE).read_text(encoding="utf-8").splitlines()]
    except OSError:
        return False
    return any(fnmatchcase(branch, p) for p in pats if p and not p.startswith("#"))


def base_of(d: Path, all_wts: dict[Path, str | None]) -> str | None:
    """The merge-base the diff of the worktree `d` starts from, when it is
    one whose whole branch is pending: a subagent worktree, a work branch,
    or a worktree beside the primary one on a branch of its own."""
    top = toplevel(d)
    cur = all_wts.get(top) or run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=str(d),
                                  check=False).stdout.strip()
    default = default_of(d)
    primary = next(iter(worktrees(d)), None)
    if sub_parent(top) is None and not (cur and f"origin/{cur}" != default
                                        and (listed_in(top, cur) or primary != top)):
        return None
    p = run(["git", "merge-base", default, "HEAD"], cwd=str(d), check=False)
    return p.stdout.strip() or None if p.returncode == 0 else None


class Out:
    """The review's output: the terminal's pager, else stdout."""

    def __init__(self) -> None:
        self.paint = painter()
        self.color = self.paint("x", "bold") != "x"
        self.pager: subprocess.Popen[str] | None = None
        self.sink: IO[str] = sys.stdout
        if self.color:
            cmd = run(["git", "var", "GIT_PAGER"], check=False).stdout.strip() or "less"
            if cmd != "cat":
                env = {**os.environ, "LESS": os.environ.get("LESS", "FRX"), "LV": os.environ.get("LV", "-c")}
                self.pager = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, text=True, env=env)  # noqa: S602 - git's pager, a shell command
                assert self.pager.stdin is not None
                self.sink = self.pager.stdin

    def line(self, text: str, style: str = "") -> None:
        if style:
            text = self.paint(text, style.replace("heading", "bold yellow"))
        self.sink.write(text + "\n")
        self.sink.flush()

    def diff(self, d: Path, *args: str) -> None:
        self.sink.flush()
        subprocess.run(["git", "-C", str(d), "diff", *(["--color"] if self.color else []), *args],
                       stdout=self.sink, check=False)

    def close(self) -> int:
        if self.pager is not None:
            self.sink.close()
            return self.pager.wait()
        return 0


def message(o: Out, text: str) -> None:
    o.line(f"── {MSG_FILE} ──", "bold")
    for ln in text.splitlines():
        o.line(ln, "cyan")
    o.line("")


def show_branch(o: Out, here: Path, branch: str, args: list[str]) -> None:
    p = run(["git", "merge-base", default_of(here), branch], cwd=str(here), check=False)
    base = p.stdout.strip()
    if p.returncode != 0 or not base:
        o.line(f"review: {branch} has no merge-base with {default_of(here)}")
        return
    msg = run(["git", "show", f"{branch}:{MSG_FILE}"], cwd=str(here), check=False)
    if msg.returncode == 0:
        message(o, msg.stdout)
    o.diff(here, base, branch, *args, "--", EXCLUDE_MSG)


def show(o: Out, d: Path, args: list[str], all_wts: dict[Path, str | None]) -> None:
    top = toplevel(d)
    base = base_of(d, all_wts)
    diff_args = list(args)
    if base is not None:
        diff_args = [base, *args] + ([] if any(not a.startswith("-") for a in args) else ["--", EXCLUDE_MSG])
    if (top / MSG_FILE).is_file():
        message(o, (top / MSG_FILE).read_text(encoding="utf-8"))
    o.diff(d, "--cached", *diff_args)
    for sub in submodule.present(top):
        a = sub.dir
        default = default_of(a)
        if default == "origin/main" and run(["git", "rev-parse", "-q", "--verify", default], cwd=str(a),
                                            check=False).returncode != 0:
            default = "origin/master"
        p = run(["git", "merge-base", default, "HEAD"], cwd=str(a), check=False)
        if p.returncode != 0:
            continue
        o.line("")
        o.line(f"── submodule {sub.name} ──", "bold")
        o.diff(a, "--cached", p.stdout.strip(), "--", EXCLUDE_MSG)


def main(args: list[str]) -> int:
    here = Path.cwd()
    found, rest = targets(args, here)
    all_wts = worktrees(here)
    o = Out()
    try:
        if not found:
            show(o, here, rest, all_wts)
        for t in found:
            if isinstance(t, str):
                o.line(f"══ branch {t} ══", "heading")
                show_branch(o, here, t, rest)
            elif isinstance(t, Sub):
                o.line(f"══ submodule {t.name} ══", "heading")
                show(o, t.dir, rest, all_wts)
            else:
                parent = sub_parent(t)
                o.line(f"══ {parent.name + '/' if parent else ''}{t.name} ══", "heading")
                show(o, t, rest, all_wts)
            o.line("")
    except BrokenPipeError:
        pass
    return o.close()
