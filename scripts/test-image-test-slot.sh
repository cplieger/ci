#!/usr/bin/env bash
# Pins the meta ci.yaml docker job's two image-test steps. Both are EXTRACTED
# from the workflow at runtime, never mirrored: each `if:` goes through
# _ci_local's evaluator and each body runs under GitHub's default `bash -e` in
# a fixture checkout, so the probe cannot drift from what ships. CI_WORKFLOW
# points it at another copy. SC2016: fixture bodies are literal text.
# shellcheck disable=SC2016
set -euo pipefail

PASS=0
fail() {
  echo "FAIL: $*" >&2
  exit 1
}
chk() {
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1))
    echo "ok: $1 -> $2"
  else
    fail "$1: expected '$3', got '$2'"
  fi
}
chk_has() {
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
WORKFLOW="${CI_WORKFLOW:-$ROOT/.github/workflows/ci.yaml}"
WORK="$(mktemp -d /tmp/image-test-slot-probe.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
export PYTHONDONTWRITEBYTECODE=1

# ── Extract the subjects ─────────────────────────────────────────────────────
python3 - "$WORKFLOW" "$WORK" <<'PY'
import sys, yaml

wf, out = sys.argv[1], sys.argv[2]
jobs = yaml.safe_load(open(wf))["jobs"]
docker = jobs["docker"]
open(f"{out}/timeout", "w").write(str(docker.get("timeout-minutes", "")))
for i, step in enumerate(docker["steps"]):
    if str(step.get("uses", "")).startswith("docker/build-push-action@"):
        w = step.get("with", {})
        open(f"{out}/build", "w").write(f"{i} {w.get('tags')} {w.get('load')} {step.get('id')}\n")
    for name, key in (("Image smoke test", "harness"), ("Image test suite", "suite")):
        if step.get("name") == name:
            open(f"{out}/{key}.sh", "w").write(step.get("run", ""))
            open(f"{out}/{key}.if", "w").write(str(step.get("if", "")))
            open(f"{out}/{key}.idx", "w").write(str(i))
            open(f"{out}/{key}.coe", "w").write(str(step.get("continue-on-error", "absent")))
arm = [str(s) for s in jobs["docker-arm64"]["steps"] if "tests/image-" in str(s)]
open(f"{out}/arm64", "w").write(str(len(arm)))
PY

eval_if() {
  python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from _ci_local import evaluate_step_if
print(evaluate_step_if(open(sys.argv[2]).read(), {}, {}, sys.argv[3], {"build": sys.argv[4]}))
' "$ROOT" "$1" "$2" "${3:-success}"
}
overrides_success() {
  python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from _ci_local import overrides_implicit_success
print(overrides_implicit_success(open(sys.argv[2]).read()))
' "$ROOT" "$1"
}

run_body() {
  RC=0
  OUT="$(cd "$2" && PROBE_OUT="$2/out" bash -e "$WORK/$1.sh" 2>&1)" || RC=$?
}

# ── T1-T3, T13-T15: the steps as declared ────────────────────────────────────
for key in harness suite; do
  chk "T1 $key step extracted" "$([ -s "$WORK/$key.sh" ] && echo yes || echo no)" "yes"
  chk "T1 $key body parses under bash" "$(bash -n "$WORK/$key.sh" 2>/dev/null && echo ok || echo bad)" "ok"
  chk "T13 $key step blocks the job" "$(cat "$WORK/$key.coe")" "absent"
done
read -r build_idx build_tags build_load build_id <"$WORK/build"
chk "T2 the build loads the image both steps test" "$build_tags $build_load" "ci-smoke:latest True"
chk "T2 the build step's id is the one the suite gate reads" "$build_id" "build"
h_idx="$(cat "$WORK/harness.idx")"
s_idx="$(cat "$WORK/suite.idx")"
chk "T2 build, then harness, then suite" \
  "$([ "$build_idx" -lt "$h_idx" ] && [ "$h_idx" -lt "$s_idx" ] && echo ordered || echo "build=$build_idx harness=$h_idx suite=$s_idx")" "ordered"
chk "T3 harness gate" "$(cat "$WORK/harness.if")" "\${{ hashFiles('tests/image-smoke.sh') != '' }}"
# Exact text: always() evaluates like !cancelled() here but runs on a cancel too.
chk "T3 suite gate" "$(cat "$WORK/suite.if")" \
  "\${{ !cancelled() && steps.build.outcome == 'success' && hashFiles('tests/image-test.sh') != '' }}"
chk "T14 docker job budget (minutes)" "$(cat "$WORK/timeout")" "15"
chk "T15 docker-arm64 runs no image test" "$(cat "$WORK/arm64")" "0"

# ── Fixtures ─────────────────────────────────────────────────────────────────
# The unrelated tests/x.sh is bait for a hashFiles() glob wider than one file.
fixture() {
  local d="$WORK/fx-$1"
  mkdir -p "$d/tests"
  printf '#!/bin/sh\nexit 0\n' >"$d/tests/x.sh"
  printf '%s\n' "$d"
}
suite() {
  local d="$1" mode="$2"
  shift 2
  printf '%s\n' "$@" >"$d/tests/image-test.sh"
  chmod "$mode" "$d/tests/image-test.sh"
}

# ── T4/T5: presence decides whether each step runs ───────────────────────────
none="$(fixture none)"
only_harness="$(fixture only-harness)"
printf '#!/bin/sh\n' >"$only_harness/tests/image-smoke.sh"
only_suite="$(fixture only-suite)"
suite "$only_suite" 755 '#!/bin/sh'
chk "T4 suite skipped when absent" "$(eval_if "$WORK/suite.if" "$none")" "False"
chk "T4 suite skipped beside the harness alone" "$(eval_if "$WORK/suite.if" "$only_harness")" "False"
chk "T4 harness skipped when absent" "$(eval_if "$WORK/harness.if" "$only_suite")" "False"
chk "T5 suite runs when present" "$(eval_if "$WORK/suite.if" "$only_suite")" "True"
chk "T5 harness runs when present" "$(eval_if "$WORK/harness.if" "$only_harness")" "True"

# ── T16: a red harness does not skip the suite; a failed build skips both ────
chk "T16 suite runs after a red harness" "$(overrides_success "$WORK/suite.if")" "True"
chk "T16 harness waits on a successful build" "$(overrides_success "$WORK/harness.if")" "False"
chk "T16 suite skipped after a failed build" "$(eval_if "$WORK/suite.if" "$only_suite" failure)" "False"
chk "T16 suite skipped after a skipped build" "$(eval_if "$WORK/suite.if" "$only_suite" skipped)" "False"

# ── T6: exit 0 passes, and the suite receives exactly the image ref ──────────
d="$(fixture pass)"
suite "$d" 755 '#!/usr/bin/env bash' 'printf "%s\n" "$#" "$1" >"$PROBE_OUT"'
run_body suite "$d"
chk "T6 passing suite: step rc" "$RC" "0"
chk "T6 suite argv" "$(tr '\n' ' ' 2>/dev/null <"$d/out" || echo none)" "1 ci-smoke:latest "

# ── T7: a non-zero exit fails the step with that status ──────────────────────
d="$(fixture red)"
suite "$d" 755 '#!/usr/bin/env bash' 'exit 3'
run_body suite "$d"
chk "T7 failing suite: step rc" "$RC" "3"

# ── T8: present but not executable fails, naming the fix ─────────────────────
d="$(fixture mode644)"
suite "$d" 644 '#!/usr/bin/env bash' 'printf "ran\n" >"$PROBE_OUT"'
run_body suite "$d"
chk "T8 644 suite: step rc" "$RC" "1"
chk_has "T8 names the file and the cause" "$OUT" "::error file=tests/image-test.sh::tests/image-test.sh is not executable."
chk_has "T8 names the fix" "$OUT" "commit it as mode 100755: git update-index --chmod=+x tests/image-test.sh"
chk "T8 the suite never ran" "$([ -e "$d/out" ] && echo ran || echo none)" "none"

# ── T9/T10: the shebang picks the interpreter ────────────────────────────────
d="$(fixture bashisms)"
suite "$d" 755 '#!/usr/bin/env bash' 'set -euo pipefail' \
  'parts=("${1%%:*}" "${1#*:}")' \
  'read -r tag <<<"${parts[1]}"' \
  '[[ ${parts[0]} == ci-smoke && $tag == latest ]]' \
  'printf "bash:%s\n" "$tag" >"$PROBE_OUT"'
run_body suite "$d"
chk "T9 bash-only suite: step rc" "$RC" "0"
chk "T9 bash-only suite ran to the end" "$(cat "$d/out" 2>/dev/null || echo none)" "bash:latest"

d="$(fixture python)"
suite "$d" 755 '#!/usr/bin/env python3' 'import os, sys' \
  'open(os.environ["PROBE_OUT"], "w").write("python:" + sys.argv[1] + "\n")'
run_body suite "$d"
chk "T10 python suite: step rc" "$RC" "0"
chk "T10 python suite ran under python" "$(cat "$d/out" 2>/dev/null || echo none)" "python:ci-smoke:latest"

# ── T11: an interpreter that does not exist fails closed ─────────────────────
d="$(fixture badshebang)"
suite "$d" 755 '#!/nonexistent/interpreter' 'exit 0'
run_body suite "$d"
chk "T11 missing interpreter fails the step" "$([ "$RC" -ne 0 ] && echo failed || echo "rc=$RC")" "failed"

# ── T12: the harness runs through sh whatever its mode ───────────────────────
d="$(fixture harness)"
printf '%s\n' '#!/bin/sh' \
  'if [ -n "${BASH_VERSION:-}" ]; then shell=bash; else shell=sh; fi' \
  'printf "%s %s\n" "$shell" "$1" >"$PROBE_OUT"' >"$d/tests/image-smoke.sh"
chmod 644 "$d/tests/image-smoke.sh"
run_body harness "$d"
chk "T12 644 harness: step rc" "$RC" "0"
chk "T12 harness interpreter and argv" "$(cat "$d/out" 2>/dev/null || echo none)" "sh ci-smoke:latest"

echo "PASS ($PASS checks)"
