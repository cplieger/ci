#!/usr/bin/env bash
# Env: DASHBOARD_PATH (the files, whitespace-separated), BASE_REF (empty: no
# base), EVENT_BASE_SHA (the pull request event's base SHA: fetch-base.sh
# reads that base at the merge commit's first parent), TOKEN (contents read),
# DASHBOARD_CHECK_TOOLS (tool dir, default $RUNNER_TEMP/dashboard-check).
# Checks every file, failing if any fails.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tools=${DASHBOARD_CHECK_TOOLS:-${RUNNER_TEMP:?}/dashboard-check}

bash "$here/install.sh" "$tools"

args=(--github --cue "$tools/cue")
for schema in "$tools"/schema-*.cue; do
  tag=${schema##*/schema-}
  args+=(--schema "${tag%.cue}=${schema}")
done

read -r -d '' -a paths <<<"${DASHBOARD_PATH:-}" || true
[ "${#paths[@]}" -gt 0 ] || {
  echo "::error::dashboard-check: no dashboard file given" >&2
  exit 2
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
rc=0
for path in "${paths[@]}"; do
  file_args=("${args[@]}")
  if ! bash "$here/fetch-base.sh" "$path" "${BASE_REF:-}" "$work/base.json"; then
    rc=1
    continue
  fi
  if [ -f "$work/base.json" ]; then
    file_args+=(--base-file "$work/base.json")
  fi
  python3 "$here/dashboard-check.py" "${file_args[@]}" "$path" || rc=1
done
exit "$rc"
