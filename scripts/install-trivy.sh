#!/usr/bin/env bash
# Installs Trivy TRIVY_VERSION, cosign-verified, on GITHUB_PATH for scan steps
# that set skip-setup-trivy: trivy-action's installer fetches from get.trivy.dev,
# down since 2026-11-03 (https://github.com/aquasecurity/setup-trivy/issues/42).
set -euo pipefail

: "${TRIVY_VERSION:?TRIVY_VERSION is required}" "${RUNNER_TEMP:?RUNNER_TEMP is required}"
tgz="trivy_${TRIVY_VERSION#v}_Linux-64bit.tar.gz"
base="https://github.com/aquasecurity/trivy/releases/download/${TRIVY_VERSION}"
for f in "$tgz" "$tgz.sigstore.json"; do
  curl -fsSL --connect-timeout 10 --max-time 120 --retry 7 --retry-max-time 150 --retry-all-errors \
    -o "$RUNNER_TEMP/$f" "$base/$f"
done
cosign verify-blob --bundle "$RUNNER_TEMP/$tgz.sigstore.json" \
  --certificate-identity "https://github.com/aquasecurity/trivy/.github/workflows/reusable-release.yaml@refs/tags/${TRIVY_VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com "$RUNNER_TEMP/$tgz"
mkdir -p "$RUNNER_TEMP/trivy-bin"
tar xzf "$RUNNER_TEMP/$tgz" -C "$RUNNER_TEMP/trivy-bin" trivy
echo "$RUNNER_TEMP/trivy-bin" >>"${GITHUB_PATH:?GITHUB_PATH is required}"
