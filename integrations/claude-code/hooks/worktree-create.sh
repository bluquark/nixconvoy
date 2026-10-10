#!/bin/bash
# WorktreeCreate hook. Claude Code's own logic puts an isolated subagent's
# worktree under the repository's primary worktree, where nixconvoy would
# take it for that checkout's own subagent.  This creates it under the
# spawning checkout's own root instead (<checkout>/.claude/worktrees/<name>,
# owning <checkout's branch>-<name>) on the fresh branch worktree-<name> off
# origin's default branch, as Claude Code would (an existing worktree of
# that name is reused, as `claude --worktree <name>` expects), and takes the
# same kind of lock Claude Code takes (the spawning claude process's pid and
# start time), which nixconvoy's status, prune and abandon read.  The hook
# replaces Claude Code's logic entirely, so neither a finished subagent nor
# the periodic sweep removes these: once the session exits, the `nixconvoy
# prune --after` session-start.sh started removes those holding nothing of
# their own, and land or abandon the others (worktree-remove.sh covers the
# removals Claude Code does make, a `claude --worktree` session's).
# hooks.json beside it wires the three hooks up as the nixconvoy plugin.
set -eu
command -v jq >/dev/null 2>&1 || { echo "worktree-create: jq missing" >&2; exit 1; }
INPUT=$(cat)
name=$(printf '%s' "$INPUT" | jq -r '.name')
cwd=$(printf '%s' "$INPUT" | jq -r '.cwd // ""')
root=$(git -C "${CLAUDE_PROJECT_DIR:-${cwd:-.}}" rev-parse --show-toplevel)
dir="$root/.claude/worktrees/$name"
base=$(git -C "$root" rev-parse -q --verify refs/remotes/origin/HEAD \
    || git -C "$root" rev-parse -q --verify refs/remotes/origin/main \
    || git -C "$root" rev-parse HEAD)
branch="worktree-$name"
git -C "$root" worktree prune >&2  # a registration whose directory is gone

# The nearest claude ancestor: the session whose lifetime the lock spans,
# taken as the worktree is added, so no prune sees it unlocked.  No lock
# when none is found, rather than one nobody's exit releases.
pid=$PPID
while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
  case "$(ps -o comm= -p "$pid" 2>/dev/null)" in *claude*) break ;; esac
  pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
done
lock=()
if [ -n "$pid" ] && [ "$pid" -gt 1 ]; then
  reason="claude agent $name (pid $pid"
  if [ -r "/proc/$pid/stat" ]; then
    reason="$reason start $(sed 's/.*) //' "/proc/$pid/stat" | awk '{print $20}')"
  fi
  lock=(--lock --reason "$reason)")
fi
if [ "$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" = "$dir" ]; then
  # The worktree exists: reused (`claude --worktree <name>` again).  Its
  # lock file is replaced in one rename, never unlocked in between.
  if [ ${#lock[@]} -gt 0 ]; then
    locked=$(git -C "$dir" rev-parse --path-format=absolute --git-path locked)
    printf '%s' "${lock[2]}" > "$locked.tmp" && mv -f "$locked.tmp" "$locked"
  fi
elif git -C "$root" rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
  # The branch outlived its worktree: checked out as it is.
  mkdir -p "$root/.claude/worktrees"
  git -C "$root" worktree add --quiet ${lock[@]+"${lock[@]}"} "$dir" "$branch" >&2
else
  mkdir -p "$root/.claude/worktrees"
  git -C "$root" worktree add --quiet ${lock[@]+"${lock[@]}"} -b "$branch" "$dir" "$base" >&2
fi
printf '%s\n' "$dir"
