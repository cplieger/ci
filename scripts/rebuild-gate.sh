#!/usr/bin/env bash
# The publish gate of a rebuild-only image release, for docker-release.yaml.
#   scope    env CHANNEL VERSION PLATFORMS SUBPACKAGES_JSON GO_LANES_JSON CI_TOOLS
#            GITHUB_EVENT_NAME, in the checkout of HEAD; prints rebuild_only=true|false and compare_tag=
#            (the release the rebuild is compared with, empty unless rebuild_only is
#            true). A read that fails prints rebuild_only=false with a warning.
#   compare <new> <compared>
#            (the SBOMs of the rebuilt image and of the image at PREV_REF)
#            env COMPARE_TAG PREV_REF UNREADABLE CI_TOOLS GITHUB_OUTPUT
#            GITHUB_STEP_SUMMARY; writes skip=true only when the package sets are
#            equal, skip=false for any other outcome.
# Both exit 2 on a usage error or an unset CI_TOOLS, scope also on a CHANNEL other than
# dev or stable, and 0 otherwise. Siblings are reached through CI_TOOLS alone.
set -euo pipefail

# shellcheck source=SCRIPTDIR/gha-log.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/gha-log.sh"

usage() {
  echo "usage: rebuild-gate.sh scope | compare NEW COMPARED" >&2
  exit 2
}

need_tools() {
  [ -n "${CI_TOOLS:-}" ] || {
    echo "::error::rebuild-gate: CI_TOOLS is required" >&2
    exit 2
  }
}

result() { # <true|false> [tag]
  echo "rebuild_only=$1"
  if [ "$1" = true ]; then
    echo "compare_tag=$2"
  else
    echo "compare_tag="
  fi
  exit 0
}
publishes() { # <warning|notice> <format> <arg>...: gha_annotate's arguments
  gha_annotate "$@" >&2
  result false
}

scope() {
  local tag tags tag_commit rc ps_out ps_err reason sig p amd64=false recs
  local -a platforms
  need_tools
  case "${CHANNEL:-}" in
    dev | stable) ;;
    *)
      gha_annotate error "rebuild-gate: CHANNEL must be dev or stable, got '%s'" "${CHANNEL:-}" >&2
      exit 2
      ;;
  esac
  # shellcheck source=SCRIPTDIR/reconciliation.sh
  . "$CI_TOOLS/reconciliation.sh"

  [ "${GITHUB_EVENT_NAME:-}" = push ] || result false
  if [ "$CHANNEL" = stable ]; then
    # The notes diff against this same tag, so the rebuild is compared with the
    # release its notes would follow.
    tag=$(bash "$CI_TOOLS/render-notes.sh" --site docker --version "${VERSION:-}" --print-previous) \
      || publishes warning 'The release before %s could not be resolved, so this release publishes as usual.' "${VERSION:-}"
  else
    tags=$(git tag --list --sort=-version:refname) \
      || publishes warning "The dev tags could not be listed, so this release publishes as usual."
    tag=$(grep -m1 -E '^v[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+$' <<<"$tags" || true)
  fi
  [ -n "$tag" ] || result false
  tag_commit=$(git rev-parse --verify --quiet "${tag}^{commit}") \
    || publishes warning 'The commit of %s could not be read, so this release publishes as usual.' "${tag}"
  rc=0
  git merge-base --is-ancestor "$tag_commit" HEAD || rc=$?
  case "$rc" in
    0) ;;
    1) result false ;;
    *) publishes warning 'Whether %s is an ancestor of this commit could not be read, so this release publishes as usual.' "${tag}" ;;
  esac

  # Paths mode notes what it drops on stderr, which only a failure needs.
  ps_err=$(mktemp)
  # shellcheck disable=SC2064 # the path is fixed now
  trap "rm -f '$ps_err'" EXIT
  rc=0
  ps_out=$(MODE=paths FROM="$tag_commit" TO=HEAD REQUEST_CHANNEL="$CHANNEL" REPO_TYPE=docker SUBPACKAGES_JSON="${SUBPACKAGES_JSON:-[]}" \
    GO_LANES_JSON="${GO_LANES_JSON:-[]}" bash "$CI_TOOLS/path-significance.sh" 2>"$ps_err") || rc=$?
  if [ "$rc" -ne 0 ]; then
    gha_annotate warning 'The paths changed since %s could not be classified, so this release publishes as usual.' "${tag}" >&2
    reason=$(tail -n 1 "$ps_err" | sed 's/^::[a-z]*:://')
    [ -z "$reason" ] || gha_say '%s' "$reason" >&2
    result false
  fi
  [ -n "$(sed -n 's/^rebuild_request=//p' <<<"$ps_out")" ] || result false

  # The candidate is built, and the compared image scanned, for linux/amd64 alone.
  IFS=',' read -ra platforms <<<"${PLATFORMS:-}"
  for p in "${platforms[@]}"; do
    if [ "$(xargs <<<"$p")" = linux/amd64 ]; then
      amd64=true
    fi
  done
  [ "$amd64" = true ] || publishes notice "This image is not built for linux/amd64, so its rebuild publishes as usual."
  if [ "$CHANNEL" = stable ]; then
    recs=$(reconciliations_in "$tag_commit" HEAD) \
      || publishes warning 'The promotions since %s could not be listed, so this release publishes as usual.' "${tag}"
    [ -z "$recs" ] || publishes notice "A promotion is in this range beside its rebuild request, so it publishes as usual."
  fi
  sig=$(sed -n 's/^significant=//p' <<<"$ps_out")
  [ "$sig" = '[]' ] || publishes notice "This range ships changes beside its rebuild request, so it publishes as usual."
  gha_annotate notice 'This range only asks for a rebuild against refreshed base packages, so its image is compared with %s before anything is published.' "${tag}" >&2
  result true "$tag"
}

decide() { # <true|false> <kind> <format> <arg>...: gha_annotate's arguments after the first
  echo "skip=$1" >>"$GITHUB_OUTPUT"
  shift
  gha_annotate "$@" >&2
  exit 0
}

compare() {
  local new prev count noun=packages
  need_tools
  [ "$#" -eq 2 ] || usage
  new=$1 prev=$2
  [ -n "${PREV_REF:-}" ] \
    || decide false warning 'Publishing this rebuild, because %s.' "${UNREADABLE:-the SBOM of ${COMPARE_TAG:-} could not be read}"
  [ -s "$prev" ] \
    || decide false warning 'Publishing this rebuild, because the image of %s could not be scanned.' "${COMPARE_TAG:-}"
  [ -s "$new" ] || decide false warning "Publishing this rebuild, because the rebuilt image's SBOM could not be generated."
  # A count that is not a number would fail the -gt test below, which reads as equal sets.
  if ! count=$(bash "$CI_TOOLS/render-notes.sh" --sbom-changes --sbom-prev "$prev" --sbom-new "$new") \
    || ! [[ $count =~ ^[0-9]+$ ]]; then
    decide false warning 'Publishing this rebuild, because its SBOM and that of %s could not be compared.' "${COMPARE_TAG:-}"
  fi
  if [ "$count" -gt 0 ]; then
    [ "$count" -ne 1 ] || noun=package
    decide false notice '%s %s changed since %s, so this rebuild is published.' "${count}" "${noun}" "${COMPARE_TAG:-}"
  fi
  {
    echo "## Rebuild"
    echo ""
    echo "Rebuild skipped: no package changed since \`${COMPARE_TAG:-}\` (\`${PREV_REF}\`), so the image built from source for the comparison is discarded and nothing is published."
  } >>"$GITHUB_STEP_SUMMARY"
  decide true notice 'No package changed since %s, so this rebuild publishes nothing.' "${COMPARE_TAG:-}"
}

case "${1:-}" in
  scope)
    [ "$#" -eq 1 ] || usage
    scope
    ;;
  compare)
    shift
    compare "$@"
    ;;
  *) usage ;;
esac
