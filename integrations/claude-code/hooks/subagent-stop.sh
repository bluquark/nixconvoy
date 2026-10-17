#!/bin/bash
# SubagentStop hook: a subagent's run ended - for good, or until a resume or
# its own background work wakes it.  `nixconvoy hook subagent-stop`
# (nixconvoy --help: Hook), detached, removes the agent's worktree when it
# holds nothing of its own and no process or other running agent works in
# it; worktree-resume.sh recreates it for a resume.  The nixconvoy resolved
# as session-start.sh resolves it.
set -u
# Not a nixconvoy repository: nothing to do, settled before Python starts.
top=$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -f "$top/deploy-branches" ] || exit 0
nixconvoy=nixconvoy
for hook in "$0" "$(readlink "$0" 2>/dev/null || true)"; do
  if [ -n "$hook" ] && [ -x "$(dirname "$hook")/../../../../bin/nixconvoy" ]; then
    nixconvoy=$(dirname "$hook")/../../../../bin/nixconvoy
    break
  fi
done
command -v "$nixconvoy" >/dev/null 2>&1 || exit 0
"$nixconvoy" hook subagent-stop >/dev/null 2>&1
exit 0
