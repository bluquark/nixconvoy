"""Every sentence nixconvoy says: what die() and the notes print, the
verdicts, the help, and the prose in what it writes for people (the
commit messages it composes, the nixconvoy.toml init writes).  The
switch-guard crate's are in switch-guard/src/strings.rs.

A message is a template over named fields, filled by calling it -
LAND_NO_MSG_FILE(wb=wb) - and the vocabulary below fills itself ({TOOL},
{CONFIG_FILE}, ...).  A literal brace is doubled: {{machine}}.  A text
with no fields at all is a plain str, used as it is.  Edit the wording
freely; keep each {field}."""

from __future__ import annotations

# Names the code matches or parses too (the flags, the files): changing one
# here changes what it accepts, so it is not wording.
TOOL = "nixconvoy"
CONFIG_FILE = "nixconvoy.toml"
MSG_FILE = "COMMIT_MSG.md"
EVICT_FLAG = "--evict"
DRY_FLAG = "--dry-activate"
BACK_TO_FLAG = "--back-to"
# The tail of every failure before any target is switched (the work branch is
# committed only after the switches).
NOTHING_DONE = "nothing was switched or committed"
VOCAB = {k: v for k, v in globals().items() if k.isupper() and isinstance(v, str)}


class M(str):
    """A message template; calling it fills its fields and the vocabulary."""

    __slots__ = ()

    def __call__(self, **fields: object) -> str:
        return self.format(**VOCAB, **fields)



# --- cli.py: help and usage (--help prints HELP, the usage, then USAGE_NOTES)
HELP = """nixconvoy - clobber-safe nixos-rebuild, branch-based, target-inferring.

Problem: several checkouts of one NixOS flake repo (worktrees, machines)
deploy work in progress to the same targets, so a plain nixos-rebuild from
one tree silently reverts what another tree already deployed.

Model (origin and main below are nixconvoy.toml's `remote` and
`main_branch`, those names unless set):
  - main: mirror of origin/main; only finished (landed) commits.
  - A work branch, one shaped like nixconvoy.toml's branch_prefix ({machine}
    in it any hostname) and named with this machine's, is owned by the checkout that
    has it checked out; nixconvoy is its only writer: it commits the staged
    state onto it and pushes it after a successful deploy.  A deploy, land
    or undeploy finding origin's copy holding commits this checkout never
    pushed (nor took at open) is refused before it switches, pushes or
    resets anything: some other checkout writes it (one arriving while a
    deploy runs leaves the branch unpushed, the deploy failing at its end).  A checkout on
    main (or any other branch; another machine's work branch too, when
    branch_prefix has {machine}) is a main checkout: it deploys main plus
    the pushed work branches and owns nothing.  Without {machine}, every
    work branch is this machine's to own: read another checkout's with
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
  1. start fetching origin; enumerate the targets (the hosts directory's
     subdirectories, or the [targets.<name>] tables without targets_dir)
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
     evaluates nothing.  The same eval makes the target's switch kit (the
     confirmed-switch helper, its deadline and checks from nixconvoy.toml's
     [switch] tables, against the target's own nixpkgs) and reads whether
     its configuration carries the boot-assessment module.  Targets whose running system
     matches are up to date; the rest will be switched (their drift may be
     catch-up to main or other branches, not just this increment) - unless
     the provenance shows the target runs another checkout's increment that this
     union cannot contain: a switch whose push has not landed yet, or one
     of a branch not shaped like branch_prefix here.  Such a target is never
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
     or the push of its branch that keeps it.  Work of a branch not
     shaped like branch_prefix stays refused until it is.  When the configuration
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
     builds on to its own remote builders), as root through sudo -n where
     that daemon does not trust the user, unsandboxed
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
     record.  A target whose policy is confirmed (every target's unless
     [switch] or its [targets.<name>.switch] says confirmed = false) is
     switched through its kit, copied and rooted beside the toplevel:
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
worktrees): the branch or detached state, commits ahead of origin/main and
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
agent checkout on its work branch (never a main checkout, nor the
primary worktree), with its work branch here
and on origin, when it holds nothing of its own - no edits, nothing
beyond origin/main, no submodule holding anything - or --force.

Init (nixconvoy init [<flake-dir>]): nixconvoy.toml for a fresh install,
at the repository's root, refused when one is there.  Its required keys
are detected: flake_dir, the
directory holding flake.nix (<flake-dir>, the root, else the one no
deeper than any other); the targets as [targets.<name>] tables, the
nixosConfigurations' names (`nix eval` after `nix flake archive`: refused
when they cannot be read); targets_arch,
the one system literal the hosts' .nix files agree on, else the first
target's evaluated hostPlatform, else this machine's architecture as a
guess the file says to verify; ssh_address_template, "<this
user>@{target}", to edit; branch_prefix nixconvoy/ ({machine} in it, the
hostname, namespaces one machine's branches); child_worktrees
.claude/worktrees; remote, origin, else the one remote, and main_branch,
that remote's HEAD's branch as fetched, else main or master as fetched,
else main; eval.parallel "auto".  The optional sections follow as
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
its agents' stops kept (one waiter per session).  Both log to the
repository's prune log, logs/prune-<common git dir, each / a %>.log in the
cache dir (~/.cache/nixconvoy, or XDG_CACHE_HOME's).  worktree-remove: Claude
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

# --- cli.py: usage and dispatch
USAGE_HEADING = "usage:"
CLI_SECTION = M("== {name}")
CLI_RUNNING_IN_WORKTREE = M("{holder} is running in {name}'s worktree")
CLI_RUNNING_HERE = M("{holder} is running in this checkout")
CLI_DRY_IGNORES_MSG = M("note: -m does nothing with {DRY_FLAG}: nothing is committed")

# --- cli.py: undeploy
UNDEPLOY_CHILDREN_FROM_SUB = "undeploy --with-children runs from the checkout the worktrees hang off"
UNDEPLOY_NOTHING_ANYWHERE = "nothing to undeploy here or in any subagent worktree"
UNDEPLOY_DEPLOYER_STAGED = M("{path} has staged changes, which the deploy after the undeploy "
                             "would carry: deploy or unstage them first")
UNDEPLOY_FAILED_IN = M("undeploy failed in {names} (see above)")

# --- commands.py: land
LAND_HOLDS_BACK = M("{wb} holds back {them}: increments of it reach {it_them} "
                    "but were deployed to other targets alone.  "
                    "`{TOOL} deploy {targets}` from its checkout deploys them there first; "
                    "`land --not-yet-deployed` lands them as they are, for the next deploy anywhere to roll out")
LAND_NOT_YET_DEPLOYED = M("landing with {them} not yet deployed: the next deploy anywhere rolls the landed work out there")
LAND_NO_MSG_FILE = M("{wb} has no {MSG_FILE}; write one (it becomes the main commit message), or pass -m <msg>")
LAND_MAIN_MOVED_DURING_EDIT = M("{main} moved while the message was being edited; re-run (the edit is kept)")
LAND_NOT_ON_WORK_BRANCH = M("land runs from the work branch ({wb}); currently on {cur}")
LAND_DETACHED_HEAD = "(detached HEAD)"
LAND_STAGED = "staged but undeployed changes present; deploy or unstage them first"
LAND_NO_CHANGES = M("no changes vs {main} to land")
LAND_MAIN_NOT_FOUND = M("{main} not found")
LAND_CARRIES_UNLANDED = M("{wb} contains the unlanded increments of {carried}; land that branch first, then re-run")
LAND_SUBMODULE_ORPHANED = M("the tree pins {wb}'s rev of submodule {gone}, whose worktree is gone: "
                            "`{TOOL} open --submodule <input>` recreates it from origin's branch to land it, or "
                            "`{TOOL} close <input> --force` after it drops it")
LAND_PUSH_MAIN_FAILED = M("pushing {main_branch} failed (moved since fetch?); re-run:\n{stderr}")
LAND_DONE = M("landed on {main_branch}: {summary}")
LAND_CATCH_UP_FAILED = M("some checkouts could not be brought onto the new main (see above); "
                         "fix them and `{TOOL} pull`")
LAND_RESET_ONTO = M("{wb} reset onto {onto}")
LAND_WORKTREE_STAYS = M("the worktree stays; `{TOOL} close {name}` removes it")

# --- commands.py: land --with-children
LAND_CHILDREN_FROM_SUB = "land --with-children runs from the checkout the worktrees hang off"
LAND_CHILD_STAGED = M("{label} has staged but undeployed changes; deploy or unstage them first")
LAND_CHILD_UNDEPLOYED = M("{label} holds commits on {where} that were never deployed: "
                          "deploy or undeploy them first")
LAND_A_DETACHED_HEAD = "a detached HEAD"
LAND_CHILD_GIT_OP = M("{label} has a {op} in progress")
LAND_NO_CHILD_WORK = M("no subagent worktree under {name} holds work to land; `{TOOL} land` "
                       "lands this checkout alone")
LAND_CHILD_PROBLEM = M("{name}: {problem}")
LAND_CHILDREN_SUBMODULE_ORPHANED = M("the tree pins {wb}'s rev of submodule {gone}, whose worktree is gone: "
                                     "`{TOOL} open --submodule <input>` in {name} first "
                                     "(then `close <input> --force` drops it)")
LAND_UNION_MERGE_MSG = M("union {branch}")
LAND_CHILD_CONFLICT = M("{branch} conflicts with the rest in {file}: resolve it (rebase {branch} onto {wb} in "
                        "{label}), or `{TOOL} land {name}` alone first")
LAND_CLASH = M("{label} has unstaged edits or untracked files "
               "the landing changes: {files}; stage and deploy them, or put them aside")
LAND_CHILD_TREE_KEPT = M("note: {name}'s working tree kept as it was (a file in the way); "
                         "`git checkout -- .` there brings it onto the landing")

# --- commands.py: pull
PULL_LEFT_AS_THEY_WERE = "pull left some checkouts or submodule repositories as they were (above)"

# --- commands.py: undeploy
UNDEPLOY_SUB_NOT_ON_WORK_BRANCH = M("{label} is not on its work branch {wb} (nothing deployed from it yet): "
                                    "nothing to undeploy; `{TOOL} close {name} --force` discards it")
UNDEPLOY_NOT_ON_WORK_BRANCH = M("undeploy runs from the work branch ({wb}); currently on {cur}")
UNDEPLOY_NO_SUCH_COMMIT = M("{BACK_TO_FLAG} {back_to}: no such commit")
UNDEPLOY_NOT_ON_BRANCH = M("{BACK_TO_FLAG} {back_to} ({sha}) is not on {wb}")
UNDEPLOY_NOTHING_BEYOND_MAIN = M("nothing to undeploy: no increment beyond {main}")
UNDEPLOY_NOTHING_HEAD_IS = M("nothing to undeploy: HEAD is {back_to}")
UNDEPLOY_STAGED = ("staged but undeployed changes present, which the deploy would carry; "
                   "deploy or unstage them first")
UNDEPLOY_NOT_AN_INCREMENT = M("{summary} is not a deploy's increment; undeploy does not cross it: reset by hand")
UNDEPLOY_LOST_EDITS = M("the increments' files have unstaged edits, which the reset would lose; "
                        "stash or discard them first: {files}")
UNDEPLOY_RESET_FAILED = M("the reset onto {sha} failed; nothing moved:\n{stderr}")
UNDEPLOY_DONE = M("undeployed {wb}'s {n} increment(s) (see reflog):\n  {summaries}")
UNDEPLOY_REVERTING = M("deployed to {targets}: deploying now to revert it")
UNDEPLOY_REACHED_NONE = "it reached no target: nothing to deploy"

# --- commands.py: close (subagent worktrees, their locks and branches)
CLOSE_FORCE_NEEDS_NAMES = "--force needs the worktrees named"
CLOSE_NO_SUBS = M("no subagent worktree under {top}")
CLOSE_TOOL_RUNNING = M("a {TOOL} is running in {label}")
CLOSE_REMOVED_FORCE = "removed (--force)"
CLOSE_EMPTY_REMOVED = "empty, removed"
CLOSE_KEPT = M("kept, {why}")
CLOSE_CHECK_FAILED = M("kept, its check failed: {error}")
CLOSE_VERDICT = M("{label}: {verdict}")
CLOSE_LOCK_BY_PID = M("pid {pid}")
CLOSE_LOCK_BY_CHILD = M("pid {pid}, a child of {holder}")
CLOSE_LOCK_WAITING = M("{what}: locked by {by}; waiting for it")
CLOSE_LOCK_HOLDER_GONE = M("{what} stays locked, its holder gone: {holder}")
CLOSE_LOCK_NAMED_NONE = "a run that named none"
CLOSE_SUB_REMOVED = M("subagent worktree {path} removed, with {branches} here and on {remote}")
CLOSE_NO_OWN_BRANCH = "no branch of its own"
CLOSE_BRANCH_STAYS = M("note: {branch} stays on {remote}, holding another checkout's increments")
CLOSE_DELETE_FAILED = M("deleting {branches} on {remote} failed: {error}")
CLOSE_DELETE_ANNOUNCEMENTS_FAILED = M("deleting the announcements of {branches} on {remote} failed "
                                      "(git push {remote} :{refs}/<branch> clears one by hand): {error}")
CLOSE_NO_ERROR_GIVEN = "no error given"

# --- commands.py: close <submodule>
CLOSE_SUBMODULE_KEPT = M("submodule {name} is kept: {why} (--force removes it anyway)")
CLOSE_SUBMODULES_FAILED = M("close failed for submodule {names} (see above)")

# --- commands.py: close <path>
CLOSE_FROM_ANOTHER = "run close from another checkout"
CLOSE_NOT_A_WORKTREE = M("{path} is no worktree of this repository")
CLOSE_PRIMARY = M("{path} is the primary worktree, which holds the repository")
CLOSE_IS_SUB = M("{path} is a subagent worktree: `{TOOL} close {name}`")
CLOSE_HOLDS_SUBS = M("{path} holds the subagent worktrees {names}: close them first")
CLOSE_RUNNING_IN = M("{holder} is running in {where}")
CLOSE_MAIN_CHECKOUT_ON = M("{path} is a main checkout (on {branch}), not an agent's on its work branch")
CLOSE_MAIN_CHECKOUT_DETACHED = M("{path} is a main checkout (detached), not an agent's on its work branch")
CLOSE_KEPT_EDITS = "edits or untracked files"
CLOSE_KEPT_COMMITS = M("commits not on {main}")
CLOSE_KEPT_BRANCH = M("its work branch {wb}, with commits not on {main}")
CLOSE_KEPT_SUBMODULE = "a submodule holding something"
CLOSE_CHECKOUT_HOLDS = M("{path} holds {kept}: land or undeploy it first (--force removes it anyway)")
CLOSE_CHECKOUT_REMOVED = M("checkout {path} removed, with {branches} here and on {remote}")

# --- commands.py: open --child
OPEN_BAD_NAME = M("{name}: a worktree name holds letters, digits, '.', '-' and '_' only")
OPEN_NAME_IS_SUBMODULE = M("{name} is a submodule of this checkout: a subagent worktree may not have its name")
OPEN_ADD_FAILED_RETRYING = M("open: git worktree add failed: {stderr}")
OPEN_COULD_NOT_CREATE = M("could not create or reuse {path} (git's errors above)")
OPEN_NOT_IGNORED = M("{dir}/ must be ignored (a nested worktree) and {exclude} did not take: "
                     "add it to .gitignore, or point {CONFIG_FILE}'s `child_worktrees` at a directory that is")

# --- commands.py: open <path>
OPEN_EXISTS = M("{path} exists already")
OPEN_NESTED = M("{path} would be nested in the checkout {up}: `{TOOL} open --child <name>` makes a subagent "
                "worktree there; an agent checkout goes beside the checkouts")
OPEN_BRANCH_HELD = M("{remote} holds {held}: a removed checkout's, or another machine's (the prefix "
                     "shared).  `--branch {name}` makes it this checkout's, `--branch <leaf>` opens another")
OPEN_NOT_WORK_BRANCH = M("{branch} is not a work branch of this machine's: one named {prefix}<leaf>; "
                         "`--branch <leaf>` names one")
OPEN_BAD_LEAF = M("{branch}: a checkout's branch is {prefix}<leaf> with a leaf that is not 'main' "
                  "and holds no '-'; name one with --branch <leaf>")
OPEN_DIVERGED = M("{branch} here and on {remote} have diverged: `git branch -f {branch} "
                  "{remote}/{branch}` takes {remote}'s copy (or the other way round), then re-run")
OPEN_ADD_FAILED = M("git worktree add failed:\n{stderr}")
OPEN_PUSH_STALE = M("{remote} holds one now: --branch says whether to take it over")
OPEN_PUSH_UNREACHABLE = M("{remote} unreachable?")
OPEN_PUSH_FAILED = M("pushing {branch} failed ({why}); nothing opened:\n{stderr}")
OPEN_CHECKOUT_ON = M("checkout {name} on {branch}")
OPEN_CHECKOUT_ON_TAKEN = M("checkout {name} on {branch} ({remote}'s copy: this checkout's now)")

# --- commands.py: status
STATUS_DETACHED_AT_MAIN = M("detached at {main}")
STATUS_DETACHED_NOTHING_OWN = M("detached at {sha}, nothing of its own")
STATUS_DETACHED_AHEAD = M("detached, {n} ahead of {main}")
STATUS_MAIN_UNPUSHED = M(", {n} unpushed")
STATUS_PUSHED = "pushed"
STATUS_UNPUSHED = "unpushed"
STATUS_NOT_ON_REMOTE = M("not on {remote}")
STATUS_AHEAD = M("{branch} +{n} {pushed}")
STATUS_IDLE = M("{branch} = {main} (idle)")
STATUS_BEHIND = M(", {n} behind {main_branch}")
STATUS_OWNS = M("owns {wb}")
STATUS_STAGED = "staged"
STATUS_UNSTAGED = "unstaged"
STATUS_UNTRACKED = "untracked"
STATUS_COUNT = M("{label} {n}")
STATUS_HOLDS_BACK = M("holds back {targets}")
STATUS_EMPTY_SESSIONLESS = M("empty, its create found no claude session: {TOOL} close {name} removes it")
STATUS_EMPTY_HAND_LOCKED = "empty, locked by hand: the sweep keeps it"
STATUS_EMPTY_SESSION_LOCKED = ("empty, locked by its agent's claude session: closed when its agent "
                               "stops or that session exits")
STATUS_EMPTY = M("empty: {TOOL} close {name} removes it")
STATUS_SUBJECT = M('"{subject}"')
STATUS_OP_IN_PROGRESS = M("[{op} in progress]")
STATUS_DEPLOYING = "[deploying]"
STATUS_NOT_A_CHECKOUT = "(not a checkout here)"
STATUS_ELSEWHERE_HEADER = M("on {remote} only (other machines' checkouts):")
STATUS_ELSEWHERE_ENTRY = M("{branch} +{n}: {summary}")
STATUS_SUBMODULES = M("submodules: {names} ({TOOL} open --submodule <input>)")
STATUS_STRAY_HEADER = "submodule repositories edited outside any checkout:"
STATUS_PENDING_HEADER = "deploys under way (announced increments):"
STATUS_PENDING = M("{key}: {summary}")
STATUS_PENDING_SETTLED = M("{key}: {summary} (settled, awaiting its push)")
STATUS_PENDING_DEAD = M("{entry} - not renewed for {minutes:.0f} min: dead?")


# --- checkout.py: work branch
CHECKOUT_NEEDS_WB = M("{what} needs a work branch, and this checkout has none: "
                      "`git checkout -b {prefix}<leaf>` and re-run")

# --- checkout.py: deploy lock holders
CHECKOUT_LOCK_UNKNOWN_HOLDER = M("another {TOOL}")
CHECKOUT_LOCK_CHILD_OF = M("a child of {what}")
CHECKOUT_HOLDER_CMD = M("`{TOOL} {args}`")
CHECKOUT_HOLDER_CMD_BARE = M("`{TOOL}`")
CHECKOUT_HOLDER_GONE = M("{what} (pid {pid}, gone; started {age} ago)")
CHECKOUT_HOLDER_RUNNING = M("{what} (pid {pid}, running for {age})")
CHECKOUT_AGE_SECONDS = M("{n} s")
CHECKOUT_AGE_MINUTES = M("{n} min")
CHECKOUT_AGE_HOURS = M("{h:.1f} h")

# --- checkout.py: rebase
CHECKOUT_REBASE_HINT = M("run `{command}` there, resolve, then re-run")
CHECKOUT_REBASE_GITLINK_HINT = M("; at a stop on {gitlinks} keep this branch's own pin, the landing merging the "
                                 "submodule onto its default branch itself (a deploy rebases it there first):\n  "
                                 "{fixes}")
CHECKOUT_REBASE_FAILED = M("{hint}:\n{stderr}")

# --- checkout.py: catching up checkouts
CHECKOUT_SKIPPED_LOCKED = M("skipped: {holder} is running there")
CHECKOUT_NOT_CAUGHT_UP = M("not caught up: {error}")
CHECKOUT_CATCH_UP_LINE = M("{name}: {status}")

# --- checkout.py: landing message and the land editor
CHECKOUT_LAND_MSG_AND_M = M("{where} has a {MSG_FILE} and -m was given: land -m is for a branch without one, "
                            "so remove the file or drop -m")
CHECKOUT_SCISSORS = "# ------------------------ >8 ------------------------"
CHECKOUT_NOTE_LANDING = M("Landing {wb} on {main_branch}.")
CHECKOUT_NOTE_OF = M(" of {label}")
CHECKOUT_NOTE_INCREMENTS = M("Increments{of}:")
CHECKOUT_NOTE_CHANGES = M("Changes{of}:")
CHECKOUT_EDITOR_FAILED = M("editor {cmd} exited with {rc}; landing aborted")
CHECKOUT_EMPTY_MESSAGE = "empty message; landing aborted"
CHECKOUT_NO_MSG_FILE_YET = M("note: {wb} carries work but no {MSG_FILE} yet - landing needs "
                             "it, or land -m <msg>")

# --- checkout.py: snapshot, announcement and record commits
CHECKOUT_INDEX_UNMERGED = M("cannot snapshot the index{of} (unmerged paths?)")
CHECKOUT_INDEX_OF = M(" of {cwd}")
CHECKOUT_SNAPSHOT_SUBJECT = "staged snapshot (not yet committed)"
CHECKOUT_ANNOUNCE_SUBJECT = M("pending increment of {branch} from {machine}:{cwd} (pid {pid})")
CHECKOUT_RECORD_MESSAGE = M("deploy {target}: {subject}\n\n{doc}\n")
CHECKOUT_COMMIT_TREE_FAILED = "commit-tree failed"
CHECKOUT_PUSH_FAILED = "push failed"
CHECKOUT_RENEW_FAILED = M("note: renewing this deploy's announcement failed; a deploy "
                          "elsewhere may take this one for dead:\n{detail}")

# --- checkout.py: pushes
CHECKOUT_PUSH_TIMED_OUT = "timed out"
CHECKOUT_PUSH_NO_PID = "no pid"
CHECKOUT_PUSH_BG_FAILED = M("note: the previous run's background push failed ({status}); this run's "
                            "push retries it{log}")
CHECKOUT_PUSH_BG_LOG = M(":\n{log}")
CHECKOUT_SOLE_WRITER = M("{wb} moved on {remote} ({sha}): another checkout deployed on it, but a work branch has one "
                         "writer.  `git log refs/heads/{wb}..{up_ref}` shows what it holds; in that "
                         "branch's checkout, `git rebase --autostash {up_ref}` carries this checkout's "
                         "increments onto it, or `git checkout -b {prefix}<leaf>` moves them to a branch of their "
                         "own; then re-run")
CHECKOUT_PUSH_WB_FAILED = M("pushing {wb} failed - it moved on {remote}, but this checkout is its "
                            "only writer.  Investigate:\n{stderr}")
CHECKOUT_PUSHED = M("pushed {wb} ({sha})")
CHECKOUT_PUSHING_BRANCH = M("{wb} ({sha})")
CHECKOUT_PUSHING_BG = M("pushing {what} in the background")
CHECKOUT_PUSHING_CLEARED = "the cleared announcement"

# --- checkout.py: worktree locks
CHECKOUT_NO_LOCK_REASON = "no reason given"
CHECKOUT_LOCKED_BY_HAND = M("this worktree is locked by hand ({reason}): "
                            "`git worktree unlock` it, then re-run")
CHECKOUT_LOCKED_LIVE = M("this worktree is locked ({reason}): the claude session that "
                         "spawned its agent still runs; wait for it to exit, or `git worktree unlock` it, "
                         "then re-run")

# --- names.py
NAMES_AMBIGUOUS = M("{name} is ambiguous among the subagent worktrees {worktrees}")
NAMES_IS_SUBMODULE = M("{name} is a submodule of this checkout: {verb} acts on it with the checkout "
                       "(`close {name} --force` discards it)")
NAMES_NO_WORKTREE_ON_BRANCH = M("no worktree on this machine is on branch {name}: {verb} acts in a checkout of it")
NAMES_NOT_FOUND = M("no subagent worktree or branch named {name}{listing}")
NAMES_NOT_FOUND_LISTING = M("; the subagent worktrees here: {worktrees}")
NAMES_NOT_SUBAGENT = M("{name} is the checkout {path}, not a subagent worktree")

# --- orphans.py
ORPHANS_FETCH_FAILED = M("note: git fetch failed, judging the refs fetched before: {stderr}")
ORPHANS_NO_WORKTREE = "(no worktree)"

# --- review.py
REVIEW_UNKNOWN_NAME = M("review: no subagent worktree, submodule, branch or path named {name}")
REVIEW_NO_MERGE_BASE = M("review: {branch} has no merge-base with {default}")
REVIEW_MSG_HEADER = M("── {MSG_FILE} ──")
REVIEW_SUBMODULE_HEADER = M("── submodule {name} ──")
REVIEW_BRANCH_HEADING = M("══ branch {branch} ══")
REVIEW_SUBMODULE_HEADING = M("══ submodule {name} ══")
REVIEW_WORKTREE_HEADING = M("══ {label} ══")

# --- union.py
UNION_MERGE_SUBJECT = M("union {name}")
UNION_COMMIT_SUBJECT = "deploy union of work branches"


# --- proc.py
PROC_WITH_NOTHING_DONE = M("{problem}; {NOTHING_DONE}")
# A failure's command output, appended to its message as {detail} when there is any.
PROC_DETAIL = M(":\n{output}")
PROC_DETAIL_LINES = M("\n{output}")
PROC_PERF = M("perf: total {total}s ({summary}) -> {path}")
PROC_PERF_PHASE = M("{phase} {secs}s")
PROC_DIE = M("{TOOL}: {msg}")
PROC_STILL_ARMED = M("{TOOL}: {targets} still armed: each finishes verifying and is then confirmed or rolled back")
PROC_CMD_FAILED = M("`{cmd}` failed{detail}")
PROC_AGENIX_DECRYPTED = M("[agenix] decrypted {n} secrets (generation {generation})")

# --- deploy.py
DEPLOY_ALIVE_AGAIN = M("{key}'s deploy is under way again: waited for after all")
DEPLOY_STALE_STAMP = M("warning: {key}'s deploy last renewed its announcement {minutes:.0f} min ago (by its machine's clock): taken as dead, going on without it")
DEPLOY_NO_NEWS = M("warning: no news of {key}'s deploy for {secs} s (its announcement is no longer renewed): taken as dead, going on without it")
DEPLOY_KILLED_BY_SIGNAL = M("killed by signal {signum}")
DEPLOY_RUNNING_PRECHECK = "running the precheck..."
DEPLOY_REACHES_NONE = M("reaches no target: committed onto {wb} and pushed; nothing built or switched (the probes' results dropped)")
DEPLOY_FOREIGN_INCREMENTS = M("{remote} holds {wb} with increments this worktree never pushed: another worktree of this name?  `git log {main_ref}..{remote_wb}` shows them; open this worktree under another name, or `git branch {wb} {remote_wb}` to take them over")
DEPLOY_NOT_WORK_BRANCH = M("on branch {branch}, not a work branch of this machine's ({prefix}<leaf>); the main checkout deploys from {main_branch} (a hand commit there is claimed as {main_branch}'s until pushed)")
DEPLOY_INCREMENT_NAME = M("{checkout}: increment {n}")
DEPLOY_STAGED_ON_MAIN = "staged changes in the main checkout, which has no work branch: commit them by hand here, or stage them in a checkout on a work branch"
DEPLOY_REACHES_NO_TARGET = "the increment reaches no target"
DEPLOY_NOTHING_STAGED = "nothing is staged; stage the change or drop -m"
DEPLOY_ANNOUNCEMENT_KEPT_RECORDS = M("note: {targets} were switched (or a switch was started), so this deploy's announcement stays, carrying their deploy records: re-run to finish the deploy, or clear it (git push {remote} :{pending_refs}/{key})")
DEPLOY_ANNOUNCEMENT_KEPT_INCREMENT = M("note: {targets} run this checkout's unpushed increment (or a switch onto it was started), so its announcement stays: re-run to finish the deploy, or clear it to have the increment reverted by the next deploy there (git push {remote} :{pending_refs}/{key})")
DEPLOY_UNREACHABLE = M("unreachable: {targets}; {NOTHING_DONE}; fix connectivity (or restrict targets) and re-run")
DEPLOY_WAITING = M("waiting for {key}'s deploy under way: its increment and this checkout's both reach {targets}...")
DEPLOY_STILL_WAITING = M("still waiting for {keys} ({minutes:.0f} min)")
DEPLOY_REMOTE_SILENT = M("{remote} stopped answering while waiting for the deploy under way; {NOTHING_DONE}")
DEPLOY_WAIT_ENDED = "...ended; probing the targets again and redoing the union"
DEPLOY_SKIP_DRY = M("NIXCONVOY_SKIP_SWITCH=1 {DRY_FLAG}: union {union}; nothing done")
DEPLOY_SKIP = M("NIXCONVOY_SKIP_SWITCH=1: skipping probe+eval+switch (union {union})")
DEPLOY_ANNOUNCE_FAILED = M("announcing this deploy on {remote} failed; {NOTHING_DONE}:\n{detail}")
DEPLOY_EVALUATING = "evaluating targets..."
DEPLOY_EVALUATING_PRECHECK = "evaluating targets, running the precheck..."
DEPLOY_ASSESSED_PLAIN = M("warning: {target} is switched plainly (confirmed = false) but carries the boot-assessment module: a counted boot there runs no switch checks")
DEPLOY_REJECTED_GENERATION = M("{target}'s bootloader rejected the very generation this deploy would switch it to ({stub}): it failed its boot assessment and the target fell back.  Nothing was built, switched or committed.  See `journalctl -u boot-assessment-watch -u boot-assessment` on {target}; then change the configuration, or re-arm the stub to retry it as is (sudo mv of the +0-N stub to +2 under /boot/EFI/Linux).")
# A deploy record's subject: the main branch, then each unioned branch.
DEPLOY_SUBJECT_BRANCH = M(" + {branch}")
DEPLOY_HOLD_ENDED_SUBJECT = M("deployed to {targets}, held back until now")
DEPLOY_DRY_KIT_REFRESH = M("{DRY_FLAG}: the switch kit on {targets} would be refreshed")
DEPLOY_BUILD_WHERE = M(" on {host}")
DEPLOY_BUILD_KIT = M("{target}'s switch kit")
DEPLOY_BUILDING = M("building {what}{where}...")
DEPLOY_BUILD_OF = M("build of {what}")
DEPLOY_DRY_ACTIVATING = M("dry-activating {targets}...")
DEPLOY_KIT_REFRESH_FAILED = M("warning: {err}")
DEPLOY_DRY_FAILED = M("{DRY_FLAG}: {NOTHING_DONE}; a dry activation failed:\n{failures}")
DEPLOY_DRY_WOULD_SWITCH = M("{DRY_FLAG}: {targets} would be switched (union {union}); {NOTHING_DONE}")
DEPLOY_DRY_LEFT_ALONE = M("{DRY_FLAG}: nothing would be switched: {targets} left alone, running another checkout's work this checkout's does not reach (union {union})")
DEPLOY_DRY_NO_DRIFT = M("{DRY_FLAG}: nothing drifts from the union ({union}); nothing to switch")
DEPLOY_RECORDS_ANNOUNCE_FAILED = M("announcing the deploy records on {remote} failed; {NOTHING_DONE}:\n{detail}")
DEPLOY_MOVED_TOO_OFTEN = M("{moved}, {times} times over; nothing was committed: re-run to converge with it")
DEPLOY_MOVED_REDO = M("{moved}: probing the targets again and redoing the plan")
DEPLOY_DONE = M("deployed {targets}: union {union}")
DEPLOY_LEFT_ALONE = M("nothing switched: {targets} left alone, running another checkout's work this checkout's does not reach (union {union})")
DEPLOY_NO_DRIFT = M("nothing drifts from the union ({union}); nothing to switch")
DEPLOY_UNPUSHED = M("{wb} is not pushed (the targets run it; its announcement stays for the re-run): {problem}")

# --- switch.py
SWITCH_MOVED_MEANWHILE = M("{target} was switched by another deploy while this one ran (now running {running})")
SWITCH_NOTHING_READABLE = "nothing readable"
# Appended by the target's shell to the system it found ({running} above); shell-quoted there.
SWITCH_PROVENANCE_REWRITTEN = ", its provenance rewritten by another deploy"
SWITCH_UNCLAIMED = M("note: could not claim {target}'s provenance in {file}; until its next switch, other deploys treat its system as untracked")
SWITCH_STALE_KIT_DROP_FAILED = M("warning: dropping {target}'s stale switch kit link failed (exit {code})")
SWITCH_KIT_ROOT_FAILED = M("rooting the switch kit {kit} on {target} failed (exit {code}){detail}")
SWITCH_CHECK_INTERRUPTED = M("check interrupted: {error}")
SWITCH_DURING = M(" while switching {target}")

# --- targets.py
TARGETS_INVENTORY_UNREADABLE = M("cannot read the target inventory under {dir}/: {error}")
TARGETS_NONE_FOUND = M("no targets found under {dir}/")
TARGETS_NO_TAG = M("no target carries the tag {tag} ({CONFIG_FILE}: targets.<name>.tags)")
TARGETS_PHANTOM_TAG = M("{CONFIG_FILE} tags {phantom} as {tag}, but the inventory has no such target (it has: {inventory})")
TARGETS_UNKNOWN = M("unknown target(s) {unknown}; the inventory has: {inventory}")
TARGETS_REFRESHING_KIT = M("refreshing the switch kit on {target}...")
TARGETS_KIT_ROOT_FAILED = M("rooting the switch kit on {target} failed (exit {code}){detail}")
TARGETS_SUBSTITUTERS_TIMEOUT = M("`nix config show substituters` timed out after {secs} s")
TARGETS_SUBSTITUTERS_FAILED = M("reading the substituters failed:\n{error}")
TARGETS_STORE_TIMEOUT = M("`nix store {verb}` timed out after {secs} s")
TARGETS_STORE_NO_JSON = M("`nix store {verb}` printed no JSON object:\n{output}")
TARGETS_STORE_FAILED = M("`nix store {verb}` failed:\n{error}")
TARGETS_UNTRUSTED_ROOT_FAILED = M("the nix daemon does not trust the user (nix.settings.trusted-users), and nix as root ({sudo}) failed: {error}")
TARGETS_UNTRUSTED_NOR_ROOT = "the nix daemon does not trust the user (nix.settings.trusted-users), nor root"
TARGETS_COPY_DRVS_FAILED = M("copying derivations to {host} failed{detail}")
TARGETS_BUILD_FAILED_ON_BECAUSE = M("{what} failed on {host}: {error}")
TARGETS_BUILD_FAILED_ON = M("{what} failed on {host}{detail}")
TARGETS_BUILD_FAILED_BECAUSE = M("{what} failed: {error}")
TARGETS_BUILD_FAILED = M("{what} failed{detail}")
TARGETS_FETCHING_CLOSURE = M("fetching the closure from {host}...")
TARGETS_FETCH_CLOSURE_FAILED = M("fetching the closure from {host} failed; {NOTHING_DONE} - the changes are still staged{detail}")
TARGETS_COPYING_CLOSURE = M("copying closure to {target}...")
TARGETS_COPY_CLOSURE_FAILED = M("copying the closure to {target} failed; {NOTHING_DONE} - the changes are still staged{detail}")
TARGETS_STORE_HOST_UNUSABLE = M("store host {host}: {problem}; {NOTHING_DONE}")
TARGETS_ESP_SCAN_FAILED = M("could not scan {target}'s ESP for rejected generations (exit {code}); {NOTHING_DONE}")
TARGETS_DRY_ACTIVATE_FAILED = M("dry-activating the new system on {target} failed (exit {code}); {NOTHING_DONE}{detail}")
TARGETS_WOULD = M("  {target}: would {verb} {units}")
TARGETS_WOULD_NOTHING = M("  {target}: would stop, restart or reload no unit")


# --- guard.py
GUARD_MISSING = M("{exe} is missing: {TOOL} runs from its nix package (nix run .#{TOOL})")
GUARD_CANNOT_RUN = M("cannot run {name} ({exe}): {error}; nothing was committed")
GUARD_LOST = M("{name} {what}{during}; nothing was committed")
GUARD_STOPPED = M("stopped answering (exit {code})")
GUARD_SENT = M("sent {line!r} ({error})")
GUARD_DECIDING = M(" deciding {core}")

# --- init.py: main
INIT_EXISTS = M("{CONFIG_FILE} is already there; move it aside to generate a new one")
INIT_WHAT_HOSTS = "the nixosConfigurations' names"
INIT_WHAT_HEAD_HOSTS = "HEAD's nixosConfigurations' names"
INIT_WHAT_CHECKS = "the flake's checks"
INIT_NO_TARGETS = M("the targets are the flake's nixosConfigurations, which could not be read:\n  {notes}"
                    "\nfix that (nix on PATH, a flake that evaluates) and re-run")
INIT_NO_HOSTS = "the flake has no nixosConfigurations yet: nothing to deploy to; add one and re-run"
INIT_INVALID = M("generated an invalid {CONFIG_FILE}: {error}")
INIT_WROTE = M("wrote {CONFIG_FILE}:")
INIT_WROTE_FLAKE = M("  flake_dir = {flake!r}, targets: {targets}")
INIT_WROTE_SYSTEM = M("  targets_arch = {system!r} ({how}), ssh_address_template = {ssh!r}, branch_prefix = {prefix!r}")
INIT_WROTE_REMOTE = M("  remote = {remote!r}, main_branch = {main!r}")
INIT_WROTE_NOTE = M("  note: {note}")

# --- init.py: detect_names
INIT_REMOTE_ASSUMED = M('remote = "origin" assumed ({remotes}); {TOOL} fetches and pushes it')
INIT_NO_REMOTE_YET = "no remote yet"
INIT_MAIN_ASSUMED = M('main_branch = "main" assumed ({remote} has no HEAD, main or master as fetched)')

# --- init.py: NixProbe
INIT_NIX_MISSING = M("no nix on PATH: {what} not read")
INIT_NIX_FAILED = M("{doing} failed, so {what} not read: {error}")
INIT_NIX_NO_OUTPUT = "(no output)"
INIT_NIX_NOT_ON_PATH = M("{what} not read: nix is not on PATH")
INIT_DOING_FETCH = M("fetching the inputs of {ref}")
INIT_DOING_EVAL = "nix eval"
INIT_NIX_NO_JSON = M("nix eval printed no JSON for {what}")

# --- init.py: find_flake
INIT_NO_FLAKE_THERE = M("{given}: no flake.nix there")
INIT_FLAKE_OUTSIDE = M("{given} is outside the repository")
INIT_NO_FLAKE = M("no flake.nix in this repository ({TOOL} init [<flake-dir>])")
INIT_SEVERAL_FLAKES = M("several flakes: {flakes}; name one: {TOOL} init <flake-dir>")

# --- init.py: detect_system
INIT_SYSTEM_FROM_FILES = M("from {hosts_dir}'s .nix files")
INIT_WHAT_SYSTEM = M("{target}'s system")
INIT_SYSTEM_EVALUATED = M("evaluated from {target}")
INIT_SYSTEM_SEVERAL = M(" ({systems} all appear under {hosts_dir})")
INIT_SYSTEM_GUESS_NOTE = M("system guessed from this machine's architecture{several}: verify it")
INIT_SYSTEM_GUESSED = "guessed"

# --- init.py: render (the comments of the nixconvoy.toml written)
INIT_TOML_REMOTE = "# The remote the work branches are pushed to and main is landed on, and main's name there."
INIT_TOML_BRANCH_PREFIX = M(
    "# A checkout's work branch is <branch_prefix><leaf>, `open <path>` naming it after the path.\n"
    "# {{machine}} in the prefix is this machine's hostname, a namespace per machine when you want one.\n"
    "# Every branch shaped so ({{machine}} any hostname) is a work branch {TOOL} unions into a deploy;\n"
    "# a pushed branch shaped otherwise is ignored.")
INIT_TOML_CHILD_WORKTREES = M(
    "# Child worktrees are an optional feature of {TOOL} that can be used by hand if desired,\n"
    "# but is primarily intended to hook into worktrees that harnesses automatically create to isolate their"
    " subagents.\n"
    "# The default directory is set to Claude Code's, so that {TOOL}'s Claude Code plugin works out of the box.")
INIT_TOML_PARALLEL = "# Determinate Nix's parallel evaluation: true, false or \"auto\" (on when nix has it)"
INIT_TOML_PRECHECK = M("# Optional: a flake check (checks.<targets_arch>.<name>) proven before any build{these}.")
INIT_TOML_PRECHECK_THESE = M("; this flake's: {checks}")
INIT_TOML_BUILD = (
    "# Optional: realise the closures in another machine's store, keyed by the hostname nixconvoy\n"
    "# runs on (\"*\": any other machine; a machine mapped to itself realises locally); that\n"
    "# machine's nix daemon builds them or hands them to its own remote builders.\n"
    "# [build]\n"
    "# store = { \"*\" = \"bigbox\" }\n"
    "# skip_substituters = [\"https://slow.cachix.org\"]")
INIT_TOML_SWITCH = (
    "# How the targets are switched (every target's defaults; [targets.<name>.switch] overrides one's).\n"
    "# Confirmed, unless `confirmed = false`: a rollback timer is armed on the target, the new system\n"
    "# activated, verified over a new ssh connection (and its `checks`, bash run as root), and only then\n"
    "# set as the profile and boot entry; otherwise it rolls back within `deadline` seconds.\n"
    "# [switch]\n"
    "# deadline = 30\n"
    "# internet_check = false   # the verification also wants the internet reachable from the target\n"
    "# checks = \"\"\"\n"
    "# systemctl is-active -q nginx.service\n"
    "# \"\"\"")
INIT_TOML_SUBMODULES = (
    "# Optional support for git submodules. Feel free to ignore this if you don't have any submodules in your"
    " git repo.\n"
    "# [submodules.<input>]\n"
    "# repo = \"~/src/<input>\"")
INIT_TOML_TARGETS = (
    "# The targets: one table per nixosConfiguration (the inventory, as there is no targets_dir);\n"
    "# a nixosConfiguration added later needs its table here.\n"
    "# A deploy re-evaluates a target only when flake.nix, a file it names (transitively) or the\n"
    "# target's own directory changed: a directory named after it beside the other targets' is\n"
    "# found on its own, `dir` names one elsewhere.")
INIT_TOML_FIRST_TARGET = M(
    "# # overrides ssh_address_template, if one target has an unusual username or domain-name\n"
    "# ssh_address = \"root@10.0.0.9\"\n"
    "# # For larger fleets, tags can be used to refer to several machines as a group in `{TOOL} deploy`\n"
    "# tags = [\"vps\"]")

# --- hooks.py
HOOK_CREATE_NO_REPO = M("hook worktree-create: {where} is not in a git repository")
HOOK_CREATE_NO_NAME = "hook worktree-create: the input names no worktree"
HOOK_RESUME_FAILED = M("worktree-resume: could not recreate or relock agent {agent}'s worktree ({error}), "
                       "so it is not resumed; retry")
HOOK_REMOVE_NOT_SUBAGENT = "it is no subagent worktree"
HOOK_REMOVE_KEPT = M("hook worktree-remove: {path} kept: {why}")
HOOK_KEEP_LOCKED = M("another claude session holds it ({reason})")
HOOK_KEEP_CHANGES = "it has uncommitted, untracked or ignored changes"
HOOK_KEEP_COMMITS = M("it carries commits not on {main}")
HOOK_KEEP_SUBMODULE = M("it holds a submodule worktree ({path}), left to {TOOL} close")
HOOK_SWEEP_STARTED = M("{time} claude session {pid} started: pruning dead sessions' ({top})")
HOOK_SWEEP_EXITED = M("{time} claude session {pid} exited: pruning its own ({top})")
HOOK_PRUNE_FAILED = M("prune failed: {error}")
HOOK_STOP_KEPT_TASK = M("kept, its background task {task} still runs")
HOOK_STOP_KEPT_AGENT = M("kept, agent {agent} works in it")
HOOK_STOP_GONE = "removed meanwhile"
HOOK_STOP_REMOVED = "empty, removed"
HOOK_STOP_KEPT = M("kept, {why}")
HOOK_STOP_CHECK_FAILED = M("kept, its check failed: {error}")
HOOK_STOP_LINE = M("{time} agent {agent} stopped: {label}: {verdict}")

# --- submodule.py: repository and pin
SUBMODULE_LABEL = M("submodule {name}")
SUBMODULE_NO_DEFAULT_BRANCH = M("{repo}: origin has neither a HEAD, main nor master branch")
SUBMODULE_PIN_DATE_MISSING = M("submodule {name}: {sha} is not in {repo}, so its pin file cannot name the commit's date")
SUBMODULE_PIN_FILE_STALE = M("warning: {pin_file} does not name {rev}, which the tree pins submodule {name} on: "
                             "a deploy from a checkout on a work branch rewrites it")
SUBMODULE_PIN_FILE_REWRITTEN = M("submodule {name}: {pin_file} rewritten for {rev}")
SUBMODULE_FETCH_FAILED = M("note: submodule {name}: fetching its origin failed:\n{stderr}")
SUBMODULE_PAST_REFS_DELETE_FAILED = M("warning: submodule {name}: deleting its past refs on its origin failed:\n"
                                      "{stderr}")
SUBMODULE_FAILED_ABOVE = M("submodule {name}: failed (above)")
SUBMODULE_STATUS = M("submodule {name}: {status}")
SUBMODULE_REPO_FAILED_ABOVE = M("{repo} (submodule {name}): failed (above)")
SUBMODULE_REPO_STATUS = M("{repo} (submodule {name}): {status}")
SUBMODULE_PIN_NONE = "nothing"
SUBMODULE_DETACHED = "a detached HEAD"
SUBMODULE_OFF_BRANCH = M("submodule {name} ({dir}) is on {cur}, not {wb}")
SUBMODULE_OFF_BRANCH_SWITCH = M("submodule {name} ({dir}) is on {cur}, not {wb}: `git -C {dir} switch {wb}`")

# --- submodule.py: union
SUBMODULE_MERGE_CONFLICT = M("note: submodule {name}: {ours} and {theirs} conflict in {path}")
SUBMODULE_MIRROR_CLONE_FAILED = M("submodule {name}: cloning {url} to union its branches failed:\n{stderr}")
SUBMODULE_NOT_ON_ORIGIN = M("note: submodule {name}: {sha} is not on its origin")
SUBMODULE_UNION_SUBJECT = M("union of {name}")
SUBMODULE_UNION_PUSH_FAILED = M("submodule {name}: pushing its union to {ref} failed:\n{stderr}")
SUBMODULE_UNION_DONE = M("submodule {name}: union of {ours} and {theirs} is {sha}, on {ref}")

# --- submodule.py: rebase
SUBMODULE_REBASE_WHY = M("{upstream} and {wb} both moved submodule {name}'s pin (a landing there), which git "
                         "cannot merge itself: {TOOL} rebases the submodule's branch onto the landed one instead, but ")
SUBMODULE_REBASE_PIN_FILE_ALONE = M("{pin_file} conflicts with no gitlink to rewrite it from: resolve the rebase by hand")
SUBMODULE_REBASE_PIN_DELETED = M("one side deleted the pin: resolve the rebase by hand (`git rebase {upstream}` there)")
SUBMODULE_REBASE_THEN_PULL = M("{problem}; then `{TOOL} pull`")
SUBMODULE_REBASE_NO_WORKTREE = M("no worktree of it on {wb} is under {checkout} to rebase: "
                                 "`{TOOL} open --submodule {name}` there")
SUBMODULE_REBASE_EDITS = M("its worktree ({dir}) has edits, which that rebase cannot keep: stash or commit them")
SUBMODULE_REBASE_LEAVES_BEHIND = M("note: submodule {name}: pinning its worktree leaves {n} deployed commit(s) "
                                   "behind as such (rebased, amended or dropped):\n  {commits}")
SUBMODULE_REBASE_UNFETCHED = "its origin could not be fetched (above)"
SUBMODULE_REBASE_NOT_CLEAN = M("its {wb} does not rebase cleanly onto origin/{default}: `git -C {dir} {hint}`, resolve")
SUBMODULE_REBASE_NO_REWRITES = M("its rebase reported no rewritten commits (git's post-rewrite hook did not run): "
                                 "rebase it by hand, `git -C {dir} {hint}`")
SUBMODULE_REBASED = M("submodule {name}: {wb} rebased onto origin/{default} ({head})")
SUBMODULE_PUSHED = M("submodule {name}: pushed {wb}")
SUBMODULE_PUT_BACK = M("submodule {name}: {wb} put back to {rev}")

# --- submodule.py: open --submodule
SUBMODULE_NOT_A_REPO = M("submodule {name}: {repo} is not a git repository")
SUBMODULE_NOT_A_SUBMODULE = M("{name} is no submodule of this repository: `git submodule add --name {name} <url> "
                              "<path>` first, with flake.nix taking it as a relative path input")
SUBMODULE_ORIGIN_MISMATCH = M("submodule {name}: {repo}'s origin is {origin}, not .gitmodules' {url}, "
                              "which the build fetches the pushed branch from")
SUBMODULE_AT_OFF_BRANCH = M("submodule {name} at {dir} is on {cur}, not {wb}: `git -C {dir} switch {wb}`")
SUBMODULE_ALREADY_OPEN = M("submodule {name}: {dir} (on {wb})")
SUBMODULE_DIR_NOT_EMPTY = M("{dir} is not empty and no worktree: move it away")
SUBMODULE_CREATE_FETCH_FAILED = M("submodule {name}: fetching {repo}'s origin failed:\n{stderr}")
SUBMODULE_BRANCH_DIVERGED = M("submodule {name}: the local branch {wb} ({local}) and origin's ({remote}) diverged: "
                              "reconcile them in {repo} first")
SUBMODULE_FROM_LOCAL = M("the local branch {wb}")
SUBMODULE_OPENED = M("submodule {name}: {dir} on {wb} (from {where})")
SUBMODULE_NOT_CONFIGURED = M("{name} is not a configured submodule{hint}")
SUBMODULE_CONFIGURED_ARE = M(" (they are: {names})")
SUBMODULE_NONE_CONFIGURED = M(' ({CONFIG_FILE}: [submodules.<input>] repo = "...")')
SUBMODULE_NO_WORKTREE = M("no worktree of submodule {name} under this checkout{where}")
SUBMODULE_NO_WORKTREE_AT = M(" ({path})")
SUBMODULE_NO_GITMODULES_ENTRY = " (nor a .gitmodules entry)"

# --- submodule.py: deploy (sync, catch_up)
SUBMODULE_DROPPED = M("submodule {name}'s worktree ({dir}) was reset below the work the tree deploys on {wb}; "
                      "{n} commit(s) would leave the pin and the targets:\n  {commits}"
                      "\nput them back (git -C {dir} reset --keep {pinned}), or drop them deliberately: "
                      "{TOOL} close {name} --force")
SUBMODULE_CATCH_UP_UNFETCHED = M("submodule {name}: its origin could not be fetched (above), so its branch "
                                 "cannot be brought onto the default branch before it is pinned")
SUBMODULE_PINNED_ON = M("submodule {name}: pinned on {rev} ({wb})")
SUBMODULE_IN_MAIN_CHECKOUT = ("a submodule worktree in the main checkout, which has no work branch to carry it: "
                              "remove it, or work in a checkout on a work branch")
SUBMODULE_UNSTAGED_NOT_DEPLOYED = M("note: submodule {name} has unstaged edits, which are not deployed")
SUBMODULE_STAGED_DRY = M("submodule {name} has staged changes, which a dry activation cannot carry "
                         "(the build fetches the submodule from its origin): deploy, or unstage them")
SUBMODULE_STAGED_NO_MSG = M('submodule {name} has staged changes; describe this increment with -m "..."')
SUBMODULE_BEHIND_DEFAULT = M("submodule {name}'s {wb} does not contain origin/{default}{fix}")
SUBMODULE_BEHIND_DEFAULT_DRY = " (a dry activation brings no submodule along): deploy first"
SUBMODULE_BEHIND_DEFAULT_REBASE = M(": rebase it in {dir}, then re-run")
SUBMODULE_UNDEPLOYED_DRY = M("submodule {name} has commits not deployed yet, which a dry activation cannot carry: "
                             "deploy first")
SUBMODULE_PUSHED_AT = M("submodule {name}: pushed {wb} ({rev})")
SUBMODULE_PIN_LEAVES_BEHIND = M("note: submodule {name}: pinning {rev} leaves {n} deployed commit(s) "
                                "behind as such (rebased, amended or dropped):\n  {commits}")

# --- submodule.py: land
SUBMODULE_LANDED_TREE_PIN = M("submodule {name}: the landed tree pins {want}, not the end of its landings {end}; "
                              "nothing landed")
SUBMODULE_FINISH_FAILED = M("warning: submodule {name}: finishing its landing failed: {error}; main pins "
                            "{landed}; bring its worktree and {wb} onto that rev by hand (in {dir})")
SUBMODULE_LAND_STAGED = M("submodule {name} has staged but undeployed changes; deploy or unstage them first")
SUBMODULE_LAND_UNSTAGED = M("submodule {name} has unstaged edits, which its rebase onto the default branch cannot "
                            "keep: stage and deploy them, or put them aside ({n} path(s))")
SUBMODULE_LAND_UNFETCHED = M("submodule {name}: cannot land it unfetched")
SUBMODULE_LAND_NOT_DEPLOYED = M("submodule {name}'s HEAD ({head}) is not what the tree pins ({pinned}): "
                                "deploy it first")
SUBMODULE_LANDING_SUBJECT = M("landing of {wb}")
SUBMODULE_LAND_CONFLICT = M("submodule {name}'s {wb} conflicts with origin/{default}: rebase it there "
                            "(in {dir}), deploy, then re-run")
SUBMODULE_LAND_PUSH_FAILED = M("pushing submodule {name}'s landing commit to its origin failed:\n{stderr}")
SUBMODULE_PIN_COMMIT = M("submodule {name}: pinned on {default}")
SUBMODULE_PUSH_DEFAULT_FAILED = M("warning: submodule {name}: pushing {default} failed (moved since fetch?); "
                                  "main pins {rev}, kept on its origin as {ref} until a landing carries it: "
                                  "in {dir}, fetch and merge origin/{default}, deploy, then land again:\n{stderr}")
SUBMODULE_LANDED = M("submodule {name}: landed on {default} ({rev})")
SUBMODULE_PUSH_BRANCH_FAILED = M("pushing submodule {name}'s {wb} failed - it moved on its origin, but this "
                                 "worktree is its only writer.  Investigate:\n{stderr}")

# --- submodule.py: undeploy, close, restore
SUBMODULE_RESET = M("submodule {name}: {wb} reset onto origin/{default}{was}")
SUBMODULE_RESET_WAS = M(" (was {rev}, see its reflog)")
SUBMODULE_UNDEPLOY_ADDED = M("the increment added submodule {name}, whose worktree and branch would stay behind: "
                             "`{TOOL} close {name} --force` first")
SUBMODULE_UNDEPLOY_STAGED = M("submodule {name} has staged changes, which the deploy would carry; "
                              "deploy or unstage them first")
SUBMODULE_UNDEPLOY_PIN_MISSING = M("submodule {name}: the pin to put back, {rev}, is not in {repo}")
SUBMODULE_UNDEPLOY_LOST_EDITS = M("submodule {name}: the increments' files have unstaged edits, which the reset "
                                  "would lose; stash or discard them in {dir} first: {files}")
SUBMODULE_UNDEPLOY_RESET_FAILED = M("submodule {name}: the reset onto {rev} failed (the checkout is reset below "
                                    "the increment already): `git -C {dir} reset --keep {rev}`, then deploy:\n"
                                    "{stderr}")
SUBMODULE_UNDEPLOY_RESET = M("submodule {name}: {wb} reset onto {rev} (was {prev}, see its reflog)")
SUBMODULE_KEEP_CHANGES = "it has uncommitted or untracked changes"
SUBMODULE_KEEP_NO_DEFAULT = "its repository's origin has no default branch to judge it against"
SUBMODULE_KEEP_COMMITS = M("it carries commits not on origin/{default}; land or undeploy this checkout")
SUBMODULE_KEEP_PIN = M("the tree still pins a rev not on origin/{default}; land or undeploy this checkout")
SUBMODULE_BRANCH_NOT_OURS = M("submodule {name}: {branch} is no work branch of this machine's; kept")
SUBMODULE_BRANCH_DELETE_FAILED = M("warning: deleting submodule {name}'s {branch} on its origin failed:\n{stderr}")
SUBMODULE_REMOVED = M("submodule {name}: worktree removed{with_branch}")
SUBMODULE_REMOVED_WITH = M(" with {branch}")
SUBMODULE_MAIN_PINS_NONE = M("{main} pins no {path} to restore {name}'s pin from")
SUBMODULE_RESTORED = M("submodule {name}: pinned on {main}'s {rev}, staged (deploy to revert it on the targets)")

# --- submodule.py: status notes
SUBMODULE_NOTE_AHEAD = M("+{n}")
SUBMODULE_NOTE_STAGED = M("staged {n}")
SUBMODULE_NOTE_UNSTAGED = M("unstaged {n}")
SUBMODULE_NOTE_UNTRACKED = M("untracked {n}")
SUBMODULE_NOTE_DETACHED = "detached!"
SUBMODULE_REPO_EDITED = M("{repo} ({name}): {n} edited file{s} - no deploy picks these up; "
                          "`{TOOL} open --submodule {name}` is where {name} is worked on")


# --- config.py: reading nixconvoy.toml
CONFIG_MISSING = M("no {CONFIG_FILE} at {root}: `{TOOL} init` writes one")
CONFIG_TOML = M("{CONFIG_FILE}: {error}")
CONFIG_VERSION = M("{CONFIG_FILE}: config_version {version} is not supported (1 is)")
CONFIG_REQUIRED = M("{CONFIG_FILE}: {key} is required (`{TOOL} init` writes a complete file)")
CONFIG_UNKNOWN_KEY = M("{CONFIG_FILE}: unknown key {key}")
CONFIG_UNKNOWN_KEY_LAST = M("{CONFIG_FILE}: unknown key {key} (last is a target's setting: [targets.<name>.switch])")
CONFIG_NOT_A_TABLE = M("{where} must be a table")
CONFIG_NOT_A_STRING = M("{where}.{key} must be a string")
CONFIG_NOT_STRINGS = M("{where}.{key} must be a list of strings")
CONFIG_NOT_AUTO_BOOL = M('{where}.{key} must be true, false or "auto"')
CONFIG_NOT_A_BOOL = M("{CONFIG_FILE}: {where}.{key} must be true or false")
CONFIG_NOT_A_TEMPLATE = M("{CONFIG_FILE}: {key} is a template over {{{placeholder}}} alone ({error!r})")
CONFIG_DEADLINE = M("{CONFIG_FILE}: {where}.deadline is a number of seconds, above 0")
CONFIG_WORK_BRANCHES_IGNORED = M("note: {CONFIG_FILE}: work_branches is ignored (a work branch is any branch shaped like "
                                 "branch_prefix, {{machine}} in it any hostname): drop the line")
CONFIG_BRANCH_PREFIX_CHARS = M("{CONFIG_FILE}: branch_prefix may hold ASCII letters, digits, '.', '-', '_', '/' and "
                               "{{machine}} only: {branch_prefix!r}")
CONFIG_CHILD_WORKTREES = M("{CONFIG_FILE}: child_worktrees is a directory inside the checkout")
CONFIG_REMOTE = M("{CONFIG_FILE}: remote is a git remote's name (letters, digits, '.', '-', '_', "
                  "not starting with '-'), not {remote!r}")
CONFIG_MAIN_BRANCH = M("{CONFIG_FILE}: main_branch is a branch's name (main, not {remote}/main or refs/...), "
                       "not {main_branch!r}")
CONFIG_STORE_AND_BUILDER = M("{CONFIG_FILE}: build.store and build.builder are the same setting; keep build.store")
CONFIG_STORE_TYPE = M("{CONFIG_FILE}: build.{key} is a machine name, or a table of them keyed by "
                      "the hostname {TOOL} runs on")
CONFIG_STORE_ENTRY = M("{CONFIG_FILE}: build.{key}.{name} must be a string")
CONFIG_SKIP_SUBSTITUTER = M("{CONFIG_FILE}: build.skip_substituters entry {entry!r} is not a substituter URL")
CONFIG_TARGET_DIR = M("{CONFIG_FILE}: targets.{name}.dir is a directory inside the repository, "
                      "relative to its root, not {dir!r}")
CONFIG_TARGET_DIR_ROOT = M("{CONFIG_FILE}: targets.{name}.dir names the repository root: leave it out instead")
CONFIG_NO_TARGETS = M("{CONFIG_FILE}: targets_dir, or a [targets.<name>] table per target, is required")
CONFIG_SUBMODULE_REPO = M("{CONFIG_FILE}: submodules.{name}.repo is required")
CONFIG_SUBMODULE_PIN_FILE = M("{CONFIG_FILE}: submodules.{name}.pin_file must be a path inside the repository")

# --- memo.py: the eval memo
MEMO_LOCK_WAIT = "waiting for another deploy's turn at the eval memo..."
MEMO_MEMOIZED = M("  (memoized: {targets})")
MEMO_NO_NIX_INSTANTIATE = "nix-instantiate is not on PATH: a memoized .drv cannot be rooted"
MEMO_ROOT_TIMEOUT = M("rooting {drv} timed out after {timeout} s: is the daemon stuck?")
MEMO_ROOT_FAILED = M("rooting {drv} failed:\n{stderr}")
MEMO_SPECIAL_ARGS = ("eval memo: flake.nix's specialArgs is not an inherit list; "
                     "keying every target on the whole tree")
MEMO_WHOLE_REPO = M("eval memo: {target} keys on the whole repository: {via} names its root "
                    "(a directory named by a path counts whole)")
MEMO_WHOLE_TREE = M("eval memo: {target} keys on the whole {tree}/ tree: {via} names it "
                    "(a directory named by a path counts whole)")
# What names a target's root, as MEMO_WHOLE_REPO's {via} says it.
MEMO_VIA_CONFIG = M("{CONFIG_FILE}'s targets.{target}")
MEMO_VIA_DIRECTORY = M("{target}'s directory")
MEMO_UNION_CONFIG = M("the union's {CONFIG_FILE} cannot be read: {error}; {NOTHING_DONE}")
MEMO_KIT_MISSING = M("{src} is missing: {TOOL} runs from its nix package (nix run .#nixconvoy)")
MEMO_KIT_HASH_FAILED = M("hashing the switch kit's sources {src} failed:\n{stderr}")
MEMO_PARALLEL_PROBE_FAILED = M("probing nix for parallel evaluation failed:\n{stderr}")
MEMO_EVAL_FAILED = M("evaluating {target} failed:\n{stderr}")
MEMO_NO_TOPLEVEL = M("nix eval returned no toplevel for {target}:\n{stdout}")
MEMO_DRV_COLLECTED = M("{target}'s .drv was collected as soon as it was evaluated; re-run")
MEMO_CHECK_FAILED = M("{failure}; {NOTHING_DONE}")
MEMO_PRECHECK_WHAT = M("precheck {check}")
MEMO_PRECHECK_EVAL_FAILED = M("evaluating the precheck failed:\n{stderr}")
MEMO_PRECHECK_NO_DRV = M("nix eval returned no derivation for the precheck:\n{stdout}")
MEMO_SHORT_WRITE = "short write"
MEMO_PROOF_WRITE_FAILED = M("eval memo: recording the precheck's pass in {log} failed ({error}); "
                            "the next deploy runs it again")
