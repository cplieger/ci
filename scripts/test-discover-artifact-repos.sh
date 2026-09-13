#!/usr/bin/env bash
#
# Probe scripts/discover-artifact-repos.sh, which three weekly jobs depend on
# (weekly-stryker aggregate, weekly-gremlins badge and aggregate) and which none
# of them exercise until its own cron fires. Its two failure directions matter in
# opposite ways: missing a repo silently skips a tracker issue with the job green,
# and firing the guard wrongly reddens a healthy weekly run.
#
# Run: bash scripts/test-discover-artifact-repos.sh    (exit 0 = pass)
set -uo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
SUBJECT="$HERE/discover-artifact-repos.sh"

failures=0

# Run the subject and compare stdout (comma-joined) and exit status.
expect() {
  local label="$1" want_out="$2" want_rc="$3" dir="$4"
  local got rc
  got=$(bash "$SUBJECT" "$dir" 2>/dev/null | tr '\n' ',')
  rc=$?
  got=${got%,}
  if [ "$got" = "$want_out" ] && [ "$rc" = "$want_rc" ]; then
    printf '  PASS  %s\n' "$label"
  else
    printf '  FAIL  %s: got %s rc=%s, want %s rc=%s\n' \
      "$label" "'$got'" "$rc" "'$want_out'" "$want_rc"
    failures=$((failures + 1))
  fi
}

meta() { # meta <dir> <repo>
  mkdir -p "$1"
  printf '{"repo":"%s","dir":"."}' "$2" >"$1/meta.json"
}

if [ ! -f "$SUBJECT" ]; then
  printf 'missing subject: %s\n' "$SUBJECT"
  exit 1
fi

root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT

# THE DEFECT. Exactly one artifact matched the download pattern, so
# actions/download-artifact extracted it flat and no per-artifact directory
# exists. Every caller's previous glob found nothing here, silently.
meta "$root/flat/artifacts" reactive
: >"$root/flat/artifacts/mutation.json"
expect 'a flat single artifact is discovered' 'reactive' 0 "$root/flat/artifacts"

# The N>1 layout, which is what the callers were written against.
meta "$root/nested/artifacts/stryker-vibekit-static-src" vibekit
meta "$root/nested/artifacts/stryker-reactive-root" reactive
expect 'nested artifacts are discovered and sorted' 'reactive,vibekit' 0 "$root/nested/artifacts"

# Several attempts of one repo collapse to one name, so a caller loops once.
meta "$root/dedup/artifacts/gremlins-envx-1" envx
meta "$root/dedup/artifacts/gremlins-envx-2" envx
meta "$root/dedup/artifacts/gremlins-envx-3" envx
expect 'repeated attempts of one repo yield one name' 'envx' 0 "$root/dedup/artifacts"

# Nothing downloaded is legitimate: every matrix entry may fail, and each one's
# own red is the signal. Silence and exit 0, so the caller skips quietly.
mkdir -p "$root/empty/artifacts"
expect 'an empty artifacts dir is quiet' '' 0 "$root/empty/artifacts"
expect 'an absent artifacts dir is quiet' '' 0 "$root/absent/artifacts"

# Files present with no repo readable is the defect, so it must be LOUD.
mkdir -p "$root/broken/artifacts/x"
printf 'not json' >"$root/broken/artifacts/x/meta.json"
expect 'an unreadable meta.json fires the guard' '' 3 "$root/broken/artifacts"

mkdir -p "$root/nometa/artifacts/x"
: >"$root/nometa/artifacts/x/mutation.json"
expect 'artifacts with no meta.json fire the guard' '' 3 "$root/nometa/artifacts"

mkdir -p "$root/norepo/artifacts/x"
printf '{"dir":"."}' >"$root/norepo/artifacts/x/meta.json"
expect 'a meta.json with no repo key fires the guard' '' 3 "$root/norepo/artifacts"

# One malformed artifact must not cost the healthy ones their tracker update, and
# it must not matter which way the paths sort. Handing jq the whole file list
# aborts on the first unparseable one, so the healthy repo survives in one order
# and vanishes in the other; both orders are pinned here for that reason.
meta "$root/mixed/artifacts/good" good
mkdir -p "$root/mixed/artifacts/bad"
printf 'not json' >"$root/mixed/artifacts/bad/meta.json"
expect 'a broken meta sorting FIRST does not lose a healthy repo' 'good' 0 "$root/mixed/artifacts"

meta "$root/mixed2/artifacts/aaa-good" good
mkdir -p "$root/mixed2/artifacts/zzz-bad"
printf 'not json' >"$root/mixed2/artifacts/zzz-bad/meta.json"
expect 'a broken meta sorting LAST does not lose a healthy repo' 'good' 0 "$root/mixed2/artifacts"

# Usage error is its own status, distinct from both success and the guard.
got_rc=0
bash "$SUBJECT" >/dev/null 2>&1 || got_rc=$?
if [ "$got_rc" = 2 ]; then
  printf '  PASS  a missing argument exits 2\n'
else
  printf '  FAIL  a missing argument exits 2: got rc=%s\n' "$got_rc"
  failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
  printf '\nall artifact-discovery checks passed\n'
  exit 0
fi
printf '\n%s failure(s)\n' "$failures"
exit 1
