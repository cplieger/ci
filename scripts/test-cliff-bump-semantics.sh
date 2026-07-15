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

# ── Pin consistency: every CLIFF_VERSION in the repo must agree ─────────────
mapfile -t pins < <(
  grep -rhoE 'CLIFF_VERSION[=:] *"?v[0-9][0-9.]*' \
    "$ROOT/actions/git-cliff-version/action.yml" \
    "$ROOT/.github/workflows/release.yaml" \
    "$ROOT/.github/workflows/docker-release.yaml" \
    "$ROOT/.github/workflows/self-release.yaml" \
    | grep -oE 'v[0-9][0-9.]*' | sort -u
)
[ "${#pins[@]}" -eq 1 ] || fail "CLIFF_VERSION pins disagree across workflows/action: ${pins[*]}"
VERSION="${pins[0]}"
echo "pinned git-cliff: $VERSION (consistent across all call sites)"

# ── Binary: reuse CLIFF_BIN if provided, else download the pinned release ───
if [ -n "${CLIFF_BIN:-}" ]; then
  CLIFF="$CLIFF_BIN"
else
  curl -fsSL --retry 3 \
    "https://github.com/orhun/git-cliff/releases/download/${VERSION}/git-cliff-${VERSION#v}-x86_64-unknown-linux-gnu.tar.gz" \
    | tar xz -C "$WORK" --strip-components=1 "git-cliff-${VERSION#v}/git-cliff"
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

echo "PASS: git-cliff $VERSION semantics match the fleet release-gate contract"
