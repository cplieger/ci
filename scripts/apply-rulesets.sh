#!/usr/bin/env bash
# Create or update the `dev` and `main` rulesets of one repository from the
# JSON bodies in configs/rulesets/, matched by ruleset name so a rerun updates
# in place. Usage: scripts/apply-rulesets.sh <repo>   (gh must be authenticated
# with a token that administers the repository). Nothing else is changed.
set -euo pipefail

OWNER=cplieger
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

repo="${1:-}"
case "$repo" in
  '' | *[!A-Za-z0-9._-]*)
    echo "usage: $0 <repo>   (a repository name matching [A-Za-z0-9._-]+)" >&2
    exit 2
    ;;
esac

for file in "$ROOT"/configs/rulesets/dev.json "$ROOT"/configs/rulesets/main.json; do
  name="$(jq -r .name "$file")"
  id="$(gh api "repos/${OWNER}/${repo}/rulesets" --jq ".[] | select(.name == \"${name}\") | .id" | head -1)"
  if [ -n "$id" ]; then
    gh api -X PUT "repos/${OWNER}/${repo}/rulesets/${id}" --input "$file" >/dev/null
    echo "updated ${OWNER}/${repo} ruleset ${name} (id ${id})"
  else
    id="$(gh api -X POST "repos/${OWNER}/${repo}/rulesets" --input "$file" --jq .id)"
    echo "created ${OWNER}/${repo} ruleset ${name} (id ${id})"
  fi
done
