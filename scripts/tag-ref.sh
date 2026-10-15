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
