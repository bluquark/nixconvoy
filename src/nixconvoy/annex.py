"""Annexes: the flake inputs a checkout develops in place.  An annex is a
git worktree of the input's repository under <checkout>/annexes/<name>,
on a branch named after the checkout's work branch.  A deploy commits the
annex's staged state onto that branch, pushes it to the input's origin
(what the lock fetches) and points the flake.lock node at the pushed rev,
so the increment carries the annex as a lock change, which the union, the
eval memo and the lineage handle as any other.  `land` lands the annex
branch on its repository's default branch first and relocks the node
there; `abandon` discards it, the node restored to origin/main's.  The
annex worktree stays either way, its branch reset onto the default
branch like an agent checkout's; `land --annex` and `abandon --annex` do
it for one annex ahead of the checkout's own landing, and `close
--annex` removes the worktree with its branch.  Two checkouts relocking
the same annex on different branches meet in the deploy's union: their
lock nodes are merged like the trees are (solve_lock), the annex branches
merged in the annex repository and the result pushed to the annex origin's
refs/nixconvoy/union/<input>/<sha>, which the union's node locks - a
derived value like the union commit, deterministic across retries; and
once one of them lands, the other's rebase onto main meets the landed
node, which rebases its annex branch onto the landed default branch
(solve_rebase).  Only the git side lives here: each relock is `nix flake
lock --override-input` on a scratch copy of the flake (lock_with, replaced
by the tests), so the node's hash and the nodes of the input's own inputs
are nix's."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

from . import proc
from .checkout import DATELESS_ENV, DEPLOYING_REFS, MACHINE_ENV, PENDING_REFS, TMP_PREFIX, tree_less_msg
from .config import MSG_FILE, cfg
from .guard import decide
from .proc import die, git, is_ancestor, load_json, ok, out, run, solve_with_mergiraf

ANNEX_DIR = "annexes"
# On an annex's origin: every tip an annex branch left behind (a reset, a
# rebase, its removal), kept reachable for the deploy records that lock
# revs of it (nix fetches a rev by `ref=` only while a ref holds it) as
# refs/nixconvoy/past/<branch>/<sha>, for PAST_DAYS after it was left.
PAST_REFS = "refs/nixconvoy/past"
PAST_DAYS = 90
# On an annex's origin: the merges of annex branches the deploys' unions
# carry (union_node), refs/nixconvoy/union/<input>/<sha>; one that no ref
# of this repository's origin locks any more (its deploy history of
# PAST_DAYS included) is deleted by the next union, once UNION_GRACE old -
# a deploy elsewhere pushes its union before the records locking it.
UNION_REFS = "refs/nixconvoy/union"
UNION_GRACE = 86400
# On an annex's origin, beside each past and union ref: a dated empty
# commit saying when the ref was pushed (the union commits' own dates are
# fixed, for determinism), refs/nixconvoy/stamp/<sha>.
STAMP_REFS = "refs/nixconvoy/stamp"
ANNEX_REFSPECS = ("+refs/heads/*:refs/remotes/origin/*", f"+{PAST_REFS}/*:{PAST_REFS}/*",
                  f"+{UNION_REFS}/*:{UNION_REFS}/*", f"+{STAMP_REFS}/*:{STAMP_REFS}/*")
# In an annex repository's common git dir once nixconvoy has used it as
# an annex: a hook can then tell an agent's commit on its default branch
# from work in an annex worktree (nixconvoy's integrations/git-hooks).
MARKER = "nixconvoy-annexed"


class Annex(NamedTuple):
    name: str  # the flake input
    repo: Path  # the repository's checkout here; its origin is what the lock fetches
    dir: Path  # <checkout>/annexes/<name>


def configured() -> dict[str, Path]:
    return {n: Path(r).expanduser() for n, r in cfg().annexes.items()}


def present(top: Path) -> list[Annex]:
    """The configured annexes with a worktree under `top`."""
    found = []
    for name, repo in configured().items():
        d = top / ANNEX_DIR / name
        if (d / ".git").exists():
            found.append(Annex(name, repo, d))
    return found


def branch_at(d: Path) -> str | None:
    p = run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=str(d), check=False)
    return p.stdout.strip() or None


def default_branch_of(repo: Path) -> str | None:
    """The annex repository's default branch (origin/HEAD, else main or
    master), None when origin has none of them."""
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


def rev_in(d: Path, ref: str) -> str | None:
    p = run(["git", "rev-parse", "-q", "--verify", ref], cwd=str(d), check=False)
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def index_tree(d: Path, *, less_msg: bool = False) -> str:
    """The tree of `d`'s index (`less_msg`: without MSG_FILE), through a
    copy (write-tree on the real one would rewrite its cache-tree
    extension)."""
    fd, tmp = tempfile.mkstemp()
    os.close(fd)
    try:
        shutil.copy(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
                        cwd=str(d)), tmp)
        env = {"GIT_INDEX_FILE": tmp}
        if less_msg:
            git("update-index", "--force-remove", MSG_FILE, env=env, cwd=str(d))
        p = run(["git", "write-tree"], env=env, cwd=str(d), check=False)
        if p.returncode != 0:
            die(f"cannot snapshot the index of {d} (unmerged paths?)")
        return p.stdout.strip()
    finally:
        Path(tmp).unlink()


def staged(d: Path, *, less_msg: bool = False) -> bool:
    """Whether `d`'s index differs from HEAD (`less_msg`: MSG_FILE aside,
    which a landing reads from the worktree and never deploys)."""
    if less_msg:
        return index_tree(d, less_msg=True) != tree_less_msg("HEAD", cwd=str(d))
    return index_tree(d) != out(["git", "rev-parse", "HEAD^{tree}"], cwd=str(d))


def mark(repo: Path) -> None:
    """Leave MARKER in the annex repository's common git dir."""
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                 cwd=str(repo), check=False).stdout.strip()
    if common and not (Path(common) / MARKER).exists():
        with contextlib.suppress(OSError):
            (Path(common) / MARKER).write_text(f"{Path.cwd()}\n")


# --- the lock node ---------------------------------------------------------

def lock_path() -> Path:
    return Path(cfg().flake_file("flake.lock"))


def node_key(lock: object, name: str) -> str | None:
    """The key of the root input `name`'s node in `lock`, when it has one."""
    if not isinstance(lock, dict):
        return None
    nodes, root = lock.get("nodes"), lock.get("root")
    if not isinstance(nodes, dict) or not isinstance(root, str) or not isinstance(nodes.get(root), dict):
        return None
    inputs = nodes[root].get("inputs")
    key = inputs.get(name) if isinstance(inputs, dict) else None
    return key if isinstance(key, str) and isinstance(nodes.get(key), dict) else None


def lock_node(lock: dict[str, object], name: str) -> dict[str, object]:
    """The node of the root input `name`; its locked/original tables are
    the node's own."""
    nodes, root = lock.get("nodes"), lock.get("root")
    if not isinstance(nodes, dict) or not isinstance(root, str) or not isinstance(nodes.get(root), dict):
        die(f"{lock_path()}: malformed")
    key = node_key(lock, name)
    if key is None:
        die(f"{name} is not a flake input of {cfg().flake_file('flake.nix')} (as {lock_path()} "
            f"locks it); annexes name root inputs")
    return nodes[key]


def flakeref(original: dict[str, object], ref: str, sha: str) -> str:
    """The flake reference fetching `sha` of the input as `original` names
    it, on the full ref `ref`: what the lock node will lock."""
    kind = original.get("type")
    # Every other attribute of the original (dir, shallow, submodules, lfs,
    # host, ...) rides along, so the locked table keeps them.
    rest = {k: ("1" if v is True else str(v)) for k, v in original.items()
            if k not in ("type", "url", "owner", "repo", "ref", "rev") and v is not False and v is not None}
    if kind == "git":
        params = {"ref": ref, "rev": sha, **rest}
        return f"git+{original.get('url')}?{urlencode(params, safe='/')}"
    if kind == "github":
        tail = f"?{urlencode(rest, safe='/')}" if rest else ""
        return f"github:{original.get('owner')}/{original.get('repo')}/{sha}{tail}"
    return die(f"an annex input must be a git or github input, not {kind!r}")


def lock_with(name: str, ref: str, lock: dict[str, object], flake_nix: str) -> dict[str, object] | None:
    """`lock` with its root input `name` locked on the flake reference
    `ref`: `nix flake lock --override-input` run on a scratch flake of
    `flake_nix` (the flake.nix text `lock` locks the inputs of) and
    `lock`, so nix writes the node's locked table and the nodes of the
    input's own inputs, which follow the ref.  None when nix refuses
    (said on stderr)."""
    with tempfile.TemporaryDirectory(prefix=f"{TMP_PREFIX}lock.", dir="/tmp") as tmp:
        Path(tmp, "flake.nix").write_text(flake_nix, encoding="utf-8")
        Path(tmp, "flake.lock").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # `path:`, so nix never takes an enclosing directory for a git repository.
        p = run(["nix", "flake", "lock", "--override-input", name, ref, f"path:{tmp}"], check=False, timeout=300)
        if p.returncode != 0:
            print(f"note: nix flake lock --override-input {name} {ref} failed:\n{(p.stderr or '').strip()}",
                  file=sys.stderr)
            return None
        try:
            got = load_json(Path(tmp, "flake.lock").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"note: nix flake lock --override-input {name} {ref} left no lock: {e}", file=sys.stderr)
            return None
    key = node_key(got, name)
    if key is None or not isinstance(got, dict):
        print(f"note: nix flake lock --override-input {name} {ref} wrote no node for {name}", file=sys.stderr)
        return None
    return got


def index_lock() -> str:
    """The lock as the index holds it: what the deploy snapshots."""
    p = run(["git", "show", f":{lock_path()}"], check=False)
    return p.stdout if p.returncode == 0 else ""


def flake_text(wt: Path | None = None) -> str:
    """The flake.nix as the index holds it, or as the worktree `wt` does."""
    path = cfg().flake_file("flake.nix")
    if wt is None:
        p = run(["git", "show", f":{path}"], check=False)
        return p.stdout if p.returncode == 0 else ""
    try:
        return (wt / path).read_text(encoding="utf-8")
    except OSError:
        return ""


def load_lock() -> dict[str, object]:
    return parse_lock(index_lock(), f"{lock_path()} (index)")


def parse_lock(text: str, what: str) -> dict[str, object]:
    try:
        doc = load_json(text)
    except ValueError as e:
        die(f"{what}: {e}")
    if not isinstance(doc, dict):
        die(f"{what}: not a lock file")
    return doc


def write_lock(lock: dict[str, object], was: str) -> None:
    """Stage `lock` as the index's copy; the working-tree file follows
    only when it still read `was` (an unstaged edit there is kept, and said)."""
    text = json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    blob = out(["git", "hash-object", "-w", "--stdin"], input=text)
    git("update-index", "--add", "--cacheinfo", f"100644,{blob},{lock_path()}")
    path = lock_path()
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current == was:
        path.write_text(text, encoding="utf-8")
    else:
        print(f"note: {path} has unstaged edits; the relock is staged, the working copy left as is",
              file=sys.stderr)


def relock(name: str, branch: str, sha: str) -> bool:
    """Point `name`'s lock node at `sha` on `branch`, staged: whether it
    moved."""
    was = index_lock()
    lock = parse_lock(was, f"{lock_path()} (index)")
    node = lock_node(lock, name)
    locked, original = node.get("locked"), node.get("original")
    if not isinstance(original, dict):
        die(f"{lock_path()}: {name} has no original table")
    want_ref = f"refs/heads/{branch}" if original.get("type") == "git" else None
    if isinstance(locked, dict) and locked.get("rev") == sha and \
            (want_ref is None or locked.get("ref") == want_ref):
        return False
    got = lock_with(name, flakeref(original, f"refs/heads/{branch}", sha), lock, flake_text())
    if got is None:
        die(f"annex {name}: locking {sha[:12]} on {branch} failed")
    write_lock(got, was)
    return True


def origin_url(original: dict[str, object]) -> str:
    """Where the input's repository is fetched from, as the lock names it."""
    if original.get("type") == "git" and isinstance(original.get("url"), str):
        return str(original["url"])
    if original.get("type") == "github":
        return f"https://github.com/{original.get('owner')}/{original.get('repo')}"
    return die(f"an annex input must be a git or github input, not {original.get('type')!r}")


class Relock(NamedTuple):
    """A lock merge's answer for a node nix is to lock (lock_with): the
    flake reference, and `done`, the node to keep given the one nix wrote."""
    ref: str
    done: Callable[[dict[str, object]], dict[str, object]]


class Pending(NamedTuple):
    """An annex branch solve_rebase rebased for a work branch's rebase onto
    main: pushed once that rebase succeeds, put back if it aborts."""
    ax: Annex
    wb: str
    was: str
    relock: Relock


# Per run: the annex repositories fetched, the fetches under way beside
# the deploy's own (prefetch), and the rebased annexes.
FETCHED: set[Path] = set()
PREFETCH: dict[Path, Future[bool]] = {}
PENDING: dict[tuple[str, str], Pending] = {}


def fetch_refs(name: str, repo: Path) -> bool:
    """Fetch the annex repository `repo`'s origin: branches, past and
    union refs, the gone ones pruned; whether it is fetched (else said)."""
    p = run(["git", "fetch", "--quiet", "--prune", "origin", *ANNEX_REFSPECS],
            cwd=str(repo), check=False, timeout=300)
    if p.returncode != 0:
        if not proc.DYING:  # killed by a die under way: not news
            print(f"note: annex {name}: fetching its origin failed:\n{(p.stderr or '').strip()}", file=sys.stderr)
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
    """The present annexes' origins fetched on `pool`, beside the deploy's
    own fetch, for the catch-up of the annex step to find done."""
    for ax in present(top):
        if ax.repo not in FETCHED and ax.repo not in PREFETCH:
            PREFETCH[ax.repo] = pool.submit(fetch_refs, ax.name, ax.repo)


def locked_revs(name: str) -> set[str]:
    """Every rev of the input `name` locked by a ref this repository's
    origin holds, as fetched - its branches (main, the work branches), the
    deploys' announcements and records, and the deploy records' history
    of the last PAST_DAYS."""
    commits = set(out(["git", "for-each-ref", "--format=%(objectname)", "refs/remotes/origin/",
                       PENDING_REFS, DEPLOYING_REFS]).split())
    commits |= set(run(["git", "rev-list", "--first-parent", f"--since={PAST_DAYS}.days",
                        "--glob=refs/remotes/origin/deployed/*"], check=False).stdout.split())
    path = lock_path()
    # The lock blobs, then each distinct one once, in one stream.
    listing = run(["git", "cat-file", "--batch-check=%(objectname)"], input="".join(f"{c}:{path}\n" for c in commits),
                  check=False).stdout.split()
    blobs = sorted({b for b in listing if len(b) == 40})
    docs = subprocess.run(["git", "cat-file", "--batch"], input="".join(f"{b}\n" for b in blobs).encode(),
                          capture_output=True, check=False).stdout
    found = set()
    pos = 0
    for _ in blobs:
        nl = docs.find(b"\n", pos)
        if nl < 0:
            break
        header = docs[pos:nl].split()
        if len(header) < 3:
            break
        size = int(header[2])
        body, pos = docs[nl + 1:nl + 1 + size], nl + 1 + size + 1
        try:
            lock = load_json(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        key = node_key(lock, name)
        if key is None:
            continue
        assert isinstance(lock, dict)
        nodes = lock["nodes"]
        assert isinstance(nodes, dict)
        locked = nodes[key].get("locked")
        if isinstance(locked, dict) and isinstance(locked.get("rev"), str):
            found.add(locked["rev"])
    return found


def stamp(repo: Path, sha: str) -> str:
    """A dated empty commit for STAMP_REFS/<sha>: when `sha`'s past or
    union ref was pushed."""
    tree = out(["git", "mktree"], input="", cwd=str(repo))
    return out(["git", "commit-tree", tree, "-m", f"stamp {sha}"], cwd=str(repo), env=DATELESS_ENV)


def stamp_refspec(repo: Path, sha: str) -> str:
    return f"+{stamp(repo, sha)}:{STAMP_REFS}/{sha}"


def stamps(repo: Path) -> dict[str, int]:
    """Each stamped sha's push time, as fetched."""
    listing = out(["git", "for-each-ref", "--format=%(refname:lstrip=3) %(committerdate:unix)", f"{STAMP_REFS}/"],
                  cwd=str(repo))
    return {sha: int(date) for sha, date in (ln.split() for ln in listing.splitlines())}


def delete_refs(name: str, repo: Path, refs: list[str], what: str) -> None:
    """`refs` deleted on the annex origin and here, with the stamps of
    the shas they name (a failure is a warning)."""
    if not refs:
        return
    refs = sorted({*refs, *(f"{STAMP_REFS}/{r.rsplit('/', 1)[1]}" for r in refs)})
    p = run(["git", "-c", "core.hooksPath=/dev/null", "push", "--quiet", "origin", *(f":{r}" for r in refs)],
            cwd=str(repo), check=False, timeout=120)
    if p.returncode != 0:
        print(f"warning: annex {name}: deleting its {what} on its origin failed:\n{(p.stderr or '').strip()}",
              file=sys.stderr)
        return
    for r in refs:
        git("update-ref", "-d", r, cwd=str(repo), check=False)


def prune_union_refs(name: str, repo: Path, keep: str) -> None:
    """The union refs of `name` nothing locks any more deleted, once
    UNION_GRACE old: `keep` (this union's) and the revs locked_revs finds
    stay; one without a stamp is stamped now, for the next prune."""
    wanted = locked_revs(name) | {keep}
    listed = [r for r in out(["git", "for-each-ref", "--format=%(refname)", f"{UNION_REFS}/{name}/"],
                             cwd=str(repo)).split() if r.rsplit("/", 1)[1] not in wanted]
    dated = stamps(repo)
    cutoff = time.time() - UNION_GRACE
    stale = [r for r in listed if (sha := r.rsplit("/", 1)[1]) in dated and dated[sha] < cutoff]
    delete_refs(name, repo, stale, "union refs")
    unstamped = [stamp_refspec(repo, sha) for r in listed if (sha := r.rsplit("/", 1)[1]) not in dated]
    if unstamped:
        run(["git", "-c", "core.hooksPath=/dev/null", "push", "--quiet", "origin", *unstamped],
            cwd=str(repo), check=False, timeout=120)


def prune_past_refs(name: str, repo: Path) -> None:
    """The past refs pushed over PAST_DAYS ago deleted (an unstamped one
    judged by its commit's date)."""
    cutoff = time.time() - PAST_DAYS * 86400
    dated = stamps(repo)
    listing = out(["git", "for-each-ref", "--format=%(refname) %(committerdate:unix)", f"{PAST_REFS}/"],
                  cwd=str(repo))
    stale = [ref for ref, date in (ln.split() for ln in listing.splitlines())
             if dated.get(ref.rsplit("/", 1)[1], int(date)) < cutoff]
    delete_refs(name, repo, stale, "past refs")


@contextlib.contextmanager
def repo_lock(repo: Path) -> Iterator[None]:
    """A lock on the annex repository, serialising the deploys merging in
    it (two subagents' unions at once)."""
    common = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(repo)))
    with (common / "nixconvoy-annex.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        yield


def merge_commit(name: str, repo: Path, ours: str, theirs: str, subject: str) -> str | None:
    """`theirs` merged into `ours` in a throwaway worktree of `repo`
    (mergiraf on the conflicts, MSG_FILE dropped) as a deterministic
    commit: its sha, or None when they conflict (said on stderr)."""
    wt = tempfile.mkdtemp(prefix=f"{TMP_PREFIX}annex-merge.", dir="/tmp")
    try:
        git("worktree", "add", "--detach", "--quiet", wt, ours, cwd=str(repo))
        p = git("merge", "--quiet", "--no-edit", "-m", subject, theirs, cwd=wt, env=MACHINE_ENV, check=False)
        if p.returncode != 0:
            for f in out(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt).splitlines():
                if f == MSG_FILE:
                    git("rm", "--quiet", "-f", f, cwd=wt, check=False)
                elif solve_with_mergiraf(wt, f):
                    git("add", "--", f, cwd=wt, check=False)
                else:
                    print(f"note: annex {name}: {ours[:12]} and {theirs[:12]} conflict in {f}", file=sys.stderr)
                    return None
            git("commit", "--quiet", "--no-edit", "--no-verify", cwd=wt, env=MACHINE_ENV)
        return out(["git", "rev-parse", "HEAD"], cwd=wt)
    finally:
        git("worktree", "remove", "--force", wt, cwd=str(repo), check=False, cleanup=True)
        shutil.rmtree(wt, ignore_errors=True)


def store(name: str, url: str) -> Path:
    """The repository the annex `name`'s branches are merged in: its
    configured checkout when it is here, else a bare mirror of `url`
    under the cache dir (a machine deploying without the repository)."""
    repo = configured().get(name)
    if repo is not None and (repo / ".git").exists():
        return repo
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "nixconvoy" / "annexes" / f"{name}.git"
    if not cache.is_dir():
        cache.parent.mkdir(parents=True, exist_ok=True)
        p = run(["git", "clone", "--quiet", "--bare", url, str(cache)], check=False, timeout=600)
        if p.returncode != 0:
            die(f"annex {name}: cloning {url} to union its branches failed:\n{(p.stderr or '').strip()}")
    return cache


Resolved = dict[str, object] | Relock | None


def union_node(name: str, ours: dict[str, object], theirs: dict[str, object]) -> Resolved:
    """The lock node carrying both `ours` and `theirs`, two nodes of the
    annex `name` locking different revs: the one containing the other,
    else their merge (mergiraf on the conflicts) as a deterministic commit
    pushed to UNION_REFS/<name>/<sha> on the annex origin, to be locked
    there (the union refs nothing locks any more pruned); None when they
    conflict (said on stderr)."""
    o, t = ours.get("locked"), theirs.get("locked")
    original = ours.get("original")
    if not (isinstance(o, dict) and isinstance(t, dict) and isinstance(original, dict)):
        return None
    o_rev, t_rev = o.get("rev"), t.get("rev")
    if not (isinstance(o_rev, str) and isinstance(t_rev, str)):
        return None
    repo = store(name, origin_url(original))
    with repo_lock(repo):
        if not fetch_once(name, repo):
            return None
        for sha in (o_rev, t_rev):
            if rev_in(repo, f"{sha}^{{commit}}") is None:
                print(f"note: annex {name}: {sha[:12]} is not on its origin", file=sys.stderr)
                return None
        if is_ancestor(t_rev, o_rev, cwd=str(repo)):
            return ours
        if is_ancestor(o_rev, t_rev, cwd=str(repo)):
            return theirs
        sha = merge_commit(name, repo, o_rev, t_rev, f"union of {name}")
        if sha is None:
            return None
        ref = f"{UNION_REFS}/{name}/{sha}"
        # Restamped on every push: the grace runs from the last deploy using it.
        stamped = stamp_refspec(repo, sha)
        p = run(["git", "-c", "core.hooksPath=/dev/null", "push", "--quiet", "--force", "--atomic", "origin",
                 f"{sha}:{ref}", stamped], cwd=str(repo), check=False, timeout=120)
        if p.returncode != 0:
            die(f"annex {name}: pushing its union to {ref} failed:\n{(p.stderr or '').strip()}")
        git("update-ref", ref, sha, cwd=str(repo), check=False)
        git("update-ref", f"{STAMP_REFS}/{sha}", stamped[1:].partition(":")[0], cwd=str(repo), check=False)
        prune_union_refs(name, repo, sha)
    print(f"annex {name}: union of {o_rev[:12]} and {t_rev[:12]} is {sha[:12]}, on {ref}")
    return Relock(flakeref(original, ref, sha), lambda node: node)


def merge_locks(base: object, ours: object, theirs: object,
                resolve: Callable[[str, dict[str, object], dict[str, object]], Resolved],
                flake_nix: str = "") -> dict[str, object] | None:
    """`ours` with every node `theirs` alone changed since `base`; a node
    both changed is `resolve`d when it is an annex's - to a node, or to a
    Relock nix then locks it by (lock_with, on `flake_nix`) - else the
    merge fails (None)."""
    if not all(isinstance(x, dict) and isinstance(x.get("nodes"), dict) for x in (base, ours, theirs)):
        return None
    assert isinstance(base, dict) and isinstance(ours, dict) and isinstance(theirs, dict)
    b_nodes, o_nodes, t_nodes = base["nodes"], ours["nodes"], theirs["nodes"]
    assert isinstance(b_nodes, dict) and isinstance(o_nodes, dict) and isinstance(t_nodes, dict)
    root = ours.get("root")
    root_node = o_nodes.get(root) if isinstance(root, str) else None
    inputs = root_node.get("inputs") if isinstance(root_node, dict) else None
    annexed = ({key: name for name, key in inputs.items() if name in configured() and isinstance(key, str)}
               if isinstance(inputs, dict) else {})
    owned = owned_nodes(o_nodes, t_nodes, root if isinstance(root, str) else "", annexed)
    merged = json.loads(json.dumps(ours))
    relocks: list[tuple[str, Relock]] = []
    # An annex's decision, which the nodes of its own inputs follow.
    taken: dict[str, str] = {}
    for key in sorted(set(o_nodes) | set(t_nodes)):
        o, t, b = o_nodes.get(key), t_nodes.get(key), b_nodes.get(key)
        if key in owned:
            continue
        if o == t:
            continue
        if b == o:
            if t is None:
                merged["nodes"].pop(key, None)
            else:
                merged["nodes"][key] = t
            if key in annexed:
                taken[annexed[key]] = "theirs"
        elif b == t:
            if key in annexed:
                taken[annexed[key]] = "ours"
        elif key in annexed and isinstance(o, dict) and isinstance(t, dict):
            got = resolve(annexed[key], o, t)
            if got is None:
                return None
            if isinstance(got, Relock):
                relocks.append((annexed[key], got))
                taken[annexed[key]] = "relock"
            else:
                merged["nodes"][key] = got
                taken[annexed[key]] = "theirs" if got == t else "ours"
        else:
            return None
    for key, name in owned.items():
        o, t, b = o_nodes.get(key), t_nodes.get(key), b_nodes.get(key)
        side = taken.get(name)
        if side == "relock" or o == t or (side is None and b == t) or (side == "ours"):
            continue  # nix rewrites them, or ours stays
        if side == "theirs" or b == o:
            if t is None:
                merged["nodes"].pop(key, None)
            else:
                merged["nodes"][key] = t
        else:
            return None
    for name, todo in relocks:
        merged = lock_with(name, todo.ref, merged, flake_nix)
        if merged is None:
            return None
        key = node_key(merged, name)
        nodes = merged["nodes"]
        assert key is not None and isinstance(nodes, dict)
        nodes[key] = todo.done(nodes[key])
    return merged


def owned_nodes(o_nodes: dict[str, object], t_nodes: dict[str, object], root: str,
                annexed: dict[str, str]) -> dict[str, str]:
    """The lock nodes of each annex's own inputs (key -> annex), on either
    side: reached from its node alone, never from the root or another
    annex without passing through it."""
    def below(nodes: dict[str, object], start: str, stop: set[str]) -> set[str]:
        seen, todo = set(), [start]
        while todo:
            key = todo.pop()
            node = nodes.get(key)
            inputs = node.get("inputs") if isinstance(node, dict) else None
            for v in (inputs.values() if isinstance(inputs, dict) else ()):
                if isinstance(v, str) and v not in seen and v not in stop:
                    seen.add(v)
                    todo.append(v)
        return seen

    owned: dict[str, str] = {}
    shared: set[str] = set()
    # The root's own on either side (nix keys nodes by name and rank, so a
    # key may be the root's here and an annex's there).
    plain = below(o_nodes, root, set(annexed)) | below(t_nodes, root, set(annexed))
    for nodes in (o_nodes, t_nodes):
        for key, name in annexed.items():
            for k in below(nodes, key, set(annexed) - {key}) - plain:
                if owned.get(k, name) != name:
                    shared.add(k)
                owned[k] = name
    return {k: n for k, n in owned.items() if k not in shared and k not in annexed}


def conflict_stages(wt: Path, path: str) -> tuple[object, object, object] | None:
    """The base, ours and theirs of the conflicted lock `path` in `wt`,
    parsed; None unless all three are there and parse."""
    found = []
    for n in (1, 2, 3):
        p = run(["git", "show", f":{n}:{path}"], cwd=str(wt), check=False)
        if p.returncode != 0:
            return None
        try:
            found.append(load_json(p.stdout))
        except ValueError:
            return None
    return found[0], found[1], found[2]


def write_merged(wt: Path, path: str, lock: dict[str, object]) -> None:
    (wt / path).write_text(json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                           encoding="utf-8")


def solve_lock(wt: Path) -> bool:
    """A deploy's union: the lock conflicted in the union worktree `wt`
    (another branch relocked a node this union carries too) merged node by
    node, each annex both sides moved locked on the union of its branches
    (union_node).  Whether it resolved; the file is left for `git add`."""
    path = str(lock_path())
    stages = conflict_stages(wt, path)
    if stages is None:
        return False
    merged = merge_locks(*stages, resolve=union_node, flake_nix=flake_text(wt))
    if merged is None:
        return False
    write_merged(wt, path, merged)
    return True


def solve_rebase(wt: Path, checkout: Path) -> bool:
    """A rebase of `checkout`'s work branch onto origin/main stopped on
    the lock (in `wt`: the checkout itself, or the throwaway worktree a
    branch not checked out is rebased in) because main locks an annex
    elsewhere (its landing) that a replayed commit relocked on this
    branch: the annex worktree here is rebased onto the default branch
    (once per run: every later replayed commit locks the same tip) and the
    node locked on its tip, whose hash comes from the repository here; the
    push waits for the rebase's end (push_pending, else undo_pending).
    Whether it resolved, the lock then staged for `git rebase --continue`."""
    path = str(lock_path())
    if out(["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(wt)).splitlines() != [path]:
        return False
    stages = conflict_stages(wt, path)
    if stages is None:
        return False
    # HEAD is detached mid-rebase: the branch is in the rebase's state.
    try:
        head_name = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "rebase-merge/head-name"],
                             cwd=str(wt))).read_text(encoding="utf-8").strip()
    except OSError:
        head_name = ""
    wb = head_name.removeprefix("refs/heads/") or branch_at(checkout) or ""

    def rebase_annex(name: str, upstream: dict[str, object], mine: dict[str, object]) -> Resolved:
        # upstream (stage 2) is main's side; mine (stage 3) the replayed commit's.
        if (name, wb) in PENDING:
            return PENDING[name, wb].relock
        up, me, original = upstream.get("locked"), mine.get("locked"), mine.get("original")
        if not (isinstance(up, dict) and isinstance(me, dict) and isinstance(original, dict)):
            return None
        if original.get("type") == "git" and me.get("ref") != f"refs/heads/{wb}":
            return None
        found = [ax for ax in present(checkout) if ax.name == name]
        if not found or branch_at(found[0].dir) != wb:
            print(f"note: annex {name}: no worktree on {wb} under {checkout} to rebase it in", file=sys.stderr)
            return None
        ax = found[0]
        if out(["git", "status", "--porcelain", "-uno"], cwd=str(ax.dir)):
            print(f"note: annex {name} has edits, which its rebase onto the landed branch cannot keep", file=sys.stderr)
            return None
        tip_locked = tip_lock_rev(wt, name) or (str(me["rev"]) if isinstance(me.get("rev"), str) else None)
        gone = reset_below_deployed(ax, tip_locked)
        if gone:  # a hand reset: relocking onto HEAD would drop them in silence
            assert tip_locked is not None
            print(f"note: {dropped_message(ax, wb, tip_locked, gone)}", file=sys.stderr)
            return None
        behind = left_behind(ax, tip_locked)
        if behind:
            print(f"note: annex {name}: relocking onto its worktree leaves {len(behind)} deployed commit(s) "
                  f"behind as such (rebased, amended or dropped):\n  " + "\n  ".join(behind), file=sys.stderr)
        if not fetch_once(name, ax.repo):
            return None
        was = rev_in(ax.dir, "HEAD")
        default = default_branch(ax.repo)
        p = git("rebase", "--quiet", f"refs/remotes/origin/{default}", cwd=str(ax.dir), check=False)
        if p.returncode != 0:
            git("rebase", "--abort", cwd=str(ax.dir), check=False)
            print(f"note: rebasing annex {name}'s {wb} onto origin/{default} is not clean; resolve it in {ax.dir}",
                  file=sys.stderr)
            return None
        head = rev_in(ax.dir, "HEAD")
        assert head is not None and was is not None

        def done(node: dict[str, object]) -> dict[str, object]:
            # Locked from the repository here (the tip is not on origin
            # yet), named after the input's origin as the lock will fetch it.
            local = node.get("locked")
            assert isinstance(local, dict)
            if original.get("type") == "github":
                locked: dict[str, object] = {"type": "github", "owner": original.get("owner"),
                                             "repo": original.get("repo"), "rev": head,
                                             "narHash": local.get("narHash"),
                                             "lastModified": local.get("lastModified")}
            else:
                locked = {**local, "url": original.get("url")}
            return {**node, "locked": locked}

        todo = Relock(flakeref({"type": "git", "url": f"file://{ax.repo.resolve()}"}, f"refs/heads/{wb}", head), done)
        PENDING[name, wb] = Pending(ax, wb, was, todo)
        print(f"annex {name}: {wb} rebased onto origin/{default} ({head[:12]})")
        return todo

    base, upstream, mine = stages
    merged = merge_locks(base, upstream, mine, resolve=rebase_annex, flake_nix=flake_text(wt))
    if merged is None:
        return False
    write_merged(wt, path, merged)
    git("add", "--", path, cwd=str(wt))
    return True


def push_pending() -> None:
    """The rebase succeeded: the rebased annex branches pushed."""
    for pending in PENDING.values():
        push_branch(pending.ax, pending.wb)
        print(f"annex {pending.ax.name}: pushed {pending.wb}")
    PENDING.clear()


def undo_pending() -> None:
    """The rebase aborted: each rebased annex put back where it was."""
    for pending in PENDING.values():
        git("reset", "--quiet", "--hard", pending.was, cwd=str(pending.ax.dir), check=False)
        print(f"annex {pending.ax.name}: {pending.wb} put back to {pending.was[:12]}", file=sys.stderr)
    PENDING.clear()


def original_of(name: str) -> dict[str, object]:
    original = lock_node(load_lock(), name).get("original")
    return original if isinstance(original, dict) else {}


def locked_rev(name: str) -> tuple[str | None, str | None]:
    """(rev, ref) `name`'s lock node locks now."""
    locked = lock_node(load_lock(), name).get("locked")
    if not isinstance(locked, dict):
        return None, None
    r, b = locked.get("rev"), locked.get("ref")
    return (r if isinstance(r, str) else None), (b if isinstance(b, str) else None)


# --- the verbs -------------------------------------------------------------

def create(name: str, wb: str) -> Path:
    """`nixconvoy open --annex <name>`: the annex worktree under this checkout, on
    `wb` - origin's copy of the branch when it has one (a worktree
    re-created), else a new branch off the repository's default."""
    repo = configured_repo(name)
    if not (repo.is_dir() and ok(["git", "rev-parse", "--git-dir"], cwd=str(repo))):
        die(f"annex {name}: {repo} is not a git repository")
    lock_node(load_lock(), name)
    top = Path.cwd()
    d = top / ANNEX_DIR / name
    if (d / ".git").exists():
        cur = branch_at(d)
        if cur != wb:
            die(f"annex {name} at {d} is on {cur or 'a detached HEAD'}, not {wb}: "
                f"`git -C {d} switch {wb}`")
        print(f"annex {name}: {d} (on {wb})")
        return d
    if d.exists():
        die(f"{d} exists and is no worktree: move it away")
    if not ok(["git", "check-ignore", "-q", f"{ANNEX_DIR}/{name}"]):
        die(f"add `{ANNEX_DIR}/` to this repo's .gitignore first: an annex is a nested "
            f"checkout, which git would otherwise list as untracked and `git add .` stage as a gitlink")
    git("worktree", "prune", cwd=str(repo), check=False)
    p = run(["git", "fetch", "--quiet", "origin"], cwd=str(repo), check=False, timeout=120)
    if p.returncode != 0:
        die(f"annex {name}: fetching {repo}'s origin failed:\n{(p.stderr or '').strip()}")
    default = default_branch(repo)
    d.parent.mkdir(exist_ok=True)
    remote, local = rev_in(repo, f"refs/remotes/origin/{wb}"), rev_in(repo, f"refs/heads/{wb}")
    if remote is not None and local is not None and not is_ancestor(local, remote, cwd=str(repo)):
        if not is_ancestor(remote, local, cwd=str(repo)):
            die(f"annex {name}: the local branch {wb} ({local[:12]}) and origin's ({remote[:12]}) diverged: "
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
    print(f"annex {name}: {d} on {wb} (from {where})")
    return d


def configured_repo(name: str) -> Path:
    repos = configured()
    if name not in repos:
        die(f"{name} is not a configured annex" + (
            f" (they are: {', '.join(sorted(repos))})" if repos else
            " (nixconvoy.toml: [annexes.<input>] repo = \"...\")"))
    return repos[name]


def present_one(top: Path, name: str) -> Annex:
    found = [ax for ax in present(top) if ax.name == name]
    if not found:
        configured_repo(name)
        die(f"no annex {name} under this checkout ({top / ANNEX_DIR / name})")
    return found[0]


def carries_one(ax: Annex) -> bool:
    """Whether the annex has something a deploy would carry into the lock:
    staged changes, or a HEAD beyond its default branch as last fetched
    that the lock does not deploy yet."""
    if staged(ax.dir):
        return True
    head = rev_in(ax.dir, "HEAD")
    default = default_branch_of(ax.repo)
    return default is None or (locked_rev(ax.name)[0] != head and not ok(
        ["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{default}"], cwd=str(ax.dir)))


def carries(top: Path) -> bool:
    """carries_one over the annexes present under `top`."""
    return any(carries_one(ax) for ax in present(top))


def reset_below_deployed(ax: Annex, locked: str | None) -> list[str]:
    """The deployed commits a worktree reset below its own work dropped:
    when HEAD holds nothing beyond the default branch as fetched while
    `locked` (the rev the lock deploys of this annex) does, the commits of
    `locked` beyond HEAD, one line each.  Empty while HEAD still carries
    work of its own - a rebase, an amend or a squash of the deployed
    commits is the branch's business, said by left_behind - or the lock
    deploys nothing of the branch's."""
    default = default_branch_of(ax.repo)
    if locked is None or default is None or rev_in(ax.dir, f"{locked}^{{commit}}") is None:
        return []
    upstream = f"refs/remotes/origin/{default}"
    d = str(ax.dir)
    if is_ancestor(locked, "HEAD", cwd=d) or is_ancestor(locked, upstream, cwd=d) \
            or not is_ancestor("HEAD", upstream, cwd=d):
        return []
    return out(["git", "log", "--format=%h %s", f"HEAD..{locked}", "--not", upstream], cwd=d).splitlines()


def dropped_message(ax: Annex, wb: str, locked: str, gone: list[str]) -> str:
    return (f"annex {ax.name}'s worktree ({ax.dir}) was reset below the work the lock deploys on {wb}; "
            f"{len(gone)} commit(s) would leave the lock and the targets:\n  " + "\n  ".join(gone)
            + f"\nput them back (git -C {ax.dir} reset --keep {locked[:12]}), or drop them deliberately: "
            f"nixconvoy abandon --annex {ax.name}")


def left_behind(ax: Annex, locked: str | None) -> list[str]:
    """The deployed commits (up to `locked`) a relock onto HEAD leaves behind
    as such - rebased, amended, squashed or dropped - as `git cherry` tells
    them from the patches HEAD holds: one line each."""
    d = str(ax.dir)
    if locked is None or rev_in(ax.dir, f"{locked}^{{commit}}") is None or is_ancestor(locked, "HEAD", cwd=d):
        return []
    gone = [ln[2:] for ln in out(["git", "cherry", "HEAD", locked], cwd=d).splitlines() if ln.startswith("+ ")]
    return [out(["git", "log", "-1", "--format=%h %s", sha], cwd=d) for sha in gone]


def refuse_dropped(ax: Annex, wb: str) -> None:
    """Die when the worktree was reset below the work the lock deploys on
    `wb`, rather than relock it away in silence."""
    locked = locked_rev(ax.name)[0]
    gone = reset_below_deployed(ax, locked)
    if gone:
        assert locked is not None
        die(dropped_message(ax, wb, locked, gone))


def tip_lock_rev(wt: Path, name: str) -> str | None:
    """The rev `name`'s node locks at the tip the rebase under way in `wt`
    replays (rebase-merge/orig-head: the branch as it was, tmp commits
    included), None when unknown."""
    try:
        orig = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "rebase-merge/orig-head"],
                        cwd=str(wt))).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    p = run(["git", "show", f"{orig}:{lock_path()}"], cwd=str(wt), check=False)
    if p.returncode != 0:
        return None
    try:
        lock = load_json(p.stdout)
    except ValueError:
        return None
    if not isinstance(lock, dict) or node_key(lock, name) is None:
        return None
    locked = lock_node(lock, name).get("locked")
    r = locked.get("rev") if isinstance(locked, dict) else None
    return r if isinstance(r, str) else None


def catch_up(top: Path, wb: str) -> list[str]:
    """Each annex present under `top` (the current directory, whose lock a
    relock edits) brought onto its repository's default branch as fetched,
    the way a checkout is brought onto origin/main (the rebase core's
    Onto: its staged and unstaged state riding along as tmp commits; a
    fast-forward when it has nothing of its own).  One with commits of its
    own is pushed where its origin has the branch, and the lock naming its
    old tip (a deploy's staged relock) relocked on the new.  The problems,
    one line each, for the annexes left as they were: a rebase that is not
    clean, a worktree off `wb`, an origin that cannot be fetched while
    something waits to be relocked."""
    problems: list[str] = []
    for ax in present(top):
        try:
            problem = catch_up_one(ax, wb)
        except SystemExit:  # a die() along the way, said above: this annex's alone
            proc.DYING = False
            problem = f"annex {ax.name}: failed (above)"
        if problem is not None:
            problems.append(problem)
    return problems


def catch_up_one(ax: Annex, wb: str) -> str | None:
    """catch_up for one annex: the problem, or None."""
    cur = branch_at(ax.dir)
    if cur != wb:
        return (f"annex {ax.name} ({ax.dir}) is on {cur or 'a detached HEAD'}, not {wb}: "
                f"`git -C {ax.dir} switch {wb}`")
    if not fetch_once(ax.name, ax.repo):
        if carries_one(ax):
            return (f"annex {ax.name}: its origin could not be fetched (above), so its branch "
                    f"cannot be brought onto the default branch before its relock")
        return None
    upstream = f"refs/remotes/origin/{default_branch(ax.repo)}"
    was = rev_in(ax.dir, "HEAD")
    own = not ok(["git", "merge-base", "--is-ancestor", "HEAD", upstream], cwd=str(ax.dir))
    prev = Path.cwd()
    os.chdir(ax.dir)
    try:
        done = decide("rebase", mode="onto", upstream=upstream)
    finally:
        os.chdir(prev)
    assert isinstance(done, dict)
    status = str(done.get("status"))
    if done.get("ok") is not True:
        return f"annex {ax.name}: {status}"
    head = rev_in(ax.dir, "HEAD")
    if head is None or was is None or head == was:
        return None
    print(f"annex {ax.name}: {status}")
    if own:
        if rev_in(ax.dir, f"refs/remotes/origin/{wb}") is not None:
            push_branch(ax, wb)
        if locked_rev(ax.name)[0] == was and relock(ax.name, wb, head):
            print(f"annex {ax.name}: {lock_path()} locks {head[:12]} on {wb}")
    return None


def sync(wb: str | None, msg: str | None, *, dry: bool = False) -> list[str]:
    """A deploy's annex step: each present annex brought onto its default
    branch (catch_up, a problem there ending the deploy; not when `dry`), its staged state
    committed onto `wb` with `msg`, pushed, and its lock node relocked on
    it, staged here.  What changed, one line each.  `dry` (a dry
    activation) commits and pushes nothing, so an annex change it cannot
    carry is refused instead."""
    top = Path.cwd()
    found = present(top)
    if not found:
        return []
    if wb is None:
        die(f"{ANNEX_DIR}/ in the main checkout, which has no work branch to carry an annex: "
            f"remove it, or work in a checkout on a work branch")
    for ax in found:
        cur = branch_at(ax.dir)
        if cur != wb:
            die(f"annex {ax.name} ({ax.dir}) is on {cur or 'a detached HEAD'}, not {wb}: "
                f"`git -C {ax.dir} switch {wb}`")
        refuse_dropped(ax, wb)
    if not dry:  # a dry activation rebases, pushes and relocks nothing
        for problem in catch_up(top, wb):
            die(f"{problem}; nothing was deployed")
    told = []
    for ax in found:
        mark(ax.repo)
        if out(["git", "diff", "--name-only"], cwd=str(ax.dir)):
            print(f"note: annex {ax.name} has unstaged edits, which are not deployed", file=sys.stderr)
        if staged(ax.dir):
            if dry:
                die(f"annex {ax.name} has staged changes, which a dry activation cannot carry "
                    f"(the build fetches the annex from its origin): deploy, or unstage them")
            if not msg:
                die(f'annex {ax.name} has staged changes; describe this increment with -m "..."')
            git("commit", "--quiet", "-m", msg, cwd=str(ax.dir))
        head = rev_in(ax.dir, "HEAD")
        assert head is not None
        # Nothing of its own yet: the lock stays as it was (the branch is
        # off the default branch's tip, maybe past the locked rev).
        default = default_branch(ax.repo)
        if ok(["git", "merge-base", "--is-ancestor", head, f"refs/remotes/origin/{default}"], cwd=str(ax.dir)):
            continue
        # Behind the landed default branch, a relock would move the input
        # backwards for every target main's lock reaches.
        if not ok(["git", "merge-base", "--is-ancestor", f"refs/remotes/origin/{default}", head], cwd=str(ax.dir)):
            die(f"annex {ax.name}'s {wb} does not contain origin/{default}"
                + (" (a dry activation brings no annex along): deploy first" if dry
                   else f": rebase it in {ax.dir}, then re-run"))
        if dry and (rev_in(ax.dir, f"refs/remotes/origin/{wb}") != head or locked_rev(ax.name)[0] != head):
            die(f"annex {ax.name} has commits not deployed yet, which a dry activation cannot carry: "
                f"deploy first")
        if rev_in(ax.dir, f"refs/remotes/origin/{wb}") != head:
            push_branch(ax, wb)
            told.append(f"annex {ax.name}: pushed {wb} ({head[:12]})")
        behind = left_behind(ax, locked_rev(ax.name)[0])
        if behind:
            print(f"note: annex {ax.name}: relocking onto {head[:12]} leaves {len(behind)} deployed commit(s) "
                  f"behind as such (rebased, amended or dropped):\n  " + "\n  ".join(behind), file=sys.stderr)
        if relock(ax.name, wb, head):
            told.append(f"annex {ax.name}: {lock_path()} locks {head[:12]} on {wb}")
    return told


def land_all(wb: str, fallback_msg: str) -> None:
    """Each present annex landed (land_one); run by land before main's
    commit, so a landing retried finds nothing left to do here."""
    for ax in present(Path.cwd()):
        land_one(ax, wb, fallback_msg)


def land_one(ax: Annex, wb: str, fallback_msg: str) -> None:
    """The annex landed on its repository's default branch as one commit
    (the MSG_FILE in its worktree - tracked or not, it is read here alone -
    else `fallback_msg`) and its lock node relocked there, as a commit on
    `wb`."""
    cur = branch_at(ax.dir)
    if cur != wb:
        die(f"annex {ax.name} ({ax.dir}) is on {cur or 'a detached HEAD'}, not {wb}")
    if staged(ax.dir, less_msg=True):
        die(f"annex {ax.name} has staged but undeployed changes; deploy or unstage them first")
    # The rebase onto the default branch needs a clean tree.
    loose = [ln for ln in out(["git", "status", "--porcelain", "-uno"], cwd=str(ax.dir)).splitlines()
             if not ln.endswith(f" {MSG_FILE}")]
    if loose:
        die(f"annex {ax.name} has unstaged edits, which its rebase onto the default branch cannot keep: "
            f"stage and deploy them, or put them aside ({len(loose)} path(s))")
    if not fetch_refs(ax.name, ax.repo):
        die(f"annex {ax.name}: cannot land it unfetched")
    default = default_branch(ax.repo)
    main = rev_in(ax.dir, f"refs/remotes/origin/{default}")
    head = rev_in(ax.dir, "HEAD")
    assert main is not None and head is not None
    # A github input's lock names no branch.
    on_wb = f"refs/heads/{wb}" if original_of(ax.name).get("type") == "git" else None
    # The branch's own commits squashed onto the default branch - its
    # tip merged there as a deploy's union merges it (mergiraf on the
    # conflicts); one with none keeps its rev, relocked under the default
    # branch's name.
    landed = head
    if not ok(["git", "merge-base", "--is-ancestor", head, main], cwd=str(ax.dir)):
        deployed = locked_rev(ax.name)
        if deployed != (head, on_wb):
            die(f"annex {ax.name}'s HEAD ({head[:12]}) is not what the lock deploys "
                f"({(deployed[0] or 'nothing')[:12]}" + (f" on {deployed[1]}" if deployed[1] else "")
                + "): deploy it first")
        merged = merge_commit(ax.name, ax.repo, main, head, f"landing of {wb}")
        if merged is None:
            die(f"annex {ax.name}'s {wb} conflicts with origin/{default}: rebase it there "
                f"(in {ax.dir}), deploy, then re-run")
        tree = tree_less_msg(merged, cwd=str(ax.dir))
        try:
            msg = (ax.dir / MSG_FILE).read_text(encoding="utf-8").strip() or fallback_msg
        except OSError:
            msg = fallback_msg
        lines = msg.splitlines()
        if len(lines) > 1 and lines[1].strip():
            msg = "\n".join([lines[0], "", *lines[1:]])
        if tree == out(["git", "rev-parse", f"{main}^{{tree}}"], cwd=str(ax.dir)):
            landed = main
        else:
            landed = out(["git", "commit-tree", tree, "-p", main, "-m", msg], cwd=str(ax.dir))
            p = git("push", "--quiet", "origin", f"{landed}:refs/heads/{default}",
                    cwd=str(ax.dir), check=False)
            if p.returncode != 0:
                die(f"pushing annex {ax.name}'s {default} failed (moved since fetch?); "
                    f"re-run:\n{(p.stderr or '').strip()}")
            print(f"annex {ax.name}: landed on {default} ({landed[:12]})")
        # The worktree onto the landing; its tree is clean of edits.
        if git("reset", "--quiet", "--keep", landed, cwd=str(ax.dir), check=False).returncode != 0:
            git("reset", "--quiet", "--hard", landed, cwd=str(ax.dir))
        (ax.dir / MSG_FILE).unlink(missing_ok=True)
        push_branch(ax, wb)
    # Relocked on the default branch's name; one never locked on the
    # branch (nothing of its own) keeps the lock it had.
    if (landed != head or (on_wb is not None and locked_rev(ax.name)[1] == on_wb)
            or lock_off_default(ax.name, ax.repo)) and relock(ax.name, default, landed):
        git("commit", "--quiet", "-m", f"annex {ax.name}: {lock_path()} locks {default}")
    prune_past_refs(ax.name, ax.repo)


def push_branch(ax: Annex, wb: str) -> None:
    """The annex branch's tip onto its origin, leased on the tip fetched; a
    tip it leaves behind (a reset, a rebase) stays reachable under
    PAST_REFS, since the deploy records lock revs of it."""
    head = rev_in(ax.dir, "HEAD")
    old = rev_in(ax.dir, f"refs/remotes/origin/{wb}")
    if head is None or old == head:
        return
    past = ([f"+{old}:{PAST_REFS}/{wb}/{old}", stamp_refspec(ax.repo, old)]
            if old and not is_ancestor(old, head, cwd=str(ax.dir)) else [])
    p = git("push", "--quiet", "--atomic", "origin", f"+{head}:refs/heads/{wb}", *past,
            f"--force-with-lease=refs/heads/{wb}:{old or ''}", cwd=str(ax.dir), check=False)
    if p.returncode != 0:
        die(f"pushing annex {ax.name}'s {wb} failed - it moved on its origin, but this annex is "
            f"its only writer.  Investigate:\n{(p.stderr or '').strip()}")


def reset_all(top: Path, wb: str) -> None:
    """An abandon's annexes: each hard-reset onto its repository's default
    branch (untracked files survive), the branch pushed so; the checkout's
    own reset restores their lock nodes."""
    for ax in present(top):
        reset_one(ax, wb)


def reset_one(ax: Annex, wb: str) -> None:
    cur = branch_at(ax.dir)
    if cur != wb:
        die(f"annex {ax.name} ({ax.dir}) is on {cur or 'a detached HEAD'}, not {wb}")
    default = default_branch(ax.repo)
    main = rev_in(ax.dir, f"refs/remotes/origin/{default}")
    prev = rev_in(ax.dir, "HEAD") or ""
    assert main is not None
    git("reset", "--quiet", "--hard", main, cwd=str(ax.dir))
    (ax.dir / MSG_FILE).unlink(missing_ok=True)
    if rev_in(ax.dir, f"refs/remotes/origin/{wb}") is not None:
        push_branch(ax, wb)
    print(f"annex {ax.name}: {wb} reset onto origin/{default}" + (
        f" (was {prev[:12]}, see its reflog)" if prev != main else ""))


def remove_all(top: Path) -> None:
    """Remove `top`'s annex worktrees with their branches, here and on
    origin (a subagent worktree's, going with it)."""
    for ax in present(top):
        remove_one(ax)


def keeps(ax: Annex, wb: str | None) -> str | None:
    """Why `close --annex` keeps the annex: what its removal would lose."""
    if out(["git", "status", "--porcelain"], cwd=str(ax.dir)):
        return "it has uncommitted or untracked changes"
    default = default_branch_of(ax.repo)
    if default is None:
        return "its repository's origin has no default branch to judge it against"
    if not ok(["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{default}"],
              cwd=str(ax.dir)):
        return f"it carries commits not on origin/{default}; land or abandon it (--annex {ax.name})"
    if wb and locked_rev(ax.name)[1] == f"refs/heads/{wb}":
        return f"the lock still names its branch {wb}; land or abandon it (--annex {ax.name})"
    if lock_off_default(ax.name, ax.repo):
        return f"the lock still deploys a rev not on origin/{default}; land or abandon it (--annex {ax.name})"
    return None


def lock_off_default(name: str, repo: Path) -> bool:
    """Whether a github input's lock (naming no branch) deploys a rev its
    repository's origin default branch lacks: a work branch's."""
    if original_of(name).get("type") != "github" or not (repo / ".git").exists():
        return False
    rev, default = locked_rev(name)[0], default_branch_of(repo)
    return bool(rev and default and rev_in(repo, f"{rev}^{{commit}}")
                and not is_ancestor(rev, f"refs/remotes/origin/{default}", cwd=str(repo)))


def remove_one(ax: Annex) -> None:
    """The annex worktree removed with its branch, here and on origin."""
    cur = branch_at(ax.dir)
    git("worktree", "remove", "--force", str(ax.dir), cwd=str(ax.repo), check=False)
    shutil.rmtree(ax.dir, ignore_errors=True)
    git("worktree", "prune", cwd=str(ax.repo), check=False)
    if cur is not None:
        git("branch", "--quiet", "-D", cur, cwd=str(ax.repo), check=False)
        old = rev_in(ax.repo, f"refs/remotes/origin/{cur}")
        if old:
            # The tip stays reachable under PAST_REFS: the deploy records and
            # the branch's history lock revs of it.
            p = git("push", "--quiet", "--atomic", "origin", f"+{old}:{PAST_REFS}/{cur}/{old}", stamp_refspec(ax.repo, old),
                    f":refs/heads/{cur}",
                    f"--force-with-lease=refs/heads/{cur}:{old}", cwd=str(ax.repo), check=False)
            if p.returncode != 0:
                print(f"warning: deleting annex {ax.name}'s {cur} on its origin failed:\n"
                      f"{(p.stderr or '').strip()}", file=sys.stderr)
    print(f"annex {ax.name}: worktree removed" + (f" with {cur}" if cur else ""))
    with_dir = ax.dir.parent
    if with_dir.is_dir() and not any(with_dir.iterdir()):
        with_dir.rmdir()


def restore_lock(ax: Annex) -> bool:
    """The annex's lock node put back to what origin/main locks, staged -
    the next deploy reverts it on the targets.  Whether the node moved."""
    p = run(["git", "show", f"refs/remotes/origin/main:{lock_path()}"], check=False)
    if p.returncode != 0:
        die(f"origin/main has no {lock_path()} to restore {ax.name}'s lock from")
    try:
        was = load_json(p.stdout)
    except ValueError as e:
        die(f"origin/main's {lock_path()}: {e}")
    if not isinstance(was, dict):
        die(f"origin/main's {lock_path()}: not a lock file")
    locked = lock_node(was, ax.name).get("locked")
    before = index_lock()
    lock = parse_lock(before, f"{lock_path()} (index)")
    node = lock_node(lock, ax.name)
    if node.get("locked") == locked:
        return False
    original = node.get("original")
    if not (isinstance(locked, dict) and isinstance(locked.get("rev"), str) and isinstance(original, dict)):
        die(f"origin/main's {lock_path()}: {ax.name} locks no rev")
    # Through nix, so the nodes of the annex's own inputs follow too.
    ref = locked.get("ref") if isinstance(locked.get("ref"), str) else f"refs/heads/{default_branch(ax.repo)}"
    got = lock_with(ax.name, flakeref(original, str(ref), str(locked["rev"])), lock, flake_text())
    if got is None:
        die(f"annex {ax.name}: restoring origin/main's lock failed")
    write_lock(got, before)
    print(f"annex {ax.name}: {lock_path()} restored to origin/main's, staged (deploy to revert it "
          f"on the targets)")
    return True


def orphaned(top: Path, wb: str) -> list[str]:
    """The configured annexes whose lock node names `wb` while no worktree
    of theirs is under `top`: a landing must not take that branch to main."""
    here = {ax.name for ax in present(top)}
    return [name for name, repo in configured().items() if name not in here
            and (locked_rev(name)[1] == f"refs/heads/{wb}" or lock_off_default(name, repo))]


def own(top: Path) -> bool:
    """Whether an annex under `top` holds anything of its own: edits, or
    commits its origin's default branch lacks."""
    for ax in present(top):
        if out(["git", "status", "--porcelain"], cwd=str(ax.dir)):
            return True
        default = default_branch_of(ax.repo)
        if default is None or not ok(["git", "merge-base", "--is-ancestor", "HEAD",
                                      f"refs/remotes/origin/{default}"], cwd=str(ax.dir)):
            return True
    return False


def notes(d: Path) -> list[str]:
    """Status notes for the annexes under the checkout `d`."""
    found = []
    for ax in present(d):
        default = default_branch_of(ax.repo)
        ahead = (out(["git", "rev-list", "--count", "HEAD", "--not", f"refs/remotes/origin/{default}"],
                     cwd=str(ax.dir)) or "0") if default else "?"
        n_staged = len(out(["git", "diff", "--cached", "--name-only"], cwd=str(ax.dir)).splitlines())
        n_unstaged = len(out(["git", "diff", "--name-only"], cwd=str(ax.dir)).splitlines())
        n_untracked = len(out(["git", "ls-files", "--others", "--exclude-standard"],
                              cwd=str(ax.dir)).splitlines())
        parts = [f"annex {ax.name}"]
        if ahead != "0":
            parts.append(f"+{ahead}")
        if n_staged:
            parts.append(f"staged {n_staged}")
        if n_unstaged:
            parts.append(f"unstaged {n_unstaged}")
        if n_untracked:
            parts.append(f"untracked {n_untracked}")
        cur = branch_at(ax.dir)
        if cur is None:
            parts.append("detached!")
        found.append(" ".join(parts))
    return found


def repo_notes() -> list[str]:
    """The configured annex repositories' own checkouts holding edits,
    which no deploy picks up."""
    found = []
    for name, repo in configured().items():
        if not repo.is_dir():
            continue
        dirty = run(["git", "status", "--porcelain", "-uno"], cwd=str(repo), check=False).stdout
        if dirty.strip():
            n = len(dirty.strip().splitlines())
            found.append(f"{repo} ({name}): {n} edited file{'s' if n > 1 else ''} - no deploy "
                         f"picks these up; `nixconvoy open --annex {name}` is where {name} is worked on")
    return found
