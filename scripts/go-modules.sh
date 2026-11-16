#!/usr/bin/env bash
# go-modules.sh list <rev>: every dir below the root with a tracked go.mod at
#   <rev>, less vendored and generated trees.
# go-modules.sh boundaries <rev> (env REPO_TYPE docker|go|ts|none|empty,
#   GO_LANES_JSON): every lane, plus every other nested module its container
#   does not ship. A path belongs to its nearest ancestor here, else the root.
# Both print sorted dirs, one per line; exit 2 on an unreadable <rev> or input.
set -euo pipefail

die() {
  echo "::error::go-modules: $*" >&2
  exit 2
}
[ "$#" -eq 2 ] || die "usage: go-modules.sh list|boundaries <rev>"
cmd=$1 rev=$2
case "$cmd" in list | boundaries) ;; *) die "unknown command '${cmd}'" ;; esac

FILES=$(git ls-tree -r --name-only "$rev") || die "cannot list the tree of ${rev}"
# A committed node_modules or vendor tree carries upstream go.mod files.
MODULES=$(grep -E '/go\.mod$' <<<"$FILES" | grep -Ev '(^|/)(node_modules|vendor|testdata|static|dist)/' \
  | sed 's|/go\.mod$||' | LC_ALL=C sort -u || true)
if [ "$cmd" = list ]; then
  printf '%s\n' "$MODULES" | sed '/^$/d'
  exit 0
fi

LANES=$(jq -r 'if type == "array" and all(.[]; type == "string") then .[] else error("not an array of strings") end' \
  <<<"${GO_LANES_JSON:-[]}") || die "GO_LANES_JSON must be a JSON array of dirs, got '${GO_LANES_JSON:-}'"
REPO_TYPE=${REPO_TYPE:-}
case "$REPO_TYPE" in
  "" | docker | go | ts | none) ;;
  *) die "REPO_TYPE must be docker, go, ts, none or empty, got '${REPO_TYPE}'" ;;
esac
# A go.mod in a TS package (a root subpackage or <lane>/web, found as release.yaml
# finds them) only keeps Go tooling out: the package ships its tree.
PACKAGES=$(grep -E '^[^/]+/jsr\.json$' <<<"$FILES" | sed 's|/jsr\.json$||' || true)
while IFS= read -r l; do
  if [ -n "$l" ] && grep -qxF -- "$l/web/jsr.json" <<<"$FILES"; then
    PACKAGES+=$'\n'"$l/web"
  fi
done <<<"$LANES"

under() { # <dir> <list> -> 0 when <dir> is an entry of <list> or below one
  local e
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    [[ $1 != "$e" && $1 != "$e/"* ]] || return 0
  done <<<"$2"
  return 1
}
{
  printf '%s\n' "$LANES"
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    ! grep -qxF -- "$m" <<<"$LANES" || continue
    ! under "$m" "$PACKAGES" || continue
    # A lane is a module zip, which leaves out every nested module. So is a
    # go root; a docker or ts root builds its artifact from the whole tree.
    if under "${m%/*}" "$LANES" || [ "$REPO_TYPE" = go ] || [ "$REPO_TYPE" = none ]; then
      printf '%s\n' "$m"
    fi
  done <<<"$MODULES"
} | sed '/^$/d' | LC_ALL=C sort -u
