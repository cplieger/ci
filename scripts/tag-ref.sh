#!/usr/bin/env bash
# Tag creation for the release workflows; source it after retry.sh (it sets no
# shell options).
# shellcheck disable=SC2034 # TAG_CREATED and CLAIMED are the caller's to read

# release_channels.HANDOFF_PREFIX reads this context.
TAG_HANDOFF_PREFIX='release/handoff/'

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

# later_dev_tag <branch> <tag> <commit>: prints "<tag> <commit>" of the newest
# dev tag of <tag>'s lane (its dir/ prefix, none for the root), other than
# <tag>, on a descendant of <commit> that origin/<branch> holds, nothing when
# none; returns 1 when git cannot tell. It reads the job's checkout
# (fetch-depth 0) unfetched: only release runs, queued one at a time per
# branch, create a dev tag, and a lane's only from its own job.
later_dev_tag() {
  local -A at=()
  local name obj peeled refs revs c lane=""
  [[ $2 != */* ]] || lane=${2%/*}/
  refs=$(git for-each-ref refs/tags --format='%(refname:strip=2) %(objectname) %(*objectname)') || return 1
  while read -r name obj peeled; do
    if [[ $name == "$lane"* && ${name#"$lane"} =~ ^v[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+$ ]] && [ "$name" != "$2" ]; then
      at[${peeled:-$obj}]=$name
    fi
  done <<<"$refs"
  revs=$(git rev-list --ancestry-path "$3..refs/remotes/origin/$1") || return 1
  while read -r c; do
    if [ -n "$c" ] && [ -n "${at[$c]:-}" ]; then
      printf '%s %s\n' "${at[$c]}" "$c"
      return 0
    fi
  done <<<"$revs"
}

# claim_version <owner/repo> <branch> <tag> <commit> <channel>: every release
# tag's claim, before the version's first write. Sets CLAIMED, and claimed= in
# GITHUB_OUTPUT: true once <tag> is at <commit>, false on a handoff; returns 1
# when the version cannot be claimed. On dev, a later dev tag of the lane on a
# descendant already ships this commit's changes, so the claim hands off
# before any create: a new tag would rank this older tree above it, and a
# reused one would move the dev channel back.
claim_version() {
  local rc=0 later
  CLAIMED=false TAG_CREATED=false
  if [ "$5" = dev ]; then
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

# root_handed_off <owner/repo> <branch> <version> <commit> <channel>: for a job
# after the root's claim, 0 when that claim handed <version> off (warned
# here): no tag at <commit> and the branch moved past it, or a later dev tag
# over the one at <commit>. 1 when the root claimed it at <commit>, 2 when
# that cannot be told.
root_handed_off() {
  local head later rc=0
  if [ "$(git rev-list -n1 "$3" 2>/dev/null || true)" != "$4" ]; then
    head=$(branch_moved_past "$1" "$2" "$4") || rc=$?
    [ "$rc" -eq 0 ] || return "$rc"
    echo "::warning::$3 is not tagged at $4 and $2 advanced past it (now ${head}), so the run on the new head releases it."
    return 0
  fi
  [ "$5" = dev ] || return 1
  later=$(later_dev_tag "$2" "$3" "$4") || return 2
  [ -n "$later" ] || return 1
  echo "::warning::$3 at $4 was handed off to ${later% *} at ${later#* }, which ships its changes."
  return 0
}
