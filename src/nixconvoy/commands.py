"""The verbs beside deploy: commit, land, pull, abandon, prune and status."""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path

from . import hosts, memo, proc
from .checkout import (
    PAST_REFS,
    add_worktree,
    allowed_branches,
    await_background_push,
    catch_up,
    checkout_identity,
    current_branch,
    deploy_running,
    drop_worktree,
    ensure_on_work_branch,
    ensure_rebased,
    git_op_in_progress,
    identity_of,
    listed_tips,
    lock_checkout,
    lock_pid_alive,
    on_origin,
    origin_tips,
    pop_tmp_commits,
    proc_stat,
    push_state,
    push_work_branch,
    require_unlocked,
    require_work_branch,
    sibling_checkouts,
    staged_differs,
    staged_snapshot,
    sub_parent,
    summarize,
    tree_less_msg,
    warn_no_msgfile,
    work_branch_name,
    worktree_lock,
)
from .config import MSG_FILE
from .guard import binary
from .proc import die, git, is_ancestor, ok, out, painter, rev, run


def commit_only(msg: str | None) -> None:
    if not msg:
        die('commit requires -m "..."')
    await_background_push()
    git("fetch", "--quiet", "origin")
    pop_tmp_commits()
    wb = require_work_branch("commit")
    if not staged_differs():
        die("nothing is staged")
    # No build gate here, so a staged change the precheck reads is checked alone.
    if memo.precheck_files(out(["git", "diff", "--cached", "--name-only"]).splitlines()):
        snap = staged_snapshot()
        objs = memo.union_objects(snap)
        if memo.precheck_unproven(objs):
            hosts.check_build_host()
            wt = add_worktree(snap)
            try:
                print("running the precheck...")
                err = memo.precheck_gate(wt, objs)
            finally:
                drop_worktree(wt)
            if err:
                die(f"{err}; nothing was committed")
    ensure_on_work_branch(wb)
    git("commit", "--quiet", "-m", msg)
    warn_no_msgfile(wb)
    push_work_branch(wb)


def land() -> None:
    await_background_push()
    git("fetch", "--quiet", "origin")
    pop_tmp_commits()
    wb = require_work_branch("land")
    if current_branch() != wb:
        die(f"land runs from the work branch ({wb}); currently on "
            f"{current_branch() or '(detached HEAD)'}")
    if staged_differs():
        die("staged but undeployed changes present; deploy or unstage them first")
    ensure_rebased()
    tree = tree_less_msg("HEAD")
    main = rev("refs/remotes/origin/main")
    if main is None or tree == out(["git", "rev-parse", "origin/main^{tree}"]):
        die("no changes vs origin/main to land")
    msg = out(["git", "show", f"HEAD:{MSG_FILE}"]) if rev(f"HEAD:{MSG_FILE}") else ""
    if not msg:
        die(f"{wb} has no {MSG_FILE}; write one (it becomes the main commit message)")
    # A branch forked from another's tip (a subagent worktree made by hand at
    # its parent's) would land that branch's increments under this message.
    carried = [b for b, tip in listed_tips(allowed_branches()).items()
               if b != wb and is_ancestor(tip, "HEAD")
               and not is_ancestor(tip, "refs/remotes/origin/main")]
    if carried:
        die(f"{wb} contains the unlanded increments of {', '.join(carried)}; land "
            "that branch first, then re-run")
    if sub_parent(Path.cwd().resolve()) is not None:
        require_unlocked(Path.cwd())  # before anything is pushed: finish removes the worktree
    # A blank line after the subject, or git folds the whole first paragraph
    # (subject + bullets) into %s.
    lines = msg.splitlines()
    if len(lines) > 1 and lines[1].strip():
        msg = "\n".join([lines[0], "", *lines[1:]])

    commit = out(["git", "commit-tree", tree, "-p", main, "-m", msg])

    p = git("push", "--quiet", "origin", f"{commit}:refs/heads/main", check=False)
    if p.returncode != 0:
        die(f"pushing main failed (moved since fetch?); re-run:\n{p.stderr.strip()}")
    # Refused where main is checked out (the main worktree): catch_up moves it.
    git("branch", "-f", "main", commit, check=False)

    git("reset", "--quiet", commit)  # move wb to main; working tree untouched
    Path(MSG_FILE).unlink(missing_ok=True)  # now main's commit message
    print(f"landed on main: {summarize(commit)}")
    siblings = sibling_checkouts()  # the parent of a subagent worktree included
    finish(wb)
    if not catch_up(siblings):
        die("some checkouts could not be brought onto the new main (see above); "
            "fix them and `nixconvoy pull`")


def pull() -> None:
    """This checkout and its siblings onto origin/main (cli docstring)."""
    await_background_push()
    binary("deploy-plan")  # resolved before update_checkout moves into the others
    if not catch_up([Path.cwd(), *sibling_checkouts()]):
        sys.exit(1)


def abandon() -> None:
    await_background_push()
    git("fetch", "--quiet", "origin")
    pop_tmp_commits()
    ident = checkout_identity()
    here = Path.cwd().resolve()
    if ident.kind == "sub" and sub_parent(here) is not None:
        require_unlocked(Path.cwd())
    if ident.kind == "sub" and ident.wb and current_branch() != ident.wb and sub_parent(here):
        if (why := keep_reason(ident.wb)) is not None:
            die(f"this worktree {why}, then re-run")
        remove_sub([ident.wb])  # never deployed: still on its fresh branch
        return
    wb = require_work_branch("abandon")
    if current_branch() != wb:
        die(f"abandon runs from the work branch ({wb}); currently on "
            f"{current_branch() or '(detached HEAD)'}")
    main = rev("refs/remotes/origin/main")
    if main is None:
        die("origin/main not found")
    prev = rev("HEAD") or ""
    if prev == main and ok(["git", "diff", "--quiet", "HEAD"]) and checkout_identity().kind != "sub":
        die("nothing to abandon")

    git("reset", "--quiet", "--hard", main)
    Path(MSG_FILE).unlink(missing_ok=True)  # an uncommitted copy survives the hard reset
    git("branch", "-f", "main", main, check=False)
    print(f"abandoned {wb} (was {prev[:12]}, see reflog): {summarize(main)}")
    finish(wb)


def keep_reason(wb: str) -> str | None:
    """Why the current subagent worktree, owning `wb` but not on it, is not
    simply removable - what the removal, which takes the reflog and `wb`
    along, would lose - or None when it holds nothing of its own."""
    tip = rev(f"refs/remotes/origin/{wb}")
    if tip and not is_ancestor(tip, "refs/remotes/origin/main"):
        return (f"is not on {wb}, which carries pushed increments; "
                f"`git switch {wb}` there to discard them")
    local = rev(f"refs/heads/{wb}")
    if local and out(["git", "rev-list", "-n", "1", local, "--not", "--remotes=origin"]):
        return (f"owns {wb}, which carries commits not on origin (a deploy whose push "
                f"failed?); push it or `git switch {wb}` there and abandon")
    if out(["git", "status", "--porcelain"]):
        return ("has uncommitted or untracked changes, which the removal would lose; "
                "commit or discard them (git status there)")
    if not on_origin():
        return ("carries commits not on origin, which the removal would lose; push them "
                "(git push origin HEAD:main) or deploy from the work branch")
    return None


def prune(names: list[str]) -> None:
    """Remove the subagent worktrees that hold nothing of their own (cli
    docstring: Prune): the named ones, or every one under this checkout.
    One whose spawning claude session still runs is kept, whichever session
    asks (it may yet resume its agent, which needs the worktree), as is one
    locked by hand, or unlocked unless named, one at work (a nixconvoy, git
    operation or push running in it), on a work branch (deployed: land or
    abandon decides it) or with anything of its own; each kept one is said
    so.  Runs are serialised per repository."""
    here = Path.cwd().resolve()
    top = sub_parent(here) or here
    serial = Path(out(["git", "rev-parse", "--git-common-dir"])).resolve() / "nixconvoy-prune.lock"
    with serial.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        subs = ([find_sub(n) for n in names] if names
                else sorted(d for d in sibling_checkouts() if sub_parent(d) == top))
        if not subs:
            print(f"no subagent worktree under {top}")
        fetched = False
        for d in subs:
            try:
                if not d.is_dir():
                    continue  # removed since it was listed
                os.chdir(d)
                verdict = prune_check(d, named=bool(names))
                if verdict is None:
                    if not fetched:  # keep_reason reads origin's tips
                        git("fetch", "--quiet", "--no-auto-gc", "origin", check=False)
                        fetched = True
                    verdict = prune_here(d, named=bool(names))
            except (Exception, SystemExit) as e:  # noqa: BLE001 - one worktree's failure, reported
                proc.DYING = False
                verdict = f"kept, its check failed: {e!r}"
            finally:
                os.chdir(top)
            print(f"{sub_label(d)}: {verdict}", flush=True)


def prune_check(d: Path, *, named: bool) -> str | None:
    """Why the subagent worktree `d`, the cwd, is kept, judged without
    origin; None when only its contents remain to be checked."""
    reason = worktree_lock(d)
    if reason is None:
        if not named:
            return ("kept, unlocked: no session to outlive; "
                    f"`nixconvoy prune {d.name}` judges it")
    else:
        alive = lock_pid_alive(reason)
        if alive is None:
            return f"kept, locked by hand ({reason or 'no reason given'})"
        if alive:
            return "kept, the claude session that spawned its agent still runs"
    if (op := git_op_in_progress(d)) is not None:
        return f"kept, {op} in progress"
    if Path(push_state("pid")).exists():
        return "kept, a push of its work branch is in flight"
    cur = current_branch()
    wb = identity_of(d, cur).wb
    if wb is None:
        return "kept, it owns no work branch"
    if cur == wb:
        if wb.endswith(f"-{d.name}"):
            return f"kept, on its work branch {wb}: land or abandon decides it"
        return f"kept, it has {wb}, another checkout's work branch, checked out"
    return None


def prune_here(d: Path, *, named: bool) -> str:
    """Prune the subagent worktree `d`, the cwd, under its deploy lock:
    what became of it."""
    lock = lock_checkout()
    if lock is None:
        return "kept, a nixconvoy runs in it"
    try:
        if (why := prune_check(d, named=named)) is not None:
            return why
        wb = identity_of(d, current_branch()).wb
        assert wb is not None
        if (why := keep_reason(wb)) is not None:
            return f"kept, it {why}"
        with contextlib.redirect_stdout(io.StringIO()):
            remove_sub([wb])
        return "empty, removed"
    finally:
        lock.close()


def prune_after(pid: int) -> None:
    """`prune --after PID`: detach, prune what dead sessions left, wait for
    the claude session PID to exit, then prune again - its own agents'
    worktrees among them.  One waiter per session (a lock file named after
    it in the git dir); the output goes to nixconvoy-prune.log there, which
    starts over past 1 MiB."""
    stat = proc_stat(pid)
    session = f"pid {pid}" + (f" start {stat.split()[19]}" if stat else "")  # as a lock names it
    gitdir = Path(out(["git", "rev-parse", "--absolute-git-dir"]))
    if os.fork():
        return
    os.setsid()
    if os.fork():
        os._exit(0)
    logf = gitdir / "nixconvoy-prune.log"
    if logf.exists() and logf.stat().st_size > 1 << 20:
        logf.replace(logf.with_suffix(".log.old"))
    log = logf.open("a")
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    waiter = gitdir / f"nixconvoy-prune-after.{pid}"
    held = waiter.open("w")
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os._exit(0)

    def sweep(what: str) -> None:
        print(f"{time.strftime('%F %T')} claude session {pid} {what}", flush=True)
        try:
            prune([])
        except (Exception, SystemExit) as e:  # noqa: BLE001 - the waiter outlives a failed sweep
            proc.DYING = False
            print(f"prune failed: {e!r}", flush=True)

    try:
        sweep("started: pruning dead sessions'")
        while lock_pid_alive(session) is not False:
            time.sleep(5)
        sweep("exited: pruning its own")
    finally:
        waiter.unlink(missing_ok=True)
    os._exit(0)


def finish(wb: str) -> None:
    """The checked-out work branch after a land or abandon, now at
    origin/main's commit: an agent's moved there on origin too; a
    subagent's worktree has done its one task, so it is removed with its
    branch, here and on origin (the process moves into the parent)."""
    if sub_parent(Path.cwd().resolve()) is not None:
        remove_sub([wb])
        return
    old = rev(f"refs/remotes/origin/{wb}") or ""
    git("push", "--quiet", "origin", f"{wb}:refs/heads/{wb}",
        f"--force-with-lease=refs/heads/{wb}:{old}")
    print(f"{wb} reset onto origin/main, here and on origin")


def remove_sub(branches: list[str]) -> None:
    """Remove the current subagent worktree with its branches (`branches`
    and whatever it has checked out), here and on origin; the process moves
    into the parent."""
    here = Path.cwd().resolve()
    parent = sub_parent(here)
    assert parent is not None
    cur = current_branch()
    # The worktree's own branches only - its work branch, the fresh one it was
    # created on (a hand commit's post-commit hook pushes it), and the
    # checked-out one when named after it - never another checkout's it may
    # have checked out to read.  The fresh one goes when it holds nothing
    # beyond origin/main.
    fresh = f"worktree-{here.name}"
    names = [b for b in {cur, *branches} if b is not None and (b in branches or b == fresh or b.endswith(f"-{here.name}"))]
    if fresh not in names and rev(f"refs/heads/{fresh}") and is_ancestor(fresh, "refs/remotes/origin/main"):
        names.append(fresh)
    gone = {b: rev(f"refs/remotes/origin/{b}") for b in names}
    require_unlocked(here)
    if worktree_lock(here) is not None:
        git("worktree", "unlock", str(here))
    os.chdir(parent)
    git("worktree", "remove", "--force", str(here))
    for b in names:
        git("branch", "--quiet", "-D", b, check=False)
    for b, old in gone.items():
        if old:
            git("update-ref", "--create-reflog", f"{PAST_REFS}/{b}", old)
            git("push", "--quiet", "origin", f":refs/heads/{b}",
                f"--force-with-lease=refs/heads/{b}:{old}")
    # A review of a branch gone this long is no longer expected.
    cutoff = time.time() - 90 * 86400
    for ln in out(["git", "for-each-ref", "--format=%(refname) %(committerdate:unix)", PAST_REFS]).splitlines():
        ref, _, when = ln.partition(" ")
        if when and int(when) < cutoff:
            git("update-ref", "-d", ref, check=False)
    print(f"subagent worktree {here.relative_to(parent)} removed, with "
          f"{', '.join(branches)} here and on origin")


def sub_label(d: Path) -> str:
    return f"{d.parent.parent.parent.name}/{d.name}"


def enter_sub(name: str) -> None:
    """Act in the subagent worktree `name` - or the one whose name `name`
    is a unique prefix of - of any checkout on this machine (land/abandon
    run from elsewhere, as status suggests)."""
    os.chdir(find_sub(name))


def find_sub(name: str) -> Path:
    here = Path.cwd().resolve()
    subs = sorted((d for d in [here, *sibling_checkouts()] if sub_parent(d) is not None),
                  key=lambda d: (d.name, str(d)))
    own = [d for d in subs if sub_parent(d) == here]
    # The exact name anywhere, else a prefix: this checkout's first.
    found = ([d for d in subs if d.name == name]
             or [d for d in own if d.name.startswith(name)]
             or [d for d in subs if d.name.startswith(name)])
    if not found:
        die(f"no subagent worktree {name}* in any checkout here" + (
            f"; there are: {', '.join(map(sub_label, subs))}" if subs else ""))
    if len(found) > 1:
        die(f"{name} is ambiguous among the subagent worktrees {', '.join(map(sub_label, found))}")
    return found[0]


def status() -> None:
    """Every checkout on this machine as a tree, and the work branches only
    origin holds (cli docstring: Status)."""
    paint = painter()
    git("fetch", "--quiet", "--no-auto-gc", "origin", check=False)
    main = rev("refs/remotes/origin/main")
    tips = origin_tips()
    pats = allowed_branches()
    here = Path.cwd().resolve()
    checkouts = [here, *sibling_checkouts()]

    def g(d: Path, *args: str) -> str:
        try:
            return run(["git", *args], cwd=str(d), check=False).stdout.strip()
        except FileNotFoundError:  # a subagent worktree pruned since it was listed
            return ""

    def count(d: Path, *args: str) -> int:
        return len(g(d, *args).splitlines())

    def line(d: Path) -> tuple[str, bool]:
        """The checkout's state after its name, and whether it is quiet -
        nothing of its own, nothing pending, no agent at work - and so
        rendered gray as a whole."""
        cur = g(d, "symbolic-ref", "--short", "-q", "HEAD") or None
        head = g(d, "rev-parse", "HEAD")
        ident = identity_of(d, cur, pats)
        # A subagent's commits of its own: beyond main and beyond the parent's
        # (any listed branch's) pushed tip it forked from.
        beyond = ["origin/main"] + ([f"refs/remotes/origin/{b}" for b in tips
                                     if b != cur and any(fnmatchcase(b, p) for p in pats)]
                                    if ident.kind == "sub" else [])
        ahead = int(g(d, "rev-list", "--count", "HEAD", "--not", *beyond) or 0)
        behind = int(g(d, "rev-list", "--count", "HEAD..origin/main") or 0)
        own = ahead  # commits of its own: a review checkout's are on origin
        if cur is None and ahead and on_origin(str(d), pats):
            own = 0
        counts = {label: count(d, *args) for label, args in (
            ("staged", ["diff", "--cached", "--name-only"]),
            ("unstaged", ["diff", "--name-only"]),
            ("untracked", ["ls-files", "--others", "--exclude-standard"]))}
        live = False  # a subagent worktree's lock: its session alive, or a hand lock
        alive: bool | None = False
        if ident.kind == "sub":
            reason = worktree_lock(d)
            alive = lock_pid_alive(reason) if reason is not None else False
            live = alive is not False
        op = git_op_in_progress(d)
        deploying = deploy_running(d)
        quiet = not own and not any(counts.values()) and not live and op is None and not deploying
        p = (lambda text, _style: text) if quiet else paint
        if cur is None:
            where = (p("detached at origin/main", "green") if head == main
                     else p(f"detached at {head[:12]}, nothing of its own", "green")
                     if not own else p(f"detached, {ahead} ahead of origin/main", "red"))
        elif cur == "main":
            where = p("main", "green") + (p(f", {ahead} unpushed", "red") if ahead else "")
        elif ahead:
            tip = tips.get(cur)
            pushed = (p("pushed", "green") if tip == head
                      else p("unpushed" if tip else "not on origin", "red"))
            where = f"{p(cur, 'yellow')} +{ahead} {pushed}"
        else:
            where = p(f"{cur} = origin/main (idle)", "green")
        if behind:
            where += p(f", {behind} behind main", "magenta")
        notes: list[str] = []
        if ident.wb and cur != ident.wb and ident.kind != "main":
            notes.append(p(f"owns {ident.wb}", "dim"))
        notes += [p(f"{label} {n}", "cyan") for label, n in counts.items() if n]
        if ident.kind == "sub" and not own and not any(counts.values()):
            if alive is None:
                notes.append(p("empty, locked by hand: prune keeps it", "dim"))
            elif live:
                notes.append(p("empty, locked by its agent's claude session: pruned once that "
                               "exits", "dim"))
            else:
                notes.append(p(f"empty: nixconvoy prune {d.name} removes it", "dim"))
        msg = g(d, "show", f"HEAD:{MSG_FILE}").partition("\n")[0] if cur not in (None, "main") else ""
        if msg:
            notes.append(p(f'"{msg[:60]}"', "dim"))
        if op:
            notes.append(p(f"[{op} in progress]", "bold red"))
        if deploying:
            notes.append(p("[deploying]", "bold red"))
        text = "  ".join([where, *notes])
        return (paint(text, "dim") if quiet else text), quiet

    def parent(d: Path) -> Path:
        return sub_parent(d) or d

    def is_main(d: Path) -> bool:
        return identity_of(d, g(d, "symbolic-ref", "--short", "-q", "HEAD") or None, pats).kind == "main"

    tops = sorted({parent(d) for d in checkouts}, key=lambda p: (not is_main(p), p.name))
    width = max(len(d.name) + 4 for d in checkouts)

    def show(indent: str, d: Path, pad: int) -> None:
        text, quiet = line(d)
        label = d.name if indent else d.name + "/"
        print(f"{indent}{paint(label.ljust(pad), 'dim' if quiet else 'bold')}  {text}")

    for top in tops:
        if top in checkouts:
            show("", top, width)
        else:
            name = paint(f"{top.name + '/':<{width}}", "bold")
            print(f"{name}  {paint('(not a checkout here)', 'dim')}")
        for d in sorted(d for d in checkouts if d != top and parent(d) == top):
            show("  └ ", d, width - 4)
    local = {g(d, "symbolic-ref", "--short", "-q", "HEAD") for d in checkouts}
    local |= set(out(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]).splitlines())
    elsewhere = []
    for name, tip in sorted(tips.items()):
        if name == "main" or name in local or not any(fnmatchcase(name, p) for p in pats):
            continue
        if main and is_ancestor(tip, main):
            continue
        n = out(["git", "rev-list", "--count", f"origin/main..{tip}"])
        elsewhere.append(f"{name} +{n}: {summarize(tip)}")
    if elsewhere:
        print(paint("on origin only (other machines' checkouts):", "dim"))
        for e in elsewhere:
            print(f"  {paint(e, 'yellow')}")


__all__ = ["abandon", "commit_only", "enter_sub", "land", "prune", "prune_after", "pull", "status", "work_branch_name"]
