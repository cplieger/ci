#!/usr/bin/env bash
# Probe configs/collect-licenses.sh. Nothing in this repo builds an image, so without
# this its two failure directions (a module silently skipped, a false refusal) are
# only ever measured in a consumer's release. Network-free: GOPROXY=off with local
# replace directives. Run: bash scripts/test-collect-licenses.sh   (exit 0 = pass)
set -uo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
SUBJECT="$HERE/../configs/collect-licenses.sh"

failures=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() {
  printf '  FAIL  %s: %s\n' "$1" "$2"
  failures=$((failures + 1))
}

expect_file() { # expect_file <label> <path>
  if [ -f "$2" ]; then pass "$1"; else fail "$1" "missing $2"; fi
}

expect_absent() { # expect_absent <label> <path>
  if [ ! -e "$2" ]; then pass "$1"; else fail "$1" "unexpected $2"; fi
}

if [ ! -f "$SUBJECT" ]; then
  printf 'missing subject: %s\n' "$SUBJECT"
  exit 1
fi
if ! command -v go >/dev/null 2>&1; then
  printf 'go is not installed; cannot build the fixture module\n'
  exit 1
fi

root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
export GOPROXY=off GOFLAGS=-mod=mod GOTOOLCHAIN=local GOWORK=off
export GOMODCACHE="$root/modcache" GOCACHE="$root/gocache"
goversion=$(go env GOVERSION)
goversion=${goversion#go}
goversion=${goversion%%[a-z]*}

# dep <dir> <module path> <package>: a one-package module exporting one func.
dep() {
  mkdir -p "$1"
  printf 'module %s\n\ngo %s\n' "$2" "$goversion" >"$1/go.mod"
  printf 'package %s\n\nfunc Name() string { return "%s" }\n' "$3" "$3" >"$1/$3.go"
}

dep "$root/licensed" example.com/licensed licensed
printf 'the licensed license\n' >"$root/licensed/LICENSE"
printf 'the licensed notice\n' >"$root/licensed/NOTICE"

dep "$root/copying" example.com/copying copying
printf 'the copying license\n' >"$root/copying/COPYING"
# A directory named like a license file is not a license file.
mkdir -p "$root/copying/LICENSES"

# Matching is case-insensitive: josharian/intern ships license.md (measured 2026-09-18
# in the docker-caddy tree), and a case-sensitive glob reported it as unlicensed.
dep "$root/lower" example.com/lower lower
printf 'the lowercase license\n' >"$root/lower/license.md"

dep "$root/bare" example.com/bare bare

# test-only and tool-only modules must NOT be collected: attribution covers what is
# linked into the binary, not the module graph.
dep "$root/testonly" example.com/testonly testonly
printf 'the testonly license\n' >"$root/testonly/LICENSE"

app="$root/app"
mkdir -p "$app/cmd/app" "$app/cmd/helper" "$app/internal/lib"
cat >"$app/go.mod" <<EOF
module example.com/app

go $goversion

require (
	example.com/bare v0.0.0
	example.com/copying v0.0.0
	example.com/licensed v0.0.0
	example.com/lower v0.0.0
	example.com/testonly v0.0.0
)

replace (
	example.com/bare => ../bare
	example.com/copying => ../copying
	example.com/licensed => ../licensed
	example.com/lower => ../lower
	example.com/testonly => ../testonly
)
EOF
printf 'the app license\n' >"$app/LICENSE"
printf 'app\nCopyright 2026 cplieger\nhttps://github.com/cplieger/app\n' >"$app/NOTICE"
printf '# Third-party notices\n' >"$app/THIRD_PARTY_NOTICES.md"
cat >"$app/cmd/app/main.go" <<'EOF'
package main

import (
	"example.com/app/internal/lib"
	"example.com/licensed"
)

func main() { println(lib.Name(), licensed.Name()) }
EOF
cat >"$app/cmd/helper/main.go" <<'EOF'
package main

import (
	"example.com/copying"
	"example.com/lower"
)

func main() { println(copying.Name(), lower.Name()) }
EOF
cat >"$app/internal/lib/lib.go" <<'EOF'
package lib

import "example.com/bare"

func Name() string { return bare.Name() }
EOF
cat >"$app/internal/lib/lib_test.go" <<'EOF'
package lib

import (
	"testing"

	"example.com/testonly"
)

func TestName(t *testing.T) { _ = testonly.Name() }
EOF

run() { # run <out dir> <args...>; sets $out $rc
  local dest=$1
  shift
  out=$(cd "$app" && sh "$SUBJECT" --name app --out "$dest" "$@" 2>&1)
  rc=$?
}

# --- fail closed: the bare module is linked, so the whole run refuses -------------
run "$root/out-fail"
if [ "$rc" -eq 1 ]; then pass 'a linked module with no license file exits 1'; else fail 'a linked module with no license file exits 1' "rc=$rc: $out"; fi
case "$out" in
  *'example.com/bare'*) pass 'the refusal names the module' ;;
  *) fail 'the refusal names the module' "$out" ;;
esac
case "$out" in
  *'licenses/example.com/bare/'*) pass 'the refusal names the override path' ;;
  *) fail 'the refusal names the override path' "$out" ;;
esac

# --- the licenses/<module path>/ override unblocks it -----------------------------
mkdir -p "$app/licenses/example.com/bare"
printf 'the bare license, supplied by the repo\n' >"$app/licenses/example.com/bare/LICENSE.txt"
run "$root/out"
if [ "$rc" -eq 0 ]; then pass 'with the override in place the run exits 0'; else fail 'with the override in place the run exits 0' "rc=$rc: $out"; fi
if [ "$(printf '%s\n' "$out" | wc -l)" -eq 1 ]; then pass 'success prints exactly one line'; else fail 'success prints exactly one line' "$out"; fi
case "$out" in
  'collect-licenses: 5 modules, 8 files under '*) pass 'the summary counts modules and files' ;;
  *) fail 'the summary counts modules and files' "$out" ;;
esac

o="$root/out"
expect_file 'the image root carries LICENSE' "$o/app/LICENSE"
expect_file 'the image root carries NOTICE' "$o/app/NOTICE"
expect_file 'the image root carries THIRD_PARTY_NOTICES.md' "$o/app/THIRD_PARTY_NOTICES.md"
expect_absent 'the main module is not duplicated under its module path' "$o/example.com/app"
expect_file 'a dependency LICENSE lands under the module path' "$o/example.com/licensed/LICENSE"
expect_file 'a dependency NOTICE travels with its LICENSE' "$o/example.com/licensed/NOTICE"
expect_file 'a COPYING file is a license file' "$o/example.com/copying/COPYING"
expect_absent 'a LICENSES directory is not copied as a file' "$o/example.com/copying/LICENSES"
expect_file 'a lowercase license.md is collected' "$o/example.com/lower/license.md"
expect_absent 'a Go source file named like a license is not collected' "$o/example.com/licensed/licensed.go"
expect_file 'the override file lands under the module path' "$o/example.com/bare/LICENSE.txt"
expect_absent 'a test-only module is not collected' "$o/example.com/testonly"
if cmp -s "$root/licensed/LICENSE" "$o/example.com/licensed/LICENSE"; then
  pass 'a copied license is byte-identical to the source'
else
  fail 'a copied license is byte-identical to the source' 'bytes differ'
fi

# --- package patterns scope the roots --------------------------------------------
run "$root/out-scoped" ./cmd/helper
if [ "$rc" -eq 0 ]; then pass 'a package pattern narrows the root set'; else fail 'a package pattern narrows the root set' "rc=$rc: $out"; fi
expect_file 'a scoped run collects the scoped root'\''s modules' "$root/out-scoped/example.com/copying/COPYING"
expect_absent 'a scoped run leaves out modules only other roots link' "$root/out-scoped/example.com/licensed"

run "$root/out-none" ./internal/...
if [ "$rc" -eq 1 ]; then pass 'a pattern matching no main package exits 1'; else fail 'a pattern matching no main package exits 1' "rc=$rc: $out"; fi

# --- the main module's own LICENSE is required, NOTICE alone does not pass ---------
mv "$app/LICENSE" "$root/"
run "$root/out-nomain"
mv "$root/LICENSE" "$app/"
if [ "$rc" -eq 1 ]; then pass 'a main module with NOTICE but no LICENSE exits 1'; else fail 'a main module with NOTICE but no LICENSE exits 1' "rc=$rc: $out"; fi
case "$out" in
  *'example.com/app has no LICENSE'*) pass 'the refusal names the main module and LICENSE' ;;
  *) fail 'the refusal names the main module and LICENSE' "$out" ;;
esac
expect_absent 'a refused run does not leave a partial image root' "$root/out-nomain/app"

# --- --src walks another module directory, --out stays relative to the caller ---------
(cd "$root" && sh "$SUBJECT" --src "$app" --name app --out out-src ./... >/dev/null 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then pass '--src runs against the named module directory'; else fail '--src runs against the named module directory' "rc=$rc"; fi
expect_file '--src output lands under the caller-relative --out' "$root/out-src/app/LICENSE"

# --- usage -------------------------------------------------------------------------
out=$(cd "$app" && sh "$SUBJECT" --out "$root/out-usage" 2>&1)
rc=$?
if [ "$rc" -eq 2 ]; then pass 'a missing --name exits 2'; else fail 'a missing --name exits 2' "rc=$rc: $out"; fi
out=$(cd "$app" && sh "$SUBJECT" --name 'nested/app' --out "$root/out-usage" 2>&1)
rc=$?
if [ "$rc" -eq 2 ]; then pass 'a --name with a slash exits 2'; else fail 'a --name with a slash exits 2' "rc=$rc: $out"; fi
expect_absent 'a refused --name writes nothing' "$root/out-usage"
out=$(cd "$app" && sh "$SUBJECT" --name app --bogus 2>&1)
rc=$?
if [ "$rc" -eq 2 ]; then pass 'an unknown option exits 2'; else fail 'an unknown option exits 2' "rc=$rc: $out"; fi

if [ "$failures" -eq 0 ]; then
  printf '\nall collect-licenses checks passed\n'
  exit 0
fi
printf '\n%s failure(s)\n' "$failures"
exit 1
