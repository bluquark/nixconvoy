#!/bin/bash
# SubagentStop hook: Claude Code never removes a worktree a WorktreeCreate
# hook made (worktree-create.sh), so a finished subagent's would outlive it
# until someone ran abandon.  This prunes the finished agent's worktree -
# <checkout>/.claude/worktrees/agent-<agent_id> - the moment it stops:
# `nixconvoy prune` removes it when it holds nothing of its own and keeps
# it, saying why, when it does (deployed: land or abandon; uncommitted
# work).  Resuming the agent recreates an empty one through the create
# hook.  The nixconvoy of the package these hooks are in (bin/ four levels
# up from hooks/, reached through the symlink a repo may install the hook
# as - one level, since the package is a symlink join) runs, PATH's when
# there is none.  Never exit 2: that would keep the agent running.
set -u
command -v jq >/dev/null 2>&1 || exit 0
INPUT=$(cat)
id=$(printf '%s' "$INPUT" | jq -r '.agent_id // ""')
[ -n "$id" ] || exit 0
cwd=$(printf '%s' "$INPUT" | jq -r '.cwd // ""')
root=$(git -C "${CLAUDE_PROJECT_DIR:-${cwd:-.}}" rev-parse --show-toplevel 2>/dev/null) || exit 0
case "$root" in */.claude/worktrees/*) root=${root%/.claude/worktrees/*} ;; esac
name="agent-${id#agent-}"
[ -d "$root/.claude/worktrees/$name" ] || exit 0
nixconvoy=nixconvoy
for hook in "$0" "$(readlink "$0" 2>/dev/null || true)"; do
  if [ -n "$hook" ] && [ -x "$(dirname "$hook")/../../../../bin/nixconvoy" ]; then
    nixconvoy=$(dirname "$hook")/../../../../bin/nixconvoy
    break
  fi
done
cd "$root" && "$nixconvoy" prune "$name" >&2 || exit 1
