#!/usr/bin/env bash
# Regression probe for the nested-Go-module lane SHELL semantics that
# release.yaml (detect: gomodules + changed-path classification, go-nested:
# module-path verify + version guards + regex escaping) and ci.yaml (detect:
# nested validation discovery) embed inline. The classification step is
# extracted from release.yaml and executed in fixture repos; the other
# function bodies below MIRROR the workflow snippets, so when editing either
# side update the other in the same change. Runs in the ci repo's `scripts`
# CI job (opt-in by file presence).
set -euo pipefail

# Hermetic git, same rationale as test-cliff-bump-semantics.sh: a global
# `tag.gpgsign true` (or any other workstation config) must not reach the
# throwaway repos this probe builds.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

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

# release.yaml detect/changes is EXTRACTED and executed, not mirrored: the
# classification reads git tags and diffs, which a mirror could only fake.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE_YAML="$ROOT/.github/workflows/release.yaml"
python3 - "$RELEASE_YAML" "$WORK" <<'PY'
import sys, yaml

release, out = sys.argv[1], sys.argv[2]
jobs = yaml.safe_load(open(release))["jobs"]
step = next(s for s in jobs["detect"]["steps"] if s.get("name") == "Detect changed paths")
open(f"{out}/changes.sh", "w").write(step["run"])
PY
classify() { # <channel> <anchor> <head> -> root=<bool> lanes=<json>; env SUBPACKAGES_JSON GO_LANES_JSON; run in a repo
  : >"$WORK/out"
  : >"$WORK/summary"
  CI_TOOLS="$ROOT/scripts" CHANNEL="$1" BEFORE="$2" ANCHOR_SHA="$2" HEAD="$3" REPO_TYPE=go \
    GITHUB_OUTPUT="$WORK/out" GITHUB_STEP_SUMMARY="$WORK/summary" \
    bash "$WORK/changes.sh" >"$WORK/changes.log" 2>&1 || echo "EXIT=$?"
  echo "root=$(sed -n 's/^root_changed=//p' "$WORK/out") lanes=$(sed -n 's/^go_modules_to_release=//p' "$WORK/out")"
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
# No lane carries a tag yet, so every lane is judged on the root range: its
# first release is owed by whatever that range shows under it.
commit() { # <message> <path>... -> sha; touches each path with a fresh line
  local p
  for p in "${@:2}"; do
    mkdir -p "$R/$(dirname "$p")"
    echo "$1" >>"$R/$p"
  done
  git -C "$R" add -A
  git -C "$R" commit -qm "$1"
  git -C "$R" rev-parse HEAD
}
mkrepo classify
put go.mod "module github.com/cplieger/envx" "go 1.26.5"
put yamlenv/go.mod "module github.com/cplieger/envx/yamlenv" "go 1.26.5"
put tools/gen/go.mod "module github.com/cplieger/envx/tools/gen" "go 1.26.5"
put web/jsr.json '{"name":"@o/web","version":"1.0.0"}'
export SUBPACKAGES_JSON='["web"]'
export GO_LANES_JSON='["yamlenv","tools/gen"]'
K0=$(commit "feat: initial" envx.go yamlenv/yamlenv.go tools/gen/x.go web/index.ts yamlenv2/file.go)
git -C "$R" tag v1.0.0 "$K0"
K1=$(commit "fix: lane" yamlenv/yamlenv.go)
K2=$(commit "fix: root" envx.go)
K3=$(commit "fix: mixed" envx.go yamlenv/go.mod tools/gen/x.go)
K4=$(commit "fix: web" web/index.ts)
K5=$(commit "fix: root sibling" yamlenv2/file.go)
K6=$(commit "docs: readme" README.md)
cd "$R" || exit 1
chk "L-B1 lane-only change" "$(classify dev "$K0" "$K1")" 'root=false lanes=["yamlenv"]'
chk "L-B2 root-only change" "$(classify dev "$K1" "$K2")" 'root=true lanes=[]'
chk "L-B3 mixed change" "$(classify dev "$K2" "$K3")" 'root=true lanes=["yamlenv","tools/gen"]'
chk "L-B4 subpackage change is neither root nor lane" "$(classify dev "$K3" "$K4")" 'root=false lanes=[]'
chk "L-B5 lane prefix is dir-anchored (yamlenv2/ is root)" "$(classify dev "$K4" "$K5")" 'root=true lanes=[]'
chk "L-B6 empty significant set" "$(classify dev "$K5" "$K6")" 'root=false lanes=[]'
cd "$WORK" || exit 1

# ── Lane anchor states ───────────────────────────────────────────────────────
# A lane with a tag on this channel is measured from that tag, not from the
# root anchor, which ignores lane tags and so still predates a lane-only
# release. compute.sh's nearest_tag is extracted and run on the same fixture,
# so the lane job's anchor and detect's cannot disagree.
awk '/^nearest_tag\(\) \{/,/^}/' "$ROOT/actions/git-cliff-version/compute.sh" >"$WORK/nearest_tag.sh"
lane_anchor_of_compute() { # <channel> <lane> -> compute.sh's anchor_sha for the lane at HEAD; run in a repo
  local esc pattern dev_pattern tag
  esc=$(printf '%s' "$2" | sed -e 's/[][\.|(){}?+*^$]/\\&/g')
  pattern="^${esc}/v[0-9]+\\.[0-9]+\\.[0-9]+\$"
  dev_pattern="${pattern%\$}-dev\\.[0-9]+\$"
  # The function body extracted above, not a script of its own.
  # shellcheck source=/dev/null
  . "$WORK/nearest_tag.sh"
  if [ "$1" = dev ]; then
    tag=$(nearest_tag "${pattern}|${dev_pattern}")
  else
    tag=$(nearest_tag "$pattern")
  fi
  [ -n "$tag" ] && git rev-list -n1 "$tag"
}
detect_lane_anchor() { # <lane> -> the anchor detect's notice line names for the lane
  sed -n "s/^::notice::lane $1: deriving changed paths from \([0-9a-f]*\)\.\.HEAD.*/\1/p" "$WORK/changes.log"
}
mkrepo anchored
put go.mod "module github.com/cplieger/envx" "go 1.26.5"
put yamlenv/go.mod "module github.com/cplieger/envx/yamlenv" "go 1.26.5"
export SUBPACKAGES_JSON='[]'
export GO_LANES_JSON='["yamlenv"]'
G_A=$(commit "feat: initial" main.go yamlenv/y.go README.md)
git -C "$R" tag v1.0.0 "$G_A"
git -C "$R" tag yamlenv/v1.0.0 "$G_A"
G_B=$(commit "feat(yamlenv): lane change" yamlenv/y.go)
cd "$R" || exit 1
chk "L-G1 the lane change since its own tag is owed on dev" "$(classify dev "$G_A" "$G_B")" 'root=false lanes=["yamlenv"]'
git tag yamlenv/v1.1.0-dev.1 "$G_B"
G_C=$(commit "docs: readme only" README.md)
chk "L-G2 a later docs-only commit does not republish the tagged lane" "$(classify dev "$G_A" "$G_C")" 'root=false lanes=[]'
chk "L-G2 detect measured the lane from its own dev tag" "$(detect_lane_anchor yamlenv)" "$G_B"
chk "L-G2 compute.sh's lane anchor is the same commit" "$(lane_anchor_of_compute dev yamlenv)" "$G_B"
G_D=$(commit "fix: root only" main.go)
chk "L-G3 a root-only commit above the lane tag releases the root alone" "$(classify dev "$G_A" "$G_D")" 'root=true lanes=[]'
G_E=$(commit "fix(yamlenv): second lane change" yamlenv/y.go)
chk "L-G4 a new lane change since the lane tag is owed again" "$(classify dev "$G_A" "$G_E")" 'root=true lanes=["yamlenv"]'
chk "L-G5 on stable the dev tag is no anchor: the lane is owed since yamlenv/v1.0.0" "$(classify stable "$G_A" "$G_C")" 'root=false lanes=["yamlenv"]'
chk "L-G5 compute.sh agrees on the stable anchor" "$(lane_anchor_of_compute stable yamlenv)" "$G_A"
git tag yamlenv/v1.1.0-dev.2 "$G_E"
G_F=$(commit "docs(yamlenv): lane readme" yamlenv/README.md)
chk "L-G6 an excluded path under the lane is not a lane change" "$(classify dev "$G_A" "$G_F")" 'root=true lanes=[]'
git tag yamlenv/v1.1.0 "$G_F"
chk "L-G7 on stable a lane tag at this commit is emitted for the Release repair" "$(classify stable "$G_A" "$G_F")" 'root=true lanes=["yamlenv"]'
chk "L-G7 on dev a lane tag at this commit has nothing to do" "$(classify dev "$G_A" "$G_F")" 'root=true lanes=[]'
cd "$WORK" || exit 1

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
DOCKER_YAML="$ROOT/.github/workflows/docker-release.yaml"
# shellcheck disable=SC2016 # the ${{ }} is a literal GitHub expression, not shell
chk "L-F1 release.yaml forwards detect's go_modules to docker-release" \
  "$(grep -c 'go-modules: ${{ needs.detect.outputs.go_modules }}' "$RELEASE_YAML")" "1"
chk "L-F2 docker-release go-modules input defaults to '[]'" \
  "$(awk '/^      go-modules:$/ { f = 1; next }
    f && /^      [a-z-]+:$/ { exit }
    f && $1 == "default:" { print $2; exit }' "$DOCKER_YAML")" '"[]"'
# shellcheck disable=SC2016 # literal single-quoted grep pattern, no expansion wanted
chk "L-F3 the docker notes step hands the lanes to render-notes.sh" \
  "$(grep -c -- '--go-lanes "$GO_LANES_JSON"' "$DOCKER_YAML")" "1"
# shellcheck disable=SC2016 # literal single-quoted grep pattern, no expansion wanted
chk "L-F3 and render-notes.sh scopes both models' git-cliff calls by them" \
  "$(grep -c '"\${SCOPE_ARGS\[@\]}"' "$ROOT/scripts/render-notes.sh")" "2"
# An empty lane array must contribute ZERO argv words, keeping every
# no-lane docker repo's git-cliff command argument-identical (same
# expansion form the renderer uses; bash >= 4.4 drops the empty array
# under set -u).
LANE_ARGS=()
set -- git-cliff --unreleased --tag v1.2.3 "${LANE_ARGS[@]}" --strip header
chk "L-F4 empty LANE_ARGS contributes zero argv words" "$#" "6"

echo "PASS: nested-module lane shell semantics match the pinned contract (${PASS} checks)"
