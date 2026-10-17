#!/bin/bash
# SessionStart hook: `nixconvoy hook session-start` (nixconvoy --help:
# Hook), detached, which removes the subagent worktrees this session's
# agents leave behind with nothing of their own - a sweep of dead sessions'
# now, one of this session's at its exit.  Claude Code never removes a
# worktree a WorktreeCreate hook made.  The nixconvoy of the package these
# hooks are in (bin/ four levels up from hooks/, reached through the
# symlink a repo may install the hook as) runs, PATH's when there is none.
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
"$nixconvoy" hook session-start >/dev/null 2>&1
exit 0
