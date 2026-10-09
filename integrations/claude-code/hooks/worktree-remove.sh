#!/bin/bash
# WorktreeRemove hook: the counterpart of worktree-create.sh.  Claude Code
# fires it when it removes a worktree that hook created - a `claude
# --worktree` session's on exit; not a finished subagent's, which
# `nixconvoy abandon` removes - and without it the worktree's branch
# would stay behind.  A worktree with changes, or commits not on
# origin/main (a work branch deployed but not squashed: nixconvoy's squash
# or abandon is its way out), is kept (exit 1 leaves it on disk, as
# documented).
set -eu
command -v jq >/dev/null 2>&1 || { echo "worktree-remove: jq missing" >&2; exit 1; }
path=$(jq -r '.worktree_path')
[ -d "$path" ] || exit 0
landed=refs/remotes/origin/main
git -C "$path" rev-parse -q --verify "$landed" >/dev/null 2>&1 || landed=--remotes
if [ -n "$(git -C "$path" status --porcelain)" ] \
   || [ "$(git -C "$path" rev-list --count HEAD --not "$landed")" != 0 ]; then
  echo "worktree-remove: $path has work not on origin/main, kept" >&2
  exit 1
fi
branch=$(git -C "$path" symbolic-ref --short -q HEAD || true)
top=$(dirname "$(dirname "$(dirname "$path")")")
git -C "$top" worktree unlock "$path" 2>/dev/null || true
git -C "$top" worktree remove --force "$path"
[ -z "$branch" ] || git -C "$top" branch -D "$branch" >&2 || true
