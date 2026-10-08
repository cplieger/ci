#!/usr/bin/env bash
# Shipped paths of a range per lane, for release.yaml detect and promotion.
# MODE=release (default): env BEFORE HEAD ANCHOR_SHA CHANNEL REPO_TYPE
#   SUBPACKAGES_JSON GO_LANES_JSON RELEASE_MODEL (legacy|two-branch, default
#   legacy); writes root_changed, subpackages_to_publish, go_modules_to_release
#   and exclude_re to $GITHUB_OUTPUT, and a summary.
# MODE=paths: env FROM TO REPO_TYPE SUBPACKAGES_JSON GO_LANES_JSON; the same
#   keys plus `significant` (JSON array) on stdout. Tree diff FROM..TO only:
#   no empty-commit rule, no lane tags, status 2 when the diff fails.
set -euo pipefail

MODE="${MODE:-release}"
case "$MODE" in
  release | paths) ;;
  *)
    echo "::error::path-significance: unknown MODE '$MODE'" >&2
    exit 2
    ;;
esac
note() {
  if [ "$MODE" = paths ]; then echo "::notice::$*" >&2; else echo "::notice::$*"; fi
}

RANGE_START=""
# Paths mode and two-branch release mode count both sides of a rename: a file
# leaving a lane or moving under an excluded path still changed what the lane
# ships, and a dev build skipped for it leaves a promotion nothing to re-tag.
RENAMES=()
case "${RELEASE_MODEL:-legacy}" in
  legacy) ;;
  two-branch) RENAMES=(--no-renames) ;;
  *)
    echo "::error::path-significance: unknown RELEASE_MODEL '${RELEASE_MODEL}'" >&2
    exit 2
    ;;
esac
# Release mode: the range is <anchor>..HEAD, the anchor being the channel's
# newest reachable tag. A dispatch (no `before`) or an unreachable `before`
# (force push) counts every tracked file, so a dispatch stays the repair lever.
if [ "$MODE" = paths ]; then
  : "${FROM:?MODE=paths needs FROM}" "${TO:?MODE=paths needs TO}"
  if ! CHANGED=$(git diff --no-renames --name-only "$FROM..$TO"); then
    echo "::error::path-significance: cannot diff ${FROM}..${TO}" >&2
    exit 2
  fi
  BEFORE="$FROM"
  HEAD="$TO"
  CHANNEL=""
  ANCHOR_SHA=""
elif [ -z "$BEFORE" ] || [ "$BEFORE" = "0000000000000000000000000000000000000000" ]; then
  note "No before SHA — treating all tracked files as changed"
  CHANGED=$(git ls-files)
elif [ -n "$ANCHOR_SHA" ]; then
  if CHANGED=$(git diff "${RENAMES[@]}" --name-only "$ANCHOR_SHA..$HEAD" 2>/dev/null); then
    RANGE_START="$ANCHOR_SHA"
    BEFORE="$ANCHOR_SHA"
    note "deriving changed paths from ${ANCHOR_SHA}..HEAD (newest tag on this channel)"
  else
    note "git diff from the anchor failed — treating all tracked files as changed"
    CHANGED=$(git ls-files)
  fi
elif CHANGED=$(git diff "${RENAMES[@]}" --name-only "$BEFORE..$HEAD" 2>/dev/null); then
  RANGE_START="$BEFORE"
else
  note "git diff failed (force push or unreachable history) — treating all tracked files as changed"
  CHANGED=$(git ls-files)
fi

# Paths that never ship in an artifact (extended regex). Invariant: the
# consumer cliff configs' `exclude_paths` (configs/cliff-*.toml) stay a strict
# subset of this list, and every config file the ci sync writes (editor, lint,
# CI and changelog configs) is matched here.
EXCLUDE_PATTERNS=(
  # Editor / formatter / linter configs
  '(^|/)\.editorconfig$'
  '(^|/)\.gitignore$'
  '(^|/)\.gitattributes$'
  '(^|/)\.dockerignore$'
  '(^|/)\.prettierrc(\.json|\.yaml|\.toml)?$'
  '(^|/)\.prettierignore$'
  '(^|/)\.htmlvalidate\.json$'
  '(^|/)\.stylelintrc\.json$'
  '(^|/)eslint\.config\.(mjs|js|cjs)$'
  '(^|/)eslint\.config\.base\.mjs$'
  '(^|/)vitest\.config\.(ts|js)$'
  '(^|/)tsconfig\.(test|tests|node)\.json$'
  # Root-anchored only: a nested static-src/tsconfig.json is a tsc build input
  # of the web client a Go binary embeds.
  '^tsconfig\.json$'
  '(^|/)deadset(-ignore|-edges)?\.json$'
  # knip's configs, and the retired .punused-ignore repos carry until they delete it.
  '(^|/)(\.knip\.jsonc?|knip\.(jsonc?|[jt]s)|knip\.config\.[jt]s)$'
  '(^|/)\.punused-ignore$'
  '(^|/)stryker\.config\.json$'
  '(^|/)\.golangci\.(yaml|yml)$'
  '(^|/)\.gremlins\.(yaml|yml)$'
  '(^|/)ruff\.toml$'

  # Docs and license
  '\.md$'
  '(^|/)LICENSE$'
  # README images. Root docs/ only, and valid only while no
  # Dockerfile COPYs or embeds docs/ into an image.
  '^docs/.*\.(png|jpe?g|webp|gif|svg)$'
  # README-companion alert rules. The root alerts.yaml form stays matched
  # until no repo ships one.
  '^alerts/'
  '^alerts\.yaml$'

  # CI / security tooling configs
  '(^|/)cliff\.toml$'
  '(^|/)renovate\.json$'
  '(^|/)\.gitleaks\.toml$'
  '(^|/)\.codeql-suppressions\.yaml$'
  '(^|/)\.trivyignore$'
  '(^|/)\.syft\.yaml$'
  '^\.github/'

  # Go test code
  '_test\.go$'
  '(^|/)testdata/'
  '(^|/)testsupport/'
  '(^|/)testcerts/'
  '(^|/)authtest/'

  # TS test code
  '\.test\.ts$'
  '\.spec\.ts$'
  '\.fuzz\.test\.ts$'
  '\.property\.test\.ts$'
  '(^|/)__test-helpers__/'
  '(^|/)__snapshots__/'
  '(^|/)test-stubs/'
  '(^|/)fc-strict-setup\.ts$'

  # Generated / vendored output
  '(^|/)dist/'
  '(^|/)coverage/'
  '(^|/)node_modules/'
  '(^|/)static/[^/]+\.js$'
  '(^|/)static/[^/]+\.js\.map$'
  '(^|/)static/style\.css$'
  '(^|/)static/vendor/'

  # Lockfiles: npm publish omits them, consumers resolve from the manifest
  # ranges, and no artifact Dockerfile reads one.
  '(^|/)package-lock\.json$'
  '(^|/)npm-shrinkwrap\.json$'
  '(^|/)yarn\.lock$'
  '(^|/)pnpm-lock\.yaml$'
  '(^|/)bun\.lockb$'
  '(^|/)bun\.lock$'

  # Dev convenience / manual test scripts
  '^compose\.yaml$'
  '^test-validate\.sh$'
  '(^|/)setup-tools\.test\.sh$'
  '^tests/'
)
EXCLUDE_RE=$(
  IFS='|'
  echo "${EXCLUDE_PATTERNS[*]}"
)

if [ -z "$CHANGED" ]; then
  SIGNIFICANT=""
else
  SIGNIFICANT=$(echo "$CHANGED" | grep -Ev "$EXCLUDE_RE" || true)
fi

# A commit changing no file exists only to force a rebuild, and git-cliff
# cannot see it, so it counts as a significant root change.
EMPTY_COMMIT=""
if [ -z "$SIGNIFICANT" ] && [ -n "$RANGE_START" ]; then
  while IFS= read -r c; do
    [ -z "$c" ] && continue
    if [ -z "$(git diff-tree --root --no-commit-id --name-only -r "$c")" ]; then
      EMPTY_COMMIT="$c"
      note "commit ${c} changes no file; treating it as a significant root change"
      break
    fi
  done < <(git rev-list --no-merges "$RANGE_START..$HEAD")
fi

# package.json ships only as a published manifest (a declared TS subpackage's,
# or the root one in a ts repo), and there only through its fields other than
# devDependencies and overrides: npm reads overrides from the installing
# project's root manifest alone
# (https://docs.npmjs.com/cli/v11/configuring-npm/package-json). Any other
# package.json is dev tooling. With no BEFORE to compare, stay significant.
if [ -n "$SIGNIFICANT" ] && echo "$SIGNIFICANT" | grep -qE '(^|/)package\.json$'; then
  MANIFESTS=$(echo "$SUBPACKAGES_JSON" | jq -r '.[] | . + "/package.json"')
  if [ "$REPO_TYPE" = "ts" ]; then
    MANIFESTS=$(printf '%s\npackage.json\n' "$MANIFESTS")
  fi
  while IFS= read -r pj; do
    [ -z "$pj" ] && continue
    if ! printf '%s\n' "$MANIFESTS" | grep -qxF "$pj"; then
      SIGNIFICANT=$(echo "$SIGNIFICANT" | grep -vxF "$pj" || true)
      note "$pj is not a published manifest — dropped from significance"
      continue
    fi
    if [ -n "$BEFORE" ] && [ "$BEFORE" != "0000000000000000000000000000000000000000" ]; then
      b=$(git show "$BEFORE:$pj" 2>/dev/null | jq -S 'del(.devDependencies, .overrides)' 2>/dev/null || echo "")
      h=$(git show "$HEAD:$pj" 2>/dev/null | jq -S 'del(.devDependencies, .overrides)' 2>/dev/null || echo "")
      if [ -n "$b" ] && [ "$b" = "$h" ]; then
        SIGNIFICANT=$(echo "$SIGNIFICANT" | grep -vxF "$pj" || true)
        note "$pj changed only in devDependencies/overrides — not significant"
      fi
    fi
  done < <(echo "$SIGNIFICANT" | grep -E '(^|/)package\.json$')
fi

# A path under a subpackage or a nested lane belongs to that one, never to
# the root.
ROOT_CHANGED=false
declare -A SUBPKG_CHANGED
declare -A LANE_CHANGED
declare -A LANE_ANCHOR
mapfile -t SUBPACKAGES < <(echo "$SUBPACKAGES_JSON" | jq -r '.[]')
mapfile -t GO_LANES < <(echo "$GO_LANES_JSON" | jq -r '.[]')
for s in "${SUBPACKAGES[@]}"; do
  SUBPKG_CHANGED["$s"]=false
done

# A lane is measured from its own newest channel tag: the root anchor ignores
# lane tags, so it predates a lane-only release. Same exact-regex nearest-tag
# rule as compute.sh's nearest_tag, so detect and the lane job agree.
lane_anchor() { # <dir> -> commit of the lane's channel tag nearest HEAD, or nothing
  local esc re name obj peeled commit
  local -A tag_at=()
  esc=$(printf '%s' "$1" | sed -e 's/[][\.|(){}?+*^$]/\\&/g')
  re="^${esc}/v[0-9]+\\.[0-9]+\\.[0-9]+\$"
  if [ "$CHANNEL" = dev ]; then
    re="${re}|${re%\$}-dev\\.[0-9]+\$"
  fi
  while read -r name obj peeled; do
    [[ $name =~ $re ]] || continue
    tag_at["${peeled:-$obj}"]="$name"
  done < <(git for-each-ref refs/tags --format='%(refname:strip=2) %(objectname) %(*objectname)')
  [ "${#tag_at[@]}" -gt 0 ] || return 0
  while read -r commit; do
    if [ -n "${tag_at[$commit]:-}" ]; then
      printf '%s' "$commit"
      return 0
    fi
  done < <(git rev-list --topo-order "$HEAD")
}
for l in "${GO_LANES[@]}"; do
  LANE_CHANGED["$l"]=false
  if [ "$MODE" = release ]; then
    LANE_ANCHOR["$l"]="$(lane_anchor "$l")"
  else
    LANE_ANCHOR["$l"]=""
  fi
done

if [ -n "$EMPTY_COMMIT" ]; then
  ROOT_CHANGED=true
fi
if [ -n "$SIGNIFICANT" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    in_subpkg=false
    for s in "${SUBPACKAGES[@]}"; do
      if [[ "$f" == "$s/"* ]]; then
        SUBPKG_CHANGED["$s"]=true
        in_subpkg=true
        break
      fi
    done
    in_lane=false
    for l in "${GO_LANES[@]}"; do
      if [[ "$f" == "$l/"* ]]; then
        in_lane=true
        # Without a tag on this channel the lane's first release is owed by
        # whatever the root range shows under it.
        if [ -z "${LANE_ANCHOR[$l]}" ]; then
          LANE_CHANGED["$l"]=true
        fi
        break
      fi
    done
    if ! $in_subpkg && ! $in_lane; then
      ROOT_CHANGED=true
    fi
  done <<<"$SIGNIFICANT"
fi

# A tagged lane is owed a release when a shipped path under it changed since
# its tag; on stable a lane tag at HEAD is emitted too, so the lane job can
# repair a missing Release.
for l in "${GO_LANES[@]}"; do
  anchor="${LANE_ANCHOR[$l]}"
  [ -n "$anchor" ] || continue
  if [ "$anchor" = "$HEAD" ]; then
    if [ "$CHANNEL" = stable ]; then
      LANE_CHANGED["$l"]=true
    fi
    continue
  fi
  note "lane ${l}: deriving changed paths from ${anchor}..HEAD (newest lane tag on this channel)"
  lane_diff=$(git diff "${RENAMES[@]}" --name-only "$anchor..$HEAD" -- "$l/")
  if [ -n "$lane_diff" ] && grep -Eqv "$EXCLUDE_RE" <<<"$lane_diff"; then
    LANE_CHANGED["$l"]=true
  fi
done

SUBS_TO_PUBLISH="[]"
for s in "${SUBPACKAGES[@]}"; do
  if [ "${SUBPKG_CHANGED[$s]}" = "true" ]; then
    SUBS_TO_PUBLISH=$(echo "$SUBS_TO_PUBLISH" | jq -c --arg v "$s" '. + [$v]')
  fi
done

GO_LANES_TO_RELEASE="[]"
for l in "${GO_LANES[@]}"; do
  if [ "${LANE_CHANGED[$l]}" = "true" ]; then
    GO_LANES_TO_RELEASE=$(echo "$GO_LANES_TO_RELEASE" | jq -c --arg v "$l" '. + [$v]')
  fi
done

if [ "$MODE" = paths ]; then
  echo "root_changed=$ROOT_CHANGED"
  echo "subpackages_to_publish=$SUBS_TO_PUBLISH"
  echo "go_modules_to_release=$GO_LANES_TO_RELEASE"
  echo "exclude_re=$EXCLUDE_RE"
  echo "significant=$(printf '%s' "$SIGNIFICANT" | jq -Rsc 'split("\n") | map(select(. != ""))')"
  exit 0
fi

{
  echo "root_changed=$ROOT_CHANGED"
  echo "subpackages_to_publish=$SUBS_TO_PUBLISH"
  echo "go_modules_to_release=$GO_LANES_TO_RELEASE"
  echo "exclude_re=$EXCLUDE_RE"
} >>"$GITHUB_OUTPUT"

{
  echo "## Change detection summary"
  echo ""
  echo "**Before:** \`$BEFORE\`"
  echo "**Head:** \`$HEAD\`"
  echo ""
  echo "**Significant paths:**"
  echo '```'
  echo "${SIGNIFICANT:-(none — only excluded paths changed)}"
  if [ -n "$EMPTY_COMMIT" ]; then
    echo "(commit ${EMPTY_COMMIT} changes no file and counts as a root change)"
  fi
  echo '```'
  echo ""
  echo "**Outputs:**"
  echo "- root_changed: $ROOT_CHANGED"
  echo "- subpackages_to_publish: $SUBS_TO_PUBLISH"
  echo "- go_modules_to_release: $GO_LANES_TO_RELEASE"
} >>"$GITHUB_STEP_SUMMARY"
