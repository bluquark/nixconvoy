"""nixconvoy - clobber-safe nixos-rebuild, branch-based, target-inferring.

Problem: several checkouts of one NixOS flake repo (worktrees, machines)
deploy work in progress to the same targets, so a plain nixos-rebuild from
one tree silently reverts what another tree already deployed.

Model:
  - main: mirror of origin/main; only finished (landed) commits.
  - A work branch, listed by prefix in nixconvoy.toml's work_branches and
    named with this machine's branch_prefix, is owned by the checkout that
    has it checked out; nixconvoy is its only writer: it commits the staged
    state onto it and pushes it after a successful deploy.  A deploy, land
    or undeploy finding origin's copy holding commits this checkout never
    pushed (nor took at open) is refused before it switches, pushes or
    resets anything: some other checkout writes it (one arriving while a
    deploy runs leaves the branch unpushed, the deploy failing at its end).  A checkout on
    main (or any other branch; another machine's work branch too, when
    branch_prefix has {machine}) is a main checkout: it deploys main plus
    the pushed work branches and owns nothing.  Without {machine}, every
    listed branch is this machine's to own: read another checkout's with
    `nixconvoy review`, never by checking it out.  A subagent worktree - one nested in a checkout,
    <checkout>/<child_worktrees>/<name> by `open` - owns <parent's branch>-<name>
    (<branch_prefix>main-<name> under a main checkout) from its first deploy.
    After a land, or an undeploy back to origin/main, a checkout stays on
    its work branch, reset onto origin/main.
  - COMMIT_MSG.md at a work branch's root describes the branch's work since
    its last landing.  It never reaches main's tree: `land` consumes it as
    the commit message, opened in git's editor first when run on a terminal
    (`land -m <msg>`, for a branch without one, lands under <msg>).
  - Targets are inferred: each target's ground truth is its running system
    (/run/current-system), compared against the union build.  Every deploy
    converges whatever differs.
  - The decisions - rebasing a checkout, the union and its retries, which
    drifting targets may be switched, the switch itself - are the switch-guard
    crate's (nixconvoy-plan, nixconvoy-switch: Sans-I/O cores); this program
    performs the git, eval, build and ssh they ask for.

Deploy (nixconvoy deploy [<target> | @<tag> ...] [-m <msg>] [--evict]
         [--dry-activate]): -m describes the
  staged increment the deploy commits (default: "<checkout>: increment
  <n>", n its number on the branch); with nothing staged the deploy
  converges the targets on the union as it is (the nightlies', and a main
  checkout's, deploy).
  1. start fetching origin; enumerate the targets from the hosts directory
  2. probe every target's running system and its provenance
     (/var/lib/nixconvoy/provenance.json, in the same round trip), concurrently
     with steps 3-4.  Candidates default to all targets; naming targets (or @tag, every
     target nixconvoy.toml gives that tag) restricts switching to (at most)
     those.  An unreachable candidate fails the deploy before anything is
     evaluated
  3. snapshot the staged state (index) as an ephemeral deterministic commit
     on top of the work branch, rebased onto the fetched origin/main;
     unstaged and untracked changes are excluded, since staging marks a
     change as ready to deploy.  Nothing is committed.
     The pending state - the increment, or a HEAD origin lacks (the
     nightly's lock commit on main) - is announced: its tree as a commit
     of its own, pushed to origin's refs/nixconvoy/deploying/branch/<branch> and
     renewed there every 10 s for as long as this run lives (a main
     checkout's under main@<machine>-<checkout>), so a deploy starting elsewhere sees
     this one under way (step 4a) until the push that ends this deploy
     clears it (step 8; a main checkout's, the push of main by its owner
     that lands the announced HEAD) - or this run withdraws it, ending
     without a switch.  Its author date is the run's start, its rank among
     the deploys under way (a re-run keeps it only while the announcement
     carries records: a switch onto the increment was started)
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
     each other); one whose announcement was last renewed over 10 min before
     the fetch that first showed it (its machine's clock against this one's),
     or goes 30 s without a renewal (by this machine's clock, as of
     successful fetches), is taken for dead and gone on without, with a
     warning, until it is renewed again (a re-run);
     one settled (over, its push pending: a main checkout's, which its
     owner pushes) is not waited for; --evict waits for nobody
  5. evaluate every reachable target's toplevel for the union, one nix eval
     process per stale target, concurrently, memoized per target by the git
     hashes of the files its configuration references and the lock entries
     of the flake inputs those files name: a change to a module only one
     target imports re-evaluates that target alone, and a no-op redeploy
     evaluates nothing.  The same eval reads the switch policy the target's
     configuration declares through the nixconvoy NixOS module (switch
     last, boot assessment).  Targets whose running system
     matches are up to date; the rest will be switched (their drift may be
     catch-up to main or other branches, not just this increment) - unless
     the provenance shows the target runs another checkout's increment that this
     union cannot contain: a switch whose push has not landed yet, or one
     of a branch work_branches here does not list.  Such a target is never
     switched as is: when this checkout's own work does not reach it (no
     file its configuration reads changed since the tip of this branch its
     provenance recorded) it is left alone, running that work; when it does,
     the deploy is refused whole.  An increment whose deploy is over - its
     announcement cleared (step 8) without the push landing - is
     abandoned: switched over with a warning; so is a refused target
     under --evict, when this work reaches it or the target was named.
     A dead deploy's announcement stays up, so its increment stays
     refused: the refusal names the deploy and the clear that ends it
     (git push origin :refs/nixconvoy/deploying/branch/<branch>),
     or the push of its branch that keeps it.  Work of a branch
     work_branches does not list stays refused until listed.  When the configuration
     names a precheck (a flake check), it runs on a thread beside the evals
     unless a pass is already proven for the union's tree (nix rebuilds it
     only when a file it reads changed), and a failure stops the deploy the
     moment it is known.  A drifting candidate
     whose boot counting rejected its new toplevel stops the deploy before
     anything is built.  Each target to be switched gets its deploy record:
     a commit of the union's tree whose message carries the provenance below,
     on the target's previous record (git log --first-parent is its deploy
     history), origin/main, each unioned branch's tip and the increment's
     commit, which is made now with its -m message and its trailers:
     Reaches:, the targets the increment reaches (a changed file their
     evaluation reads, or a lock entry it names); Deployed:, the candidates running the branch's tip once the
     deploy is over
  6. build the drifting candidates' closures straight from the evaluated
     .drvs (no re-eval), in the configured store host's store if any
     (build.store, by the machine deploying; its nix daemon may hand the
     builds on to its own remote builders), unsandboxed
     when a dry-run shows only trivial builders left.  Nothing drifting and
     no precheck to run means no build
  7. copy the closures over, concurrently, while re-fetching to check that
     no work branch moved during the build and renewing the announcement
     with the records as further parents of its commit, so no target ever
     runs a state origin does not hold (a deploy with nothing pending
     announces once it has targets to switch, for this alone).  Then every
     target not marked switch-last is switched at once and the switch-last
     ones alone after, activating the exact evaluated toplevel under a flock
     held ON THE TARGET that first re-verifies the target still runs what
     step 2 probed.  This compare-and-swap never reverts a switch another
     machine made meanwhile: the run probes the targets again and redoes
     steps 4-6 (memoized: seconds), twice at most, then fails.  The
     guard compares the provenance too, then claims the provenance file for the
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
  8. only now adopt the increment's commit onto the work branch and push,
     in the background and atomically (all of it lands or none), the
     branch, each switched target's record onto
     refs/heads/nixconvoy-history/<target> (and, with them, the record of
     any target an earlier run from this checkout switched without its
     push landing: its provenance names it) and the announcement's deletion
     (a main checkout's is settled instead: over, awaiting the owner's push
     of main) - the next run collecting the result before its own fetch.  An increment
     that fails to eval, build or switch
     is never committed, so other checkouts cannot pull a broken commit and
     pushed tips never overestimate what is running; its changes stay staged
     for the re-run, and its announcement is withdrawn - unless a target
     runs it already, or a switch onto it was started: then it stays, so
     a deploy elsewhere is refused there rather than reverting the
     increment, until the re-run ends with the push (or a hand clear)

An increment that changes nothing any target's evaluation reads (the eval
memo's read scan over the staged snapshot, no eval: docs, dotfiles, a
COMMIT_MSG.md) is committed onto the work branch and pushed without a
deploy - nothing evaluated, built or switched, step 2's probes dropped
unread, the commit's trailers Reaches: none and Deployed: none - after
the precheck unless
its tree has a proven pass; the precheck needs the store host when one
is set.  The targets' drift
(another branch's work, main's) waits for the next deploy reaching one, or
for a deploy naming targets, which never takes that shortcut; a deploy
with nothing staged converges every target on the union.  The scan sees the staged
snapshot alone: a reader another unlanded branch adds for a changed file
is caught up by that branch's deploys.

A target an increment reaches but a target filter left out is held back:
named by the commit's Reaches: trailer and not its Deployed: one, with no
later increment's deploy on the branch switching it since (a deploy with
nothing staged that switches it commits an empty increment saying so).  The
union every deploy builds carries that work, so until the holding checkout
deploys the target (nixconvoy deploy <target> there, or a deploy naming no
target) or lands with --not-yet-deployed, a deploy from any other work
branch leaves the target alone - or is refused when its own work reaches
it, as for an in-flight increment - a main checkout's deploy (the
nightly's) leaves it behind on main's advance, and `land` refuses without
--not-yet-deployed.  --evict unions no other
branch, so it switches the target to this checkout's view alone, without
the held work, the hold standing.  `status` shows what each checkout holds
back.

Land (nixconvoy land [<worktree> ... | --with-children] [-m <msg>] [--not-yet-deployed]):
commit the work branch's tree minus COMMIT_MSG.md onto origin/main with
COMMIT_MSG.md as the message (or -m <msg>, for a branch without the file:
both at once is refused).  Without -m, on a terminal, the message opens in
git's editor (`git var GIT_EDITOR`; `:` skips it) over a scissors line
listing the branch's increments and changed paths - an agent's COMMIT_MSG.md
adjusted by hand before it lands, an empty message aborting; off a terminal
(an agent's shell) COMMIT_MSG.md lands as it is.  The editor opens once every
check has passed, and the edit is kept in the git dir (nixconvoy/landmsg)
until it lands, so a landing that fails after it (a push rejected) re-runs
with the edit.  Then push
main, reset this branch (and its origin copy) onto the new main, the
checkout staying on it.  Every other checkout on this machine is then
brought onto the new main (see Pull); checkouts on other machines rebase
at their next deploy.  A subagent worktree stays too, idle on its reset branch:
`close` removes it (the session-exit sweep does, once its agent is done).
Run from any checkout with <worktree>s - each a subagent worktree's name
(a unique prefix will do) or a branch a worktree here is on - land and
undeploy act on each in turn.  A branch holding a target back (see
Deploy) is refused unless --not-yet-deployed: the landed work then rolls out
there with the next deploy from anywhere.  The checkout's submodules land first
(see Open).
--with-children lands this checkout and its subagent worktrees as one unit
- for subagents that each did a piece of this checkout's task: every sub's
branch (one on its work branch with work beyond main; a sub holding
undeployed commits stops it) is merged into this branch's tree as a
deploy's union merges them
(mergiraf on the conflicts), and that lands as one commit under this
checkout's COMMIT_MSG.md (or -m) with each sub's COMMIT_MSG.md appended;
each branch is reset onto the new main, each
worktree staying, its unstaged edits kept (an edited file the landing
changes stops it before main moves).

Pull (nixconvoy pull): fetch and bring this checkout and every sibling
checkout onto origin/main - a deploy's automatic rebase, for all of them at
once (a main checkout fast-forwards; a work branch not checked out is
rebased in a throwaway worktree).  Each checkout is taken under its deploy
lock (one whose nixconvoy is running is skipped) and either ends up rebased
with its staged/unstaged state intact and its work branch pushed, or is
left exactly as it was: a rebase that isn't clean is aborted and reported.
The rebase replays only what no landing has squashed yet (a branch sitting
on another's commits that main now holds as one commit replays its own
alone), and a conflict mergiraf solves structurally does not stop it.
Each submodule worktree of a checkout follows (see Open), onto its
repository's default branch as fetched, by the same rebase: fast-forwarded
when it has nothing of its own, its staged and unstaged state riding
along; one with commits of its own is pushed where its origin has the
branch, and a pin naming its old tip moved to the new, staged.  One that
cannot follow - its rebase not clean, its origin unreachable while a pin
waits on it - is named in the checkout's line, which then counts as
failed, and left as it was.  The submodule repository's own checkout, when
on its default branch, follows the same way, as the main checkout follows
main.

Status (nixconvoy status): after a fetch, every checkout on this machine as
a tree (the main checkouts, the work-branch ones, each with its subagent
worktrees): the branch or parked state, commits ahead of origin/main and
whether they are pushed, staged/unstaged/untracked counts, the
COMMIT_MSG.md subject, a deploy or git operation in progress - then the
work branches only origin holds (other machines' checkouts).

Orphans (nixconvoy orphans): after a fetch, this machine's subagent work
branches on origin that still union into every deploy while nobody holds
them: commits beyond origin/main and every other listed branch, and no
worktree here owning the branch, or one whose lock names a claude session
that is gone (a hand lock, a sessionless `open --child` and an unlocked
one are a person's, held).  One line per orphan, tab-separated: the
branch, the worktree's path or "(no worktree)", its COMMIT_MSG.md subject;
nothing when there is none.  Land or undeploy each.

Review (nixconvoy review [<worktree> | <submodule> | <branch> ... | --with-children]
[<diff-arg> ...]): the review of the pending landing (review.py; `git
review`, in the package's bin, is the same): COMMIT_MSG.md, then the diff
against the merge-base with origin's default branch, then the submodules'.
A name is a subagent
worktree's, a submodule's (that one alone) or a branch's (the worktree on
it, else the branch against its merge-base, so a submodule branch reviews
from its repository too); one that is none of these is an error;
--with-children reviews this checkout and then each subagent worktree; the
rest are git diff arguments.

Undeploy (nixconvoy undeploy [<worktree> ... | --with-children] [--back-to <rev>]):
undo this checkout's increments on the
targets, from HEAD back to <rev> (HEAD^ by default: the latest one;
origin/main, or anything older: the whole branch) - each a deploy's
increment, else refused: the work branch is reset onto <rev> (git reset
--keep: unstaged edits stay, unless they touch a file the undone
increments changed; staged changes are refused first), here and on
origin, each submodule whose pin they moved is reset onto <rev>'s pin
likewise (onto its default branch when that pin is landed), and a deploy
follows at once, from the checkout undeploy ran in, whose verdict names
what it reverts ("will switch (catch-up: ...; reverting <branch>'s
abandoned increment <sha>)").  The old tip remains
reachable via the reflog; untracked files survive; a subagent worktree
stays (close removes it).  <worktree>s: the subagent worktrees to do it
in (as for land), each in turn; --with-children: every subagent worktree holding
increments, then this checkout - one deploy for all, run here.  A
worktree never deployed has nothing to undeploy: close --force discards it.

Open (nixconvoy open <path> [--branch <leaf>] | --child <worktree>
| --submodule <input>): a worktree.  --child <worktree>: a subagent
worktree of that name, <root>/<child_worktrees>/<worktree> (the directory
nixconvoy.toml's `child_worktrees` names; a nested
worktree must be ignored, so the directory is
written into .git/info/exclude unless it is already; root: the checkout
this one's subagent worktrees hang off) on the fresh branch
worktree-<worktree> off origin's default branch, owning <root's work
branch>-<worktree> from its first deploy - what the Claude Code plugin's
create hook makes for an isolated subagent (see Hook); one already opened
is reused.  Opened by hand, it is locked as sessionless: the sweeps keep it
until `close <worktree>` names it.  A worktree nested in a checkout is its
subagent worktree wherever it was made.  <path> (for close, written as
one or with a / in it and no branch here): an agent checkout beside the
others - a worktree of this repository at that path,
outside every checkout, on a work branch of this machine's: --branch (a
leaf, or the whole name under this machine's prefix), else
<branch_prefix><path's name> (lab5 -> nixconvoy/lab5), refused when origin
holds that branch or a subagent branch under it - a checkout removed
before, or another machine's with the prefix shared - so that --branch
says which: the same name takes the branch over (origin's copy), another
opens a fresh one.  A branch open makes is pushed at once, so origin
holds it from then on and an open of the same name elsewhere is refused
(one this repository held already is pushed by its next deploy).  --submodule:
the flake input <input> developed in place - a git submodule of this
repository named after the input, taken by flake.nix as a relative path
input with inputs.self.submodules = true, so the gitlink in the tree is
the pin and the build fetches it from the .gitmodules url at that rev
(the README's "Submodules" has the model).  open plants a worktree of
its repository (nixconvoy.toml's submodules.<input>.repo, whose origin
that url must name; never `git submodule update`) at the submodule's
path, on a branch named after this checkout's work branch - origin's
copy of it when there is one, else new off the repository's default
branch.  Work there as in the checkout: stage what is ready, and each
deploy commits the worktree's staged state onto that branch with the -m
message, pushes it (before the build, which fetches it) and stages the
gitlink on the pushed rev (and, with submodules.<input>.pin_file set,
that file beside it naming the rev and its committer date, for the flake
to read) - the increment then carries the submodule as
a tree change.  Two work branches that moved the same submodule meet in
the union as a gitlink conflict, merged in the submodule repository and
pinned on refs/nixconvoy/union/<input>/<sha> of its origin; once one
lands, every other checkout's catch-up (a deploy's submodule step, pull,
a landing's) brings its worktree onto the landed default branch as it
brings the checkout onto main (see Pull).  A deploy refuses a submodule
still behind that branch, and a worktree reset below the work the tree
deploys (nothing of its own left: a hand reset; a rebase or amend keeps
work of its own and passes, the deployed commits it leaves behind named):
put it back, or `close <input> --force` drops it deliberately.  land
squashes each submodule's tip onto its default branch as one commit (the
COMMIT_MSG.md in the worktree, tracked or not, else the checkout's), pins
that commit on the work branch and lands main, then moves the default
branch onto the commit - main's push goes first, so refused, it leaves
every default branch as it was;
`close <input> --force` drops one, the pin put back to
origin/main's and staged for the next deploy to revert it.  status
lists the configured submodules.  A submodule repository's own checkout
is never deployed from: status flags edits there, and nixconvoy leaves a
`submodule` marker in its git dir's nixconvoy/ for a pre-commit hook to
refuse an agent's commit on its default branch there.

Close (nixconvoy close [<path> ... | <worktree> ... | <submodule> ...]
[--force]): remove worktrees, named as one kind.  The named subagent
worktrees (<worktree>: as for land) when they hold nothing of their own -
never deployed, no staged, unstaged or untracked change, HEAD and its
work branch on origin, no submodule holding anything - with their fresh
branch, as nixconvoy-plan's prune core decides (a named one's lock counts
its session as ended for it); --force removes a named one whatever it
holds, with its branches and submodules, here and on origin.  With no
name, every one under this checkout (a sweep), where one whose spawning
claude session still runs is kept, whichever session asks: the session
may resume its agent, which needs the worktree.  So is one at work (a
nixconvoy, git operation or push running in it), on a work branch (land
or undeploy decides it), locked by hand (or, in a sweep, unlocked) or
holding anything; each kept one is said so, as is one a process works
in.  It all happens under a lock on the worktree's name, which open (and
so a resume's relock) takes too, so the lock judged is the one the
removal lifts; git refuses an unclean worktree still.  The plugin's hooks run these removals on
their own as agents stop and sessions exit (see Hook).  <submodule> (a
name .gitmodules declares): its worktree with its branch, here and on its
origin (the last tip kept under refs/nixconvoy/past/<branch>/<sha> there
while a pushed branch or deploy record pins it), unless that would lose
work - edits, commits not on the default branch, a pin still off it -
which --force overrides, the pin then put back to origin/main's, staged;
the directory stays, empty, as an unpopulated submodule.  <path>: an
agent checkout (never the primary worktree), with its work branch here
and on origin, when it holds nothing of its own - no edits, nothing
beyond origin/main, no submodule holding anything - or --force.

Init (nixconvoy init [<flake-dir>]): nixconvoy.toml for a fresh install,
at the repository's root, refused when one is there.  Its required keys
are detected: flake_dir, the
directory holding flake.nix (<flake-dir>, the root, else the one no
deeper than any other); targets_dir, the directory whose subdirectories
best match the nixosConfigurations' names (`nix eval` after `nix flake
archive`, best effort; without it the one holding the most configuration.nix); targets_arch,
the one system literal the hosts' .nix files agree on, else the first
target's evaluated hostPlatform, else this machine's architecture as a
guess the file says to verify; ssh_address_template, "<this
user>@{target}", to edit; branch_prefix nixconvoy/ ({machine} in it, the
hostname, namespaces one machine's branches) and work_branches, that
prefix; child_worktrees
.claude/worktrees; eval.parallel "auto".  The optional sections follow as
commented examples filled with what was found: the flake's checks for
eval.precheck, the targets' names.  git is only read: what it wrote is
left for you to stage.

Hook (nixconvoy hook <event>): the Claude Code plugin's hook of that
event - session-start, subagent-stop, worktree-create, worktree-remove
or worktree-resume - run by integrations/claude-code/hooks/run.sh <event>
with the hook's JSON input on stdin, never by hand.  Each acts for the
claude session it runs under (the nearest claude ancestor of its
process) in the repository Claude Code runs it for (CLAUDE_PROJECT_DIR,
else the input's cwd; the worktree's own for worktree-remove and
worktree-resume); the two prunes and worktree-resume do nothing outside a
nixconvoy repository.  worktree-create:
`open --child <name>` for the input's name under the session's checkout, the
worktree locked with the session's pid and start time (how status and
close tell whether the session that may resume the agent still runs)
plus a token of this create, so a lock taken again - a reuse, a resume -
never reads as one a close judged before; with no session found it is
locked as sessionless; prints the path.  subagent-stop: detached, the
stopped agent's worktree (where its transcript starts) removed when it
holds nothing of its own, no process, background task or other running
agent of the session works in it and, a few seconds on, no resume
relocked it - whether its session still runs or not.  worktree-resume
(PreToolUse on SendMessage): a message to a stopped agent first has its
worktree recreated, or relocked, as worktree-create does; the resume is
refused (exit 2) when that fails.  session-start: detached, a sweep of
what dead sessions left, then, once this session exits, a sweep of what
its agents' stops kept (one waiter per session).  Both log to
nixconvoy/prune.log in the common git dir.  worktree-remove: Claude
Code letting go of a worktree the create hook made (a `claude
--worktree` session's, at its exit), removed with its branch unless
another live session holds it, it has changes, commits not on
origin/main or a submodule worktree planted in it, or close by name
would keep it; a kept one fails the hook, which leaves it on disk.
Outside a nixconvoy repository run.sh never calls nixconvoy: the prunes
and the resume do nothing there, and its create and remove do what
Claude Code's own git would.

--evict evicts every other work branch from the targets it switches: they
run this checkout's view alone, the other branches left out of the union
(and no deploy under way waited for), and a target running another
checkout's in-flight or unlisted work (step 5) is switched over too,
reverting that work, when this checkout's work reaches it or the target is
named (one left alone otherwise stays as it is).  It is a one-off:
the others return on their next deploy - the durable fix is to reset the
branch that carries the unwanted change (git push --force-with-lease origin
origin/main:<branch>) or repair it in its own checkout.

--dry-activate runs steps 1-7 up to the switch (the checkout is rebased and
the closures copied as for a deploy), dry-activating every target that would
be switched instead, and prints the units each would stop, restart or
reload.  Nothing is switched, committed or pushed, and the staged changes
need no -m; a dry activation that fails fails the run.

Env: NIXCONVOY_SKIP_SWITCH=1 exercises only the branch/union machinery: no
probe, eval, build or switch.  NIXCONVOY_FAIL_VERIFY=1 fails a confirmed
switch's verification, exercising the rollback.  NIXCONVOY_STORE
overrides the store host ("" realises locally).  NIXCONVOY_HOLD_PUSH=N delays
the background push N seconds, keeping the switched increment in flight."""

from __future__ import annotations

import atexit
import os
import re
import sys
from pathlib import Path
from typing import IO, NoReturn

from . import commands, config, hooks, init, names, orphans, proc, review, submodule
from .checkout import lock_checkout, lock_holder, note_prefix, require_work_branch, sub_parent
from .deploy import deploy
from .proc import PERF, TOOL, Fail, die, ok, out, painter

# The synopsis: one line per form under its section's heading (= ...).
# <x> a value, [x] optional, (a | b) one of, ... repeated.  USAGE_NOTES
# follows it under --help only.
SYNOPSIS = """\
= Setting up a repository
init [<flake-dir>]
= Managing worktrees
open <path> [--branch <leaf>]
open --child <worktree>
open --submodule <input>
close [<path> ... | <worktree> ... | <submodule> ...] [--force]
pull
status
orphans
hook <event> < <hook-input>
= Deploying and committing changes
deploy [<target> | @<tag> ...] [-m <msg>] [--evict] [--dry-activate]
undeploy [<worktree> ... | --with-children] [--back-to <rev>]
review [<worktree> | <submodule> | <branch> ... | --with-children] [<diff-arg> ...]
land [<worktree> ... | --with-children] [-m <msg>] [--not-yet-deployed]
"""
USAGE_NOTES = """\
-m describes the staged increment a deploy commits once its targets are
switched (default: "<checkout>: increment <n>"); with nothing staged, the
deploy converges the targets on the union as it is.
With no targets named, every target whose running system differs from the union
is switched; naming targets, or @tag for the targets nixconvoy.toml tags so,
restricts switching to (at most) those.
--dry-activate stops before any switch, printing what each would do.
land --not-yet-deployed lands work a target is held back from (increments
that reach it, deployed to other targets alone), for the next deploy anywhere
to roll out there; without it, such a landing is refused.
undeploy reverts this checkout's increments on the targets, from HEAD back
to --back-to <rev> (HEAD^ by default: the latest one; origin/main: the whole
branch): the work branch is reset onto <rev> (the reflog keeps the tip),
then a deploy follows at once.  --with-children acts on this checkout and
its subagent worktrees as one unit.  <worktree> is a subagent worktree's
name (a unique prefix will do) or a branch a worktree on this machine is
on: land and undeploy act in that worktree, so a branch with none here is
refused, while review, which only reads, takes any <branch> (against its
merge-base) and a <submodule>'s name too.  A <path> (with a /, no branch
here) is an agent checkout's.
hook <event> (session-start, subagent-stop, worktree-create,
worktree-remove or worktree-resume) is the plugin's hook of that event,
which its hooks/run.sh <event> runs with the hook's input on stdin - never
typed by hand.
init writes nixconvoy.toml for a fresh
install, every required key detected (the ssh address template as this
user, to edit)."""


def usage(stream: IO[str] | None = None) -> str:
    """The synopsis in its sections, colored when `stream` (stdout) is a
    terminal: the headings and the subcommand bold, flags yellow and the value a flag takes cyan,
    positional values green; inside [...] the brackets, "..." and "|"
    gray."""
    paint = painter(stream)
    depth = 0

    def color(m: re.Match[str]) -> str:
        nonlocal depth
        if m.group("open"):
            depth += 1
            return paint("[", "gray")
        if m.group("close"):
            depth -= 1
            return paint("]", "gray")
        if m.group("mark"):
            return paint(m.group("mark"), "gray") if depth else m.group("mark")
        if m.group("flag"):
            return paint(m.group("flag"), "yellow") + (
                " " + paint(m.group("fval"), "cyan") if m.group("fval") else "")
        return paint(m.group("pos"), "green")

    lines = ["usage:"]
    for form in SYNOPSIS.splitlines():
        if form.startswith("= "):
            lines += ["", paint(form[2:] + ":", "bold")]
            continue
        sub, _, rest = form.partition(" ")
        rest = re.sub(r"(?P<open>\[)|(?P<close>\])|(?P<mark>\.\.\.|\|)"
                      r"|(?P<flag>(?<![\w<])--?\w[\w-]*)(?: (?P<fval><[\w-]+>))?|(?P<pos>@?<[\w-]+>)", color, rest)
        lines.append(f"  {TOOL} {paint(sub, 'bold')} {rest}".rstrip())
    return "\n".join(lines)


def bad_usage() -> NoReturn:
    sys.stdout.flush()
    print(usage(sys.stderr), file=sys.stderr)
    sys.exit(1)


DRY_FLAG = "--dry-activate"
BACK_TO_FLAG = "--back-to"
# Each subcommand's flags, True for one taking a value (never one starting
# with -), and whether it takes positionals.  A value flag given twice: the
# last counts.
FLAGS: dict[str, tuple[dict[str, bool], bool]] = {
    "deploy": ({"-m": True, "--evict": False, DRY_FLAG: False}, True),
    "land": ({"-m": True, "--with-children": False, "--not-yet-deployed": False}, True),
    "undeploy": ({"--with-children": False, BACK_TO_FLAG: True}, True),
    "close": ({"--force": False}, True),
    "open": ({"--submodule": False, "--child": False, "--branch": True}, True),
    "pull": ({}, False),
    "status": ({}, False),
}


def main() -> None:
    try:
        dispatch()
    except Fail:  # said by die()
        sys.exit(1)


def dispatch() -> None:
    args = sys.argv[1:]
    if not args:
        print(usage())
        return
    if args[0] in ("-h", "--help"):
        print(__doc__)
        print(usage())
        print(USAGE_NOTES)
        return
    if args[0] not in ("deploy", "land", "undeploy", "close", "pull", "status", "review", "open", "hook", "init",
                       "orphans"):
        bad_usage()
    subcmd = args.pop(0)
    if subcmd == "hook":
        # Where Claude Code runs it, which need not be a repository.
        if len(args) != 1 or args[0] not in hooks.EVENTS:
            bad_usage()
        hooks.run_hook(args[0])
        return
    start = Path.cwd()
    os.chdir(out(["git", "rev-parse", "--show-toplevel"]))
    config.set_root(Path.cwd())
    if subcmd == "init":
        # Before anything reads nixconvoy.toml, which it writes.
        init_args(args, start)
    note_prefix(Path.cwd())
    if subcmd == "init":
        return
    if subcmd == "review":
        os.chdir(start)  # its paths are the caller's
        sys.exit(review.main(args))
    if subcmd == "orphans":
        if args == ["-h"] or args == ["--help"]:
            print(usage())
            return
        if args:
            bad_usage()
        orphans.main()
        return
    if subcmd not in ("status", "close", "open"):
        atexit.register(PERF.write, Path.cwd())
    takes, positionals = FLAGS[subcmd]
    flags: dict[str, list[str]] = {}
    given: list[str] = []
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            print(usage())
            print(USAGE_NOTES)
            return
        if a in takes:
            values = flags.setdefault(a, [])
            if takes[a]:
                if not args or args[0].startswith("-"):
                    bad_usage()
                values.append(args.pop(0))
        elif positionals and not a.startswith("-"):
            if a not in given:
                given.append(a)
        else:
            bad_usage()
    msg = flags["-m"][-1] if "-m" in flags else None
    back_to = flags[BACK_TO_FLAG][-1] if BACK_TO_FLAG in flags else None
    with_children = "--with-children" in flags
    not_yet_deployed = "--not-yet-deployed" in flags

    if subcmd == "pull":
        commands.pull()  # takes each checkout's lock itself, this one included
        return
    if subcmd == "status":
        commands.status()
        return
    if subcmd == "open":
        branch = flags["--branch"][-1] if "--branch" in flags else None
        sub_flag, child = "--submodule" in flags, "--child" in flags
        if len(given) != 1 or child + bool(branch) + sub_flag > 1:
            bad_usage()
        if sub_flag:
            submodule.create(given[0], require_work_branch("open --submodule"))
        elif child:
            commands.open_worktree(given[0], None)
        else:
            commands.open_checkout(start / Path(given[0]).expanduser(), branch)
        return
    if subcmd == "close":
        force = "--force" in flags
        # One kind of name per call: a submodule's (declared, configured or
        # not: never a worktree's), a path's, else a subagent worktree's.
        declared = names.gitmodule_names(Path.cwd())
        kinds = {"submodule" if n in declared else "path" if is_path(n) else "worktree" for n in given}
        if len(kinds) > 1:
            bad_usage()
        if kinds == {"path"}:
            take_lock()
            for n in given:
                commands.close_checkout(start / Path(n).expanduser(), force=force)
        elif kinds == {"submodule"}:
            take_lock()
            commands.close_submodules(given, force=force)
        else:
            commands.close_worktrees(given, force=force)
        return
    top = Path.cwd()
    here = top.resolve()
    if subcmd in ("land", "undeploy"):
        if (with_children and given) or (msg is not None and (not msg.strip() or len(given) > 1)):
            bad_usage()
        if with_children and subcmd == "land":
            take_lock()
            commands.land_with_children(msg, not_yet_deployed)
            return
        parent_too = False
        if with_children:
            # Each sub holding increments, then this checkout.
            if sub_parent(here) is not None:
                die("undeploy --with-children runs from the checkout the worktrees hang off")
            given = [str(d) for d in commands.subs_of(here) if commands.has_increments(d)]
            parent_too = commands.has_increments(here)
            if not given and not parent_too:
                die("nothing to undeploy here or in any subagent worktree")
        # Each worktree once, however many of its names were given.
        each: dict[Path, str] = {}
        for n in given:
            each.setdefault(Path(n) if with_children else names.worktree(n, here, subcmd, subagent=False), n)
        given, named = list(each.values()), list(each.keys())
        deploys_from = here
        # The deploy runs from a checkout the undeploy does not reset, whose
        # staged changes it would have to carry without a message.
        if (subcmd == "undeploy" and given and here not in named and not parent_too
                and not ok(["git", "diff", "--cached", "--quiet"], cwd=str(deploys_from))):
            die(f"{deploys_from} has staged changes, which the deploy after the undeploy "
                "would carry: deploy or unstage them first")
        # Several subagent worktrees, one after the other, each under its
        # own checkout's lock.  An undeploy that fails in one stops none of
        # the others, and what the rest undid is deployed all the same.
        due, failed = False, []
        for name, d in zip(given, named, strict=True):
            if len(given) > 1 or parent_too:
                print(f"== {d.name}")
            os.chdir(d)
            lock = lock_checkout()
            if lock is None:
                die(f"{lock_holder()} is running in {name}'s worktree")
            if subcmd == "land":
                commands.land(msg, not_yet_deployed)
            else:
                try:
                    due |= commands.undeploy(back_to or "HEAD^")
                except Fail:
                    failed.append(d.name)
            lock.close()
        if given or with_children:
            if subcmd == "undeploy":
                os.chdir(deploys_from)
                take_lock()
                if parent_too:
                    print(f"== {here.name}")
                    try:
                        due |= commands.undeploy(back_to or "HEAD^")
                    except Fail:
                        failed.append(here.name)
                if due:
                    deploy([], None, False)
                if failed:
                    die(f"undeploy failed in {', '.join(failed)} (see above)")
            return
    take_lock()
    if subcmd == "land":
        commands.land(msg, not_yet_deployed)
    elif subcmd == "undeploy":
        if commands.undeploy(back_to or "HEAD^"):
            deploy([], None, False)
    else:  # deploy
        dry = DRY_FLAG in flags
        if dry and msg is not None:
            # Accepted so a deploy's command line reruns with the flag added.
            print(f"note: -m does nothing with {DRY_FLAG}: nothing is committed", file=sys.stderr)
        deploy(given, msg, "--evict" in flags, dry)


def init_args(args: list[str], start: Path) -> None:
    """init's command line: at most one <flake-dir>, relative to where
    nixconvoy was run."""
    flake: str | None = None
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            print(usage())
            print(USAGE_NOTES)
            return
        if a.startswith("-") or flake is not None:
            bad_usage()
        flake = a
    init.main(flake, start)


def is_path(arg: str) -> bool:
    """A close argument naming an agent checkout by its path rather than a
    subagent worktree by its name (no / in one) or a branch
    (work/<machine>/<leaf> has slashes too): one written as a path, or with a /
    in it when it is no branch of this repository."""
    if arg.startswith(("/", "~", "./", "../")) or arg.endswith("/"):
        return True
    return "/" in arg and not ok(["git", "rev-parse", "-q", "--verify", f"refs/heads/{arg}"])


def take_lock() -> None:
    """The current checkout's deploy lock, held until exit."""
    lock = lock_checkout()
    if lock is None:
        die(f"{lock_holder()} is running in this checkout")
    proc.HELD.append(lock)


if __name__ == "__main__":
    main()
