#!/usr/bin/env bash
# One module's gremlins run, executed INSIDE the memory-capped container that
# .github/workflows/weekly-gremlins.yaml starts (see that workflow for why the
# container exists at all: a mutant can turn a bounded loop into an unbounded
# allocation, and the container cgroup is what keeps the OOM-killer away from
# the runner agent).
#
# It lives in a file rather than inline in the workflow so shellcheck lints it —
# the concurrency probe below has real control flow (background jobs, exit-code
# triage), and an error in it would first surface at 22:00 on a Sunday, across
# every Go repo in the fleet.
#
# Working directory: the module root (the workflow sets -w). Inputs, all env:
#   GREMLINS_VERSION  release tag to download, e.g. v0.6.0
#   WORKERS_MAX       worker ceiling derived from the container memory cap
#   GOMEM_MAX_MB      GOMEMLIMIT for WORKERS_MAX workers
#   GOMEM_SOLO_MB     GOMEMLIMIT when the probe forces one worker
#   PROBE_TIMEOUT     seconds per probe phase
#   OUT               absolute path for gremlins' JSON result
set -euo pipefail

: "${GREMLINS_VERSION:?}" "${WORKERS_MAX:?}" "${GOMEM_MAX_MB:?}" "${GOMEM_SOLO_MB:?}"
: "${PROBE_TIMEOUT:?}" "${OUT:?}"

# File first, then extract: --retry does not cover a mid-transfer receive
# failure, and --retry-all-errors cannot retry into a pipe.
curl -fsSL --connect-timeout 10 --max-time 60 \
  --retry 7 --retry-max-time 150 --retry-all-errors \
  -o /tmp/gremlins.tgz \
  "https://github.com/go-gremlins/gremlins/releases/download/${GREMLINS_VERSION}/gremlins_${GREMLINS_VERSION#v}_linux_amd64.tar.gz"
tar -xzf /tmp/gremlins.tgz -C /usr/local/bin gremlins

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

  if (cd "${probe_a}" && timeout "${PROBE_TIMEOUT}" go test -count=1 ./...) >/tmp/probe-solo.log 2>&1; then
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
  rm -rf "${probe_a}" "${probe_b}"
fi

echo "workers=${workers} GOMEMLIMIT=${GOMEMLIMIT} module=$(pwd)"
gremlins unleash --workers "${workers}" --output "${OUT}" .
