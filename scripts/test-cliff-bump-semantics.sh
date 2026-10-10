#!/usr/bin/env bash
# Regression probe for the git-cliff behaviours the release gate relies on and
# upstream does not document (issues #816, #1570): exclude_paths globbing, the
# --unreleased --bumped-version base anchoring self-release.yaml runs, the
# stable tag_pattern and the nested-module lane scoping (states A to J), the
# synced notes template (T0 to T4), and the channels and release arithmetic of
# actions/git-cliff-version/compute.sh (M to R, S1 to S14). Runs in the scripts
# CI job, so a git-cliff pin bump re-runs it against the new binary.
# CLIFF_BIN=/path/to/git-cliff skips the download.
set -euo pipefail

# Hermetic git: a global `tag.gpgsign true` turns each bare `git tag` below
# into a signed tag that opens an editor and hangs the probe, so the probe
# reads per-repo config only.
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
# Each install site pins version and tarball sha256 as a pair. Four checks,
# each blind to the others' drift: uniqueness; site count (an added or removed
# site must bump EXPECTED_SITES, which a symmetric pair removal would pass);
# the `sha256sum -c` gate line per site; and the Renovate manager shape, whose
# regex below mirrors cplieger/.github default.json's custom.git-cliff manager
# (a broken annotation or depName freezes that site). Pin files are
# discovered, so a new site in a new file cannot hide.
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
# still "agree" on uniqueness.
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
c docs/images/header.webp "fix: refresh the header screenshot"
c docs/detail.png "fix: crop the settings screenshot"
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
echo "$OUT" | grep -q "Update alpine docker tag" && fail "B: a deps-scoped commit rendered in the notes"
echo "$OUT" | grep -q "Readme-only edit" && fail "B: **/*.md exclusion not applied"
echo "$OUT" | grep -q "ci digest" && fail "B: .github/ exclusion not applied"
echo "$OUT" | grep -q "lock file maintenance" && fail "B: **/package-lock.json exclusion not applied"
echo "$OUT" | grep -q '<!--' && fail "B: sort-key comment residue in rendered notes"
pos() { echo "$OUT" | grep -n "^### $1" | cut -d: -f1; }
P_SEC="$(pos Security)" P_ADD="$(pos Added)" P_FIX="$(pos Fixed)"
[ -n "$P_SEC" ] && [ -n "$P_ADD" ] && [ -n "$P_FIX" ] \
  || fail "B: missing sections (Security=$P_SEC Added=$P_ADD Fixed=$P_FIX)"
{ [ "$P_SEC" -lt "$P_ADD" ] && [ "$P_ADD" -lt "$P_FIX" ]; } \
  || fail "B: section order wrong (Security=$P_SEC Added=$P_ADD Fixed=$P_FIX)"
echo "ok: B render (inclusion, exclusion, mixed commit, ordering, no residue)"

# ── States C to E: the --unreleased anchoring self-release.yaml relies on ────
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

# ── Nested-module release lanes ─────────────────────────────────────────────
# A lane repo: root module + one nested module dir ("yamlenv"), each with its
# own tag universe. These states pin git-cliff's scoping under the tag
# patterns, path filters and lane initial tag compute.sh and render-notes.sh
# pass.
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
# Stale-config defense: under a deliberately UNANCHORED config, the explicit
# CLI --tag-pattern must still keep the root version string lane-clean.
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
# H4 (root lane in a repo with NO tags at all, lane commits only): must fall
# back to the config initial_tag with exit 0, never emit empty or fail.
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

# ── Template states: the synced notes body ──────────────────────────────────
# Both tier configs must render identically; only [bump] may differ.
cmp -s <(sed '1,/^\[changelog\]/d' "$CFG") <(sed '1,/^\[changelog\]/d' "$ROOT/configs/cliff-alpha.toml") \
  || fail "T0: cliff-stable.toml and cliff-alpha.toml differ outside [bump]"
echo "ok: T0 both tier configs share [changelog] and [git]"
N="$WORK/notes"
mkdir -p "$N"
git -C "$N" init -q -b main
git -C "$N" config user.email probe@ci.local
git -C "$N" config user.name probe
git -C "$N" config commit.gpgsign false
nc() { # path message (notes repo commit)
  mkdir -p "$N/$(dirname "$1")"
  echo "x$RANDOM" >>"$N/$1"
  git -C "$N" add -A
  git -C "$N" commit -qm "$2"
}
nc src/main.go "feat: initial"
git -C "$N" tag v1.0.0
nc src/a.go "fix: handle empty input (#2)"
nc src/b.go "perf: faster scan (#3)"
nc src/c.go "sec: escape the header (#4)"
nc Dockerfile "chore(deps): update alpine docker tag to v3.24 (#5)"
nc go.mod "fix(deps): update module example.com/dep to v1.2.0 (#6)"
nc src/d.go "feat!: footerless break (#7)"
nc src/e.go "feat(api)!: drop the v1 route (#8)

BREAKING CHANGE: call /v2 instead"
nc src/f.go "an unconventional subject (#9)"
notes() { (cd "$N" && "$CLIFF" --config "$CFG" --unreleased --tag v2.0.0 --strip header 2>/dev/null); }
want_notes='
### Breaking changes

#### Drop the v1 route (#8)

Call /v2 instead

### Security

- Escape the header (#4)

### Added

- [**breaking**] Footerless break (#7)

### Fixed

- Handle empty input (#2)

### Performance

- Faster scan (#3)

### Changed

- An unconventional subject (#9)'
assert_eq "$(notes)" "$want_notes" "T1: deps-scoped commits stay out, sections run Security to Changed, a footer-bearing breaking commit renders once"
git -C "$N" tag v2.0.0
nc go.mod "fix(deps): update module example.com/dep to v1.2.1 (#10)"
assert_eq "$(cd "$N" && "$CLIFF" --config "$CFG" --unreleased --bumped-version 2>/dev/null)" "v2.0.1" "T3: a fix(deps) commit still bumps a patch"
nc src/g.go "feat!: only a footer-bearing feature (#11)

BREAKING CHANGE: migrate first"
assert_eq "$(notes)" '
### Breaking changes

#### Only a footer-bearing feature (#11)

Migrate first' "T4: a group left holding only a footer-bearing commit renders no heading"

# ── Channel states: the action's compute.sh, run in fixture repos ────────────
# compute.sh is the one owner of the tag universe and the dev counter, so
# these states execute it rather than restate its arithmetic.
ACTION="$ROOT/actions/git-cliff-version/compute.sh"
[ -f "$ACTION" ] || fail "compute.sh not found at $ACTION"
compute_tb() { # <repo> <channel> <pending-version> <pending-in-range> [lane] [exclude-paths]
  local repo="$1" channel="$2" outfile
  outfile="$(mktemp "$WORK/compute-out.XXXXXX")"
  (
    cd "$repo" && GIT_CLIFF_CONFIG="$CFG" CLIFF_BIN="$CLIFF" GITHUB_OUTPUT="$outfile" \
      CHANNEL="$channel" PENDING_VERSION="$3" PENDING_IN_RANGE="$4" LANE="${5:-}" \
      EXCLUDE_PATHS="${6:-}" bash "$ACTION" >"$WORK/compute.log" 2>&1
  ) || fail "compute.sh failed in $repo (channel=$channel pending=${3:-<none>} in-range=$4 lane=${5:-<none>}): $(cat "$WORK/compute.log")"
  read_outputs "$outfile"
  [ -n "$OUT_BASE" ] || fail "compute.sh wrote no base output in $repo"
}
compute() { # <repo> <channel> [lane] [exclude-paths]: nothing pending
  compute_tb "$1" "$2" "" false "${3:-}" "${4:-}"
}
read_outputs() { # <GITHUB_OUTPUT file> -> OUT_* variables
  local line
  OUT_BASE="" OUT_DEV="" OUT_LATEST="" OUT_ANCHOR="" OUT_RELEASE=""
  OUT_H_TAG="" OUT_FROM="" OUT_LEVEL=""
  while IFS= read -r line; do
    case "$line" in
      base=*) OUT_BASE="${line#base=}" ;;
      dev_version=*) OUT_DEV="${line#dev_version=}" ;;
      latest=*) OUT_LATEST="${line#latest=}" ;;
      anchor_sha=*) OUT_ANCHOR="${line#anchor_sha=}" ;;
      release=*) OUT_RELEASE="${line#release=}" ;;
      h_tag=*) OUT_H_TAG="${line#h_tag=}" ;;
      range_from=*) OUT_FROM="${line#range_from=}" ;;
      change_level=*) OUT_LEVEL="${line#change_level=}" ;;
    esac
  done <"$1"
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
# refs/remotes/origin/main stands in for the remote-tracking ref a fetch-depth 0
# checkout carries, which a dev run needs.
publish_main() { git -C "$1" update-ref refs/remotes/origin/main refs/heads/main; }

# ── State M: a -dev.N tag is invisible to the stable base ───────────────────
M=$(mkfixture chan-m)
fc "$M" src/main.go "feat: initial"
git -C "$M" tag v1.2.0
fc "$M" src/a.go "feat: first dev build"
git -C "$M" tag v1.3.0-dev.1
fc "$M" src/b.go "feat: second change"
publish_main "$M"
compute "$M" stable
assert_eq "$OUT_BASE" "v1.2.1" "M1: the stable base is the patch above v1.2.0, the v1.3.0-dev.1 tag ignored"
assert_eq "$OUT_LATEST" "v1.2.0" "M2: latest is the stable tag, not the dev tag"

# ── State N: the dev counter continues from the existing <base>-dev.* tags ──
git -C "$M" tag v1.3.0-dev.2
git -C "$M" tag v1.4.0-dev.9 # decoy on another base
compute "$M" dev
assert_eq "$OUT_DEV" "v1.3.0-dev.3" "N1: dev version is base-dev.<count+1>, decoys on other bases ignored"
assert_eq "$OUT_BASE" "v1.3.0" "N2: the dev base is the next minor above the stable tag"

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
publish_main "$Q"
compute "$Q" dev yamlenv
assert_eq "$OUT_DEV" "yamlenv/v1.1.0-dev.1" "Q1: lane dev version carries the lane prefix"
assert_eq "$OUT_LATEST" "yamlenv/v1.0.0" "Q2: lane latest is the lane's own stable tag"
compute "$Q" stable "" 'yamlenv/**'
assert_eq "$OUT_LEVEL" "none" "Q3: with the lane dir excluded, the lane feat is no root change"
compute "$Q" stable
assert_eq "$OUT_LEVEL" "minor" "Q4: without the exclusion the same feat is a root minor (the exclude-paths input is load-bearing)"

# ── State R: an rc tag at HEAD is outside both channels' tag universes ───────
# A glob-shaped tag filter (v[0-9]*.[0-9]*.[0-9]*) admits v1.3.0-rc.1; it then
# becomes the anchor at HEAD, the changed-path range is empty and the stable
# release is suppressed while cliff still proposes v1.3.0.
fc "$M" src/c.go "feat: third change"
git -C "$M" tag v1.3.0-rc.1
publish_main "$M"
compute "$M" stable
assert_eq "$OUT_BASE" "v1.2.1" "R1: stable base ignores the rc tag at HEAD"
assert_eq "$OUT_LATEST" "v1.2.0" "R2: latest is still the exact stable tag, not the rc tag"
assert_eq "$OUT_ANCHOR" "$(git -C "$M" rev-list -n1 v1.2.0)" "R3: stable anchor stays at v1.2.0 with an rc tag at HEAD"
assert_eq "$OUT_RELEASE" "true" "R4: the stable release is not suppressed by the rc tag"
compute "$M" dev
assert_eq "$OUT_ANCHOR" "$(git -C "$M" rev-list -n1 v1.3.0-dev.2)" "R5: dev anchor skips the rc tag at HEAD for the newest dev tag"
git -C "$M" tag v1.3.0-beta.2 "$(git -C "$M" rev-list -n1 v1.3.0-dev.2)"
compute "$M" stable
assert_eq "$OUT_LATEST" "v1.2.0" "R6: a beta tag beside the dev tag is invisible too"

# ── Release states: main and dev branches, promotions, lanes ────────────────
pending_of() { # <repo> <commit> [lane] [exclude-paths] -> the pending promotion's version
  (
    cd "$1" && GIT_CLIFF_CONFIG="$CFG" CLIFF_BIN="$CLIFF" EXCLUDE_PATHS="${4:-}" \
      bash "$ACTION" pending-version "$2" ${3:+"$3"} 2>"$WORK/pending.log"
  ) || fail "compute.sh pending-version $2 failed in $1: $(cat "$WORK/pending.log")"
}
refused() { # <label> <expected message fragment> <repo> <compute.sh args and env...>
  local label="$1" want="$2" repo="$3" rc=0
  shift 3
  (cd "$repo" && env GIT_CLIFF_CONFIG="$CFG" CLIFF_BIN="$CLIFF" GITHUB_OUTPUT="$WORK/refused.out" \
    "$@" >"$WORK/refused.log" 2>&1) || rc=$?
  [ "$rc" -ne 0 ] || fail "$label: compute.sh accepted it"
  grep -qF -- "$want" "$WORK/refused.log" || fail "$label: refusal does not say '$want': $(cat "$WORK/refused.log")"
  echo "ok: $label -> refused (rc $rc)"
}
tb_fixture() { # <name> -> path; main holds "feat: initial" at v1.2.0, dev branches there
  local d
  d=$(mkfixture "$1")
  fc "$d" src/main.go "feat: initial"
  git -C "$d" tag v1.2.0
  git -C "$d" branch dev
  printf '%s\n' "$d"
}
promote() { # <repo> <dev commit T> -> R: tree T, parents [main, T], main fast-forwarded to it
  local r
  r=$(git -C "$1" commit-tree "$2^{tree}" -p "$(git -C "$1" rev-parse main)" -p "$2" -m "release: promote dev into main")
  git -C "$1" checkout -q main
  git -C "$1" merge -q --ff-only "$r"
  publish_main "$1"
  printf '%s\n' "$r"
}
commit_of() { git -C "$1" rev-list -n1 "$2"; }

# The action forwards every input compute.sh reads; an input it drops is inert.
for pair in channel:CHANNEL exclude-paths:EXCLUDE_PATHS lane:LANE \
  pending-version:PENDING_VERSION pending-in-range:PENDING_IN_RANGE; do
  grep -qE "^  ${pair%%:*}:\$" "$ROOT/actions/git-cliff-version/action.yml" \
    || fail "action.yml declares no '${pair%%:*}' input"
  grep -qF "${pair#*:}: \${{ inputs.${pair%%:*} }}" "$ROOT/actions/git-cliff-version/action.yml" \
    || fail "action.yml does not forward input '${pair%%:*}' as ${pair#*:}"
done
echo "ok: action.yml forwards every compute.sh input"

# ── State S2: a dev build numbers above a main patch it cannot reach ─────────
F=$(tb_fixture tb-floor)
git -C "$F" checkout -q dev
fc "$F" src/a.go "fix: a dev-only fix"
git -C "$F" checkout -q main
fc "$F" go.mod "fix(deps): update a module"
git -C "$F" tag v1.2.1
fc "$F" src/m.go "feat: a feature that reached main"
publish_main "$F"
git -C "$F" checkout -q dev
! git -C "$F" merge-base --is-ancestor v1.2.1 dev || fail "S2 precondition: v1.2.1 must be unreachable from dev"
compute_tb "$F" dev "" false
assert_eq "$OUT_BASE" "v1.3.0" "S2a: a fixes-only dev build takes the next minor above the unreachable H"
assert_eq "$OUT_DEV" "v1.3.0-dev.1" "S2b: the dev counter runs on that base"
assert_eq "$OUT_H_TAG" "v1.2.1" "S2c: H is the highest stable tag over all refs"
assert_eq "$OUT_LATEST" "v1.2.1" "S2d: latest is H's tag"
assert_eq "$OUT_FROM" "$(commit_of "$F" v1.2.1)" "S2e: with no promotion yet the range starts at H's commit"
assert_eq "$OUT_LEVEL" "patch" "S2f: the range holds only the dev fix"

# ── State S3: the patch cap on a main run with nothing pending ───────────────
git -C "$F" checkout -q main
compute_tb "$F" stable "" false
assert_eq "$OUT_BASE" "v1.2.2" "S3a: a feat on main with nothing pending publishes the next patch only"
assert_eq "$OUT_LEVEL" "minor" "S3b: the cap holds against the minor git-cliff read"
assert_eq "$OUT_RELEASE" "true" "S3c: base differs from latest"
git -C "$F" tag v1.2.2
compute_tb "$F" stable "" false
assert_eq "$OUT_BASE" "v1.2.2" "S3d: a stable run at a tagged commit repairs that version"
assert_eq "$OUT_RELEASE" "false" "S3e: the repair run reads release=false"

# ── States S6, S4, S7, S1: a breaking promotion through its lifecycle ────────
P=$(tb_fixture tb-promo)
git -C "$P" checkout -q dev
fc "$P" src/api.go "feat!: drop the v1 flags"
fc "$P" src/b.go "fix: a dev fix"
T=$(git -C "$P" rev-parse dev)
git -C "$P" checkout -q main
fc "$P" go.mod "fix(deps): update b module"
git -C "$P" tag v1.2.1
PR=$(promote "$P" "$T")
compute_tb "$P" stable "" true
assert_eq "$OUT_BASE" "v2.0.0" "S6a: a promoted feat! at R reserves the next major"
assert_eq "$OUT_FROM" "$(commit_of "$P" v1.2.1)" "S6b: the promotion's range starts at the highest existing stable tag"
assert_eq "$OUT_LEVEL" "major" "S6c: git-cliff read the breaking change"
compute_tb "$P" stable "" false
assert_eq "$OUT_BASE" "v1.2.2" "S6d: the same commit with nothing pending is a patch (pending-in-range is load-bearing)"
assert_eq "$(pending_of "$P" "$PR")" "v2.0.0" "S4a: an untagged R's version is the promotion arithmetic over P..R"

git -C "$P" checkout -q dev
fc "$P" src/c.go "fix: a dev fix after the promotion"
compute_tb "$P" dev "v2.0.0" false
assert_eq "$OUT_BASE" "v2.1.0" "S4b: a dev build numbers above the untagged pending promotion"
assert_eq "$OUT_DEV" "v2.1.0-dev.1" "S4c: its dev version"
assert_eq "$OUT_FROM" "$PR" "S4d: the range starts at the newest reconciliation, newer than H's commit"
assert_eq "$OUT_LEVEL" "patch" "S4e: the promoted feat! is not recounted"
compute_tb "$P" dev "" false
assert_eq "$OUT_BASE" "v1.3.0" "S4f: without the pending version the build would rank below the promotion"

git -C "$P" checkout -q main
fc "$P" Dockerfile "fix(deps): update alpine docker tag"
publish_main "$P"
compute_tb "$P" stable "" true
assert_eq "$OUT_BASE" "v2.0.0" "S7a: a later S over an untagged R publishes the promoted major"
assert_eq "$OUT_FROM" "$(commit_of "$P" v1.2.1)" "S7b: S's range still starts at the highest existing stable tag"

git -C "$P" tag v2.0.0 "$PR"
git -C "$P" checkout -q dev
compute_tb "$P" dev "" false
assert_eq "$OUT_BASE" "v2.1.0" "S1a: once R is tagged, the next dev build does not recount its feat!"
assert_eq "$OUT_FROM" "$PR" "S1b: the range starts at R"
git -C "$P" tag v2.0.1 main
compute_tb "$P" dev "" false
assert_eq "$OUT_FROM" "$(git -C "$P" rev-parse main)" "S1c: a main patch tagged after R moves the range start to it"
assert_eq "$OUT_BASE" "v2.1.0" "S1d: the next minor above v2.0.1"

# ── State S5: R tagged before its receipt keeps its tag's version ────────────
V=$(tb_fixture tb-receipt)
git -C "$V" checkout -q dev
fc "$V" src/a.go "fix: a dev fix"
VT=$(git -C "$V" rev-parse dev)
git -C "$V" checkout -q main
fc "$V" go.mod "fix(deps): update a module"
git -C "$V" tag v1.2.1
VR=$(promote "$V" "$VT")
# The tag is authoritative where today's arithmetic would say v1.3.0: the
# version was published, so it is never recomputed.
git -C "$V" tag v2.0.0 "$VR"
fc "$V" go.mod "fix(deps): update a module again"
git -C "$V" tag v2.0.1
publish_main "$V"
assert_eq "$(pending_of "$V" "$VR")" "v2.0.0" "S5a: the lowest stable tag on R or a descendant is the pending version"
git -C "$V" checkout -q dev
fc "$V" src/b.go "fix: a dev fix after the promotion"
compute_tb "$V" dev "v2.0.0" false
assert_eq "$OUT_BASE" "v2.1.0" "S5b: the dev build numbers above it"

# ── State S8: a fixes-only promotion publishes the next minor ───────────────
X8=$(tb_fixture tb-fixes)
git -C "$X8" checkout -q dev
fc "$X8" src/a.go "fix: only a fix"
X8T=$(git -C "$X8" rev-parse dev)
X8R=$(promote "$X8" "$X8T")
compute_tb "$X8" stable "" true
assert_eq "$OUT_BASE" "v1.3.0" "S8a: a fixes-only promotion publishes the next minor"
assert_eq "$OUT_LEVEL" "patch" "S8b: although git-cliff read a patch"
assert_eq "$(pending_of "$X8" "$X8R")" "v1.3.0" "S8c: the pending helper agrees with the stable run at R"

# ── State S9: each lane computes from its own H ─────────────────────────────
L9=$(mkfixture tb-lanes)
fc "$L9" src/main.go "feat: initial"
fc "$L9" yamlenv/y.go "feat: introduce nested module"
git -C "$L9" tag v1.0.0
git -C "$L9" tag yamlenv/v1.0.0
git -C "$L9" branch dev
git -C "$L9" checkout -q dev
fc "$L9" src/root.go "feat: a root-only feature"
L9T=$(git -C "$L9" rev-parse dev)
git -C "$L9" checkout -q main
fc "$L9" yamlenv/go.mod "fix(deps): update a lane dependency"
git -C "$L9" tag yamlenv/v1.0.1
promote "$L9" "$L9T" >/dev/null
compute_tb "$L9" stable "" true "" 'yamlenv/**'
assert_eq "$OUT_BASE" "v1.1.0" "S9a: the root lane publishes the promotion's minor"
compute_tb "$L9" stable "" false yamlenv
assert_eq "$OUT_BASE" "yamlenv/v1.0.2" "S9b: the lane a root-only promotion left alone publishes its next patch"
assert_eq "$OUT_H_TAG" "yamlenv/v1.0.1" "S9c: the lane's H is its own tag"
git -C "$L9" checkout -q dev
fc "$L9" yamlenv/y.go "fix: a lane fix on dev"
compute_tb "$L9" dev "" false yamlenv
assert_eq "$OUT_BASE" "yamlenv/v1.1.0" "S9d: a lane dev build takes the next minor above the lane's H"
compute_tb "$L9" dev "v1.1.0" false "" 'yamlenv/**'
assert_eq "$OUT_BASE" "v1.2.0" "S9e: the root dev build numbers above the root's pending promotion"
assert_eq "$OUT_LEVEL" "none" "S9f: the lane fix is outside the root range"

# ── State S11: refusals ─────────────────────────────────────────────────────
refused "S11a: malformed pending version" "is not of the form" "$P" CHANNEL=dev PENDING_VERSION=2.1.0 bash "$ACTION"
refused "S11b: pending-in-range on a dev run" "describes a stable run" "$P" CHANNEL=dev PENDING_IN_RANGE=true bash "$ACTION"
refused "S11c: pending-in-range not a boolean" "must be 'true' or 'false'" "$P" CHANNEL=stable PENDING_IN_RANGE=yes bash "$ACTION"
git -C "$P" update-ref -d refs/remotes/origin/main
refused "S11d: a dev run with no remote main" "needs refs/remotes/origin/main" "$P" CHANNEL=dev bash "$ACTION"
refused "S11e: pending-version of a commit that is not a reconciliation" "is not a reconciliation commit" "$P" bash "$ACTION" pending-version "$T"
refused "S11f: unknown command" "unknown compute.sh command" "$P" bash "$ACTION" pending-versions "$PR"
git -C "$X8" tag v1.1.9 "$X8T"
refused "S11g: a stable tag inside the range" "sit inside" "$X8" CHANNEL=stable PENDING_IN_RANGE=true bash "$ACTION"

# ── State S12: a stable run older than H is refused (a re-run, say) ─────────
Z=$(tb_fixture tb-stale)
fc "$Z" go.mod "fix(deps): update a module"
git -C "$Z" tag v1.2.1
fc "$Z" src/a.go "fix: c1"
ZC1=$(git -C "$Z" rev-parse HEAD)
fc "$Z" src/b.go "fix: c2"
git -C "$Z" tag v1.2.2
git -C "$Z" checkout -q --detach "$ZC1"
refused "S12a: a stable run at a commit H's commit is not an ancestor of" "which HEAD does not contain" \
  "$Z" CHANNEL=stable bash "$ACTION"
refused "S12b: the same with a promotion in range" "which HEAD does not contain" \
  "$Z" CHANNEL=stable PENDING_IN_RANGE=true bash "$ACTION"
git -C "$Z" checkout -q main
compute_tb "$Z" stable "" false
assert_eq "$OUT_BASE" "v1.2.2" "S12c: at H's own commit the run repairs H"

# ── State S13: a tagless pending promotion is read up to R, never past it ────
N13=$(mkfixture tb-tagless)
fc "$N13" src/main.go "fix: initial"
git -C "$N13" branch dev
git -C "$N13" checkout -q dev
fc "$N13" src/a.go "fix: a dev fix"
N13R=$(promote "$N13" "$(git -C "$N13" rev-parse dev)")
fc "$N13" src/b.go "feat!: a break after the promotion"
cat >"$WORK/cliff-argv" <<SH
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$WORK/cliff-argv.log"
exec "$CLIFF" "\$@"
SH
chmod 755 "$WORK/cliff-argv"
: >"$WORK/cliff-argv.log"
N13_AHEAD=$(cd "$N13" && GIT_CLIFF_CONFIG="$CFG" CLIFF_BIN="$WORK/cliff-argv" bash "$ACTION" pending-version "$N13R" 2>"$WORK/pending.log") \
  || fail "S13: pending-version failed: $(cat "$WORK/pending.log")"
assert_eq "$(grep -c -- "--bump --context .* ${N13R}\$" "$WORK/cliff-argv.log")" "1" \
  "S13a: with no stable tag git-cliff is still bounded by R"
git -C "$N13" checkout -q --detach "$N13R"
assert_eq "$N13_AHEAD" "$(pending_of "$N13" "$N13R")" "S13b: the version does not depend on how far the checkout is past R"
compute_tb "$N13" stable "" true
assert_eq "$OUT_BASE|$OUT_H_TAG|$OUT_RELEASE" "v1.0.0||true" "S13c: the tagless stable run at R publishes the initial tag"

# ── State S14: below 1.0.0 on cliff-alpha a breaking range reserves a minor ──
CFG_STABLE="$CFG"
CFG="$ROOT/configs/cliff-alpha.toml"
A14=$(mkfixture tb-alpha)
fc "$A14" src/main.go "feat: initial"
git -C "$A14" tag v0.3.0
git -C "$A14" branch dev
git -C "$A14" checkout -q dev
fc "$A14" src/api.go "feat!: drop the old flags"
A14T=$(git -C "$A14" rev-parse dev)
git -C "$A14" checkout -q main
fc "$A14" go.mod "fix(deps): update a module"
git -C "$A14" tag v0.3.1
publish_main "$A14"
git -C "$A14" checkout -q dev
compute_tb "$A14" dev "" false
assert_eq "$OUT_BASE|$OUT_LEVEL" "v0.4.0|minor" "S14a: a breaking 0.x dev build takes the next minor, not 1.0.0"
A14R=$(promote "$A14" "$A14T")
compute_tb "$A14" stable "" true
assert_eq "$OUT_BASE|$OUT_LEVEL" "v0.4.0|minor" "S14b: a breaking 0.x promotion publishes the next minor"
assert_eq "$(pending_of "$A14" "$A14R")" "v0.4.0" "S14c: the pending helper agrees"
CFG="$CFG_STABLE"
compute_tb "$A14" stable "" true
assert_eq "$OUT_BASE" "v1.0.0" "S14d: the same promotion on cliff-stable reserves the major (the config is load-bearing)"

echo "PASS: git-cliff $VERSION semantics match the release-gate contract"
