#!/usr/bin/env bash
# playwright-deps.sh <engine...>: installs the engines' system libraries with the
# pinned `playwright install-deps`, run from the package directory that pins it.
# A self-hosted runner may grant no root for apt, so there it installs nothing
# and its image must carry the libraries. PLAYWRIGHT_DEPS_CACHE, when set, is a
# directory of .deb files that seeds apt and is refreshed after a success; with
# PLAYWRIGHT_DEPS_CACHE_KEY, a cache key prefix, a non-empty refresh then writes
# `key=<prefix><sha256 of the sorted .deb names>` to $GITHUB_OUTPUT. Exits 1 with
# an ::error:: once every attempt failed; apt's own error is in the log above it.
set -euo pipefail

[ "$#" -gt 0 ] || {
  echo "::error::playwright-deps.sh: name at least one engine"
  exit 2
}
if [ "${RUNNER_ENVIRONMENT:-github-hosted}" != github-hosted ]; then
  echo "self-hosted runner: skipping playwright install-deps $*"
  exit 0
fi

# A healthy install takes under a minute. A bound per attempt rather than one
# for the whole install lets a stalled or crawling mirror connection be retried;
# apt keeps every finished .deb, so a retry fetches only the remainder.
ATTEMPTS=3
ATTEMPT_SECONDS=${PLAYWRIGHT_DEPS_ATTEMPT_SECONDS:-180}
CACHE=${PLAYWRIGHT_DEPS_CACHE:-}
KEY=${PLAYWRIGHT_DEPS_CACHE_KEY:-}
ARCHIVES=${PLAYWRIGHT_DEPS_ARCHIVES:-/var/cache/apt/archives}
# playwright runs apt-get through `sudo sh -c`, which drops the environment, so
# the options reach apt only as a drop-in. apt's defaults are a 30 s timeout and
# 3 retries (https://salsa.debian.org/apt-team/apt, methods/basehttp.cc and
# apt-pkg/acquire-item.cc); the lock wait outlasts a killed attempt's apt.
CONF=${PLAYWRIGHT_DEPS_CONF:-/etc/apt/apt.conf.d/80-cplieger-ci-retries}

sudo tee "$CONF" >/dev/null <<'EOF'
Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
Acquire::Retries "5";
APT::Keep-Downloaded-Packages "true";
DPkg::Lock::Timeout "60";
EOF

# apt installs an archived .deb of the expected name and size without hashing it
# (pkgAcqArchive::QueueNext, apt-pkg/acquire-item.cc). Only the calling repository's
# own runs, which already have root through sudo, can write this cache, and a pull
# request's entries are readable only by that pull request:
# https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache
if [ -n "$CACHE" ] && [ -d "$CACHE" ]; then
  find "$CACHE" -maxdepth 1 -name '*.deb' -exec sudo cp -t "$ARCHIVES" {} + \
    || echo "::warning::could not seed apt's archive from ${CACHE}"
fi

attempt=1 delay=15
while :; do
  rc=0
  # -k: a process that ignores TERM would otherwise hold the step to its timeout.
  timeout -k 30 "$ATTEMPT_SECONDS" npx --no-install playwright install-deps "$@" || rc=$?
  [ "$rc" -ne 0 ] || break
  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    echo "::error::playwright install-deps $* failed ${ATTEMPTS} times, the last with exit ${rc} (124 or 137 means it ran past ${ATTEMPT_SECONDS}s). An Ubuntu mirror or DNS outage is the usual cause: rerun the job once it clears."
    exit 1
  fi
  echo "::warning::playwright install-deps $* attempt ${attempt}/${ATTEMPTS} failed (exit ${rc}), retrying in ${delay}s"
  sleep "${PLAYWRIGHT_DEPS_SLEEP:-$delay}"
  # A killed attempt can leave dpkg half-configured, which refuses every later install.
  sudo dpkg --configure -a || echo "::warning::dpkg --configure -a failed (exit $?)"
  attempt=$((attempt + 1)) delay=$((delay * 2))
done

[ -n "$CACHE" ] || exit 0
# autoclean drops the debs the indexes no longer list: the cache stays one install's worth.
sudo apt-get autoclean -qq || echo "::warning::apt-get autoclean failed (exit $?)"
if ! mkdir -p "$CACHE" \
  || ! find "$CACHE" -maxdepth 1 -name '*.deb' -delete \
  || ! find "$ARCHIVES" -maxdepth 1 -name '*.deb' -exec cp -t "$CACHE" {} +; then
  echo "::warning::could not copy apt's archive to ${CACHE}, so the .deb cache is not saved"
elif [ -z "$(find "$CACHE" -maxdepth 1 -name '*.deb' -print -quit)" ]; then
  echo "apt kept no .deb files, so the .deb cache is not saved"
elif [ -n "$KEY" ]; then
  # Keyed on the .deb set, so a library update is saved once, not fetched every run.
  sum=$(find "$CACHE" -maxdepth 1 -name '*.deb' -printf '%f\n' | LC_ALL=C sort | sha256sum)
  echo "key=${KEY}${sum%% *}" >>"$GITHUB_OUTPUT"
fi
