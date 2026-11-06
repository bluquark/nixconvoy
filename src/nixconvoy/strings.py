"""Every sentence nixconvoy says: what die() and the notes print, the
verdicts, the usage, and the prose in what it writes for people (the
commit messages it composes, the nixconvoy.toml init writes).  The
switch-guard crate's are in switch-guard/src/strings.rs.

A message is a template over named fields, filled by calling it -
LAND_NO_MSG_FILE(wb=wb) - and the vocabulary below fills itself ({TOOL},
{CONFIG_FILE}, ...).  A literal brace is doubled: {{local_hostname}}.  A text
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


# --- cli.py: usage and dispatch
# The synopsis: one line per form under its section's heading (= ...).
# <x> a value, [x] optional, (a | b) one of, ... repeated.
SYNOPSIS = """\
= Setting up a repository
init [<flake-dir>]
= Managing worktrees
open <path> [--branch <branch-suffix>]
open --child <worktree>
open --submodule <input>
close [<path> ... | <worktree> ... | <submodule> ...] [--force]
pull
status
hook <event> < <hook-input>
= Deploying and committing changes
deploy [<target> | @<tag> ...] [-m <msg>] [--evict] [--dry-activate]
undeploy [<worktree> ... | --with-children] [--back-to <rev>] [--dry-activate]
review [<worktree> | <submodule> | <branch> ... | --with-children] [<diff-arg> ...]
land [<worktree> ... | --with-children] [-m <msg>] [--not-yet-deployed]
"""
USAGE_HEADING = "usage:"
CLI_VERSION = M("{TOOL} {version}{rev}\n")
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
LAND_HOLDS_BACK = M("{wb} is not yet deployed to {them}: work of it reaches {it_them} "
                    "that no deploy from its checkout brought there.  "
                    "`{TOOL} deploy {targets}` from its checkout deploys it there first; "
                    "`land --not-yet-deployed` lands it as it is, for the next deploy anywhere to roll out")
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
LAND_PUSH_MAIN_UNSURE = M("pushing {main_branch} failed, and {remote} may yet take it; re-run, which finishes "
                          "the landing if it did:\n{stderr}")
LAND_FINISHING = M("note: {remote} took {commit}, a landing from here that was not finished ({file}): "
                   "finishing it")
LAND_LEFT_EDITED = M("note: {name} has an edit in the way of the landing: left where it is, for its next "
                     "catch-up (`{TOOL} pull`)")
LAND_SAID_ABOVE = "see above"
LAND_NOT_FINISHED = M("warning: the landing was not finished ({error}); the next `{TOOL} land`, `deploy` or "
                      "`pull` here tries again")
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
UNDEPLOY_DRY = M("{DRY_FLAG}: would undeploy {wb}'s {n} increment(s):\n  {summaries}")
UNDEPLOY_DRY_REVERTING = M("deployed to {targets}: dry-activating the deploy that would revert it")

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
                     "shared).  `--branch {name}` makes it this checkout's, `--branch <branch-suffix>` opens another")
OPEN_NOT_WORK_BRANCH = M("{branch} is not a work branch of this machine's: one named {prefix}<branch-suffix>; "
                            "`--branch <branch-suffix>` names one")
OPEN_BAD_SUFFIX = M("{branch}: a checkout's branch is {prefix}<branch-suffix> with a suffix that is not 'main' "
                    "and holds no '-'; name one with --branch <branch-suffix>")
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
STATUS_HOLDS_BACK = M("not yet deployed to {targets}")
STATUS_EMPTY_SESSIONLESS = M("empty, its create found no claude session: {TOOL} close {name} removes it")
STATUS_EMPTY_HAND_LOCKED = "empty, locked by hand: the sweep keeps it"
STATUS_EMPTY_SESSION_LOCKED = ("empty, locked by its agent's claude session: closed when its agent "
                               "stops or that session exits")
STATUS_EMPTY = M("empty: {TOOL} close {name} removes it")
STATUS_SUBJECT = M('"{subject}"')
STATUS_OP_IN_PROGRESS = M("[{op} in progress]")
STATUS_DEPLOYING = "[deploying]"
STATUS_NOT_A_CHECKOUT = "(not a checkout here)"
STATUS_ORPHANS_HEADER = "orphaned subagent branches (no live session holds them; each still unions into every deploy):"
STATUS_ORPHAN_ENTRY = M("{branch} +{n}: {summary}  {where}")
STATUS_ORPHAN_NO_WORKTREE = "(no worktree)"
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
                      "`git checkout -b {prefix}<branch-suffix>` and re-run")

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
CHECKOUT_REBASE_MAP_SUBJECT = M("rebase map of {branch}")
CHECKOUT_WITH_MAIN_SUBJECT = M("{main} merged for a dry activation")
CHECKOUT_WITH_MAIN_CONFLICT = M("{DRY_FLAG}: {main} does not merge cleanly into this checkout's work, which a dry "
                                "activation does not rebase; `{TOOL} pull` first")
CHECKOUT_ANNOUNCE_SUBJECT = M("pending increment of {branch} from {machine}:{cwd} (pid {pid})")
CHECKOUT_RECORD_MESSAGE = M("deploy {target}: {subject}\n\n{doc}\n")
CHECKOUT_COMMIT_TREE_FAILED = "commit-tree failed"
CHECKOUT_PUSH_FAILED = "push failed"
CHECKOUT_RENEW_FAILED = M("note: renewing this deploy's announcement failed; a deploy "
                          "elsewhere may take this one for dead:\n{detail}")

# --- checkout.py: pushes
CHECKOUT_PUSH_TIMED_OUT = "timed out"
CHECKOUT_PUSH_NO_PID = "no pid"
CHECKOUT_PUSH_GONE = "gone without a status"
CHECKOUT_PUSH_LOCK_HELD = M("the previous run's background push still runs (holding {lock}) {secs} s after this "
                            "deploy's end, so nothing of it is pushed: re-run once that push is over")
CHECKOUT_PUSH_BG_FAILED = M("note: the previous run's background push failed ({status}); this run's "
                            "push retries it{log}")
CHECKOUT_PUSH_BG_FAILED_FINAL = M("note: the last background push of {where} failed ({status}): the deploy records "
                                  "it carried are not on {remote}, and nothing retries them{log}")
CHECKOUT_PUSH_BG_LOG = M(":\n{log}")
CHECKOUT_SOLE_WRITER = M("{wb} moved on {remote} ({sha}): another checkout deployed on it, but a work branch has one "
                         "writer.  `git log refs/heads/{wb}..{up_ref}` shows what it holds; in that "
                         "branch's checkout, `git rebase --autostash {up_ref}` carries this checkout's "
                         "increments onto it, or `git checkout -b {prefix}<branch-suffix>` moves them to a branch of their "
                         "own; then re-run")
CHECKOUT_PUSH_WB_FAILED = M("pushing {wb} failed - it moved on {remote}, but this checkout is its "
                            "only writer.  Investigate:\n{stderr}")
CHECKOUT_PUSHED = M("pushed {wb} ({sha})")
CHECKOUT_PUSHING_BRANCH = M("{wb} ({sha})")
CHECKOUT_PUSHING_BG = M("pushing {what} in the background")
CHECKOUT_PUSHING_WAITED = M("pushing {what} (a service's run: waited for)")
CHECKOUT_PUSH_WAITED_FAILED = M("note: the push failed ({status}); the next run's push retries it{log}")
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
PROC_REMOTE_RETRY = M("note: git {verb}: the remote failed on its own side ({error}); retrying in {secs} s")
PROC_AGENIX_DECRYPTED = M("[agenix] decrypted {n} secrets (generation {generation})")

# --- deploy.py
DEPLOY_ALIVE_AGAIN = M("{key}'s deploy is under way again: waited for after all")
DEPLOY_STALE_STAMP = M("warning: {key}'s deploy last renewed its announcement {minutes:.0f} min ago (by its machine's clock): taken as dead, going on without it")
DEPLOY_NO_NEWS = M("warning: no news of {key}'s deploy for {secs} s (its announcement is no longer renewed): taken as dead, going on without it")
DEPLOY_KILLED_BY_SIGNAL = M("killed by signal {signum}")
DEPLOY_RUNNING_PRECHECK = "running the precheck..."
DEPLOY_REACHES_NONE = M("reaches no target: committed onto {wb} and pushed; nothing built or switched (the probes' results dropped)")
DEPLOY_FOREIGN_INCREMENTS = M("{remote} holds {wb} with increments this worktree never pushed: another worktree of this name?  `git log {main_ref}..{remote_wb}` shows them; open this worktree under another name, or `git branch {wb} {remote_wb}` to take them over")
DEPLOY_NOT_WORK_BRANCH = M("on branch {branch}, not a work branch of this machine's ({prefix}<branch-suffix>); the main checkout deploys from {main_branch} (a hand commit there is claimed as {main_branch}'s until pushed)")
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
DEPLOY_ASSESSED_PLAIN = M("warning: {target} is switched plainly (auto_rollback_checks = false) but carries the boot-assessment module: a counted boot there runs no switch checks")
DEPLOY_REJECTED_GENERATION = M("{target}'s bootloader rejected the very generation this deploy would switch it to ({stub}): it failed its boot assessment and the target fell back.  Nothing was built, switched or committed.  See `journalctl -u boot-assessment-watch -u boot-assessment` on {target}; then change the configuration, or re-arm the stub to retry it as is (sudo mv of the +0-N stub to +2 under /boot/EFI/Linux).")
# A deploy record's subject: the main branch, then each unioned branch.
DEPLOY_SUBJECT_BRANCH = M(" + {branch}")
DEPLOY_UNMERGED = M("not deployed to {targets}: another branch as deployed there does not merge with this "
                    "checkout's work (the warning above says which, and how to end it)")
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
SWITCH_CHECK_EXITED = M('"{statement}" exited {code}')
SWITCH_CHECK_TIMED_OUT = M('"{statement}" timed out')
SWITCH_CHECK_UNPARSED = M('{shell} cannot parse "{statement}": {detail}')
DEPLOY_CONTROLLER_CHECK_UNPARSED = M("nothing switched: {target}'s controller_shell check: {fault}")
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
TARGETS_NIX_TIMEOUT = M("`nix {cmd}` timed out after {secs} s")
TARGETS_NIX_NO_JSON = M("`nix {cmd}` printed no JSON object:\n{output}")
TARGETS_NIX_FAILED = M("`nix {cmd}` failed:\n{error}")
TARGETS_BUILDER_ARCH = M("its nix builds {systems}, not {arch} (targets_arch)")
TARGETS_UNTRUSTED_ROOT_FAILED = M("the nix daemon does not trust the user (nix.settings.trusted-users), and nix as root ({sudo}) failed: {error}")
TARGETS_UNTRUSTED_NOR_ROOT = "the nix daemon does not trust the user (nix.settings.trusted-users), nor root"
TARGETS_COPY_DRVS_FAILED = M("copying derivations to {host} failed{detail}")
TARGETS_BUILD_FAILED_ON_BECAUSE = M("{what} failed on {host}: {error}")
TARGETS_BUILD_FAILED_ON = M("{what} failed on {host}{detail}")
TARGETS_BUILD_FAILED_BECAUSE = M("{what} failed: {error}")
TARGETS_BUILD_FAILED = M("{what} failed{detail}")
TARGETS_COPYING_CLOSURE = M("copying closure to {target}...")
TARGETS_COPY_CLOSURE_FAILED = M("copying the closure to {target} failed; {NOTHING_DONE} - the changes are still staged{detail}")
TARGETS_BUILDER_SKIPPED = M("builder {host} is unusable: {problem}")
TARGETS_NO_BUILDER = M("no usable builder among {names} ({CONFIG_FILE}: builders); {NOTHING_DONE}")
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
INIT_TOML_BRANCH_PREFIX = M(
    "# Prefix for all nixconvoy work branches (visible in `git branch`).\n"
    "# {{local_hostname}} can be omitted for concision, if you only ever launch nixconvoy from one machine.")
INIT_TOML_CHILD_WORKTREES = M(
    "# The subdirectory child worktrees are placed in.\n"
    "# The default directory is set to Claude Code's, so that {TOOL}'s Claude Code plugin works out of the box.")
INIT_TOML_PRECHECK = M("# Precheck step that runs in parallel with eval. Names a flake check (checks.<targets_arch>.<name>).")
INIT_TOML_BUILD = (
    "# List of build machines with fallbacks if the earlier ones are unavailable.\n"
    "# The {local_hostname} is considered unavailable if it's the wrong CPU architecture (i.e. my Macbook).\n"
    "builders = [\"{local_hostname}\"]")
# A statement proving the internet reachable from where it runs: exactly the 204 Google answers.
PROBE = '[ "$(curl -s --max-time 8 -o /dev/null -w "%{http_code}" https://www.google.com/generate_204)" = 204 ]'
INIT_TOML_SWITCH = M(
    "# Configure automatic rollback after activating the new NixOS generation.\n"
    "# The default check recommended for all setups is to roll back if ssh connectivity is lost after N seconds.\n"
    "# Another built-in check is whether the target reaches the internet, and you can also provide arbitrary other\n"
    "# checks that run on either the target machine or the control machine.\n"
    "[switch]\n"
    "auto_rollback_checks = [\n"
    "  {{ ssh_deadline_secs = {deadline} }},\n"
    "#   {{ internet = true }},  # the target reaches the internet\n"
    "#   {{ target_shell = [\"systemctl is-active -q nginx.service\"] }},  # run as root on the target\n"
    "]\n"
    "# [targets.router.switch]\n"
    "# # this machine still reaches the internet, through it\n"
    "# auto_rollback_checks = [{{ controller_shell = ['" + PROBE.replace("{", "{{").replace("}", "}}") + "'] }}]")
INIT_TOML_TARGETS = (
    "# nixconvoy init populates your targets from nixosConfiguration.")
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
SUBMODULE_PIN_COMMIT_MISSING = M("submodule {name}: {sha} is not in {repo}, even fetched: no pin file for it")
SUBMODULE_PIN_NAR_FAILED = M("{name}: nix flake prefetch of {sha} failed, no NAR hash for its pin file:\n{stderr}")
SUBMODULE_PIN_FILE_STALE = M("warning: {file} is not the pin file of the rev the tree pins submodule {name} on: "
                             "a deploy from a checkout on a work branch rewrites it")
SUBMODULE_PIN_FILE_REWRITTEN = M("submodule {name}: {file} rewritten for {rev}")
SUBMODULE_PIN_FILE_REMOVED = M("submodule {name}: {file} removed, its gitlink gone")
SUBMODULE_PIN_FILE_UNCLAIMED = M("warning: {file} is no submodule's pin file: "
                                 "a deploy from a checkout on a work branch removes it")
SUBMODULE_PIN_FILE_UNCLAIMED_REMOVED = M("{file} removed: no submodule's pin file")
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
SUBMODULE_NOT_ON_ORIGIN = M("note: submodule {name}: {sha} is not on its origin")
SUBMODULE_UNION_SUBJECT = M("union of {name}")
SUBMODULE_UNION_PUSH_FAILED = M("submodule {name}: pushing its union to {ref} failed:\n{stderr}")
SUBMODULE_UNION_DONE = M("submodule {name}: union of {ours} and {theirs} is {sha}, on {ref}")

# --- submodule.py: rebase
SUBMODULE_REBASE_WHY = M("{upstream} and {wb} both moved submodule {name}'s pin (a landing there), which git "
                         "cannot merge itself: {TOOL} rebases the submodule's branch onto the landed one instead, but ")
SUBMODULE_REBASE_PIN_FILE_ALONE = M("{file} conflicts with no gitlink to rewrite it from: resolve the rebase by hand")
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

# --- submodule.py: the repository
SUBMODULE_NO_PRIMARY = M("{top} is no worktree of a repository with a .git directory to hold the submodules' "
                         "repositories")
SUBMODULE_INIT_FAILED = M("submodule {name}: cloning it (`git submodule update --init` in {primary}) failed:"
                          "\n{stderr}")
SUBMODULE_CLONED = M("submodule {name}: cloned, {dir}")
SUBMODULE_REPO_OWN_COMMITS = M("{repo} (submodule {name}): holds commits not on origin/{default}, left as it is; "
                               "`{TOOL} open --submodule {name}` is where {name} is worked on")
SUBMODULE_REPO_AT = M("{default} at {rev}")
SUBMODULE_REPO_OFF_DEFAULT = M("{repo} (submodule {name}): on {cur}, not {default}, left as it is "
                               "(the main checkout's gitlink is dirty until it is back on {default})")
SUBMODULE_REPO_NO_DEFAULT = M("{repo} (submodule {name}): origin/{default} is not here; fetched?")

# --- submodule.py: open --submodule
SUBMODULE_NOT_A_SUBMODULE = M("{name} is no submodule of this repository{hint}: `git submodule add --name {name} "
                              "<url> <path>` first, with flake.nix taking it as a relative path input")
SUBMODULE_NAMES_ARE = M(" (they are: {names})")
SUBMODULE_ORIGIN_MISMATCH = M("submodule {name}: {repo}'s origin is {origin}, not .gitmodules' {url}, "
                              "which the build fetches the pushed branch from: `git -C {primary} submodule sync -- {path}`")
SUBMODULE_AT_OFF_BRANCH = M("submodule {name} at {dir} is on {cur}, not {wb}: `git -C {dir} switch {wb}`")
SUBMODULE_ALREADY_OPEN = M("submodule {name}: {dir} (on {wb})")
SUBMODULE_DIR_NOT_EMPTY = M("{dir} is not empty and no worktree: move it away")
SUBMODULE_CREATE_FETCH_FAILED = M("submodule {name}: fetching {repo}'s origin failed:\n{stderr}")
SUBMODULE_BRANCH_DIVERGED = M("submodule {name}: the local branch {wb} ({local}) and origin's ({remote}) diverged: "
                              "reconcile them in {repo} first")
SUBMODULE_FROM_LOCAL = M("the local branch {wb}")
SUBMODULE_OPENED = M("submodule {name}: {dir} on {wb} (from {where})")
SUBMODULE_NO_WORKTREE = M("no worktree of submodule {name} under this checkout ({path})")

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
SUBMODULE_PUSH_DEFAULT_LATER = M("warning: submodule {name}: pushing {default} failed; main pins {rev}, kept on "
                                 "its origin as {ref}: the next `{TOOL} land`, `deploy` or `pull` here tries "
                                 "again:\n{stderr}")
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
CONFIG_NOT_A_TABLE = M("{where} must be a table")
CONFIG_NOT_A_STRING = M("{where}.{key} must be a string")
CONFIG_NOT_STRINGS = M("{where}.{key} must be a list of strings")
CONFIG_NOT_A_TEMPLATE = M("{CONFIG_FILE}: {key} is a template over {{{placeholder}}} alone ({error!r})")
CONFIG_EMPTY_TEMPLATE = M("{CONFIG_FILE}: {key} is a template over {{{placeholder}}}, not empty")
CONFIG_DEADLINE = M("{CONFIG_FILE}: {where}.ssh_deadline_secs is a number of seconds, above 0 (auto_rollback_checks = "
                    "false switches plainly)")
CONFIG_CHECK_DEADLINE_TWICE = M("{CONFIG_FILE}: {where} is a second ssh_deadline_secs item: one per list (a target's "
                                "replaces [switch]'s)")
CONFIG_CHECK_NO_DEADLINE = M("{CONFIG_FILE}: {where} has checks but no {{ ssh_deadline_secs = <seconds> }} item, their "
                             "deadline, and [switch] gives none")
CONFIG_DEADLINE_SECS_GONE = M("{CONFIG_FILE}: {where}.deadline_secs is gone: the deadline is auto_rollback_checks' "
                              "{{ ssh_deadline_secs = <seconds> }} item, counted from the activation's end")
CONFIG_CHECKS = M("{CONFIG_FILE}: {where} is a list of checks, tables like {{ ssh_deadline_secs = 10 }}, or false "
                  "(switched plainly)")
CONFIG_CHECK_STRIKE = M("{CONFIG_FILE}: {where} strikes nothing: [switch] has no fleet checks over it (leave "
                        "the kind out instead)")
CONFIG_CHECK_ITEM = M("{CONFIG_FILE}: {where} is a table with one key: ssh_deadline_secs = <seconds>, internet = "
                      "true, or target_shell / controller_shell = [\"...\"] (statements, each run in a shell of its "
                      "own); = false strikes the fleet's checks of that kind")
CONFIG_CHECK_SHELL = M("{CONFIG_FILE}: {where}.{kind} is a list of statements (strings), each run in a shell of its own, "
                       "or false (striking the fleet's)")
CONFIG_CHECK_KIND_GONE = M("{CONFIG_FILE}: {where}: `{kind}` is gone: use {instead}")
CONFIG_WORK_BRANCHES_IGNORED = M("note: {CONFIG_FILE}: work_branches is ignored (a work branch is any branch shaped like "
                                 "branch_prefix, {{local_hostname}} in it any hostname): drop the line")
CONFIG_BRANCH_PREFIX_CHARS = M("{CONFIG_FILE}: branch_prefix may hold ASCII letters, digits, '.', '-', '_', '/' and "
                               "{{local_hostname}} only: {branch_prefix!r}")
CONFIG_CHILD_WORKTREES = M("{CONFIG_FILE}: child_worktrees is a directory inside the checkout")
CONFIG_REMOTE = M("{CONFIG_FILE}: remote is a git remote's name (letters, digits, '.', '-', '_', "
                  "not starting with '-'), not {remote!r}")
CONFIG_MAIN_BRANCH = M("{CONFIG_FILE}: main_branch is a branch's name (main, not {remote}/main or refs/...), "
                       "not {main_branch!r}")
CONFIG_EVAL_GONE = M("{CONFIG_FILE}: [eval] is gone: precheck is a top-level key")
CONFIG_BUILDER_GONE = M("{CONFIG_FILE}: builder is gone: builders is a list of machines tried in order, the first "
                        "usable one realising the closures (builders = [\"{{local_hostname}}\"] realises here)")
CONFIG_HOSTNAMES_GONE = M("{CONFIG_FILE}: [hostnames] is gone: a machine falls through builders to one that can "
                          "build, and a target is the machine of its hostname")
CONFIG_PARALLEL_EVAL_GONE = M("{CONFIG_FILE}: parallel_eval is gone: nix evaluates in parallel whenever it can")
CONFIG_NOT_TEMPLATES = M("{CONFIG_FILE}: {key} is a non-empty list of templates over {{{placeholder}}}")
CONFIG_TARGET_DIR = M("{CONFIG_FILE}: targets.{name}.dir is a directory inside the repository, "
                      "relative to its root, not {dir!r}")
CONFIG_TARGET_DIR_ROOT = M("{CONFIG_FILE}: targets.{name}.dir names the repository root: leave it out instead")
CONFIG_NO_TARGETS = M("{CONFIG_FILE}: targets_dir, or a [targets.<name>] table per target, is required")
CONFIG_SUBMODULES_IGNORED = M("note: {CONFIG_FILE}: [submodules] is ignored (every .gitmodules entry is a "
                              "submodule {TOOL} manages, its repository the superproject's .git/modules/<input>): "
                              "drop the table")

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
