#!/usr/bin/env bash
# playwright-deps.sh <engine...>: installs the engines' system libraries with the
# pinned `playwright install-deps`, run from the package directory that pins it.
# A self-hosted runner may grant no root for apt, so it installs nothing there.
# PLAYWRIGHT_DEPS_CACHE, when set, is a directory of .deb files that seeds apt and
# is refreshed after a success; PLAYWRIGHT_DEPS_CACHE_KEY, a key prefix, then gets
# `key=<prefix><sha256 of the sorted .deb names>` in $GITHUB_OUTPUT. Exits 1 with
# an ::error:: when apt ignores the drop-in's values or every attempt failed.
set -euo pipefail

# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

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
KILL_SECONDS=30
FIRST_DELAY=15
CACHE=${PLAYWRIGHT_DEPS_CACHE:-}
KEY=${PLAYWRIGHT_DEPS_CACHE_KEY:-}
ARCHIVES=${PLAYWRIGHT_DEPS_ARCHIVES:-/var/cache/apt/archives}
# playwright runs apt-get through `sudo sh -c`, which drops the environment, so the
# options reach apt only as a drop-in; the lock wait outlasts a killed attempt's apt.
# apt reads apt.conf.d in C-locale order and the last value wins, so the name sorts
# after the image's zz-retries and the check below fails if a file still overrides it:
# https://github.com/actions/runner-images/blob/main/images/ubuntu/scripts/build/configure-apt.sh
CONF=${PLAYWRIGHT_DEPS_CONF:-/etc/apt/apt.conf.d/zzzz-cplieger-ci-retries}
OPTIONS='Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
Acquire::Retries "5";
APT::Keep-Downloaded-Packages "true";
DPkg::Lock::Timeout "60";'

printf '%s\n' "$OPTIONS" | sudo tee "$CONF" >/dev/null
overridden=0
while read -r name value; do
  want=${value#\"} want=${want%\";}
  got=$(apt-config dump --no-empty --format '%v' "$name")
  echo "apt uses ${name} \"${got}\""
  if [ "$got" != "$want" ]; then
    gha_annotate error 'apt uses %s "%s", not "%s" from %s: a file that sorts after it in its directory, or apt.conf, sets it' "${name}" "${got}" "${want}" "${CONF}"
    overridden=1
  fi
done <<<"$OPTIONS"
[ "$overridden" -eq 0 ] || exit 1

# apt installs an archived .deb of the expected name and size without hashing it
# (pkgAcqArchive::QueueNext, apt-pkg/acquire-item.cc). Only the calling repository's
# own runs, which already have root through sudo, can write this cache, and a pull
# request's entries are readable only by that pull request:
# https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache
if [ -n "$CACHE" ] && [ -d "$CACHE" ]; then
  find "$CACHE" -maxdepth 1 -name '*.deb' -exec sudo cp -t "$ARCHIVES" {} + \
    || gha_annotate warning "could not seed apt's archive from %s" "${CACHE}"
fi

attempt=1 delay=$FIRST_DELAY
while :; do
  rc=0
  # -k: a process that ignores TERM would otherwise hold the step to its timeout.
  timeout -k "$KILL_SECONDS" "$ATTEMPT_SECONDS" npx --no-install playwright install-deps "$@" || rc=$?
  [ "$rc" -ne 0 ] || break
  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    gha_annotate error 'playwright install-deps %s failed %s times, the last with exit %s (124 or 137 means it ran past %ss). An Ubuntu mirror or DNS outage is the usual cause: rerun the job once it clears.' "$*" "${ATTEMPTS}" "${rc}" "${ATTEMPT_SECONDS}"
    exit 1
  fi
  gha_annotate warning 'playwright install-deps %s attempt %s/%s failed (exit %s), retrying in %ss' "$*" "${attempt}" "${ATTEMPTS}" "${rc}" "${delay}"
  sleep "${PLAYWRIGHT_DEPS_SLEEP:-$delay}"
  # A killed attempt can leave dpkg half-configured, which refuses every later install.
  sudo dpkg --configure -a || gha_annotate warning 'dpkg --configure -a failed (exit %s)' "$?"
  attempt=$((attempt + 1)) delay=$((delay * 2))
done

[ -n "$CACHE" ] || exit 0
# autoclean drops the debs the indexes no longer list: the cache stays one install's worth.
sudo apt-get autoclean -qq || gha_annotate warning 'apt-get autoclean failed (exit %s)' "$?"
if ! mkdir -p "$CACHE" \
  || ! find "$CACHE" -maxdepth 1 -name '*.deb' -delete \
  || ! find "$ARCHIVES" -maxdepth 1 -name '*.deb' -exec cp -t "$CACHE" {} +; then
  gha_annotate warning "could not copy apt's archive to %s, so the .deb cache is not saved" "${CACHE}"
elif [ -z "$(find "$CACHE" -maxdepth 1 -name '*.deb' -print -quit)" ]; then
  echo "apt kept no .deb files, so the .deb cache is not saved"
elif [ -n "$KEY" ]; then
  # Keyed on the .deb set, so a library update is saved once, not fetched every run.
  sum=$(find "$CACHE" -maxdepth 1 -name '*.deb' -printf '%f\n' | LC_ALL=C sort | sha256sum)
  echo "key=${KEY}${sum%% *}" >>"$GITHUB_OUTPUT"
fi
