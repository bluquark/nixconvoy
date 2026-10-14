#!/bin/bash
# PreToolUse hook on SendMessage: resuming a subagent needs the worktree its
# transcript started in, which `nixconvoy close --stopped` removes when the
# agent stops holding nothing of its own.  worktree-create.sh, beside this,
# recreates it under its name (on a fresh branch off origin's default
# branch) before the resume, or relocks it when it is still there, so a
# close judging the stop afterwards sees a lock taken since and keeps it.
# The resume is refused (exit 2) when that fails.
set -u
INPUT=$(cat)
to=$(printf '%s' "$INPUT" | jq -r '.tool_input.to // ""' 2>/dev/null)
[[ $to =~ ^a[0-9a-f]+$ ]] || exit 0  # an agent by id
transcript=$(printf '%s' "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null)
sub="${transcript%.jsonl}/subagents/agent-$to.jsonl"
[ -f "$sub" ] || exit 0
dir=$(grep '"cwd"' "$sub" | head -n 20 | jq -r 'select(type == "object") | .cwd // empty' 2>/dev/null | head -n 1)
case "$dir" in */agent-"$to") ;; *) exit 0 ;; esac
# The checkout the worktree is (or was) nested in: the repository found
# above its directory, which the removal may have taken with it.
up=$(dirname "$dir")
while [ ! -d "$up" ] && [ "$up" != / ]; do up=$(dirname "$up"); done
root=$(git -C "$up" rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -f "$root/deploy-branches" ] || exit 0  # not a nixconvoy repository
create=
for hook in "$0" "$(readlink "$0" 2>/dev/null || true)"; do
  if [ -n "$hook" ] && [ -f "$(dirname "$hook")/worktree-create.sh" ]; then
    create=$(dirname "$hook")/worktree-create.sh
    break
  fi
done
[ -n "$create" ] || exit 0
if ! jq -n --arg name "agent-$to" --arg cwd "$root" '{name: $name, cwd: $cwd}' \
     | CLAUDE_PROJECT_DIR="$root" bash "$create" >/dev/null; then
  echo "worktree-resume: could not recreate or relock $dir, so agent $to is not resumed; retry" >&2
  exit 2
fi
exit 0
