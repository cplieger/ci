#!/usr/bin/env bash
# Regression probe for scripts/path-significance.sh and the ci-source
# checkout that ships it. The workflow step body is EXTRACTED and executed in
# fixture repositories, so the chain under test is the one that ships.
#
# PS_BASELINE=<file> also runs every release-mode fixture through that file
# (an older inline step body) and requires identical outputs, summary and log.
set -euo pipefail

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=probe GIT_AUTHOR_EMAIL=probe@example.invalid
export GIT_COMMITTER_NAME=probe GIT_COMMITTER_EMAIL=probe@example.invalid

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
chk_has() { # label haystack needle
  case "$2" in
    *"$3"*)
      PASS=$((PASS + 1))
      echo "ok: $1"
      ;;
    *) fail "$1: output does not contain '$3'
--- output ---
$2" ;;
  esac
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/path-significance-probe.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

python3 - "$ROOT" "$WORK" <<'PY'
import json, sys, yaml

root, out = sys.argv[1], sys.argv[2]
steps = yaml.safe_load(open(f"{root}/.github/workflows/release.yaml"))["jobs"]["detect"]["steps"]
names = [s.get("name") for s in steps]
changes = steps[names.index("Detect changed paths")]
src = steps[names.index("Check out the ci source")]
open(f"{out}/changes.sh", "w").write(changes["run"])
open(f"{out}/changes.tools", "w").write(changes["env"]["CI_TOOLS"])
open(f"{out}/changes.model", "w").write(changes["env"].get("RELEASE_MODEL", ""))
open(f"{out}/src.with", "w").write(json.dumps(src["with"], sort_keys=True))
open(f"{out}/src.order", "w").write(str(names.index("Check out the ci source") < names.index("Detect changed paths")))
PY

# ── The ci-source checkout ───────────────────────────────────────────────────
chk "P1 detect checks out the ci source before it detects" "$(cat "$WORK/src.order")" "True"
# shellcheck disable=SC2016 # literal workflow expressions
chk "P1 at the workflow's own commit, beside the consumer's tree, without credentials" "$(cat "$WORK/src.with")" \
  '{"path": ".cplieger-ci", "persist-credentials": false, "ref": "${{ job.workflow_sha }}", "repository": "${{ job.workflow_repository }}"}'
# shellcheck disable=SC2016
chk "P1 and hands the step that copy's scripts" "$(cat "$WORK/changes.tools")" '${{ github.workspace }}/.cplieger-ci/scripts'
# shellcheck disable=SC2016
chk "P1 and the release model detect selected" "$(cat "$WORK/changes.model")" '${{ steps.channel.outputs.release_model }}'
TOOLS="$ROOT/scripts"

# ── Fixture helpers ──────────────────────────────────────────────────────────
put() { # <path> <content>
  mkdir -p "$(dirname "$1")"
  printf '%s\n' "$2" >"$1"
}
commit() { # <message> -> sha of a commit of everything staged and unstaged
  git add -A
  git commit -qm "$1" --allow-empty
  git rev-parse HEAD
}
newrepo() { # <name>
  R="$WORK/$1"
  git init -q -b main "$R"
  cd "$R" || exit 1
}
run_release() { # <body> -> outputs in $WORK/out, summary in $WORK/summary, log in $WORK/log
  : >"$WORK/out"
  : >"$WORK/summary"
  CI_TOOLS="$TOOLS" GITHUB_OUTPUT="$WORK/out" GITHUB_STEP_SUMMARY="$WORK/summary" \
    bash "$1" >"$WORK/log" 2>&1 || echo "EXIT=$?" >>"$WORK/log"
}
# release <label> <channel> <before> <anchor> <head> -> root|subs|lanes; env REPO_TYPE SUBPACKAGES_JSON GO_LANES_JSON
release() {
  export CHANNEL="$2" BEFORE="$3" ANCHOR_SHA="$4" HEAD="$5"
  run_release "$WORK/changes.sh"
  if [ -n "${PS_BASELINE:-}" ] && [ "${RELEASE_MODEL:-legacy}" = legacy ]; then
    cp "$WORK/out" "$WORK/out.new"
    cp "$WORK/summary" "$WORK/summary.new"
    cp "$WORK/log" "$WORK/log.new"
    run_release "$PS_BASELINE"
    for k in out summary log; do
      diff -u "$WORK/$k" "$WORK/$k.new" >&2 || fail "$1: $k differs from PS_BASELINE"
    done
    echo "ok: $1 matches PS_BASELINE byte for byte" >&2
    echo "$1" >>"$WORK/baseline.matched"
  fi
  grep -q '^EXIT=' "$WORK/log" && fail "$1: the step failed: $(cat "$WORK/log")"
  printf '%s|%s|%s' "$(sed -n 's/^root_changed=//p' "$WORK/out")" \
    "$(sed -n 's/^subpackages_to_publish=//p' "$WORK/out")" "$(sed -n 's/^go_modules_to_release=//p' "$WORK/out")"
}
# paths <from> <to> -> root|subs|lanes|significant from MODE=paths stdout
paths() {
  MODE=paths FROM="$1" TO="$2" bash "$TOOLS/path-significance.sh" >"$WORK/pout" 2>"$WORK/perr" || echo "EXIT=$?" >>"$WORK/perr"
  grep -q '^EXIT=' "$WORK/perr" && fail "paths $1..$2 failed: $(cat "$WORK/perr")"
  printf '%s|%s|%s|%s' "$(sed -n 's/^root_changed=//p' "$WORK/pout")" "$(sed -n 's/^subpackages_to_publish=//p' "$WORK/pout")" \
    "$(sed -n 's/^go_modules_to_release=//p' "$WORK/pout")" "$(sed -n 's/^significant=//p' "$WORK/pout")"
}

# ── A docker repo: docs, lockfile, package.json, empty commit, dispatch ──────
export REPO_TYPE=docker SUBPACKAGES_JSON='[]' GO_LANES_JSON='[]'
newrepo docker
put Dockerfile 'FROM scratch'
put main.go 'package main'
put package.json '{"name":"tools","devDependencies":{"a":"1"},"dependencies":{"b":"1"}}'
put package-lock.json '{}'
D0=$(commit "feat: initial")
put README.md 'docs'
put docs/shot.png 'png'
D1=$(commit "docs: readme and screenshot")
chk "S1 a docs-only push publishes nothing" "$(release S1 stable "$D0" "$D0" "$D1")" "false|[]|[]"
chk_has "S1 the summary says only excluded paths changed" "$(cat "$WORK/summary")" "(none — only excluded paths changed)"
put package-lock.json '{"lock":2}'
D2=$(commit "chore(deps): lock file maintenance")
chk "S2 a lockfile-only push publishes nothing" "$(release S2 stable "$D1" "$D1" "$D2")" "false|[]|[]"
put package.json '{"name":"tools","devDependencies":{"a":"2"},"dependencies":{"b":"2"}}'
D3=$(commit "fix(deps): bump b")
chk "S3 a docker repo's package.json is never a published manifest" "$(release S3 stable "$D2" "$D2" "$D3")" "false|[]|[]"
chk_has "S3 and the log says so" "$(cat "$WORK/log")" "package.json is not a published manifest"
D4=$(commit "fix: rebuild for base packages")
chk "S4 an empty commit is a root change" "$(release S4 stable "$D3" "$D3" "$D4")" "true|[]|[]"
chk_has "S4 the summary names it" "$(cat "$WORK/summary")" "(commit $D4 changes no file and counts as a root change)"
chk "S5 a dispatch (no before) counts every tracked file" "$(release S5 stable "" "$D3" "$D4")" "true|[]|[]"
chk_has "S5 and says so" "$(cat "$WORK/log")" "No before SHA"
chk "S5 so does an all-zero before" "$(release S5z stable 0000000000000000000000000000000000000000 "$D3" "$D4")" "true|[]|[]"
chk "S6 an unreachable before counts every tracked file" \
  "$(release S6 stable 0123456789abcdef0123456789abcdef01234567 "" "$D4")" "true|[]|[]"
chk "S7 with no anchor the push range is used" "$(release S7 stable "$D0" "" "$D1")" "false|[]|[]"
put docs/guide.txt 'text'
D5=$(commit "docs: a text page")
chk "S8 only images under docs/ are excluded" "$(release S8 stable "$D4" "$D4" "$D5")" "true|[]|[]"
put main.go 'package main // v2'
D6=$(commit "fix: a real change")
chk "S9 a code change is significant" "$(release S9 stable "$D5" "$D5" "$D6")" "true|[]|[]"
chk_has "S9 the summary lists the path" "$(cat "$WORK/summary")" $'```\nmain.go\n```'
chk "S10 MODE=paths ignores the empty commit" "$(paths "$D3" "$D4")" 'false|[]|[]|[]'
chk "S10 and lists what is significant" "$(paths "$D5" "$D6")" 'true|[]|[]|["main.go"]'
chk "S10 its stdout holds key=value lines only" "$(grep -cv '^[a-z_]*=' "$WORK/pout" || true)" "0"
chk_has "S10 its notices go to stderr" "$(paths "$D2" "$D3" >/dev/null && cat "$WORK/perr")" "::notice::package.json is not a published manifest"
: >"$WORK/gh_out"
GITHUB_OUTPUT="$WORK/gh_out" MODE=paths FROM="$D5" TO="$D6" bash "$TOOLS/path-significance.sh" >/dev/null
chk "S10 MODE=paths never writes GITHUB_OUTPUT" "$(wc -c <"$WORK/gh_out" | tr -d ' ')" "0"
rc=0
MODE=paths FROM=nope TO="$D6" bash "$TOOLS/path-significance.sh" >/dev/null 2>&1 || rc=$?
chk "S11 MODE=paths with an unknown FROM is status 2" "$rc" "2"
rc=0
MODE=paths TO="$D6" bash "$TOOLS/path-significance.sh" >/dev/null 2>&1 || rc=$?
chk "S11 MODE=paths without FROM fails" "$([ "$rc" -ne 0 ] && echo failed)" "failed"
rc=0
MODE=bogus bash "$TOOLS/path-significance.sh" >/dev/null 2>&1 || rc=$?
chk "S11 an unknown MODE is status 2" "$rc" "2"
mkdir -p testdata
git mv main.go testdata/main.go
D7=$(commit "refactor: move the entrypoint into testdata")
chk "S11r MODE=paths counts a shipped file moved under an excluded path" "$(paths "$D6" "$D7")" \
  'true|[]|[]|["main.go"]'
chk "S11r legacy release mode sees only the excluded destination, as before" \
  "$(RELEASE_MODEL=legacy release S11rl stable "$D6" "$D6" "$D7")" "false|[]|[]"
chk "S11r two-branch release mode counts the shipped file that moved" \
  "$(RELEASE_MODEL=two-branch release S11rt stable "$D6" "$D6" "$D7")" "true|[]|[]"
chk "S11r so does a two-branch push range with no anchor" \
  "$(RELEASE_MODEL=two-branch release S11rp dev "$D6" "" "$D7")" "true|[]|[]"
rc=0
RELEASE_MODEL=bogus BEFORE="$D6" HEAD="$D7" ANCHOR_SHA="" GITHUB_OUTPUT=/dev/null GITHUB_STEP_SUMMARY=/dev/null \
  bash "$TOOLS/path-significance.sh" >/dev/null 2>&1 || rc=$?
chk "S11r an unknown RELEASE_MODEL is status 2" "$rc" "2"
put deadset.json '{}'
put web/deadset-ignore.json '[]'
put web/knip.json '{}'
put web/knip.config.ts 'export default {}'
D8=$(commit "refactor: dead-code adjudications only")
chk "S11d deadset and knip configs ship nothing" "$(paths "$D7" "$D8")" 'false|[]|[]|[]'

# ── A ts repo: the root package.json is the published manifest ───────────────
export REPO_TYPE=ts
newrepo ts
put jsr.json '{"name":"@o/lib","version":"1.0.0"}'
put src/index.ts 'export {}'
put package.json '{"name":"@o/lib","devDependencies":{"a":"1"},"dependencies":{"b":"1"}}'
T0=$(commit "feat: initial")
put package.json '{"name":"@o/lib","devDependencies":{"a":"2"},"dependencies":{"b":"1"}}'
T1=$(commit "chore(devdeps): bump a")
chk "S12 a devDependency-only change publishes nothing" "$(release S12 stable "$T0" "$T0" "$T1")" "false|[]|[]"
chk_has "S12 and says why" "$(cat "$WORK/log")" "changed only in devDependencies/overrides"
put package.json '{"name":"@o/lib","devDependencies":{"a":"2"},"dependencies":{"b":"1"},"overrides":{"qs":"6"}}'
T2=$(commit "fix: override qs")
chk "S13 an overrides-only change publishes nothing" "$(release S13 stable "$T1" "$T1" "$T2")" "false|[]|[]"
put package.json '{"name":"@o/lib","devDependencies":{"a":"2"},"dependencies":{"b":"2"},"overrides":{"qs":"6"}}'
T3=$(commit "fix(deps): bump b")
chk "S14 a dependencies change publishes" "$(release S14 stable "$T2" "$T2" "$T3")" "true|[]|[]"
chk "S14 a dispatch keeps the manifest significant" "$(release S14d stable "" "" "$T3")" "true|[]|[]"
chk "S15 MODE=paths applies the same refinement" "$(paths "$T0" "$T1")|$(paths "$T2" "$T3")" \
  'false|[]|[]|[]|true|[]|[]|["package.json"]'

# ── A Go root with a TS subpackage and two nested lanes ──────────────────────
export REPO_TYPE=go SUBPACKAGES_JSON='["web"]' GO_LANES_JSON='["yamlenv","tools/gen"]'
newrepo hybrid
put go.mod 'module example.com/app'
put main.go 'package main'
put web/jsr.json '{"name":"@o/web","version":"1.0.0"}'
put web/package.json '{"name":"@o/web","devDependencies":{"a":"1"},"dependencies":{"b":"1"}}'
put web/src/index.ts 'export {}'
put yamlenv/go.mod 'module example.com/app/yamlenv'
put yamlenv/y.go 'package yamlenv'
put tools/gen/go.mod 'module example.com/app/tools/gen'
put tools/gen/g.go 'package gen'
H0=$(commit "feat: initial")
git tag v1.0.0 "$H0"
put yamlenv/y.go 'package yamlenv // v2'
H1=$(commit "fix(yamlenv): lane")
chk "S16 a lane-only change is the lane's alone" "$(release S16 dev "$H0" "$H0" "$H1")" 'false|[]|["yamlenv"]'
put main.go 'package main // v2'
put tools/gen/g.go 'package gen // v2'
H2=$(commit "fix: root and lane")
chk "S17 a root and lane change is both" "$(release S17 dev "$H1" "$H1" "$H2")" 'true|[]|["tools/gen"]'
put web/src/index.ts 'export const x = 1'
H3=$(commit "feat(web): api")
chk "S18 a subpackage-only change is the subpackage's" "$(release S18 dev "$H2" "$H2" "$H3")" 'false|["web"]|[]'
put web/package.json '{"name":"@o/web","devDependencies":{"a":"2"},"dependencies":{"b":"1"}}'
H4=$(commit "chore(devdeps): web")
chk "S19 a subpackage devDependency-only change publishes nothing" "$(release S19 dev "$H3" "$H3" "$H4")" 'false|[]|[]'
put web/package.json '{"name":"@o/web","devDependencies":{"a":"2"},"dependencies":{"b":"2"}}'
H5=$(commit "fix(deps): web b")
chk "S20 a subpackage dependencies change publishes the subpackage" "$(release S20 dev "$H4" "$H4" "$H5")" 'false|["web"]|[]'
chk "S21 MODE=paths: root paths exclude the nested lanes" "$(paths "$H0" "$H1")" 'false|[]|["yamlenv"]|["yamlenv/y.go"]'
chk "S21 MODE=paths: a mixed range is both" "$(paths "$H1" "$H3")" \
  'true|["web"]|["tools/gen"]|["main.go","tools/gen/g.go","web/src/index.ts"]'
git tag yamlenv/v1.0.1 "$H5"
chk "S22 on stable a lane tag at HEAD is emitted for the Release repair" "$(release S22 stable "$H4" "$H4" "$H5")" \
  'false|["web"]|["yamlenv"]'
chk "S22 MODE=paths reads no lane tags" "$(paths "$H4" "$H5")" 'false|["web"]|[]|["web/package.json"]'
put main.go 'package main // v3'
H5B=$(commit "fix: root after the lane tag")
chk "S23 a tagged lane is not owed for lane changes its tag already covers" \
  "$(release S23a stable "$H0" "$H0" "$H5B")" 'true|["web"]|["tools/gen"]'
chk "S23 MODE=paths ignores a lane tag inside the range" "$(paths "$H0" "$H5B")" \
  'true|["web"]|["yamlenv","tools/gen"]|["main.go","tools/gen/g.go","web/package.json","web/src/index.ts","yamlenv/y.go"]'
put yamlenv/y.go 'package yamlenv // v3'
H6=$(commit "fix(yamlenv): after the lane tag")
chk "S23 a tagged lane is measured from its own tag, an untagged one from the root range" \
  "$(release S23 stable "$H0" "$H0" "$H6")" 'true|["web"]|["yamlenv","tools/gen"]'
chk_has "S23 and names that tag's commit" "$(cat "$WORK/log")" "lane yamlenv: deriving changed paths from ${H5}..HEAD"
git mv tools/gen/g.go g_moved.go
H7=$(commit "refactor: move a lane file to the root")
chk "S24 MODE=paths counts a file leaving a lane against that lane" "$(paths "$H6" "$H7")" \
  'true|[]|["tools/gen"]|["g_moved.go","tools/gen/g.go"]'
git tag yamlenv/v1.0.2 "$H7"
mkdir -p yamlenv/testdata
git mv yamlenv/y.go yamlenv/testdata/y.go
H8=$(commit "refactor(yamlenv): move the source under testdata")
chk "S25 legacy release mode misses a lane file moved under an excluded path, as before" \
  "$(RELEASE_MODEL=legacy release S25l stable "$H7" "$H7" "$H8")" 'false|[]|[]'
chk "S25 two-branch release mode owes that lane a release from its own tag" \
  "$(RELEASE_MODEL=two-branch release S25t stable "$H7" "$H7" "$H8")" 'false|[]|["yamlenv"]'
cd "$WORK" || exit 1

if [ -n "${PS_BASELINE:-}" ]; then
  echo "PS_BASELINE: $(wc -l <"$WORK/baseline.matched" | tr -d ' ') release-mode runs identical to $PS_BASELINE"
fi
echo "PASS: path significance ($PASS checks)"
