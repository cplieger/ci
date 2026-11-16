#!/usr/bin/env bash
# go-modules.sh list <rev>: every dir below the root with a tracked go.mod at
#   <rev>, less those under node_modules, vendor, testdata, static or dist.
# go-modules.sh boundaries <to> [<from>] (env REPO_TYPE docker|go|ts|none|empty,
#   GO_LANES_JSON): every lane, plus every other nested module its container
#   does not ship at <to> and, given a <from>, at <from> too. A path belongs to
#   its nearest ancestor here, else the root.
# Both print sorted dirs, one per line; exit 2 on an unreadable rev or input.
set -euo pipefail

die() {
  echo "::error::go-modules: $*" >&2
  exit 2
}
usage="usage: go-modules.sh list <rev> | boundaries <to> [<from>]"
[ "$#" -ge 2 ] || die "$usage"
cmd=$1 to=$2 from=${3:-}
case "$cmd" in
  list) [ "$#" -eq 2 ] || die "$usage" ;;
  boundaries) [ "$#" -le 3 ] || die "$usage" ;;
  *) die "unknown command '${cmd}'" ;;
esac

tree_of() { # <rev> -> every tracked path at <rev>
  git ls-tree -r --name-only "$1" || die "cannot list the tree of $1"
}
modules_of() { # <paths> -> the dirs holding a go.mod among them
  # A committed node_modules or vendor tree carries upstream go.mod files.
  grep -E '/go\.mod$' <<<"$1" | grep -Ev '(^|/)(node_modules|vendor|testdata|static|dist)/' \
    | sed 's|/go\.mod$||' | LC_ALL=C sort -u || true
}
if [ "$cmd" = list ]; then
  FILES=$(tree_of "$to") || exit 2
  modules_of "$FILES" | sed '/^$/d'
  exit 0
fi

LANES=$(jq -r 'if type == "array" and all(.[]; type == "string") then .[] else error("not an array of strings") end' \
  <<<"${GO_LANES_JSON:-[]}") || die "GO_LANES_JSON must be a JSON array of dirs, got '${GO_LANES_JSON:-}'"
REPO_TYPE=${REPO_TYPE:-}
case "$REPO_TYPE" in
  "" | docker | go | ts | none) ;;
  *) die "REPO_TYPE must be docker, go, ts, none or empty, got '${REPO_TYPE}'" ;;
esac

lane_holding() { # <dir> -> the lane <dir> sits strictly inside; status 1 for none
  local l
  while IFS= read -r l; do
    if [ -n "$l" ] && [[ $1 == "$l/"* ]]; then
      printf '%s' "$l"
      return 0
    fi
  done <<<"$LANES"
  return 1
}
nested_at() { # <rev> -> every nested module at <rev> that is no lane and that its container does not ship
  local files modules packages m l p skip
  files=$(tree_of "$1") || exit 2
  modules=$(modules_of "$files")
  packages=$(grep -E '^[^/]+/jsr\.json$' <<<"$files" | sed 's|/jsr\.json$||' || true)
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    ! grep -qxF -- "$m" <<<"$LANES" || continue
    # A go.mod in a TS package (<lane>/web, or a root subpackage outside every
    # lane) only keeps Go tooling out: the package ships its tree.
    if l=$(lane_holding "$m"); then
      [[ $m != "$l/web" && $m != "$l/web/"* ]] || ! grep -qxF -- "$l/web/jsr.json" <<<"$files" \
        || continue
      printf '%s\n' "$m"
      continue
    fi
    skip=false
    while IFS= read -r p; do
      [[ -z $p || ($m != "$p" && $m != "$p/"*) ]] || skip=true
    done <<<"$packages"
    # A lane is a module zip, which leaves out every nested module. So is a
    # go root; a docker or ts root builds its artifact from the whole tree.
    if ! $skip && { [ "$REPO_TYPE" = go ] || [ "$REPO_TYPE" = none ]; }; then
      printf '%s\n' "$m"
    fi
  done <<<"$modules"
}

NESTED=$(nested_at "$to" | LC_ALL=C sort -u) || exit 2
if [ -n "$from" ]; then
  # A module the range carves out of its container was the container's at the
  # start, so the range's changes under it are the container's.
  AT_FROM=$(nested_at "$from" | LC_ALL=C sort -u) || exit 2
  NESTED=$(LC_ALL=C comm -12 <(printf '%s\n' "$NESTED") <(printf '%s\n' "$AT_FROM"))
fi
printf '%s\n%s\n' "$LANES" "$NESTED" | sed '/^$/d' | LC_ALL=C sort -u
