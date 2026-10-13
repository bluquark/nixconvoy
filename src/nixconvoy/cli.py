"""nixconvoy - clobber-safe nixos-rebuild, branch-based, target-inferring.

Problem: several checkouts of one NixOS flake repo (worktrees, machines)
deploy work in progress to the same targets, so a plain nixos-rebuild from
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
  - Targets are inferred: each target's ground truth is its running system
    (/run/current-system), compared against the union build.  Every deploy
    converges whatever differs.
  - The decisions - rebasing a checkout, the union and its retries, which
    drifting targets may be switched, the switch itself - are the switch-guard
    crate's (nixconvoy-plan, nixconvoy-switch: Sans-I/O cores); this program
    performs the git, eval, build and ssh they ask for.

Deploy (nixconvoy apply [-m "..."] [--reset] [--danger-network-config-change]
         [--dry-activate] [target|@tag ...]):
  1. start fetching origin; enumerate the targets from the hosts directory
  2. probe every target's running system and its lineage
     (/var/lib/nixconvoy/lineage.json, in the same round trip; a local_only
     target participates only when the checkout is on it), concurrently with
     steps 3-4.  Candidates default to all targets; naming targets (or @tag, every
     target nixconvoy.toml gives that tag) restricts switching to (at most)
     those.  An unreachable candidate fails the deploy before anything is
     evaluated
  3. snapshot the staged state (index) as an ephemeral deterministic commit
     on top of the work branch; unstaged and untracked changes are excluded,
     since staging marks a change as ready to deploy.  Nothing is committed.
     The pending state - the increment, or a HEAD origin lacks (the
     nightly's lock commit on main) - is announced: its tree as a commit
     of its own, pushed to origin's refs/nixconvoy/pending/<branch> and
     renewed there every 10 s for as long as this run lives (a main
     checkout's under main@<machine>), so a deploy starting elsewhere sees
     this one under way (step 4a) until the push that ends this deploy
     clears it (step 8; a main checkout's, the push of main by its owner
     that lands the announced HEAD) - or this run withdraws it, ending
     without a switch.  Its author date is the run's start (a re-run keeps
     it): its rank among the deploys under way
  4. in a temporary worktree on that snapshot, union-merge every other
     pushed, listed work branch that still declares changes (mergiraf
     resolves structural conflicts; real conflicts abort with per-branch
     guidance).  COMMIT_MSG.md is stripped, and the union commit is
     deterministic (fixed identity/date, parent = origin/main), so the eval
     memo stays valid across retries and no-op redeploys.  Once step 1's
     fetch lands, steps 3-4 are redone if it moved origin/main or a work branch
  4a. before anything is evaluated: while another checkout's deploy is under
     way (its announcement, as fetched) and its increment reaches a
     candidate this checkout's own work reaches too (the eval memo's read
     scan over the union, no eval), wait for it, polling origin, then probe
     the targets again and redo step 4 with what it pushed.  Building without
     it would only end refused - or rebuilt - once it switched.  Only
     deploys announced before this one are waited for (two never wait for
     each other); one whose announcement goes 30 s without a renewal, or
     whose process is gone on this machine, is taken for dead and gone on
     without, with a warning; one settled (over, its push pending) is not
     waited for; --reset waits for nobody
  5. evaluate every reachable target's toplevel for the union, one nix eval
     process per stale target, concurrently, memoized per target by the git
     hashes of the files its configuration references and the lock entries
     of the flake inputs those files name: a change to a module only one
     target imports re-evaluates that target alone, and a no-op redeploy
     evaluates nothing.  The same eval reads the switch policy the target's
     configuration declares through the nixconvoy NixOS module (protected
     units, switch last, boot assessment).  Targets whose running system
     matches are up to date; the rest will be switched (their drift may be
     catch-up to main or other branches, not just this increment) - unless
     the lineage shows the target runs another checkout's increment that this
     union cannot contain: a switch whose push has not landed yet, or one
     of a branch deploy-branches here does not list.  Such a target is never
     switched as is: when this checkout's own work does not reach it (no
     file its configuration reads changed since the tip of this branch its
     lineage recorded) it is left alone, running that work; when it does,
     the deploy is refused whole.  An increment whose deploy is over - its
     announcement cleared (step 8) without the push landing - is
     abandoned: switched over with a warning; so is a refused target
     under --reset, when this work reaches it or the target was named.
     A dead deploy's announcement stays up, so its increment stays
     refused: the refusal names the deploy and the clear that ends it
     (git push --force origin origin/main:refs/nixconvoy/pending/<branch>),
     or the push of its branch that keeps it.  Work of a branch
     deploy-branches does not list stays refused until listed.  When the configuration
     names a precheck (a flake check), it runs on a thread beside the evals
     unless a pass is already proven for exactly the files it reads, and a
     failure stops the deploy the moment it is known.  A drifting candidate
     whose boot counting rejected its new toplevel stops the deploy before
     anything is built.  Each target to be switched gets its deploy record:
     a commit of the union's tree whose message carries the lineage below,
     on the target's previous record (git log --first-parent is its deploy
     history), origin/main, each unioned branch's tip and the increment's
     commit, which is made now with its -m message and Affected:/Deployed:
     trailers
  6. build the drifting candidates' closures straight from the evaluated
     .drvs (no re-eval), on the configured builder if any, unsandboxed
     when a dry-run shows only trivial builders left.  Nothing drifting and
     no precheck to run means no build
  7. copy the closures over, concurrently, while re-fetching to check that
     no work branch moved during the build and pushing the records to
     origin's refs/nixconvoy/deploying/<target>, so no target ever runs a
     state origin does not hold.  A target with protected units is
     dry-activated once its closure has landed: if the switch would stop,
     restart or reload one of them, the deploy stops before any target is
     switched unless --danger-network-config-change was given.  Then every
     target not marked switch-last is switched at once and the switch-last
     ones alone after, activating the exact evaluated toplevel under a flock
     held ON THE TARGET that first re-verifies the target still runs what
     step 2 probed.  This compare-and-swap makes a deploy abort cleanly
     instead of reverting a switch another machine made meanwhile.  The
     guard compares the lineage too, then claims the lineage file for the
     new toplevel before activation with its provenance: the toplevel, the
     union, origin/main and each unioned branch's tip, this checkout's
     branch as pending when what it unioned of it is not on origin yet
     (with the announcement), the time, this machine and checkout, and the
     record.  A target whose toplevel carries
     /confirmed-switch (the confirmed-switch module) is switched confirmed:
     a rollback timer is armed, the toplevel activated with `test`, and
     only once a new ssh connection finds the target running it and its own
     checks pass (the internet probe when configured; for a switch-last
     target, this machine must reach the internet too) are its profile and
     bootloader entry set - otherwise it is rolled back to the probed
     system, never rebooted
  8. only now adopt the increment's commit onto the work branch and push it
     in the background - then, once that landed, point the announcement at
     origin/main and each switched target's record at
     refs/heads/deployed/<target> - the next run collecting the result
     before its own fetch.  An increment that fails to eval, build or switch
     is never committed, so other checkouts cannot pull a broken commit and
     pushed tips never overestimate what is running; its changes stay staged
     for the re-run, and its announcement is withdrawn - unless a target
     runs it already, or a switch onto it was started: then it stays, so
     a deploy elsewhere is refused there rather than reverting the
     increment, until the re-run ends with the push (or a hand clear)

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

Abandon (nixconvoy abandon [--apply-now] [<name> ...]): discard this
checkout's work instead of landing it: hard-reset the checkout and reset
the work branch (local and origin) onto origin/main, staying on it.
Untracked files survive; the old tip remains reachable via the reflog.
No target is touched: the ones its increments were deployed to (named from
their Deployed: trailers) keep running them until the next deploy by
anyone, whose verdict names what it reverts ("will switch (catch-up: ...;
reverting <branch>'s abandoned increment <sha>)").  --apply-now runs that
deploy at once, so the targets drop the work now - from the checkout abandon
ran in, a subagent worktree's parent once the worktree is removed; it
takes --danger-network-config-change.

Prune (nixconvoy prune [<name> ...]): remove the subagent worktrees that
hold nothing of their own - never deployed, no staged, unstaged or
untracked change, HEAD and its work branch on origin - with their fresh
branch; the named ones, or every one under this checkout (nixconvoy-plan's
prune core decides each).  One whose spawning claude session still runs is
kept, whichever session asks: the session may resume its agent, which needs
the worktree.  So is one at work (a nixconvoy, git operation or push
running in it), on a work branch (land or abandon decides it), locked by
hand (or, in a sweep, unlocked) or holding anything; each kept one is said
so, as is one a process works in.  It all happens under a lock on the
worktree's name, which the create hook takes too, and the removal claims
the lock it judged and leaves git to refuse a worktree locked meanwhile.  `--after PID` detaches, prunes what dead
sessions left, waits for the claude session PID to exit and prunes again
(one waiter per session, logged to nixconvoy-prune.log in the common git
dir): the Claude Code plugin's SessionStart hook starts it, so a subagent's
worktree with nothing of its own goes when its session ends.  `--stopped
PID`, the SubagentStop hook's, takes that hook's input on stdin and,
detached, removes the stopped agent's worktree sooner, at its stop, when it
holds nothing of its own, no process or other running agent works in it
and, a few seconds on, the agent has not run again (a message or Claude
Code's own follow-up turn) and no resume relocked it, whether session PID
runs or not: resuming the agent recreates it (the PreToolUse hook on
SendMessage).  It keeps a worktree whose lock names no session.


Commit (nixconvoy commit -m "..."): commit + push the staged state onto the
work branch without probing, building or switching anything - for changes
that affect no NixOS target, or to publish work while a target is unreachable.
A staged change to what the precheck reads is checked on its own first.

--reset deploys this checkout's view alone: the other work branches are
left out of the union (and no deploy under way is waited for), so every
target switched loses their work, and a target
running another checkout's in-flight or unlisted work (step 5) is switched
over too, reverting that work, when this checkout's work reaches it or the
target is named (one left alone otherwise stays as it is).  It is a one-off:
the others return on their next deploy - the durable fix is to reset the
branch that carries the unwanted change (git push --force-with-lease origin
origin/main:<branch>) or repair it in its own checkout.

--danger-network-config-change admits a switch that would stop, restart or
reload a protected unit (step 7), for this run only.

--dry-activate runs steps 1-7 up to the switch (the checkout is rebased and
the closures copied as for a deploy), dry-activating every target that would
be switched instead, and prints the units each would stop, restart or
reload.  Nothing is switched, committed or pushed, and the staged changes
need no -m; the run fails where a deploy would stop: a dry activation that
fails, or protected units the switch would touch without the flag.

Exec (nixconvoy exec [target|@tag ...] -- command [arg ...]): the command on
each named target (every target addressable from here when none is named),
concurrently, each target's output printed under its name once it finishes.
Arguments are passed as they are (a pipeline wants `sh -c '...'`); the exit
status is non-zero when it failed anywhere.

Env: NIXCONVOY_SKIP_SWITCH=1 exercises only the branch/union machinery: no
probe, eval, build or switch.  NIXCONVOY_FAIL_VERIFY=1 fails a confirmed
switch's verification, exercising the rollback.  NIXCONVOY_BUILDER
overrides the builder ("" builds locally).  NIXCONVOY_HOLD_PUSH=N delays
the background push N seconds, keeping the switched increment in flight.
NIXCONVOY_GUARD_DIR names the directory holding nixconvoy-plan and
nixconvoy-switch (the nix package sets it)."""

from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path

from . import commands, proc
from .checkout import lock_checkout, sub_parent
from .deploy import deploy
from .proc import PERF, PERF_FILE, die, git, ok, out
from .targets import NETWORK_FLAG, exec_on

USAGE = """usage: nixconvoy apply [-m "msg"] [--reset] [--danger-network-config-change] [--dry-activate] [target|@tag ...]
       nixconvoy commit -m "msg"
       nixconvoy land [worktree ...]
       nixconvoy abandon [--apply-now] [worktree ...]
       nixconvoy prune [worktree ...] | --after PID | --stopped PID < hook-input
       nixconvoy pull
       nixconvoy status
       nixconvoy review [worktree ...] [diff args]
       nixconvoy exec [target|@tag ...] -- command [arg ...]
With no targets named, every target whose running system differs from the union
is switched; naming targets, or @tag for the targets nixconvoy.toml tags so,
restricts switching to (at most) those.
--danger-network-config-change admits a switch that would stop, restart or
reload a unit the target's configuration protects (nixconvoy.protectedUnits).
--dry-activate stops before any switch, printing what each would do.
abandon --apply-now deploys at once, so the targets drop the abandoned work
now rather than at the next deploy."""
DRY_FLAG = "--dry-activate"
APPLY_NOW_FLAG = "--apply-now"


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return
    if args[0] in ("-h", "--help"):
        print(__doc__)
        print(USAGE)
        return
    if args[0] not in ("apply", "land", "commit", "abandon", "prune", "pull", "status", "review", "exec"):
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
    if subcmd not in ("status", "prune"):
        atexit.register(PERF.write, Path.cwd() / PERF_FILE)
    msg: str | None = None
    reset = False
    danger = False
    dry = False
    apply_now = False
    names: list[str] = []
    after = ""
    stopped = ""
    while args:
        a = args.pop(0)
        if a == "-m" and args:
            msg = args.pop(0)
        elif a == "--after" and subcmd == "prune" and args and args[0].isdigit():
            after = args.pop(0)
        elif a == "--stopped" and subcmd == "prune" and args and args[0].isdigit():
            stopped = args.pop(0)
        elif a == "--reset":
            reset = True
        elif a == NETWORK_FLAG:
            danger = True
        elif a == DRY_FLAG:
            dry = True
        elif a == APPLY_NOW_FLAG and subcmd == "abandon":
            apply_now = True
        elif a in ("-h", "--help"):
            print(USAGE)
            return
        elif not a.startswith("-"):
            names.append(a)
        else:
            die(USAGE)

    if subcmd == "pull":
        if msg or reset or danger or dry or names:
            die(USAGE)
        commands.pull()  # takes each checkout's lock itself, this one included
        return
    if subcmd == "status":
        if msg or reset or danger or dry or names:
            die(USAGE)
        commands.status()
        return
    if subcmd == "prune":
        if msg or reset or danger or dry or ((after or stopped) and names) or (after and stopped):
            die(USAGE)
        if stopped:
            stop = json.loads(sys.stdin.read() or "{}")
            commands.prune_stopped(int(stopped), stop if isinstance(stop, dict) else {})
        elif after:
            commands.prune_after(int(after))
        else:
            commands.prune(names)
        return
    top = Path.cwd()
    here = top.resolve()
    if subcmd in ("land", "abandon"):
        if msg or reset or dry or (danger and not apply_now):
            die(USAGE)
        named = [commands.find_sub(n) for n in names]
        deploys_from = here if names and here not in named else sub_parent(here) or here
        # The deploy runs from a checkout the abandon does not reset, whose
        # staged changes it would have to carry without a message.
        if (apply_now and (names or deploys_from != here)
                and not ok(["git", "diff", "--cached", "--quiet"], cwd=str(deploys_from))):
            die(f"{deploys_from} has staged changes, which the deploy of {APPLY_NOW_FLAG} "
                "would carry: deploy or unstage them first")
        # Several subagent worktrees, one after the other, each under its
        # own checkout's lock.
        for name in names:
            if len(names) > 1:
                print(f"== {name}")
            os.chdir(top)  # the previous one's removal moved into its parent
            commands.enter_sub(name)
            lock = lock_checkout()
            if lock is None:
                die(f"another nixconvoy is running in {name}'s worktree")
            if subcmd == "land":
                commands.land()
            else:
                commands.abandon(apply_now)
            lock.close()
        if names:
            if apply_now:
                os.chdir(deploys_from)  # the parent's, when the named worktree was this one
                take_lock()
                deploy([], None, False, danger)
            return
    take_lock()
    if subcmd == "land":
        commands.land()
    elif subcmd == "abandon":
        commands.abandon(apply_now)
        if apply_now:
            if Path.cwd().resolve() != here:
                take_lock()  # a subagent worktree's removal moved into its parent
            deploy([], None, False, danger)
    elif subcmd == "commit":
        if reset or danger or dry or names:
            die(USAGE)
        commands.commit_only(msg)
    else:  # apply
        deploy(names, msg, reset, danger, dry)


def take_lock() -> None:
    """The current checkout's deploy lock, held until exit."""
    lock = lock_checkout()
    if lock is None:
        die("another nixconvoy is running in this checkout")
    proc.HELD.append(lock)


if __name__ == "__main__":
    main()
