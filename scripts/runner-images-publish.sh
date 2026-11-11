#!/usr/bin/env bash
# runner-images-publish.sh index <image> <digest dir> | tags <digest dir>, env PLAN and PACKAGE.
# index: the image's PLATFORM_COUNT per-platform digests as one index under its index_tag, whose
# digest it prints. tags: every image's version tag, then every moving tag, on the digest
# <digest dir>/<image> holds. Each tag is read back logged in, as GHCR makes a new package private.
set -euo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/retry.sh
. "$TOOLS/retry.sh"

USAGE='usage: runner-images-publish.sh index <image> <digest dir> | tags <digest dir>'
MODE=${1:?$USAGE}
: "${PLAN:?PLAN is required}" "${PACKAGE:?PACKAGE is required}"

fail() {
  echo "::error::$*" >&2
  exit 1
}
entry_of() { # <image> -> its plan entry
  jq -ce --arg n "$1" 'map(select(.name == $n)) | if length == 1 then .[0] else empty end' <<<"$PLAN" \
    || fail "the runner image plan holds no single entry named $1"
}
imagetools_create() { # <metadata file> <tag args and sources...>
  local meta=$1
  shift
  docker buildx imagetools create --metadata-file "$meta" "$@"
}
create() { # <tag args and sources...> -> the index digest imagetools wrote
  local meta digest
  meta=$(mktemp)
  retry imagetools_create "$meta" "$@" >&2 || {
    rm -f "$meta"
    return 1
  }
  digest=$(jq -r '."containerimage.descriptor".digest // empty' "$meta")
  rm -f "$meta"
  case "$digest" in sha256:*) printf '%s\n' "$digest" ;; *) return 1 ;; esac
}
# shellcheck disable=SC2329 # called through await_registry
serves() { # <ref> <digest>
  [ "sha256:$(docker buildx imagetools inspect --raw "$1" | sha256sum | cut -d' ' -f1)" = "$2" ]
}
read_back() { # <tag> <digest>
  await_registry "${PACKAGE}:$1" serves "${PACKAGE}:$1" "$2" >&2 \
    || fail "${PACKAGE}:$1 does not serve $2"
}

case "$MODE" in
  index)
    IMAGE=${2:?$USAGE}
    DIR=${3:?$USAGE}
    entry=$(entry_of "$IMAGE")
    tag=$(jq -er '.index_tag' <<<"$entry")
    src_args=()
    shopt -s nullglob
    for f in "$DIR/${IMAGE}".*; do
      src_args+=("${PACKAGE}@$(cat "$f")")
    done
    [ "${#src_args[@]}" -eq "${PLATFORM_COUNT:?PLATFORM_COUNT is required}" ] \
      || fail "${IMAGE} has ${#src_args[@]} platform digests in ${DIR}, and the build matrix has ${PLATFORM_COUNT}"
    digest=$(create -t "${PACKAGE}:${tag}" "${src_args[@]}") || fail "imagetools could not write ${PACKAGE}:${tag}"
    read_back "$tag" "$digest"
    printf '%s\n' "$digest"
    ;;
  tags)
    DIR=${2:?$USAGE}
    mapfile -t names < <(jq -r '.[].name' <<<"$PLAN")
    # Every digest is read before the first write, and every version tag is written and read back
    # before the first moving tag, so a missing digest or a failed version tag moves no moving tag.
    # A pass writes all its tags before its first read-back and zeroes the clock they share, which
    # await_registry allows only for what was published before the first check (retry.sh).
    declare -A digests=() pass=()
    for n in "${names[@]}"; do
      d=$(cat "$DIR/$n" 2>/dev/null) || d=""
      case "$d" in sha256:*) digests[$n]=$d ;; *) fail "no signed build of ${n} in ${DIR}, so no runner image is tagged" ;; esac
    done
    for field in version_tag moving_tag; do
      for n in "${names[@]}"; do
        tag=$(entry_of "$n" | jq -er --arg f "$field" '.[$f]')
        got=$(create -t "${PACKAGE}:${tag}" "${PACKAGE}@${digests[$n]}") || fail "imagetools could not write ${PACKAGE}:${tag}"
        [ "$got" = "${digests[$n]}" ] \
          || fail "tagging changed ${n}'s digest from ${digests[$n]} to ${got}, and a tagged image must be the build it signed"
        pass[$n]=$tag
      done
      REGISTRY_WAITED=0
      for n in "${names[@]}"; do
        read_back "${pass[$n]}" "${digests[$n]}"
        echo "${PACKAGE}:${pass[$n]} is ${digests[$n]}"
      done
    done
    ;;
  *) fail "$USAGE" ;;
esac
