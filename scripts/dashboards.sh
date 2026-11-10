#!/usr/bin/env bash
# The Grafana dashboards a repository bundles: the root grafana-dashboard.json,
# published as <image>/dashboard, and each root grafana-dashboard-<suffix>.json,
# published as <image>/dashboard-<suffix>. Every mode fails naming a refused suffix.
#   list                 the files the working directory holds, grafana-dashboard.json first, then byte order
#   list-tree <tree-ish> the same for the tree's root; reads git in the working directory
#   filter               the same for the names on stdin, each once
#   package <file>       the file's package under the image: dashboard or dashboard-<suffix>
set -euo pipefail
export LC_ALL=C

# Keeps dashboard-<suffix> a valid OCI repository path component:
# https://github.com/opencontainers/distribution-spec/blob/main/spec.md#pulling-manifests
SUFFIX_RE='^[a-z0-9]+(-[a-z0-9]+)*$'

# A workflow command's data encoding: https://github.com/actions/toolkit/blob/main/packages/core/src/command.ts
die() {
  local msg=$*
  msg=${msg//\%/%25}
  msg=${msg//$'\r'/%0D}
  msg=${msg//$'\n'/%0A}
  printf '%s\n' "::error::${msg}" >&2
  exit 1
}

package_of() { # <file name>
  local suffix
  if [ "$1" = grafana-dashboard.json ]; then
    echo dashboard
    return
  fi
  suffix=${1#grafana-dashboard-}
  suffix=${suffix%.json}
  [[ $suffix =~ $SUFFIX_RE ]] \
    || die "$1 cannot be published: the part after grafana-dashboard- must be lowercase letters and digits, in runs joined by single hyphens, because it names the package <image>/dashboard-<suffix>."
  echo "dashboard-${suffix}"
}

# Takes raw names as arguments and refuses before printing anything, so a name
# holding a newline is refused whole and no caller reads a partial list.
select_dashboards() { # <name>...
  local name main=0 others=()
  for name; do
    case "$name" in
      grafana-dashboard.json) main=1 ;;
      grafana-dashboard-*.json)
        package_of "$name" >/dev/null
        others+=("$name")
        ;;
    esac
  done
  [ "$main" -eq 0 ] || echo grafana-dashboard.json
  [ "${#others[@]}" -eq 0 ] || printf '%s\n' "${others[@]}" | sort -u
}

cmd_filter() {
  local names
  mapfile -t names
  select_dashboards "${names[@]}"
}

cmd_list() {
  local f names=()
  shopt -s nullglob
  for f in grafana-dashboard.json grafana-dashboard-*.json; do
    [ ! -f "$f" ] || names+=("$f")
  done
  select_dashboards "${names[@]}"
}

cmd_list_tree() { # <tree-ish>
  local records record names=()
  # -z, because the text form quotes unusual names (core.quotePath) and they would go
  # unrefused; wait "$!" reads git's status, which the process substitution hides.
  mapfile -d '' records < <(git ls-tree -z --full-tree "$1")
  wait "$!" || die "git cannot list the files at the root of $1"
  for record in "${records[@]}"; do
    case "${record%%$'\t'*}" in
      *' blob '*) names+=("${record#*$'\t'}") ;;
    esac
  done
  select_dashboards "${names[@]}"
}

case "${1:-}" in
  list) cmd_list ;;
  list-tree) cmd_list_tree "${2:?usage: dashboards.sh list-tree TREE-ISH}" ;;
  filter) cmd_filter ;;
  package) package_of "${2:?usage: dashboards.sh package FILE}" ;;
  *) die "usage: dashboards.sh list|list-tree|filter|package ..." ;;
esac
