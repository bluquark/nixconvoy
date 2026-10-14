#!/bin/bash
# WorktreeCreate hook. Claude Code's own logic puts an isolated subagent's
# worktree under the repository's primary worktree, where nixconvoy would
# take it for that checkout's own subagent.  This hands the creation to
# `nixconvoy open --lock <reason> <name>` instead: the worktree goes under
# the spawning checkout's own root (<checkout>/<worktrees>/<name>, the
# directory nixconvoy.toml names, owning <checkout's branch>-<name>) on
# the fresh branch worktree-<name>
# off origin's default branch, as Claude Code would (an existing worktree
# of that name is reused, as `claude --worktree <name>` expects), locked
# the way Claude Code locks one (the spawning claude process's pid and
# start time), which nixconvoy's status and close read.  The hook
# replaces Claude Code's logic entirely, so neither a finished subagent nor
# the periodic sweep removes these: subagent-stop.sh removes one holding
# nothing of its own when its agent stops (worktree-resume.sh recreates it
# through this hook for a resume), session-start.sh's `nixconvoy close
# --after` those left at the session's exit, and `nixconvoy close` the others
# (worktree-remove.sh covers the removals Claude Code does make, a `claude
# --worktree` session's).  hooks.json beside it wires the hooks up as the
# nixconvoy plugin.  The nixconvoy is the package these hooks ship in,
# else the one on PATH.
set -eu
command -v jq >/dev/null 2>&1 || { echo "worktree-create: jq missing" >&2; exit 1; }
INPUT=$(cat)
name=$(printf '%s' "$INPUT" | jq -r '.name')
cwd=$(printf '%s' "$INPUT" | jq -r '.cwd // ""')
root=$(git -C "${CLAUDE_PROJECT_DIR:-${cwd:-.}}" rev-parse --show-toplevel)

# The nearest claude ancestor: the session whose lifetime the lock spans.
# The worktree is never unlocked, so a concurrent close sweep, which
# removes only an unlocked worktree after claiming the lock it judged dead,
# never removes one handed out here: with no ancestor found the lock names
# no session, which the sweep keeps like a hand lock.
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
  # Then a token of this create, so a lock taken again (a reuse, a resume)
  # never reads as one a close judged before.
  reason="$reason) $(date +%s).$$"
else
  reason="claude agent $name (no claude session found)"
fi

nixconvoy=nixconvoy
# One readlink, as the other hooks: fully resolved, the path lands in the
# integrations' own store path, whose bin/ has no nixconvoy.
for hook in "${BASH_SOURCE[0]}" "$(readlink "${BASH_SOURCE[0]}" 2>/dev/null || true)"; do
  if [ -n "$hook" ] && [ -x "$(dirname "$hook")/../../../../bin/nixconvoy" ]; then
    nixconvoy=$(dirname "$hook")/../../../../bin/nixconvoy
    break
  fi
done
command -v "$nixconvoy" >/dev/null 2>&1 \
  || { echo "worktree-create: nixconvoy not found (not on PATH, nor beside these hooks)" >&2; exit 1; }
cd "$root"
# open prints the worktree's path alone on stdout.
exec "$nixconvoy" open --lock "$reason" "$name"
