#!/usr/bin/env bash
# Env: DASHBOARD_PATH (the file), BASE_REF (empty: no base), TOKEN (contents
# read), DASHBOARD_CHECK_TOOLS (tool dir, default $RUNNER_TEMP/dashboard-check).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tools=${DASHBOARD_CHECK_TOOLS:-${RUNNER_TEMP:?}/dashboard-check}

bash "$here/install.sh" "$tools"

args=(--github --cue "$tools/cue")
for schema in "$tools"/schema-*.cue; do
  tag=${schema##*/schema-}
  args+=(--schema "${tag%.cue}=${schema}")
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
bash "$here/fetch-base.sh" "$DASHBOARD_PATH" "${BASE_REF:-}" "$work/base.json"
if [ -f "$work/base.json" ]; then
  args+=(--base-file "$work/base.json")
fi

python3 "$here/dashboard-check.py" "${args[@]}" "$DASHBOARD_PATH"
