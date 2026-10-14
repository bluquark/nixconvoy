"""Annexes: the flake inputs a checkout develops in place.  An annex is a
git worktree of the input's repository under <checkout>/annexes/<name>,
on a branch named after the checkout's work branch.  A deploy commits the
annex's staged state onto that branch, pushes it to the input's origin
(what the lock fetches) and points the flake.lock node at the pushed rev,
so the increment carries the annex as a lock change, which the union, the
eval memo and the lineage handle as any other.  `land` lands the annex
branch on its repository's default branch first and relocks the node
there; `abandon` discards it.  The annex worktree is removed with its
branch either way.  Only the git side lives here: the lock's narHash
comes from `nix flake prefetch` (prefetch, replaced by the tests)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

from .checkout import tree_less_msg
from .config import MSG_FILE, cfg
from .proc import die, git, load_json, ok, out, run

ANNEX_DIR = "annexes"
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


def default_branch(repo: Path) -> str:
    """The annex repository's default branch (origin/HEAD, else main or master)."""
    head = run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
               cwd=str(repo), check=False).stdout.strip()
    if head.startswith("origin/"):
        return head[len("origin/"):]
    for b in ("main", "master"):
        if ok(["git", "rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}"], cwd=str(repo)):
            return b
    return die(f"{repo}: origin has neither a HEAD, main nor master branch")


def rev_in(d: Path, ref: str) -> str | None:
    p = run(["git", "rev-parse", "-q", "--verify", ref], cwd=str(d), check=False)
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def index_tree(d: Path) -> str:
    """The tree of `d`'s index, through a copy (write-tree on the real one
    would rewrite its cache-tree extension)."""
    fd, tmp = tempfile.mkstemp()
    os.close(fd)
    try:
        shutil.copy(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
                        cwd=str(d)), tmp)
        p = run(["git", "write-tree"], env={"GIT_INDEX_FILE": tmp}, cwd=str(d), check=False)
        if p.returncode != 0:
            die(f"cannot snapshot the index of {d} (unmerged paths?)")
        return p.stdout.strip()
    finally:
        Path(tmp).unlink()


def staged(d: Path) -> bool:
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


def load_lock() -> dict[str, object]:
    try:
        doc = load_json(lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        die(f"{lock_path()}: {e}")
    if not isinstance(doc, dict):
        die(f"{lock_path()}: not a lock file")
    return doc


def lock_node(lock: dict[str, object], name: str) -> dict[str, object]:
    """The node of the root input `name`; its locked/original tables are
    the node's own."""
    nodes, root = lock.get("nodes"), lock.get("root")
    if not isinstance(nodes, dict) or not isinstance(root, str) or not isinstance(nodes.get(root), dict):
        die(f"{lock_path()}: malformed")
    inputs = nodes[root].get("inputs")
    key = inputs.get(name) if isinstance(inputs, dict) else None
    if not isinstance(key, str) or not isinstance(nodes.get(key), dict):
        die(f"{name} is not a flake input of {cfg().flake_file('flake.nix')} (as {lock_path()} "
            f"locks it); annexes name root inputs")
    return nodes[key]


def flakeref(original: dict[str, object], branch: str, sha: str) -> str:
    """The flake reference fetching `sha` of the input as `original` names
    it, on `branch`: what the lock node will lock."""
    kind = original.get("type")
    if kind == "git":
        params = {"ref": f"refs/heads/{branch}", "rev": sha}
        for flag in ("shallow", "submodules"):
            if original.get(flag):
                params[flag] = "1"
        return f"git+{original.get('url')}?{urlencode(params, safe='/')}"
    if kind == "github":
        return f"github:{original.get('owner')}/{original.get('repo')}/{sha}"
    return die(f"an annex input must be a git or github input, not {kind!r}")


def prefetch(ref: str) -> dict[str, object]:
    """`nix flake prefetch --json`'s locked table for `ref`."""
    p = run(["nix", "flake", "prefetch", "--json", ref], check=False, timeout=300)
    if p.returncode != 0:
        die(f"nix flake prefetch {ref} failed:\n{(p.stderr or '').strip()}")
    doc = load_json(p.stdout)
    locked = doc.get("locked") if isinstance(doc, dict) else None
    if not isinstance(locked, dict) or not isinstance(locked.get("narHash"), str):
        die(f"nix flake prefetch {ref}: no locked table in its output")
    return locked


def relock(name: str, branch: str, sha: str) -> bool:
    """Point `name`'s lock node at `sha` on `branch`, staged: whether it
    moved."""
    lock = load_lock()
    node = lock_node(lock, name)
    locked, original = node.get("locked"), node.get("original")
    if not isinstance(original, dict):
        die(f"{lock_path()}: {name} has no original table")
    want_ref = f"refs/heads/{branch}" if original.get("type") == "git" else None
    if isinstance(locked, dict) and locked.get("rev") == sha and \
            (want_ref is None or locked.get("ref") == want_ref):
        return False
    node["locked"] = prefetch(flakeref(original, branch, sha))
    path = lock_path()
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    git("add", "--", str(path))
    return True


def locked_rev(name: str) -> tuple[str | None, str | None]:
    """(rev, ref) `name`'s lock node locks now."""
    locked = lock_node(load_lock(), name).get("locked")
    if not isinstance(locked, dict):
        return None, None
    r, b = locked.get("rev"), locked.get("ref")
    return (r if isinstance(r, str) else None), (b if isinstance(b, str) else None)


# --- the verbs -------------------------------------------------------------

def create(name: str, wb: str) -> Path:
    """`nixconvoy annex <name>`: the annex worktree under this checkout, on
    `wb` - origin's copy of the branch when it has one (a worktree
    re-created), else a new branch off the repository's default."""
    repos = configured()
    if name not in repos:
        die(f"{name} is not a configured annex" + (
            f" (they are: {', '.join(sorted(repos))})" if repos else
            " (nixconvoy.toml: [annexes.<input>] repo = \"...\")"))
    repo = repos[name]
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
    if rev_in(repo, f"refs/remotes/origin/{wb}") is not None:
        git("branch", "--quiet", "-f", wb, f"refs/remotes/origin/{wb}", cwd=str(repo), check=False)
        git("worktree", "add", "--quiet", str(d), wb, cwd=str(repo))
        where = f"origin/{wb}"
    elif rev_in(repo, f"refs/heads/{wb}") is not None:
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


def sync(wb: str | None, msg: str | None) -> list[str]:
    """A deploy's (or commit's) annex step: each present annex's staged
    state committed onto `wb` with `msg`, pushed, and its lock node relocked
    on it, staged here.  What changed, one line each."""
    top = Path.cwd()
    found = present(top)
    if not found:
        return []
    if wb is None:
        die(f"{ANNEX_DIR}/ in the main checkout, which has no work branch to carry an annex: "
            f"remove it, or work in a checkout on a work branch")
    told = []
    for ax in found:
        cur = branch_at(ax.dir)
        if cur != wb:
            die(f"annex {ax.name} ({ax.dir}) is on {cur or 'a detached HEAD'}, not {wb}: "
                f"`git -C {ax.dir} switch {wb}`")
        mark(ax.repo)
        if out(["git", "diff", "--name-only"], cwd=str(ax.dir)):
            print(f"note: annex {ax.name} has unstaged edits, which are not deployed", file=sys.stderr)
        if staged(ax.dir):
            if not msg:
                die(f'annex {ax.name} has staged changes; describe this increment with -m "..."')
            git("commit", "--quiet", "-m", msg, cwd=str(ax.dir))
        head = rev_in(ax.dir, "HEAD")
        assert head is not None
        # Nothing of its own yet: the lock stays as it was (the branch is
        # off the default branch's tip, maybe past the locked rev).
        if ok(["git", "merge-base", "--is-ancestor", head,
               f"refs/remotes/origin/{default_branch(ax.repo)}"], cwd=str(ax.dir)):
            continue
        if rev_in(ax.dir, f"refs/remotes/origin/{wb}") != head:
            old = rev_in(ax.dir, f"refs/remotes/origin/{wb}") or ""
            p = git("push", "--quiet", "origin", f"{wb}:refs/heads/{wb}",
                    f"--force-with-lease=refs/heads/{wb}:{old}", cwd=str(ax.dir), check=False)
            if p.returncode != 0:
                die(f"pushing annex {ax.name}'s {wb} failed - it moved on its origin, but this "
                    f"annex is its only writer.  Investigate:\n{(p.stderr or '').strip()}")
            told.append(f"annex {ax.name}: pushed {wb} ({head[:12]})")
        if relock(ax.name, wb, head):
            told.append(f"annex {ax.name}: {lock_path()} locks {head[:12]} on {wb}")
    return told


def land_all(wb: str, fallback_msg: str) -> None:
    """Each present annex landed on its repository's default branch as one
    commit (its own MSG_FILE's message, else `fallback_msg`) and its lock
    node relocked there, as a commit on `wb`; run by land before main's
    commit, so a landing retried finds nothing left to do here."""
    top = Path.cwd()
    for ax in present(top):
        cur = branch_at(ax.dir)
        if cur != wb:
            die(f"annex {ax.name} ({ax.dir}) is on {cur or 'a detached HEAD'}, not {wb}")
        if staged(ax.dir):
            die(f"annex {ax.name} has staged but undeployed changes; deploy or unstage them first")
        p = run(["git", "fetch", "--quiet", "origin"], cwd=str(ax.dir), check=False, timeout=120)
        if p.returncode != 0:
            die(f"annex {ax.name}: fetching its origin failed:\n{(p.stderr or '').strip()}")
        default = default_branch(ax.repo)
        main = rev_in(ax.dir, f"refs/remotes/origin/{default}")
        head = rev_in(ax.dir, "HEAD")
        assert main is not None and head is not None
        # The branch's own commits squashed onto the default branch; one
        # with none keeps its rev, relocked under the default branch's name.
        landed = head
        if not ok(["git", "merge-base", "--is-ancestor", head, main], cwd=str(ax.dir)):
            p = git("rebase", "--quiet", f"origin/{default}", cwd=str(ax.dir), check=False)
            if p.returncode != 0:
                git("rebase", "--abort", cwd=str(ax.dir), check=False)
                die(f"rebasing annex {ax.name}'s {wb} onto origin/{default} is not clean; "
                    f"resolve it in {ax.dir} and re-run:\n{(p.stderr or '').strip()}")
            tree = tree_less_msg("HEAD", cwd=str(ax.dir))
            msg = (out(["git", "show", f"HEAD:{MSG_FILE}"], cwd=str(ax.dir))
                   if rev_in(ax.dir, f"HEAD:{MSG_FILE}") else "") or fallback_msg
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
            git("reset", "--quiet", landed, cwd=str(ax.dir))
            (ax.dir / MSG_FILE).unlink(missing_ok=True)
        # Relocked on the default branch's name; one never locked on the
        # branch (nothing of its own) keeps the lock it had.
        if (landed != head or locked_rev(ax.name)[1] == f"refs/heads/{wb}") and \
                relock(ax.name, default, landed):
            git("commit", "--quiet", "-m", f"annex {ax.name}: {lock_path()} locks {default}")


def remove_all(top: Path, *, keep_origin: bool = False) -> None:
    """Remove `top`'s annex worktrees with their branches, here and on
    origin (`keep_origin`: origin's copy stays)."""
    for ax in present(top):
        cur = branch_at(ax.dir)
        git("worktree", "remove", "--force", str(ax.dir), cwd=str(ax.repo), check=False)
        shutil.rmtree(ax.dir, ignore_errors=True)
        git("worktree", "prune", cwd=str(ax.repo), check=False)
        if cur is None:
            continue
        git("branch", "--quiet", "-D", cur, cwd=str(ax.repo), check=False)
        old = rev_in(ax.repo, f"refs/remotes/origin/{cur}")
        if old and not keep_origin:
            p = git("push", "--quiet", "origin", f":refs/heads/{cur}",
                    f"--force-with-lease=refs/heads/{cur}:{old}", cwd=str(ax.repo), check=False)
            if p.returncode != 0:
                print(f"warning: deleting annex {ax.name}'s {cur} on its origin failed:\n"
                      f"{(p.stderr or '').strip()}", file=sys.stderr)
        print(f"annex {ax.name}: worktree removed with {cur}")
    with_dir = top / ANNEX_DIR
    if with_dir.is_dir() and not any(with_dir.iterdir()):
        with_dir.rmdir()


def own(top: Path) -> bool:
    """Whether an annex under `top` holds anything of its own: edits, or
    commits its origin's default branch lacks."""
    for ax in present(top):
        if out(["git", "status", "--porcelain"], cwd=str(ax.dir)):
            return True
        default = default_branch(ax.repo)
        if not ok(["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{default}"],
                  cwd=str(ax.dir)):
            return True
    return False


def notes(d: Path) -> list[str]:
    """Status notes for the annexes under the checkout `d`."""
    found = []
    for ax in present(d):
        default = default_branch(ax.repo)
        ahead = out(["git", "rev-list", "--count", "HEAD", "--not", f"refs/remotes/origin/{default}"],
                    cwd=str(ax.dir)) or "0"
        n_staged = len(out(["git", "diff", "--cached", "--name-only"], cwd=str(ax.dir)).splitlines())
        n_unstaged = len(out(["git", "diff", "--name-only"], cwd=str(ax.dir)).splitlines())
        parts = [f"annex {ax.name}"]
        if ahead != "0":
            parts.append(f"+{ahead}")
        if n_staged:
            parts.append(f"staged {n_staged}")
        if n_unstaged:
            parts.append(f"unstaged {n_unstaged}")
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
                         f"picks these up; `nixconvoy annex {name}` is where {name} is worked on")
    return found
