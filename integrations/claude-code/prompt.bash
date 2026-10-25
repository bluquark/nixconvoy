# A prompt segment for a checkout nixconvoy manages: the ref, +N commits
# beyond origin/main, ", unstaged edits" and ", staged edits", ", N agents"
# for the subagent worktrees under the checkout's .claude/worktrees/ that
# changed something (edits, or commits beyond origin/main).  One git status
# call carries all of it (nixconvoy points every work branch's upstream at
# origin/main, so branch.ab is the count beyond main); only a detached HEAD,
# having no upstream, costs a count of its own.  Elsewhere it falls back to
# __git_ps1 when that exists.
#   source prompt.bash; PS1='\h:$(__nixconvoy_ps1)\w\$ '
__nixconvoy_ps1() {
  local top w n=0 agents='' out line head='' oid='' ahead='' xy unstaged='' staged=''
  top=$(git rev-parse --show-toplevel 2>/dev/null) && [ -f "$top/nixconvoy.toml" ] \
    || { command -v __git_ps1 >/dev/null 2>&1 && __git_ps1 "(%s)"; return; }
  for w in "$top"/.claude/worktrees/*/; do
    [ -e "$w.git" ] || continue
    if [ -n "$(git -C "$w" status --porcelain -uno 2>/dev/null | head -n 1)" ] \
       || [ "$(git -C "$w" rev-list --count origin/main..HEAD 2>/dev/null)" != 0 ]; then
      n=$((n + 1))
    fi
  done
  case $n in 0) ;; 1) agents=', 1 agent' ;; *) agents=", $n agents" ;; esac
  out=$(git status --porcelain=v2 --branch -uno 2>/dev/null) || { printf '(?%s)' "$agents"; return; }
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
    ahead=$(git rev-list --count origin/main..HEAD 2>/dev/null)
    if [ "$ahead" = 0 ] && [ "$oid" = "$(git rev-parse -q --verify origin/main 2>/dev/null)" ]; then
      head='origin/main'
    else
      head="detached ${oid:0:7}"
    fi
  elif [ -z "$ahead" ]; then
    ahead=$(git rev-list --count origin/main..HEAD 2>/dev/null)
  fi
  [ "${ahead:-0}" = 0 ] && ahead='' || ahead=" +$ahead"
  printf '(%s%s%s%s%s)' "$head" "$ahead" "$unstaged" "$staged" "$agents"
}
