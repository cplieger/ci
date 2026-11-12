#!/usr/bin/env bash
# Clone cplieger/REPO into ./target, find the Go modules to measure and write
# each one's .gremlins.yaml, from the ci checkout root. The plan and run jobs of
# mutation-gremlins-repo.yaml both call it; a shard passes its run PLAN and SHARD
# so it measures the modules its plan sized. GREMLINS_MODULE measures that one
# module and lists the rest as `untested`. Writes sha, modules, untested and
# refused to GITHUB_OUTPUT. The clone is anonymous: the callers name public repositories.
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
# Every shard measures the commit its plan sized, so its files are the ones
# the plan assigned even if the repo moved since.
if [ -n "${plan}" ]; then
  sha=$(jq -er '.sha | select(test("^[0-9a-f]{40}$"))' "${plan}")
  git -C target fetch --quiet --depth 1 origin "${sha}"
  git -C target -c advice.detachedHead=false checkout --quiet "${sha}"
fi
echo "sha=$(git -C target rev-parse HEAD)" >>"${GITHUB_OUTPUT}"

# One gremlins run per module: its coverage pass cannot run a nested module's
# tests. Eligibility mirrors ci.yaml's detect step; keep the two in sync.
# A dir mutation_cost.py's entry_key refuses is not measured, is excluded from
# its outer module as a pruned one is, and is written to `refused`, which makes
# the call red without failing its other modules.
# shellcheck source=scripts/gha-log.sh
. scripts/gha-log.sh
pruned_re='(^|/)(node_modules|vendor|testdata|static|dist)/'
mapfile -d '' gomods < <(git -C target ls-files -z -- '*/go.mod')
nested=()
unmeasured=()
for f in "${gomods[@]}"; do
  d=$(dirname -- "${f}")
  if [[ ${f} =~ ${pruned_re} ]]; then
    unmeasured+=("${d}")
    gha_say 'exclude: %s (pruned tree, not measured)' "${d}"
  else
    nested+=("${d}")
  fi
done
# Looked up by index into nested, because a name that is not UTF-8 is not one JSON can carry.
refused='[]'
if [ "${#nested[@]}" -gt 0 ]; then
  refused=$(printf '%s\0' "${nested[@]}" | python3 scripts/mutation_cost.py refused --repo "${repo}")
fi
declare -A is_refused=()
mapfile -t bad < <(jq -r '.[].index' <<<"${refused}")
for i in "${bad[@]}"; do is_refused["${nested[i]}"]=1; done
refused=$(jq -c 'map(del(.index))' <<<"${refused}")
modules=(.)
for d in "${nested[@]}"; do
  if [ -n "${is_refused["${d}"]:-}" ]; then
    unmeasured+=("${d}")
    continue
  fi
  if [ -z "$(git -C target ls-files -- "${d}/*.go" | head -1)" ]; then
    echo "  skip:    ${d} (no tracked .go files, nothing to mutate)"
    continue
  fi
  modules+=("${d}")
  gha_say 'measure: %s (%s)' "${d}" "$(awk '$1=="module"{print $2; exit}' "target/${d}/go.mod")"
done
untested=()
if [ -n "${GREMLINS_MODULE:-}" ]; then
  selected=()
  for d in "${modules[@]}"; do
    if [ "${d}" = "${GREMLINS_MODULE}" ]; then
      selected+=("${d}")
    else
      untested+=("${d}")
      # A deeper module left out is excluded from its outer one, as a pruned one is.
      [ "${d}" != . ] && unmeasured+=("${d}")
    fi
  done
  if [ "${#selected[@]}" -eq 0 ]; then
    echo "::error::the module input names no measurable Go module of ${repo}"
    exit 1
  fi
  modules=("${selected[@]}")
  # The other dirs are not this call's entries.
  refused='[]'
fi
{
  echo "modules=${modules[*]}"
  echo "untested=${untested[*]}"
  echo "refused=$(jq -c . <<<"${refused}")"
} >>"${GITHUB_OUTPUT}"
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
