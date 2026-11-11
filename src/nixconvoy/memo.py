"""The eval memo: each target's toplevel (and its switch kit) keyed by
exactly what its evaluation can read, so a change to a module one target
imports re-evaluates that target alone and a no-op redeploy evaluates
nothing; the flake checks' passes memoized on the union's tree."""

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
from collections.abc import Collection, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from . import strings
from .checkout import lock_pid_alive, pid_started
from .config import (
    CONFIG_FILE,
    SHELL_PRELUDE,
    Config,
    ConfigError,
    SwitchPolicy,
    cfg,
    main_ref,
    parse,
    root,
)
from .proc import cache_dir, die, dump_json, is_ancestor, load_json, out, run

# A target entry lives while its .drv does: unrooted once no checkout's
# last deploy used it (memo_prune), the store's collector decides.
# Registering a root takes well under 0.1 s; a daemon that keeps it
# waiting is stuck, and the deploy says so.
ROOT_TIMEOUT = 5
# A file being written, named after its writer: <name>.tmp<pid>, or nix's
# <link>.tmp-<pid>-<rand> under roots/.
TMP_WRITER = re.compile(r"\.tmp-?(\d+)(?:-\d+)?$")
# pid_started's rounding: a writer starts before its file, never after.
START_SLACK = 2
# The flock excludes this process's own threads too; this lock in front
# keeps the wait message for another process.
MEMO_LOCK = threading.Lock()


class Top(NamedTuple):
    """A target's evaluated toplevel and its switch kit (nix/kit.nix,
    evaluated against the target's own pkgs: "" for a target switched
    plainly)."""
    out: str
    drv: str
    kit: str
    kit_drv: str


def memo_entry(obj: object) -> Top | None:
    """A Top from a memo file's or nix eval's attrset."""
    if not isinstance(obj, dict):
        return None
    o, drv = obj.get("out"), obj.get("drv")
    kit, kit_drv = obj.get("kit", ""), obj.get("kit_drv", "")
    if not (isinstance(o, str) and isinstance(drv, str) and isinstance(kit, str) and isinstance(kit_drv, str)):
        return None
    return Top(o, drv, kit, kit_drv)


def memo_read(path: Path) -> Top | None:
    try:
        return memo_entry(load_json(path.read_text()))
    except (OSError, ValueError):
        return None


def memo_write(path: Path, ent: Top) -> None:
    """Atomic rename, so concurrent deploys never see a partial entry."""
    try:
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(dump_json({"out": ent.out, "drv": ent.drv, "kit": ent.kit,
                                  "kit_drv": ent.kit_drv}))
        tmp.replace(path)
    except OSError:
        pass


def memo_root(path: Path, kit: bool = False) -> Path:
    """A target entry's GC root: a symlink to its .drv (`kit`: its kit's,
    <stem>.kit.drv) under roots/ beside the entries, registered with the
    store as an indirect root."""
    return path.parent / "roots" / f"{path.stem}{'.kit' if kit else ''}.drv"


def root_entry(path: Path, ent: Top) -> bool:
    """Whether `path`'s entry's .drvs (the toplevel's and the kit's) are
    rooted, registering what is not (root_drv)."""
    return root_drv(path, ent.drv) and (not ent.kit_drv or root_drv(path, ent.kit_drv, kit=True))


def root_drv(path: Path, drv: str, kit: bool = False) -> bool:
    """Whether `path`'s entry's .drv is rooted, registering the root when
    it is not: nix-instantiate's --add-root on the imported .drv (no
    build), through the daemon, so on return the .drv closure is kept
    whole or gone - gone is False, the entry stale.  A root already
    pointing at a .drv still there holds without asking the daemon.  Any
    other failure is the deploy's."""
    link = memo_root(path, kit)
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
        die(strings.MEMO_NO_NIX_INSTANTIATE)
    finally:
        if not rooted:
            with contextlib.suppress(OSError):
                if prior is None or link.readlink() != prior:
                    link.unlink()
    if p.returncode == -signal.SIGKILL:
        die(strings.MEMO_ROOT_TIMEOUT(drv=drv, timeout=ROOT_TIMEOUT))
    if not Path(drv).exists():
        return False
    if p.returncode != 0:
        die(strings.MEMO_ROOT_FAILED(drv=drv, stderr=p.stderr.strip()))
    return True


@contextlib.contextmanager
def memo_lock(d: Path) -> Iterator[None]:
    """The memo's lock, held by a prune, by a deploy from its record
    (memo_use) through the hits it trusts, and around each fresh
    evaluation's rooting: a prune reads the records once, so one running
    beside a deploy could unroot an entry recorded and trusted after its
    read, and two deploys rooting one entry would unlink each other's
    link.  The lock file sits beside the memo dir, which holds entries,
    records, roots and the proof logs alone."""
    with MEMO_LOCK, d.with_name(f"{d.name}.lock").open("a") as f:
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
    alternating different trees do not evict each other, beside the
    prechecks' proof log (precheck_log).  Per-entry files written by
    atomic rename make concurrent deploys safe."""
    d = cache_dir() / "eval-memo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def memo_prune(d: Path) -> None:
    """Drop a target entry whose .drv is gone, and the root of an entry
    gone or of one no checkout's record names (memo_use): an unrooted
    entry stays a hint until the store's collector takes its .drv.  The
    entries and records are the *.json files, the proof logs the *.jsonl;
    what else the dirs hold is another deploy's file being written, or
    nix's temporary link under roots/, left alone while its writer runs
    (drop_stale_tmp).  A root is kept while a record names it, entry or
    no entry.  Under memo_lock."""
    used = memo_used(d)
    roots = d / "roots"
    for path in d.iterdir():
        if path.is_dir() or path.suffix == ".jsonl":
            continue
        if path.suffix != ".json":
            drop_stale_tmp(path)
            continue
        ent = memo_read(path)
        try:
            if ent is None or not Path(ent.drv).exists() or (ent.kit_drv and not Path(ent.kit_drv).exists()):
                path.unlink()
                memo_root(path).unlink(missing_ok=True)
                memo_root(path, kit=True).unlink(missing_ok=True)
            elif path.stem not in used:
                memo_root(path).unlink(missing_ok=True)
                memo_root(path, kit=True).unlink(missing_ok=True)
        except OSError:
            pass
    for stray in ((d / "users").iterdir() if (d / "users").is_dir() else ()):
        if stray.suffix != ".json":
            drop_stale_tmp(stray)
    for link in (roots.iterdir() if roots.is_dir() else ()):
        try:
            stem = link.stem.removesuffix(".kit")
            if link.suffix != ".drv":
                drop_stale_tmp(link)
            elif stem not in used and not (d / f"{stem}.json").exists():
                link.unlink()
        except OSError:
            pass


def drop_stale_tmp(path: Path) -> None:
    """Drop `path`, a file being written, unless the writer its name
    carries (TMP_WRITER) runs: a pid alive yet started after the file was
    written is another process's, the writer's reused."""
    m = TMP_WRITER.search(path.name)
    try:
        if m is not None and lock_pid_alive(f"pid {m.group(1)}"):
            started = pid_started(int(m.group(1)))
            if started is None or started <= path.lstat().st_mtime + START_SLACK:
                return
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
            print(strings.MEMO_SPECIAL_ARGS, file=sys.stderr)
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
    flake.nix, a flake check's `<check> = import <path> { ... };`
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
        for check in c.flakes:
            # `checks.<system>.<check> = import <path> { ... };`, or a bare
            # `<check> = import <path>` binding when none is written so: the
            # whole stanza is dropped where its braces are flat, else the
            # import alone.
            leaf = rf"{re.escape(check)}\s*=\s*import\s+(\.\.?/[\w.+\-/]*)"
            stanza = next((p for p in (rf"checks\.[^\s=]*\.{leaf}", rf"(?<![\w\-.]){leaf}")
                           if re.search(p, scan)), None)
            if stanza:
                for m in re.finditer(stanza, scan):
                    skipped.add(os.path.normpath(Path(f).parent / m.group(1)))
                scan = re.sub(stanza + r"\s*\{[^{}]*\}\s*;", "", scan)
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
    flake_nix, hosts_dir, root = c.flake_file("flake.nix"), c.targets_path, c.target_root(target)
    objs = union_objects(union)
    # The other targets' directories, skipped where flake.nix names them:
    # the configured ones and the subdirectories of targets_dir, less any
    # holding this target's own directory or the flake (a target whose
    # directory is the flake's holds every other's files, which are still
    # theirs to read).
    others = {r for t in c.targets if t != target and (r := c.target_root(t))}
    if hosts_dir:
        depth = hosts_dir.count("/") + 1
        others.update(q for q, (t, _) in objs.items()
                      if t == "tree" and q.count("/") == depth and q.startswith(hosts_dir + "/"))
    if not hosts_dir:
        # The hosts directory inferred (hosts_candidates).  This target's
        # subdirectory of a candidate is its directory, read whole, when
        # one candidate holds it; the other targets' subdirectories are
        # skipped only when there is one candidate at all - a second one is
        # as likely a tree of shared profiles named after the targets,
        # which they import, so skipping it would hide its changes.
        candidates = hosts_candidates(wt, union, tuple(sorted({*c.inventory, target})), flake_nix)
        mine = [p for p, found in candidates if target in found]
        if root is None and len(mine) == 1:
            root = f"{mine[0]}/{target}" if mine[0] else target
        if len(candidates) == 1:
            parent, found = candidates[0]
            others.update(f"{parent}/{n}" if parent else n for n in found if n != target and not c.target(n).dir)
    flake_dir = str(Path(flake_nix).parent)
    others = {r for r in others if r != root and not any(d == r or d.startswith(r + "/")
                                                           for d in (root or "", flake_dir))}
    other_targets = tuple(f"{r}/" for r in others)
    seen: set[str] = set()
    todo = [flake_nix, *([root] if root else [])]
    via: dict[str, str] = {}
    if root:
        via[root] = (strings.MEMO_VIA_CONFIG if c.target(target).dir else strings.MEMO_VIA_DIRECTORY)(target=target)
    named: set[str] = set()
    while todo:
        path = todo.pop()
        if path == ".":
            print(strings.MEMO_WHOLE_REPO(target=target, via=via[path]), file=sys.stderr)
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
                if f == flake_nix and (ref in others or ref.startswith(other_targets)):
                    continue
                via.setdefault(ref, f)
                todo.append(ref)
    for q in sorted(seen):
        if objs[q][0] == "tree" and (q == flake_dir or flake_dir.startswith(q + "/")):
            print(strings.MEMO_WHOLE_TREE(target=target, tree=q, via=via[q]), file=sys.stderr)
    # An input taken as a relative path is read from the tree: its
    # directory, or the gitlink of a submodule (a commit object in objs).
    seen.update(q for n, q in path_inputs(wt).items() if n in named and q in objs)
    return frozenset(seen), frozenset(named)


@functools.cache
def hosts_candidates(wt: str, union: str, targets: tuple[str, ...],
                     flake_nix: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """The directories of the `union` commit that look like the hosts
    directory, (parent, the targets found): a directory holding
    subdirectories named after two or more of `targets`, each of which
    flake.nix names, itself or a file under it.  One alone could be anything; two named by flake.nix are the
    hosts' directories, or a tree of shared profiles (target_eval_reads
    tells by their number)."""
    objs = union_objects(union)
    _, refs = file_facts(wt, flake_nix)
    wanted = set(targets)
    by_parent: dict[str, set[str]] = {}
    for q, (t, _) in objs.items():
        parent, _, name = q.rpartition("/")
        if t == "tree" and name in wanted and any(r == q or r.startswith(q + "/") for r in refs):
            by_parent.setdefault(parent, set()).add(name)
    return tuple(sorted((p, frozenset(found)) for p, found in by_parent.items() if len(found) >= 2))


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


@functools.cache
def union_config(wt: str) -> Config:
    """nixconvoy.toml as the union tree checked out in `wt` holds it: the
    switch policies a deploy builds its kits from and orders its switches
    by, so every checkout deploying the same union makes the same kits,
    and an edit reaches a target only once it is in the union (staged)."""
    try:
        return parse((Path(wt) / CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ConfigError) as e:
        die(strings.MEMO_UNION_CONFIG(error=e))


def kit_inputs(pol: SwitchPolicy) -> tuple[object, ...]:
    """What of a policy the kit is made from (the controller_shell checks
    are this machine's, no part of it): nothing for a plain switch."""
    return (pol.deadline, pol.has("internet"), pol.statements("target_shell")) if pol.confirmed else ()


def kit_part(wt: str, target: str) -> str:
    """The switch kit's share of `target`'s key: the kit sources this
    build carries and the target's policy (the union's nixconvoy.toml), as
    the same eval makes the kit; "kit none" for a target switched plainly."""
    pol = union_config(wt).switch_policy(target)
    if not pol.confirmed:
        return "kit none"
    checks = hashlib.sha256("\0".join((SHELL_PRELUDE, *pol.statements("target_shell"))).encode()).hexdigest()
    return f"kit {kit_src()} {pol.deadline} {pol.has('internet')} {checks}"


def target_eval_key(wt: str, union: str, target: str) -> str:
    """Hash of everything `target`'s evaluation can read (target_eval_reads: the
    paths by git object id, flake.lock pruned by lock_part; the switch
    kit's sources and the target's policy, kit_part), so it is identical
    across checkouts and untouched by changes to unrelated files; the
    whole tree when the scan cannot tell."""
    reads = target_eval_reads(wt, union, target)
    if reads is None:
        return hashlib.sha256(f"tree {out(['git', 'rev-parse', union + '^{tree}'])}\n{kit_part(wt, target)}"
                              .encode()).hexdigest()
    objs = union_objects(union)
    paths, named = reads
    parts = [*(f"{objs[q][1]} {q}" for q in paths), kit_part(wt, target)]
    lock_path = cfg().flake_file("flake.lock")
    if lock_path in objs:
        try:
            lock = load_json((Path(wt) / lock_path).read_text())
            parts.append(f"{lock_part(lock, named)} {lock_path}")
        except ValueError:
            parts.append(f"{objs[lock_path][1]} {lock_path}")
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def policy_moved(targets: list[str], base: str, tip: str) -> set[str]:
    """The targets whose kit inputs (kit_inputs of their switch policy)
    differ between nixconvoy.toml at `base` and at `tip`; every target
    when either file cannot be read as one."""
    def policies(ref: str) -> dict[str, tuple[object, ...]] | None:
        p = run(["git", "show", f"{ref}:{CONFIG_FILE}"], check=False)
        if p.returncode != 0:
            return None
        try:
            c = parse(p.stdout)
        except ConfigError:
            return None
        return {h: kit_inputs(c.switch_policy(h)) for h in targets}
    a, b = policies(base), policies(tip)
    if a is None or b is None:
        return set(targets)
    return {h for h in targets if a[h] != b[h]}


def reached_between(wt: str, union: str, targets: list[str], base: str, tip: str) -> set[str]:
    """The targets among `targets` whose evaluation reads something that
    differs between the commits `base` and `tip`: a path of
    target_eval_reads (in the `union` commit checked out in `wt`), a lock
    entry of an input those files name, or their switch policy in
    nixconvoy.toml (policy_moved: the kit the same eval makes)."""
    lock_path = cfg().flake_file("flake.lock")
    # A rename lists both paths: the one a target read may be the old.
    delta = set(out(["git", "diff", "--name-only", "--no-renames", base, tip]).splitlines())
    if not delta:
        return set()
    reached: set[str] = policy_moved(targets, base, tip) if CONFIG_FILE in delta else set()
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
                    runs: dict[str, str | None]) -> set[str]:
    """The targets among `targets` whose evaluation reads something this
    checkout's own work changed (reached_between): differing between `tip`
    (the commit about to deploy) and what of this branch the target already
    runs - `runs`, the commit of it the target's record holds, when that
    lies between origin/main's
    merge base and `tip`, else the merge base itself, so main's movement, a
    rebase or another branch's work never counts as this checkout's.  The
    drift core consults it for a target carrying another checkout's refused
    work alone: one this work does not reach is left running that work.  No
    merge base (nothing to measure from) reaches every target."""
    p = run(["git", "merge-base", tip, main_ref()], check=False)
    mb = p.stdout.strip()
    if p.returncode != 0 or not mb:
        return set(targets)
    by_base: dict[str, list[str]] = {}
    for h in targets:
        base = mb
        rec = runs.get(h)
        if rec and rec != mb and is_ancestor(mb, rec) and is_ancestor(rec, tip):
            base = rec
        by_base.setdefault(base, []).append(h)
    reached: set[str] = set()
    for base, hs in by_base.items():
        reached |= reached_between(wt, union, hs, base, tip)
    return reached


def nix_string(text: str) -> str:
    """`text` as a Nix string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${") + '"'


@functools.cache
def kit_src() -> str:
    """The switch kit's sources (nix/kit.nix with the crate), the tree
    nix/package.nix links beside this package under kit/: what a target's
    eval imports (eval_expr), through fetchTree by its narHash, the one
    way a pure evaluation reads a store path."""
    src = Path(__file__).resolve().parent / "kit"
    if not (src / "nix" / "kit.nix").is_file():
        die(strings.MEMO_KIT_MISSING(src=src))
    return str(src.resolve())


@functools.cache
def kit_nar_hash() -> str:
    p = run(["nix", "hash", "path", "--sri", kit_src()], check=False)
    if p.returncode != 0 or not p.stdout.strip():
        die(strings.MEMO_KIT_HASH_FAILED(src=kit_src(), stderr=p.stderr.strip()))
    return p.stdout.strip()


def kit_expr(pol: SwitchPolicy, target: str) -> str:
    """The kit's derivation for `target` (`c`, its nixosConfiguration):
    nix/kit.nix over its pkgs and config with `pol`; null for one switched
    plainly."""
    if not pol.confirmed:
        return "null"
    tree = (f'builtins.fetchTree {{ type = "path"; path = {nix_string(kit_src())}; '
            f'narHash = {nix_string(kit_nar_hash())}; }}')
    return (f'import "${{{tree}}}/nix/kit.nix" {{ inherit (c) pkgs config; target = {nix_string(target)}; '
            f'deadline = {pol.deadline}; '
            f'internetCheck = {"true" if pol.has("internet") else "false"}; prelude = {nix_string(SHELL_PRELUDE)}; '
            f'checks = [ {" ".join(nix_string(st) for st in pol.statements("target_shell"))} ]; }}')


def eval_expr(target: str, parallel: bool, pol: SwitchPolicy) -> str:
    """The --apply over nixosConfigurations: the toplevel's and the kit's
    out/drv (not outPath: toJSON collapses any attrset holding an outPath
    attribute to that path string).  With parallel evaluation the nspawn containers'
    toplevels (nested NixOS evaluations, about a second each) are forced
    beside the target's - the only parallelism one configuration offers.
    tryEval passes over a container declared by `path` alone, whose
    `config` is unset."""
    head = (f'cs: let c = cs."{target}"; top = c.config.system.build.toplevel; '
            f'kit = {kit_expr(pol, target)}; '
            'policy = { out = top.outPath; drv = top.drvPath; '
            'kit = if kit == null then "" else kit.outPath; kit_drv = if kit == null then "" else kit.drvPath; }; ')
    if not parallel:
        return head + "in policy"
    return (head + "containers = map (x: (builtins.tryEval x.config.system.build.toplevel.outPath).value) "
            "(builtins.attrValues c.config.containers); in "
            "builtins.parallel (containers ++ [ top.outPath ]) policy")


@functools.cache
def parallel_eval_known() -> bool:
    """Whether the nix here has the parallel-eval feature (Determinate Nix):
    whether builtins.parallel appears once the feature is named, since
    upstream only warns about an unknown feature and goes on."""
    p = run(["nix", "eval", "--extra-experimental-features", "parallel-eval",
             "--expr", "builtins ? parallel"], check=False, timeout=10)
    if p.returncode != 0:
        die(strings.MEMO_PARALLEL_PROBE_FAILED(stderr=p.stderr.strip()))
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
    as its eval returns.  A failing `check` (the tree gate) ends the deploy
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
            if ent is not None and not root_entry(paths[h], ent):
                paths[h].unlink(missing_ok=True)
                ent = None
            if ent is None:
                stale.append(h)
            else:
                tops[h] = ent
    if not stale:
        return tops
    if len(stale) < len(targets):
        print(strings.MEMO_MEMOIZED(targets=", ".join(h for h in targets if h not in stale)))
    c = cfg()
    flake = f"{wt}/{c.flake_path}" if c.flake_path != "." else wt
    parallel = parallel_eval_known()

    def eval_target(h: str) -> Top | str:
        """The target's Top, written and rooted, or the failure text."""
        par = (["--eval-cores", str(min(8, os.cpu_count() or 1)),
                "--extra-experimental-features", "parallel-eval"] if parallel else [])
        p = run(["nix", "eval", "--json", *par, f"{flake}#nixosConfigurations",
                 "--apply", eval_expr(h, parallel, union_config(wt).switch_policy(h))], check=False)
        if p.returncode != 0:
            return strings.MEMO_EVAL_FAILED(target=h, stderr=p.stderr.strip())
        ent = memo_entry(load_json(p.stdout))
        if ent is None:
            return strings.MEMO_NO_TOPLEVEL(target=h, stdout=p.stdout[:500])
        memo_write(paths[h], ent)
        with memo_lock(d):
            if not root_entry(paths[h], ent):
                paths[h].unlink(missing_ok=True)
                return strings.MEMO_DRV_COLLECTED(target=h)
        return ent

    with ThreadPoolExecutor(len(stale)) as pool:
        evals = {h: pool.submit(eval_target, h) for h in stale}
        pending = set(evals.values())
        while pending:
            # While the tree gate is pending, wake to notice its failure early.
            waiting = check is not None and not check.done()
            done, _ = wait(pending, timeout=0.2 if waiting else None, return_when=FIRST_COMPLETED)
            pending -= done
            if check is not None and check.done() and check.result():
                die(strings.MEMO_CHECK_FAILED(failure=check.result()))
    for h in stale:
        r = evals[h].result()
        if isinstance(r, str):
            die(r)
        tops[h] = r
    return tops


def precheck_log() -> Path:
    """The flake checks' proof log: one JSON line per tree a check passed on
    - the `check`, the `tree`, `at` (UTC), the `checkout` that proved it,
    the check's `out` and `drv` - appended by prove_tree, never pruned (a
    line per distinct tree is nothing).  Keyed on the whole tree, so
    nothing declares what the check reads; nix's own hash of the check's
    inputs spares the build when a changed tree left those alone."""
    return memo_dir() / "prechecks.jsonl"


def precheck_tree(rev: str) -> str:
    return out(["git", "rev-parse", rev + "^{tree}"])


def tree_proven(log: Path, check: str, tree: str) -> bool:
    """Whether `log` holds a line for `check` passing on `tree`.  Only the
    lines naming the tree are parsed (the log is read whole on every
    deploy).  A line another deploy is appending at this moment may read
    torn: skipped, like any line that is not a proof."""
    try:
        text = log.read_text()
    except OSError:
        return False
    for line in text.splitlines():
        if tree not in line:
            continue
        try:
            doc = load_json(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("check") == check and doc.get("tree") == tree:
            return True
    return False


def prove_tree(log: Path, check: str, tree: str, ent: Top) -> None:
    """Append `check`'s pass on `tree` to `log` in one write under
    O_APPEND, so concurrent deploys' lines never interleave.  A last line
    left unterminated (a write cut short: the disk full) is ended first,
    or every proof after it would be glued to it; another deploy's line
    half-landed at that moment earns a blank line, which no reader
    minds.  A write that fails costs the next deploy the check again,
    said on stderr."""
    doc = {"check": check, "tree": tree, "at": datetime.now(UTC).isoformat(timespec="seconds"),
           "checkout": str(root().resolve()), "out": ent.out, "drv": ent.drv}
    data = (dump_json(doc) + "\n").encode()
    try:
        fd = os.open(log, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            if os.lseek(fd, 0, os.SEEK_END) and os.pread(fd, 1, os.lseek(fd, -1, os.SEEK_END)) != b"\n":
                data = b"\n" + data
            if os.write(fd, data) != len(data):
                raise OSError(strings.MEMO_SHORT_WRITE)
        finally:
            os.close(fd)
    except OSError as e:
        print(strings.MEMO_PROOF_WRITE_FAILED(log=log, error=e), file=sys.stderr)


def tree_gate(wt: str, rev: str) -> str | None:
    """The pre-switch checks that see the tree alone, for `rev` checked out
    in `wt`: the flake checks (checks.<system>.<name>) its tree has no
    proof for yet, evaluated and built as one and each then logged; and
    the controller statements, run every deploy with the tree as their
    cwd.  The failure text, or None.  The flake checks' own build rather
    than an input of the toplevels', so a violation is reported before any
    package build is scheduled and never sways the toplevel build's sandbox
    decision.  Output is captured (-L: the whole report, not nix's log
    tail) and the caller dies, since this runs on a thread beside the
    target evals."""
    from .switch import parse_checks, run_checks  # noqa: PLC0415 - switch imports this module
    from .targets import build_drvs  # noqa: PLC0415 - targets imports this module

    c = cfg()
    log, tree = precheck_log(), precheck_tree(rev)
    due = [n for n in c.flakes if not tree_proven(log, n, tree)]
    if due:
        flake = f"{wt}/{c.flake_path}" if c.flake_path != "." else wt
        attrs = " ".join(f"{nix_string(n)} = {{ out = cs.{nix_string(n)}.outPath; drv = cs.{nix_string(n)}.drvPath; }};"
                         for n in due)
        p = run(["nix", "eval", "--json", f"{flake}#checks.{c.targets_arch}", "--apply", f"cs: {{ {attrs} }}"],
                check=False)
        if p.returncode != 0:
            return strings.MEMO_FLAKE_EVAL_FAILED(checks=", ".join(due), stderr=p.stderr.strip())
        doc = load_json(p.stdout)
        ents = {n: memo_entry(doc.get(n)) if isinstance(doc, dict) else None for n in due}
        for n, ent in ents.items():
            if ent is None:
                return strings.MEMO_FLAKE_NO_DRV(check=n, stdout=p.stdout[:500])
        err = build_drvs([ent.drv for ent in ents.values() if ent], strings.MEMO_FLAKE_WHAT(checks=", ".join(due)),
                         ("-L",), capture=True)
        if err:
            return err
        for n, ent in ents.items():
            if ent:
                prove_tree(log, n, tree, ent)
    if c.tree_statements:
        if (fault := parse_checks(c.tree_statements)) is not None:
            return strings.MEMO_TREE_CHECK_UNPARSED(fault=fault)
        code, report = run_checks(c.tree_statements, None, cwd=wt)
        if code != 0:
            return strings.MEMO_TREE_CHECK_FAILED(report=report or "")
    return None


def tree_checks_due(rev: str) -> bool:
    """Whether tree_gate has anything to run for `rev`: a flake check its
    tree has no proof for, or any controller statement (run every time)."""
    c = cfg()
    if c.tree_statements:
        return True
    if not c.flakes:
        return False
    log, tree = precheck_log(), precheck_tree(rev)
    return any(not tree_proven(log, n, tree) for n in c.flakes)
