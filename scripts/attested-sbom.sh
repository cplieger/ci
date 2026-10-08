#!/usr/bin/env bash
# attested-sbom.sh <image@digest> <tag> <out>: writes to <out> the SPDX document
# attested to the image by a docker-release.yaml run of GITHUB_REPOSITORY at a
# commit that could have published <tag> (release-state.sh signing-window, which
# reads EXCLUDE_RE and SUBPACKAGES_JSON). Exit 0 written; 3 nothing verifies as
# this repository's, or no SPDX document names the digest; 1 the window or
# cosign could not tell. On failure stdout carries the reason and <out> is
# left absent.
set -euo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/retry.sh
. "$TOOLS/retry.sh"

ref="${1:?usage: attested-sbom.sh IMAGE@DIGEST TAG OUT}"
tag="${2:?usage: attested-sbom.sh IMAGE@DIGEST TAG OUT}"
out="${3:?usage: attested-sbom.sh IMAGE@DIGEST TAG OUT}"
digest=${ref##*@}
verified=$(mktemp)
doc=$(mktemp)
trap 'rm -f "$verified" "$doc"' EXIT

# The document read is one cosign printed as verified, never a separate
# download. Every consumer signs under this workflow's identity, so the
# repository and a commit that could have published the image are what tie
# the attestation to this release.
verified_at() { # <commit>: fails only when cosign cannot tell, so a retry is for transport alone
  local err
  verdict=no
  if err=$(cosign verify-attestation --type spdxjson \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@' \
    --certificate-github-workflow-repository "${GITHUB_REPOSITORY:?}" --certificate-github-workflow-sha "$1" \
    "$ref" 2>&1 >"$verified"); then
    verdict=yes
    return 0
  fi
  case "$err" in *"no matching attestations"* | *"no matching signatures"* | *"no signatures found"*) return 0 ;; esac
  printf '%s\n' "$err" >&2
  return 1
}
if ! window=$(bash "$TOOLS/release-state.sh" signing-window "$tag"); then
  echo "the commits that could have published ${tag} are unknown"
  exit 1
fi
verdict=no
rc=3
while IFS= read -r c; do
  [ -n "$c" ] || continue
  if ! retry verified_at "$c" >&2; then
    rc=1
    break
  fi
  [ "$verdict" = no ] || break
done <<<"$window"
if [ "$verdict" != yes ]; then
  echo "the SBOM attestation of ${tag} on ${ref} does not verify as this repository's"
  exit "$rc"
fi
# Both envelope shapes (the sigstore bundle cosign v3 writes, and DSSE); the
# statement must name the digest that was verified.
sbom_of() {
  local p
  while IFS= read -r p; do
    if printf '%s' "$p" | base64 -d | jq -ce --arg d "${digest#sha256:}" \
      'select(.predicateType == "https://spdx.dev/Document" and any(.subject[]?; .digest.sha256 == $d)) | .predicate | objects'; then
      return 0
    fi
  done < <(jq -r '.dsseEnvelope.payload // .payload // empty' "$verified")
  return 1
}
if ! sbom_of >"$doc"; then
  echo "the verified SBOM attestation of ${tag} carries no SPDX document for ${digest}"
  exit 3
fi
mv "$doc" "$out"
