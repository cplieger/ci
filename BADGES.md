# README badge standard

Canonical badge blocks for `cplieger` repos. The README badge row is **not**
synced (it carries per-repo URLs), so this is the reference to copy from when
creating a repo or auditing an existing one. Replace `REPO` with the repo name
and `MODPATH` with the Go module path (usually `REPO`, but a `/v2`-style major
suffix for versioned modules, e.g. `metrics/v2`).

## Principles

1. **Dynamic over static.** Prefer badges that read live state (pkg.go.dev,
   npm, JSR, Go Report Card, OpenSSF, image size, coverage, mutation) over
   hand-written values. A hand-written value is a future stale value.
2. **No hardcoded versions in a badge.** The base-image badge carries the base
   **name only** (`Alpine`, `Caddy`, `Distroless`, `scratch`) — never a patch
   version. Renovate bumps the `Dockerfile` `FROM` constantly; a version in the
   badge silently rots. The exact pin lives in the `Dockerfile` + the SBOM.
3. **No CI or release badge.** Both were dropped fleet-wide:
   - **CI**: a workflow status badge tracks the latest run on the _default
     branch_. With required PR checks + auto-merge, nothing lands on `main`
     until it is already green, so the badge is a near-permanent green
     decoration that conveys nothing. Red would be a rare flake, not a signal a
     consumer can act on.
   - **Release/version**: redundant on every surface a consumer actually reads.
     GitHub shows it in the sidebar; Docker Hub shows the latest tag on the
     page; pkg.go.dev / npm / JSR all surface the version natively. The Go
     Reference / npm / JSR badges already carry version-to-docs.
4. **One style, one order.** Same per-type order (below). Group as
   identity/version → quality → security/supply-chain.
5. **Every badge earns its place, and the row has a credibility cliff.** No
   decorative badges; each communicates where to get it, docs, code quality, or
   security posture. Past ~8 badges a row reads as a trophy case and people stop
   trusting any single badge — keep rows at 8 or under and prune hard.

## Blocks by repo type

### Go library

```markdown
[![Go Reference](https://pkg.go.dev/badge/github.com/cplieger/MODPATH.svg)](https://pkg.go.dev/github.com/cplieger/MODPATH)
[![Go version](https://img.shields.io/github/go-mod/go-version/cplieger/REPO)](https://github.com/cplieger/REPO/blob/main/go.mod)
[![Go Report Card](https://goreportcard.com/badge/github.com/cplieger/REPO)](https://goreportcard.com/report/github.com/cplieger/REPO)
[![Test coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/coverage.json)](https://github.com/cplieger/REPO/actions/workflows/coverage.yml)
[![Mutation](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/mutation.json)](https://github.com/cplieger/REPO/issues?q=label%3Agremlins-tracker)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
```

`Go Reference` uses `MODPATH` (with the `/v2` suffix if any); `Go Report Card`
and `Go version` always use the bare `REPO`. Omit the **Mutation** badge on
repos below the weekly-gremlins size threshold (≈200 LOC of non-test Go) — they
get no mutation run, so the badge would read `invalid`.

### TypeScript library

```markdown
[![npm](https://img.shields.io/npm/v/@cplieger/REPO)](https://www.npmjs.com/package/@cplieger/REPO)
[![JSR](https://jsr.io/badges/@cplieger/REPO)](https://jsr.io/@cplieger/REPO)
[![Test coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/coverage.json)](https://github.com/cplieger/REPO/actions/workflows/coverage.yml)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
```

No **Node version** badge. It read `engines.node` from the published npm
package, so unless `package.json` declared `engines.node` it rendered
`node | not specified` — and even when populated it links to the same npm
package page as the npm badge, so it fails principle #5 (every badge earns its
place). Dropped fleet-wide (`actions`, `reactive`). No **Mutation** badge either
— gremlins is Go-only (there is no Stryker equivalent wired up).

### Hybrid Go + TS library (e.g. web-terminal-engine)

```markdown
[![Go Reference](https://pkg.go.dev/badge/github.com/cplieger/REPO.svg)](https://pkg.go.dev/github.com/cplieger/REPO)
[![npm](https://img.shields.io/npm/v/@cplieger/REPO)](https://www.npmjs.com/package/@cplieger/REPO)
[![JSR](https://jsr.io/badges/@cplieger/REPO)](https://jsr.io/@cplieger/REPO)
[![Go version](https://img.shields.io/github/go-mod/go-version/cplieger/REPO)](https://github.com/cplieger/REPO/blob/main/go.mod)
[![Go Report Card](https://goreportcard.com/badge/github.com/cplieger/REPO)](https://goreportcard.com/report/github.com/cplieger/REPO)
[![Test coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/coverage.json)](https://github.com/cplieger/REPO/actions/workflows/coverage.yml)
[![Mutation](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/mutation.json)](https://github.com/cplieger/REPO/issues?q=label%3Agremlins-tracker)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
```

9 badges — one over the soft cap, the one deliberate exception to the ≤8 rule.
A hybrid lib carries both ecosystems' identity badges (Go Reference + npm + JSR)
_and_ earns a Mutation badge (gremlins runs on its Go surface), so dropping the
Node-version badge (now gone fleet-wide) gets it to 9, not 8. The single
Node-version badge is not re-added here. Coverage and the Mutation badge reflect
whichever surface `coverage.yaml` / gremlins measure on the repo (Go, for
web-terminal-engine).

### Docker image (built from Go source in this repo)

```markdown
[![Image Size](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/size.json)](https://github.com/cplieger/REPO/pkgs/container/CONTAINER)
![Platforms](https://img.shields.io/badge/platforms-amd64%20%7C%20arm64-blue)
![base: NAME](https://img.shields.io/badge/base-NAME-COLOR?logo=LOGO)
[![Go Report Card](https://goreportcard.com/badge/github.com/cplieger/REPO)](https://goreportcard.com/report/github.com/cplieger/REPO)
[![Test coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/coverage.json)](https://github.com/cplieger/REPO/actions/workflows/coverage.yml)
[![Mutation](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/mutation.json)](https://github.com/cplieger/REPO/issues?q=label%3Agremlins-tracker)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
[![SBOM](https://img.shields.io/badge/SBOM-SPDX-1D4ED8)](https://github.com/cplieger/REPO/releases)
```

### Docker image (thin upstream wrapper, no Go source)

```markdown
[![Image Size](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/size.json)](https://github.com/cplieger/REPO/pkgs/container/CONTAINER)
![Platforms](https://img.shields.io/badge/platforms-amd64%20%7C%20arm64-blue)
![base: NAME](https://img.shields.io/badge/base-NAME-COLOR?logo=LOGO)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
[![SBOM](https://img.shields.io/badge/SBOM-SPDX-1D4ED8)](https://github.com/cplieger/REPO/releases)
```

- The **Test coverage** and **Mutation** rows apply only to images **built from
  Go source in this repo**. Omit both for thin upstream-wrapper images
  (`docker-caddy`, `docker-keepalived`, `docker-nut-upsd`, `docker-radvd`,
  `docker-smtp-relay`, `docker-static-web`), which have no statement coverage
  and no mutation run.
- `CONTAINER` is the GHCR package name. It equals `REPO` for every current
  image repo (the image is pushed as `ghcr.io/cplieger/REPO`), so the link is
  `pkgs/container/REPO`. Earlier revisions listed short names (`fclones`,
  `nut-upsd`, `smtp-relay`) for `docker-fclones-scheduler` / `docker-nut-upsd` /
  `docker-smtp-relay`, but no such packages exist — those links 404. Use the
  repo name.
- `base` is **name-only**: `Alpine` (`0D597F`, `logo=alpinelinux`), `Caddy`
  (`1F88C0`, `logo=caddy`), `Distroless` / `distroless%2Fstatic` (`4285F4` /
  `2496ED`, `logo=google` / `logo=docker`), `scratch` (`2496ED`,
  `logo=docker`), `renovate%2Frenovate` (`1A1F6C`). Where the image runs as a
  non-root user on a base whose name does not already imply it (Alpine, Caddy,
  scratch), encode that in the base label (e.g. `base: Alpine (rootless)`)
  rather than adding a separate badge — distroless `nonroot` variants already
  say it in the name.
- Image Size reads a self-published `size.json` from the orphan `badges` branch
  (see "Image size badge wiring" below), so it has **no external dependency**.
  The previous third-party service (`ghcr-badge.egpl.dev`) was suspended in 2026
  and broke every size badge at once; GHCR still has no first-party shields
  support ([badges/shields#5594]), which is why we publish the value ourselves
  rather than point at another hosted service.

[badges/shields#5594]: https://github.com/badges/shields/issues/5594

## Test coverage badge wiring

The **Test coverage** badge reads a shields `endpoint` JSON published to an
orphan `badges` branch in each repo by the synced `coverage.yml` workflow (which
calls `cplieger/ci`'s reusable `coverage.yaml`). It runs on push to `main`,
measures real statement coverage (Go: `go test -coverpkg=./...`, which includes
classic, `rapid` property, and fuzz-seed tests; TS: vitest v8), and force-pushes
`coverage.json` to the `badges` branch using the built-in `GITHUB_TOKEN` — **no
external service and no per-repo secret**. The badge label is `Test coverage`
(set in `coverage.yaml`, not the README — the `[![Test coverage]…]` alt text is
cosmetic; the visible label comes from the endpoint JSON). The badge shows
`invalid` until the first run on `main` publishes the file. Only the Go/TS repos
receive `coverage.yml`.

Publishing goes through `scripts/publish-badge.sh`, which **preserves sibling
badge files** on the branch (so `coverage.json` and `mutation.json` coexist
instead of clobbering each other — see below).

## Mutation badge wiring

The **Mutation** badge reads `mutation.json` from the same orphan `badges`
branch. It is published by the `badge` job in `cplieger/ci`'s
`weekly-gremlins.yaml`, which runs gremlins (Go mutation testing) across every
Go-having repo above ≈200 LOC of non-test Go, three times each, on a weekly
schedule (Sundays 22:00 UTC). `scripts/gremlins-aggregate.py --badge-file`
computes the mean efficacy (kill rate) from the per-attempt artifacts and
`scripts/publish-badge.sh` force-pushes `mutation.json` alongside
`coverage.json`. Colour bands are tuned lower than coverage (≥85 brightgreen,
≥70 green, ≥50 yellow, ≥30 orange, else red), because a healthy suite kills most
but rarely all runnable mutants (equivalent mutants form a noise floor). The
badge links to the per-repo `gremlins-tracker` issue, which carries the rolling
12-week history and the current live-mutant list. It shows `invalid` until the
first weekly run publishes the file, and updates weekly (not per-push — mutation
testing is too expensive for the PR path).

`scripts/publish-badge.sh` requires a token with `contents:write` on the target
repo. Coverage uses the consumer's own `GITHUB_TOKEN` (it runs in-repo);
weekly-gremlins runs in `cplieger/ci` and pushes cross-repo, so it uses the
`CI_SCHEDULE` PAT (which already clones consumers and edits their tracker
issues).

## Image size badge wiring

The **Image Size** badge (image repos only) reads `size.json` from the same
orphan `badges` branch as coverage/mutation. It is published by the
`docker-release.yaml` finalize job on every image build, so it refreshes
whenever the image is actually rebuilt (a source change, a base-image bump, or a
release) — the same "publish on every build" model as the coverage badge. The
job sums the compressed (download) layer sizes of the `linux/amd64` sub-manifest
of the just-pushed image (`docker buildx imagetools inspect --raw`, no pull) and
publishes `{"label":"image size","message":"<N> MB"}` through
`scripts/publish-badge.sh` (sibling-preserving, so it coexists with
`coverage.json` / `mutation.json`) using the consumer's own `GITHUB_TOKEN`
(finalize already has `contents: write`). amd64 is reported by convention (the
size a typical consumer pulls); arm64 differs by a few percent. The step is
`continue-on-error` — a badge hiccup never fails a release. The badge shows
`invalid` until the first build after this wiring landed publishes the file
(a one-time backfill seeded the existing repos so they didn't wait for a
release). No external service and no per-repo secret.

## OpenSSF Scorecard wiring

The badge reads `api.scorecard.dev`, populated by `ossf/scorecard-action`
running with `publish_results: true`. That workflow is **synced fleet-wide**:
`.github/workflow-templates/scorecard.yml` → `.github/workflows/scorecard.yml`
on every public consumer repo (added to the unified-CI group in
`scripts/classify-repos.sh`). It is push-triggered (no weekly cron) to stay
within the 20-job account concurrency cap. The badge shows `no data` until the
first run on `main` completes after the workflow lands.

## OpenSSF Best Practices badge

The **OpenSSF Best Practices** badge links the repo to its entry on the
metal-tier badge program (`bestpractices.dev`). `PROJECT_ID` is **per repo**
(it is the numeric project id, not synced); fill it in from the repo's entry.
The badge image reflects the live tiered status (in-progress / passing / silver
/ gold).

## SBOM badge

The **SBOM** badge (image repos only) links to the published software bill of
materials. Every image release produces a syft SPDX-JSON SBOM
(`anchore/sbom-action`, `format: spdx-json`) and publishes it two ways: a
`cosign attest --type spdxjson` attestation on the GHCR (and Docker Hub) image,
and a signed `sbom.spdx.json` (+ `sbom.spdx.json.sigstore.json`) asset on the
GitHub Release. The badge therefore links to the repo's **Releases** page (where
the SBOM is a named, signed, downloadable asset) — not `/attestations`, which is
empty because the flow uses cosign registry attestations, not GitHub-native
`actions/attest-*`. Label is `SPDX` (the format actually produced), not
CycloneDX. Verified present on every image repo's latest release before adding.
Libraries omit it — their `go.mod` / `package-lock.json` _is_ the bill of
materials.

## Notes

- The **base-image version** problem is solved structurally (name-only), so it
  cannot rot. If a future repo genuinely needs the exact base version surfaced,
  add a Renovate `customManager` to bump the badge literal in lockstep with the
  `Dockerfile` `FROM` — do **not** hand-write it.
- For dual-published images, shields offers first-party Docker Hub badges
  (`docker/pulls`, `docker/image-size`, `docker/v`). We use the self-published
  GHCR size badge instead, because GHCR is the primary registry, the same badge
  works for the GHCR-only repos (`subflux`, `vibecli`, `vibekit`), and a
  self-published value depends on no third-party service.
- **License** and **Code of Conduct** badges were considered for Docker Hub
  (which lacks the GitHub chrome that surfaces both) and rejected: they would
  render redundantly on the GitHub copy of the README, and version/license are
  one click away on the linked GitHub repo. Keeping image rows at ≤8 uniform
  badges won out over Docker Hub legibility.
