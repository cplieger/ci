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
#   dispatch <repo> <bases>          GH_TOKEN and GITHUB_STEP_SUMMARY; stale-pr for each of a matrix
#                                    leg's bases in turn, failing once any base failed or is unreadable
set -euo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

INDEX_TYPES='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'
MANIFEST_TYPES='application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
# path-significance.sh and promote-digest.sh hold copies; test-path-significance.sh holds them equal.
REBUILD_SUBJECT='fix(deps): rebuild against refreshed base packages'

usage() {
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
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
  local repo=$1 base=$2 reason=$3 since=${4:-} open req head tree commit branch body url rc
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
  open_rebuild() { # [branch] -> the newest open rebuild pull request's URL, that branch's when given
    local found
    found=$(gh api --paginate "repos/cplieger/${repo}/pulls?state=open&base=${base}&sort=created&direction=desc&per_page=100" \
      --jq ".[] | select(.head.repo.full_name == \"cplieger/${repo}\" and (.head.ref | startswith(\"rebuild/${base}-\"))
        and (\"${1:-}\" == \"\" or .head.ref == \"${1:-}\")) | .html_url") || return 1
    echo "${found%%$'\n'*}"
  }
  # Another writer (release-maintenance.yaml opens main's) can create the branch or
  # its pull request between this run's read and its write, which GitHub answers
  # with a 422. That writer's open pull request is then asked to merge, as an open
  # one found by the read is.
  concurrent() { # -> 0 once that writer's open pull request is asked to merge, 3 when none is open
    local url
    url=$(open_rebuild "$branch") || return 1
    [ -n "$url" ] || return 3
    echo "::notice::cplieger/${repo}: ${url} opened ${branch} concurrently, so its merge is asked for"
    request_merge "$url"
  }
  open=$(open_rebuild)
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
  # A branch with no open pull request holds a commit nobody vouches for, so it
  # is moved to this run's commit rather than opened as it is.
  move_branch() { # <why>
    echo "::notice::cplieger/${repo}: ${branch} $1, so it is moved to the new commit"
    gh api -X PATCH "repos/cplieger/${repo}/git/refs/heads/${branch}" -f sha="$commit" -F force=true >/dev/null
  }
  create_branch() { gh api -X POST "repos/cplieger/${repo}/git/refs" -f ref="refs/heads/${branch}" -f sha="$commit" >/dev/null 2>"$WORK/ref.err"; }
  branch_state() { # -> 0 when the branch reads as present, 3 on a 404, 1 on any other answer (printed)
    gh api "repos/cplieger/${repo}/git/ref/heads/${branch}" >/dev/null 2>"$WORK/branch.err" && return 0
    grep -q 'HTTP 404' "$WORK/branch.err" && return 3
    cat "$WORK/branch.err"
    return 1
  }
  if gh api "repos/cplieger/${repo}/git/ref/heads/${branch}" >/dev/null 2>&1; then
    move_branch "already exists without an open pull request"
  elif ! create_branch; then
    cat "$WORK/ref.err"
    grep -q 'HTTP 422' "$WORK/ref.err" || return 1
    rc=0
    concurrent || rc=$?
    case "$rc" in
      0) return 0 ;;
      3) ;;
      *) return 1 ;;
    esac
    # GitHub answers a ref create with a bare 422 whether or not it applied it
    # (github/rest-api-description#4887), so each refusal is judged by reading
    # the branch: present was created by someone, absent after the first
    # refusal is created once more.
    rc=0
    branch_state || rc=$?
    if [ "$rc" -eq 3 ] && create_branch; then
      rc=created
    elif [ "$rc" -eq 3 ]; then
      cat "$WORK/ref.err"
      rc=0
      branch_state || rc=$?
    fi
    case "$rc" in
      created) ;;
      0) move_branch "was created concurrently without an open pull request" ;;
      3)
        echo "::error::cplieger/${repo}: ${branch} could not be created and does not exist, so no rebuild pull request into ${base} was opened"
        return 1
        ;;
      *)
        echo "::error::cplieger/${repo}: ${branch} could not be created, and reading it again failed"
        return 1
        ;;
    esac
  fi
  body="${reason} Merging this empty commit rebuilds it against the base packages published since then; nothing else changes."
  if ! url=$(gh api -X POST "repos/cplieger/${repo}/pulls" \
    -f title="$REBUILD_SUBJECT" \
    -f head="$branch" -f base="$base" -f body="$body" --jq .html_url 2>"$WORK/pull.err"); then
    cat "$WORK/pull.err"
    grep -q 'HTTP 422' "$WORK/pull.err" || return 1
    rc=0
    concurrent || rc=$?
    [ "$rc" -ne 3 ] || {
      echo "::error::cplieger/${repo}: no rebuild pull request into ${base} was opened, and none is open"
      rc=1
    }
    return "$rc"
  fi
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

# Each base runs stale-pr as its own process, so one base's failure under set -e
# leaves the next base handled.
dispatch() { # <repo> <bases JSON: fanout's rows of that repo, main first>
  local repo=$1 rows base channel last age since unreadable outcome failed=0
  # U+001F, not a tab: read collapses adjacent whitespace separators, which would
  # shift every field after an empty since.
  if ! rows=$(jq -r 'if type == "array" and all(.[]; type == "object" and (.base | type) == "string")
      then .[] | [.base, .channel, .last, .age_days, (.since // ""), (.unreadable // "")] | join("\u001f")
      else error("not fanout rows") end' <<<"$2" 2>/dev/null); then
    echo "::error::dispatch: the bases of cplieger/${repo} are not fanout rows: $2" >&2
    exit 2
  fi
  {
    echo "| Repo | Base | Channel | Outcome |"
    echo "|---|---|---|---|"
  } >>"$GITHUB_STEP_SUMMARY"
  while IFS=$'\x1f' read -r base channel last age since unreadable; do
    [ -n "$base" ] || continue
    if [ -n "$unreadable" ]; then
      echo "::error::ghcr.io/cplieger/${repo}:${channel} (${base}) did not read, so its age is unknown and nothing was rebuilt. ${unreadable}"
      outcome=unreadable
    else
      echo "::group::cplieger/${repo}: ${base} (:${channel})"
      outcome="done"
      bash "${BASH_SOURCE[0]}" stale-pr "$repo" "$base" "$channel" "$age" "$last" "$since" </dev/null || outcome=failed
      echo "::endgroup::"
      [ "$outcome" = "done" ] || echo "::error::cplieger/${repo}: the rebuild of ${base} (:${channel}) failed. Its log is above."
    fi
    [ "$outcome" = "done" ] || failed=1
    echo "| ${repo} | ${base} | :${channel} | ${outcome} |" >>"$GITHUB_STEP_SUMMARY"
  done <<<"$rows"
  return "$failed"
}

fanout() {
  # Per-repo staleness overrides (days), keyed by repo name, e.g.
  #   INTERVAL_OVERRIDES[docker-keepalived]=3
  declare -A INTERVAL_OVERRIDES=()
  local two_branch listing matrix

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

  # One leg per repository, its rows adjacent and main first as two_branch_rows
  # appends them: GitHub has refused one of two concurrent pushes to one
  # repository (#845), and asks for mutating requests to be made serially
  # (https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api),
  # so dispatch handles a repository's bases in turn.
  matrix=$(jq -c 'reduce .[] as $r ([];
      if length > 0 and .[-1].repo == $r.repo then .[-1].bases += [$r | del(.repo)]
      else . + [{repo: $r.repo, bases: [$r | del(.repo)]}] end)
    | map(. + {readable: any(.bases[]; .unreadable == null)})' <<<"$STALE")
  echo "matrix=${matrix}" >>"$GITHUB_OUTPUT"
  count=$(jq '[.[] | select(.unreadable == null)] | length' <<<"$STALE")
  unreadable=$(jq '[.[] | select(.unreadable != null)] | length' <<<"$STALE")
  {
    echo ""
    echo "Rebuilding ${count} image(s)."
    [ "$unreadable" -eq 0 ] || echo "${unreadable} image(s) could not be read; each fails its repository's dispatch job."
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
  dispatch)
    [ "$#" -eq 3 ] || usage
    dispatch "$2" "$3"
    ;;
  *) usage ;;
esac
