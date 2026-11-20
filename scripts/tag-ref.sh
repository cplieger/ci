#!/usr/bin/env bash
# Tag creation for the release workflows; source it after retry.sh (it sets no
# shell options).
# shellcheck disable=SC2034 # TAG_CREATED, CLAIMED and CLAIM_AT are the caller's to read

# release_channels.HANDOFF_PREFIX reads this context.
TAG_HANDOFF_PREFIX='release/handoff/'
# claim_held posts it and claim_unused reads it.
TAG_CLAIM_PREFIX='release/claim/'
# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

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
  gha_annotate error 'Tag %s exists at %s, not %s. A published version never moves, so nothing is published under it.' "$2" "${TAG_ELSEWHERE}" "$3"
  return 1
}
tag_ref_once() { # <owner/repo> <tag> <commit>
  local at
  if at=$(gh api "repos/$1/git/ref/tags/$2" --jq .object.sha 2>/dev/null); then
    if [ "$at" = "$3" ]; then
      gha_annotate notice 'Tag %s already exists at %s, so it is reused' "$2" "$3"
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
      gha_annotate warning 'tag %s not created: %s advanced past %s (now %s), and a superseded run loses ref-create. The run on the new head releases this range.' "$3" "$2" "$4" "${head}"
      handoff_record "$1" "$3" "$4" "the run on ${head}"
      return 2
      ;;
    1) gha_annotate error "tag create for %s failed while this run is still %s's head. It is not a superseded run, so check the token's permissions and the API." "$3" "$2" ;;
    *) gha_annotate error "tag create for %s failed and %s's head is unreadable, so a handoff cannot be confirmed." "$3" "$2" ;;
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
    || gha_annotate warning 'handoff record for %s not posted on %s, so only this log names %s as what releases it.' "$2" "$3" "$4"
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
      gha_annotate error 'git cannot tell whether a later dev tag on %s holds %s, so %s is not claimed.' "$2" "$4" "$3"
      return 1
    fi
    if [ -n "$later" ]; then
      gha_annotate notice '%s at %s already ships the changes of %s, so nothing is published under %s.' "${later% *}" "${later#* }" "$4" "$3"
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

# claim_held <owner/repo> <branch> <tag> <commit> <channel>: a publishing
# job's check of the claim job's claim, never a create (one sent after a
# newer push is refused). With <tag> at <commit>: on dev, when a later dev
# tag of the lane supersedes <tag> (a failed-jobs rerun keeps the claim job's
# first answer), hands off with claimed=false; else posts release/claim/<tag>
# on <commit> and writes claimed=true. Returns 1 when the tag is elsewhere,
# absent or unreadable, git cannot tell, or the status is not posted.
claim_held() {
  local later
  if ! retry claim_read "$1" "$3"; then
    gha_annotate error "tag %s cannot be read, so this run's claim of it at %s is unconfirmed and nothing is published under it." "$3" "$4"
    return 1
  fi
  if [ -z "$CLAIM_AT" ]; then
    gha_annotate error "tag %s does not exist, so nothing is published under it. An attempt that ended before its first write deleted this run's claim: rerun all jobs so the claim job claims it again." "$3"
    return 1
  fi
  if [ "$CLAIM_AT" != "$4" ]; then
    gha_annotate error "tag %s is at %s, not this run's commit %s, so nothing is published under it." "$3" "${CLAIM_AT}" "$4"
    return 1
  fi
  if [ "$5" = dev ]; then
    if ! later=$(later_dev_tag "$2" "$3" "$4"); then
      gha_annotate error 'git cannot tell whether a later dev tag on %s holds %s, so nothing is published under %s.' "$2" "$4" "$3"
      return 1
    fi
    if [ -n "$later" ]; then
      gha_annotate notice '%s at %s already ships the changes of %s, so nothing is published under %s.' "${later% *}" "${later#* }" "$4" "$3"
      handoff_record "$1" "$3" "$4" "${later% *} at ${later#* }"
      echo "claimed=false" >>"${GITHUB_OUTPUT:?}"
      return 0
    fi
  fi
  if ! retry gh api -X POST "repos/$1/statuses/$4" -f state=success -f context="${TAG_CLAIM_PREFIX}$3" \
    -f description="the release run writes under this claim" \
    -f target_url="${GITHUB_SERVER_URL:-https://github.com}/$1/actions/runs/${GITHUB_RUN_ID:-0}" >/dev/null; then
    gha_annotate error "the %s%s status was not posted on %s, so nothing is published under %s, which would let the run's end delete it." "${TAG_CLAIM_PREFIX}" "$3" "$4" "$3"
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
    gha_annotate error 'tag %s cannot be read, so whether this run left an unused claim of it at %s is unknown.' "$2" "$3"
    return 1
  fi
  if [ "$CLAIM_AT" != "$3" ]; then
    gha_annotate notice 'tag %s is not at %s, so this run holds no claim of it.' "$2" "$3"
    return 0
  fi
  if ! contexts=$(retry gh api --paginate "repos/$1/commits/$3/statuses?per_page=100" --jq '.[] | select(.state == "success") | .context'); then
    gha_annotate error 'the statuses of %s cannot be read, so tag %s is kept and may be an unused claim: delete refs/tags/%s when no release run wrote under it.' "$3" "$2" "$2"
    return 1
  fi
  if grep -qxF "${TAG_CLAIM_PREFIX}$2" <<<"$contexts"; then
    gha_annotate notice 'a release run wrote under tag %s at %s, so the tag stays.' "$2" "$3"
    return 0
  fi
  if ! retry gh api -X DELETE "repos/$1/git/refs/tags/$2" >/dev/null; then
    gha_annotate error 'tag %s at %s has nothing written under it, and GitHub refused its delete. Delete refs/tags/%s by hand: until then it stands as an unproven claim of a version never published.' "$2" "$3" "$2"
    return 1
  fi
  gha_annotate notice "nothing was written under tag %s, so this run's claim of it is deleted." "$2"
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
  gha_annotate warning '%s is not tagged at %s and %s advanced past it (now %s), so the run on the new head releases it.' "$3" "$4" "$2" "${head}"
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
  gha_annotate warning '%s at %s was handed off to %s at %s, which ships its changes.' "$3" "$4" "${later% *}" "${later#* }"
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
  gha_annotate warning "%s is not published at %s: npm's dev dist-tag already names %s, and %s at %s holds %s." "$(jq -r .name "$6")" "$3" "${dev}" "${later% *}" "${later#* }" "$4"
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
