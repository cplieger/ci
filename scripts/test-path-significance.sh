#!/usr/bin/env bash
# Regression probe for scripts/path-significance.sh and the ci-source
# checkout that ships it. The workflow step body is EXTRACTED and executed in
# fixture repositories, so the chain under test is the one that ships.
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

# A git whose call FAIL_GIT_N among those whose argv matches the glob FAIL_GIT
# alone fails; git_calls is how many matched.
GIT_FAIL="$WORK/git-fail"
mkdir -p "$GIT_FAIL"
cat >"$GIT_FAIL/git" <<SH
#!/usr/bin/env bash
if [[ "\$*" == \${FAIL_GIT:?} ]]; then
  n=\$((\$(cat "$GIT_FAIL/n" 2>/dev/null || echo 0) + 1))
  echo "\$n" >"$GIT_FAIL/n"
  [ "\$n" -ne "\${FAIL_GIT_N:?}" ] || { echo "stub: git \$* fails" >&2; exit 92; }
fi
exec "$(command -v git)" "\$@"
SH
chmod 755 "$GIT_FAIL/git"
git_calls() { cat "$GIT_FAIL/n" 2>/dev/null || echo 0; }
# failing_release <argv glob> <N> <channel> <before> <anchor> <head> -> root|subs|lanes, or EXIT=n|matching git calls
failing_release() {
  rm -f "$GIT_FAIL/n"
  export CHANNEL="$3" BEFORE="$4" ANCHOR_SHA="$5" HEAD="$6"
  FAIL_GIT="$1" FAIL_GIT_N="$2" PATH="$GIT_FAIL:$PATH" run_release "$WORK/changes.sh"
  if grep -q '^EXIT=' "$WORK/log"; then
    echo "$(sed -n 's/^EXIT=//p' "$WORK/log" | tail -1)|$(git_calls)"
  else
    printf '%s|%s|%s' "$(sed -n 's/^root_changed=//p' "$WORK/out")" \
      "$(sed -n 's/^subpackages_to_publish=//p' "$WORK/out")" "$(sed -n 's/^go_modules_to_release=//p' "$WORK/out")"
  fi
}
paths_rc() { # <from> <to> -> the MODE=paths exit status; stderr in $WORK/perr
  local rc=0
  MODE=paths FROM="$1" TO="$2" bash "$TOOLS/path-significance.sh" >"$WORK/pout" 2>"$WORK/perr" || rc=$?
  echo "$rc"
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
REBUILD_SUBJECT=$(sed -n "s/^REBUILD_SUBJECT='\(.*\)'\$/\1/p" "$TOOLS/path-significance.sh")
[ -n "$REBUILD_SUBJECT" ] || fail "S4 path-significance.sh holds no REBUILD_SUBJECT"
for f in rebuild-stale.sh promote-digest.sh; do
  chk "S4 the rebuild subject is ${f}'s" "$(grep -cxF "REBUILD_SUBJECT='${REBUILD_SUBJECT}'" "$TOOLS/$f")" "1"
done
# shellcheck disable=SC2016 # the literal argument rebuild-stale.sh passes
chk "S4 rebuild-stale.sh sends it as the commit message and the PR title" \
  "$(grep -c -F -- '-f message="$REBUILD_SUBJECT"' "$TOOLS/rebuild-stale.sh")|$(grep -c -F -- '-f title="$REBUILD_SUBJECT"' "$TOOLS/rebuild-stale.sh")" "1|1"
D3S=$(commit "chore(sync): synced file(s) with cplieger/ci (#40)")
chk "S4 an empty machine commit is no root change" "$(release S4s stable "$D3" "$D3" "$D3S")" "false|[]|[]"
chk_has "S4 and says why" "$(cat "$WORK/log")" "commit $D3S changes no file and is not a rebuild request, so it is not significant"
D3R=$(commit "$REBUILD_SUBJECT")
chk "S4 rebuild-stale.sh's empty commit is a root change" "$(release S4u stable "$D3S" "$D3S" "$D3R")" "true|[]|[]"
chk "S27 a range git cannot list for a rebuild request fails the step" \
  "$(failing_release 'rev-list --no-merges *' 1 stable "$D3S" "$D3S" "$D3R")" "2|1"
chk_has "S27 and says so" "$(cat "$WORK/log")" "path-significance: cannot list the commits of ${D3S}..${D3R}"
chk "S27 as does a rebuild request whose subject git cannot read" \
  "$(failing_release "log -1 --format=%s $D3R" 1 stable "$D3S" "$D3S" "$D3R")" "2|1"
chk_has "S27 and says so" "$(cat "$WORK/log")" "path-significance: cannot read the subject of commit $D3R"
D4=$(commit "${REBUILD_SUBJECT} (#41)")
chk "S4 so is its squash" "$(release S4 stable "$D3R" "$D3R" "$D4")" "true|[]|[]"
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
chk "S11r release mode counts the shipped file that moved" \
  "$(release S11rt stable "$D6" "$D6" "$D7")" "true|[]|[]"
chk "S11r so does a push range with no anchor" \
  "$(release S11rp dev "$D6" "" "$D7")" "true|[]|[]"
put deadset.json '{}'
put web/deadset-ignore.json '[]'
put web/knip.json '{}'
put web/knip.config.ts 'export default {}'
D8=$(commit "refactor: dead-code adjudications only")
chk "S11d deadset and knip configs ship nothing" "$(paths "$D7" "$D8")" 'false|[]|[]|[]'
put socket.yml 'version: 2'
put corpus.lock.json '{}'
put api-surface.json '{}'
D9=$(commit "chore: test inputs and scanner config only")
chk "S11t the Socket config and test-only inputs ship nothing" "$(paths "$D8" "$D9")" 'false|[]|[]|[]'
put web/api-surface.json '{}'
D10=$(commit "feat(web): a nested api-surface.json")
chk "S11t a nested api-surface.json is not the root test snapshot" "$(paths "$D9" "$D10")" 'true|[]|[]|["web/api-surface.json"]'
put README.md 'docs, under the rebuild subject'
D11=$(commit "$REBUILD_SUBJECT")
chk "S27 a commit under the rebuild subject that changes a file is no rebuild request" \
  "$(release S27f stable "$D10" "$D10" "$D11")" "false|[]|[]"
chk "S27 and one whose files git cannot read fails the step" \
  "$(failing_release "diff-tree --root --no-commit-id --name-only -r $D11" 1 stable "$D10" "$D10" "$D11")" "2|1"
chk_has "S27 and says so" "$(cat "$WORK/log")" "path-significance: cannot read the files commit $D11 changes"
chk "S28 a subpackage list that is not JSON fails" "$(SUBPACKAGES_JSON='["web"' paths_rc "$D10" "$D11")" "2"
chk_has "S28 and says so" "$(cat "$WORK/perr")" "path-significance: SUBPACKAGES_JSON must be a JSON array of dirs, got '[\"web\"'"
chk "S28 as does one that is not an array of dirs" "$(SUBPACKAGES_JSON='"web"' paths_rc "$D10" "$D11")" "2"
chk "S28 and a lane list that is not JSON" "$(GO_LANES_JSON='{"a":1}' paths_rc "$D10" "$D11")" "2"
chk_has "S28 and says so" "$(cat "$WORK/perr")" "path-significance: GO_LANES_JSON must be a JSON array of dirs, got '{\"a\":1}'"
chk "S28 an empty subpackage list is none" "$(SUBPACKAGES_JSON='' paths "$D9" "$D10")" 'true|[]|[]|["web/api-surface.json"]'
chk "S28 as is an unset one, when a package.json changed" "$(
  unset SUBPACKAGES_JSON
  paths "$D2" "$D3"
)" 'false|[]|[]|[]'
request() { # <from> <to> -> rebuild_request from MODE=paths stdout, or why there is none
  # Called in a command substitution, where fail exits only the substitution
  # and so reads as no request.
  if ! (paths "$1" "$2" >/dev/null); then
    echo "paths $1..$2 failed"
  elif [ "$(grep -c '^rebuild_request=' "$WORK/pout")" != 1 ]; then
    echo "paths $1..$2 printed no single rebuild_request line"
  else
    sed -n 's/^rebuild_request=//p' "$WORK/pout"
  fi
}
chk "S30 MODE=paths names the rebuild request in range" "$(request "$D3S" "$D3R")" "$D3R"
chk "S30 the newest of two" "$(request "$D3S" "$D4")" "$D4"
chk "S30 none for an empty commit that is not one" "$(request "$D3" "$D3S")" ""
chk "S30 and notes nothing about it" "$(grep -c '::notice::' "$WORK/perr" || true)" "0"
chk "S30 none for a commit with files under the rebuild subject" "$(request "$D10" "$D11")" ""
failing_paths() { # <argv glob> <N> <from> <to> -> the MODE=paths exit status|matching git calls; stderr in $WORK/perr
  local rc
  rm -f "$GIT_FAIL/n"
  rc=$(FAIL_GIT="$1" FAIL_GIT_N="$2" PATH="$GIT_FAIL:$PATH" paths_rc "$3" "$4")
  echo "$rc|$(git_calls)"
}
chk "S31 MODE=paths fails when the commits cannot be listed" \
  "$(failing_paths 'rev-list --no-merges *' 1 "$D5" "$D6")" "2|1"
chk_has "S31 and says which range" "$(cat "$WORK/perr")" "::error::path-significance: cannot list the commits of ${D5}..${D6}"
chk "S31 as does a rebuild request whose subject git cannot read" \
  "$(failing_paths "log -1 --format=%s $D3R" 1 "$D3S" "$D3R")" "2|1"
chk_has "S31 and says so" "$(cat "$WORK/perr")" "::error::path-significance: cannot read the subject of commit $D3R"
chk "S31 and one whose files git cannot read" \
  "$(failing_paths "diff-tree --root --no-commit-id --name-only -r $D11" 1 "$D10" "$D11")" "2|1"
chk_has "S31 and says so" "$(cat "$WORK/perr")" "::error::path-significance: cannot read the files commit $D11 changes"

# ── Docker repos whose rebuild request sits beside a change the image ignores ─
export GO_LANES_JSON='["lane"]'
newrepo docker-lane
put Dockerfile 'FROM scratch'
put main.go 'package main'
put lane/go.mod 'module example.com/app/lane'
put lane/l.go 'package lane'
L0=$(commit "feat: initial")
put lane/l.go 'package lane // v2'
commit "fix(lane): a lane change" >/dev/null
L2=$(commit "$REBUILD_SUBJECT")
chk "S32 a lane change beside a rebuild request still rebuilds the image" "$(release S32 stable "$L0" "$L0" "$L2")" 'true|[]|["lane"]'
export GO_LANES_JSON='[]'
newrepo docker-static
put Dockerfile 'FROM scratch'
put static-src/package.json '{"name":"client","devDependencies":{"a":"1"}}'
P0=$(commit "feat: initial")
put static-src/package.json '{"name":"client","devDependencies":{"a":"2"}}'
commit "chore(devdeps): bump a" >/dev/null
P2=$(commit "${REBUILD_SUBJECT} (#3)")
chk "S33 a dropped package.json beside a rebuild request still rebuilds the image" "$(release S33 stable "$P0" "$P0" "$P2")" 'true|[]|[]'
chk_has "S33 the summary names the request" "$(cat "$WORK/summary")" "(commit $P2 changes no file and counts as a root change)"

# ── A promotion: dev's rebuild request on its second parent ─────────────────
newrepo docker-promotion
put Dockerfile 'FROM scratch'
M0=$(commit "feat: initial")
git checkout -q -b dev
MD=$(commit "$REBUILD_SUBJECT")
promote() { # <main parent> <dev parent> -> a promotion commit of the dev parent's tree
  git commit-tree "${2}^{tree}" -p "$1" -p "$2" -m "release: promote dev into main"
}
MP=$(promote "$M0" "$MD")
chk "S34 on stable a rebuild request reached only through a promotion's second parent is none" \
  "$(REQUEST_CHANNEL=stable request "$M0" "$MP")" ""
chk "S34 on dev it is the range's request" "$(REQUEST_CHANNEL=dev request "$M0" "$MP")" "$MD"
chk "S34 as it is with no channel" "$(request "$M0" "$MP")" "$MD"
git checkout -q -b main2 "$M0"
MR=$(commit "${REBUILD_SUBJECT} (#2)")
MP2=$(promote "$MR" "$MD")
chk "S34 on stable a rebuild request on main's first parent is the range's" \
  "$(REQUEST_CHANNEL=stable request "$M0" "$MP2")" "$MR"
chk "S34 a stable range whose first-parent commits git cannot list fails" \
  "$(REQUEST_CHANNEL=stable failing_paths 'rev-list --no-merges --first-parent *' 1 "$M0" "$MP2")" "2|1"
chk_has "S34 and says which range" "$(cat "$WORK/perr")" "::error::path-significance: cannot list the commits of ${M0}..${MP2}"
chk "S34 a channel that is neither dev nor stable fails" "$(REQUEST_CHANNEL=beta paths_rc "$M0" "$MP2")" "2"
chk_has "S34 and says so" "$(cat "$WORK/perr")" "::error::path-significance: REQUEST_CHANNEL must be dev or stable, got 'beta'"

# A promotion of the same shape in MODE=release: on stable the root-change scan
# skips the rebuild request dev's side carried in, and counts main's own.
git checkout -q main
M35=$(git rev-parse HEAD)
git checkout -q -b dev35
put main_test.go 'package main'
commit "test: cover main" >/dev/null
T35=$(commit "${REBUILD_SUBJECT} (#5)")
git checkout -q main
R35=$(promote "$M35" "$T35")
git merge -q --ff-only "$R35"
chk "S35 on stable a rebuild request reached through a promotion's second parent is no root change" \
  "$(release S35a stable "$M35" "$M35" "$R35")" "false|[]|[]"
chk "S35 on dev the same request is the build's own" "$(release S35b dev "$M35" "$M35" "$T35")" "true|[]|[]"
M35R=$(commit "${REBUILD_SUBJECT} (#6)")
chk "S35 main's own rebuild request after the promotion is a root change on stable" \
  "$(release S35c stable "$R35" "$M35" "$M35R")" "true|[]|[]"

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
T4=$(commit "${REBUILD_SUBJECT} (#7)")
chk "S15e a library ignores even a rebuild request, having no image" "$(release S15e stable "$T3" "$T3" "$T4")" "false|[]|[]"

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
chk "S29 a lane whose tags git cannot list fails the step" \
  "$(failing_release 'for-each-ref refs/tags *' 1 stable "$H0" "$H0" "$H6")" "2|1"
chk_has "S29 and names the lane" "$(cat "$WORK/log")" "path-significance: cannot read lane yamlenv's tags or the history of $H6"
chk "S29 as does a history git cannot walk to the lane's tag" \
  "$(failing_release "rev-list --topo-order $H6" 1 stable "$H0" "$H0" "$H6")" "2|1"
git mv tools/gen/g.go g_moved.go
H7=$(commit "refactor: move a lane file to the root")
chk "S24 MODE=paths counts a file leaving a lane against that lane" "$(paths "$H6" "$H7")" \
  'true|[]|["tools/gen"]|["g_moved.go","tools/gen/g.go"]'
git tag yamlenv/v1.0.2 "$H7"
mkdir -p yamlenv/testdata
git mv yamlenv/y.go yamlenv/testdata/y.go
H8=$(commit "refactor(yamlenv): move the source under testdata")
chk "S25 a lane file moved under an excluded path owes that lane a release from its own tag" \
  "$(release S25t stable "$H7" "$H7" "$H8")" 'false|[]|["yamlenv"]'
git tag v1.0.3 "$H8"
git tag yamlenv/v1.0.3 "$H8"
H9=$(commit "chore(sync): synced file(s) with cplieger/ci (#9)")
chk "S26 an empty commit on a Go library releases nothing" "$(release S26 stable "$H8" "$H8" "$H9")" 'false|[]|[]'
cd "$WORK" || exit 1

echo "PASS: path significance ($PASS checks)"
