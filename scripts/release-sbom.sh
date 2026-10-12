#!/usr/bin/env bash
# release-sbom.sh <tag> <out>: the attested SBOM of ${REGISTRY}/${IMAGE_NAME}:<tag>, through
# attested-sbom.sh (env GITHUB_REPOSITORY, EXCLUDE_RE, SUBPACKAGES_JSON; cosign on PATH).
# Exit 0: stdout holds the <registry>/<image>@<digest> read and <out> the document.
# Exit 1 (the tag resolves to no digest) or attested-sbom.sh's 1 or 3: stdout holds the
# one-line reason and <out> is absent. Exit 2: usage.
set -euo pipefail

[ "$#" -eq 2 ] || {
  echo "usage: release-sbom.sh TAG OUT" >&2
  exit 2
}
tag=$1 out=$2
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${REGISTRY:-}" ] || [ -z "${IMAGE_NAME:-}" ]; then
  echo "release-sbom.sh: REGISTRY and IMAGE_NAME are required" >&2
  exit 2
fi

if ! token=$(curl -fsSL --connect-timeout 10 --max-time 30 --retry 3 --retry-all-errors \
  "https://${REGISTRY}/token?scope=repository:${IMAGE_NAME}:pull" | jq -r .token); then
  token=""
fi
digest=$(curl -fsSI --connect-timeout 10 --max-time 30 -H "Authorization: Bearer $token" \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
  "https://${REGISTRY}/v2/${IMAGE_NAME}/manifests/${tag}" 2>/dev/null \
  | tr -d '\r' | awk 'tolower($1) == "docker-content-digest:" { print $2 }' || true)
if [ -z "$digest" ]; then
  echo "${REGISTRY}/${IMAGE_NAME}:${tag} is not readable"
  exit 1
fi
ref="${REGISTRY}/${IMAGE_NAME}@${digest}"
rc=0
bash "$TOOLS/attested-sbom.sh" "$ref" "$tag" "$out" || rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
printf '%s\n' "$ref"
