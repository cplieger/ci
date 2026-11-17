#!/usr/bin/env bash
# Tag creation for the release workflows; source it after retry.sh (it sets no
# shell options).
# shellcheck disable=SC2034 # TAG_CREATED, CLAIMED and CLAIM_AT are the caller's to read

# release_channels.HANDOFF_PREFIX reads this context.
TAG_HANDOFF_PREFIX='release/handoff/'
# claim_held posts it and claim_unused reads it.
TAG_CLAIM_PREFIX='release/claim/'

# tag_ref <owner/repo> <tag> <commit>: makes refs/tags/<tag> point at <commit>
# through retry, reading the tag again before each attempt: a release run's lanes
# create tags in one repository at once, and GitHub has refused concurrent writes
# to one repository (#845) and ref creates with a bare 422
# (github/rest-api-description#4887). Returns 0 once the tag is at <commit>, with
# TAG_CREATED=true only when a create this call sent was accepted; 1 when the tag
# is at another commit, which is never moved; 2 when every attempt failed.
tag_ref() {
  TAG_CREATED=false TAG_ELSEWHERE=""
  retry tag_ref_once "$1" "$2" "$3" || return 2
  [ -n "$TAG_ELSEWHERE" ] || return 0
  echo "::error::Tag $2 exists at ${TAG_ELSEWHERE}, not $3. A published version never moves, so nothing is published under it."
  return 1
}
tag_ref_once() { # <owner/repo> <tag> <commit>
  local at
  if at=$(gh api "repos/$1/git/ref/tags/$2" --jq .object.sha 2>/dev/null); then
    if [ "$at" = "$3" ]; then
      echo "::notice::Tag $2 already exists at $3, so it is reused"
    else
      TAG_ELSEWHERE=$at
    fi
    return 0
  fi
  gh api "repos/$1/git/refs" -f ref="refs/tags/$2" -f sha="$3" >/dev/null || return 1
  TAG_CREATED=true
}

# branch_moved_past <owner/repo> <branch> <commit>: prints the branch's head and
# returns 0 when it is not <commit>, 1 when it is; prints nothing and returns 2
# when the head cannot be read.
branch_moved_past() {
  local head
  head=$(gh api "repos/$1/git/ref/heads/$2" --jq .object.sha 2>/dev/null) || return 2
  [ -n "$head" ] || return 2
  printf '%s' "$head"
  [ "$head" != "$3" ]
}

# tag_ref_or_handoff <owner/repo> <branch> <tag> <commit>: tag_ref for a release run on <branch>.
# Once a push that edits workflow files lands past <commit>, GitHub refuses the
# integration token's ref create (ui#42, deadset#281, toolbelt#506), and runs
# queue per ref, so the run on the new head releases this range under its own
# claim. Returns 0 when the tag is at <commit>, 2 when the create failed after
# <branch> moved on (a handoff, warned and recorded on <commit>), 1 otherwise:
# the tag sits elsewhere, or the create failed while <commit> is still the head
# or the head cannot be read.
tag_ref_or_handoff() {
  local rc=0 head mrc=0
  tag_ref "$1" "$3" "$4" || rc=$?
  [ "$rc" -eq 2 ] || return "$rc"
  head=$(branch_moved_past "$1" "$2" "$4") || mrc=$?
  case "$mrc" in
    0)
      echo "::warning::tag $3 not created: $2 advanced past $4 (now ${head}), and a superseded run loses ref-create. The run on the new head releases this range."
      handoff_record "$1" "$3" "$4" "the run on ${head}"
      return 2
      ;;
    1) echo "::error::tag create for $3 failed while this run is still $2's head. It is not a superseded run, so check the token's permissions and the API." ;;
    *) echo "::error::tag create for $3 failed and $2's head is unreadable, so a handoff cannot be confirmed." ;;
  esac
  return 1
}

# handoff_record <owner/repo> <tag> <commit> <what releases it>: the
# release/handoff/<tag> status on the handed-off <commit>, naming the run or the
# later version that releases its changes. Best effort: the handoff stands
# without it.
handoff_record() {
  gh api -X POST "repos/$1/statuses/$3" -f state=success -f context="${TAG_HANDOFF_PREFIX}$2" \
    -f description="$4 releases this commit's changes" \
    -f target_url="${GITHUB_SERVER_URL:-https://github.com}/$1/actions/runs/${GITHUB_RUN_ID:-0}" >/dev/null \
    || echo "::warning::handoff record for $2 not posted on $3, so only this log names $4 as what releases it."
}

# later_dev_tag <branch> <tag> <commit>: prints "<tag> <commit>" of a dev tag
# of <tag>'s lane (its dir/ prefix, none for the root) that supersedes <tag>:
# one ranking above it on <commit> itself (a renumbered build), else the
# newest on a descendant of <commit> that origin/<branch> holds; nothing when
# none, 1 when git cannot tell. It reads the job's checkout
# (fetch-depth 0) unfetched: only release runs, queued one at a time per
# branch, create a dev tag, and a lane's only from its own job.
later_dev_tag() {
  local -A at=()
  local name obj peeled refs revs c lane=""
  [[ $2 != */* ]] || lane=${2%/*}/
  # Version order, so a commit carrying two of the lane's dev tags (a renumbered
  # build) maps to the higher one.
  refs=$(git for-each-ref --sort=v:refname refs/tags --format='%(refname:strip=2) %(objectname) %(*objectname)') || return 1
  while read -r name obj peeled; do
    if [[ $name == "$lane"* && ${name#"$lane"} =~ ^v[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+$ ]] && [ "$name" != "$2" ]; then
      at[${peeled:-$obj}]=$name
    fi
  done <<<"$refs"
  c=$(git rev-parse --verify -q "$3^{commit}") || return 1
  if [ -n "${at[$c]:-}" ] && [ "$(printf '%s\n%s\n' "${2#"$lane"}" "${at[$c]#"$lane"}" | sort -V | tail -n1)" != "${2#"$lane"}" ]; then
    printf '%s %s\n' "${at[$c]}" "$c"
    return 0
  fi
  revs=$(git rev-list --ancestry-path "$3..refs/remotes/origin/$1") || return 1
  while read -r c; do
    if [ -n "$c" ] && [ -n "${at[$c]:-}" ]; then
      printf '%s %s\n' "${at[$c]}" "$c"
      return 0
    fi
  done <<<"$revs"
}

# claim_version <owner/repo> <branch> <tag> <commit> <channel>: a release job's
# claim, before the version's first write. Sets CLAIMED, and claimed= in
# GITHUB_OUTPUT: true once <tag> is at <commit>, false on a handoff; returns 1
# when the version cannot be claimed. On dev, a dev tag of the lane that
# supersedes <tag> (later_dev_tag) already ships this commit's changes, so the
# claim hands off before any create: a new tag would rank this older tree
# above it, and a reused one would move the dev channel back. <tag> held at
# another commit is refused whatever later tag exists.
claim_version() {
  local rc=0 later at
  CLAIMED=false TAG_CREATED=false
  at=$(git rev-list -n1 "$3" 2>/dev/null || true)
  if [ "$5" = dev ] && { [ -z "$at" ] || [ "$at" = "$4" ]; }; then
    if ! later=$(later_dev_tag "$2" "$3" "$4"); then
      echo "::error::git cannot tell whether a later dev tag on $2 holds $4, so $3 is not claimed."
      return 1
    fi
    if [ -n "$later" ]; then
      echo "::notice::${later% *} at ${later#* } already ships the changes of $4, so nothing is published under $3."
      handoff_record "$1" "$3" "$4" "${later% *} at ${later#* }"
      echo "claimed=false" >>"${GITHUB_OUTPUT:?}"
      return 0
    fi
  fi
  tag_ref_or_handoff "$1" "$2" "$3" "$4" || rc=$?
  case "$rc" in
    0) CLAIMED=true ;;
    2) ;;
    *) return 1 ;;
  esac
  echo "claimed=$CLAIMED" >>"${GITHUB_OUTPUT:?}"
}

# claim_held <owner/repo> <tag> <commit>: a publishing job's check of the claim
# job's claim_version, which it never repeats: a ref create it sent after a
# newer push would be refused. When <tag> is at <commit>, posts the
# release/claim/<tag> status on <commit> (claim_unused keeps the tag once it
# exists), writes claimed=true in GITHUB_OUTPUT and returns 0; returns 1 when
# the tag is elsewhere, absent or unreadable, or the status is not posted, so
# nothing is published under it.
claim_held() {
  if ! retry claim_read "$1" "$2"; then
    echo "::error::tag $2 cannot be read, so this run's claim of it at $3 is unconfirmed and nothing is published under it."
    return 1
  fi
  if [ -z "$CLAIM_AT" ]; then
    echo "::error::tag $2 does not exist, so nothing is published under it. An attempt that ended before its first write deleted this run's claim: rerun all jobs so the claim job claims it again."
    return 1
  fi
  if [ "$CLAIM_AT" != "$3" ]; then
    echo "::error::tag $2 is at ${CLAIM_AT}, not this run's commit $3, so nothing is published under it."
    return 1
  fi
  if ! retry gh api -X POST "repos/$1/statuses/$3" -f state=success -f context="${TAG_CLAIM_PREFIX}$2" \
    -f description="the release run writes under this claim" \
    -f target_url="${GITHUB_SERVER_URL:-https://github.com}/$1/actions/runs/${GITHUB_RUN_ID:-0}" >/dev/null; then
    echo "::error::the ${TAG_CLAIM_PREFIX}$2 status was not posted on $3, so nothing is published under $2, which would let the run's end delete it."
    return 1
  fi
  echo "claimed=true" >>"${GITHUB_OUTPUT:?}"
}

# claim_unused <owner/repo> <tag> <commit>: at a run's end, deletes <tag> when
# it is at <commit> with no release/claim/<tag> status there, since then no
# write ran under it (claim_held posts the status first). A tag at <commit>
# for a version the run releases is never proven: a proven one is an anchor,
# and a run never releases its anchor's commit. Returns 0 when the tag is
# absent, elsewhere, kept or deleted; 1 when GitHub cannot read or delete it.
claim_unused() {
  local contexts
  if ! retry claim_read "$1" "$2"; then
    echo "::error::tag $2 cannot be read, so whether this run left an unused claim of it at $3 is unknown."
    return 1
  fi
  if [ "$CLAIM_AT" != "$3" ]; then
    echo "::notice::tag $2 is not at $3, so this run holds no claim of it."
    return 0
  fi
  if ! contexts=$(retry gh api --paginate "repos/$1/commits/$3/statuses?per_page=100" --jq '.[] | select(.state == "success") | .context'); then
    echo "::error::the statuses of $3 cannot be read, so tag $2 is kept and may be an unused claim: delete refs/tags/$2 when no release run wrote under it."
    return 1
  fi
  if grep -qxF "${TAG_CLAIM_PREFIX}$2" <<<"$contexts"; then
    echo "::notice::a release run wrote under tag $2 at $3, so the tag stays."
    return 0
  fi
  if ! retry gh api -X DELETE "repos/$1/git/refs/tags/$2" >/dev/null; then
    echo "::error::tag $2 at $3 has nothing written under it, and GitHub refused its delete. Delete refs/tags/$2 by hand, or a later stable run stops at it."
    return 1
  fi
  echo "::notice::nothing was written under tag $2, so this run's claim of it is deleted."
}

# claim_read <owner/repo> <tag>: CLAIM_AT is the tag's commit, empty when
# GitHub answers 404; returns 1 on any other failure, said on stderr.
claim_read() {
  local err rc=0
  err=$(mktemp)
  CLAIM_AT=$(gh api "repos/$1/git/ref/tags/$2" --jq .object.sha 2>"$err") || rc=$?
  if [ "$rc" -ne 0 ]; then
    CLAIM_AT=""
    grep -q 'HTTP 404' "$err" && rc=0
    [ "$rc" -eq 0 ] || cat "$err" >&2
  fi
  rm -f "$err"
  return "$rc"
}

# untagged_handed_off <owner/repo> <branch> <tag> <commit>: for <tag> not at
# <commit>, 0 when the branch moved past it (warned here), so the run on the
# new head releases it; 1 when <commit> is still the head, 2 when that cannot
# be told.
untagged_handed_off() {
  local head rc=0
  head=$(branch_moved_past "$1" "$2" "$4") || rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  echo "::warning::$3 is not tagged at $4 and $2 advanced past it (now ${head}), so the run on the new head releases it."
}

# root_handed_off <owner/repo> <branch> <version> <commit> <channel>: for a job
# after the root's claim, 0 when that claim handed <version> off (warned
# here), as claim_version decides: untagged, or a later dev tag over the one
# at <commit>. 1 when the root claimed it at <commit>, 2 when that cannot be
# told.
root_handed_off() {
  local later
  if [ "$(git rev-list -n1 "$3" 2>/dev/null || true)" != "$4" ]; then
    untagged_handed_off "$1" "$2" "$3" "$4"
    return
  fi
  [ "$5" = dev ] || return 1
  later=$(later_dev_tag "$2" "$3" "$4") || return 2
  [ -n "$later" ] || return 1
  echo "::warning::$3 at $4 was handed off to ${later% *} at ${later#* }, which ships its changes."
  return 0
}

# package_handed_off <owner/repo> <branch> <tag> <commit> <channel> <package.json>:
# for a package published at <tag>'s version after its root or lane claim, 0
# when it is not published here (warned): <tag> handed off untagged, or on
# dev a later dev tag of the lane exists and npm's dev dist-tag ranks above
# the version. A later tag alone is not enough, since its range starts at
# <tag>; the dev channel is linear, so an npm dev build above the version
# comes from a descendant. 1 when it is published here, 2 when that cannot be
# told.
package_handed_off() {
  local later dev ver=${3##*/}
  if [ "$(git rev-list -n1 "$3" 2>/dev/null || true)" != "$4" ]; then
    untagged_handed_off "$1" "$2" "$3" "$4"
    return
  fi
  [ "$5" = dev ] || return 1
  later=$(later_dev_tag "$2" "$3" "$4") || return 2
  [ -n "$later" ] || return 1
  dev=$(npm_dev_above "$6" "${ver#v}") || return
  echo "::warning::$(jq -r .name "$6") is not published at $3: npm's dev dist-tag already names ${dev}, and ${later% *} at ${later#* } holds $4."
}

# npm_dev_above <package.json> <X.Y.Z-dev.N>: prints npm's dev dist-tag of the
# package and returns 0 when it ranks above the version; 1 when it is absent
# or not above it, or the package is private (a dry run, on no registry); 2
# when npm cannot be read. Equal is this version's own build, which a publish
# skips as published and verify-publish reads back.
npm_dev_above() {
  local pkg body code dev=""
  [ "$(jq -r '.private // false' "$1" 2>/dev/null)" != true ] || return 1
  pkg=$(jq -er .name "$1") || return 2
  body=$(mktemp)
  # npm's CDN serves the packument up to 300 s stale (cache-control max-age=300)
  # and ignores a no-cache request header; a unique query string misses it.
  code=$(curl -sS --retry 3 --connect-timeout 10 --max-time 30 -H 'Accept: application/vnd.npm.install-v1+json' \
    -o "$body" -w '%{http_code}' "https://registry.npmjs.org/${pkg}?t=${EPOCHREALTIME//[!0-9]/}") || code=000
  if [ "$code" = 200 ]; then
    dev=$(jq -r '."dist-tags".dev // ""' "$body") || code=000
  fi
  rm -f "$body"
  case "$code" in
    200 | 404) ;;
    *) return 2 ;;
  esac
  [ -n "$dev" ] && [ "$dev" != "$2" ] && [ "$(printf '%s\n%s\n' "$2" "$dev" | sort -V | tail -n1)" = "$dev" ] || return 1
  printf '%s' "$dev"
}
