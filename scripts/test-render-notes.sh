#!/usr/bin/env bash
# Probe for scripts/render-notes.sh against the pinned git-cliff and the synced
# cliff config: states N1 to N14 pin the explicit-range notes with their
# dependency and system-package parts.
# CLIFF_BIN=/path/to/git-cliff skips the download.
# shellcheck disable=SC2016 # the expected notes carry literal markdown backticks
set -euo pipefail
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=probe GIT_AUTHOR_EMAIL=probe@ci.local
export GIT_COMMITTER_NAME=probe GIT_COMMITTER_EMAIL=probe@ci.local
unset GITHUB_TOKEN GH_TOKEN

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/render-notes.sh"
CFG="$ROOT/configs/cliff-stable.toml"
WORK="$(mktemp -d /tmp/render-notes-probe.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
CHECKS=0

fail() {
  echo "FAIL: $*" >&2
  exit 1
}
ok() {
  CHECKS=$((CHECKS + 1))
  echo "ok: $1"
}

if [ -n "${CLIFF_BIN:-}" ]; then
  CLIFF="$CLIFF_BIN"
else
  PIN="$ROOT/.github/workflows/release.yaml"
  VERSION=$(grep -m1 -oE 'CLIFF_VERSION=v[0-9.]+' "$PIN" | cut -d= -f2)
  SHA256=$(grep -m1 -oE 'CLIFF_SHA256=[a-f0-9]{64}' "$PIN" | cut -d= -f2)
  [ -n "$VERSION" ] && [ -n "$SHA256" ] || fail "no git-cliff pin found in $PIN"
  curl -fsSL --retry 7 --retry-max-time 150 --retry-all-errors -o "$WORK/git-cliff.tgz" \
    "https://github.com/orhun/git-cliff/releases/download/${VERSION}/git-cliff-${VERSION#v}-x86_64-unknown-linux-gnu.tar.gz"
  echo "${SHA256}  ${WORK}/git-cliff.tgz" | sha256sum -c -
  tar xzf "$WORK/git-cliff.tgz" -C "$WORK" --strip-components=1 "git-cliff-${VERSION#v}/git-cliff"
  CLIFF="$WORK/git-cliff"
fi
"$CLIFF" --version >/dev/null || fail "git-cliff binary unusable"

new_repo() { # <dir>: a repo carrying the synced stable cliff config
  git init -q -b main "$1"
  cp "$CFG" "$1/cliff.toml"
  printf 'cliff.toml\n' >"$1/.gitignore"
}
c() { # <repo> <path> <message>
  mkdir -p "$1/$(dirname "$2")"
  echo "x$RANDOM" >>"$1/$2"
  git -C "$1" add -A
  git -C "$1" commit -qm "$3"
}
rn() { # <repo> <render-notes args...>: renders to $WORK/out
  local repo=$1
  shift
  (cd "$repo" && CLIFF_BIN="$CLIFF" bash "$SCRIPT" "$@" --out "$WORK/out") >"$WORK/rn.log" 2>&1 \
    || fail "render-notes $* failed: $(cat "$WORK/rn.log")"
}
expect() { # <label>: $WORK/out must equal stdin byte for byte
  cat >"$WORK/want"
  cmp -s "$WORK/want" "$WORK/out" || {
    diff -u "$WORK/want" "$WORK/out" >&2 || true
    fail "$1"
  }
  ok "$1"
}
refused() { # <label> <message fragment> <repo> <render-notes args...>
  local label=$1 want=$2 repo=$3 rc=0
  shift 3
  (cd "$repo" && CLIFF_BIN="$CLIFF" bash "$SCRIPT" "$@") >"$WORK/refused.log" 2>&1 || rc=$?
  [ "$rc" -ne 0 ] || fail "$label: render-notes accepted it"
  grep -qF -- "$want" "$WORK/refused.log" || fail "$label: refusal does not say '$want': $(cat "$WORK/refused.log")"
  ok "$label (rc $rc)"
}

# ── Two-branch: a promotion R over main's patch, lanes, SBOMs ───────────────
T="$WORK/two-branch"
new_repo "$T"
gomod() { # <repo> <dep version>
  printf 'module example.com/app\n\ngo 1.26\n\nrequire example.com/dep %s\n' "$2" >"$1/go.mod"
}
gomod "$T" v1.0.0
mkdir -p "$T/yamlenv"
printf 'module example.com/app/yamlenv\n\ngo 1.26\n' >"$T/yamlenv/go.mod"
c "$T" yamlenv/y.go "feat: initial"
git -C "$T" tag v1.0.0
git -C "$T" tag yamlenv/v1.0.0
git -C "$T" branch dev
gomod "$T" v1.0.1
git -C "$T" add -A
git -C "$T" commit -qm "fix(deps): update module example.com/dep to v1.0.1 (#10)"
git -C "$T" tag v1.0.1
git -C "$T" checkout -q dev
gomod "$T" v1.0.1
git -C "$T" add -A
git -C "$T" commit -qm "fix(deps): update module example.com/dep to v1.0.1 (#11)"
c "$T" src/h.go "sec: escape the header (#12)"
c "$T" src/x.go "feat: add export (#13)"
git -C "$T" tag v1.1.0-dev.1
c "$T" src/s.go "perf: faster scan (#14)"
c "$T" src/p.go "refactor: split parser (#15)"
c "$T" src/e.go "fix: handle empty input (#16)"
c "$T" src/v.go "chore(devdeps): update vitest (#17)"
gomod "$T" v1.2.0
git -C "$T" add -A
git -C "$T" commit -qm "chore(deps): update module example.com/dep to v1.2.0 (#18)"
DEP_SHA=$(git -C "$T" rev-parse HEAD)
c "$T" src/c.go "feat!: drop v1 config (#19)

BREAKING CHANGE: rename A to B"
c "$T" src/f.go "fix!: footerless break (#20)"
c "$T" yamlenv/o.go "feat: lane option (#21)"
git -C "$T" checkout -q main
R=$(git -C "$T" commit-tree "dev^{tree}" -p main -p dev -m "release: promote dev into main")
git -C "$T" merge -q --ff-only "$R"
STRAY=$(git -C "$T" commit-tree "$R^{tree}" -p "$R" -m "fix: a commit no release reaches")
git -C "$T" tag v3.0.0 "$STRAY"

LANES='["yamlenv"]'
TB=(--repo owner/app --release-commit "$R")
PROMOTED='Promoted from `v2.0.0-dev.3`'
ROOT_V3='Promoted from `v2.0.0-dev.3`

### Breaking changes

#### Drop v1 config (#19)

Rename A to B

### Security

- Escape the header (#12)

### Added

- Add export (#13)

### Fixed

- Handle empty input (#16)
- [**breaking**] Footerless break (#20)

### Performance

- Faster scan (#14)

### Changed

- Split parser (#15)

**Full changelog**: https://github.com/owner/app/compare/v1.0.1...v2.0.0

<details>
<summary>Dependencies: 1 change (example.com/dep)</summary>

- `example.com/dep` v1.0.1 to v1.2.0 (Go)

</details>
'
rn "$T" "${TB[@]}" --site go --version v2.0.0 --go-lanes "$LANES" --kind-note "$PROMOTED"
expect "N1 promotion at a merge HEAD: kind line first, sections Security to Changed, Renovate lines hidden, breaking once, compare link, Dependencies" < <(printf '%s' "$ROOT_V3")
rn "$T" "${TB[@]}" --site ts --version v2.0.0 --go-lanes "$LANES" --kind-note "$PROMOTED"
expect "N2 the ts site renders the same explicit range" < <(printf '%s' "$ROOT_V3")
git -C "$T" tag v2.0.0 "$R"
git -C "$T" tag yamlenv/v1.1.0 "$R"
rn "$T" "${TB[@]}" --site go --version v2.0.0 --go-lanes "$LANES" --kind-note "$PROMOTED"
expect "N3 a tag at the release commit and a dev tag inside the range split nothing" < <(printf '%s' "$ROOT_V3")
rn "$T" "${TB[@]}" --site lane --lane yamlenv --version yamlenv/v1.1.0
expect "N4 lane: its own commits, its own previous tag, beside a co-located root tag" <<'EOF'
### Added

- Lane option (#21)

**Full changelog**: https://github.com/owner/app/compare/yamlenv/v1.0.0...yamlenv/v1.1.0
EOF
printf '%s\n' "${DEP_SHA:0:12}" >"$WORK/security-shas"
rn "$T" "${TB[@]}" --site go --version v2.0.0 --go-lanes "$LANES" --security-shas "$WORK/security-shas"
grep -qxF -- '- `example.com/dep` v1.0.1 to v1.2.0 (Go, security update)' "$WORK/out" \
  || fail "N5 an update merged from a security PR is not marked: $(cat "$WORK/out")"
grep -qiE 'GHSA-|CVE-' "$WORK/out" && fail "N5 an advisory ID was rendered"
ok "N5 an update from a security PR is marked, with no advisory ID"

# A later main commit over the untagged promotion still renders all of it.
git -C "$T" tag -d v2.0.0 yamlenv/v1.1.0 >/dev/null
c "$T" go.sum "fix(deps): update module example.com/other to v1.0.1 (#23)"
S=$(git -C "$T" rev-parse HEAD)
rn "$T" --repo owner/app --release-commit "$S" --site go \
  --version v2.0.0 --go-lanes "$LANES" --kind-note "$PROMOTED"
expect "N7 a later S over an untagged R: the same notes, its own Renovate line hidden" < <(printf '%s' "$ROOT_V3")

# Hidden lines still count for the version: the template hides them, the parser keeps them.
git -C "$T" tag -d v3.0.0 >/dev/null
git -C "$T" tag v2.0.0 "$R"
bumped=$(cd "$T" && "$CLIFF" --tag-pattern '^v[0-9]+\.[0-9]+\.[0-9]+$' --unreleased --bumped-version 2>/dev/null)
[ "$bumped" = v2.0.1 ] || fail "N8 a fix(deps)-only range must still bump a patch, got '$bumped'"
rn "$T" --repo owner/app --release-commit "$S" --site go --version v2.0.1 --go-lanes "$LANES"
expect "N8 a Renovate-only range bumps a patch and renders only its compare link" <<'EOF'
**Full changelog**: https://github.com/owner/app/compare/v2.0.0...v2.0.1
EOF

# System packages: the diff of two SPDX documents, the image itself excluded.
sbom() { # <file> <image digest> <name@version...>
  local f=$1 digest=$2
  shift 2
  jq -n --arg d "$digest" --args '{
    spdxVersion: "SPDX-2.3",
    documentDescribes: ["SPDXRef-DocumentRoot-Image"],
    packages: ([{SPDXID: "SPDXRef-DocumentRoot-Image", name: "ghcr.io/owner/app", versionInfo: $d}]
      + [$ARGS.positional | to_entries[] | (.value | split("@")) as [$n, $v]
         | {SPDXID: "SPDXRef-Package-\(.key)", name: $n, versionInfo: $v,
            externalRefs: [{referenceType: "purl", referenceLocator: "pkg:apk/alpine/\($n)@\($v)?arch=x86_64"}]}])
  }' "$@" >"$f"
}
sbom "$WORK/prev.spdx.json" sha256:aaaa busybox@1.37.0-r29 musl@1.2.5-r10 zlib@1.3.1-r2 ssl_client@1.37.0-r29
sbom "$WORK/new.spdx.json" sha256:bbbb busybox@1.37.0-r30 musl@1.2.5-r10 libssl3@3.5.4-r0 ssl_client@1.37.0-r30
git -C "$T" commit -q --allow-empty -m "fix(deps): rebuild for base packages (#24)"
RB=$(git -C "$T" rev-parse HEAD)
rn "$T" --repo owner/app --release-commit "$RB" --site docker --version v2.0.1 \
  --kind-note 'Built from `main` for dependency and system-package updates' \
  --sbom-prev "$WORK/prev.spdx.json" --sbom-new "$WORK/new.spdx.json"
expect "N9 a rebuild-only image release renders its packages, never the subjects" <<'EOF'
Built from `main` for dependency and system-package updates

**Full changelog**: https://github.com/owner/app/compare/v2.0.0...v2.0.1

<details>
<summary>System packages: 4 changes (busybox, libssl3, ssl_client)</summary>

- `busybox` 1.37.0-r29 to 1.37.0-r30
- Added `libssl3` 3.5.4-r0
- `ssl_client` 1.37.0-r29 to 1.37.0-r30
- Removed `zlib` 1.3.1-r2

</details>
EOF
grep -q 'ghcr.io/owner/app' "$WORK/out" && fail "N9 the image's own SPDX root package was diffed"

# Previous tag by version, numerically; bootstrap has neither link nor diff.
B="$WORK/boot"
new_repo "$B"
c "$B" src/main.go "feat: initial (#1)"
git -C "$B" tag v1.2.0
c "$B" src/a.go "fix: two to nine (#2)"
git -C "$B" tag v1.9.0
c "$B" src/b.go "fix: after nine (#3)"
rn "$B" --repo owner/boot --release-commit HEAD --site go --version v1.10.0
expect "N10 the previous stable tag is chosen by version, not by name" <<'EOF'
### Fixed

- After nine (#3)

**Full changelog**: https://github.com/owner/boot/compare/v1.9.0...v1.10.0
EOF
F="$WORK/first"
new_repo "$F"
c "$F" src/main.go "feat: initial (#1)"
rn "$F" --repo owner/first --release-commit HEAD --site go --version v1.0.0
expect "N11 a first release has no compare link and no dependency diff" <<'EOF'
### Added

- Initial (#1)
EOF

refused "N12a an unstable version" "is not a stable version of this lane" "$T" "${TB[@]}" --site go --version v2.0.0-dev.1 --out "$WORK/out"
refused "N12b a lane version without the lane prefix" "is not a stable version of this lane" "$T" "${TB[@]}" --site lane --lane yamlenv --version v1.1.0 --out "$WORK/out"
refused "N12c no --repo" "--repo is required" "$T" --release-commit "$R" --site go --version v2.0.0 --out "$WORK/out"
refused "N12d no --release-commit" "--release-commit is required" "$T" --repo owner/app --site go --version v2.0.0 --out "$WORK/out"
refused "N12e SBOMs off the docker site" "only valid with --site docker" "$T" "${TB[@]}" --site go --version v2.0.0 --sbom-new "$WORK/new.spdx.json" --out "$WORK/out"
refused "N12f --release-model is not an argument" "unknown argument: --release-model" "$T" --release-model two-branch "${TB[@]}" --site go --version v2.0.0 --out "$WORK/out"
refused "N12g an unknown site" "--site must be" "$T" "${TB[@]}" --site npm --version v2.0.0 --out "$WORK/out"
rc=0
(cd "$T" && GH_TOKEN=x bash "$SCRIPT" "${TB[@]}" --site go --version v2.0.0 --out "$WORK/out") >"$WORK/refused.log" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] || ! grep -qF "refusing to run with GITHUB_TOKEN or GH_TOKEN set" "$WORK/refused.log"; then
  fail "N12h a token in the environment was not refused: $(cat "$WORK/refused.log")"
fi
ok "N12h a token in the environment (rc $rc)"
# A config whose command dumps the environment it runs in sees no OIDC request pair.
sed -e '/^\[changelog\]/a postprocessors = [{ pattern = "^", replace_command = "env >>'"$WORK"'/cliff-env; cat" }]' \
  "$CFG" >"$WORK/hostile.toml"
grep -q "replace_command = \"env >>$WORK/cliff-env" "$WORK/hostile.toml" || fail "N12l the hostile config was not written"
rm -f "$WORK/cliff-env"
(cd "$T" && CLIFF_BIN="$CLIFF" ACTIONS_ID_TOKEN_REQUEST_URL=https://oidc.invalid ACTIONS_ID_TOKEN_REQUEST_TOKEN=probe-oidc \
  bash "$SCRIPT" "${TB[@]}" --site go --version v2.0.0 --config "$WORK/hostile.toml" --out "$WORK/out") \
  >"$WORK/rn.log" 2>&1 || fail "N12l render-notes failed: $(cat "$WORK/rn.log")"
[ -s "$WORK/cliff-env" ] || fail "N12l the config's command never ran"
if grep -q '^ACTIONS_ID_TOKEN_REQUEST_' "$WORK/cliff-env"; then
  fail "N12l the cliff config read the OIDC request pair"
fi
ok "N12l a cliff command runs without the OIDC request pair"
refused "N12j malformed go lanes" "--go-lanes must be a JSON array" "$T" "${TB[@]}" --site go --version v2.0.0 --go-lanes '{"a":1}' --out "$WORK/out"
refused "N12k a lane dir off the lane site" "--lane is only valid" "$T" "${TB[@]}" --site go --lane yamlenv --version v2.0.0 --out "$WORK/out"

# The range start the docker caller needs for the previous SBOM, from the same rule.
prev_of() { # <repo> <args...> -> the printed previous tag, or EXIT=n
  local repo=$1
  shift
  (cd "$repo" && CLIFF_BIN=/nonexistent bash "$SCRIPT" --print-previous "$@") 2>/dev/null || echo "EXIT=$?"
}
[ "$(prev_of "$B" --site docker --version v1.10.0)" = v1.9.0 ] || fail "N13a --print-previous must pick v1.9.0 by version"
ok "N13a --print-previous picks the previous stable tag by version, running no git-cliff"
[ "$(prev_of "$F" --site docker --version v1.0.0)" = "" ] || fail "N13b a first release must print nothing"
ok "N13b a first release prints no previous tag"
[ "$(prev_of "$T" --site lane --lane yamlenv --version yamlenv/v1.1.0)" = yamlenv/v1.0.0 ] \
  || fail "N13c the lane's previous tag must be its own"
ok "N13c a lane's previous tag is its own"
[ "$(prev_of "$B" --site docker --version v1.10.0-dev.1)" = "EXIT=2" ] || fail "N13d an unstable version must be refused"
ok "N13d --print-previous refuses a version that is not the lane's"

printf '#!/bin/sh\nexit 3\n' >"$WORK/cliff-fails"
chmod +x "$WORK/cliff-fails"
for args in "--site go --version v2.0.0" "--site docker --version v2.0.0" "--site lane --lane yamlenv --version yamlenv/v1.1.0"; do
  # shellcheck disable=SC2086 # word-split on purpose
  if (cd "$T" && CLIFF_BIN="$WORK/cliff-fails" bash "$SCRIPT" "${TB[@]}" $args --out "$WORK/out") >/dev/null 2>&1; then
    fail "N14 a git-cliff error must fail the $args render"
  fi
done
ok "N14 a git-cliff error fails every site's render"

echo "PASS: render-notes ($CHECKS checks)"
