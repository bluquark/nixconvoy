"""The eval memo: each target's toplevel (and the switch policy its
configuration declares) keyed by exactly what its evaluation can read, so a
change to a module one target imports re-evaluates that target alone and a
no-op redeploy evaluates nothing; the precheck's pass memoized on the
union's tree."""

from __future__ import annotations

import functools
import hashlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Collection
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import NamedTuple

from .config import cfg
from .proc import NOTHING_DONE, cache_dir, die, dump_json, is_ancestor, load_json, out, run

MEMO_MAX_AGE = 30 * 86400


class Protection(NamedTuple):
    """A named group of a target's units (nixconvoy.protected.<name>) whose
    stop/restart/reload the switch refuses unless `--allow <name>` admits it."""
    name: str
    units: tuple[str, ...]
    why: str


class Top(NamedTuple):
    """A target's evaluated toplevel and the switch policy its configuration
    declares through the nixconvoy NixOS module."""
    out: str
    drv: str
    protected: tuple[Protection, ...]
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
    prot = protections(obj.get("protected", {}))
    if prot is None:
        return None
    return Top(o, drv, prot, obj.get("last") is True, obj.get("assessed") is True)


def protections(obj: object) -> tuple[Protection, ...] | None:
    """The nixconvoy.protected attrset (name -> {units, why}) as Protections,
    sorted by name; None where it is not one."""
    if not isinstance(obj, dict):
        return None
    found = []
    for name, p in sorted(obj.items()):
        if not isinstance(name, str) or not isinstance(p, dict):
            return None
        units, why = p.get("units"), p.get("why")
        if not (isinstance(units, list) and all(isinstance(u, str) for u in units)
                and isinstance(why, str)):
            return None
        found.append(Protection(name, tuple(units), why))
    return tuple(found)


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
                                  "protected": {p.name: {"units": list(p.units), "why": p.why}
                                                for p in ent.protected},
                                  "last": ent.last, "assessed": ent.assessed}))
        tmp.replace(path)
    except OSError:
        pass


def memo_dir() -> Path:
    """One JSON file per (target, key) under the XDG cache, shared by every
    checkout on the machine (keys are content-addressed), so checkouts
    alternating different trees do not evict each other.  Per-entry files
    written by atomic rename make concurrent deploys safe."""
    d = cache_dir() / "eval-memo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def memo_prune(d: Path) -> None:
    """Drop entries whose .drv was garbage-collected or older than
    MEMO_MAX_AGE.  A precheck entry proves a tree, not a .drv, so only
    age applies to it."""
    now = time.time()
    for path in d.iterdir():
        ent = memo_read(path)
        try:
            if (ent is None or now - path.stat().st_mtime > MEMO_MAX_AGE
                    or (not path.name.startswith("check-") and not Path(ent.drv).exists())):
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
            'policy = { out = top.outPath; drv = top.drvPath; protected = ncv.protected or {}; '
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
    cache, hence memo_dir.  A memo entry is trusted only while its .drv
    exists, which implies its whole closure (GC never breaks references).
    A failing `check` (the precheck) ends the deploy at once rather than
    after the evals."""
    d = memo_dir()
    keys = {h: target_eval_key(wt, union, h) for h in targets}
    tops: dict[str, Top] = {}
    stale: list[str] = []
    for h in targets:
        ent = memo_read(d / f"{h}-{keys[h]}.json")
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

    def eval_target(h: str) -> subprocess.CompletedProcess[str]:
        par = (["--eval-cores", str(min(8, os.cpu_count() or 1)),
                "--extra-experimental-features", "parallel-eval"] if parallel else [])
        return run(["nix", "eval", "--json", *par, f"{flake}#nixosConfigurations",
                    "--apply", eval_expr(h, parallel)], check=False)

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
        p = evals[h].result()
        if p.returncode != 0:
            die(f"evaluating {h} failed:\n{p.stderr.strip()}")
        ent = memo_entry(load_json(p.stdout))
        if ent is None:
            die(f"nix eval returned no toplevel for {h}:\n{p.stdout[:500]}")
        tops[h] = ent
        memo_write(d / f"{h}-{keys[h]}.json", ent)
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
