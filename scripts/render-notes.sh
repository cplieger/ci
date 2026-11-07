#!/usr/bin/env bash
# Renders one released lane's notes for release.yaml and docker-release.yaml:
#   --site docker|go|ts|lane --version V --release-commit SHA --repo OWNER/NAME --out FILE
#   [--lane DIR] [--go-lanes JSON] [--kind-note TEXT] [--sbom-prev F --sbom-new F]
#   [--security-shas F] [--inventory PATH] [--config FILE]; --print-previous, without --out,
#   prints the lane's previous stable tag by version (the range start) and stops.
#   [--sbom-changes] --sbom-prev F --sbom-new F alone prints the number of package
#   changes the System packages block would list, and stops.
# The notes cover previous..--release-commit plus the dependency and package diffs.
# git-cliff executes the repo's cliff config: no token here.
set -euo pipefail

die() {
  echo "render-notes: $*" >&2
  exit 2
}

SITE="" VERSION="" OUT="" COMMIT="" LANE="" GO_LANES_JSON="[]"
PRINT_PREVIOUS=false SBOM_CHANGES=false KIND_NOTE="" REPO=""
SBOM_PREV="" SBOM_NEW="" SECURITY_SHAS="" CONFIG=""
INVENTORY="$(dirname "$0")/inventory.py"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print-previous) PRINT_PREVIOUS=true ;;
    --sbom-changes) SBOM_CHANGES=true ;;
    --site | --version | --out | --release-commit | --lane | --go-lanes | --kind-note | --repo | --sbom-prev | --sbom-new | --security-shas | --inventory | --config)
      [ "$#" -ge 2 ] || die "$1 needs a value"
      case "$1" in
        --site) SITE=$2 ;;
        --version) VERSION=$2 ;;
        --out) OUT=$2 ;;
        --release-commit) COMMIT=$2 ;;
        --lane) LANE=$2 ;;
        --go-lanes) GO_LANES_JSON=${2:-[]} ;;
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

# shellcheck disable=SC2016 # jq program, not shell
SBOM_CHANGES_JQ='
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
| sort_by(.name, .line)'
SBOM_DIFF_JQ="${SBOM_CHANGES_JQ}"'
| if length == 0 then empty else
    "<details>\n<summary>System packages: \(length) change\(if length == 1 then "" else "s" end) ("
    + (map(.name) | .[0:3] | join(", "))
    + ")</summary>\n\n" + (map(.line) | join("\n")) + "\n\n</details>"
  end'

if [ "$SBOM_CHANGES" = true ]; then
  if [ -z "$SBOM_PREV" ] || [ -z "$SBOM_NEW" ]; then
    die "--sbom-changes needs --sbom-prev and --sbom-new"
  fi
  for f in "$SBOM_PREV" "$SBOM_NEW"; do
    jq -e '(.packages | type) == "array"' "$f" >/dev/null 2>&1 || die "${f} is not an SPDX document"
  done
  jq -nr --slurpfile prev "$SBOM_PREV" --slurpfile new "$SBOM_NEW" "${SBOM_CHANGES_JQ} | length"
  exit 0
fi

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

render() {
  [ -n "$COMMIT" ] || die "--release-commit is required"
  [ -n "$REPO" ] || die "--repo is required"
  printf '%s' "$VERSION" | grep -Eq "$PATTERN" || die "--version ${VERSION} is not a stable version of this lane"
  if [ "$SITE" != docker ] && [ -n "${SBOM_PREV}${SBOM_NEW}" ]; then
    die "--sbom-prev/--sbom-new are only valid with --site docker"
  fi
  local commit prev range changes deps="" sbom="" parts=()
  commit=$(git rev-parse --verify "${COMMIT}^{commit}") || die "--release-commit ${COMMIT} is not a commit"
  prev=$(previous_stable_tag)
  range=$commit
  [ -z "$prev" ] || range="${prev}..${commit}"
  # A commit the path filter drops that changes a file a non-test //go:embed
  # names ships, so its line belongs here (go-embeds.py; compute.sh counts it).
  local -a with=() scope=(--config "${CONFIG:-${GIT_CLIFF_CONFIG:-cliff.toml}}")
  local msg i embeds order
  for ((i = 0; i < ${#SCOPE_ARGS[@]}; i += 2)); do
    case "${SCOPE_ARGS[$i]}" in
      --include-path) scope+=(--include "${SCOPE_ARGS[$((i + 1))]}") ;;
      --exclude-path) scope+=(--exclude "${SCOPE_ARGS[$((i + 1))]}") ;;
    esac
  done
  embeds=$(mktemp)
  order=$(mktemp)
  # shellcheck disable=SC2064 # the paths are fixed now
  trap "rm -f '$embeds' '$order'" EXIT
  python3 "$(dirname "$0")/go-embeds.py" --with-commits "$range" "${scope[@]}" --positions "$order" >"$embeds" \
    || die "cannot read the //go:embed directives over ${range}"
  while IFS= read -r -d '' msg; do
    with+=(--with-commit "$msg")
  done <"$embeds"
  if [ "${#with[@]}" -eq 0 ]; then
    changes=$("$CLIFF" --tag-pattern "$PATTERN" --tag "$VERSION" "${SCOPE_ARGS[@]}" "${CLIFF_ARGS[@]}" --strip header "$range")
  else
    # git-cliff 2.14.2 adds --with-commit commits to its last release, an empty
    # one after the tagged release, which renders as a second block; so the
    # releases' commits are merged into the tagged one, sorted back into the
    # range's order (a --with-commit commit has an empty id, so it is found by
    # its message), and rendered once.
    local ctx
    ctx=$("$CLIFF" --tag-pattern "$PATTERN" --tag "$VERSION" "${SCOPE_ARGS[@]}" "${CLIFF_ARGS[@]}" "${with[@]}" --context "$range")
    changes=$(jq --slurpfile at "$order" '
      (map(select(.version != null)) + map(select(.version == null))) as $r
      | [$r[0] + {commits: ($r | map(.commits) | add
          | sort_by(if .id != "" then $at[0].commits[.id] else $at[0].messages[.raw_message] end // 1e9))}]' <<<"$ctx" \
      | "$CLIFF" "${CLIFF_ARGS[@]}" --from-context - --strip header)
  fi
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
else
  render
fi
