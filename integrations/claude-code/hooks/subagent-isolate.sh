#!/bin/bash
# PreToolUse hook (matcher: Agent|Task).  A subagent spawned inside a git
# repository gets a worktree of its own (isolation: worktree, unless the
# call chose otherwise), which worktree-create.sh then puts under the
# spawning checkout, so parallel agents never share one checkout's index,
# branch and deploy lock.  The repository is the session's project
# directory, not the shell's working directory, which drifts (an agent
# spawned after a cd into /tmp would otherwise get no worktree and make
# one by hand, unlocked and outside what Claude Code manages).  Prints
# nothing when the call stands as it is; without jq every call does.
set -u
command -v jq >/dev/null 2>&1 || exit 0
INPUT=$(cat)
[ "$(printf '%s' "$INPUT" | jq -r '.tool_input | has("isolation")')" = "true" ] && exit 0
cwd=$(printf '%s' "$INPUT" | jq -r '.cwd // ""')
git -C "${CLAUDE_PROJECT_DIR:-${cwd:-.}}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
printf '%s' "$INPUT" | jq -c '{hookSpecificOutput:{hookEventName:"PreToolUse",
  permissionDecision:"allow",
  permissionDecisionReason:"Subagent given its own worktree (isolation: worktree).",
  updatedInput:(.tool_input + {isolation:"worktree"})}}'
