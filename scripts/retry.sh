#!/usr/bin/env bash
# Retry helpers for the release workflows; source it (it sets no shell
# options). READBACK_SLEEP, when set, replaces the length of every wait
# without changing the schedule.

# retry <command...>: a network call safe to repeat (a registry login, a
# cosign signature or attestation, which is additive, or an oras copy, which
# is idempotent by content); none of them retries its own transport. Five
# attempts, waiting 5 s doubling to 40 s. The command's arguments are echoed,
# so a secret travels on stdin or in the environment, never as an argument.
retry() {
  local attempt=1 max=5 delay=5
  until "$@"; do
    if [ "$attempt" -ge "$max" ]; then
      echo "::error::$1 failed after ${max} attempts: $*"
      return 1
    fi
    echo "::warning::$1 attempt ${attempt}/${max} failed; retrying in ${delay}s"
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

# await_registry <label> <predicate...>: polls the predicate (output
# discarded) until a just-published artifact is served; npm has lagged its
# publish by two minutes. Backs off 5 s doubling to a 60 s cap within
# REGISTRY_WAIT_SECONDS (default 600) of waiting per shell, shared by every
# call because one publish starts all propagation at once and the job must
# end inside its timeout (a caller whose later publish starts a new one
# zeroes REGISTRY_WAITED); once spent, a call tries once. Returns 1 with no
# annotation: the caller owns the verdict.
REGISTRY_WAITED=${REGISTRY_WAITED:-0}
await_registry() {
  local label=$1 attempt=1 delay=5 waited=0 budget=${REGISTRY_WAIT_SECONDS:-600} left
  shift
  until "$@" >/dev/null 2>&1; do
    left=$((budget - REGISTRY_WAITED))
    if [ "$left" -le 0 ]; then
      echo "${label}: absent after ${attempt} attempt(s) and ${waited}s of waiting" >&2
      return 1
    fi
    [ "$delay" -le "$left" ] || delay=$left
    echo "${label}: not served yet (attempt ${attempt}); retrying in ${delay}s" >&2
    sleep "${READBACK_SLEEP:-$delay}"
    REGISTRY_WAITED=$((REGISTRY_WAITED + delay))
    waited=$((waited + delay))
    attempt=$((attempt + 1))
    delay=$((delay * 2 > 60 ? 60 : delay * 2))
  done
  if [ "$attempt" -gt 1 ]; then
    echo "::notice::${label} was served on attempt ${attempt}, after ${waited}s of waiting"
  fi
}
