"""A deploy: probe, snapshot, union, eval, precheck, build, copy, switch,
then commit and push - in the order the module docstring of cli.py gives."""

from __future__ import annotations

import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from . import hosts, memo, proc
from .checkout import (
    adopt_commit,
    await_background_push,
    commit_deployed,
    current_branch,
    deployed_commit,
    ensure_on_work_branch,
    ensure_rebased,
    origin_tips,
    pop_tmp_commits,
    push_work_branch,
    staged_differs,
    staged_snapshot,
    work_branch,
)
from .config import build_host
from .guard import binary, decide
from .hosts import Probe, expand_targets, flake_host_names, remote_only
from .memo import Top
from .proc import PERF, die, dump_json, git, rev
from .switch import lineage_record, switch
from .union import UnionQueries


def deploy(filter_hosts: list[str], msg: str | None, reset: bool,
           danger: bool = False, dry: bool = False) -> None:
    """`reset`: this checkout's view alone - no other work branch in the
    union, and a host running another deployer's refused work switched over
    when this work reaches it or `filter_hosts` names it.
    `dry`: stop after the dry activation of every host that would be
    switched, printing the units it would stop, restart or reload; nothing
    is switched, committed or pushed."""
    skip = os.environ.get("NIXCONVOY_SKIP_SWITCH") == "1"
    all_hosts = flake_host_names()
    filter_hosts = expand_targets(filter_hosts, all_hosts)
    me = proc.this_host()

    for h in filter_hosts:
        if remote_only(h):
            die(f"{h} is deployable only from itself (nixconvoy.toml: local_only); run nixconvoy there")
    candidates = filter_hosts or [h for h in all_hosts if not remote_only(h)]

    await_background_push()
    main0 = rev("refs/remotes/origin/main")
    # Non-candidates are probed best-effort so the Affected: trailer can
    # still cover them.
    probe_names: list[str] = [] if skip else [n for n in all_hosts if not remote_only(n)]
    pool = ThreadPoolExecutor(1 + len(probe_names))
    fetch = pool.submit(git, "fetch", "--quiet", "--no-auto-gc", "origin")
    probes: dict[str, Future[Probe]] = {n: pool.submit(hosts.probe, n) for n in probe_names}
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

    snap = ""
    PERF.mark("snapshot")
    if staged_differs():
        if not msg and not dry:
            die('staged changes present; describe this increment with -m "..."')
        if wb is None:
            die("staged changes in the main checkout, which has no work branch: commit "
                "them by hand here, or stage them in a checkout on a work branch")
        ensure_on_work_branch(wb)  # the increment must land (and union) on wb
        # A snapshot's tree is HEAD + staged, so one taken on a stale HEAD
        # would commit main's changes away.
        ensure_rebased()
        snap = staged_snapshot()
    elif msg:
        die("nothing is staged; stage the change or drop -m")
    else:
        ensure_rebased()

    running: dict[str, str | None] = {}
    probed: dict[str, Probe] = {}
    if not skip:
        PERF.mark("probe")
        probed = {n: f.result() for n, f in probes.items()}
        running = {n: p.running for n, p in probed.items()}
        dead = [h for h in candidates if running.get(h) is None]
        if dead:
            die(f"unreachable: {', '.join(dead)} - nothing was committed; "
                f"fix connectivity (or restrict hosts / use `commit`) and re-run")
        hosts.check_build_host()

    plan: list[str] = []
    affected: list[str] = []
    left_alone: list[str] = []
    tops: dict[str, Top] = {}

    def refresh() -> None:
        nonlocal snap
        ensure_rebased()
        if snap:
            snap = staged_snapshot()

    def stage(wt: str, union: str, listed: list[str]) -> bool:
        """Eval, precheck, build and copy for `union`, checked out in `wt`
        from the branches `listed` in deploy-branches: whether the deploy
        ends here."""
        nonlocal plan, affected, left_alone, tops
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

        PERF.mark("eval")
        memo.memo_prune(memo.memo_dir())
        objs = memo.union_objects(union)
        # The precheck gates the union, not the increment: a branch rebased
        # by pull/land, a skip-switch push or a merge of two passing
        # branches can each carry a violation no increment introduced.
        check: Future[str | None] | None = None
        if memo.precheck_unproven(objs):
            check_pool = ThreadPoolExecutor(1)
            check = check_pool.submit(memo.precheck_gate, wt, objs)
            check_pool.shutdown(wait=False)
        print("evaluating hosts..." if check is None else "evaluating hosts, running the precheck...")
        tops = memo.eval_toplevels(wt, union, [h for h in all_hosts if running.get(h) is not None],
                                   check)
        if check is not None:
            err = check.result()
            if err:
                die(f"{err}; nothing was pushed or switched")
        reached = memo.reached_hosts(wt, union, sorted(tops), snap or "HEAD",
                                     {h: probed[h].lineage for h in tops if h in probed})
        last = [h for h in sorted(tops) if tops[h].last]
        verdict = decide(
            "drift", me=[me, str(Path.cwd())], union=union,
            listed=sorted({*listed, "main"}), tips=origin_tips(), now=time.time(),
            tz_offset=time.localtime().tm_gmtoff, router=last[0] if last else "",
            hosts=all_hosts, candidates=candidates, reset=reset, named=bool(filter_hosts),
            reached=sorted(reached), running=running,
            built={h: t.out for h, t in tops.items()},
            lineages={h: {"text": p.lineage, "age": p.age} for h, p in probed.items()})
        assert isinstance(verdict, dict)
        plan = [str(h) for h in verdict["plan"]]
        affected = [str(h) for h in verdict["affected"]]
        drift = verdict["drift"]
        assert isinstance(drift, dict)
        left_alone = [str(h) for h, v in drift.items() if v[0] == "left-alone"]

        # The host fell back from this very toplevel: switching onto it
        # would run a configuration known not to boot.
        for name in plan:
            if not tops[name].assessed:
                continue
            stub = hosts.rejected_generation(name, tops[name].out)
            if stub:
                die(f"{name}'s bootloader rejected the very generation this "
                    f"deploy would switch it to ({stub}): it failed its boot "
                    f"assessment and the host fell back.  Nothing was built, "
                    f"switched or committed.  See `journalctl -u boot-assessment-watch "
                    f"-u boot-assessment` on {name}; then change the configuration, or "
                    f"re-arm the stub to retry it as is (sudo mv of the +0-N stub to +2 "
                    f"under /boot/EFI/Linux).")

        if plan:
            bh = build_host()
            where = f" on {bh}" if bh else ""
            PERF.mark("build")
            print(f"building {', '.join(plan)}{where}...")
            err = hosts.build_drvs([tops[h].drv for h in plan], f"build of {', '.join(plan)}")
            if err:
                die(f"{err}; nothing was pushed or switched")

        # Another machine may have pushed a work branch while we built;
        # switching without it would revert its deployed changes (the union
        # core compares once this returns).
        PERF.mark("copy")

        def copy_then_gate(name: str, top: Top, quiet: bool) -> str | None:
            hosts.copy_closure(name, top.out, quiet)
            if dry:
                # The gate's verdict is part of the report, not a stop.
                return hosts.protected_units_gate(name, top.out, top.protected, danger)
            if top.protected:
                return hosts.protected_units_gate(name, top.out, top.protected, danger)
            return None

        if dry and plan:
            print(f"dry-activating {', '.join(plan)}...")
        with ThreadPoolExecutor(1 + len(plan)) as pool:
            refetch = pool.submit(git, "fetch", "--quiet", "origin")
            copies = [pool.submit(copy_then_gate, n, tops[n], len(plan) > 1) for n in plan]
            refusals = [r for r in (cp.result() for cp in copies) if r]
            refetch.result()
        if dry:
            if refusals:
                die("--dry-activate: nothing was switched, committed or pushed; a deploy "
                    "would stop here:\n" + "\n".join(f"  {r}" for r in refusals))
            if plan:
                print(f"--dry-activate: {', '.join(plan)} would be switched (union {union}); "
                      f"nothing was switched, committed or pushed")
            elif left_alone:
                print(f"--dry-activate: nothing would be switched: {', '.join(left_alone)} left "
                      f"alone, running another deployer's work this checkout's does not reach "
                      f"(union {union})")
            else:
                print(f"--dry-activate: nothing drifts from the union ({union}); nothing to switch")
            return True
        if refusals:
            die(refusals[0])
        return False

    queries = UnionQueries(lambda: snap or "HEAD", fetch, refresh, stage)
    try:
        done = decide("union", queries, wb=wb or "", reset=reset, main0=main0)
        if skip or dry:
            return
        assert isinstance(done, dict)
        union = str(done["union"])
        branches = [(str(n), str(t)) for n, t in done["branches"]]

        committed = ""
        if snap:
            assert wb is not None  # a snapshot needs the work branch (above)
            fmt = ", ".join
            full = (f"{msg}\n\nAffected: {fmt(affected) or 'none'}"
                    f"\nDeployed: {fmt(plan) or 'none'}")
            committed = deployed_commit(snap, full)
        record = lineage_record(union, branches, committed)
        deployer = binary("deploy-switch") if plan else ""

        # The switch-last hosts alone, one by one, once everything else has
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
                assert r is not None  # unreachable hosts never enter the plan
                expected[name] = r
            PERF.mark("switch:" + "+".join(batch))

            def line(n: str) -> str:
                return dump_json({**record, "toplevel": tops[n].out})

            if len(batch) == 1:
                n = batch[0]
                switch(n, expected[n], tops[n].out, deployer, lineage=line(n),
                       digest=probed[n].digest, needs_internet=tops[n].last)
                continue
            with ThreadPoolExecutor(len(batch)) as pool:
                switches = [pool.submit(switch, n, expected[n], tops[n].out, deployer, True,
                                        line(n), probed[n].digest, tops[n].last)
                            for n in batch]
            for f in switches:
                f.result()

        PERF.mark("commit-push")
        if committed:
            assert wb is not None
            adopt_commit(wb, committed)
        if wb is not None and current_branch() == wb:
            push_work_branch(wb, background=True)
        # A local main ahead of origin is pushed by its owner once every
        # host it targets has switched.

        if plan:
            print(f"deployed {', '.join(plan)}: union {union}")
        elif left_alone:
            print(f"nothing switched: {', '.join(left_alone)} left alone, running another "
                  f"deployer's work this checkout's does not reach (union {union})")
        else:
            print(f"nothing drifts from the union ({union}); nothing to switch")
    finally:
        queries.drop()

