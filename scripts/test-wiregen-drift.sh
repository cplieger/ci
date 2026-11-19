#!/usr/bin/env bash
# Probe go-ci.yaml's "Wiregen drift" step. The body is EXTRACTED from the workflow
# at runtime and run under GitHub's bash flags in fixture repositories, so what is
# tested is what ships. GO_CI_WORKFLOW points it at another copy. Network-free:
# GOPROXY=off, and the wiregen import paths resolve to local stub modules.
# Run: bash scripts/test-wiregen-drift.sh   (exit 0 = pass)
set -uo pipefail

ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
WORKFLOW=${GO_CI_WORKFLOW:-$ROOT/.github/workflows/go-ci.yaml}

failures=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() {
  printf '  FAIL  %s: %s\n' "$1" "$2"
  failures=$((failures + 1))
}
expect_rc() { # expect_rc <label> <want>
  if [ "$rc" -eq "$2" ]; then pass "$1"; else fail "$1" "rc=$rc, want $2
--- output ---
$out"; fi
}
expect_has() { # expect_has <label> <needle>
  case "$out" in
    *"$2"*) pass "$1" ;;
    *) fail "$1" "output lacks '$2'
--- output ---
$out" ;;
  esac
}
expect_runs() { # expect_runs <label> <expected "wiregen: go -C" lines, sorted>
  local got
  got=$(printf '%s\n' "$out" | grep '^wiregen: go -C ' | sort)
  if [ "$got" = "$2" ]; then pass "$1"; else fail "$1" "ran:
$got
want:
$2"; fi
}
expect_lacks() { # expect_lacks <label> <needle>
  case "$out" in
    *"$2"*) fail "$1" "output has '$2'
--- output ---
$out" ;;
    *) pass "$1" ;;
  esac
}
expect_absent() { # expect_absent <label> <path>
  if [ ! -e "$2" ]; then pass "$1"; else fail "$1" "unexpected $2"; fi
}
expect_marker() { # expect_marker <label>
  if grep -qx 'wiregen drift' "$WORK/failures" 2>/dev/null; then pass "$1"; else fail "$1" "no 'wiregen drift' marker"; fi
}

if ! command -v go >/dev/null 2>&1; then
  printf 'go is not installed; cannot build the fixture modules\n'
  exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export GOPROXY=off GOFLAGS='' GOTOOLCHAIN=local GOWORK=off GIT_CONFIG_GLOBAL=/dev/null
export GOMODCACHE="$WORK/modcache" GOCACHE="$WORK/gocache"
export GIT_AUTHOR_NAME=probe GIT_AUTHOR_EMAIL=probe@example.com
export GIT_COMMITTER_NAME=probe GIT_COMMITTER_EMAIL=probe@example.com
goversion=$(go env GOVERSION)
goversion=${goversion#go}
goversion=${goversion%%[a-z]*}

# The step appends its failure marker to /tmp/_ci_failures, which the probe's own
# CI job reads: the extracted copy writes to the scratch dir instead.
extract() {
  python3 - "$WORKFLOW" "$WORK/step.sh" "$WORK/failures" <<'PY'
import sys, yaml

wf, out, marker = sys.argv[1:]
steps = yaml.safe_load(open(wf))["jobs"]["validate"]["steps"]
body = next(s for s in steps if s.get("name") == "Wiregen drift")["run"]
if body.count("/tmp/_ci_failures") != 1:
    sys.exit("the step body no longer writes /tmp/_ci_failures exactly once")
open(out, "w").write(body.replace("/tmp/_ci_failures", marker))
PY
}
if ! extract; then
  printf 'could not extract the Wiregen drift step from %s\n' "$WORKFLOW"
  exit 1
fi

stub() { # stub <module path>: a wiregen stand-in whose Write writes one file
  local dir="$WORK/stubs/$1"
  mkdir -p "$dir"
  printf 'module %s\n\ngo %s\n' "$1" "$goversion" >"$dir/go.mod"
  cat >"$dir/wiregen.go" <<'EOF'
package wiregen

import "os"

func Write(path, body string) error { return os.WriteFile(path, []byte(body), 0o644) }
EOF
}
stub github.com/cplieger/wiregen
stub github.com/cplieger/go/wiregen

gomod() { # gomod <dir> <module path> [require/replace block]
  mkdir -p "$1"
  printf 'module %s\n\ngo %s\n%s' "$2" "$goversion" "${3:-}" >"$1/go.mod"
}
uses_wiregen() { # uses_wiregen <import path> [extra require] [extra replace]
  printf '\nrequire (\n\t%s v0.0.0\n%s)\n\nreplace (\n\t%s => %s\n%s)\n' \
    "$1" "${2:-}" "$1" "$WORK/stubs/$1" "${3:-}"
}
generator() { # generator <dir> <wiregen import> <out path> <content expr> [extra import]
  mkdir -p "$1"
  cat >"$1/main.go" <<EOF
package main

import (
	"os"

	"$2"
	${5:-}
)

func main() {
	if err := wiregen.Write("$3", $4); err != nil {
		os.Exit(1)
	}
}
EOF
}
commit() { # commit <tree>
  git -C "$1" init -q && git -C "$1" add -A && git -C "$1" commit -qm fixture
}
run_step() { # run_step <tree>; sets $out $rc
  rm -f "$WORK/failures"
  out=$(cd "$1" && CI_TOOLS="$ROOT/scripts" bash --noprofile --norc -e "$WORK/step.sh" 2>&1)
  rc=$?
}
root_app() { # root_app <tree>: a root module whose cmd/gen writes api.gen.go
  gomod "$1" example.com/app "$(uses_wiregen github.com/cplieger/wiregen)"
  printf 'package app\n\nconst Version = "v1"\n' >"$1/app.go"
  generator "$1/cmd/gen" github.com/cplieger/wiregen api.gen.go \
    '"// generated from "+app.Version+"\n\npackage app\n"' '"example.com/app"'
  printf '// generated from v1\n\npackage app\n' >"$1/api.gen.go"
}

echo 'generator in the root module'
t="$WORK/root"
root_app "$t"
commit "$t"
run_step "$t"
expect_rc 'root: up-to-date output passes' 0
expect_runs 'root: the generator runs from the root module' 'wiregen: go -C . run ./cmd/gen'
expect_has 'root: the tracked file is counted' 'generated files up to date (1 tracked)'
sed -i 's/v1/v2/' "$t/app.go"
run_step "$t"
expect_rc 'root: stale output fails' 1
expect_has 'root: stale output is named' 'wiregen output is stale'
expect_marker 'root: stale output writes the failure marker'

# The chartdata shape: a generator module under internal/ that replaces its parent
# with ../.., plus a second nested module whose generator is below its root.
echo 'generators in nested modules'
t="$WORK/nested"
gomod "$t" example.com/chartdata
printf 'package chartdata\n\nconst Value = "v1"\n' >"$t/chartdata.go"
gomod "$t/internal/gen" chartdata-gen "$(uses_wiregen github.com/cplieger/go/wiregen \
  $'\texample.com/chartdata v0.0.0\n' $'\texample.com/chartdata => ../..\n')"
generator "$t/internal/gen" github.com/cplieger/go/wiregen ../../chart.gen.go \
  '"// generated from "+chartdata.Value+"\n\npackage chartdata\n"' '"example.com/chartdata"'
printf '// generated from v1\n\npackage chartdata\n' >"$t/chart.gen.go"
gomod "$t/tools" example.com/tools "$(uses_wiregen github.com/cplieger/wiregen)"
generator "$t/tools/cmd/schema" github.com/cplieger/wiregen schema.gen.json '"{}\n"'
printf '{}\n' >"$t/tools/schema.gen.json"
commit "$t"
run_step "$t"
expect_rc 'nested: up-to-date output passes' 0
expect_runs 'nested: each generator runs from its own module root' \
  "$(printf '%s\n' 'wiregen: go -C ./internal/gen run .' 'wiregen: go -C ./tools run ./cmd/schema' | sort)"
expect_has 'nested: both tracked files are counted' 'generated files up to date (2 tracked)'
# go-nested runs go-ci with the nested module as its working directory.
run_step "$t/internal/gen"
expect_rc 'nested job: passes' 0
expect_runs 'nested job: runs its own generator' 'wiregen: go -C . run .'
expect_lacks 'nested job: an empty generated-file pathspec raises no git error' 'fatal:'
sed -i 's/v1/v2/' "$t/chartdata.go"
run_step "$t"
expect_rc 'nested: stale output fails' 1
expect_has 'nested: the stale file is listed' 'chart.gen.go'
expect_marker 'nested: stale output writes the failure marker'
run_step "$t/internal/gen"
expect_rc 'nested job: the parent module owns the stale parent file' 0

echo 'nested modules with no generator'
t="$WORK/nogen"
root_app "$t"
gomod "$t/tools" example.com/tools
mkdir -p "$t/tools/cmd/lint"
printf 'package main\n\nfunc main() {}\n' >"$t/tools/cmd/lint/main.go"
gomod "$t/wirelib" example.com/wirelib "$(uses_wiregen github.com/cplieger/wiregen)"
printf 'package wirelib\n\nimport _ "github.com/cplieger/wiregen"\n' >"$t/wirelib/wirelib.go"
commit "$t"
run_step "$t"
expect_rc 'no-generator: passes' 0
expect_runs 'no-generator: only the root generator runs' 'wiregen: go -C . run ./cmd/gen'

# grelot's shape: the root is `package main` (the server) and imports wiregen only
# from a test file, to check its import graph; the real generator is cmd/gen.
echo 'a package main root whose only wiregen import is a test file'
t="$WORK/testimport"
gomod "$t" example.com/app "$(uses_wiregen github.com/cplieger/wiregen)"
printf 'package main\n\nimport "os"\n\nfunc main() { _ = os.WriteFile("RAN-server", nil, 0o644); os.Exit(2) }\n' \
  >"$t/main.go"
printf 'package main\n\nimport (\n\t"testing"\n\n\t_ "github.com/cplieger/wiregen"\n)\n\nfunc TestImports(t *testing.T) {}\n' \
  >"$t/architecture_test.go"
mkdir -p "$t/api"
printf 'package api\n\nconst Version = "v1"\n' >"$t/api/api.go"
generator "$t/cmd/gen" github.com/cplieger/wiregen api/api.gen.go \
  '"// generated from "+api.Version+"\n\npackage api\n"' '"example.com/app/api"'
printf '// generated from v1\n\npackage api\n' >"$t/api/api.gen.go"
commit "$t"
run_step "$t"
expect_rc 'test import: passes' 0
expect_runs 'test import: only the real generator runs' 'wiregen: go -C . run ./cmd/gen'
expect_absent 'test import: the root package never runs' "$t/RAN-server"

echo 'generators in fixture directories'
t="$WORK/fixtures"
root_app "$t"
generator "$t/testdata/gen" github.com/cplieger/wiregen RAN-testdata '""'
gomod "$t/fixtures/mod" example.com/fixture "$(uses_wiregen github.com/cplieger/wiregen)"
generator "$t/fixtures/mod" github.com/cplieger/wiregen ../../RAN-fixtures '""'
generator "$t/node_modules/pkg" github.com/cplieger/wiregen RAN-node-modules '""'
printf 'stale fixture output\n' >"$t/testdata/old.gen.go"
commit "$t"
run_step "$t"
expect_rc 'fixtures: passes' 0
expect_runs 'fixtures: only the root generator runs' 'wiregen: go -C . run ./cmd/gen'
expect_absent 'fixtures: a testdata generator never runs' "$t/RAN-testdata"
expect_absent 'fixtures: a fixtures module generator never runs' "$t/RAN-fixtures"
expect_absent 'fixtures: a node_modules generator never runs' "$t/RAN-node-modules"
expect_has 'fixtures: fixture output is not counted' 'generated files up to date (1 tracked)'
run_step "$t/fixtures/mod"
expect_rc 'fixture job: passes' 0
expect_has 'fixture job: skips' 'this module is a fixture; skipping'
expect_runs 'fixture job: runs no generator' ''
expect_absent 'fixture job: its generator never runs' "$t/RAN-fixtures"

echo 'tracked generated files and no generator'
t="$WORK/orphan"
gomod "$t" example.com/app
printf 'package app\n' >"$t/x.gen.go"
commit "$t"
run_step "$t"
expect_rc 'orphan: fails' 1
expect_has 'orphan: says no generator was discovered' 'no generator command was discovered'
expect_has 'orphan: lists the file' 'tracked generated file: x.gen.go'
expect_marker 'orphan: writes the failure marker'

echo 'no generator and no generated files'
t="$WORK/empty"
gomod "$t" example.com/app
printf 'package app\n' >"$t/app.go"
commit "$t"
run_step "$t"
expect_rc 'empty: passes' 0
expect_has 'empty: skips' 'skipping'

echo 'a generated file never committed'
t="$WORK/uncommitted"
root_app "$t"
rm "$t/api.gen.go"
commit "$t"
run_step "$t"
expect_rc 'uncommitted: a generated file never committed fails' 1
expect_has 'uncommitted: the new file is listed' 'api.gen.go'
expect_marker 'uncommitted: writes the failure marker'

echo 'a generator module whose go directive is ahead of the toolchain'
t="$WORK/newgo"
root_app "$t"
gomod "$t/gen" example.com/gen "$(uses_wiregen github.com/cplieger/wiregen)"
sed -i 's/^go .*/go 1.99/' "$t/gen/go.mod"
generator "$t/gen" github.com/cplieger/wiregen gen.gen.json '"{}\n"'
commit "$t"
run_step "$t"
expect_rc 'toolchain: a newer go directive is fetched, not refused' 1
expect_has 'toolchain: the newer toolchain is fetched' 'downloading go1.99'
expect_lacks 'toolchain: no local-toolchain refusal' 'GOTOOLCHAIN=local'

echo 'an index git cannot write'
t="$WORK/locked"
root_app "$t"
commit "$t"
sed -i 's/v1/v2/' "$t/app.go"
: >"$t/.git/index.lock"
run_step "$t"
expect_rc 'locked: a failed git add fails the step' 1
expect_marker 'locked: writes the failure marker'

echo 'a generator that fails'
t="$WORK/broken"
root_app "$t"
printf 'package main\n\nimport (\n\t"os"\n\n\t_ "github.com/cplieger/wiregen"\n)\n\nfunc main() { os.Exit(3) }\n' \
  >"$t/cmd/gen/main.go"
commit "$t"
run_step "$t"
expect_rc 'broken: fails' 1
expect_has 'broken: names the generator and its module' 'the generator ./cmd/gen failed in the module at .'
expect_marker 'broken: writes the failure marker'

if [ "$failures" -eq 0 ]; then
  printf '\nall wiregen drift checks passed\n'
  exit 0
fi
printf '\n%s failure(s)\n' "$failures"
exit 1
