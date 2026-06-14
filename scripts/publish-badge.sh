#!/usr/bin/env bash
# publish-badge.sh — publish or update one shields `endpoint` JSON on a repo's
# orphan `badges` branch, PRESERVING any sibling badge files already there.
#
# Usage:
#   publish-badge.sh <owner/repo> <token> <filename> <json>
#
#   owner/repo  e.g. cplieger/atomicfile
#   token       a token with contents:write on <owner/repo>
#   filename    badge file to write, e.g. coverage.json | mutation.json
#   json        full one-line shields endpoint JSON for that badge
#
# Why this exists: the `badges` branch is machine-managed and squashed to a
# single commit (so it never accumulates history). The naive approach —
# `git init` a fresh tree with only your own file and force-push — clobbers
# every other badge on the branch. With coverage.json (pushed on every main
# push) and mutation.json (pushed weekly) both living there, that would leave
# one badge permanently missing. This script fetches the current branch first,
# carries every existing *.json forward, replaces/adds just your file, then
# force-pushes one fresh commit containing the union.
#
# Concurrency: coverage (frequent) and mutation (weekly) can in rare cases race
# and one push can drop the other's just-written file. This self-heals — the
# next coverage push re-adds coverage.json; the next weekly run re-adds
# mutation.json. The branch is disposable, so an occasional lost update is
# acceptable.
set -euo pipefail

REPO="${1:?owner/repo required}"
TOKEN="${2:?token required}"
FILE="${3:?badge filename required}"
JSON="${4:?badge json required}"

URL="https://x-access-token:${TOKEN}@github.com/${REPO}.git"

stage=$(mktemp -d)
prev=$(mktemp -d)

# Best-effort: pull the existing badge files so siblings survive the rewrite.
if git -C "$prev" init -q \
  && git -C "$prev" fetch -q --depth 1 "$URL" badges 2>/dev/null \
  && git -C "$prev" checkout -q FETCH_HEAD 2>/dev/null; then
  # Carry every existing badge JSON forward (ignore the .git dir).
  find "$prev" -maxdepth 1 -name '*.json' -exec cp {} "$stage/" \;
fi

# Add or replace just our file (single-line JSON; never a heredoc — heredoc
# terminators break under GHA run-step YAML dedent).
printf '%s\n' "$JSON" > "${stage}/${FILE}"

cd "$stage"
git init -q
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git add -A
git commit -qm "chore: update ${FILE}"
git push -q -f "$URL" HEAD:badges

echo "published ${FILE} to ${REPO}@badges ($(find . -maxdepth 1 -name '*.json' -printf '%f ' 2>/dev/null))"
