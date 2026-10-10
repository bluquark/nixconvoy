#!/bin/bash
# PreToolUse hook on SendMessage: resuming a subagent needs the worktree its
# transcript started in, which `nixconvoy prune --stopped` removes when the
# agent stops holding nothing of its own.  worktree-create.sh, beside this,
# recreates it under its name (on a fresh branch off origin's default
# branch) before the resume.
set -u
INPUT=$(cat)
to=$(printf '%s' "$INPUT" | jq -r '.tool_input.to // ""' 2>/dev/null)
[[ $to =~ ^a[0-9a-f]+$ ]] || exit 0  # an agent by id
transcript=$(printf '%s' "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null)
sub="${transcript%.jsonl}/subagents/agent-$to.jsonl"
[ -f "$sub" ] || exit 0
dir=$(head -n 50 "$sub" | jq -r 'select(.cwd != null) | .cwd' 2>/dev/null | head -n 1)
case "$dir" in */.claude/worktrees/agent-"$to") ;; *) exit 0 ;; esac
[ -d "$dir" ] && exit 0
root=${dir%/.claude/worktrees/*}
[ -f "$root/deploy-branches" ] || exit 0  # not a nixconvoy repository
create=
for hook in "$0" "$(readlink "$0" 2>/dev/null || true)"; do
  if [ -n "$hook" ] && [ -f "$(dirname "$hook")/worktree-create.sh" ]; then
    create=$(dirname "$hook")/worktree-create.sh
    break
  fi
done
[ -n "$create" ] || exit 0
jq -n --arg name "agent-$to" --arg cwd "$root" '{name: $name, cwd: $cwd}' \
  | CLAUDE_PROJECT_DIR="$root" bash "$create" >/dev/null \
  || echo "worktree-resume: could not recreate $dir for agent $to" >&2
exit 0
