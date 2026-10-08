#!/usr/bin/env bash
# Create or update the dev and main rulesets of one repo from configs/rulesets/,
# matched by name. Usage: scripts/apply-rulesets.sh [--dry-run] <repo>. Refuses a
# repo that is not two-branch (exit 2). Applies main only when a job of ci.yaml on
# both branches calls a meta-workflow pin whose validate needs the pr-policy intake job, else
# exit 1: without the intake, main's ruleset would admit any head.
set -euo pipefail

OWNER=cplieger
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

dry_run=false
if [ "${1:-}" = --dry-run ]; then
  dry_run=true
  shift
fi
repo="${1:-}"
case "$repo" in
  '' | *[!A-Za-z0-9._-]*)
    echo "usage: $0 [--dry-run] <repo>   (a repository name matching [A-Za-z0-9._-]+)" >&2
    exit 2
    ;;
esac
if [ "$#" -ne 1 ]; then
  echo "usage: $0 [--dry-run] <repo>" >&2
  exit 2
fi

meta="$(gh api "repos/${OWNER}/${repo}")"
if ! python3 -c 'import json, sys; sys.path.insert(0, sys.argv[1]); import release_channels as rc; sys.exit(0 if rc.is_two_branch(json.loads(sys.stdin.read())) else 1)' \
  "$ROOT/scripts" <<<"$meta"; then
  echo "refusing ${OWNER}/${repo}: not a two-branch repository (want a public, non-fork, non-archived repo whose default branch is dev)" >&2
  exit 2
fi

raw() { # <path> <ref> -> the file's bytes at <ref>
  gh api -H 'Accept: application/vnd.github.raw+json' "repos/$1?ref=$2"
}

# stdin: a workflow. Prints each distinct pin of the meta workflow that a job's
# `uses:` names, one per line; exits non-zero when the workflow does not parse.
meta_pins() {
  python3 -c '
import re, sys, yaml
pin = re.compile(r"cplieger/ci/\.github/workflows/ci\.yaml@([0-9a-f]{40})")
try:
    jobs = (yaml.safe_load(sys.stdin) or {}).get("jobs") or {}
    uses = [job.get("uses") for job in jobs.values() if isinstance(job, dict)]
except (yaml.YAMLError, AttributeError):
    sys.exit(1)
found = {m[1] for u in uses if isinstance(u, str) and (m := pin.fullmatch(u))}
print("\n".join(sorted(found)))
'
}

# stdin: the meta workflow. Exits 0 when its `jobs` maps `pr-policy` to a job and
# `validate`, the ruleset's required context, needs it; 1 when not; 3 when it does
# not parse.
has_pr_policy() {
  python3 -c '
import sys, yaml
try:
    doc = yaml.safe_load(sys.stdin)
except yaml.YAMLError:
    sys.exit(3)
jobs = doc.get("jobs") if isinstance(doc, dict) else None
if not isinstance(jobs, dict):
    sys.exit(3)
validate = jobs.get("validate")
needs = validate.get("needs") if isinstance(validate, dict) else None
needs = [needs] if isinstance(needs, str) else needs if isinstance(needs, list) else []
sys.exit(0 if isinstance(jobs.get("pr-policy"), dict) and "pr-policy" in needs else 1)
'
}

# Prints the reason `main` must wait, or nothing when every branch's pin is ready.
main_blocker() {
  local base content pins pin rc
  for base in dev main; do
    if ! content="$(raw "${OWNER}/${repo}/contents/.github/workflows/ci.yaml" "$base")"; then
      echo "${base}: .github/workflows/ci.yaml unreadable"
      return
    fi
    if ! pins="$(meta_pins <<<"$content")"; then
      echo "${base}: .github/workflows/ci.yaml does not parse as a workflow"
      return
    fi
    if [ "$(grep -c . <<<"$pins")" -ne 1 ]; then
      echo "${base}: want exactly one cplieger/ci ci.yaml pin, found '${pins//$'\n'/ }'"
      return
    fi
    pin="$pins"
    if ! content="$(raw "${OWNER}/ci/contents/.github/workflows/ci.yaml" "$pin")"; then
      echo "${base}: cplieger/ci ci.yaml at ${pin} unreadable"
      return
    fi
    rc=0
    has_pr_policy <<<"$content" || rc=$?
    case "$rc" in
      0) ;;
      1)
        echo "${base}: pinned cplieger/ci ${pin} has no pr-policy job that validate needs"
        return
        ;;
      *)
        echo "${base}: pinned cplieger/ci ${pin} ci.yaml does not parse as a workflow"
        return
        ;;
    esac
  done
}

blocker="$(main_blocker)"
files=("$ROOT/configs/rulesets/dev.json")
if [ -z "$blocker" ]; then
  files+=("$ROOT/configs/rulesets/main.json")
  echo "main ruleset allowed: dev and main pin a cplieger/ci meta workflow whose validate needs pr-policy"
else
  echo "main ruleset refused: ${blocker}" >&2
fi

for file in "${files[@]}"; do
  name="$(jq -r .name "$file")"
  if [ "$dry_run" = true ]; then
    echo "would apply ${OWNER}/${repo} ruleset ${name}: $(jq -c . "$file")"
    continue
  fi
  id="$(gh api "repos/${OWNER}/${repo}/rulesets" --jq ".[] | select(.name == \"${name}\") | .id" | head -1)"
  if [ -n "$id" ]; then
    gh api -X PUT "repos/${OWNER}/${repo}/rulesets/${id}" --input "$file" >/dev/null
    echo "updated ${OWNER}/${repo} ruleset ${name} (id ${id})"
  else
    id="$(gh api -X POST "repos/${OWNER}/${repo}/rulesets" --input "$file" --jq .id)"
    echo "created ${OWNER}/${repo} ruleset ${name} (id ${id})"
  fi
done

[ -z "$blocker" ]
