#!/usr/bin/env bash
# Version arithmetic for the release channels, configured by environment
# (CHANNEL, EXCLUDE_PATHS, LANE, CLIFF_BIN, PENDING_VERSION, PENDING_IN_RANGE);
# results go to $GITHUB_OUTPUT.
# `compute.sh pending-version <commit> [lane]` prints a pending promotion's
# version instead. Callers: action.yml, scripts/release-state.sh,
# test-cliff-bump-semantics.sh.
set -euo pipefail

PENDING_OF=""
case "${1:-}" in
  "") ;;
  pending-version)
    PENDING_OF="${2:-}"
    [ -n "$PENDING_OF" ] || {
      echo "::error::usage: compute.sh pending-version <commit> [lane]"
      exit 1
    }
    LANE="${3:-${LANE:-}}"
    ;;
  *)
    echo "::error::unknown compute.sh command '$1'"
    exit 1
    ;;
esac

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

# ── Every version below matches PATTERN ─────────────────────────────────────
semver() { # <version> -> "X Y Z" in base 10
  local bare="${1#"$PREFIX"v}" major minor patch
  IFS=. read -r major minor patch <<<"$bare"
  printf '%s %s %s' "$((10#$major))" "$((10#$minor))" "$((10#$patch))"
}
version_gt() { # <a> <b> -> status 0 when a ranks above b
  local -a a b
  local i
  read -ra a <<<"$(semver "$1")"
  read -ra b <<<"$(semver "$2")"
  for i in 0 1 2; do
    if [ "${a[i]}" -ne "${b[i]}" ]; then
      [ "${a[i]}" -gt "${b[i]}" ]
      return
    fi
  done
  return 1
}
raise() { # <version> <major|minor> -> the next version of that kind
  local -a v
  read -ra v <<<"$(semver "$1")"
  if [ "$2" = major ]; then
    printf '%sv%s.0.0' "$PREFIX" "$((v[0] + 1))"
  else
    printf '%sv%s.%s.0' "$PREFIX" "${v[0]}" "$((v[1] + 1))"
  fi
}
level_between() { # <from> <to> -> the largest component <to> raises over <from>
  local -a a b
  read -ra a <<<"$(semver "$1")"
  read -ra b <<<"$(semver "$2")"
  if [ "${b[0]}" -gt "${a[0]}" ]; then
    echo major
  elif [ "${b[0]}" -lt "${a[0]}" ]; then
    echo none
  elif [ "${b[1]}" -gt "${a[1]}" ]; then
    echo minor
  elif [ "${b[1]}" -eq "${a[1]}" ] && [ "${b[2]}" -gt "${a[2]}" ]; then
    echo patch
  else
    echo none
  fi
}
stable_tags() { # -> "<tag> <commit>" per stable tag of the lane, all refs
  local name obj peeled
  while read -r name obj peeled; do
    [[ $name =~ $PATTERN ]] || continue
    printf '%s %s\n' "$name" "${peeled:-$obj}"
  done < <(git for-each-ref refs/tags --format='%(refname:strip=2) %(objectname) %(*objectname)')
}
tags_in_range() { # <from> <to> -> "<tag> " per stable tag of the lane on a commit of from..to
  local -A in_range=()
  local c name commit
  while IFS= read -r c; do
    in_range["$c"]=1
  done < <(git rev-list "$1..$2")
  while read -r name commit; do
    [ -z "${in_range[$commit]:-}" ] || printf '%s ' "$name"
  done < <(stable_tags)
}
# git-cliff supplies the change level only, read as its proposal against its
# own base under the repo's [bump] rules (a 0.x breaking change is a minor on
# cliff-alpha). An empty <from> means no stable tag, so git-cliff reads <to>'s
# whole history and answers its initial_tag. Invariant: no stable tag sits
# inside a range (main's rise along its chain, dev commits carry none);
# git-cliff would split there, so refuse.
cliff_level() { # <from> <to> -> sets LEVEL and CLIFF_VERSION (its proposal)
  local ctx fields prev ver tagged
  local -a range
  # git-cliff takes a single revision as a commit id only.
  range=("$(git rev-parse --verify "$2^{commit}")")
  if [ -n "$1" ]; then
    range=("$1..$2")
    tagged="$(tags_in_range "$1" "$2")"
    if [ -n "$tagged" ]; then
      echo "::error::stable tags ${tagged}sit inside ${range[*]}, so no change level is read across them"
      exit 1
    fi
  fi
  if ! ctx="$("$CLIFF_BIN" --bump --context "${ARGS[@]}" "${range[@]}" 2>"$err")"; then
    echo "::error::git-cliff --bump --context failed over ${range[*]}:"
    sed 's/^/  /' "$err" >&2 || true
    exit 1
  fi
  # '|' rather than a tab: read strips a leading empty field at a whitespace IFS.
  fields="$(jq -r 'if length == 1 then [(.[0].previous.version // ""), (.[0].version // "")] | join("|") else "releases \(length)" end' <<<"$ctx")"
  case "$fields" in
    releases*)
      echo "::error::git-cliff returned ${fields#releases } releases over ${range[*]}, expected one"
      exit 1
      ;;
  esac
  IFS='|' read -r prev ver <<<"$fields"
  if ! [[ $ver =~ $PATTERN ]] || { [ -n "$prev" ] && ! [[ $prev =~ $PATTERN ]]; }; then
    echo "::error::git-cliff proposed '${ver}' over '${prev:-<none>}', which is not of the form ${PREFIX}vX.Y.Z, so it is refused"
    exit 1
  fi
  CLIFF_VERSION="$ver"
  LEVEL=none
  if [ -n "$prev" ]; then
    LEVEL="$(level_between "$prev" "$ver")"
  fi
}
promotion_version() { # <H> <level> -> the next major for a breaking range, else the next minor
  if [ "$2" = major ]; then
    raise "$1" major
  else
    raise "$1" minor
  fi
}
newer_on_chain() { # <ref> <a> <b> -> whichever of a, b comes first on ref's first-parent chain
  local c
  if [ -z "$2" ] || [ -z "$3" ]; then
    printf '%s' "${2:-$3}"
    return 0
  fi
  while IFS= read -r c; do
    case "$c" in
      "$2" | "$3")
        printf '%s' "$c"
        return 0
        ;;
    esac
  done < <(git rev-list --first-parent "$1")
  printf '%s' "$2"
}
# Fixed once: the lowest stable tag on <R> or a descendant published the
# promotion, receipt or not; otherwise the stable run's arithmetic over P..R,
# P being the highest stable tag on any other commit.
pending_version_of() { # <R> -> the version the pending promotion publishes
  local r="$1" name commit lowest="" p="" p_commit=""
  while read -r name commit; do
    if git merge-base --is-ancestor "$r" "$commit"; then
      if [ -z "$lowest" ] || version_gt "$lowest" "$name"; then
        lowest="$name"
      fi
    elif [ -z "$p" ] || version_gt "$name" "$p"; then
      p="$name"
      p_commit="$commit"
    fi
  done < <(stable_tags)
  if [ -n "$lowest" ]; then
    printf '%s\n' "$lowest"
    return 0
  fi
  cliff_level "$p_commit" "$r"
  if [ -n "$p" ]; then
    printf '%s\n' "$(promotion_version "$p" "$LEVEL")"
  else
    printf '%s\n' "$CLIFF_VERSION"
  fi
}

# shellcheck source=SCRIPTDIR/../../scripts/reconciliation.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/../../scripts/reconciliation.sh"

if [ -n "$PENDING_OF" ]; then
  if ! is_reconciliation "$PENDING_OF"; then
    echo "::error::'${PENDING_OF}' is not a reconciliation commit, so it is not numbered as a promotion"
    exit 1
  fi
  pending_version_of "$(git rev-parse "$PENDING_OF^{commit}")"
  exit 0
fi

DEV_PATTERN="${PATTERN%\$}-dev\\.[0-9]+\$"
REACHABLE_STABLE="$(nearest_tag "$PATTERN")"
if [ "$CHANNEL" = dev ]; then
  ANCHOR_REF="$(nearest_tag "${PATTERN}|${DEV_PATTERN}")"
else
  ANCHOR_REF="$REACHABLE_STABLE"
fi
ANCHOR_SHA=""
if [ -n "$ANCHOR_REF" ]; then
  ANCHOR_SHA="$(git rev-list -n1 "$ANCHOR_REF")"
fi

PENDING_VERSION="${PENDING_VERSION:-}"
PENDING_IN_RANGE="${PENDING_IN_RANGE:-false}"
case "$PENDING_IN_RANGE" in
  false) ;;
  true)
    if [ "$CHANNEL" != stable ]; then
      echo "::error::pending-in-range describes a stable run's range, but this run is on the ${CHANNEL} channel"
      exit 1
    fi
    ;;
  *)
    echo "::error::pending-in-range must be 'true' or 'false', got '${PENDING_IN_RANGE}'"
    exit 1
    ;;
esac
if [ -n "$PENDING_VERSION" ] && ! [[ $PENDING_VERSION =~ $PATTERN ]]; then
  echo "::error::pending-version '${PENDING_VERSION}' is not of the form ${PREFIX}vX.Y.Z"
  exit 1
fi

HEAD_SHA="$(git rev-parse HEAD)"
H_TAG="" H_COMMIT="" HEAD_TAG=""
while read -r name commit; do
  if [ -z "$H_TAG" ] || version_gt "$name" "$H_TAG"; then
    H_TAG="$name"
    H_COMMIT="$commit"
  fi
  if [ "$commit" = "$HEAD_SHA" ] && { [ -z "$HEAD_TAG" ] || version_gt "$name" "$HEAD_TAG"; }; then
    HEAD_TAG="$name"
  fi
done < <(stable_tags)
# Every stable tag sits on main's history, so a stable HEAD that does not
# contain H is an older commit (a re-run after a newer release): numbering it
# above H would publish older content as the newest version.
if [ "$CHANNEL" = stable ] && [ -n "$H_COMMIT" ] && ! git merge-base --is-ancestor "$H_COMMIT" HEAD; then
  echo "::error::the highest stable tag ${H_TAG} is on ${H_COMMIT}, which HEAD does not contain, so an older commit is not published above it"
  exit 1
fi
H="$H_TAG"
if [ "$CHANNEL" = dev ] && [ -n "$PENDING_VERSION" ] && { [ -z "$H" ] || version_gt "$PENDING_VERSION" "$H"; }; then
  H="$PENDING_VERSION"
fi

# A stable run at a tagged commit is the repair of that release: same version.
if [ "$CHANNEL" = stable ] && [ -n "$HEAD_TAG" ]; then
  RANGE_FROM=""
  LEVEL=none
  BASE="$HEAD_TAG"
else
  if [ "$PENDING_IN_RANGE" = true ]; then
    RANGE_FROM="$H_COMMIT"
  else
    if [ "$CHANNEL" = dev ]; then
      MAIN_REF=refs/remotes/origin/main
      if ! git rev-parse --verify --quiet "${MAIN_REF}^{commit}" >/dev/null; then
        echo "::error::a two-branch dev run needs ${MAIN_REF} to exclude promoted commits. Check out with fetch-depth: 0."
        exit 1
      fi
    else
      MAIN_REF=HEAD
    fi
    # A promotion's commits are counted by the stable run that publishes it,
    # never again by the runs after it.
    RANGE_FROM="$(newer_on_chain "$MAIN_REF" "$H_COMMIT" "$(newest_reconciliation "$MAIN_REF")")"
  fi
  cliff_level "$RANGE_FROM" HEAD
  if [ -z "$H" ]; then
    BASE="$CLIFF_VERSION"
  elif [ "$CHANNEL" = stable ] && [ "$PENDING_IN_RANGE" = false ]; then
    BASE="$(patch_bump "$H")"
  else
    BASE="$(promotion_version "$H" "$LEVEL")"
  fi
fi
LATEST="$H_TAG"
case "$BASE" in
  "${PREFIX}"v[0-9]*) ;;
  *)
    echo "::error::computed version '${BASE}' is not of the form ${PREFIX}vX.Y.Z; refusing"
    exit 1
    ;;
esac

DEV_VERSION="$(dev_version "$BASE")"
if [ "$BASE" = "$LATEST" ]; then
  RELEASE=false
else
  RELEASE=true
fi

{
  echo "base=${BASE}"
  echo "dev_version=${DEV_VERSION}"
  echo "latest=${LATEST}"
  echo "anchor_sha=${ANCHOR_SHA}"
  echo "version=${BASE}"
  echo "release=${RELEASE}"
  echo "h_tag=${H_TAG}"
  echo "range_from=${RANGE_FROM}"
  echo "change_level=${LEVEL}"
} >>"$GITHUB_OUTPUT"
echo "Computed version: ${BASE} (dev: ${DEV_VERSION}; H: ${H:-<none>}; highest stable tag: ${H_TAG:-<none>}; range: ${RANGE_FROM:-<root>}..HEAD; level: ${LEVEL}; channel: ${CHANNEL})"
