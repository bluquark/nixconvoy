#!/bin/bash
# The plugin's hooks, as hooks.json wires them: `nixconvoy hook <event>`
# (nixconvoy --help: Hook) with the hook's input on stdin, nixconvoy on
# PATH.  The prunes (session-start, subagent-stop) and worktree-resume
# have nothing to do outside a nixconvoy repository, settled before Python
# starts; the prunes detach and say nothing.
set -u
case "${1:-}" in
  session-start | subagent-stop | worktree-resume)
    top=$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel 2>/dev/null) || exit 0
    [ -f "$top/nixconvoy.toml" ] || exit 0
    ;;
esac
case "${1:-}" in
  session-start | subagent-stop)
    command -v nixconvoy >/dev/null 2>&1 || exit 0
    exec nixconvoy hook "$1" >/dev/null 2>&1
    ;;
esac
if ! command -v nixconvoy >/dev/null 2>&1; then
  echo "nixconvoy hook ${1:-}: nixconvoy is not on PATH" >&2
  # A resume's PreToolUse hook blocks only with 2.
  [ "${1:-}" = worktree-resume ] && exit 2
  exit 1
fi
exec nixconvoy hook "$1"
