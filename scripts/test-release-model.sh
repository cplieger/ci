#!/usr/bin/env bash
# End-to-end probe of the two-branch release model: a throwaway Go repository
# with the nested lane yamlenv lives through dev builds, promotions and stable
# runs. Release runs replay release.yaml's detect and go-nested jobs, and
# promotions promote.yaml's jobs, through scripts/workflow_replay.py;
# promote.py writes to a fake GitHub backed by the fixture's bare repository.
# The harness stands in for the publishing jobs: it tags what a run selected,
# creates the Release and records the receipt. CLIFF_BIN skips the download.
# shellcheck disable=SC2016 # expected values quote markdown backticks
set -euo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=probe GIT_AUTHOR_EMAIL=probe@example.invalid
export GIT_COMMITTER_NAME=probe GIT_COMMITTER_EMAIL=probe@example.invalid
export PYTHONDONTWRITEBYTECODE=1
unset GITHUB_TOKEN GH_TOKEN

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE_YAML="$ROOT/.github/workflows/release.yaml"
PROMOTE_YAML="$ROOT/.github/workflows/promote.yaml"
REPLAY="$ROOT/scripts/workflow_replay.py"
RS="$ROOT/scripts/release-state.sh"
WORK="$(mktemp -d /tmp/release-model-probe.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
export TMPDIR="$WORK/tmp"
mkdir -p "$TMPDIR"

PASS=0
fail() {
  echo "FAIL: $*" >&2
  exit 1
}
chk() { # label actual expected
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1))
    echo "ok: $1 -> $2"
  else
    fail "$1: expected '$3', got '$2'"
  fi
}
chk_has() { # label haystack needle
  case "$2" in
    *"$3"*)
      PASS=$((PASS + 1))
      echo "ok: $1"
      ;;
    *) fail "$1: output does not contain '$3'
--- output ---
$2" ;;
  esac
}

if [ -n "${CLIFF_BIN:-}" ]; then
  CLIFF="$CLIFF_BIN"
else
  VERSION=$(grep -m1 -oE 'CLIFF_VERSION=v[0-9.]+' "$RELEASE_YAML" | cut -d= -f2)
  SHA256=$(grep -m1 -oE 'CLIFF_SHA256=[a-f0-9]{64}' "$RELEASE_YAML" | cut -d= -f2)
  [ -n "$VERSION" ] && [ -n "$SHA256" ] || fail "no git-cliff pin found in $RELEASE_YAML"
  curl -fsSL --connect-timeout 10 --max-time 120 --retry 7 --retry-max-time 150 --retry-all-errors -o "$WORK/git-cliff.tgz" \
    "https://github.com/orhun/git-cliff/releases/download/${VERSION}/git-cliff-${VERSION#v}-x86_64-unknown-linux-gnu.tar.gz"
  echo "${SHA256}  ${WORK}/git-cliff.tgz" | sha256sum -c -
  tar xzf "$WORK/git-cliff.tgz" -C "$WORK" --strip-components=1 "git-cliff-${VERSION#v}/git-cliff"
  CLIFF="$WORK/git-cliff"
fi
"$CLIFF" --version >/dev/null || fail "git-cliff binary unusable"

# ── Stubs: gh (statuses, Releases, pulls), git-cliff on PATH, python3 ────────
BIN="$WORK/bin"
export GH_DIR="$WORK/gh" O="$WORK/app.git" REAL_PY
REAL_PY=$(command -v python3)
mkdir -p "$BIN" "$GH_DIR"
ln -s "$CLIFF" "$BIN/git-cliff"
cat >"$BIN/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GH_DIR/log"
[ "${1:-}" = api ] || { echo "stub: unexpected gh $*" >&2; exit 22; }
shift
jqf="" ep="" method=GET ctx="" paginate=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --jq) jqf=$2; shift 2 ;;
    -X) method=$2; shift 2 ;;
    -f) case "$2" in context=*) ctx=${2#context=} ;; esac; shift 2 ;;
    --paginate) paginate=1; shift ;;
    -*) echo "stub: unexpected gh flag $1" >&2; exit 22 ;;
    *) ep=$1; shift ;;
  esac
done
R=repos/o/app
case "$method $ep" in
  "GET $R/commits/"*"/statuses?per_page=100")
    [ "$paginate" = 1 ] || { echo "stub: statuses read without --paginate" >&2; exit 22; }
    sha=${ep#"$R"/commits/}
    f="$GH_DIR/statuses-${sha%%/*}.json"
    [ -f "$f" ] || echo '[]' >"$f"
    if [ -n "$jqf" ]; then jq -r "$jqf" "$f"; else cat "$f"; fi ;;
  "GET $R/pulls?state=closed&base="*"&sort=updated&direction=desc&per_page=100&page="*) echo '[]' ;;
  "GET $R/releases/tags/"*)
    tag=${ep#"$R"/releases/tags/}
    [ -f "$GH_DIR/release-${tag//\//%}" ] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
    echo '{}' ;;
  "POST $R/statuses/"*)
    sha=${ep#"$R"/statuses/}
    f="$GH_DIR/statuses-$sha.json"
    [ -f "$f" ] || echo '[]' >"$f"
    jq --arg c "$ctx" '. + [{context: $c, state: "success"}]' "$f" >"$f.new" && mv "$f.new" "$f" ;;
  *) echo "stub: unexpected gh $method $ep" >&2; exit 22 ;;
esac
SH
# promote.yaml's steps run `python3 scripts/promote.py`; that one call goes
# through promote-shim.py, everything else to the real interpreter.
cat >"$BIN/python3" <<'SH'
#!/usr/bin/env bash
if [ "${1:-}" = scripts/promote.py ]; then
  shift
  exec "$REAL_PY" "$GH_DIR/promote-shim.py" "$@"
fi
exec "$REAL_PY" "$@"
SH
chmod 755 "$BIN/gh" "$BIN/python3"
# promote.py with GitHub replaced by the bare repository: POST git/commits
# writes the commit as GitHub would (its own committer), PATCH refs/heads/main
# honours force=false as a fast-forward check.
cat >"$GH_DIR/promote-shim.py" <<'PY'
import json, os, subprocess, sys, types
sys.path.insert(0, os.path.abspath('scripts'))
import promote

ORIGIN = os.environ['O']
LOG = os.path.join(os.environ['GH_DIR'], 'github.log')
GITHUB = {'GIT_COMMITTER_NAME': 'GitHub', 'GIT_COMMITTER_EMAIL': 'noreply@github.com'}


def log(line):
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def git(*args, stdin=None):
    return subprocess.run(['git', '-C', ORIGIN, *args], input=stdin, capture_output=True, text=True,
                          check=True, env={**os.environ, **GITHUB}).stdout.strip()


def gh_json(path):
    log(f'GET {path}')
    if path == 'repos/cplieger/app':
        return {'name': 'app', 'default_branch': 'dev', 'visibility': 'public', 'archived': False, 'fork': False}
    if path.startswith('repos/cplieger/app/git/commits/'):
        sha = path.rsplit('/', 1)[1]
        return {'sha': sha, 'tree': {'sha': git('rev-parse', f'{sha}^{{tree}}')},
                'parents': [{'sha': p} for p in git('rev-list', '--parents', '-n1', sha).split()[1:]],
                'message': git('log', '-1', '--format=%B', sha)}
    raise promote.GhError(f'fake GitHub: unexpected GET {path}')


def gh_send(method, path, body):
    log(f'{method} {path} {json.dumps(body, sort_keys=True)}')
    if (method, path) == ('POST', 'repos/cplieger/app/git/commits'):
        parents = [a for p in body['parents'] for a in ('-p', p)]
        sha = git('commit-tree', body['tree'], *parents, '-F', '-', stdin=body['message'])
        return {'sha': sha, 'tree': {'sha': git('rev-parse', f'{sha}^{{tree}}')},
                'parents': [{'sha': p} for p in git('rev-list', '--parents', '-n1', sha).split()[1:]],
                'message': git('log', '-1', '--format=%B', sha)}
    if (method, path) == ('PATCH', 'repos/cplieger/app/git/refs/heads/main'):
        current = git('rev-parse', 'refs/heads/main')
        ff = subprocess.run(['git', '-C', ORIGIN, 'merge-base', '--is-ancestor', current, body['sha']]).returncode == 0
        if not body.get('force') and not ff:
            raise promote.GhError('HTTP 422: Update is not a fast forward')
        git('update-ref', 'refs/heads/main', body['sha'], current)
        return {'object': {'sha': body['sha']}}
    raise promote.GhError(f'fake GitHub: unexpected {method} {path}')


promote.CLONE_URL = os.path.join(os.path.dirname(ORIGIN), '{repo}.git')
promote.gh_json = gh_json
promote.gh_send = gh_send
promote.load_classify = lambda: types.SimpleNamespace(
    sync_owned_patterns=lambda: ['.editorconfig'], canonical_sources=lambda repo: {},
    ClassifyError=type('ClassifyError', (Exception,), {}))
sys.exit(promote.main(sys.argv[1:]))
PY
export PATH="$BIN:$PATH" GITHUB_REPOSITORY=o/app GITHUB_SERVER_URL=https://gh GITHUB_RUN_ID=7

# ── The fixture: commits written with plumbing straight into the bare origin ─
git init -q --bare -b main "$O"
commit_on() { # <branch> <message> [path=content | path=@file]... -> the new commit, branch advanced
  local b=$1 msg=$2 idx parent tree c kv blob
  shift 2
  idx="$WORK/index.$$"
  rm -f "$idx"
  parent=$(git -C "$O" rev-parse --verify -q "refs/heads/$b" || true)
  if [ -n "$parent" ]; then GIT_INDEX_FILE=$idx git -C "$O" read-tree "$parent"; fi
  for kv in "$@"; do
    case "${kv#*=}" in
      @*) blob=$(git -C "$O" hash-object -w "${kv#*=@}") ;;
      *) blob=$(printf '%b\n' "${kv#*=}" | git -C "$O" hash-object -w --stdin) ;;
    esac
    GIT_INDEX_FILE=$idx git -C "$O" update-index --add --cacheinfo "100644,$blob,${kv%%=*}"
  done
  tree=$(GIT_INDEX_FILE=$idx git -C "$O" write-tree)
  rm -f "$idx"
  N=$(($(cat "$WORK/commits" 2>/dev/null || echo 0) + 1))
  echo "$N" >"$WORK/commits"
  c=$(GIT_AUTHOR_DATE="2026-10-01T00:00:00Z +${N}min" GIT_COMMITTER_DATE="@$((1790000000 + N * 60)) +0000" \
    git -C "$O" commit-tree "$tree" ${parent:+-p "$parent"} -m "$msg")
  git -C "$O" update-ref "refs/heads/$b" "$c" ${parent:+"$parent"}
  echo "$c"
}
gomod() { printf 'module github.com/o/app\\n\\ngo 1.27\\n\\nrequire example.org/dep %s' "$1"; }
head_of() { git -C "$O" rev-parse "refs/heads/$1"; }
tag_of() { git -C "$O" rev-list -n1 "$1" 2>/dev/null || true; }
published() { # <tag> <commit> <receipt yes|no>: what the publishing jobs leave behind
  git -C "$O" tag "$1" "$2"
  touch "$GH_DIR/release-${1//\//%}"
  if [ "$3" = yes ]; then
    (cd "$WORK" && bash "$RS" receipt "$1" "$2") >/dev/null || fail "release-state.sh receipt $1 failed"
  fi
}
has_word() {
  case " $1 " in *" $2 "*) return 0 ;; esac
  return 1
}

C0=$(commit_on main "feat: initial" cliff.toml=@"$ROOT/configs/cliff-stable.toml" \
  go.mod="$(gomod v1.2.0)" main.go='package main\n// v1' \
  yamlenv/go.mod='module github.com/o/app/yamlenv\n\ngo 1.27' yamlenv/y.go='package yamlenv\n// v1')
git -C "$O" update-ref refs/heads/dev "$C0"
published v1.0.0 "$C0" yes
published yamlenv/v1.0.0 "$C0" yes

# ── A release run: release.yaml's detect, then go-nested per lane ───────────
# env: AT (commit, default the branch head), NO_RECEIPT and FAIL_LANES (lane
# keys, '.' the root), NO_PUBLISH=1 (the run dies before its first tag),
# DEFAULT_BRANCH (default dev), TREE (the ci tree whose release.yaml and
# scripts run, default this one).
# Sets RUN (its clone); result() prints the tags it published, or "refused: <why>".
run_release() { # <branch> <label>
  local branch=$1 label=$2 sha before outs dir rel ver lane_ctx repairs tree=${TREE:-$ROOT}
  RUN="$WORK/run-$label"
  git clone -q "$O" "$RUN"
  sha=${AT:-$(git -C "$RUN" rev-parse "origin/$branch")}
  git -C "$RUN" checkout -q --detach "$sha"
  before=$(git -C "$RUN" rev-parse "$sha^1")
  jq -n --arg sha "$sha" --arg ref "refs/heads/$branch" --arg before "$before" --arg default "${DEFAULT_BRANCH:-dev}" '{
    github: {sha: $sha, ref: $ref, repository: "o/app", token: "", server_url: "https://gh",
      event: {before: $before, inputs: {}, repository: {default_branch: $default, name: "app", license: {spdx_id: "Apache-2.0"}}}},
    inputs: {}, secrets: {}, needs: {}, matrix: {}}' >"$RUN.ctx"
  if ! GITHUB_SHA=$sha GITHUB_REF="refs/heads/$branch" "$REAL_PY" "$REPLAY" --workflow "$tree/.github/workflows/release.yaml" \
    --job detect --actions-root "$tree" --context "$RUN.ctx" --cwd "$RUN" --out "$RUN.detect.json" \
    --skip git-cliff-version/install 2>"$RUN.err"; then
    echo "refused: $(grep -m1 -oE '::error::.*' "$RUN.err" || tail -n3 "$RUN.err")" >"$RUN.result"
    return 0
  fi
  outs=$(jq -c .outputs "$RUN.detect.json")
  repairs=$(jq -r '.repairs // ""' <<<"$outs")
  local printed=""
  if [ -n "$repairs" ] && [ "$repairs" != '[]' ]; then
    while IFS=$'\t' read -r tag commit; do
      touch "$GH_DIR/release-${tag//\//%}"
      (cd "$WORK" && bash "$RS" receipt "$tag" "$commit") >/dev/null || fail "repair receipt of $tag failed"
      printed+="repaired:$tag@${commit:0:12} "
    done < <(jq -r '.[] | [.tag, .commit] | @tsv' <<<"$repairs")
  fi
  if [ "$(jq -r .release <<<"$outs")" = true ] && [ -z "${NO_PUBLISH:-}" ]; then
    ver=$(jq -r .version <<<"$outs")
    if has_word "${NO_RECEIPT:-}" .; then published "$ver" "$sha" no; else published "$ver" "$sha" yes; fi
    printed+="$ver "
  fi
  for dir in $(jq -r '.go_modules_to_release | if . == "" then [] else fromjson end | .[]' <<<"$outs"); do
    lane_ctx="$RUN.lane-${dir//\//_}"
    jq --argjson outs "$outs" --arg dir "$dir" --arg repair "$([ -n "$repairs" ] && [ "$repairs" != '[]' ] && echo success || echo skipped)" \
      '.needs = {detect: {result: "success", outputs: $outs}, repair: {result: $repair}, barrier: {result: "skipped"}}
       | .matrix = {dir: $dir}' "$RUN.ctx" >"$lane_ctx.ctx"
    GITHUB_SHA=$sha "$REAL_PY" "$REPLAY" --workflow "$tree/.github/workflows/release.yaml" --job go-nested \
      --actions-root "$tree" --context "$lane_ctx.ctx" --cwd "$RUN" --out "$lane_ctx.json" --skip git-cliff-version/install --stop-before "Tag + GitHub Release (lane)" 2>"$lane_ctx.err" \
      || fail "$label: go-nested $dir failed: $(tail -n5 "$lane_ctx.err")"
    rel=$(jq -r '.steps.lane.outputs.release' "$lane_ctx.json")
    ver=$(jq -r '.steps.lane.outputs.version' "$lane_ctx.json")
    if [ "$rel" = true ] && [ -z "${NO_PUBLISH:-}" ] && ! has_word "${FAIL_LANES:-}" "$dir"; then
      if has_word "${NO_RECEIPT:-}" "$dir"; then published "$ver" "$sha" no; else published "$ver" "$sha" yes; fi
      printed+="$ver "
    fi
    [ ! -f "$RUN/NOTES.md" ] || mv "$RUN/NOTES.md" "$lane_ctx.notes"
  done
  echo "${printed% }" >"$RUN.result"
}
result() { cat "$RUN.result"; }
detect() { jq -r ".outputs.$1" "$RUN.detect.json"; }
lane_state() { detect lane_state | jq -r --arg k "$1" ".[\$k].$2"; }
step_out() { jq -r ".steps.$1.outputs.$2" "$RUN.detect.json"; }
lane_notes() { head -n1 "$RUN.lane-$1.notes"; }

# ── A promotion: promote.yaml's jobs ─────────────────────────────────────────
# env: TARGET, DRY_RUN=true, TAMPER=1 (apply sees forged preview outputs),
# PREVIEW_FAILS=1, BETWEEN (command run after preview, before apply),
# BEFORE_WRITE (command run before move's Promote step), IMAGE_DIGEST (apply
# outputs it as an image lane would; tag's GHCR write is skipped and move stops
# before Promote). Prints "promoted <R>", "dry run", "move reached Promote",
# "not moved: move skipped" or "refused: <check output>".
run_promote() { # <label>
  local P="$WORK/promote-$1" snap preview_res forged preview_hook=()
  jq -n --arg target "${TARGET:-}" --argjson dry "${DRY_RUN:-false}" '{
    github: {token: "", workflow: "Promote", server_url: "https://gh", repository: "cplieger/ci", run_id: "7"},
    inputs: {repo: "app", target: $target, dry_run: $dry}, secrets: {PROMOTE_PAT: "pat", PACKAGES_PAT: "packages"}, needs: {}, matrix: {}}' >"$P.ctx"
  "$REAL_PY" "$REPLAY" --workflow "$PROMOTE_YAML" --job snapshot --context "$P.ctx" --cwd "$ROOT" --out "$P.snapshot.json" \
    2>"$P.err" || {
    echo "refused: $(grep -oE '::error::.*' "$P.err" | tr '\n' ' ')"
    return 0
  }
  snap=$(jq -c '{result: "success", outputs: .outputs}' "$P.snapshot.json")
  jq --argjson snap "$snap" '.needs = {snapshot: $snap}' "$P.ctx" >"$P.preview.ctx"
  preview_res=success
  if [ -n "${PREVIEW_FAILS:-}" ]; then preview_hook=(--hook "Preview=exit 1"); fi
  "$REAL_PY" "$REPLAY" --workflow "$PROMOTE_YAML" --job preview --context "$P.preview.ctx" --cwd "$ROOT" \
    --out "$P.preview.json" --skip "Install git-cliff" "${preview_hook[@]}" 2>"$P.preview.err" || preview_res=failure
  forged='{}'
  if [ -n "${TAMPER:-}" ]; then
    forged=$(jq -nc --arg x "$(head_of dev)" --arg m "$(head_of main)" '{repo: "evil", target: "0000000000000000000000000000000000000000", main: $m, dev: $x}')
  fi
  jq --argjson snap "$snap" --arg pr "$preview_res" --argjson forged "$forged" \
    '.needs = {snapshot: $snap, preview: {result: $pr, outputs: $forged}}' "$P.ctx" >"$P.apply.ctx"
  if [ -n "${BETWEEN:-}" ]; then eval "$BETWEEN" >/dev/null; fi
  if ! "$REAL_PY" "$REPLAY" --workflow "$PROMOTE_YAML" --job apply --context "$P.apply.ctx" --cwd "$ROOT" --out "$P.apply.json" \
    --skip "Install Cosign" --skip "Install Trivy and its database" 2>"$P.err"; then
    echo "refused: $(grep -oE '::error::.*' "$P.err" | tr '\n' ' ')"
    return 0
  fi
  if jq -e '.ran | index("Create R (if false)")' "$P.apply.json" >/dev/null; then
    echo "dry run"
    return 0
  fi
  local apply hook=() tag_res tag_args=() move_args=()
  apply=$(jq -c --arg d "${IMAGE_DIGEST:-}" '{result: "success", outputs: (.outputs + (if $d == "" then {} else {digest: $d} end))}' "$P.apply.json")
  jq --argjson snap "$snap" --arg pr "$preview_res" --argjson apply "$apply" \
    '.needs = {snapshot: $snap, preview: {result: $pr, outputs: {}}, apply: $apply}' "$P.ctx" >"$P.tag.ctx"
  if [ -n "${IMAGE_DIGEST:-}" ]; then
    tag_args=(--skip "Tag the promoted image")
    move_args=(--stop-before Promote)
  fi
  "$REAL_PY" "$REPLAY" --workflow "$PROMOTE_YAML" --job tag --context "$P.tag.ctx" --cwd "$ROOT" --out "$P.tag.json" \
    "${tag_args[@]}" 2>"$P.err" || {
    echo "refused: $(grep -oE '::error::.*' "$P.err" | tr '\n' ' ')"
    return 0
  }
  tag_res=$(jq -r 'if .skipped then "skipped" else "success" end' "$P.tag.json")
  jq --arg t "$tag_res" '.needs.tag = {result: $t, outputs: {}}' "$P.tag.ctx" >"$P.move.ctx"
  if [ -n "${BEFORE_WRITE:-}" ]; then hook=(--hook "Promote=$BEFORE_WRITE"); fi
  if ! "$REAL_PY" "$REPLAY" --workflow "$PROMOTE_YAML" --job move --context "$P.move.ctx" --cwd "$ROOT" --out "$P.move.json" \
    "${hook[@]}" "${move_args[@]}" 2>"$P.err"; then
    echo "refused: $(grep -oE '::error::.*' "$P.err" | tr '\n' ' ')"
    return 0
  fi
  if jq -e '.skipped' "$P.move.json" >/dev/null; then
    echo "not moved: move skipped"
    return 0
  fi
  if [ -n "${IMAGE_DIGEST:-}" ]; then
    echo "move reached Promote"
    return 0
  fi
  echo "promoted $(head_of main)"
}
apply_read_preview() { jq -r '[.expressions[] | select(test("needs\\.preview"))] | length' "$WORK/promote-$1.apply.json"; }
is_r() { (cd "$O" && . "$ROOT/scripts/reconciliation.sh" && is_reconciliation "$1" && echo yes) || echo no; }
parents_of() { git -C "$O" rev-list --parents -n1 "$1" | cut -d' ' -f2-; }
BUILT='Built from `main` for dependency and system-package updates'

# ── 1. A nested-only promotion ───────────────────────────────────────────────
Y1=$(commit_on dev "fix(yamlenv): parse anchors" yamlenv/y.go='package yamlenv\n// v2')
run_release dev y1
chk "M1 a lane's first dev build takes the lane's next minor" "$(result)" "yamlenv/v1.1.0-dev.1"
out=$(run_promote r0)
R0=$(head_of main)
chk "M2 the nested-only promotion moved main to a reconciliation of the dev head" "$out|$(is_r "$R0")|$(parents_of "$R0")" \
  "promoted $R0|yes|$C0 $Y1"
run_release main r0
chk "M3 at R0 the lane publishes the promotion's lane minor, the root nothing" "$(result)" "yamlenv/v1.1.0"
chk "M3 the root lane is not pending at a nested-only R" "$(lane_state . commit)|$(lane_state . in_range)|$(detect root_changed)" "|false|false"
chk "M3 the lane's notes open with its promotion" "$(lane_notes yamlenv)" 'Promoted from `yamlenv/v1.1.0-dev.1`'

# ── 2. main's own commits are patches, whatever their type ───────────────────
M1=$(commit_on main "feat(deps): update module example.org/dep to v1.3.0" go.mod="$(gomod v1.3.0)")
run_release main m1
chk "M4 a feat on main with nothing pending publishes a patch" "$(result)" "v1.0.1"
chk "M4 although git-cliff read a minor" "$(step_out cliff change_level)" "minor"
chk "M4 and the release says it was built from main" "$(detect release_kind_note)" "$BUILT"
chk "M4 the nested-only promotion is no longer pending for the lane" "$(lane_state yamlenv in_range)|$(detect go_modules_to_release)" 'false|[]'

# ── 3. dev floors over a stable tag it cannot reach ──────────────────────────
D1=$(commit_on dev "fix: trim the greeting" main.go='package main\n// v2')
chk "M5 v1.0.1 is on main only" "$(git -C "$O" merge-base --is-ancestor "$M1" "$D1" && echo reachable || echo unreachable)" "unreachable"
run_release dev d1
chk "M5 a fixes-only dev build takes the next minor above that tag" "$(result)" "v1.1.0-dev.1"
chk "M5 its range starts past everything main shipped" "$(step_out cliff range_from)|$(step_out cliff h_tag)" "$M1|v1.0.1"

# ── 4. dominance: dev must carry what main shipped ───────────────────────────
out=$(run_promote d1)
chk_has "M6 a dev lacking main's dependency bump is refused" "$out" "refused:"
chk_has "M6 naming the record" "$out" "example.org/dep"
chk "M6 and main is untouched" "$(head_of main)" "$M1"
D2=$(commit_on dev "fix(deps): update module example.org/dep to v1.3.0" go.mod="$(gomod v1.3.0)")
run_release dev d2
chk "M7 the dev twin of the bump builds" "$(result)" "v1.1.0-dev.2"

# ── 5. the promotion, with forged preview outputs ────────────────────────────
out=$(TAMPER=1 run_promote r1)
R1=$(head_of main)
chk "M8 apply promotes snapshot's T over snapshot's M, whatever preview output" "$out|$(parents_of "$R1")" "promoted $R1|$M1 $D2"
chk "M8 R1 has dev's tree and the reconciliation shape" \
  "$(git -C "$O" rev-parse "$R1^{tree}")|$(is_r "$R1")" "$(git -C "$O" rev-parse "$D2^{tree}")|yes"
chk "M8 apply never evaluated a preview output" "$(apply_read_preview r1)" "0"
chk_has "M8 the preview rendered the version the stable run will publish" \
  "$(cat "$(jq -r .runner_temp "$WORK/promote-r1.preview.json")/summary.md")" "### root v1.1.0"

# ── 6. dev numbers above an untagged promotion ───────────────────────────────
commit_on dev "feat: add a farewell" main.go='package main\n// v3' >/dev/null
run_release dev d3
chk "M9 with R1 untagged, the next dev build ranks above the promotion" "$(result)" "v1.2.0-dev.1"
chk "M9 it numbered R1 itself" "$(lane_state . commit)|$(lane_state . version)" "$R1|v1.1.0"

# ── 7. the stable run at R1, whose receipt never lands ───────────────────────
NO_RECEIPT=. run_release main r1
chk "M10 the root-only promotion publishes its minor at R1" "$(result)" "v1.1.0"
chk "M10 with the promoted build in its kind line" "$(detect release_kind_note)" 'Promoted from `v1.1.0-dev.2`'
chk "M10 and leaves the lane alone" "$(lane_state yamlenv commit)|$(detect go_modules_to_release)" '|[]'
commit_on dev "fix: say goodbye politely" main.go='package main\n// v4' >/dev/null
run_release dev d4
chk "M11 tagged before its receipt, the promotion's tag is dev's floor" "$(result)" "v1.2.0-dev.2"
chk "M11 H is that tag, with nothing left pending" "$(lane_state . h_tag)|$(lane_state . commit)" "v1.1.0|"

# ── 8. the next main commit repairs it first ─────────────────────────────────
S1=$(commit_on main "fix(deps): update module example.org/dep to v1.3.1" go.mod="$(gomod v1.3.1)")
run_release main s1
chk "M12 the unreceipted version is completed at its own commit before S1 publishes a patch" \
  "$(result)" "repaired:v1.1.0@${R1:0:12} v1.1.1"
chk "M12 S1 is built from main" "$(detect release_kind_note)" "$BUILT"

# ── 9. a multi-lane promotion whose lane fails, then S ───────────────────────
commit_on dev "fix(deps): update module example.org/dep to v1.3.1" go.mod="$(gomod v1.3.1)" >/dev/null
run_release dev d5
chk "M13 dev builds the bump" "$(result)" "v1.2.0-dev.3"
Y2=$(commit_on dev "fix(yamlenv): handle aliases" yamlenv/y.go='package yamlenv\n// v3')
run_release dev y2
chk "M13 and the lane fix" "$(result)" "yamlenv/v1.2.0-dev.1"
out=$(run_promote r2)
R2=$(head_of main)
chk "M14 the root-and-lane promotion" "$out|$(parents_of "$R2")" "promoted $R2|$S1 $Y2"
FAIL_LANES=yamlenv run_release main r2
chk "M14 at R2 the root publishes and the lane's publish fails" "$(result)" "v1.2.0"
chk "M14 both lanes were pending" "$(lane_state . commit)|$(lane_state yamlenv commit)|$(lane_state yamlenv version)" \
  "$R2|$R2|yamlenv/v1.2.0"
commit_on main "fix(deps): update module example.org/dep to v1.3.2" go.mod="$(gomod v1.3.2)" >/dev/null
run_release main s2
chk "M15 at S the root is a patch and the failed lane publishes the promotion" "$(result)" "v1.2.1 yamlenv/v1.2.0"
chk "M15 the lane's notes say S carried the promotion" "$(lane_notes yamlenv)" \
  'Built from `main`, including the promotion of `yamlenv/v1.2.0-dev.1`'

# ── 10. a promoted breaking change, published by a later S ───────────────────
commit_on dev "fix(deps): update module example.org/dep to v1.3.2" go.mod="$(gomod v1.3.2)" >/dev/null
run_release dev d6
chk "M16 dev builds the bump" "$(result)" "v1.3.0-dev.1"
commit_on dev "feat!: rename the greeting flag" main.go='package main\n// v5' >/dev/null
run_release dev d7
chk "M16 a breaking dev build reserves the next major" "$(result)" "v2.0.0-dev.1"
out=$(run_promote r3)
R3=$(head_of main)
chk "M17 the breaking promotion" "$out" "promoted $R3"
commit_on dev "fix: polish" main.go='package main\n// v6' >/dev/null
run_release dev d8
chk "M18 dev ranks above the untagged major" "$(result)" "v2.1.0-dev.1"
chk "M18 the promotion is numbered the major" "$(lane_state . version)" "v2.0.0"
NO_PUBLISH=1 run_release main r3-failed
chk "M19 R3's own run selects the major and dies before tagging" "$(result)" ""
chk "M19 its version" "$(detect version)|$(detect release)" "v2.0.0|true"
S3=$(commit_on main "fix(deps): update module example.org/dep to v1.3.3" go.mod="$(gomod v1.3.3)")
run_release main s3
chk "M20 S, merged before R3 was tagged, publishes the major" "$(result)" "v2.0.0"
chk "M20 and says it carried the promotion" "$(detect release_kind_note)" 'Built from `main`, including the promotion of `v2.0.0-dev.1`'
AT=$R3 run_release main r3-rerun
out=$(result)
chk_has "M21 a re-run at R3 is refused" "$out" "refused:"
chk_has "M21 naming the tag it is behind" "$out" "the highest stable tag v2.0.0 is on $S3"
commit_on dev "fix: tidy" main.go='package main\n// v7' >/dev/null
run_release dev d9
chk "M22 the next dev build ranks above the published major" "$(result)" "v2.1.0-dev.2"

# ── 11. main moving under a promotion ────────────────────────────────────────
commit_on dev "fix(deps): update module example.org/dep to v1.3.3" go.mod="$(gomod v1.3.3)" >/dev/null
out=$(BETWEEN='S4=$(commit_on main "fix(deps): update module example.org/dep to v1.3.4" go.mod="$(gomod v1.3.4)")' run_promote moved)
S4=$(head_of main)
chk_has "M23 main moved between snapshot and apply: refused" "$out" "main moved since the snapshot"
chk "M23 main keeps the commit that moved it" "$(git -C "$O" log -1 --format=%s "$S4")" \
  "fix(deps): update module example.org/dep to v1.3.4"
commit_on dev "fix(deps): update module example.org/dep to v1.3.4" go.mod="$(gomod v1.3.4)" >/dev/null
git -C "$O" update-ref refs/heads/race "$S4"
RACE=$(commit_on race "fix(deps): update module example.org/dep to v1.3.5" go.mod="$(gomod v1.3.5)")
out=$(BEFORE_WRITE="git -C '$O' update-ref refs/heads/main $RACE $S4" run_promote race)
chk_has "M24 main moved between the checks and the write: the update is refused" "$out" "not a fast forward"
chk "M24 main is the commit that moved it" "$(head_of main)" "$RACE"
chk_has "M24 the write asked for force=false" "$(grep PATCH "$GH_DIR/github.log" | tail -n1)" '"force": false'
commit_on dev "fix(deps): update module example.org/dep to v1.3.5" go.mod="$(gomod v1.3.5)" >/dev/null
M=$(head_of main)
chk "M25 a dry run passes every check and moves nothing" "$(DRY_RUN=true run_promote dry)|$(head_of main)" "dry run|$M"
out=$(PREVIEW_FAILS=1 IMAGE_DIGEST="sha256:$(printf '%064d' 0 | tr 0 d)" run_promote image-preview-failed)
chk "M25 a failed preview still tags an image lane and reaches move" \
  "$out|$(jq -r .failed "$WORK/promote-image-preview-failed.preview.json")|$(jq -r '.ran | join(",")' "$WORK/promote-image-preview-failed.tag.json")|$(head_of main)" \
  "move reached Promote|the hook before 'Preview' exited 1|Checkout (checkout: --cwd),Tag the promoted image (skipped)|$M"
out=$(PREVIEW_FAILS=1 run_promote r4)
R4=$(head_of main)
chk "M25 then the promotion, its preview failed too" "$out|$(is_r "$R4")" "promoted $R4|yes"

# ── 12. nothing to publish ───────────────────────────────────────────────────
commit_on dev "fix(deps): rebuild the image" >/dev/null
out=$(run_promote empty)
chk_has "M26 a dev holding only an empty rebuild beyond main is refused" "$out" "nothing to publish"
chk "M26 main is untouched" "$(head_of main)" "$R4"
run_release main r4
chk "M27 R4 publishes its minor" "$(result)" "v2.1.0"
commit_on main "chore(deps): lock file maintenance" package-lock.json='{"lockfileVersion": 3}' >/dev/null
run_release main lock
chk "M27 a lockfile-only commit on main publishes nothing" "$(result)" ""
chk "M27 no lane ships it" "$(detect root_changed)|$(detect go_modules_to_release)|$(detect release)" 'false|[]|false'

chk "M27 the run checked out the ci source and ran compute from it" \
  "$(jq -r '[.ran[] | select(test("ci source|Compute version"))] | join(",")' "$RUN.detect.json")" \
  "Check out the ci source (checkout: --actions-root),Compute version (cliff)"

# ── 13. a main-default repository is refused ─────────────────────────────────
commit_on main "fix: a main-only fix" main.go='package main\n// main-only' >/dev/null
DEFAULT_BRANCH=main run_release main maindefault
chk "M28 a main-default repository's release run is refused at detect" "$(result)" \
  "refused: ::error::release.yaml publishes only from a public, non-fork repository whose default branch is dev (this one: default branch main, private <unknown>, fork <unknown>). Make dev the default branch, or remove the release.yaml caller and publish from the repository's own workflow."
chk "M28 running no step after Select channel" "$(jq -r '.ran | join(",")' "$RUN.detect.json")" "Checkout (checkout: --cwd),Select channel"

echo "PASS: two-branch release model end to end ($PASS checks)"
