#!/usr/bin/env bash
# install-local-tools.sh: install the dev tools at the versions cplieger/ci's
# workflows pin, so a local lint or scan agrees with the gate. Each version is
# read from a `# renovate:` pin or a `go install <pkg>@${VAR}` line under
# .github/workflows; re-run after a Renovate bump lands there. Not covered: the
# per-package npm devDependencies, and the browser of a Vitest Browser Mode
# package (`npx --no-install playwright install chromium` in its directory).
# USAGE: scripts/install-local-tools.sh. BIN_DIR (default ~/.local/bin) and the
# Go bin dir must precede /usr/bin on PATH to shadow distro packages.

set -euo pipefail

WF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.github/workflows"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

declare -a SUMMARY=()
declare -a FAILED=()

# pin_version <depName>: print the version literal pinned on the line after the
# first `# renovate: ... depName=<depName>` comment across the workflows, or
# nothing (status 0) when none pins it, so the caller reports "no pin found".
# index() matches literally, so the dots in a depName are no regex.
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
  ' "$WF_DIR"/*.yaml 2>/dev/null || true
}

# semver: extract the first X.Y.Z from stdin (a tool's --version output).
semver() { grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1; }

# fetch_extract <url> <tar-arg>...: download an archive to a temp file, then run
# tar on it with the given arguments. Returns non-zero if either half fails.
# Never `curl | tar`: `--retry-all-errors` is what retries a mid-transfer receive
# failure (curl exit 56), and curl can reset only output it owns via `-o`, so a
# retry into a pipe would replay bytes tar already consumed.
fetch_extract() {
  local url=$1
  shift
  local tmp rc=0
  tmp="$(mktemp "${TMPDIR:-/tmp}/install-local-tools.XXXXXX")" || return 1
  curl -fsSL --connect-timeout 10 --max-time 120 \
    --retry 7 --retry-max-time 150 --retry-all-errors \
    -o "$tmp" "$url" \
    && tar -f "$tmp" "$@" || rc=$?
  rm -f "$tmp"
  return "$rc"
}

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
  if fetch_extract "https://github.com/gitleaks/gitleaks/releases/download/${want}/gitleaks_${want#v}_linux_${arch}.tar.gz" \
    -xz -C "$BIN_DIR" gitleaks 2>/dev/null; then
    ok gitleaks "$want" "-> $BIN_DIR"
  else
    bad gitleaks "download failed"
  fi
}

# The go-tools pins sit in go-ci.yaml and deadset-ci.yaml, as `NAME=value` or
# `NAME: value` lines that a `go install <pkg>@${VAR}` reads. GOTOOLCHAIN=auto,
# as deadset-ci.yaml's Install deadset sets, lets a tool needing a newer Go than
# the local one build.
install_go_tools() {
  local spec name ver verpart varname line trimmed k v wf
  local -a wfs=("$WF_DIR/go-ci.yaml" "$WF_DIR/deadset-ci.yaml")
  for wf in "${wfs[@]}"; do
    [ -f "$wf" ] || {
      bad "go-tools" "${wf##*/} not found"
      return
    }
  done
  command -v go >/dev/null 2>&1 || {
    bad "go-tools" "go not found"
    return
  }

  # A key holding punctuation (`echo "work=$work"`) is no assignment.
  local -A vers=()
  while IFS= read -r line; do
    trimmed="${line#"${line%%[![:space:]]*}"}" # strip leading indentation
    case "$trimmed" in
      [A-Za-z_]*=* | [A-Za-z_]*:\ *)
        k="${trimmed%%[=:]*}"
        case "$k" in
          *[!A-Za-z0-9_]*) ;; # key holds spaces/punct -> not an assignment
          *)
            v="${trimmed#"$k"}"
            v="${v#[=:]}"
            v="${v#"${v%%[![:space:]]*}"}"
            v="${v%%[[:space:]]*}"
            [ -n "$v" ] && vers["$k"]="$v"
            ;;
        esac
        ;;
    esac
  done < <(cat "${wfs[@]}")

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
  done < <(grep -ohE 'go install [^[:space:]]+@[^[:space:]]+' "${wfs[@]}" | awk '{print $3}')
}

# install_complexity_tools: gocyclo and gocognit for their `-avg` reports, which
# golangci-lint does not give. No workflow pins them, so @latest.
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

install_deadset_ts() {
  local want cur
  want="$(pin_version @cplieger/deadset-ts)"
  [ -n "$want" ] || {
    bad deadset-ts "no pin found"
    return
  }
  # The pin can be a prerelease (7.0.0-dev.2), which semver would cut to 7.0.0.
  cur="$(deadset-ts version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?' | head -n1 || true)"
  [ "$cur" = "$want" ] && {
    skip deadset-ts "$want"
    return
  }
  command -v npm >/dev/null 2>&1 || {
    bad deadset-ts "npm not found"
    return
  }
  if npm install -g --ignore-scripts "@cplieger/deadset-ts@${want}" >/dev/null 2>&1; then
    ok deadset-ts "$want" "npm -g"
  else
    bad deadset-ts "npm failed"
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
  if fetch_extract "https://github.com/aquasecurity/trivy/releases/download/${want}/trivy_${want#v}_Linux-${arch}.tar.gz" \
    -xz -C "$BIN_DIR" trivy 2>/dev/null; then
    ok trivy "$want" "-> $BIN_DIR"
  else
    bad trivy "download failed"
  fi
}

# install_hadolint: CI runs hadolint as a Docker image tagged HADOLINT_VERSION;
# this installs the release binary of the same version.
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
  if fetch_extract "https://github.com/koalaman/shellcheck/releases/download/${want}/shellcheck-${want}.linux.${arch}.tar.xz" \
    -xJ -C "$BIN_DIR" --strip-components=1 "shellcheck-${want}/shellcheck" 2>/dev/null; then
    ok shellcheck "$want" "-> $BIN_DIR"
  else
    bad shellcheck "download failed"
  fi
}

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

install_actionlint() {
  local want cur arch
  want="$(pin_version rhysd/actionlint)"
  [ -n "$want" ] || {
    bad actionlint "no pin found"
    return
  }
  cur="$(actionlint --version 2>/dev/null | semver || true)"
  [ "$cur" = "${want#v}" ] && {
    skip actionlint "$want"
    return
  }
  case "$(uname -m)" in
    x86_64 | amd64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *)
      bad actionlint "unsupported arch $(uname -m)"
      return
      ;;
  esac
  mkdir -p "$BIN_DIR"
  if fetch_extract "https://github.com/rhysd/actionlint/releases/download/${want}/actionlint_${want#v}_linux_${arch}.tar.gz" \
    -xz -C "$BIN_DIR" actionlint 2>/dev/null; then
    ok actionlint "$want" "-> $BIN_DIR"
  else
    bad actionlint "download failed"
  fi
}

# install_lychee: the tag is `lychee-vX.Y.Z` (the repo also tags lychee-lib-*),
# and the archive nests the binary in a target-named directory.
install_lychee() {
  local want cur target
  want="$(pin_version lycheeverse/lychee)"
  [ -n "$want" ] || {
    bad lychee "no pin found"
    return
  }
  cur="$(lychee --version 2>/dev/null | semver || true)"
  [ "$cur" = "${want#v}" ] && {
    skip lychee "$want"
    return
  }
  case "$(uname -m)" in
    x86_64 | amd64) target=x86_64-unknown-linux-gnu ;;
    aarch64 | arm64) target=aarch64-unknown-linux-gnu ;;
    *)
      bad lychee "unsupported arch $(uname -m)"
      return
      ;;
  esac
  mkdir -p "$BIN_DIR"
  if fetch_extract "https://github.com/lycheeverse/lychee/releases/download/lychee-${want}/lychee-${target}.tar.gz" \
    -xz -C "$BIN_DIR" --strip-components=1 "lychee-${target}/lychee" 2>/dev/null; then
    ok lychee "$want" "-> $BIN_DIR"
  else
    bad lychee "download failed"
  fi
}

# advise_gotoolchain: warn rather than rewrite the user's global setting. With
# GOTOOLCHAIN=local, a repo whose go.mod needs a newer Go fails to build
# instead of fetching that Go, unlike CI and the Docker builds.
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
  install_actionlint
  install_hadolint
  install_shellcheck
  install_shfmt
  install_yamllint
  install_zizmor
  install_trivy
  install_ruff
  install_markdownlint
  install_deadset_ts
  install_lychee

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
