# README badge standard

Canonical badge blocks for `cplieger` repos. The README badge row is **not**
synced (it carries per-repo URLs), so this is the reference to copy from when
creating a repo or auditing an existing one. Replace `REPO` with the repo name
and `MODPATH` with the Go module path (usually `REPO`, but a `/v2`-style major
suffix for versioned modules, e.g. `metrics/v2`).

## Principles

1. **Dynamic over static.** Prefer badges that read live state (CI, release,
   pkg.go.dev, npm, JSR, Go Report Card, OpenSSF, image size) over hand-written
   values. A hand-written value is a future stale value.
2. **No hardcoded versions in a badge.** The base-image badge carries the base
   **name only** (`Alpine`, `Caddy`, `Distroless`, `scratch`) — never a patch
   version. Renovate bumps the `Dockerfile` `FROM` constantly; a version in the
   badge silently rots. The exact pin lives in the `Dockerfile` + the SBOM.
3. **One style, one order.** Same per-type order (below). CI badge first.
4. **Every badge earns its place.** No decorative badges; each communicates
   build health, where to get it, docs, or security posture.

## Blocks by repo type

### Go library

```markdown
[![CI](https://github.com/cplieger/REPO/actions/workflows/ci.yaml/badge.svg)](https://github.com/cplieger/REPO/actions/workflows/ci.yaml)
[![Go Reference](https://pkg.go.dev/badge/github.com/cplieger/MODPATH.svg)](https://pkg.go.dev/github.com/cplieger/MODPATH)
[![Go Report Card](https://goreportcard.com/badge/github.com/cplieger/REPO)](https://goreportcard.com/report/github.com/cplieger/REPO)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/coverage.json)](https://github.com/cplieger/REPO/actions/workflows/coverage.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
```

`Go Reference` uses `MODPATH` (with the `/v2` suffix if any); `Go Report Card`
always uses the bare `REPO`.

### TypeScript library

```markdown
[![CI](https://github.com/cplieger/REPO/actions/workflows/ci.yaml/badge.svg)](https://github.com/cplieger/REPO/actions/workflows/ci.yaml)
[![npm](https://img.shields.io/npm/v/@cplieger/REPO)](https://www.npmjs.com/package/@cplieger/REPO)
[![JSR](https://jsr.io/badges/@cplieger/REPO)](https://jsr.io/@cplieger/REPO)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/coverage.json)](https://github.com/cplieger/REPO/actions/workflows/coverage.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
```

### Hybrid Go + TS library (e.g. vterm)

```markdown
[![CI](.../ci.yaml/badge.svg)](...)
[![Go Reference](https://pkg.go.dev/badge/github.com/cplieger/REPO.svg)](https://pkg.go.dev/github.com/cplieger/REPO)
[![npm](https://img.shields.io/npm/v/@cplieger/REPO)](https://www.npmjs.com/package/@cplieger/REPO)
[![JSR](https://jsr.io/badges/@cplieger/REPO)](https://jsr.io/@cplieger/REPO)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/coverage.json)](https://github.com/cplieger/REPO/actions/workflows/coverage.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
```

### Docker image

```markdown
[![CI](https://github.com/cplieger/REPO/actions/workflows/ci.yaml/badge.svg)](https://github.com/cplieger/REPO/actions/workflows/ci.yaml)
[![GitHub release](https://img.shields.io/github/v/release/cplieger/REPO)](https://github.com/cplieger/REPO/releases)
[![Image Size](https://ghcr-badge.egpl.dev/cplieger/REPO/size)](https://github.com/cplieger/REPO/pkgs/container/CONTAINER)
![Platforms](https://img.shields.io/badge/platforms-amd64%20%7C%20arm64-blue)
![base: NAME](https://img.shields.io/badge/base-NAME-COLOR?logo=LOGO)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/cplieger/REPO/badges/coverage.json)](https://github.com/cplieger/REPO/actions/workflows/coverage.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/REPO/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/REPO)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/PROJECT_ID/badge)](https://www.bestpractices.dev/projects/PROJECT_ID)
```

- The **Coverage** row applies only to images **built from Go source in this
  repo** (they publish a `coverage.json` via the synced `coverage.yml`). Omit
  it for thin upstream-wrapper images (`docker-caddy`, `docker-keepalived`,
  `docker-nut-upsd`, `docker-radvd`, `docker-smtp-relay`, `docker-static-web`),
  which have no statement coverage.

- `CONTAINER` is the GHCR package name (often `REPO`, but some differ, e.g.
  `fclones`, `nut-upsd`, `smtp-relay`).
- `base` is **name-only**: `Alpine` (`0D597F`, `logo=alpinelinux`), `Caddy`
  (`1F88C0`, `logo=caddy`), `Distroless` / `distroless%2Fstatic` (`4285F4` /
  `2496ED`, `logo=google` / `logo=docker`), `scratch` (`2496ED`,
  `logo=docker`), `renovate%2Frenovate` (`1A1F6C`).
- Image Size uses `ghcr-badge.egpl.dev` (a third-party service; GHCR has no
  first-party shields support — [badges/shields#5594]). It is the one external
  dependency in the badge row; self-hostable from `eggplants/ghcr-badge` if
  that service ever degrades.

[badges/shields#5594]: https://github.com/badges/shields/issues/5594

## OpenSSF Scorecard wiring

The badge reads `api.scorecard.dev`, populated by `ossf/scorecard-action`
running with `publish_results: true`. That workflow is **synced fleet-wide**:
`.github/workflow-templates/scorecard.yml` → `.github/workflows/scorecard.yml`
on every public consumer repo (added to the unified-CI group in
`scripts/classify-repos.sh`). It is push-triggered (no weekly cron) to stay
within the 20-job account concurrency cap. The badge shows `no data` until the
first run on `main` completes after the workflow lands.

## Coverage badge wiring

The **Coverage** badge reads a shields `endpoint` JSON published to an orphan
`badges` branch in each repo by the synced `coverage.yml` workflow (which calls
`cplieger/ci`'s reusable `coverage.yaml`). It runs on push to `main`, measures
real statement coverage (Go: `go test -coverpkg=./...`, which includes classic,
`rapid` property, and fuzz-seed tests; TS: vitest v8), and force-pushes
`coverage.json` to the `badges` branch using the built-in `GITHUB_TOKEN` — **no
external service and no per-repo secret**. The badge shows `coverage | invalid`
until the first run on `main` publishes the file. Only the Go/TS repos receive
`coverage.yml` (the sync group excludes shell/Dockerfile-only repos).

## OpenSSF Best Practices badge

The **OpenSSF Best Practices** badge links the repo to its entry on the
metal-tier badge program (`bestpractices.dev`). `PROJECT_ID` is **per repo**
(it is the numeric project id, not synced); fill it in from the repo's entry.
The badge image reflects the live tiered status (in-progress / passing / silver
/ gold).

## Notes

- The **base-image version** problem is solved structurally (name-only), so it
  cannot rot. If a future repo genuinely needs the exact base version surfaced,
  add a Renovate `customManager` to bump the badge literal in lockstep with the
  `Dockerfile` `FROM` — do **not** hand-write it.
- For dual-published images, shields offers first-party Docker Hub badges
  (`docker/pulls`, `docker/image-size`, `docker/v`). We standardize on the GHCR
  size badge instead, because GHCR is the primary registry and the same badge
  works for the GHCR-only repos (`subflux`, `vibecli`, `vibekit`).
