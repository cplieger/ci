#!/usr/bin/env bash
# Usage: go-units.sh <tree> [<regex>]    (probed by scripts/test_go_units.py)
# Prints each Go module of a checkout, root first, then nested ones in byte
# order: <dir> TAB <slug> TAB <count>. A root go.mod is always a unit, and a
# nested one when its dir holds a .go file, outside node_modules, vendor,
# testdata, static and dist (ci.yaml's rule). <slug> is the dir with `/` as `_`,
# `root` for `.`. <count> is the lines of the module's own *_test.go files
# matching the ERE <regex>, else 0. An unsafe dir, or one whose slug is taken,
# is skipped with a ::warning::.
set -euo pipefail

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || {
  echo "usage: $(basename -- "$0") <tree> [<regex>]" >&2
  exit 2
}
tree=${1%/}
regex=${2:-}
[ -d "$tree" ] || {
  echo "go-units: $tree is not a directory" >&2
  exit 2
}

pruned=(-name .git -o -name node_modules -o -name vendor -o -name testdata -o -name static -o -name dist)

mods=()
[ -f "$tree/go.mod" ] && mods+=(.)
# NUL-separated, so a dir name holding a newline stays one name and fails the
# character check instead of splitting into names that pass it.
while IFS= read -r -d '' f; do
  d=$(dirname -- "${f#"$tree"/}")
  case "$d" in
    *[!A-Za-z0-9._/-]*)
      echo "::warning::go-units: skipped module dir '$d', which holds a character outside A-Z, a-z, 0-9, '.', '_', '-' and '/'" >&2
      continue
      ;;
  esac
  case "/$d/" in
    */../* | */./*)
      echo "::warning::go-units: skipped module dir '$d', which holds a '.' or '..' segment" >&2
      continue
      ;;
  esac
  if [ -z "$(find "$tree/$d" -name '*.go' -type f -print -quit)" ]; then
    continue
  fi
  mods+=("$d")
done < <(find "$tree" \( "${pruned[@]}" \) -prune -o -name go.mod -type f ! -path "$tree/go.mod" -print0 | LC_ALL=C sort -z)

declare -A taken=()
for d in "${mods[@]}"; do
  if [ "$d" = . ]; then slug=root; else slug=${d//\//_}; fi
  if [ -n "${taken[$slug]:-}" ]; then
    echo "::warning::go-units: skipped module dir '$d', because module dir '${taken[$slug]}' already has slug '$slug'" >&2
    continue
  fi
  taken[$slug]=$d

  count=0
  if [ -n "$regex" ]; then
    # Prune every other module under this one, so its tests count only there.
    inner=()
    for o in "${mods[@]}"; do
      [ "$o" = "$d" ] && continue
      if [ "$d" = . ] || [[ $o == "$d"/* ]]; then
        inner+=(-o -path "$tree/$o")
      fi
    done
    start=$tree
    [ "$d" = . ] || start=$tree/$d
    count=$(find "$start" \( "${pruned[@]}" ${inner[@]+"${inner[@]}"} \) -prune -o -name '*_test.go' -type f -print0 \
      | xargs -0 -r grep -hE -- "$regex" 2>/dev/null | wc -l | tr -d ' ' || true)
  fi
  printf '%s\t%s\t%s\n' "$d" "$slug" "${count:-0}"
done
