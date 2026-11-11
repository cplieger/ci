#!/usr/bin/env bash
# Clone cplieger/REPO into ./target, find the Go modules to measure and write
# each one's .gremlins.yaml, from the ci checkout root. weekly-gremlins.yaml's
# plan and run jobs both call it, so a shard measures the modules its plan
# sized. A shard passes its PLAN (scripts/gremlins-shard.py's plan.json) and
# SHARD. Writes sha and modules to GITHUB_OUTPUT. The clone is anonymous,
# because the fanout lists only public repositories.
#
# Usage: gremlins-prepare.sh REPO [PLAN SHARD]
set -euo pipefail

repo="${1:?repo}"
plan="${2:-}"
shard="${3:-}"
if [ -n "${plan}" ] && [ -z "${shard}" ]; then
  echo "::error::gremlins-prepare.sh: a PLAN needs its SHARD" >&2
  exit 2
fi
: "${GITHUB_OUTPUT:?}"

# git over HTTPS draws no API quota, so a rate-limited run still clones.
git clone --depth 1 "https://github.com/cplieger/${repo}.git" target
# Every shard measures the commit its plan sized, so its packages are the ones
# the plan assigned even if the repo moved since.
if [ -n "${plan}" ]; then
  sha=$(jq -er '.sha | select(test("^[0-9a-f]{40}$"))' "${plan}")
  git -C target fetch --quiet --depth 1 origin "${sha}"
  git -C target -c advice.detachedHead=false checkout --quiet "${sha}"
fi
echo "sha=$(git -C target rev-parse HEAD)" >>"${GITHUB_OUTPUT}"

# One gremlins run per module: its coverage pass cannot run a nested module's
# tests. Eligibility mirrors ci.yaml's detect step; keep the two in sync.
safe_dir() {
  case "$1" in
    *[!A-Za-z0-9._/-]*)
      echo "::error::nested go.mod dir '$1' contains characters outside [A-Za-z0-9._/-]"
      exit 1
      ;;
  esac
}
pruned_re='(^|/)(node_modules|vendor|testdata|static|dist)/'
modules=(.)
while IFS= read -r f; do
  [ -z "${f}" ] && continue
  d=$(dirname -- "${f}")
  safe_dir "${d}"
  if [ -z "$(git -C target ls-files -- "${d}/*.go" | head -1)" ]; then
    echo "  skip:    ${d} (no tracked .go files, nothing to mutate)"
    continue
  fi
  modules+=("${d}")
  echo "  measure: ${d} ($(awk '$1=="module"{print $2; exit}' "target/${f}"))"
done < <(git -C target ls-files '*/go.mod' | grep -Ev "${pruned_re}" || true)
# A pruned module is not measured, yet the outer module's run walks its files
# all the same, so its config excludes it too.
unmeasured=()
while IFS= read -r f; do
  [ -z "${f}" ] && continue
  d=$(dirname -- "${f}")
  safe_dir "${d}"
  unmeasured+=("${d}")
  echo "  exclude: ${d} (pruned tree, not measured)"
done < <(git -C target ls-files '*/go.mod' | grep -E "${pruned_re}" || true)
echo "modules=${modules[*]}" >>"${GITHUB_OUTPUT}"
echo "modules to measure: ${modules[*]}"

# One copy per module, because gremlins reads .gremlins.yaml from the module
# root it runs in (findModuleRoot()); the central config beats a lagging copy.
excludes=()
for d in "${unmeasured[@]}"; do excludes+=("--exclude-dir=${d}"); done
shard_args=()
if [ -n "${plan}" ]; then
  shard_args=(--plan "${plan}" --shard "${shard}")
fi
python3 scripts/gremlins-scope.py --config configs/gremlins.yaml --target target \
  "${excludes[@]}" "${shard_args[@]}" -- "${modules[@]}"
