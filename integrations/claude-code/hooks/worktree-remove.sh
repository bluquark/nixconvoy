#!/bin/bash
# WorktreeRemove hook: the counterpart of worktree-create.sh.  Claude Code
# fires it when it removes a worktree that hook created - a `claude
# --worktree` session's on exit; not a finished subagent's, which
# `nixconvoy close` removes when it stops or its session exits - and without it the
# worktree's branch would stay behind.  A worktree with changes, or commits not on
# origin/main (a work branch deployed but not landed: nixconvoy's land
# or unapply is its way out), is kept (exit 1 leaves it on disk, as
# documented), as is one another live claude session has taken over, or
# one holding a submodule worktree (a gitlink whose directory nixconvoy
# planted a worktree in: its close judges that and removes both in order).
set -eu
command -v jq >/dev/null 2>&1 || { echo "worktree-remove: jq missing" >&2; exit 1; }
path=$(jq -r '.worktree_path')
# Resolved, as the create hook names the lock after git's resolved path.
path=$(cd -P "$path" 2>/dev/null && pwd -P) || exit 0
# The checkout it is nested in.
top=$(git -C "$(dirname "$path")" rev-parse --show-toplevel 2>/dev/null) || exit 0
# The name lock worktree-create.sh and nixconvoy's close take, so neither
# meets this removal half done; everything is checked under it.
locks="$(git -C "$top" rev-parse --path-format=absolute --git-common-dir)/nixconvoy-worktree-locks"
mkdir -p "$locks"
exec 9>>"$locks/${path//\//%}.lock"
if command -v flock >/dev/null 2>&1; then
  locked_name() { flock -w 60 9; }
else
  locked_name() {
    perl -MFcntl=:flock -e 'open(my $f, ">&=", 9) or exit 1; $SIG{ALRM} = sub { exit 1 }; alarm 60; flock($f, LOCK_EX) or exit 1'
  }
fi
if ! locked_name; then
  echo "worktree-remove: $path stayed locked for 60s by a nixconvoy close or a create, kept" >&2
  exit 1
fi
[ -d "$path" ] || exit 0  # removed meanwhile
# The session exiting is the nearest claude ancestor; a lock naming another
# live one is a reuse (`claude --worktree <name>` in another session).
me=$PPID
while [ -n "$me" ] && [ "$me" -gt 1 ]; do
  case "$(ps -o comm= -p "$me" 2>/dev/null)" in *claude*) break ;; esac
  me=$(ps -o ppid= -p "$me" 2>/dev/null | tr -d ' ')
done
locked=$(git -C "$path" rev-parse --path-format=absolute --git-path locked)
session=$({ grep -oE '(^|[^[:alnum:]_])pid [0-9]+( start [0-9]+)?' "$locked" 2>/dev/null || true; } | head -n 1)
holder=$(printf '%s' "$session" | sed -n 's/.*pid \([0-9][0-9]*\).*/\1/p')
start=$(printf '%s' "$session" | sed -n 's/.* start \([0-9][0-9]*\)$/\1/p')
# A start time the pid's process does not have: the pid reused.
if [ -n "$start" ] && [ -r "/proc/$holder/stat" ] \
   && [ "$(sed 's/.*) //' "/proc/$holder/stat" | awk '{print $20}')" != "$start" ]; then
  holder=
fi
if [ -n "$holder" ] && [ "$holder" != "$me" ] && kill -0 "$holder" 2>/dev/null; then
  echo "worktree-remove: $path is another claude session's now (pid $holder), kept" >&2
  exit 1
fi
landed=refs/remotes/origin/main
git -C "$path" rev-parse -q --verify "$landed" >/dev/null 2>&1 || landed=--remotes
if [ -n "$(git -C "$path" status --porcelain)" ] \
   || [ "$(git -C "$path" rev-list --count HEAD --not "$landed")" != 0 ]; then
  echo "worktree-remove: $path has work not on origin/main, kept" >&2
  exit 1
fi
while IFS=$'\t' read -r meta sub; do
  case "$meta" in 160000*) ;; *) continue ;; esac
  [ -e "$path/$sub/.git" ] || continue
  echo "worktree-remove: $path holds a submodule worktree ($sub): left to nixconvoy close" >&2
  exit 1
done < <(git -C "$path" ls-files -s)
branch=$(git -C "$path" symbolic-ref --short -q HEAD || true)
git -C "$top" worktree unlock "$path" 2>/dev/null || true
git -C "$top" worktree remove --force "$path"
[ -z "$branch" ] || git -C "$top" branch -D "$branch" >&2 || true
