#!/usr/bin/env bash
# The assets of an image's GitHub Release, gathered in one directory: the SBOM
# and each TREE_FILES file the tagged tree holds, each with a cosign bundle beside
# it, and each tree file also with its .sha256 checksum.
#   stage <tree-ish> <dir>  writes each TREE_FILES file the tree holds into an existing <dir>, with its checksum; reads git in the working directory
#   sign <tag> <dir>        writes <file>.sigstore.json beside the SBOM and each staged file
#   names <tag> <dir>       prints the Release's asset names, one per line; fails naming the first file <dir> lacks or holds empty
#   check <tag> <name...>   fails naming the first name GITHUB_REPOSITORY's published Release of <tag> does not list
#   cleanup-drafts <tag>    deletes every draft Release of <tag> in GITHUB_REPOSITORY, before that Release is created
set -euo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/retry.sh
. "$TOOLS/retry.sh"

SBOM=sbom.spdx.json
# A file's asset name is its basename, so no two paths here may share one.
TREE_FILES=(grafana-dashboard.json docs/openapi.json)

die() {
  echo "::error::$*" >&2
  exit 1
}

cmd_stage() { # <tree-ish> <dir>
  local tree=$1 dir=$2 path name entry
  for path in "${TREE_FILES[@]}"; do
    entry=$(git ls-tree --full-tree "$tree" -- "$path") || die "git cannot read ${path} at ${tree}"
    [ -n "$entry" ] || continue
    name=${path##*/}
    # A symlink's blob is its target's path, which would ship as the asset.
    case "$entry" in
      100644\ * | 100755\ *) ;;
      *) die "${path} at ${tree} is not a regular file, so ${name} cannot be published from it." ;;
    esac
    git cat-file blob "${tree}:${path}" >"$dir/$name" || die "git cannot read ${path} at ${tree}"
    (cd "$dir" && sha256sum "$name" >"$name.sha256")
  done
}

cmd_sign() { # <tag> <dir>
  local tag=$1 dir=$2 path name
  local files=("$SBOM")
  [ -s "$dir/$SBOM" ] || die "${SBOM} is missing. ${tag} is not published without it."
  for path in "${TREE_FILES[@]}"; do
    name=${path##*/}
    [ ! -e "$dir/$name" ] || files+=("$name")
  done
  cd "$dir"
  for name in "${files[@]}"; do
    # cosign v3 requires --bundle: its one .sigstore.json carries the
    # signature, certificate and Rekor inclusion proof
    # (https://github.com/sigstore/cosign/blob/main/CHANGELOG.md, v3.0.0).
    retry cosign sign-blob --yes "$name" --bundle "$name.sigstore.json" \
      || die "${name}.sigstore.json is missing, because cosign could not sign ${name}. ${tag} is not published without it."
  done
}

cmd_names() { # <tag> <dir>
  local tag=$1 dir=$2 path name f
  local names=("$SBOM" "$SBOM.sigstore.json")
  for path in "${TREE_FILES[@]}"; do
    name=${path##*/}
    for f in "$name" "$name.sha256" "$name.sigstore.json"; do
      if [ -e "$dir/$f" ]; then
        names+=("$name" "$name.sha256" "$name.sigstore.json")
        break
      fi
    done
  done
  for f in "${names[@]}"; do
    [ -s "$dir/$f" ] || die "${f} is missing. ${tag} is not published without it."
  done
  printf '%s\n' "${names[@]}"
}

cmd_check() { # <tag> <name...>
  local tag=$1 have f
  shift
  [ "$#" -gt 0 ] || die "usage: release-assets.sh check TAG NAME..."
  # REST, not gh release view: GraphQL's secondary rate limit reads as "release not found".
  have=$(gh api "repos/${GITHUB_REPOSITORY:?}/releases/tags/${tag}" --jq '.assets[].name') \
    || die "could not read the assets of Release ${tag}"
  for f; do
    grep -qxF "$f" <<<"$have" || die "Release ${tag} is missing asset ${f}"
  done
}

# gh release create with assets drafts, uploads, then publishes (cli/cli v2.96.0
# pkg/cmd/release/create/create.go), so an upload that died leaves a draft the
# by-tag read does not return.
cmd_cleanup_drafts() { # <tag>
  local tag=$1 id
  gh api --paginate "repos/${GITHUB_REPOSITORY:?}/releases?per_page=100" \
    | jq -r --arg v "$tag" '.[] | select(.draft and .tag_name == $v) | .id' \
    | while IFS= read -r id; do
      echo "::notice::deleting orphaned draft release ${id} for ${tag}"
      gh api -X DELETE "repos/${GITHUB_REPOSITORY}/releases/${id}"
    done
}

case "${1:-}" in
  stage) cmd_stage "${2:?usage: release-assets.sh stage TREE-ISH DIR}" "${3:?usage: release-assets.sh stage TREE-ISH DIR}" ;;
  sign) cmd_sign "${2:?usage: release-assets.sh sign TAG DIR}" "${3:?usage: release-assets.sh sign TAG DIR}" ;;
  names) cmd_names "${2:?usage: release-assets.sh names TAG DIR}" "${3:?usage: release-assets.sh names TAG DIR}" ;;
  check) cmd_check "${2:?usage: release-assets.sh check TAG NAME...}" "${@:3}" ;;
  cleanup-drafts) cmd_cleanup_drafts "${2:?usage: release-assets.sh cleanup-drafts TAG}" ;;
  *) die "usage: release-assets.sh stage|sign|names|check|cleanup-drafts ..." ;;
esac
