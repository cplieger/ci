#!/usr/bin/env bash
# The image a stable release re-tags instead of building: promote-digest.sh <commit>
# Env IMAGE_NAME (<owner>/<name> on GHCR), RELEASE_MODEL (legacy|two-branch), REGISTRY,
# EXCLUDE_RE (path-significance.sh's exclude_re; empty accepts an exact sha-<commit> hit
# only); two-branch: SUBPACKAGES_JSON (required with EXCLUDE_RE), GITHUB_REPOSITORY, cosign.
# Prints promote_digest= (empty: build from source), promote_source=, promote_via=
# exact|ancestor|trailer|none. Exit 1 refuses a two-branch promotion commit whose
# Promoted-Digest this image's package lacks. --sources <commit> reads no registry: the
# commit, then each first-parent ancestor whose image it carries unchanged, nearest first.
set -euo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/reconciliation.sh
. "$TOOLS/reconciliation.sh"

die() {
  echo "::error::$*" >&2
  exit 1
}

MODEL=${RELEASE_MODEL:-legacy}
case "$MODEL" in legacy | two-branch) ;; *) die "RELEASE_MODEL must be legacy or two-branch, got '${MODEL}'" ;; esac
EXCLUDE_RE=${EXCLUDE_RE:-}
if [ "$MODEL" = two-branch ] && [ -n "$EXCLUDE_RE" ]; then
  jq -e 'type == "array" and all(.[]; type == "string")' <<<"${SUBPACKAGES_JSON:-}" >/dev/null 2>&1 \
    || die "SUBPACKAGES_JSON must be the JSON array of declared subpackage dirs, got '${SUBPACKAGES_JSON:-}'"
fi

# Each first-parent commit is judged on its own diff, not the endpoints': a shipped change
# a later commit reverts leaves the trees equal, yet the ancestor's image names the wrong
# revision. Two-branch asks path-significance.sh, so a change that owes any release (a
# subpackage manifest's included) builds that commit's image; legacy, whose release builds
# no image for a subpackage alone, keeps every package.json image-pure.
commit_is_pure() { # <commit> -> 0 when its own diff changes no shipped path, so it carries its parent's image
  local c=$1 changed sig
  if [ "$MODEL" = two-branch ] && [ -n "$(git rev-parse --verify --quiet "${c}^2")" ]; then
    echo "::notice::commit ${c} is a merge, and no digest is inherited across one, so the image is built from source" >&2
    return 1
  fi
  changed=$(git diff --name-only "${c}^" "$c")
  if [ -z "$changed" ]; then
    echo "::notice::commit ${c} changes no file (a forced rebuild); building from source" >&2
    return 1
  fi
  if [ "$MODEL" = two-branch ]; then
    sig=$(MODE=paths FROM="${c}^" TO="$c" REPO_TYPE=docker GO_LANES_JSON='[]' \
      bash "$TOOLS/path-significance.sh" 2>/dev/null | sed -n 's/^significant=//p') || sig=""
    [ "$sig" = '[]' ] && return 0
    echo "::notice::commit ${c} changes a shipped path; building from source" >&2
    return 1
  fi
  if printf '%s\n' "$changed" | grep -Eqv "${EXCLUDE_RE}|(^|/)package\.json$"; then
    echo "::notice::commit ${c} changes a shipped path; building from source" >&2
    return 1
  fi
}
walk_is_pure() { # <from> <to> -> 0 when no commit in from..to changes a shipped path
  local c
  while IFS= read -r c; do
    [ -z "$c" ] && continue
    commit_is_pure "$c" || return 1
  done < <(git rev-list --first-parent "$1..$2")
  return 0
}

if [ "${1:-}" = --sources ]; then
  c=$(git rev-parse --verify --quiet "${2:?usage: promote-digest.sh --sources <commit>}^{commit}") \
    || die "promote-digest: '$2' is not a commit"
  printf '%s\n' "$c"
  [ -n "$EXCLUDE_RE" ] || exit 0
  for _ in $(seq 200); do
    parent=$(git rev-parse --verify --quiet "${c}^1") || break
    commit_is_pure "$c" 2>/dev/null || break
    if [ "$MODEL" = two-branch ] && [ -n "$(git rev-parse --verify --quiet "${parent}^2")" ]; then
      break
    fi
    printf '%s\n' "$parent"
    c=$parent
  done
  exit 0
fi

: "${IMAGE_NAME:?IMAGE_NAME is required}"
REGISTRY=${REGISTRY:-ghcr.io}
COMMIT=$(git rev-parse --verify --quiet "${1:?usage: promote-digest.sh <commit>}^{commit}") \
  || die "promote-digest: '$1' is not a commit"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
curl -fsSL --connect-timeout 10 --max-time 30 --retry 3 --retry-all-errors \
  -o "$WORK/ghcr-token.json" "https://${REGISTRY}/token?scope=repository:${IMAGE_NAME}:pull"
token=$(jq -r .token "$WORK/ghcr-token.json")

emit() { # <digest> <source> <via>
  printf 'promote_digest=%s\npromote_source=%s\npromote_via=%s\n' "$1" "$2" "$3"
}
digest_of_sha() { # <commit> -> index digest of :sha-<commit>, or nothing
  curl -fsSI --connect-timeout 10 --max-time 30 -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://${REGISTRY}/v2/${IMAGE_NAME}/manifests/sha-$1" 2>/dev/null \
    | tr -d '\r' | awk 'tolower($1) == "docker-content-digest:" { print $2 }'
}

# A manifest is addressed within one repository, so a digest pushed to another
# package answers 404 here.
if [ "$MODEL" = two-branch ] && is_reconciliation "$COMMIT"; then
  DIGEST=$(promoted_digest "$COMMIT") \
    || die "promotion commit ${COMMIT} carries no single well-formed Promoted-Digest trailer for the image, and a promotion never builds from source"
  reply=$(curl -sSI --connect-timeout 10 --max-time 30 --retry 3 -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "https://${REGISTRY}/v2/${IMAGE_NAME}/manifests/${DIGEST}" 2>/dev/null | tr -d '\r') \
    || die "could not read ${REGISTRY}/${IMAGE_NAME}@${DIGEST}, and a promotion is never built from source"
  status=$(awk 'NR == 1 { print $2 }' <<<"$reply")
  served=$(awk 'tolower($1) == "docker-content-digest:" { print $2 }' <<<"$reply")
  case "$status" in
    200) [ "$served" = "$DIGEST" ] || die "${REGISTRY}/${IMAGE_NAME}@${DIGEST} answered digest '${served}'" ;;
    404) die "the promoted digest ${DIGEST} is not in ${REGISTRY}/${IMAGE_NAME}, because retention deleted it or it is another image's. A promotion never builds from source, so rebuild on dev and promote again." ;;
    *) die "${REGISTRY}/${IMAGE_NAME}@${DIGEST} answered HTTP '${status:-nothing}', and a promotion is never built from source" ;;
  esac
  emit "$DIGEST" "$(git rev-parse "${COMMIT}^2")" trailer
  exit 0
fi

DIGEST=$(digest_of_sha "$COMMIT" || true)
SOURCE="$COMMIT"
VIA=exact
[ -n "$DIGEST" ] || VIA=none
if [ -z "$DIGEST" ] && [ -n "$EXCLUDE_RE" ]; then
  while IFS= read -r c; do
    [ -z "$c" ] && continue
    d=$(digest_of_sha "$c" || true)
    [ -z "$d" ] && continue
    # A promotion's run pushes :sha-R at a dev build's digest, which no commit
    # on main signed, so two-branch never takes a merge's image.
    if [ "$MODEL" = two-branch ] && [ -n "$(git rev-parse --verify --quiet "${c}^2")" ]; then
      echo "::notice::the nearest image is the merge ${c}'s, and no digest is inherited across one, so the image is built from source" >&2
    elif walk_is_pure "$c" "$COMMIT"; then
      DIGEST="$d"
      SOURCE="$c"
      VIA=ancestor
    fi
    break
  done < <(git rev-list --first-parent --skip=1 --max-count=200 "$COMMIT")
fi
# A run can push sha-<commit> and die before signing; reusing that image would
# stall at finalize's provenance gate, so two-branch builds from source instead.
if [ "$MODEL" = two-branch ] && [ -n "$DIGEST" ]; then
  rc=0
  bash "$TOOLS/release-state.sh" complete "${REGISTRY}/${IMAGE_NAME}@${DIGEST}" "$SOURCE" >&2 || rc=$?
  case "$rc" in
    0) ;;
    3)
      echo "::notice::building ${COMMIT} from source, as the image of ${SOURCE} is incomplete" >&2
      DIGEST="" SOURCE="$COMMIT" VIA=none
      ;;
    *) die "cannot tell whether the image of ${SOURCE} at ${DIGEST} was signed and attested, so nothing is assumed" ;;
  esac
fi
emit "$DIGEST" "$SOURCE" "$VIA"
