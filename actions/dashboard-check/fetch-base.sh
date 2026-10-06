#!/usr/bin/env bash
# fetch-base.sh <path> <base-ref> <out-file>: write the base revision's copy of
# <path> to <out-file> through the contents API (env TOKEN, GITHUB_API_URL,
# GITHUB_REPOSITORY). Exit 0 with no <out-file> when there is no base: an
# empty or all-zero ref (a manual run, a new branch) or a file the base lacks.
# Any other answer exits 1, because the identity and regression checks would
# otherwise pass unchecked.
set -euo pipefail

[ "$#" -eq 3 ] || {
  echo "usage: fetch-base.sh <path> <base-ref> <out-file>" >&2
  exit 2
}
path=$1 ref=$2 out=$3
rm -f "$out"

if [ -z "$ref" ] || [ -z "${ref//0/}" ]; then
  echo "dashboard-check: no base revision; the identity and regression checks are skipped"
  exit 0
fi

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
# No --fail: a 404 is a complete answer. Without it curl retries transport
# errors and the transient HTTP codes (408, 429, 500, 502-504), never a 404.
code=$(curl -sS --connect-timeout 10 --max-time 60 \
  --retry 7 --retry-max-time 150 --retry-all-errors \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/vnd.github.raw+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -o "$tmp" -w '%{http_code}' \
  --get --data-urlencode "ref=${ref}" \
  "${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/contents/${path}") || code=000

case "$code" in
  200)
    mv "$tmp" "$out"
    echo "dashboard-check: base copy of ${path} read at ${ref}"
    ;;
  404) echo "dashboard-check: ${path} does not exist at ${ref}; no base copy" ;;
  *)
    echo "::error::dashboard-check: reading ${path} at ${ref} answered HTTP ${code}"
    exit 1
    ;;
esac
