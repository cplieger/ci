#!/usr/bin/env bash
#
# Run gremlins' own test suite with every scripts/gremlins-*.patch applied to the
# release mutation-gremlins-repo.yaml pins. The weekly run applies the same patches but
# never runs the tests they carry, and a version bump that breaks a patch shows a
# red check on its PR instead of first failing on Sunday.
#
# Run: bash scripts/test-gremlins-patches.sh    (needs git, go and network; exit 0 = pass)
set -euo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
version=$(grep -oP '^\s*GREMLINS_VERSION: \K\S+$' "${HERE}/../.github/workflows/mutation-gremlins-repo.yaml")
src=$(mktemp -d)
trap 'rm -rf "${src}"' EXIT

# shellcheck source=scripts/retry.sh
. "${HERE}/retry.sh"

clone_gremlins() {
  rm -rf "${src}"
  timeout 120 git -c advice.detachedHead=false clone --quiet --depth 1 --branch "${version}" \
    https://github.com/go-gremlins/gremlins "${src}"
}

retry clone_gremlins
for patch in "${HERE}"/gremlins-*.patch; do
  printf 'applying %s to gremlins %s\n' "${patch##*/}" "${version}"
  if ! git -C "${src}" apply "${patch}"; then
    echo "::error::scripts/${patch##*/} no longer applies to gremlins ${version}. If that release fixes the upstream issue listed for this patch in scripts/gremlins-run.sh, delete the patch. Otherwise rebase the patch onto the release."
    exit 1
  fi
done
cd "${src}"
retry timeout 300 go mod download
go vet ./...
go test -count=1 ./...
