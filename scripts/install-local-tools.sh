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
#     (release binaries), shfmt + the shellcheck binary (release binaries; CI
#     pins the latter since the SC2317 runner false-positive fix), ruff, zizmor
#     (pipx), yamllint (pipx), markdownlint-cli2 (npm). Every one of these is
#     pinned in a workflow next to a `# renovate:` comment.
#   - standalone complexity binaries (no CI pin to read): gocyclo, gocognit,
#     installed @latest. The `ci / validate` gate runs cyclomatic + cognitive
#     complexity INSIDE golangci-lint (the gocyclo + gocognit linters); these
#     standalone binaries are the `-avg` tools the test-review agent and local
#     measurement use (`gocyclo -avg .`, `gocognit -avg .`), which golangci
#     does not expose.
#   - every `go install <pkg>@<ver>` line in go-ci.yaml; the version is pinned in
#     a shell var (e.g. GOVULNCHECK_VERSION=v1.6.0) which this script resolves:
#     govulncheck, actionlint, deadcode, punused
#   Versions are always read live from the workflows, never hardcoded here,
#   via the `# renovate: ... depName=X` + `VERSION` pins (hadolint's pin is the
#   HADOLINT_VERSION var feeding its `hadolint/hadolint:<tag>` docker-run).
# NOT covered (install via your package manager, or not part of local validate):
#   - release- or niche-only: git-cliff (release), gremlins (weekly mutation)
#   - project-local TS devdeps run via `npm ci` (the native `tsc` via each
#     repo's @typescript/native alias, plus eslint, prettier, vitest, stylelint,
#     html-validate, knip), pinned per-repo in package-lock.json
#   - supply-chain/scan actions that don't run locally: cosign, syft, CodeQL
#
# INSTALL TARGETS: Go tools via `go install` (Go bin dir); golangci-lint,
# gitleaks, trivy, hadolint, shellcheck into $BIN_DIR (default ~/.local/bin);
# ruff via pipx; markdownlint-cli2 via npm -g. $BIN_DIR and the Go bin dir must
# precede /usr/bin on PATH so these shadow any distro packages (e.g. a distro
# trivy in /usr/bin).
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
# unchanged, so the un-pinned form still works. Each `go install` runs under
# GOTOOLCHAIN=auto so a tool whose module requires a newer Go than the local base
# toolchain still builds (auto fetches the needed toolchain on demand) — matching
# CI, where setup-go installs the go.mod version and GOTOOLCHAIN is unset (auto).
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
    if GOTOOLCHAIN=auto go install "$spec" >/dev/null 2>&1; then
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
  # GOTOOLCHAIN=auto: build even when the base Go lags the tool's go.mod (see install_go_tools).
  if GOTOOLCHAIN=auto go install github.com/fzipp/gocyclo/cmd/gocyclo@latest >/dev/null 2>&1; then
    ok gocyclo "latest" "go install"
  else
    bad gocyclo "go install failed"
  fi
  if GOTOOLCHAIN=auto go install github.com/uudashr/gocognit/cmd/gocognit@latest >/dev/null 2>&1; then
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

# install_hadolint: hadolint runs in CI as a Docker image whose tag is pinned
# by the HADOLINT_VERSION var next to a `# renovate: datasource=docker` comment
# in shell-ci.yaml — readable via pin_version like every other tool. Install
# the matching release binary so local Dockerfile lint applies the same rule
# set as the gate.
install_hadolint() {
  local want cur arch
  want="$(pin_version hadolint/hadolint)"
  want="${want#v}" # docker tag carries no v prefix; normalize anyway
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

# install_shellcheck: pinned in shell-ci.yaml AND the meta ci.yaml scripts job
# (the runner's preinstalled copy false-positives SC2317, so CI installs its
# own); read the pin like every other tool.
install_shellcheck() {
  local want cur arch
  want="$(pin_version koalaman/shellcheck)"
  [ -n "$want" ] || {
    bad shellcheck "no pin found"
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
# private repos' bespoke CI. Pinned in the scripts job next to a
# `# renovate: datasource=pypi` comment; install the exact version.
install_yamllint() {
  local want cur
  want="$(pin_version yamllint)"
  [ -n "$want" ] || {
    bad yamllint "no pin found"
    return
  }
  cur="$(yamllint --version 2>/dev/null | semver || true)"
  [ "$cur" = "$want" ] && {
    skip yamllint "$want"
    return
  }
  command -v pipx >/dev/null 2>&1 || {
    bad yamllint "pipx not found"
    return
  }
  if pipx install --force "yamllint==${want}" >/dev/null 2>&1; then
    ok yamllint "$want" "pipx"
  else
    bad yamllint "pipx failed"
  fi
}

# install_zizmor: the GitHub Actions security auditor run by the meta CI
# scripts job (soft-gated). Pinned there next to a `# renovate:
# datasource=pypi` comment; install the exact version so local ci-local runs
# match the gate.
install_zizmor() {
  local want cur
  want="$(pin_version zizmor)"
  [ -n "$want" ] || {
    bad zizmor "no pin found"
    return
  }
  cur="$(zizmor --version 2>/dev/null | semver || true)"
  [ "$cur" = "$want" ] && {
    skip zizmor "$want"
    return
  }
  command -v pipx >/dev/null 2>&1 || {
    bad zizmor "pipx not found"
    return
  }
  if pipx install --force "zizmor==${want}" >/dev/null 2>&1; then
    ok zizmor "$want" "pipx"
  else
    bad zizmor "pipx failed"
  fi
}

# advise_gotoolchain: the go installs above self-heal a base/toolchain version
# skew via GOTOOLCHAIN=auto, but local *repo* builds do not. With
# GOTOOLCHAIN=local (Fedora bakes this default into its Go build) a
# `go build`/`go test` in a repo whose go.mod pins a newer Go than the base
# toolchain fails instead of fetching it, unlike CI (setup-go installs the
# go.mod version) and the Docker builds (GOTOOLCHAIN=auto). Warn so the dev box
# can match; do not rewrite the user's global setting from a shared installer.
advise_gotoolchain() {
  command -v go >/dev/null 2>&1 || return 0
  [ "$(go env GOTOOLCHAIN 2>/dev/null)" = local ] || return 0
  local base
  base="$(go env GOVERSION 2>/dev/null)"
  printf '\nNote: GOTOOLCHAIN=local (base %s). Local go build / go test in a repo\n' "$base"
  printf '  whose go.mod pins a newer Go fails instead of fetching it, unlike CI\n'
  printf '  (setup-go installs the go.mod version) and the Docker builds. To match:\n'
  printf '    go env -w GOTOOLCHAIN=auto\n'
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
  install_zizmor
  install_trivy
  install_ruff
  install_markdownlint

  printf 'tool               version    status\n'
  printf '%s\n' "${SUMMARY[@]}"

  advise_gotoolchain

  if [ "${#FAILED[@]}" -gt 0 ]; then
    printf '\nWARNING: %d tool(s) not installed: %s\n' "${#FAILED[@]}" "${FAILED[*]}"
    printf 'Install the missing toolchain (go / pipx / npm / curl) and re-run.\n'
    exit 1
  fi
  printf '\nDone. Ensure %s and your Go bin dir precede /usr/bin on PATH.\n' "$BIN_DIR"
}

main "$@"
