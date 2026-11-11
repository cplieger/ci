#!/usr/bin/env bash
# Usage: discover-artifact-repos.sh [--allow '<repo> ...'] <artifacts-dir>
# Prints, one per line, the repos named by the meta.json of each downloaded
# artifact (download-artifact extracts a single match with no directory, so the
# name is never read from a path). --allow keeps only the listed repos and warns
# on others; an empty list keeps none. Exit 0, silent when nothing was
# downloaded; exit 3 when artifacts are present but name no repo, which the
# caller goes red on. Probed by scripts/test-discover-artifact-repos.sh.
set -uo pipefail

usage() {
  echo "usage: $(basename -- "$0") [--allow '<repo> ...'] <artifacts-dir>" >&2
  exit 2
}

filter=0
allowed=""
if [ "${1:-}" = --allow ]; then
  [ "$#" -ge 2 ] || usage
  filter=1
  allowed=" $2 "
  shift 2
fi
dir="${1:-}"
[ -n "$dir" ] || usage

metas=$(find "$dir" -name meta.json 2>/dev/null | sort || true)

# One jq per file: jq aborts on the first file it cannot parse, so handing it
# the whole list loses every repo whose meta.json sorts after a malformed one.
repos=""
while IFS= read -r meta; do
  [ -n "$meta" ] || continue
  repo=$(jq -r '.repo // empty' "$meta" 2>/dev/null) || repo=""
  if [ -z "$repo" ]; then
    echo "::warning::no repo name readable in '${meta}'; that artifact is skipped" >&2
    continue
  fi
  repos="${repos}${repo}"$'\n'
done < <(printf '%s\n' "$metas")

repos=$(printf '%s' "$repos" | grep -v '^$' | sort -u || true)

if [ -n "$repos" ]; then
  if [ "$filter" -eq 1 ]; then
    kept=""
    while IFS= read -r repo; do
      case "$allowed" in
        *" $repo "*) kept="${kept}${repo}"$'\n' ;;
        *) echo "::warning::an artifact names '${repo}', which this run did not schedule, so it is skipped" >&2 ;;
      esac
    done <<<"$repos"
    printf '%s' "$kept"
  else
    printf '%s\n' "$repos"
  fi
  exit 0
fi

# Nothing downloaded is legitimate: every matrix entry is allowed to fail, and
# each one's own red is the signal.
if [ -n "$(find "$dir" -type f 2>/dev/null | head -1)" ]; then
  echo "::error::artifacts were downloaded to '${dir}' but no repo could be read from any meta.json" >&2
  find "$dir" 2>/dev/null | head -20 >&2
  exit 3
fi
exit 0
