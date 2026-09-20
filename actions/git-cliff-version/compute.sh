#!/usr/bin/env bash
# Version arithmetic for the two release channels. Inputs arrive as environment
# variables (CHANNEL, EXCLUDE_PATHS, LANE, CLIFF_BIN); every result is appended
# to $GITHUB_OUTPUT. Callers: actions/git-cliff-version/action.yml and
# scripts/test-cliff-bump-semantics.sh.
set -euo pipefail

CHANNEL="${CHANNEL:-stable}"
EXCLUDE_PATHS="${EXCLUDE_PATHS:-}"
LANE="${LANE:-}"
CLIFF_BIN="${CLIFF_BIN:-git-cliff}"

case "$CHANNEL" in
  dev | stable) ;;
  *)
    echo "::error::channel must be 'dev' or 'stable', got '${CHANNEL}'"
    exit 1
    ;;
esac

# The tag universe is stable versions only, anchored at both ends: a lane tag
# (yamlenv/v1.0.0) and any pre-release (v1.3.0-dev.7, v1.3.0-rc.1) must stay
# invisible to the version base, or the next stable version would be computed
# from them. The same exact regex filters the anchor tags below.
if [ -n "$LANE" ]; then
  esc=$(printf '%s' "$LANE" | sed -e 's/[][\.|(){}?+*^$]/\\&/g')
  PATTERN="^${esc}/v[0-9]+\\.[0-9]+\\.[0-9]+\$"
  PREFIX="${LANE}/"
  ARGS=(--tag-pattern "$PATTERN" --include-path "${LANE}/**")
  # The synced config's initial_tag is root-shaped and would fail the pattern.
  export GIT_CLIFF__BUMP__INITIAL_TAG="${LANE}/v1.0.0"
else
  PATTERN='^v[0-9]+\.[0-9]+\.[0-9]+$'
  PREFIX=""
  ARGS=(--tag-pattern "$PATTERN")
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    ARGS+=(--exclude-path "$p")
  done <<<"$EXCLUDE_PATHS"
fi

# The dev counter reads existing tags, so a shallow or stale tag list would
# reuse a taken number.
git fetch --tags --quiet origin 2>/dev/null || true

err="$(mktemp)"
trap 'rm -f "$err"' EXIT
# --unreleased anchors the base at the newest matching tag; without it a tag
# whose whole commit window is filtered out vanishes from cliff's release model
# and the base regresses to an older tag (verified on git-cliff 2.13.1).
if ! BASE="$("$CLIFF_BIN" --unreleased --bumped-version "${ARGS[@]}" 2>"$err")"; then
  echo "::error::git-cliff --bumped-version failed:"
  sed 's/^/  /' "$err" >&2 || true
  exit 1
fi
if [ -z "$BASE" ]; then
  echo "::error::git-cliff returned an empty version with no error"
  exit 1
fi
case "$BASE" in
  "${PREFIX}"v[0-9]*) ;;
  *)
    echo "::error::computed version '${BASE}' is not of the form ${PREFIX}vX.Y.Z; refusing"
    exit 1
    ;;
esac

nearest_tag() { # <regex> -> the tag matching it on the commit nearest HEAD, or nothing
  local -A tag_at=()
  local name obj peeled commit
  while read -r name obj peeled; do
    [[ $name =~ $1 ]] || continue
    tag_at["${peeled:-$obj}"]="$name"
  done < <(git for-each-ref refs/tags --format='%(refname:strip=2) %(objectname) %(*objectname)')
  [ "${#tag_at[@]}" -gt 0 ] || return 0
  while read -r commit; do
    if [ -n "${tag_at[$commit]:-}" ]; then
      printf '%s' "${tag_at[$commit]}"
      return 0
    fi
  done < <(git rev-list --topo-order HEAD)
}

DEV_PATTERN="${PATTERN%\$}-dev\\.[0-9]+\$"
LATEST="$(nearest_tag "$PATTERN")"
if [ "$CHANNEL" = dev ]; then
  ANCHOR_REF="$(nearest_tag "${PATTERN}|${DEV_PATTERN}")"
else
  ANCHOR_REF="$LATEST"
fi
ANCHOR_SHA=""
if [ -n "$ANCHOR_REF" ]; then
  ANCHOR_SHA="$(git rev-list -n1 "$ANCHOR_REF")"
fi

patch_bump() { # vX.Y.Z (with optional lane prefix) -> vX.Y.(Z+1)
  local bare="${1#"$PREFIX"v}" major minor patch
  IFS=. read -r major minor patch <<<"$bare"
  printf '%sv%s.%s.%s' "$PREFIX" "$major" "$minor" "$((patch + 1))"
}
dev_version() { # base -> base-dev.<count of existing base-dev.* tags + 1>
  local base_re n
  base_re="$(printf '%s' "$1" | sed -e 's/[][\.|(){}?+*^$]/\\&/g')"
  n="$(git tag --list "$1-dev.*" | grep -cE "^${base_re}-dev\.[0-9]+\$" || true)"
  printf '%s-dev.%s' "$1" "$((n + 1))"
}

FLOOR_BASE=""
FLOOR_DEV_VERSION=""
if [ -n "$LATEST" ]; then
  FLOOR_BASE="$(patch_bump "$LATEST")"
  FLOOR_DEV_VERSION="$(dev_version "$FLOOR_BASE")"
fi
DEV_VERSION="$(dev_version "$BASE")"
if [ "$BASE" = "$LATEST" ]; then
  RELEASE=false
else
  RELEASE=true
fi

{
  echo "base=${BASE}"
  echo "dev_version=${DEV_VERSION}"
  echo "floor_base=${FLOOR_BASE}"
  echo "floor_dev_version=${FLOOR_DEV_VERSION}"
  echo "latest=${LATEST}"
  echo "anchor_sha=${ANCHOR_SHA}"
  echo "version=${BASE}"
  echo "release=${RELEASE}"
} >>"$GITHUB_OUTPUT"
echo "Computed version: ${BASE} (dev: ${DEV_VERSION}; latest stable: ${LATEST:-<none>}; anchor: ${ANCHOR_SHA:-<none>}; channel: ${CHANNEL})"
