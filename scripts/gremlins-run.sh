#!/usr/bin/env bash
# One module's gremlins run inside the memory-capped container that
# .github/workflows/mutation-gremlins-repo.yaml starts, from the module root. Env:
# GREMLINS_VERSION, GREMLINS_PATCHES (applied in order), WORKERS_MAX, GOMEM_MAX_MB /
# GOMEM_SOLO_MB (GOMEMLIMIT at WORKERS_MAX / one worker), PROBE_TIMEOUT (seconds per
# probe phase), optional JOB_TIMEOUT_MINUTES (the run job's cap; unset keeps per-package),
# a shard's GREMLINS_MODE and GREMLINS_WORKERS (set together, replacing the probe and the
# mode choice), and OUT (result JSON; OUT.times and RUN_DEADLINE: unleash_mutants) or
# PLAN_DIR and PLAN_DEADLINE (size the module).
set -euo pipefail

# The clone is bind-mounted from the runner and owned by the runner uid, while
# this container runs as root, so git refuses it as "dubious ownership" (exit
# 128) and every `go build` a test runs fails under -buildvcs=auto. Trust every
# path: the probe's cp -a copies keep the runner uid too, and gremlins' worker
# copies live elsewhere. The config is the container's /root/.gitconfig, which
# dies with --rm.
git config --global --add safe.directory '*'

: "${GREMLINS_VERSION:?}" "${GREMLINS_PATCHES:?}" "${WORKERS_MAX:?}"
: "${GOMEM_MAX_MB:?}" "${GOMEM_SOLO_MB:?}" "${PROBE_TIMEOUT:?}" "${PLAN_DIR:-${OUT:?}}"
: "${PLAN_DIR:+${PLAN_DEADLINE:?}}" "${GREMLINS_WORKERS:+${GREMLINS_MODE:?}}" "${GREMLINS_MODE:+${GREMLINS_WORKERS:?}}"

# gremlins budgets each mutant at its coverage-pass test time x this coefficient:
# its own package's in per-package mode (with a floor), the whole pass's under
# --integration or when the package has no recorded time
# (gremlins-pkgtimeout.patch owns both; default 3:
# https://github.com/go-gremlins/gremlins/blob/v0.6.0/internal/engine/executor.go#L101).
# A mutant gets about 1/workers of the CPUs its package's tests had in the
# coverage pass, so a CPU-bound survivor would time out at the stock 3 and drop
# out of efficacy.
timeout_coefficient() {
  printf '%s\n' "$((3 * $1))"
}

# integration_wanted SUITE_SECS MUTANTS WORKERS CAP_MINUTES prints the estimate
# (suite x mutants / workers) against a quarter of the cap and succeeds when it
# fits. Any missing or non-numeric input fails, keeping per-package mode, and so
# does a suite of 0 s: under the one-second tick of `date +%s`, it is unmeasured.
integration_wanted() {
  [ "$#" -eq 4 ] || return 1
  local n
  for n in "$@"; do
    case "${n}" in '' | 0?* | *[!0-9]*) return 1 ;; esac
  done
  [ "$1" -gt 0 ] && [ "$3" -gt 0 ] || return 1
  local estimate=$(($1 * $2 / $3)) budget=$(($4 * 60 / 4))
  printf 'estimate=%ss budget=%ss\n' "${estimate}" "${budget}"
  [ "${estimate}" -lt "${budget}" ]
}

# select_mode SUITE_SECS MUTANTS WORKERS prints the mode and logs it with its
# inputs to stderr; an unset or empty JOB_TIMEOUT_MINUTES is an unmeasured cap.
# A GREMLINS_MODE from the shard plan wins: every shard of a module must run
# the mode its whole-module plan chose, or the merged attempt mixes two.
select_mode() {
  case "${GREMLINS_MODE:-}" in
    '') ;;
    integration | per-package)
      printf 'mode=%s (set by the shard plan)\n' "${GREMLINS_MODE}" >&2
      printf '%s\n' "${GREMLINS_MODE}"
      return 0
      ;;
    *)
      printf '::error::GREMLINS_MODE=%s is neither integration nor per-package\n' "${GREMLINS_MODE}" >&2
      return 1
      ;;
  esac
  local cap="${JOB_TIMEOUT_MINUTES:-}" mode=per-package sizing=
  if sizing=$(integration_wanted "$1" "$2" "$3" "${cap}"); then
    mode=integration
  fi
  local suite="${1:+$1s}" cap_text="${cap:+${cap}min}"
  printf 'mode=%s (suite=%s mutants=%s workers=%s cap=%s%s)\n' "${mode}" \
    "${suite:-unmeasured}" "${2:-unmeasured}" "$3" "${cap_text:-unmeasured}" "${sizing:+ ${sizing}}" >&2
  printf '%s\n' "${mode}"
}

# time_package_mutants DEADLINE prints "<import path><TAB><ms>" for each
# package with non-test Go files under the current directory: the larger of the
# wall and CPU time `go test` takes to rebuild and test it after a code change.
# A surviving mutant holds its worker for the wall time, and for at least the
# CPU time when the workers share the cores its tests use. A failing package,
# and every package left at DEADLINE (epoch seconds), gets no row. It edits each
# package in place and restores it, so run it in a copy of the tree.
time_package_mutants() {
  local dir path file rc out real user sys ms late='' TIMEFORMAT='%3R %3U %3S'
  go list -f '{{.Dir}}{{"\t"}}{{.ImportPath}}{{"\t"}}{{if .GoFiles}}{{index .GoFiles 0}}{{end}}' ./... \
    | while IFS=$'\t' read -r dir path file; do
      [ -n "${file}" ] || continue
      if [ "$(date +%s)" -ge "$1" ]; then
        [ -n "${late}" ] || echo "::warning::the plan deadline passed before ${path}, so it and every later package take the dearest package's time" >&2
        late=1
        continue
      fi
      cp -p "${dir}/${file}" "${dir}/${file}.orig"
      printf '\nfunc gremlinsPlan%s%s() int { return %s }\n' "${RANDOM}" "${RANDOM}" "${RANDOM}" >>"${dir}/${file}"
      rc=0
      out=$({ time go test -count=1 -failfast "${path}" >/dev/null 2>&1; } 2>&1) || rc=$?
      mv "${dir}/${file}.orig" "${dir}/${file}"
      [ "${rc}" -eq 0 ] || continue
      read -r real user sys <<<"${out}" || continue
      ms=$((10#${user/[.,]/} + 10#${sys/[.,]/}))
      real=$((10#${real/[.,]/}))
      [ "${real}" -le "${ms}" ] || ms="${real}"
      printf '%s\t%s\n' "${path}" "${ms}"
    done
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

# Each patch fixes a v0.6.0 bug. Delete a patch once a pinned gremlins release
# contains its fix, and this build once no patch is left:
# - gremlins-pkgname.patch: a `package main` mutant is tested against the module
#   root's tests (https://github.com/go-gremlins/gremlins/issues/268, fix
#   https://github.com/go-gremlins/gremlins/pull/306).
# - gremlins-pkgtimeout.patch: every mutant's timeout is the whole module's
#   coverage time, so a hang in a fast package costs the slowest suite's time
#   (https://github.com/go-gremlins/gremlins/issues/261).
retry clone_gremlins
read -r -a patches <<<"${GREMLINS_PATCHES}"
for patch in "${patches[@]}"; do
  if ! git -C "${gremlins_src}" apply --check "${patch}"; then
    echo "::error::scripts/${patch##*/} no longer applies to gremlins ${GREMLINS_VERSION}. If that release fixes the upstream issue listed for this patch in scripts/gremlins-run.sh, delete the patch (and this build step if it was the last one). Otherwise rebase the patch onto the release."
    exit 1
  fi
  git -C "${gremlins_src}" apply "${patch}"
done
(
  cd "${gremlins_src}"
  retry timeout 300 go mod download
  CGO_ENABLED=0 timeout 600 go build -trimpath \
    -ldflags "-X main.version=${GREMLINS_VERSION#v}" \
    -o /usr/local/bin/gremlins ./cmd/gremlins
)

# Compiler output does not depend on GOGC and -toolexec never wraps the test
# binary, so no test sees a difference. It shortens every compile and link
# gremlins runs, which was about 15% of a large module's coverage pass.
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
choose_workers() {
  workers="${WORKERS_MAX}"
  export GOMEMLIMIT="${GOMEM_MAX_MB}MiB"
  suite_secs=
  mutants=
  if [ -n "${GREMLINS_WORKERS:-}" ]; then
    # The plan sized this shard at the count its own probe left; a probe
    # here that failed would run the shard at 1 worker, ~4x its projection.
    workers="${GREMLINS_WORKERS}"
    echo "workers=${workers} (set by the shard plan, so no concurrency probe)"
    return 0
  fi
  [ "${WORKERS_MAX}" -gt 1 ] || return 0

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
}

# In plan mode the three probe phases share the time left before PLAN_DEADLINE.
# Under 3 s left would give each `timeout 0`, which is no limit at all.
plan_time_left() {
  [ -n "${PLAN_DIR:-}" ] || return 0
  local left=$((PLAN_DEADLINE - $(date +%s)))
  if [ "${left}" -lt 3 ]; then
    echo "::error::the plan deadline passed before the concurrency probe"
    exit 1
  fi
  [ "${PROBE_TIMEOUT}" -le $((left / 3)) ] || PROBE_TIMEOUT=$((left / 3))
}
plan_time_left
choose_workers

mode=$(select_mode "${suite_secs}" "${mutants}" "${workers}")

if [ -n "${PLAN_DIR:-}" ]; then
  printf 'workers=%s\nmode=%s\nsuite_secs=%s\nmutants=%s\nsetup_secs=%s\n' \
    "${workers}" "${mode}" "${suite_secs}" "${mutants}" "${SECONDS}" >"${PLAN_DIR}/plan.env"
  if [ "${mode}" = per-package ]; then
    left=$((PLAN_DEADLINE - $(date +%s)))
    if [ "${left}" -le 0 ]; then
      echo "::error::the plan deadline passed before the per-package gremlins dry run"
      exit 1
    fi
    timeout "${left}" gremlins unleash --dry-run --output "${PLAN_DIR}/dry-run.json" . >/tmp/gremlins-plan-dry.log 2>&1 || {
      echo "::error::the per-package gremlins dry run failed"
      tail --lines=20 /tmp/gremlins-plan-dry.log
      exit 1
    }
    plan_tree=/tmp/plan-tree
    rm -rf "${plan_tree}"
    cp -a . "${plan_tree}"
    (cd "${plan_tree}" && time_package_mutants "${PLAN_DEADLINE}") >"${PLAN_DIR}/mutant-ms.tsv"
  fi
  # gremlins-shard.py size_module trusts the files above only after this line.
  printf 'sized=1\n' >>"${PLAN_DIR}/plan.env"
  exit 0
fi

mode_flags=()
if [ "${mode}" = integration ]; then
  mode_flags=(--integration --coverpkg ./...)
fi

# stamp_lines FILE copies stdin to stdout and appends each line to FILE behind
# its arrival time, from which scripts/gremlins-shard.py costs prices each file.
stamp_lines() {
  local line
  {
    while IFS= read -r line || [ -n "${line}" ]; do
      printf '%s\n' "${line}"
      printf '%s\t%s\n' "${EPOCHREALTIME/,/.}" "${line}" >&3
    done
  } 3>>"$1"
}

# unleash_mutants runs the mutation pass and records it in OUT.times. By
# RUN_DEADLINE (epoch seconds; unset is none) it stops the pass and returns 124,
# the mutants finished so far still on record.
unleash_mutants() {
  local limit=()
  if [ -n "${RUN_DEADLINE:-}" ]; then
    local left=$((RUN_DEADLINE - $(date +%s)))
    if [ "${left}" -lt 3 ]; then
      echo "::error::the run deadline passed before the mutation pass"
      return 124
    fi
    limit=(timeout -k 60 "${left}")
  fi
  printf '#workers\t%s\n#launch\t%s\n' "${workers}" "$((EPOCHSECONDS - SECONDS))" >"${OUT}.times"
  "${limit[@]}" gremlins unleash --workers "${workers}" --timeout-coefficient "${coefficient}" \
    "${mode_flags[@]}" --output "${OUT}" . | stamp_lines "${OUT}.times"
}

coefficient=$(timeout_coefficient "${workers}")
printf 'workers=%s timeout-coefficient=%s GOMEMLIMIT=%s GOFLAGS=%s module=%s\n' \
  "${workers}" "${coefficient}" "${GOMEMLIMIT}" "${GOFLAGS}" "$(pwd)"
unleash_mutants
printf '%s\n' "${mode}" >"${OUT}.mode"
