#!/usr/bin/env bash
# Retry helpers for the release workflows; source it (it sets no shell
# options). READBACK_SLEEP, when set, replaces the length of every wait
# without changing the schedule.
# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

# retry <command...>: a network call safe to repeat (a registry login, a
# cosign signature or attestation, which is additive, or an oras copy, which
# is idempotent by content); none of them retries its own transport. Five
# attempts, waiting 5 s doubling to 40 s. The command's arguments are echoed,
# so a secret travels on stdin or in the environment, never as an argument.
retry() {
  local attempt=1 max=5 delay=5
  until "$@"; do
    if [ "$attempt" -ge "$max" ]; then
      gha_annotate error '%s failed after %s attempts: %s' "$1" "${max}" "$*"
      return 1
    fi
    gha_annotate warning '%s attempt %s/%s failed, so it is retried in %ss' "$1" "${attempt}" "${max}" "${delay}"
    sleep "${READBACK_SLEEP:-$delay}"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}

# registry_login <registry> <username>: `docker login` with REGISTRY_PASSWORD
# on stdin. A single reset connection to the registry's token service fails a
# login outright, so it goes through retry.
registry_login() {
  retry login_once "$1" "$2"
}
login_once() { # <registry> <username>
  printf '%s' "${REGISTRY_PASSWORD:?}" | docker login --username "$2" --password-stdin "$1"
}

# npm serves a new version only once its malware scan clears it, typically in
# about 5 min and 15 min or more at peak, with no guaranteed bound
# (https://github.blog/changelog/2026-07-28-npm-publish-time-malware-scanning-and-dual-use-metadata/).
# 20 min is a third past that stated peak. An npm wait passes this as its
# REGISTRY_WAIT_SECONDS; verify-publish, renumber-npm and repair size their
# timeout-minutes to it.
NPM_SCAN_SECONDS=${NPM_SCAN_SECONDS:-1200}

# await_registry <label> <predicate...>: polls the predicate (output
# discarded) until a just-published artifact is served, backing off 5 s
# doubling to a 60 s cap until REGISTRY_WAITED, the waiting of every call in
# this shell, reaches the call's REGISTRY_WAIT_SECONDS (default 600); once
# spent, a call tries once. Callers may share that clock, so a job waits its
# longest bound, not their sum, only while everything they check was published
# before the first of them; a caller zeroes it once something it checks is
# published later. Returns 1 with no annotation: the caller owns the verdict.
REGISTRY_WAITED=${REGISTRY_WAITED:-0}
await_registry() {
  local label=$1 attempt=1 delay=5 waited=0 budget=${REGISTRY_WAIT_SECONDS:-600} left
  shift
  until "$@" >/dev/null 2>&1; do
    left=$((budget - REGISTRY_WAITED))
    if [ "$left" -le 0 ]; then
      gha_say '%s: absent after %s attempt(s) and %ss of waiting' "${label}" "${attempt}" "${waited}" >&2
      return 1
    fi
    [ "$delay" -le "$left" ] || delay=$left
    gha_say '%s: not served yet (attempt %s); retrying in %ss' "${label}" "${attempt}" "${delay}" >&2
    sleep "${READBACK_SLEEP:-$delay}"
    REGISTRY_WAITED=$((REGISTRY_WAITED + delay))
    waited=$((waited + delay))
    attempt=$((attempt + 1))
    delay=$((delay * 2 > 60 ? 60 : delay * 2))
  done
  if [ "$attempt" -gt 1 ]; then
    gha_annotate notice '%s was served on attempt %s, after %ss of waiting' "${label}" "${attempt}" "${waited}"
  fi
}
