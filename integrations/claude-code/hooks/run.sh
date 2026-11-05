#!/bin/bash
# The plugin's hooks, as hooks.json wires them: `nixconvoy hook <event>`
# with the hook's input on stdin, nixconvoy on PATH - in a nixconvoy
# repository alone (nixconvoy.toml at the root of
# the session's project directory, else of the input's cwd; of the
# worktree's own repository for a remove).  Anywhere else (the plugin
# enabled at user scope, say) nixconvoy never runs and the repository is
# left as Claude Code alone would leave it: the prunes and the resume say
# nothing, while the create and the remove, which Claude Code hands to a
# configured hook entirely, do here what its own git does - the worktree
# at .claude/worktrees/<name> on worktree-<name> off origin's default
# branch (the directory excluded, as Claude Code's own is), removed along
# with that branch, outright: Claude Code looks for work in it and asks
# before this hook runs.
set -u
event=${1:-}
input=$(cat)
if ! command -v jq >/dev/null 2>&1; then
  echo "nixconvoy hook $event: jq is not on PATH" >&2
  # A resume's PreToolUse hook blocks only with 2.
  [ "$event" = worktree-resume ] && exit 2
  exit 1
fi
field() { printf '%s' "$input" | jq -r ".$1 // \"\"" 2>/dev/null; }

case "$event" in
  worktree-remove)
    path=$(field worktree_path)
    [ -n "$path" ] && [ -d "$path" ] || exit 0
    top=$(git -C "$path/.." rev-parse --show-toplevel 2>/dev/null) || exit 0
    ;;
  *)
    where=${CLAUDE_PROJECT_DIR:-$(field cwd)}
    top=$(git -C "${where:-.}" rev-parse --show-toplevel 2>/dev/null) || top=
    ;;
esac

if [ -n "$top" ] && [ -f "$top/nixconvoy.toml" ]; then
  if ! command -v nixconvoy >/dev/null 2>&1; then
    case "$event" in
      session-start | subagent-stop) exit 0 ;;
    esac
    echo "nixconvoy hook $event: nixconvoy is not on PATH" >&2
    # A resume's PreToolUse hook blocks only with 2.
    [ "$event" = worktree-resume ] && exit 2
    exit 1
  fi
  case "$event" in
    session-start | subagent-stop)
      printf '%s' "$input" | nixconvoy hook "$event" >/dev/null 2>&1
      exit 0
      ;;
  esac
  printf '%s' "$input" | nixconvoy hook "$event"
  exit $?
fi

case "$event" in
  worktree-create)
    [ -n "$top" ] || { echo "hook worktree-create: ${where:-.} is not in a git repository" >&2; exit 1; }
    name=$(field name)
    case "$name" in
      '' | *[!A-Za-z0-9._-]*)
        echo "hook worktree-create: a worktree name holds letters, digits, '.', '-' and '_' only, not '$name'" >&2
        exit 1 ;;
    esac
    dir=$top/.claude/worktrees
    d=$dir/$name
    git -C "$top" worktree prune
    if [ ! -e "$d/.git" ]; then
      branch=worktree-$name
      if git -C "$top" show-ref --verify --quiet "refs/heads/$branch"; then
        add=("$d" "$branch")
      else
        base=HEAD
        for r in refs/remotes/origin/HEAD refs/remotes/origin/main; do
          if git -C "$top" rev-parse -q --verify "$r" >/dev/null 2>&1; then base=$r; break; fi
        done
        add=(-b "$branch" "$d" "$base")
      fi
      mkdir -p "$dir"
      git -C "$top" worktree add --quiet "${add[@]}" >&2 || exit 1
    fi
    if ! git -C "$top" check-ignore -q --no-index "$d"; then
      exclude=$(git -C "$top" rev-parse --path-format=absolute --git-path info/exclude)
      mkdir -p "$(dirname "$exclude")"
      [ -s "$exclude" ] && [ -n "$(tail -c 1 "$exclude")" ] && echo >> "$exclude"
      echo "/.claude/worktrees/" >> "$exclude"
    fi
    echo "$d"
    ;;
  worktree-remove)
    branch=$(git -C "$path" symbolic-ref --quiet --short HEAD 2>/dev/null) || branch=
    git -C "$top" worktree unlock "$path" >/dev/null 2>&1
    git -C "$top" worktree remove --force "$path" >&2 || exit 1
    if [ "$branch" = "worktree-$(basename "$path")" ]; then
      git -C "$top" branch -D "$branch" >/dev/null 2>&1
    fi
    ;;
esac
exit 0
