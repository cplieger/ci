#!/usr/bin/env bash
# One module's gremlins run, executed INSIDE the memory-capped container that
# .github/workflows/weekly-gremlins.yaml starts (see that workflow for why the
# container exists at all: a mutant can turn a bounded loop into an unbounded
# allocation, and the container cgroup is what keeps the OOM-killer away from
# the runner agent).
#
# It lives in a file rather than inline in the workflow so shellcheck lints it,
# and so the per-module loop that calls it stays readable.
#
# Working directory: the module root (the workflow sets -w). Inputs, all env:
#   GREMLINS_VERSION  release tag to download, e.g. v0.6.0
#   WORKERS           worker count, derived from the container memory cap
#   OUT               absolute path for gremlins' JSON result
set -euo pipefail

: "${GREMLINS_VERSION:?}" "${WORKERS:?}" "${OUT:?}"

# File first, then extract: --retry does not cover a mid-transfer receive
# failure, and --retry-all-errors cannot retry into a pipe.
curl -fsSL --connect-timeout 10 --max-time 60 \
  --retry 7 --retry-max-time 150 --retry-all-errors \
  -o /tmp/gremlins.tgz \
  "https://github.com/go-gremlins/gremlins/releases/download/${GREMLINS_VERSION}/gremlins_${GREMLINS_VERSION#v}_linux_amd64.tar.gz"
tar -xzf /tmp/gremlins.tgz -C /usr/local/bin gremlins

echo "workers=${WORKERS} GOMEMLIMIT=${GOMEMLIMIT:-unset} module=$(pwd)"
gremlins unleash --workers "${WORKERS}" --output "${OUT}" .
