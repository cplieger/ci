#!/usr/bin/env bash
# runner-images-build.sh <platform> <arch name> <out dir>, env PLAN and PACKAGE: each image pushed
# by digest into <out dir>/<image>.<arch name>, a sibling it names swapped for this run's build.
set -euo pipefail

# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

PLATFORM=${1:?usage: runner-images-build.sh <platform> <arch name> <out dir>}
ARCH=${2:?usage: runner-images-build.sh <platform> <arch name> <out dir>}
OUT=${3:?usage: runner-images-build.sh <platform> <arch name> <out dir>}
: "${PLAN:?PLAN is required}" "${PACKAGE:?PACKAGE is required}"
mkdir -p "$OUT"
meta=$(mktemp)
trap 'rm -f "$meta"' EXIT

count=$(jq -er 'length' <<<"$PLAN")
for ((i = 0; i < count; i++)); do
  image=$(jq -c ".[$i]" <<<"$PLAN")
  name=$(jq -er '.name' <<<"$image")
  args=(--file "$(jq -er '.dockerfile' <<<"$image")" --platform "$PLATFORM" --provenance mode=max)
  args+=(--metadata-file "$meta")
  args+=(--output "type=image,name=${PACKAGE},push-by-digest=true,name-canonical=true,push=true")
  while IFS= read -r v; do args+=(--label "$v"); done < <(jq -r '.labels[]' <<<"$image")
  while IFS= read -r v; do args+=(--build-arg "$v"); done < <(jq -r '.build_args[]' <<<"$image")
  while IFS=' ' read -r ref parent; do
    [ -n "$ref" ] || continue
    if [ ! -s "$OUT/${parent}.${ARCH}" ]; then
      gha_annotate error '%s names %s, but %s has no %s build in this run' "${name}" "${ref}" "${parent}" "${ARCH}"
      exit 1
    fi
    args+=(--build-context "${ref}=docker-image://${PACKAGE}@$(cat "$OUT/${parent}.${ARCH}")")
  done < <(jq -r '.parents | to_entries[] | "\(.key) \(.value)"' <<<"$image")
  gha_annotate group '%s (%s)' "${name}" "${PLATFORM}"
  : >"$meta"
  docker buildx build "${args[@]}" .
  echo "::endgroup::"
  digest=$(jq -r '."containerimage.digest" // empty' "$meta")
  case "$digest" in
    sha256:*) printf '%s\n' "$digest" >"$OUT/${name}.${ARCH}" ;;
    *)
      gha_annotate error 'buildx reported no digest for %s (%s)' "${name}" "${PLATFORM}"
      exit 1
      ;;
  esac
  echo "${PACKAGE}@${digest} is ${name} for ${PLATFORM}"
done
