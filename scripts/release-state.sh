#!/usr/bin/env bash
# Two-branch release state, one subcommand per release.yaml step: pending
# (each lane's pending promotion, range and kind line), publish, number (via
# CLIFF_SCRIPT), receipts, receipt TAG COMMIT, readback SITE TAG [LANE],
# provenance and complete (IMAGE@DIGEST SOURCE), barrier (holds a stable publish
# until dev ranks above it), renumber-target, renumber-handoff, renumber (re-tag
# LANE_KEY's newest dev build as DEV_VERSION), renumber-subpackages, signing-window
# TAG (EXCLUDE_RE, SUBPACKAGES_JSON), release-digest TAG (IMAGE) and security-shas
# [FLOOR]. Lane keys: '.' for the root, else the lane dir. pending, publish and number hold no token.
set -euo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/reconciliation.sh
. "$TOOLS/reconciliation.sh"
# shellcheck source=SCRIPTDIR/retry.sh
. "$TOOLS/retry.sh"

# Owned by scripts/release_channels.py (COMPLETION_RECEIPT_PREFIX, TAG_RECEIPT_PREFIX).
COMPLETION_PREFIX='release/complete/'
TAG_RECEIPT_PREFIX='release/tag/'
# shellcheck disable=SC2016 # markdown backticks, not command substitution
BUILT_FROM_MAIN='Built from `main` for dependency and system-package updates'

die() {
  echo "::error::$*" >&2
  exit 1
}
note() { echo "::notice::$*" >&2; }
out() { printf '%s\n' "$@" >>"${GITHUB_OUTPUT:?}"; }

esc() { printf '%s' "$1" | sed -e 's/[][\.|(){}?+*^$]/\\&/g'; }
lane_dir() { if [ "$1" = . ]; then printf ''; else printf '%s' "$1"; fi; }
stable_re() { # <key>
  if [ "$1" = . ]; then
    printf '%s' '^v[0-9]+\.[0-9]+\.[0-9]+$'
  else
    printf '^%s/v[0-9]+\\.[0-9]+\\.[0-9]+$' "$(esc "$1")"
  fi
}
dev_re() { # <key>
  local re
  re=$(stable_re "$1")
  printf '%s' "${re%\$}-dev\\.[0-9]+\$"
}
core() { # <tag> -> X.Y.Z of a stable or dev tag of any lane
  local v="${1##*/}"
  v="${v#v}"
  printf '%s' "${v%%-*}"
}
counter() { # <tag> -> N of a -dev.N tag, 0 for a stable one
  case "$1" in *-dev.*) printf '%s' "${1##*-dev.}" ;; *) printf 0 ;; esac
}
vkey() { # X.Y.Z [N] -> fixed-width sortable key
  local IFS=. p
  read -ra p <<<"$1"
  printf '%010d%010d%010d%010d' "$((10#${p[0]}))" "$((10#${p[1]}))" "$((10#${p[2]}))" "$((10#${2:-0}))"
}
gt() { [[ $(vkey "$1") > $(vkey "$2") ]]; } # X.Y.Z X.Y.Z
tag_gt() { [[ $(vkey "$(core "$1")" "$(counter "$1")") > $(vkey "$(core "$2")" "$(counter "$2")") ]]; }

lane_keys() { # -> '.' when the root releases, then each nested lane
  case "${REPO_TYPE:-none}" in docker | go | ts) echo . ;; esac
  jq -r '.[]' <<<"${GO_LANES_JSON:-[]}"
}
tags_of() { # <regex> -> "<tag> <commit>" per matching tag
  local name obj peeled
  while read -r name obj peeled; do
    if [[ $name =~ $1 ]]; then
      printf '%s %s\n' "$name" "${peeled:-$obj}"
    fi
  done < <(git for-each-ref refs/tags --format='%(refname:strip=2) %(objectname) %(*objectname)')
}
highest_stable() { # <key> [below X.Y.Z] -> "<tag> <commit>" of the lane's highest stable tag by version
  local best="" at="" name c
  while read -r name c; do
    if [ -n "${2:-}" ] && ! gt "$2" "$(core "$name")"; then
      continue
    fi
    if [ -z "$best" ] || gt "$(core "$name")" "$(core "$best")"; then
      best=$name at=$c
    fi
  done < <(tags_of "$(stable_re "$1")")
  [ -z "$best" ] || printf '%s %s\n' "$best" "$at"
}
nearest_dev_tag() { # <commit> <key> -> the highest dev tag of the lane on the commit nearest <commit>
  local -A at=()
  local name c
  while read -r name c; do
    if [ -z "${at[$c]:-}" ] || tag_gt "$name" "${at[$c]}"; then
      at[$c]=$name
    fi
  done < <(tags_of "$(dev_re "$2")")
  [ "${#at[@]}" -gt 0 ] || return 0
  while read -r c; do
    if [ -n "${at[$c]:-}" ]; then
      printf '%s' "${at[$c]}"
      return 0
    fi
  done < <(git rev-list --topo-order "$1")
}

# A lane is affected by R when R's diff from its first parent changes a path the
# lane ships (path-significance.sh MODE=paths; TS subpackages share the root).
affects() { # <R> <key>
  local res
  res=$(MODE=paths FROM="$1^1" TO="$1" bash "$TOOLS/path-significance.sh") \
    || die "path significance of ${1}^1..${1} failed"
  if [ "$2" = . ]; then
    if grep -qx 'root_changed=true' <<<"$res"; then
      return 0
    fi
    [ "$(sed -n 's/^subpackages_to_publish=//p' <<<"$res")" != '[]' ]
  else
    sed -n 's/^go_modules_to_release=//p' <<<"$res" | jq -e --arg l "$2" 'index($l) != null' >/dev/null
  fi
}
newest_affecting() { # <from> <to> <key> -> the newest reconciliation of from..to that affected the lane
  local c found=""
  while IFS= read -r c; do
    if [ -n "$c" ] && affects "$c" "$3"; then
      found=$c
    fi
  done < <(reconciliations_in "$1" "$2")
  printf '%s' "$found"
}
kind_note() { # <release commit> <pending R or ''> <key> -> the release body's opening kind line
  local t d from
  if [ -z "$2" ]; then
    printf '%s' "$BUILT_FROM_MAIN"
    return 0
  fi
  t=$(git rev-parse "$2^2")
  d=$(nearest_dev_tag "$t" "$3")
  if [ -n "$d" ]; then from="\`$d\`"; else from="\`dev\` at \`${t:0:12}\`"; fi
  if [ "$1" = "$2" ]; then
    printf 'Promoted from %s' "$from"
  else
    # shellcheck disable=SC2016 # markdown backticks
    printf 'Built from `main`, including the promotion of %s' "$from"
  fi
}

cmd_pending() {
  local main=HEAD head state='{}' lanes='[]' key h h_tag h_commit r in_range kind repair_kind prev
  case "${CHANNEL:-}" in
    stable) ;;
    dev)
      main=refs/remotes/origin/main
      git rev-parse --verify --quiet "${main}^{commit}" >/dev/null \
        || die "a dev run needs ${main} to find pending promotions. Check out with fetch-depth: 0."
      ;;
    *) die "CHANNEL must be dev or stable, got '${CHANNEL:-}'" ;;
  esac
  head=$(git rev-parse HEAD)
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    h=$(highest_stable "$key")
    h_tag=${h% *} h_commit=${h#* }
    # Same refusal as compute.sh's: a stable run behind its lane's highest
    # stable tag is older than a published release.
    if [ "$CHANNEL" = stable ] && [ -n "$h_commit" ] && ! git merge-base --is-ancestor "$h_commit" "$head"; then
      die "lane ${key}: the highest stable tag ${h_tag} is on ${h_commit}, which HEAD does not contain, so an older commit is not published above it"
    fi
    r=$(newest_affecting "$h_commit" "$main" "$key")
    in_range=false kind="" repair_kind=""
    if [ "$CHANNEL" = stable ]; then
      [ -z "$r" ] || in_range=true
      kind=$(kind_note "$head" "$r" "$key")
      if [ -n "$h_tag" ]; then
        prev=$(highest_stable "$key" "$(core "$h_tag")")
        repair_kind=$(kind_note "$h_commit" "$(newest_affecting "${prev#* }" "$h_commit" "$key")" "$key")
      fi
    fi
    state=$(jq -c --arg k "$key" --arg lane "$(lane_dir "$key")" --arg h_tag "$h_tag" --arg h_commit "$h_commit" \
      --arg commit "$r" --argjson in_range "$in_range" --arg kind "$kind" --arg repair "$repair_kind" \
      '. + {($k): {lane: $lane, h_tag: $h_tag, h_commit: $h_commit, commit: $commit, version: "",
        in_range: $in_range, kind_note: $kind, repair_kind_note: $repair}}' <<<"$state")
    lanes=$(jq -c --arg k "$key" --arg lane "$(lane_dir "$key")" '. + [{key: $k, lane: $lane}]' <<<"$lanes")
    echo "lane ${key}: highest stable ${h_tag:-<none>}, pending promotion ${r:-<none>}, in range ${in_range}"
  done < <(lane_keys)
  out "state=${state}" "lanes=${lanes}" \
    "any=$(jq -r 'any(.[]; .commit != "")' <<<"$state")" \
    "in_range_any=$(jq -r 'any(.[]; .in_range)' <<<"$state")" \
    "root_in_range=$(jq -r '.["."].in_range // false' <<<"$state")" \
    "root_kind_note=$(jq -r '.["."].kind_note // ""' <<<"$state")"
}

# The root publishes every promotion past its stable tag, since an earlier one
# whose run failed may have changed a subpackage the newest did not.
root_promotions() { # <state> -> the root's in-range promotions past its stable tag, oldest first
  local r h
  IFS=$'\t' read -r r h < <(jq -r '.["."] // {} | select(.in_range) | [.commit, (.h_commit // "")] | @tsv' <<<"$1") || return 0
  [ -z "$r" ] || reconciliations_in "$h" "$r"
}

# A pending promotion is a reason to publish its lane on its own: the commit
# that triggered this run may ship nothing (R's own run failed, then a docs-only
# S landed). Unions each in-range lane into the current publication sets.
cmd_publish() {
  local state="${STATE:?}" rc="${ROOT_CHANGED:-false}" subs="${SUBPACKAGES_TO_PUBLISH:-[]}" mods="${GO_MODULES_TO_RELEASE:-[]}"
  local key r c recs res
  [ "${CHANNEL:-}" = stable ] || die "publish describes a stable run's range, but this run is on the '${CHANNEL:-}' channel"
  while IFS=$'\t' read -r key r; do
    [ -n "$key" ] || continue
    if [ "$key" = . ]; then
      recs=$(root_promotions "$state") || die "cannot list the root's promotions past its stable tag up to ${r}"
      while IFS= read -r c; do
        [ -n "$c" ] || continue
        res=$(MODE=paths FROM="$c^1" TO="$c" bash "$TOOLS/path-significance.sh") \
          || die "path significance of ${c}^1..${c} failed"
        if grep -qx 'root_changed=true' <<<"$res"; then
          rc=true
        fi
        subs=$(jq -c --argjson add "$(sed -n 's/^subpackages_to_publish=//p' <<<"$res")" \
          'reduce $add[] as $x (.; if any(.[]; . == $x) then . else . + [$x] end)' <<<"$subs")
      done <<<"$recs"
    else
      mods=$(jq -c --arg l "$key" 'if any(.[]; . == $l) then . else . + [$l] end' <<<"$mods")
    fi
    note "lane ${key}: this run publishes the pending promotion ${r}"
  done < <(jq -r 'to_entries[] | select(.value.in_range) | [.key, .value.commit] | @tsv' <<<"$state")
  out "root_changed=${rc}" "subpackages_to_publish=${subs}" "go_modules_to_release=${mods}"
}

cmd_number() {
  local state="${STATE:?}" key r v res
  : "${CLIFF_SCRIPT:?}"
  while IFS=$'\t' read -r key r; do
    [ -n "$key" ] || continue
    if [ "$key" = . ]; then
      res=$(LANE="" EXCLUDE_PATHS="${EXCLUDE_PATHS:-}" bash "$CLIFF_SCRIPT" pending-version "$r") \
        || die "could not number the pending promotion ${r}: ${res}"
    else
      res=$(LANE="" EXCLUDE_PATHS="" bash "$CLIFF_SCRIPT" pending-version "$r" "$key") \
        || die "could not number the pending promotion ${r} of lane ${key}: ${res}"
    fi
    v=$(tail -n1 <<<"$res")
    [[ $v =~ $(stable_re "$key") ]] || die "pending-version of ${r} answered '${v}', not a stable version of lane ${key}"
    state=$(jq -c --arg k "$key" --arg v "$v" '.[$k].version = $v' <<<"$state")
    echo "lane ${key}: the promotion ${r} publishes ${v}"
  done < <(jq -r 'to_entries[] | select(.value.commit != "") | [.key, .value.commit] | @tsv' <<<"$state")
  out "state=${state}" "root_version=$(jq -r '.["."].version // ""' <<<"$state")"
}

subpackages_at() { # <commit> -> JSON array of the first-level dirs carrying a jsr.json at <commit>
  local d
  git ls-tree -d --name-only "$1" | while IFS= read -r d; do
    if git cat-file -e "$1:$d/jsr.json" 2>/dev/null; then printf '%s\n' "$d"; fi
  done | jq -Rsc 'split("\n") | map(select(. != ""))'
}
# TS subpackages share the root version and publish after the root tag, so a
# root release is complete only once each subpackage its range changed is
# published too: the range from the root tag below it, every subpackage when
# there is none, plus each promotion's own diff, as detect's publish step adds.
published_subpackages() { # <root tag> <commit> -> JSON array of subpackage dirs
  local all prev subs from to res add
  all=$(subpackages_at "$2") || return 1
  if [ "$all" = '[]' ]; then
    echo '[]'
    return 0
  fi
  prev=$(highest_stable . "$(core "$1")")
  prev=${prev#* }
  if [ -z "$prev" ]; then
    echo "$all"
    return 0
  fi
  subs='[]'
  while read -r from to; do
    res=$(MODE=paths FROM="$from" TO="$to" SUBPACKAGES_JSON="$all" GO_LANES_JSON='[]' bash "$TOOLS/path-significance.sh") || return 1
    add=$(sed -n 's/^subpackages_to_publish=//p' <<<"$res")
    subs=$(jq -c --argjson add "$add" 'reduce $add[] as $x (.; if any(.[]; . == $x) then . else . + [$x] end)' <<<"$subs")
  done < <(
    echo "$prev $2"
    reconciliations_in "$prev" "$2" | sed -n 's/^\(.\+\)$/\1^1 \1/p'
  )
  printf '%s\n' "$subs"
}

cmd_receipts() {
  local state="${STATE:?}" repo="${GITHUB_REPOSITORY:?}" repairs='[]' key tag commit kind contexts site subs shas floor
  while IFS=$'\t' read -r key tag commit kind; do
    [ -n "$key" ] || continue
    if ! contexts=$(gh api --paginate "repos/${repo}/commits/${commit}/statuses?per_page=100" \
      --jq '.[] | select(.state == "success") | .context'); then
      die "could not read the statuses of ${commit}, the commit of ${tag}, so its completion is not guessed"
    fi
    if grep -qxF "${COMPLETION_PREFIX}${tag}" <<<"$contexts"; then
      echo "lane ${key}: ${tag} carries its completion receipt"
      continue
    fi
    subs='[]'
    if [ "$key" = . ]; then
      site=${REPO_TYPE:?}
      subs=$(published_subpackages "$tag" "$commit") || die "could not tell which subpackages ${tag} publishes"
    else
      site=lane
    fi
    note "lane ${key}: ${tag} has no completion receipt, so this run repairs it at ${commit} first"
    repairs=$(jq -c --arg k "$key" --arg lane "$(lane_dir "$key")" --arg tag "$tag" --arg commit "$commit" \
      --arg site "$site" --arg kind "$kind" --arg subs "$subs" \
      '. + [{key: $k, lane: $lane, tag: $tag, commit: $commit, site: $site, kind_note: $kind, subpackages: $subs}]' <<<"$repairs")
  done < <(jq -r 'to_entries[] | select(.value.h_tag != "") | [.key, .value.h_tag, .value.h_commit, .value.repair_kind_note] | @tsv' <<<"$state")
  floor=$(notes_floor "$state" "$repairs") || die "could not date the notes ranges of this run"
  if ! shas=$(security_shas "$floor"); then
    die "could not list the merged security pull requests of ${repo}"
  fi
  out "repairs=${repairs}" "repair_publishes=$(jq -c '[.[] | select(.subpackages != "[]")]' <<<"$repairs")" \
    "repair_images=$(jq -c '[.[] | select(.site == "docker")]' <<<"$repairs")" \
    "security_shas=$(tr '\n' ' ' <<<"$shas" | sed 's/ *$//')"
}

# The oldest commit any notes range of this run can start after: per lane, the
# tag below a repaired version, else the lane's highest stable tag. Empty when
# a lane has no such tag, since its range then starts at the root.
notes_floor() { # <state> <repairs> -> epoch seconds, or nothing
  local key h_tag h_commit base floor="" t
  while IFS=$'\t' read -r key h_tag h_commit; do
    [ -n "$key" ] || continue
    base=$h_commit
    if jq -e --arg k "$key" 'any(.[]; .key == $k)' <<<"$2" >/dev/null; then
      base=$(highest_stable "$key" "$(core "$h_tag")")
      base=${base#* }
    fi
    [ -n "$base" ] || return 0
    t=$(git log -1 --format=%ct "$base") || return 1
    if [ -z "$floor" ] || [ "$t" -lt "$floor" ]; then floor=$t; fi
  done < <(jq -r 'to_entries[] | [.key, .value.h_tag, .value.h_commit] | @tsv' <<<"$1")
  printf '%s' "$floor"
}
# A promotion's range reaches dev's own merges through R's second parent, and a
# dev PR's squash commit is not the one its main twin made, so both bases count.
security_shas() { # <floor epoch or ''> -> merge SHAs of merged Renovate security PRs into main or dev
  local base shas=""
  for base in main dev; do
    shas+=$(security_shas_into "$base" "$1") || return 1
    shas+=$'\n'
  done
  awk 'NF && !seen[$0]++' <<<"$shas"
}
# Newest-updated first, stopping at a page that ends before the floor: a PR
# merged after a range's base was updated no earlier, and the day of slack
# absorbs skew between a merge and its commit's date.
security_shas_into() { # <base> <floor epoch or ''>
  local page=1 body n
  while :; do
    body=$(gh api "repos/${GITHUB_REPOSITORY}/pulls?state=closed&base=${1}&sort=updated&direction=desc&per_page=100&page=${page}") || return 1
    n=$(jq length <<<"$body") || return 1
    jq -r '.[] | select(.merged_at != null and (.head.ref | startswith("renovate/")) and any(.labels[]; .name == "security")) | .merge_commit_sha' <<<"$body" || return 1
    [ "$n" -eq 100 ] || return 0
    if [ -n "$2" ] && jq -e --argjson f "$(($2 - 86400))" '.[-1].updated_at | fromdateiso8601 < $f' <<<"$body" >/dev/null; then
      return 0
    fi
    page=$((page + 1))
  done
}

cmd_receipt() { # <tag> <commit>
  local tag="${1:?usage: receipt TAG COMMIT}" commit="${2:?usage: receipt TAG COMMIT}" repo="${GITHUB_REPOSITORY:?}"
  gh api "repos/${repo}/releases/tags/${tag}" >/dev/null \
    || die "${tag} has no published Release, so its completion receipt is not recorded"
  gh api -X POST "repos/${repo}/statuses/${commit}" -f state=success -f context="${COMPLETION_PREFIX}${tag}" \
    -f description="Release and registry readback verified" \
    -f target_url="${GITHUB_SERVER_URL:-https://github.com}/${repo}/actions/runs/${GITHUB_RUN_ID:-0}" >/dev/null
  echo "recorded ${COMPLETION_PREFIX}${tag} on ${commit}"
}

answers() { # <label> <command...>: retry.sh's await_registry, then the verdict
  local label=$1
  shift
  if await_registry "$label" "$@"; then
    echo "ok: ${label}"
    return 0
  fi
  echo "::error::${label} does not answer"
  return 1
}
fetch() { curl -fsSL --connect-timeout 10 --max-time 30 -o /dev/null "$1"; }
INDEX_TYPES='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'
registry_digest() { # <registry host> <token url> <repository> <ref> <accept> -> the digest served, or nothing
  local token
  token=$(curl -fsSL --connect-timeout 10 --max-time 30 --retry 3 --retry-all-errors "$2" | jq -r .token) || return 0
  curl -fsSI --connect-timeout 10 --max-time 30 -H "Authorization: Bearer ${token}" -H "Accept: $5" \
    "https://${1}/v2/${3}/manifests/${4}" 2>/dev/null \
    | tr -d '\r' | awk 'tolower($1) == "docker-content-digest:" { print $2 }' || true
}
ghcr_digest() { # <repository> <ref> [accept] -> the digest GHCR serves (an index by default), or nothing
  registry_digest ghcr.io "https://ghcr.io/token?scope=repository:${1}:pull" "$1" "$2" "${3:-$INDEX_TYPES}"
}
ghcr_state() { # <repository> <ref> -> the index digest served, or "absent" on a 404; fails when GHCR cannot tell
  local token hdr
  token=$(curl -fsSL --connect-timeout 10 --max-time 30 --retry 3 --retry-all-errors \
    "https://ghcr.io/token?scope=repository:${1}:pull" | jq -r .token) || return 1
  hdr=$(curl -sSI --connect-timeout 10 --max-time 30 --retry 3 -H "Authorization: Bearer ${token}" \
    -H "Accept: ${INDEX_TYPES}" "https://ghcr.io/v2/${1}/manifests/${2}" | tr -d '\r') || return 1
  case "$(awk 'NR == 1 { print $2 }' <<<"$hdr")" in
    200) awk 'tolower($1) == "docker-content-digest:" { print $2 }' <<<"$hdr" | grep . ;;
    404) echo absent ;;
    *) return 1 ;;
  esac
}
hub_digest() { # <repository> <ref>
  registry_digest registry-1.docker.io \
    "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${1}:pull" "$1" "$2" "$INDEX_TYPES"
}
# serves <digest> <digest command...>
serves() { [ "$("${@:2}")" = "$1" ]; }
# cosign retries none of its own network calls, so only its verification
# verdicts (sigstore/cosign pkg/cosign/verify.go) mean "not signed there".
verified_at() { # <image@digest> <commit> <signature|sbom>: 0 when docker-release.yaml in a run of this repository at <commit> signed it (or attested its SPDX SBOM), 1 not; dies when cosign cannot tell
  local i err
  local -a verb=(verify)
  [ "$3" = signature ] || verb=(verify-attestation --type spdxjson)
  for i in 1 2 3 4 5; do
    err=$(cosign "${verb[@]}" --certificate-oidc-issuer https://token.actions.githubusercontent.com \
      --certificate-identity-regexp '^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@' \
      --certificate-github-workflow-repository "${GITHUB_REPOSITORY:?}" --certificate-github-workflow-sha "$2" \
      "$1" 2>&1 >/dev/null) && return 0
    case "$err" in *"no matching signatures"* | *"no matching attestations"* | *"no signatures found"*) return 1 ;; esac
    [ "$i" -lt 5 ] && sleep "${READBACK_SLEEP:-20}"
  done
  die "cosign could not verify $1 at $2: $(printf '%s' "$err" | tail -n 1)"
}

# A stable tag published before its repository released from dev may be
# served by a build signed at a later first-parent commit that re-pushed the
# tag, so the window runs from the tag's commit up to the lane's next stable tag.
publishing_window() { # <tag> <commit> -> the tag's commit, then each later commit that may have re-pushed the tag
  local c name
  printf '%s\n' "$2"
  git merge-base --is-ancestor "$2" HEAD || return 0
  while IFS= read -r c; do
    while IFS= read -r name; do
      if [[ $name =~ $(stable_re .) ]] && [ "$name" != "$1" ]; then
        return 0
      fi
    done < <(git tag --points-at "$c")
    printf '%s\n' "$c"
  done < <(git rev-list --first-parent --ancestry-path --reverse "$2..HEAD")
}
window_order() { # <tag> <commit> -> publishing_window, the tag's commit first, then newest first
  local -a w
  local i
  mapfile -t w < <(publishing_window "$1" "$2")
  printf '%s\n' "${w[0]}"
  for ((i = ${#w[@]} - 1; i > 0; i--)); do
    printf '%s\n' "${w[i]}"
  done
}
signed_in_window() { # <image@digest> <commits, one per line, in the order to ask> [signature|sbom]
  local c
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    verified_at "$1" "$c" "${3:-signature}" && return 0
  done <<<"$2"
  return 1
}
# A re-tagged image is never signed or attested again, and a run can push its
# sha- tag and die before either: an image is complete only once a
# docker-release.yaml run its source carries signed it and attested its SBOM.
provenance_gap() { # <image@digest> <source commit> -> why the image is incomplete, nothing when complete; dies when cosign cannot tell
  local ref=$1 src=$2 window
  window=$(bash "$TOOLS/promote-digest.sh" --sources "$src") \
    || die "cannot tell which commits could have built ${ref}"
  if ! signed_in_window "$ref" "$window" signature; then
    printf '%s carries no signature from a docker-release.yaml run of %s at %s or an ancestor whose image it carries, because its build stopped before signing' \
      "$ref" "$GITHUB_REPOSITORY" "$src"
  elif ! signed_in_window "$ref" "$window" sbom; then
    printf '%s carries no SPDX SBOM attestation from a docker-release.yaml run of %s at %s or an ancestor whose image it carries, because its build stopped before attesting' \
      "$ref" "$GITHUB_REPOSITORY" "$src"
  fi
}
cmd_provenance() { # <image@digest> <source commit, promote-digest.sh's promote_source>
  local ref="${1:?usage: provenance IMAGE@DIGEST SOURCE}" src="${2:?usage: provenance IMAGE@DIGEST SOURCE}" gap
  gap=$(provenance_gap "$ref" "$src") || exit 1
  [ -z "$gap" ] || die "${gap}, so it is not promoted"
  echo "ok: ${ref} was signed and attested by the build it promotes"
}
# Exit 3: incomplete, so the caller builds from source instead.
cmd_complete() { # <image@digest> <source commit>
  local ref="${1:?usage: complete IMAGE@DIGEST SOURCE}" src="${2:?usage: complete IMAGE@DIGEST SOURCE}" gap
  gap=$(provenance_gap "$ref" "$src") || exit 1
  if [ -n "$gap" ]; then
    note "${gap}, so it is not reused"
    exit 3
  fi
  echo "ok: ${ref} was signed and attested by the build that made it"
}
# An image is signed by the run that built it: a promotion's by the dev run
# of its promoted commit or of a first-parent ancestor it carries unchanged,
# main's commits included; any other tag's at its commit, at such an ancestor
# whose image it re-tagged, or in its publishing window. promote-digest.sh
# owns which ancestors those are.
signing_window() { # <tag> -> the commits whose docker-release.yaml run may have signed the tag's image, in the order to try
  local commit
  commit=$(git rev-list -n1 "$1") || die "no tag ${1} in this checkout"
  if is_reconciliation "$commit"; then
    bash "$TOOLS/promote-digest.sh" --sources "${commit}^2" || return 1
  else
    bash "$TOOLS/promote-digest.sh" --sources "$commit" || return 1
    window_order "$1" "$commit" | tail -n +2
  fi
}

# A stable image tag is complete only at the digest its release published:
# R's Promoted-Digest, signed and attested by the build it promotes, for a
# promotion, else a digest a docker-release.yaml run in the tag's signing
# window signed. Every registry that run writes must serve it, and the
# dashboard artifact too when the commit ships one.
release_digest() { # <tag> -> the digest the tag's release published: R's Promoted-Digest, else what GHCR serves at the tag
  local commit want
  : "${IMAGE:?}"
  commit=$(git rev-list -n1 "$1") || die "no tag ${1} in this checkout"
  if is_reconciliation "$commit"; then
    promoted_digest "$commit" || die "${1} is on the promotion ${commit}, which carries no root Promoted-Digest"
  else
    want=$(ghcr_digest "$IMAGE" "$1")
    [ -n "$want" ] || die "ghcr.io/${IMAGE}:${1} does not exist"
    printf '%s\n' "$want"
  fi
}
readback_image() { # <tag>
  local tag=$1 commit want dash window
  commit=$(git rev-list -n1 "$tag") || die "no tag ${tag} in this checkout"
  want=$(release_digest "$tag") || exit 1
  if is_reconciliation "$commit"; then
    cmd_provenance "ghcr.io/${IMAGE}@${want}" "$(git rev-parse "${commit}^2")"
  else
    window=$(signing_window "$tag") || die "cannot tell which commits could have signed ${tag}'s image"
    signed_in_window "ghcr.io/${IMAGE}@${want}" "$window" \
      || die "ghcr.io/${IMAGE}:${tag} is ${want}, which no docker-release.yaml run at ${commit}, an ancestor whose image it carries, or a later main commit below the next stable tag signed"
  fi
  answers "ghcr.io/${IMAGE}:${tag} at ${want}" serves "$want" ghcr_digest "$IMAGE" "$tag" || return 1
  if [[ ${REGISTRIES:-} == *dockerhub* ]]; then
    answers "docker.io/${IMAGE}:${tag} at ${want}" serves "$want" hub_digest "$IMAGE" "$tag" || return 1
  fi
  git cat-file -e "${commit}:grafana-dashboard.json" 2>/dev/null || return 0
  dash=$(ghcr_digest "${IMAGE}/dashboard" "$tag" application/vnd.oci.image.manifest.v1+json)
  if [ -z "$dash" ] || ! signed_in_window "ghcr.io/${IMAGE}/dashboard@${dash}" "$(window_order "$tag" "$commit")"; then
    die "ghcr.io/${IMAGE}/dashboard:${tag} is '${dash}', which no docker-release.yaml run at ${commit} or a later main commit below the next stable tag signed"
  fi
  echo "ok: ghcr.io/${IMAGE}/dashboard:${tag} -> ${dash}"
}

cmd_readback() { # <site> <tag> [lane]; SUBPACKAGES: the root release's subpackage dirs (JSON)
  local site="${1:?}" tag="${2:?}" lane="${3:-}" mod pkg sub ver="${2##*/}"
  case "$site" in
    go | lane)
      mod=$(git show "${tag}:${lane:+$lane/}go.mod" | awk '$1=="module"{print $2; exit}' | tr -d '"' | sed 's/[A-Z]/!\L&/g')
      [ -n "$mod" ] || die "no module directive in ${tag}:${lane:+$lane/}go.mod"
      answers "proxy.golang.org ${mod}@${ver}" fetch "https://proxy.golang.org/${mod}/@v/${ver}.info"
      ;;
    ts)
      pkg=$(git show "${tag}:package.json" | jq -r .name)
      answers "npm ${pkg}@${ver#v}" fetch "https://registry.npmjs.org/${pkg}/${ver#v}"
      answers "jsr ${pkg}@${ver#v}" fetch "https://jsr.io/${pkg}/${ver#v}_meta.json"
      ;;
    docker) readback_image "$tag" ;;
    *) die "unknown readback site '${site}'" ;;
  esac
  while IFS= read -r sub; do
    [ -n "$sub" ] || continue
    pkg=$(git show "${tag}:${sub}/package.json" | jq -r .name) || die "no ${sub}/package.json at ${tag}"
    answers "npm ${pkg}@${ver#v}" fetch "https://registry.npmjs.org/${pkg}/${ver#v}" || return 1
    answers "jsr ${pkg}@${ver#v}" fetch "https://jsr.io/${pkg}/${ver#v}_meta.json" || return 1
  done < <(jq -r '.[]' <<<"${SUBPACKAGES:-[]}")
}

dev_published() { # <key> <dev tag> -> 0 once every registry the lane's dev channel writes serves the tag
  local pkg
  [ "$1" = . ] && [ "${REPO_TYPE:-}" = ts ] || return 0
  pkg=$(jq -r .name package.json) || return 1
  answers "npm ${pkg}@${2#v}" fetch "https://registry.npmjs.org/${pkg}/${2#v}" >/dev/null
}
has_dev_above() { # <key> <X.Y.Z> -> 0 when a published dev build of the lane on dev's history ranks above X.Y.Z
  local name c
  while read -r name c; do
    if gt "$(core "$name")" "$2" && git merge-base --is-ancestor "$c" refs/remotes/origin/dev && dev_published "$1" "$name"; then
      return 0
    fi
  done < <(tags_of "$(dev_re "$1")")
  return 1
}
built_past() { # <key> <T> -> 0 when dev's history holds a dev build of the lane that T cannot reach
  local name c
  while read -r name c; do
    if git merge-base --is-ancestor "$c" refs/remotes/origin/dev && ! git merge-base --is-ancestor "$c" "$2"; then
      return 0
    fi
  done < <(tags_of "$(dev_re "$1")")
  return 1
}
newest_built() { # <key> <T> -> the newest first-parent dev commit past T carrying a dev tag of the lane, or nothing
  local c
  while IFS= read -r c; do
    if [ -n "$(tags_at "$c" "$1")" ]; then
      printf '%s' "$c"
      return 0
    fi
  done < <(git rev-list --first-parent "$2..refs/remotes/origin/dev")
}
# Dev publishes a TS root's npm version and an image's GHCR tags before its git
# tag, so a run that died in between left a build no dev tag shows. A newer
# tagged build carries that build's content and renumber lifts it, so only a
# build past the newest tagged one holds.
untagged_past() { # <key> <T> -> a dev commit past T whose build is published untagged, newer than every tagged build; fails when a registry cannot tell
  local c got res code pkg newest
  [ "$1" = . ] || return 0
  newest=$(newest_built "$1" "$2")
  case "${REPO_TYPE:-}" in
    docker)
      while IFS= read -r c; do
        [ "$c" != "$newest" ] || return 0
        got=$(ghcr_state "${IMAGE:?}" "sha-${c}") || return 1
        if [ "$got" != absent ]; then
          printf '%s' "$c"
          return 0
        fi
      done < <(git rev-list --first-parent "$2..refs/remotes/origin/dev")
      ;;
    ts)
      pkg=$(jq -r .name package.json) || return 1
      res=$(curl -sS --connect-timeout 10 --max-time 30 --retry 3 -w '\n%{http_code}' \
        "https://registry.npmjs.org/${pkg}") || return 1
      code=${res##*$'\n'}
      case "$code" in
        404) return 0 ;;
        200) ;;
        *) return 1 ;;
      esac
      while IFS= read -r c; do
        if git merge-base --is-ancestor "$c" refs/remotes/origin/dev 2>/dev/null \
          && ! git merge-base --is-ancestor "$c" "$2" && [ -z "$(tags_at "$c" "$1")" ] \
          && { [ -z "$newest" ] || ! git merge-base --is-ancestor "$c" "$newest"; }; then
          printf '%s' "$c"
          return 0
        fi
      done < <(jq -r '.versions // {} | to_entries[] | select(.key | test("-dev\\.[0-9]+$")) | .value.gitHead // empty' <<<"${res%$'\n'*}" | sort -u)
      ;;
  esac
}
tags_at() { # <commit> <key> -> the lane's dev tags on the commit
  local name c
  while read -r name c; do
    [ "$c" != "$1" ] || printf '%s\n' "$name"
  done < <(tags_of "$(dev_re "$2")")
}

# A dev run publishes a TS subpackage only when its range changed it, and only
# after the run's root tag, so no root dev tag shows whether it published.
promoted_subpackages() { # <R> -> JSON array of the subpackages R's promotion publishes
  local res
  res=$(MODE=paths FROM="$1^1" TO="$1" SUBPACKAGES_JSON="$(subpackages_at "$1")" GO_LANES_JSON='[]' \
    bash "$TOOLS/path-significance.sh") || return 1
  sed -n 's/^subpackages_to_publish=//p' <<<"$res"
}
last_change() { # <ref> <dir> -> the newest first-parent commit of ref whose own diff changes what the subpackage ships
  local c from res
  while IFS= read -r c; do
    from=$(git rev-parse --verify --quiet "${c}^1") || from=$(git hash-object -t tree /dev/null)
    res=$(MODE=paths FROM="$from" TO="$c" SUBPACKAGES_JSON="$(jq -cn --arg d "$2" '[$d]')" GO_LANES_JSON='[]' \
      bash "$TOOLS/path-significance.sh") || return 1
    if sed -n 's/^subpackages_to_publish=//p' <<<"$res" | jq -e --arg d "$2" 'index($d) != null' >/dev/null; then
      printf '%s' "$c"
      return 0
    fi
  done < <(git rev-list --first-parent "$1" -- "$2")
}
sub_dev_heads() { # <dir> <commit> -> "<version> <gitHead>" per -dev.N version npm serves; nothing on a 404; fails when npm cannot tell
  local pkg res code
  pkg=$(git show "$2:$1/package.json" | jq -er '.name | strings') || return 1
  res=$(curl -sS --connect-timeout 10 --max-time 30 --retry 3 -w '\n%{http_code}' "https://registry.npmjs.org/${pkg}") || return 1
  code=${res##*$'\n'}
  case "$code" in
    404) return 0 ;;
    200) ;;
    *) return 1 ;;
  esac
  jq -r '.versions // {} | to_entries[] | select(.key | test("-dev\\.[0-9]+$")) | "\(.key) \(.value.gitHead // "")"' <<<"${res%$'\n'*}"
}
# A subpackage's dev build is complete once npm serves a -dev.N version
# published from its newest change on dev or a descendant on dev. With T, a
# change past T must also rank above V, as a root build past T must.
sub_dev_state() { # <dir> <T or ''> <V> -> "ok", "incomplete <change>" or "below <change>"
  local dir=$1 t=$2 v=$3 l heads ver head complete=false above=false
  git cat-file -e "refs/remotes/origin/dev:${dir}/package.json" 2>/dev/null || {
    echo ok
    return 0
  }
  l=$(last_change refs/remotes/origin/dev "$dir") || return 1
  if [ -z "$l" ]; then
    echo ok
    return 0
  fi
  heads=$(sub_dev_heads "$dir" refs/remotes/origin/dev) || return 1
  while read -r ver head; do
    [ -n "$head" ] || continue
    git merge-base --is-ancestor "$l" "$head" 2>/dev/null || continue
    git merge-base --is-ancestor "$head" refs/remotes/origin/dev 2>/dev/null || continue
    complete=true
    if gt "$(core "$ver")" "$(core "$v")"; then above=true; fi
  done <<<"$heads"
  if [ "$complete" = false ]; then
    echo "incomplete $l"
  elif [ -z "$t" ] || git merge-base --is-ancestor "$l" "$t" || [ "$above" = true ]; then
    echo ok
  else
    echo "below $l"
  fi
}
subs_lacking() { # <rows "<dir>\t<T>\t<V>"> [state] -> "<dir> <state> <change>" per subpackage not ok (or only in that state)
  local dir t v st
  while IFS=$'\t' read -r dir t v; do
    [ -n "$dir" ] || continue
    st=$(sub_dev_state "$dir" "$t" "$v") || die "subpackage ${dir}: cannot tell whether npm serves its newest dev build"
    [ "$st" != ok ] || continue
    [ -z "${2:-}" ] || [ "${st%% *}" = "$2" ] || continue
    printf '%s %s\n' "$dir" "$st"
  done <<<"$1"
}
runs_api() { echo "repos/${GITHUB_REPOSITORY}/actions/workflows/${WORKFLOW}/runs?branch=dev&per_page=100${1:-}"; }
# Every wait of one barrier draws on one read budget, and the dev-run wait
# leaves RENUMBER_READS of it, so the renumber run the hold dispatches is
# waited for past its three chained renumber jobs' timeout-minutes. BARRIER_SECONDS
# bounds the whole hold in time, slow and wedged reads included, so the hold
# ends with its reason inside the barrier job's timeout-minutes.
BARRIER_READS=${BARRIER_POLLS:-210}
RENUMBER_READS=${RENUMBER_POLLS:-90}
BARRIER_SECONDS=${BARRIER_SECONDS:-9000}
read_floor=0
spend_read() { # <what>: one read of the barrier's budget above read_floor, or die naming what it waited for
  [ "$BARRIER_READS" -gt "$read_floor" ] || die "gave up waiting for $1"
  BARRIER_READS=$((BARRIER_READS - 1))
}
poll() { # <what> <reads> <command...>: until the command prints 0 or "done" on <reads> consecutive reads, POLL_SECONDS apart
  local what=$1 reads=$2 got ok=0
  shift 2
  while spend_read "$what"; do
    got=$("$@") || die "could not read ${what}"
    case "$got" in
      0 | done)
        ok=$((ok + 1))
        [ "$ok" -lt "$reads" ] || return 0
        ;;
      *)
        ok=0
        echo "waiting for ${what} (${got})"
        ;;
    esac
    sleep "${POLL_SECONDS:-30}"
  done
}
# shellcheck disable=SC2329 # invoked through poll
run_done() {
  local s
  s=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}" --jq '.status + " " + (.conclusion // "")') || return 1
  case "$s" in "completed "*) echo 'done' ;; *) echo "$s" ;; esac
}
run_id=0 run_conclusion="" run_head=""
# API version 2026-03-10 always answers a dispatch with the id of the run it
# created (it has no return_run_details), so the wait follows that run and
# never one another actor dispatched beside it:
# https://docs.github.com/en/rest/actions/workflows?apiVersion=2026-03-10#create-a-workflow-dispatch-event
dispatch_wait() { # <what> [input=value...]: dispatches WORKFLOW on dev, waits for that run, sets run_id, run_conclusion and run_head
  local what=$1 kv s fields=()
  shift
  for kv in "$@"; do fields+=(-f "inputs[${kv%%=*}]=${kv#*=}"); done
  run_id=$(gh api -X POST -H 'X-GitHub-Api-Version: 2026-03-10' \
    "repos/${GITHUB_REPOSITORY}/actions/workflows/${WORKFLOW}/dispatches" -f ref=dev "${fields[@]}" \
    --jq '.workflow_run_id // ""') || die "could not dispatch the ${what} run"
  [[ $run_id =~ ^[1-9][0-9]*$ ]] || die "the ${what} dispatch named no run ('${run_id}'), so the stable publish stays held"
  echo "dispatched the ${what} run ${run_id}"
  poll "${what} run ${run_id}" 1 run_done
  s=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}" --jq '(.conclusion // "") + " " + (.head_sha // "")') \
    || die "could not read run ${run_id}"
  run_conclusion=${s%% *} run_head=${s#* }
}
# shellcheck disable=SC2329 # invoked through poll
head_unfinished() { # <commit> -> how many push release runs of the commit are unfinished
  gh api --paginate "$(runs_api "&head_sha=$1")" \
    --jq '.workflow_runs[] | select(.event == "push") | select(.status != "completed") | .id' | wc -l
}
# A queued renumber run replaces the dev run pending in their concurrency group
# and builds nothing, and a push run can still fail, so the hold releases only
# once a release run of dev's head succeeded: an unfinished push run is waited
# for, else a normal dev run (which republishes nothing already tagged) is
# dispatched and its own run waited for. A dev tag at the head proves one lane
# only. A newer queued run cancels the dispatched one, so a cancelled one is
# dispatched again for dev's head as it then stands. dev_moved is set when it waited.
settled="" dev_moved=""
settle_dev_head() {
  local head runs
  dev_moved=""
  while :; do
    git fetch --quiet --force --tags origin '+refs/heads/dev:refs/remotes/origin/dev' || die "cannot fetch dev"
    head=$(git rev-parse refs/remotes/origin/dev)
    [ "$head" != "$settled" ] || return 0
    runs=$(gh api --paginate "$(runs_api "&head_sha=${head}")" \
      --jq '.workflow_runs[] | select(.event == "push") | if .status != "completed" then "live" elif .conclusion == "success" then "ok" else "failed" end') \
      || die "could not list the dev release runs of ${head}. Rerun this run."
    if grep -qx ok <<<"$runs"; then return 0; fi
    dev_moved=1
    if grep -qx live <<<"$runs"; then
      poll "the push release run of dev's head ${head}" 1 head_unfinished "$head"
      continue
    fi
    note "dev's head ${head} has no push release run that succeeded or is still running, so a dev release of it was dispatched"
    dispatch_wait "dev release"
    case "$run_conclusion" in
      success)
        [[ $run_head =~ ^[0-9a-f]{40}$ ]] || die "dev release run ${run_id} names no commit it built, so the stable publish stays held"
        settled=$run_head
        ;;
      cancelled) echo "dev release run ${run_id} was cancelled; settling dev's head again" ;;
      *) die "dev release run ${run_id} of dev's head ${head} concluded '${run_conclusion}', so the stable publish stays held" ;;
    esac
  done
}

# Invariant: a stable release never outranks newer dev content, so nothing is
# tagged or pushed before every dev release run that started before main held
# R finished, a release run of dev's head succeeded, and dev ranks above the
# version published.
cmd_barrier() {
  local state="${STATE:?}" plan cutoff key r v c held need="" subrows="" subs s t st lacking waited="" recs
  : "${GITHUB_REPOSITORY:?}" "${WORKFLOW:?}" "${GITHUB_RUN_ID:?}"
  plan=$(jq -r --arg root "${ROOT_VERSION:-}" \
    'to_entries[] | select(.value.in_range) | [.key, .value.commit, (if .key == "." then $root else .value.version end)] | @tsv' <<<"$state")
  [ -n "$plan" ] || {
    echo "no pending promotion in range; nothing to hold"
    return 0
  }
  while IFS=$'\t' read -r key r v; do
    [ -n "$v" ] || die "lane ${key}: no version to hold the barrier for"
  done <<<"$plan"
  # R's committer date precedes the main ref update, so a dev run started in
  # between would slip past it. This run exists only once main holds R, so its
  # creation time is a bound at or after the update.
  cutoff=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}" --jq .created_at) \
    || die "could not read this run's creation time"
  [[ $cutoff =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
    || die "this run's creation time '${cutoff}' is not an RFC 3339 UTC timestamp"
  # Inclusive: created_at has second resolution, so a run created in the
  # cutoff's own second may have started before main held R.
  # Each status is a separate read, so they go in lifecycle order (a run that
  # moves forward between reads is met again later) and a zero must hold on
  # two polls (a run that moves back into waiting is met on the next).
  # shellcheck disable=SC2329 # invoked through poll
  pending_runs() { # [cutoff]: unset counts every unfinished run
    local s n=0 c sel=""
    [ -z "${1:-}" ] || sel=" | select(.created_at <= \"${1}\")"
    for s in requested pending waiting queued in_progress; do
      c=$(gh api --paginate "$(runs_api "&status=${s}")" \
        --jq ".workflow_runs[]${sel} | .id" | wc -l) || return 1
      n=$((n + c))
    done
    echo "$n"
  }
  read_floor=$RENUMBER_READS
  poll "dev release runs created before ${cutoff}" 2 pending_runs "$cutoff"
  git fetch --quiet --force --tags origin '+refs/heads/dev:refs/remotes/origin/dev' || die "cannot fetch dev"
  # A build published but untagged is invisible to the rank checks below, so
  # its run must finish first: every dev run, as a later one may own it.
  held=""
  while IFS=$'\t' read -r key r v; do
    c=$(untagged_past "$key" "$(git rev-parse "$r^2")") || die "lane ${key}: cannot tell whether dev holds a published, untagged build"
    [ -z "$c" ] || held="${held}${key} "
  done <<<"$plan"
  if [ -n "$held" ]; then
    poll "every dev release run, as dev holds a published build with no dev tag" 2 pending_runs
    git fetch --quiet --force --tags origin '+refs/heads/dev:refs/remotes/origin/dev' || die "cannot fetch dev"
    while IFS=$'\t' read -r key r v; do
      c=$(untagged_past "$key" "$(git rev-parse "$r^2")") || die "lane ${key}: cannot tell whether dev holds a published, untagged build"
      [ -z "$c" ] || die "lane ${key}: dev's build of ${c} is published with no dev tag and no newer build is tagged, so its version cannot rank above ${v}. Dispatch ${WORKFLOW} on dev to build and tag dev's head, then rerun this run."
    done <<<"$plan"
  fi
  # The rank checks below see a root dev tag only, and a dev run publishes a
  # subpackage after that tag: each subpackage the run publishes (every root
  # promotion's, as cmd_publish takes them) needs a dev build of its newest dev
  # change, ranking above the version when that change is past the newest T.
  # Renumber publishes one that is missing or below.
  recs=$(root_promotions "$state") || die "cannot list the root's promotions past its stable tag"
  subs='[]'
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    s=$(promoted_subpackages "$c") || die "cannot tell which subpackages ${c} publishes"
    subs=$(jq -c --argjson add "$s" 'reduce $add[] as $x (.; if any(.[]; . == $x) then . else . + [$x] end)' <<<"$subs")
  done <<<"$recs"
  if [ -n "$recs" ]; then
    t=$(git rev-parse "$(tail -n1 <<<"$recs")^2")
    while IFS= read -r s; do
      [ -z "$s" ] || subrows+="${s}"$'\t'"${t}"$'\t'"${ROOT_VERSION:-}"$'\n'
    done < <(jq -r '.[]' <<<"$subs")
  fi
  [ -z "$held" ] || waited=1
  lacking=$(subs_lacking "$subrows" incomplete)
  if [ -n "$lacking" ]; then
    poll "every dev release run, as a subpackage's newest dev change has no dev build on npm" 2 pending_runs
    git fetch --quiet --force --tags origin '+refs/heads/dev:refs/remotes/origin/dev' || die "cannot fetch dev"
    waited=1
  fi
  assess() { # sets need and lacking from dev as last fetched
    need=""
    lacking=$(subs_lacking "$subrows")
    while read -r s st c; do
      [ -n "$s" ] || continue
      note "subpackage ${s}: dev's change ${c} has no dev build on npm$([ "$st" = incomplete ] || echo " above the version")"
    done <<<"$lacking"
    while IFS=$'\t' read -r key r v; do
      if has_dev_above "$key" "$(core "$v")"; then
        echo "lane ${key}: dev already ranks above ${v}"
      elif built_past "$key" "$(git rev-parse "$r^2")"; then
        note "lane ${key}: dev holds a build past the promoted commit numbered below ${v}"
        need="${need}${key} "
      else
        echo "lane ${key}: dev holds no build past the promoted commit"
      fi
    done <<<"$plan"
  }
  assess
  # Queueing the renumber run cancels a dev run pending in its concurrency
  # group, so it is dispatched only once no dev run is unfinished; a run that
  # finished meanwhile may already rank dev above the version.
  while :; do
    if { [ -n "$need" ] || [ -n "$lacking" ]; } && [ -z "$waited" ]; then
      poll "every dev release run, before the renumber run joins their concurrency group" 2 pending_runs
      git fetch --quiet --force --tags origin '+refs/heads/dev:refs/remotes/origin/dev' || die "cannot fetch dev"
      assess
    fi
    if [ -n "$need" ] || [ -n "$lacking" ]; then break; fi
    settle_dev_head
    [ -n "$dev_moved" ] || return 0
    waited=""
    assess
  done
  read_floor=0
  dispatch_wait renumber mode=renumber
  case "$run_conclusion" in
    success) ;;
    # A dev run queued behind the renumber run replaced it in the group, and
    # numbers dev's head itself.
    cancelled) poll "every dev release run, as renumber run ${run_id} was cancelled" 2 pending_runs ;;
    *) die "renumber run ${run_id} concluded '${run_conclusion}', so the stable publish stays held" ;;
  esac
  # The renumber run's publish starts a propagation of its own.
  REGISTRY_WAITED=0
  settle_dev_head
  while IFS=$'\t' read -r key r v; do
    built_past "$key" "$(git rev-parse "$r^2")" || continue
    has_dev_above "$key" "$(core "$v")" || die "lane ${key}: renumber run ${run_id} left no dev build above ${v}"
    echo "lane ${key}: dev now ranks above ${v}"
  done <<<"$plan"
  # npm's packument can trail a publish by minutes. A subs_lacking that
  # cannot tell ends the wait, and the call after it dies saying so.
  subs_served() { [ -z "$(subs_lacking "$subrows")" ]; }
  await_registry "every subpackage's dev build on npm" subs_served || true
  lacking=$(subs_lacking "$subrows")
  while read -r s st c; do
    [ -n "$s" ] || continue
    die "subpackage ${s}: renumber run ${run_id} left dev's change ${c} with no dev build on npm$([ "$st" = incomplete ] || echo " above the version"), so the stable publish stays held"
  done <<<"$lacking"
}

# The commit a renumber run numbers and tags must be the same one, so this
# checks the lane's newest dev build out before the job computes its version.
# EXPECT_COMMIT: the build an earlier job numbered; a newer one refuses.
cmd_renumber_target() {
  local key="${LANE_KEY:?}" d_tag d
  d_tag=$(nearest_dev_tag HEAD "$key")
  if [ -z "$d_tag" ]; then
    note "lane ${key}: this branch has no dev build, so nothing is renumbered"
    out "commit="
    return 0
  fi
  d=$(git rev-list -n1 "$d_tag")
  [ -z "${EXPECT_COMMIT:-}" ] || [ "$d" = "$EXPECT_COMMIT" ] \
    || die "lane ${key}: the newest build is now ${d_tag} at ${d}, not the ${EXPECT_COMMIT} this run numbered. Rerun the renumber."
  git checkout --quiet --detach "$d"
  echo "lane ${key}: renumbering ${d_tag}'s build at ${d}"
  out "commit=${d}"
}

# A git tag cannot move an npm consumer: the package at the build is published
# under the new pre-release and `dev` dist-tag, and read back, before the tag.
# An existing version is accepted only when npm recorded it from this commit.
publish_npm_dev() { # <dev tag> <commit>
  local pkg ver="${1#v}" res code head
  pkg=$(jq -r .name package.json) || die "no package.json name to publish ${1} under"
  res=$(curl -sS --connect-timeout 10 --max-time 30 --retry 3 -w '\n%{http_code}' \
    "https://registry.npmjs.org/${pkg}/${ver}") || res=$'\n000'
  code=${res##*$'\n'}
  case "$code" in
    404)
      npm pkg set version="$ver"
      npm publish --access public --tag dev
      ;;
    200)
      head=$(jq -r '.gitHead // ""' <<<"${res%$'\n'*}") || head=""
      [ "$head" = "$2" ] \
        || die "npm ${pkg}@${ver} is already published from '${head:-an unrecorded commit}', not ${2}. A dev release run published it without tagging it, so rerun that run instead of renumbering over it."
      note "npm ${pkg}@${ver} already published from ${2}"
      ;;
    *) die "npm cannot tell whether ${pkg}@${ver} exists (HTTP ${code}), so ${1} is not published or tagged" ;;
  esac
  answers "npm ${pkg}@${ver}" fetch "https://registry.npmjs.org/${pkg}/${ver}" \
    || die "npm ${pkg}@${ver} does not read back, so ${1} is not tagged"
}

renumber_build() { # <key> <new dev version> -> the build at HEAD to renumber, or nothing when it already ranks there
  local key=$1 new=$2 d_tag d
  [ "${CHANNEL:-}" = dev ] || die "renumber runs on the dev channel only"
  [[ $new =~ $(dev_re "$key") ]] || die "'${new}' is not a dev version of lane ${key}"
  d_tag=$(nearest_dev_tag HEAD "$key")
  if [ -z "$d_tag" ]; then
    note "lane ${key}: this branch has no dev build, so nothing is renumbered"
    return 0
  fi
  d=$(git rev-list -n1 "$d_tag")
  [ "$d" = "$(git rev-parse HEAD)" ] \
    || die "lane ${key}: ${new} was computed at HEAD, but the newest build is ${d_tag} at ${d}. Check that build out first."
  if ! gt "$(core "$new")" "$(core "$d_tag")"; then
    note "lane ${key}: ${d_tag} already ranks at or above ${new}, so nothing is renumbered"
    return 0
  fi
  printf '%s\n' "$d"
}

# The job that ran the cliff config holds no write scope, so it hands the
# version and build to the job that tags: through job outputs for the TS
# root, through an artifact per matrix leg.
cmd_renumber_handoff() {
  local key="${LANE_KEY:?}" new="${DEV_VERSION:?}" d
  d=$(renumber_build "$key" "$new") || exit 1
  [ -n "$d" ] || return 0
  echo "lane ${key}: ${new} for the build at ${d} goes to the tagging job"
  out "version=${new}" "commit=${d}"
}

# A handoff was written by a job that ran the repository's config, so it is
# read as data and refused unless it is a version and a full commit id.
cmd_handoff_read() { # <file>
  local v c
  v=$(jq -er '.version | strings' "$1") || die "handoff ${1} carries no version"
  c=$(jq -er '.commit | strings' "$1") || die "handoff ${1} carries no commit"
  [ -z "$c" ] || [[ $c =~ ^[0-9a-f]{40}$ ]] || die "handoff commit '${c}' is not a commit id"
  [ -z "$v" ] || [[ $v =~ ^[A-Za-z0-9._/-]+$ ]] || die "handoff version '${v}' is not a version"
  [ -z "$c" ] || [ -n "$v" ] || die "handoff names the build ${c} with no version"
  out "version=${v}" "commit=${c}"
}

handoff_name() { # <prefix> <id> -> an artifact name unique per id, which may hold a slash
  printf '%s-%s\n' "$1" "$(printf '%s' "$2" | sha256sum | cut -c1-16)"
}

# After the root renumber, at the build renumber-target checked out: each
# subpackage the barrier finds without a dev build, or below the pending
# version, is published from this build under its newest dev tag.
cmd_renumber_subpackages() {
  local state="${STATE:?}" d_tag r v t="" s st c dirs='[]'
  [ "${CHANNEL:-}" = dev ] || die "renumber runs on the dev channel only"
  d_tag=$(nearest_dev_tag HEAD .)
  [ -n "$d_tag" ] && [ "$(git rev-list -n1 "$d_tag")" = "$(git rev-parse HEAD)" ] \
    || die "HEAD is not the root's newest dev build. Check that build out with renumber-target first."
  r=$(jq -r '.["."].commit // ""' <<<"$state")
  v=$(jq -r '.["."].version // ""' <<<"$state")
  [ -z "$r" ] || t=$(git rev-parse "$r^2")
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    st=$(sub_dev_state "$s" "$t" "${v:-v0.0.0}") || die "subpackage ${s}: cannot tell whether npm serves its newest dev build"
    [ "$st" != ok ] || continue
    c=${st#* }
    if ! git merge-base --is-ancestor "$c" HEAD; then
      note "subpackage ${s}: dev's change ${c} is newer than the build ${d_tag}, so that change's own dev run publishes it"
      continue
    fi
    dirs=$(jq -c --arg s "$s" '. + [$s]' <<<"$dirs")
    echo "subpackage ${s}: publishing the build ${d_tag} for dev's change ${c}"
  done < <(jq -r '.[]' <<<"$(subpackages_at HEAD)")
  out "version=${d_tag}" "dirs=${dirs}"
}

cmd_renumber() {
  local key="${LANE_KEY:?}" new="${DEV_VERSION:?}" repo="${GITHUB_REPOSITORY:?}" d digest served meta got existing created=false
  d=$(renumber_build "$key" "$new") || exit 1
  [ -n "$d" ] || return 0
  if [ "$key" = . ] && [ "${REPO_TYPE:-}" = docker ]; then
    digest=$(ghcr_digest "${IMAGE:?}" "sha-${d}")
    [ -n "$digest" ] || die "ghcr.io/${IMAGE}:sha-${d} does not exist, and renumber re-tags an existing build but never builds one"
    # Dev publication pushes the version tag before the git tag, so a version
    # tag serving another digest belongs to a newer build and must not move.
    served=$(ghcr_state "$IMAGE" "$new") || die "cannot tell whether ghcr.io/${IMAGE}:${new} exists, so it is not written"
    if [ "$served" = "$digest" ]; then
      note "ghcr.io/${IMAGE}:${new} already serves the build at ${d}"
    elif [ "$served" != absent ]; then
      die "ghcr.io/${IMAGE}:${new} already serves ${served}, not the build at ${d} (${digest}). A dev release run published it without tagging it, so rerun that run instead of renumbering over it."
    else
      meta=$(mktemp "${RUNNER_TEMP:-/tmp}/renumber-meta.XXXXXX")
      docker buildx imagetools create --metadata-file "$meta" -t "ghcr.io/${IMAGE}:${new}" "ghcr.io/${IMAGE}@${digest}"
      got=$(jq -r '."containerimage.descriptor".digest' "$meta")
      rm -f "$meta"
      [ "$got" = "$digest" ] || die "re-tagging ${digest} as ${new} changed the digest to ${got}"
    fi
  fi
  if [ "$key" = . ] && [ "${REPO_TYPE:-}" = ts ]; then
    publish_npm_dev "$new" "$d"
  fi
  if existing=$(gh api "repos/${repo}/git/ref/tags/${new}" --jq .object.sha 2>/dev/null); then
    [ "$existing" = "$d" ] || die "tag ${new} exists at ${existing}, not ${d}, so it is not moved"
  else
    gh api "repos/${repo}/git/refs" -f ref="refs/tags/${new}" -f sha="$d" >/dev/null
    created=true
  fi
  if ! gh api -X POST "repos/${repo}/statuses/${d}" -f state=success -f context="${TAG_RECEIPT_PREFIX}${new}" \
    -f description="created by the dev renumber run" \
    -f target_url="${GITHUB_SERVER_URL:-https://github.com}/${repo}/actions/runs/${GITHUB_RUN_ID:-0}" >/dev/null; then
    if [ "$created" = true ]; then
      gh api -X DELETE "repos/${repo}/git/refs/tags/${new}" >/dev/null
    fi
    die "receipt for ${new} not recorded"
  fi
  echo "lane ${key}: the build at ${d} is now also ${new}"
}

case "${1:-}" in
  pending | publish | number | receipts | renumber) "cmd_$1" ;;
  barrier)
    rc=0
    timeout --kill-after=10 "$BARRIER_SECONDS" "$BASH" "${BASH_SOURCE[0]}" barrier-wait || rc=$?
    case "$rc" in
      0) ;;
      124 | 137) die "gave up waiting, because the barrier passed its ${BARRIER_SECONDS} s deadline. The stable publish stays held." ;;
      *) exit "$rc" ;;
    esac
    ;;
  barrier-wait) cmd_barrier ;;
  renumber-target) cmd_renumber_target ;;
  renumber-handoff) cmd_renumber_handoff ;;
  renumber-subpackages) cmd_renumber_subpackages ;;
  handoff-read) cmd_handoff_read "${2:?usage: release-state.sh handoff-read <file>}" ;;
  handoff-name) handoff_name "${2:?usage: release-state.sh handoff-name <prefix> <id>}" "${3:?usage: release-state.sh handoff-name <prefix> <id>}" ;;
  signing-window) signing_window "${2:?usage: release-state.sh signing-window <tag>}" ;;
  release-digest) release_digest "${2:?usage: release-state.sh release-digest <tag>}" ;;
  security-shas) security_shas "${2:-}" ;;
  receipt | readback | provenance | complete)
    cmd="cmd_$1"
    shift
    "$cmd" "$@"
    ;;
  *) die "usage: release-state.sh pending|publish|number|receipts|receipt|readback|provenance|complete|barrier|renumber-target|renumber-handoff|renumber-subpackages|handoff-read|handoff-name|renumber|signing-window|release-digest|security-shas" ;;
esac
