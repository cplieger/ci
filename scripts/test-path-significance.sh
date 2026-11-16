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
put deadset-baseline.json '{}'
put web/knip.json '{}'
put web/knip.config.ts 'export default {}'
D8=$(commit "refactor: dead-code adjudications only")
chk "S11d deadset and knip configs ship nothing" "$(paths "$D7" "$D8")" 'false|[]|[]|[]'
put corpus.lock.json '{}'
put api-surface.json '{}'
D9=$(commit "chore: test inputs only")
chk "S11t test-only inputs ship nothing" "$(paths "$D8" "$D9")" 'false|[]|[]|[]'
put web/api-surface.json '{}'
D10=$(commit "feat(web): a nested api-surface.json")
chk "S11t a nested api-surface.json is not the root test snapshot" "$(paths "$D9" "$D10")" 'true|[]|[]|["web/api-surface.json"]'
put README.md 'docs, under the rebuild subject'
D11=$(commit "$REBUILD_SUBJECT")
chk "S27 a commit under the rebuild subject that changes a file is no rebuild request" \
  "$(release S27f stable "$D10" "$D10" "$D11")" "false|[]|[]"
chk "S27 and one whose files git cannot read fails the step" \
  "$(failing_release "-c core.quotePath=false diff-tree --root --no-commit-id --name-only -r $D11" 1 stable "$D10" "$D10" "$D11")" "2|1"
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
  "$(failing_paths "-c core.quotePath=false diff-tree --root --no-commit-id --name-only -r $D11" 1 "$D10" "$D11")" "2|1"
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
chk_has "S12 and says why" "$(cat "$WORK/log")" "changed none of the fields it ships"
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

# ── The synced license collector rides the next build ───────────────────────
export GO_LANES_JSON='[]'
newrepo docker-licenses
put Dockerfile 'FROM scratch'
put main.go 'package main'
put scripts/collect-licenses.sh '#!/bin/sh'
put scripts/repin-sha.sh '#!/bin/sh'
K0=$(commit "feat: initial")
put scripts/collect-licenses.sh '#!/bin/sh # v2'
K1=$(commit "chore(sync): synced file(s) with cplieger/ci (#2)")
chk "S36 a change to the synced license collector alone releases nothing" "$(release S34 stable "$K0" "$K0" "$K1")" 'false|[]|[]'
chk "S36 and MODE=paths lists nothing significant" "$(paths "$K0" "$K1")" 'false|[]|[]|[]'
put scripts/repin-sha.sh '#!/bin/sh # v2'
K2=$(commit "chore(sync): synced file(s) with cplieger/ci (#3)")
chk "S36 another script under scripts/ still counts" "$(paths "$K1" "$K2")" 'true|[]|[]|["scripts/repin-sha.sh"]'
put cmd/scripts/collect-licenses.sh '#!/bin/sh'
K3=$(commit "feat: a nested copy")
chk "S36 only the root copy is excluded" "$(paths "$K2" "$K3")" 'true|[]|[]|["cmd/scripts/collect-licenses.sh"]'
# ── A file a non-test //go:embed names ships, whatever the excludes say ─────
# grelot#67 changed only internal/cli/manual.md, which manual.go embeds as
# `grelot help`, and released nothing.
export REPO_TYPE=go SUBPACKAGES_JSON='[]' GO_LANES_JSON='["lane"]'
newrepo embeds
put go.mod 'module example.com/app'
put main.go 'package main'
put internal/cli/manual.go 'package cli

import _ "embed"

//go:embed manual.md
var Manual string'
put internal/cli/manual.md 'v1'
put internal/cli/notes.md 'v1'
put internal/cli/x_test.go 'package cli

//go:embed testonly.md
var testOnly string'
put internal/cli/testonly.md 'v1'
# shellcheck disable=SC2016 # a Go raw string, literal backticks
put web/web.go 'package web

import "embed"

//go:embed "static" all:hidden `assets/*.md`
var FS embed.FS'
put web/static/index.md 'v1'
put web/static/sub/.hidden.md 'v1'
put web/static/_skip.md 'v1'
put web/hidden/.dot.md 'v1'
put web/assets/logo.md 'v1'
put web/assets/deep/logo.md 'v1'
put docs/guide.md 'v1'
put lane/go.mod 'module example.com/app/lane'
put lane/lane.go 'package lane

import _ "embed"

//go:embed README.md
var readme string'
put lane/README.md 'v1'
E0=$(commit "feat: initial")
git tag v1.0.0 "$E0"
git tag lane/v1.0.0 "$E0"
for f in internal/cli/manual.md internal/cli/notes.md internal/cli/testonly.md web/static/index.md \
  web/static/sub/.hidden.md web/static/_skip.md web/hidden/.dot.md web/assets/logo.md web/assets/deep/logo.md docs/guide.md; do
  put "$f" 'v2'
done
E1=$(commit "fix: reword the shipped and unshipped docs")
chk "S37 an embedded .md is significant; an unembedded or test-embedded one is not" \
  "$(paths "$E0" "$E1")" 'true|[]|[]|["internal/cli/manual.md","web/assets/logo.md","web/hidden/.dot.md","web/static/index.md"]'
chk "S37 and the release owes the root" "$(release S37 dev "$E0" "$E0" "$E1")" 'true|[]|[]'
put lane/README.md 'v2'
E2=$(commit "fix(lane): reword the embedded readme")
chk "S38 an embedded file in a lane owes that lane, not the root" "$(release S38 dev "$E1" "$E1" "$E2")" 'false|[]|["lane"]'
chk "S38 and MODE=paths names it" "$(paths "$E1" "$E2")" 'false|[]|["lane"]|["lane/README.md"]'
put docs/guide.md 'v3'
E3=$(commit "docs: the guide")
chk "S39 an unembedded doc alone still releases nothing" "$(paths "$E2" "$E3")" 'false|[]|[]|[]'
# The first ls-tree is the embed check's, which runs before go-modules.sh.
chk "S40 a tree the embed check cannot read fails the run, never drops the path" \
  "$(failing_paths '*ls-tree*' 1 "$E2" "$E3")" '2|1'
chk_has "S40 and says why" "$(cat "$WORK/perr")" "cannot read the //go:embed directives at $E3"
# Go's embed skips a subdirectory that holds its own go.mod, so a directory
# pattern above a nested module never names that module's files.
newrepo embeds-nested
put go.mod 'module example.com/site'
put site.go 'package site

import "embed"

//go:embed docs
var Docs embed.FS'
put docs/guide.md 'v1'
put docs/tool/go.mod 'module example.com/site/docs/tool'
put docs/tool/README.md 'v1'
N0=$(commit "feat: initial")
put docs/guide.md 'v2'
put docs/tool/README.md 'v2'
N1=$(commit "docs: both")
chk "S41 a directory pattern stops at a nested module" "$(paths "$N0" "$N1")" 'true|[]|[]|["docs/guide.md"]'
# A directive in a file Go never builds embeds nothing: one under testdata or
# a `_` directory, or behind `//go:build ignore`.
newrepo embeds-ignored
put go.mod 'module example.com/ign'
put gen/gen.go '//go:build ignore

package main

import _ "embed"

//go:embed notes.md
var notes string'
put gen/notes.md 'v1'
put tools/testdata/t.go 'package t

//go:embed fixture.md
var f string'
put tools/testdata/fixture.md 'v1'
put _old/o.go 'package old

//go:embed old.md
var o string'
put _old/old.md 'v1'
I0=$(commit "feat: initial")
put gen/notes.md 'v2'
put tools/testdata/fixture.md 'v2'
put _old/old.md 'v2'
I1=$(commit "docs: all three")
chk "S42 a directive in a file Go never builds embeds nothing" "$(paths "$I0" "$I1")" 'false|[]|[]|[]'
cd "$WORK" || exit 1

# ── A tagged lane's web half that changed only its dev tooling ──────────────
# cplieger/go released keyenc/v1.2.1 with no Go change: keyenc/web's
# package.json moved only a devDependency and its lockfile followed. The lane
# lane range drops it by the same manifest rule the root range applies.
export REPO_TYPE=go SUBPACKAGES_JSON='[]' GO_LANES_JSON='["keyenc"]'
newrepo lane-half
put keyenc/go.mod 'module example.com/go/keyenc'
put keyenc/k.go 'package keyenc'
put keyenc/web/package.json '{"name":"@o/keyenc","version":"0.0.0","devDependencies":{"knip":"1"},"dependencies":{}}'
put keyenc/web/package-lock.json '{"lockfileVersion":3}'
put keyenc/web/jsr.json '{"name":"@o/keyenc","version":"0.0.0"}'
N0=$(commit "feat: initial")
git tag keyenc/v1.2.0 "$N0"
put keyenc/web/package.json '{"name":"@o/keyenc","version":"0.0.0","devDependencies":{"knip":"2"},"dependencies":{}}'
put keyenc/web/package-lock.json '{"lockfileVersion":3,"x":1}'
N1=$(commit "chore(devdeps): update knip")
chk "S43 a lane whose web half changed only devDependencies and its lockfile is owed nothing" \
  "$(release S43 stable "$N0" "$N0" "$N1")" 'false|[]|[]'
chk_has "S43 and says why" "$(cat "$WORK/log")" "keyenc/web/package.json changed none of the fields it ships"
put keyenc/web/package.json '{"name":"@o/keyenc","version":"0.0.0","devDependencies":{"knip":"2"},"dependencies":{"b":"1"}}'
N2=$(commit "fix(deps): keyenc web b")
chk "S43 a lane web half's dependencies change still owes the lane a release" \
  "$(release S43b stable "$N1" "$N0" "$N2")" 'false|[]|["keyenc"]'
put keyenc/k.go 'package keyenc // v2'
git tag keyenc/v1.2.1 "$N2"
N3=$(commit "fix(keyenc): code")
chk "S43 a lane code change beside nothing else still owes a release" "$(release S43c stable "$N2" "$N2" "$N3")" 'false|[]|["keyenc"]'
# MODE=paths (release-state.sh's affects and ahead, promote.py) reads the same
# rule: the half's manifest ships through its runtime fields, a lane-level
# package.json beside no jsr.json is dev tooling.
N4=$N3
put keyenc/web/package.json '{"name":"@o/keyenc","version":"0.0.0","devDependencies":{"knip":"3"},"dependencies":{"b":"1"}}'
N5=$(commit "chore(devdeps): update knip")
chk "S44 MODE=paths: a half's devDependencies bump ships nothing" "$(paths "$N4" "$N5")" 'false|[]|[]|[]'
put keyenc/web/package.json '{"name":"@o/keyenc","version":"0.0.0","devDependencies":{"knip":"3"},"dependencies":{"b":"2"}}'
N6=$(commit "fix(deps): keyenc web b")
chk "S44 MODE=paths: a half's dependencies change is the lane's" "$(paths "$N5" "$N6")" 'false|[]|["keyenc"]|["keyenc/web/package.json"]'
put keyenc/tools/package.json '{"scripts":{"x":"1"}}'
N7=$(commit "chore(keyenc): tool")
git tag keyenc/v1.2.2 "$N7"
put keyenc/tools/package.json '{"scripts":{"x":"2"}}'
N8=$(commit "chore(keyenc): tool script")
chk "S44 a lane's package.json that is no half is dev tooling" "$(release S44 stable "$N7" "$N7" "$N8")" 'false|[]|[]'

put keyenc/web/package.json '{"name":"@o/keyenc","version":"0.0.0","devDependencies":{"knip":"3"},"dependencies":{"b":"2"},"exports":{".":{"node":"./n.js","default":"./d.js"}}}'
N9=$(commit "feat(keyenc): node entry")
git tag keyenc/v1.2.3 "$N9"
put keyenc/web/package.json '{"name":"@o/keyenc","version":"0.0.0","devDependencies":{"knip":"3"},"dependencies":{"b":"2"},"exports":{".":{"default":"./d.js","node":"./n.js"}}}'
N10=$(commit "fix(keyenc): condition order")
chk "S45 a reordered exports condition is a change the half ships" "$(release S45 stable "$N9" "$N9" "$N10")" 'false|[]|["keyenc"]'
git tag keyenc/v1.2.4 "$N10"
put keyenc/web/package.json '{"name":"@o/keyenc","version":"1.0.0","devDependencies":{"knip":"3"},"dependencies":{"b":"2"},"exports":{".":{"default":"./d.js","node":"./n.js"}}}'
put keyenc/web/jsr.json '{"name":"@o/keyenc","version":"1.0.0"}'
N11=$(commit "chore(keyenc): align the half's version")
chk "S45 a version-only edit of the half's package.json and jsr.json ships nothing" \
  "$(release S45b stable "$N10" "$N10" "$N11")" 'false|[]|[]'
put keyenc/web/jsr.json '{"name":"@o/keyenc","version":"1.0.0","exports":"./d.ts"}'
N12=$(commit "feat(keyenc): jsr entry")
chk "S45 a jsr.json change beyond its version ships" "$(release S45c stable "$N11" "$N10" "$N12")" 'false|[]|["keyenc"]'
chk "S45 MODE=paths as half_changed calls it sees an exports reorder" \
  "$(SUBPACKAGES_JSON='["keyenc/web"]' GO_LANES_JSON='[]' REPO_TYPE=none paths "$N9" "$N10")" \
  'false|["keyenc/web"]|[]|["keyenc/web/package.json"]'
git tag keyenc/v1.2.5 "$N12"
put keyenc/web/package.json '{"dependencies":{"b":"2"},"version":"1.0.0","name":"@o/keyenc","devDependencies":{"knip":"3"},"exports":{".":{"default":"./d.js","node":"./n.js"}}}'
N13=$(commit "chore(keyenc): sort the half's manifest")
chk "S46 a key reorder outside exports, imports and typesVersions ships nothing" "$(release S46 stable "$N12" "$N12" "$N13")" 'false|[]|[]'
put keyenc/web/package.json '{"name":"@o/keyenc","version":"1.0.0","dependencies":{"b":"2"},"typesVersions":{">=5.0":{"*":["a/*"]},"*":{"*":["b/*"]}}}'
N14=$(commit "feat(keyenc): types per compiler")
git tag keyenc/v1.2.6 "$N14"
put keyenc/web/package.json '{"name":"@o/keyenc","version":"1.0.0","dependencies":{"b":"2"},"typesVersions":{"*":{"*":["b/*"]},">=5.0":{"*":["a/*"]}}}'
N15=$(commit "fix(keyenc): typesVersions order")
chk "S46 a reordered typesVersions range is a change the half ships" "$(release S46b stable "$N14" "$N14" "$N15")" 'false|[]|["keyenc"]'
git tag keyenc/v1.2.7 "$N15"
put keyenc/tools/jsr.json '{"name":"@o/tools","version":"1.0.0"}'
N16=$(commit "chore(keyenc): tool jsr")
chk "S46 a lane's jsr.json beside no half is dev tooling" "$(release S46c stable "$N15" "$N15" "$N16")" 'false|[]|[]'

# ── A path belongs to the module of its nearest ancestor go.mod ──────────────
# cplieger/go cut chartdata/v1.0.0-dev.2 for a change confined to
# chartdata/internal/gen, a module of its own that chartdata's zip leaves out.
# tools holds a dotless sentinel go.mod and tools/gen is a lane inside it.
export REPO_TYPE=go SUBPACKAGES_JSON='[]' GO_LANES_JSON='["chartdata","tools/gen"]'
newrepo nested
put go.mod 'module example.com/go'
put main.go 'package main'
put chartdata/go.mod 'module example.com/go/chartdata'
put chartdata/c.go 'package chartdata'
put chartdata/internal/gen/go.mod 'module example.com/go/chartdata/internal/gen'
put chartdata/internal/gen/gen.go 'package main'
put internal/gen/go.mod 'module example.com/go/internal/gen'
put internal/gen/gen.go 'package main'
put tools/go.mod 'module tools-ignore'
put tools/run.sh '#!/bin/sh'
put tools/gen/go.mod 'module example.com/go/tools/gen'
put tools/gen/g.go 'package gen'
G0=$(commit "feat: initial")
git tag v1.0.0 "$G0"
put chartdata/internal/gen/go.mod 'module example.com/go/chartdata/internal/gen

require example.com/wiregen v1.1.0'
put chartdata/internal/gen/gen.go 'package main // v2'
G1=$(commit "chore(deps): update wiregen (#248)")
chk "S47 a change confined to a nested module inside an untagged lane releases neither the lane nor the root" \
  "$(release S47 dev "$G0" "$G0" "$G1")" 'false|[]|[]'
chk_has "S47 and says a nested module owns it" "$(cat "$WORK/log")" \
  "chartdata/internal/gen/gen.go ships with no lane and not the root (a nested module owns it), so it is not significant"
chk "S47 MODE=paths finds nothing significant" "$(paths "$G0" "$G1")" 'false|[]|[]|[]'
git tag chartdata/v1.0.0-dev.1 "$G0"
chk "S47 nor does it release a lane measured from its own tag" "$(release S47t dev "$G0" "$G0" "$G1")" 'false|[]|[]'
chk_has "S47 which detect measured from that tag" "$(cat "$WORK/log")" "lane chartdata: deriving changed paths from ${G0}..HEAD"
put chartdata/c.go 'package chartdata // v2'
G2=$(commit "fix(chartdata): a lane change")
chk "S48 a change in the lane itself still releases the lane" "$(release S48 dev "$G1" "$G1" "$G2")" 'false|[]|["chartdata"]'
put chartdata/c.go 'package chartdata // v3'
put chartdata/internal/gen/gen.go 'package main // v3'
G3=$(commit "fix(chartdata): the lane and its nested module")
chk "S49 a commit touching the lane and its nested module releases the lane once" \
  "$(release S49 dev "$G2" "$G2" "$G3")" 'false|[]|["chartdata"]'
chk "S49 MODE=paths lists only the lane's path" "$(paths "$G2" "$G3")" 'false|[]|["chartdata"]|["chartdata/c.go"]'
git tag chartdata/v1.0.0-dev.2 "$G3"
put internal/gen/go.mod 'module example.com/go/internal/gen

require example.com/tool v1.1.0'
put internal/gen/gen.go 'package main // v2'
G4=$(commit "chore(deps): update tool")
chk "S50 a change under a root-level nested module does not release a go root" \
  "$(release S50 dev "$G3" "$G3" "$G4")" 'false|[]|[]'
chk "S50 MODE=paths agrees" "$(paths "$G3" "$G4")" 'false|[]|[]|[]'
put main.go 'package main // v2'
put internal/gen/gen.go 'package main // v3'
G5=$(commit "fix: the root beside its nested module")
chk "S50 the root's own file beside it still releases the root" "$(paths "$G4" "$G5")" 'true|[]|[]|["main.go"]'
put tools/gen/g.go 'package gen // v2'
G6=$(commit "fix(tools/gen): a lane inside a sentinel module")
chk "S51 a lane nested inside a module that is no lane still releases" \
  "$(release S51 dev "$G5" "$G5" "$G6")" 'false|[]|["tools/gen"]'
put tools/run.sh '#!/bin/sh # v2'
G7=$(commit "chore(tools): the sentinel module's own file")
chk "S51 and the outer module's own file releases nothing" "$(paths "$G6" "$G7")" 'false|[]|[]|[]'
chk "S52 a tree go-modules.sh cannot read fails MODE=paths" "$(failing_paths '*ls-tree*' 1 "$G6" "$G7")" '2|1'
chk_has "S52 and says why" "$(cat "$WORK/perr")" "::error::go-modules: cannot list the tree of $G6"
chk "S52 as it fails the release step" "$(failing_release '*ls-tree*' 1 dev "$G6" "$G6" "$G7")" '2|1'

# A docker or ts root builds its artifact from the whole tree, so a sentinel
# module outside every lane is still the root's: web-terminal-kiro's image
# builds the static-src client its static-src-ignore go.mod keeps from Go.
export REPO_TYPE=docker SUBPACKAGES_JSON='[]' GO_LANES_JSON='[]'
newrepo docker-sentinel
put Dockerfile 'FROM scratch'
put go.mod 'module example.com/app'
put main.go 'package main'
put static-src/go.mod 'module static-src-ignore'
put static-src/main.ts 'export {}'
Q0=$(commit "feat: initial")
put static-src/main.ts 'export const x = 1'
Q1=$(commit "feat: the client")
chk "S53 a docker root still ships a sentinel module's tree" "$(release S53 stable "$Q0" "$Q0" "$Q1")" 'true|[]|[]'
# A go.mod inside a TS package (a root subpackage or a lane's half) keeps Go
# tooling out of a tree the package ships.
export REPO_TYPE=go SUBPACKAGES_JSON='["web"]' GO_LANES_JSON='["keyenc"]'
newrepo package-sentinel
put go.mod 'module example.com/app'
put main.go 'package main'
put web/go.mod 'module web-ignore'
put web/jsr.json '{"name":"@o/web","version":"1.0.0"}'
put web/package.json '{"name":"@o/web","version":"1.0.0"}'
put web/src/index.ts 'export {}'
put keyenc/go.mod 'module example.com/app/keyenc'
put keyenc/k.go 'package keyenc'
put keyenc/web/go.mod 'module web-ignore'
put keyenc/web/jsr.json '{"name":"@o/keyenc","version":"1.0.0"}'
put keyenc/web/package.json '{"name":"@o/keyenc","version":"1.0.0"}'
put keyenc/web/mod.ts 'export {}'
W0=$(commit "feat: initial")
put web/src/index.ts 'export const x = 1'
put keyenc/web/mod.ts 'export const y = 1'
W1=$(commit "feat: both packages")
chk "S54 a subpackage and a lane's half ship past their sentinel go.mod" "$(release S54 dev "$W0" "$W0" "$W1")" \
  'false|["web"]|["keyenc"]'
put keyenc/web/mod.ts 'export const y = 2'
W2=$(commit "fix(keyenc): the half alone")
chk "S54 as half_changed reads the half" \
  "$(SUBPACKAGES_JSON='["keyenc/web"]' GO_LANES_JSON='[]' REPO_TYPE=none paths "$W1" "$W2")" \
  'false|["keyenc/web"]|[]|["keyenc/web/mod.ts"]'

# A path counts for its owner in each tree of the commit that changes it: the
# commit that carves a module out of a lane changes the lane's module zip.
export REPO_TYPE=go SUBPACKAGES_JSON='[]' GO_LANES_JSON='["chartdata"]'
newrepo carve-out
put go.mod 'module example.com/go'
put main.go 'package main'
put chartdata/go.mod 'module example.com/go/chartdata'
put chartdata/c.go 'package chartdata'
put chartdata/internal/gen/gen.go 'package gen'
put internal/gen/gen.go 'package gen'
K0=$(commit "feat: initial")
git tag v1.0.0 "$K0"
put chartdata/internal/gen/go.mod 'module example.com/go/chartdata/internal/gen'
K1=$(commit "feat(chartdata)!: move gen into its own module")
chk "S55 carving a nested module out of an untagged lane releases the lane" \
  "$(release S55 dev "$K0" "$K0" "$K1")" 'false|[]|["chartdata"]'
chk "S55 MODE=paths names the new go.mod" "$(paths "$K0" "$K1")" \
  'false|[]|["chartdata"]|["chartdata/internal/gen/go.mod"]'
git tag chartdata/v1.0.0-dev.1 "$K0"
chk "S55 as it does a lane measured from its own tag" "$(release S55t dev "$K0" "$K0" "$K1")" 'false|[]|["chartdata"]'
git tag chartdata/v2.0.0-dev.1 "$K1"
put chartdata/internal/gen/gen.go 'package gen // v2'
K2=$(commit "chore(chartdata): regenerate")
chk "S55 once carved out its changes release nothing" \
  "$(release S55s dev "$K1" "$K1" "$K2")" 'false|[]|[]'
git rm -q chartdata/internal/gen/go.mod
K3=$(commit "feat(chartdata): fold gen back into the lane")
chk "S55 folding it back into the lane releases the lane" "$(release S55f dev "$K2" "$K2" "$K3")" 'false|[]|["chartdata"]'
put internal/gen/go.mod 'module example.com/go/internal/gen'
K4=$(commit "feat!: move the root's gen into its own module")
chk "S55 carving one out of a go root releases the root" "$(paths "$K3" "$K4")" 'true|[]|[]|["internal/gen/go.mod"]'
put chartdata/internal/gen/gen.go 'package gen // v3'
K5=$(commit "fix(chartdata): gen, folded back")
chk "S55 and once folded back its files are the lane's again" "$(paths "$K4" "$K5")" \
  'false|[]|["chartdata"]|["chartdata/internal/gen/gen.go"]'
put chartdata/internal/wire/go.mod 'module example.com/go/chartdata/internal/wire'
put chartdata/internal/wire/w.go 'package wire'
K6=$(commit "feat(chartdata): a new module of its own")
chk "S55 a new module added with its own files takes nothing from the lane" "$(paths "$K5" "$K6")" 'false|[]|[]|[]'
git rm -q -r chartdata/internal/wire
K7=$(commit "chore(chartdata): drop the module")
chk "S55 nor does removing it whole" "$(paths "$K6" "$K7")" 'false|[]|[]|[]'

# A go.mod added or removed over files its container's zip leaves out moves
# nothing to or from it: those in any dir holding a go.mod, a vendored package
# by the zip's own go line (golang.org/x/mod/zip isVendoredPackage), a symlink.
# The root is on go 1.27, the lane has no go line until it takes go 1.27.
newrepo carve-out-of-nothing
put go.mod 'module example.com/go

go 1.27'
put main.go 'package main'
ln -s main.go alias.go
put tools/testdata/mod/go.mod 'module example.com/fixture'
put tools/testdata/mod/a.go 'package mod'
put chartdata/tools/testdata/mod/go.mod 'module example.com/fixture'
put chartdata/tools/testdata/mod/a.go 'package mod'
put case/deep/GO.MOD 'module example.com/go/case/deep'
put case/deep/c.go 'package deep'
put links/deep/sub/go.mod 'module example.com/go/links/deep/sub'
put links/deep/sub/s.go 'package sub'
ln -s ../main.go links/l.go
put internal/deep/sub/go.mod 'module example.com/go/internal/deep/sub'
put internal/deep/sub/s.go 'package sub'
put third_party/vendor/example.com/x/x.go 'package x'
put chartdata/go.mod 'module example.com/go/chartdata'
put chartdata/c.go 'package chartdata'
put chartdata/internal/deep/sub/go.mod 'module example.com/go/chartdata/internal/deep/sub'
put chartdata/internal/deep/sub/s.go 'package sub'
put chartdata/gen/vendor/example.com/x/x.go 'package x'
V0=$(commit "feat: initial")
git tag v1.0.0 "$V0"
put internal/go.mod 'module example.com/go/internal'
V1=$(commit "chore: a module over a nested one")
chk "S59 a go.mod over a dir holding only a deeper module releases nothing" "$(paths "$V0" "$V1")" 'false|[]|[]|[]'
chk "S59 as the release step reads it" "$(release S59 dev "$V0" "$V0" "$V1")" 'false|[]|[]'
git rm -q internal/go.mod
V2=$(commit "chore: drop it")
chk "S59 nor does removing it" "$(paths "$V1" "$V2")" 'false|[]|[]|[]'
put chartdata/internal/go.mod 'module example.com/go/chartdata/internal'
V3=$(commit "chore(chartdata): a module over a nested one")
chk "S59 nor in a lane" "$(paths "$V2" "$V3")" 'false|[]|[]|[]'
put third_party/go.mod 'module example.com/go/third_party'
V4=$(commit "chore: a module over vendored packages")
chk "S59 a go.mod over a dir holding only vendored packages releases nothing" "$(paths "$V3" "$V4")" 'false|[]|[]|[]'
put chartdata/gen/go.mod 'module example.com/go/chartdata/gen'
V5=$(commit "chore(chartdata): a module over vendored packages")
chk "S59 nor over vendored packages in a lane" "$(paths "$V4" "$V5")" 'false|[]|[]|[]'
chk "S59 nor does the range as the release step reads it" "$(release S59r dev "$V0" "$V0" "$V5")" 'false|[]|[]'
put internal/keep.go 'package internal'
V6=$(commit "feat: a root package beside the nested module")
put internal/go.mod 'module example.com/go/internal'
V7=$(commit "feat!: move it into its own module")
chk "S59 one file of the container's beside them makes it a carve-out" "$(paths "$V6" "$V7")" \
  'true|[]|[]|["internal/go.mod"]'
put third_party/vendor/modules.txt '# example.com/x v1.0.0'
V8=$(commit "chore: a vendor manifest")
git rm -q third_party/go.mod
V9=$(commit "feat: fold third_party back into the root")
chk "S59 as does a file a nested vendor dir holds directly, which go 1.24 zips" "$(paths "$V8" "$V9")" \
  'true|[]|[]|["third_party/go.mod"]'
put chartdata/internal/k.go 'package internal'
V10=$(commit "feat(chartdata): a package in the nested module")
git rm -q chartdata/internal/go.mod
V11=$(commit "feat(chartdata): fold it back into the lane")
chk "S59 and a lane gaining a file it ships releases the lane" "$(paths "$V10" "$V11")" \
  'false|[]|["chartdata"]|["chartdata/internal/go.mod"]'
put chartdata/gen/vendor/modules.txt '# example.com/x v1.0.0'
V12=$(commit "chore(chartdata): a vendor manifest")
git rm -q chartdata/gen/go.mod
V13=$(commit "chore(chartdata): fold gen back into the lane")
chk "S59 but a zip with no go line, as before go 1.24, leaves that file out" "$(paths "$V12" "$V13")" 'false|[]|[]|[]'
put tools/go.mod 'module example.com/go/tools'
V14=$(commit "chore: a module over a testdata module")
chk "S59 a go.mod over a dir holding only a testdata module releases nothing" "$(paths "$V13" "$V14")" 'false|[]|[]|[]'
put chartdata/tools/go.mod 'module example.com/go/chartdata/tools'
V15=$(commit "chore(chartdata): a module over a testdata module")
chk "S59 nor a testdata module in a lane" "$(paths "$V14" "$V15")" 'false|[]|[]|[]'
put case/go.mod 'module example.com/go/case'
V16=$(commit "chore: a module over a GO.MOD")
chk "S59 nor over a dir whose go.mod is named in another case" "$(paths "$V15" "$V16")" 'false|[]|[]|[]'
put links/go.mod 'module example.com/go/links'
V17=$(commit "chore: a module over a nested one and a symlink")
chk "S59 nor over a deeper module and a symlink" "$(paths "$V16" "$V17")" 'false|[]|[]|[]'
put tools/keep.go 'package tools'
V18=$(commit "feat(tools): a package")
git rm -q tools/go.mod
V19=$(commit "feat: fold tools into the root")
chk "S59 while a file beside the testdata module carves it" "$(paths "$V18" "$V19")" 'true|[]|[]|["tools/go.mod"]'

# The same zip rule decides a path's own change.
put third_party/vendor/example.com/x/x.go 'package x // v2'
V20=$(commit "chore: bump a vendored package")
chk "S59 a vendored package's change does not release a go root" "$(paths "$V19" "$V20")" 'false|[]|[]|[]'
chk "S59 but releases a docker root, built from the tree" "$(REPO_TYPE=docker paths "$V19" "$V20")" \
  'true|[]|[]|["third_party/vendor/example.com/x/x.go"]'
put tools/testdata/mod/a.go 'package mod // v2'
put chartdata/tools/testdata/mod/a.go 'package mod // v2'
V21=$(commit "test: the testdata modules")
chk "S59 nor does a testdata module's change, for the root or a lane" \
  "$(
    bash "$TOOLS/go-modules.sh" commits "$V20" "$V21" .
    bash "$TOOLS/go-modules.sh" commits "$V20" "$V21" chartdata
  )" ''
chk "S59 which a docker root ships" "$(REPO_TYPE=docker bash "$TOOLS/go-modules.sh" commits "$V20" "$V21" .)" \
  "$V21"$'\t'tools/testdata/mod/a.go
ln -sfn tools/keep.go alias.go
V22=$(commit "chore: retarget the symlink")
chk "S59 nor does a symlink's change" "$(paths "$V21" "$V22")" 'false|[]|[]|[]'
chk "S59 which a docker root ships" "$(REPO_TYPE=docker paths "$V21" "$V22")" 'true|[]|[]|["alias.go"]'
put vendor/modules.txt '# example.com/x v1.0.0'
put go.mod 'module example.com/go

go 1.23'
V23=$(commit "chore: an older go line and a vendor manifest")
put go.mod 'module example.com/go

go 1.27'
commit "chore: go 1.27" >/dev/null
put vendor/modules.txt '# example.com/x v1.1.0'
V25=$(commit "chore: the vendor manifest")
chk "S59 the zip's go line is read per commit: go 1.24 leaves out vendor/modules.txt" \
  "$(bash "$TOOLS/go-modules.sh" commits "$V23" "$V25" . | cut -f2 | tr '\n' ' ')" 'go.mod '
chk "S59 a symlink no commit changes is no go root's either" \
  "$(printf 'alias.go\nmain.go\n' | bash "$TOOLS/go-modules.sh" paths "" "$V22" . | LC_ALL=C sort | tr '\t\n' ': ')" '.:main.go '
put .hg_archival.txt 'repo: 0'
V26=$(commit "chore: an hg archive stamp")
chk "S59 nor does a module's .hg_archival.txt, which the zip drops" "$(paths "$V25" "$V26")" 'false|[]|[]|[]'
chk "S59 which a docker root ships" "$(REPO_TYPE=docker paths "$V25" "$V26")" 'true|[]|[]|[".hg_archival.txt"]'
put chartdata/.hg_archival.txt 'repo: 0'
put docs/.hg_archival.txt 'repo: 0'
V27=$(commit "chore: more hg archive stamps")
chk "S59 in a lane too, while one in a subdir ships" "$(paths "$V26" "$V27")" 'true|[]|[]|["docs/.hg_archival.txt"]'
chmod +x main.go chartdata/c.go
V28=$(commit "chore: executable bits")
chk "S59 nor does a change of mode alone, which the zip keeps no trace of" "$(paths "$V27" "$V28")" 'false|[]|[]|[]'
chk "S59 which a docker root ships" "$(REPO_TYPE=docker paths "$V27" "$V28")" 'true|[]|[]|["main.go"]'
put symlinked/x.go 'package symlinked'
V29=$(commit "feat: a root package")
ln -s ../main.go symlinked/go.mod
V30=$(commit "chore: a symlinked go.mod")
chk "S59 a symlinked go.mod is no file of the zip" "$(paths "$V29" "$V30")" 'false|[]|[]|[]'
put symlinked/x.go 'package symlinked // v2'
V31=$(commit "fix: the root package")
chk "S59 nor a module boundary, so its dir stays the root's" "$(paths "$V30" "$V31")" 'true|[]|[]|["symlinked/x.go"]'
put chartdata/go.mod 'module example.com/go/chartdata

go 1.27'
V32=$(commit "chore(chartdata): go 1.27")
put chartdata/gen/go.mod 'module example.com/go/chartdata/gen'
V33=$(commit "feat(chartdata)!: gen into its own module again")
chk "S59 a lane on go 1.24 or later ships a file a nested vendor dir holds directly" "$(paths "$V32" "$V33")" \
  'false|[]|["chartdata"]|["chartdata/gen/go.mod"]'
git rm -q chartdata/gen/go.mod
V34=$(commit "feat(chartdata): fold gen back")
chk "S59 so carving it out and folding it back both release the lane" "$(paths "$V33" "$V34")" \
  'false|[]|["chartdata"]|["chartdata/gen/go.mod"]'

# Ownership is read per commit, never at the range's ends: a range from a
# stable tag older than a carve-out holds both the carve-out, which is the
# lane's, and later commits confined to the new module, which are not.
newrepo carve-then-nested
put go.mod 'module example.com/go'
put chartdata/go.mod 'module example.com/go/chartdata'
put chartdata/c.go 'package chartdata'
put chartdata/internal/gen/gen.go 'package gen'
P0=$(commit "feat(chartdata): initial")
git tag v1.0.0 "$P0"
git tag chartdata/v1.0.0 "$P0"
put chartdata/internal/gen/go.mod 'module example.com/go/chartdata/internal/gen'
P1=$(commit "chore(chartdata): move gen into its own module")
git tag chartdata/v1.1.0-dev.1 "$P1"
put chartdata/c.go 'package chartdata // v2'
P2=$(commit "fix(chartdata): a lane fix")
git tag chartdata/v1.1.0-dev.2 "$P2"
put chartdata/internal/gen/gen.go 'package gen // v2'
P3=$(commit "feat(chartdata)!: regenerate the generator")
chk "S57 a range spanning a carve-out ships the carve-out and the lane's fix, not the later nested change" \
  "$(paths "$P0" "$P3")" 'false|[]|["chartdata"]|["chartdata/c.go","chartdata/internal/gen/go.mod"]'
chk "S57 the stable run from the lane's stable tag releases the lane" "$(release S57 stable "$P0" "$P0" "$P3")" \
  'false|[]|["chartdata"]'
chk "S57 the dev run past the lane's last dev tag releases nothing" "$(release S57d dev "$P2" "$P2" "$P3")" 'false|[]|[]'
git checkout -q -b side "$P1"
put chartdata/internal/gen/gen.go 'package gen // side'
put chartdata/c.go 'package chartdata // side'
P4=$(commit "fix: a side branch")
git checkout -q main
chk "S57 a path only the range's start changed counts for its owner at either end" \
  "$(paths "$P4" "$P1")" 'false|[]|["chartdata"]|["chartdata/c.go"]'

# A go root's zip holds a lane's files until the lane's go.mod exists, so the
# commit carving a lane out of a go root releases both; a docker root never
# counted the lane's files.
export REPO_TYPE=go SUBPACKAGES_JSON='[]' GO_LANES_JSON='["pkg/split"]'
newrepo lane-carve-out
put go.mod 'module example.com/go'
put main.go 'package main'
put pkg/split/s.go 'package split'
Q0=$(commit "feat: initial")
git tag v1.0.0 "$Q0"
put pkg/split/go.mod 'module example.com/go/pkg/split'
Q1=$(commit "feat!: move pkg/split into its own module")
chk "S58 carving a lane out of a go root releases the root and the lane" "$(release S58 dev "$Q0" "$Q0" "$Q1")" \
  'true|[]|["pkg/split"]'
chk "S58 MODE=paths agrees" "$(paths "$Q0" "$Q1")" 'true|[]|["pkg/split"]|["pkg/split/go.mod"]'
git tag pkg/split/v1.0.0-dev.1 "$Q1"
git tag v2.0.0-dev.1 "$Q1"
put pkg/split/s.go 'package split // v2'
Q2=$(commit "fix(split): a lane change")
chk "S58 a later lane-only change releases only the lane" "$(release S58l dev "$Q1" "$Q1" "$Q2")" 'false|[]|["pkg/split"]'
export REPO_TYPE=docker
newrepo docker-lane-carve-out
put Dockerfile 'FROM scratch'
put main.go 'package main'
put pkg/split/s.go 'package split'
Q3=$(commit "feat: initial")
put pkg/split/go.mod 'module example.com/go/pkg/split'
Q4=$(commit "feat!: move pkg/split into its own module")
chk "S58 a docker root's carve-out releases the lane alone" "$(paths "$Q3" "$Q4")" 'false|[]|["pkg/split"]|["pkg/split/go.mod"]'

# Only <lane>/web is a TS package inside a lane: a lane that is a root
# subpackage, or sits under one, still leaves out its nested modules.
export REPO_TYPE=go SUBPACKAGES_JSON='["ui","pkg"]' GO_LANES_JSON='["ui","pkg/lane"]'
newrepo lane-in-package
put go.mod 'module example.com/app'
put main.go 'package main'
put ui/jsr.json '{"name":"@o/ui","version":"1.0.0"}'
put ui/go.mod 'module example.com/app/ui'
put ui/u.go 'package ui'
put ui/internal/gen/go.mod 'module example.com/app/ui/internal/gen'
put ui/internal/gen/g.go 'package gen'
put ui/web/go.mod 'module web-ignore'
put ui/web/jsr.json '{"name":"@o/ui-web","version":"1.0.0"}'
put pkg/jsr.json '{"name":"@o/pkg","version":"1.0.0"}'
put pkg/lane/go.mod 'module example.com/app/pkg/lane'
put pkg/lane/l.go 'package lane'
put pkg/lane/internal/gen/go.mod 'module example.com/app/pkg/lane/internal/gen'
put pkg/lane/internal/gen/g.go 'package gen'
put pkg/web/go.mod 'module web-ignore'
I0=$(commit "feat: initial")
chk "S56 go-modules.sh leaves out a lane's nested module, though the lane is or sits in a TS package" \
  "$(bash "$TOOLS/go-modules.sh" boundaries HEAD | tr '\n' ' ')" 'pkg/lane pkg/lane/internal/gen ui ui/internal/gen '
put ui/internal/gen/g.go 'package gen // v2'
put pkg/lane/internal/gen/g.go 'package gen // v2'
I1=$(commit "chore: regenerate both")
chk "S56 so a change confined to them releases no lane; the packages ship their trees" \
  "$(release S56 dev "$I0" "$I0" "$I1")" 'false|["ui","pkg"]|[]'
for t in ui/v1.0.0 ui/v1.0.0-dev.1 pkg/lane/v1.0.0 pkg/lane/v1.0.0-dev.1; do
  git tag "$t" "$I0"
done
chk "S56 as it does for the lanes measured from their own tags" "$(release S56t dev "$I0" "$I0" "$I1")" \
  'false|["ui","pkg"]|[]'
chk "S56 and on stable" "$(release S56s stable "$I0" "$I0" "$I1")" 'false|["ui","pkg"]|[]'
# git quotes a non-ASCII path name by default; go-modules.sh judges raw names.
export REPO_TYPE=go SUBPACKAGES_JSON='[]' GO_LANES_JSON='["chartdata"]'
newrepo non-ascii
put go.mod 'module example.com/go'
put main.go 'package main'
put chartdata/go.mod 'module example.com/go/chartdata'
put chartdata/c.go 'package chartdata'
U0=$(commit "feat: initial")
put café.go 'package main'
U1=$(commit "fix: a root file with an accented name")
chk "S57 a change confined to a non-ASCII root file releases the root" "$(paths "$U0" "$U1")" 'true|[]|[]|["café.go"]'
put chartdata/é.go 'package chartdata'
U2=$(commit "fix(chartdata): a lane file with an accented name")
chk "S57 and one in a lane releases the lane" "$(paths "$U1" "$U2")" 'false|[]|["chartdata"]|["chartdata/é.go"]'
chk "S57 as the release step reads it" "$(release S57 dev "$U1" "$U1" "$U2")" 'false|[]|["chartdata"]'
cd "$WORK" || exit 1

echo "PASS: path significance ($PASS checks)"
