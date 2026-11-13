#!/usr/bin/env bash
# Install one Stryker entry and run Stryker in it, for mutation-stryker-repo.yaml's plan
# and run jobs, from the entry's package dir. Env: ENTRY (repo:dir, for messages),
# REPORTER (scripts/stryker-ci-reporter.mjs, absolute), and either CI_STRYKER_PLAN (the
# plan pass) or CI_STRYKER_TIMES, CI_STRYKER_RESULT and RUN_DEADLINE (a shard: SIGINT at
# that epoch second, SIGKILL KILL_GRACE seconds later, 300 by default); optional MUTATE.
# Exits 2 for a bad RUN_DEADLINE or KILL_GRACE, 1 when an install fails, 3 when the entry
# is not enrolled, 124 when the deadline passed before Stryker started or interrupted it,
# 137 when Stryker was then killed, else Stryker's status.
set -uo pipefail

: "${ENTRY:?}" "${REPORTER:?}" "${CI_STRYKER_PLAN:-${CI_STRYKER_TIMES:?}}"
: "${CI_STRYKER_TIMES:+${RUN_DEADLINE:?}${CI_STRYKER_RESULT:?}}"
case "${RUN_DEADLINE:-0}" in
  *[!0-9]*)
    echo "::error::${ENTRY}: RUN_DEADLINE=${RUN_DEADLINE} is not an epoch second"
    exit 2
    ;;
esac
case "${KILL_GRACE:=300}" in
  *[!0-9]*)
    echo "::error::${ENTRY}: KILL_GRACE=${KILL_GRACE} is not a number of seconds"
    exit 2
    ;;
esac

# Runs "$@" in its own process group for LEFT seconds, then interrupts the whole
# group, as GNU timeout does, and kills it GRACE seconds later. Not timeout(1):
# uutils' timeout (ubuntu-26.04's) kills at once after `-s INT -k N`, its own SIGINT
# ending the kill-after wait (src/uu/timeout/src/timeout.rs wait_or_kill_process, 0.12.0).
until_deadline() {
  local left=$1 grace=$2 marks pid timer rc=0
  shift 2
  marks=$(mktemp -d)
  set -m
  "$@" &
  pid=$!
  (
    sleep "${left}"
    : >"${marks}/interrupted"
    kill -INT -- "-${pid}" 2>/dev/null
    sleep "${grace}"
    : >"${marks}/killed"
    kill -KILL -- "-${pid}" 2>/dev/null
  ) &
  timer=$!
  set +m
  wait "${pid}" || rc=$?
  kill -- "-${timer}" 2>/dev/null
  wait "${timer}" 2>/dev/null
  if [ -e "${marks}/killed" ]; then
    rc=137
    echo "::error::${ENTRY}: the shard reached its time budget and Stryker did not end ${grace} s after the interrupt, so it was killed"
  elif [ -e "${marks}/interrupted" ]; then
    rc=124
    echo "::error::${ENTRY}: the shard reached its time budget, so it publishes nothing and records what it measured"
  elif [ "${rc}" -gt 128 ]; then
    echo "::error::${ENTRY}: Stryker ended on signal $((rc - 128)) before the deadline"
  fi
  rm -rf "${marks}"
  return "${rc}"
}

# A failed install must not reach the enrolment gate, which would read the
# missing binary as an un-enrolled dir and exit green having tested nothing.
if ! npm ci; then
  echo "::error::${ENTRY}: npm ci failed"
  exit 1
fi

# A config without @stryker-mutator/core is not run, with an actionable notice;
# the plan job records the dir not enrolled, and a run that still reaches it
# stages nothing, so its merge job fails and with it the call.
if ! npx --no-install stryker --version >/dev/null 2>&1; then
  msg="${ENTRY}: stryker.config.* present but @stryker-mutator/core is not a devDependency, so it is not enrolled and its call fails."
  echo "::notice::${msg} Enroll: npm i -D @stryker-mutator/core @stryker-mutator/vitest-runner"
  exit 3
fi

# Vitest Browser Mode needs the Chromium build the repo's own pinned playwright
# expects, enrolled by the `@vitest/browser-playwright` devDependency as in
# ts-ci.yaml. Chromium alone and no --with-deps: the runner image carries its
# libraries, and a suite that stubs engine quirks kills the same mutants in any
# engine.
if node -e "const d=require('./package.json').devDependencies||{}; process.exit(d['@vitest/browser-playwright']?0:1)"; then
  if ! npx --no-install playwright install chromium; then
    echo "::error::${ENTRY}: playwright chromium install failed"
    exit 1
  fi
fi

args=(--reporters "${CI_STRYKER_TIMES:+progress,}ci" --appendPlugins "${REPORTER}")
if [ -n "${CI_STRYKER_TIMES:-}" ]; then
  # A fresh test runner, and its Browser Mode Chromium, every 200 of its mutants:
  # about 30 min at 9.4 s a mutant, 4 h at 72.4 s. A restart costs about 70 s.
  args+=(--maxTestRunnerReuse 200)
fi
if [ -n "${MUTATE:-}" ]; then
  args+=(--mutate "${MUTATE}")
fi
# Below the runner's own priority, so the runner keeps the CPU it needs to stay
# connected (GitHub reports a starved runner as "lost communication").
limit=(nice -n 10)
if [ -n "${CI_STRYKER_TIMES:-}" ]; then
  left=$((RUN_DEADLINE - $(date +%s)))
  if [ "${left}" -le 0 ]; then
    echo "::error::${ENTRY}: the steps before Stryker used the shard's whole budget, so it does not start"
    exit 124
  fi
  limit=(until_deadline "${left}" "${KILL_GRACE}" "${limit[@]}")
fi

# GITHUB_ACTIONS=false on this command only: vitest adds its github-actions
# reporter when it is "true", so each killed mutant would annotate the job.
# Node 26's JSON.stringify fast path aborts ("Zone Allocation failed") on the
# checker's very large IPC message and Node refuses the flag in NODE_OPTIONS, so
# a declared script's node gets it from a preload. Remove both once Node fixes it.
if node -e "const s=require('./package.json').scripts||{}; process.exit(s['test:mutation']?0:1)"; then
  preload="${RUNNER_TEMP:-/tmp}/no-json-stringify-fast-path.cjs"
  printf '%s\n' "require('node:v8').setFlagsFromString('--no-json-stringify-fast-path');" >"${preload}"
  GITHUB_ACTIONS=false NODE_OPTIONS="${NODE_OPTIONS:+${NODE_OPTIONS} }--require=${preload}" \
    "${limit[@]}" npm run test:mutation -- "${args[@]}"
else
  GITHUB_ACTIONS=false "${limit[@]}" node --no-json-stringify-fast-path node_modules/.bin/stryker run "${args[@]}"
fi
