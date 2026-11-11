#!/usr/bin/env bash
# version-owner.sh <tag>, env GH_TOKEN, GITHUB_REPOSITORY, GITHUB_SHA: exit 1 when the tag names
# a commit this one does not descend from, so a rerun of an older run never republishes it.
set -euo pipefail

VERSION=${1:?usage: version-owner.sh <tag>}
if ! EXISTING=$(gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${VERSION}" --jq .object.sha 2>/dev/null); then
  echo "::notice::${VERSION} is not tagged yet; publishing."
  exit 0
fi
if [ "$EXISTING" = "$GITHUB_SHA" ]; then
  echo "::notice::${VERSION} already names this commit; publishing."
  exit 0
fi
STATUS=$(gh api "repos/${GITHUB_REPOSITORY}/compare/${GITHUB_SHA}...${EXISTING}" \
  --jq .status 2>/dev/null || echo "unknown")
if [ "$STATUS" = "behind" ]; then
  echo "::notice::${VERSION} names ${EXISTING}, an ancestor of ${GITHUB_SHA}; publishing forward."
  exit 0
fi
MSG="refusing to publish ${VERSION}: its tag names ${EXISTING} and this run built ${GITHUB_SHA}"
MSG="${MSG} (compare status: ${STATUS}). Publishing would overwrite the released image with an"
MSG="${MSG} older build and sign it at that version. The commit at ${EXISTING} already carries"
MSG="${MSG} this version, so a rerun has nothing to publish."
echo "::error::${MSG}"
exit 1
