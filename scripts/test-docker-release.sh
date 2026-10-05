#!/usr/bin/env bash
# Regression probe for the release-path shell docker-release.yaml embeds
# inline (`Derive version tags`, `Resolve promoted digest`, `Resolve build
# args`, `Create dev tag`) and release.yaml embeds (`Detect changed paths`,
# `Select version`, `Read promotion record`, the tag steps). The bodies are
# EXTRACTED at runtime and executed in fixture repositories against stub curl
# and gh, so what is tested cannot drift from what ships; CONTRIBUTING.md lists
# the cases. Expected values quoting workflow expressions are literal text.
# shellcheck disable=SC2016
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
WORKFLOW="$ROOT/.github/workflows/docker-release.yaml"
WORK="$(mktemp -d /tmp/docker-release-probe.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# ── Extract the subjects ─────────────────────────────────────────────────────
RELEASE_YAML="$ROOT/.github/workflows/release.yaml"
python3 - "$WORKFLOW" "$RELEASE_YAML" "$WORK" <<'PY'
import sys, yaml

wf, release, out = sys.argv[1], sys.argv[2], sys.argv[3]
jobs = yaml.safe_load(open(wf))["jobs"]
rjobs = yaml.safe_load(open(release))["jobs"]
wanted = {
    "prepare": {
        "Derive version tags": "derive.sh",
        "Verify root module path": "modpath.sh",
        "Resolve promoted digest": "promote.sh",
    },
    "build": {"Resolve build args": "buildargs.sh"},
    "finalize": {
        "Generate release notes": "notes.sh",
        "Prepare tag-create helper": "taghelper.sh",
        "Create dev tag": "devtag.sh",
    },
}
for job, steps in wanted.items():
    for step in jobs[job]["steps"]:
        name = step.get("name")
        if name in steps:
            open(f"{out}/{steps[name]}", "w").write(step["run"])
            env = step.get("env", {})
            open(f"{out}/{steps[name]}.env", "w").write("\n".join(sorted(env)) + "\n")
            open(f"{out}/{steps[name]}.envmap", "w").write("".join(f"{k}={v}\n" for k, v in sorted(env.items())))
            open(f"{out}/{steps[name]}.if", "w").write(str(step.get("if", "")))
# The soak-override handoff: release.yaml reads the record and hands the
# sentence to every stable notes step, docker-release included.
step = next(s for s in rjobs["detect"]["steps"] if s.get("name") == "Read promotion record")
open(f"{out}/record.sh", "w").write(step["run"])
open(f"{out}/record.if", "w").write(step.get("if", ""))
open(f"{out}/docker-with.txt", "w").write(str(rjobs["docker"]["with"].get("promotion-note", "")))
for job in ("go", "ts"):
    notes = next(s for s in rjobs[job]["steps"] if s.get("name") == "Generate release notes")
    open(f"{out}/{job}-notes.env", "w").write(str(notes.get("env", {}).get("PROMOTION_NOTE", "")))
    open(f"{out}/{job}-notes.sh", "w").write(notes["run"])
# The dev tag receipt: every dev tag path posts it, and every job that does
# holds the statuses scope, the caller template included.
for job, name in (("go", "Tag + GitHub Release"), ("ts", "Tag + GitHub Release"), ("go-nested", "Tag + GitHub Release (lane)")):
    step = next(s for s in rjobs[job]["steps"] if s.get("name") == name)
    open(f"{out}/{job}-tag.sh", "w").write(step["run"])
perms = {job: sorted(rjobs[job].get("permissions") or {}) for job in ("go", "ts", "go-nested", "docker")}
perms["docker-release/finalize"] = sorted(jobs["finalize"].get("permissions") or {})
template = yaml.safe_load(open(release.replace("workflows/release.yaml", "workflow-templates/release.yml")))
perms["template/release"] = sorted(template["jobs"]["release"].get("permissions") or {})
open(f"{out}/statuses-scope.txt", "w").write("".join(f"{job}={'statuses' in p}\n" for job, p in sorted(perms.items())))
# The release decision: a subpackage-only change must release on both
# channels through the root tag job, and must never rebuild the root image.
for name, fname in (("Detect changed paths", "changes.sh"), ("Select version", "select.sh")):
    step = next(s for s in rjobs["detect"]["steps"] if s.get("name") == name)
    open(f"{out}/{fname}", "w").write(step["run"])
    open(f"{out}/{fname}.env", "w").write("\n".join(sorted(step.get("env", {}))) + "\n")
for job in ("docker", "go", "ts", "subpackage"):
    open(f"{out}/{job}.if", "w").write(" ".join(str(rjobs[job].get("if", "")).split()))
open(f"{out}/subpackage.needs", "w").write(" ".join(rjobs["subpackage"]["needs"]))
open(f"{out}/docker-release-needed.txt", "w").write(str(rjobs["docker"]["with"]["release-needed"]))
PY

for f in derive modpath promote buildargs; do
  chk "D-P1 $f extracted" "$([ -s "$WORK/$f.sh" ] && echo yes || echo no)" "yes"
  chk "D-P2 $f is strict-mode" "$(head -1 "$WORK/$f.sh")" "set -euo pipefail"
  chk "D-P3 $f parses under bash" "$(bash -n "$WORK/$f.sh" 2>/dev/null && echo ok || echo bad)" "ok"
done
chk "D-P4 derive reads the five inputs" "$(tr '\n' ' ' <"$WORK/derive.sh.env")" \
  "CHANNEL DEV_VERSION FINALIZE RELEASE_NEEDED VERSION_INPUT "
chk "D-P5 promote reads the exclusion list" "$(cat "$WORK/promote.sh.env")" "EXCLUDE_RE"
chk "D-P6 build args read the channel tag" "$(tr '\n' ' ' <"$WORK/buildargs.sh.env")" "DOCKERFILE TAG "
# The extracted body sees only the TAG variable; what feeds it is an
# expression the shell cannot evaluate, so it is pinned by text: the channel
# tag prepare derived, never the stable base a dev build has not earned.
chk "D-P7 BUILD_VERSION is fed from the channel tag" \
  "$(sed -n 's/^TAG=//p' "$WORK/buildargs.sh.envmap")" '${{ needs.prepare.outputs.tag }}'

# ── Stub curl: the anonymous GHCR token and manifest HEADs ───────────────────
# BUILT lists the commits that have a `sha-<commit>` tag on the registry.
BIN="$WORK/bin"
mkdir -p "$BIN"
export BUILT="$WORK/built"
cat >"$BIN/curl" <<'SH'
#!/bin/sh
OUT=
URL=
take_out=0
for a in "$@"; do
  if [ "$take_out" = 1 ]; then
    OUT=$a
    take_out=0
    continue
  fi
  case $a in
    -o) take_out=1 ;;
    http*) URL=$a ;;
  esac
done
case "$URL" in
  */token?*)
    printf '{"token":"stub"}\n' >"$OUT"
    exit 0
    ;;
  */manifests/sha-*)
    sha=${URL##*/manifests/sha-}
    if grep -qx "$sha" "$BUILT"; then
      printf 'HTTP/2 200\r\nDocker-Content-Digest: sha256:%s\r\n\r\n' "$(printf '%s' "$sha" | cut -c1-12)0000"
      exit 0
    fi
    exit 22
    ;;
esac
printf 'stub: unexpected url %s\n' "$URL" >&2
exit 22
SH
chmod 755 "$BIN/curl"

run_step() { # <script> -> runs it with GITHUB_OUTPUT/STEP_SUMMARY captured; prints stdout+stderr
  : >"$WORK/out"
  : >"$WORK/summary"
  PATH="$BIN:$PATH" GITHUB_OUTPUT="$WORK/out" GITHUB_STEP_SUMMARY="$WORK/summary" \
    RUNNER_TEMP="$WORK" REGISTRY=ghcr.io IMAGE_NAME=owner/app bash "$WORK/$1" 2>&1 || echo "EXIT=$?"
}
out() { # <key> -> value from GITHUB_OUTPUT
  sed -n "s/^$1=//p" "$WORK/out" | head -1
}

# ── Derive version tags ──────────────────────────────────────────────────────
export CHANNEL=dev VERSION_INPUT=v1.3.0 DEV_VERSION=v1.3.0-dev.4 RELEASE_NEEDED=true FINALIZE=false
run_step derive.sh >/dev/null
chk "D-V1 dev tag is the dev version" "$(out tag)" "v1.3.0-dev.4"
chk "D-V2 dev publishes when a version is due" "$(out publish)" "true"
chk "D-V3 dev keeps the stable base for vX.Y" "$(out minor)" "v1.3"
RELEASE_NEEDED=false
run_step derive.sh >/dev/null
chk "D-V4 dev rerun at a tagged commit publishes nothing" "$(out publish)" "false"
DEV_VERSION=""
RELEASE_NEEDED=true
chk_has "D-V5 dev without dev-version refuses" "$(run_step derive.sh)" "EXIT=1"
export CHANNEL=stable DEV_VERSION="" RELEASE_NEEDED=false FINALIZE=false
run_step derive.sh >/dev/null
chk "D-V6 stable tag is the stable version" "$(out tag)" "v1.3.0"
chk "D-V7 stable publishes nothing without a release" "$(out publish)" "false"
FINALIZE=true
run_step derive.sh >/dev/null
chk "D-V8 stable finalize republishes" "$(out publish)" "true"
FINALIZE=false RELEASE_NEEDED=true
run_step derive.sh >/dev/null
chk "D-V9 stable release publishes" "$(out publish)" "true"
chk_has "D-V9 stable meta tags carry latest" "$(sed -n '/^meta_tags<<EOF$/,/^EOF$/p' "$WORK/out")" "type=raw,value=latest"
export CHANNEL=nightly
chk_has "D-V10 an unknown channel refuses" "$(run_step derive.sh)" "EXIT=1"
# The highest-priority rule is what metadata-action writes into
# org.opencontainers.image.version, so a build from source is labelled with
# its version on either channel, never with `sha-<commit>`.
top_rule() { sed -n '/^meta_tags<<EOF$/,/^EOF$/p' "$WORK/out" | grep -o 'type=[^,]*,[^,]*,priority=[0-9]*' | awk -F',priority=' '$2 + 0 > max { max = $2 + 0; rule = $1 } END { print rule }'; }
export CHANNEL=stable DEV_VERSION="" RELEASE_NEEDED=true FINALIZE=false
run_step derive.sh >/dev/null
chk "D-V11 the stable version tag has the highest priority" "$(top_rule)" "type=raw,value=v1.3.0"
export CHANNEL=dev DEV_VERSION=v1.3.0-dev.4
run_step derive.sh >/dev/null
chk "D-V11 the dev version tag has the highest priority" "$(top_rule)" "type=raw,value=v1.3.0-dev.4"
unset CHANNEL VERSION_INPUT DEV_VERSION RELEASE_NEEDED FINALIZE

# ── Verify root module path ──────────────────────────────────────────────────
# An image app keeps the plain repository path at every major.
chk "D-M1 the module path check runs only when something publishes" \
  "$(cat "$WORK/modpath.sh.if")" "\${{ steps.tags.outputs.publish == 'true' }}"
APP="$WORK/modapp"
mkdir -p "$APP"
modpath_in() { # <go.mod body or ''> -> step output; no argument removes go.mod
  rm -f "$APP/go.mod"
  [ "$#" -eq 0 ] || printf '%s\n' "$1" >"$APP/go.mod"
  (cd "$APP" && GITHUB_REPOSITORY=owner/app run_step modpath.sh)
}
out_mod=$(modpath_in 'module github.com/owner/app')
chk "D-M2 the plain repository path passes" "$(printf '%s' "$out_mod" | grep -c '^EXIT=' || true)" "0"
out_mod=$(modpath_in 'module github.com/owner/app/v4')
chk "D-M3 a /vN suffix fails" "$(printf '%s' "$out_mod" | sed -n 's/^EXIT=//p')" "1"
chk_has "D-M3 the /vN error names the fix" "$out_mod" "drop the /vN suffix from go.mod and rewrite internal imports; apps use the plain module path"
out_mod=$(modpath_in 'module github.com/owner/other')
chk "D-M4 another repository's path fails" "$(printf '%s' "$out_mod" | sed -n 's/^EXIT=//p')" "1"
chk_has "D-M4 the error names the expected path" "$out_mod" "must be 'github.com/owner/app'"
out_mod=$(modpath_in 'go 1.27')
chk "D-M5 a go.mod with no module directive fails" "$(printf '%s' "$out_mod" | sed -n 's/^EXIT=//p')" "1"
out_mod=$(modpath_in)
chk "D-M6 a repo with no root go.mod passes" "$(printf '%s' "$out_mod" | grep -c '^EXIT=' || true)" "0"

# ── Fixture repository for the digest walk ───────────────────────────────────
# release.yaml's exclusion list, joined the way its detect step joins it.
EXCLUDE_RE_FULL='(^|/)[^/]*\.md$|^\.github/|^docs/|(^|/)[^/]*_test\.go$|^tests/'
REPO="$WORK/repo"
git init -q -b main "$REPO"
commit_in() { # <repo> <message> [file=content ...]; no file makes an empty commit
  local repo="$1" msg="$2"
  shift 2
  for kv in "$@"; do
    mkdir -p "$(dirname "$repo/${kv%%=*}")"
    printf '%s\n' "${kv#*=}" >"$repo/${kv%%=*}"
    git -C "$repo" add "${kv%%=*}"
  done
  git -C "$repo" commit -q --allow-empty -m "$msg"
  git -C "$repo" rev-parse HEAD
}
commit() { commit_in "$REPO" "$@"; }
C_BUILT=$(commit "feat: first" main.go=v1 README.md=a)
C_DOCS=$(commit "docs: readme" README.md=b)
C_SHIP=$(commit "fix: main" main.go=v2)
C_REVERT=$(commit "revert: main" main.go=v1)
C_DOCS2=$(commit "docs: more" docs/x.md=x)
C_EMPTY=$(commit "fix(deps): rebuild against refreshed base packages")
C_DOCS3=$(commit "docs: after empty" README.md=c)
# A fixture commit that failed to land makes every walk case vacuous.
chk "D-F1 the fixture holds seven distinct commits" \
  "$(printf '%s\n' "$C_BUILT" "$C_DOCS" "$C_SHIP" "$C_REVERT" "$C_DOCS2" "$C_EMPTY" "$C_DOCS3" | grep -c '^[0-9a-f]\{40\}$')" "7"
chk "D-F2 the empty commit changes no file" "$(git -C "$REPO" diff --name-only "${C_EMPTY}^" "$C_EMPTY")" ""
chk "D-F3 the revert restores the built tree" "$(git -C "$REPO" diff --name-only "$C_BUILT" "$C_REVERT")" "README.md"

promote_at() { # <head commit> <exclude re> [built commits...] -> runs the walk; prints digest|source
  local head="$1" re="$2"
  shift 2
  : >"$BUILT"
  for c in "$@"; do echo "$c" >>"$BUILT"; done
  (cd "$REPO" && GITHUB_SHA="$head" EXCLUDE_RE="$re" run_step promote.sh) >"$WORK/promote.log"
  printf '%s|%s' "$(out promote_digest)" "$(out promote_source)"
}

r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT" "$C_DOCS")
chk "D-W1 an exact sha- hit promotes this commit's own digest" "${r#*|}" "$C_DOCS"
r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT")
chk "D-W2 a docs-only target inherits the ancestor's digest" "${r#*|}" "$C_BUILT"
chk_has "D-W2 summary names the promoted build" "$(cat "$WORK/summary")" "built for \`${C_BUILT}\`"
r=$(promote_at "$C_SHIP" "$EXCLUDE_RE_FULL" "$C_BUILT")
chk "D-W3 a shipped change since builds from source" "${r%|*}" ""
chk_has "D-W3 the log names the shipped path" "$(cat "$WORK/promote.log")" "changes a shipped path"
r=$(promote_at "$C_REVERT" "$EXCLUDE_RE_FULL" "$C_BUILT")
chk "D-W4 a shipped change later reverted still builds from source" "${r%|*}" ""
chk "D-W4 the endpoint trees are equal, so only a per-commit walk can refuse" \
  "$(git -C "$REPO" diff --name-only "$C_BUILT" "$C_REVERT" | grep -Ev "$EXCLUDE_RE_FULL" || true)" ""
r=$(promote_at "$C_DOCS2" "$EXCLUDE_RE_FULL" "$C_BUILT" "$C_REVERT")
chk "D-W5 docs on top of a rebuilt revert inherit the revert's digest" "${r#*|}" "$C_REVERT"
r=$(promote_at "$C_DOCS3" "$EXCLUDE_RE_FULL" "$C_REVERT")
chk "D-W6 an empty commit since (a forced rebuild) builds from source" "${r%|*}" ""
chk_has "D-W6 the log names the empty commit" "$(cat "$WORK/promote.log")" "changes no file"
r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL")
chk "D-W7 no dev build anywhere builds from source" "${r%|*}" ""
chk_has "D-W7 summary says so" "$(cat "$WORK/summary")" "No dev build could be reused"
r=$(promote_at "$C_DOCS" "" "$C_BUILT")
chk "D-W8 an empty exclusion list accepts only an exact hit" "${r%|*}" ""
r=$(promote_at "$C_DOCS3" "$EXCLUDE_RE_FULL" "$C_BUILT" "$C_REVERT" "$C_DOCS3")
chk "D-W9 the exact hit wins over any ancestor" "${r#*|}" "$C_DOCS3"

# ── Release decision on a subpackage-only change ─────────────────────────────
# A Go root with a TS subpackage under web/: a change under web/ alone owes a
# release on both channels, through the root tag the go job creates, and owes
# no image rebuild.
SUB="$WORK/subrepo"
git init -q -b main "$SUB"
S_BASE=$(commit_in "$SUB" "feat: initial" go.mod='module example.com/app' main.go=v1 \
  web/jsr.json='{"name":"@o/web","version":"1.2.0"}' web/package.json='{"name":"@o/web","version":"1.2.0"}' web/src/index.ts=v1)
git -C "$SUB" tag v1.2.0 "$S_BASE"
S_WEB=$(commit_in "$SUB" "feat(web): api" web/src/index.ts=v2)
S_ROOT=$(commit_in "$SUB" "fix: main" main.go=v2)
chk "D-R1 changes reads the subpackage list" "$(tr '\n' ' ' <"$WORK/changes.sh.env")" \
  "ANCHOR_SHA BEFORE CHANNEL GO_LANES_JSON HEAD REPO_TYPE SUBPACKAGES_JSON "
chk "D-R1 select reads the subpackage list" "$(grep -c '^SUBPACKAGES_TO_PUBLISH$' "$WORK/select.sh.env")" "1"
changes_at() { # <anchor> <head> -> root_changed|subpackages_to_publish
  (cd "$SUB" && CHANNEL=stable BEFORE="$1" HEAD="$2" ANCHOR_SHA="$1" SUBPACKAGES_JSON='["web"]' GO_LANES_JSON='[]' REPO_TYPE=go \
    run_step changes.sh) >"$WORK/changes.log"
  printf '%s|%s' "$(out root_changed)" "$(out subpackages_to_publish)"
}
select_at() { # <channel> <root_changed> <subpackages_to_publish> <anchor> <head> -> release|version
  CHANNEL="$1" ROOT_CHANGED="$2" SUBPACKAGES_TO_PUBLISH="$3" ANCHOR_SHA="$4" GITHUB_SHA="$5" \
    BASE=v1.3.0 DEV_VERSION=v1.3.0-dev.1 FLOOR_BASE=v1.2.1 FLOOR_DEV_VERSION=v1.2.1-dev.1 LATEST=v1.2.0 \
    run_step select.sh >/dev/null
  printf '%s|%s' "$(out release)" "$(out version)"
}
r=$(changes_at "$S_BASE" "$S_WEB")
chk "D-R2 a web/ change is the subpackage's, not the root's" "$r" 'false|["web"]'
chk "D-R3 the subpackage-only change releases on dev at the dev version" \
  "$(select_at dev "${r%|*}" "${r#*|}" "$S_BASE" "$S_WEB")" "true|v1.3.0-dev.1"
chk "D-R3 and on stable at the stable base" \
  "$(select_at stable "${r%|*}" "${r#*|}" "$S_BASE" "$S_WEB")" "true|v1.3.0"
chk "D-R4 a commit already tagged on this channel releases nothing" \
  "$(select_at dev "${r%|*}" "${r#*|}" "$S_WEB" "$S_WEB")" "false|v1.3.0-dev.1"
chk "D-R5 no root change and no subpackage change releases nothing" \
  "$(select_at dev false '[]' "$S_BASE" "$S_WEB")" "false|v1.3.0-dev.1"
r=$(changes_at "$S_WEB" "$S_ROOT")
chk "D-R6 a root change is the root's alone" "$r" 'true|[]'
chk "D-R6 and releases" "$(select_at dev true '[]' "$S_WEB" "$S_ROOT")" "true|v1.3.0-dev.1"
# The job gates: the root tag job and the subpackage job fire on `release`
# without a channel condition, needs-ordered tag before publish; the image
# job and its publish input also require root_changed.
chk "D-R7 the go job fires on a subpackage change" \
  "$(grep -c "needs.detect.outputs.subpackages_to_publish != '\[\]'" "$WORK/go.if")" "1"
chk_has "D-R7 the subpackage job fires on release" "$(cat "$WORK/subpackage.if")" "needs.detect.outputs.release == 'true'"
chk "D-R7 the subpackage job has no channel condition" "$(grep -c 'channel' "$WORK/subpackage.if" || true)" "0"
chk_has "D-R7 the subpackage job runs after the root tag job" "$(cat "$WORK/subpackage.needs")" "go"
chk_has "D-R8 the docker job requires a root change" "$(cat "$WORK/docker.if")" \
  "(needs.detect.outputs.release == 'true' && needs.detect.outputs.root_changed == 'true')"
chk_has "D-R8 and publishes only on a root change" "$(cat "$WORK/docker-release-needed.txt")" \
  "needs.detect.outputs.release == 'true' && needs.detect.outputs.root_changed == 'true'"

# ── Resolve build args ───────────────────────────────────────────────────────
printf 'FROM scratch\nARG BUILD_VERSION=dev\nARG PKG_REFRESH\nRUN echo "$BUILD_VERSION" "$PKG_REFRESH"\n' >"$WORK/Dockerfile"
export DOCKERFILE="$WORK/Dockerfile"
TAG=v1.3.0-dev.4 run_step buildargs.sh >/dev/null
chk "D-B1 a dev build is stamped with its dev tag" \
  "$(sed -n '/^build-args<<EOF$/,/^EOF$/p' "$WORK/out" | grep '^BUILD_VERSION=')" "BUILD_VERSION=v1.3.0-dev.4"
TAG=v1.3.0 run_step buildargs.sh >/dev/null
chk "D-B2 a stable build is stamped with its stable tag" \
  "$(sed -n '/^build-args<<EOF$/,/^EOF$/p' "$WORK/out" | grep '^BUILD_VERSION=')" "BUILD_VERSION=v1.3.0"
chk_has "D-B3 PKG_REFRESH is today's date" "$(cat "$WORK/out")" "PKG_REFRESH=$(date -u +%Y-%m-%d)"
printf 'FROM scratch\n' >"$WORK/Dockerfile"
TAG=v1.3.0 run_step buildargs.sh >/dev/null
chk "D-B4 a Dockerfile declaring neither gets no build arg" \
  "$(sed -n '/^build-args<<EOF$/,/^EOF$/p' "$WORK/out" | grep -c '=' || true)" "0"
unset DOCKERFILE

# ── Soak-override handoff ────────────────────────────────────────────────────
chk "D-S1 the record is read on the stable channel only" "$(cat "$WORK/record.if")" \
  "\${{ steps.channel.outputs.channel == 'stable' }}"
chk "D-S2 the docker job forwards the note" "$(cat "$WORK/docker-with.txt")" \
  '${{ needs.detect.outputs.promotion_note }}'
chk "D-S3 docker notes read the note input" "$(sed -n 's/^PROMOTION_NOTE=//p' "$WORK/notes.sh.envmap")" \
  '${{ inputs.promotion-note }}'
chk_has "D-S4 docker notes append the note" "$(cat "$WORK/notes.sh")" 'printf '"'"'\n%s\n'"'"' "$PROMOTION_NOTE" >> RELEASE_NOTES.md'
for job in go ts; do
  chk "D-S5 $job notes read the note" "$(cat "$WORK/$job-notes.env")" '${{ needs.detect.outputs.promotion_note }}'
  chk_has "D-S6 $job notes append the note" "$(cat "$WORK/$job-notes.sh")" 'printf '"'"'\n%s\n'"'"' "$PROMOTION_NOTE" >> NOTES.md'
done
# The stub gh answers the combined-status read; GH_SPEC is the description
# it returns, and GH_FAIL makes it fail like a transient API error.
export GH_SPEC="$WORK/gh.spec" GH_FAIL="$WORK/gh.fail"
cat >"$BIN/gh" <<'SH'
#!/bin/sh
if [ -f "$GH_FAIL" ]; then
  echo "gh: HTTP 502" >&2
  exit 1
fi
if [ -s "$GH_SPEC" ]; then
  cat "$GH_SPEC"
else
  printf ''
fi
SH
chmod 755 "$BIN/gh"
rm -f "$GH_FAIL"
printf 'hotfix for CVE-2026-1 in the base image\n' >"$GH_SPEC"
out_record=$(GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" run_step record.sh)
chk "D-S7 a recorded reason becomes the notes sentence" "$(out promotion_note)" \
  "_Promoted without the homelab soak: hotfix for CVE-2026-1 in the base image_"
: >"$GH_SPEC"
GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" run_step record.sh >/dev/null
chk "D-S8 no record means no sentence" "$(out promotion_note)" ""
touch "$GH_FAIL"
out_record=$(GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" run_step record.sh)
chk "D-S9 a read failure leaves the release green" "$(out promotion_note)" ""
chk_has "D-S9 and warns" "$out_record" "::warning::could not read the promotion record"
rm -f "$GH_FAIL"
# The description is free text: a newline inside it must not break the
# key=value output write, which the runner would reject.
printf 'first line\r\nsecond line\n' >"$GH_SPEC"
GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" run_step record.sh >/dev/null
chk "D-S13 a multi-line description becomes a one-line note" "$(out promotion_note)" \
  "_Promoted without the homelab soak: first linesecond line_"
chk "D-S13 the output file holds one line" "$(wc -l <"$WORK/out")" "1"
rm -f "$BIN/gh"
# The stub gh ignores --jq, so the filter itself is run through jq here.
JQ_FILTER=$(sed -n "s/^ *FILTER='\(.*\)'$/\1/p" "$WORK/record.sh")
chk "D-S10 the step carries one jq filter" "$(printf '%s' "$JQ_FILTER" | grep -c 'promotion/soak-override')" "1"
# A promotion record row: state, description, the day of its updated_at, and
# its id (the day unless given, so ids order like the days).
prow() {
  printf '{"context":"promotion/soak-override","state":"%s","description":"%s","updated_at":"2026-09-%sT10:00:00Z","id":%s}' "$1" "$2" "$3" "${4:-$3}"
}
payload='{"statuses":[{"context":"homelab/soak","state":"success","description":"healthy"},'"$(prow success hotfix 20)"','"$(prow success older 19)"']}'
chk "D-S11 the filter picks the override description" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" "hotfix"
chk "D-S12 the filter yields an empty string without one" "$(printf '{"statuses":[{"context":"homelab/soak"}]}' | jq -r "$JQ_FILTER")" ""
# The promote script records every promotion before the ref update: success
# with the reason for a skipped soak, success with an empty description
# otherwise, failure when main did not move. Only the newest row by updated_at,
# then by id, counts, whatever order the API lists the rows in, and only a
# success with a reason is one.
for state in pending failure; do
  payload='{"statuses":['"$(prow "$state" hotfix 20)"']}'
  chk "D-S14 a $state override is not a reason" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" ""
done
payload='{"statuses":['"$(prow success "" 20)"','"$(prow success hotfix 19)"']}'
chk "D-S15 a normal promotion's empty record retires an older reason" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" ""
payload='{"statuses":['"$(prow failure "main moved" 20)"','"$(prow success hotfix 19)"']}'
chk "D-S15 a newer failure hides an older reason" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" ""
payload='{"statuses":['"$(prow success hotfix 19)"','"$(prow success "" 20)"']}'
chk "D-S15 the newest row decides when the API lists it last" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" ""
payload='{"statuses":['"$(prow failure "main moved" 19)"','"$(prow success hotfix 20)"']}'
chk "D-S15 a newer success listed last outranks an older failure" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" "hotfix"
# The live tie: promote writes success, the ref update raises, and the failure
# row lands in the same second with the higher id.
payload='{"statuses":['"$(prow success hotfix 20 1)"','"$(prow failure "main moved" 20 2)"']}'
chk "D-S16 on a same-second tie the higher id decides, success listed first" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" ""
payload='{"statuses":['"$(prow failure "main moved" 20 2)"','"$(prow success hotfix 20 1)"']}'
chk "D-S16 on a same-second tie the higher id decides, failure listed first" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" ""
payload='{"statuses":[{"context":"promotion/soak-override","state":"success","updated_at":"2026-09-20T10:00:00Z","id":20}]}'
chk "D-S15 a success without a description is no reason" "$(printf '%s' "$payload" | jq -r "$JQ_FILTER")" ""

# ── Dev tag receipt ──────────────────────────────────────────────────────────
# The stub gh records every call and answers the git-ref read as absent (404)
# unless GH_TAG_EXISTS names a commit; GH_REF_FAIL makes the ref create fail,
# GH_HEAD is what the branch head then reads as, and GH_STATUS_FAIL makes the
# receipt POST fail.
export GH_LOG="$WORK/gh.log" GH_TAG_EXISTS="" GH_REF_FAIL="" GH_HEAD="" GH_STATUS_FAIL=""
cat >"$BIN/gh" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >>"$GH_LOG"
case "$*" in
  *"-X DELETE "*"git/refs/tags/"*) exit 0 ;;
  *"git/ref/tags/"*)
    if [ -n "$GH_TAG_EXISTS" ]; then printf '%s\n' "$GH_TAG_EXISTS"; exit 0; fi
    echo "gh: HTTP 404" >&2
    exit 1
    ;;
  *"git/refs "*"refs/tags/"*)
    if [ -n "$GH_REF_FAIL" ]; then echo "gh: HTTP 403" >&2; exit 1; fi
    exit 0
    ;;
  *"git/ref/heads/"*) printf '%s\n' "$GH_HEAD"; exit 0 ;;
  *"/statuses/"*)
    if [ -n "$GH_STATUS_FAIL" ]; then echo "gh: HTTP 502" >&2; exit 1; fi
    exit 0
    ;;
esac
printf 'stub: unexpected gh %s\n' "$*" >&2
exit 22
SH
chmod 755 "$BIN/gh"
: >"$GH_LOG"
GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" GITHUB_REF_NAME=dev GITHUB_SERVER_URL=https://gh GITHUB_RUN_ID=7 \
  run_step taghelper.sh >/dev/null
out_tag=$(GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" GITHUB_REF_NAME=dev GITHUB_SERVER_URL=https://gh GITHUB_RUN_ID=7 \
  TAG=v1.3.0-dev.4 run_step devtag.sh)
chk "D-T1 the dev tag is created at this commit" "$(grep -c "git/refs -f ref=refs/tags/v1.3.0-dev.4 -f sha=$C_BUILT" "$GH_LOG")" "1"
chk "D-T2 the receipt is posted on the same commit after the tag" \
  "$(sed -n '/git\/refs -f ref=/,$p' "$GH_LOG" | grep -c "api -X POST repos/owner/app/statuses/$C_BUILT -f state=success -f context=release/tag/v1.3.0-dev.4 ")" "1"
chk "D-T3 the receipt links the run" "$(grep -c 'target_url=https://gh/owner/app/actions/runs/7' "$GH_LOG")" "1"
chk "D-T4 the step exits 0" "$(printf '%s' "$out_tag" | grep -c '^EXIT=' || true)" "0"
: >"$GH_LOG"
out_tag=$(GH_REF_FAIL=1 GH_HEAD="$C_DOCS" GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" GITHUB_REF_NAME=dev \
  GITHUB_SERVER_URL=https://gh GITHUB_RUN_ID=7 TAG=v1.3.0-dev.4 run_step devtag.sh)
chk "D-T5 a superseded run hands off without a receipt" "$(grep -c 'statuses/' "$GH_LOG")" "0"
chk_has "D-T5 and warns" "$out_tag" "::warning::tag v1.3.0-dev.4 not created"
chk "D-T5 and exits 0" "$(printf '%s' "$out_tag" | grep -c '^EXIT=' || true)" "0"
: >"$GH_LOG"
out_tag=$(GH_TAG_EXISTS="$C_DOCS" GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" GITHUB_REF_NAME=dev \
  GITHUB_SERVER_URL=https://gh GITHUB_RUN_ID=7 TAG=v1.3.0-dev.4 run_step devtag.sh)
chk "D-T6 a tag elsewhere refuses and leaves no receipt" "$(grep -c 'statuses/' "$GH_LOG")" "0"
chk "D-T6 and exits 1" "$(printf '%s' "$out_tag" | sed -n 's/^EXIT=//p')" "1"
# A tag without its receipt makes every rerun compute release=false, so the
# failed POST takes the tag this attempt created with it and a rerun makes both.
export GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" GITHUB_REF_NAME=dev GITHUB_SERVER_URL=https://gh GITHUB_RUN_ID=7
: >"$GH_LOG"
out_tag=$(GH_STATUS_FAIL=1 TAG=v1.3.0-dev.4 run_step devtag.sh)
chk "D-T10 a failed receipt POST deletes the tag this attempt created" \
  "$(grep -c "api -X DELETE repos/owner/app/git/refs/tags/v1.3.0-dev.4" "$GH_LOG")" "1"
chk "D-T10 the deletion follows the create and the failed POST" \
  "$(grep -E 'git/refs -f ref=|/statuses/|-X DELETE' "$GH_LOG" | sed 's/.*-X DELETE.*/DELETE/; s/.*git\/refs -f.*/CREATE/; s/.*statuses.*/POST/' | tr '\n' ' ')" "CREATE POST DELETE "
chk "D-T10 and exits 1" "$(printf '%s' "$out_tag" | sed -n 's/^EXIT=//p')" "1"
chk_has "D-T10 and says why" "$out_tag" "::error::receipt for v1.3.0-dev.4 not recorded"
: >"$GH_LOG"
out_tag=$(TAG=v1.3.0-dev.4 run_step devtag.sh)
chk "D-T11 the rerun recreates the tag once" "$(grep -c "git/refs -f ref=refs/tags/v1.3.0-dev.4 -f sha=$C_BUILT" "$GH_LOG")" "1"
chk "D-T11 and records one receipt" "$(grep -c "api -X POST repos/owner/app/statuses/$C_BUILT -f state=success -f context=release/tag/v1.3.0-dev.4 " "$GH_LOG")" "1"
chk "D-T11 and exits 0" "$(printf '%s' "$out_tag" | grep -c '^EXIT=' || true)" "0"
: >"$GH_LOG"
out_tag=$(GH_STATUS_FAIL=1 GH_TAG_EXISTS="$C_BUILT" TAG=v1.3.0-dev.4 run_step devtag.sh)
chk "D-T12 a failed POST never deletes a tag that predated the attempt" "$(grep -c -- '-X DELETE' "$GH_LOG")" "0"
chk "D-T12 and still exits 1" "$(printf '%s' "$out_tag" | sed -n 's/^EXIT=//p')" "1"
# The three release.yaml tag steps are executed the same way; their bodies
# read VERSION and CHANNEL and never reach `gh release` on the dev channel.
for job in go ts go-nested; do
  : >"$GH_LOG"
  out_tag=$(GH_STATUS_FAIL=1 VERSION=v1.3.0-dev.4 CHANNEL=dev run_step "$job-tag.sh")
  chk "D-T13 the $job step deletes the tag it created when the receipt POST fails" \
    "$(grep -c "api -X DELETE repos/owner/app/git/refs/tags/v1.3.0-dev.4" "$GH_LOG")" "1"
  chk "D-T13 the $job step exits 1" "$(printf '%s' "$out_tag" | sed -n 's/^EXIT=//p')" "1"
  : >"$GH_LOG"
  out_tag=$(GH_STATUS_FAIL=1 GH_TAG_EXISTS="$C_BUILT" VERSION=v1.3.0-dev.4 CHANNEL=dev run_step "$job-tag.sh")
  chk "D-T14 the $job step keeps a pre-existing tag on a failed POST" "$(grep -c -- '-X DELETE' "$GH_LOG")" "0"
  : >"$GH_LOG"
  out_tag=$(VERSION=v1.3.0-dev.4 CHANNEL=dev run_step "$job-tag.sh")
  chk "D-T15 the $job step tags and records the receipt on a clean run" \
    "$(grep -c -E "git/refs -f ref=refs/tags/v1.3.0-dev.4 -f sha=$C_BUILT|api -X POST repos/owner/app/statuses/$C_BUILT -f state=success -f context=release/tag/v1.3.0-dev.4 " "$GH_LOG")" "2"
  chk "D-T15 the $job step exits 0" "$(printf '%s' "$out_tag" | grep -c '^EXIT=' || true)" "0"
done
unset GITHUB_REPOSITORY GITHUB_SHA GITHUB_REF_NAME GITHUB_SERVER_URL GITHUB_RUN_ID
rm -f "$BIN/gh"
for job in go ts go-nested; do
  chk "D-T7 the $job tag step posts the receipt" \
    "$(grep -c 'context="release/tag/${VERSION}"' "$WORK/$job-tag.sh")" "1"
  chk_has "D-T8 the $job receipt sits in the dev arm" "$(sed -n '/CHANNEL" != "stable"/,/exit 0/p' "$WORK/$job-tag.sh")" 'release/tag/${VERSION}'
done
chk "D-T9 every tagging job and the caller hold the statuses scope" "$(tr '\n' ' ' <"$WORK/statuses-scope.txt")" \
  "docker=True docker-release/finalize=True go=True go-nested=True template/release=True ts=True "

echo "PASS ($PASS checks)"
