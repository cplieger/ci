#!/usr/bin/env bash
# Tag creation for the release workflows; source it after retry.sh (it sets no
# shell options).
# shellcheck disable=SC2034 # TAG_CREATED is the caller's to read

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
  echo "::error::Tag $2 exists at ${TAG_ELSEWHERE}, not $3, so it is not moved."
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

# tag_ref_or_handoff <owner/repo> <branch> <tag> <commit> [<run head>]: tag_ref
# for a release run on <branch> whose head is <run head> (default <commit>).
# Once a push that edits workflow files lands past the run's head, GitHub
# refuses the integration token's ref create (ui#42, deadset#281, toolbelt#506),
# and runs queue per ref, so the run on the new head releases this range.
# Returns 0 when the tag is at <commit>, 2 when the create failed after <branch>
# moved past the run's head (a handoff, warned), 1 otherwise: the tag sits
# elsewhere, or the create failed while the run's head is still the branch's,
# or the head cannot be read.
tag_ref_or_handoff() {
  local rc=0 head mrc=0 run=${5-$4}
  tag_ref "$1" "$3" "$4" || rc=$?
  [ "$rc" -eq 2 ] || return "$rc"
  if [ -n "$run" ]; then
    head=$(branch_moved_past "$1" "$2" "$run") || mrc=$?
  else
    mrc=2
  fi
  case "$mrc" in
    0)
      echo "::warning::tag $3 not created: $2 advanced past ${run} (now ${head}), and a superseded run loses ref-create. The run on the new head releases this range."
      return 2
      ;;
    1) echo "::error::tag create for $3 failed while this run is still $2's head. It is not a superseded run, so check the token's permissions and the API." ;;
    *) echo "::error::tag create for $3 failed and $2's head is unreadable, so a handoff cannot be confirmed." ;;
  esac
  return 1
}
