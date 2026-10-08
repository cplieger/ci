#!/usr/bin/env bash
# Install the pinned cue binary and Grafana's dashboard schemas into <dest-dir>:
# <dest-dir>/cue and one <dest-dir>/schema-<grafana tag>.cue per pin below.
# Files that already match their pin are kept, so a rerun downloads nothing.
set -euo pipefail

[ "$#" -eq 1 ] || {
  echo "usage: install.sh <dest-dir>" >&2
  exit 2
}
DEST=$1

# No Renovate manager reads this file: move CUE_VERSION and both sha256s
# together, by hand.
CUE_VERSION=v0.17.1
CUE_SHA256_AMD64=a39b0c97695069d95d276d99be0f5dbabb081d801bfdc9ba49b76efaf94e2369
CUE_SHA256_ARM64=0d729be30d52c952ca38fc9dcb692caa09d8463fa0b64df5781312779183fbcd

# grafana/grafana's apps/dashboard tree is AGPL-3.0-only, so the schema is
# fetched by commit and digest instead of being vendored into this repo.
# Grafana 13.2.0 through 13.2.3 ship this file byte for byte.
SCHEMAS=(
  "v13.2.3 6193dc03311b631b9727b560d24369e683dc396e d3f5115a32ce587abb84e276cdc57b8db4615d7fff0b7830232fdc45ff8d1e35"
)

fetch() {
  curl -fsSL --connect-timeout 10 --max-time 60 \
    --retry 7 --retry-max-time 150 --retry-all-errors \
    -o "$2" "$1"
}

matches() {
  [ -f "$2" ] && echo "$1  $2" | sha256sum -c --status -
}

case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 cue_sha=$CUE_SHA256_AMD64 ;;
  aarch64 | arm64) arch=arm64 cue_sha=$CUE_SHA256_ARM64 ;;
  *)
    echo "::error::dashboard-check: no cue build pinned for $(uname -m)" >&2
    exit 1
    ;;
esac

mkdir -p "$DEST"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if ! "$DEST/cue" version 2>/dev/null | grep -qx "cue version ${CUE_VERSION}"; then
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

echo "dashboard-check: cue ${CUE_VERSION} and ${#SCHEMAS[@]} Grafana schema(s) in ${DEST}"
