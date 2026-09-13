#!/usr/bin/env bash
#
# Print the repo names present in a directory of downloaded workflow artifacts,
# one per line, read from the meta.json each artifact carries.
#
# Usage: discover-artifact-repos.sh <artifacts-dir>
#
# Exit 0 with names on stdout, or exit 0 silently when nothing was downloaded at
# all. Exit 3 when artifacts ARE present but no repo can be read from any of
# them, because that is a defect and the caller must go red rather than quietly
# process zero repos.
#
# WHY THIS IS NOT A GLOB IN THE CALLER. Every caller used to discover repos from
# the per-artifact DIRECTORY, either by globbing `<name>-*/meta.json` or by
# parsing the directory name. actions/download-artifact only creates that
# directory when MORE THAN ONE artifact matched its pattern — its own docs say a
# single matched artifact is "extracted directly to the specified path" — so a run
# with exactly one matching artifact extracts flat and every one of those
# discoveries found nothing. It found nothing SILENTLY: no tracker issue updated,
# no badge published, no warning, job green. Measured 2026-09-13 on a single-repo
# weekly-stryker dispatch, which published a correct 95.5% badge from the run job
# and left the tracker untouched from the aggregate job.
#
# So depth is not something a caller may assume, and the artifact's identity has
# to live in its payload rather than in its name. Probed by
# scripts/test-discover-artifact-repos.sh.
set -uo pipefail

dir="${1:-}"
if [ -z "$dir" ]; then
  echo "usage: $(basename -- "$0") <artifacts-dir>" >&2
  exit 2
fi

metas=$(find "$dir" -name meta.json 2>/dev/null | sort || true)

# One jq per file, deliberately. Handing jq the whole list is shorter and
# order-dependently wrong: jq aborts on the first file it cannot parse, so a
# single malformed meta.json suppresses every repo whose path sorts after it.
# Measured while writing the probe (a 'bad' directory sorting before a 'good'
# one lost the good repo entirely), which would have been a worse silent skip
# than the one this script exists to fix.
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
  printf '%s\n' "$repos"
  exit 0
fi

# Nothing downloaded is legitimate: every matrix entry is allowed to fail, and
# each one's own red is the signal. Files present with no repo readable from them
# is the layout defect above.
if [ -n "$(find "$dir" -type f 2>/dev/null | head -1)" ]; then
  echo "::error::artifacts were downloaded to '${dir}' but no repo could be read from any meta.json" >&2
  find "$dir" 2>/dev/null | head -20 >&2
  exit 3
fi
exit 0
