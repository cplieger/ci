#!/usr/bin/env bash
# Regression probe for the verify-publish registry read-back contract that
# release.yaml embeds inline (the `Verify published artifacts` step).
#
# That step is the only executor of its own shell, and its function signatures
# are invisible to shellcheck: a signature change to `probe` once left
# `probe_ts` on the old arity, so `url=$3` was unset and every npm/JSR read
# died under `set -u` whether or not the package had published — breaking every
# TypeScript release with the linters completely clean. It was caught by an
# ad-hoc harness that was then discarded. This file is that harness, kept.
#
# What is pinned: the Go-proxy classification (a `unknown revision` negative
# cache WARNS and keeps the release green; every other refusal stays a red
# error), the six-tries retry loop, the npm/JSR pair and the exact URLs both
# receive, the nested-lane discovery through `git tag --points-at HEAD`, and
# that a warning can never mask a real failure in the same run.
#
# The step body is EXTRACTED from release.yaml at runtime and executed; the
# script carries no copy, because a copy drifts and would have missed the very
# bug above. PyYAML is available in the `scripts` CI job by step order: the
# yamllint install earlier in the same job depends on it.
#
# Runs in the ci repo's `scripts` CI job (opt-in by file presence), so an edit
# to the probe functions, the retry loop or the dispatch cannot merge if it
# breaks the pinned contract. Local run: scripts/test-verify-publish.sh
set -euo pipefail

# Hermetic git, same rationale as the sibling probes: a workstation
# `tag.gpgsign true` turns the lane fixture's bare `git tag` into an
# editor-opening signed tag and hangs the run, while CI stays green.
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
chk_lacks() { # label haystack needle
  case "$2" in
    *"$3"*) fail "$1: output unexpectedly contains '$3'
--- output ---
$2" ;;
    *)
      PASS=$((PASS + 1))
      echo "ok: $1"
      ;;
  esac
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE_YAML="$ROOT/.github/workflows/release.yaml"
WORK="$(mktemp -d /tmp/verify-publish.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# ── Extract the subject ──────────────────────────────────────────────────────
# The YAML parser dedents the block scalar, so `run` is directly runnable.
python3 - "$RELEASE_YAML" "$WORK/step.sh" "$WORK/env-keys" <<'PY'
import sys, yaml

wf, out, keys = sys.argv[1], sys.argv[2], sys.argv[3]
job = yaml.safe_load(open(wf))["jobs"]["verify-publish"]
step = next(s for s in job["steps"] if s.get("name") == "Verify published artifacts")
open(out, "w").write(step["run"])
open(keys, "w").write("\n".join(sorted(step.get("env", {}))) + "\n")
PY

# ── Pre-flight: a failed extraction makes every negative case vacuous ────────
STEP_BODY=$(cat "$WORK/step.sh")
chk "V-P1 extracted body is substantial" \
  "$([ "$(wc -c <"$WORK/step.sh")" -ge 500 ] && echo yes || echo no)" "yes"
chk_has "V-P2 body defines probe()" "$STEP_BODY" 'probe() {'
chk_has "V-P3 body defines probe_go()" "$STEP_BODY" 'probe_go() {'
chk_has "V-P4 body defines probe_ts()" "$STEP_BODY" 'probe_ts() {'
chk_has "V-P5 body is strict-mode" "$STEP_BODY" 'set -euo pipefail'
chk_has "V-P6 body retries six times" "$STEP_BODY" 'for i in 1 2 3 4 5 6'
chk_has "V-P7 body discovers lane tags at HEAD" "$STEP_BODY" 'git tag --points-at HEAD'
chk "V-P8 body parses under bash" \
  "$(bash -n "$WORK/step.sh" 2>/dev/null && echo ok || echo bad)" "ok"
# Guard against an eighth env var arriving later: the cases would leave it
# unset, it would die under set -u, and that reads as a real refusal.
chk "V-P9 step env is exactly the seven the cases populate" \
  "$(cat "$WORK/env-keys")" \
  "GO_LANES_JSON
GO_NESTED_RESULT
GO_RESULT
SUBPACKAGES_JSON
SUBPACKAGE_RESULT
TS_RESULT
VERSION"

# ── Stubs: no network, no waiting ────────────────────────────────────────────
# jq, git, awk, sed and tr stay REAL — the body needs them, and stubbing them
# would test the stub.
BIN="$WORK/bin"
mkdir -p "$BIN"
export CURL_SPEC="$WORK/curl.spec" CURL_LOG="$WORK/curl.log"

cat >"$BIN/sleep" <<'SH'
#!/bin/sh
exit 0
SH

# Honours -o, because `probe` passes -o /dev/null and a body on stdout would
# land in the captured output the assertions grep.
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
printf '%s\n' "$URL" >>"$CURL_LOG"
# `while read -r pat rc body`, never `while IFS= read -r ...`: IFS= disables
# exactly the field splitting the three variables need, so the whole line
# lands in pat, nothing ever matches, and every case reports the success path.
while read -r pat rc body; do
  case "$URL" in
    $pat)
      if [ -n "$OUT" ]; then
        printf '%s\n' "$body" >"$OUT"
      else
        printf '%s\n' "$body"
      fi
      exit "$rc"
      ;;
  esac
done <"$CURL_SPEC"
# Deliberately free of `unknown revision`, so a mis-specified case cannot
# silently take the warn path.
printf 'stub: no spec entry for %s\n' "$URL"
exit 22
SH
chmod 755 "$BIN/sleep" "$BIN/curl"

# ── Case plumbing ────────────────────────────────────────────────────────────
defaults() { # every case starts fully populated; an unset var dies under set -u
  export VERSION=v1.2.3 SUBPACKAGES_JSON='[]' GO_LANES_JSON='[]'
  export GO_RESULT=skipped TS_RESULT=skipped SUBPACKAGE_RESULT=skipped GO_NESTED_RESULT=skipped
}
spec() { # <line>...
  printf '%s\n' "$@" >"$CURL_SPEC"
  : >"$CURL_LOG"
}
casedir() { # <name> -> path of a fresh fixture dir
  rm -rf "$WORK/case-$1"
  mkdir -p "$WORK/case-$1"
  printf '%s\n' "$WORK/case-$1"
}
gomod() { printf 'module github.com/cplieger/probe\n\ngo 1.26.5\n' >"$1/go.mod"; }
pkgjson() { printf '{"name":"@cplieger/probe","version":"1.2.3"}\n' >"$1/package.json"; }
# The body begins `set -euo pipefail`, so each case runs in a child bash; an
# in-process run would let a case's exit kill the suite.
run_step() { # <cwd> -> sets RC and OUT
  RC=0
  OUT=$(cd "$1" && PATH="$BIN:$PATH" bash "$WORK/step.sh" 2>&1) || RC=$?
}
lines() { awk 'END{print NR}' "$1"; }

PROXY_URL="https://proxy.golang.org/github.com/cplieger/probe/@v/v1.2.3.info"
LANE_URL="https://proxy.golang.org/github.com/cplieger/probe/yamlenv/@v/v1.1.0.info"
NPM_URL="https://registry.npmjs.org/@cplieger/probe/1.2.3"
JSR_URL="https://jsr.io/@cplieger/probe/1.2.3_meta.json"

# ── V-A: the Go proxy, one case per classification ───────────────────────────
D=$(casedir a1)
gomod "$D"
defaults
export GO_RESULT=success
spec "*proxy.golang.org* 22 github.com/cplieger/probe@v1.2.3: invalid version: unknown revision v1.2.3"
run_step "$D"
chk "V-A1 proxy negative cache keeps the release green" "$RC" "0"
chk_has "V-A1 proxy negative cache warns" "$OUT" \
  "::warning::proxy.golang.org github.com/cplieger/probe@v1.2.3"
chk_lacks "V-A1 proxy negative cache does not error" "$OUT" "::error::"

D=$(casedir a2)
gomod "$D"
defaults
export GO_RESULT=success
spec "*proxy.golang.org* 22 github.com/cplieger/probe@v1.2.3: invalid version: go.mod has post-v3 module path \"github.com/cplieger/probe/v3\" at revision v1.2.3"
run_step "$D"
chk "V-A2 wrong module path stays red" "$RC" "1"
chk_has "V-A2 wrong module path errors" "$OUT" "::error::proxy.golang.org"
chk_lacks "V-A2 wrong module path does not warn" "$OUT" "::warning::"
chk "V-A2 the proxy is tried six times" "$(lines "$CURL_LOG")" "6"

D=$(casedir a3)
gomod "$D"
defaults
export GO_RESULT=success
spec "*proxy.golang.org* 22 github.com/cplieger/probe@v1.2.3: invalid version: git ls-remote -q origin in /tmp/x: exit status 128"
run_step "$D"
chk "V-A3 unfetchable module stays red" "$RC" "1"
chk_has "V-A3 unfetchable module errors" "$OUT" "::error::proxy.golang.org"
chk_lacks "V-A3 unfetchable module does not warn" "$OUT" "::warning::"

D=$(casedir a4)
gomod "$D"
defaults
export GO_RESULT=success
spec '*proxy.golang.org* 0 {"Version":"v1.2.3","Time":"2026-01-01T00:00:00Z"}'
run_step "$D"
chk "V-A4 mirrored module passes" "$RC" "0"
chk_has "V-A4 mirrored module reports ok" "$OUT" \
  "ok: proxy.golang.org github.com/cplieger/probe@v1.2.3"
chk "V-A4 proxy URL is the module info endpoint" "$(cat "$CURL_LOG")" "$PROXY_URL"

# ── V-B: npm and JSR ─────────────────────────────────────────────────────────
D=$(casedir b1)
pkgjson "$D"
defaults
export TS_RESULT=success
spec "*registry.npmjs.org* 22 {}" "*jsr.io* 0 {}"
run_step "$D"
chk "V-B1 absent npm package fails the run" "$RC" "1"
chk_has "V-B1 absent npm package errors" "$OUT" \
  "::error::npm @cplieger/probe@1.2.3 is not published"
chk_has "V-B1 JSR still reported ok" "$OUT" "ok: jsr @cplieger/probe@1.2.3"

D=$(casedir b2)
pkgjson "$D"
defaults
export TS_RESULT=success
spec "*registry.npmjs.org* 0 {}" "*jsr.io* 22 {}"
run_step "$D"
chk "V-B2 absent JSR package fails the run" "$RC" "1"
chk_has "V-B2 absent JSR package errors" "$OUT" \
  "::error::jsr @cplieger/probe@1.2.3 is not published"
chk_has "V-B2 npm still reported ok" "$OUT" "ok: npm @cplieger/probe@1.2.3"

D=$(casedir b3)
pkgjson "$D"
defaults
export TS_RESULT=success
spec "*registry.npmjs.org* 0 {}" "*jsr.io* 0 {}"
run_step "$D"
chk "V-B3 both registries published passes" "$RC" "0"
chk_has "V-B3 npm ok" "$OUT" "ok: npm @cplieger/probe@1.2.3"
chk_has "V-B3 jsr ok" "$OUT" "ok: jsr @cplieger/probe@1.2.3"
chk_lacks "V-B3 no error on a clean run" "$OUT" "::error::"

# ── V-C: a warning must not mask a real failure ──────────────────────────────
D=$(casedir c1)
gomod "$D"
pkgjson "$D"
defaults
export GO_RESULT=success TS_RESULT=success
spec "*proxy.golang.org* 22 github.com/cplieger/probe@v1.2.3: invalid version: unknown revision v1.2.3" \
  "*registry.npmjs.org* 22 {}" "*jsr.io* 0 {}"
run_step "$D"
chk "V-C1 npm failure fails the run despite the proxy warning" "$RC" "1"
chk_has "V-C1 proxy only warns" "$OUT" "::warning::proxy.golang.org"
chk_has "V-C1 npm carries the error" "$OUT" "::error::npm"
chk_lacks "V-C1 proxy contributes no error" "$OUT" "::error::proxy.golang.org"

# ── V-D: nested Go lanes ─────────────────────────────────────────────────────
# The lane loop reads `git tag --points-at HEAD`, so this needs a real repo
# with a real lane tag, not a stub.
LANE="$WORK/lane"
mkdir -p "$LANE/yamlenv"
git -C "$LANE" init -q -b main
git -C "$LANE" config user.email probe@ci.local
git -C "$LANE" config user.name probe
git -C "$LANE" config commit.gpgsign false
printf 'module github.com/cplieger/probe\n\ngo 1.26.5\n' >"$LANE/go.mod"
printf 'module github.com/cplieger/probe/yamlenv\n\ngo 1.26.5\n' >"$LANE/yamlenv/go.mod"
git -C "$LANE" add -A
git -C "$LANE" commit -qm "feat: lane fixture"
git -C "$LANE" tag yamlenv/v1.1.0
# A fixture that produced no tag makes both cases pass with the loop never
# entering, so the tag is a precondition assertion.
chk "V-D0 lane fixture tag points at HEAD" \
  "$(git -C "$LANE" tag --points-at HEAD --list 'yamlenv/v[0-9]*')" "yamlenv/v1.1.0"

defaults
export GO_NESTED_RESULT=success GO_LANES_JSON='["yamlenv"]'
spec "*proxy.golang.org* 22 github.com/cplieger/probe/yamlenv@v1.1.0: invalid version: unknown revision yamlenv/v1.1.0"
run_step "$LANE"
chk "V-D1 lane negative cache keeps the release green" "$RC" "0"
chk_has "V-D1 lane negative cache warns" "$OUT" \
  "::warning::proxy.golang.org github.com/cplieger/probe/yamlenv@v1.1.0"
chk "V-D1 lane version is the tag with its dir prefix stripped" \
  "$(sort -u "$CURL_LOG")" "$LANE_URL"
chk "V-D1 the lane is tried six times" "$(lines "$CURL_LOG")" "6"

defaults
export GO_NESTED_RESULT=success GO_LANES_JSON='["yamlenv"]'
spec '*proxy.golang.org* 0 {"Version":"v1.1.0"}'
run_step "$LANE"
chk "V-D2 mirrored lane passes" "$RC" "0"
chk_has "V-D2 mirrored lane reports ok" "$OUT" \
  "ok: proxy.golang.org github.com/cplieger/probe/yamlenv@v1.1.0"

# ── V-E: the arity invariant the file exists for ─────────────────────────────
# With `probe` on a stale arity, `url` is unset, the function dies under set -u
# and NO url is ever requested — so asserting the argv names that failure
# instead of leaving it to a generic red.
D=$(casedir e1)
pkgjson "$D"
defaults
export TS_RESULT=success
spec "*registry.npmjs.org* 0 {}" "*jsr.io* 0 {}"
run_step "$D"
chk "V-E1 probe/probe_ts arity agrees: both registries reached, in order" \
  "$(cat "$CURL_LOG")" "$NPM_URL
$JSR_URL"
chk "V-E1 nothing else was requested" "$(lines "$CURL_LOG")" "2"
chk "V-E1 the run passes" "$RC" "0"

echo "PASS: verify-publish probe contract holds (${PASS} checks)"
