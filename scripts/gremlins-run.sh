#!/usr/bin/env bash
# One module's gremlins run inside the memory-capped container that
# .github/workflows/weekly-gremlins.yaml starts, from the module root. Env:
# GREMLINS_VERSION, GREMLINS_PATCH (absolute path of gremlins-pkgname.patch),
# WORKERS_MAX, GOMEM_MAX_MB / GOMEM_SOLO_MB (GOMEMLIMIT for WORKERS_MAX workers /
# one forced worker), PROBE_TIMEOUT (seconds per probe phase), OUT (result JSON),
# and optional JOB_TIMEOUT_MINUTES (the run job's cap; unset keeps per-package).
# Writes the chosen mode to "${OUT}.mode" for scripts/gremlins-merge.py.
set -euo pipefail

# The clone is bind-mounted from the runner and owned by the runner uid, while
# this container runs as root, so git refuses it as "dubious ownership" (exit
# 128) and every `go build` a test runs fails under -buildvcs=auto. Trust every
# path: the probe's cp -a copies keep the runner uid too, and gremlins' worker
# copies live elsewhere. The config is the container's /root/.gitconfig, which
# dies with --rm.
git config --global --add safe.directory '*'

: "${GREMLINS_VERSION:?}" "${GREMLINS_PATCH:?}" "${WORKERS_MAX:?}"
: "${GOMEM_MAX_MB:?}" "${GOMEM_SOLO_MB:?}" "${PROBE_TIMEOUT:?}" "${OUT:?}"

# gremlins budgets each mutant at coverage-pass time x this coefficient (default
# 3: https://github.com/go-gremlins/gremlins/blob/v0.6.0/internal/engine/executor.go#L101).
# The coverage pass has every CPU to itself and a mutant gets about 1/workers of
# them, so a CPU-bound survivor would time out at the stock 3 and drop out of
# efficacy.
timeout_coefficient() {
  printf '%s\n' "$((3 * $1))"
}

# integration_wanted SUITE_SECS MUTANTS WORKERS CAP_MINUTES prints the estimate
# (suite x mutants / workers) against a quarter of the cap and succeeds when it
# fits. Any missing or non-numeric input fails, keeping per-package mode.
integration_wanted() {
  [ "$#" -eq 4 ] || return 1
  local n
  for n in "$@"; do
    case "${n}" in '' | 0?* | *[!0-9]*) return 1 ;; esac
  done
  [ "$3" -gt 0 ] || return 1
  local estimate=$(($1 * $2 / $3)) budget=$(($4 * 60 / 4))
  printf 'estimate=%ss budget=%ss\n' "${estimate}" "${budget}"
  [ "${estimate}" -lt "${budget}" ]
}

# select_mode SUITE_SECS MUTANTS WORKERS prints the mode and logs it with its
# inputs to stderr; an unset or empty JOB_TIMEOUT_MINUTES is an unmeasured cap.
select_mode() {
  local cap="${JOB_TIMEOUT_MINUTES:-}" mode=per-package sizing=
  if sizing=$(integration_wanted "$1" "$2" "$3" "${cap}"); then
    mode=integration
  fi
  local suite="${1:+$1s}" cap_text="${cap:+${cap}min}"
  printf 'mode=%s (suite=%s mutants=%s workers=%s cap=%s%s)\n' "${mode}" \
    "${suite:-unmeasured}" "${2:-unmeasured}" "$3" "${cap_text:-unmeasured}" "${sizing:+ ${sizing}}" >&2
  printf '%s\n' "${mode}"
}

retry() {
  local delay=1 attempt
  for attempt in 1 2 3 4 5 6 7 8; do
    if "$@"; then
      return 0
    fi
    [ "${attempt}" -eq 8 ] && break
    echo "::warning::attempt ${attempt} of '$*' failed; retrying in ${delay}s" >&2
    sleep "${delay}"
    delay=$((delay * 2))
  done
  return 1
}

gremlins_src=/tmp/gremlins-src
clone_gremlins() {
  rm -rf "${gremlins_src}"
  timeout 120 git -c advice.detachedHead=false \
    -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 \
    clone --quiet --depth 1 --branch "${GREMLINS_VERSION}" \
    https://github.com/go-gremlins/gremlins "${gremlins_src}"
}

# v0.6.0 tests a `package main` mutant against the module root's tests
# (https://github.com/go-gremlins/gremlins/issues/268). Delete the patch and this
# build once a pinned gremlins release contains the fix:
# https://github.com/go-gremlins/gremlins/pull/306
retry clone_gremlins
if ! git -C "${gremlins_src}" apply --check "${GREMLINS_PATCH}"; then
  echo "::error::scripts/gremlins-pkgname.patch no longer applies to gremlins ${GREMLINS_VERSION}: upstream has probably changed or fixed pkgName. Delete scripts/gremlins-pkgname.patch and this build step, and go back to the release download."
  exit 1
fi
git -C "${gremlins_src}" apply "${GREMLINS_PATCH}"
(
  cd "${gremlins_src}"
  retry timeout 300 go mod download
  CGO_ENABLED=0 timeout 600 go build -trimpath \
    -ldflags "-X main.version=${GREMLINS_VERSION#v}" \
    -o /usr/local/bin/gremlins ./cmd/gremlins
)

# Compiler output does not depend on GOGC and -toolexec never wraps the test
# binary, so no test sees a difference. It does shorten the coverage pass that
# gremlins multiplies into the per-mutant timeout, by about 15% on a large module.
emit_gogc_toolexec() {
  cat <<'EOF'
#!/bin/sh
case "${1##*/}" in compile | link | vet) GOGC=400 && export GOGC ;; esac
exec "$@"
EOF
}
emit_gogc_toolexec >/usr/local/bin/gogc-toolexec
chmod 0755 /usr/local/bin/gogc-toolexec
export GOFLAGS="${GOFLAGS:+${GOFLAGS} }-toolexec=/usr/local/bin/gogc-toolexec"

# --- Concurrency probe -------------------------------------------------------
# Each gremlins worker gets its own COPY of the module tree, but an absolute
# path does not move with the copy and every worker shares this container's
# /tmp. A test bound to a fixed global path therefore collides with a sibling
# worker running a copy of that same test, and the damage is not a lost mutant
# but a FALSE one: measured on docker-rsync-scheduler, whose three tests create
# health.DefaultPath = /tmp/.healthy, one worker's cleanup deleted the marker
# another was polling for. All three of its live mutants are provably
# equivalent, yet each of three attempts named a DIFFERENT single survivor and
# called the other two KILLED — six of nine verdicts false, and its 100.0%
# weeks (2026-07-20, 07-27, 08-10) were measurement failures, because
# equivalent mutants cannot all be killed.
#
# The condition is DETECTABLE, so it is detected instead of listed: run the
# suite alone, then run two copies at once, and if it only fails the second way
# this suite cannot share a filesystem with itself and gets --workers 1. Why not
# a per-repo opt-out list — the obvious shape: a list keyed on "this repo's
# tests currently bind /tmp/.healthy" keeps halving throughput long after the
# test is fixed and silently misses the next repo that grows the same habit
# (this repo has been bitten by exactly that kind of carve-out before). The
# probe graduates itself: fix the test, and the next run goes back to full
# workers with no edit here. It also catches what grepping the repo cannot —
# the offending path is a const in a DEPENDENCY (cplieger/health), not a
# literal in the repo under test.
#
# Detection is one-sided. A narrow race can pass the probe and still fake a
# verdict, which is why gremlins-aggregate.py separately flags attempts that
# disagree about which mutants survived. Cost is one extra suite run against a
# mutation run that executes the suite once per mutant.
workers="${WORKERS_MAX}"
export GOMEMLIMIT="${GOMEM_MAX_MB}MiB"
suite_secs=
mutants=

if [ "${WORKERS_MAX}" -gt 1 ]; then
  # gremlins downloads modules itself; doing it here first keeps the probe from
  # timing out on a cold module cache.
  go mod download || echo "::warning::go mod download failed before the concurrency probe; letting gremlins report it"

  # Probe in COPIES of the module tree, the way gremlins runs workers, for two
  # reasons. First, the probe must not leave debris in the tree gremlins is
  # about to mutate: a test that writes inside its own package directory would
  # otherwise dirty /work/<module> before the run, and gremlins copies that tree
  # to every worker. Second, two runs in ONE tree can collide over an in-tree
  # path that would never collide under gremlins, where each worker has its own
  # copy — a narrow window rather than a measured problem (a three-test fixture
  # writing a fixed in-tree file did not reproduce it), but the copies close it
  # for free. What the copies deliberately keep sharing is /tmp, because that is
  # the actual hazard.
  #
  # The solo baseline runs in a copy too: a module that only builds in place (a
  # relative `replace`, say) then fails the solo phase and skips the probe,
  # instead of failing the concurrent phase and looking like interference.
  probe_a=/tmp/probe-tree-a
  probe_b=/tmp/probe-tree-b
  mkdir -p "${probe_a}" "${probe_b}"
  cp -a . "${probe_a}/"
  cp -a . "${probe_b}/"

  suite_start=$(date +%s)
  if (cd "${probe_a}" && timeout "${PROBE_TIMEOUT}" go test -count=1 ./...) >/tmp/probe-solo.log 2>&1; then
    suite_secs=$(($(date +%s) - suite_start))
    # Stagger the second run by a second. This is what makes the probe work:
    # the failure mode is one process's CLEANUP landing inside another's poll or
    # read, and two suites started together stay in near-lockstep, so their
    # write/read/cleanup phases align instead of interleaving. Measured on two
    # fixtures modelled on docker-rsync-scheduler (three tests creating,
    # polling and removing a fixed /tmp marker): simultaneous starts detected
    # the collision in 1 and 2 of 5 runs, a 1-second offset in 5 of 5 for both.
    # Under gremlins the same rare window gets hit anyway, because it runs the
    # suite once per mutant — hundreds of times, from workers that are never in
    # step. The probe only gets one shot, so it has to buy its sensitivity.
    (cd "${probe_a}" && timeout "${PROBE_TIMEOUT}" go test -count=1 ./...) >/tmp/probe-a.log 2>&1 &
    pa=$!
    sleep 1
    (cd "${probe_b}" && timeout "${PROBE_TIMEOUT}" go test -count=1 ./...) >/tmp/probe-b.log 2>&1 &
    pb=$!
    ra=0
    wait "${pa}" || ra=$?
    rb=0
    wait "${pb}" || rb=$?

    if [ "${ra}" = 124 ] || [ "${rb}" = 124 ]; then
      # Unverified beats throttled: a suite too slow to probe is also a suite
      # whose mutation run needs every worker it can get.
      echo "::warning::concurrency probe timed out (>${PROBE_TIMEOUT}s); keeping ${WORKERS_MAX} workers unverified"
    elif [ "${ra}" -ne 0 ] || [ "${rb}" -ne 0 ]; then
      workers=1
      export GOMEMLIMIT="${GOMEM_SOLO_MB}MiB"
      echo "::warning::suite passes in one tree copy but fails when two copies run at once (exit ${ra}/${rb})"
      echo "::warning::forcing --workers 1: cross-worker interference reports false KILLs, not lost mutants"
      grep -hE "FAIL|panic:" /tmp/probe-a.log /tmp/probe-b.log | head -20 || true
    else
      echo "concurrency probe passed; keeping ${WORKERS_MAX} workers"
    fi
  else
    # Not the probe's business to diagnose a red suite — gremlins fails its own
    # coverage pass next and reports it as the error it is.
    echo "::warning::suite does not pass on its own; skipping the concurrency probe, keeping ${WORKERS_MAX} workers"
  fi

  # --integration alone changes only which tests a mutant runs; --coverpkg ./...
  # is what credits code that only another package's tests reach, which would
  # otherwise stay NOT COVERED and never run
  # (https://github.com/go-gremlins/gremlins/blob/v0.6.0/internal/coverage/coverage.go#L150).
  # The dry run counts the mutants that pair would execute.
  if [ -n "${suite_secs}" ]; then
    if (cd "${probe_a}" && timeout "${PROBE_TIMEOUT}" gremlins unleash --dry-run --coverpkg ./... --output /tmp/gremlins-dry.json .) >/tmp/gremlins-dry.log 2>&1; then
      mutants=$(grep --only-matching '"status":"RUNNABLE"' /tmp/gremlins-dry.json | wc --lines) || mutants=
    else
      printf '::warning::gremlins --dry-run failed; keeping per-package mode\n'
      tail --lines=5 /tmp/gremlins-dry.log
    fi
  fi
  rm -rf "${probe_a}" "${probe_b}"
fi

mode=$(select_mode "${suite_secs}" "${mutants}" "${workers}")
mode_flags=()
if [ "${mode}" = integration ]; then
  mode_flags=(--integration --coverpkg ./...)
fi

coefficient=$(timeout_coefficient "${workers}")
printf 'workers=%s timeout-coefficient=%s GOMEMLIMIT=%s GOFLAGS=%s module=%s\n' \
  "${workers}" "${coefficient}" "${GOMEMLIMIT}" "${GOFLAGS}" "$(pwd)"
gremlins unleash --workers "${workers}" --timeout-coefficient "${coefficient}" "${mode_flags[@]}" --output "${OUT}" .
printf '%s\n' "${mode}" >"${OUT}.mode"
