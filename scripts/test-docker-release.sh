#!/usr/bin/env bash
# Probe of the release-path shell in docker-release.yaml (tag derivation, the
# promoted-digest step and promote-digest.sh, build args, the notes and
# previous-SBOM steps, the dev tag, the stable Release, the receipt) and
# release.yaml (changed paths, version selection, the kind line, the tag steps).
# Bodies are EXTRACTED at runtime and run in fixture repositories against stub
# curl, gh and cosign, so the test cannot drift from what ships. Expected
# values quoting workflow expressions are literal.
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
        "Verify the promoted build's signature and SBOM": "provenance.sh",
        "Fetch the previous release's SBOM": "prevsbom.sh",
        "Generate release notes": "notes.sh",
        "Prepare tag-create helper": "taghelper.sh",
        "Create dev tag": "devtag.sh",
        "Create release": "createrel.sh",
        "Verify dashboard release assets": "verifydash.sh",
    },
    "receipt": {"Record the completion receipt": "receipt.sh"},
    "repair-assets": {"Check the Release": "rassets-check.sh", "Sign the release assets": "rassets.sh"},
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
# Gates and order the extracted bodies cannot show.
def names(job):
    return [s.get("name") for s in jobs[job]["steps"]]
def step_if(job, name):
    return " ".join(str(next(s for s in jobs[job]["steps"] if s.get("name") == name).get("if", "")).split())
fin = names("finalize")
prep = names("prepare")
wf_text = open(wf).read()
wf_inputs = yaml.safe_load(wf_text)[True]["workflow_call"]["inputs"]
gates = {
    "meta-if": step_if("prepare", "Extract metadata"),
    "prepare-cosign-if": step_if("prepare", "Install Cosign"),
    "prepare-cosign-order": prep.index("Install Cosign") < prep.index("Resolve promoted digest"),
    "prepare-cosign-uses": next(s for s in jobs["prepare"]["steps"] if s.get("name") == "Install Cosign")["uses"],
    "build-if": " ".join(str(jobs["build"]["if"]).split()),
    "prevsbom-if": step_if("finalize", "Fetch the previous release's SBOM"),
    "prevsbom-order": fin.index("Generate SBOM (from path)") < fin.index("Fetch the previous release's SBOM") < fin.index("Generate release notes"),
    "provenance-if": step_if("finalize", "Verify the promoted build's signature and SBOM"),
    "before-provenance": fin[: fin.index("Verify the promoted build's signature and SBOM")],
    "first-write": fin[fin.index("Verify the promoted build's signature and SBOM") + 1],
    "receipt-if": " ".join(str(jobs["receipt"]["if"]).split()),
    "receipt-needs": jobs["receipt"]["needs"],
    "receipt-perms": jobs["receipt"].get("permissions"),
    "receipt-step-ifs": [s.get("if", "") for s in jobs["receipt"]["steps"]],
    "vp-perms": jobs["verify-publish"].get("permissions"),
    "prepare-if": str(jobs["prepare"].get("if", "")),
    "ra-if": str(jobs["repair-assets"].get("if", "")),
    "ra-needs": jobs["repair-assets"].get("needs"),
    "ra-perms": jobs["repair-assets"].get("permissions"),
    "ra-steps": names("repair-assets"),
    "ra-step-ifs": [str(s.get("if", "")) for s in jobs["repair-assets"]["steps"][3:]],
    "ra-upload": next(s for s in jobs["repair-assets"]["steps"] if s.get("name") == "Hand the assets on")["with"],
    "ra-cosign": next(s for s in jobs["repair-assets"]["steps"] if s.get("name") == "Install Cosign"),
    "repair-tag-default": wf_inputs["repair-tag"]["default"],
    "released": jobs["finalize"]["outputs"].get("released"),
    "inputs": sorted(wf_inputs),
    "jobs": sorted(jobs),
    "model-default": wf_inputs["release-model"]["default"],
    "soak": wf_text.lower().count("soak"),
    "token-env": sorted(
        k for s in jobs["finalize"]["steps"]
        if s.get("name") in ("Fetch the previous release's SBOM", "Generate release notes")
        for k in s.get("env", {}) if k in ("GH_TOKEN", "GITHUB_TOKEN")
    ),
}
import json
# Which jobs a repair call starts: prepare is skipped, and a job runs only if its
# condition survives that, read as the runner would for the shapes used here.
skipped = {"prepare"}
for name, job in jobs.items():
    if name == "prepare":
        continue
    cond = " ".join(str(job.get("if", "")).split())
    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    if "always()" not in cond and "cancelled()" not in cond:
        hit = any(n in skipped for n in needs)
    else:
        hit = any(f"needs.{n}.result == 'success'" in cond for n in skipped)
    if hit or cond == "${{ inputs.repair-tag == '' }}":
        skipped.add(name)
gates["repair-call-runs"] = [n for n in jobs if n not in skipped]
gates["sarif-if"] = step_if("finalize", "Upload Trivy SARIF")
gates["build-steps"] = names("build")
# Every registry login on the release path, both workflows.
logins = []
for wname, wjobs in (("docker-release", jobs), ("release", rjobs)):
    for jname, job in wjobs.items():
        for step in job.get("steps") or []:
            if str(step.get("name", "")).startswith("Log in to"):
                tag = f"{wname}-{jname}-{step['name'].split()[-1].lower()}"
                logins.append(tag)
                open(f"{out}/login-{tag}.sh", "w").write(step.get("run", ""))
                open(f"{out}/login-{tag}.env", "w").write("".join(f"{k}={v}\n" for k, v in sorted((step.get("env") or {}).items())))
                open(f"{out}/login-{tag}.uses", "w").write(step.get("uses", ""))
gates["logins"] = logins
vp = next(s for s in jobs["verify-publish"]["steps"] if s.get("name") == "Verify published image")
open(f"{out}/vpimage.sh", "w").write(vp["run"])
json.dump(gates, open(f"{out}/gates.json", "w"))
# The release-kind line: detect hands the root lane's sentence to every
# stable notes step, docker-release included.
open(f"{out}/detect-steps.txt", "w").write("".join(f"{s.get('name')}\n" for s in rjobs["detect"]["steps"]))
open(f"{out}/kind-output.txt", "w").write(str(rjobs["detect"]["outputs"].get("release_kind_note", "")))
open(f"{out}/docker-with.json", "w").write(json.dumps({k: str(v) for k, v in rjobs["docker"]["with"].items()}))
for job in ("go", "ts"):
    notes = next(s for s in rjobs[job]["steps"] if s.get("name") == "Generate release notes")
    open(f"{out}/{job}-notes.env", "w").write(str(notes.get("env", {}).get("KIND_NOTE", "")))
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
# The release decision: a subpackage-only change releases on both channels
# through the root tag job; only two-branch builds the image for it (D-R10).
for name, fname in (("Detect changed paths", "changes.sh"), ("Select version", "select.sh")):
    step = next(s for s in rjobs["detect"]["steps"] if s.get("name") == name)
    open(f"{out}/{fname}", "w").write(step["run"])
    open(f"{out}/{fname}.env", "w").write("\n".join(sorted(step.get("env", {}))) + "\n")
for job in ("docker", "go", "ts", "subpackage"):
    open(f"{out}/{job}.if", "w").write(" ".join(str(rjobs[job].get("if", "")).split()))
open(f"{out}/subpackage.needs", "w").write(" ".join(rjobs["subpackage"]["needs"]))
# The docker job's gate and publish input, evaluated per detect outcome: the
# expression grammar used there is &&, ||, ==, != over outputs and results.
import re


def ghx(expr, o):
    e = " ".join(str(expr).split())
    e = e[3:-2] if e.startswith("${{") else e
    e = re.sub(r"needs\.detect\.outputs\.(\w+)", lambda m: repr(o[m.group(1)]), e)
    e = re.sub(r"needs\.\w+\.result", "'success'", e).replace("!cancelled()", "True")
    return eval(e.replace("&&", " and ").replace("||", " or "), {}, {})


cases = {}
for model in ("legacy", "two-branch"):
    for channel in ("dev", "stable"):
        for name, release, root, subs in (
            ("sub", "true", "false", '["web"]'),
            ("root", "true", "true", "[]"),
            ("none", "false", "false", "[]"),
        ):
            o = {"type": "docker", "channel": channel, "release": release, "root_changed": root,
                 "subpackages_to_publish": subs, "release_model": model}
            d = rjobs["docker"]
            cases[f"{model}/{channel}/{name}"] = f"{bool(ghx(d['if'], o))}|{ghx(d['with']['release-needed'], o)}"
json.dump(cases, open(f"{out}/docker-gate.json", "w"))
PY

for f in derive modpath promote buildargs; do
  chk "D-P1 $f extracted" "$([ -s "$WORK/$f.sh" ] && echo yes || echo no)" "yes"
  chk "D-P2 $f is strict-mode" "$(head -1 "$WORK/$f.sh")" "set -euo pipefail"
  chk "D-P3 $f parses under bash" "$(bash -n "$WORK/$f.sh" 2>/dev/null && echo ok || echo bad)" "ok"
done
chk "D-P4 derive reads the five inputs" "$(tr '\n' ' ' <"$WORK/derive.sh.env")" \
  "CHANNEL DEV_VERSION FINALIZE RELEASE_NEEDED VERSION_INPUT "
chk "D-P5 promote reads the exclusion list, the model and the subpackages" "$(tr '\n' ' ' <"$WORK/promote.sh.env")" "CI_TOOLS EXCLUDE_RE RELEASE_MODEL SUBPACKAGES_JSON "
chk "D-P5 the subpackages are the caller's declared list" "$(sed -n 's/^SUBPACKAGES_JSON=//p' "$WORK/promote.sh.envmap")" '${{ inputs.subpackages }}'
chk_has "D-P5 promote runs the shared digest resolver" "$(cat "$WORK/promote.sh")" 'bash "$CI_TOOLS/promote-digest.sh" "$GITHUB_SHA"'
chk "D-P6 build args read the channel tag" "$(tr '\n' ' ' <"$WORK/buildargs.sh.env")" "DOCKERFILE TAG "
# The extracted body sees only the TAG variable; what feeds it is an
# expression the shell cannot evaluate, so it is pinned by text: the channel
# tag prepare derived, never the stable base a dev build has not earned.
chk "D-P7 BUILD_VERSION is fed from the channel tag" \
  "$(sed -n 's/^TAG=//p' "$WORK/buildargs.sh.envmap")" '${{ needs.prepare.outputs.tag }}'
# A re-tagged digest keeps the dev build's own version label and BUILD_VERSION
# because nothing builds or relabels it: both steps that would are skipped.
gate() { jq -r "$1" "$WORK/gates.json"; }
chk_has "D-P8 no metadata for a promoted digest" "$(gate '."meta-if"')" "steps.promote.outputs.promote_digest == ''"
chk_has "D-P8 no build for a promoted digest" "$(gate '."build-if"')" "needs.prepare.outputs.promote_digest == ''"
chk "D-P8 cosign reaches the digest walk on two-branch stable publishes only, before it runs" \
  "$(gate '."prepare-cosign-if"')|$(gate '."prepare-cosign-order"')" \
  "\${{ inputs.channel == 'stable' && steps.tags.outputs.publish == 'true' && inputs.release-model == 'two-branch' }}|true"
chk "D-P9 the model defaults to legacy" "$(gate '."model-default"')" "legacy"
chk "D-P9 the inputs carry the kind line, the model and the security merges, no promotion note" \
  "$(gate '.inputs | map(select(test("note|model|security"))) | join(" ")')" "release-kind-note release-model security-shas"
chk "D-P10 the workflow says nothing of a soak" "$(gate '.soak')" "0"

# ── Stub curl: the anonymous GHCR token and manifest HEADs ───────────────────
# BUILT lists the commits that have a `sha-<commit>` tag on the registry;
# PKGS lists "<image> <digest>" and TAGS "<image>:<tag> <digest>" lines that
# exist. CURL_DOWN makes every registry read fail as a network error.
BIN="$WORK/bin"
mkdir -p "$BIN"
export BUILT="$WORK/built" PKGS="$WORK/pkgs" TAGS="$WORK/tags" CURL_DOWN=""
: >"$PKGS"
: >"$TAGS"
cat >"$BIN/curl" <<'SH'
#!/bin/sh
OUT=
URL=
FAIL=0
take_out=0
for a in "$@"; do
  if [ "$take_out" = 1 ]; then
    OUT=$a
    take_out=0
    continue
  fi
  case $a in
    -o) take_out=1 ;;
    --*) ;;
    -*f*) FAIL=1 ;;
    http*) URL=$a ;;
  esac
done
answer() { # <status> [digest]
  if [ "$1" != 200 ]; then
    [ "$FAIL" = 1 ] && exit 22
    printf 'HTTP/2 %s\r\n\r\n' "$1"
    exit 0
  fi
  printf 'HTTP/2 200\r\nDocker-Content-Digest: %s\r\n\r\n' "$2"
  exit 0
}
case "$URL" in
  https://github.com/orhun/*)
    : >"$OUT"
    exit 0
    ;;
  */token?*)
    if [ -n "$OUT" ]; then printf '{"token":"stub"}\n' >"$OUT"; else printf '{"token":"stub"}\n'; fi
    exit 0
    ;;
esac
case "$URL" in
  https://ghcr.io/v2/*/manifests/*) ;;
  *)
    printf 'stub: unexpected url %s\n' "$URL" >&2
    exit 22
    ;;
esac
[ -z "$CURL_DOWN" ] || exit 7
image=${URL#*/v2/}
image=${image%%/manifests/*}
ref=${URL##*/manifests/}
case "$ref" in
  sha-*)
    sha=${ref#sha-}
    if grep -qx "$sha" "$BUILT"; then
      answer 200 "sha256:$(printf '%s' "$sha" | cut -c1-12)0000"
    fi
    answer 404
    ;;
  sha256:*)
    if grep -qx "$image $ref" "$PKGS"; then answer 200 "$ref"; fi
    answer 404
    ;;
  *)
    d=$(awk -v k="$image:$ref" '$1 == k { print $2 }' "$TAGS")
    [ -n "$d" ] && answer 200 "$d"
    answer 404
    ;;
esac
SH
chmod 755 "$BIN/curl"

# ── Stub cosign: which build signed and attested an image ────────────────────
# A run pushes sha-<commit> before it signs and attests, so a run that died in
# between left a digest GHCR serves and nothing vouches for. The stub answers
# "<verify|verify-attestation> <digest> <commit>" lines of PROV_OK, for this
# workflow's identity in owner/app only; PROV_DOWN answers no verdict at all.
PBIN="$WORK/pbin"
mkdir -p "$PBIN"
export PROV_OK="$WORK/prov-ok" PROV_LOG="$WORK/prov.log"
cat >"$PBIN/cosign" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$PROV_LOG"
id='--certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp ^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@ --certificate-github-workflow-repository owner/app --certificate-github-workflow-sha'
case "$*" in
  "verify $id "*) verb=verify ;;
  "verify-attestation --type spdxjson $id "*) verb=verify-attestation ;;
  *) echo "stub: unexpected cosign $*" >&2; exit 22 ;;
esac
[ -z "${PROV_DOWN:-}" ] || {
  echo "Error: Get https://rekor.sigstore.dev: read: connection reset by peer" >&2
  exit 1
}
sha=${*: -2:1} ref=${*: -1}
grep -qxF "$verb ${ref##*@} $sha" "$PROV_OK" 2>/dev/null && exit 0
echo "Error: no matching signatures: expected GitHub Workflow SHA not found in certificate" >&2
exit 1
SH
chmod 755 "$PBIN/cosign"

run_step() { # <script> -> runs it with GITHUB_OUTPUT/STEP_SUMMARY captured; prints stdout+stderr
  : >"$WORK/out"
  : >"$WORK/summary"
  PATH="$PBIN:$BIN:$PATH" GITHUB_OUTPUT="$WORK/out" GITHUB_STEP_SUMMARY="$WORK/summary" CI_TOOLS="${CI_TOOLS:-$ROOT/scripts}" \
    RUNNER_TEMP="$WORK" REGISTRY=ghcr.io IMAGE_NAME=owner/app GITHUB_REPOSITORY=owner/app READBACK_SLEEP=0 \
    SUBPACKAGES_JSON="${SUBPACKAGES_JSON-[]}" bash "$WORK/$1" 2>&1 || echo "EXIT=$?"
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
# A stand-in for path-significance.sh's exclude_re output, joined the same way.
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

PROMOTE_REPO="$REPO" WALK_MODEL=""
promote_at() { # <head commit> <exclude re> [built commits...] -> runs the walk; prints digest|source
  local head="$1" re="$2" c
  shift 2
  : >"$BUILT"
  : >"$PROV_OK"
  : >"$PROV_LOG"
  for c in "$@"; do
    echo "$c" >>"$BUILT"
    # Each build signed and attested itself, unless WALK_UNSIGNED or
    # WALK_UNATTESTED names it.
    case " ${WALK_UNSIGNED:-} " in *" $c "*) continue ;; esac
    echo "verify sha256:${c:0:12}0000 $c" >>"$PROV_OK"
    case " ${WALK_UNATTESTED:-} " in *" $c "*) continue ;; esac
    echo "verify-attestation sha256:${c:0:12}0000 $c" >>"$PROV_OK"
  done
  (cd "$PROMOTE_REPO" && GITHUB_SHA="$head" EXCLUDE_RE="$re" RELEASE_MODEL="$WALK_MODEL" SUBPACKAGES_JSON="${WALK_SUBS-[]}" run_step promote.sh) >"$WORK/promote.log"
  printf '%s|%s' "$(out promote_digest)" "$(out promote_source)"
}

# A linear history walks the same under every model; '' is a direct caller.
for WALK_MODEL in "" legacy two-branch; do
  m="[${WALK_MODEL:-unset}]"
  r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT" "$C_DOCS")
  chk "D-W1 $m an exact sha- hit promotes this commit's own digest" "${r#*|}|$(out promote_via)" "$C_DOCS|exact"
  r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT")
  chk "D-W2 $m a docs-only target inherits the ancestor's digest" "${r#*|}|$(out promote_via)" "$C_BUILT|ancestor"
  chk_has "D-W2 $m summary names the promoted build" "$(cat "$WORK/summary")" "built for \`${C_BUILT}\`"
  chk "D-W2 $m only two-branch asks whether the reused image was signed and attested" \
    "$(cut -d' ' -f1 "$PROV_LOG" | tr '\n' ' ')" "$([ "$WALK_MODEL" != two-branch ] || echo 'verify verify-attestation ')"
  r=$(promote_at "$C_SHIP" "$EXCLUDE_RE_FULL" "$C_BUILT")
  chk "D-W3 $m a shipped change since builds from source" "${r%|*}" ""
  chk_has "D-W3 $m the log names the shipped path" "$(cat "$WORK/promote.log")" "changes a shipped path"
  r=$(promote_at "$C_REVERT" "$EXCLUDE_RE_FULL" "$C_BUILT")
  chk "D-W4 $m a shipped change later reverted still builds from source" "${r%|*}" ""
  chk "D-W4 $m the endpoint trees are equal, so only a per-commit walk can refuse" \
    "$(git -C "$REPO" diff --name-only "$C_BUILT" "$C_REVERT" | grep -Ev "$EXCLUDE_RE_FULL" || true)" ""
  r=$(promote_at "$C_DOCS2" "$EXCLUDE_RE_FULL" "$C_BUILT" "$C_REVERT")
  chk "D-W5 $m docs on top of a rebuilt revert inherit the revert's digest" "${r#*|}" "$C_REVERT"
  r=$(promote_at "$C_DOCS3" "$EXCLUDE_RE_FULL" "$C_REVERT")
  chk "D-W6 $m an empty commit since (a forced rebuild) builds from source" "${r%|*}" ""
  chk_has "D-W6 $m the log names the empty commit" "$(cat "$WORK/promote.log")" "changes no file"
  r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL")
  chk "D-W7 $m no dev build anywhere builds from source" "${r%|*}|$(out promote_via)" "|none"
  chk_has "D-W7 $m summary says so" "$(cat "$WORK/summary")" "No dev build could be reused"
  r=$(promote_at "$C_DOCS" "" "$C_BUILT")
  chk "D-W8 $m an empty exclusion list accepts only an exact hit" "${r%|*}" ""
  r=$(promote_at "$C_DOCS3" "$EXCLUDE_RE_FULL" "$C_BUILT" "$C_REVERT" "$C_DOCS3")
  chk "D-W9 $m the exact hit wins over any ancestor" "${r#*|}" "$C_DOCS3"
done
# Under two-branch a reused image must be complete, or the provenance gate
# before the first write would refuse it with nothing to fall back on.
WALK_MODEL=two-branch WALK_UNSIGNED="$C_BUILT"
r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT")
chk "D-W11 a pure commit over an image its run never signed builds from source" "$r|$(out promote_via)" "|$C_DOCS|none"
chk_has "D-W11 naming the missing signature" "$(cat "$WORK/promote.log")" \
  "ghcr.io/owner/app@sha256:${C_BUILT:0:12}0000 carries no signature"
chk_has "D-W11 and the commit it builds instead" "$(cat "$WORK/promote.log")" "building ${C_DOCS} from source"
chk_has "D-W11 summary says so" "$(cat "$WORK/summary")" "No dev build could be reused"
WALK_UNSIGNED="$C_DOCS"
r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT" "$C_DOCS")
chk "D-W11 an unsigned exact hit builds from source, even with a complete image below it" "$r|$(out promote_via)" "|$C_DOCS|none"
WALK_UNSIGNED="" WALK_UNATTESTED="$C_BUILT"
r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT")
chk "D-W12 one signed but never attested builds from source" "$r|$(out promote_via)" "|$C_DOCS|none"
chk_has "D-W12 naming the missing attestation" "$(cat "$WORK/promote.log")" "carries no SPDX SBOM attestation"
WALK_UNATTESTED=""
r=$(PROV_DOWN=1 promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT")
chk "D-W13 a cosign that cannot answer fails the step, neither reusing nor building" \
  "$r|$(sed -n 's/^EXIT=//p' "$WORK/promote.log")" "||1"
chk "D-W13 after retrying" "$(grep -c '^verify ' "$PROV_LOG")" "5"
WALK_MODEL=legacy WALK_UNSIGNED="$C_BUILT"
r=$(promote_at "$C_DOCS" "$EXCLUDE_RE_FULL" "$C_BUILT")
chk "D-W14 legacy reuses the same image unchanged, asking cosign nothing" "$r|$(out promote_via)|$(wc -l <"$PROV_LOG" | tr -d ' ')" \
  "$(printf 'sha256:%s0000' "${C_BUILT:0:12}")|$C_BUILT|ancestor|0"
WALK_UNSIGNED=""
WALK_MODEL=""
chk_has "D-W10 an unknown model is refused" "$(cd "$REPO" && GITHUB_SHA="$C_DOCS" EXCLUDE_RE="" RELEASE_MODEL=v3 run_step promote.sh)" "EXIT=1"

# ── Two-branch: promotion commits on main ────────────────────────────────────
# M0 built on main; dev's T1 changes main.go and T_D only docs; M1 is a main
# docs commit. R promotes T1 over M1 with T1's digest as its trailer; R_D
# promotes the docs-only T_D over M0; S and S_D are docs commits after each.
REC="$WORK/rec"
git init -q -b main "$REC"
M0=$(commit_in "$REC" "feat: first" main.go=v1 README.md=a)
git -C "$REC" checkout -q -b dev
T1=$(commit_in "$REC" "fix: dev change" main.go=v2)
git -C "$REC" checkout -q main
M1=$(commit_in "$REC" "docs: main docs" README.md=b)
dig() { printf 'sha256:%064d' "$1"; }
D_T=$(dig 1) D_OTHER=$(dig 2) D_GONE=$(dig 3)
promotion() { # <main parent> <dev parent> [trailer...] -> the reconciliation commit
  local m=$1 t=$2 msg='release: promote dev into main'
  shift 2
  if [ "$#" -gt 0 ]; then
    msg="${msg}

$(printf 'Promoted-Digest: %s\n' "$@")"
  fi
  git -C "$REC" commit-tree "${t}^{tree}" -p "$m" -p "$t" -m "$msg"
}
R=$(promotion "$M1" "$T1" "$D_T")
R_OTHER=$(promotion "$M1" "$T1" "$D_OTHER")
R_GONE=$(promotion "$M1" "$T1" "$D_GONE")
R_BARE=$(promotion "$M1" "$T1")
git -C "$REC" update-ref refs/heads/main "$R"
git -C "$REC" checkout -q -f main
S=$(commit_in "$REC" "docs: after the promotion" README.md=c)
git -C "$REC" checkout -q -b docs-promo "$M0"
T_D=$(commit_in "$REC" "docs: dev docs" README.md=d)
R_D=$(promotion "$M0" "$T_D")
git -C "$REC" update-ref refs/heads/docs-promo "$R_D"
git -C "$REC" checkout -q -f docs-promo
S_D=$(commit_in "$REC" "docs: after the docs promotion" docs/y.md=y)
printf '%s\n' "owner/app $D_T" "owner/other $D_OTHER" >"$PKGS"
# shellcheck source=SCRIPTDIR/reconciliation.sh
chk "D-M0 the fixture's promotions have the reconciliation shape" \
  "$(cd "$REC" && . "$ROOT/scripts/reconciliation.sh" && for c in "$R" "$R_D" "$S"; do is_reconciliation "$c" && echo y || echo n; done | tr -d '\n')" "yyn"
PROMOTE_REPO="$REC" WALK_MODEL=two-branch
r=$(promote_at "$R" "$EXCLUDE_RE_FULL" "$M0" "$T1")
chk "D-M1 a promotion commit re-tags its Promoted-Digest" "$r|$(out promote_via)" "$D_T|$T1|trailer"
chk "D-M1 leaving its signature to the gate that refuses rather than builds (D-G)" "$(wc -l <"$PROV_LOG" | tr -d ' ')" "0"
chk_has "D-M1 summary names the trailer" "$(cat "$WORK/summary")" "this promotion commit records (digest \`${D_T}\`, promoted commit \`${T1}\`)"
out_m=$(cd "$REC" && GITHUB_SHA="$R_OTHER" EXCLUDE_RE="$EXCLUDE_RE_FULL" RELEASE_MODEL=two-branch run_step promote.sh)
chk_has "D-M2 a digest of another image's package is refused" "$out_m" "is not in ghcr.io/owner/app"
chk "D-M2 and fails the step with no digest" "$(printf '%s' "$out_m" | sed -n 's/^EXIT=//p')|$(out promote_digest)" "1|"
out_m=$(cd "$REC" && GITHUB_SHA="$R_GONE" EXCLUDE_RE="$EXCLUDE_RE_FULL" RELEASE_MODEL=two-branch run_step promote.sh)
chk_has "D-M3 a digest retention deleted is refused, never built" "$out_m" "A promotion never builds from source"
chk "D-M3 and fails the step" "$(printf '%s' "$out_m" | sed -n 's/^EXIT=//p')" "1"
out_m=$(cd "$REC" && GITHUB_SHA="$R_BARE" EXCLUDE_RE="$EXCLUDE_RE_FULL" RELEASE_MODEL=two-branch run_step promote.sh)
chk_has "D-M3 a promotion without its trailer is refused" "$out_m" "no single well-formed Promoted-Digest trailer"
out_m=$(cd "$REC" && CURL_DOWN=1 GITHUB_SHA="$R" EXCLUDE_RE="$EXCLUDE_RE_FULL" RELEASE_MODEL=two-branch run_step promote.sh)
chk_has "D-M3 an unreadable registry is refused, never built" "$out_m" "could not read ghcr.io/owner/app@${D_T}"
r=$(promote_at "$S" "$EXCLUDE_RE_FULL" "$M0" "$M1" "$T1")
chk "D-M4 a later main commit over the untagged promotion builds from source" "${r%|*}|$(out promote_via)" "|none"
chk_has "D-M4 the log names the promotion as the merge crossed" "$(cat "$WORK/promote.log")" "commit ${R} is a merge"
# R's own run pushes :sha-R (its stable tag set) before it tags, so a run that
# died after publishing leaves R untagged with an image no main commit signed.
r=$(promote_at "$S" "$EXCLUDE_RE_FULL" "$M0" "$M1" "$T1" "$R")
chk "D-M4 nor does it take the image R's interrupted run pushed as :sha-R" "${r%|*}|$(out promote_via)" "|none"
chk_has "D-M4 naming the merge whose image it refused" "$(cat "$WORK/promote.log")" "the nearest image is the merge ${R}'s"
sources_rec() { (cd "$REC" && RELEASE_MODEL=two-branch EXCLUDE_RE="$EXCLUDE_RE_FULL" SUBPACKAGES_JSON='[]' \
  bash "$ROOT/scripts/promote-digest.sh" --sources "$1" 2>/dev/null | tr '\n' ' '); }
chk "D-M4 and its signing window holds S alone, the commit that builds it" "$(sources_rec "$S")" "$S "
r=$(promote_at "$S_D" "$EXCLUDE_RE_FULL" "$M0")
chk "D-M5 a merge crossing stays impure even when every path it changed is excluded" "${r%|*}" ""
WALK_MODEL=legacy
r=$(promote_at "$S_D" "$EXCLUDE_RE_FULL" "$M0")
chk "D-M5 where legacy's per-path walk inherits across it" "$r" "$(printf 'sha256:%s0000' "${M0:0:12}")|$M0"
r=$(promote_at "$R" "$EXCLUDE_RE_FULL" "$M0" "$T1")
chk "D-M6 legacy never reads a trailer" "$r|$(out promote_via)" "|$R|none"
WALK_MODEL=two-branch
EMPTY_ON_MAIN=$(git -C "$REC" commit-tree "${S_D}^{tree}" -p "$S_D" -m "fix(deps): rebuild against refreshed base packages")
r=$(promote_at "$EMPTY_ON_MAIN" "$EXCLUDE_RE_FULL" "$S_D")
chk "D-M7 an empty fix(deps) commit stays a forced rebuild" "${r%|*}" ""
# A promotion of a subpackage-only dev commit, R's already tagged: detect's
# publish step owes the subpackage alone, the docker job still publishes
# (D-R10), and the image is the trailer's dev digest, re-tagged with no build.
git -C "$REC" checkout -q dev
T_W=$(commit_in "$REC" "feat(web): api" web/jsr.json='{"name":"@o/web"}' web/src/index.ts=v1)
R_W=$(promotion "$S" "$T_W" "$D_T")
: >"$WORK/out"
(cd "$REC" && CHANNEL=stable STATE="{\".\":{\"in_range\":true,\"commit\":\"$R_W\",\"h_commit\":\"$R\"}}" ROOT_CHANGED=false \
  SUBPACKAGES_TO_PUBLISH='[]' GO_MODULES_TO_RELEASE='[]' REPO_TYPE=docker SUBPACKAGES_JSON='["web"]' \
  GO_LANES_JSON='[]' GITHUB_OUTPUT="$WORK/out" bash "$ROOT/scripts/release-state.sh" publish) >/dev/null 2>&1
chk "D-M8 a subpackage-only promotion owes the subpackage, not a root change" \
  "$(out root_changed)|$(out subpackages_to_publish)" 'false|["web"]'
r=$(promote_at "$R_W" "$EXCLUDE_RE_FULL" "$M0" "$T1")
chk "D-M8 and its image re-tags the promoted dev digest" "$r|$(out promote_via)" "$D_T|$T_W|trailer"
# A declared subpackage's shipped manifest owes a release (path-significance.sh),
# so under two-branch that commit's image is built from it, never inherited;
# a devDependencies-only change and a docker repo's root package.json are not.
SPK="$WORK/subpkg"
git init -q -b main "$SPK"
web_pj() { printf '{"name":"@o/web","dependencies":{"a":"%s"},"devDependencies":{"t":"%s"}}' "$1" "$2"; }
P0=$(commit_in "$SPK" "feat: first" main.go=v1 package.json='{"devDependencies":{"x":"1"}}' \
  web/jsr.json='{"name":"@o/web"}' web/package.json="$(web_pj 1.0.0 1)")
P_DEV=$(commit_in "$SPK" "chore(devdeps): update dependency t to v2" web/package.json="$(web_pj 1.0.0 2)" web/package-lock.json=l1)
P_ROOT=$(commit_in "$SPK" "chore(devdeps): update dependency x to v2" package.json='{"devDependencies":{"x":"2"}}')
P_DEP=$(commit_in "$SPK" "fix(deps): update dependency a to v1.0.1" web/package.json="$(web_pj 1.0.1 2)" web/package-lock.json=l2)
changed_of() { git -C "$SPK" diff --name-only "$1^" "$1" | tr '\n' ,; }
chk "D-M9 the fixture's commits each change what they claim" \
  "$(changed_of "$P_DEV") $(changed_of "$P_ROOT") $(changed_of "$P_DEP")" \
  "web/package-lock.json,web/package.json, package.json, web/package-lock.json,web/package.json,"
PROMOTE_REPO="$SPK" WALK_MODEL=two-branch WALK_SUBS='["web"]'
r=$(promote_at "$P_DEP" "$EXCLUDE_RE_FULL" "$P0")
chk "D-M9 a release owed only to a subpackage's runtime dependency builds its image" "$r|$(out promote_via)" "|$P_DEP|none"
chk_has "D-M9 naming the shipped change" "$(cat "$WORK/promote.log")" "commit ${P_DEP} changes a shipped path"
r=$(promote_at "$P_ROOT" "$EXCLUDE_RE_FULL" "$P0")
chk "D-M9 a subpackage devDependencies change and a root package.json carry the image" "${r#*|}|$(out promote_via)" "$P0|ancestor"
r=$(promote_at "$P_DEP" "$EXCLUDE_RE_FULL" "$P0" "$P_DEP")
chk "D-M10 a promoted dev commit that changed a shipped subpackage manifest re-tags its own build" "${r#*|}|$(out promote_via)" "$P_DEP|exact"
r=$(promote_at "$P_DEP" "$EXCLUDE_RE_FULL" "$P0" "$P_ROOT")
chk "D-M10 and with that build gone, never its parent's image" "$r|$(out promote_via)" "|$P_DEP|none"
WALK_SUBS='[]'
r=$(promote_at "$P_DEP" "$EXCLUDE_RE_FULL" "$P0")
chk "D-M11 an undeclared package.json ships nothing, so the image carries over" "${r#*|}|$(out promote_via)" "$P0|ancestor"
WALK_SUBS=''
r=$(promote_at "$P_DEP" "$EXCLUDE_RE_FULL" "$P0")
chk_has "D-M11 a two-branch walk without the subpackage list is refused" "$(cat "$WORK/promote.log")" "SUBPACKAGES_JSON must be the JSON array"
WALK_MODEL=legacy WALK_SUBS='["web"]'
r=$(promote_at "$P_DEP" "${EXCLUDE_RE_FULL}|(^|/)package-lock\.json\$" "$P0")
chk "D-M12 legacy keeps every package.json image-pure" "${r#*|}|$(out promote_via)" "$P0|ancestor"
sources() { (cd "$SPK" && RELEASE_MODEL=two-branch EXCLUDE_RE="$EXCLUDE_RE_FULL" SUBPACKAGES_JSON='["web"]' \
  bash "$ROOT/scripts/promote-digest.sh" --sources "$1" 2>/dev/null | tr '\n' ' '); }
chk "D-M13 --sources stops at a shipped subpackage manifest" "$(sources "$P_DEP")" "$P_DEP "
chk "D-M13 and walks the commits that carried the image" "$(sources "$P_ROOT")" "$P_ROOT $P_DEV $P0 "
WALK_SUBS='[]'
PROMOTE_REPO="$REPO" WALK_MODEL=""

# ── Release decision on a subpackage-only change ─────────────────────────────
# A Go root with a TS subpackage under web/: a change under web/ alone owes a
# release on both channels, through the root tag the go job creates, and is
# no root change.
SUB="$WORK/subrepo"
git init -q -b main "$SUB"
S_BASE=$(commit_in "$SUB" "feat: initial" go.mod='module example.com/app' main.go=v1 \
  web/jsr.json='{"name":"@o/web","version":"1.2.0"}' web/package.json='{"name":"@o/web","version":"1.2.0"}' web/src/index.ts=v1)
git -C "$SUB" tag v1.2.0 "$S_BASE"
S_WEB=$(commit_in "$SUB" "feat(web): api" web/src/index.ts=v2)
S_ROOT=$(commit_in "$SUB" "fix: main" main.go=v2)
chk "D-R1 changes reads the subpackage list" "$(tr '\n' ' ' <"$WORK/changes.sh.env")" \
  "ANCHOR_SHA BEFORE CHANNEL CI_TOOLS GO_LANES_JSON HEAD RELEASE_MODEL REPO_TYPE SUBPACKAGES_JSON "
chk "D-R1 select reads the subpackage list" "$(grep -c '^SUBPACKAGES_TO_PUBLISH$' "$WORK/select.sh.env")" "1"
changes_at() { # <anchor> <head> -> root_changed|subpackages_to_publish
  (cd "$SUB" && CI_TOOLS="$ROOT/scripts" CHANNEL=stable BEFORE="$1" HEAD="$2" ANCHOR_SHA="$1" SUBPACKAGES_JSON='["web"]' GO_LANES_JSON='[]' REPO_TYPE=go \
    run_step changes.sh) >"$WORK/changes.log"
  printf '%s|%s' "$(out root_changed)" "$(out subpackages_to_publish)"
}
select_at() { # <channel> <root_changed> <subpackages_to_publish> <anchor> <head> -> release|version
  CHANNEL="$1" MODE=normal ROOT_CHANGED="$2" SUBPACKAGES_TO_PUBLISH="$3" ANCHOR_SHA="$4" GITHUB_SHA="$5" \
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
# without a channel condition, needs-ordered tag before publish.
chk "D-R7 the go job fires on a subpackage change" \
  "$(grep -c "needs.detect.outputs.subpackages_to_publish != '\[\]'" "$WORK/go.if")" "1"
chk_has "D-R7 the subpackage job fires on release" "$(cat "$WORK/subpackage.if")" "needs.detect.outputs.release == 'true'"
chk "D-R7 the subpackage job has no channel condition" "$(grep -c 'channel' "$WORK/subpackage.if" || true)" "0"
chk_has "D-R7 the subpackage job runs after the root tag job" "$(cat "$WORK/subpackage.needs")" "go"
# The image job, evaluated: <docker job runs>|<release-needed> per
# model/channel/outcome.
dgate() { jq -r --arg k "$1" '.[$k]' "$WORK/docker-gate.json"; }
chk "D-R9 legacy: a subpackage-only release builds no image on dev" "$(dgate legacy/dev/sub)" "False|false"
chk "D-R9 nor publishes one on stable" "$(dgate legacy/stable/sub)" "True|false"
chk "D-R9 and a root change still publishes on both" "$(dgate legacy/dev/root) $(dgate legacy/stable/root)" "True|true True|true"
chk "D-R10 two-branch: a subpackage-only release builds the image on dev" "$(dgate two-branch/dev/sub)" "True|true"
chk "D-R10 and publishes it on stable" "$(dgate two-branch/stable/sub)" "True|true"
chk "D-R10 nothing owed publishes nothing" "$(dgate two-branch/dev/none) $(dgate two-branch/stable/none)" "False|false True|false"
chk_has "D-R10 the subpackage publishes after the image's tag and Release" "$(cat "$WORK/subpackage.needs")" "docker"

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

# ── Release-kind line ────────────────────────────────────────────────────────
chk "D-S1 detect reads no soak record" "$(grep -c 'Read promotion record' "$WORK/detect-steps.txt" || true)" "0"
chk "D-S1 release.yaml reads no soak-override status" "$(grep -c 'soak-override' "$RELEASE_YAML" || true)" "0"
chk "D-S1 the root kind line is the pending step's" "$(cat "$WORK/kind-output.txt")" \
  '${{ steps.pending.outputs.root_kind_note }}'
chk "D-S2 the docker job forwards the kind line" "$(jq -r '."release-kind-note"' "$WORK/docker-with.json")" \
  '${{ needs.detect.outputs.release_kind_note }}'
chk "D-S2 and the model and the security merges, and no promotion note" \
  "$(jq -r '[."release-model", ."security-shas", (has("promotion-note") | tostring)] | join(" ")' "$WORK/docker-with.json")" \
  '${{ needs.detect.outputs.release_model }} ${{ needs.detect.outputs.security_shas }} false'
chk "D-S3 docker notes read the kind-line input" "$(sed -n 's/^KIND_NOTE=//p' "$WORK/notes.sh.envmap")" \
  '${{ inputs.release-kind-note }}'
for job in go ts; do
  chk "D-S5 $job notes read the kind line" "$(cat "$WORK/$job-notes.env")" '${{ needs.detect.outputs.release_kind_note }}'
  chk_has "D-S6 $job notes hand it to the renderer" "$(cat "$WORK/$job-notes.sh")" '--kind-note "$KIND_NOTE"'
done

# ── Docker release notes: what the step hands render-notes.sh ────────────────
# The git-cliff install is stubbed out (curl, sha256sum and tar), and CI_TOOLS
# holds a render-notes.sh that records its argv one argument per line.
NBIN="$WORK/nbin" RN_TOOLS="$WORK/rn-tools"
mkdir -p "$NBIN" "$RN_TOOLS"
printf '#!/bin/sh\ncat >/dev/null\nexit 0\n' >"$NBIN/sha256sum"
printf '#!/bin/sh\nexit 0\n' >"$NBIN/tar"
cat >"$RN_TOOLS/render-notes.sh" <<'SH'
printf '%s\n' "$@" >"$RUNNER_TEMP/rn-argv"
SH
chmod 755 "$NBIN/sha256sum" "$NBIN/tar"
notes_argv() { # env-prefixed: runs the notes step; prints its render-notes argv on one line
  rm -f "$WORK/rn-argv"
  PATH="$NBIN:$BIN:$PATH" CI_TOOLS="$RN_TOOLS" RUNNER_TEMP="$WORK" GITHUB_REPOSITORY=owner/app \
    GITHUB_SHA=abc123 VERSION=v1.3.0 LATEST=v1.2.0 GO_LANES_JSON='["yamlenv"]' \
    bash "$WORK/notes.sh" >"$WORK/notes.log" 2>&1 || echo "EXIT=$?"
  [ -f "$WORK/rn-argv" ] && tr '\n' ' ' <"$WORK/rn-argv"
}
LEGACY_HEAD='--release-model legacy --site docker --version v1.3.0 --go-lanes ["yamlenv"] --kind-note  --out RELEASE_NOTES.md --latest v1.2.0'
chk "D-N1 legacy publish keeps the latest tag and the subject fallback" \
  "$(RELEASE_MODEL=legacy FINALIZE=false RELEASE_NEEDED=true KIND_NOTE="" SECURITY_SHAS="" notes_argv)" "$LEGACY_HEAD --release-needed "
chk "D-N2 legacy finalize renders the current release" \
  "$(RELEASE_MODEL=legacy FINALIZE=true RELEASE_NEEDED=false KIND_NOTE="" SECURITY_SHAS="" notes_argv)" "$LEGACY_HEAD --finalize "
chk "D-N3 a caller passing no model renders legacy" \
  "$(RELEASE_MODEL="" FINALIZE=false RELEASE_NEEDED=false KIND_NOTE="" SECURITY_SHAS="" notes_argv)" "$LEGACY_HEAD "
rm -f "$WORK/sbom-prev.spdx.json"
TWO_HEAD='--release-model two-branch --site docker --version v1.3.0 --go-lanes ["yamlenv"] --kind-note Promoted from `v1.3.0-dev.2` --out RELEASE_NOTES.md --repo owner/app --release-commit abc123'
chk "D-N4 two-branch renders the explicit range with no fallback inputs and no SBOM pair without a previous one" \
  "$(RELEASE_MODEL=two-branch FINALIZE=false RELEASE_NEEDED=true KIND_NOTE='Promoted from `v1.3.0-dev.2`' SECURITY_SHAS="aaa bbb" notes_argv)" \
  "$TWO_HEAD --security-shas $WORK/security-shas "
chk "D-N4 the security merges are one per line" "$(tr '\n' ' ' <"$WORK/security-shas")" "aaa bbb "
printf '{}\n' >"$WORK/sbom-prev.spdx.json"
chk "D-N5 a verified previous SBOM joins the new one" \
  "$(RELEASE_MODEL=two-branch FINALIZE=false RELEASE_NEEDED=true KIND_NOTE='Promoted from `v1.3.0-dev.2`' SECURITY_SHAS="" notes_argv)" \
  "$TWO_HEAD --security-shas $WORK/security-shas --sbom-prev $WORK/sbom-prev.spdx.json --sbom-new sbom.spdx.json "
rm -f "$WORK/sbom-prev.spdx.json"
chk "D-N6 the notes and previous-SBOM steps hold no token" "$(gate '."token-env" | length')" "0"

# ── Two-branch: the previous release's SBOM, verified before it is read ──────
# Stub cosign: verify-attestation exits COSIGN_VERIFY_RC (a transport error)
# and otherwise prints COSIGN_VERIFIED (the attestations it verified) only for
# a certificate "<repository> <commit>" line in COSIGN_SIGNED its flags
# match, answering a mismatch as cosign does; download prints COSIGN_ATT, every attestation
# attached, signed or not. A stub sleep keeps the retry backoff instant.
printf '#!/bin/sh\nexit 0\n' >"$NBIN/sleep"
export COSIGN_LOG="$WORK/cosign.log" COSIGN_VERIFY_RC=0 COSIGN_ATT="$WORK/att.jsonl" COSIGN_VERIFIED="$WORK/verified.jsonl" \
  COSIGN_SIGNED="$WORK/cosign-signed"
cat >"$NBIN/cosign" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >>"$COSIGN_LOG"
case "$1 $2" in
  "verify-attestation --type")
    [ "$COSIGN_VERIFY_RC" = 0 ] || exit "$COSIGN_VERIFY_RC"
    repo="" sha=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --certificate-github-workflow-repository) repo=$2 ;;
        --certificate-github-workflow-sha) sha=$2 ;;
      esac
      shift
    done
    # An absent flag constrains nothing, as in cosign.
    if awk -v r="$repo" -v s="$sha" '(r == "" || $1 == r) && (s == "" || $2 == s) { f = 1 } END { exit !f }' "$COSIGN_SIGNED" 2>/dev/null; then
      cat "$COSIGN_VERIFIED"
      exit 0
    fi
    echo "Error: no matching attestations: none of the expected identities matched what was in the certificate" >&2
    exit 1
    ;;
  "download attestation") cat "$COSIGN_ATT"; exit 0 ;;
  "sign-blob --yes")
    if [ "${COSIGN_SIGN_RC:-0}" != 0 ]; then
      case "$3" in ${COSIGN_SIGN_FAIL:-*}) exit "$COSIGN_SIGN_RC" ;; esac
    fi
    printf 'bundle over %s\n' "$(sha256sum <"$3" | cut -d' ' -f1)" >"$5"
    exit 0
    ;;
esac
echo "stub: unexpected cosign $*" >&2
exit 22
SH
chmod 755 "$NBIN/sleep" "$NBIN/cosign"
NOTESREPO="$WORK/notesrepo"
git init -q -b main "$NOTESREPO"
commit_in "$NOTESREPO" "feat: first" main.go=v1 >/dev/null
prev_sbom() { # env-prefixed: runs the step in NOTESREPO; prints EXIT=n on failure
  : >"$COSIGN_LOG"
  rm -f "$WORK/sbom-prev.spdx.json"
  (cd "$NOTESREPO" && PATH="$NBIN:$BIN:$PATH" CI_TOOLS="$ROOT/scripts" RUNNER_TEMP="$WORK" REGISTRY=ghcr.io GITHUB_REPOSITORY=owner/app \
    IMAGE_NAME=owner/app VERSION=v1.3.0 EXCLUDE_RE="$EXCLUDE_RE_FULL" SUBPACKAGES_JSON="${PREV_SUBS:-[]}" bash "$WORK/prevsbom.sh" 2>&1) || echo "EXIT=$?"
}
SPDX='{"spdxVersion":"SPDX-2.3","packages":[{"SPDXID":"SPDXRef-a","name":"busybox","versionInfo":"1.37.0-r29"}]}'
EVIL='{"spdxVersion":"SPDX-2.3","packages":[{"SPDXID":"SPDXRef-a","name":"busybox","versionInfo":"0.0.0-forged"}]}'
envelope() { # <shape> <subject digest> [predicate] -> one attestation line
  local payload
  payload=$(jq -cn --argjson p "${3:-$SPDX}" --arg d "${2#sha256:}" \
    '{_type: "https://in-toto.io/Statement/v1", subject: [{name: "ghcr.io/owner/app", digest: {sha256: $d}}], predicateType: "https://spdx.dev/Document", predicate: $p}' \
    | base64 | tr -d '\n')
  case "$1" in
    bundle) jq -cn --arg p "$payload" '{dsseEnvelope: {payload: $p, payloadType: "application/vnd.in-toto+json"}}' ;;
    dsse) jq -cn --arg p "$payload" '{payload: $p, payloadType: "application/vnd.in-toto+json"}' ;;
  esac
}
o=$(prev_sbom)
chk_has "D-Q1 a first release has no previous SBOM" "$o" "No earlier stable release exists, so the notes carry no system-package diff."
chk "D-Q1 and asks cosign nothing" "$(wc -l <"$COSIGN_LOG" | tr -d ' ')|$([ -f "$WORK/sbom-prev.spdx.json" ] && echo file || echo none)" "0|none"
git -C "$NOTESREPO" tag v1.2.0
git -C "$NOTESREPO" tag v1.10.0
git -C "$NOTESREPO" tag v1.2.0-dev.1
o=$(prev_sbom)
chk_has "D-Q2 a previous tag missing from the registry is skipped" "$o" "ghcr.io/owner/app:v1.2.0 is not readable, so the notes carry no system-package diff."
chk "D-Q2 by version, not by name, and with no cosign call" "$(wc -l <"$COSIGN_LOG" | tr -d ' ')" "0"
PREV_DIGEST=$(dig 7)
printf '%s\n' "owner/app:v1.2.0 $PREV_DIGEST" >"$TAGS"
C1=$(git -C "$NOTESREPO" rev-parse v1.2.0)
echo "owner/app $C1" >"$COSIGN_SIGNED"
envelope bundle "$PREV_DIGEST" >"$COSIGN_VERIFIED"
# Every attestation attached: an unsigned forgery first, then the signed one.
{
  envelope bundle "$PREV_DIGEST" "$EVIL"
  envelope bundle "$PREV_DIGEST"
} >"$COSIGN_ATT"
o=$(prev_sbom)
chk "D-Q3 a verified bundle attestation yields the previous SPDX document" "$(jq -c . "$WORK/sbom-prev.spdx.json" 2>/dev/null)" "$(jq -c . <<<"$SPDX")"
chk_has "D-Q3 verified against this workflow's identity, by digest" "$(head -n1 "$COSIGN_LOG")" \
  "verify-attestation --type spdxjson --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity-regexp ^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@ --certificate-github-workflow-repository owner/app --certificate-github-workflow-sha $C1 ghcr.io/owner/app@${PREV_DIGEST}"
chk "D-Q3 read from what verification printed, with no separate download" "$(cut -d' ' -f1 "$COSIGN_LOG" | tr '\n' ' ')" "verify-attestation "
envelope dsse "$PREV_DIGEST" >"$COSIGN_VERIFIED"
prev_sbom >/dev/null
chk "D-Q4 the DSSE envelope shape decodes too" "$(jq -c . "$WORK/sbom-prev.spdx.json" 2>/dev/null)" "$(jq -c . <<<"$SPDX")"
{
  envelope bundle "$(dig 8)" "$EVIL"
  envelope bundle "$PREV_DIGEST"
} >"$COSIGN_VERIFIED"
prev_sbom >/dev/null
chk "D-Q8 a verified statement about another image is skipped for the one about this digest" \
  "$(jq -c . "$WORK/sbom-prev.spdx.json" 2>/dev/null)" "$(jq -c . <<<"$SPDX")"
envelope bundle "$(dig 8)" >"$COSIGN_VERIFIED"
o=$(prev_sbom)
chk_has "D-Q8 and with only such a statement nothing is read" "$o" "carries no SPDX document for ${PREV_DIGEST}"
chk "D-Q8 leaving no file" "$([ -f "$WORK/sbom-prev.spdx.json" ] && echo file || echo none)" "none"
o=$(COSIGN_VERIFY_RC=1 prev_sbom)
chk_has "D-Q5 an attestation that does not verify is not read" "$o" "does not verify"
chk "D-Q5 nothing downloaded, no file, the step still passes" \
  "$(grep -c '^download' "$COSIGN_LOG" || true)|$([ -f "$WORK/sbom-prev.spdx.json" ] && echo file || echo none)|$(printf '%s' "$o" | grep -c '^EXIT=' || true)" "0|none|0"
printf '{"payload":"%s"}\n' "$(printf '{"predicate":"not a document"}' | base64 | tr -d '\n')" >"$COSIGN_VERIFIED"
o=$(prev_sbom)
chk_has "D-Q6 an attestation without an SPDX document is dropped" "$o" "carries no SPDX document"
chk "D-Q6 and leaves no file" "$([ -f "$WORK/sbom-prev.spdx.json" ] && echo file || echo none)" "none"
# Every consumer signs as this workflow, so the repository and a commit that
# could have published the tag are what bind the attestation to it.
prev_file() { [ -f "$WORK/sbom-prev.spdx.json" ] && echo file || echo none; }
asked() { sed -n 's/.*--certificate-github-workflow-sha \([0-9a-f]*\) .*/\1/p' "$COSIGN_LOG" | tr '\n' ' '; }
envelope bundle "$PREV_DIGEST" >"$COSIGN_VERIFIED"
echo "other/app $C1" >"$COSIGN_SIGNED"
o=$(prev_sbom)
chk "D-Q9 a byte-valid attestation another repository's run signed is not read" "$(prev_file)" "none"
chk_has "D-Q9 and says why" "$o" "The notes carry no system-package diff, because the SBOM attestation of v1.2.0 on ghcr.io/owner/app@${PREV_DIGEST} does not verify as this repository's."
M2=$(commit_in "$NOTESREPO" "fix: a later main commit" main.go=v2)
SIDE=$(git -C "$NOTESREPO" commit-tree -p "$C1" -m "fix: off main's history" "$C1^{tree}")
echo "owner/app $SIDE" >"$COSIGN_SIGNED"
o=$(prev_sbom)
chk "D-Q9 one this repository signed at a commit outside the publishing window is not read" \
  "$(prev_file)|$(asked)" "none|$C1 $M2 "
echo "owner/app $M2" >"$COSIGN_SIGNED"
prev_sbom >/dev/null
chk "D-Q9 one a later main commit in the window signed (it re-pushed the tag) is read" "$(prev_file)|$(asked)" "file|$C1 $M2 "
# A promotion's image was signed by the dev run that built it: the promoted
# commit's, or an ancestor's it carries unchanged, main's commits included.
git -C "$NOTESREPO" checkout -q -b dev
TA=$(commit_in "$NOTESREPO" "feat: dev work" main.go=v3)
TT=$(commit_in "$NOTESREPO" "docs: dev notes" README.md=dev)
REC=$(git -C "$NOTESREPO" commit-tree -p "$M2" -p "$TT" -m "release: promote dev into main" "$TT^{tree}")
git -C "$NOTESREPO" checkout -q -B main "$REC"
git -C "$NOTESREPO" tag v1.2.5
printf '%s\n' "owner/app:v1.2.5 $PREV_DIGEST" >"$TAGS"
echo "owner/app $TA" >"$COSIGN_SIGNED"
prev_sbom >/dev/null
chk "D-Q10 a promotion's attestation signed by its dev build's run is read, the promoted commit asked first" \
  "$(prev_file)|$(asked)" "file|$TT $TA "
echo "owner/app $M2" >"$COSIGN_SIGNED"
o=$(prev_sbom)
chk "D-Q10 one signed at a main commit the promotion did not bring is not" "$(prev_file)|$(asked)" "none|$TT $TA "
MM=$(commit_in "$NOTESREPO" "fix(deps): update module example.org/x" go.sum=b)
git -C "$NOTESREPO" checkout -q dev
TD=$(commit_in "$NOTESREPO" "docs: more dev notes" README.md=dev2)
REC2=$(git -C "$NOTESREPO" commit-tree -p "$MM" -p "$TD" -m "release: promote dev into main" "$TD^{tree}")
git -C "$NOTESREPO" checkout -q -B main "$REC2"
git -C "$NOTESREPO" tag v1.2.6
printf '%s\n' "owner/app:v1.2.6 $PREV_DIGEST" >"$TAGS"
echo "owner/app $TT" >"$COSIGN_SIGNED"
prev_sbom >/dev/null
chk "D-Q10 one signed at the merge base the promoted commit carries unchanged is read" "$(prev_file)|$(asked)" "file|$TD $TT "
echo "owner/app $MM" >"$COSIGN_SIGNED"
prev_sbom >/dev/null
chk "D-Q10 one signed at main's own commit is not, the walk stopping at the shipped change" \
  "$(prev_file)|$(asked)" "none|$TD $TT $TA "
git -C "$NOTESREPO" checkout -q dev
TX=$(commit_in "$NOTESREPO" "fix: dev fix" main.go=v5)
REC3=$(git -C "$NOTESREPO" commit-tree -p "$REC2" -p "$TX" -m "release: promote dev into main" "$TX^{tree}")
git -C "$NOTESREPO" checkout -q -B main "$REC3"
git -C "$NOTESREPO" tag v1.2.7
printf '%s\n' "owner/app:v1.2.7 $PREV_DIGEST" >"$TAGS"
echo "owner/app $TD" >"$COSIGN_SIGNED"
prev_sbom >/dev/null
chk "D-Q10 a promoted commit that changes a shipped path is asked alone" "$(prev_file)|$(asked)" "none|$TX "
chk "D-Q10 the step reads the exclusion list the promotion's digest walk used" \
  "$(sed -n 's/^EXCLUDE_RE=//p' "$WORK/prevsbom.sh.envmap")" '${{ inputs.exclude-re }}'
chk "D-Q10 and the subpackage list it used" "$(sed -n 's/^SUBPACKAGES_JSON=//p' "$WORK/prevsbom.sh.envmap")" '${{ inputs.subpackages }}'
chk "D-Q10 which release.yaml hands the image job" "$(jq -r '.subpackages' "$WORK/docker-with.json")" '${{ needs.detect.outputs.subpackages }}'
# Any other tag's image is signed at its commit or at a first-parent ancestor
# whose image it re-tagged, which promote-digest.sh decides the same way: never
# a merge's, so a main release right after a promotion was built at its commit.
TN=$(commit_in "$NOTESREPO" "docs: main notes" README.md=main)
git -C "$NOTESREPO" tag v1.2.8
printf '%s\n' "owner/app:v1.2.8 $PREV_DIGEST" >"$TAGS"
echo "owner/app $TN" >"$COSIGN_SIGNED"
prev_sbom >/dev/null
chk "D-Q11 a main release over a promotion reads the attestation its own build made" "$(prev_file)|$(asked)" "file|$TN "
echo "owner/app $REC3" >"$COSIGN_SIGNED"
prev_sbom >/dev/null
chk "D-Q11 never the promotion's, whose image it does not carry" "$(prev_file)|$(asked)" "none|$TN "
echo "owner/app $TX" >"$COSIGN_SIGNED"
prev_sbom >/dev/null
chk "D-Q11 nor past the merge it would have had to cross" "$(prev_file)|$(asked)" "none|$TN "
TP1=$(commit_in "$NOTESREPO" "feat(web): add the web package" web/package.json='{"name":"@o/web","dependencies":{"a":"1.0.0"}}')
TP2=$(commit_in "$NOTESREPO" "fix(deps): update dependency a to v1.0.1" web/package.json='{"name":"@o/web","dependencies":{"a":"1.0.1"}}')
git -C "$NOTESREPO" tag v1.2.9
printf '%s\n' "owner/app:v1.2.9 $PREV_DIGEST" >"$TAGS"
echo "owner/app $TP1" >"$COSIGN_SIGNED"
PREV_SUBS='["web"]' prev_sbom >/dev/null
chk "D-Q11 a release owed to a subpackage's manifest was built at its own commit" "$(prev_file)|$(asked)" "none|$TP2 "
prev_sbom >/dev/null
chk "D-Q11 where an undeclared package.json carried the earlier image" "$(prev_file)|$(asked)" "file|$TP2 $TP1 "
: >"$TAGS"
chk_has "D-Q7 runs on two-branch stable publishes only" "$(gate '."prevsbom-if"')" \
  "env.PUBLISH == 'true' && inputs.channel == 'stable' && inputs.release-model == 'two-branch'"
chk "D-Q7 after the new SBOM and before the notes" "$(gate '."prevsbom-order"')" "true"

# ── Two-branch repair: the signed assets of a missing image Release ──────────
attested() { # <tag> -> the script's exit status|its stdout, with the document in $WORK/att.out
  local rc=0 o
  rm -f "$WORK/att.out"
  o=$(cd "$NOTESREPO" && PATH="$NBIN:$BIN:$PATH" GITHUB_REPOSITORY=owner/app EXCLUDE_RE="$EXCLUDE_RE_FULL" SUBPACKAGES_JSON='[]' \
    READBACK_SLEEP=0 bash "$ROOT/scripts/attested-sbom.sh" "ghcr.io/owner/app@${PREV_DIGEST}" v1.2.0 "$WORK/att.out" 2>/dev/null) || rc=$?
  echo "$rc|$o"
}
echo "owner/app $C1" >"$COSIGN_SIGNED"
envelope bundle "$PREV_DIGEST" >"$COSIGN_VERIFIED"
chk "D-A1 attested-sbom.sh writes the verified document and says nothing" "$(attested v1.2.0)|$(jq -c . "$WORK/att.out")" "0||$(jq -c . <<<"$SPDX")"
echo "other/app $C1" >"$COSIGN_SIGNED"
chk "D-A1 exits 3 when nothing verifies as this repository's, writing nothing" "$(attested v1.2.0)|$([ -e "$WORK/att.out" ] && echo file || echo none)" \
  "3|the SBOM attestation of v1.2.0 on ghcr.io/owner/app@${PREV_DIGEST} does not verify as this repository's|none"
chk "D-A1 and 1 when cosign cannot tell" "$(COSIGN_VERIFY_RC=1 attested v1.2.0 | cut -d'|' -f1)" "1"
echo "owner/app $C1" >"$COSIGN_SIGNED"
envelope bundle "$(dig 8)" >"$COSIGN_VERIFIED"
chk "D-A1 and 3 when the verified statements name another digest" "$(attested v1.2.0)" \
  "3|the verified SBOM attestation of v1.2.0 carries no SPDX document for ${PREV_DIGEST}"
ASSETS_RT="$WORK/rt-assets" ASSETS_DIR="$WORK/rt-assets/assets" ABIN="$WORK/abin"
mkdir -p "$ABIN"
ln -s "$NBIN/cosign" "$NBIN/sleep" "$ABIN/"
assets() { # <tag> -> rc|the files handed on, in ASSETS_DIR, with a log in $WORK/assets.log
  local rc=0
  rm -rf "$ASSETS_RT"
  mkdir -p "$ASSETS_RT"
  : >"$COSIGN_LOG"
  (cd "$NOTESREPO" && PATH="$ABIN:$BIN:$PATH" CI_TOOLS="$ROOT/scripts" RUNNER_TEMP="$ASSETS_RT" GITHUB_WORKSPACE="$NOTESREPO" REGISTRY=ghcr.io \
    IMAGE_NAME=owner/app GITHUB_REPOSITORY=owner/app TAG="$1" EXCLUDE_RE="$EXCLUDE_RE_FULL" SUBPACKAGES_JSON='[]' READBACK_SLEEP=0 \
    bash "$WORK/rassets.sh") >"$WORK/assets.log" 2>&1 || rc=$?
  echo "$rc|$(find "$ASSETS_DIR" -mindepth 1 -printf '%f\n' 2>/dev/null | sort | tr '\n' ' ')"
}
signed() { grep -c '^sign-blob --yes' "$COSIGN_LOG" || true; }
printf '%s\n' "owner/app:v1.2.0 $PREV_DIGEST" >"$TAGS"
envelope bundle "$PREV_DIGEST" >"$COSIGN_VERIFIED"
chk "D-A2 a tag without a dashboard hands on its attested SBOM and the SBOM's signature" \
  "$(assets v1.2.0)|$(jq -c . "$ASSETS_DIR/sbom.spdx.json")" "0|sbom.spdx.json sbom.spdx.json.sigstore.json |$(jq -c . <<<"$SPDX")"
chk "D-A2 signed with the release's own bundle name" "$(grep '^sign-blob' "$COSIGN_LOG")|$(cat "$ASSETS_DIR/sbom.spdx.json.sigstore.json")" \
  "sign-blob --yes sbom.spdx.json --bundle sbom.spdx.json.sigstore.json|bundle over $(sha256sum <"$ASSETS_DIR/sbom.spdx.json" | cut -d' ' -f1)"
DSH=$(commit_in "$NOTESREPO" "feat: ship a dashboard" grafana-dashboard.json='{"uid":"app"}')
git -C "$NOTESREPO" tag v2.0.0
printf '{"uid":"edited after the tag"}' >"$NOTESREPO/grafana-dashboard.json"
printf '%s\n' "owner/app:v2.0.0 $PREV_DIGEST" >"$TAGS"
echo "owner/app $DSH" >"$COSIGN_SIGNED"
chk "D-A3 a tag shipping a dashboard hands on the dashboard, its checksum and its signature too" "$(assets v2.0.0)|$(signed)" \
  "0|grafana-dashboard.json grafana-dashboard.json.sha256 grafana-dashboard.json.sigstore.json sbom.spdx.json sbom.spdx.json.sigstore.json |2"
chk "D-A3 the dashboard is the tag commit's, checked as the release names it" \
  "$(cat "$ASSETS_DIR/grafana-dashboard.json")|$(cd "$ASSETS_DIR" && sha256sum -c grafana-dashboard.json.sha256)" '{"uid":"app"}|grafana-dashboard.json: OK'
chk "D-A4 a failed SBOM signature hands nothing on" "$(COSIGN_SIGN_RC=1 assets v2.0.0 | cut -d'|' -f1)" "1"
chk_has "D-A4 naming the missing bundle" "$(cat "$WORK/assets.log")" \
  "::error::sbom.spdx.json.sigstore.json is missing, because cosign could not sign sbom.spdx.json. v2.0.0 is not published without it."
chk "D-A4 a failed dashboard signature hands nothing on either" \
  "$(COSIGN_SIGN_RC=1 COSIGN_SIGN_FAIL=grafana-dashboard.json assets v2.0.0 | cut -d'|' -f1)|$(grep -c '^sign-blob --yes sbom.spdx.json ' "$COSIGN_LOG")" "1|1"
chk_has "D-A4 naming that bundle" "$(cat "$WORK/assets.log")" \
  "::error::grafana-dashboard.json.sigstore.json is missing, because cosign could not sign grafana-dashboard.json. v2.0.0 is not published without it."
echo "owner/app $C1" >"$COSIGN_SIGNED"
chk "D-A5 an SBOM attestation that does not verify fails before any signature" "$(assets v2.0.0)|$(signed)" "1||0"
chk_has "D-A5 naming the missing asset and why" "$(cat "$WORK/assets.log")" \
  "::error::sbom.spdx.json is missing, because the SBOM attestation of v2.0.0 on ghcr.io/owner/app@${PREV_DIGEST} does not verify as this repository's. v2.0.0 is not published without it."
: >"$TAGS"
echo "owner/app $DSH" >"$COSIGN_SIGNED"
chk "D-A6 an image the registry does not serve at the tag fails before cosign" "$(assets v2.0.0)|$(wc -l <"$COSIGN_LOG" | tr -d ' ')" "1||0"
chk_has "D-A6 naming the missing asset" "$(cat "$WORK/assets.log")" \
  "::error::sbom.spdx.json is missing, because the image digest that v2.0.0 published is unknown. v2.0.0 is not published without it."
rassets_check() { # -> the missing output, or EXIT=n
  : >"$WORK/ra.out"
  (PATH="$ABIN:$BIN:$PATH" CI_TOOLS="$ROOT/scripts" GITHUB_REPOSITORY=owner/app TAG=v2.0.0 GITHUB_OUTPUT="$WORK/ra.out" \
    bash "$WORK/rassets-check.sh") >/dev/null 2>&1 || echo "EXIT=$?"
  sed -n 's/^missing=//p' "$WORK/ra.out"
}
cat >"$ABIN/gh" <<'SH'
#!/bin/sh
case "$*" in
  "api repos/owner/app/releases/tags/v2.0.0")
    case "$RELEASE_STATE" in
      present) echo '{}' ;;
      absent) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
      *) echo "gh: HTTP 502" >&2; exit 1 ;;
    esac ;;
  *) echo "stub: unexpected gh $*" >&2; exit 22 ;;
esac
SH
chmod 755 "$ABIN/gh"
chk "D-A7 the assets are made only for a Release that is missing" \
  "$(RELEASE_STATE=present rassets_check) $(RELEASE_STATE=absent rassets_check) $(RELEASE_STATE=down rassets_check | tr '\n' ' ')" "false true EXIT=1 "
RELEASE_STATE=absent rassets_check >/dev/null
chk "D-A7 and handed on under the name repair-release fetches" "$(sed -n 's/^artifact=//p' "$WORK/ra.out")" \
  "$(bash "$ROOT/scripts/release-state.sh" handoff-name repair-assets v2.0.0)"
chk "D-A8 a repair call builds nothing: prepare, which every other job needs, runs only without a repair tag" \
  "$(gate '."prepare-if"')|$(gate '."repair-tag-default"')" "\${{ inputs.repair-tag == '' }}|"
chk "D-A8 so a repair call runs repair-assets alone" "$(gate '."repair-call-runs" | join(" ")')" "repair-assets"
chk "D-A8 and repair-assets only with one, reading and signing, in its own job" \
  "$(gate '."ra-if"')|$(gate '."ra-needs"')|$(gate '."ra-perms" | tojson')" "\${{ inputs.repair-tag != '' }}|null|{\"contents\":\"read\",\"id-token\":\"write\"}"
chk "D-A8 checking the Release before installing, signing and uploading" "$(gate '."ra-steps" | join(",")')|$(gate '."ra-step-ifs" | unique | join(" ")')" \
  "Checkout,Check out the ci source,Check the Release,Install Cosign,Sign the release assets,Hand the assets on|\${{ steps.release.outputs.missing == 'true' }}"
chk "D-A8 with the pinned cosign" "$(gate '."ra-cosign".uses')|$(gate '."ra-cosign".with."cosign-release"')" \
  "$(gate '."prepare-cosign-uses"')|\${{ env.COSIGN_VERSION }}"
chk "D-A8 uploading every file the step wrote, failing on none" \
  "$(gate '."ra-upload" | "\(.name)|\(.path)|\(."if-no-files-found")"')" '${{ steps.release.outputs.artifact }}|${{ runner.temp }}/assets/|error'

# ── Two-branch: a promoted digest's signature and SBOM, before any write ─────
provenance() { # <source commit> -> runs the extracted step in the promotion fixture at T1's trailer digest; ok or refused
  : >"$PROV_LOG"
  (cd "$WORK/rec" && PATH="$PBIN:$BIN:$PATH" CI_TOOLS="$ROOT/scripts" GITHUB_REPOSITORY=owner/app REGISTRY=ghcr.io IMAGE_NAME=owner/app \
    PROMOTE_DIGEST="$D_T" PROMOTE_SOURCE="$1" EXCLUDE_RE="$EXCLUDE_RE_FULL" SUBPACKAGES_JSON='[]' READBACK_SLEEP=0 \
    bash "$WORK/provenance.sh") >"$WORK/provenance.log" 2>&1 && echo ok || echo refused
}
: >"$PROV_OK"
chk "D-G1 a dev build that pushed its digest and died before signing is refused" "$(provenance "$T1")" "refused"
chk_has "D-G1 naming the missing signature" "$(cat "$WORK/provenance.log")" "ghcr.io/owner/app@${D_T} carries no signature"
echo "verify $D_T $T1" >"$PROV_OK"
chk "D-G2 one that signed and died before attesting its SBOM is refused" "$(provenance "$T1")" "refused"
chk_has "D-G2 naming the missing attestation" "$(cat "$WORK/provenance.log")" "carries no SPDX SBOM attestation"
printf '%s\n' "verify $D_T $M1" "verify-attestation $D_T $M1" >"$PROV_OK"
chk "D-G3 a signature and SBOM made at a commit the source does not carry are refused" "$(provenance "$T1")" "refused"
printf '%s\n' "verify $D_T $T1" "verify-attestation $D_T $T1" >"$PROV_OK"
chk "D-G4 a completed dev build is promoted, its tag or none" "$(provenance "$T1")" "ok"
chk "D-G4 asking for this repository's run at the source, signature then SBOM" "$(cut -d' ' -f1 "$PROV_LOG" | tr '\n' ' ')|$(grep -c -- "-sha $T1 ghcr.io/owner/app@$D_T\$" "$PROV_LOG")" "verify verify-attestation |2"
printf '%s\n' "verify $D_T $M0" "verify-attestation $D_T $M0" >"$PROV_OK"
chk "D-G5 a docs-only source carries the image its ancestor's run built" "$(provenance "$T_D")" "ok"
chk "D-G5 asking the source first, then that ancestor" "$(sed 's/.*-sha \([0-9a-f]*\) .*/\1/' "$PROV_LOG" | tr '\n' ' ')" "$T_D $M0 $T_D $M0 "
chk_has "D-G6 runs on two-branch publishes of a promoted digest only" "$(gate '."provenance-if"')" \
  "env.PUBLISH == 'true' && inputs.release-model == 'two-branch' && env.PROMOTE_DIGEST != ''"
chk "D-G6 after cosign and the tools are in place, before every registry write" "$(gate '."before-provenance" | join("|")')" \
  "Checkout|Check out the ci source|Version ownership gate|Download digests|Set up Docker Buildx|Log in to GHCR|Install Cosign"
chk "D-G6 the next step is the first write" "$(gate '."first-write"')" "Publish channel tags on GHCR"
chk "D-G6 the source is the one the digest walk named" "$(sed -n 's/^PROMOTE_SOURCE=//p' "$WORK/provenance.sh.envmap")" \
  '${{ needs.prepare.outputs.promote_source }}'
chk "D-G6 with the exclusion list and subpackages that walk used" \
  "$(sed -n 's/^EXCLUDE_RE=//p;s/^SUBPACKAGES_JSON=//p' "$WORK/provenance.sh.envmap" | tr '\n' ' ')" '${{ inputs.exclude-re }} ${{ inputs.subpackages }} '

# ── Dev tag receipt ──────────────────────────────────────────────────────────
# The stub gh records every call and answers the git-ref read as absent (404)
# unless GH_TAG_EXISTS names a commit; GH_REF_FAIL makes the ref create fail,
# GH_HEAD is what the branch head then reads as, and GH_STATUS_FAIL makes the
# receipt POST fail.
export GH_LOG="$WORK/gh.log" GH_TAG_EXISTS="" GH_REF_FAIL="" GH_HEAD="" GH_STATUS_FAIL="" GH_RELEASE_MISSING=""
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
  *"/releases/tags/"*)
    if [ -n "$GH_RELEASE_MISSING" ]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
    printf '{}\n'
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

# ── Two-branch: the image lane's completion receipt ──────────────────────────
: >"$GH_LOG"
out_c=$(VERSION=v1.3.0 run_step receipt.sh)
chk "D-C1 the receipt reads the Release, then posts on the release commit" \
  "$(sed 's/ -f description=.*//' "$GH_LOG" | tr '\n' ';')" \
  "api repos/owner/app/releases/tags/v1.3.0;api -X POST repos/owner/app/statuses/$C_BUILT -f state=success -f context=release/complete/v1.3.0;"
chk "D-C1 and exits 0" "$(printf '%s' "$out_c" | grep -c '^EXIT=' || true)" "0"
: >"$GH_LOG"
out_c=$(GH_RELEASE_MISSING=1 VERSION=v1.3.0 run_step receipt.sh)
chk "D-C2 no Release, no receipt" "$(grep -c '/statuses/' "$GH_LOG" || true)|$(printf '%s' "$out_c" | sed -n 's/^EXIT=//p')" "0|1"
RECEIPT_GATE="always() && needs.verify-publish.result == 'success' && !inputs.defer-receipt && inputs.release-model == 'two-branch' && inputs.channel == 'stable' && needs.finalize.outputs.released == 'true'"
chk "D-C3 the receipt is two-branch stable only, after a published Release, unless the caller defers it" "$(gate '."receipt-if"')" "$RECEIPT_GATE"
chk "D-C3 its steps carry no condition of their own" "$(gate '."receipt-step-ifs" | join("")')" ""
chk "D-C3 released is the Release step's published flag" "$(gate '.released')" '${{ steps.release.outputs.published }}'
chk "D-C4 after the readback, in a job of its own with the statuses scope" \
  "$(gate '."receipt-needs" | join(" ")')|$(gate '."receipt-perms" | to_entries | map("\(.key)=\(.value)") | join(" ")')" \
  "prepare finalize verify-publish|contents=read statuses=write"
chk "D-C5 verify-publish, which legacy runs reach, holds only the read scope every caller grants" "$(gate '."vp-perms" | tojson')" '{"contents":"read"}'
unset GITHUB_REPOSITORY GITHUB_SHA GITHUB_REF_NAME GITHUB_SERVER_URL GITHUB_RUN_ID
rm -f "$BIN/gh"
for job in go ts go-nested; do
  chk "D-T7 the $job tag step posts the receipt" \
    "$(grep -c 'context="release/tag/${VERSION}"' "$WORK/$job-tag.sh")" "1"
  chk_has "D-T8 the $job receipt sits in the dev arm" "$(sed -n '/CHANNEL" != "stable"/,/exit 0/p' "$WORK/$job-tag.sh")" 'release/tag/${VERSION}'
done
chk "D-T9 every tagging job and the caller hold the statuses scope" "$(tr '\n' ' ' <"$WORK/statuses-scope.txt")" \
  "docker=True docker-release/finalize=True go=True go-nested=True template/release=True ts=True "

# ── Stable Release: draft-then-publish, and a rerun over an immutable Release ─
# The stub gh models one Release store for v2.0.0: REL_DIR/state is absent,
# mutable, immutable or down (a 502 on the by-tag read), assets its names, and
# listing.json what the releases listing answers. `gh release create` with
# assets is the draft, upload, publish sequence (cli/cli v2.96.0
# pkg/cmd/release/create/create.go), so it publishes every asset at once and
# the Release is immutable from then on when IMMUTABLE_ON is set; an upload to
# an immutable Release is refused as GitHub refuses it.
export REL_DIR="$WORK/rel" IMMUTABLE_ON=""
mkdir -p "$REL_DIR" "$WORK/relcwd"
cat >"$BIN/gh" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >>"$GH_LOG"
st=$(cat "$REL_DIR/state")
case "$*" in
  "api repos/owner/app/git/ref/tags/v2.0.0 --jq .object.sha")
    printf '%s\n' "$GITHUB_SHA"
    exit 0
    ;;
  "api repos/owner/app/releases/tags/v2.0.0 --jq .immutable")
    case $st in
      absent) echo "gh: Not Found (HTTP 404)" >&2 && exit 1 ;;
      down) echo "gh: Server Error (HTTP 502)" >&2 && exit 1 ;;
      immutable) echo true ;;
      *) echo false ;;
    esac
    exit 0
    ;;
  "api repos/owner/app/releases/tags/v2.0.0 --jq .assets[].name")
    cat "$REL_DIR/assets"
    exit 0
    ;;
  "api repos/owner/app/releases?per_page=100")
    cat "$REL_DIR/listing.json"
    exit 0
    ;;
  "api -X DELETE repos/owner/app/releases/"*) exit 0 ;;
  "release create v2.0.0 --title v2.0.0 --notes-file RELEASE_NOTES.md "*)
    shift 7
    printf '%s\n' "$@" >"$REL_DIR/assets"
    if [ -n "$IMMUTABLE_ON" ]; then echo immutable; else echo mutable; fi >"$REL_DIR/state"
    exit 0
    ;;
  "release upload --clobber v2.0.0 "*)
    if [ "$st" = immutable ]; then
      echo "HTTP 422: Cannot upload assets to an immutable release." >&2
      exit 1
    fi
    exit 0
    ;;
esac
printf 'stub: unexpected gh %s\n' "$*" >&2
exit 22
SH
chmod 755 "$BIN/gh"
DASH_ASSETS=(grafana-dashboard.json grafana-dashboard.json.sha256 grafana-dashboard.json.sigstore.json)
ALL_ASSETS=(sbom.spdx.json sbom.spdx.json.sigstore.json "${DASH_ASSETS[@]}")
release_store() { # <state> [asset names...]: the store as the run finds it
  echo "$1" >"$REL_DIR/state"
  shift
  if [ "$#" -gt 0 ]; then printf '%s\n' "$@" >"$REL_DIR/assets"; else : >"$REL_DIR/assets"; fi
  echo '[]' >"$REL_DIR/listing.json"
  : >"$GH_LOG"
}
finalize_release() { # [IMMUTABLE env] -> runs Create release then, when it published, the verify step
  (
    cd "$WORK/relcwd" || exit 1
    export GITHUB_REPOSITORY=owner/app GITHUB_SHA="$C_BUILT" GITHUB_REF_NAME=main VERSION=v2.0.0
    run_step createrel.sh
    reused=$(out reused) immutable=$(out immutable)
    [ "$(out published)" = true ] || exit 0
    echo "outputs: reused=${reused} immutable=${immutable}"
    REUSED=$reused IMMUTABLE=$immutable run_step verifydash.sh
  )
}
calls() { grep -cE "$1" "$GH_LOG" || true; }
(cd "$WORK/relcwd" && touch sbom.spdx.json sbom.spdx.json.sigstore.json RELEASE_NOTES.md "${DASH_ASSETS[@]}")
chk "D-I0 the verify step reads whether the Release is immutable from the create step" \
  "$(sed -n 's/^IMMUTABLE=//p' "$WORK/verifydash.sh.envmap")" '${{ steps.release.outputs.immutable }}'

for on in "" 1; do
  release_store absent
  out_rel=$(IMMUTABLE_ON=$on finalize_release)
  chk "D-I1 a first publish (immutable setting '${on:-off}') is one create carrying every asset" \
    "$(grep '^release ' "$GH_LOG" | tr '\n' ';')" "release create v2.0.0 --title v2.0.0 --notes-file RELEASE_NOTES.md ${ALL_ASSETS[*]};"
  chk "D-I1 and the published Release holds them all" "$(tr '\n' ' ' <"$REL_DIR/assets")" "${ALL_ASSETS[*]} "
  chk "D-I1 and nothing is uploaded after it" "$(calls '^release upload')" "0"
  chk "D-I1 and the verify step passes" "$(printf '%s' "$out_rel" | grep -c '^EXIT=' || true)" "0"
done
chk_has "D-I1 a first publish is no reuse" "$out_rel" "outputs: reused= immutable="

(cd "$WORK/relcwd" && rm -f "${DASH_ASSETS[@]}")
release_store absent
out_rel=$(finalize_release)
chk "D-I2 without a dashboard the create carries the SBOM pair alone" \
  "$(grep '^release ' "$GH_LOG")" "release create v2.0.0 --title v2.0.0 --notes-file RELEASE_NOTES.md sbom.spdx.json sbom.spdx.json.sigstore.json"
(cd "$WORK/relcwd" && touch "${DASH_ASSETS[@]}")

release_store absent
printf '%s\n' '[{"id":11,"draft":true,"tag_name":"v2.0.0"},{"id":12,"draft":true,"tag_name":"v1.9.0"},{"id":13,"draft":false,"tag_name":"v2.0.0"}]' >"$REL_DIR/listing.json"
out_rel=$(finalize_release)
chk "D-I3 an orphaned draft of this version is deleted, and nothing else" \
  "$(grep -- '-X DELETE' "$GH_LOG" | tr '\n' ';')" "api -X DELETE repos/owner/app/releases/11;"
chk "D-I3 before the one create" \
  "$(grep -E -- '-X DELETE|^release create' "$GH_LOG" | cut -d' ' -f1-2 | tr '\n' ';')" "api -X;release create;"

release_store immutable "${ALL_ASSETS[@]}"
out_rel=$(finalize_release)
chk_has "D-I4 a rerun over an immutable Release reuses it" "$out_rel" "outputs: reused=true immutable=true"
chk "D-I4 and uploads nothing" "$(calls '^release ')" "0"
chk_has "D-I4 and says why" "$out_rel" "::notice::Release v2.0.0 is immutable, so its dashboard assets stay as first published."
chk "D-I4 and still verifies the assets, passing" \
  "$(calls 'releases/tags/v2.0.0 --jq .assets')|$(printf '%s' "$out_rel" | grep -c '^EXIT=' || true)" "1|0"

release_store immutable sbom.spdx.json sbom.spdx.json.sigstore.json grafana-dashboard.json
out_rel=$(finalize_release)
chk "D-I5 an immutable Release missing a dashboard asset fails the verify step" \
  "$(printf '%s' "$out_rel" | sed -n 's/^EXIT=//p')" "1"
chk_has "D-I5 naming the asset" "$out_rel" "::error::release v2.0.0 is missing asset grafana-dashboard.json.sha256"

release_store mutable "${ALL_ASSETS[@]}"
out_rel=$(finalize_release)
chk_has "D-I6 a rerun over a mutable Release reuses it" "$out_rel" "outputs: reused=true immutable=false"
chk "D-I6 and clobbers the dashboard assets with this build's" \
  "$(grep '^release ' "$GH_LOG")" "release upload --clobber v2.0.0 ${DASH_ASSETS[*]}"
chk "D-I6 and passes" "$(printf '%s' "$out_rel" | grep -c '^EXIT=' || true)" "0"

release_store down
out_rel=$(finalize_release)
chk "D-I7 an unreadable Release fails the create step and writes nothing" \
  "$(printf '%s' "$out_rel" | sed -n 's/^EXIT=//p')|$(calls '^release |-X DELETE')" "1|0"
rm -f "$BIN/gh"

# ── Registry logins: one retried helper, the password on stdin ───────────────
# Stub docker: records argv and stdin per call; fails DOCKER_FAILS times with
# the connection reset auth.docker.io answered, then logs in.
LBIN="$WORK/lbin"
mkdir -p "$LBIN"
export DOCKER_LOG="$WORK/docker.log"
cat >"$LBIN/docker" <<'SH'
#!/bin/sh
n=$(($(wc -l <"$DOCKER_LOG") + 1))
printf '%s | stdin=%s\n' "$*" "$(cat)" >>"$DOCKER_LOG"
if [ "$n" -le "${DOCKER_FAILS:-0}" ]; then
  echo 'Error response from daemon: Get "https://registry-1.docker.io/v2/": read: connection reset by peer' >&2
  exit 1
fi
echo "Login Succeeded"
SH
printf '#!/bin/sh\nexit 0\n' >"$LBIN/sleep"
chmod 755 "$LBIN/docker" "$LBIN/sleep"
login() { # <tag> <registry env> -> runs the extracted login step; prints its output, EXIT=n on failure
  : >"$DOCKER_LOG"
  (PATH="$LBIN:$PATH" CI_TOOLS="$ROOT/scripts" REGISTRY="$2" REGISTRY_USERNAME=probe-user \
    REGISTRY_PASSWORD=probe-secret bash "$WORK/login-$1.sh" 2>&1) || echo "EXIT=$?"
}
chk "D-L1 the release path logs in at four sites" "$(gate '.logins | join(" ")')" \
  "docker-release-build-ghcr docker-release-finalize-ghcr docker-release-finalize-hub release-renumber-tag-ghcr"
chk "D-L1 and no workflow uses a login action" \
  "$(grep -c 'docker/login-action' "$WORKFLOW" "$RELEASE_YAML" | awk -F: '{ s += $2 } END { print s }')" "0"
for tag in $(gate '.logins[]'); do
  chk "D-L2 $tag is a run step" "$(cat "$WORK/login-$tag.uses")" ""
  chk_has "D-L2 $tag logs in through the retried helper" "$(cat "$WORK/login-$tag.sh")" 'registry_login '
  chk "D-L2 $tag takes its password from the environment" \
    "$(grep -c '^REGISTRY_PASSWORD=\${{ \(github.token\|secrets.DOCKERHUB_TOKEN\) }}$' "$WORK/login-$tag.env")" "1"
done
out_l=$(DOCKER_FAILS=1 login docker-release-finalize-hub ghcr.io)
chk "D-L3 a login that hits one reset connection succeeds on its second attempt" \
  "$(wc -l <"$DOCKER_LOG" | tr -d ' ')|$(printf '%s' "$out_l" | grep -c '^EXIT=' || true)" "2|0"
chk_has "D-L3 and warns about the retry in the legacy words" "$out_l" "::warning::login_once attempt 1/5 failed; retrying in 5s"
chk "D-L3 Docker Hub is docker.io, the password on stdin and never in argv" "$(tail -1 "$DOCKER_LOG")" \
  "login --username probe-user --password-stdin docker.io | stdin=probe-secret"
out_l=$(DOCKER_FAILS=1 login docker-release-build-ghcr ghcr.io)
chk "D-L3 GHCR logs in to the job's registry" "$(tail -1 "$DOCKER_LOG")" \
  "login --username probe-user --password-stdin ghcr.io | stdin=probe-secret"
out_l=$(DOCKER_FAILS=9 login docker-release-finalize-ghcr ghcr.io)
chk "D-L4 a login that never succeeds stops after five attempts and fails the step" \
  "$(wc -l <"$DOCKER_LOG" | tr -d ' ')|$(printf '%s' "$out_l" | sed -n 's/^EXIT=//p')" "5|1"
chk_has "D-L4 naming the failure" "$out_l" "::error::login_once failed after 5 attempts"
chk "D-L5 the build job moves the ci source out of the build context before building" \
  "$(gate '."build-steps" | (index("Move the ci source out of the build context") < index("Build and push by digest")) and (index("Check out the ci source") < index("Log in to GHCR"))')" "true"
chk "D-L6 the SARIF upload runs only when the scan wrote its file" "$(gate '."sarif-if"')" \
  "\${{ always() && env.PUBLISH == 'true' && hashFiles('trivy-image.sarif') != '' }}"

# ── verify-publish: every registry written serves this run's digest ──────────
# Stub curl: a token request (-o) gets a token; a manifest HEAD (-I) answers
# SERVE_<site> as its digest once the site has been asked SERVE_<site>_FROM
# times (default 1), else fails as a 404 does. Sites: ghcr, hub, dash.
VBIN="$WORK/vbin"
mkdir -p "$VBIN"
export VCURL_LOG="$WORK/vcurl.log"
cat >"$VBIN/curl" <<'SH'
#!/bin/sh
out="" url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift ;;
    http*) url=$1 ;;
  esac
  shift
done
if [ -n "$out" ]; then
  printf '{"token":"t"}\n' >"$out"
  exit 0
fi
case "$url" in
  */dashboard/manifests/*) site=dash ;;
  https://registry-1.docker.io/*) site=hub ;;
  *) site=ghcr ;;
esac
printf '%s\n' "$site" >>"$VCURL_LOG"
eval "want=\${SERVE_$site:-} from=\${SERVE_${site}_FROM:-1}"
if [ -n "$want" ] && [ "$(grep -cx "$site" "$VCURL_LOG")" -ge "$from" ]; then
  printf 'HTTP/2 200\r\ndocker-content-digest: %s\r\n\r\n' "$want"
  exit 0
fi
exit 22
SH
printf '#!/bin/sh\nexit 0\n' >"$VBIN/sleep"
chmod 755 "$VBIN/curl" "$VBIN/sleep"
D_IDX="sha256:$(printf '1%.0s' $(seq 64))" D_DASH="sha256:$(printf '2%.0s' $(seq 64))"
vp_image() { # env-prefixed: runs the step; prints its output, EXIT=n on failure
  : >"$VCURL_LOG"
  (PATH="$VBIN:$PATH" CI_TOOLS="$ROOT/scripts" RUNNER_TEMP="$WORK" REGISTRY=ghcr.io IMAGE_NAME=owner/app \
    VERSION=v1.3.0 DIGEST="$D_IDX" bash "$WORK/vpimage.sh" 2>&1) || echo "EXIT=$?"
}
out_v=$(CHANNEL=stable REGISTRIES=ghcr,dockerhub DASHBOARD_DIGEST="$D_DASH" SERVE_ghcr="$D_IDX" \
  SERVE_hub="$D_IDX" SERVE_hub_FROM=7 SERVE_dash="$D_DASH" vp_image)
chk "D-X1 a Docker Hub tag served on the seventh read passes" "$(printf '%s' "$out_v" | grep -c '^EXIT=' || true)" "0"
chk_has "D-X1 reported as a late answer" "$out_v" \
  "::notice::docker.io/owner/app:v1.3.0 was served on attempt 7, after 195s of waiting"
chk_has "D-X1 the dashboard at its own digest" "$out_v" "ok: ghcr.io/owner/app/dashboard:v1.3.0 -> $D_DASH"
out_v=$(CHANNEL=stable REGISTRIES=ghcr,dockerhub DASHBOARD_DIGEST="" SERVE_ghcr="$D_DASH" SERVE_hub="$D_IDX" vp_image)
chk "D-X2 a tag at another digest fails" "$(printf '%s' "$out_v" | sed -n 's/^EXIT=//p')" "1"
chk_has "D-X2 naming what the registry served" "$out_v" \
  "::error::ghcr.io/owner/app:v1.3.0: expected $D_IDX, registry answered '$D_DASH'"
chk "D-X2 after the whole wait, and Docker Hub still read once" \
  "$(grep -cx ghcr "$VCURL_LOG")|$(grep -cx hub "$VCURL_LOG")" "14|1"
out_v=$(CHANNEL=dev REGISTRIES=ghcr,dockerhub DASHBOARD_DIGEST="$D_DASH" SERVE_ghcr="$D_IDX" vp_image)
chk_has "D-X3 a dashboard never served fails as nothing" "$out_v" \
  "::error::ghcr.io/owner/app/dashboard:v1.3.0: expected $D_DASH, registry answered 'nothing'"
chk "D-X3 and a dev run never reads Docker Hub" "$(grep -cx hub "$VCURL_LOG" || true)" "0"

echo "PASS ($PASS checks)"
