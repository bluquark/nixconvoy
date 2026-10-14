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
    nothing.  A subagent (task) worktree - one nested in a checkout,
    <checkout>/<worktrees>/<name> by `open` - owns <parent's branch>-<name>
    (<branch_prefix>main-<name> under a main checkout) from its first deploy.
    After a land or abandon a checkout stays on its work branch, reset onto
    origin/main.
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

Deploy (nixconvoy apply [-m <msg>] [--reset] [--allow <protection> ...]
         [--dry-activate] [<target> | @<tag> ...]):
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
     commit, which is made now with its -m message and its trailers:
     Reaches:, the targets the increment reaches (a changed file their
     evaluation reads, or a lock entry it names) among those deployable
     from here; Deployed:, the candidates running the branch's tip once the
     deploy is over
  6. build the drifting candidates' closures straight from the evaluated
     .drvs (no re-eval), on the configured builder if any, unsandboxed
     when a dry-run shows only trivial builders left.  Nothing drifting and
     no precheck to run means no build
  7. copy the closures over, concurrently, while re-fetching to check that
     no work branch moved during the build and pushing the records to
     origin's refs/nixconvoy/deploying/<target>, so no target ever runs a
     state origin does not hold.  A target with protected units
     (nixconvoy.protected.<name>) is dry-activated once its closure has
     landed: if the switch would stop, restart or reload one of them, the
     deploy stops before any target is switched unless --allow <name> was
     given, the refusal quoting the group's why and the command that admits
     it.  Then every
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

An increment that changes nothing any target's evaluation reads (the eval
memo's read scan over the staged snapshot, no eval: docs, dotfiles, a
COMMIT_MSG.md) is committed onto the work branch and pushed without a
deploy - nothing probed, evaluated, built or switched, the commit's
trailers Reaches: none and Deployed: none - after the precheck when a
staged file is one it reads.  Nothing need be reachable.  The targets' drift
(another branch's work, main's) waits for the next deploy reaching one, or
for an apply naming targets, which always deploys; an apply with nothing
staged converges every target on the union.  The scan sees the staged
snapshot alone: a reader another unlanded branch adds for a changed file
is caught up by that branch's deploys.

A target an increment reaches but a target filter left out is held back:
named by the commit's Reaches: trailer and not its Deployed: one, with no
later increment's deploy on the branch switching it since (a deploy with
nothing staged that switches it commits an empty increment saying so).  The
union every deploy builds carries that work, so until the holding checkout
deploys the target (nixconvoy apply <target> there, or an apply naming no
target) or lands with --release, a deploy from any other work branch leaves
the target alone - or is refused when its own work reaches it, as for an
in-flight increment - a main checkout's deploy (the nightly's) leaves it
behind on main's advance, and `land` refuses.  --reset unions no other
branch, so it switches the target to this checkout's view alone, without
the held work, the hold standing.  `status` shows what each checkout holds
back.

Land (nixconvoy land [<name> ...] | --union | --annex <input>): commit the work branch's
tree minus COMMIT_MSG.md onto origin/main with COMMIT_MSG.md as the message,
push main, reset this branch (and its origin copy) onto the new main, the
checkout staying on it.  Every other checkout on this machine is then
brought onto the new main (see Pull); checkouts on other machines rebase at
their next deploy.  A subagent worktree stays too, idle on its reset branch:
`close` removes it (the session-exit sweep does, once its agent is done).
Run from any checkout with the worktrees' <name>s (a unique prefix will do),
land and abandon act on each in turn.  A branch holding a target back (see
Apply) is refused unless --release: the landed work then rolls out there with
the next deploy from anywhere.  The checkout's annexes land first
(see Annexes); --annex <input> lands that one annex alone.  --union lands
this checkout and its subagent worktrees as one unit - for subagents that
each did a piece of this checkout's task: every sub's branch (one on its
work branch with work beyond main; a sub holding undeployed commits stops
it) is merged into this branch's tree as a deploy's union merges them
(mergiraf on the conflicts), and that lands as one commit under this
checkout's COMMIT_MSG.md; each branch is reset onto the new main, each
worktree staying, its unstaged edits kept (an edited file the landing
changes stops it before main moves).

Pull (nixconvoy pull): fetch and bring this checkout and every sibling
checkout onto origin/main - a deploy's automatic rebase, for all of them at
once (a main checkout fast-forwards; a work branch not checked out is
rebased in a throwaway worktree).  Each checkout is taken under its deploy
lock (one whose nixconvoy is running is skipped) and either ends up rebased
with its staged/unstaged state intact and its work branch pushed, or is
left exactly as it was: a rebase that isn't clean is aborted and reported.
Each annex worktree of a checkout follows (see Annexes), onto its
repository's default branch as fetched, by the same rebase: fast-forwarded
when it has nothing of its own, its staged and unstaged state riding
along; one with commits of its own is pushed where its origin has the
branch, and a lock naming its old tip relocked on the new, staged.  One
that cannot follow - its rebase not clean, its origin unreachable while a
relock waits on it - is named in the checkout's line, which then counts
as failed, and left as it was.

Status (nixconvoy status): after a fetch, every checkout on this machine as
a tree (the main checkouts, the work-branch ones, each with its subagent
worktrees): the branch or parked state, commits ahead of origin/main and
whether they are pushed, staged/unstaged/untracked counts, the
COMMIT_MSG.md subject, a deploy or git operation in progress - then the
work branches only origin holds (other machines' checkouts).

Review (nixconvoy review [<worktree> | <branch> ...] [<diff-arg> ...] | review
--union [<diff-arg> ...]): the review of
the pending landing (review.py; `git review`, in the package's bin, is
the same): COMMIT_MSG.md, then the diff against the merge-base with
origin's default branch, then the annexes'.  A name is a subagent
worktree's, an annex's (that annex alone) or a branch's (the worktree on
it, else the branch against its merge-base, so an annex branch reviews
from its repository too); one that is none of these is an error; --union reviews this checkout and then each
subagent worktree; the rest are git diff arguments.

Abandon (nixconvoy abandon [--apply-now] [<name> ...] | --union | --annex <input>):
discard this checkout's work instead of landing it: hard-reset the checkout
and reset the work branch (local and origin) onto origin/main, staying on
it; a subagent worktree stays too (close removes it); its annexes are
reset onto their default branch likewise.  Untracked files survive; the
old tip remains reachable via the reflog.
No target is touched: the ones running its increments (named from their
Deployed: trailers) keep running them until the next deploy by
anyone, whose verdict names what it reverts ("will switch (catch-up: ...;
reverting <branch>'s abandoned increment <sha>)").  --apply-now runs that
deploy at once, so the targets drop the work now, from the checkout abandon
ran in; it takes --allow.  --union abandons each
subagent worktree holding anything, then this checkout.

Open (nixconvoy open [--lock <reason>] <name> | [--branch <work-branch>] <path> |
--annex <input>): a worktree.  <name> (no / in it): a task worktree,
<root>/<worktrees>/<name> (nixconvoy.toml's `worktrees`, `worktrees/` by
default, which must be gitignored; root: the checkout this one's subagent
worktrees hang off) on the fresh branch worktree-<name> off origin's
default branch, owning <root's work branch>-<name> from its first deploy
- what Claude Code's worktree isolation makes through the plugin's
WorktreeCreate hook, which runs this with --lock naming its session; a
name already opened is reused.  A worktree nested in a checkout is its
subagent worktree wherever it was made.  <path> (with a /): an agent
checkout beside the others - a worktree of this repository at that path,
outside every checkout, on a work branch of this machine's: --branch, else
<branch_prefix><N> for the digits the path's name ends with (homelab5 ->
work/<machine>/5) when that is listed and free, else the first listed free
number; origin's copy of the branch when it has one (a checkout removed
before), else new off origin's default branch.  --annex:
the flake input <input> developed in place: a worktree of its repository
(nixconvoy.toml's annexes.<input>.repo, whose origin the lock fetches) at
<checkout>/annexes/<input>, on a branch named after this checkout's work
branch - origin's copy of that branch when it has one, else new off the
repository's default branch, with its upstream set there.  annexes/ must
be gitignored (a nested checkout).  Work there as in the checkout: stage
what is ready, and each deploy commits the annex's staged
state onto that branch with the -m message, pushes it to the annex's
origin and points the flake.lock node at the pushed rev (nix flake lock
--override-input on a scratch copy of the flake, so the input's own
inputs follow), staged here, so the increment carries the annex as a lock
change: the union, the eval memo and the lineage see nothing new.  Two work branches that moved the same annex meet in the union as
a lock conflict the union resolves node by node: the annex branches are
merged in the annex repository (its configured checkout, else a bare
mirror under ~/.cache/nixconvoy/annexes/), the merge pushed to the annex
origin's refs/nixconvoy/union/<input>/<sha> and the union's node locked
there - deterministic, so retries and the eval memo agree; a union ref no
ref of origin (nor its deploy history of 90 days) locks any more is deleted
by the next union, a day after its push; annex branches that conflict are
reported as any union conflict.  Once one lands, every other checkout's catch-up (a deploy's annex step,
pull, a landing's) brings its annex worktree onto the landed default
branch as it brings the checkout onto main (see Pull), and a deploy whose
annex is still behind that branch refuses to relock it there, since the
lock would move the input backwards for every target main reaches; a
deployed relock the rebase onto main replays meets the landed node as a
lock conflict, resolved the same way.  The annex branch is pushed before the build, since the build fetches
it, so it may run ahead of what is deployed; the work branch's lock is the
claim.  land lands each annex first - its tip merged onto the
repository's default branch and squashed there as one commit (the
COMMIT_MSG.md in the annex worktree, tracked or not, else the checkout's)
- and relocks the node on the default branch as a commit of the work
branch, then lands main; the annex worktree stays, its branch reset onto
the landed commit.  A deploy refuses an annex whose worktree was reset below the work the
lock deploys (nothing of its own left: a hand reset; a rebase or amend
keeps work of its own and passes, the deploy naming the deployed commits
its relock leaves behind as such): put it back, or `abandon --annex`
drops it deliberately.  `land --annex <input>` lands that one annex ahead of
the checkout (the relock commit pushed on the work branch); `abandon
--annex <input>` resets it onto the default branch and puts its lock node
back to what origin/main locks, staged for the next deploy to revert it on
the targets; the checkout's own abandon resets its annexes so.  status
lists the configured annexes.  An annex repository's own checkout is never
deployed from: status flags edits there, and nixconvoy leaves a
`nixconvoy-annexed` marker in its git dir for a pre-commit hook to refuse
an agent's commit on its default branch.

Close (nixconvoy close [--force] [<name> ...] | [--force] <path> ... | --annex <input> |
--after PID | --stopped PID): remove a worktree.  The named task worktrees when they hold
nothing of their own - never deployed, no staged, unstaged or untracked
change, HEAD and its work branch on origin, no annex holding anything -
with their fresh branch, as nixconvoy-plan's prune core decides (a named
one's lock counts its session as ended for it); --force removes a named
one whatever it holds, with its branches and annexes, here and on origin.
With no name, every one under this checkout (a sweep), where one whose
spawning claude session still runs is kept, whichever session asks: the
session may resume its agent, which needs the worktree.  So is one at
work (a nixconvoy, git operation or push running in it), on a work branch
(land or abandon decides it), locked by hand (or, in a sweep, unlocked) or
holding anything; each kept one is said so, as is one a process works in.
It all happens under a lock on the worktree's name, which open takes too,
and the removal claims the lock it judged and leaves git to refuse a
worktree locked meanwhile.  --annex <input>: that annex's worktree with
its branch, here and on its origin (the last tip kept under
refs/nixconvoy/past/<branch>/<sha> there for 90 days), unless that would lose work - edits, commits
not on the default branch, a lock still naming its branch - which --force
overrides, the lock node then put back to origin/main's, staged.  <path>:
an agent checkout (never the primary worktree), with its work branch here
and on origin, when it holds nothing of its own - no edits, nothing beyond
origin/main, no annex holding anything - or --force.  `--after
PID` detaches, sweeps what dead sessions left, waits for the claude session
PID to exit and sweeps again (one waiter per session, logged to
nixconvoy-prune.log in the common git dir): the Claude Code plugin's
SessionStart hook starts it, so a subagent's worktree with nothing of its
own goes when its session ends.  `--stopped PID`, the SubagentStop hook's,
takes that hook's input on stdin and, detached, removes the stopped
agent's worktree sooner, at its stop, when it holds nothing of its own, no
process or other running agent works in it and, a few seconds on, the
agent has not run again (a message or Claude Code's own follow-up turn)
and no resume relocked it, whether session PID runs or not: resuming the
agent recreates it (the PreToolUse hook on SendMessage).  It keeps a
worktree whose lock names no session.

--reset deploys this checkout's view alone: the other work branches are
left out of the union (and no deploy under way is waited for), so every
target switched loses their work, and a target
running another checkout's in-flight or unlisted work (step 5) is switched
over too, reverting that work, when this checkout's work reaches it or the
target is named (one left alone otherwise stays as it is).  It is a one-off:
the others return on their next deploy - the durable fix is to reset the
branch that carries the unwanted change (git push --force-with-lease origin
origin/main:<branch>) or repair it in its own checkout.

--allow <protection> admits a switch that would stop, restart or reload a
unit of that protected group (step 7), for this run only; repeat it for
several.

--dry-activate runs steps 1-7 up to the switch (the checkout is rebased and
the closures copied as for a deploy), dry-activating every target that would
be switched instead, and prints the units each would stop, restart or
reload.  Nothing is switched, committed or pushed, and the staged changes
need no -m; the run fails where a deploy would stop: a dry activation that
fails, or protected units the switch would touch without --allow.

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
import re
import sys
from pathlib import Path
from typing import IO, NoReturn

from . import annex, commands, proc, review
from .checkout import lock_checkout, require_work_branch, sub_parent
from .deploy import deploy
from .proc import PERF, PERF_FILE, TOOL, die, git, ok, out, painter
from .targets import ALLOW_FLAG

# The synopsis: one line per form.  <x> a value, [x] optional, (a | b) one
# of, ... repeated.  USAGE_NOTES follows it under --help only.
SYNOPSIS = """\
apply [-m <msg>] [--reset] [--allow <protection> ...] [--dry-activate] [<target> | @<tag> ...]
review [<worktree> | <branch> ...] [<diff-arg> ...]
review --union [<diff-arg> ...]
land [--release] [<worktree> ...]
land [--release] --union
land --annex <input>
abandon [--apply-now [--allow <protection> ...]] [<worktree> ...]
abandon [--apply-now [--allow <protection> ...]] --union
abandon --annex <input>
open [--lock <reason>] <name>
open [--branch <work-branch>] <path>
open --annex <input>
close [--force] [<worktree> ...]
close [--force] <path> ...
close [--force] --annex <input>
close --after <pid>
close --stopped <pid> < <hook-input>
pull
status
"""
USAGE_NOTES = """\
With no targets named, every target whose running system differs from the union
is switched; naming targets, or @tag for the targets nixconvoy.toml tags so,
restricts switching to (at most) those.
--allow <protection> admits a switch that would stop, restart or reload a unit
of a group the target's configuration protects (nixconvoy.protected.<name>).
--dry-activate stops before any switch, printing what each would do.
land --release lands work a target is held back from (increments that reach
it, deployed to other targets alone), for the next deploy anywhere to roll
out there.
abandon --apply-now deploys at once, so the targets drop the abandoned work
now rather than at the next deploy.  --union acts on this checkout and its
subagent worktrees as one unit; a path (with a /) names an agent checkout."""


def usage(stream: IO[str] | None = None) -> str:
    """The synopsis, colored when `stream` (stdout) is a terminal: the
    subcommand bold, flags yellow and the value a flag takes cyan,
    positional values green."""
    paint = painter(stream)

    def color(m: re.Match[str]) -> str:
        if m.group("flag"):
            return paint(m.group("flag"), "yellow") + (
                " " + paint(m.group("fval"), "cyan") if m.group("fval") else "")
        return paint(m.group("pos"), "green")

    lines = []
    for form in SYNOPSIS.splitlines():
        sub, _, rest = form.partition(" ")
        rest = re.sub(r"(?P<flag>(?<![\w<])--?\w[\w-]*)(?: (?P<fval><[\w-]+>))?|(?P<pos>@?<[\w-]+>)",
                      color, rest)
        lines.append(f"{TOOL} {paint(sub, 'bold')} {rest}".rstrip())
    lines[0] = "usage: " + lines[0]
    return "\n".join("       " + ln if i else ln for i, ln in enumerate(lines))


def bad_usage() -> NoReturn:
    sys.stdout.flush()
    print(usage(sys.stderr), file=sys.stderr)
    sys.exit(1)


DRY_FLAG = "--dry-activate"
APPLY_NOW_FLAG = "--apply-now"


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(usage())
        return
    if args[0] in ("-h", "--help"):
        print(__doc__)
        print(usage())
        print(USAGE_NOTES)
        return
    if args[0] not in ("apply", "land", "abandon", "close", "pull", "status", "review", "open"):
        bad_usage()
    subcmd = args.pop(0)
    start = Path.cwd()
    os.chdir(out(["git", "rev-parse", "--show-toplevel"]))
    # A dead run's leftovers, seen from every worktree; the age spares a
    # live run's throwaway worktree in a private /tmp this process cannot see.
    git("worktree", "prune", "--expire", "1.day.ago", check=False)
    if subcmd == "review":
        os.chdir(start)  # its paths are the caller's
        sys.exit(review.main(args))
    if subcmd not in ("status", "close", "open"):
        atexit.register(PERF.write, Path.cwd() / PERF_FILE)
    msg: str | None = None
    reset = False
    allow: set[str] = set()
    dry = False
    apply_now = False
    names: list[str] = []
    after = ""
    stopped = ""
    annex_name: str | None = None
    annex_flag = False
    lock_reason: str | None = None
    branch: str | None = None
    force = False
    union = False
    release = False
    while args:
        a = args.pop(0)
        if a == "-m" and args:
            msg = args.pop(0)
        elif a == "--annex" and subcmd == "open":
            annex_flag = True
        elif a == "--annex" and subcmd in ("land", "abandon", "close") and args:
            annex_name = args.pop(0)
        elif a == "--lock" and subcmd == "open" and args:
            lock_reason = args.pop(0)
        elif a == "--branch" and subcmd == "open" and args:
            branch = args.pop(0)
        elif a == "--union" and subcmd in ("land", "abandon"):
            union = True
        elif a == "--release" and subcmd == "land":
            release = True
        elif a == "--force" and subcmd == "close":
            force = True
        elif a == "--after" and subcmd == "close" and args and args[0].isdigit():
            after = args.pop(0)
        elif a == "--stopped" and subcmd == "close" and args and args[0].isdigit():
            stopped = args.pop(0)
        elif a == "--reset":
            reset = True
        elif a == ALLOW_FLAG and args and not args[0].startswith("-"):
            allow.add(args.pop(0))
        elif a == DRY_FLAG:
            dry = True
        elif a == APPLY_NOW_FLAG and subcmd == "abandon":
            apply_now = True
        elif a in ("-h", "--help"):
            print(usage())
            print(USAGE_NOTES)
            return
        elif not a.startswith("-"):
            names.append(a)
        else:
            bad_usage()

    if subcmd == "pull":
        if msg or reset or allow or dry or names:
            bad_usage()
        commands.pull()  # takes each checkout's lock itself, this one included
        return
    if subcmd == "status":
        if msg or reset or allow or dry or names:
            bad_usage()
        commands.status()
        return
    if subcmd == "open":
        if msg or reset or allow or dry or len(names) != 1 or (branch and (annex_flag or lock_reason)) \
                or (annex_flag and lock_reason):
            bad_usage()
        if annex_flag:
            annex.create(names[0], require_work_branch("open --annex"))
        elif is_path(names[0]):
            if lock_reason:
                bad_usage()
            commands.open_checkout(start / Path(names[0]).expanduser(), branch)
        elif branch:
            bad_usage()
        else:
            commands.open_worktree(names[0], lock_reason)
        return
    if annex_name is not None:
        if names or apply_now or msg or reset or dry or allow or after or stopped or union or release:
            bad_usage()
        take_lock()
        if subcmd == "land":
            commands.land_annex(annex_name)
        elif subcmd == "abandon":
            commands.abandon_annex(annex_name)
        else:
            commands.close_annex(annex_name, force=force)
        return
    if subcmd == "close":
        if msg or reset or allow or dry or ((after or stopped) and (names or force)) or (after and stopped):
            bad_usage()
        if stopped:
            stop = json.loads(sys.stdin.read() or "{}")
            commands.prune_stopped(int(stopped), stop if isinstance(stop, dict) else {})
        elif after:
            commands.prune_after(int(after))
        elif any(is_path(n) for n in names):
            if not all(is_path(n) for n in names):
                bad_usage()
            take_lock()
            for n in names:
                commands.close_checkout(start / Path(n).expanduser(), force=force)
        else:
            commands.close_worktrees(names, force=force)
        return
    top = Path.cwd()
    here = top.resolve()
    if subcmd in ("land", "abandon"):
        if msg or reset or dry or (allow and not apply_now) or (union and names):
            bad_usage()
        if union and subcmd == "land":
            take_lock()
            commands.land_union(release)
            return
        parent_too = False
        if union:
            # Each sub holding anything, then this checkout.
            if sub_parent(here) is not None:
                die("abandon --union runs from the checkout the worktrees hang off")
            names = [str(d) for d in commands.subs_of(here) if commands.holds_work(d)]
            parent_too = commands.holds_work(here)
            if not names and not parent_too:
                die("nothing to abandon here or in any subagent worktree")
        named = [Path(n) if union else commands.find_sub(n) for n in names]
        deploys_from = here
        # The deploy runs from a checkout the abandon does not reset, whose
        # staged changes it would have to carry without a message.
        if (apply_now and names and here not in named and not parent_too
                and not ok(["git", "diff", "--cached", "--quiet"], cwd=str(deploys_from))):
            die(f"{deploys_from} has staged changes, which the deploy of {APPLY_NOW_FLAG} "
                "would carry: deploy or unstage them first")
        # Several subagent worktrees, one after the other, each under its
        # own checkout's lock.
        for name, d in zip(names, named, strict=True):
            if len(names) > 1 or parent_too:
                print(f"== {d.name}")
            os.chdir(d)
            lock = lock_checkout()
            if lock is None:
                die(f"another nixconvoy is running in {name}'s worktree")
            if subcmd == "land":
                commands.land(release)
            else:
                commands.abandon(apply_now)
            lock.close()
        if names or union:
            if parent_too or apply_now:
                os.chdir(deploys_from)
                take_lock()
            if parent_too:
                print(f"== {here.name}")
                commands.abandon(apply_now)
            if apply_now:
                deploy([], None, False, frozenset(allow))
            return
    take_lock()
    if subcmd == "land":
        commands.land(release)
    elif subcmd == "abandon":
        commands.abandon(apply_now)
        if apply_now:
            deploy([], None, False, frozenset(allow))
    else:  # apply
        deploy(names, msg, reset, frozenset(allow), dry)


def is_path(arg: str) -> bool:
    """An open/close argument naming an agent checkout by its path rather
    than a task worktree by its name."""
    return "/" in arg or arg.startswith("~")


def take_lock() -> None:
    """The current checkout's deploy lock, held until exit."""
    lock = lock_checkout()
    if lock is None:
        die("another nixconvoy is running in this checkout")
    proc.HELD.append(lock)


if __name__ == "__main__":
    main()
