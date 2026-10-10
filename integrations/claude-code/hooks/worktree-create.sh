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

# The nearest claude ancestor: the session whose lifetime the lock spans.
# The worktree is never unlocked, so a concurrent `nixconvoy prune`, which
# removes only an unlocked worktree after claiming the lock it judged dead,
# never removes one handed out here: with no ancestor found the lock names
# no session, which prune keeps like a hand lock.
pid=$PPID
while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
  case "$(ps -o comm= -p "$pid" 2>/dev/null)" in *claude*) break ;; esac
  pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
done
if [ -n "$pid" ] && [ "$pid" -gt 1 ]; then
  reason="claude agent $name (pid $pid"
  if [ -r "/proc/$pid/stat" ]; then
    reason="$reason start $(sed 's/.*) //' "/proc/$pid/stat" | awk '{print $20}')"
  fi
  reason="$reason)"
else
  reason="claude agent $name (no claude session found)"
fi

# A prune may remove the worktree, or its fresh branch, between any two of
# these steps; each failure starts over (nixconvoy's prune model checks
# this protocol against the prune core).
for _ in 1 2 3 4 5; do
  if [ "$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" = "$dir" ]; then
    # The worktree exists: reused (`claude --worktree <name>` again), its
    # lock replaced in one rename.
    locked=$(git -C "$dir" rev-parse --path-format=absolute --git-path locked 2>/dev/null) || continue
    if ! { printf '%s' "$reason" > "$locked.tmp" && mv -f "$locked.tmp" "$locked"; } 2>/dev/null; then
      continue
    fi
    if [ "$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" = "$dir" ]; then
      printf '%s\n' "$dir"
      exit 0
    fi
  else
    git -C "$root" worktree prune >&2  # a registration whose directory is gone
    mkdir -p "$root/.claude/worktrees"
    if git -C "$root" rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
      # The branch outlived its worktree: checked out as it is.
      add=("$dir" "$branch")
    else
      add=(-b "$branch" "$dir" "$base")
    fi
    if git -C "$root" worktree add --quiet --lock --reason "$reason" "${add[@]}" >&2; then
      printf '%s\n' "$dir"
      exit 0
    fi
  fi
done
echo "worktree-create: $dir kept changing under a concurrent prune" >&2
exit 1
