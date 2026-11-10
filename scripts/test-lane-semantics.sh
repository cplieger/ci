#!/usr/bin/env bash
# Regression probe for the shell semantics release.yaml and ci.yaml embed
# inline: nested-Go-module lanes (release.yaml detect and go-nested, ci.yaml's
# nested validation discovery), ci.yaml's web-frontend probe, its go-os
# runner detection and its pull-request module scoping. A snippet not
# extracted below is MIRRORED, so editing either side updates the other in
# the same change. Runs in the ci repo's `scripts` CI job (opt-in by file
# presence).
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
    d=$(dirname -- "$f")
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
    if [ -z "$(git ls-files -- "$d/*.go" | head -1)" ]; then
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
    d=$(dirname -- "$f")
    case "$d" in
      *[!A-Za-z0-9._/-]*)
        echo "CHARSET_ERROR"
        return 0
        ;;
    esac
    if [ -z "$(git ls-files -- "$d/*.go" | head -1)" ]; then
      continue
    fi
    DIRS=$(echo "$DIRS" | jq -c --arg v "$d" '. + [$v]')
  done < <(git ls-files '*/go.mod' | grep -Ev '(^|/)(node_modules|vendor|testdata|static|dist)/' || true)
  echo "$DIRS"
}

# release.yaml detect/changes, ci.yaml's web-frontend probe loop and its go-os
# block are EXTRACTED and executed: the classification reads git tags and diffs
# a mirror could only fake, and a mirrored folder list or file rule would pass
# after ci.yaml's changed.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE_YAML="$ROOT/.github/workflows/release.yaml"
CI_YAML="$ROOT/.github/workflows/ci.yaml"
python3 - "$RELEASE_YAML" "$CI_YAML" "$WORK" <<'PY'
import sys, textwrap, yaml

release, ci, out = sys.argv[1], sys.argv[2], sys.argv[3]
jobs = yaml.safe_load(open(release))["jobs"]
step = next(s for s in jobs["detect"]["steps"] if s.get("name") == "Detect changed paths")
open(f"{out}/changes.sh", "w").write(step["run"])

jobs = yaml.safe_load(open(ci))["jobs"]
lines = next(s for s in jobs["detect"]["steps"] if s.get("name") == "Detect repo surfaces")["run"].splitlines()
start = next(i for i, l in enumerate(lines) if l.strip().startswith("# Probe known nested web-frontend locations"))
end = next(i for i in range(start, len(lines)) if lines[i].strip() == "done")
open(f"{out}/web-probe.sh", "w").write(textwrap.dedent("\n".join(lines[start : end + 1])) + "\n")

start = next(i for i, l in enumerate(lines) if l.strip().startswith("# A module tracking a Go file built for windows or darwin"))
end = next(i for i in range(start, len(lines)) if lines[i].strip().startswith("go_os_matrix=$(jq -c 'unique_by"))
open(f"{out}/go-os.sh", "w").write(textwrap.dedent("\n".join(lines[start : end + 1])) + "\n")

start = next(i for i, l in enumerate(lines) if l.strip().startswith("# A module requiring a sibling by version"))
end = next(i for i in range(start, len(lines)) if lines[i].strip() == 'done <<< "$mod_dirs"')
open(f"{out}/go-work.sh", "w").write(textwrap.dedent("\n".join(lines[start : end + 1])) + "\n")

start = next(i for i, l in enumerate(lines) if l.strip().startswith("# On a pull request in a repository of modules"))
indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
end = next(i for i in range(start + 1, len(lines)) if lines[i] == indent + "fi")
open(f"{out}/module-scope.sh", "w").write(textwrap.dedent("\n".join(lines[start : end + 1])) + "\n")
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
  mkdir -p "$R/$(dirname -- "$1")"
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

mkrepo dash
put go.mod "module github.com/cplieger/dash" "go 1.26.5"
put m.go "package main"
put -h/go.mod "module github.com/cplieger/dash/-h" "go 1.26.5"
put -h/h.go "package h"
git -C "$R" add -A
git -C "$R" commit -qm init
chk "L-A8 a module dir starting with a dash is a lane, not an option (release)" \
  "$(cd "$R" && discover_release)" '["-h"]'
chk "L-A9 a module dir starting with a dash is validated (ci)" "$(cd "$R" && discover_ci)" '["-h"]'

# ── Web-frontend probe states ────────────────────────────────────────────────
web_probe() {
  local web_dir="" run_web=false d
  cd "$1" || exit 1
  # shellcheck source=/dev/null
  . "$WORK/web-probe.sh"
  echo "web_dir=$web_dir run_web=$run_web"
}
mkweb() {
  W="$WORK/web-$1"
  local p
  for p in "${@:2}"; do
    mkdir -p "$W/$(dirname "$p")"
    echo '{}' >"$W/$p"
  done
}
mkweb client internal/webui/static-src/package.json
chk "L-W1 internal/webui/static-src/package.json is the web frontend" \
  "$(web_probe "$W")" "web_dir=internal/webui/static-src run_web=true"
mkweb both internal/webui/static-src/package.json static-src/package.json
chk "L-W2 static-src is probed first" "$(web_probe "$W")" "web_dir=static-src run_web=true"
mkweb jsr internal/webui/static-src/jsr.json
chk "L-W4 a jsr.json alone marks the frontend" \
  "$(web_probe "$W")" "web_dir=internal/webui/static-src run_web=true"
mkweb bare internal/webui/static-src/app.ts
chk "L-W3 a folder with neither package.json nor jsr.json is no frontend" \
  "$(web_probe "$W")" "web_dir= run_web=false"

# ── go-os runner detection states ────────────────────────────────────────────
# Expected rows follow Go's build constraints (go help buildconstraint): a file
# name whose part before the first dot ends _GOOS, _GOOS_GOARCH with a known
# GOARCH, or either plus _test, or a //go:build line above the package clause
# naming the OS outside an odd number of negations; Go ignores a name starting
# with _ or . below the module folder (go help packages). mod_dirs is MIRRORED from
# ci.yaml's detect (nested validation discovery plus the root go.mod).
# The block runs in a fresh bash with the runner's options (bash -eo pipefail),
# because a command substitution here would run it with errexit off.
cat >"$WORK/go-os-run.sh" <<'SH'
mod_dirs=$1
go_os_matrix="[]"
# shellcheck source=/dev/null
. "$2"
jq -r 'sort_by([.dir, .runner]) | map("\(.dir)=\(.runner)") | join(" ")' <<<"$go_os_matrix"
SH
os_rows() { # sets rows to <dir>=<runner>... for the repo at $R
  local mod_dirs
  mod_dirs=$(cd "$R" && discover_ci | jq -r '.[]')
  if [ -f "$R/go.mod" ]; then mod_dirs=$(printf '.\n%s' "$mod_dirs"); fi
  rows=$(cd "$R" && bash -eo pipefail "$WORK/go-os-run.sh" "$mod_dirs" "$WORK/go-os.sh") \
    || fail "ci.yaml's go-os block failed in $R"
}
commit_all() {
  git -C "$R" add -A
  git -C "$R" commit -qm init
}

mkrepo goos
put go.mod "module github.com/cplieger/goos" "go 1.26.5"
put root.go "package goos"
put root_darwin.go "package goos"
put win/go.mod "module github.com/cplieger/goos/win" "go 1.26.5"
put win/w_windows.go "package win"
put win/sub/deep_windows.go "package sub"
put mac/go.mod "module github.com/cplieger/goos/mac" "go 1.26.5"
put mac/m.go "package mac"
put mac/m_darwin_test.go "package mac"
put arch/go.mod "module github.com/cplieger/goos/arch" "go 1.26.5"
put arch/a.go "package arch"
put arch/a_windows_arm64.go "package arch"
put archtest/go.mod "module github.com/cplieger/goos/archtest" "go 1.26.5"
put archtest/a.go "package archtest"
put archtest/a_darwin_arm64_test.go "package archtest"
put tag/go.mod "module github.com/cplieger/goos/tag" "go 1.26.5"
put tag/t.go "//go:build windows" "" "package tag"
put both/go.mod "module github.com/cplieger/goos/both" "go 1.26.5"
put both/b.go "//go:build (darwin && arm64) || windows" "" "package both"
commit_all
os_rows
chk "L-O1 each module gets the runner of the OS files it tracks; a nested module's file stays its own" \
  "$rows" \
  ".=macos-latest arch=windows-latest archtest=macos-latest both=macos-latest both=windows-latest mac=macos-latest tag=windows-latest win=windows-latest"

mkrepo goos-none
put go.mod "module github.com/cplieger/none" "go 1.26.5"
put main.go "package main"
put main_linux.go "package main"
put main_test.go "package main"
put windows.go "package main"
put str.go "package main" "" 'const s = "//go:build windows"'
put raw.go "package main" "" 'const r = `' "//go:build windows" '`'
put x_windows_helper.go "package main"
put x_darwin_cgo_test.go "package main"
put b.c_windows.go "package main"
put _x_windows.go "package main"
put .y_darwin.go "package main"
put _h.go "//go:build windows" "" "package main"
put _gen/g_windows.go "package gen"
put sub/_z_windows.go "package sub"
put p/.cache/c.go "//go:build darwin" "" "package cache"
commit_all
os_rows
chk "L-O2 no OS-named file or header constraint: no OS leg" "$rows" ""

mkrepo goos-cut
put go.mod "module github.com/cplieger/cut" "go 1.26.5"
put main.go "package main"
put x_windows.pb.go "package main"
put static/s_darwin.go "package static"
put dist/d.go "//go:build windows" "" "package dist"
put _tools/go.mod "module github.com/cplieger/cut/tools" "go 1.26.5"
put _tools/t_darwin.go "package tools"
commit_all
os_rows
chk "L-O6 Go's name cut at the first dot, static and dist trees, and a module in a _ folder" \
  "$rows" ".=macos-latest .=windows-latest _tools=macos-latest"

mkrepo goos-negated
put go.mod "module github.com/cplieger/negated" "go 1.26.5"
put n.go "//go:build !windows" "" "package negated"
put o.go "//go:build !(linux || darwin || windows)" "" "package negated"
put k.go "//go:build unix && !darwin" "" "package negated"
put l.go "//go:build linux && !(freebsd || (darwin && arm64))" "" "package negated"
commit_all
os_rows
chk "L-O3 an OS named only under negation gains no leg" "$rows" ""

mkrepo goos-vendored
put go.mod "module github.com/cplieger/vendored" "go 1.26.5"
put main.go "package main"
put vendor/x/v_windows.go "package x"
put vendor/q/q.go "//go:build darwin" "" "package q"
put testdata/t_windows.go "package t"
put node_modules/y/go.mod "module github.com/vendored/y" "go 1.0"
put node_modules/y/y_darwin.go "package y"
commit_all
os_rows
chk "L-O4 vendored and testdata trees gain no leg" "$rows" ""

mkrepo goos-orphan
put tool/go.mod "module github.com/cplieger/orphan/tool" "go 1.26.5"
put tool/t.go "package tool"
put tool/t_darwin.go "package tool"
put hack/x_windows.go "package hack"
commit_all
put tool/u_windows.go "package tool"
os_rows
chk "L-O5 a file under no module, or untracked, gains no leg" "$rows" "tool=macos-latest"

# go_build_positive_terms decides polarity alone; each expression pins one rule.
awk '/^go_build_positive_terms\(\) \{/,/^}/' "$WORK/go-os.sh" >"$WORK/positive-terms.sh"
[ -s "$WORK/positive-terms.sh" ] || fail "go_build_positive_terms not found in ci.yaml's go-os block"
# shellcheck source=/dev/null
. "$WORK/positive-terms.sh"
positive() { go_build_positive_terms "$1" | sort | paste -sd' ' -; }
chk "L-P1 a bare term" "$(positive 'windows')" "windows"
chk "L-P2 a negated term" "$(positive '!windows')" ""
chk "L-P3 a double negation" "$(positive '!!windows')" "windows"
chk "L-P4 an alternation" "$(positive 'linux || darwin')" "darwin linux"
chk "L-P5 a negated group" "$(positive '!(linux || darwin)')" ""
chk "L-P6 a negation inside a negated group" "$(positive '!(!windows)')" "windows"
chk "L-P7 nested groups" "$(positive 'a && !(b || !(c && darwin))')" "a c darwin"
chk "L-P8 a version term" "$(positive 'go1.21 && windows')" "go1.21 windows"

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
chk "L-F3 and render-notes.sh scopes each git-cliff read of the range by them" \
  "$(grep -c '"$CLIFF" --tag-pattern' "$ROOT/scripts/render-notes.sh")|$(grep '"$CLIFF" --tag-pattern' "$ROOT/scripts/render-notes.sh" | grep -vc '"\${SCOPE_ARGS\[@\]}"')" "2|0"
# An empty lane array must contribute ZERO argv words, keeping every
# no-lane docker repo's git-cliff command argument-identical (same
# expansion form the renderer uses; bash >= 4.4 drops the empty array
# under set -u).
LANE_ARGS=()
set -- git-cliff --unreleased --tag v1.2.3 "${LANE_ARGS[@]}" --strip header
chk "L-F4 empty LANE_ARGS contributes zero argv words" "$#" "6"

# ── module scoping on a pull request ─────────────────────────────────────────
# ci.yaml's detect narrows go-nested, go-os and ts-nested to the modules a
# changed path belongs to, on a pull request in a repo with no root go.mod. Each
# go-nested and go-os leg builds with GOWORK=off against its siblings' tags, so
# no other module's result can change there; any non-doc path outside every
# module, a replace of a sibling or a symlink leaving its module keeps
# them all, as does a push.
cat >"$WORK/scope-run.sh" <<'SH'
scope_modules=$1
changed=$2
sibling_replaced=false
go_nested_dirs=${NESTED:-'["filesafe","httpclient","keyenc","measure","ssrf"]'}
if [ "$4" = true ]; then
  # shellcheck source=/dev/null
  . "$(dirname "$3")/go-work.sh"
fi
go_os_matrix='[{"dir":"filesafe","runner":"windows-latest"},{"dir":"measure","runner":"macos-latest"}]'
ts_nested_dirs='["keyenc/web"]'
run_go_nested=true
run_go_os=true
run_ts_nested=true
# shellcheck source=/dev/null
. "$3" >/dev/null
printf 'nested=%s os=%s ts=%s run=%s,%s,%s\n' "$go_nested_dirs" "$(jq -c 'map(.dir)' <<<"$go_os_matrix")" \
  "$ts_nested_dirs" "$run_go_nested" "$run_go_os" "$run_ts_nested"
SH
scope() { # <scope_modules> <changed paths, one per line> [root|symlink|inside|absolute|loop|quoted|replaced|required|unrequired|crlf]
  local d
  d=$(mktemp -d "$WORK/scope.XXXXXX")
  git -C "$d" init -q
  case "${3:-}" in
    root) : >"$d/go.mod" ;;
    symlink)
      mkdir -p "$d/measure" "$d/ssrf"
      : >"$d/ssrf/a.go"
      ln -s ../ssrf/a.go "$d/measure/a.go"
      git -C "$d" add -A
      ;;
    replaced | required | unrequired)
      for m in filesafe httpclient keyenc measure ssrf; do
        mkdir -p "$d/$m"
        printf 'module example.com/r/%s\n' "$m" >"$d/$m/go.mod"
      done
      if [ "$3" != unrequired ]; then printf 'require example.com/r/ssrf v0.1.0\n' >>"$d/measure/go.mod"; fi
      if [ "$3" != required ]; then printf 'replace example.com/r/ssrf => ../ssrf\n' >>"$d/measure/go.mod"; fi
      ;;
    crlf)
      for m in filesafe httpclient keyenc measure ssrf; do
        mkdir -p "$d/$m"
        printf 'module example.com/r/%s\r\n' "$m" >"$d/$m/go.mod"
      done
      printf 'replace (\r\n\texample.com/r/ssrf => ../ssrf\r\n)\r\n' >>"$d/measure/go.mod"
      ;;
    absolute)
      mkdir -p "$d/measure" "$d/ssrf"
      : >"$d/ssrf/a.go"
      ln -s "$d/ssrf/a.go" "$d/measure/a.go"
      git -C "$d" add -A
      ;;
    loop)
      mkdir -p "$d/measure"
      ln -s loop "$d/measure/loop"
      git -C "$d" add -A
      ;;
    quoted)
      mkdir -p "$d/measure" "$d/ssrf"
      : >"$d/ssrf/a.go"
      ln -s ../ssrf/a.go "$d/measure/é.go"
      git -C "$d" add -A
      ;;
    inside)
      mkdir -p "$d/measure/sub"
      : >"$d/measure/a.go"
      ln -s ../a.go "$d/measure/sub/b.go"
      git -C "$d" add -A
      ;;
  esac
  local replaced=false
  case "${3:-}" in replaced | required | unrequired | crlf) replaced=true ;; esac
  (cd "$d" && bash -eo pipefail "$WORK/scope-run.sh" "$1" "$2" "$WORK/module-scope.sh" "$replaced") \
    || fail "ci.yaml's module-scope block failed for '$2'"
}
ALL='nested=["filesafe","httpclient","keyenc","measure","ssrf"] os=["filesafe","measure"] ts=["keyenc/web"] run=true,true,true'
chk "M-S1 a change inside one module validates that module alone" \
  "$(scope true 'measure/exposition.go')" 'nested=["measure"] os=["measure"] ts=[] run=true,true,false'
chk "M-S2 a doc file outside every module does not widen the scope" \
  "$(scope true $'measure/exposition.go\nREADME.md\nLICENSE')" 'nested=["measure"] os=["measure"] ts=[] run=true,true,false'
chk "M-S3 a non-doc path outside every module keeps every module" \
  "$(scope true $'measure/exposition.go\n.golangci.yaml')" "$ALL"
chk "M-S4 a change to a module's web half validates the module and the half" \
  "$(scope true 'keyenc/web/src/keyenc.ts')" 'nested=["keyenc"] os=[] ts=["keyenc/web"] run=true,false,true'
chk "M-S5 a package below a module belongs to that module" \
  "$(scope true $'httpclient/certtest/certtest.go\nssrf/README.md')" 'nested=["httpclient","ssrf"] os=[] ts=[] run=true,false,false'
chk "M-S6 a path in a folder that is no longer a module keeps every module" \
  "$(scope true 'oldmod/old.go')" "$ALL"
chk "M-S7 a push keeps every module" "$(scope false 'measure/exposition.go')" "$ALL"
chk "M-S8 a repo with a root go.mod keeps every module" "$(scope true 'measure/exposition.go' root)" "$ALL"
chk "M-S9 detect lists both sides of a rename, so a moved file's old module is validated" \
  "$(grep -c 'git diff --name-only --no-renames' "$CI_YAML")" "2"
chk "M-S10 a sibling replaced by a local path keeps every module" \
  "$(scope true 'measure/exposition.go' replaced)" "$ALL"
chk "M-S11 a symlink into another module keeps every module" \
  "$(scope true 'ssrf/a.go' symlink)" "$ALL"
chk "M-S12 a symlink inside its own module keeps the scope" \
  "$(scope true 'measure/a.go' inside)" 'nested=["measure"] os=["measure"] ts=[] run=true,true,false'
chk "M-S13 a web half that is itself a module keeps its ts-nested leg" \
  "$(NESTED='["filesafe","keyenc","keyenc/web","measure"]' scope true 'keyenc/web/src/keyenc.ts')" \
  'nested=["keyenc/web"] os=[] ts=["keyenc/web"] run=true,false,true'
# shellcheck disable=SC2016 # literal single-quoted grep pattern, no expansion wanted
chk "M-S9 and self-release lists both sides of a rename since the last tag" \
  "$(grep -c 'git diff --name-only --no-renames "\${LAST_TAG}..HEAD"' "$ROOT/.github/workflows/self-release.yaml")" "1"
chk "M-S10 and a sibling required by version alone keeps the scope" \
  "$(scope true 'measure/exposition.go' required)" 'nested=["measure"] os=["measure"] ts=[] run=true,true,false'
chk "M-S14 a symlink loop does not fail detect" "$(scope true 'measure/a.go' loop | wc -l)" "1"
# A realpath that fails, as uutils' does on a loop, keeps every module on any host.
mkdir -p "$WORK/realpath-fails"
printf '#!/bin/sh\nexit 1\n' >"$WORK/realpath-fails/realpath"
chmod +x "$WORK/realpath-fails/realpath"
chk "M-S14 and a realpath failure keeps every module" \
  "$(PATH="$WORK/realpath-fails:$PATH" scope true 'measure/a.go' inside)" "$ALL"
chk "M-S15 a symlink whose name git quotes still keeps every module" \
  "$(scope true 'ssrf/a.go' quoted)" "$ALL"
chk "M-S16 an absolute symlink into another module keeps every module" \
  "$(scope true 'ssrf/a.go' absolute)" "$ALL"
chk "M-S10 and a replaced sibling the module does not require keeps every module" \
  "$(scope true 'measure/exposition.go' unrequired)" "$ALL"
chk "M-S10 and a replace block in a CRLF go.mod keeps every module" \
  "$(scope true 'measure/exposition.go' crlf)" "$ALL"

echo "PASS: nested-module lane, web-probe, go-os detection and module-scope shell semantics match the pinned contract (${PASS} checks)"
