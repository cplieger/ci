#!/usr/bin/env bash
# Usage: discover-artifact-repos.sh [--allow '<repo> ...'] <artifacts-dir>
# Prints, one per line, the repos named by the meta.json of each downloaded
# artifact (download-artifact extracts a single match with no directory, so the
# name is never read from a path). --allow keeps only the listed repos and warns
# on others; an empty list keeps none. Exit 0, silent when nothing was
# downloaded; exit 3 when artifacts are present but name no repo, which the
# caller goes red on. Probed by scripts/test-discover-artifact-repos.sh.
set -uo pipefail
# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

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

# One jq per file: jq aborts on the first file it cannot parse, so handing it
# the whole list loses every repo whose meta.json sorts after a malformed one.
repos=""
# NUL-delimited, because target code names the files and a name can hold a newline.
while IFS= read -r -d '' meta; do
  # Target code wrote meta.json, so a name outside GitHub's repository characters names no repo.
  repo=$(jq -r '.repo | select(type == "string" and test("^[A-Za-z0-9._-]+$"))' "$meta" 2>/dev/null) || repo=""
  if [ -z "$repo" ]; then
    gha_annotate warning "no repo name readable in '%s'; that artifact is skipped" "$meta" >&2
    continue
  fi
  repos="${repos}${repo}"$'\n'
done < <(find "$dir" -name meta.json -print0 2>/dev/null | sort -z)

repos=$(printf '%s' "$repos" | grep -v '^$' | sort -u || true)

if [ -n "$repos" ]; then
  if [ "$filter" -eq 1 ]; then
    kept=""
    while IFS= read -r repo; do
      case "$allowed" in
        *" $repo "*) kept="${kept}${repo}"$'\n' ;;
        *) gha_annotate warning "an artifact names '%s', which this run did not schedule, so it is skipped" "$repo" >&2 ;;
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
  gha_annotate error "artifacts were downloaded to '%s' but no repo could be read from any meta.json" "$dir" >&2
  find "$dir" -print0 2>/dev/null | head -z -n 20 | while IFS= read -r -d '' f; do gha_say '%s' "$f" >&2; done
  exit 3
fi
exit 0
