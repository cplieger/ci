#!/usr/bin/env bash
# release-exists.sh <tag>: prints "present" or "absent" for GITHUB_REPOSITORY's
# published Release of <tag>, read over REST. Only an HTTP 404 reads as absent;
# any other failure prints the error on stderr and exits 1.
set -euo pipefail

tag="${1:?usage: release-exists.sh TAG}"
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
echo absent
