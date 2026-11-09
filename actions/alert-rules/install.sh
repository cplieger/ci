#!/usr/bin/env bash
# Install one pinned rule tool into <dest-dir> and print its path:
#   install.sh promtool|lokitool <version> <dest-dir>
# A binary already installed at that version is kept, so a rerun downloads nothing.
set -euo pipefail

usage() {
  echo "usage: install.sh promtool|lokitool <version> <dest-dir>" >&2
  exit 2
}
[ "$#" -eq 3 ] || usage
tool=$1 version=$2 dest=$3
bin="$dest/$tool-$version"
if [ -x "$bin" ]; then
  echo "$bin"
  exit 0
fi

case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *)
    echo "::error::alert-rules: no $tool build for $(uname -m)" >&2
    exit 1
    ;;
esac

fetch() {
  curl -fsSL --connect-timeout 10 --max-time 300 \
    --retry 7 --retry-max-time 150 --retry-all-errors \
    -o "$2" "$1"
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

case "$tool" in
  promtool)
    name="prometheus-${version#v}.linux-${arch}"
    fetch "https://github.com/prometheus/prometheus/releases/download/${version}/${name}.tar.gz" "$work/prometheus.tgz"
    tar -xzf "$work/prometheus.tgz" -C "$work" "$name/promtool"
    src="$work/$name/promtool"
    ;;
  lokitool)
    fetch "https://github.com/grafana/loki/releases/download/${version}/lokitool-linux-${arch}.zip" "$work/lokitool.zip"
    unzip -q "$work/lokitool.zip" -d "$work"
    src="$work/lokitool-linux-${arch}"
    ;;
  *) usage ;;
esac

mkdir -p "$dest"
install -m 0755 "$src" "$bin"
echo "$bin"
