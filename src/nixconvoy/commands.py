"""The verbs beside deploy: commit, land, pull, undeploy, open, close and status."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import IO, NamedTuple

from . import config, lanes, names, strings, submodule
from .checkout import (
    DEPLOYED_TRAILER,
    KEPT_EDIT,
    LOCK_FILE,
    MACHINE_ENV,
    PENDING_REFS,
    REBASE_MAP_REFS,
    REWRITTEN_REFS,
    SESSIONLESS,
    STALE_STAMP,
    GitQueries,
    add_worktree,
    adopt_origin_tip,
    ancestor_pids,
    await_background_push,
    branch_at,
    catch_up,
    checkout_identity,
    clear_refspec,
    common_state_path,
    current_branch,
    deploy_running,
    drop_worktree,
    edit_message,
    editor,
    ensure_rebased,
    ensure_sole_writer,
    fetch_args,
    flock_holder,
    forget_pushed,
    git_common,
    git_op_in_progress,
    holder_of,
    identity_of,
    landing_message,
    listed,
    listed_tips,
    lock_checkout,
    lock_holder,
    lock_live,
    lock_pid_alive,
    message_on_file,
    note_pushed,
    on_origin,
    origin_tips,
    owned,
    pending_announcements,
    pop_tmp_commits,
    proc_stat,
    push_cmd,
    push_in_flight,
    ran,
    require_work_branch,
    restage_msg,
    sibling_checkouts,
    sole_writer_problem,
    staged_differs,
    state_path,
    sub_dir,
    sub_parent,
    summarize,
    this_prefix,
    trailer_targets,
    tree_less_msg,
    unstage_msg,
    work_branch_name,
    worktree_lock,
    write_holder,
)
from .config import CONFIG_FILE, MSG_FILE, cfg, main_ref, main_short, remote_ref, repo_names
from .guard import binary, decide
from .names import label as sub_label
from .proc import (
    Fail,
    die,
    failure,
    git,
    git_remote,
    is_ancestor,
    ok,
    out,
    painter,
    refused,
    rev,
    run,
    solve_with_mergiraf,
)
from .wire import (
    Alive,
    Dirty,
    DropBranches,
    Fetch,
    Flag,
    Lock,
    Nested,
    Occupied,
    OwnBranch,
    PlanQuery,
    PlanReply,
    PruneParams,
    PushInFlight,
    Ran,
    ReleaseDeployLock,
    ReleaseNameLock,
    Removed,
    RemoveWorktree,
    Stopped,
    TakeDeployLock,
    TakeNameLock,
    Text,
    Unpushed,
)


def holds(wb: str, tip: str, not_yet_deployed: bool = False) -> None:
    """The targets `wb` (at `tip`) has work not deployed to
    (lanes.undeployed): a landing is refused on them unless
    `not_yet_deployed` (the landed work then rolls out there from the next
    deploy anywhere)."""
    wt = add_worktree(tip)
    try:
        held = lanes.undeployed(wb, tip, wt)
    finally:
        drop_worktree(wt)
    if not held:
        return
    them = ", ".join(held)
    if not not_yet_deployed:
        die(strings.LAND_HOLDS_BACK(wb=wb, them=them, it_them="it" if len(held) == 1 else "them",
                                    targets=" ".join(held)))
    print(strings.LAND_NOT_YET_DEPLOYED(them=them))


def kept_edit() -> Path:
    """Where a landing's edited message waits for the landing (checkout.KEPT_EDIT)."""
    return state_path(KEPT_EDIT)


def file_message(wb: str, given: str | None) -> str:
    """checkout.landing_message over this checkout's MSG_FILE (message_on_file)."""
    return landing_message(given, message_on_file(), wb)


def refuse_no_message(wb: str, given: str | None) -> None:
    """The refusal main_message ends in, up front when no editor could
    supply the message - before the checks that cost (holds' evaluation,
    the merge of --with-children)."""
    if not file_message(wb, given) and not kept_edit().exists() and editor() is None:
        die(strings.LAND_NO_MSG_FILE(wb=wb))


def main_message(wb: str, given: str | None, main: str, appended: str = "",
                 tips: list[tuple[str, str]] | None = None) -> str:
    """The main commit's message: file_message, `appended` (the subs'
    MSG_FILEs, land --with-children) below it, the whole offered in the
    editor on a terminal (checkout.edit_message, `tips` as there) unless -m
    gave it; refused when the checkout's own says nothing and no editor
    runs.  Called once every refusal is behind, since the user's edit is
    work: it is kept in the git dir (kept_edit), where a re-run after a
    failure further on (a push rejected) seeds the editor from it, or lands
    it off a terminal; the landing removes it.  `main`, origin/main as the
    landing read it, is checked again after the edit, the landing refused
    rather than retried on a main that moved meanwhile."""
    msg = file_message(wb, given)
    kept = kept_edit()
    whole = "\n\n".join(x for x in (msg, appended) if x)
    if given is None and kept.exists():
        whole = kept.read_text(encoding="utf-8").strip()
    if given is None and (edited := edit_message(whole, wb, tips)) is not None:
        kept.write_text(edited + "\n", encoding="utf-8")
        git_remote(*fetch_args())
        if rev(main_ref()) != main:
            die(strings.LAND_MAIN_MOVED_DURING_EDIT(main=main_short()))
        return edited
    if not whole:
        die(strings.LAND_NO_MSG_FILE(wb=wb))
    return whole


LANDINGS = "landings.json"  # in the git dir (state_path): the landings not known to be finished


class Part(NamedTuple):
    dir: str  # a checkout the landing takes
    wb: str
    tip: str  # its HEAD as landed
    msg: str  # its MSG_FILE then (message_on_file)


class Landed(NamedTuple):
    """A landing from the push of main on: what its finish takes.  It is
    on file (LANDINGS) from before the push until the finish is through:
    a failed push may have gone through all the same, or go through yet,
    and a run may be cut short anywhere, so nothing a landing did is ever
    taken back - the next run finishes the one main holds
    (settle_landings)."""
    commit: str  # main's
    parent: str  # the origin/main it is built on
    parts: list[Part]  # the checkout it runs in first
    landings: list[submodule.Landing]


def noted_landings() -> list[Landed]:
    try:
        return [Landed(r["commit"], r["parent"], [Part(*p) for p in r["parts"]],
                       [submodule.Landing.of(e) for e in r["landings"]])
                for r in json.loads(state_path(LANDINGS).read_text(encoding="utf-8"))]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def note_landings(them: list[Landed]) -> None:
    path = state_path(LANDINGS)
    if not them:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([{**r._asdict(), "landings": [e.record() for e in r.landings]} for r in them]),
                   encoding="utf-8")
    tmp.replace(path)


def push_main(r: Landed) -> None:
    """Main pushed onto `r.commit`, the landing on file first.  A push
    that failed is looked for on origin: one that went through is the
    landing; one origin refused, or main moved away from, can no longer
    be taken, and is forgotten; one that may yet be is kept for the next
    run."""
    note_landings([*noted_landings(), r])
    p = git_remote("push", "--quiet", cfg().remote, f"{r.commit}:refs/heads/{cfg().main_branch}", check=False)
    if p.returncode == 0:
        return
    git_remote(*fetch_args(), check=False)
    if is_ancestor(r.commit, main_ref()):
        return
    never = rev(main_ref()) != r.parent or refused(p)
    if never:
        note_landings([x for x in noted_landings() if x.commit != r.commit])
    die((strings.LAND_PUSH_MAIN_FAILED if never else strings.LAND_PUSH_MAIN_UNSURE)(
        main_branch=cfg().main_branch, remote=cfg().remote, stderr=p.stderr.strip()))


def take_landing(part: Part, commit: str, fresh: bool) -> None:
    """The checkout (the current directory) onto main's `commit`, its
    branch pushed so, where it is as the landing left it; one worked in
    since is its next catch-up's - as is, for a landing finished by a
    later run (not `fresh`), one with an edit in the way.  Its MSG_FILE
    is the commit's message now, and goes; one written since is the next
    landing's draft, kept (staged, when it was)."""
    head = rev("HEAD")
    if (current_branch() != part.wb or head not in (part.tip, commit)
            or (head == commit and rev(remote_ref(part.wb)) == commit)):
        return
    problem = sole_writer_problem(part.wb)
    lease = rev(remote_ref(part.wb)) or ""
    if head == part.tip:
        draft = message_on_file()
        staged = unstage_msg()
        git("checkout", "--quiet", "--", MSG_FILE, check=False)
        # What is staged or edited rides along.
        took = git("checkout", "--quiet", "-B", part.wb, commit, check=False).returncode == 0
        if not took and fresh:
            # Landed all the same: the branch moves, the working tree
            # showing the landing's changes until `git checkout -- .`.
            git("reset", "--quiet", commit)
            print(strings.LAND_CHILD_TREE_KEPT(name=Path.cwd().name), file=sys.stderr)
        Path(MSG_FILE).unlink(missing_ok=True)
        if draft != part.msg or not (took or fresh):
            if draft:
                Path(MSG_FILE).write_text(draft, encoding="utf-8")
            if staged:
                restage_msg()
        if not (took or fresh):
            print(strings.LAND_LEFT_EDITED(name=Path.cwd().name), file=sys.stderr)
            return
    if problem is not None:
        print(problem, file=sys.stderr)
    else:
        finish(part.wb, lease)


def complete(r: Landed, locked: bool = False, fresh: bool = True) -> bool:
    """Main holds `r.commit`: the landing finished, from the checkout it
    ran in (the current directory, under its lock) - each checkout it
    took, under its lock (`locked`: all held already), has its
    submodules' landings finished (submodule.finish_all) and is moved
    onto the commit (take_landing).  Every step does nothing where it is
    done already or the checkout has moved on, so this is also how a
    later run finishes a landing cut short (not `fresh`).  False when
    something is left for a later run."""
    main = cfg().main_branch
    # Refused where main is checked out (the main worktree): catch_up moves it.
    if not is_ancestor(r.commit, f"refs/heads/{main}"):
        git("branch", "-f", main, r.commit, check=False)
    here = Path.cwd()
    dirs = [Path(part.dir) for part in r.parts]
    locks: list[IO[str]] = []
    busy: set[Path] = set()
    try:
        for d in dirs[1:]:
            if locked or not d.is_dir():
                continue
            os.chdir(d)
            try:
                lock = lock_checkout()
                if lock is None:
                    print(strings.CHECKOUT_CATCH_UP_LINE(
                        name=d.name, status=strings.CHECKOUT_SKIPPED_LOCKED(holder=lock_holder())), file=sys.stderr)
                    busy.add(d)
                else:
                    locks.append(lock)
            finally:
                os.chdir(here)

        def top(e: submodule.Landing) -> Path:
            """The checkout the landing's worktree is planted in (a subagent worktree's nests in its parent's)."""
            return max((d for d in dirs if e.sub.dir.is_relative_to(d)), key=lambda d: len(d.parts), default=here)

        done = submodule.finish_all([e for e in r.landings if top(e) not in busy]) and not busy
        print(strings.LAND_DONE(main_branch=main, summary=summarize(r.commit)))
        kept = kept_edit()
        # A later run finds the edit of a landing after this one there, as well.
        if fresh or (kept.exists() and kept.read_text(encoding="utf-8").strip()
                     == out(["git", "log", "-1", "--format=%B", r.commit]).strip()):
            kept.unlink(missing_ok=True)
        for part, d in zip(r.parts, dirs, strict=True):
            if d in busy or not d.is_dir():
                continue  # not a directory: a subagent worktree removed since
            os.chdir(d)
            try:
                take_landing(part, r.commit, fresh)
            finally:
                os.chdir(here)
    finally:
        for lock in locks:
            lock.close()
    return done


def finish_landing(r: Landed, locked: bool = False, fresh: bool = True) -> None:
    """complete, the landing off file once nothing is left of it - with
    the others built on its parent, which main can no longer take."""
    if complete(r, locked, fresh):
        note_landings([x for x in noted_landings() if x.parent != r.parent])


def settle_landings(*, fetched: bool = False) -> list[Landed]:
    """The landings on file, under this checkout's lock: one main holds
    is finished (its push went through unknown to its run, or the run was
    cut short), one main moved away from forgotten, one main may yet
    take - it is still where the landing was built - kept until it has
    moved.  `fetched`: origin is, by the caller, the background push
    awaited and a dead rebase's tmp commits unwound.  Those main holds."""
    on_file = state_path(LANDINGS)
    if not on_file.exists():
        return []
    if not fetched:
        await_background_push()
        git_remote(*fetch_args())
        pop_tmp_commits()
    held = []
    for r in noted_landings():
        if is_ancestor(r.commit, main_ref()):
            print(strings.LAND_FINISHING(remote=cfg().remote, commit=r.commit[:12], file=on_file))
            finish_landing(r, fresh=False)
            held.append(r)
    note_landings([r for r in noted_landings() if rev(main_ref()) == r.parent or is_ancestor(r.commit, main_ref())])
    return held


def resume_landings(*, locked: bool = True) -> None:
    """settle_landings by a run that is no landing (`locked`: under this
    checkout's lock already; else it is taken, the landings left to the
    run holding it, and the caller is a pull, catching every checkout up
    itself): a failure is said and the run goes on."""
    if not state_path(LANDINGS).exists():
        return
    lock = None if locked else lock_checkout()
    if lock is None and not locked:
        return
    try:
        held = settle_landings()
        left = {r.commit for r in noted_landings()}
        # The catch-up the landing ends in, by the run that gets it through.
        if locked and any(r.commit not in left for r in held):
            catch_up_rest(held)
    except Fail:
        print(strings.LAND_NOT_FINISHED(error=strings.LAND_SAID_ABOVE), file=sys.stderr)
    except OSError as e:
        print(strings.LAND_NOT_FINISHED(error=e), file=sys.stderr)
    finally:
        if lock is not None:
            lock.close()


def catch_up_rest(landed: list[Landed]) -> bool:
    """After a landing: the checkouts it did not take, and the submodule
    repositories, onto the new main; whether all could be."""
    took = {Path(part.dir) for r in landed for part in r.parts}
    return catch_up([d for d in sibling_checkouts() if d not in took]) & submodule.catch_up_repos()


def land(message: str | None = None, not_yet_deployed: bool = False) -> None:
    await_background_push()
    git_remote(*fetch_args())
    pop_tmp_commits()
    # A landing finished only now was the one asked for, unless work was done here since.
    if (held := settle_landings(fetched=True)) and is_ancestor("HEAD", main_ref()):
        if not catch_up_rest(held):
            die(strings.LAND_CATCH_UP_FAILED())
        return
    wb = require_work_branch("land")
    if current_branch() != wb:
        die(strings.LAND_NOT_ON_WORK_BRANCH(wb=wb, cur=current_branch() or strings.LAND_DETACHED_HEAD))
    if staged_differs(less_msg=True):
        die(strings.LAND_STAGED)
    ensure_sole_writer(wb)  # origin's copy is reset at the end: it must be ours
    ensure_rebased()
    main = rev(main_ref())
    if main is None or tree_less_msg("HEAD") == out(["git", "rev-parse", f"{main_short()}^{{tree}}"]):
        die(strings.LAND_NO_CHANGES(main=main_short()))
    refuse_no_message(wb, message)
    # A branch forked from another's tip (a subagent worktree made by hand at
    # its parent's) would land that branch's increments under this message.
    carried = [b for b, tip in listed_tips().items()
               if b != wb and is_ancestor(tip, "HEAD")
               and not is_ancestor(tip, main_ref())]
    if carried:
        die(strings.LAND_CARRIES_UNLANDED(wb=wb, carried=", ".join(carried)))
    # The submodules first: main must never pin a branch about to go.
    gone = submodule.orphaned(Path.cwd())
    if gone:
        die(strings.LAND_SUBMODULE_ORPHANED(wb=wb, gone=", ".join(gone)))
    holds(wb, "HEAD", not_yet_deployed)
    draft = message_on_file()
    msg = main_message(wb, message, main)
    ensure_sole_writer(wb)  # the edit's fetch may have brought a tip
    here, tip = Path.cwd().resolve(), out(["git", "rev-parse", "HEAD"])
    # Until main's push nothing moves, here or on any origin, but the
    # landing commits' temporary refs: a landing refused has nothing to undo.
    landings = submodule.prepare_all(wb, msg)
    tree = tree_less_msg(submodule.pinned_commit(tip, landings, here))
    landed = Landed(out(["git", "commit-tree", tree, "-p", main, "-m", msg]), main,
                    [Part(str(here), wb, tip, draft)], landings)
    push_main(landed)
    finish_landing(landed)
    if not catch_up_rest([landed]):
        die(strings.LAND_CATCH_UP_FAILED())


def pull() -> None:
    """This checkout and its siblings onto origin/main (cli docstring)."""
    await_background_push()
    binary("nixconvoy-plan")  # dies here rather than once a checkout moved
    resume_landings(locked=False)
    if not (catch_up([Path.cwd(), *sibling_checkouts()]) & submodule.catch_up_repos()):
        die(strings.PULL_LEFT_AS_THEY_WERE)


def deployed_targets(commits: str) -> list[str]:
    """The targets the deploys of the commits in the range `commits` switched
    (their Deployed: trailers), sorted."""
    names: set[str] = set()
    for ln in out(["git", "log", f"--format={DEPLOYED_TRAILER}", commits]).splitlines():
        names |= trailer_targets(ln)
    return sorted(names)


def undeploy(back_to: str = "HEAD^", as_if: dict[str, str] | None = None) -> bool:
    """Undo the work branch's increments from HEAD back to `back_to` (cli
    docstring: Undeploy) ahead of the deploy the caller runs at once, which
    reverts them on the targets: whether any target runs what was undone,
    so that deploy is due.
    `as_if` (a dry activation): every check made and nothing moved, the
    commit the branch would be reset onto recorded there under its name,
    for that deploy to take the branch to be at."""
    await_background_push()
    git_remote(*fetch_args())
    pop_tmp_commits()
    ident = checkout_identity()
    here = Path.cwd().resolve()
    if ident.kind == "sub" and ident.wb and current_branch() != ident.wb and sub_parent(here):
        die(strings.UNDEPLOY_SUB_NOT_ON_WORK_BRANCH(label=sub_label(here), wb=ident.wb, name=here.name))
    wb = require_work_branch("undeploy")
    if current_branch() != wb:
        die(strings.UNDEPLOY_NOT_ON_WORK_BRANCH(wb=wb, cur=current_branch() or strings.LAND_DETACHED_HEAD))
    main = rev(main_ref())
    if main is None:
        die(strings.LAND_MAIN_NOT_FOUND(main=main_short()))
    prev = rev("HEAD") or ""
    base = out(["git", "merge-base", "HEAD", main])  # the branch's increments start here
    target = rev(f"{back_to}^{{commit}}")
    if target is None:
        die(strings.UNDEPLOY_NO_SUCH_COMMIT(back_to=back_to))
    if is_ancestor(target, base):
        target = base  # origin/main, or older: the whole branch
    elif not is_ancestor(target, "HEAD"):
        die(strings.UNDEPLOY_NOT_ON_BRANCH(back_to=back_to, sha=target[:12], wb=wb))
    if target == prev:
        die(strings.UNDEPLOY_NOTHING_BEYOND_MAIN(main=main_short()) if target == base
            else strings.UNDEPLOY_NOTHING_HEAD_IS(back_to=back_to))
    if staged_differs():
        die(strings.UNDEPLOY_STAGED)
    # A deploy's increment carries its trailers; a hand commit is not one
    # and is not crossed.
    undone = out(["git", "log", f"--format=%H%x09{DEPLOYED_TRAILER}",
                  f"{target}..HEAD"]).splitlines()
    for ln in undone:
        sha, _, trailer = ln.partition("\t")
        if not trailer.strip(" ,"):
            die(strings.UNDEPLOY_NOT_AN_INCREMENT(summary=summarize(sha)))
    deployed = deployed_targets(f"{target}..{prev}")
    # Every check before anything moves: the checkout's reset would lose
    # an unstaged edit to a file the increments changed (git reset --keep
    # refuses it), and so would a submodule's; origin's copy, reset at the
    # end, must be ours.
    lease = ensure_sole_writer(wb)
    edited = lost_edits(target, prev)
    if edited:
        die(strings.UNDEPLOY_LOST_EDITS(files=", ".join(edited)))
    moved = submodule.moved_pins(here, wb, prev, target)
    summaries = "\n  ".join(summarize(ln.partition("\t")[0]) for ln in undone)
    if as_if is not None:
        as_if[wb] = target
        print(strings.UNDEPLOY_DRY(wb=wb, n=len(undone), summaries=summaries))
        print(strings.UNDEPLOY_DRY_REVERTING(targets=", ".join(deployed)) if deployed else strings.UNDEPLOY_REACHED_NONE)
        return bool(deployed)
    p = git("reset", "--quiet", "--keep", target, check=False)
    if p.returncode != 0:
        die(strings.UNDEPLOY_RESET_FAILED(sha=target[:12], stderr=(p.stderr or "").strip()))
    submodule.undeploy_pins(moved, wb)
    print(strings.UNDEPLOY_DONE(wb=wb, n=len(undone), summaries=summaries))
    if deployed:
        print(strings.UNDEPLOY_REVERTING(targets=", ".join(deployed)))
    else:
        print(strings.UNDEPLOY_REACHED_NONE)
    finish(wb, lease, main_short() if target == main else target[:12])
    return bool(deployed)


def lost_edits(a: str, b: str, cwd: Path | None = None) -> list[str]:
    """The files with unstaged edits in `cwd` that `a` and `b` differ in:
    what `git reset --keep` from one to the other refuses to lose."""
    where = None if cwd is None else str(cwd)
    changed = set(out(["git", "diff", "--name-only", a, b], cwd=where).split("\n"))
    return sorted(changed & set(out(["git", "diff", "--name-only"], cwd=where).split("\n")) - {""})


def close_worktrees(names: list[str], *, force: bool = False) -> None:
    """Remove the subagent worktrees that hold nothing of their own (cli
    docstring: Close), as nixconvoy-plan's prune core decides: the named
    ones (their lock's session counted as ended for them), or every one
    under this checkout.  `force` removes
    the named ones whatever they hold, with their branches.  Runs are
    serialised per repository."""
    here = Path.cwd().resolve()
    top = sub_parent(here) or here
    if force and not names:
        die(strings.CLOSE_FORCE_NEEDS_NAMES)
    with prune_lock(Path(out(["git", "rev-parse", "--git-common-dir"])).resolve()).open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        subs = ([find_sub(n) for n in names] if names
                else sorted(d for d in sibling_checkouts() if sub_parent(d) == top))
        if not subs:
            print(strings.CLOSE_NO_SUBS(top=top), flush=True)
        fetched: list[PlanReply] = []
        for d in subs:
            label = sub_label(d)
            try:
                if not d.is_dir():
                    continue  # removed since it was listed
                if force:
                    if deploy_running(d):
                        die(strings.CLOSE_TOOL_RUNNING(label=sub_label(d)))
                    remove_sub(d, [identity_of(d, branch_at(d)).wb or ""])
                    verdict = strings.CLOSE_REMOVED_FORCE
                else:
                    why = prune_one(d, named=bool(names), own_session_ends=bool(names), fetched=fetched)
                    verdict = strings.CLOSE_EMPTY_REMOVED if why is None else strings.CLOSE_KEPT(why=why)
            except Exception as e:  # noqa: BLE001 - one worktree's failure, reported
                verdict = strings.CLOSE_CHECK_FAILED(error=failure(e))
            finally:
                os.chdir(top)
            print(strings.CLOSE_VERDICT(label=label, verdict=verdict), flush=True)


def close_submodules(names: list[str], *, force: bool = False) -> None:
    """`close <input> ...`: each submodule worktree removed with its
    branch, unless that would lose work (`force`: removed anyway, the pin
    put back to origin/main's when it named the branch's work, staged).
    One kept or failing stops none of the others; the run then fails."""
    git_remote(*fetch_args("--no-auto-gc"), check=False)
    top = Path.cwd()
    failed = []
    for name in names:
        try:
            sub = submodule.present_one(top, name)
            why = submodule.keeps(sub, top)
            if why is not None and not force:
                die(strings.CLOSE_SUBMODULE_KEPT(name=name, why=why))
            if work_branch_name() and submodule.off_default(sub, top):
                submodule.restore(sub, top)
            submodule.remove_one(sub)
        except Fail:
            if len(names) == 1:
                raise
            failed.append(name)
    if failed:
        die(strings.CLOSE_SUBMODULES_FAILED(names=", ".join(failed)))


class Stop(NamedTuple):
    """A stop's prune: the claude session (pid) whose agent stopped and the
    worktree's lock then (see the core's Stopped)."""

    pid: int
    lock: str | None


def prune_one(d: Path, *, named: bool, own_session_ends: bool = False,
              fetched: list[PlanReply] | None = None, prefetched: bool = False,
              deploy_held: bool = False, dropped_failed: list[str] | None = None,
              stopped: Stop | None = None, wait_name: bool | None = None) -> str | None:
    """nixconvoy-plan's prune core on the subagent worktree `d`: None once
    removed (the process then in its parent), else why it was kept.
    `fetched` shares one fetch across a run (`prefetched`: the caller
    fetched); `deploy_held`: the caller holds its deploy lock already;
    `dropped_failed` collects the origin branch deletions that failed.
    `own_session_ends`: the session the lock names ends for it - this
    one's, a named close (the invoking command no occupant).
    `stopped`: a stop's prune.  `wait_name` (a worktree named waits for its
    name lock, hold_lock, a sweep does not): the stop's prune, holding the
    repository's prune lock, is kept instead."""
    os.chdir(d)
    memo: list[PlanReply] = [] if fetched is None else fetched
    if prefetched and not memo:
        memo.append(Ran(True, ""))
    perform = PruneQueries(d, memo, deploy_held=deploy_held, invoker=own_session_ends,
                           stop=stopped, name_wait=named if wait_name is None else wait_name)
    try:
        done = decide(PruneParams(repo_names(), d.name, named, own_session_ends,
                                  None if stopped is None else Stopped(stopped.pid, stopped.lock)), perform)
    finally:
        perform.close()
    if dropped_failed is not None:
        dropped_failed.extend(perform.dropped_failed)
    return None if isinstance(done, Removed) else done.value


def name_lock_path(d: Path) -> Path:
    """The lock on the subagent worktree `d`'s path that the create and
    remove hooks take too: in the common git dir, so it outlives the
    worktree; named after the path with each / a %."""
    common = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]))
    return common_state_path("worktree-locks", common) / f"{str(d).replace('/', '%')}.lock"


def prune_lock(common: Path) -> Path:
    """The lock serialising the prunes of a repository's subagent
    worktrees (close, the stop and session hooks), in its common git dir."""
    return common_state_path("prune.lock", common)


# How long a lock may stay held by no process found holding it: the next
# holder has taken the flock but not yet written its line.
HOLDER_GONE_GRACE = 5


def hold_lock(path: Path, *, wait: bool = False, what: str = "") -> IO[str] | None:
    """The lock file `path` (`what`, for the messages), with this run's
    holder line written in it (write_holder); None when another holds it.
    `wait`: for as long as the holder's run is alive, or, once it is gone,
    the process holding the flock (a child it was passed to; flock_holder)
    - a flock goes with its last process, so no fixed deadline - dying
    naming the holder when the lock outlives both by HOLDER_GONE_GRACE."""
    path.parent.mkdir(exist_ok=True)
    f = path.open("a")
    gone: float | None = None
    told: int | None = None
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not wait:
                f.close()
                return None
            try:
                held = holder_of(path.read_text())
            except OSError:
                held = None
            if held is not None and held[1]:
                by, pid = held[0], held[2]
            elif (pid := flock_holder(path)) is not None:
                by = (strings.CLOSE_LOCK_BY_PID(pid=pid) if held is None
                      else strings.CLOSE_LOCK_BY_CHILD(pid=pid, holder=held[0]))
            else:
                by = None
            if by is not None:
                gone = None
                if told != pid:
                    print(strings.CLOSE_LOCK_WAITING(what=what or path, by=by), file=sys.stderr, flush=True)
                    told = pid
            elif gone is None:
                gone = time.monotonic()
            elif time.monotonic() - gone >= HOLDER_GONE_GRACE:
                f.close()
                die(strings.CLOSE_LOCK_HOLDER_GONE(
                    what=what or path, holder=held[0] if held is not None else strings.CLOSE_LOCK_NAMED_NONE))
            time.sleep(0.2)
        else:
            write_holder(f)
            return f


def occupied(d: Path, *, invoker: bool = False) -> bool:
    """Whether a process other than this one or its children has its
    working directory in `d` (via /proc, else lsof).  `invoker`: nor its
    ancestors, nor the process groups of those working in `d` - the session
    closing its own worktree by name, its shell and pipeline there with it
    (`timeout` gives its command a group of its own)."""
    me = os.getpid()
    mine = {me, *ancestor_pids()} if invoker else {me}
    cwds: dict[int, str] = {}
    stats: dict[int, tuple[int, int]] = {}  # pid -> (ppid, pgid)
    if Path("/proc/self").exists():
        for e in Path("/proc").iterdir():
            if not e.name.isdigit():
                continue
            pid = int(e.name)
            try:
                cwds[pid] = str((e / "cwd").readlink())
            except OSError:
                continue  # gone, or another user's
            stat = proc_stat(pid)
            if stat is not None:
                f = stat.split()
                stats[pid] = (int(f[1]), int(f[2]))
    else:
        pid = 0
        for ln in run(["lsof", "-d", "cwd", "-F", "pn"], check=False).stdout.splitlines():
            if ln.startswith("p"):
                pid = int(ln[1:])
            elif ln.startswith("n") and pid:
                cwds[pid] = ln[1:]
        for ln in run(["ps", "-A", "-o", "pid=,ppid=,pgid="], check=False).stdout.splitlines():
            f = ln.split()
            if len(f) == 3 and all(x.isdigit() for x in f):
                stats[int(f[0])] = (int(f[1]), int(f[2]))
    def inside(cwd: str | None) -> bool:
        return cwd is not None and (Path(cwd) == d or d in Path(cwd).parents)

    groups = {stats[p][1] for p in mine if p in stats and inside(cwds.get(p))} if invoker else set()
    for pid, cwd in cwds.items():
        ppid, pgid = stats.get(pid, (0, 0))
        if pid in mine or ppid == me or pgid in groups:
            continue
        if inside(cwd):
            return True
    return False


class PruneQueries(GitQueries):
    """The prune core's queries on the subagent worktree `d`: its name lock
    (the create hook's too) and deploy lock held until close(); `fetched`
    shares one fetch across a run; `invoker`: the invoking command is a
    named close's, its processes in `d` no occupants; `stop`: a stop's
    prune, its agent's ignored files counted as its own."""

    def __init__(self, d: Path, fetched: list[PlanReply], *, deploy_held: bool = False,
                 invoker: bool = False, stop: Stop | None = None,
                 name_wait: bool = False) -> None:
        super().__init__()
        self.d = d
        self.invoker = invoker
        self.stop = stop
        self.name_wait = name_wait
        # The origin branch deletions that failed after the removal.
        self.dropped_failed: list[str] = []
        parent = sub_parent(d)
        assert parent is not None
        self.parent = parent
        self.fetched = fetched
        self.deploy_held = deploy_held
        self.locked = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "locked"]))
        self.held: list[IO[str]] = []
        self.name_fd: int | None = None

    def close(self) -> None:
        for f in self.held:
            f.close()
        self.held.clear()

    def take(self, path: Path, *, wait: bool = False, what: str = "") -> IO[str] | None:
        f = hold_lock(path, wait=wait, what=what)
        if f is not None:
            self.held.append(f)
        return f

    @staticmethod
    def read(f: Path) -> str | None:
        try:
            return f.read_text().removesuffix("\n")
        except FileNotFoundError:
            return None

    def __call__(self, ask: PlanQuery) -> PlanReply:
        done = Ran(True, "")
        match ask:
            case TakeNameLock():
                f = self.take(name_lock_path(self.d), wait=self.name_wait, what=str(self.d))
                if f is None:
                    return Flag(False)
                self.name_fd = f.fileno()
                return Flag(True)
            case ReleaseNameLock() | ReleaseDeployLock():
                return done  # close() releases both once the core is done
            case TakeDeployLock():
                if self.deploy_held:
                    return Flag(True)
                return Flag(self.take(state_path(LOCK_FILE)) is not None)
            case Lock():
                return Text(self.read(self.locked))
            case Alive(pid, start, own_ends):
                reason = f"pid {pid}" + (f" start {start}" if start is not None else "")
                return Flag(lock_live(reason) if own_ends else lock_pid_alive(reason) is not False)
            case PushInFlight():
                return Flag(push_in_flight())
            case Nested():
                inside = [Path(ln[len("worktree "):]).resolve() for ln in out(
                    ["git", "worktree", "list", "--porcelain"]).splitlines() if ln.startswith("worktree ")]
                return Flag(any(w != self.d and self.d in w.parents for w in inside))
            case OwnBranch():
                return Text(identity_of(self.d, current_branch()).wb)
            case Unpushed(sha):
                return Flag(bool(out(["git", "rev-list", "-n", "1", sha, "--not", f"--remotes={cfg().remote}"])))
            case Dirty():
                # An agent that stopped may be resumed: what git ignores (its
                # builds, notes) is kept for it too.
                ignored = ["--ignored"] if self.stop is not None else []
                return Flag(bool(out(["git", "status", "--porcelain", *ignored])) or submodule.own(self.d))
            case Occupied():
                return Flag(occupied(self.d, invoker=self.invoker))
            case Fetch():
                if not self.fetched:
                    self.fetched.append(super().__call__(ask))
                return self.fetched[0]
            case RemoveWorktree():
                await_background_push(wait=False)  # its last push's outcome, told before its files go
                submodule.remove_all(self.d)  # nested in it: their registrations go first
                # Under the name lock, which every relock takes too: the lock
                # judged is the one lifted.
                try:
                    lock: bytes | None = self.locked.read_bytes()
                except FileNotFoundError:
                    lock = None
                self.locked.unlink(missing_ok=True)
                try:
                    os.chdir(self.parent)
                    # git holds the name lock too, so a prune killed meanwhile
                    # leaves it deleting under the lock.
                    removed = git("worktree", "remove", str(self.d), check=False,
                                  pass_fds=() if self.name_fd is None else (self.name_fd,))
                finally:
                    # Still there (refused, or interrupted): locked as it was,
                    # unless a hand lock took its place meanwhile.
                    if lock is not None and self.d.is_dir() and not self.locked.exists():
                        with contextlib.suppress(FileNotFoundError):
                            self.locked.write_bytes(lock)
                if removed.returncode != 0 and self.d.is_dir():
                    os.chdir(self.d)
                    return ran(removed)
                if removed.returncode != 0:
                    # The tree went but not its registration.
                    git("worktree", "prune", check=False)
                return done
            case DropBranches(wb, cur):
                failed = drop_branches(sub_branches(self.d, cur, [wb]))
                self.dropped_failed = failed
                return Ran(not failed, "\n".join(failed))
            case _:
                return super().__call__(ask)


def outermost(d: Path) -> Path:
    """The checkout `d`'s subagent worktrees hang off, `d` if it is none."""
    while (up := sub_parent(d)) is not None:
        d = up
    return d


def transcript_cwd(path: Path, *, last: bool = False) -> Path | None:
    """The working directory a Claude Code transcript records first (where
    its agent started), or `last` (within its final 256 KiB)."""
    def cwd(raw: bytes) -> str | None:
        try:
            rec = json.loads(raw)
        except ValueError:
            return None
        found = rec.get("cwd") if isinstance(rec, dict) else None
        return found if isinstance(found, str) else None

    try:
        with path.open("rb") as f:
            if last:
                f.seek(max(0, f.seek(0, os.SEEK_END) - (256 << 10)))
                found = next(filter(None, map(cwd, reversed(f.read().splitlines()))), None)
            else:
                found = next(filter(None, map(cwd, f)), None)
    except OSError:
        return None
    return Path(found).resolve() if found else None


def finish(wb: str, lease: str, onto: str | None = None) -> None:
    """The checked-out work branch after a land or undeploy, now at `onto`
    (origin/main's commit, the default, or an earlier increment), moved
    there on origin too, over `lease` (ensure_sole_writer's).  A subagent
    worktree left with nothing stays, idle: `close` removes it (the
    session-exit sweep does so on its own)."""
    onto = onto or main_short()
    git_remote(*push_cmd(wb, lease))
    note_pushed(wb, rev(f"refs/heads/{wb}") or "")
    print(strings.LAND_RESET_ONTO(wb=wb, onto=onto))
    here = Path.cwd().resolve()
    if sub_parent(here) is not None and rev("HEAD") == rev(main_ref()):
        print(strings.LAND_WORKTREE_STAYS(name=here.name))


def remove_sub(here: Path, branches: list[str]) -> None:
    """`close --force`: the subagent worktree `here` removed with its
    branches (`branches` and whatever it has checked out) and its submodules,
    here and on origin, whatever they hold; the process ends up in the
    parent."""
    parent = sub_parent(here)
    assert parent is not None
    names = sub_branches(here, branch_at(here), [b for b in branches if b])
    # Under the name lock, so no create reuses it between the check and the
    # removal; git holds it too, through a kill of this process.
    held = hold_lock(name_lock_path(here), wait=True, what=str(here))
    assert held is not None
    if worktree_lock(here) is not None:
        git("worktree", "unlock", str(here))
    submodule.remove_all(here)
    os.chdir(parent)
    git("worktree", "remove", "--force", str(here), pass_fds=(held.fileno(),))
    # Still held: a create of the name reuses no branch about to go.
    failed = drop_branches(names)
    held.close()
    if failed:
        die("\n".join(failed))
    print(strings.CLOSE_SUB_REMOVED(path=here.relative_to(parent), branches=", ".join(names)
                                    or strings.CLOSE_NO_OWN_BRANCH, remote=cfg().remote))


def sub_branches(here: Path, cur: str | None, branches: list[str]) -> list[str]:
    """The subagent worktree `here`'s own branches only - its work branch
    (`branches`), the fresh one it was created on (a hand commit's
    post-commit hook pushes it) and the checked-out `cur` when named after
    it - never another checkout's it may have checked out to read.  The
    fresh one goes when it holds nothing beyond origin/main."""
    fresh = f"worktree-{here.name}"
    names = [b for b in {cur, *branches} if b is not None and (b in branches or b == fresh or b.endswith(f"-{here.name}"))]
    if fresh not in names and rev(f"refs/heads/{fresh}") and is_ancestor(fresh, main_ref()):
        names.append(fresh)
    return names


def drop_branches(names: list[str]) -> list[str]:
    """Delete `names` here and on origin: the origin deletions that failed.
    One origin holds commits of another checkout's (sole_writer_problem:
    taken over by name elsewhere) stays there, that checkout's now."""
    gone = {b: rev(remote_ref(b)) for b in names}
    failed = []
    for b in names:
        if sole_writer_problem(b) is not None:
            del gone[b]
            print(strings.CLOSE_BRANCH_STAYS(branch=b, remote=cfg().remote), file=sys.stderr)
        git("branch", "--quiet", "-D", b, check=False)
        forget_pushed(b)
    pushed = {b: old for b, old in gone.items() if old}
    if pushed:
        # One push (a stall ends it: prune holds the worktree's name lock
        # meanwhile).
        p = git_remote("push", "--quiet", cfg().remote, *(f":refs/heads/{b}" for b in pushed),
                       *(f"--force-with-lease=refs/heads/{b}:{old}" for b, old in pushed.items()), check=False)
        if p.returncode != 0:
            failed.append(strings.CLOSE_DELETE_FAILED(
                branches=", ".join(pushed), remote=cfg().remote,
                error=(p.stderr or "").strip() or strings.CLOSE_NO_ERROR_GIVEN))
            for b in pushed:
                del gone[b]
    # The gone branches' announcements and, of those gone from origin
    # too, rebase maps, on their own: a stale lease (origin moved since the
    # fetch) must not count as a branch deletion failed.
    for b in gone:
        git("update-ref", "-d", f"{REWRITTEN_REFS}/{b}", check=False)
    clears = [spec for b in names for spec in clear_refspec(b)]
    clears += [f":{REBASE_MAP_REFS}/{b}" for b in gone if rev(f"{REBASE_MAP_REFS}/{b}")]
    if clears:
        p = git_remote("push", "--quiet", cfg().remote, *clears, check=False)
        if p.returncode != 0:
            failed.append(strings.CLOSE_DELETE_ANNOUNCEMENTS_FAILED(
                branches=", ".join(names), remote=cfg().remote, refs=PENDING_REFS,
                error=(p.stderr or "").strip() or strings.CLOSE_NO_ERROR_GIVEN))
    return failed


def subs_of(top: Path) -> list[Path]:
    """The subagent worktrees nested in the checkout `top`."""
    return sorted(d for d in sibling_checkouts() if sub_parent(d) == top)


def has_increments(d: Path) -> bool:
    """Whether the checkout `d` has commits beyond origin/main: what an
    undeploy back to origin/main undoes."""
    head = run(["git", "rev-parse", "HEAD"], cwd=str(d), check=False).stdout.strip()
    return bool(head and not is_ancestor(head, main_ref()))


def enter_sub(name: str) -> None:
    """Act in the subagent worktree `name` names (names.worktree) of any
    checkout on this machine (land/undeploy run from elsewhere, as status
    suggests)."""
    os.chdir(find_sub(name))


def find_sub(name: str, verb: str = "close") -> Path:
    return names.worktree(name, Path.cwd().resolve(), verb)


def held(d: Path) -> bool:
    """Whether the worktree `d` is held: any checkout but a subagent
    worktree; a subagent worktree unless its lock names a claude session
    that is gone.  A hand lock, a sessionless one (`open --child` by hand)
    and none at all are a person's, as the sweeps take them."""
    if sub_parent(d) is None:
        return True
    reason = worktree_lock(d)
    if reason is None or reason.endswith(SESSIONLESS):
        return True
    return lock_pid_alive(reason) is not False


def orphans(tips: dict[str, str], checkouts: list[Path]) -> list[tuple[str, Path | None]]:
    """(branch, worktree owning it or None) for each orphan among `tips`
    (origin's): one of this machine's listed subagent branches
    (<prefix><branch-suffix>-<name>) with commits beyond origin/main and
    every other listed branch, which none of `checkouts` owns or whose
    owner is a subagent worktree no live session holds."""
    prefix = this_prefix()
    work = [b for b in tips if listed(b)]
    owners: dict[str, Path] = {}
    for d in checkouts:
        try:
            wb = identity_of(d, branch_at(d)).wb
        except FileNotFoundError:  # removed since it was listed
            continue
        if wb is not None:
            owners.setdefault(wb, d)
    found = []
    for b in sorted(work):
        if not b.startswith(prefix) or "-" not in b[len(prefix):]:
            continue
        d = owners.get(b)
        if d is not None and held(d):
            continue
        others = [remote_ref(o) for o in [cfg().main_branch, *work] if o != b and o in tips]
        if run(["git", "rev-list", "-n", "1", remote_ref(b), "--not", *others], check=False).stdout.strip():
            found.append((b, d))
    return found


def status() -> None:
    """Every checkout on this machine as a tree, this machine's orphaned
    subagent branches, and the work branches only origin holds."""
    paint = painter()
    git_remote(*fetch_args("--no-auto-gc"), check=False)
    main = rev(main_ref())
    tips = origin_tips()
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
        rendered gray as a whole.  A submodule holding anything keeps it lit."""
        cur = g(d, "symbolic-ref", "--short", "-q", "HEAD") or None
        head = g(d, "rev-parse", "HEAD")
        ident = identity_of(d, cur)
        # A subagent's commits of its own: beyond main and beyond the parent's
        # (any listed branch's) pushed tip it forked from.
        beyond = [main_short()] + ([remote_ref(b) for b in tips if b != cur and listed(b)]
                                    if ident.kind == "sub" else [])
        ahead = int(g(d, "rev-list", "--count", "HEAD", "--not", *beyond) or 0)
        behind = int(g(d, "rev-list", "--count", f"HEAD..{main_short()}") or 0)
        own = ahead  # commits of its own: a review checkout's are on origin
        if cur is None and ahead and on_origin(str(d)):
            own = 0
        counts = {label: count(d, *args) for label, args in (
            (strings.STATUS_STAGED, ["diff", "--cached", "--name-only"]),
            (strings.STATUS_UNSTAGED, ["diff", "--name-only"]),
            (strings.STATUS_UNTRACKED, ["ls-files", "--others", "--exclude-standard"]))}
        live = False  # a subagent worktree's lock: its session alive, or a hand lock
        alive: bool | None = False
        sessionless = False
        if ident.kind == "sub":
            reason = worktree_lock(d)
            sessionless = reason is not None and reason.endswith(SESSIONLESS)
            alive = lock_pid_alive(reason) if reason is not None else False
            live = alive is not False
        op = git_op_in_progress(d)
        deploying = deploy_running(d)
        quiet = (not own and not any(counts.values()) and not live and op is None and not deploying
                 and not submodule.own(d))
        p = (lambda text, _style: text) if quiet else paint
        if cur is None:
            where = (p(strings.STATUS_DETACHED_AT_MAIN(main=main_short()), "green") if head == main
                     else p(strings.STATUS_DETACHED_NOTHING_OWN(sha=head[:12]), "green")
                     if not own else p(strings.STATUS_DETACHED_AHEAD(n=ahead, main=main_short()), "red"))
        elif cur == cfg().main_branch:
            where = p(cur, "green") + (p(strings.STATUS_MAIN_UNPUSHED(n=ahead), "red") if ahead else "")
        elif ahead:
            tip = tips.get(cur)
            pushed = (p(strings.STATUS_PUSHED, "green") if tip == head
                      else p(strings.STATUS_UNPUSHED if tip else strings.STATUS_NOT_ON_REMOTE(remote=cfg().remote),
                             "red"))
            where = strings.STATUS_AHEAD(branch=p(cur, "yellow"), n=ahead, pushed=pushed)
        else:
            where = p(strings.STATUS_IDLE(branch=cur, main=main_short()), "green")
        if behind:
            where += p(strings.STATUS_BEHIND(n=behind, main_branch=cfg().main_branch), "magenta")
        notes: list[str] = []
        if ident.wb and cur != ident.wb and ident.kind != "main":
            notes.append(p(strings.STATUS_OWNS(wb=ident.wb), "dim"))
        notes += [p(strings.STATUS_COUNT(label=label, n=n), "cyan") for label, n in counts.items() if n]
        if own and cur is not None and cur != cfg().main_branch and git_common(d) == git_common(Path.cwd()):
            # By the checkout's files as they are: a note, not a verdict.
            try:
                held = lanes.undeployed(cur, head, str(d))
            except (Fail, OSError):
                held = []
            if held:
                notes.append(p(strings.STATUS_HOLDS_BACK(targets=", ".join(held)), "yellow"))
        notes += [p(a, "cyan") for a in submodule.notes(d)]
        if ident.kind == "sub" and not own and not any(counts.values()) and cur != ident.wb:
            if sessionless:
                notes.append(p(strings.STATUS_EMPTY_SESSIONLESS(name=d.name), "dim"))
            elif alive is None:
                notes.append(p(strings.STATUS_EMPTY_HAND_LOCKED, "dim"))
            elif live:
                notes.append(p(strings.STATUS_EMPTY_SESSION_LOCKED, "dim"))
            else:
                notes.append(p(strings.STATUS_EMPTY(name=d.name), "dim"))
        msg = message_on_file(str(d)).strip().partition("\n")[0] if cur not in (None, cfg().main_branch) else ""
        if msg:
            notes.append(p(strings.STATUS_SUBJECT(subject=msg[:60]), "dim"))
        if op:
            notes.append(p(strings.STATUS_OP_IN_PROGRESS(op=op), "bold red"))
        if deploying:
            notes.append(p(strings.STATUS_DEPLOYING, "bold red"))
        text = "  ".join([where, *notes])
        return (paint(text, "dim") if quiet else text), quiet

    def parent(d: Path) -> Path:
        return sub_parent(d) or d

    def is_main(d: Path) -> bool:
        return identity_of(d, g(d, "symbolic-ref", "--short", "-q", "HEAD") or None).kind == "main"

    tops = sorted({parent(d) for d in checkouts}, key=lambda p: (not is_main(p), p.name))
    width = max(len(d.name) + 4 for d in checkouts)

    def show(indent: str, d: Path, pad: int) -> None:
        try:
            text, quiet = line(d)
        except FileNotFoundError:
            return  # a subagent worktree pruned since it was listed
        label = d.name if indent else d.name + "/"
        print(f"{indent}{paint(label.ljust(pad), 'dim' if quiet else 'bold')}  {text}")

    for top in tops:
        if top in checkouts:
            show("", top, width)
        else:
            name = paint(f"{top.name + '/':<{width}}", "bold")
            print(f"{name}  {paint(strings.STATUS_NOT_A_CHECKOUT, 'dim')}")
        for d in sorted(d for d in checkouts if d != top and parent(d) == top):
            show("  └ ", d, width - 4)
    orphaned = orphans(tips, checkouts)
    if orphaned:
        print(paint(strings.STATUS_ORPHANS_HEADER, "dim"))
        for b, d in orphaned:
            n = out(["git", "rev-list", "--count", f"{main_short()}..{tips[b]}"])
            entry = strings.STATUS_ORPHAN_ENTRY(branch=b, n=n, summary=summarize(tips[b]),
                                                where=d if d is not None else strings.STATUS_ORPHAN_NO_WORKTREE)
            print(f"  {paint(entry, 'red')}")
    local = {g(d, "symbolic-ref", "--short", "-q", "HEAD") for d in checkouts}
    local |= set(out(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]).splitlines())
    local |= {b for b, _ in orphaned}
    elsewhere = []
    for name, tip in sorted(tips.items()):
        if name == cfg().main_branch or name in local or not listed(name):
            continue
        if main and is_ancestor(tip, main):
            continue
        n = out(["git", "rev-list", "--count", f"{main_short()}..{tip}"])
        elsewhere.append(strings.STATUS_ELSEWHERE_ENTRY(branch=name, n=n, summary=summarize(tip)))
    if elsewhere:
        print(paint(strings.STATUS_ELSEWHERE_HEADER(remote=cfg().remote), "dim"))
        for e in elsewhere:
            print(f"  {paint(e, 'yellow')}")
    names = sorted(submodule.entries(Path.cwd()))
    if names:
        print(paint(strings.STATUS_SUBMODULES(names=", ".join(names)), "dim"))
    stray = submodule.repo_notes()
    if stray:
        print(paint(strings.STATUS_STRAY_HEADER, "dim"))
        for e in stray:
            print(f"  {paint(e, 'red')}")
    pending = pending_announcements()
    if pending:
        print(paint(strings.STATUS_PENDING_HEADER, "dim"))
        now = time.time()
        for a in pending:
            entry = (strings.STATUS_PENDING_SETTLED if a.settled else strings.STATUS_PENDING)(
                key=a.key, summary=summarize(a.sha))
            # Display only: the announcer's clock, not this machine's.
            silent = now - a.at
            if not a.settled and silent > STALE_STAMP:
                print(f"  {paint(strings.STATUS_PENDING_DEAD(entry=entry, minutes=silent / 60), 'red')}")
            else:
                print(f"  {paint(entry, 'yellow')}")


def open_worktree(name: str, lock_reason: str | None) -> Path:
    """`nixconvoy open --child <name>`: the child worktree <root>/<child_worktrees>/<name>
    (nixconvoy.toml's `child_worktrees`; root: the checkout this one's subagent
    worktrees hang off) on the fresh branch worktree-<name> off origin's
    default branch - a worktree of that name is reused - owning <root's
    branch>-<name> from its first deploy.  Under the name lock prune holds
    while judging a worktree of the name.  `lock_reason` is the worktree
    lock Claude Code's create hook names its session with (replacing a
    reused one's); without it a hand-opened worktree is locked as
    sessionless, which prune keeps and land or undeploy lift."""
    if not re.fullmatch(r"[\w.-]+", name):
        die(strings.OPEN_BAD_NAME(name=name))
    root = outermost(Path.cwd().resolve())
    # close takes a declared submodule's name as the submodule, so a
    # worktree of that name could never be closed by it.
    if name in names.gitmodule_names(root):
        die(strings.OPEN_NAME_IS_SUBMODULE(name=name))
    d = root / sub_dir() / name
    ensure_excluded(root, d)
    branch = f"worktree-{name}"
    base = default_base(root)
    reason = lock_reason or f"nixconvoy open --child {name} {SESSIONLESS}"
    held = hold_lock(name_lock_path(d), wait=True, what=str(d))
    assert held is not None
    try:
        for attempt in range(3):
            if attempt:
                time.sleep(1)
            if d.is_dir() and run(["git", "rev-parse", "--show-toplevel"], cwd=str(d),
                                  check=False).stdout.strip() == str(d):
                if lock_reason is not None:
                    # Reused: its lock replaced in one rename.
                    locked = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "locked"],
                                      cwd=str(d)))
                    locked.with_suffix(".tmp").write_text(lock_reason)
                    locked.with_suffix(".tmp").replace(locked)
                print(d)
                return d
            git("worktree", "prune", cwd=str(root), check=False)
            d.parent.mkdir(parents=True, exist_ok=True)
            add = ([str(d), branch] if rev(f"refs/heads/{branch}") else ["-b", branch, str(d), base])
            p = git("worktree", "add", "--quiet", "--lock", "--reason", reason, *add, cwd=str(root),
                    check=False, pass_fds=(held.fileno(),))
            if p.returncode == 0:
                print(d)
                return d
            print(strings.OPEN_ADD_FAILED_RETRYING(stderr=(p.stderr or "").strip()), file=sys.stderr)
    finally:
        held.close()
    return die(strings.OPEN_COULD_NOT_CREATE(path=d))


def ensure_excluded(root: Path, d: Path) -> None:
    """A nested worktree not ignored is an untracked directory `git add -A`
    would stage as a gitlink: `d`'s directory is excluded through the
    repository's .git/info/exclude (local to the clone, as Claude Code
    excludes its own .claude/worktrees) unless something ignores it already."""
    if ok(["git", "check-ignore", "-q", "--no-index", str(d)], cwd=str(root)):
        return
    exclude = Path(out(["git", "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
                       cwd=str(root)))
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    with exclude.open("a", encoding="utf-8") as f:
        f.write(("" if not text or text.endswith("\n") else "\n") + f"/{sub_dir()}/\n")
    if not ok(["git", "check-ignore", "-q", "--no-index", str(d)], cwd=str(root)):
        die(strings.OPEN_NOT_IGNORED(dir=sub_dir(), exclude=exclude))


def default_base(root: Path) -> str:
    """Where a new branch starts: main as fetched (nixconvoy.toml's names;
    in a repository without one, the plugin's create hook runs here too:
    origin/HEAD, else origin/main), else HEAD."""
    if (root / CONFIG_FILE).is_file():
        refs = [config.names_at(root).main_ref]
    else:
        refs = ["refs/remotes/origin/HEAD", "refs/remotes/origin/main"]
    return next((r for r in refs
                 if run(["git", "rev-parse", "-q", "--verify", r], cwd=str(root), check=False).returncode == 0),
                "HEAD")


def open_checkout(path: Path, branch: str | None) -> Path:
    """`nixconvoy open <path>`: an agent checkout - a worktree of this
    repository at `path`, outside every checkout, on a work branch of this
    machine's: `branch` (a suffix, or the whole name under this machine's
    prefix), else <branch_prefix><path's name>, refused when origin holds
    that branch or a subagent branch under it (a removed checkout's, or
    another machine's when the prefix is shared: `branch` names it to take
    it over, or another).  A named branch is taken as
    this repository or origin holds it (origin's copy over a stale local
    one), else new off origin's default branch; a branch made here is
    pushed at once, so origin holds it from then on.  Its upstream is
    origin/main as nixconvoy keeps it.  Prints the path."""
    here = Path.cwd().resolve()
    d = path.expanduser().resolve()
    if d.exists():
        die(strings.OPEN_EXISTS(path=d))
    mine = git_common(here)
    up = d.parent
    while up != up.parent:
        if (up / ".git").exists():
            if git_common(up) == mine:
                die(strings.OPEN_NESTED(path=d, up=up))
            break
        up = up.parent
    git_remote(*fetch_args("--no-auto-gc"), check=False)
    tips = origin_tips()
    local = set(out(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]).split())
    prefix = this_prefix()
    if branch is None:
        branch = f"{prefix}{d.name}"
        held = sorted(b for b in tips if b == branch or b.startswith(f"{branch}-"))
        if held:
            die(strings.OPEN_BRANCH_HELD(remote=cfg().remote, held=", ".join(held), name=d.name))
    elif not branch.startswith(prefix) and "/" not in branch:
        branch = f"{prefix}{branch}"
    if not owned(branch):
        die(strings.OPEN_NOT_WORK_BRANCH(branch=branch, prefix=prefix))
    if branch.removeprefix(prefix) == "main" or "-" in branch.removeprefix(prefix):
        # "<branch-suffix>-<name>" is a subagent worktree's branch, "main-<name>" the main checkout's.
        die(strings.OPEN_BAD_SUFFIX(branch=branch, prefix=prefix))
    d.parent.mkdir(parents=True, exist_ok=True)
    if branch in local and branch in tips and rev(f"refs/heads/{branch}") != tips[branch]:
        # Origin's copy taken over a stale local one; a local one ahead
        # of it (its push pending) stays.
        if is_ancestor(branch, remote_ref(branch)):
            git("branch", "--quiet", "-f", branch, tips[branch])
        elif not is_ancestor(remote_ref(branch), branch):
            die(strings.OPEN_DIVERGED(branch=branch, remote=cfg().remote))
    if branch in local:
        p = git("worktree", "add", "--quiet", str(d), branch, check=False)
    elif branch in tips:
        p = git("worktree", "add", "--quiet", "-b", branch, str(d), remote_ref(branch), check=False)
    else:
        p = git("worktree", "add", "--quiet", "-b", branch, str(d), default_base(here), check=False)
    if p.returncode != 0:
        die(strings.OPEN_ADD_FAILED(stderr=(p.stderr or "").strip()))
    if branch in tips and is_ancestor(remote_ref(branch), branch):
        adopt_origin_tip(branch)
    git("branch", "--quiet", f"--set-upstream-to={main_short()}", branch, check=False)
    if branch not in local and branch not in tips:
        # A branch made here is reserved on origin: --force-with-lease
        # against no branch at all loses to an open of the same name
        # elsewhere since the fetch.  One this repository held already
        # is pushed by its next deploy.
        p = git_remote(*push_cmd(branch, ""), check=False)
        if p.returncode != 0:
            git("worktree", "remove", "--force", str(d), check=False)
            git("branch", "--quiet", "-D", branch, check=False)
            err = (p.stderr or "").strip()
            why = (strings.OPEN_PUSH_STALE if "stale info" in err else strings.OPEN_PUSH_UNREACHABLE)(
                remote=cfg().remote)
            die(strings.OPEN_PUSH_FAILED(branch=branch, why=why, stderr=err))
        note_pushed(branch, rev(f"refs/heads/{branch}") or "")
    print(d)
    print((strings.OPEN_CHECKOUT_ON_TAKEN if branch in tips and branch not in local else strings.OPEN_CHECKOUT_ON)(
        name=d.name, branch=branch, remote=cfg().remote), file=sys.stderr)
    return d


def close_checkout(path: Path, *, force: bool = False) -> None:
    """`nixconvoy close <path>`: the agent checkout at `path` (on its work
    branch: a main checkout is refused) removed with its work branch, here
    and on origin, when it holds nothing of its own - no edits, nothing
    beyond origin/main, no submodule holding anything - or `force`."""
    here = Path.cwd().resolve()
    d = path.expanduser().resolve()
    if d == here:
        die(strings.CLOSE_FROM_ANOTHER)
    if git_common(d) is None or git_common(d) != git_common(here):
        die(strings.CLOSE_NOT_A_WORKTREE(path=d))
    if git_common(d) == (d / ".git").resolve():
        die(strings.CLOSE_PRIMARY(path=d))
    if sub_parent(d) is not None:
        die(strings.CLOSE_IS_SUB(path=d, name=d.name))
    git_remote(*fetch_args("--no-auto-gc"), check=False)
    nested = subs_of(d)
    if nested:
        die(strings.CLOSE_HOLDS_SUBS(path=d, names=", ".join(w.name for w in nested)))
    os.chdir(d)
    lock = lock_checkout()
    os.chdir(here)
    if lock is None:
        die(strings.CLOSE_RUNNING_IN(holder=lock_holder(d), where=d))
    cur = branch_at(d)
    wb = identity_of(d, cur).wb
    if wb is None:
        die(strings.CLOSE_MAIN_CHECKOUT_ON(path=d, branch=cur) if cur
            else strings.CLOSE_MAIN_CHECKOUT_DETACHED(path=d))
    head = out(["git", "rev-parse", "HEAD"], cwd=str(d))
    # Its submodule worktrees are its own (removed with it): a pin they moved is not an edit.
    edits = [ln for ln in run(["git", "status", "--porcelain"], cwd=str(d), check=False).stdout.splitlines()
             if ln[3:] not in submodule.paths(d)]
    tip = rev(f"refs/heads/{wb}") if wb else None
    kept = (strings.CLOSE_KEPT_EDITS if edits
            else strings.CLOSE_KEPT_COMMITS(main=main_short()) if not is_ancestor(head, main_ref())
            else strings.CLOSE_KEPT_BRANCH(wb=wb, main=main_short())
            if tip and not is_ancestor(tip, main_ref())
            else strings.CLOSE_KEPT_SUBMODULE if submodule.own(d) else None)
    if kept and not force:
        die(strings.CLOSE_CHECKOUT_HOLDS(path=d, kept=kept))
    if worktree_lock(d) is not None:
        git("worktree", "unlock", str(d), check=False)
    submodule.remove_all(d)
    lock.close()
    git("worktree", "remove", "--force", str(d))
    names = [b for b in {cur, wb} if b and owned(b)]
    failed = drop_branches(names)
    if failed:
        die("\n".join(failed))
    print(strings.CLOSE_CHECKOUT_REMOVED(path=d, branches=", ".join(sorted(names)) or strings.CLOSE_NO_OWN_BRANCH,
                                         remote=cfg().remote))


def land_with_children(message: str | None = None, not_yet_deployed: bool = False) -> None:
    """`nixconvoy land --with-children`: this checkout and its subagent worktrees
    landed as one commit on main - each sub's branch merged into this
    branch's tree (mergiraf on the conflicts, as a deploy's union) under
    this checkout's MSG_FILE (or `message`) with each sub's MSG_FILE
    appended - every branch reset onto the new main, here and on origin,
    each worktree staying."""
    await_background_push()
    git_remote(*fetch_args())
    pop_tmp_commits()
    here = Path.cwd().resolve()
    if (held := settle_landings(fetched=True)) and all(
            is_ancestor("HEAD", main_ref(), cwd=str(d)) for d in [here, *subs_of(here)]):
        if not catch_up_rest(held):
            die(strings.LAND_CATCH_UP_FAILED())
        return
    if sub_parent(here) is not None:
        die(strings.LAND_CHILDREN_FROM_SUB)
    wb = require_work_branch("land --with-children")
    if current_branch() != wb:
        die(strings.LAND_NOT_ON_WORK_BRANCH(wb=wb, cur=current_branch() or strings.LAND_DETACHED_HEAD))
    ensure_sole_writer(wb)  # every branch is reset on origin at the end: all must be ours
    if staged_differs(less_msg=True):
        die(strings.LAND_STAGED)
    ensure_rebased()
    main = rev(main_ref())
    if main is None:
        die(strings.LAND_MAIN_NOT_FOUND(main=main_short()))
    refuse_no_message(wb, message)
    main_tree = out(["git", "rev-parse", f"{main}^{{tree}}"])
    # The participants: each sub on its work branch with work beyond main.
    parts: list[tuple[Path, str]] = []
    locks: list[IO[str]] = []
    try:
        for d in subs_of(here):
            label = sub_label(d)
            cur = branch_at(d)
            ident = identity_of(d, cur)
            head = out(["git", "rev-parse", "HEAD"], cwd=str(d))
            if staged_differs(str(d), less_msg=True):
                die(strings.LAND_CHILD_STAGED(label=label))
            if is_ancestor(head, main) or tree_less_msg("HEAD", str(d)) == main_tree:
                continue
            if cur != ident.wb or not ident.wb:
                die(strings.LAND_CHILD_UNDEPLOYED(label=label, where=cur or strings.LAND_A_DETACHED_HEAD))
            if git_op_in_progress(d):
                die(strings.LAND_CHILD_GIT_OP(label=label, op=git_op_in_progress(d)))
            os.chdir(d)
            lock = lock_checkout()
            os.chdir(here)
            if lock is None:
                die(strings.CLOSE_RUNNING_IN(holder=lock_holder(d), where=label))
            locks.append(lock)
            parts.append((d, ident.wb))
        if not parts:
            die(strings.LAND_NO_CHILD_WORK(name=here.name))
        for d, b in parts:
            problem = sole_writer_problem(b)
            if problem is not None:
                die(strings.LAND_CHILD_PROBLEM(name=d.name, problem=problem))
        # Each sub's MSG_FILE (message_on_file) appended in turn.
        drafts = {d: message_on_file(str(d)) for d, _ in [(here, wb), *parts]}
        sub_msgs = [m for d, _ in parts if (m := drafts[d].strip())]
        own = {wb, *(b for _, b in parts)}
        for d, b in [(here, wb), *parts]:
            carried = [x for x, tip in listed_tips().items()
                       if x not in own and is_ancestor(tip, "HEAD" if d == here else f"refs/heads/{b}")
                       and not is_ancestor(tip, main)]
            if carried:
                die(strings.LAND_CARRIES_UNLANDED(wb=b, carried=", ".join(carried)))
            gone = submodule.orphaned(d)
            if gone:
                die(strings.LAND_CHILDREN_SUBMODULE_ORPHANED(wb=b, gone=", ".join(gone), name=d.name))
        for d, b in [(here, wb), *parts]:
            holds(b, "HEAD" if d == here else f"refs/heads/{b}", not_yet_deployed)

        tips = {d: out(["git", "rev-parse", "HEAD"], cwd=str(d)) for d, _ in [(here, wb), *parts]}

        def merged_tree(tips: dict[Path, str]) -> str:
            """The branches, each at its commit of `tips`, merged in a
            throwaway worktree on this branch's (a gitlink both moved on
            the union of its branches, as a deploy's union): its tree."""
            wt = add_worktree(tips[here])
            try:
                for d, b in parts:
                    p = git("merge", "--quiet", "--no-edit", "-m", strings.LAND_UNION_MERGE_MSG(branch=b), tips[d],
                            cwd=wt, env=MACHINE_ENV, check=False)
                    if p.returncode == 0:
                        continue
                    for f in out(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt).splitlines():
                        if f == MSG_FILE:
                            git("rm", "--quiet", "-f", f, cwd=wt, check=False)
                        elif (submodule.solve_union(Path(wt), f) if submodule.is_pin_path(Path(wt), f)
                              else solve_with_mergiraf(wt, f)):
                            git("add", "--", f, cwd=wt, check=False)
                        else:
                            git("merge", "--abort", cwd=wt, check=False)
                            die(strings.LAND_CHILD_CONFLICT(branch=b, file=f, wb=wb, label=sub_label(d), name=d.name))
                    git("commit", "--quiet", "--no-edit", "--no-verify", cwd=wt, env=MACHINE_ENV)
                return tree_less_msg("HEAD", wt)
            finally:
                drop_worktree(wt)

        def clashes(tree: str, *, pinned: bool = False) -> None:
            """Each worktree is moved onto the landed commit with its unstaged
            edits kept (take_landing), which an edited or untracked file the
            others changed would stop: told now, before anything moves.
            `pinned`: `tree` carries the landing pins, which move each
            gitlink under a submodule worktree prepare_all found fit to
            follow - a gitlink is no edit then."""
            for d in [here, *(d for d, _ in parts)]:
                diff = ["git", "diff", "--name-only", *(["--ignore-submodules=all"] if pinned else [])]
                loose = set(out(diff, cwd=str(d)).splitlines()) - {MSG_FILE}
                loose |= set(out(["git", "ls-files", "--others", "--exclude-standard"], cwd=str(d)).splitlines())
                moved = set(out(["git", "diff", "--name-only", "HEAD", tree], cwd=str(d)).splitlines())
                if clash := sorted(loose & moved):
                    die(strings.LAND_CLASH(label=d.name if d == here else sub_label(d), files=", ".join(clash)))

        # Proven to merge before any submodule's landing commit is pushed.
        clashes(merged_tree(tips))
        msg = main_message(wb, message, main, "\n\n".join(sub_msgs),
                           [(here.name, "HEAD"), *((sub_label(d), f"refs/heads/{b}") for d, b in parts)])
        for d, b in [(here, wb), *parts]:  # the edit's fetch may have brought a tip
            problem = sole_writer_problem(b)
            if problem is not None:
                die(strings.LAND_CHILD_PROBLEM(name=d.name, problem=problem))
        # As in land: nothing moves before main's push.
        landings: list[submodule.Landing] = []
        pinned: dict[Path, str] = {}
        for d, b in [(here, wb), *parts]:
            os.chdir(d)
            own = submodule.prepare_all(b, msg, landings)
            os.chdir(here)
            pinned[d] = submodule.pinned_commit(tips[d], own, d)
            landings += own
        tree = merged_tree(pinned)
        if tree == main_tree:
            die(strings.LAND_NO_CHANGES(main=main_short()))
        clashes(tree, pinned=True)
        submodule.check_pins(tree, landings, here)
        landed = Landed(out(["git", "commit-tree", tree, "-p", main, "-m", msg]), main,
                        [Part(str(d), b, tips[d], drafts[d]) for d, b in [(here, wb), *parts]], landings)
        push_main(landed)
        finish_landing(landed, locked=True)
    finally:
        for lock in locks:
            lock.close()
    if not catch_up_rest([landed]):
        die(strings.LAND_CATCH_UP_FAILED())


__all__ = [
    "close_checkout",
    "close_submodules",
    "close_worktrees",
    "enter_sub",
    "has_increments",
    "land",
    "land_with_children",
    "open_checkout",
    "open_worktree",
    "pull",
    "resume_landings",
    "status",
    "subs_of",
    "undeploy",
    "work_branch_name",
]
