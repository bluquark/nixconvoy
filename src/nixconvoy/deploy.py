"""A deploy: probe, snapshot and its announcement, union (waiting for a
deploy under way that reaches the same targets), eval, precheck, build,
copy and the records' push, switch, then commit and push - in the order the
module docstring of cli.py gives."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from . import memo, proc, submodule, targets
from .checkout import (
    PENDING_REFS,
    WAIT_STALE,
    Announce,
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
    fetch_args,
    held_targets,
    origin_tips,
    pending_announcements,
    pop_tmp_commits,
    push_deployed,
    push_records,
    push_work_branch,
    record_commit,
    staged_differs,
    staged_snapshot,
    work_branch,
)
from .config import builder
from .guard import binary, decide
from .memo import Top
from .proc import NOTHING_DONE, PERF, die, dump_json, git, is_ancestor, load_json, out, rev
from .switch import lineage_record, switch
from .targets import Probe, expand_targets, flake_target_names
from .union import UnionQueries

# Seconds between the looks at origin while waiting for a deploy under way.
WAIT_POLL = 5


def exit_on_signal(signum: int, _frame: object) -> None:
    """SIGTERM and SIGHUP end the run: the children killed, as by die(),
    and SystemExit raised, so a deploy's finally clauses (the
    announcement's withdrawal) still run.  A switch armed by then is left
    to its rollback timer."""
    proc.kill_children(f"killed by signal {signum}")
    raise SystemExit(128 + signum)


def claims(lineage: str, me: str, branch: str) -> bool:
    """Whether a target's lineage is this checkout's announced, unpushed
    increment of `branch`."""
    try:
        doc = load_json(lineage)
    except ValueError:
        return False
    if not isinstance(doc, dict):
        return False
    pend, by = doc.get("pending"), doc.get("by")
    return (isinstance(pend, dict) and isinstance(by, dict) and bool(pend.get("announce"))
            and pend.get("branch") == branch
            and (by.get("machine"), by.get("checkout")) == (me, str(Path.cwd())))


def previous_record(lineage: str) -> str:
    """The deploy record a target's lineage names, "" when none."""
    try:
        doc = load_json(lineage)
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
            print("running the precheck...")
            err = memo.precheck_gate(wt, snap)
        finally:
            drop_worktree(wt)
        if err:
            die(f"{err}; {NOTHING_DONE}")
    PERF.mark("commit-push")
    commit_deployed(wb, snap, f"{msg}\n\nReaches: none\nApplied: none")
    push_work_branch(wb)
    print(f"reaches no target: committed onto {wb} and pushed; nothing built or switched (the probes' results dropped)")


def deploy(filter_targets: list[str], msg: str | None, evict: bool,
           allow: frozenset[str] = frozenset(), dry: bool = False) -> None:
    """`msg` describes the staged increment (default: the checkout's name
    and the increment's number on its branch); with nothing staged the
    deploy converges the targets on the union as it is.
    `evict`: this checkout's view alone - no other work branch in the
    union, no wait for a deploy under way, and a target running another
    checkout's refused work switched over when this work reaches it or
    `filter_targets` names it.
    `allow`: the protected groups (nixconvoy.protected.<name>) a switch may
    stop, restart or reload units of, this run.
    `dry`: stop after the dry activation of every target that would be
    switched, printing the units it would stop, restart or reload; nothing
    is switched, committed or pushed."""
    skip = os.environ.get("NIXCONVOY_SKIP_SWITCH") == "1"
    all_targets = flake_target_names()
    filter_targets = expand_targets(filter_targets, all_targets)
    me = proc.this_machine()

    candidates = filter_targets or list(all_targets)

    await_background_push()
    main0 = rev("refs/remotes/origin/main")
    # Non-candidates are probed best-effort: the verdict reports their
    # drift, and a hold on one is read off its lineage.
    probe_names: list[str] = [] if skip else list(all_targets)
    pool = ThreadPoolExecutor(1 + len(probe_names) + len(submodule.present(Path.cwd())))
    fetch = pool.submit(git, *fetch_args("--no-auto-gc"))
    if not dry:  # a dry activation brings no submodule along
        submodule.prefetch(Path.cwd(), pool)
    probes = {n: pool.submit(targets.probe, n) for n in probe_names}
    pool.shutdown(wait=False)

    pop_tmp_commits()
    wb = work_branch()
    cur = current_branch()
    if wb is not None and cur not in (wb, "main", None):
        # A subagent worktree's fresh branch: the lineage must name wb.
        ensure_on_work_branch(wb)
    elif wb is None and cur not in ("main", None):
        die(f"on branch {cur}, which deploy-branches does not list as a work branch; the "
            "main checkout deploys from main (a hand commit there is claimed as main's "
            "until pushed) or parked on origin/main")

    def increment_name() -> str:
        n = int(out(["git", "rev-list", "--count", "refs/remotes/origin/main..HEAD"])) + 1
        return f"{Path.cwd().name}: increment {n}"

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
            die("staged changes in the main checkout, which has no work branch: commit "
                "them by hand here, or stage them in a checkout on a work branch")
        ensure_on_work_branch(wb)  # the increment must land (and union) on wb
        # A snapshot's tree is HEAD + staged, so one taken on a stale HEAD
        # would commit main's changes away.
        fetch.result()
        ensure_rebased()
        snap = staged_snapshot()
        # An increment that changes nothing any target's evaluation reads
        # is committed without a deploy, the targets left to the next
        # deploy reaching one; naming targets asks for a deploy.
        PERF.mark("reach")
        if msg and not (dry or skip or evict or filter_targets) and not increment_reaches(snap, all_targets):
            # The probes are never collected: an ssh to a dark target must
            # not hold the exit.
            proc.kill_children("the increment reaches no target")
            commit_unreached(wb, msg, snap)
            return
    elif msg:
        die("nothing is staged; stage the change or drop -m")
    else:
        ensure_rebased()

    # The pending state the targets' lineages will name - the increment, or
    # a HEAD origin lacks (the nightly's lock commit on main) - is announced
    # for as long as this run lives.
    ann: Announce | None = None
    own = current_branch() or "main"  # detached: at origin/main (ensure_rebased)
    # A work branch holding targets back may end a hold with an empty
    # increment (the stage below): announced like any increment.
    if not dry and not skip and (snap or rev("HEAD") != rev(f"refs/remotes/origin/{own}")
                                 or (wb is not None and own == wb
                                     and held_targets("refs/remotes/origin/main..HEAD"))):
        ann = Announce(own, snap or "HEAD")
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, exit_on_signal)
    pushed = False
    try:
        pushed = run_deploy(filter_targets, candidates, all_targets, msg, evict, allow, dry, skip,
                             me, main0, wb, snap, ann, fetch, probes)
    finally:
        if ann is not None and not pushed:
            if ann.claimed:
                # Those targets run the increment (or a switch onto it was
                # started): the announcement stays, dead, so a deploy
                # elsewhere is refused there rather than reverting it,
                # until a re-run here ends with the push.
                ann.stop()
                print(f"note: {', '.join(ann.claimed)} run this checkout's unpushed increment "
                      f"(or a switch onto it was started), so its announcement stays: re-run "
                      f"to finish the deploy, or clear it to have the increment reverted by the "
                      f"next deploy there (git push --force origin "
                      f"origin/main:{PENDING_REFS}/{ann.key})", file=sys.stderr)
            else:
                ann.withdraw()


def run_deploy(filter_targets: list[str], candidates: list[str], all_targets: list[str],
               msg: str | None, evict: bool, allow: frozenset[str], dry: bool, skip: bool, me: str,
               main0: str | None, wb: str | None, snap: str, ann: Announce | None,
               fetch: Future[subprocess.CompletedProcess[str]], probes: dict[str, Future[Probe]]) -> bool:
    """The deploy from its union on: whether its announcement is left to
    the push that ends it (push_deployed), rather than to withdraw."""
    running: dict[str, str | None] = {}
    probed: dict[str, Probe] = {}

    def collect_probes() -> None:
        nonlocal running, probed
        probed = {n: f.result() for n, f in probes.items()}
        running = {n: p.running for n, p in probed.items()}
        unreachable = [h for h in candidates if running.get(h) is None]
        if unreachable:
            die(f"unreachable: {', '.join(unreachable)}; {NOTHING_DONE}; "
                f"fix connectivity (or restrict targets) and re-run")
        if ann is not None:
            # A target running this checkout's announced increment from
            # an earlier run that ended without its push: still its claim.
            for h, pr in probed.items():
                if h not in ann.claimed and claims(pr.lineage, me, ann.branch):
                    ann.claimed.append(h)

    if not skip:
        PERF.mark("probe")
        collect_probes()
        targets.check_builder()

    plan: list[str] = []
    affected: list[str] = []
    left_alone: list[str] = []
    tops: dict[str, Top] = {}
    committed = ""
    records: dict[str, str] = {}
    lineages: dict[str, str] = {}

    def refresh() -> None:
        nonlocal snap
        ensure_rebased()
        if snap:
            snap = staged_snapshot()
        if ann is not None:
            ann.retarget(snap or "HEAD")

    # The announcements (by key) whose deploy is dead as far as this run
    # can tell (no renewal for WAIT_STALE seconds while waited for): told
    # to the drift core, which names the dead deploy when it refuses a
    # target running its increment.
    dead: set[str] = set()

    def await_pending(wt: str, union: str) -> bool:
        """The union core's AwaitPending (step 4a): while another checkout's
        deploy is under way (its announcement) and its increment reaches a
        candidate this checkout's own work reaches too (memo.reached_between
        on `union`, checked out in `wt`), wait for it to end rather than
        build without it and be refused (or rebuild) once it switched.
        Only deploys announced before this one are waited for, so two never
        wait for each other; one whose announcement is not renewed for
        WAIT_STALE seconds of successful looks at origin is taken for dead;
        one settled (over, its push pending) is not waited for.
        Whether it waited: the targets are then probed again, and the union
        redone with what landed."""
        nonlocal probes
        if evict or skip:
            return False
        own = ann.key if ann is not None else None
        rank = (ann.since, ann.branch) if ann is not None else None
        mine: list[str] | None = None
        # Each announcement as last seen, and when it last changed (this
        # machine's clock, as of a successful fetch).
        seen: dict[str, tuple[str, float]] = {}
        told: set[str] = set()
        waited = False
        started = status_at = time.monotonic()
        while True:
            waiting: dict[str, list[str]] = {}
            now = time.monotonic()
            for p in pending_announcements():
                if p.key == own or p.key in dead or p.settled or \
                        (rank is not None and (p.since, p.branch) >= rank):
                    continue
                sha, changed = seen.get(p.key, ("", now))
                if sha != p.sha:
                    seen[p.key] = (p.sha, now)
                elif now - changed > WAIT_STALE:
                    print(f"warning: no news of {p.key}'s deploy for {WAIT_STALE} s (its "
                          f"announcement is no longer renewed): taken as dead, going on "
                          f"without it", flush=True)
                    dead.add(p.key)
                    continue
                if mine is None:
                    mine = sorted(memo.reached_targets(
                        wt, union, candidates, snap or "HEAD",
                        {h: probed[h].lineage for h in candidates if h in probed}))
                # The announced state against what the union has of the
                # branch: its parent (the announcer's HEAD) when origin has
                # that, else origin's tip of the branch - a wider diff, so
                # main's changes count too, but never a narrower one.
                tip = rev(f"refs/remotes/origin/{p.branch}")
                base = f"{p.sha}^" if tip is None or is_ancestor(f"{p.sha}^", tip) else tip
                both = memo.reached_between(wt, union, mine, base, p.sha)
                if both:
                    waiting[p.key] = sorted(both)
            if not waiting:
                break
            for b, hs in waiting.items():
                if b not in told:
                    told.add(b)
                    print(f"waiting for {b}'s deploy under way: its increment and this "
                          f"checkout's both reach {', '.join(hs)}...", flush=True)
            if now - status_at >= 60:
                status_at = now
                print(f"still waiting for {', '.join(waiting)} ({(now - started) / 60:.0f} min)",
                      flush=True)
            waited = True
            # Announcements are judged as of a successful look at origin:
            # a blip here must not make a live deploy look dead.
            failed_polls = 0
            while True:
                time.sleep(WAIT_POLL)
                if git(*fetch_args("--no-auto-gc"), check=False).returncode == 0:
                    break
                failed_polls += 1
                if failed_polls >= 12:
                    die(f"origin stopped answering while waiting for the deploy under way; {NOTHING_DONE}")
        if waited:
            print("...ended; probing the targets again and redoing the union", flush=True)
            PERF.mark("probe")
            with ThreadPoolExecutor(max(1, len(probes))) as pool:
                probes = {n: pool.submit(targets.probe, n) for n in probes}
            collect_probes()
        return waited

    def stage(wt: str, union: str, listed: list[str], branches: list[tuple[str, str]]) -> bool:
        """Eval, precheck, build and copy for `union`, checked out in `wt`,
        from the branches `listed` in deploy-branches, `branches` the ones
        it merged: whether the deploy ends here."""
        nonlocal plan, affected, left_alone, tops, committed, records, lineages
        if skip:
            if dry:
                print(f"NIXCONVOY_SKIP_SWITCH=1 --dry-activate: union {union}; nothing done")
                return True
            if wb is not None:
                if snap:
                    commit_deployed(wb, snap, msg or "")
                push_work_branch(wb)
            print(f"NIXCONVOY_SKIP_SWITCH=1: skipping probe+eval+switch (union {union})")
            return True

        if ann is not None and not ann.collect():
            die(f"announcing this deploy on origin failed; {NOTHING_DONE}:\n{ann.detail}")
        PERF.mark("eval")
        memo.memo_prune(memo.memo_dir())
        # The precheck gates the union, not the increment: a branch rebased
        # by pull/land, a skip-switch push or a merge of two passing
        # branches can each carry a violation no increment introduced.
        check: Future[str | None] | None = None
        if memo.precheck_unproven(union):
            check_pool = ThreadPoolExecutor(1)
            check = check_pool.submit(memo.precheck_gate, wt, union)
            check_pool.shutdown(wait=False)
        print("evaluating targets..." if check is None else "evaluating targets, running the precheck...")
        tops = memo.eval_toplevels(wt, union, [h for h in all_targets if running.get(h) is not None],
                                   check)
        if check is not None:
            err = check.result()
            if err:
                die(f"{err}; {NOTHING_DONE}")
        reached = memo.reached_targets(wt, union, sorted(tops), snap or "HEAD",
                                     {h: probed[h].lineage for h in tops if h in probed})
        # The targets the other unioned branches hold back (held_targets):
        # each ran its increments to other targets alone - one already
        # running the branch's tip (its lineage) is not held by it.
        held: dict[str, list[str]] = {}
        for name, tip in branches:
            for h in held_targets(f"refs/remotes/origin/main..{tip}"):
                if h in probed and memo.recorded_tip(probed[h].lineage, name) == tip:
                    continue
                held.setdefault(h, []).append(name)
        last = [h for h in sorted(tops) if tops[h].last]
        verdict = decide(
            "drift", me=[me, str(Path.cwd())], union=union,
            listed=sorted({*listed, "main"}), tips=origin_tips(),
            tz_offset=time.localtime().tm_gmtoff, router=last[0] if last else "",
            targets=all_targets, candidates=candidates, evict=evict, named=bool(filter_targets),
            work_branch=wb is not None, reached=sorted(reached), running=running,
            built={h: t.out for h, t in tops.items()},
            lineages={h: p.lineage for h, p in probed.items()},
            held={h: sorted(bs) for h, bs in held.items()},
            in_flight=announced(ann.key if ann is not None else None), dead=sorted(dead))
        assert isinstance(verdict, dict)
        plan = [str(h) for h in verdict["plan"]]
        affected = [str(h) for h in verdict["affected"]]
        drift = verdict["drift"]
        assert isinstance(drift, dict)
        left_alone = [str(h) for h, v in drift.items() if v[0] == "left-alone"]

        # The target fell back from this very toplevel: switching onto it
        # would run a configuration known not to boot.
        for name in plan:
            if not tops[name].assessed:
                continue
            stub = targets.rejected_generation(name, tops[name].out)
            if stub:
                die(f"{name}'s bootloader rejected the very generation this "
                    f"deploy would switch it to ({stub}): it failed its boot "
                    f"assessment and the target fell back.  Nothing was built, "
                    f"switched or committed.  See `journalctl -u boot-assessment-watch "
                    f"-u boot-assessment` on {name}; then change the configuration, or "
                    f"re-arm the stub to retry it as is (sudo mv of the +0-N stub to +2 "
                    f"under /boot/EFI/Linux).")

        # The increment's commit and each target's record, before the
        # switch: the lineage the targets get names the commit origin will
        # hold, and the records reach origin before anything runs them.
        # The commit's trailers: Reaches, the targets the increment reaches
        # among those deployable from here; Applied, the probed targets
        # running the branch's tip once this deploy is over (switched now,
        # or up to date with the union already, a non-candidate too: every
        # reachable target was evaluated) - a target in the first and not
        # the second is held back (held_targets) until a deploy here
        # switches it.
        committed = ""
        subject = "main" + "".join(f" + {n}" for n, _ in branches)
        fmt = ", ".join
        deployed = sorted(h for h in tops if h in plan or h not in affected)
        if snap and not dry:
            assert wb is not None  # a snapshot needs the work branch (deploy)
            reaches = sorted(memo.reached_between(wt, union, all_targets,
                                                  "HEAD", snap))
            full = (f"{msg}\n\nReaches: {fmt(reaches) or 'none'}"
                    f"\nApplied: {fmt(deployed) or 'none'}")
            committed = deployed_commit(snap, full)
            subject = (msg or "").partition("\n")[0] or subject
        elif wb is not None and not dry and current_branch() == wb:
            # No increment, but a target this branch held back now runs its
            # tip: an empty increment records the hold's end, as a hold is
            # read from the branch's trailers alone.
            ends = sorted(held_targets("refs/remotes/origin/main..HEAD") & set(deployed))
            if ends:
                subject = f"deployed to {fmt(ends)}, held back until now"
                committed = deployed_commit("HEAD", f"{subject}\n\nReaches: none\nApplied: {fmt(deployed)}")
        doc = lineage_record(union, branches, committed, ann.sha if ann else "", ann.key if ann else "")
        records, lineages = {}, {}
        if not dry:
            for h in plan:
                body = dump_json({**doc, "toplevel": tops[h].out})
                parents = [previous_record(probed[h].lineage), rev("refs/remotes/origin/main") or "",
                           *(t for _, t in branches), committed]
                records[h] = record_commit(h, union, parents, subject, body)
                lineages[h] = dump_json({**doc, "toplevel": tops[h].out, "record": records[h]})

        if plan:
            b = builder()
            where = f" on {b}" if b else ""
            PERF.mark("build")
            print(f"building {', '.join(plan)}{where}...")
            err = targets.build_drvs([tops[h].drv for h in plan], f"build of {', '.join(plan)}")
            if err:
                die(f"{err}; {NOTHING_DONE}")

        # Another machine may have pushed a work branch while we built;
        # switching without it would revert its deployed changes (the union
        # core compares once this returns).
        PERF.mark("copy")

        def copy_then_gate(name: str, top: Top, quiet: bool) -> str | targets.Refusal | None:
            targets.copy_closure(name, top.out, quiet)
            if dry:
                # The gate's verdict is part of the report, not a stop.
                return targets.protected_units_gate(name, top.out, top.protected, allow)
            if top.protected:
                return targets.protected_units_gate(name, top.out, top.protected, allow)
            return None

        if dry and plan:
            print(f"dry-activating {', '.join(plan)}...")
        with ThreadPoolExecutor(2 + len(plan)) as pool:
            refetch = pool.submit(git, *fetch_args())
            pushed = pool.submit(push_records, records)
            copies = [pool.submit(copy_then_gate, n, tops[n], len(plan) > 1) for n in plan]
            refusals = [r for r in (cp.result() for cp in copies) if r]
            refetch.result()
            unpushed = pushed.result()
        if dry:
            if refusals:
                die(f"--dry-activate: {NOTHING_DONE}; a deploy "
                    "would stop here:\n" + targets.refusals_message(refusals))
            if plan:
                print(f"--dry-activate: {', '.join(plan)} would be switched (union {union}); "
                      f"{NOTHING_DONE}")
            elif left_alone:
                print(f"--dry-activate: nothing would be switched: {', '.join(left_alone)} left "
                      f"alone, running another checkout's work this checkout's does not reach "
                      f"(union {union})")
            else:
                print(f"--dry-activate: nothing drifts from the union ({union}); nothing to switch")
            return True
        if refusals:
            die(targets.refusals_message(refusals))
        if unpushed:
            die(f"pushing the deploy records to origin failed; {NOTHING_DONE}:\n{unpushed}")
        return False

    queries = UnionQueries(lambda: snap or "HEAD", fetch, refresh, stage, await_pending)
    try:
        done = decide("union", queries, wb=wb or "", evict=evict, main0=main0)
        if skip or dry:
            return False
        assert isinstance(done, dict)
        union = str(done["union"])
        nixconvoy_switch = binary("nixconvoy-switch") if plan else ""

        # The switch-last targets alone, one by one, once everything else has
        # finished talking to them.  A failed switch dies once the others in
        # its batch finish.
        first = [h for h in plan if not tops[h].last]
        batches = [first, *([h] for h in plan if tops[h].last)]
        for batch in batches:
            if not batch:
                continue
            expected: dict[str, str] = {}
            for name in batch:
                r = running.get(name)
                assert r is not None  # unreachable targets never enter the plan
                expected[name] = r
            PERF.mark("switch:" + "+".join(batch))
            # Claimed before the switch starts: a switch cut short (a signal,
            # a batch sibling's die) may still leave the target running it.
            if ann is not None:
                ann.claimed += [n for n in batch if n not in ann.claimed]
            if len(batch) == 1:
                n = batch[0]
                switch(n, expected[n], tops[n].out, nixconvoy_switch, lineage=lineages[n],
                       digest=probed[n].digest, needs_internet=tops[n].last)
                continue
            with ThreadPoolExecutor(len(batch)) as pool:
                switches = [pool.submit(switch, n, expected[n], tops[n].out, nixconvoy_switch, True,
                                        lineages[n], probed[n].digest, tops[n].last)
                            for n in batch]
            for f in switches:
                f.result()

        PERF.mark("commit-push")
        if committed:
            assert wb is not None
            adopt_commit(wb, committed)
        # A local main ahead of origin is pushed by its owner once every
        # target it affects has switched.
        push_deployed(wb, ann, records)

        if plan:
            print(f"deployed {', '.join(plan)}: union {union}")
        elif left_alone:
            print(f"nothing switched: {', '.join(left_alone)} left alone, running another "
                  f"checkout's work this checkout's does not reach (union {union})")
        else:
            print(f"nothing drifts from the union ({union}); nothing to switch")
        return True
    finally:
        queries.drop()
