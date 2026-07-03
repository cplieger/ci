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
# COVERS (the tools the `ci / validate` gate + advisory security scan run):
#   - Renovate-pinned, exact version: golangci-lint, gitleaks, trivy, hadolint
#     (release binaries), shfmt (release binary), ruff (pipx),
#     markdownlint-cli2 (npm), tsgo (npm tarball)
#   - best-effort LATEST (no CI pin exists to read): shellcheck, yamllint. CI
#     uses the ubuntu-24.04 runner's preinstalled shellcheck (floats with the
#     runner image) and a bare `pip install yamllint` (unpinned), so there is
#     nothing to pin against; the newest release / latest pipx build is the
#     closest local proxy.
#   - standalone complexity binaries (no CI pin to read): gocyclo, gocognit,
#     installed @latest. The `ci / validate` gate runs cyclomatic + cognitive
#     complexity INSIDE golangci-lint (the gocyclo + gocognit linters); these
#     standalone binaries are the `-avg` tools the test-review agent and local
#     measurement use (`gocyclo -avg .`, `gocognit -avg .`), which golangci
#     does not expose.
#   - every `go install <pkg>@<ver>` line in go-ci.yaml; the version is pinned in
#     a shell var (e.g. GOVULNCHECK_VERSION=v1.4.0) which this script resolves:
#     govulncheck, actionlint, deadcode, punused
#   Versions are always read live from the workflows, never hardcoded here: the
#   `# renovate: ... depName=X` + `VERSION` pins for most tools; trivy from the
#   security-scan.yaml TRIVY_VERSION pin; hadolint from its `hadolint/hadolint:
#   <tag>` image reference (it runs in CI as a Docker image, not a VERSION pin).
# NOT covered (install via your package manager, or not part of local validate):
#   - release- or niche-only: git-cliff (release), gremlins (weekly mutation)
#   - project-local TS devdeps run via `npm ci` (eslint, prettier, vitest,
#     stylelint, html-validate, knip), pinned per-repo in package-lock.json
#   - supply-chain/scan actions that don't run locally: cosign, syft, CodeQL
#
# INSTALL TARGETS: Go tools via `go install` (Go bin dir); golangci-lint,
# gitleaks, trivy, hadolint, shellcheck into $BIN_DIR (default ~/.local/bin);
# ruff via pipx; markdownlint-cli2 via npm -g; tsgo extracted to <bindir>/../lib
# and symlinked into $BIN_DIR. $BIN_DIR and the Go bin dir must precede /usr/bin
# on PATH so these shadow any distro packages (e.g. a distro trivy in /usr/bin).
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

ok() { SUMMARY+=("$(printf '  %-18s %-10s %s' "$1" "$2" "${3:-installed}")"); }
skip() { SUMMARY+=("$(printf '  %-18s %-10s %s' "$1" "$2" "already current")"); }
bad() {
  SUMMARY+=("$(printf '  %-18s %-10s %s' "$1" "-" "FAILED: ${2:-}")")
  FAILED+=("$1")
}

install_golangci_lint() {
  local want cur
  want="$(pin_version golangci/golangci-lint)"
  [ -n "$want" ] || {
    bad golangci-lint "no pin found"
    return
  }
  cur="$(golangci-lint version 2>/dev/null | semver || true)"
  [ "$cur" = "${want#v}" ] && {
    skip golangci-lint "$want"
    return
  }
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
  [ -n "$want" ] || {
    bad gitleaks "no pin found"
    return
  }
  cur="$(gitleaks version 2>/dev/null | semver || true)"
  [ "$cur" = "${want#v}" ] && {
    skip gitleaks "$want"
    return
  }
  case "$(uname -m)" in
    x86_64 | amd64) arch=x64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *)
      bad gitleaks "unsupported arch $(uname -m)"
      return
      ;;
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
# locally installed Go helper tools match CI exactly. go-ci.yaml pins each tool's
# version in a shell variable (e.g. `GOVULNCHECK_VERSION=v1.4.0`) and installs it
# as `go install "<pkg>@${GOVULNCHECK_VERSION}"`. We replay that: build a map of
# the workflow's `<NAME>=<value>` assignments, then for each go-install spec strip
# the quotes the YAML left on the token and resolve the `${VAR}` version reference
# against that map. A bare literal version (`@v1.2.3` / `@latest`) passes through
# unchanged, so the un-pinned form still works.
install_go_tools() {
  local goci="$WF_DIR/go-ci.yaml" spec name ver verpart varname line trimmed k v
  [ -f "$goci" ] || {
    bad "go-tools" "go-ci.yaml not found"
    return
  }
  command -v go >/dev/null 2>&1 || {
    bad "go-tools" "go not found"
    return
  }

  # Map every clean `<identifier>=<value>` assignment in the workflow (captures
  # the *_VERSION pins; skips `echo 'app=true'`-style lines whose key holds
  # spaces). Value is taken up to the first whitespace, dropping inline comments.
  local -A vers=()
  while IFS= read -r line; do
    trimmed="${line#"${line%%[![:space:]]*}"}" # strip leading indentation
    case "$trimmed" in
      [A-Za-z_]*=*)
        k="${trimmed%%=*}"
        case "$k" in
          *[!A-Za-z0-9_]*) ;; # key holds spaces/punct -> not an assignment
          *)
            v="${trimmed#*=}"
            v="${v%%[[:space:]]*}"
            [ -n "$v" ] && vers["$k"]="$v"
            ;;
        esac
        ;;
    esac
  done <"$goci"

  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    spec="${spec%\"}"
    spec="${spec#\"}" # strip the quotes YAML left on the token
    spec="${spec%\'}"
    spec="${spec#\'}"
    verpart="${spec##*@}"
    if [ "${verpart:0:1}" = '$' ]; then # version is a ${VAR}/$VAR reference
      varname="${verpart#\$}"
      varname="${varname#\{}"
      varname="${varname%\}}"
      ver="${vers[$varname]:-}"
      name="${spec%@*}"
      name="${name##*/}"
      [ -n "$ver" ] || {
        bad "$name" "unresolved version var \$$varname"
        continue
      }
      spec="${spec%@*}@${ver}"
    fi
    name="${spec##*/}" # last path segment, e.g. govulncheck@v1.4.0
    name="${name%@*}"  # drop @version
    if go install "$spec" >/dev/null 2>&1; then
      ok "$name" "${spec##*@}" "go install"
    else
      bad "$name" "go install failed"
    fi
  done < <(grep -oE 'go install [^[:space:]]+@[^[:space:]]+' "$goci" | awk '{print $3}')
}

# install_complexity_tools: standalone gocyclo + gocognit. These are NOT gate
# tools; the `ci / validate` gate runs cyclomatic AND cognitive complexity
# inside golangci-lint (installed above). These binaries are what the
# test-review agent and local measurement use for the per-package AVERAGE
# (`gocyclo -avg .`, `gocognit -avg .`), which golangci does not report. No CI
# pin exists to read (they live in no workflow), so install @latest: the
# complexity algorithms are stable and these never drive the gate.
install_complexity_tools() {
  command -v go >/dev/null 2>&1 || {
    bad "gocyclo/gocognit" "go not found"
    return
  }
  if go install github.com/fzipp/gocyclo/cmd/gocyclo@latest >/dev/null 2>&1; then
    ok gocyclo "latest" "go install"
  else
    bad gocyclo "go install failed"
  fi
  if go install github.com/uudashr/gocognit/cmd/gocognit@latest >/dev/null 2>&1; then
    ok gocognit "latest" "go install"
  else
    bad gocognit "go install failed"
  fi
}

install_ruff() {
  local want cur
  want="$(pin_version ruff)"
  [ -n "$want" ] || {
    bad ruff "no pin found"
    return
  }
  cur="$(ruff --version 2>/dev/null | semver || true)"
  [ "$cur" = "$want" ] && {
    skip ruff "$want"
    return
  }
  command -v pipx >/dev/null 2>&1 || {
    bad ruff "pipx not found"
    return
  }
  if pipx install --force "ruff==${want}" >/dev/null 2>&1; then
    ok ruff "$want" "pipx"
  else
    bad ruff "pipx failed"
  fi
}

install_markdownlint() {
  local want cur
  want="$(pin_version markdownlint-cli2)"
  [ -n "$want" ] || {
    bad markdownlint-cli2 "no pin found"
    return
  }
  cur="$(markdownlint-cli2 --version 2>/dev/null | semver || true)"
  [ "$cur" = "$want" ] && {
    skip markdownlint-cli2 "$want"
    return
  }
  command -v npm >/dev/null 2>&1 || {
    bad markdownlint-cli2 "npm not found"
    return
  }
  if npm install -g "markdownlint-cli2@${want}" >/dev/null 2>&1; then
    ok markdownlint-cli2 "$want" "npm -g"
  else
    bad markdownlint-cli2 "npm failed"
  fi
}

install_trivy() {
  local want cur arch
  want="$(pin_version aquasecurity/trivy)"
  [ -n "$want" ] || {
    bad trivy "no pin found"
    return
  }
  cur="$(trivy --version 2>/dev/null | semver || true)"
  [ "$cur" = "${want#v}" ] && {
    skip trivy "$want"
    return
  }
  case "$(uname -m)" in
    x86_64 | amd64) arch=64bit ;;
    aarch64 | arm64) arch=ARM64 ;;
    *)
      bad trivy "unsupported arch $(uname -m)"
      return
      ;;
  esac
  mkdir -p "$BIN_DIR"
  if curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/${want}/trivy_${want#v}_Linux-${arch}.tar.gz" \
    | tar -xzf - -C "$BIN_DIR" trivy 2>/dev/null; then
    ok trivy "$want" "-> $BIN_DIR"
  else
    bad trivy "download failed"
  fi
}

# install_hadolint: hadolint runs in CI as a pinned Docker image
# (hadolint/hadolint:<tag> in go-ci.yaml + shell-ci.yaml), not a
# `# renovate: ... VERSION=` line, so read the tag directly rather than via
# pin_version. Install the matching release binary so local Dockerfile lint
# applies the same rule set as the gate.
install_hadolint() {
  local want cur arch
  want="$(grep -hoE 'hadolint/hadolint:[0-9]+\.[0-9]+\.[0-9]+' "$WF_DIR"/*.yaml 2>/dev/null | head -n1 | sed 's/.*://')"
  [ -n "$want" ] || {
    bad hadolint "no pin found"
    return
  }
  cur="$(hadolint --version 2>/dev/null | semver || true)"
  [ "$cur" = "$want" ] && {
    skip hadolint "$want"
    return
  }
  case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *)
      bad hadolint "unsupported arch $(uname -m)"
      return
      ;;
  esac
  mkdir -p "$BIN_DIR"
  if curl -fsSL "https://github.com/hadolint/hadolint/releases/download/v${want}/hadolint-Linux-${arch}" -o "$BIN_DIR/hadolint" \
    && chmod +x "$BIN_DIR/hadolint"; then
    ok hadolint "$want" "-> $BIN_DIR"
  else
    bad hadolint "download failed"
  fi
}

# install_tsgo: the TypeScript native-preview typecheck binary. CI installs it
# out-of-band from the npm registry (it is deliberately NOT a repo
# devDependency) and puts package/lib on PATH. Mirror that: extract the platform
# tarball to a stable lib dir and symlink the tsgo binary into $BIN_DIR. The
# pinned version carries a `-dev.<date>` suffix, so compare the full string
# (not semver()).
install_tsgo() {
  local want cur arch libdir tmp pkg
  want="$(pin_version @typescript/native-preview)"
  [ -n "$want" ] || {
    bad tsgo "no pin found"
    return
  }
  cur="$(tsgo --version 2>/dev/null | grep -oE '[0-9][0-9A-Za-z.-]*' | head -n1 || true)"
  [ "$cur" = "$want" ] && {
    skip tsgo "$want"
    return
  }
  case "$(uname -m)" in
    x86_64 | amd64) arch=x64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *)
      bad tsgo "unsupported arch $(uname -m)"
      return
      ;;
  esac
  pkg="native-preview-linux-${arch}"
  libdir="$(dirname "$BIN_DIR")/lib/tsgo-native"
  tmp="$(mktemp -d)"
  if curl -fsSL "https://registry.npmjs.org/@typescript/${pkg}/-/${pkg}-${want}.tgz" \
    | tar -xzf - -C "$tmp" 2>/dev/null && [ -x "$tmp/package/lib/tsgo" ]; then
    rm -rf "$libdir"
    mkdir -p "$(dirname "$libdir")" "$BIN_DIR"
    mv "$tmp/package/lib" "$libdir"
    ln -sf "$libdir/tsgo" "$BIN_DIR/tsgo"
    ok tsgo "$want" "-> $BIN_DIR"
  else
    bad tsgo "download failed"
  fi
  rm -rf "$tmp"
}

# install_shellcheck: CI does not pin shellcheck — it uses the ubuntu-24.04
# runner's preinstalled copy, which floats with the runner image — so there is
# no pin to read. Best effort per the maintainer's call: track the newest
# stable release (the closest local proxy to the runner's). The tag is resolved
# live from the GitHub API rather than a workflow pin.
install_shellcheck() {
  local want cur arch
  want="$(curl -fsSL "https://api.github.com/repos/koalaman/shellcheck/releases/latest" 2>/dev/null \
    | grep -oE '"tag_name": *"[^"]+"' | head -n1 | sed -E 's/.*"([^"]+)"$/\1/')"
  [ -n "$want" ] || {
    bad shellcheck "could not resolve latest tag"
    return
  }
  cur="$(shellcheck --version 2>/dev/null | awk '/^version:/ {print $2}' || true)"
  [ "$cur" = "${want#v}" ] && {
    skip shellcheck "$want"
    return
  }
  case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    aarch64 | arm64) arch=aarch64 ;;
    *)
      bad shellcheck "unsupported arch $(uname -m)"
      return
      ;;
  esac
  mkdir -p "$BIN_DIR"
  if curl -fsSL "https://github.com/koalaman/shellcheck/releases/download/${want}/shellcheck-${want}.linux.${arch}.tar.xz" \
    | tar -xJf - -C "$BIN_DIR" --strip-components=1 "shellcheck-${want}/shellcheck" 2>/dev/null; then
    ok shellcheck "$want" "-> $BIN_DIR"
  else
    bad shellcheck "download failed"
  fi
}

# install_shfmt: the shell formatter run by the meta CI scripts job and the
# the private repos' bespoke CI. Pinned via `# renovate: ... depName=mvdan/sh` +
# VERSION in the workflows; install the matching single-file release binary.
install_shfmt() {
  local want cur arch
  want="$(pin_version mvdan/sh)"
  [ -n "$want" ] || {
    bad shfmt "no pin found"
    return
  }
  cur="$(shfmt --version 2>/dev/null | sed 's/^v//' || true)"
  [ "$cur" = "${want#v}" ] && {
    skip shfmt "$want"
    return
  }
  case "$(uname -m)" in
    x86_64 | amd64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *)
      bad shfmt "unsupported arch $(uname -m)"
      return
      ;;
  esac
  mkdir -p "$BIN_DIR"
  if curl -fsSL "https://github.com/mvdan/sh/releases/download/${want}/shfmt_${want}_linux_${arch}" -o "$BIN_DIR/shfmt" \
    && chmod +x "$BIN_DIR/shfmt"; then
    ok shfmt "$want" "-> $BIN_DIR"
  else
    bad shfmt "download failed"
  fi
}

# install_yamllint: the YAML linter run by the meta CI scripts job and the
# the private repos' bespoke CI. CI installs it unpinned (`pip install yamllint`), so
# there is no version to read — best-effort, same rationale as shellcheck. Skip
# when already present (presence is enough absent a pin).
install_yamllint() {
  command -v pipx >/dev/null 2>&1 || {
    bad yamllint "pipx not found"
    return
  }
  if command -v yamllint >/dev/null 2>&1; then
    skip yamllint "$(yamllint --version 2>/dev/null | semver || echo present)"
    return
  fi
  if pipx install yamllint >/dev/null 2>&1; then
    ok yamllint "latest" "pipx"
  else
    bad yamllint "pipx failed"
  fi
}

main() {
  printf 'Installing CI-pinned dev tools (pins from %s)\n' "$WF_DIR"
  printf '  bin dir: %s\n\n' "$BIN_DIR"

  install_golangci_lint
  install_go_tools
  install_complexity_tools
  install_gitleaks
  install_hadolint
  install_shellcheck
  install_shfmt
  install_yamllint
  install_trivy
  install_ruff
  install_markdownlint
  install_tsgo

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
