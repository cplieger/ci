#!/usr/bin/env bash
# Renders one released lane's notes for release.yaml and docker-release.yaml:
#   --release-model legacy|two-branch --site docker|go|ts|lane --version V --out FILE
#   [--release-commit SHA] [--lane DIR] [--go-lanes JSON] [--finalize] [--release-needed]
#   [--latest TAG] [--kind-note TEXT] [--repo OWNER/NAME] [--sbom-prev F --sbom-new F]
#   [--security-shas F] [--inventory PATH] [--config FILE]; --print-previous, without --out,
#   prints the lane's previous stable tag by version (two-branch's range start) and stops.
# legacy runs each site's main-default git-cliff line; two-branch renders previous..--release-commit
# plus the dependency and package diffs. git-cliff executes the repo's cliff config: no token here.
set -euo pipefail

die() {
  echo "render-notes: $*" >&2
  exit 2
}

MODEL="" SITE="" VERSION="" OUT="" COMMIT="" LANE="" GO_LANES_JSON="[]"
FINALIZE=false RELEASE_NEEDED=false PRINT_PREVIOUS=false LATEST="" KIND_NOTE="" REPO=""
SBOM_PREV="" SBOM_NEW="" SECURITY_SHAS="" CONFIG=""
INVENTORY="$(dirname "$0")/inventory.py"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --finalize) FINALIZE=true ;;
    --release-needed) RELEASE_NEEDED=true ;;
    --print-previous) PRINT_PREVIOUS=true ;;
    --release-model | --site | --version | --out | --release-commit | --lane | --go-lanes | --latest | --kind-note | --repo | --sbom-prev | --sbom-new | --security-shas | --inventory | --config)
      [ "$#" -ge 2 ] || die "$1 needs a value"
      case "$1" in
        --release-model) MODEL=$2 ;;
        --site) SITE=$2 ;;
        --version) VERSION=$2 ;;
        --out) OUT=$2 ;;
        --release-commit) COMMIT=$2 ;;
        --lane) LANE=$2 ;;
        --go-lanes) GO_LANES_JSON=${2:-[]} ;;
        --latest) LATEST=$2 ;;
        --kind-note) KIND_NOTE=$2 ;;
        --repo) REPO=$2 ;;
        --sbom-prev) SBOM_PREV=$2 ;;
        --sbom-new) SBOM_NEW=$2 ;;
        --security-shas) SECURITY_SHAS=$2 ;;
        --inventory) INVENTORY=$2 ;;
        --config) CONFIG=$2 ;;
      esac
      shift
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

case "$MODEL" in legacy | two-branch) ;; *) die "--release-model must be legacy or two-branch, got '${MODEL}'" ;; esac
case "$SITE" in docker | go | ts | lane) ;; *) die "--site must be docker, go, ts or lane, got '${SITE}'" ;; esac
[ -n "$VERSION" ] || die "--version is required"
[ -n "$OUT" ] || [ "$PRINT_PREVIOUS" = true ] || die "--out is required"
if [ "$SITE" = lane ]; then
  [ -n "$LANE" ] || die "--site lane needs --lane"
else
  [ -z "$LANE" ] || die "--lane is only valid with --site lane"
fi
if [ -n "${GITHUB_TOKEN:-}${GH_TOKEN:-}" ]; then
  die "refusing to run with GITHUB_TOKEN or GH_TOKEN set: git-cliff executes the repository's cliff config"
fi
# Keeps a cliff command from reading the OIDC request pair directly. Not the
# boundary: an ancestor's environment still holds it, and only a job without
# id-token keeps it from the config.
unset ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN
# A malformed value would otherwise fail inside the process substitution below,
# which cannot fail the script, and leave the notes silently unscoped.
if ! jq -e 'type == "array" and all(.[]; type == "string")' <<<"$GO_LANES_JSON" >/dev/null 2>&1; then
  die "--go-lanes must be a JSON array of strings, not ${GO_LANES_JSON}"
fi
CLIFF=${CLIFF_BIN:-git-cliff}
CLIFF_ARGS=()
[ -z "$CONFIG" ] || CLIFF_ARGS+=(--config "$CONFIG")

if [ "$SITE" = lane ]; then
  ESC=$(printf '%s' "$LANE" | sed -e 's/[][\.|(){}?+*^$]/\\&/g')
  PATTERN="^${ESC}/v[0-9]+\\.[0-9]+\\.[0-9]+\$"
  PREFIX="${LANE}/"
  SCOPE_ARGS=(--include-path "${LANE}/**")
else
  PATTERN='^v[0-9]+\.[0-9]+\.[0-9]+$'
  PREFIX=""
  SCOPE_ARGS=()
  while IFS= read -r d; do
    [ -z "$d" ] && continue
    SCOPE_ARGS+=(--exclude-path "$d/**")
  done < <(jq -r '.[]' <<<"$GO_LANES_JSON")
fi

legacy() {
  if [ -n "$COMMIT" ] && [ "$(git rev-parse --verify "${COMMIT}^{commit}")" != "$(git rev-parse HEAD)" ]; then
    die "legacy renders HEAD, and --release-commit ${COMMIT} is not HEAD"
  fi
  local args=(--tag-pattern "$PATTERN" "${SCOPE_ARGS[@]}" "${CLIFF_ARGS[@]}")
  # --unreleased --tag names the commits since the last tag $VERSION, whose tag
  # does not exist yet; in finalize mode the tag already sits at HEAD, so those
  # commits are the CURRENT release (cliff probe states K/L).
  if [ "$FINALIZE" = true ]; then
    "$CLIFF" --current "${args[@]}" --strip header >"$OUT"
  elif [ "$SITE" = docker ]; then
    "$CLIFF" --unreleased --tag "$VERSION" "${args[@]}" --strip header >"$OUT" || echo "Release ${VERSION}" >"$OUT"
  else
    "$CLIFF" --unreleased --tag "$VERSION" "${args[@]}" --strip header >"$OUT"
  fi
  # git-cliff drops a commit that changes no file, so an image release made of a
  # forced base-package rebuild alone renders nothing.
  if [ "$SITE" = docker ] && [ "$RELEASE_NEEDED" = true ] && [ -z "$(tr -d '[:space:]' <"$OUT")" ]; then
    local range=HEAD
    [ -z "$LATEST" ] || range="${LATEST}..HEAD"
    {
      echo "### Changes"
      echo ""
      git log --no-merges --format='- %s' "$range"
    } >"$OUT"
  fi
  if [ -n "$KIND_NOTE" ]; then
    printf '\n%s\n' "$KIND_NOTE" >>"$OUT"
  fi
}

version_key() { # X.Y.Z -> fixed-width sortable key
  local IFS=. parts
  read -r -a parts <<<"$1"
  printf '%010d%010d%010d' "$((10#${parts[0]}))" "$((10#${parts[1]}))" "$((10#${parts[2]}))"
}

previous_stable_tag() {
  local want best="" best_key="" tag key
  want=$(version_key "${VERSION#"${PREFIX}v"}")
  while IFS= read -r tag; do
    key=$(version_key "${tag#"${PREFIX}v"}")
    if [[ $key < $want ]] && { [ -z "$best" ] || [[ $key > $best_key ]]; }; then
      best=$tag best_key=$key
    fi
  done < <(git tag --list | grep -E "$PATTERN" || true)
  printf '%s' "$best"
}

# shellcheck disable=SC2016 # jq program, not shell
SBOM_DIFF_JQ='
def roots: [.documentDescribes // [] | .[]]
  + [.relationships[]? | select(.relationshipType == "DESCRIBES" and .spdxElementId == "SPDXRef-DOCUMENT") | .relatedSpdxElement];
def entries: roots as $r
  | [ .packages[]?
      | select(.SPDXID as $id | ($r | index($id)) == null)
      | .name as $n
      | { key: (([.externalRefs[]? | select(.referenceType == "purl") | .referenceLocator] | first // "")
               | sub("@.*$"; "") | if . == "" then "name:" + $n else . end),
          name: $n, version: (.versionInfo // "") } ]
  | group_by(.key)
  | map({ key: .[0].key, value: { name: .[0].name, versions: (map(.version) | unique) } })
  | from_entries;
($prev[0] | entries) as $a | ($new[0] | entries) as $b
| [ ([$a, $b] | map(keys) | add | unique)[] as $k
    | if ($a | has($k) | not) then
        { name: $b[$k].name, line: "- Added `\($b[$k].name)` \($b[$k].versions | join(", "))" }
      elif ($b | has($k) | not) then
        { name: $a[$k].name, line: "- Removed `\($a[$k].name)` \($a[$k].versions | join(", "))" }
      elif $a[$k].versions != $b[$k].versions then
        { name: $a[$k].name, line: "- `\($a[$k].name)` \($a[$k].versions | join(", ")) to \($b[$k].versions | join(", "))" }
      else empty end ]
| sort_by(.name, .line)
| if length == 0 then empty else
    "<details>\n<summary>System packages: \(length) change\(if length == 1 then "" else "s" end) ("
    + (map(.name) | .[0:3] | join(", "))
    + ")</summary>\n\n" + (map(.line) | join("\n")) + "\n\n</details>"
  end'

two_branch() {
  [ -n "$COMMIT" ] || die "two-branch needs --release-commit"
  [ -n "$REPO" ] || die "two-branch needs --repo"
  printf '%s' "$VERSION" | grep -Eq "$PATTERN" || die "--version ${VERSION} is not a stable version of this lane"
  if [ "$SITE" != docker ] && [ -n "${SBOM_PREV}${SBOM_NEW}" ]; then
    die "--sbom-prev/--sbom-new are only valid with --site docker"
  fi
  local commit prev range changes deps="" sbom="" parts=()
  commit=$(git rev-parse --verify "${COMMIT}^{commit}") || die "--release-commit ${COMMIT} is not a commit"
  prev=$(previous_stable_tag)
  range=$commit
  [ -z "$prev" ] || range="${prev}..${commit}"
  changes=$(CLIFF_NOTES_MODE=v3 "$CLIFF" --tag-pattern "$PATTERN" --tag "$VERSION" "${SCOPE_ARGS[@]}" "${CLIFF_ARGS[@]}" --strip header "$range")
  if [ -n "$prev" ]; then
    changes+=$'\n\n'"**Full changelog**: https://github.com/${REPO}/compare/${prev}...${VERSION}"
    local inv=(diff --git-dir . --from "$prev" --to "$commit" --lane "${LANE:-.}" --format markdown)
    [ -z "$SECURITY_SHAS" ] || inv+=(--security-shas "$SECURITY_SHAS")
    deps=$(python3 "$INVENTORY" "${inv[@]}")
  fi
  if [ -n "$SBOM_PREV" ] && [ -n "$SBOM_NEW" ]; then
    sbom=$(jq -nr --slurpfile prev "$SBOM_PREV" --slurpfile new "$SBOM_NEW" "$SBOM_DIFF_JQ")
  elif [ -n "${SBOM_PREV}${SBOM_NEW}" ]; then
    echo "render-notes: one SBOM given, so no system-package diff" >&2
  fi
  local part
  for part in "$KIND_NOTE" "$changes" "$deps" "$sbom"; do
    part=$(printf '%s\n' "$part" | sed '/./,$!d')
    [ -z "$(tr -d '[:space:]' <<<"$part")" ] || parts+=("$part")
  done
  : >"$OUT"
  local i
  for i in "${!parts[@]}"; do
    [ "$i" -eq 0 ] || printf '\n' >>"$OUT"
    printf '%s\n' "${parts[$i]}" >>"$OUT"
  done
}

if [ "$PRINT_PREVIOUS" = true ]; then
  printf '%s' "$VERSION" | grep -Eq "$PATTERN" || die "--version ${VERSION} is not a stable version of this lane"
  previous_stable_tag
  echo
elif [ "$MODEL" = legacy ]; then
  legacy
else
  two_branch
fi
