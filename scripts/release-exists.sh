#!/usr/bin/env bash
# release-exists.sh <tag>: prints "present" or "absent" for GITHUB_REPOSITORY's
# published Release of <tag>, read over REST. Only an HTTP 404 reads as absent;
# any other failure prints the error on stderr and exits 1.
set -euo pipefail

# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

tag="${1:?usage: release-exists.sh TAG}"
err=$(mktemp)
trap 'rm -f "$err"' EXIT
fail() {
  gha_annotate error 'could not determine whether Release %s exists:' "${tag}" >&2
  cat "$err" >&2
  exit 1
}
if gh api "repos/${GITHUB_REPOSITORY:?}/releases/tags/${tag}" >/dev/null 2>"$err"; then
  echo present
  exit 0
fi
grep -q 'HTTP 404' "$err" || fail
echo absent
