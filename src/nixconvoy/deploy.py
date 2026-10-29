"""A deploy: probe, snapshot and its announcement, union (waiting for a
deploy under way that reaches the same targets), eval, precheck, build,
copy and the records' push, switch, then commit and push - in the order the
help (strings.HELP) gives."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from . import checkout, memo, proc, strings, submodule, targets
from .checkout import (
    DEPLOYED_PREFIX,
    PENDING_REFS,
    STALE_STAMP,
    WAIT_STALE,
    Announce,
    Pending,
    add_worktree,
    adopt_commit,
    announced,
    await_background_push,
    commit_deployed,
    current_branch,
    deployed_commit,
    drop_worktree,
    ensure_on_work_branch,
    ensure_rebased,
    ensure_sole_writer,
    fetch_origin,
    held_targets,
    note_pushed,
    origin_tips,
    pending_announcements,
    pop_tmp_commits,
    push_deployed,
    push_work_branch,
    record_commit,
    staged_differs,
    staged_snapshot,
    this_prefix,
    work_branch_name,
)
from .config import SwitchPolicy, cfg, main_ref, remote_ref, repo_names
from .guard import binary, decide
from .memo import Top
from .proc import PERF, Fail, die, dump_json, is_ancestor, load_json, out, rev
from .switch import SwitchedMeanwhile, provenance_record, switch
from .targets import Probe, expand_targets, flake_target_names
from .union import UnionQueries
from .wire import Context, DriftParams, UnionParams

# Seconds between the looks at origin while waiting for a deploy under way.
WAIT_POLL = 5
# How many times a run whose target another deploy switched meanwhile
# probes again and redoes its plan (memoized: seconds) before giving up.
SWITCH_REDOS = 2


def news_of(p: Pending, now: float, fetched: float, seen: dict[str, tuple[str, float]],
            dead: set[str]) -> bool:
    """Whether `p`'s deploy is alive by its renewals: `seen` keeps each
    key's sha as last seen and when it changed (the caller's monotonic
    clock, `now`, as of a successful fetch).  One first seen with its last
    renewal STALE_STAMP old by its own stamp against the fetch's wall time
    `fetched` (0.0: no fetch yet, the rule skipped), or unchanged for
    WAIT_STALE, is dead (`dead`, said once) until it changes again - a
    re-run from that checkout, said too."""
    sha, changed = seen.get(p.key, ("", now))
    first = not sha
    if sha != p.sha:
        seen[p.key] = (p.sha, now)
        changed = now
        if p.key in dead:
            print(strings.DEPLOY_ALIVE_AGAIN(key=p.key), flush=True)
            dead.discard(p.key)
    if p.key in dead:
        return False
    if first and fetched and fetched - p.at > STALE_STAMP:
        print(strings.DEPLOY_STALE_STAMP(key=p.key, minutes=(fetched - p.at) / 60), flush=True)
    elif now - changed > WAIT_STALE:
        print(strings.DEPLOY_NO_NEWS(key=p.key, secs=WAIT_STALE), flush=True)
    else:
        return True
    dead.add(p.key)
    return False


def exit_on_signal(signum: int, _frame: object) -> None:
    """SIGTERM and SIGHUP end the run: the children killed, as by die(),
    and SystemExit raised, so a deploy's finally clauses (the
    announcement's withdrawal) still run.  A switch armed by then is left
    to its rollback timer."""
    proc.kill_children(strings.DEPLOY_KILLED_BY_SIGNAL(signum=signum))
    raise SystemExit(128 + signum)


class Run:
    """What deploy() keeps of run_deploy past its end: the announcement,
    which run_deploy makes itself for a deploy with nothing pending once
    it has targets to switch, for their records to ride."""

    def __init__(self, ann: Announce | None) -> None:
        self.ann = ann


def claims(provenance: str, me: str, branch: str) -> bool:
    """Whether a target's provenance is this checkout's announced, unpushed
    increment of `branch`."""
    try:
        doc = load_json(provenance)
    except ValueError:
        return False
    if not isinstance(doc, dict):
        return False
    pend, by = doc.get("pending"), doc.get("by")
    return (isinstance(pend, dict) and isinstance(by, dict) and bool(pend.get("announce"))
            and pend.get("branch") == branch
            and (by.get("machine"), by.get("checkout")) == (me, str(Path.cwd())))


def by_me(provenance: str, me: str) -> bool:
    """Whether a target's provenance was written by a deploy from this
    checkout."""
    try:
        doc = load_json(provenance)
    except ValueError:
        return False
    by = doc.get("by") if isinstance(doc, dict) else None
    return isinstance(by, dict) and (by.get("machine"), by.get("checkout")) == (me, str(Path.cwd()))


def previous_record(provenance: str) -> str:
    """The deploy record a target's provenance names, "" when none."""
    try:
        doc = load_json(provenance)
    except ValueError:
        return ""
    rec = doc.get("record") if isinstance(doc, dict) else None
    return rec if isinstance(rec, str) else ""


def increment_reaches(snap: str, all_targets: list[str]) -> bool:
    """Whether the staged increment `snap` (staged_snapshot) changes
    something a target's evaluation reads - a path of the eval memo's read
    scan over the snapshot, or a lock entry of an input those files name -
    by the scan alone, no eval; one that cannot tell reaches every target."""
    wt = add_worktree(snap)
    try:
        return bool(memo.reached_between(wt, snap, all_targets, "HEAD", snap))
    finally:
        drop_worktree(wt)


def commit_unreached(wb: str, msg: str, snap: str) -> None:
    """The increment `snap`, reaching no target: committed onto `wb` and
    pushed, after the precheck unless its tree has a proven pass (no build
    gates it), with nothing evaluated, built or switched - the probes
    started are dropped unread: the targets have nothing to catch up on
    from it."""
    if memo.precheck_unproven(snap):
        targets.check_builder()
        wt = add_worktree(snap)
        try:
            print(strings.DEPLOY_RUNNING_PRECHECK)
            err = memo.precheck_gate(wt, snap)
        finally:
            drop_worktree(wt)
        if err:
            die(strings.PROC_WITH_NOTHING_DONE(problem=err))
    PERF.mark("commit-push")
    commit_deployed(wb, snap, f"{msg}\n\nReaches: none\nDeployed: none")
    push_work_branch(wb)
    print(strings.DEPLOY_REACHES_NONE(wb=wb))


def deploy(filter_targets: list[str], msg: str | None, evict: bool, dry: bool = False) -> None:
    """`msg` describes the staged increment (default: the checkout's name
    and the increment's number on its branch); with nothing staged the
    deploy converges the targets on the union as it is.
    `evict`: this checkout's view alone - no other work branch in the
    union, no wait for a deploy under way, and a target running another
    checkout's refused work switched over when this work reaches it or
    `filter_targets` names it.
    `dry`: stop after the dry activation of every target that would be
    switched, printing the units it would stop, restart or reload; nothing
    is switched, committed or pushed."""
    skip = os.environ.get("NIXCONVOY_SKIP_SWITCH") == "1"
    all_targets = flake_target_names()
    filter_targets = expand_targets(filter_targets, all_targets)
    me = proc.this_machine()

    candidates = filter_targets or list(all_targets)

    await_background_push()
    main0 = rev(main_ref())
    # Non-candidates are probed best-effort: the verdict reports their
    # drift, and a hold on one is read off its provenance.
    probe_names: list[str] = [] if skip else list(all_targets)
    pool = ThreadPoolExecutor(1 + len(probe_names) + len(submodule.present(Path.cwd())))
    fetch = pool.submit(fetch_origin, "--no-auto-gc")
    if not dry:  # a dry activation brings no submodule along
        submodule.prefetch(Path.cwd(), pool)
    probes = {n: pool.submit(targets.probe, n) for n in probe_names}
    pool.shutdown(wait=False)

    pop_tmp_commits()
    wb = work_branch_name()
    cur = current_branch()
    if wb is not None and cur not in (wb, cfg().main_branch, None):
        # A subagent worktree's fresh branch: the provenance must name wb.
        had = rev(f"refs/heads/{wb}") is not None
        up = rev(remote_ref(wb))
        if not had and up is not None and not is_ancestor(up, main_ref()):
            # Recovered from origin only while empty there (a removed
            # worktree's): increments there are another worktree's of
            # this name (a machine sharing the prefix).
            die(strings.DEPLOY_FOREIGN_INCREMENTS(remote=cfg().remote, wb=wb, main_ref=main_ref(),
                                                  remote_wb=remote_ref(wb)))
        ensure_on_work_branch(wb)
        if not had and (made := rev(f"refs/heads/{wb}")) is not None:
            note_pushed(wb, made)  # the branch as taken from origin's empty copy, or fresh
    elif wb is None and cur not in (cfg().main_branch, None):
        die(strings.DEPLOY_NOT_WORK_BRANCH(branch=cur, prefix=this_prefix(), main_branch=cfg().main_branch))

    def increment_name() -> str:
        n = int(out(["git", "rev-list", "--count", f"{main_ref()}..HEAD"])) + 1
        return strings.DEPLOY_INCREMENT_NAME(checkout=Path.cwd().name, n=n)

    if msg is None and wb is not None and not dry and (
            staged_differs() or any(submodule.staged(sub.dir, less_msg=True) for sub in submodule.present(Path.cwd()))):
        msg = increment_name()
    snap = ""
    PERF.mark("submodules")
    for told in submodule.sync(wb, msg, dry=dry):
        print(told)
    if msg is None and wb is not None and not dry and staged_differs():
        # A pin the submodule step staged on its own (a catch-up) is an
        # increment too.
        msg = increment_name()
    PERF.mark("snapshot")
    if staged_differs():
        if wb is None:
            die(strings.DEPLOY_STAGED_ON_MAIN)
        ensure_on_work_branch(wb)  # the increment must land (and union) on wb
        # A snapshot's tree is HEAD + staged, so one taken on a stale HEAD
        # would commit main's changes away.
        fetch.result()
        ensure_sole_writer(wb)
        ensure_rebased()
        snap = staged_snapshot()
        # An increment that changes nothing any target's evaluation reads
        # is committed without a deploy, the targets left to the next
        # deploy reaching one; naming targets asks for a deploy.
        PERF.mark("reach")
        if msg and not (dry or skip or evict or filter_targets) and not increment_reaches(snap, all_targets):
            # The probes are never collected: an ssh to a dark target must
            # not hold the exit.
            proc.kill_children(strings.DEPLOY_REACHES_NO_TARGET)
            commit_unreached(wb, msg, snap)
            return
    elif msg:
        die(strings.DEPLOY_NOTHING_STAGED)
    else:
        if wb is not None and current_branch() == wb:  # a detached subagent worktree commits nothing onto it
            fetch.result()
            ensure_sole_writer(wb)
        ensure_rebased()

    # The pending state the targets' provenances will name - the increment, or
    # a HEAD origin lacks (the nightly's lock commit on main) - is announced
    # for as long as this run lives.
    ann: Announce | None = None
    own = current_branch() or cfg().main_branch  # detached: at origin/main (ensure_rebased)
    # A work branch holding targets back may end a hold with an empty
    # increment (the stage below): announced like any increment.
    if not dry and not skip and (snap or rev("HEAD") != rev(remote_ref(own))
                                 or (wb is not None and own == wb
                                     and held_targets(f"{main_ref()}..HEAD"))):
        ann = Announce(own, snap or "HEAD")
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, exit_on_signal)
    pushed = False
    run = Run(ann)
    try:
        pushed = run_deploy(filter_targets, candidates, all_targets, msg, evict, dry, skip,
                             me, main0, wb, snap, run, fetch, probes)
    finally:
        lazy = run.ann is not ann
        ann = run.ann
        if ann is not None and not pushed:
            if ann.claimed and lazy:
                # Those targets run (or may run) the records it carries:
                # kept, for their history to chain on them.
                ann.stop()
                print(strings.DEPLOY_ANNOUNCEMENT_KEPT_RECORDS(targets=", ".join(ann.claimed), remote=cfg().remote,
                                                              pending_refs=PENDING_REFS, key=ann.key),
                      file=sys.stderr)
            elif ann.claimed:
                # Those targets run the increment (or a switch onto it was
                # started): the announcement stays, dead, so a deploy
                # elsewhere is refused there rather than reverting it,
                # until a re-run here ends with the push.
                ann.stop()
                print(strings.DEPLOY_ANNOUNCEMENT_KEPT_INCREMENT(targets=", ".join(ann.claimed), remote=cfg().remote,
                                                                pending_refs=PENDING_REFS, key=ann.key),
                      file=sys.stderr)
            else:
                ann.withdraw()


def run_deploy(filter_targets: list[str], candidates: list[str], all_targets: list[str],
               msg: str | None, evict: bool, dry: bool, skip: bool, me: str,
               main0: str | None, wb: str | None, snap: str, run: Run,
               fetch: Future[subprocess.CompletedProcess[str]], probes: dict[str, Future[Probe]]) -> bool:
    """The deploy from its union on: whether its announcement is left to
    the push that ends it (push_deployed), rather than to withdraw."""
    ann = run.ann
    running: dict[str, str | None] = {}
    probed: dict[str, Probe] = {}

    def collect_probes() -> None:
        nonlocal running, probed
        probed = {n: f.result() for n, f in probes.items()}
        running = {n: p.running for n, p in probed.items()}
        unreachable = [h for h in candidates if running.get(h) is None]
        if unreachable:
            die(strings.DEPLOY_UNREACHABLE(targets=", ".join(unreachable)))
        if ann is not None:
            # A target running this checkout's announced increment from
            # an earlier run that ended without its push: still its claim.
            for h, pr in probed.items():
                if h not in ann.claimed and claims(pr.provenance, me, ann.branch):
                    ann.claimed.append(h)

    if not skip:
        PERF.mark("probe")
        collect_probes()
        targets.check_builder()

    plan: list[str] = []
    # Each built target's switch policy (the union's).
    policies: dict[str, SwitchPolicy] = {}
    affected: list[str] = []
    left_alone: list[str] = []
    tops: dict[str, Top] = {}
    committed = ""
    records: dict[str, str] = {}
    provenances: dict[str, str] = {}

    def refresh() -> None:
        nonlocal snap
        ensure_rebased()
        if snap:
            snap = staged_snapshot()
        if ann is not None:
            ann.retarget(snap or "HEAD")

    # The announcements (by key) whose deploy is dead as far as this run
    # can tell (news_of: a stale stamp at first sight, or no renewal for
    # WAIT_STALE seconds while waited for): told to the drift core, which
    # names the dead deploy when it refuses a target running its increment.
    dead: set[str] = set()
    # Each announcement as last seen, and when it last changed (this
    # machine's clock, as of a successful fetch): across the waits of a
    # run, so a dead key seen again unchanged stays dead.
    seen: dict[str, tuple[str, float]] = {}

    def await_pending(wt: str, union: str) -> bool:
        """The union core's AwaitPending (step 4a): while another checkout's
        deploy is under way (its announcement) and its increment reaches a
        candidate this checkout's own work reaches too (memo.reached_between
        on `union`, checked out in `wt`), wait for it to end rather than
        build without it and be refused (or rebuild) once it switched.
        Only deploys announced before this one are waited for, so two never
        wait for each other; one whose announcement was last renewed
        STALE_STAMP seconds before the fetch that first showed it, or is not
        renewed for WAIT_STALE seconds of successful looks at origin, is
        taken for dead (until it is renewed again: a re-run); one settled
        (over, its push pending) is not waited for.
        Whether it waited: the targets are then probed again, and the union
        redone with what landed."""
        nonlocal probes
        if evict or skip:
            return False
        own = ann.key if ann is not None else None
        rank = (ann.since, ann.branch) if ann is not None else None
        mine: list[str] | None = None
        told: set[str] = set()
        waited = False
        started = status_at = time.monotonic()
        while True:
            waiting: dict[str, list[str]] = {}
            now, fetched = time.monotonic(), checkout.FETCHED_AT
            for p in pending_announcements():
                if p.key == own or p.settled:
                    continue
                # Judged whatever its rank: a dead one the drift core names.
                if not news_of(p, now, fetched, seen, dead) or (rank is not None and (p.since, p.branch) >= rank):
                    continue
                if mine is None:
                    mine = sorted(memo.reached_targets(
                        wt, union, candidates, snap or "HEAD",
                        {h: probed[h].provenance for h in candidates if h in probed}))
                # The announced state against what the union has of the
                # branch: its parent (the announcer's HEAD) when origin has
                # that, else origin's tip of the branch - a wider diff, so
                # main's changes count too, but never a narrower one.
                tip = rev(remote_ref(p.branch))
                base = f"{p.sha}^" if tip is None or is_ancestor(f"{p.sha}^", tip) else tip
                both = memo.reached_between(wt, union, mine, base, p.sha)
                if both:
                    waiting[p.key] = sorted(both)
            if not waiting:
                break
            for b, hs in waiting.items():
                if b not in told:
                    told.add(b)
                    print(strings.DEPLOY_WAITING(key=b, targets=", ".join(hs)), flush=True)
            if now - status_at >= 60:
                status_at = now
                print(strings.DEPLOY_STILL_WAITING(keys=", ".join(waiting), minutes=(now - started) / 60),
                      flush=True)
            waited = True
            # Announcements are judged as of a successful look at origin:
            # a blip here must not make a live deploy look dead.
            failed_polls = 0
            while True:
                time.sleep(WAIT_POLL)
                if fetch_origin("--no-auto-gc", check=False).returncode == 0:
                    break
                failed_polls += 1
                if failed_polls >= 12:
                    die(strings.DEPLOY_REMOTE_SILENT(remote=cfg().remote))
        if waited:
            print(strings.DEPLOY_WAIT_ENDED, flush=True)
            PERF.mark("probe")
            with ThreadPoolExecutor(max(1, len(probes))) as pool:
                probes = {n: pool.submit(targets.probe, n) for n in probes}
            collect_probes()
        return waited

    def stage(wt: str, union: str, listed: list[str], branches: list[tuple[str, str]]) -> bool:
        """Eval, precheck, build and copy for `union`, checked out in `wt`,
        the work branches shaped like `listed` (nixconvoy.toml's branch_prefix), `branches`
        the ones it merged: whether the deploy ends here."""
        nonlocal plan, affected, left_alone, tops, committed, records, provenances, ann, policies
        if skip:
            if dry:
                print(strings.DEPLOY_SKIP_DRY(union=union))
                return True
            if wb is not None:
                if snap:
                    commit_deployed(wb, snap, msg or "")
                push_work_branch(wb)
            print(strings.DEPLOY_SKIP(union=union))
            return True

        if ann is not None and not ann.collect():
            die(strings.DEPLOY_ANNOUNCE_FAILED(remote=cfg().remote, detail=ann.detail))
        PERF.mark("eval")
        with memo.memo_lock(memo.memo_dir()):
            memo.memo_prune(memo.memo_dir())
        # The precheck gates the union, not the increment: a branch rebased
        # by pull/land, a skip-switch push or a merge of two passing
        # branches can each carry a violation no increment introduced.
        check: Future[str | None] | None = None
        if memo.precheck_unproven(union):
            check_pool = ThreadPoolExecutor(1)
            check = check_pool.submit(memo.precheck_gate, wt, union)
            check_pool.shutdown(wait=False)
        print(strings.DEPLOY_EVALUATING if check is None else strings.DEPLOY_EVALUATING_PRECHECK)
        tops = memo.eval_toplevels(wt, union, [h for h in all_targets if running.get(h) is not None],
                                   check)
        if check is not None:
            err = check.result()
            if err:
                die(strings.PROC_WITH_NOTHING_DONE(problem=err))
        reached = memo.reached_targets(wt, union, sorted(tops), snap or "HEAD",
                                     {h: probed[h].provenance for h in tops if h in probed})
        # The targets the other unioned branches hold back (held_targets):
        # each ran its increments to other targets alone - one already
        # running the branch's tip (its provenance) is not held by it.
        held: dict[str, list[str]] = {}
        for name, tip in branches:
            for h in held_targets(f"{main_ref()}..{tip}"):
                if h in probed and memo.recorded_tip(probed[h].provenance, name) == tip:
                    continue
                held.setdefault(h, []).append(name)
        policy = memo.union_config(wt).switch_policy
        policies = {h: policy(h) for h in tops}
        for h in sorted(tops):
            if tops[h].assessed and not tops[h].kit:
                print(strings.DEPLOY_ASSESSED_PLAIN(target=h), file=sys.stderr)
        cx = Context(names=repo_names(), me=(me, str(Path.cwd())), listed=sorted(listed), tips=origin_tips(),
                     in_flight=announced(ann.key if ann is not None else None), dead=sorted(dead),
                     tz_offset=time.localtime().tm_gmtoff)
        verdict = decide(DriftParams(
            cx=cx, union=union, targets=all_targets, candidates=candidates,
            evict=evict, named=bool(filter_targets), work_branch=wb is not None, reached=sorted(reached),
            running=running, built={h: t.out for h, t in tops.items()},
            held={h: sorted(bs) for h, bs in held.items()},
            provenances={h: p.provenance for h, p in probed.items()}))
        plan, affected = verdict.plan, verdict.affected
        left_alone = [h for h, (kind, _) in verdict.drift.items() if kind == "left-alone"]

        # The target fell back from this very toplevel: switching onto it
        # would run a configuration known not to boot.
        for name in plan:
            if not tops[name].assessed:
                continue
            stub = targets.rejected_generation(name, tops[name].out)
            if stub:
                die(strings.DEPLOY_REJECTED_GENERATION(target=name, stub=stub))

        # Nothing pending, but targets to switch: announced now, for the
        # records to ride (and the provenance to name).
        if ann is None and plan and not dry:
            ann = run.ann = Announce(current_branch() or cfg().main_branch, "HEAD")
            if not ann.collect():
                die(strings.DEPLOY_ANNOUNCE_FAILED(remote=cfg().remote, detail=ann.detail))

        # The increment's commit and each target's record, before the
        # switch: the provenance the targets get names the commit origin will
        # hold, and the records reach origin before anything runs them
        # (carried by the announcement).
        # The commit's trailers: Reaches, the targets the increment reaches
        # among those deployable from here; Deployed, the probed targets
        # running the branch's tip once this deploy is over (switched now,
        # or up to date with the union already, a non-candidate too: every
        # reachable target was evaluated) - a target in the first and not
        # the second is held back (held_targets) until a deploy here
        # switches it.
        committed = ""
        subject = cfg().main_branch + "".join(strings.DEPLOY_SUBJECT_BRANCH(branch=n) for n, _ in branches)
        fmt = ", ".join
        deployed = sorted(h for h in tops if h in plan or h not in affected)
        if snap and not dry:
            assert wb is not None  # a snapshot needs the work branch (deploy)
            reaches = sorted(memo.reached_between(wt, union, all_targets,
                                                  "HEAD", snap))
            full = (f"{msg}\n\nReaches: {fmt(reaches) or 'none'}"
                    f"\nDeployed: {fmt(deployed) or 'none'}")
            committed = deployed_commit(snap, full)
            subject = (msg or "").partition("\n")[0] or subject
        elif wb is not None and not dry and current_branch() == wb:
            # No increment, but a target this branch held back now runs its
            # tip: an empty increment records the hold's end, as a hold is
            # read from the branch's trailers alone.
            ends = sorted(held_targets(f"{main_ref()}..HEAD") & set(deployed))
            if ends:
                subject = strings.DEPLOY_HOLD_ENDED_SUBJECT(targets=fmt(ends))
                committed = deployed_commit("HEAD", f"{subject}\n\nReaches: none\nDeployed: {fmt(deployed)}")
        doc = provenance_record(union, branches, committed, ann.sha if ann else "", ann.key if ann else "")
        records, provenances = {}, {}
        if not dry:
            for h in plan:
                body = dump_json({**doc, "toplevel": tops[h].out})
                parents = [previous_record(probed[h].provenance), rev(main_ref()) or "",
                           *(t for _, t in branches), committed]
                records[h] = record_commit(h, union, parents, subject, body)
                provenances[h] = dump_json({**doc, "toplevel": tops[h].out, "record": records[h]})
            # A target an earlier run from this checkout switched without
            # its push landing (died, or its push failed) runs a record its
            # history branch lacks: carried and pushed with this run's, so
            # the branch chains on it - whether or not this run switches it.
            for h, pr in probed.items():
                r = previous_record(pr.provenance)
                if h not in records and r and by_me(pr.provenance, me) and rev(f"{r}^{{commit}}") \
                        and r != rev(remote_ref(f"{DEPLOYED_PREFIX}/{h}")):
                    records[h] = r

        # A candidate up to date (running the union's toplevel, not left
        # alone) whose rooted kit is not the one this eval made (its policy
        # or the kit sources moved): the kit alone is refreshed, no switch -
        # so a change to a target's checks lands, and a boot's assessment
        # runs the current ones.
        refresh = [h for h in sorted(tops) if h in candidates and h not in plan and h not in left_alone
                   and running.get(h) == tops[h].out and probed[h].kit != tops[h].kit]
        if dry and refresh:
            print(strings.DEPLOY_DRY_KIT_REFRESH(targets=", ".join(refresh)))
            refresh = []
        if plan or refresh:
            b = targets.builder_host()
            where = strings.DEPLOY_BUILD_WHERE(host=b) if b else ""
            PERF.mark("build")
            what = ", ".join([*plan, *(strings.DEPLOY_BUILD_KIT(target=h) for h in refresh)])
            print(strings.DEPLOY_BUILDING(what=what, where=where))
            err = targets.build_drvs([d for h in plan for d in (tops[h].drv, tops[h].kit_drv) if d]
                                     + [tops[h].kit_drv for h in refresh if tops[h].kit_drv],
                                     strings.DEPLOY_BUILD_OF(what=what))
            if err:
                die(strings.PROC_WITH_NOTHING_DONE(problem=err))

        # Another machine may have pushed a work branch while we built;
        # switching without it would revert its deployed changes (the union
        # core compares once this returns).
        PERF.mark("copy")

        def copy_then_dry(name: str, top: Top, quiet: bool) -> str | None:
            targets.copy_closure(name, [top.out, *([top.kit] if top.kit else [])], quiet)
            return targets.dry_activate(name, top.out) if dry else None

        if dry and plan:
            print(strings.DEPLOY_DRY_ACTIVATING(targets=", ".join(plan)))
        with ThreadPoolExecutor(2 + len(plan) + len(refresh)) as pool:
            refetch = pool.submit(fetch_origin)
            # Carried, or a redone stage's stale records dropped.
            pushed = pool.submit(ann.carry, records) if ann is not None and (records or ann.records) else None
            copies = [pool.submit(copy_then_dry, n, tops[n], len(plan) > 1) for n in plan]
            refreshes = [pool.submit(targets.refresh_kit, h, tops[h].out, tops[h].kit) for h in refresh]
            failures = [r for r in (cp.result() for cp in copies) if r]
            # The old kit stays rooted there: the next deploy tries again.
            for err in (r.result() for r in refreshes):
                if err:
                    print(strings.DEPLOY_KIT_REFRESH_FAILED(err=err), file=sys.stderr)
            refetch.result()
            unpushed = pushed.result() if pushed is not None else None
        if dry:
            if failures:
                die(strings.DEPLOY_DRY_FAILED(failures="\n".join(failures)))
            if plan:
                print(strings.DEPLOY_DRY_WOULD_SWITCH(targets=", ".join(plan), union=union))
            elif left_alone:
                print(strings.DEPLOY_DRY_LEFT_ALONE(targets=", ".join(left_alone), union=union))
            else:
                print(strings.DEPLOY_DRY_NO_DRIFT(union=union))
            return True
        if unpushed:
            die(strings.DEPLOY_RECORDS_ANNOUNCE_FAILED(remote=cfg().remote,
                                                       detail=unpushed))
        return False

    def switch_all(nixconvoy_switch: str) -> None:
        """The plan's switches, all at once.  A failed switch dies once the
        others finish; one refused because the target moved meanwhile
        (SwitchedMeanwhile) is raised once none failed, the moved targets'
        claims taken back."""
        if not plan:
            return
        expected: dict[str, str] = {}
        for name in plan:
            r = running.get(name)
            assert r is not None  # unreachable targets never enter the plan
            expected[name] = r
        PERF.mark("switch:" + "+".join(plan))
        # Claimed before the switch starts: a switch cut short (a signal,
        # a sibling's die) may still leave the target running it.
        if ann is not None:
            ann.claimed += [n for n in plan if n not in ann.claimed]
        with ThreadPoolExecutor(len(plan)) as pool:
            switches = [pool.submit(switch, n, expected[n], tops[n].out, tops[n].kit, nixconvoy_switch,
                                    len(plan) > 1, provenances[n], probed[n].digest, policies[n].has("routing"))
                        for n in plan]
        moved: list[SwitchedMeanwhile] = []
        failed: Fail | None = None
        for f in switches:
            try:
                f.result()
            except SwitchedMeanwhile as e:
                moved.append(e)
            except Fail as e:
                failed = failed or e
        if moved and ann is not None:
            ann.claimed = [h for h in ann.claimed if h not in {e.target for e in moved}]
        if failed is not None:
            raise failed
        if moved:
            raise moved[0]

    for redo in range(SWITCH_REDOS + 1):
        queries = UnionQueries(lambda: snap or "HEAD", fetch, refresh, stage, await_pending)
        try:
            done = decide(UnionParams(repo_names(), wb or "", evict, main0), queries)
            if skip or dry:
                return False
            union = done.union
            try:
                switch_all(binary("nixconvoy-switch") if plan else "")
            except SwitchedMeanwhile as e:
                if redo == SWITCH_REDOS:
                    die(strings.DEPLOY_MOVED_TOO_OFTEN(moved=e, times=redo + 1))
                # Its deploy's push may still be on its way: the union redone
                # with what origin has by then (await_pending waits for a
                # deploy still announced), the targets probed afresh.
                print(strings.DEPLOY_MOVED_REDO(moved=e), flush=True)
                PERF.mark("probe")
                with ThreadPoolExecutor(1 + len(probes)) as pool:
                    fetch = pool.submit(fetch_origin)
                    probes = {n: pool.submit(targets.probe, n) for n in probes}
                collect_probes()
                continue
        finally:
            queries.drop()
        break

    PERF.mark("commit-push")
    if committed:
        assert wb is not None
        adopt_commit(wb, committed)
    # A local main ahead of origin is pushed by its owner once every
    # target it affects has switched.
    problem = push_deployed(wb, ann, records)

    if plan:
        print(strings.DEPLOY_DONE(targets=", ".join(plan), union=union))
    elif left_alone:
        print(strings.DEPLOY_LEFT_ALONE(targets=", ".join(left_alone), union=union))
    else:
        print(strings.DEPLOY_NO_DRIFT(union=union))
    if problem is not None:
        die(strings.DEPLOY_UNPUSHED(wb=wb, problem=problem))
    return True
