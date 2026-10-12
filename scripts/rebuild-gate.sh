#!/usr/bin/env bash
# The publish gate of a rebuild-only image release, for docker-release.yaml.
#   scope    env CHANNEL VERSION PLATFORMS SUBPACKAGES_JSON GO_LANES_JSON CI_TOOLS
#            GITHUB_EVENT_NAME, in the checkout of HEAD; prints rebuild_only=true|false and compare_tag=
#            (the release the rebuild is compared with, empty unless rebuild_only is
#            true). A read that fails prints rebuild_only=false with a warning.
#   compare <new> <attested> <scanned>
#            env COMPARE_TAG PREV_REF UNREADABLE SBOM_FROM CI_TOOLS GITHUB_OUTPUT
#            GITHUB_STEP_SUMMARY; writes skip=true only when the package sets are
#            equal, skip=false for any other outcome.
# Both exit 2 on a usage error or an unset CI_TOOLS, scope also on a CHANNEL other than
# dev or stable, and 0 otherwise. Siblings are reached through CI_TOOLS alone.
set -euo pipefail

usage() {
  echo "usage: rebuild-gate.sh scope | compare NEW ATTESTED SCANNED" >&2
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
publishes() { # <warning|notice> <text>
  echo "::$1::$2" >&2
  result false
}

scope() {
  local tag tags tag_commit rc ps_out ps_err reason sig p amd64=false recs
  local -a platforms
  need_tools
  case "${CHANNEL:-}" in
    dev | stable) ;;
    *)
      echo "::error::rebuild-gate: CHANNEL must be dev or stable, got '${CHANNEL:-}'" >&2
      exit 2
      ;;
  esac
  # shellcheck source=SCRIPTDIR/reconciliation.sh
  . "$CI_TOOLS/reconciliation.sh"

  [ "${GITHUB_EVENT_NAME:-}" = push ] || result false
  if [ "$CHANNEL" = stable ]; then
    # The notes diff against this same tag, so a skip is a release whose System
    # packages block would be empty.
    tag=$(bash "$CI_TOOLS/render-notes.sh" --site docker --version "${VERSION:-}" --print-previous) \
      || publishes warning "The release before ${VERSION:-} could not be resolved, so this release publishes as usual."
  else
    tags=$(git tag --list --sort=-version:refname) \
      || publishes warning "The dev tags could not be listed, so this release publishes as usual."
    tag=$(grep -m1 -E '^v[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+$' <<<"$tags" || true)
  fi
  [ -n "$tag" ] || result false
  tag_commit=$(git rev-parse --verify --quiet "${tag}^{commit}") \
    || publishes warning "The commit of ${tag} could not be read, so this release publishes as usual."
  rc=0
  git merge-base --is-ancestor "$tag_commit" HEAD || rc=$?
  case "$rc" in
    0) ;;
    1) result false ;;
    *) publishes warning "Whether ${tag} is an ancestor of this commit could not be read, so this release publishes as usual." ;;
  esac

  # Paths mode notes what it drops on stderr, which only a failure needs.
  ps_err=$(mktemp)
  # shellcheck disable=SC2064 # the path is fixed now
  trap "rm -f '$ps_err'" EXIT
  rc=0
  ps_out=$(MODE=paths FROM="$tag_commit" TO=HEAD REQUEST_CHANNEL="$CHANNEL" REPO_TYPE=docker SUBPACKAGES_JSON="${SUBPACKAGES_JSON:-[]}" \
    GO_LANES_JSON="${GO_LANES_JSON:-[]}" bash "$CI_TOOLS/path-significance.sh" 2>"$ps_err") || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "::warning::The paths changed since ${tag} could not be classified, so this release publishes as usual." >&2
    reason=$(tail -n 1 "$ps_err" | sed 's/^::[a-z]*:://')
    [ -z "$reason" ] || printf '%s\n' "$reason" >&2
    result false
  fi
  [ -n "$(sed -n 's/^rebuild_request=//p' <<<"$ps_out")" ] || result false

  # The attested SBOM inventories the amd64 image alone.
  IFS=',' read -ra platforms <<<"${PLATFORMS:-}"
  for p in "${platforms[@]}"; do
    if [ "$(xargs <<<"$p")" = linux/amd64 ]; then
      amd64=true
    fi
  done
  [ "$amd64" = true ] || publishes notice "This image is not built for linux/amd64, so its rebuild publishes as usual."
  if [ "$CHANNEL" = stable ]; then
    recs=$(reconciliations_in "$tag_commit" HEAD) \
      || publishes warning "The promotions since ${tag} could not be listed, so this release publishes as usual."
    [ -z "$recs" ] || publishes notice "A promotion is in this range beside its rebuild request, so it publishes as usual."
  fi
  sig=$(sed -n 's/^significant=//p' <<<"$ps_out")
  [ "$sig" = '[]' ] || publishes notice "This range ships changes beside its rebuild request, so it publishes as usual."
  echo "::notice::This range only asks for a rebuild against refreshed base packages, so its image is compared with ${tag} before anything is published." >&2
  result true "$tag"
}

decide() { # <true|false> <annotation>
  echo "skip=$1" >>"$GITHUB_OUTPUT"
  echo "$2" >&2
  exit 0
}

compare() {
  local new prev count noun=packages
  need_tools
  [ "$#" -eq 3 ] || usage
  new=$1 prev=$2
  [ "${SBOM_FROM:-image}" != path ] || prev=$3
  [ -n "${PREV_REF:-}" ] \
    || decide false "::warning::Publishing this rebuild, because ${UNREADABLE:-the SBOM of ${COMPARE_TAG:-} could not be read}."
  [ -s "$prev" ] \
    || decide false "::warning::Publishing this rebuild, because no image SBOM of ${COMPARE_TAG:-} could be generated."
  [ -s "$new" ] || decide false "::warning::Publishing this rebuild, because the rebuilt image's SBOM could not be generated."
  # A count that is not a number would fail the -gt test below, which reads as equal sets.
  if ! count=$(bash "$CI_TOOLS/render-notes.sh" --sbom-changes --sbom-prev "$prev" --sbom-new "$new") \
    || ! [[ $count =~ ^[0-9]+$ ]]; then
    decide false "::warning::Publishing this rebuild, because its SBOM and that of ${COMPARE_TAG:-} could not be compared."
  fi
  if [ "$count" -gt 0 ]; then
    [ "$count" -ne 1 ] || noun=package
    decide false "::notice::${count} ${noun} changed since ${COMPARE_TAG:-}, so this rebuild is published."
  fi
  {
    echo "## Rebuild"
    echo ""
    echo "Rebuild skipped: no package changed since \`${COMPARE_TAG:-}\` (\`${PREV_REF}\`), so the image built from source for the comparison is discarded and nothing is published."
  } >>"$GITHUB_STEP_SUMMARY"
  decide true "::notice::No package changed since ${COMPARE_TAG:-}, so this rebuild publishes nothing."
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
