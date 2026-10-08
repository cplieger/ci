#!/usr/bin/env bash
# Coverage oracle for scripts/inventory.py: fails when a package file Renovate
# manages on a base is not read by the engine (DEV-ONLY, not a failure, on another
# base when main ran and does not manage it), when a file the engine reads does not
# parse or order, or when Renovate extracts nothing where the engine reads records.
#   inventory-corpus.sh [--base BRANCH]... [--work DIR] [owner/repo ...]
# No repo: every non-archived non-fork repo of the token's owner that
# release_channels.py calls two-branch. RENOVATE_CMD (default: a local `renovate`) runs as `<cmd> <cfg> <args>`,
# Renovate with <cfg> as RENOVATE_ADDITIONAL_CONFIG_FILE, report at --report-path.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
bases=()
work=
repos=()
while [ $# -gt 0 ]; do
  case $1 in
    --base)
      bases+=("$2")
      shift 2
      ;;
    --work)
      work=$2
      shift 2
      ;;
    -*)
      echo "usage: $0 [--base BRANCH]... [--work DIR] [owner/repo ...]" >&2
      exit 2
      ;;
    *)
      repos+=("$1")
      shift
      ;;
  esac
done
[ ${#bases[@]} -gt 0 ] || bases=(main)
# main first: the other bases read its expected sets.
ordered=()
for base in "${bases[@]}"; do
  [ "$base" = main ] && ordered=(main "${ordered[@]}") || ordered+=("$base")
done
bases=("${ordered[@]}")
if [ -z "$work" ]; then
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
fi
mkdir -p "$work"

run_local_renovate() {
  local cfg=$1
  shift
  RENOVATE_ADDITIONAL_CONFIG_FILE=$cfg renovate "$@"
}
read -r -a renovate_cmd <<<"${RENOVATE_CMD:-run_local_renovate}"

# Package files (and their lockfiles) of <repo> holding a dependency Renovate
# does not skip, update pending or not: an up-to-date surface still meets the
# next update. A repo the report has no clean, well-formed result for fails; one
# that has writes to <count-file> how many dependencies it extracted outside
# .github/, whose workflow and composite-action pins ship nothing and which the
# engine skips.
managed_package_files() {
  python3 - "$1" "$2" "$3" <<'PY'
import json
import sys

report, repo = json.load(open(sys.argv[1], encoding='utf-8')), sys.argv[2]
entry = report.get('repositories', {}).get(repo)
if entry is None or entry.get('problems') or report.get('problems'):
    sys.exit(f'{repo}: no clean Renovate result in the report')
managers = entry.get('packageFiles')
if not isinstance(managers, dict) or not all(
    isinstance(pfs, list)
    and all(
        isinstance(pf, dict)
        and isinstance(pf.get('packageFile'), str)
        and isinstance(pf.get('deps'), list)
        and all(isinstance(d, dict) for d in pf['deps'])
        for pf in pfs
    )
    for pfs in managers.values()
):
    sys.exit(f'{repo}: report has no packageFiles.<manager>[].{{packageFile,deps}} result')
files, extracted = set(), 0
for package_files in managers.values():
    for pf in package_files:
        if pf['packageFile'].startswith('.github/'):
            continue
        extracted += len(pf['deps'])
        if any(not d.get('skipReason') for d in pf['deps']):
            files.add(pf['packageFile'])
            files.update(pf.get('lockFiles') or [])
with open(sys.argv[3], 'w', encoding='utf-8') as count:
    count.write(f'{extracted}\n')
print('\n'.join(sorted(files)))
PY
}

if [ ${#repos[@]} -eq 0 ]; then
  # A failed listing must stop the run, which a process substitution would hide.
  listing=$(gh api --paginate 'user/repos?per_page=100&affiliation=owner' \
    --jq '.[] | select(.archived | not) | select(.fork | not) | {name, full_name, default_branch, visibility, fork, archived} | @json')
  names=$(jq -r '"\(.name) \(.full_name)"' <<<"$listing")
  declare -A full_name=()
  while read -r name full; do
    [ -n "$name" ] || continue
    full_name[$name]=$full
  done <<<"$names"
  two_branch=$(python3 "$here/release_channels.py" two-branch <<<"$listing")
  while read -r name; do
    [ -z "$name" ] || repos+=("${full_name[$name]}")
  done <<<"$two_branch"
fi
if [ ${#repos[@]} -eq 0 ]; then
  echo "::error::no repo to check: none is a two-branch repository" >&2
  exit 1
fi

failed=0
rm -f "$work"/expected-main-*.txt
# One run per base: in multi-base mode the report keeps the first base's
# package files only. `force` outranks each repo's own baseBranchPatterns.
for base in "${bases[@]}"; do
  cfg="$work/corpus-$base.json"
  report="$work/report-$base.json"
  rm -f "$report"
  jq -n --arg base "$base" '{force: {baseBranchPatterns: [$base]}}' >"$cfg"
  echo "== Renovate lookup on $base over ${#repos[@]} repo(s)"
  "${renovate_cmd[@]}" "$cfg" --dry-run=lookup --report-type=file "--report-path=$report" \
    --autodiscover=false "${repos[@]}"
  if [ ! -s "$report" ]; then
    echo "::error::Renovate wrote no report at $report" >&2
    exit 1
  fi
  for repo in "${repos[@]}"; do
    expected="$work/expected-$base-${repo//\//_}.txt"
    count="$work/extracted-$base-${repo//\//_}.txt"
    if ! managed_package_files "$report" "$repo" "$count" >"$expected"; then
      rm -f "$expected"
      failed=1
      continue
    fi
    clone="$work/clone-$base-${repo//\//_}"
    rm -rf "$clone"
    git clone --quiet --depth 1 --branch "$base" "${CORPUS_GIT_BASE:-https://github.com}/$repo.git" "$clone"
    # Every surface must parse and order, Renovate-managed or not: diff parses them all and dominance orders them.
    refused="$work/refused-$base-${repo//\//_}.txt"
    if ! surfaces_json=$(python3 "$here/inventory.py" surfaces --git-dir "$clone" --rev HEAD 2>"$refused"); then
      sed "s|^inventory: |UNREADABLE $repo@$base: |" "$refused"
      failed=1
    fi
    read_files=$(jq -r '.[]' <<<"$surfaces_json")
    on_main="$work/expected-main-${repo//\//_}.txt"
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      if grep -qxF -- "$f" <<<"$read_files"; then
        continue
      fi
      if [ "$base" != main ] && [ -f "$on_main" ] && ! grep -qxF -- "$f" "$on_main"; then
        echo "DEV-ONLY $repo@$base: $f"
      else
        echo "UNREAD $repo@$base: $f"
        failed=1
      fi
    done <"$expected"
    echo "checked $repo@$base: $(grep -c . "$expected" || true) package file(s) Renovate manages"
    # An extraction of nothing proves no package file read, so it passes only for
    # a revision in which the engine finds no dependency either.
    if [ "$(<"$count")" -eq 0 ]; then
      records_err="$work/records-$base-${repo//\//_}.txt"
      if ! records_json=$(python3 "$here/inventory.py" records --git-dir "$clone" --rev HEAD 2>"$records_err"); then
        echo "::error::Renovate extracted no dependency from $repo@$base, and inventory.py cannot read its records: $(tr '\n' ' ' <"$records_err")" >&2
        failed=1
      elif [ "$(jq length <<<"$records_json")" -ne 0 ]; then
        echo "::error::Renovate extracted no dependency from $repo@$base, where inventory.py reads $(jq length <<<"$records_json") record(s)" >&2
        failed=1
      fi
    fi
  done
done
if [ "$failed" -ne 0 ]; then
  echo "RESULT: FAIL (a Renovate result is missing or extracted nothing from a repo holding dependencies, a package file it manages is not read by inventory.py, or one inventory.py reads is refused)"
  exit 1
fi
echo "RESULT: PASS"
