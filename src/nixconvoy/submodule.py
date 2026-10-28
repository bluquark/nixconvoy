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
union, the eval memo and the provenance handle like any other.  `land` lands
the branch on the repository's default branch first and pins the
gitlink there; `undeploy` resets it onto the pin of the rev the checkout
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
branch, each replayed commit pinned on the rebase of its own pin
(solve_rebase)."""

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
from pathlib import Path
from typing import NamedTuple

from . import proc, strings
from .checkout import (
    MACHINE_ENV,
    PENDING_REFS,
    TMP_PREFIX,
    GitQueries,
    common_state_path,
    fold_subject,
    landed_prefix,
    landing_message,
    message_on_file,
    owned,
    rebase_hint,
    rebase_solving,
    staged_differs,
    tree_less_msg,
)
from .config import MSG_FILE, cfg, main_ref, main_short
from .guard import decide
from .proc import Fail, die, git, is_ancestor, ok, out, run, solve_with_mergiraf
from .wire import Ok, Onto, RebaseParams, RepoNames

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
MARKER = "submodule"
GITLINK = "160000"


class Sub(NamedTuple):
    name: str  # the flake input and the .gitmodules name
    repo: Path  # the repository's checkout here; its origin is what nix fetches
    path: str  # the submodule's path in the tree (.gitmodules)
    dir: Path  # <checkout>/<path>
    pin_file: str  # the pin file beside the gitlink (pin_file_of)


class Entry(NamedTuple):
    path: str
    url: str


def configured() -> dict[str, Path]:
    return {n: Path(c.repo).expanduser() for n, c in cfg().submodules.items()}


def pin_file_of(name: str, path: str) -> str:
    """The submodule's pin file, in its gitlink's directory:
    .nixconvoy_submodule_<name>.json, the pinned rev as JSON ({"rev"}),
    kept by every pin (set_pin) - a nix evaluation reads no gitlink, and
    `fetchGit` by the rev gives a flake the commit's tree and date."""
    d = path.rpartition("/")[0]
    return f"{d}/.nixconvoy_submodule_{name}.json" if d else f".nixconvoy_submodule_{name}.json"


def sub_of(name: str, entry: Entry, top: Path) -> Sub:
    return Sub(name, configured()[name], entry.path, top / entry.path, pin_file_of(name, entry.path))


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


def owner(top: Path, path: str) -> Sub | None:
    """The configured submodule `path` belongs to in the tree under `top`:
    its gitlink, or its pin file."""
    for n, e in entries(top).items():
        if path in (e.path, pin_file_of(n, e.path)):
            return sub_of(n, e, top)
    return None


def is_pin_path(top: Path, path: str) -> bool:
    """Whether `path` is a pin's: a configured submodule's gitlink or pin file."""
    return owner(top, path) is not None


def paths(top: Path) -> set[str]:
    """The paths a pin's move touches: the gitlinks and their pin files."""
    return {p for n, e in entries(top).items() for p in (e.path, pin_file_of(n, e.path))}


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
    return default_branch_of(repo) or die(strings.SUBMODULE_NO_DEFAULT_BRANCH(repo=repo))


def has_own(sub: Sub, default: str) -> bool:
    """Whether the worktree's HEAD carries commits origin/<default> lacks."""
    return not ok(["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{default}"], cwd=str(sub.dir))


def rev_in(d: Path, ref: str) -> str | None:
    p = run(["git", "rev-parse", "-q", "--verify", ref], cwd=str(d), check=False)
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def staged(d: Path, *, less_msg: bool = False) -> bool:
    """checkout.staged_differs in `d`."""
    return staged_differs(str(d), less_msg=less_msg)


def mark(repo: Path) -> None:
    """Leave MARKER in the submodule repository's common git dir's nixconvoy/."""
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                 cwd=str(repo), check=False).stdout.strip()
    if common:
        with contextlib.suppress(OSError):
            marker = common_state_path(MARKER, Path(common))
            if not marker.exists():
                marker.write_text(f"{Path.cwd()}\n")


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
    """The index of `top` pinning the submodule on `sha`, staged, its pin
    file following (write_pin; a conflict on either resolved so)."""
    git("update-index", "--add", "--cacheinfo", f"{GITLINK},{sha},{sub.path}", cwd=str(top))
    write_pin(sub, sha, top)


def pin_text(sha: str) -> str:
    return json.dumps({"rev": sha}, indent=2) + "\n"


def write_pin(sub: Sub, sha: str | None, top: Path) -> None:
    """The submodule's pin file under `top` naming `sha`, staged; removed
    for None (no gitlink)."""
    if sha is None:
        git("rm", "--quiet", "--force", "--ignore-unmatch", "--", sub.pin_file, cwd=str(top))
        (top / sub.pin_file).unlink(missing_ok=True)
        return
    (top / sub.pin_file).write_text(pin_text(sha), encoding="utf-8")
    git("add", "--", sub.pin_file, cwd=str(top))


def heal_pins(top: Path, wb: str | None, dry: bool) -> list[str]:
    """Each configured submodule's pin file staged under `top` rewritten
    when it does not name the rev the index's gitlink pins (moved by
    hand, or no file yet), removed when the gitlink is gone, as is a pin
    file no configured submodule claims (one removed, renamed or moved),
    one line each; only warned about, with no work branch to carry it or
    for a dry activation."""
    told = []
    claimed = {pin_file_of(n, e.path) for n, e in entries(top).items()}
    for f in out(["git", "ls-files", "--", ".nixconvoy_submodule_*.json", "*/.nixconvoy_submodule_*.json"],
                 cwd=str(top)).splitlines():
        if f in claimed:
            continue
        if wb is None or dry:
            print(strings.SUBMODULE_PIN_FILE_UNCLAIMED(file=f), file=sys.stderr)
            continue
        git("rm", "--quiet", "--force", "--", f, cwd=str(top))
        told.append(strings.SUBMODULE_PIN_FILE_UNCLAIMED_REMOVED(file=f))
    for n, e in entries(top).items():
        sub = sub_of(n, e, top)
        cur = pin(sub, top)
        p = run(["git", "show", f":{sub.pin_file}"], cwd=str(top), check=False)
        have = p.stdout if p.returncode == 0 else None
        if have == (pin_text(cur) if cur else None):
            continue
        if wb is None or dry:
            print(strings.SUBMODULE_PIN_FILE_STALE(file=sub.pin_file, name=n), file=sys.stderr)
            continue
        write_pin(sub, cur, top)
        told.append(strings.SUBMODULE_PIN_FILE_REWRITTEN(name=n, file=sub.pin_file, rev=cur[:12]) if cur
                    else strings.SUBMODULE_PIN_FILE_REMOVED(name=n, file=sub.pin_file))
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
    # Each replayed commit's rewrite (old -> new), the commits of the
    # landed prefix it skipped and the default branch's commit that landed
    # them, and the default branch's tip it went onto.
    rewritten: dict[str, str]
    landed: frozenset[str]
    landing: str
    onto: str


# Per run: the submodule repositories fetched, the fetches under way beside
# the deploy's own (prefetch), and the rebased submodules.
FETCHED: set[Path] = set()
PREFETCH: dict[Path, Future[bool]] = {}
PENDING: dict[tuple[str, str], Pending] = {}


def fetch_refs(name: str, repo: Path) -> bool:
    """Fetch the submodule repository `repo`'s origin: branches, past and
    union refs, the gone ones pruned; whether it is fetched (else said)."""
    p = git("fetch", "--quiet", "--prune", "origin", *REFSPECS, cwd=str(repo), check=False)
    if p.returncode != 0:
        print(strings.SUBMODULE_FETCH_FAILED(name=name, stderr=(p.stderr or "").strip()), file=sys.stderr)
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
    records') and the deploys' announcements under way with the records
    they carry (their parents)."""
    commits = set(out(["git", "for-each-ref", "--format=%(objectname)", "refs/remotes/origin/"]).split())
    pending = out(["git", "for-each-ref", "--format=%(objectname)", PENDING_REFS]).split()
    if pending:
        commits.update(out(["git", "rev-list", "--no-walk", "--parents", *pending]).split())
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
    p = git("push", "--quiet", "origin", *(f":{r}" for r in stale), cwd=str(sub.repo), check=False)
    if p.returncode != 0:
        print(strings.SUBMODULE_PAST_REFS_DELETE_FAILED(name=sub.name, stderr=(p.stderr or "").strip()),
              file=sys.stderr)
        return
    for r in stale:
        git("update-ref", "-d", r, cwd=str(sub.repo), check=False)


@contextlib.contextmanager
def repo_lock(repo: Path) -> Iterator[None]:
    """A lock on the submodule repository, serialising the deploys merging
    in it (two subagents' unions at once)."""
    common = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(repo)))
    with common_state_path("submodule.lock", common).open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        yield


def merge_commit(name: str, repo: Path, ours: str, theirs: str, subject: str) -> str | None:
    """`theirs` merged into `ours` in a throwaway worktree of `repo`
    (mergiraf on the conflicts, MSG_FILE dropped) as a deterministic
    commit, dated a second past the newer of the two (so a version taken
    from the pinned commit's date keeps rising through nested unions): its
    sha, or None when they
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
                    print(strings.SUBMODULE_MERGE_CONFLICT(name=name, ours=ours[:12], theirs=theirs[:12], path=f),
                          file=sys.stderr)
                    return None
            git("commit", "--quiet", "--no-edit", "--no-verify", cwd=wt, env=env)
        return out(["git", "rev-parse", "HEAD"], cwd=wt)
    finally:
        with proc.finishing():
            git("worktree", "remove", "--force", wt, cwd=str(repo), check=False)
        shutil.rmtree(wt, ignore_errors=True)


def mirror_dir(name: str) -> Path:
    return proc.cache_dir() / "submodules" / f"{name}.git"


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
        p = git("clone", "--quiet", "--bare", url, str(cache), check=False)
        if p.returncode != 0:
            die(strings.SUBMODULE_MIRROR_CLONE_FAILED(name=name, url=url, stderr=(p.stderr or "").strip()))
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
                print(strings.SUBMODULE_NOT_ON_ORIGIN(name=sub.name, sha=sha[:12]), file=sys.stderr)
                return None
        if is_ancestor(theirs, ours, cwd=str(repo)):
            return ours
        if is_ancestor(ours, theirs, cwd=str(repo)):
            return theirs
        sha = merge_commit(sub.name, repo, ours, theirs, strings.SUBMODULE_UNION_SUBJECT(name=sub.name))
        if sha is None:
            return None
        ref = f"{UNION_REFS}/{sub.name}/{sha}"
        p = git("push", "--quiet", "--force", "origin", f"{sha}:{ref}", cwd=str(repo), check=False)
        if p.returncode != 0:
            die(strings.SUBMODULE_UNION_PUSH_FAILED(name=sub.name, ref=ref, stderr=(p.stderr or "").strip()))
        git("update-ref", ref, sha, cwd=str(repo), check=False)
    print(strings.SUBMODULE_UNION_DONE(name=sub.name, ours=ours[:12], theirs=theirs[:12], sha=sha[:12], ref=ref))
    return sha


def solve_union(wt: Path, path: str) -> bool:
    """A deploy's union: the gitlink `path` conflicted in the union
    worktree `wt` (another branch pinned the submodule too) pinned on the
    union of the two revs (union_rev), staged; its pin file conflicted
    (both moved the pin) solved with the gitlink, whichever comes first.
    Whether it resolved."""
    sub = owner(wt, path)
    if sub is None:
        return False
    stages = gitlink_stages(wt, sub.path)
    if not stages:  # the pin file alone, or resolved with its gitlink already: rewritten from the gitlink
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
    (once per run) and
    each replayed commit pinned on the rebased counterpart of its own pin
    (counterpart), staged for `git rebase --continue`; the push waits
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
        why = strings.SUBMODULE_REBASE_WHY(upstream=upstream, wb=wb, name=sub.name)
        if not stages:  # the pin file alone: rewritten from the gitlink
            cur = pin(sub, wt)
            if cur is None:
                return strings.SUBMODULE_REBASE_PIN_FILE_ALONE(file=sub.pin_file)
            set_pin(sub, cur, wt)
            continue
        if 3 not in stages:  # mine (stage 3) is the replayed commit's pin
            return why + strings.SUBMODULE_REBASE_PIN_DELETED(upstream=upstream)
        pending = PENDING.get((sub.name, wb))
        if pending is None:
            pending, problem = rebase_sub(sub, wb, stages[3], wt, checkout)
            if pending is None:
                return why + strings.SUBMODULE_REBASE_THEN_PULL(problem=problem)
        set_pin(sub, counterpart(pending, stages[3], stages.get(2)), wt)
    return None


def counterpart(pending: Pending, pinned: str, floor: str | None) -> str:
    """The rev of the rebased branch standing for `pinned`, a replayed
    commit's pin: its own rewrite, else its nearest first-parent
    ancestor's (a commit the rebase dropped, solved to nothing, is in the
    one before it); a commit of the landed prefix stands for the commit
    that landed it, one the rebased branch holds for itself, one the
    rebase dropped with nothing replayed before it (its change upstream
    already) for the default branch's tip.  Never behind `floor`, the pin
    the rebase has at this commit (a pin older than main's would revert a
    landing).  The rebased tip for a pin unknown here."""
    d = str(pending.sub.dir)
    chain = run(["git", "rev-list", "--first-parent", pinned, f"^{pending.head}"], cwd=d, check=False)
    if chain.returncode != 0:
        return pending.head
    found = pinned
    for c in chain.stdout.split():
        if c in pending.rewritten:
            found = pending.rewritten[c]
            break
        if c in pending.landed:
            found = pending.landing
            break
    else:
        if chain.stdout.strip():
            found = pending.onto
    if floor and found != floor and is_ancestor(found, floor, cwd=d):
        return floor
    return found


def landing_of(base: str, upstream: str, d: str) -> str | None:
    """The default branch's commit that landed the prefix ending at
    `base` (landed_prefix's): the oldest past it with its tree, MSG_FILE
    aside."""
    trees = {out(["git", "rev-parse", f"{base}^{{tree}}"], cwd=d), tree_less_msg(base, d)}
    for ln in reversed(out(["git", "log", "--format=%H %T", f"{base}..{upstream}"], cwd=d).splitlines()):
        sha, tree = ln.split()
        if tree in trees:
            return sha
    return None


def rebase_sub(sub: Sub, wb: str, mine: str, wt: Path, checkout: Path) -> tuple[Pending | None, str]:
    """solve_rebase for one submodule: its worktree under `checkout` on
    `wb` rebased onto the default branch as fetched (the push pending),
    as recorded in PENDING - or None and why it cannot be, with what to do."""
    found = [s for s in present(checkout) if s.name == sub.name]
    if not found or branch_at(found[0].dir) != wb:
        return None, strings.SUBMODULE_REBASE_NO_WORKTREE(wb=wb, checkout=checkout, name=sub.name)
    here = found[0]
    d = str(here.dir)
    if out(["git", "status", "--porcelain", "-uno"], cwd=d):
        return None, strings.SUBMODULE_REBASE_EDITS(dir=here.dir)
    tip_pinned = tip_pin(wt, sub.path) or mine
    gone = reset_below_deployed(here, tip_pinned)
    if gone:  # a hand reset: pinning HEAD would drop them in silence
        return None, dropped_message(here, wb, tip_pinned, gone)
    behind = left_behind(here, tip_pinned)
    if behind:
        print(strings.SUBMODULE_REBASE_LEAVES_BEHIND(name=sub.name, n=len(behind), commits="\n  ".join(behind)),
              file=sys.stderr)
    if not fetch_once(sub.name, here.repo):
        return None, strings.SUBMODULE_REBASE_UNFETCHED
    was = rev_in(here.dir, "HEAD")
    default = default_branch(here.repo)
    upstream = f"refs/remotes/origin/{default}"
    onto = rev_in(here.dir, upstream)
    assert was is not None and onto is not None
    base = landed_prefix(upstream, d)
    landed = frozenset(out(["git", "rev-list", base, f"^{upstream}"], cwd=d).split()) if base else frozenset()
    landing = (landing_of(base, upstream, d) if base else None) or onto
    rewritten: dict[str, str] = {}
    if not rebase_solving(here.dir, upstream, base, rewritten):
        return None, strings.SUBMODULE_REBASE_NOT_CLEAN(wb=wb, default=default, dir=here.dir,
                                                       hint=rebase_hint(upstream, d))
    head = rev_in(here.dir, "HEAD")
    assert head is not None
    if not rewritten and head != was and out(["git", "rev-list", "-n", "1", f"{upstream}..HEAD"], cwd=d):
        # Commits replayed and none reported: pinning by the fallbacks
        # alone would collapse the increments' pins.
        git("reset", "--quiet", "--hard", was, cwd=d)
        return None, strings.SUBMODULE_REBASE_NO_REWRITES(dir=here.dir, hint=rebase_hint(upstream, d))
    pending = PENDING[sub.name, wb] = Pending(here, wb, was, head, rewritten, landed, landing, onto)
    print(strings.SUBMODULE_REBASED(name=sub.name, wb=wb, default=default, head=head[:12]))
    return pending, ""


def push_pending() -> None:
    """The rebase succeeded: the rebased submodule branches pushed."""
    for pending in PENDING.values():
        push_branch(pending.sub, pending.wb)
        print(strings.SUBMODULE_PUSHED(name=pending.sub.name, wb=pending.wb))
    PENDING.clear()


def undo_pending() -> None:
    """The rebase aborted: each rebased submodule put back where it was."""
    for pending in PENDING.values():
        git("reset", "--quiet", "--hard", pending.was, cwd=str(pending.sub.dir), check=False)
        print(strings.SUBMODULE_PUT_BACK(name=pending.sub.name, wb=pending.wb, rev=pending.was[:12]), file=sys.stderr)
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
        die(strings.SUBMODULE_NOT_A_REPO(name=name, repo=repo))
    top = Path.cwd()
    entry = entries(top).get(name)
    if entry is None:
        die(strings.SUBMODULE_NOT_A_SUBMODULE(name=name))
    origin = run(["git", "remote", "get-url", "origin"], cwd=str(repo), check=False).stdout.strip()
    if not same_repo(origin, entry.url):
        die(strings.SUBMODULE_ORIGIN_MISMATCH(name=name, repo=repo, origin=origin, url=entry.url))
    d = top / entry.path
    if (d / ".git").exists():
        cur = branch_at(d)
        if cur != wb:
            die(strings.SUBMODULE_AT_OFF_BRANCH(name=name, dir=d, cur=cur or strings.SUBMODULE_DETACHED, wb=wb))
        print(strings.SUBMODULE_ALREADY_OPEN(name=name, dir=d, wb=wb))
        return d
    if d.exists() and any(d.iterdir()):
        die(strings.SUBMODULE_DIR_NOT_EMPTY(dir=d))
    git("worktree", "prune", cwd=str(repo), check=False)
    p = git("fetch", "--quiet", "origin", cwd=str(repo), check=False)
    if p.returncode != 0:
        die(strings.SUBMODULE_CREATE_FETCH_FAILED(name=name, repo=repo, stderr=(p.stderr or "").strip()))
    default = default_branch(repo)
    d.parent.mkdir(parents=True, exist_ok=True)
    remote, local = rev_in(repo, f"refs/remotes/origin/{wb}"), rev_in(repo, f"refs/heads/{wb}")
    if remote is not None and local is not None and not is_ancestor(local, remote, cwd=str(repo)):
        if not is_ancestor(remote, local, cwd=str(repo)):
            die(strings.SUBMODULE_BRANCH_DIVERGED(name=name, wb=wb, local=local[:12], remote=remote[:12],
                                                  repo=repo))
        remote = None  # the local branch is ahead (a push that failed): kept
    if remote is not None:
        git("branch", "--quiet", "-f", wb, remote, cwd=str(repo), check=False)
        git("worktree", "add", "--quiet", str(d), wb, cwd=str(repo))
        where = f"origin/{wb}"
    elif local is not None:
        git("worktree", "add", "--quiet", str(d), wb, cwd=str(repo))
        where = strings.SUBMODULE_FROM_LOCAL(wb=wb)
    else:
        git("worktree", "add", "--quiet", "-b", wb, str(d), f"refs/remotes/origin/{default}",
            cwd=str(repo))
        where = f"origin/{default}"
    # `git status` there counts against the branch it will land on.
    git("branch", "--quiet", f"--set-upstream-to=origin/{default}", wb, cwd=str(d), check=False)
    mark(repo)
    print(strings.SUBMODULE_OPENED(name=name, dir=d, wb=wb, where=where))
    return d


def configured_repo(name: str) -> Path:
    repos = configured()
    if name not in repos:
        die(strings.SUBMODULE_NOT_CONFIGURED(name=name, hint=strings.SUBMODULE_CONFIGURED_ARE(
            names=", ".join(sorted(repos))) if repos else strings.SUBMODULE_NONE_CONFIGURED()))
    return repos[name]


def present_one(top: Path, name: str) -> Sub:
    found = [s for s in present(top) if s.name == name]
    if not found:
        configured_repo(name)
        entry = entries(top).get(name)
        die(strings.SUBMODULE_NO_WORKTREE(name=name, where=strings.SUBMODULE_NO_WORKTREE_AT(
            path=top / entry.path) if entry else strings.SUBMODULE_NO_GITMODULES_ENTRY))
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
    return strings.SUBMODULE_DROPPED(name=sub.name, dir=sub.dir, wb=wb, n=len(gone), commits="\n  ".join(gone),
                                     pinned=pinned[:12])


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
            problem = strings.SUBMODULE_FAILED_ABOVE(name=sub.name)
        if problem is not None:
            problems.append(problem)
    return problems


def onto(d: Path, default: str) -> tuple[bool, str]:
    """The rebase core's Onto run in `d`, under the submodule repository's
    own names (origin, its `default` branch): HEAD made to contain
    origin/<default>, (ok, status)."""
    names = RepoNames("origin", default)
    prev = Path.cwd()
    os.chdir(d)
    try:
        done = decide(RebaseParams(names, Onto(names.main_ref)), GitQueries(names))
    finally:
        os.chdir(prev)
    return isinstance(done, Ok), done.value


def catch_up_one(sub: Sub, wb: str, top: Path) -> str | None:
    """catch_up for one submodule: the problem, or None."""
    cur = branch_at(sub.dir)
    if cur != wb:
        return strings.SUBMODULE_OFF_BRANCH_SWITCH(name=sub.name, dir=sub.dir, cur=cur or strings.SUBMODULE_DETACHED, wb=wb)
    if not fetch_once(sub.name, sub.repo):
        if carries_one(sub, top):
            return strings.SUBMODULE_CATCH_UP_UNFETCHED(name=sub.name)
        return None
    default = default_branch(sub.repo)
    was = rev_in(sub.dir, "HEAD")
    own = has_own(sub, default)
    good, status = onto(sub.dir, default)
    if not good:
        return strings.SUBMODULE_STATUS(name=sub.name, status=status)
    head = rev_in(sub.dir, "HEAD")
    if head is None or was is None or head == was:
        return None
    print(strings.SUBMODULE_STATUS(name=sub.name, status=status))
    if own and rev_in(sub.dir, f"refs/remotes/origin/{wb}") is not None:
        push_branch(sub, wb)
    if pin(sub, top) == was:
        set_pin(sub, head, top)
        print(strings.SUBMODULE_PINNED_ON(name=sub.name, rev=head[:12], wb=wb))
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
            problem = strings.SUBMODULE_REPO_FAILED_ABOVE(repo=repo, name=name)
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
        was = rev_in(repo, "HEAD")
        good, status = onto(repo, default)
    if not good:
        return strings.SUBMODULE_REPO_STATUS(repo=repo, name=name, status=status)
    if rev_in(repo, "HEAD") != was:
        print(strings.SUBMODULE_REPO_STATUS(repo=repo, name=name, status=status))
    return None


def sync(wb: str | None, msg: str | None, *, dry: bool = False) -> list[str]:
    """A deploy's submodule step: each present submodule brought onto its
    default branch (catch_up, a problem there ending the deploy; not when
    `dry`), its staged state committed onto `wb` with `msg`, pushed, and
    the tree pinned on it, staged here.  What changed, one line each.
    `dry` (a dry activation) commits and pushes nothing, so a change it
    cannot carry is refused instead."""
    top = Path.cwd()
    told = heal_pins(top, wb, dry)
    found = present(top)
    if not found:
        return told
    if wb is None:
        die(strings.SUBMODULE_IN_MAIN_CHECKOUT)
    for sub in found:
        cur = branch_at(sub.dir)
        if cur != wb:
            die(strings.SUBMODULE_OFF_BRANCH_SWITCH(name=sub.name, dir=sub.dir, cur=cur or strings.SUBMODULE_DETACHED, wb=wb))
        refuse_dropped(sub, wb, top)
    if not dry:  # a dry activation rebases, pushes and pins nothing
        for problem in catch_up(top, wb):
            die(strings.PROC_WITH_NOTHING_DONE(problem=problem))
    for sub in found:
        mark(sub.repo)
        if out(["git", "diff", "--name-only"], cwd=str(sub.dir)):
            print(strings.SUBMODULE_UNSTAGED_NOT_DEPLOYED(name=sub.name), file=sys.stderr)
        if staged(sub.dir):
            if dry:
                die(strings.SUBMODULE_STAGED_DRY(name=sub.name))
            if not msg:
                die(strings.SUBMODULE_STAGED_NO_MSG(name=sub.name))
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
            die(strings.SUBMODULE_BEHIND_DEFAULT(
                name=sub.name, wb=wb, default=default,
                fix=strings.SUBMODULE_BEHIND_DEFAULT_DRY if dry
                else strings.SUBMODULE_BEHIND_DEFAULT_REBASE(dir=sub.dir)))
        if dry and (rev_in(sub.dir, f"refs/remotes/origin/{wb}") != head or pin(sub, top) != head):
            die(strings.SUBMODULE_UNDEPLOYED_DRY(name=sub.name))
        if rev_in(sub.dir, f"refs/remotes/origin/{wb}") != head:
            push_branch(sub, wb)
            told.append(strings.SUBMODULE_PUSHED_AT(name=sub.name, wb=wb, rev=head[:12]))
        behind = left_behind(sub, pin(sub, top))
        if behind:
            print(strings.SUBMODULE_PIN_LEAVES_BEHIND(name=sub.name, rev=head[:12], n=len(behind),
                                                      commits="\n  ".join(behind)), file=sys.stderr)
        if pin(sub, top) != head:
            set_pin(sub, head, top)
            told.append(strings.SUBMODULE_PINNED_ON(name=sub.name, rev=head[:12], wb=wb))
    return told


class Landing(NamedTuple):
    sub: Sub
    wb: str
    default: str
    landed: str  # the rev the work branch now pins
    pending: str | None  # `landed` as a commit for the default branch, once main is pushed
    settle: bool  # the worktree and the branch still to move onto `landed`


def prepare_all(wb: str, fallback_msg: str, earlier: list[Landing] | None = None) -> list[Landing]:
    """Each present submodule's landing prepared (prepare_one), for land to
    finish (finish_all) once main is pushed, or abandon (abandon_all); one
    that fails abandons the ones before it.  `earlier`: the landings of
    the participants before this one in a land --with-children: a
    repository among them is landed on top of the end of its line
    (line_end), which the landing then extends."""
    landings: list[Landing] = []
    try:
        for sub in present(Path.cwd()):
            landings.append(prepare_one(sub, wb, fallback_msg, line_end(earlier or [], sub.repo)))
    except Fail:
        abandon_all(landings)
        raise
    return landings


def line_end(landings: list[Landing], repo: Path) -> str | None:
    """The rev the landings of the repository `repo` so far put its
    default branch on: the latest that carried work there (one with
    nothing of its own just follows a pin, behind that line or on it).
    None before any did."""
    return next((e.landed for e in reversed(landings) if e.sub.repo == repo and e.settle), None)


def check_pins(tree: str, landings: list[Landing], top: Path) -> None:
    """Before main's push: `tree` (the landed tree) must pin each landed
    repository on the end of its line (line_end), the rev finish_all
    leaves its default branch on; a union merge, or a pin from outside
    the landings, would leave that branch short of main's pin."""
    for landing in {e.sub.repo: e for e in landings}.values():
        end = line_end(landings, landing.sub.repo)
        want = pin_at(tree, landing.sub.path, top)
        if end is not None and want != end:
            die(strings.SUBMODULE_LANDED_TREE_PIN(name=landing.sub.name,
                                                  want=want[:12] if want else strings.SUBMODULE_PIN_NONE,
                                                  end=end[:12]))


def finish_all(landings: list[Landing]) -> None:
    """Main is pushed: each landing finished, a failure said and the rest
    carried on (the pin is on main either way)."""
    for landing in landings:
        try:
            finish_one(landing)
        except Fail as e:
            print(strings.SUBMODULE_FINISH_FAILED(name=landing.sub.name, error=e, landed=landing.landed[:12],
                                                  wb=landing.wb, dir=landing.sub.dir), file=sys.stderr)


def abandon_all(landings: list[Landing]) -> None:
    """The landing commits' temporary refs dropped: nothing else moved on
    any origin, and the caller resets the pin commits of its branch."""
    for landing in landings:
        if landing.pending is not None:
            git("push", "--quiet", "origin", f":{landing_ref(landing)}", cwd=str(landing.sub.dir), check=False)


def landing_ref(landing: Landing) -> str:
    """Where the landing commit waits on its origin for main's push (a
    PAST_REFS entry: pruned like any once nothing pins it)."""
    return f"{PAST_REFS}/{landing.wb}/{landing.pending}"


def prepare_one(sub: Sub, wb: str, fallback_msg: str, base: str | None = None) -> Landing:
    """The submodule's landing on its repository's default branch (`base`:
    on an earlier participant's landing of the same repository instead) as
    one commit (the MSG_FILE in its worktree - tracked or not, it is read
    here alone - else `fallback_msg`), pinned as a commit on `wb`; the
    commit itself waits under a temporary ref on its origin, so that the
    push of main, which the pin makes depend on it, goes first and a
    refused one leaves the default branch as it was.  A branch with
    nothing of its own just follows the pin, worktree and branch moved
    at once (nothing of that reaches the default branch)."""
    top = Path.cwd()
    cur = branch_at(sub.dir)
    if cur != wb:
        die(strings.SUBMODULE_OFF_BRANCH(name=sub.name, dir=sub.dir, cur=cur or strings.SUBMODULE_DETACHED, wb=wb))
    if staged(sub.dir, less_msg=True):
        die(strings.SUBMODULE_LAND_STAGED(name=sub.name))
    # The rebase onto the default branch needs a clean tree.
    loose = [ln for ln in out(["git", "status", "--porcelain", "-uno"], cwd=str(sub.dir)).splitlines()
             if not ln.endswith(f" {MSG_FILE}")]
    if loose:
        die(strings.SUBMODULE_LAND_UNSTAGED(name=sub.name, n=len(loose)))
    if not fetch_refs(sub.name, sub.repo):
        die(strings.SUBMODULE_LAND_UNFETCHED(name=sub.name))
    refuse_dropped(sub, wb, top)
    default = default_branch(sub.repo)
    main = base or rev_in(sub.dir, f"refs/remotes/origin/{default}")
    head = rev_in(sub.dir, "HEAD")
    assert main is not None and head is not None
    # The branch's own commits squashed onto the default branch - its
    # tip merged there as a deploy's union merges it (mergiraf on the
    # conflicts); one with none follows the pin, when a landing elsewhere
    # put that ahead of it on the default branch, else keeps its rev.
    landed = head
    pending = None
    settle = False
    pinned = pin(sub, top)
    if is_ancestor(head, main, cwd=str(sub.dir)):
        if pinned is not None and pinned != head and is_ancestor(pinned, main, cwd=str(sub.dir)):
            if git("reset", "--quiet", "--keep", pinned, cwd=str(sub.dir), check=False).returncode != 0:
                git("reset", "--quiet", "--hard", pinned, cwd=str(sub.dir))
            push_branch(sub, wb)
            landed = pinned
    else:
        settle = True
        deployed = pinned
        if deployed != head:
            die(strings.SUBMODULE_LAND_NOT_DEPLOYED(
                name=sub.name, head=head[:12], pinned=deployed[:12] if deployed else strings.SUBMODULE_PIN_NONE))
        merged = merge_commit(sub.name, sub.repo, main, head, strings.SUBMODULE_LANDING_SUBJECT(wb=wb))
        if merged is None:
            die(strings.SUBMODULE_LAND_CONFLICT(name=sub.name, wb=wb, default=default, dir=sub.dir))
        tree = tree_less_msg(merged, cwd=str(sub.dir))
        msg = (landing_message(None, message_on_file(str(sub.dir)), strings.SUBMODULE_LABEL(name=sub.name))
               or fold_subject(fallback_msg))
        if tree == out(["git", "rev-parse", f"{main}^{{tree}}"], cwd=str(sub.dir)):
            landed = main
        else:
            landed = pending = out(["git", "commit-tree", tree, "-p", main, "-m", msg], cwd=str(sub.dir))
            p = git("push", "--quiet", "origin", f"{landed}:{PAST_REFS}/{wb}/{landed}",
                    cwd=str(sub.dir), check=False)
            if p.returncode != 0:
                die(strings.SUBMODULE_LAND_PUSH_FAILED(name=sub.name, stderr=(p.stderr or "").strip()))
    if pin(sub, top) != landed:
        set_pin(sub, landed, top)
        git("commit", "--quiet", "-m", strings.SUBMODULE_PIN_COMMIT(name=sub.name, default=default))
    return Landing(sub, wb, default, landed, pending, settle)


def finish_one(landing: Landing) -> None:
    """After main's push: the default branch onto the landing commit, the
    worktree and the branch onto the landing.  A default branch that moved
    meanwhile is left as it is, said with the repair: main pins the commit
    all the same, which its temporary ref keeps fetchable."""
    sub, wb, default, landed, pending, settle = landing
    if pending is not None:
        p = git("push", "--quiet", "origin", f"{pending}:refs/heads/{default}", cwd=str(sub.dir), check=False)
        if p.returncode != 0:
            print(strings.SUBMODULE_PUSH_DEFAULT_FAILED(name=sub.name, default=default, rev=pending[:12],
                                                        ref=landing_ref(landing), dir=sub.dir,
                                                        stderr=(p.stderr or "").strip()), file=sys.stderr)
        else:
            print(strings.SUBMODULE_LANDED(name=sub.name, default=default, rev=pending[:12]))
            git("push", "--quiet", "origin", f":{landing_ref(landing)}", cwd=str(sub.dir), check=False)
            git("update-ref", "-d", landing_ref(landing), cwd=str(sub.repo), check=False)
    if settle:
        # The worktree onto the landing; its tree is clean of edits.
        if git("reset", "--quiet", "--keep", landed, cwd=str(sub.dir), check=False).returncode != 0:
            git("reset", "--quiet", "--hard", landed, cwd=str(sub.dir))
        (sub.dir / MSG_FILE).unlink(missing_ok=True)
        push_branch(sub, wb)
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
        die(strings.SUBMODULE_PUSH_BRANCH_FAILED(name=sub.name, wb=wb, stderr=(p.stderr or "").strip()))


def reset_one(sub: Sub, wb: str) -> None:
    """The submodule worktree hard-reset onto its repository's default
    branch (untracked files survive), the branch pushed so."""
    cur = branch_at(sub.dir)
    if cur != wb:
        die(strings.SUBMODULE_OFF_BRANCH(name=sub.name, dir=sub.dir, cur=cur or strings.SUBMODULE_DETACHED, wb=wb))
    fetch_once(sub.name, sub.repo)
    default = default_branch(sub.repo)
    main = rev_in(sub.dir, f"refs/remotes/origin/{default}")
    prev = rev_in(sub.dir, "HEAD") or ""
    assert main is not None
    git("reset", "--quiet", "--hard", main, cwd=str(sub.dir))
    (sub.dir / MSG_FILE).unlink(missing_ok=True)
    if rev_in(sub.dir, f"refs/remotes/origin/{wb}") is not None:
        push_branch(sub, wb)
    print(strings.SUBMODULE_RESET(name=sub.name, wb=wb, default=default,
                                  was=strings.SUBMODULE_RESET_WAS(rev=prev[:12]) if prev != main else ""))


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
            die(strings.SUBMODULE_UNDEPLOY_ADDED(name=sub.name))
        cur = branch_at(sub.dir)
        if cur != wb:
            die(strings.SUBMODULE_OFF_BRANCH(name=sub.name, dir=sub.dir, cur=cur or strings.SUBMODULE_DETACHED, wb=wb))
        if staged(sub.dir):
            die(strings.SUBMODULE_UNDEPLOY_STAGED(name=sub.name))
        fetch_once(sub.name, sub.repo)
        if rev_in(sub.dir, f"{was}^{{commit}}") is None:
            die(strings.SUBMODULE_UNDEPLOY_PIN_MISSING(name=sub.name, rev=was[:12], repo=sub.repo))
        default = f"refs/remotes/origin/{default_branch(sub.repo)}"
        if is_ancestor(was, default, cwd=str(sub.dir)):
            was = rev_in(sub.dir, default) or was
        edited = lost_edits(was, "HEAD", sub.dir)
        if edited:
            die(strings.SUBMODULE_UNDEPLOY_LOST_EDITS(name=sub.name, dir=sub.dir, files=", ".join(edited)))
        moved.append((sub, was))
    return moved


def undeploy_pins(moved: list[tuple[Sub, str]], wb: str) -> None:
    """Each worktree of `moved` (moved_pins) reset onto the pin put back,
    its unstaged edits kept (an edit the reset would lose stops it), the
    branch pushed so: the deploy's sync then finds its HEAD pinned."""
    for sub, was in moved:
        prev = rev_in(sub.dir, "HEAD") or ""
        p = git("reset", "--quiet", "--keep", was, cwd=str(sub.dir), check=False)
        if p.returncode != 0:
            die(strings.SUBMODULE_UNDEPLOY_RESET_FAILED(name=sub.name, rev=was[:12], dir=sub.dir,
                                                        stderr=(p.stderr or "").strip()))
        if rev_in(sub.dir, f"refs/remotes/origin/{wb}") is not None:
            push_branch(sub, wb)
        print(strings.SUBMODULE_UNDEPLOY_RESET(name=sub.name, wb=wb, rev=was[:12], prev=prev[:12]))


def remove_all(top: Path) -> None:
    """Remove `top`'s submodule worktrees with their branches, here and on
    origin (a subagent worktree's, going with it)."""
    for sub in present(top):
        remove_one(sub)


def keeps(sub: Sub, top: Path) -> str | None:
    """Why `close <input>` keeps the worktree: what its removal would lose."""
    if out(["git", "status", "--porcelain"], cwd=str(sub.dir)):
        return strings.SUBMODULE_KEEP_CHANGES
    default = default_branch_of(sub.repo)
    if default is None:
        return strings.SUBMODULE_KEEP_NO_DEFAULT
    if has_own(sub, default):
        return strings.SUBMODULE_KEEP_COMMITS(default=default)
    if off_default(sub, top):
        return strings.SUBMODULE_KEEP_PIN(default=default)
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
    if cur is not None and not owned(cur):
        print(strings.SUBMODULE_BRANCH_NOT_OURS(name=sub.name, branch=cur))
        cur = None
    if cur is not None:
        git("branch", "--quiet", "-D", cur, cwd=str(sub.repo), check=False)
        old = rev_in(sub.repo, f"refs/remotes/origin/{cur}")
        if old:
            # The tip stays under PAST_REFS: the work branch as pushed pins it.
            p = git("push", "--quiet", "--atomic", "origin", f"+{old}:{PAST_REFS}/{cur}/{old}", f":refs/heads/{cur}",
                    f"--force-with-lease=refs/heads/{cur}:{old}", cwd=str(sub.repo), check=False)
            if p.returncode != 0:
                print(strings.SUBMODULE_BRANCH_DELETE_FAILED(name=sub.name, branch=cur,
                                                             stderr=(p.stderr or "").strip()), file=sys.stderr)
    print(strings.SUBMODULE_REMOVED(name=sub.name,
                                    with_branch=strings.SUBMODULE_REMOVED_WITH(branch=cur) if cur else ""))


def main_pin(sub: Sub, top: Path) -> str:
    """What origin/main pins the submodule on, or die."""
    return pin_at(main_ref(), sub.path, top) or \
        die(strings.SUBMODULE_MAIN_PINS_NONE(main=main_short(), path=sub.path, name=sub.name))


def restore(sub: Sub, top: Path) -> bool:
    """The submodule's pin put back to what origin/main pins, staged - the
    next deploy reverts it on the targets.  Whether it moved."""
    want = main_pin(sub, top)
    if pin(sub, top) == want:
        return False
    set_pin(sub, want, top)
    print(strings.SUBMODULE_RESTORED(name=sub.name, main=main_short(), rev=want[:12]))
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
        parts = [strings.SUBMODULE_LABEL(name=sub.name)]
        if ahead != "0":
            parts.append(strings.SUBMODULE_NOTE_AHEAD(n=ahead))
        if n_staged:
            parts.append(strings.SUBMODULE_NOTE_STAGED(n=n_staged))
        if n_unstaged:
            parts.append(strings.SUBMODULE_NOTE_UNSTAGED(n=n_unstaged))
        if n_untracked:
            parts.append(strings.SUBMODULE_NOTE_UNTRACKED(n=n_untracked))
        cur = branch_at(sub.dir)
        if cur is None:
            parts.append(strings.SUBMODULE_NOTE_DETACHED)
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
            found.append(strings.SUBMODULE_REPO_EDITED(repo=repo, name=name, n=n, s="s" if n > 1 else ""))
    return found
