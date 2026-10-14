#!/bin/bash
# SubagentStop hook: a subagent's run ended - for good, or until a resume or
# its own background work wakes it.  `nixconvoy close --stopped`, detached,
# removes the agent's worktree when it holds nothing of its own and no
# process or other running agent works in it; worktree-resume.sh recreates
# it for a resume.  The nixconvoy resolved as session-start.sh resolves it.
set -u
INPUT=$(cat)
cwd=$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)
root=$(git -C "${CLAUDE_PROJECT_DIR:-${cwd:-.}}" rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -f "$root/deploy-branches" ] || exit 0  # not a nixconvoy repository
pid=$PPID
while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
  case "$(ps -o comm= -p "$pid" 2>/dev/null)" in *claude*) break ;; esac
  pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
done
[ -n "$pid" ] && [ "$pid" -gt 1 ] || exit 0
nixconvoy=nixconvoy
for hook in "$0" "$(readlink "$0" 2>/dev/null || true)"; do
  if [ -n "$hook" ] && [ -x "$(dirname "$hook")/../../../../bin/nixconvoy" ]; then
    nixconvoy=$(dirname "$hook")/../../../../bin/nixconvoy
    break
  fi
done
command -v "$nixconvoy" >/dev/null 2>&1 || exit 0
cd "$root" && printf '%s' "$INPUT" | "$nixconvoy" close --stopped "$pid" >/dev/null 2>&1
exit 0
