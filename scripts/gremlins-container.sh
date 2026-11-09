#!/usr/bin/env bash
# Run scripts/gremlins-run.sh for one Go module of the clone in ./target, in a
# memory-capped golang container, for weekly-gremlins.yaml's plan and run jobs.
#
# Usage (from the ci checkout root): gremlins-container.sh MODULE_DIR
# Env: GREMLINS_VERSION, PROBE_TIMEOUT, JOB_TIMEOUT_MINUTES, and either OUT (the
# result path inside the container) or PLAN_DIR (a host directory for the plan
# files) and PLAN_DEADLINE (epoch seconds); a shard's GREMLINS_MODE and
# GREMLINS_WORKERS (the plan's, which replaces the count sized here).
set -uo pipefail

m="${1:?module dir}"
: "${GREMLINS_VERSION:?}" "${PROBE_TIMEOUT:?}" "${JOB_TIMEOUT_MINUTES:?}"

# Some mutant will allocate without bound (one subflux mutant reached ~14 GiB),
# and GOMEMLIMIT is only a soft heap target. On the bare runner the VM's
# OOM-killer may reap the runner agent and kill the job; inside a container
# capped at 80% of MemAvailable the cgroup's OOM-killer reaps the runaway test
# and gremlins records it. cap_mb is both that ceiling and the budget the
# worker count derives from, reserving heap + toolchain per worker.
avail_kb=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
cap_mb=$((avail_kb * 80 / 100 / 1024))
nproc_n=$(nproc)
toolchain_mb=1024 # go compile+link RSS per worker (not bounded by GOMEMLIMIT); measured peak 0.98 GiB at GOGC=400
heap_min_mb=1024  # GOMEMLIMIT floor over the 0.61 GB measured test peak; lower GC-thrashes into false timeouts
per_worker_mb=$((toolchain_mb + heap_min_mb))
max_by_mem=$((cap_mb / per_worker_mb))
[ "$max_by_mem" -lt 1 ] && max_by_mem=1
workers=$((nproc_n < max_by_mem ? nproc_n : max_by_mem))
if [ -n "${GREMLINS_WORKERS:-}" ]; then
  case "${GREMLINS_WORKERS}" in
    0* | *[!0-9]*)
      echo "::error::GREMLINS_WORKERS=${GREMLINS_WORKERS} is not a positive integer"
      exit 1
      ;;
  esac
  if [ "${GREMLINS_WORKERS}" -gt "${workers}" ]; then
    echo "::warning::the shard plan's ${GREMLINS_WORKERS} workers exceed the ${workers} this runner sizes"
  fi
  workers="${GREMLINS_WORKERS}"
fi
gomem_mb=$(((cap_mb - workers * toolchain_mb) / workers))
[ "$gomem_mb" -lt "$heap_min_mb" ] && gomem_mb=$heap_min_mb
# Second candidate: the heap budget if the probe drops the run to one
# worker. Same formula, same cap — both computed here so ALL of the
# memory arithmetic stays in one place and the container only picks.
gomem_solo_mb=$((cap_mb - toolchain_mb))
[ "$gomem_solo_mb" -lt "$heap_min_mb" ] && gomem_solo_mb=$heap_min_mb
echo "workers<=${workers} (nproc=${nproc_n}, mem-cap=${max_by_mem})"
echo "GOMEMLIMIT=${gomem_mb}MiB (${gomem_solo_mb}MiB solo) container=${cap_mb}MiB (MemAvailable=${avail_kb}KiB)"

# A Go suite may shell out to node (wiregen runs its generated decoders),
# and go-ci.yaml gets it from the runner image; the golang image has none.
node_mount=()
if node_bin=$(command -v node); then
  node_mount=(-v "$(readlink -f "$node_bin")":/usr/local/bin/node:ro)
fi

role_env=()
if [ -n "${PLAN_DIR:-}" ]; then
  mkdir -p "${PLAN_DIR}"
  role_env=(-v "${PLAN_DIR}":/plan -e PLAN_DIR=/plan -e PLAN_DEADLINE="${PLAN_DEADLINE:?}")
else
  role_env=(-e OUT="${OUT:?}")
fi
if [ -n "${GREMLINS_MODE:-}" ]; then
  role_env+=(-e GREMLINS_MODE="${GREMLINS_MODE}")
fi
if [ -n "${GREMLINS_WORKERS:-}" ]; then
  role_env+=(-e GREMLINS_WORKERS="${GREMLINS_WORKERS}")
fi

# Go toolchain image from THAT module's go.mod (a `toolchain` line
# wins over the `go` line); fall back to the minor tag if the patch
# tag is missing, then to a sane default. Per module, because a
# nested module may require a newer Go than the root — ci.yaml pins
# each nested module to its own go.mod for the same reason.
gover=$(grep -oP '^(toolchain go|go )\K[0-9]+\.[0-9]+(\.[0-9]+)?' "target/${m}/go.mod" | tail -1)
image="golang:${gover:-1.26}"
docker pull "$image" 2>/dev/null || {
  image="golang:${gover%.*}"
  docker pull "$image"
}
echo "image=${image}"

docker run --rm \
  --memory="${cap_mb}m" --memory-swap="${cap_mb}m" \
  -v "${PWD}/target":/work \
  -v "${PWD}/scripts/gremlins-run.sh":/gremlins-run.sh:ro \
  -v "${PWD}/scripts/gremlins-pkgname.patch":/gremlins-pkgname.patch:ro \
  -v "${PWD}/scripts/gremlins-pkgtimeout.patch":/gremlins-pkgtimeout.patch:ro \
  "${node_mount[@]}" \
  -w "/work/${m}" \
  -e GREMLINS_VERSION="${GREMLINS_VERSION}" \
  -e GREMLINS_PATCHES="/gremlins-pkgname.patch /gremlins-pkgtimeout.patch" \
  -e WORKERS_MAX="${workers}" \
  -e GOMEM_MAX_MB="${gomem_mb}" \
  -e GOMEM_SOLO_MB="${gomem_solo_mb}" \
  -e PROBE_TIMEOUT="${PROBE_TIMEOUT}" \
  -e JOB_TIMEOUT_MINUTES="${JOB_TIMEOUT_MINUTES}" \
  "${role_env[@]}" \
  "${image}" bash /gremlins-run.sh
