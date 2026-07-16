#!/usr/bin/env bash
# Regression probe for the git-cliff behaviors the fleet release gate depends on.
#
# The consumer cliff.toml (configs/cliff-stable.toml) uses exclude_paths to keep
# non-shipping commits out of release notes AND out of the version bump, and
# actions/git-cliff-version derives its release boolean from
# `git cliff --unreleased --bumped-version`. Several of the behaviors this
# stack relies on are UNDOCUMENTED upstream and sit in a known-buggy area
# (git-cliff issues #816, #1570), so they are pinned here as executable
# assertions instead of trusted:
#
#   1. exclude_paths glob semantics: bare patterns root-anchored, `**/` at any
#      depth, commits touching both excluded and shipped paths still included.
#   2. Version-base anchoring: `--unreleased --bumped-version` returns the
#      latest tag when the unreleased set is fully excluded (release=false in
#      the action), anchors on the latest tag even when that tag's own window
#      is fully filtered (the bare-mode base-regression defect this replaced),
#      and bumps past such tags without colliding with existing versions.
#   3. Bump levels still honored through the filter (fix -> patch, feat -> minor).
#   4. Behind-newer-tag state: at a checkout behind an existing newer tag,
#      cliff anchors on the newest repo tag (NOT describe's reachable tag);
#      the release job's tag-create guard turns the resulting release=true
#      into a loud failure. Asserted so a behavior change is noticed.
#   5. Bootstrap: no tags at all falls back to [bump].initial_tag.
#   6. Section ordering: the <!-- N --> sort prefixes render Added before
#      Fixed before Security before Dependencies, with no comment residue.
#   7. Tag-pattern anchoring: a prefixed component tag (yamlenv/v9.9.9) is
#      invisible to the root version base — tag_pattern is a regex, and the
#      pre-2026-07 unanchored pattern let such a tag hijack --bumped-version.
#   8. Nested-module release lanes (states H/I/J/K): lane commits must not
#      count toward the root bump — the synced config cannot know per-repo
#      lane dirs, so the release pipeline excludes them via CLI
#      --exclude-path (which MERGES with the config exclude_paths), and
#      passes --tag-pattern '^v[0-9]' explicitly so the root version string
#      stays lane-clean even under a stale UNANCHORED consumer config (the
#      same explicit override now also ships in actions/git-cliff-version,
#      covering orphaned lane tags after a lane dir is deleted); a tagless
#      repo with lanes falls back to initial_tag, never empty (H4); the
#      nested lane computes from its own <dir>/vX.Y.Z tag universe with
#      first-release bootstrap via GIT_CLIFF__BUMP__INITIAL_TAG; notes are
#      cross-lane clean in both directions; and finalize-mode --current
#      rendering at a tagged HEAD stays lane-scoped for the LANE side (K)
#      and the ROOT side (L — root+lane tags co-located, lane commits
#      excluded, config excludes merged; this is the rendering the
#      go/ts/docker finalize repair path uses). The lane discovery and
#      classification SHELL semantics are pinned separately by
#      scripts/test-lane-semantics.sh.
#
# Runs in the ci repo's `scripts` CI job (opt-in by file presence), so a
# Renovate bump of the git-cliff pin re-verifies all of the above against the
# NEW binary before the bump can merge. Local run: CLIFF_BIN=/path/to/git-cliff
# scripts/test-cliff-bump-semantics.sh (skips the download).
set -euo pipefail

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
# site (Renovate maintains both via the custom.git-cliff datasource); a site
# whose version or digest drifts from the others is a review escape.
PIN_FILES=(
  "$ROOT/actions/git-cliff-version/action.yml"
  "$ROOT/.github/workflows/release.yaml"
  "$ROOT/.github/workflows/docker-release.yaml"
  "$ROOT/.github/workflows/self-release.yaml"
)
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
# Pairing invariant: every version pin must have a well-formed sha pin beside
# it. Uniqueness alone cannot see a site whose sha line is malformed or
# missing — a 63-hex-char typo simply falls out of the {64} scan and the
# remaining sites still "agree" (found by this probe's own negative test).
N_VER=$(grep -rhoE 'CLIFF_VERSION[=:] *"?v[0-9][0-9.]*' "${PIN_FILES[@]}" | wc -l)
N_SHA=$(grep -rhoE 'CLIFF_SHA256[=:] *"?[a-f0-9]{64}' "${PIN_FILES[@]}" | wc -l)
[ "$N_VER" -eq "$N_SHA" ] || fail "CLIFF pin pairing broken: $N_VER version pins vs $N_SHA well-formed sha pins (a site's CLIFF_SHA256 is missing or malformed)"
echo "pinned git-cliff: $VERSION / sha256 ${SHA256:0:12}… (consistent + paired across all $N_VER call sites)"

# ── Binary: reuse CLIFF_BIN if provided, else download the pinned release ───
# The download verifies the SAME pinned digest the workflows enforce, so a
# Renovate bump whose digest does not match the actual asset fails here, in
# this repo's own PR gate, before it can reach any consumer.
if [ -n "${CLIFF_BIN:-}" ]; then
  CLIFF="$CLIFF_BIN"
else
  curl -fsSL --retry 3 -o "$WORK/git-cliff.tgz" \
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
lane_root_bump() { (cd "$L" && "$CLIFF" --config "$CFG" --unreleased --bumped-version --tag-pattern '^v[0-9]' --exclude-path 'yamlenv/**' 2>/dev/null); }
lane_bump() { (cd "$L" && GIT_CLIFF__BUMP__INITIAL_TAG='yamlenv/v1.0.0' "$CLIFF" --config "$CFG" --unreleased --bumped-version --tag-pattern '^yamlenv/v[0-9]' --include-path 'yamlenv/**' 2>/dev/null); }

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
sed 's/^tag_pattern = "\^v\[0-9\]"/tag_pattern = "v[0-9].*"/' "$CFG" >"$WORK/cliff-unanchored.toml"
UNANCH="$(cd "$L" && "$CLIFF" --config "$WORK/cliff-unanchored.toml" --unreleased --bumped-version --tag-pattern '^v[0-9]' --exclude-path 'yamlenv/**' 2>/dev/null)"
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
BOOT="$(cd "$LB" && GIT_CLIFF__BUMP__INITIAL_TAG='yamlenv/v1.0.0' "$CLIFF" --config "$CFG" --unreleased --bumped-version --tag-pattern '^yamlenv/v[0-9]' --include-path 'yamlenv/**' 2>/dev/null)"
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
NOTAG_ROOT="$(cd "$NOTAG" && "$CLIFF" --config "$CFG" --unreleased --bumped-version --tag-pattern '^v[0-9]' --exclude-path 'yamlenv/**' 2>/dev/null)"
assert_eq "$NOTAG_ROOT" "v1.0.0" "H4: tagless repo with lanes falls back to initial_tag (never empty)"

# ── State J: notes are cross-lane clean, config excludes still merged ───────
lc yamlenv/y.go "feat: lane feature for notes"
lc src/main.go "fix: root fix for notes"
lc README.md "fix: readme-only edit for notes"
LNOTES="$(cd "$L" && "$CLIFF" --config "$CFG" --unreleased --tag 'yamlenv/v9.9.9' --tag-pattern '^yamlenv/v[0-9]' --include-path 'yamlenv/**' --strip header 2>/dev/null)"
echo "$LNOTES" | grep -q "Lane feature for notes" || fail "J: lane notes missing the lane commit"
echo "$LNOTES" | grep -q "Root fix for notes" && fail "J: lane notes leaked a root commit"
RNOTES="$(cd "$L" && "$CLIFF" --config "$CFG" --unreleased --tag 'v9.9.9' --tag-pattern '^v[0-9]' --exclude-path 'yamlenv/**' --strip header 2>/dev/null)"
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
KNOTES="$(cd "$L" && "$CLIFF" --config "$CFG" --current --tag-pattern '^yamlenv/v[0-9]' --include-path 'yamlenv/**' --strip header 2>/dev/null)"
echo "$KNOTES" | grep -q "Lane feature for notes" || fail "K: --current finalize render missing the lane commit"
echo "$KNOTES" | grep -q "Root fix for notes" && fail "K: --current finalize render leaked a root commit"
echo "ok: K finalize render (--current at tagged HEAD, lane-scoped)"

# ── State L: ROOT finalize render (--current with lane excludes) ─────────────
# The root-lane counterpart of K: after a partial ROOT release (root tag
# created at HEAD, GitHub Release missing), the go/ts/docker finalize path
# renders the current ROOT release with the lane-aware flags. Root and lane
# tags are co-located at HEAD here — the realistic both-lanes-released shape.
git -C "$L" tag v9.9.9
LNOTES2="$(cd "$L" && "$CLIFF" --config "$CFG" --current --tag-pattern '^v[0-9]' --exclude-path 'yamlenv/**' --strip header 2>/dev/null)"
echo "$LNOTES2" | grep -q "Root fix for notes" || fail "L: root --current finalize render missing the root commit"
echo "$LNOTES2" | grep -q "Lane feature for notes" && fail "L: root --current finalize render leaked a lane commit"
echo "$LNOTES2" | grep -q "Readme-only edit" && fail "L: config exclude_paths not applied in root finalize render"
echo "ok: L root finalize render (--current, lane commits excluded, config excludes merged)"

echo "PASS: git-cliff $VERSION semantics match the fleet release-gate contract"
