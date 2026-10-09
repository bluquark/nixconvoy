#!/bin/bash
# WorktreeCreate hook. Claude Code's own logic puts an isolated subagent's
# worktree under the repository's primary worktree, where nixconvoy would
# take it for that checkout's own subagent.  This creates it under the
# spawning checkout's own root instead (<checkout>/.claude/worktrees/<name>,
# owning <checkout's branch>-<name>) on the fresh branch worktree-<name> off
# origin's default branch, as Claude Code would (an existing worktree of
# that name is reused, as `claude --worktree <name>` expects), and takes the
# same kind of lock Claude Code takes (the spawning claude process's pid and
# start time), which nixconvoy's status and abandon read.  The hook replaces
# Claude Code's logic entirely, so neither a finished subagent nor the
# periodic sweep removes these: `nixconvoy status` marks one with nothing of
# its own, `nixconvoy abandon <name>` removes it (worktree-remove.sh covers
# the removals Claude Code does make, a `claude --worktree` session's).
# Wire it up in the repo's .claude/settings.json (see the README).
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
if [ "$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" = "$dir" ]; then
  : # the worktree exists: reused (`claude --worktree <name>` again)
elif git -C "$root" rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
  # The branch outlived its worktree: checked out as it is.
  mkdir -p "$root/.claude/worktrees"
  git -C "$root" worktree add --quiet "$dir" "$branch" >&2
else
  mkdir -p "$root/.claude/worktrees"
  git -C "$root" worktree add --quiet -b "$branch" "$dir" "$base" >&2
fi

# The nearest claude ancestor: the session whose lifetime the lock spans.
# No lock when none is found, rather than one nobody's exit releases.
pid=$PPID
while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
  case "$(ps -o comm= -p "$pid" 2>/dev/null)" in *claude*) break ;; esac
  pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
done
if [ -n "$pid" ] && [ "$pid" -gt 1 ]; then
  git -C "$root" worktree unlock "$dir" >/dev/null 2>&1 || true  # a reused one's old lock
  reason="claude agent $name (pid $pid"
  if [ -r "/proc/$pid/stat" ]; then
    reason="$reason start $(sed 's/.*) //' "/proc/$pid/stat" | awk '{print $20}')"
  fi
  git -C "$root" worktree lock --reason "$reason)" "$dir" >&2 || true
fi
printf '%s\n' "$dir"
