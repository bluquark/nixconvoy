# A prompt segment for a checkout nixconvoy manages: the ref (a feature
# branch of this machine's by its suffix, <branch_prefix>3 -> 3, a
# subagent's 3-<name>, with COMMIT_MSG.md's subject - the readable name
# of its work), +N commits beyond origin/main, ", unstaged edits" and
# ", staged edits", ", N agents" for the subagent worktrees nested in the
# checkout that changed something (edits, or commits beyond origin/main;
# a fresh one does not count), ", submodule X" for each submodule
# worktree planted in the checkout (nixconvoy open --submodule) holding
# edits or commits beyond its upstream.  One git status call carries most
# of it (nixconvoy points every feature branch's upstream at main as fetched, so
# branch.ab is the count beyond main); only a detached HEAD, having no
# upstream, costs a count of its own.  The prefix comes from the
# nixconvoy/branch-prefix note `nixconvoy init` and every later command
# leave in the common git dir; without it, the branch shows in full.
# Elsewhere it falls back to __git_ps1 when that exists.
#   source prompt.bash; PS1='\h:$(__nixconvoy_ps1)\w\$ '
# The package installs it as share/nixconvoy/prompt.bash.
__nixconvoy_ps1() {
  local top common prefix='' main w n=0 agents='' submodules='' out line head='' oid='' ahead='' xy unstaged='' staged='' suffix subject
  { read -r top && read -r common; } < <(git rev-parse --path-format=absolute --show-toplevel --git-common-dir 2>/dev/null) \
    && [ -f "$top/nixconvoy.toml" ] \
    || { command -v __git_ps1 >/dev/null 2>&1 && __git_ps1 "(%s)"; return; }
  read -r prefix 2>/dev/null < "$common/nixconvoy/branch-prefix"
  read -r main 2>/dev/null < "$common/nixconvoy/main-ref" || main=origin/main
  for w in $(git worktree list --porcelain 2>/dev/null | sed -n "s|^worktree $top/||p"); do
    w=$top/$w
    [ -e "$w/.git" ] || continue
    if [ -n "$(git -C "$w" status --porcelain -uno 2>/dev/null | head -n 1)" ] \
       || [ "$(git -C "$w" rev-list --count "$main"..HEAD 2>/dev/null)" != 0 ]; then
      n=$((n + 1))
    fi
  done
  case $n in 0) ;; 1) agents=', 1 agent' ;; *) agents=", $n agents" ;; esac
  while IFS=$'\t' read -r _ w; do
    [ -e "$top/$w/.git" ] || continue
    if [ -n "$(git -C "$top/$w" status --porcelain -uno 2>/dev/null | head -n 1)" ] \
       || [ "$(git -C "$top/$w" rev-list --count '@{upstream}..HEAD' 2>/dev/null || echo 0)" != 0 ]; then
      submodules="$submodules, submodule ${w##*/}"
    fi
  done < <([ -f "$top/.gitmodules" ] && git -C "$top" config -f .gitmodules --get-regexp '\.path$' | awk '{print $2}' | xargs -r git -C "$top" ls-files -s -- 2>/dev/null | grep '^160000')
  out=$(git status --porcelain=v2 --branch -uno 2>/dev/null) || { printf '(?%s%s)' "$agents" "$submodules"; return; }
  while IFS= read -r line; do
    case "$line" in
      '# branch.head '*) head=${line#'# branch.head '} ;;
      '# branch.oid '*) oid=${line#'# branch.oid '} ;;
      '# branch.ab '*) ahead=${line#'# branch.ab +'}; ahead=${ahead%% *} ;;
      [12]' '*) xy=${line#? }; xy=${xy%% *}
        [ "${xy:1:1}" != . ] && unstaged=', unstaged edits'
        [ "${xy:0:1}" != . ] && staged=', staged edits' ;;
      'u '*) unstaged=', unstaged edits'; staged=', staged edits' ;;
    esac
  done <<< "$out"
  if [ "$head" = '(detached)' ]; then
    ahead=$(git rev-list --count "$main"..HEAD 2>/dev/null)
    if [ "$ahead" = 0 ] && [ "$oid" = "$(git rev-parse -q --verify "$main" 2>/dev/null)" ]; then
      head=$main
    else
      head="detached ${oid:0:7}"
    fi
  elif [ -z "$ahead" ]; then
    ahead=$(git rev-list --count "$main"..HEAD 2>/dev/null)
  fi
  [ "${ahead:-0}" = 0 ] && ahead='' || ahead=" +$ahead"
  suffix=$head
  [ -n "$prefix" ] && suffix=${head#"$prefix"}
  if [ "$suffix" != "$head" ] && IFS= read -r subject 2>/dev/null < "$top/COMMIT_MSG.md" \
     && [ -n "$subject" ]; then
    [ ${#subject} -gt 40 ] && subject="${subject:0:39}…"
    head="$suffix: $subject"
  else
    head=$suffix
  fi
  printf '(%s%s%s%s%s%s)' "$head" "$ahead" "$unstaged" "$staged" "$agents" "$submodules"
}
