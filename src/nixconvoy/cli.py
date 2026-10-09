"""nixconvoy - clobber-safe nixos-rebuild, branch-based, target-inferring.

Problem: several checkouts of one NixOS flake repo (worktrees, machines)
deploy work in progress to the same hosts, so a plain nixos-rebuild from
one tree silently reverts what another tree already deployed.

Model:
  - main: mirror of origin/main; only finished (landed) commits.
  - A work branch, listed by pattern in deploy-branches at the repo root and
    named with this machine's branch_prefix, is owned by the checkout that
    has it checked out; nixconvoy is its only writer: it commits the staged
    state onto it and pushes it after a successful deploy.  A checkout on
    main (or any other branch, another machine's work branch included) is a
    main checkout: it deploys main plus the pushed work branches and owns
    nothing.  A Claude Code subagent worktree (<checkout>/.claude/worktrees/
    <name>) owns <parent's branch>-<name> (<branch_prefix>-<name> under a
    main checkout) from its first deploy.  After a land or abandon a
    checkout stays on its work branch, reset onto origin/main.
  - COMMIT_MSG.md at a work branch's root describes the branch's work since
    its last landing.  It never reaches main's tree: `land` consumes it as
    the commit message.
  - Targets are inferred: each host's ground truth is its running system
    (/run/current-system), compared against the union build.  Every deploy
    converges whatever differs.
  - The decisions - rebasing a checkout, the union and its retries, which
    drifting hosts may be switched, the switch itself - are the switch-guard
    crate's (deploy-plan, deploy-switch: Sans-I/O cores); this program
    performs the git, eval, build and ssh they ask for.

Deploy (nixconvoy apply [-m "..."] [--reset] [--danger-network-config-change]
         [--take-over host] [--dry-activate] [host|@tag ...]):
  1. start fetching origin; enumerate the hosts from the hosts directory
  2. probe every host's running system and its lineage
     (/var/lib/nixconvoy/lineage.json, in the same round trip; a local_only
     host participates only when the checkout is on it), concurrently with
     steps 3-4.  Candidates default to all hosts; naming hosts (or @tag, every
     host nixconvoy.toml gives that tag) restricts switching to (at most)
     those.  An unreachable candidate fails the deploy before anything is
     evaluated
  3. snapshot the staged state (index) as an ephemeral deterministic commit
     on top of the work branch; unstaged and untracked changes are excluded,
     since staging marks a change as ready to deploy.  Nothing is committed
  4. in a temporary worktree on that snapshot, union-merge every other
     pushed, listed work branch that still declares changes (mergiraf
     resolves structural conflicts; real conflicts abort with per-branch
     guidance).  COMMIT_MSG.md is stripped, and the union commit is
     deterministic (fixed identity/date, parent = origin/main), so the eval
     memo stays valid across retries and no-op redeploys.  Once step 1's
     fetch lands, steps 3-4 are redone if it moved origin/main or a work branch
  5. evaluate every reachable host's toplevel for the union, one nix eval
     process per stale host, concurrently, memoized per host by the git
     hashes of the files its configuration references and the lock entries
     of the flake inputs those files name: a change to a module only one
     host imports re-evaluates that host alone, and a no-op redeploy
     evaluates nothing.  The same eval reads the switch policy the host's
     configuration declares through the nixconvoy NixOS module (protected
     units, switch last, boot assessment).  Hosts whose running system
     matches are up to date; the rest will be switched (their drift may be
     catch-up to main or other branches, not just this increment) - unless
     the lineage shows the host runs another deployer's increment that this
     union cannot contain: a switch whose push has not landed yet, or one
     of a branch deploy-branches here does not list.  Such a host is never
     switched as is: when this checkout's own work does not reach it (no
     file its configuration reads changed since the tip of this branch its
     lineage recorded) it is left alone, running that work; when it does,
     the deploy is refused whole.  A refused lineage older than 30 min by
     the host's clock is a dead deployer's and is switched over with a
     warning, as is a host named to --take-over.  When the configuration
     names a precheck (a flake check), it runs on a thread beside the evals
     unless a pass is already proven for exactly the files it reads, and a
     failure stops the deploy the moment it is known.  A drifting candidate
     whose boot counting rejected its new toplevel stops the deploy before
     anything is built
  6. build the drifting candidates' closures straight from the evaluated
     .drvs (no re-eval), on the configured build host if any, unsandboxed
     when a dry-run shows only trivial builders left.  Nothing drifting and
     no precheck to run means no build
  7. copy the closures over, concurrently, while re-fetching to check that
     no work branch moved during the build.  A host with protected units is
     dry-activated once its closure has landed: if the switch would stop,
     restart or reload one of them, the deploy stops before any host is
     switched unless --danger-network-config-change was given.  Then every
     host not marked switch-last is switched at once and the switch-last
     ones alone after, activating the exact evaluated toplevel under a flock
     held ON THE TARGET HOST that first re-verifies the host still runs what
     step 2 probed.  This compare-and-swap makes a deploy abort cleanly
     instead of reverting a switch another machine made meanwhile.  The
     guard compares the lineage too, then claims the lineage file for the
     new toplevel before activation with its provenance: the toplevel, the
     union, origin/main and each unioned branch's tip, this checkout's
     branch as pending when what it unioned of it is not on origin yet, the
     time and this deployer.  A host whose toplevel carries
     /confirmed-switch (the confirmed-switch module) is switched confirmed:
     a rollback timer is armed, the toplevel activated with `test`, and
     only once the host runs it, reaches the internet and passes its own
     checks (for a switch-last host, this machine must reach the internet
     too) are its profile and bootloader entry set - otherwise it is rolled
     back to the probed system, never rebooted
  8. only now commit the staged state onto the work branch - the -m message
     plus Affected:/Deployed: trailers - and push it in the background, the
     next run collecting the result before its own fetch.  An increment that
     fails to eval, build or switch is never committed, so other checkouts
     cannot pull a broken commit and pushed tips never overestimate what is
     running; its changes stay staged for the re-run

Land (nixconvoy land [<name> ...]): commit the work branch's tree minus
COMMIT_MSG.md onto origin/main with COMMIT_MSG.md as the message, push main,
reset this branch (and its origin copy) onto the new main, the checkout
staying on it.  Every other checkout on this machine is then brought onto
the new main (see Pull); checkouts on other machines rebase at their next
deploy.  A subagent's worktree has done its one task once landed or
abandoned, so it is removed instead, with its branch (here and on origin);
run from any checkout with the worktrees' <name>s (a unique prefix will
do), land and abandon act on each in turn.

Pull (nixconvoy pull): fetch and bring this checkout and every sibling
checkout onto origin/main - a deploy's automatic rebase, for all of them at
once (a main checkout fast-forwards; a work branch not checked out is
rebased in a throwaway worktree).  Each checkout is taken under its deploy
lock (one whose nixconvoy is running is skipped) and either ends up rebased
with its staged/unstaged state intact and its work branch pushed, or is
left exactly as it was: a rebase that isn't clean is aborted and reported.

Status (nixconvoy status): after a fetch, every checkout on this machine as
a tree (the main checkouts, the work-branch ones, each with its subagent
worktrees): the branch or parked state, commits ahead of origin/main and
whether they are pushed, staged/unstaged/untracked counts, the
COMMIT_MSG.md subject, a deploy or git operation in progress - then the
work branches only origin holds (other machines' checkouts).

Review (nixconvoy review [<name> ...] [args]): `git review`
(integrations/claude-code/bin/git-review), the review of the pending landing.

Abandon (nixconvoy abandon [<name> ...]): discard this checkout's work
instead of landing it: hard-reset the checkout and reset the work branch
(local and origin) onto origin/main, staying on it.  Untracked files
survive; the old tip remains reachable via the reflog.

Commit (nixconvoy commit -m "..."): commit + push the staged state onto the
work branch without probing, building or switching anything - for changes
that affect no NixOS host, or to publish work while a host is unreachable.
A staged change to what the precheck reads is checked on its own first.

--take-over HOST switches HOST even though it runs another deployer's
in-flight or unlisted work (step 5), reverting that work there.

--reset deploys ignoring the other work branches.  It is a one-off: the
others return on their next deploy - the durable fix is to reset the branch
that carries the unwanted change (git push --force-with-lease origin
origin/main:<branch>) or repair it in its own checkout.

--danger-network-config-change admits a switch that would stop, restart or
reload a protected unit (step 7), for this run only.

--dry-activate runs steps 1-7 up to the switch (the checkout is rebased and
the closures copied as for a deploy), dry-activating every host that would
be switched instead, and prints the units each would stop, restart or
reload.  Nothing is switched, committed or pushed, and the staged changes
need no -m; the run fails where a deploy would stop: a dry activation that
fails, or protected units the switch would touch without the flag.

Exec (nixconvoy exec [host|@tag ...] -- command [arg ...]): the command on
each named host (every host addressable from here when none is named),
concurrently, each host's output printed under its name once it finishes.
Arguments are passed as they are (a pipeline wants `sh -c '...'`); the exit
status is non-zero when it failed anywhere.

Env: NIXCONVOY_SKIP_SWITCH=1 exercises only the branch/union machinery: no
probe, eval, build or switch.  NIXCONVOY_FAIL_VERIFY=1 fails a confirmed
switch's verification, exercising the rollback.  NIXCONVOY_BUILD_HOST
overrides the build host ("" builds locally).  NIXCONVOY_HOLD_PUSH=N delays
the background push N seconds, keeping the switched increment in flight.
NIXCONVOY_GUARD_DIR names the directory holding deploy-plan and
deploy-switch (the nix package sets it)."""

from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path

from . import commands, proc
from .checkout import lock_checkout
from .deploy import deploy
from .hosts import NETWORK_FLAG, exec_on
from .proc import PERF, PERF_FILE, die, git, out
from .switch import TAKE_OVER_FLAG

USAGE = """usage: nixconvoy apply [-m "msg"] [--reset] [--take-over host] [--danger-network-config-change] [--dry-activate] [host|@tag ...]
       nixconvoy commit -m "msg"
       nixconvoy land [worktree ...]
       nixconvoy abandon [worktree ...]
       nixconvoy pull
       nixconvoy status
       nixconvoy review [worktree ...] [diff args]
       nixconvoy exec [host|@tag ...] -- command [arg ...]
With no hosts named, every host whose running system differs from the union
is switched; naming hosts, or @tag for the hosts nixconvoy.toml tags so,
restricts switching to (at most) those.
--danger-network-config-change admits a switch that would stop, restart or
reload a unit the host's configuration protects (nixconvoy.protectedUnits).
--dry-activate stops before any switch, printing what each would do."""
DRY_FLAG = "--dry-activate"


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return
    if args[0] in ("-h", "--help"):
        print(__doc__)
        print(USAGE)
        return
    if args[0] not in ("apply", "land", "commit", "abandon", "pull", "status", "review", "exec"):
        die(USAGE)
    subcmd = args.pop(0)
    start = Path.cwd()
    os.chdir(out(["git", "rev-parse", "--show-toplevel"]))
    # A dead run's leftovers, seen from every worktree; the age spares a
    # live run's throwaway worktree in a private /tmp this process cannot see.
    git("worktree", "prune", "--expire", "1.day.ago", check=False)
    if subcmd == "review":
        os.chdir(start)  # its paths are the caller's
        os.execvp("git", ["git", "review", *args])  # noqa: S606 - integrations/claude-code/bin/git-review
    if subcmd == "exec":
        if "--" not in args or args[0] in ("-h", "--help"):
            die(USAGE)
        at = args.index("--")
        if not args[at + 1:]:
            die(USAGE)
        exec_on(args[:at], args[at + 1:])
        return
    if subcmd != "status":
        atexit.register(PERF.write, Path.cwd() / PERF_FILE)
    msg: str | None = None
    reset = False
    danger = False
    dry = False
    take_over: set[str] = set()
    hosts: list[str] = []
    while args:
        a = args.pop(0)
        if a == "-m" and args:
            msg = args.pop(0)
        elif a == "--reset":
            reset = True
        elif a == NETWORK_FLAG:
            danger = True
        elif a == DRY_FLAG:
            dry = True
        elif a == TAKE_OVER_FLAG and args:
            take_over.add(args.pop(0))
        elif a in ("-h", "--help"):
            print(USAGE)
            return
        elif not a.startswith("-"):
            hosts.append(a)
        else:
            die(USAGE)

    if subcmd == "pull":
        if msg or reset or danger or dry or hosts or take_over:
            die(USAGE)
        commands.pull()  # takes each checkout's lock itself, this one included
        return
    if subcmd == "status":
        if msg or reset or danger or dry or hosts or take_over:
            die(USAGE)
        commands.status()
        return
    if subcmd in ("land", "abandon"):
        if msg or reset or danger or dry or take_over:
            die(USAGE)
        act = commands.land if subcmd == "land" else commands.abandon
        # Several subagent worktrees, one after the other, each under its
        # own checkout's lock.
        top = Path.cwd()
        for name in hosts:
            if len(hosts) > 1:
                print(f"== {name}")
            os.chdir(top)  # the previous one's removal moved into its parent
            commands.enter_sub(name)
            lock = lock_checkout()
            if lock is None:
                die(f"another nixconvoy is running in {name}'s worktree")
            act()
            lock.close()
        if hosts:
            return
    lock = lock_checkout()
    if lock is None:
        die("another nixconvoy is running in this checkout")
    proc.HELD.append(lock)  # held until exit
    if subcmd == "land":
        commands.land()
    elif subcmd == "abandon":
        commands.abandon()
    elif subcmd == "commit":
        if reset or danger or dry or hosts or take_over:
            die(USAGE)
        commands.commit_only(msg)
    else:  # apply
        deploy(hosts, msg, reset, danger, take_over, dry)


if __name__ == "__main__":
    main()
