#!/usr/bin/env bash
# publish-badge.sh <owner/repo> <token> <filename> <json>: publishes or updates one
# shields `endpoint` JSON on a repo's orphan `badges` branch, keeping the sibling
# badge files already there.
#   owner/repo  e.g. cplieger/atomicfile
#   token       a token with contents:write on <owner/repo>
#   filename    badge file to write, e.g. size.json | mutation.json | mutation-ts.json
#   json        full one-line shields endpoint JSON for that badge
set -euo pipefail

REPO="${1:?owner/repo required}"
TOKEN="${2:?token required}"
FILE="${3:?badge filename required}"
JSON="${4:?badge json required}"

URL="https://x-access-token:${TOKEN}@github.com/${REPO}.git"
BRANCH=refs/heads/badges
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# The branch is one squashed commit that several writers share: size.json on
# every published image (docker-release.yaml), mutation.json and mutation-ts.json
# weekly (weekly-gremlins.yaml, weekly-stryker.yaml). Each attempt rebuilds that
# commit from the tip it fetched and pushes it with a lease on that tip, so a
# writer that moved the branch in between makes the push fail rather than lose
# its file, and the next attempt carries that file forward.
publish_once() {
  local work expect="" rc=0 blob tree commit
  work=$(mktemp -d "$WORK/attempt.XXXXXX") || return 1
  git -C "$work" init -q || return 1
  if git -C "$work" fetch -q --depth 1 "$URL" "$BRANCH" 2>/dev/null; then
    expect=$(git -C "$work" rev-parse FETCH_HEAD) || return 1
  else
    # Exit 2 is ls-remote's "no such ref"; anything else is a failed read.
    git ls-remote --exit-code "$URL" "$BRANCH" >/dev/null || rc=$?
    [ "$rc" -eq 2 ] || return 1
  fi
  blob=$(printf '%s\n' "$JSON" | git -C "$work" hash-object -w --stdin) || return 1
  tree=$(siblings "$work" "$expect" "$blob" | git -C "$work" mktree -z) || return 1
  commit=$(git -C "$work" commit-tree "$tree" -m "chore: update ${FILE}") || return 1
  # An empty lease value requires the branch to be absent, so two first
  # writers cannot both create it (git-push(1), --force-with-lease).
  git -C "$work" push -q --force-with-lease="${BRANCH}:${expect}" "$URL" "${commit}:${BRANCH}" || return 1
  PUBLISHED=$(git -C "$work" ls-tree --name-only "$tree" | tr '\n' ' ')
}
siblings() { # <work> <tip, or empty> <our blob> -> NUL-separated mktree entries: the tip's top-level *.json but ours, then ours
  local entry name
  if [ -n "$2" ]; then
    while IFS= read -r -d '' entry; do
      name=${entry#*$'\t'}
      [[ $entry == *" blob "* && $name == *.json && $name != "$FILE" ]] || continue
      printf '%s\0' "$entry"
    done < <(git -C "$1" ls-tree -z "$2")
  fi
  printf '100644 blob %s\t%s\0' "$3" "$FILE"
}

export GIT_AUTHOR_NAME='github-actions[bot]' GIT_COMMITTER_NAME='github-actions[bot]'
export GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com'
export GIT_COMMITTER_EMAIL=$GIT_AUTHOR_EMAIL
attempt=1
max_attempts=5
PUBLISHED=""
until publish_once; do
  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "push to ${REPO}@badges failed after ${attempt} attempts" >&2
    exit 1
  fi
  echo "push to ${REPO}@badges failed or the branch moved (attempt ${attempt}/${max_attempts}), so it is read again" >&2
  sleep $((attempt * 3))
  attempt=$((attempt + 1))
done

echo "published ${FILE} to ${REPO}@badges (${PUBLISHED})"
