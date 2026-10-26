"""The eval memo: each target's toplevel (and the switch policy its
configuration declares) keyed by exactly what its evaluation can read, so a
change to a module one target imports re-evaluates that target alone and a
no-op redeploy evaluates nothing; the precheck's pass memoized on the
union's tree."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Collection, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from .config import cfg, root
from .proc import NOTHING_DONE, cache_dir, die, dump_json, is_ancestor, load_json, out, run

# A target entry lives while its .drv does: unrooted once no checkout's
# last deploy used it (memo_prune), the store's collector decides.
# Registering a root takes well under 0.1 s; a daemon that keeps it
# waiting is stuck, and the deploy says so.
ROOT_TIMEOUT = 5
# A file being written (<name>.tmp<pid>, or nix's <link>.tmp-<pid>-<rand>
# under roots/, each renamed into place within milliseconds) is garbage
# past this age: its writer died.
TMP_MAX_AGE = 60
# The flock excludes this process's own threads too; this lock in front
# keeps the wait message for another process.
MEMO_LOCK = threading.Lock()


class Top(NamedTuple):
    """A target's evaluated toplevel and the switch policy its configuration
    declares through the nixconvoy NixOS module."""
    out: str
    drv: str
    # Switched after every other target, alone, with this machine's internet
    # verified too (the router).
    last: bool
    # systemd-boot boot counting (the boot-assessment module): a generation
    # the target fell back from is never switched to.
    assessed: bool


def memo_entry(obj: object) -> Top | None:
    """A Top from a memo file's or nix eval's attrset."""
    if not isinstance(obj, dict):
        return None
    o, drv = obj.get("out"), obj.get("drv")
    if not (isinstance(o, str) and isinstance(drv, str)):
        return None
    return Top(o, drv, obj.get("last") is True, obj.get("assessed") is True)


def memo_read(path: Path) -> Top | None:
    try:
        return memo_entry(load_json(path.read_text()))
    except (OSError, ValueError):
        return None


def memo_write(path: Path, ent: Top) -> None:
    """Atomic rename, so concurrent deploys never see a partial entry."""
    try:
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(dump_json({"out": ent.out, "drv": ent.drv,
                                  "last": ent.last, "assessed": ent.assessed}))
        tmp.replace(path)
    except OSError:
        pass


def memo_root(path: Path) -> Path:
    """A target entry's GC root: a symlink to its .drv under roots/ beside
    the entries, registered with the store as an indirect root."""
    return path.parent / "roots" / f"{path.stem}.drv"


def root_drv(path: Path, drv: str) -> bool:
    """Whether `path`'s entry's .drv is rooted, registering the root when
    it is not: nix-instantiate's --add-root on the imported .drv (no
    build), through the daemon, so on return the .drv closure is kept
    whole or gone - gone is False, the entry stale.  A root already
    pointing at a .drv still there holds without asking the daemon.  Any
    other failure is the deploy's."""
    link = memo_root(path)
    try:
        if str(link.readlink()) == drv and Path(drv).exists():
            return True
    except OSError:
        pass
    # Only a registration that returned 0 leaves a link: one nix-instantiate
    # made but did not register (killed, interrupted, the daemon gone) would
    # be trusted by the fast path while rooting nothing.  A link found
    # holding another .drv (the key missed something) is left as it was.
    prior = None
    with contextlib.suppress(OSError):
        prior = link.readlink() if Path(str(link.readlink())).exists() else None
    rooted = False
    try:
        link.parent.mkdir(exist_ok=True)
        p = run(["nix-instantiate", "--add-root", str(link), "--indirect",
                 "-E", f'import "{drv}"'], check=False, timeout=ROOT_TIMEOUT)
        rooted = p.returncode == 0 and Path(drv).exists()
    except FileNotFoundError:
        die("nix-instantiate is not on PATH: a memoized .drv cannot be rooted")
    finally:
        if not rooted:
            with contextlib.suppress(OSError):
                if prior is None or link.readlink() != prior:
                    link.unlink()
    if p.returncode == -signal.SIGKILL:
        die(f"rooting {drv} timed out after {ROOT_TIMEOUT} s: is the daemon stuck?")
    if not Path(drv).exists():
        return False
    if p.returncode != 0:
        die(f"rooting {drv} failed:\n{p.stderr.strip()}")
    return True


@contextlib.contextmanager
def memo_lock(d: Path) -> Iterator[None]:
    """The memo's lock, held by a prune, by a deploy from its record
    (memo_use) through the hits it trusts, and around each fresh
    evaluation's rooting: a prune reads the records once, so one running
    beside a deploy could unroot an entry recorded and trusted after its
    read, and two deploys rooting one entry would unlink each other's
    link.  The lock file sits beside the memo dir, which holds entries,
    records and roots alone."""
    with MEMO_LOCK, d.with_name(f"{d.name}.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("waiting for another deploy's turn at the eval memo...", flush=True)
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def memo_use(d: Path, names: Collection[str]) -> None:
    """Record this checkout's use of the entries `names` under users/,
    replacing its last record: memo_prune keeps an entry rooted while some
    checkout's record names it.  Written under memo_lock before the
    entries are read, so a prune keeps what the deploy is about to use."""
    here = root().resolve()
    users = d / "users"
    try:
        users.mkdir(exist_ok=True)
        rec = users / f"{quote(str(here), safe='')}.json"
        tmp = rec.with_name(f"{rec.name}.tmp{os.getpid()}")
        tmp.write_text(dump_json({"checkout": str(here), "entries": sorted(names)}))
        tmp.replace(rec)
    except OSError:
        pass


def memo_used(d: Path) -> set[str]:
    """The entries the checkouts' records name, dropping the record of a
    checkout gone or unreadable."""
    used: set[str] = set()
    users = d / "users"
    for rec in (users.iterdir() if users.is_dir() else ()):
        if rec.suffix != ".json":
            continue
        try:
            doc = load_json(rec.read_text())
            co = doc.get("checkout") if isinstance(doc, dict) else None
            names = doc.get("entries") if isinstance(doc, dict) else None
            if isinstance(co, str) and isinstance(names, list) and Path(co).is_dir():
                used.update(n for n in names if isinstance(n, str))
            else:
                rec.unlink()
        except (OSError, ValueError):
            pass
    return used


def memo_dir() -> Path:
    """One JSON file per (target, key) under the XDG cache, shared by every
    checkout on the machine (keys are content-addressed), so checkouts
    alternating different trees do not evict each other.  Per-entry files
    written by atomic rename make concurrent deploys safe."""
    d = cache_dir() / "eval-memo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def memo_prune(d: Path) -> None:
    """Drop a target entry whose .drv is gone, and the root of an entry
    gone or of one no checkout's record names (memo_use): an unrooted
    entry stays a hint until the store's collector takes its .drv.  A
    precheck entry proves a tree and is never rooted (one with a root
    loses it).  The entries and records are the *.json files; what else
    the dirs hold is another deploy's file being written, or nix's
    temporary link under roots/, left alone until TMP_MAX_AGE says its
    writer died.  A root is kept while a record names it, entry or no
    entry.  Under memo_lock."""
    used = memo_used(d)
    roots = d / "roots"
    for path in d.iterdir():
        if path.is_dir():
            continue
        if path.suffix != ".json":
            drop_stale_tmp(path)
            continue
        ent = memo_read(path)
        try:
            if path.name.startswith("check-"):
                memo_root(path).unlink(missing_ok=True)
                if ent is None:
                    path.unlink()
            elif ent is None or not Path(ent.drv).exists():
                path.unlink()
                memo_root(path).unlink(missing_ok=True)
            elif path.stem not in used:
                memo_root(path).unlink(missing_ok=True)
        except OSError:
            pass
    for stray in ((d / "users").iterdir() if (d / "users").is_dir() else ()):
        if stray.suffix != ".json":
            drop_stale_tmp(stray)
    for link in (roots.iterdir() if roots.is_dir() else ()):
        try:
            if link.suffix != ".drv":
                drop_stale_tmp(link)
            elif link.stem not in used and not (d / f"{link.stem}.json").exists():
                link.unlink()
        except OSError:
            pass


def drop_stale_tmp(path: Path) -> None:
    """Drop `path`, a file being written, once it is TMP_MAX_AGE old."""
    try:
        if time.time() - path.lstat().st_mtime > TMP_MAX_AGE:
            path.unlink()
    except OSError:
        pass


# A Nix path literal inside a .nix file: ./x, ../files/x.py, ../lib (a
# directory).  The lookbehind keeps "foo/../bar" fragments inside strings
# and a relative input's "path:./x" url (path_inputs decides who reads
# that one) from matching; a stray shell "../" in a string widens the key,
# one climbing to the repository root to the whole tree (said on stderr,
# naming the file).
PATH_LITERAL = re.compile(r"(?<![\w/.\-:])(\.\.?/[\w.+\-/]*)")
WORD = re.compile(r"[A-Za-z][\w\-]*")
# flake.nix's outputs argument list and mkHost's specialArgs literal name
# every input; what remains of flake.nix names the inputs it uses itself.
OUTPUTS_ARGS = re.compile(r"outputs\s*=\s*\{[^{}]*\}\s*:")
SPECIAL_ARGS = re.compile(r"specialArgs\s*=\s*\{[^{}]*\}")


@functools.cache
def union_objects(union: str) -> dict[str, tuple[str, str]]:
    """path -> (type, git object id) for every blob and tree in the union
    commit: the whole repo, which is the flake's source."""
    objs: dict[str, tuple[str, str]] = {}
    for line in out(["git", "ls-tree", "-r", "-t", union]).splitlines():
        meta, path = line.split("\t", 1)
        _mode, typ, oid = meta.split()
        objs[path] = (typ, oid)
    return objs


@functools.cache
def scan_sound(wt: str) -> bool:
    """Whether the read scan can be trusted: flake.nix's specialArgs is
    `{ inherit <names>; }` (said once, not per target, when it is not): an
    alias would hand a module an input under a name the scan cannot tie
    back to the lock, `self` or `inputs` the whole tree."""
    flake = (Path(wt) / cfg().flake_file("flake.nix")).read_text()
    for m in re.finditer(r"specialArgs\s*=\s*(\{[^{}]*\}|[^;]*);", flake):
        if not (re.fullmatch(r"\{\s*inherit(\s+[\w\-]+)+\s*;\s*\}", m.group(1))
                and not re.search(r"\b(self|inputs)\b", m.group(1))):
            print("eval memo: flake.nix's specialArgs is not an inherit list; "
                  "keying every target on the whole tree", file=sys.stderr)
            return False
    return True


def lock_part(lock: object, named: Collection[str]) -> str:
    """flake.lock's share of a target's key: the lock graph pruned to the root
    inputs in `named` and what they reach (follows paths resolved), hashed.
    A flake input reaches a target only by name, a module argument out of
    specialArgs or a use in flake.nix's outputs body, so a bump of an input
    the target's files never name leaves its key alone.  Anything but a
    well-formed lock (a node or follows target missing) hashes whole."""
    whole = hashlib.sha256(dump_json(lock).encode()).hexdigest()
    if not (isinstance(lock, dict) and isinstance(lock.get("nodes"), dict)
            and isinstance(lock.get("root"), str) and lock["root"] in lock["nodes"]):
        return whole
    nodes: dict[str, object] = lock["nodes"]
    root: str = lock["root"]

    def inputs(key: str) -> dict[str, object]:
        node = nodes.get(key)
        if not isinstance(node, dict):
            raise KeyError(key)
        ins = node.get("inputs")
        return ins if isinstance(ins, dict) else {}

    def resolve(ref: object) -> str:
        if isinstance(ref, str):
            return ref
        if not isinstance(ref, list):
            raise KeyError(str(ref))
        key = root
        for name in ref:
            key = resolve(inputs(key)[name])
        return key

    try:
        kept = {name: ref for name, ref in inputs(root).items() if name in named}
        keep = {root}
        todo = [resolve(ref) for ref in kept.values()]
        while todo:
            key = todo.pop()
            if key in keep:
                continue
            keep.add(key)
            todo.extend(resolve(ref) for ref in inputs(key).values())
        pruned = {k: nodes[k] for k in sorted(keep)}
    except KeyError:
        return whole
    root_node = nodes[root]
    pruned[root] = ({**root_node, "inputs": kept} if isinstance(root_node, dict)
                    else root_node)
    return hashlib.sha256(dump_json(pruned).encode()).hexdigest()


@functools.cache
def file_facts(wt: str, f: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """What the .nix file `f` of the tree checked out in `wt` contributes to
    target_eval_reads, the same whichever target asks: the words it names
    and the repo paths its path literals name (a directory named counts
    whole, `.` being the repository; one outside it skipped, as is, in
    flake.nix, the precheck's `<check> = import <path> { ... };`
    stanza, which no target's evaluation reads).  Cached, since the
    targets' scans share most files."""
    c = cfg()
    flake_nix = c.flake_file("flake.nix")
    text = (Path(wt) / f).read_text()
    # Comments left in: over-naming costs an eval, a `#` in a string
    # hiding a name would serve a stale toplevel.
    skipped: set[str] = set()
    scan = text
    if f == flake_nix:
        m = OUTPUTS_ARGS.search(text)
        words = SPECIAL_ARGS.sub("", text[m.end():] if m else text)
        if c.precheck:
            # `checks.<system>.<check> = import <path> { ... };`, or a bare
            # `<check> = import <path>` binding when none is written so: the
            # whole stanza is dropped where its braces are flat, else the
            # import alone.
            leaf = rf"{re.escape(c.precheck)}\s*=\s*import\s+(\.\.?/[\w.+\-/]*)"
            stanza = next((p for p in (rf"checks\.[^\s=]*\.{leaf}", rf"(?<![\w\-.]){leaf}")
                           if re.search(p, text)), None)
            if stanza:
                for m in re.finditer(stanza, text):
                    skipped.add(os.path.normpath(Path(f).parent / m.group(1)))
                scan = re.sub(stanza + r"\s*\{[^{}]*\}\s*;", "", text)
                words = re.sub(stanza + r"\s*\{[^{}]*\}\s*;", "", words)
        named = frozenset(WORD.findall(words))
    else:
        named = frozenset(WORD.findall(text))
    refs: list[str] = []
    for m in PATH_LITERAL.finditer(scan):
        ref = os.path.normpath(Path(f).parent / m.group(1))
        if ref.startswith("..") or Path(ref).is_absolute() or ref in skipped:
            continue
        refs.append(ref)
    return named, tuple(refs)


@functools.cache
def target_eval_reads(wt: str, union: str, target: str) -> tuple[frozenset[str], frozenset[str]] | None:
    """What `target`'s evaluation can read in the `union` commit checked out
    in `wt`: the repo paths - flake.nix, the target's directory, transitively
    every path any of those .nix files names (a named directory counts
    whole) - and the flake input names those files mention (lock_part's
    pruning).  In flake.nix the other targets' directories are skipped.  The
    scan is lexical (PATH_LITERAL, file_facts), sound for a path built by
    concatenation as long as its directory is named literally.  No scan can
    follow a module reading another configuration through specialArgs
    (`self`, or the whole `inputs`), so unless scan_sound rules that out
    the answer is None: the whole tree.  A directory holding the flake
    itself among the reads (the repository root, `.`, included) is the
    whole tree too, said on stderr with the file that names it, since the
    memo then never hits."""
    if not scan_sound(wt):
        return None
    c = cfg()
    flake_nix, hosts_dir = c.flake_file("flake.nix"), c.targets_path
    objs = union_objects(union)
    depth = hosts_dir.count("/") + 1
    other_targets = tuple(q for q, (t, _) in objs.items()
                          if t == "tree" and q.count("/") == depth
                          and q.startswith(hosts_dir + "/") and q != f"{hosts_dir}/{target}")
    seen: set[str] = set()
    todo = [flake_nix, f"{hosts_dir}/{target}"]
    via: dict[str, str] = {}
    named: set[str] = set()
    while todo:
        path = todo.pop()
        if path == ".":
            print(f"eval memo: {target} keys on the whole repository: {via[path]} names its root "
                  f"(a directory named by a path counts whole)", file=sys.stderr)
            return None
        if path in seen or path not in objs:
            continue
        seen.add(path)
        typ = objs[path][0]
        files = [path] if typ == "blob" else [
            q for q, (t, _) in objs.items() if t == "blob" and q.startswith(path + "/")]
        for f in files:
            if not f.endswith(".nix"):
                continue
            words, refs = file_facts(wt, f)
            named.update(words)
            for ref in refs:
                if f == flake_nix and ref.startswith(other_targets):
                    continue
                via.setdefault(ref, f)
                todo.append(ref)
    flake_dir = str(Path(flake_nix).parent)
    for q in sorted(seen):
        if objs[q][0] == "tree" and (q == flake_dir or flake_dir.startswith(q + "/")):
            print(f"eval memo: {target} keys on the whole {q}/ tree: {via[q]} names it "
                  f"(a directory named by a path counts whole)", file=sys.stderr)
    # An input taken as a relative path is read from the tree: its
    # directory, or the gitlink of a submodule (a commit object in objs).
    seen.update(q for n, q in path_inputs(wt).items() if n in named and q in objs)
    return frozenset(seen), frozenset(named)


@functools.cache
def path_inputs(wt: str) -> dict[str, str]:
    """The flake's root inputs locked as relative paths (`path:./...`): name
    -> repo path, from flake.lock in `wt`."""
    c = cfg()
    try:
        lock = load_json((Path(wt) / c.flake_file("flake.lock")).read_text())
    except (OSError, ValueError):
        return {}
    if not (isinstance(lock, dict) and isinstance(lock.get("nodes"), dict) and isinstance(lock.get("root"), str)):
        return {}
    nodes = lock["nodes"]
    root = nodes.get(lock["root"])
    inputs = root.get("inputs") if isinstance(root, dict) else None
    found: dict[str, str] = {}
    for name, key in (inputs.items() if isinstance(inputs, dict) else ()):
        node = nodes.get(key) if isinstance(key, str) else None
        locked = node.get("locked") if isinstance(node, dict) else None
        path = locked.get("path") if isinstance(locked, dict) and locked.get("type") == "path" else None
        if isinstance(path, str) and not path.startswith("/"):
            found[name] = os.path.normpath(f"{c.flake_path}/{path}")
    return found


def target_eval_key(wt: str, union: str, target: str) -> str:
    """Hash of everything `target`'s evaluation can read (target_eval_reads: the
    paths by git object id, flake.lock pruned by lock_part), so it is
    identical across checkouts and untouched by changes to unrelated
    files; the whole tree when the scan cannot tell."""
    reads = target_eval_reads(wt, union, target)
    if reads is None:
        return hashlib.sha256(f"tree {out(['git', 'rev-parse', union + '^{tree}'])}"
                              .encode()).hexdigest()
    objs = union_objects(union)
    paths, named = reads
    parts = [f"{objs[q][1]} {q}" for q in paths]
    lock_path = cfg().flake_file("flake.lock")
    if lock_path in objs:
        try:
            lock = load_json((Path(wt) / lock_path).read_text())
            parts.append(f"{lock_part(lock, named)} {lock_path}")
        except ValueError:
            parts.append(f"{objs[lock_path][1]} {lock_path}")
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def recorded_tip(provenance: str, branch: str) -> str | None:
    """The tip of `branch` a target's provenance file says its union held (the
    "main" field for main), or None."""
    try:
        doc = load_json(provenance)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    branches = doc.get("branches")
    tip = branches.get(branch) if isinstance(branches, dict) else None
    if tip is None and branch == "main":
        tip = doc.get("main")
    return tip if isinstance(tip, str) else None


def reached_between(wt: str, union: str, targets: list[str], base: str, tip: str) -> set[str]:
    """The targets among `targets` whose evaluation reads something that
    differs between the commits `base` and `tip`: a path of
    target_eval_reads (in the `union` commit checked out in `wt`), or a lock
    entry of an input those files name."""
    lock_path = cfg().flake_file("flake.lock")
    # A rename lists both paths: the one a target read may be the old.
    delta = set(out(["git", "diff", "--name-only", "--no-renames", base, tip]).splitlines())
    if not delta:
        return set()
    locks: dict[str, object] = {}

    def lock_at(ref: str) -> object:
        """flake.lock at `ref`, parsed; lock_part hashes anything else whole."""
        if ref not in locks:
            p = run(["git", "show", f"{ref}:{lock_path}"], check=False)
            try:
                locks[ref] = load_json(p.stdout) if p.returncode == 0 else None
            except ValueError:
                locks[ref] = p.stdout
        return locks[ref]

    reached: set[str] = set()
    for h in targets:
        reads = target_eval_reads(wt, union, h)
        if reads is None:
            reached.add(h)
            continue
        paths, named = reads
        files = any(c in paths or any(c.startswith(d + "/") for d in paths)
                    for c in delta - {lock_path})
        lock = (lock_path in delta
                and lock_part(lock_at(base), named) != lock_part(lock_at(tip), named))
        if files or lock:
            reached.add(h)
    return reached


def reached_targets(wt: str, union: str, targets: list[str], tip: str,
                    provenances: dict[str, str]) -> set[str]:
    """The targets among `targets` whose evaluation reads something this
    checkout's own work changed (reached_between): differing between `tip`
    (the commit about to deploy) and what of this branch the target already
    runs - the tip its provenance recorded, when that lies between origin/main's
    merge base and `tip`, else the merge base itself, so main's movement, a
    rebase or another branch's work never counts as this checkout's.  The
    drift core consults it for a target carrying another checkout's refused
    work alone: one this work does not reach is left running that work.  No
    merge base (nothing to measure from) reaches every target."""
    from .checkout import current_branch  # noqa: PLC0415 - checkout imports guard, which imports checkout

    own = current_branch() or "main"
    p = run(["git", "merge-base", tip, "refs/remotes/origin/main"], check=False)
    mb = p.stdout.strip()
    if p.returncode != 0 or not mb:
        return set(targets)
    by_base: dict[str, list[str]] = {}
    for h in targets:
        base = mb
        rec = recorded_tip(provenances.get(h, ""), own)
        if rec and rec != mb and is_ancestor(mb, rec) and is_ancestor(rec, tip):
            base = rec
        by_base.setdefault(base, []).append(h)
    reached: set[str] = set()
    for base, hs in by_base.items():
        reached |= reached_between(wt, union, hs, base, tip)
    return reached


def eval_expr(target: str, parallel: bool) -> str:
    """The --apply over nixosConfigurations: the toplevel's out/drv (not
    outPath: toJSON collapses any attrset holding an outPath attribute to
    that path string) and the switch policy the nixconvoy module declares.
    With parallel evaluation the nspawn containers' toplevels (nested NixOS
    evaluations, about a second each) are forced beside the target's - the
    only parallelism one configuration offers.  tryEval passes over a
    container declared by `path` alone, whose `config` is unset."""
    head = (f'cs: let c = cs."{target}".config; top = c.system.build.toplevel; '
            'ncv = c.nixconvoy or {}; '
            'policy = { out = top.outPath; drv = top.drvPath; '
            'last = ncv.switchLast or false; assessed = (ncv.bootAssessment or {}).enable or false; }; ')
    if not parallel:
        return head + "in policy"
    return (head + "containers = map (x: (builtins.tryEval x.config.system.build.toplevel.outPath).value) "
            "(builtins.attrValues c.containers); in "
            "builtins.parallel (containers ++ [ top.outPath ]) policy")


@functools.cache
def parallel_eval_known() -> bool:
    """Whether the nix here has the parallel-eval feature (Determinate Nix):
    whether builtins.parallel appears once the feature is named, since
    upstream only warns about an unknown feature and goes on."""
    p = run(["nix", "eval", "--extra-experimental-features", "parallel-eval",
             "--expr", "builtins ? parallel"], check=False, timeout=10)
    if p.returncode != 0:
        die(f"probing nix for parallel evaluation failed:\n{p.stderr.strip()}")
    return p.stdout.strip() == "true"


def eval_toplevels(wt: str, union: str, targets: list[str],
                   check: Future[str | None] | None) -> dict[str, Top]:
    """name -> Top, writing the .drv closures the build and switch use.  One
    `nix eval` process per stale target, all at once: the module system is
    CPU-serial per configuration, and one parallel-eval process forcing
    several targets overlaps them poorly.  --apply bypasses the flake eval
    cache, hence memo_dir.  A .drv is trusted only while rooted (root_drv,
    which keeps its closure whole through the build): a memo entry's is
    rooted again here before the hit counts, a fresh evaluation's as soon
    as its eval returns.  A failing `check` (the precheck) ends the deploy
    at once rather than after the evals."""
    d = memo_dir()
    keys = {h: target_eval_key(wt, union, h) for h in targets}
    paths = {h: d / f"{h}-{keys[h]}.json" for h in targets}
    tops: dict[str, Top] = {}
    stale: list[str] = []
    with memo_lock(d):
        memo_use(d, [p.stem for p in paths.values()])
        for h in targets:
            ent = memo_read(paths[h])
            if ent is not None and not root_drv(paths[h], ent.drv):
                paths[h].unlink(missing_ok=True)
                ent = None
            if ent is None:
                stale.append(h)
            else:
                tops[h] = ent
    if not stale:
        return tops
    if len(stale) < len(targets):
        print(f"  (memoized: {', '.join(h for h in targets if h not in stale)})")
    c = cfg()
    flake = f"{wt}/{c.flake_path}" if c.flake_path != "." else wt
    parallel = parallel_eval_known() if c.parallel_eval is None else c.parallel_eval

    def eval_target(h: str) -> Top | str:
        """The target's Top, written and rooted, or the failure text."""
        par = (["--eval-cores", str(min(8, os.cpu_count() or 1)),
                "--extra-experimental-features", "parallel-eval"] if parallel else [])
        p = run(["nix", "eval", "--json", *par, f"{flake}#nixosConfigurations",
                 "--apply", eval_expr(h, parallel)], check=False)
        if p.returncode != 0:
            return f"evaluating {h} failed:\n{p.stderr.strip()}"
        ent = memo_entry(load_json(p.stdout))
        if ent is None:
            return f"nix eval returned no toplevel for {h}:\n{p.stdout[:500]}"
        memo_write(paths[h], ent)
        with memo_lock(d):
            if not root_drv(paths[h], ent.drv):
                paths[h].unlink(missing_ok=True)
                return f"{h}'s .drv was collected as soon as it was evaluated; re-run"
        return ent

    with ThreadPoolExecutor(len(stale)) as pool:
        evals = {h: pool.submit(eval_target, h) for h in stale}
        pending = set(evals.values())
        while pending:
            # While the precheck is pending, wake to notice its failure early.
            waiting = check is not None and not check.done()
            done, _ = wait(pending, timeout=0.2 if waiting else None, return_when=FIRST_COMPLETED)
            pending -= done
            if check is not None and check.done() and check.result():
                die(f"{check.result()}; {NOTHING_DONE}")
    for h in stale:
        r = evals[h].result()
        if isinstance(r, str):
            die(r)
        tops[h] = r
    return tops


def precheck_memo_path(rev: str) -> Path:
    """The precheck memo entry for `rev`'s tree.  Keyed on the whole tree
    (so nothing declares what the check reads; nix's own hash of the
    check's inputs spares the build when a changed tree left those alone),
    written only once the check passed, so presence is the proof."""
    tree = out(["git", "rev-parse", rev + "^{tree}"])
    return memo_dir() / f"check-{cfg().precheck}-{tree}.json"


def precheck_gate(wt: str, rev: str) -> str | None:
    """Prove the precheck (the flake's checks.<system>.<check>) for `rev`,
    checked out in `wt`: the failure text, or None once it passes, from the
    memo or by building it.  Its own build rather than an input of the
    toplevels', so a violation is reported before any package build is
    scheduled and never sways the toplevel build's sandbox decision.
    Output is captured (-L: the whole report, not nix's log tail) and the
    caller dies, since this runs on a thread beside the target evals."""
    from .targets import build_drvs  # noqa: PLC0415 - targets imports this module

    c = cfg()
    path = precheck_memo_path(rev)
    if memo_read(path) is not None:
        return None
    flake = f"{wt}/{c.flake_path}" if c.flake_path != "." else wt
    p = run(["nix", "eval", "--json", f"{flake}#checks.{c.targets_arch}.{c.precheck}",
             "--apply", "c: { out = c.outPath; drv = c.drvPath; }"], check=False)
    if p.returncode != 0:
        return f"evaluating the precheck failed:\n{p.stderr.strip()}"
    ent = memo_entry(load_json(p.stdout))
    if ent is None:
        return f"nix eval returned no derivation for the precheck:\n{p.stdout[:500]}"
    err = build_drvs([ent.drv], f"precheck {c.precheck}", ("-L",), capture=True)
    if err is None:
        memo_write(path, ent)
    return err


def precheck_unproven(rev: str) -> bool:
    return bool(cfg().precheck) and memo_read(precheck_memo_path(rev)) is None
