#!/usr/bin/env bash
# Regression probe for the nested-Go-module lane SHELL semantics that
# release.yaml (detect: gomodules + changed-path classification, go-nested:
# module-path verify + version guards + regex escaping) and ci.yaml (detect:
# nested validation discovery) embed inline.
#
# The sibling probe (test-cliff-bump-semantics.sh) pins the git-cliff side of
# the lane contract (states H/I/J/K); this one pins the bash side. The
# function bodies below MIRROR the workflow snippets — when editing either,
# update the other in the same change (the same keep-in-sync convention as
# EXCLUDE_PATTERNS vs the consumer cliff.toml exclude_paths).
#
# Runs in the ci repo's `scripts` CI job (opt-in by file presence), so a
# change to the prune list, the eligibility filters, the classification
# loop, or the escaping cannot merge if it breaks the pinned contract.
set -euo pipefail

PASS=0
fail() {
  echo "FAIL: $*" >&2
  exit 1
}
chk() { # label actual expected
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1))
    echo "ok: $1 -> $2"
  else
    fail "$1: expected '$3', got '$2'"
  fi
}

WORK="$(mktemp -d /tmp/lane-semantics.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# ── The mirrored snippets ────────────────────────────────────────────────────

# release.yaml detect/gomodules: RELEASE-lane discovery (full eligibility).
discover_release() { # run in a repo; prints JSON array or fails
  local LANES="[]" f d modpath a b
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    d=$(dirname "$f")
    case "$d" in
      *[!A-Za-z0-9._/-]*)
        echo "CHARSET_ERROR"
        return 0
        ;;
    esac
    case "/$d/" in
      */internal/*) continue ;;
    esac
    modpath=$(awk '$1=="module"{print $2; exit}' "$f")
    case "${modpath%%/*}" in
      *.*) : ;;
      *) continue ;;
    esac
    if [ -z "$(git ls-files "$d/*.go" | head -1)" ]; then
      continue
    fi
    LANES=$(echo "$LANES" | jq -c --arg v "$d" '. + [$v]')
  done < <(git ls-files '*/go.mod' | grep -Ev '(^|/)(node_modules|vendor|testdata|static|dist)/' || true)
  while IFS= read -r a; do
    [ -z "$a" ] && continue
    while IFS= read -r b; do
      [ -z "$b" ] && continue
      if [ "$a" != "$b" ] && [ "${b#"$a"/}" != "$b" ]; then
        echo "OVERLAP_ERROR"
        return 0
      fi
    done < <(echo "$LANES" | jq -r '.[]')
  done < <(echo "$LANES" | jq -r '.[]')
  echo "$LANES"
}

# ci.yaml detect: VALIDATION discovery (keeps internal/ + dotless modules
# that have Go code; skips no-.go sentinels).
discover_ci() {
  local DIRS="[]" f d
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    d=$(dirname "$f")
    case "$d" in
      *[!A-Za-z0-9._/-]*)
        echo "CHARSET_ERROR"
        return 0
        ;;
    esac
    if [ -z "$(git ls-files "$d/*.go" | head -1)" ]; then
      continue
    fi
    DIRS=$(echo "$DIRS" | jq -c --arg v "$d" '. + [$v]')
  done < <(git ls-files '*/go.mod' | grep -Ev '(^|/)(node_modules|vendor|testdata|static|dist)/' || true)
  echo "$DIRS"
}

# release.yaml detect/changes: root/subpackage/lane classification.
classify() { # SIGNIFICANT on stdin; env SUBPACKAGES_JSON GO_LANES_JSON
  local SIGNIFICANT ROOT_CHANGED=false f s l in_subpkg in_lane OUT="[]"
  SIGNIFICANT=$(cat)
  declare -A SUBPKG_CHANGED
  declare -A LANE_CHANGED
  mapfile -t SUBPACKAGES < <(echo "$SUBPACKAGES_JSON" | jq -r '.[]')
  mapfile -t GO_LANES < <(echo "$GO_LANES_JSON" | jq -r '.[]')
  for s in "${SUBPACKAGES[@]}"; do SUBPKG_CHANGED["$s"]=false; done
  for l in "${GO_LANES[@]}"; do LANE_CHANGED["$l"]=false; done
  if [ -n "$SIGNIFICANT" ]; then
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      in_subpkg=false
      for s in "${SUBPACKAGES[@]}"; do
        if [[ "$f" == "$s/"* ]]; then
          # shellcheck disable=SC2034 # mirrors the workflow snippet verbatim; the workflow reads it
          SUBPKG_CHANGED["$s"]=true
          in_subpkg=true
          break
        fi
      done
      in_lane=false
      for l in "${GO_LANES[@]}"; do
        if [[ "$f" == "$l/"* ]]; then
          LANE_CHANGED["$l"]=true
          in_lane=true
          break
        fi
      done
      if ! $in_subpkg && ! $in_lane; then ROOT_CHANGED=true; fi
    done <<<"$SIGNIFICANT"
  fi
  for l in "${GO_LANES[@]}"; do
    if [ "${LANE_CHANGED[$l]}" = "true" ]; then
      OUT=$(echo "$OUT" | jq -c --arg v "$l" '. + [$v]')
    fi
  done
  echo "root=$ROOT_CHANGED lanes=$OUT"
}

# go-nested: lane module-path verification.
verify_modpath() { # DIR VERSION modpath GITHUB_REPOSITORY -> ok|err
  local DIR="$1" VERSION="$2" modpath="$3" GITHUB_REPOSITORY="$4" ver major expected
  ver="${VERSION#"$DIR"/v}"
  major="${ver%%.*}"
  expected="github.com/${GITHUB_REPOSITORY}/${DIR}"
  case "$major" in
    '' | *[!0-9]*) : ;;
    *)
      if [ "$major" -ge 2 ]; then expected="${expected}/v${major}"; fi
      ;;
  esac
  if [ "$modpath" != "$expected" ]; then echo "err"; else echo "ok"; fi
}

# go-nested / detect-lanes: version-string guards.
lane_guard() {
  case "$1" in
    "$2"/v[0-9]*) echo ok ;;
    *) echo refuse ;;
  esac
}
root_guard() {
  case "$1" in
    v[0-9]*) echo ok ;;
    *) echo refuse ;;
  esac
}

# go-nested: regex escaping for --tag-pattern.
esc() { printf '%s' "$1" | sed -e 's/[][\.|(){}?+*^$]/\\&/g'; }

# ── Discovery states ─────────────────────────────────────────────────────────
mkrepo() { # name -> $R
  R="$WORK/$1"
  mkdir -p "$R"
  git -C "$R" init -q -b main
  git -C "$R" config user.email probe@ci.local
  git -C "$R" config user.name probe
  git -C "$R" config commit.gpgsign false
}
put() { # path content...
  mkdir -p "$R/$(dirname "$1")"
  printf '%s\n' "${@:2}" >"$R/$1"
}

mkrepo full
put go.mod "module github.com/cplieger/repo" "go 1.26.5"
put src/main.go "package main"
put yamlenv/go.mod "module github.com/cplieger/repo/yamlenv" "go 1.26.5"
put yamlenv/y.go "package yamlenv"
put web/go.mod "module web-ignore" "go 1.26.5" # sentinel: dotless, no .go
put tools/gen/go.mod "module github.com/cplieger/repo/tools/gen" "go 1.26.5"
put tools/gen/main.go "package main"
put internal/mod/go.mod "module github.com/cplieger/repo/internal/mod" "go 1.26.5"
put internal/mod/m.go "package mod"
put empty/go.mod "module github.com/cplieger/repo/empty" "go 1.26.5" # no .go files
put node_modules/flatted/go.mod "module github.com/vendored/flatted" "go 1.0"
put node_modules/flatted/f.go "package flatted"
git -C "$R" add -A
git -C "$R" commit -qm init
chk "L-A1 release discovery: lanes only (sentinel, internal, empty, vendored excluded)" \
  "$(cd "$R" && discover_release)" '["tools/gen","yamlenv"]'
chk "L-A2 ci discovery: adds internal/ (has code), still skips sentinel/empty/vendored" \
  "$(cd "$R" && discover_ci)" '["internal/mod","tools/gen","yamlenv"]'

mkrepo single
put go.mod "module github.com/cplieger/single" "go 1.26.5"
put main.go "package main"
git -C "$R" add -A
git -C "$R" commit -qm init
chk "L-A3 single-module repo: no lanes (release)" "$(cd "$R" && discover_release)" "[]"
chk "L-A4 single-module repo: no nested validation (ci)" "$(cd "$R" && discover_ci)" "[]"

mkrepo overlap
put go.mod "module github.com/cplieger/overlap" "go 1.26.5"
put a/go.mod "module github.com/cplieger/overlap/a" "go 1.26.5"
put a/a.go "package a"
put a/b/go.mod "module github.com/cplieger/overlap/a/b" "go 1.26.5"
put a/b/b.go "package b"
git -C "$R" add -A
git -C "$R" commit -qm init
chk "L-A5 overlapping lanes rejected (release)" "$(cd "$R" && discover_release)" "OVERLAP_ERROR"
chk "L-A6 overlapping modules both validated (ci; ls-files order)" "$(cd "$R" && discover_ci)" '["a/b","a"]'

mkrepo untracked
put go.mod "module github.com/cplieger/u" "go 1.26.5"
put m.go "package main"
git -C "$R" add -A
git -C "$R" commit -qm init
put scratch/go.mod "module github.com/cplieger/u/scratch" "go 1.26.5"
put scratch/s.go "package scratch"
chk "L-A7 untracked go.mod is not a lane" "$(cd "$R" && discover_release)" "[]"

# ── Classification states ────────────────────────────────────────────────────
export SUBPACKAGES_JSON='["web"]'
export GO_LANES_JSON='["yamlenv","tools/gen"]'
chk "L-B1 lane-only change" "$(printf 'yamlenv/yamlenv.go\n' | classify)" 'root=false lanes=["yamlenv"]'
chk "L-B2 root-only change" "$(printf 'envx.go\n' | classify)" 'root=true lanes=[]'
chk "L-B3 mixed change" "$(printf 'envx.go\nyamlenv/go.mod\ntools/gen/x.go\n' | classify)" 'root=true lanes=["yamlenv","tools/gen"]'
chk "L-B4 subpackage change is neither root nor lane" "$(printf 'web/index.ts\n' | classify)" 'root=false lanes=[]'
chk "L-B5 lane prefix is dir-anchored (yamlenv2/ is root)" "$(printf 'yamlenv2/file.go\n' | classify)" 'root=true lanes=[]'
chk "L-B6 empty significant set" "$(printf '' | classify)" 'root=false lanes=[]'

# ── Module-path verification states ──────────────────────────────────────────
chk "L-C1 v1 exact path ok" "$(verify_modpath yamlenv yamlenv/v1.2.3 github.com/cplieger/envx/yamlenv cplieger/envx)" "ok"
chk "L-C2 v2 without /v2 rejected" "$(verify_modpath yamlenv yamlenv/v2.0.0 github.com/cplieger/envx/yamlenv cplieger/envx)" "err"
chk "L-C3 v2 with /v2 ok" "$(verify_modpath yamlenv yamlenv/v2.0.0 github.com/cplieger/envx/yamlenv/v2 cplieger/envx)" "ok"
chk "L-C4 copy-pasted module path rejected" "$(verify_modpath yamlenv yamlenv/v1.0.0 github.com/cplieger/envx cplieger/envx)" "err"
chk "L-C5 deep lane path ok" "$(verify_modpath tools/gen tools/gen/v1.0.0 github.com/cplieger/x/tools/gen cplieger/x)" "ok"

# ── Version-guard states ─────────────────────────────────────────────────────
chk "L-D1 lane guard accepts own version" "$(lane_guard yamlenv/v1.0.1 yamlenv)" "ok"
chk "L-D2 lane guard refuses root version" "$(lane_guard v1.0.1 yamlenv)" "refuse"
chk "L-D3 lane guard refuses other lane" "$(lane_guard other/v1.0.0 yamlenv)" "refuse"
chk "L-D4 root guard accepts root" "$(root_guard v1.2.3)" "ok"
chk "L-D5 root guard refuses lane-prefixed" "$(root_guard yamlenv/v1.2.3)" "refuse"

# ── Escaping states ──────────────────────────────────────────────────────────
chk "L-E1 plain dir unchanged" "$(esc yamlenv)" "yamlenv"
chk "L-E2 dots escaped" "$(esc 'v2.pkg')" 'v2\.pkg'
chk "L-E3 deep path unchanged" "$(esc tools/gen)" "tools/gen"
# shellcheck disable=SC2016 # literal $ is the point of the test
chk "L-E4 dollar escaped" "$(esc 'a$b')" 'a\$b'

# ── Workflow plumbing contract (lane-aware docker release notes) ─────────────
# The cliff probe (states J/L in test-cliff-bump-semantics.sh) pins the FLAG
# semantics; these checks pin the PLUMBING that delivers the flags to the
# docker pipeline — a later edit that stops forwarding go_modules or drops
# the LANE_ARGS expansion would keep every cliff state green while silently
# unscoping image release notes.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE_YAML="$ROOT/.github/workflows/release.yaml"
DOCKER_YAML="$ROOT/.github/workflows/docker-release.yaml"
# shellcheck disable=SC2016 # the ${{ }} is a literal GitHub expression, not shell
chk "L-F1 release.yaml forwards detect's go_modules to docker-release" \
  "$(grep -c 'go-modules: ${{ needs.detect.outputs.go_modules }}' "$RELEASE_YAML")" "1"
chk "L-F2 docker-release go-modules input defaults to '[]'" \
  "$(grep -c 'default: "\[\]"' "$DOCKER_YAML")" "1"
# shellcheck disable=SC2016 # literal single-quoted grep pattern, no expansion wanted
chk "L-F3 both docker notes branches expand LANE_ARGS" \
  "$(grep -c 'git-cliff .*"\${LANE_ARGS\[@\]}"' "$DOCKER_YAML")" "2"
# An empty lane array must contribute ZERO argv words, keeping every
# no-lane docker repo's git-cliff command argument-identical (same
# expansion form the workflows use; bash >= 4.4 drops the empty array
# under set -u).
LANE_ARGS=()
set -- git-cliff --unreleased --tag v1.2.3 "${LANE_ARGS[@]}" --strip header
chk "L-F4 empty LANE_ARGS contributes zero argv words" "$#" "6"

echo "PASS: nested-module lane shell semantics match the pinned contract (${PASS} checks)"
