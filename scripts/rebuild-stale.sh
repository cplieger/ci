#!/usr/bin/env bash
# Base-package rebuilds for rebuild-stale.yaml.
#   fanout                           GH_TOKEN, INTERVAL_DAYS, GITHUB_STEP_SUMMARY and
#                                    GITHUB_OUTPUT; writes the dispatch matrix
#   image-created <repo> <tag>       the build time of ghcr.io/cplieger/<repo>:<tag>, read
#                                    anonymously; exit 3 when the tag does not exist
#   open-pr <repo> <base> <reason>   GH_TOKEN; an auto-merging empty fix(deps) pull request
#                                    into <base> (dev or main), or a merge re-request on the open one
#   stale-pr <repo> <base> <tag> <age-days> <built> <since>   open-pr with the staleness reason,
#                                    opening nothing once a rebuild request merged at or after a
#                                    non-empty <since>
set -euo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

INDEX_TYPES='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'
MANIFEST_TYPES='application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
# path-significance.sh and promote-digest.sh hold copies; test-path-significance.sh holds them equal.
REBUILD_SUBJECT='fix(deps): rebuild against refreshed base packages'

usage() {
  sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# No --fail, so a 404 is an answer rather than a retried error; --retry still
# retries 408, 429 and 5xx, and --retry-all-errors a dropped transfer.
ghcr_get() { # <url> <accept> <out> [bearer token] -> the HTTP status; fails when no response arrived
  local auth=()
  [ -z "${4:-}" ] || auth=(-H "Authorization: Bearer $4")
  curl -sSL --connect-timeout 10 --max-time 30 --retry 7 --retry-max-time 150 --retry-all-errors \
    "${auth[@]}" -H "Accept: $2" -o "$3" -w '%{http_code}' "$1"
}

image_created() { # <repo> <tag>
  local repo="cplieger/$1" tag=$2 token status digest created
  status=$(ghcr_get "https://ghcr.io/token?scope=repository:${repo}:pull" application/json "$WORK/token.json") \
    || { echo "no answer from the GHCR token endpoint" >&2 && return 1; }
  [ "$status" = 200 ] || { echo "GHCR token endpoint answered HTTP ${status}" >&2 && return 1; }
  token=$(jq -r '.token // empty' "$WORK/token.json" 2>/dev/null) || token=""
  [ -n "$token" ] || { echo "GHCR token endpoint answered no token" >&2 && return 1; }

  status=$(ghcr_get "https://ghcr.io/v2/${repo}/manifests/${tag}" "${INDEX_TYPES}, ${MANIFEST_TYPES}" "$WORK/top.json" "$token") \
    || { echo "no answer for ${repo}:${tag}" >&2 && return 1; }
  case "$status" in
    200) ;;
    404) return 3 ;;
    *) echo "${repo}:${tag} answered HTTP ${status}" >&2 && return 1 ;;
  esac
  # An index lists attestation manifests as platform unknown/unknown; they carry no image config.
  digest=$(jq -r 'if .manifests then [.manifests[] | select(.platform.os != "unknown")][0].digest // empty
                  else empty end' "$WORK/top.json" 2>/dev/null) \
    || { echo "${repo}:${tag} is not a manifest" >&2 && return 1; }
  if [ -n "$digest" ]; then
    status=$(ghcr_get "https://ghcr.io/v2/${repo}/manifests/${digest}" "$MANIFEST_TYPES" "$WORK/image.json" "$token") \
      || { echo "no answer for ${repo}@${digest}" >&2 && return 1; }
    [ "$status" = 200 ] || { echo "${repo}@${digest} answered HTTP ${status}" >&2 && return 1; }
  elif jq -e '.manifests' "$WORK/top.json" >/dev/null 2>&1; then
    echo "${repo}:${tag} lists no platform image" >&2 && return 1
  else
    cp "$WORK/top.json" "$WORK/image.json"
  fi
  digest=$(jq -r '.config.digest // empty' "$WORK/image.json" 2>/dev/null) || digest=""
  [ -n "$digest" ] || { echo "${repo}:${tag} names no image config" >&2 && return 1; }
  status=$(ghcr_get "https://ghcr.io/v2/${repo}/blobs/${digest}" application/json "$WORK/config.json" "$token") \
    || { echo "no answer for the config blob of ${repo}:${tag}" >&2 && return 1; }
  [ "$status" = 200 ] || { echo "the config blob of ${repo}:${tag} answered HTTP ${status}" >&2 && return 1; }
  created=$(jq -r '.created // empty | sub("\\.[0-9]+Z$"; "Z")' "$WORK/config.json" 2>/dev/null) || created=""
  [ -n "$created" ] || { echo "the image config of ${repo}:${tag} carries no created time" >&2 && return 1; }
  echo "$created"
}

# A rebuild that changes no package publishes no image (docker-release.yaml's
# rebuild-gate), so the newest rebuild request on the branch dates its refresh.
rebuild_requested() { # <repo> <base> <since> -> the committer time of the newest rebuild request on base at or after since, or nothing; fails when a read fails
  local pages list sha date ts since_ts files
  since_ts=$(date -u -d "$3" +%s) || return 1
  # --paginate prints one JSON array per page, which jq reads as a stream.
  pages=$(gh api --paginate "repos/cplieger/${1}/commits?sha=${2}&since=${3}&per_page=100") || return 1
  # shellcheck disable=SC2016 # jq program, not shell
  list=$(jq -r --arg s "$REBUILD_SUBJECT" '.[] | (.commit.message | split("\n")[0]) as $m
    | select($m == $s or ($m | startswith($s + " (#") and endswith(")")))
    | "\(.sha) \(.commit.committer.date)"' <<<"$pages") || return 1
  while read -r sha date; do
    [ -n "$sha" ] || continue
    ts=$(date -u -d "$date" +%s) || return 1
    [ "$ts" -ge "$since_ts" ] || continue
    files=$(gh api "repos/cplieger/${1}/commits/${sha}" --jq '.files | length') || return 1
    if [ "$files" = 0 ]; then
      echo "$date"
      return 0
    fi
  done <<<"$list"
}

open_pr() { # <repo> <base> <reason> [since]
  local repo=$1 base=$2 reason=$3 since=${4:-} open req head tree commit branch body url
  case "$base" in dev | main) ;; *)
    echo "::error::open-pr: base must be dev or main, got '${base}'" >&2
    exit 2
    ;;
  esac
  # The pull request is armed, or merged once GitHub refuses to arm a merge-ready one,
  # only after a fresh read of its base and head: a dev pull request retargeted to main
  # must not merge there, and the rulesets exempt an admin credential from ci / validate.
  # One refused fails the run, so the tracker issue names it; the next run finds it
  # open and asks again instead of leaving it by hand.
  request_merge() { # <pr url>
    python3 "$TOOLS/release_maintenance.py" merge-checked "$repo" "${1##*/}" \
      --base "$base" --head-prefix "rebuild/${base}-" && return 0
    echo "::error::cplieger/${repo}: could not enable auto-merge on $1"
    return 1
  }
  # A fork's branch can carry any name, so only this repository's own heads count.
  # Newest first, over REST: the App installation's GraphQL quota is shared with
  # Renovate, which runs as the same App.
  open=$(gh api --paginate "repos/cplieger/${repo}/pulls?state=open&base=${base}&sort=created&direction=desc&per_page=100" \
    --jq ".[] | select(.head.repo.full_name == \"cplieger/${repo}\" and (.head.ref | startswith(\"rebuild/${base}-\"))) | .html_url")
  open=${open%%$'\n'*}
  if [ -n "$open" ]; then
    echo "::notice::cplieger/${repo}: ${open} is already open against ${base}, so its merge is asked for again"
    request_merge "$open"
    return 0
  fi
  # A rebuild pull request that merged after fanout read the base is no longer open,
  # so only a fresh read of the base keeps a second one from opening.
  if [ -n "$since" ]; then
    if ! req=$(rebuild_requested "$repo" "$base" "$since" 2>"$WORK/requested.err"); then
      echo "::warning::cplieger/${repo}: the rebuild requests on ${base} could not be read again, so a rebuild pull request is opened"
      tail -n 1 "$WORK/requested.err"
      req=""
    fi
    if [ -n "$req" ]; then
      echo "::notice::cplieger/${repo}: a rebuild request merged into ${base} at ${req}, so no other pull request is opened"
      return 0
    fi
  fi
  head=$(gh api "repos/cplieger/${repo}/git/ref/heads/${base}" --jq .object.sha)
  tree=$(gh api "repos/cplieger/${repo}/git/commits/${head}" --jq .tree.sha)
  # An empty commit: the base's own tree with the base's head as its only parent. Its
  # subject is what makes it a significant push (path-significance.sh REBUILD_SUBJECT),
  # and PKG_REFRESH makes the build re-run its package layer.
  commit=$(gh api -X POST "repos/cplieger/${repo}/git/commits" \
    -f message="$REBUILD_SUBJECT" \
    -f tree="$tree" -f 'parents[]='"$head" --jq .sha)
  branch="rebuild/${base}-$(date -u +%Y%m%d)"
  if gh api "repos/cplieger/${repo}/git/ref/heads/${branch}" >/dev/null 2>&1; then
    echo "::notice::cplieger/${repo}: ${branch} already exists without an open pull request; moving it to the new commit"
    gh api -X PATCH "repos/cplieger/${repo}/git/refs/heads/${branch}" -f sha="$commit" -F force=true >/dev/null
  else
    gh api -X POST "repos/cplieger/${repo}/git/refs" -f ref="refs/heads/${branch}" -f sha="$commit" >/dev/null
  fi
  body="${reason} Merging this empty commit rebuilds it against the base packages published since then; nothing else changes."
  url=$(gh api -X POST "repos/cplieger/${repo}/pulls" \
    -f title="$REBUILD_SUBJECT" \
    -f head="$branch" -f base="$base" -f body="$body" --jq .html_url)
  echo "Opened ${url}"
  request_merge "$url"
}

stale_pr() { # <repo> <base> <tag> <age-days> <built> <since>
  if [ "$5" = "not published" ]; then
    open_pr "$1" "$2" "No \`:${3}\` image is published." "$6"
  else
    open_pr "$1" "$2" "The \`:${3}\` image is ${4} days old (built ${5})." "$6"
  fi
}

fanout() {
  # Per-repo staleness overrides (days), keyed by repo name, e.g.
  #   INTERVAL_OVERRIDES[docker-keepalived]=3
  declare -A INTERVAL_OVERRIDES=()
  local two_branch listing

  # A fork publishes no cplieger image. REST rather than `gh repo list`: the
  # GraphQL quota is shared with every other token of the account owner.
  listing=$(gh api --paginate 'user/repos?per_page=100&affiliation=owner' --jq '
  .[] | select(.archived == false and .fork == false)
      | {name, default_branch, visibility, fork, archived} | @json
')
  two_branch=$(python3 "$TOOLS/release_channels.py" two-branch <<<"$listing")

  STALE='[]'
  now=$(date -u +%s)
  {
    echo "## Staleness decisions (default threshold: ${INTERVAL_DAYS}d)"
    echo ""
    echo "| Repo | Image built (UTC) | Age (days) | Threshold (days) | Decision |"
    echo "|---|---|---|---|---|"
  } >>"$GITHUB_STEP_SUMMARY"

  # A channel ages by its published image, because a promotion re-tags a dev
  # digest and its run says nothing about when that image was built, unless a rebuild
  # request merged within the interval, which the rebuild gate may have answered with no image.
  two_branch_rows() { # <repo>
    local r=$1 base tag content dockerfile created rc ts age_secs age_days interval decision since req
    interval="${INTERVAL_OVERRIDES[$r]:-$INTERVAL_DAYS}"
    add_channel() { # <base> <tag> <last> <age-days> <since or empty> [unreadable reason]
      STALE=$(jq -c --arg r "$r" --arg base "$1" --arg tag "$2" --arg l "$3" --arg a "$4" --arg s "$5" --arg u "${6:-}" \
        '. + [{repo: $r, base: $base, channel: $tag, last: $l, age_days: $a}
              + (if $s == "" then {} else {since: $s} end)
              + (if $u == "" then {} else {unreadable: $u} end)]' <<<"$STALE")
    }
    unreadable_channel() { # <base> <tag> <reason>
      add_channel "$1" "$2" "unreadable" "n/a" "" "$3"
      echo "| ${r} (${1}, :${2}) | unreadable: ${3} | n/a | ${interval} | **unreadable** |" >>"$GITHUB_STEP_SUMMARY"
    }
    for base in main dev; do
      tag=dev
      [ "$base" = main ] && tag=latest
      # Only a 404 means the base has no Dockerfile; any other failed read is a row.
      if ! content=$(gh api "repos/cplieger/${r}/contents/Dockerfile?ref=${base}" --jq '.content' 2>"$WORK/contents.err"); then
        grep -q 'HTTP 404' "$WORK/contents.err" && continue
        unreadable_channel "$base" "$tag" "the Dockerfile on ${base} could not be read: $(head -n 1 "$WORK/contents.err")"
        continue
      fi
      if ! dockerfile=$(base64 -d 2>/dev/null <<<"$content") || [ -z "$dockerfile" ]; then
        unreadable_channel "$base" "$tag" "the Dockerfile on ${base} did not decode as base64 content"
        continue
      fi
      # Read as Docker runs it, so a continued or exec-form install counts, and only
      # an install a new PKG_REFRESH reaches is rebuilt rather than restored from cache.
      rc=0
      python3 "$TOOLS/release_maintenance.py" rebuild-refreshes <<<"$dockerfile" 2>"$WORK/installs.err" || rc=$?
      case "$rc" in
        0) ;;
        3) continue ;;
        4)
          echo "::warning::cplieger/${r}: the Dockerfile on ${base} installs packages in no layer PKG_REFRESH reaches, so a rebuild would reuse the cached packages"
          echo "| ${r} (${base}, :${tag}) | not refreshed by a rebuild | n/a | ${interval} | **no PKG_REFRESH** |" >>"$GITHUB_STEP_SUMMARY"
          continue
          ;;
        *)
          unreadable_channel "$base" "$tag" "the Dockerfile on ${base} could not be parsed: $(tail -n 1 "$WORK/installs.err")"
          continue
          ;;
      esac
      rc=0
      created=$(image_created "$r" "$tag" 2>"$WORK/created.err") || rc=$?
      case "$rc" in
        0) ;;
        3)
          add_channel "$base" "$tag" "not published" "n/a" ""
          echo "| ${r} (${base}, :${tag}) | not published | n/a | ${interval} | **rebuild** |" >>"$GITHUB_STEP_SUMMARY"
          continue
          ;;
        *)
          unreadable_channel "$base" "$tag" "$(head -n 1 "$WORK/created.err")"
          continue
          ;;
      esac
      ts=$(date -u -d "$created" +%s 2>/dev/null) || ts=""
      if [ -z "$ts" ]; then
        unreadable_channel "$base" "$tag" "the image creation time ${created} does not parse"
        continue
      fi
      age_secs=$((now - ts))
      age_days=$((age_secs / 86400))
      if [ "$age_secs" -gt $((interval * 86400)) ]; then
        since=$(date -u -d "@$((now - interval * 86400))" +%Y-%m-%dT%H:%M:%SZ)
        if ! req=$(rebuild_requested "$r" "$base" "$since" 2>"$WORK/requested.err"); then
          echo "::warning::cplieger/${r}: the rebuild requests on ${base} could not be read, so the image age alone decides"
          tail -n 1 "$WORK/requested.err"
          req=""
        fi
        if [ -n "$req" ]; then
          decision="fresh (rebuild requested ${req})"
        else
          add_channel "$base" "$tag" "$created" "$age_days" "$since"
          decision="**rebuild**"
        fi
      else
        decision="fresh"
      fi
      echo "| ${r} (${base}, :${tag}) | ${created} | ${age_days} | ${interval} | ${decision} |" >>"$GITHUB_STEP_SUMMARY"
    done
  }

  while IFS= read -r r; do
    [ -z "$r" ] && continue
    two_branch_rows "$r"
  done <<<"$two_branch"

  echo "matrix=${STALE}" >>"$GITHUB_OUTPUT"
  count=$(jq '[.[] | select(.unreadable == null)] | length' <<<"$STALE")
  unreadable=$(jq '[.[] | select(.unreadable != null)] | length' <<<"$STALE")
  {
    echo ""
    echo "Rebuilding ${count} image(s)."
    [ "$unreadable" -eq 0 ] || echo "${unreadable} image(s) could not be read; each fails its dispatch row."
  } >>"$GITHUB_STEP_SUMMARY"
  echo "Stale images: ${count}"
}

case "${1:-}" in
  fanout)
    [ "$#" -eq 1 ] || usage
    fanout
    ;;
  image-created)
    [ "$#" -eq 3 ] || usage
    image_created "$2" "$3"
    ;;
  open-pr)
    [ "$#" -eq 4 ] || usage
    open_pr "$2" "$3" "$4"
    ;;
  stale-pr)
    [ "$#" -eq 7 ] || usage
    stale_pr "$2" "$3" "$4" "$5" "$6" "$7"
    ;;
  *) usage ;;
esac
