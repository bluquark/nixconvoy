"""Submodules: the flake inputs a checkout develops in place.  Each is a
git submodule of this repository, its .gitmodules entry named after the
flake input, which takes it as a relative path (`path:./<dir>`: the
lock names no rev); the gitlink in the tree is the pin, and nix fetches
the submodule from its .gitmodules url at that rev (`inputs.self.submodules
= true` in the flake), so the pinned commit must be on the input's
origin.  A submodule worked on is a git worktree of the input's
repository (nixconvoy.toml's [submodules.<input>].repo, which shares
its objects and holds every branch of the input: `git submodule update`
would clone a second copy under .git/modules) on a branch named after
the checkout's work branch.  A deploy commits the worktree's staged
state onto that branch, pushes it and stages the gitlink on the pushed
rev, so the increment carries the submodule as a tree change, which the
union, the eval memo and the lineage handle like any other.  `land` lands
the branch on the repository's default branch first and pins the
gitlink there; `unapply` resets it onto the pin of the rev the checkout
goes back to (`--back-to origin/main`: the gitlink restored to
origin/main's).  The worktree stays either way, its
branch reset onto the default branch like an agent checkout's, and
`close <input>` removes the worktree with its branch (`--force`: with
whatever it holds, the pin put back).  Two checkouts
pinning the same submodule on different branches meet in the deploy's
union as a gitlink conflict (solve_union): the branches are merged in the
repository, the merge pushed to the origin's refs/nixconvoy/union/<input>/<sha>
and pinned - a derived value like the union commit, deterministic across
retries; and once one of them lands, the other's rebase onto main meets
the landed gitlink, which rebases its branch onto the landed default
branch (solve_rebase)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple

from . import proc
from .checkout import (
    DEPLOYING_REFS,
    MACHINE_ENV,
    PENDING_REFS,
    TMP_PREFIX,
    allowed_branches,
    fold_subject,
    index_tree,
    landing_message,
    tree_less_msg,
)
from .config import MSG_FILE, cfg
from .guard import decide
from .proc import NOTHING_DONE, Fail, die, git, is_ancestor, ok, out, run, solve_with_mergiraf

# On a submodule's origin: every tip a branch left behind (a reset, a
# rebase, its removal) as refs/nixconvoy/past/<branch>/<sha>, since nix
# fetches a rev only while a ref holds it and a work branch pushed goes
# on pinning the old tip until its next deploy's push; a landing deletes
# those no ref of this repository's origin (a work branch, a deploy
# record, an announcement) pins any more.
PAST_REFS = "refs/nixconvoy/past"
# On a submodule's origin: the merges of its branches the deploys' unions
# carry (union_rev), refs/nixconvoy/union/<input>/<sha>, kept.
UNION_REFS = "refs/nixconvoy/union"
REFSPECS = ("+refs/heads/*:refs/remotes/origin/*", f"+{PAST_REFS}/*:{PAST_REFS}/*",
            f"+{UNION_REFS}/*:{UNION_REFS}/*")
# In a submodule repository's common git dir once nixconvoy has worked on
# it: a hook can then tell an agent's commit on its default branch (in the
# repository's own checkout) from work in a planted worktree.
MARKER = "nixconvoy-submodule"
GITLINK = "160000"


class Sub(NamedTuple):
    name: str  # the flake input and the .gitmodules name
    repo: Path  # the repository's checkout here; its origin is what nix fetches
    path: str  # the submodule's path in the tree (.gitmodules)
    dir: Path  # <checkout>/<path>
    pin_file: str | None = None  # the repo-relative pin file (nixconvoy.toml's pin_file), if any


class Entry(NamedTuple):
    path: str
    url: str


def configured() -> dict[str, Path]:
    return {n: Path(c.repo).expanduser() for n, c in cfg().submodules.items()}


def pin_files() -> dict[str, str]:
    return {n: c.pin_file for n, c in cfg().submodules.items() if c.pin_file}


def sub_of(name: str, entry: Entry, top: Path) -> Sub:
    return Sub(name, configured()[name], entry.path, top / entry.path, pin_files().get(name))


def entries(top: Path) -> dict[str, Entry]:
    """The configured submodules' .gitmodules entries under `top` (the
    checkout, or a worktree of it): name -> (path, url)."""
    f = top / ".gitmodules"
    if not f.is_file():
        return {}
    found: dict[str, dict[str, str]] = {}
    for ln in run(["git", "config", "--file", str(f), "--list"], check=False).stdout.splitlines():
        key, _, value = ln.partition("=")
        m = re.fullmatch(r"submodule\.(.+)\.(path|url)", key)
        if m:
            found.setdefault(m.group(1), {})[m.group(2)] = value
    return {n: Entry(e["path"], e.get("url", "")) for n, e in found.items() if n in configured() and "path" in e}


def present(top: Path) -> list[Sub]:
    """The configured submodules with a worktree planted under `top`."""
    return [sub_of(n, e, top) for n, e in entries(top).items() if (top / e.path / ".git").exists()]


def at_path(top: Path, path: str) -> Sub | None:
    """The configured submodule at `path` in the tree under `top`."""
    for n, e in entries(top).items():
        if e.path == path:
            return sub_of(n, e, top)
    return None


def owner(top: Path, path: str) -> Sub | None:
    """The configured submodule `path` belongs to: its gitlink, or its pin file."""
    for n, e in entries(top).items():
        if path in (e.path, pin_files().get(n)):
            return sub_of(n, e, top)
    return None


def paths(top: Path) -> set[str]:
    return {e.path for e in entries(top).values()}


def branch_at(d: Path) -> str | None:
    p = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=str(d), check=False)
    return p.stdout.strip() or None


def default_branch_of(repo: Path) -> str | None:
    """The submodule repository's default branch (origin/HEAD, else main
    or master), None when origin has none of them."""
    head = run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
               cwd=str(repo), check=False).stdout.strip()
    if head.startswith("origin/"):
        return head[len("origin/"):]
    for b in ("main", "master"):
        if ok(["git", "rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}"], cwd=str(repo)):
            return b
    return None


def default_branch(repo: Path) -> str:
    return default_branch_of(repo) or die(f"{repo}: origin has neither a HEAD, main nor master branch")


def has_own(sub: Sub, default: str) -> bool:
    """Whether the worktree's HEAD carries commits origin/<default> lacks."""
    return not ok(["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{default}"], cwd=str(sub.dir))


def rev_in(d: Path, ref: str) -> str | None:
    p = run(["git", "rev-parse", "-q", "--verify", ref], cwd=str(d), check=False)
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def staged(d: Path, *, less_msg: bool = False) -> bool:
    """Whether `d`'s index differs from HEAD (`less_msg`: MSG_FILE aside,
    which a landing reads from the worktree and never deploys)."""
    if less_msg:
        return index_tree(str(d), less_msg=True) != tree_less_msg("HEAD", cwd=str(d))
    return index_tree(str(d)) != out(["git", "rev-parse", "HEAD^{tree}"], cwd=str(d))


def mark(repo: Path) -> None:
    """Leave MARKER in the submodule repository's common git dir."""
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                 cwd=str(repo), check=False).stdout.strip()
    if common and not (Path(common) / MARKER).exists():
        with contextlib.suppress(OSError):
            (Path(common) / MARKER).write_text(f"{Path.cwd()}\n")


# --- the pin ---------------------------------------------------------------

def pin(sub: Sub, top: Path) -> str | None:
    """The rev the index of `top` pins the submodule on: what the deploy
    snapshots."""
    for ln in run(["git", "ls-files", "-s", "--", sub.path], cwd=str(top), check=False).stdout.splitlines():
        meta, _, path = ln.partition("\t")
        mode, sha, stage = meta.split()
        if path == sub.path and mode == GITLINK and stage == "0":
            return sha
    return None


def pin_at(commit: str, path: str, cwd: Path) -> str | None:
    """The rev `commit`'s tree pins `path` on (the gitlink needs no object here)."""
    return rev_in(cwd, f"{commit}:{path}")


def set_pin(sub: Sub, sha: str, top: Path) -> None:
    """The index of `top` pinning the submodule on `sha`, staged - with its
    pin file, when one is configured: the rev and its date (commit_time)
    as JSON, written and staged beside the gitlink."""
    git("update-index", "--add", "--cacheinfo", f"{GITLINK},{sha},{sub.path}", cwd=str(top))
    if sub.pin_file:
        f = top / sub.pin_file
        f.parent.mkdir(parents=True, exist_ok=True)
        date = commit_time(sub, sha, entries(top)[sub.name].url)
        f.write_text(json.dumps({"committed": date, "rev": sha}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        git("add", "--", sub.pin_file, cwd=str(top))


def pin_file_rev(sub: Sub, top: Path) -> str | None:
    """The rev the submodule's pin file under `top` names, if it is one."""
    assert sub.pin_file
    try:
        d = json.loads((top / sub.pin_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rev = d.get("rev") if isinstance(d, dict) else None
    return rev if isinstance(rev, str) else None


def commit_time(sub: Sub, sha: str, url: str) -> int:
    """The committer date of `sha` (seconds since the epoch; a union
    merge's is a second past its parents', merge_commit), from the
    repository here, else the mirror of `url` the unions merge in (store),
    fetching once."""
    repo = store(sub.name, url)
    for fetch in (False, True):
        if fetch and not fetch_once(sub.name, repo):
            break
        p = run(["git", "show", "-s", "--format=%ct", f"{sha}^{{commit}}", "--"], cwd=str(repo), check=False)
        if p.returncode == 0 and p.stdout.strip().isdigit():
            return int(p.stdout.strip())
    return die(f"submodule {sub.name}: {sha[:12]} is not in {repo}, so its pin file cannot name the commit's date")


def heal_pin_files(top: Path, wb: str | None, dry: bool) -> list[str]:
    """Each configured pin file under `top` that does not name the rev the
    index pins its submodule on (the gitlink moved by hand) rewritten and
    staged, one line each; only said, with no work branch to carry it or
    for a dry activation."""
    told = []
    for n, e in entries(top).items():
        if n not in pin_files():
            continue
        sub = sub_of(n, e, top)
        cur = pin(sub, top)
        if cur is None or pin_file_rev(sub, top) == cur:
            continue
        if wb is None or dry:
            print(f"warning: {sub.pin_file} does not name {cur[:12]}, which the tree pins submodule {n} on: "
                  f"a deploy from a checkout on a work branch rewrites it", file=sys.stderr)
            continue
        set_pin(sub, cur, top)
        told.append(f"submodule {n}: {sub.pin_file} rewritten for {cur[:12]}")
    return told


def gitlink_stages(wt: Path, path: str) -> dict[int, str]:
    """The conflicted gitlink `path` in `wt`: stage -> rev."""
    found: dict[int, str] = {}
    for ln in run(["git", "ls-files", "-u", "--", path], cwd=str(wt), check=False).stdout.splitlines():
        meta, _, p = ln.partition("\t")
        mode, sha, stage = meta.split()
        if p == path and mode == GITLINK:
            found[int(stage)] = sha
    return found


def off_default(sub: Sub, top: Path) -> bool:
    """Whether the pin names a rev the repository's origin default branch
    lacks (or none it holds): a work branch's.  Not knowable without the
    repository on this machine, or when its origin cannot be fetched
    (said): then False."""
    rev = pin(sub, top)
    if rev is None or not (sub.repo / ".git").exists():
        return False
    default = default_branch_of(sub.repo)
    if default is None:
        return False
    if rev_in(sub.repo, f"{rev}^{{commit}}") is None:
        if not fetch_once(sub.name, sub.repo):
            return False
        if rev_in(sub.repo, f"{rev}^{{commit}}") is None:
            return True
    return not is_ancestor(rev, f"refs/remotes/origin/{default}", cwd=str(sub.repo))


class Pending(NamedTuple):
    """A submodule branch solve_rebase rebased for a work branch's rebase
    onto main: pushed once that rebase succeeds, put back if it aborts."""
    sub: Sub
    wb: str
    was: str
    head: str


# Per run: the submodule repositories fetched, the fetches under way beside
# the deploy's own (prefetch), and the rebased submodules.
FETCHED: set[Path] = set()
PREFETCH: dict[Path, Future[bool]] = {}
PENDING: dict[tuple[str, str], Pending] = {}


def fetch_refs(name: str, repo: Path) -> bool:
    """Fetch the submodule repository `repo`'s origin: branches, past and
    union refs, the gone ones pruned; whether it is fetched (else said)."""
    p = run(["git", "fetch", "--quiet", "--prune", "origin", *REFSPECS],
            cwd=str(repo), check=False, timeout=300)
    if p.returncode != 0:
        print(f"note: submodule {name}: fetching its origin failed:\n{(p.stderr or '').strip()}", file=sys.stderr)
        return False
    FETCHED.add(repo)
    return True


def fetch_once(name: str, repo: Path) -> bool:
    """fetch_refs once per run, a prefetch under way waited for."""
    if repo in FETCHED:
        return True
    under_way = PREFETCH.pop(repo, None)
    return under_way.result() if under_way is not None else fetch_refs(name, repo)


def prefetch(top: Path, pool: ThreadPoolExecutor) -> None:
    """The present submodules' origins fetched on `pool`, beside the
    deploy's own fetch, for the catch-up of the submodule step to find done."""
    for sub in present(top):
        if sub.repo not in FETCHED and sub.repo not in PREFETCH:
            PREFETCH[sub.repo] = pool.submit(fetch_refs, sub.name, sub.repo)


def pinned_revs(sub: Sub) -> set[str]:
    """Every rev of the submodule pinned by a ref this repository's origin
    holds, as fetched: its branches (main, the work branches, the deploy
    records') and the deploys' announcements and records under way."""
    commits = set(out(["git", "for-each-ref", "--format=%(objectname)", "refs/remotes/origin/",
                       PENDING_REFS, DEPLOYING_REFS]).split())
    listing = run(["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
                  input="".join(f"{c}:{sub.path}\n" for c in sorted(commits)), check=False).stdout
    return {sha for sha, typ in (ln.split() for ln in listing.splitlines() if len(ln.split()) == 2)
            if typ == "submodule"}


def prune_past_refs(sub: Sub) -> None:
    """The past refs nothing pins any more deleted, on the origin and here
    (a failure is a warning)."""
    wanted = pinned_revs(sub)
    stale = [r for r in out(["git", "for-each-ref", "--format=%(refname)", f"{PAST_REFS}/"], cwd=str(sub.repo)).split()
             if r.rsplit("/", 1)[1] not in wanted]
    if not stale:
        return
    p = run(["git", "-c", "core.hooksPath=/dev/null", "push", "--quiet", "origin", *(f":{r}" for r in stale)],
            cwd=str(sub.repo), check=False, timeout=120)
    if p.returncode != 0:
        print(f"warning: submodule {sub.name}: deleting its past refs on its origin failed:\n"
              f"{(p.stderr or '').strip()}", file=sys.stderr)
        return
    for r in stale:
        git("update-ref", "-d", r, cwd=str(sub.repo), check=False)


@contextlib.contextmanager
def repo_lock(repo: Path) -> Iterator[None]:
    """A lock on the submodule repository, serialising the deploys merging
    in it (two subagents' unions at once)."""
    common = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(repo)))
    with (common / "nixconvoy-submodule.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        yield


def merge_commit(name: str, repo: Path, ours: str, theirs: str, subject: str) -> str | None:
    """`theirs` merged into `ours` in a throwaway worktree of `repo`
    (mergiraf on the conflicts, MSG_FILE dropped) as a deterministic
    commit, dated a second past the newer of the two (so a pin file's
    date keeps rising through nested unions): its sha, or None when they
    conflict (said on stderr)."""
    newest = max(int(out(["git", "show", "-s", "--format=%ct", f"{sha}^{{commit}}", "--"], cwd=str(repo)))
                 for sha in (ours, theirs))
    env = {**MACHINE_ENV, "GIT_AUTHOR_DATE": f"{newest + 1} +0000", "GIT_COMMITTER_DATE": f"{newest + 1} +0000"}
    wt = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}submodule-merge.", dir="/tmp")
    try:
        git("worktree", "add", "--detach", "--quiet", wt, ours, cwd=str(repo))
        p = git("merge", "--quiet", "--no-edit", "-m", subject, theirs, cwd=wt, env=env, check=False)
        if p.returncode != 0:
            for f in out(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt).splitlines():
                if f == MSG_FILE:
                    git("rm", "--quiet", "-f", f, cwd=wt, check=False)
                elif solve_with_mergiraf(wt, f):
                    git("add", "--", f, cwd=wt, check=False)
                else:
                    print(f"note: submodule {name}: {ours[:12]} and {theirs[:12]} conflict in {f}", file=sys.stderr)
                    return None
            git("commit", "--quiet", "--no-edit", "--no-verify", cwd=wt, env=env)
        return out(["git", "rev-parse", "HEAD"], cwd=wt)
    finally:
        with proc.finishing():
            git("worktree", "remove", "--force", wt, cwd=str(repo), check=False)
        shutil.rmtree(wt, ignore_errors=True)


def mirror_dir(name: str) -> Path:
    return proc.xdg_dir("XDG_CACHE_HOME", ".cache") / "nixconvoy" / "submodules" / f"{name}.git"


def store(name: str, url: str) -> Path:
    """The repository the submodule `name`'s branches are merged in: its
    configured checkout when it is here, else a bare mirror of `url`
    under the cache dir (a machine deploying without the repository)."""
    repo = configured().get(name)
    if repo is not None and (repo / ".git").exists():
        return repo
    cache = mirror_dir(name)
    if not cache.is_dir():
        cache.parent.mkdir(parents=True, exist_ok=True)
        p = run(["git", "clone", "--quiet", "--bare", url, str(cache)], check=False, timeout=600)
        if p.returncode != 0:
            die(f"submodule {name}: cloning {url} to union its branches failed:\n{(p.stderr or '').strip()}")
    return cache


def union_rev(sub: Sub, url: str, ours: str, theirs: str) -> str | None:
    """The rev carrying both `ours` and `theirs`, two pins of the
    submodule: the one containing the other, else their merge (mergiraf on
    the conflicts) as a deterministic commit pushed to UNION_REFS/<name>/<sha>
    on the origin, to be pinned there; None when they conflict (said on
    stderr)."""
    repo = store(sub.name, url)
    with repo_lock(repo):
        if not fetch_once(sub.name, repo):
            return None
        for sha in (ours, theirs):
            if rev_in(repo, f"{sha}^{{commit}}") is None:
                print(f"note: submodule {sub.name}: {sha[:12]} is not on its origin", file=sys.stderr)
                return None
        if is_ancestor(theirs, ours, cwd=str(repo)):
            return ours
        if is_ancestor(ours, theirs, cwd=str(repo)):
            return theirs
        sha = merge_commit(sub.name, repo, ours, theirs, f"union of {sub.name}")
        if sha is None:
            return None
        ref = f"{UNION_REFS}/{sub.name}/{sha}"
        p = run(["git", "-c", "core.hooksPath=/dev/null", "push", "--quiet", "--force", "origin", f"{sha}:{ref}"],
                cwd=str(repo), check=False, timeout=120)
        if p.returncode != 0:
            die(f"submodule {sub.name}: pushing its union to {ref} failed:\n{(p.stderr or '').strip()}")
        git("update-ref", ref, sha, cwd=str(repo), check=False)
    print(f"submodule {sub.name}: union of {ours[:12]} and {theirs[:12]} is {sha[:12]}, on {ref}")
    return sha


def solve_union(wt: Path, path: str) -> bool:
    """A deploy's union: the gitlink `path` conflicted in the union
    worktree `wt` (another branch pinned the submodule too) pinned on the
    union of the two revs (union_rev), staged.  Whether it resolved."""
    sub = owner(wt, path)
    if sub is None:
        return False
    stages = gitlink_stages(wt, sub.path)
    if not stages:
        # The pin file alone conflicted, or was resolved with its gitlink
        # already: rewritten from the gitlink.
        cur = pin(sub, wt)
        if cur is None:
            return False
        set_pin(sub, cur, wt)
        return True
    if 2 not in stages or 3 not in stages:
        return False
    sha = union_rev(sub, entries(wt)[sub.name].url, stages[2], stages[3])
    if sha is None:
        return False
    set_pin(sub, sha, wt)
    return True


def solve_rebase(wt: Path, checkout: Path, upstream: str) -> str | None:
    """A rebase of `checkout`'s work branch onto `upstream` stopped on
    gitlinks alone (in `wt`: the checkout itself, or the throwaway
    worktree a branch not checked out is rebased in) because `upstream`
    pins a submodule elsewhere (its landing) that a replayed commit pinned
    on this branch: the worktree here is rebased onto the default branch
    (once per run: every later replayed commit pins the same tip) and
    pinned on its tip, staged for `git rebase --continue`; the push waits
    for the rebase's end (push_pending, else undo_pending).  None once
    resolved; "" when the stop is not on gitlinks alone (nothing here to
    solve); else why the submodule could not follow, as the rebase's
    failure text - git merges no gitlink itself (the submodule's commits
    are not in this repository), so that is the one way through."""
    files = out(["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(wt)).splitlines()
    subs = {f: owner(wt, f) for f in files}
    if not subs or any(s is None for s in subs.values()):
        return ""
    # HEAD is detached mid-rebase: the branch is in the rebase's state.
    try:
        head_name = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "rebase-merge/head-name"],
                             cwd=str(wt))).read_text(encoding="utf-8").strip()
    except OSError:
        head_name = ""
    wb = head_name.removeprefix("refs/heads/") or branch_at(checkout) or ""
    upstream = upstream.removeprefix("refs/remotes/")
    done: set[str] = set()
    for sub in subs.values():
        assert sub is not None
        if sub.name in done:
            continue
        done.add(sub.name)
        stages = gitlink_stages(wt, sub.path)
        why = (f"{upstream} and {wb} both moved submodule {sub.name}'s pin (a landing there), which git "
               f"cannot merge itself: nixconvoy rebases the submodule's branch onto the landed one instead, but ")
        if not stages:  # the pin file alone: rewritten from the gitlink
            cur = pin(sub, wt)
            if cur is None:
                return f"{sub.pin_file} conflicts with no gitlink to rewrite it from: resolve the rebase by hand"
            set_pin(sub, cur, wt)
            continue
        if 3 not in stages:  # mine (stage 3) is the replayed commit's pin
            return why + f"one side deleted the pin: resolve the rebase by hand (`git rebase {upstream}` there)"
        pending = PENDING.get((sub.name, wb))
        if pending is not None:
            head = pending.head
        else:
            head, problem = rebase_sub(sub, wb, stages[3], wt, checkout)
            if head is None:
                return why + f"{problem}; then `nixconvoy pull`"
        set_pin(sub, head, wt)
    return None


def rebase_sub(sub: Sub, wb: str, mine: str, wt: Path, checkout: Path) -> tuple[str | None, str]:
    """solve_rebase for one submodule: its worktree under `checkout` on
    `wb` rebased onto the default branch as fetched (the push pending),
    its new tip - or None and why it cannot be, with what to do."""
    found = [s for s in present(checkout) if s.name == sub.name]
    if not found or branch_at(found[0].dir) != wb:
        return None, (f"no worktree of it on {wb} is under {checkout} to rebase: "
                      f"`nixconvoy open --submodule {sub.name}` there")
    here = found[0]
    if out(["git", "status", "--porcelain", "-uno"], cwd=str(here.dir)):
        return None, f"its worktree ({here.dir}) has edits, which that rebase cannot keep: stash or commit them"
    tip_pinned = tip_pin(wt, sub.path) or mine
    gone = reset_below_deployed(here, tip_pinned)
    if gone:  # a hand reset: pinning HEAD would drop them in silence
        return None, dropped_message(here, wb, tip_pinned, gone)
    behind = left_behind(here, tip_pinned)
    if behind:
        print(f"note: submodule {sub.name}: pinning its worktree leaves {len(behind)} deployed commit(s) "
              f"behind as such (rebased, amended or dropped):\n  " + "\n  ".join(behind), file=sys.stderr)
    if not fetch_once(sub.name, here.repo):
        return None, "its origin could not be fetched (above)"
    was = rev_in(here.dir, "HEAD")
    default = default_branch(here.repo)
    p = git("rebase", "--quiet", f"refs/remotes/origin/{default}", cwd=str(here.dir), check=False)
    if p.returncode != 0:
        git("rebase", "--abort", cwd=str(here.dir), check=False)
        return None, (f"its {wb} does not rebase cleanly onto origin/{default}: "
                      f"`git -C {here.dir} rebase origin/{default}`, resolve")
    head = rev_in(here.dir, "HEAD")
    assert head is not None and was is not None
    PENDING[sub.name, wb] = Pending(here, wb, was, head)
    print(f"submodule {sub.name}: {wb} rebased onto origin/{default} ({head[:12]})")
    return head, ""


def push_pending() -> None:
    """The rebase succeeded: the rebased submodule branches pushed."""
    for pending in PENDING.values():
        push_branch(pending.sub, pending.wb)
        print(f"submodule {pending.sub.name}: pushed {pending.wb}")
    PENDING.clear()


def undo_pending() -> None:
    """The rebase aborted: each rebased submodule put back where it was."""
    for pending in PENDING.values():
        git("reset", "--quiet", "--hard", pending.was, cwd=str(pending.sub.dir), check=False)
        print(f"submodule {pending.sub.name}: {pending.wb} put back to {pending.was[:12]}", file=sys.stderr)
    PENDING.clear()


# --- the verbs -------------------------------------------------------------

def same_repo(a: str, b: str) -> bool:
    """Whether two git urls name the same repository (scheme and .git aside)."""
    def norm(u: str) -> str:
        u = u.strip().rstrip("/").removesuffix(".git")
        u = re.sub(r"^(ssh://)?git@([^:/]+)[:/]", r"https://\2/", u)
        return u.lower()
    return norm(a) == norm(b)


def create(name: str, wb: str) -> Path:
    """`nixconvoy open --submodule <name>`: the submodule's worktree under
    this checkout, on `wb` - origin's copy of the branch when it has one
    (a worktree re-created), else a new branch off the repository's
    default."""
    repo = configured_repo(name)
    if not (repo.is_dir() and ok(["git", "rev-parse", "--git-dir"], cwd=str(repo))):
        die(f"submodule {name}: {repo} is not a git repository")
    top = Path.cwd()
    entry = entries(top).get(name)
    if entry is None:
        die(f"{name} is no submodule of this repository: `git submodule add --name {name} <url> <path>` "
            f"first, with flake.nix taking it as a relative path input")
    origin = run(["git", "remote", "get-url", "origin"], cwd=str(repo), check=False).stdout.strip()
    if not same_repo(origin, entry.url):
        die(f"submodule {name}: {repo}'s origin is {origin}, not .gitmodules' {entry.url}, "
            f"which the build fetches the pushed branch from")
    d = top / entry.path
    if (d / ".git").exists():
        cur = branch_at(d)
        if cur != wb:
            die(f"submodule {name} at {d} is on {cur or 'a detached HEAD'}, not {wb}: "
                f"`git -C {d} switch {wb}`")
        print(f"submodule {name}: {d} (on {wb})")
        return d
    if d.exists() and any(d.iterdir()):
        die(f"{d} is not empty and no worktree: move it away")
    git("worktree", "prune", cwd=str(repo), check=False)
    p = run(["git", "fetch", "--quiet", "origin"], cwd=str(repo), check=False, timeout=120)
    if p.returncode != 0:
        die(f"submodule {name}: fetching {repo}'s origin failed:\n{(p.stderr or '').strip()}")
    default = default_branch(repo)
    d.parent.mkdir(parents=True, exist_ok=True)
    remote, local = rev_in(repo, f"refs/remotes/origin/{wb}"), rev_in(repo, f"refs/heads/{wb}")
    if remote is not None and local is not None and not is_ancestor(local, remote, cwd=str(repo)):
        if not is_ancestor(remote, local, cwd=str(repo)):
            die(f"submodule {name}: the local branch {wb} ({local[:12]}) and origin's ({remote[:12]}) diverged: "
                f"reconcile them in {repo} first")
        remote = None  # the local branch is ahead (a push that failed): kept
    if remote is not None:
        git("branch", "--quiet", "-f", wb, remote, cwd=str(repo), check=False)
        git("worktree", "add", "--quiet", str(d), wb, cwd=str(repo))
        where = f"origin/{wb}"
    elif local is not None:
        git("worktree", "add", "--quiet", str(d), wb, cwd=str(repo))
        where = f"the local branch {wb}"
    else:
        git("worktree", "add", "--quiet", "-b", wb, str(d), f"refs/remotes/origin/{default}",
            cwd=str(repo))
        where = f"origin/{default}"
    # `git status` there counts against the branch it will land on.
    git("branch", "--quiet", f"--set-upstream-to=origin/{default}", wb, cwd=str(d), check=False)
    mark(repo)
    print(f"submodule {name}: {d} on {wb} (from {where})")
    return d


def configured_repo(name: str) -> Path:
    repos = configured()
    if name not in repos:
        die(f"{name} is not a configured submodule" + (
            f" (they are: {', '.join(sorted(repos))})" if repos else
            " (nixconvoy.toml: [submodules.<input>] repo = \"...\")"))
    return repos[name]


def present_one(top: Path, name: str) -> Sub:
    found = [s for s in present(top) if s.name == name]
    if not found:
        configured_repo(name)
        entry = entries(top).get(name)
        die(f"no worktree of submodule {name} under this checkout"
            + (f" ({top / entry.path})" if entry else " (nor a .gitmodules entry)"))
    return found[0]


def carries_one(sub: Sub, top: Path) -> bool:
    """Whether the submodule has something a deploy would carry into the
    tree: staged changes, or a HEAD beyond its default branch as last
    fetched that the tree does not pin yet."""
    if staged(sub.dir):
        return True
    head = rev_in(sub.dir, "HEAD")
    default = default_branch_of(sub.repo)
    return default is None or (pin(sub, top) != head and has_own(sub, default))


def reset_below_deployed(sub: Sub, pinned: str | None) -> list[str]:
    """The deployed commits a worktree reset below its own work dropped:
    when HEAD holds nothing beyond the default branch as fetched while
    `pinned` (the rev the tree deploys of this submodule) does, the
    commits of `pinned` beyond HEAD, one line each.  Empty while HEAD
    still carries work of its own - a rebase, an amend or a squash of the
    deployed commits is the branch's business, said by left_behind - or
    the tree deploys nothing of the branch's."""
    default = default_branch_of(sub.repo)
    if pinned is None or default is None or rev_in(sub.dir, f"{pinned}^{{commit}}") is None:
        return []
    upstream = f"refs/remotes/origin/{default}"
    d = str(sub.dir)
    if is_ancestor(pinned, "HEAD", cwd=d) or is_ancestor(pinned, upstream, cwd=d) \
            or not is_ancestor("HEAD", upstream, cwd=d):
        return []
    return out(["git", "log", "--format=%h %s", f"HEAD..{pinned}", "--not", upstream], cwd=d).splitlines()


def dropped_message(sub: Sub, wb: str, pinned: str, gone: list[str]) -> str:
    return (f"submodule {sub.name}'s worktree ({sub.dir}) was reset below the work the tree deploys on {wb}; "
            f"{len(gone)} commit(s) would leave the pin and the targets:\n  " + "\n  ".join(gone)
            + f"\nput them back (git -C {sub.dir} reset --keep {pinned[:12]}), or drop them deliberately: "
            f"nixconvoy close {sub.name} --force")


def left_behind(sub: Sub, pinned: str | None) -> list[str]:
    """The deployed commits (up to `pinned`) a pin on HEAD leaves behind
    as such - rebased, amended, squashed or dropped - as `git cherry` tells
    them from the patches HEAD holds: one line each."""
    d = str(sub.dir)
    if pinned is None or rev_in(sub.dir, f"{pinned}^{{commit}}") is None or is_ancestor(pinned, "HEAD", cwd=d):
        return []
    gone = [ln[2:] for ln in out(["git", "cherry", "HEAD", pinned], cwd=d).splitlines() if ln.startswith("+ ")]
    return [out(["git", "log", "-1", "--format=%h %s", sha], cwd=d) for sha in gone]


def refuse_dropped(sub: Sub, wb: str, top: Path) -> None:
    """Die when the worktree was reset below the work the tree deploys on
    `wb`, rather than pin it away in silence."""
    pinned = pin(sub, top)
    gone = reset_below_deployed(sub, pinned)
    if gone:
        assert pinned is not None
        die(dropped_message(sub, wb, pinned, gone))


def tip_pin(wt: Path, path: str) -> str | None:
    """The rev `path` is pinned on at the tip the rebase under way in `wt`
    replays (rebase-merge/orig-head: the branch as it was, tmp commits
    included), None when unknown."""
    try:
        orig = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "rebase-merge/orig-head"],
                        cwd=str(wt))).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pin_at(orig, path, wt)


def catch_up(top: Path, wb: str) -> list[str]:
    """Each submodule present under `top` (the current directory, whose
    index the pin edits) brought onto its repository's default branch as
    fetched, the way a checkout is brought onto origin/main (the rebase
    core's Onto: its staged and unstaged state riding along as tmp
    commits; a fast-forward when it has nothing of its own).  One with
    commits of its own is pushed where its origin has the branch, and a
    pin naming its old tip (a deploy's staged pin) moved to the new.  The
    problems, one line each, for the submodules left as they were: a
    rebase that is not clean, a worktree off `wb`, an origin that cannot
    be fetched while something waits to be pinned."""
    problems: list[str] = []
    for sub in present(top):
        try:
            problem = catch_up_one(sub, wb, top)
        except Fail:  # said: this submodule's alone
            problem = f"submodule {sub.name}: failed (above)"
        if problem is not None:
            problems.append(problem)
    return problems


def onto(d: Path, upstream: str) -> tuple[bool, str]:
    """The rebase core's Onto run in `d`: HEAD made to contain `upstream`,
    (ok, status)."""
    prev = Path.cwd()
    os.chdir(d)
    try:
        done = decide("rebase", mode="onto", upstream=upstream)
    finally:
        os.chdir(prev)
    assert isinstance(done, dict)
    return done.get("ok") is True, str(done.get("status"))


def catch_up_one(sub: Sub, wb: str, top: Path) -> str | None:
    """catch_up for one submodule: the problem, or None."""
    cur = branch_at(sub.dir)
    if cur != wb:
        return (f"submodule {sub.name} ({sub.dir}) is on {cur or 'a detached HEAD'}, not {wb}: "
                f"`git -C {sub.dir} switch {wb}`")
    if not fetch_once(sub.name, sub.repo):
        if carries_one(sub, top):
            return (f"submodule {sub.name}: its origin could not be fetched (above), so its branch "
                    f"cannot be brought onto the default branch before it is pinned")
        return None
    default = default_branch(sub.repo)
    was = rev_in(sub.dir, "HEAD")
    own = has_own(sub, default)
    good, status = onto(sub.dir, f"refs/remotes/origin/{default}")
    if not good:
        return f"submodule {sub.name}: {status}"
    head = rev_in(sub.dir, "HEAD")
    if head is None or was is None or head == was:
        return None
    print(f"submodule {sub.name}: {status}")
    if own and rev_in(sub.dir, f"refs/remotes/origin/{wb}") is not None:
        push_branch(sub, wb)
    if pin(sub, top) == was:
        set_pin(sub, head, top)
        print(f"submodule {sub.name}: pinned on {head[:12]} ({wb})")
    return None


def catch_up_repos() -> bool:
    """After a landing's or a pull's sweep of the checkouts: every
    configured submodule repository with a checkout here sitting on its
    default branch brought onto that branch as fetched (catch_up_repo,
    the origins fetched in parallel), as the main checkout is brought
    onto main; whether each could be, the problems said."""
    on_default = {name: repo for name, repo in configured().items()
                  if (repo / ".git").exists() and branch_at(repo) == default_branch_of(repo)}
    with ThreadPoolExecutor() as pool:
        for name, repo in on_default.items():
            if repo not in FETCHED and repo not in PREFETCH:
                PREFETCH[repo] = pool.submit(fetch_refs, name, repo)
    good = True
    for name, repo in on_default.items():
        try:
            problem = catch_up_repo(name, repo)
        except Fail:
            problem = f"{repo} (submodule {name}): failed (above)"
        if problem is not None:
            print(problem, file=sys.stderr)
            good = False
    return good


def catch_up_repo(name: str, repo: Path) -> str | None:
    """The submodule repository's own checkout (`repo`, worked in by
    nobody) brought onto its default branch as fetched the way a main
    checkout is brought onto origin/main: fast-forwarded when it holds
    nothing of its own, its edits riding along.  One whose origin cannot
    be fetched is left alone (said).  The problem, or None."""
    if not fetch_once(name, repo):
        return None
    default = default_branch(repo)
    with repo_lock(repo):  # another checkout's sweep, reaching the same repository
        good, status = onto(repo, f"refs/remotes/origin/{default}")
    if not good:
        return f"{repo} (submodule {name}): {status}"
    if status != "up to date":
        print(f"{repo} (submodule {name}): {status}")
    return None


def sync(wb: str | None, msg: str | None, *, dry: bool = False) -> list[str]:
    """A deploy's submodule step: each present submodule brought onto its
    default branch (catch_up, a problem there ending the deploy; not when
    `dry`), its staged state committed onto `wb` with `msg`, pushed, and
    the tree pinned on it, staged here.  What changed, one line each.
    `dry` (a dry activation) commits and pushes nothing, so a change it
    cannot carry is refused instead."""
    top = Path.cwd()
    told = heal_pin_files(top, wb, dry)
    found = present(top)
    if not found:
        return told
    if wb is None:
        die("a submodule worktree in the main checkout, which has no work branch to carry it: "
            "remove it, or work in a checkout on a work branch")
    for sub in found:
        cur = branch_at(sub.dir)
        if cur != wb:
            die(f"submodule {sub.name} ({sub.dir}) is on {cur or 'a detached HEAD'}, not {wb}: "
                f"`git -C {sub.dir} switch {wb}`")
        refuse_dropped(sub, wb, top)
    if not dry:  # a dry activation rebases, pushes and pins nothing
        for problem in catch_up(top, wb):
            die(f"{problem}; {NOTHING_DONE}")
    for sub in found:
        mark(sub.repo)
        if out(["git", "diff", "--name-only"], cwd=str(sub.dir)):
            print(f"note: submodule {sub.name} has unstaged edits, which are not deployed", file=sys.stderr)
        if staged(sub.dir):
            if dry:
                die(f"submodule {sub.name} has staged changes, which a dry activation cannot carry "
                    f"(the build fetches the submodule from its origin): deploy, or unstage them")
            if not msg:
                die(f'submodule {sub.name} has staged changes; describe this increment with -m "..."')
            git("commit", "--quiet", "-m", msg, cwd=str(sub.dir))
        head = rev_in(sub.dir, "HEAD")
        assert head is not None
        # Nothing of its own yet: the pin stays as it was (the branch is
        # off the default branch's tip, maybe past the pinned rev).
        default = default_branch(sub.repo)
        if ok(["git", "merge-base", "--is-ancestor", head, f"refs/remotes/origin/{default}"], cwd=str(sub.dir)):
            continue
        # Behind the landed default branch, the pin would move the input
        # backwards for every target main's tree reaches.
        if not ok(["git", "merge-base", "--is-ancestor", f"refs/remotes/origin/{default}", head], cwd=str(sub.dir)):
            die(f"submodule {sub.name}'s {wb} does not contain origin/{default}"
                + (" (a dry activation brings no submodule along): deploy first" if dry
                   else f": rebase it in {sub.dir}, then re-run"))
        if dry and (rev_in(sub.dir, f"refs/remotes/origin/{wb}") != head or pin(sub, top) != head):
            die(f"submodule {sub.name} has commits not deployed yet, which a dry activation cannot carry: "
                f"deploy first")
        if rev_in(sub.dir, f"refs/remotes/origin/{wb}") != head:
            push_branch(sub, wb)
            told.append(f"submodule {sub.name}: pushed {wb} ({head[:12]})")
        behind = left_behind(sub, pin(sub, top))
        if behind:
            print(f"note: submodule {sub.name}: pinning {head[:12]} leaves {len(behind)} deployed commit(s) "
                  f"behind as such (rebased, amended or dropped):\n  " + "\n  ".join(behind), file=sys.stderr)
        if pin(sub, top) != head:
            set_pin(sub, head, top)
            told.append(f"submodule {sub.name}: pinned on {head[:12]} ({wb})")
    return told


def land_all(wb: str, fallback_msg: str) -> None:
    """Each present submodule landed (land_one); run by land before main's
    commit, so a landing retried finds nothing left to do here."""
    for sub in present(Path.cwd()):
        land_one(sub, wb, fallback_msg)


def land_one(sub: Sub, wb: str, fallback_msg: str) -> None:
    """The submodule landed on its repository's default branch as one
    commit (the MSG_FILE in its worktree - tracked or not, it is read here
    alone - else `fallback_msg`) and pinned there, as a commit on `wb`."""
    top = Path.cwd()
    cur = branch_at(sub.dir)
    if cur != wb:
        die(f"submodule {sub.name} ({sub.dir}) is on {cur or 'a detached HEAD'}, not {wb}")
    if staged(sub.dir, less_msg=True):
        die(f"submodule {sub.name} has staged but undeployed changes; deploy or unstage them first")
    # The rebase onto the default branch needs a clean tree.
    loose = [ln for ln in out(["git", "status", "--porcelain", "-uno"], cwd=str(sub.dir)).splitlines()
             if not ln.endswith(f" {MSG_FILE}")]
    if loose:
        die(f"submodule {sub.name} has unstaged edits, which its rebase onto the default branch cannot keep: "
            f"stage and deploy them, or put them aside ({len(loose)} path(s))")
    if not fetch_refs(sub.name, sub.repo):
        die(f"submodule {sub.name}: cannot land it unfetched")
    refuse_dropped(sub, wb, top)
    default = default_branch(sub.repo)
    main = rev_in(sub.dir, f"refs/remotes/origin/{default}")
    head = rev_in(sub.dir, "HEAD")
    assert main is not None and head is not None
    # The branch's own commits squashed onto the default branch - its
    # tip merged there as a deploy's union merges it (mergiraf on the
    # conflicts); one with none follows the pin, when a landing elsewhere
    # put that ahead of it on the default branch, else keeps its rev.
    landed = head
    pinned = pin(sub, top)
    if is_ancestor(head, main, cwd=str(sub.dir)):
        if pinned is not None and pinned != head and is_ancestor(pinned, main, cwd=str(sub.dir)):
            if git("reset", "--quiet", "--keep", pinned, cwd=str(sub.dir), check=False).returncode != 0:
                git("reset", "--quiet", "--hard", pinned, cwd=str(sub.dir))
            push_branch(sub, wb)
            landed = pinned
    else:
        deployed = pinned
        if deployed != head:
            die(f"submodule {sub.name}'s HEAD ({head[:12]}) is not what the tree pins "
                f"({(deployed or 'nothing')[:12]}): deploy it first")
        merged = merge_commit(sub.name, sub.repo, main, head, f"landing of {wb}")
        if merged is None:
            die(f"submodule {sub.name}'s {wb} conflicts with origin/{default}: rebase it there "
                f"(in {sub.dir}), deploy, then re-run")
        tree = tree_less_msg(merged, cwd=str(sub.dir))
        try:
            on_file = (sub.dir / MSG_FILE).read_text(encoding="utf-8")
        except OSError:
            on_file = ""
        msg = landing_message(None, on_file, "", f"submodule {sub.name}") or fold_subject(fallback_msg)
        if tree == out(["git", "rev-parse", f"{main}^{{tree}}"], cwd=str(sub.dir)):
            landed = main
        else:
            landed = out(["git", "commit-tree", tree, "-p", main, "-m", msg], cwd=str(sub.dir))
            p = git("push", "--quiet", "origin", f"{landed}:refs/heads/{default}",
                    cwd=str(sub.dir), check=False)
            if p.returncode != 0:
                die(f"pushing submodule {sub.name}'s {default} failed (moved since fetch?); "
                    f"re-run:\n{(p.stderr or '').strip()}")
            print(f"submodule {sub.name}: landed on {default} ({landed[:12]})")
        # The worktree onto the landing; its tree is clean of edits.
        if git("reset", "--quiet", "--keep", landed, cwd=str(sub.dir), check=False).returncode != 0:
            git("reset", "--quiet", "--hard", landed, cwd=str(sub.dir))
        (sub.dir / MSG_FILE).unlink(missing_ok=True)
        push_branch(sub, wb)
    if pin(sub, top) != landed:
        set_pin(sub, landed, top)
        git("commit", "--quiet", "-m", f"submodule {sub.name}: pinned on {default}")
    prune_past_refs(sub)


def push_branch(sub: Sub, wb: str) -> None:
    """The submodule branch's tip onto its origin, leased on the tip
    fetched; a tip it leaves behind (a reset, a rebase) stays under
    PAST_REFS, pinned by the work branch as pushed until its next push."""
    head = rev_in(sub.dir, "HEAD")
    old = rev_in(sub.dir, f"refs/remotes/origin/{wb}")
    if head is None or old == head:
        return
    past = [f"+{old}:{PAST_REFS}/{wb}/{old}"] if old and not is_ancestor(old, head, cwd=str(sub.dir)) else []
    p = git("push", "--quiet", "--atomic", "origin", f"+{head}:refs/heads/{wb}", *past,
            f"--force-with-lease=refs/heads/{wb}:{old or ''}", cwd=str(sub.dir), check=False)
    if p.returncode != 0:
        die(f"pushing submodule {sub.name}'s {wb} failed - it moved on its origin, but this worktree is "
            f"its only writer.  Investigate:\n{(p.stderr or '').strip()}")


def reset_one(sub: Sub, wb: str) -> None:
    """The submodule worktree hard-reset onto its repository's default
    branch (untracked files survive), the branch pushed so."""
    cur = branch_at(sub.dir)
    if cur != wb:
        die(f"submodule {sub.name} ({sub.dir}) is on {cur or 'a detached HEAD'}, not {wb}")
    fetch_once(sub.name, sub.repo)
    default = default_branch(sub.repo)
    main = rev_in(sub.dir, f"refs/remotes/origin/{default}")
    prev = rev_in(sub.dir, "HEAD") or ""
    assert main is not None
    git("reset", "--quiet", "--hard", main, cwd=str(sub.dir))
    (sub.dir / MSG_FILE).unlink(missing_ok=True)
    if rev_in(sub.dir, f"refs/remotes/origin/{wb}") is not None:
        push_branch(sub, wb)
    print(f"submodule {sub.name}: {wb} reset onto origin/{default}" + (
        f" (was {prev[:12]}, see its reflog)" if prev != main else ""))


def moved_pins(top: Path, wb: str, tip: str, parent: str) -> list[tuple[Sub, str]]:
    """The submodules present under `top` whose pin the increments from
    `parent` to `tip` moved, each with the rev to put the worktree back on
    - `parent`'s pin, or the default branch's tip when that pin is landed
    (nothing of the branch's own is left then) - checked ahead of the
    checkout's own reset: each on `wb`, nothing staged in it, the rev at
    hand, no unstaged edit the reset would lose."""
    from .commands import lost_edits  # noqa: PLC0415 - commands imports this module

    moved = []
    for sub in present(top):
        was, now = pin_at(parent, sub.path, top), pin_at(tip, sub.path, top)
        if was == now:
            continue
        if was is None:
            die(f"the increment added submodule {sub.name}, whose worktree and branch would stay behind: "
                f"`nixconvoy close {sub.name} --force` first")
        cur = branch_at(sub.dir)
        if cur != wb:
            die(f"submodule {sub.name} ({sub.dir}) is on {cur or 'a detached HEAD'}, not {wb}")
        if staged(sub.dir):
            die(f"submodule {sub.name} has staged changes, which the deploy would carry; "
                f"deploy or unstage them first")
        fetch_once(sub.name, sub.repo)
        if rev_in(sub.dir, f"{was}^{{commit}}") is None:
            die(f"submodule {sub.name}: the pin to put back, {was[:12]}, is not in {sub.repo}")
        default = f"refs/remotes/origin/{default_branch(sub.repo)}"
        if is_ancestor(was, default, cwd=str(sub.dir)):
            was = rev_in(sub.dir, default) or was
        edited = lost_edits(was, "HEAD", sub.dir)
        if edited:
            die(f"submodule {sub.name}: the increments' files have unstaged edits, which the reset "
                f"would lose; stash or discard them in {sub.dir} first: {', '.join(edited)}")
        moved.append((sub, was))
    return moved


def unapply_pins(moved: list[tuple[Sub, str]], wb: str) -> None:
    """Each worktree of `moved` (moved_pins) reset onto the pin put back,
    its unstaged edits kept (an edit the reset would lose stops it), the
    branch pushed so: the deploy's sync then finds its HEAD pinned."""
    for sub, was in moved:
        prev = rev_in(sub.dir, "HEAD") or ""
        p = git("reset", "--quiet", "--keep", was, cwd=str(sub.dir), check=False)
        if p.returncode != 0:
            die(f"submodule {sub.name}: the reset onto {was[:12]} failed (the checkout is reset below "
                f"the increment already): `git -C {sub.dir} reset --keep {was[:12]}`, then deploy:\n"
                f"{(p.stderr or '').strip()}")
        if rev_in(sub.dir, f"refs/remotes/origin/{wb}") is not None:
            push_branch(sub, wb)
        print(f"submodule {sub.name}: {wb} reset onto {was[:12]} (was {prev[:12]}, see its reflog)")


def remove_all(top: Path) -> None:
    """Remove `top`'s submodule worktrees with their branches, here and on
    origin (a subagent worktree's, going with it)."""
    for sub in present(top):
        remove_one(sub)


def keeps(sub: Sub, top: Path) -> str | None:
    """Why `close <input>` keeps the worktree: what its removal would lose."""
    if out(["git", "status", "--porcelain"], cwd=str(sub.dir)):
        return "it has uncommitted or untracked changes"
    default = default_branch_of(sub.repo)
    if default is None:
        return "its repository's origin has no default branch to judge it against"
    if has_own(sub, default):
        return f"it carries commits not on origin/{default}; land or unapply this checkout"
    if off_default(sub, top):
        return f"the tree still pins a rev not on origin/{default}; land or unapply this checkout"
    return None


def remove_one(sub: Sub) -> None:
    """The submodule worktree removed with its branch, here and on origin;
    the directory stays, empty, as git leaves an unpopulated submodule
    (gone, the gitlink would show as deleted)."""
    cur = branch_at(sub.dir)
    git("worktree", "remove", "--force", str(sub.dir), cwd=str(sub.repo), check=False)
    shutil.rmtree(sub.dir, ignore_errors=True)
    sub.dir.mkdir(parents=True, exist_ok=True)
    git("worktree", "prune", cwd=str(sub.repo), check=False)
    if cur is not None and not any(fnmatchcase(cur, p) for p in allowed_branches()):
        print(f"submodule {sub.name}: {cur} is no work branch of this machine's; kept")
        cur = None
    if cur is not None:
        git("branch", "--quiet", "-D", cur, cwd=str(sub.repo), check=False)
        old = rev_in(sub.repo, f"refs/remotes/origin/{cur}")
        if old:
            # The tip stays under PAST_REFS: the work branch as pushed pins it.
            p = git("push", "--quiet", "--atomic", "origin", f"+{old}:{PAST_REFS}/{cur}/{old}", f":refs/heads/{cur}",
                    f"--force-with-lease=refs/heads/{cur}:{old}", cwd=str(sub.repo), check=False)
            if p.returncode != 0:
                print(f"warning: deleting submodule {sub.name}'s {cur} on its origin failed:\n"
                      f"{(p.stderr or '').strip()}", file=sys.stderr)
    print(f"submodule {sub.name}: worktree removed" + (f" with {cur}" if cur else ""))


def main_pin(sub: Sub, top: Path) -> str:
    """What origin/main pins the submodule on, or die."""
    return pin_at("refs/remotes/origin/main", sub.path, top) or \
        die(f"origin/main pins no {sub.path} to restore {sub.name}'s pin from")


def restore(sub: Sub, top: Path) -> bool:
    """The submodule's pin put back to what origin/main pins, staged - the
    next deploy reverts it on the targets.  Whether it moved."""
    want = main_pin(sub, top)
    if pin(sub, top) == want:
        return False
    set_pin(sub, want, top)
    print(f"submodule {sub.name}: pinned on origin/main's {want[:12]}, staged (deploy to revert it "
          f"on the targets)")
    return True


def orphaned(top: Path) -> list[str]:
    """The configured submodules whose pin in `top` names a rev off the
    default branch (a work branch's) while no worktree of theirs is under
    `top`: a landing must not take that to main."""
    here = {s.name for s in present(top)}
    return [name for name, e in entries(top).items() if name not in here
            and off_default(sub_of(name, e, top), top)]


def own(top: Path) -> bool:
    """Whether a submodule worktree under `top` holds anything of its own:
    edits, or commits its origin's default branch lacks."""
    for sub in present(top):
        if out(["git", "status", "--porcelain"], cwd=str(sub.dir)):
            return True
        default = default_branch_of(sub.repo)
        if default is None or has_own(sub, default):
            return True
    return False


def notes(d: Path) -> list[str]:
    """Status notes for the submodule worktrees under the checkout `d`."""
    found = []
    for sub in present(d):
        default = default_branch_of(sub.repo)
        ahead = (out(["git", "rev-list", "--count", "HEAD", "--not", f"refs/remotes/origin/{default}"],
                     cwd=str(sub.dir)) or "0") if default else "?"
        n_staged = len(out(["git", "diff", "--cached", "--name-only"], cwd=str(sub.dir)).splitlines())
        n_unstaged = len(out(["git", "diff", "--name-only"], cwd=str(sub.dir)).splitlines())
        n_untracked = len(out(["git", "ls-files", "--others", "--exclude-standard"],
                              cwd=str(sub.dir)).splitlines())
        parts = [f"submodule {sub.name}"]
        if ahead != "0":
            parts.append(f"+{ahead}")
        if n_staged:
            parts.append(f"staged {n_staged}")
        if n_unstaged:
            parts.append(f"unstaged {n_unstaged}")
        if n_untracked:
            parts.append(f"untracked {n_untracked}")
        cur = branch_at(sub.dir)
        if cur is None:
            parts.append("detached!")
        found.append(" ".join(parts))
    return found


def repo_notes() -> list[str]:
    """The configured submodule repositories' own checkouts holding edits,
    which no deploy picks up."""
    found = []
    for name, repo in configured().items():
        if not repo.is_dir():
            continue
        dirty = run(["git", "status", "--porcelain", "-uno"], cwd=str(repo), check=False).stdout
        if dirty.strip():
            n = len(dirty.strip().splitlines())
            found.append(f"{repo} ({name}): {n} edited file{'s' if n > 1 else ''} - no deploy "
                         f"picks these up; `nixconvoy open --submodule {name}` is where {name} is worked on")
    return found
