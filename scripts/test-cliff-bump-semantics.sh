#!/usr/bin/env bash
# Regression probe for the git-cliff behaviours the release gate relies on and
# upstream does not document (issues #816, #1570): exclude_paths globbing, the
# --unreleased --bumped-version base anchoring, section ordering, the stable
# tag_pattern, the nested-module lanes and the two release channels through
# actions/git-cliff-version/compute.sh, as states A to R; CONTRIBUTING.md lists
# them. Runs in the ci repo's scripts CI job, so a git-cliff pin bump re-runs
# it against the new binary. CLIFF_BIN=/path/to/git-cliff skips the download.
set -euo pipefail

# Hermetic git: a developer's global config must not reach the probe's
# throwaway repos. Found the hard way (2026-08): a workstation with
# `tag.gpgsign true` turned every bare `git tag vX.Y.Z` below into a SIGNED
# ANNOTATED tag, which opened an editor on the PTY and hung the probe under
# `ci-local` — while CI, with no global config, stayed green. Pointing
# GIT_CONFIG_GLOBAL/SYSTEM at /dev/null makes the probe see only per-repo
# config, so the per-repo `commit.gpgsign false` lines become belt-and-braces
# and no future global setting (aliases, hooks path, pagers) can leak in.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$ROOT/configs/cliff-stable.toml"
WORK="$(mktemp -d /tmp/cliff-probe.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# ── Pin consistency: every CLIFF_VERSION and CLIFF_SHA256 must agree ────────
# The version and the tarball sha256 are pinned as a pair at every install
# site (Renovate maintains both via the custom.git-cliff datasource). Four
# invariants, each catching a drift class the others cannot see:
#   uniqueness    — a site whose version or digest disagrees with the rest
#   site count    — a site added/removed without bumping EXPECTED_SITES
#                   (forces conscious review of every install-site change;
#                   symmetric pair removal fools a bare N_VER==N_SHA check)
#   enforcement   — every site's `sha256sum -c` gate line still exists (a
#                   deleted gate leaves both pin lines intact and the site
#                   silently reverts to unverified install)
#   manager shape — every site matches the Renovate customManager block
#                   (annotation + adjacent version/sha lines; the regex
#                   below MIRRORS cplieger/.github default.json's
#                   custom.git-cliff manager — keep them in sync), so every
#                   site actually auto-updates; a corrupted annotation or a
#                   typoed depName would silently freeze that site (and a
#                   wrong depName would also bypass the digest-only
#                   no-automerge packageRule, which matches on depName)
# The file list is DISCOVERED (any file under .github/ or actions/ carrying
# a CLIFF_VERSION pin), so a new install site in a new file cannot hide
# from these checks.
EXPECTED_SITES=6
mapfile -t PIN_FILES < <(
  grep -rlE 'CLIFF_VERSION[=:]' "$ROOT/.github" "$ROOT/actions" | sort
)
[ "${#PIN_FILES[@]}" -ge 5 ] || fail "pin-file discovery found only ${#PIN_FILES[@]} files (expected the release/docker-release/self-release/promote workflows + the composite action at minimum)"
mapfile -t pins < <(
  grep -rhoE 'CLIFF_VERSION[=:] *"?v[0-9][0-9.]*' "${PIN_FILES[@]}" \
    | grep -oE 'v[0-9][0-9.]*' | sort -u
)
[ "${#pins[@]}" -eq 1 ] || fail "CLIFF_VERSION pins disagree across workflows/action: ${pins[*]}"
VERSION="${pins[0]}"
mapfile -t shas < <(
  grep -rhoE 'CLIFF_SHA256[=:] *"?[a-f0-9]{64}' "${PIN_FILES[@]}" \
    | grep -oE '[a-f0-9]{64}' | sort -u
)
[ "${#shas[@]}" -eq 1 ] || fail "CLIFF_SHA256 pins disagree across workflows/action: ${shas[*]}"
SHA256="${shas[0]}"
# Per-class counts. N_SHA additionally guards well-formedness: a malformed
# sha (63 hex chars) falls out of the {64} scan while the remaining sites
# still "agree" on uniqueness (found by this probe's own negative test).
N_VER=$(grep -rhoE 'CLIFF_VERSION[=:] *"?v[0-9][0-9.]*' "${PIN_FILES[@]}" | wc -l)
N_SHA=$(grep -rhoE 'CLIFF_SHA256[=:] *"?[a-f0-9]{64}' "${PIN_FILES[@]}" | wc -l)
N_GATE=$(grep -rh 'CLIFF_SHA256' "${PIN_FILES[@]}" | grep -c 'sha256sum -c' || true)
N_ANNOT=$(grep -rh 'renovate: datasource=custom\.git-cliff depName=orhun/git-cliff' "${PIN_FILES[@]}" | grep -c . || true)
[ "$N_VER" -eq "$EXPECTED_SITES" ] || fail "expected $EXPECTED_SITES CLIFF_VERSION pins, found $N_VER — a site was added or removed; review it, then update EXPECTED_SITES here"
[ "$N_SHA" -eq "$EXPECTED_SITES" ] || fail "expected $EXPECTED_SITES well-formed CLIFF_SHA256 pins, found $N_SHA (a site's sha line is missing or malformed)"
[ "$N_GATE" -eq "$EXPECTED_SITES" ] || fail "expected $EXPECTED_SITES 'sha256sum -c' gate lines referencing CLIFF_SHA256, found $N_GATE (a site installs without verifying)"
[ "$N_ANNOT" -eq "$EXPECTED_SITES" ] || fail "expected $EXPECTED_SITES custom.git-cliff renovate annotations, found $N_ANNOT (a site will not auto-update, and a wrong depName bypasses the digest-only no-automerge rule)"
# Structural check: the annotation and its version/sha lines must be
# ADJACENT in the exact shape the Renovate manager matches — co-located
# counts alone cannot see an annotation separated from its pins.
N_BLOCKS=$(
  python3 - "${PIN_FILES[@]}" <<'PY'
import re, sys
pat = re.compile(
    r"#\s*renovate:\s*datasource=custom\.git-cliff\s+depName=orhun/git-cliff\s*\n"
    r"\s*CLIFF_VERSION[=:]\s*['\"]?[^'\"\s]+['\"]?\s*\n"
    r"\s*CLIFF_SHA256[=:]\s*['\"]?[a-f0-9]{64}"
)
print(sum(len(pat.findall(open(f, encoding="utf-8").read())) for f in sys.argv[1:]))
PY
)
[ "$N_BLOCKS" -eq "$EXPECTED_SITES" ] || fail "expected $EXPECTED_SITES complete annotation+version+sha blocks (Renovate manager shape), found $N_BLOCKS"
echo "pinned git-cliff: $VERSION / sha256 ${SHA256:0:12}… ($EXPECTED_SITES sites: consistent, paired, gated, manager-matched)"

# ── Binary: reuse CLIFF_BIN if provided, else download the pinned release ───
# The download verifies the SAME pinned digest the workflows enforce, so a
# Renovate bump whose digest does not match the actual asset fails here, in
# this repo's own PR gate, before it can reach any consumer.
if [ -n "${CLIFF_BIN:-}" ]; then
  CLIFF="$CLIFF_BIN"
else
  curl -fsSL --retry 7 --retry-max-time 150 --retry-all-errors -o "$WORK/git-cliff.tgz" \
    "https://github.com/orhun/git-cliff/releases/download/${VERSION}/git-cliff-${VERSION#v}-x86_64-unknown-linux-gnu.tar.gz"
  echo "${SHA256}  ${WORK}/git-cliff.tgz" | sha256sum -c -
  tar xzf "$WORK/git-cliff.tgz" -C "$WORK" --strip-components=1 "git-cliff-${VERSION#v}/git-cliff"
  CLIFF="$WORK/git-cliff"
fi
"$CLIFF" --version >/dev/null || fail "git-cliff binary unusable"

bump() { (cd "$1" && "$CLIFF" --config "$CFG" --unreleased --bumped-version 2>/dev/null); }
render() { (cd "$1" && "$CLIFF" --config "$CFG" --unreleased --strip header 2>/dev/null); }

assert_eq() { # actual expected label
  [ "$1" = "$2" ] || fail "$3: expected '$2', got '$1'"
  echo "ok: $3 -> $1"
}

R="$WORK/repo"
mkdir -p "$R"
git -C "$R" init -q -b main
git -C "$R" config user.email probe@ci.local
git -C "$R" config user.name probe
git -C "$R" config commit.gpgsign false

c() { # path message
  mkdir -p "$R/$(dirname "$1")"
  echo "x$RANDOM" >>"$R/$1"
  git -C "$R" add -A
  git -C "$R" commit -qm "$2"
}

# ── Baseline ────────────────────────────────────────────────────────────────
c src/main.go "feat: initial"
git -C "$R" tag v1.0.0

# ── State A: unreleased set fully excluded -> anchor at latest tag ──────────
c .github/workflows/ci.yaml "chore(deps): update ci digest to aaaaaaa"
c README.md "fix: readme-only edit"
c web/package-lock.json "chore(deps): lock file maintenance"
assert_eq "$(bump "$R")" "v1.0.0" "A: excluded-only unreleased set anchors at latest tag"

# ── State B: real changes among excluded ones -> patch bump + clean render ──
c nested/compose.yaml "fix: nested compose is included (bare patterns root-anchored)"
echo "y" >>"$R/.github/workflows/ci.yaml"
echo "y" >>"$R/src/main.go"
git -C "$R" add -A
git -C "$R" commit -qm "fix: mixed excluded plus shipped path stays included"
c src/feature.go "feat: real feature"
c src/sec.go "sec: real hardening"
c Dockerfile "chore(deps): update alpine docker tag to v9.99"
assert_eq "$(bump "$R")" "v1.1.0" "B: feat among filtered commits bumps minor"
OUT="$(render "$R")"
echo "$OUT" | grep -q "Nested compose is included" || fail "B: root-anchored bare pattern wrongly matched nested path"
echo "$OUT" | grep -q "Mixed excluded plus shipped" || fail "B: mixed commit was excluded"
echo "$OUT" | grep -q "Update alpine docker tag" || fail "B: runtime dep bump missing from notes"
echo "$OUT" | grep -q "Readme-only edit" && fail "B: **/*.md exclusion not applied"
echo "$OUT" | grep -q "ci digest" && fail "B: .github/ exclusion not applied"
echo "$OUT" | grep -q "lock file maintenance" && fail "B: **/package-lock.json exclusion not applied"
echo "$OUT" | grep -q '<!--' && fail "B: sort-key comment residue in rendered notes"
pos() { echo "$OUT" | grep -n "^### $1" | cut -d: -f1; }
P_ADD="$(pos Added)" P_FIX="$(pos Fixed)" P_SEC="$(pos Security)" P_DEP="$(pos Dependencies)"
[ -n "$P_ADD" ] && [ -n "$P_FIX" ] && [ -n "$P_SEC" ] && [ -n "$P_DEP" ] \
  || fail "B: missing sections (Added=$P_ADD Fixed=$P_FIX Security=$P_SEC Dependencies=$P_DEP)"
{ [ "$P_ADD" -lt "$P_FIX" ] && [ "$P_FIX" -lt "$P_SEC" ] && [ "$P_SEC" -lt "$P_DEP" ]; } \
  || fail "B: section order wrong (Added=$P_ADD Fixed=$P_FIX Security=$P_SEC Dependencies=$P_DEP)"
echo "ok: B render (inclusion, exclusion, mixed commit, ordering, no residue)"

# ── State C: latest tag's window fully filtered -> still anchors on it ──────
git -C "$R" tag v1.1.0
c .github/workflows/ci.yaml "chore(deps): update ci digest to bbbbbbb"
git -C "$R" tag v1.1.1 # phantom-era artifact: tag whose whole window is excluded
assert_eq "$(bump "$R")" "v1.1.1" "C: anchors on latest tag despite fully-filtered window"

# ── State D: real fix above the filtered-window tag -> next patch, no collision
c src/main.go "fix: real fix above filtered-window tag"
assert_eq "$(bump "$R")" "v1.1.2" "D: bumps past filtered-window tag without collision"

# ── State E: behind an existing newer tag (dispatch/replay shape) ───────────
git -C "$R" tag v1.1.2
git -C "$R" checkout -q v1.1.0
assert_eq "$(bump "$R")" "v1.1.2" "E: behind newer tag anchors on newest repo tag (guard contains it)"
git -C "$R" checkout -q main

# ── State F: bootstrap (no tags) -> initial_tag ─────────────────────────────
B="$WORK/boot"
mkdir -p "$B"
git -C "$B" init -q -b main
git -C "$B" config user.email probe@ci.local
git -C "$B" config user.name probe
git -C "$B" config commit.gpgsign false
echo x >"$B/main.go"
git -C "$B" add -A
git -C "$B" commit -qm "feat: initial"
assert_eq "$(bump "$B")" "v1.0.0" "F: bootstrap falls back to [bump].initial_tag"

# ── State G: prefixed component tag must not poison the version base ────────
# tag_pattern is a REGEX; unanchored ("v[0-9].*") it also matches a prefixed
# tag like "yamlenv/v9.9.9" (a nested Go module / component release), and
# --bumped-version then derives the ROOT next version from it (observed:
# "yamlenv/v9.9.10"). The anchored pattern ("^v[0-9]") must keep such tags
# invisible: the root lane bumps from its own latest vX.Y.Z only.
git -C "$R" tag yamlenv/v9.9.9
c src/main.go "fix: real fix with component tag present"
assert_eq "$(bump "$R")" "v1.1.3" "G: prefixed component tag invisible to root version base"

# ── Nested-module release lanes (the release.yaml go-nested contract) ───────
# A lane repo: root module + one nested module dir ("yamlenv"), each with its
# own tag universe. These states pin exactly the invocations release.yaml
# uses — root recompute, lane compute, and both notes renders.
L="$WORK/lanes"
mkdir -p "$L"
git -C "$L" init -q -b main
git -C "$L" config user.email probe@ci.local
git -C "$L" config user.name probe
git -C "$L" config commit.gpgsign false
lc() { # path message (lane repo commit)
  mkdir -p "$L/$(dirname "$1")"
  echo "x$RANDOM" >>"$L/$1"
  git -C "$L" add -A
  git -C "$L" commit -qm "$2"
}
# The two tag patterns compute.sh passes: stable versions only, anchored at
# both ends, so a lane tag and a -dev.N tag are both invisible to the base.
ROOT_PAT='^v[0-9]+\.[0-9]+\.[0-9]+$'
LANE_PAT='^yamlenv/v[0-9]+\.[0-9]+\.[0-9]+$'
lane_root_bump() { (cd "$L" && "$CLIFF" --config "$CFG" --unreleased --bumped-version --tag-pattern "$ROOT_PAT" --exclude-path 'yamlenv/**' 2>/dev/null); }
lane_bump() { (cd "$L" && GIT_CLIFF__BUMP__INITIAL_TAG='yamlenv/v1.0.0' "$CLIFF" --config "$CFG" --unreleased --bumped-version --tag-pattern "$LANE_PAT" --include-path 'yamlenv/**' 2>/dev/null); }

# ── State H: lane commits never bump the root lane ──────────────────────────
lc src/main.go "feat: initial"
git -C "$L" tag v1.0.0
git -C "$L" tag yamlenv/v1.0.0 # co-located: one push released both lanes
lc yamlenv/y.go "feat: lane-only feature"
assert_eq "$(lane_root_bump)" "v1.0.0" "H1: lane-only commits leave root at latest (release=false analog)"
lc src/main.go "fix: root fix"
assert_eq "$(lane_root_bump)" "v1.0.1" "H2: root bump ignores the lane feat (patch, not minor)"
# Stale-config defense: under a deliberately UNANCHORED config (the
# pre-2026-07 pattern), the explicit CLI --tag-pattern must still keep the
# root version string lane-clean.
sed 's/^tag_pattern = .*/tag_pattern = "v[0-9].*"/' "$CFG" >"$WORK/cliff-unanchored.toml"
# A sed that matched nothing would test the anchored config against itself.
grep -q 'tag_pattern = .v\[0-9\]\.\*.' "$WORK/cliff-unanchored.toml" || fail "H3: the unanchored fixture config was not produced (sed matched no tag_pattern line)"
UNANCH="$(cd "$L" && "$CLIFF" --config "$WORK/cliff-unanchored.toml" --unreleased --bumped-version --tag-pattern "$ROOT_PAT" --exclude-path 'yamlenv/**' 2>/dev/null)"
assert_eq "$UNANCH" "v1.0.1" "H3: CLI --tag-pattern overrides an unanchored stale config"

# ── State I: nested lane computes from its own tag universe ──────────────────
assert_eq "$(lane_bump)" "yamlenv/v1.1.0" "I1: lane bumps minor from its own tag; root commits invisible"
git -C "$L" tag yamlenv/v1.1.0
lc src/main.go "feat: root-only feature"
assert_eq "$(lane_bump)" "yamlenv/v1.1.0" "I2: root-only commits leave lane at latest (release=false analog)"
LB="$WORK/lane-boot"
mkdir -p "$LB"
git -C "$LB" init -q -b main
git -C "$LB" config user.email probe@ci.local
git -C "$LB" config user.name probe
git -C "$LB" config commit.gpgsign false
mkdir -p "$LB/src" "$LB/yamlenv"
echo x >"$LB/src/main.go"
git -C "$LB" add -A
git -C "$LB" commit -qm "feat: initial"
git -C "$LB" tag v1.0.0
echo y >"$LB/yamlenv/y.go"
git -C "$LB" add -A
git -C "$LB" commit -qm "feat: introduce nested module"
BOOT="$(cd "$LB" && GIT_CLIFF__BUMP__INITIAL_TAG='yamlenv/v1.0.0' "$CLIFF" --config "$CFG" --unreleased --bumped-version --tag-pattern "$LANE_PAT" --include-path 'yamlenv/**' 2>/dev/null)"
assert_eq "$BOOT" "yamlenv/v1.0.0" "I3: lane bootstrap via GIT_CLIFF__BUMP__INITIAL_TAG (no lane tag yet)"
# H4 (root recompute in a repo with NO tags at all, lane commits only): must
# fall back to the config initial_tag with exit 0, never emit empty/fail —
# the release.yaml recompute step treats empty output as a hard error, so
# this pins that the shape cannot produce it.
NOTAG="$WORK/notag"
mkdir -p "$NOTAG/yamlenv"
git -C "$NOTAG" init -q -b main
git -C "$NOTAG" config user.email probe@ci.local
git -C "$NOTAG" config user.name probe
git -C "$NOTAG" config commit.gpgsign false
echo y >"$NOTAG/yamlenv/y.go"
git -C "$NOTAG" add -A
git -C "$NOTAG" commit -qm "feat: introduce lane in tagless repo"
NOTAG_ROOT="$(cd "$NOTAG" && "$CLIFF" --config "$CFG" --unreleased --bumped-version --tag-pattern "$ROOT_PAT" --exclude-path 'yamlenv/**' 2>/dev/null)"
assert_eq "$NOTAG_ROOT" "v1.0.0" "H4: tagless repo with lanes falls back to initial_tag (never empty)"

# ── State J: notes are cross-lane clean, config excludes still merged ───────
lc yamlenv/y.go "feat: lane feature for notes"
lc src/main.go "fix: root fix for notes"
lc README.md "fix: readme-only edit for notes"
LNOTES="$(cd "$L" && "$CLIFF" --config "$CFG" --unreleased --tag 'yamlenv/v9.9.9' --tag-pattern "$LANE_PAT" --include-path 'yamlenv/**' --strip header 2>/dev/null)"
echo "$LNOTES" | grep -q "Lane feature for notes" || fail "J: lane notes missing the lane commit"
echo "$LNOTES" | grep -q "Root fix for notes" && fail "J: lane notes leaked a root commit"
RNOTES="$(cd "$L" && "$CLIFF" --config "$CFG" --unreleased --tag 'v9.9.9' --tag-pattern "$ROOT_PAT" --exclude-path 'yamlenv/**' --strip header 2>/dev/null)"
echo "$RNOTES" | grep -q "Root fix for notes" || fail "J: root notes missing the root commit"
echo "$RNOTES" | grep -q "Lane feature for notes" && fail "J: root notes leaked a lane commit"
echo "$RNOTES" | grep -q "Readme-only edit" && fail "J: config exclude_paths not merged under CLI path flags"
echo "ok: J notes cross-lane hygiene (lane/root separation + config excludes merged)"

# ── State K: finalize-mode rendering (--current at a tagged HEAD) ────────────
# After a partial lane release (tag created, GitHub Release creation failed),
# the rerun repairs the Release. At that point the lane's commits are no
# longer "unreleased", so the finalize path renders the CURRENT release with
# the same lane scoping; this pins that --current sees the tagged commits.
git -C "$L" tag yamlenv/v9.9.9 # the notes-round commits become the current lane release
KNOTES="$(cd "$L" && "$CLIFF" --config "$CFG" --current --tag-pattern "$LANE_PAT" --include-path 'yamlenv/**' --strip header 2>/dev/null)"
echo "$KNOTES" | grep -q "Lane feature for notes" || fail "K: --current finalize render missing the lane commit"
echo "$KNOTES" | grep -q "Root fix for notes" && fail "K: --current finalize render leaked a root commit"
echo "ok: K finalize render (--current at tagged HEAD, lane-scoped)"

# ── State L: ROOT finalize render (--current with lane excludes) ─────────────
# The root-lane counterpart of K: after a partial ROOT release (root tag
# created at HEAD, GitHub Release missing), the go/ts/docker finalize path
# renders the current ROOT release with the lane-aware flags. Root and lane
# tags are co-located at HEAD here — the realistic both-lanes-released shape.
git -C "$L" tag v9.9.9
LNOTES2="$(cd "$L" && "$CLIFF" --config "$CFG" --current --tag-pattern "$ROOT_PAT" --exclude-path 'yamlenv/**' --strip header 2>/dev/null)"
echo "$LNOTES2" | grep -q "Root fix for notes" || fail "L: root --current finalize render missing the root commit"
echo "$LNOTES2" | grep -q "Lane feature for notes" && fail "L: root --current finalize render leaked a lane commit"
echo "$LNOTES2" | grep -q "Readme-only edit" && fail "L: config exclude_paths not applied in root finalize render"
echo "ok: L root finalize render (--current, lane commits excluded, config excludes merged)"

# ── Channel states: the action's compute.sh, run in fixture repos ────────────
# compute.sh is the one owner of the tag universe, the dev counter and the
# patch floor, so these states execute it rather than restate its arithmetic.
ACTION="$ROOT/actions/git-cliff-version/compute.sh"
[ -f "$ACTION" ] || fail "compute.sh not found at $ACTION"
compute() { # <repo> <channel> [lane] [exclude-paths] -> populates OUT_* variables
  local repo="$1" channel="$2" lane="${3:-}" excludes="${4:-}" outfile line
  outfile="$(mktemp "$WORK/compute-out.XXXXXX")"
  (
    cd "$repo" && GIT_CLIFF_CONFIG="$CFG" CLIFF_BIN="$CLIFF" GITHUB_OUTPUT="$outfile" \
      GITHUB_SHA="$(git rev-parse HEAD)" CHANNEL="$channel" LANE="$lane" EXCLUDE_PATHS="$excludes" \
      bash "$ACTION" >/dev/null 2>&1
  ) || fail "compute.sh failed in $repo (channel=$channel lane=${lane:-<none>})"
  OUT_BASE="" OUT_DEV="" OUT_FLOOR_BASE="" OUT_FLOOR_DEV="" OUT_LATEST="" OUT_ANCHOR="" OUT_RELEASE=""
  while IFS= read -r line; do
    case "$line" in
      base=*) OUT_BASE="${line#base=}" ;;
      dev_version=*) OUT_DEV="${line#dev_version=}" ;;
      floor_base=*) OUT_FLOOR_BASE="${line#floor_base=}" ;;
      floor_dev_version=*) OUT_FLOOR_DEV="${line#floor_dev_version=}" ;;
      latest=*) OUT_LATEST="${line#latest=}" ;;
      anchor_sha=*) OUT_ANCHOR="${line#anchor_sha=}" ;;
      release=*) OUT_RELEASE="${line#release=}" ;;
    esac
  done <"$outfile"
  [ -n "$OUT_BASE" ] || fail "compute.sh wrote no base output in $repo"
}
mkfixture() { # <name> -> path
  local d="$WORK/$1"
  mkdir -p "$d"
  git -C "$d" init -q -b main
  git -C "$d" config user.email probe@ci.local
  git -C "$d" config user.name probe
  git -C "$d" config commit.gpgsign false
  printf '%s\n' "$d"
}
fc() { # <repo> <path> <message>
  mkdir -p "$1/$(dirname "$2")"
  echo "x$RANDOM" >>"$1/$2"
  git -C "$1" add -A
  git -C "$1" commit -qm "$3"
}

# ── State M: a -dev.N tag is invisible to the stable base ───────────────────
M=$(mkfixture chan-m)
fc "$M" src/main.go "feat: initial"
git -C "$M" tag v1.2.0
fc "$M" src/a.go "feat: first dev build"
git -C "$M" tag v1.3.0-dev.1
fc "$M" src/b.go "feat: second change"
compute "$M" stable
assert_eq "$OUT_BASE" "v1.3.0" "M1: stable base ignores the v1.3.0-dev.1 tag"
assert_eq "$OUT_LATEST" "v1.2.0" "M2: latest is the stable tag, not the dev tag"

# ── State N: the dev counter continues from the existing <base>-dev.* tags ──
git -C "$M" tag v1.3.0-dev.2
git -C "$M" tag v1.4.0-dev.9 # decoy on another base
compute "$M" dev
assert_eq "$OUT_DEV" "v1.3.0-dev.3" "N1: dev version is base-dev.<count+1>, decoys on other bases ignored"
assert_eq "$OUT_BASE" "v1.3.0" "N2: dev channel computes the same stable base"

# ── State O: a base equal to latest gets a patch floor ──────────────────────
O=$(mkfixture chan-o)
fc "$O" src/main.go "feat: initial"
git -C "$O" tag v1.3.1
fc "$O" Dockerfile "chore: tidy"
compute "$O" stable
assert_eq "$OUT_BASE" "v1.3.1" "O1: cliff proposes no bump for a chore"
assert_eq "$OUT_RELEASE" "false" "O2: base == latest reads release=false from the action"
assert_eq "$OUT_FLOOR_BASE" "v1.3.2" "O3: floor_base is the patch bump of latest"
assert_eq "$OUT_FLOOR_DEV" "v1.3.2-dev.1" "O4: floor_dev_version counts from zero on the floored base"

# ── State P: the anchor follows the channel's tag universe ──────────────────
compute "$M" dev
assert_eq "$OUT_ANCHOR" "$(git -C "$M" rev-list -n1 v1.3.0-dev.2)" "P1: dev anchor is the newest reachable tag of any kind"
compute "$M" stable
assert_eq "$OUT_ANCHOR" "$(git -C "$M" rev-list -n1 v1.2.0)" "P2: stable anchor is the newest reachable stable tag"

# ── State Q: a lane computes its own dev version ────────────────────────────
Q=$(mkfixture chan-q)
fc "$Q" src/main.go "feat: initial"
mkdir -p "$Q/yamlenv"
echo y >"$Q/yamlenv/y.go"
git -C "$Q" add -A
git -C "$Q" commit -qm "feat: introduce nested module"
git -C "$Q" tag v1.0.0
git -C "$Q" tag yamlenv/v1.0.0
fc "$Q" yamlenv/y.go "feat: lane feature"
compute "$Q" dev yamlenv
assert_eq "$OUT_DEV" "yamlenv/v1.1.0-dev.1" "Q1: lane dev version carries the lane prefix"
assert_eq "$OUT_LATEST" "yamlenv/v1.0.0" "Q2: lane latest is the lane's own stable tag"
compute "$Q" stable "" 'yamlenv/**'
assert_eq "$OUT_BASE" "v1.0.0" "Q3: with the lane dir excluded, the lane feat does not bump the root"
compute "$Q" stable
assert_eq "$OUT_BASE" "v1.1.0" "Q4: without the exclusion the same feat bumps the root (the exclude-paths input is load-bearing)"

# ── State R: an rc tag at HEAD is outside both channels' tag universes ───────
# A glob-shaped tag filter (v[0-9]*.[0-9]*.[0-9]*) admits v1.3.0-rc.1; it then
# becomes the anchor at HEAD, the changed-path range is empty and the stable
# release is suppressed while cliff still proposes v1.3.0.
fc "$M" src/c.go "feat: third change"
git -C "$M" tag v1.3.0-rc.1
compute "$M" stable
assert_eq "$OUT_BASE" "v1.3.0" "R1: stable base ignores the rc tag at HEAD"
assert_eq "$OUT_LATEST" "v1.2.0" "R2: latest is still the exact stable tag, not the rc tag"
assert_eq "$OUT_ANCHOR" "$(git -C "$M" rev-list -n1 v1.2.0)" "R3: stable anchor stays at v1.2.0 with an rc tag at HEAD"
assert_eq "$OUT_RELEASE" "true" "R4: the stable release is not suppressed by the rc tag"
compute "$M" dev
assert_eq "$OUT_ANCHOR" "$(git -C "$M" rev-list -n1 v1.3.0-dev.2)" "R5: dev anchor skips the rc tag at HEAD for the newest dev tag"
git -C "$M" tag v1.3.0-beta.2 "$(git -C "$M" rev-list -n1 v1.3.0-dev.2)"
compute "$M" stable
assert_eq "$OUT_LATEST" "v1.2.0" "R6: a beta tag beside the dev tag is invisible too"

echo "PASS: git-cliff $VERSION semantics match the fleet release-gate contract"
