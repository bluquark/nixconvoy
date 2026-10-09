"""The verbs beside deploy: commit, squash, pull, abandon and status."""

from __future__ import annotations

import os
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path

from . import hosts, memo
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
    lock_live,
    on_origin,
    origin_tips,
    pop_tmp_commits,
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


def squash() -> None:
    await_background_push()
    git("fetch", "--quiet", "origin")
    pop_tmp_commits()
    wb = require_work_branch("squash")
    if current_branch() != wb:
        die(f"squash runs from the work branch ({wb}); currently on "
            f"{current_branch() or '(detached HEAD)'}")
    if staged_differs():
        die("staged but undeployed changes present; deploy or unstage them first")
    ensure_rebased()
    msg = out(["git", "show", f"HEAD:{MSG_FILE}"]) if rev(f"HEAD:{MSG_FILE}") else ""
    if not msg:
        die(f"{wb} has no {MSG_FILE}; write one (it becomes the main commit message)")
    # A branch forked from another's tip (a subagent worktree made by hand at
    # its parent's) would land that branch's increments under this message.
    carried = [b for b, tip in listed_tips(allowed_branches()).items()
               if b != wb and is_ancestor(tip, "HEAD")
               and not is_ancestor(tip, "refs/remotes/origin/main")]
    if carried:
        die(f"{wb} contains the unsquashed increments of {', '.join(carried)}; squash "
            "that branch first, then re-run")
    if sub_parent(Path.cwd().resolve()) is not None:
        require_unlocked(Path.cwd())  # before anything is pushed: finish removes the worktree
    # A blank line after the subject, or git folds the whole first paragraph
    # (subject + bullets) into %s.
    lines = msg.splitlines()
    if len(lines) > 1 and lines[1].strip():
        msg = "\n".join([lines[0], "", *lines[1:]])

    tree = tree_less_msg("HEAD")
    main = rev("refs/remotes/origin/main")
    if main is None or tree == out(["git", "rev-parse", "origin/main^{tree}"]):
        die("no changes vs origin/main to squash")
    commit = out(["git", "commit-tree", tree, "-p", main, "-m", msg])

    p = git("push", "--quiet", "origin", f"{commit}:refs/heads/main", check=False)
    if p.returncode != 0:
        die(f"pushing main failed (moved since fetch?); re-run:\n{p.stderr.strip()}")
    # Refused where main is checked out (the main worktree): catch_up moves it.
    git("branch", "-f", "main", commit, check=False)

    git("reset", "--quiet", commit)  # move wb to main; working tree untouched
    Path(MSG_FILE).unlink(missing_ok=True)  # now main's commit message
    print(f"squashed onto main: {summarize(commit)}")
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
        tip = rev(f"refs/remotes/origin/{ident.wb}")
        if tip and not is_ancestor(tip, "refs/remotes/origin/main"):
            die(f"this worktree is not on {ident.wb}, which carries pushed increments; "
                f"`git switch {ident.wb}` there and re-run to discard them")
        # Its removal takes the reflog along: nothing may be lost to it.
        if out(["git", "status", "--porcelain"]):
            die("this worktree has uncommitted or untracked changes, which the removal would "
                "lose; commit or discard them (git status there), then re-run")
        if not on_origin():
            die("HEAD carries commits not on origin, which the removal would lose; push them "
                "(git push origin HEAD:main) or deploy from the work branch, then re-run")
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


def finish(wb: str) -> None:
    """The checked-out work branch after a squash or abandon, now at
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
    is a unique prefix of - of any checkout on this machine (squash/abandon
    run from elsewhere, as status suggests)."""
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
    os.chdir(found[0])


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
        return run(["git", *args], cwd=str(d), check=False).stdout.strip()

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
        live = False  # a subagent worktree's lock, another session's, alive
        if ident.kind == "sub":
            reason = worktree_lock(d)
            live = reason is not None and lock_live(reason)
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
        if ident.kind == "sub" and not own and not (counts["staged"] or counts["unstaged"]):
            if live:
                notes.append(p("empty, locked by its agent's claude session: abandon removes it "
                               "once that exits", "dim"))
            else:
                notes.append(p(f"empty: nixconvoy abandon {d.name} removes it", "dim"))
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


__all__ = ["abandon", "commit_only", "enter_sub", "pull", "squash", "status", "work_branch_name"]
