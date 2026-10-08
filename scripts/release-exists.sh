#!/usr/bin/env bash
# release-exists.sh [--count-drafts] <tag>: prints "present" or "absent" for
# GITHUB_REPOSITORY's published Release of <tag>, read over REST. Only an HTTP
# 404 reads as absent; any other failure prints the error on stderr and exits
# 1. --count-drafts also reads a draft carrying <tag> as present.
set -euo pipefail

count_drafts=false
if [ "${1:-}" = --count-drafts ]; then
  count_drafts=true
  shift
fi
tag="${1:?usage: release-exists.sh [--count-drafts] TAG}"
err=$(mktemp)
trap 'rm -f "$err"' EXIT
fail() {
  echo "::error::could not determine whether Release ${tag} exists:" >&2
  cat "$err" >&2
  exit 1
}
if gh api "repos/${GITHUB_REPOSITORY:?}/releases/tags/${tag}" >/dev/null 2>"$err"; then
  echo present
  exit 0
fi
grep -q 'HTTP 404' "$err" || fail
if [ "$count_drafts" = true ]; then
  drafts=$(gh api --paginate "repos/${GITHUB_REPOSITORY}/releases?per_page=100" \
    --jq '.[] | select(.draft) | .tag_name' 2>"$err") || fail
  if grep -qxF -- "$tag" <<<"$drafts"; then
    echo present
    exit 0
  fi
fi
echo absent
