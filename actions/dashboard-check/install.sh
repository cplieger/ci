#!/usr/bin/env bash
# Install the pinned cue binary, Grafana's dashboard schemas and the promql-check
# tool into <dest-dir>: <dest-dir>/cue, one <dest-dir>/schema-<grafana tag>.cue per
# pin below, and <dest-dir>/promql-check built from ./promql-check with the pinned
# Go toolchain. Files that already match their pin are kept, so a rerun downloads
# and builds nothing. install.sh --key prints the key of everything it installs,
# for the action's cache.
set -euo pipefail

# shellcheck source=SCRIPTDIR/../../scripts/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/../../scripts/gha-log.sh"

[ "$#" -eq 1 ] || {
  echo "usage: install.sh <dest-dir> | --key" >&2
  exit 2
}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# No Renovate manager reads this file: move CUE_VERSION and both sha256s
# together, by hand.
CUE_VERSION=v0.17.1
CUE_SHA256_AMD64=a39b0c97695069d95d276d99be0f5dbabb081d801bfdc9ba49b76efaf94e2369
CUE_SHA256_ARM64=0d729be30d52c952ca38fc9dcb692caa09d8463fa0b64df5781312779183fbcd

# The toolchain that builds promql-check, so the build is the same whatever Go
# the caller's job has, or none. Move GO_VERSION, both sha256s (go.dev/dl) and
# promql-check/go.mod's go line together, by hand: GOTOOLCHAIN=local refuses a
# go.mod that asks for a newer Go than this one.
GO_VERSION=go1.27.4
GO_SHA256_AMD64=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
GO_SHA256_ARM64=3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92

# grafana/grafana's apps/dashboard tree is AGPL-3.0-only, so the schema is
# fetched by commit and digest instead of being vendored into this repo.
# Grafana 13.2.0 through 13.2.3 ship this file byte for byte.
SCHEMAS=(
  "v13.2.3 6193dc03311b631b9727b560d24369e683dc396e d3f5115a32ce587abb84e276cdc57b8db4615d7fff0b7830232fdc45ff8d1e35"
)

# The stamp names the toolchain and every file the binary is built from, so a
# kept binary is one built from this exact source.
src=$here/promql-check
inputs=(go.mod go.sum)
for file in "$src"/*.go; do
  case "$file" in
    *_test.go) ;;
    *) inputs+=("${file##*/}") ;;
  esac
done
stamp=$({
  echo "$GO_VERSION"
  (cd "$src" && sha256sum "${inputs[@]}")
} | sha256sum)
stamp=${stamp%% *}

# The cue and schema pins are this file's text, and the build is the stamp.
if [ "$1" = --key ]; then
  key=$({
    sha256sum <"$here/install.sh"
    echo "$stamp"
  } | sha256sum)
  echo "${key%% *}"
  exit 0
fi
DEST=$1

fetch() {
  curl -fsSL --connect-timeout 10 --max-time 300 \
    --retry 7 --retry-max-time 150 --retry-all-errors \
    -o "$2" "$1"
}

matches() {
  [ -f "$2" ] && echo "$1  $2" | sha256sum -c --status -
}

case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 cue_sha=$CUE_SHA256_AMD64 go_sha=$GO_SHA256_AMD64 ;;
  aarch64 | arm64) arch=arm64 cue_sha=$CUE_SHA256_ARM64 go_sha=$GO_SHA256_ARM64 ;;
  *)
    gha_annotate error 'dashboard-check: no cue or Go build pinned for %s' "$(uname -m)" >&2
    exit 1
    ;;
esac

mkdir -p "$DEST"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Read the whole output first: grep -q exits at its match, and under pipefail
# the SIGPIPE that cue then gets would read as a version mismatch.
cue_have=$("$DEST/cue" version 2>/dev/null || true)
if ! grep -qx "cue version ${CUE_VERSION}" <<<"$cue_have"; then
  fetch "https://github.com/cue-lang/cue/releases/download/${CUE_VERSION}/cue_${CUE_VERSION}_linux_${arch}.tar.gz" "$work/cue.tgz"
  echo "${cue_sha}  ${work}/cue.tgz" | sha256sum -c --quiet -
  tar -xzf "$work/cue.tgz" -C "$work" cue
  install -m 0755 "$work/cue" "$DEST/cue"
fi

wanted=()
for pin in "${SCHEMAS[@]}"; do
  read -r tag commit sha <<<"$pin"
  file="$DEST/schema-${tag}.cue"
  wanted+=("$file")
  matches "$sha" "$file" && continue
  fetch "https://raw.githubusercontent.com/grafana/grafana/${commit}/apps/dashboard/kinds/v2/dashboard_spec.cue" "$work/schema.cue"
  echo "${sha}  ${work}/schema.cue" | sha256sum -c --quiet -
  mv "$work/schema.cue" "$file"
done

# Callers pass every schema-*.cue in DEST, so a schema dropped from the pins
# must not linger in a reused directory.
for file in "$DEST"/schema-*.cue; do
  [ -e "$file" ] || continue
  keep=false
  for want in "${wanted[@]}"; do
    [ "$file" = "$want" ] && keep=true
  done
  "$keep" || rm -f "$file"
done

if [ ! -x "$DEST/promql-check" ] || [ "$(cat "$DEST/promql-check.stamp" 2>/dev/null)" != "$stamp" ]; then
  fetch "https://go.dev/dl/${GO_VERSION}.linux-${arch}.tar.gz" "$work/go.tgz"
  echo "${go_sha}  ${work}/go.tgz" | sha256sum -c --quiet -
  tar -xzf "$work/go.tgz" -C "$work"
  # Every Go setting is pinned here, so neither the caller's environment nor a
  # go.work above the action's checkout changes what is built. Modules come
  # through the public proxy and are checked against go.sum and the checksum
  # database. -modcacherw lets the EXIT trap delete the module cache as a
  # non-root runner user.
  (
    cd "$src"
    env -u GOROOT GOENV=off GOWORK=off GOTOOLCHAIN=local GOFLAGS='-mod=readonly -modcacherw' CGO_ENABLED=0 \
      GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org \
      GONOPROXY= GONOSUMDB= GOPRIVATE= GOINSECURE= \
      GOPATH="$work/gopath" GOMODCACHE="$work/gopath/pkg/mod" GOCACHE="$work/gocache" \
      "$work/go/bin/go" build -trimpath -o "$work/promql-check" .
  )
  install -m 0755 "$work/promql-check" "$DEST/promql-check"
  echo "$stamp" >"$DEST/promql-check.stamp"
fi

echo "dashboard-check: cue ${CUE_VERSION}, ${#SCHEMAS[@]} Grafana schema(s) and promql-check (built with ${GO_VERSION}) in ${DEST}"
