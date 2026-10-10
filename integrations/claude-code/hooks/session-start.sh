#!/bin/bash
# SessionStart hook: Claude Code never removes a worktree a WorktreeCreate
# hook made (worktree-create.sh).  subagent-stop.sh removes an agent's when
# it stops; this starts `nixconvoy prune --after` on the session's claude
# process for those its stops left: detached, it prunes what dead sessions
# left, then, once this session exits, the worktrees its agents left with
# nothing of their own.  The nixconvoy of the package these hooks are in
# (bin/ four levels up from hooks/, reached through the symlink a repo may
# install the hook as) runs, PATH's when there is none.
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
cd "$root" && "$nixconvoy" prune --after "$pid" >/dev/null 2>&1 </dev/null
exit 0
