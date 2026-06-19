#!/usr/bin/env bash
#
# install-local-tools.sh — install the CI-pinned dev tools locally so local
# lint/scan runs match the cplieger/ci gate (ci.yaml / ci-local.sh).
#
# WHY: CI installs specific, Renovate-pinned tool versions (e.g. golangci-lint
# v2.12.2). A local copy on a different version can disagree with the gate: a
# newer or older golangci-lint flags or clears findings CI won't, so a local
# "clean" run is not trustworthy. This installs the exact pinned versions.
#
# HOW: versions are read from the `# renovate:` pins in this repo's workflows,
# the single source of truth, so nothing here hardcodes a version. Re-run after
# `git -C <ci> pull` to pick up Renovate bumps. Some drift between runs (and
# between local and CI) is expected and fine.
#
# COVERS (the tools the `ci / validate` gate installs):
#   - Renovate-pinned, exact version: golangci-lint, gitleaks (release
#     binaries), ruff (pipx), markdownlint-cli2 (npm)
#   - every `go install <pkg>@<ver>` line in go-ci.yaml, replayed verbatim so
#     the version matches CI (currently @latest): govulncheck, actionlint,
#     fieldalignment, deadcode, punused
# NOT covered (install via your package manager, or not part of local validate):
#   - not installed by CI here: shellcheck, hadolint
#   - release- or niche-only: git-cliff, gremlins, tsgo
#
# INSTALL TARGETS: Go tools via `go install` (Go bin dir); golangci-lint and
# gitleaks into $BIN_DIR (default ~/.local/bin); ruff via pipx; markdownlint-cli2
# via npm -g. $BIN_DIR and the Go bin dir must precede /usr/bin on PATH so these
# shadow any distro packages.
#
# USAGE: scripts/install-local-tools.sh
# ENV:   BIN_DIR  override the binary install dir (default ~/.local/bin)

set -euo pipefail

WF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.github/workflows"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

declare -a SUMMARY=()
declare -a FAILED=()

# pin_version <depName>: print the version literal pinned on the line after the
# first `# renovate: ... depName=<depName>` comment across the workflows.
# Uses index() (literal substring) so dots/slashes in depName are not regex.
pin_version() {
  awk -v dep="depName=$1" '
    FNR == 1 { found = 0 }
    index($0, "renovate:") && index($0, dep) { found = 1; next }
    found && /VERSION[ \t]*[:=]/ {
      v = $0
      sub(/^[^:=]*[:=][ \t]*/, "", v)   # drop everything up to the = or :
      sub(/[ \t#].*$/, "", v)           # drop trailing space / inline comment
      gsub(/"/, "", v)                  # drop quotes
      print v
      exit
    }
  ' "$WF_DIR"/*.yaml 2>/dev/null
}

# semver: extract the first X.Y.Z from stdin (a tool's --version output).
semver() { grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1; }

ok()   { SUMMARY+=("$(printf '  %-18s %-10s %s' "$1" "$2" "${3:-installed}")"); }
skip() { SUMMARY+=("$(printf '  %-18s %-10s %s' "$1" "$2" "already current")"); }
bad()  { SUMMARY+=("$(printf '  %-18s %-10s %s' "$1" "-" "FAILED: ${2:-}")"); FAILED+=("$1"); }

install_golangci_lint() {
  local want cur
  want="$(pin_version golangci/golangci-lint)"
  [ -n "$want" ] || { bad golangci-lint "no pin found"; return; }
  cur="$(golangci-lint version 2>/dev/null | semver || true)"
  [ "$cur" = "${want#v}" ] && { skip golangci-lint "$want"; return; }
  mkdir -p "$BIN_DIR"
  if curl -fsSL "https://raw.githubusercontent.com/golangci/golangci-lint/${want}/install.sh" \
       | sh -s -- -b "$BIN_DIR" "$want" >/dev/null 2>&1; then
    ok golangci-lint "$want" "-> $BIN_DIR"
  else
    bad golangci-lint "install.sh failed"
  fi
}

install_gitleaks() {
  local want cur arch
  want="$(pin_version gitleaks/gitleaks)"
  [ -n "$want" ] || { bad gitleaks "no pin found"; return; }
  cur="$(gitleaks version 2>/dev/null | semver || true)"
  [ "$cur" = "${want#v}" ] && { skip gitleaks "$want"; return; }
  case "$(uname -m)" in
    x86_64 | amd64) arch=x64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *) bad gitleaks "unsupported arch $(uname -m)"; return ;;
  esac
  mkdir -p "$BIN_DIR"
  if curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/${want}/gitleaks_${want#v}_linux_${arch}.tar.gz" \
       | tar -xzf - -C "$BIN_DIR" gitleaks 2>/dev/null; then
    ok gitleaks "$want" "-> $BIN_DIR"
  else
    bad gitleaks "download failed"
  fi
}

# install_go_tools: replay every `go install <pkg>@<ver>` from go-ci.yaml so the
# locally installed Go helper tools match CI exactly (CI uses @latest for these
# today; if it ever pins them, this follows automatically).
install_go_tools() {
  local goci="$WF_DIR/go-ci.yaml" spec name
  [ -f "$goci" ] || { bad "go-tools" "go-ci.yaml not found"; return; }
  command -v go >/dev/null 2>&1 || { bad "go-tools" "go not found"; return; }
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    name="${spec##*/}"   # last path segment, e.g. govulncheck@latest
    name="${name%@*}"    # drop @version
    if go install "$spec" >/dev/null 2>&1; then
      ok "$name" "${spec##*@}" "go install"
    else
      bad "$name" "go install failed"
    fi
  done < <(grep -oE 'go install [^[:space:]]+@[^[:space:]]+' "$goci" | awk '{print $3}')
}

install_ruff() {
  local want cur
  want="$(pin_version ruff)"
  [ -n "$want" ] || { bad ruff "no pin found"; return; }
  cur="$(ruff --version 2>/dev/null | semver || true)"
  [ "$cur" = "$want" ] && { skip ruff "$want"; return; }
  command -v pipx >/dev/null 2>&1 || { bad ruff "pipx not found"; return; }
  if pipx install --force "ruff==${want}" >/dev/null 2>&1; then
    ok ruff "$want" "pipx"
  else
    bad ruff "pipx failed"
  fi
}

install_markdownlint() {
  local want cur
  want="$(pin_version markdownlint-cli2)"
  [ -n "$want" ] || { bad markdownlint-cli2 "no pin found"; return; }
  cur="$(markdownlint-cli2 --version 2>/dev/null | semver || true)"
  [ "$cur" = "$want" ] && { skip markdownlint-cli2 "$want"; return; }
  command -v npm >/dev/null 2>&1 || { bad markdownlint-cli2 "npm not found"; return; }
  if npm install -g "markdownlint-cli2@${want}" >/dev/null 2>&1; then
    ok markdownlint-cli2 "$want" "npm -g"
  else
    bad markdownlint-cli2 "npm failed"
  fi
}

main() {
  printf 'Installing CI-pinned dev tools (pins from %s)\n' "$WF_DIR"
  printf '  bin dir: %s\n\n' "$BIN_DIR"

  install_golangci_lint
  install_go_tools
  install_gitleaks
  install_ruff
  install_markdownlint

  printf 'tool               version    status\n'
  printf '%s\n' "${SUMMARY[@]}"

  if [ "${#FAILED[@]}" -gt 0 ]; then
    printf '\nWARNING: %d tool(s) not installed: %s\n' "${#FAILED[@]}" "${FAILED[*]}"
    printf 'Install the missing toolchain (go / pipx / npm / curl) and re-run.\n'
    exit 1
  fi
  printf '\nDone. Ensure %s and your Go bin dir precede /usr/bin on PATH.\n' "$BIN_DIR"
}

main "$@"
