# cplieger/ci

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/ci/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/ci)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13201/badge)](https://www.bestpractices.dev/projects/13201)

Shared CI/CD for the `cplieger` repos: reusable GitHub Actions workflows,
composite actions, canonical lint/format configs, and a cross-repo governance
audit. One source of truth — consumer repos reference it instead of carrying
duplicate copies.

> Pin every reusable-workflow reference to a **full commit SHA** with a release
> tag comment, e.g. `@<40-hex-sha> # v2`. Renovate tracks the comment and
> bumps the SHA when the major tag moves. Never pin to a branch.

## Reusable workflows

| Workflow                                | Purpose                                                                                                                                                     |
|-----------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `.github/workflows/ci.yaml`             | Meta CI entry point: detects repo surfaces (go.mod / jsr.json / web dir / Dockerfile / scripts) and dispatches the jobs below into one `ci / validate` gate |
| `.github/workflows/go-ci.yaml`          | Go checks: vet, golangci-lint, race tests, govulncheck, deadcode/punused (apps), wiregen drift, gitleaks                                                    |
| `.github/workflows/ts-ci.yaml`          | TS checks: eslint, tsc typecheck, vitest, prettier, knip, version parity, import-map coverage (+ optional `web-lint` for CSS/HTML)                          |
| `.github/workflows/shell-ci.yaml`       | Shell/Docker checks: actionlint, shellcheck, shfmt, hadolint, gitleaks                                                                                      |
| `.github/workflows/release.yaml`        | Auto-detects release type (Docker / TS / Go), computes the git-cliff version, publishes (npm + JSR via OIDC), tags + GitHub Release                         |
| `.github/workflows/docker-release.yaml` | Multi-arch image build on native runners, Trivy scan, SBOM, cosign signing, release notes (called by `release.yaml`)                                        |
| `.github/workflows/coverage.yaml`       | Go/TS coverage → shields endpoint badge on the orphan `badges` branch                                                                                       |
| `.github/workflows/codeql.yaml`         | CodeQL with language auto-detect (public repos)                                                                                                             |
| `.github/workflows/security-scan.yaml`  | Trivy repo/config/image scans, advisory only — findings report to the Security tab, never block                                                             |

Repo-internal automation (not for consumers): `sync.yaml` (config
propagation), `self-release.yaml` + `move-major-tag.yaml` (tag cutting),
`audit.yaml` (weekly governance audit), `weekly-gremlins.yaml` /
`weekly-stryker.yaml` / `weekly-fuzz.yaml` (fleet Go + TS mutation testing,
Go fuzzing), `daily-security.yaml` (scan fan-out), `trigger-renovate.yaml`,
`backfill-release-sbom-sigs.yaml`, plus this repo's own `self-ci.yaml`,
`self-codeql.yml`, `scorecard.yml`.

## Consuming

Consumer repos do **not** hand-write these callers: `sync.yaml` pushes the
workflow templates (`.github/workflow-templates/`) into every releaseable repo
as PRs. The synced CI caller is a thin shim — all logic stays central:

```yaml
# .github/workflows/ci.yaml (synced — DO NOT EDIT)
jobs:
  ci:
    uses: cplieger/ci/.github/workflows/ci.yaml@<sha> # v2
```

```yaml
# .github/workflows/release.yaml (synced — DO NOT EDIT)
jobs:
  release:
    uses: cplieger/ci/.github/workflows/release.yaml@<sha> # v2
    secrets: inherit
```

`release.yaml` takes no inputs: it auto-detects the release type from the repo
surface (Dockerfile → image, `jsr.json` → npm + JSR, `go.mod` → Go tag).
Publishing uses **OIDC trusted publishing** for npm and JSR — no registry
tokens; the package just needs to be linked to its repo on npmjs.com /
jsr.io. `release.yaml` declares the `id-token: write` permission itself.

## Renovate preset

The Renovate preset lives in [`cplieger/.github`](https://github.com/cplieger/.github)
as `default.json`, not in this repo; this repo provides the reusable workflows
and canonical lint/format configs. Each repo carries a synced one-liner
`renovate.json` that extends the preset (Renovate fetches it natively):

```json
{ "extends": ["github>cplieger/.github"] }
```

## Canonical configs (synced)

Tools without remote-config support get their config pushed to consumers as
PRs by `sync.yaml` (the repo↔file mapping is generated at sync time by
`scripts/classify-repos.py`, not committed):

| Source (this repo)                                                                                               | Synced to                                            |
|------------------------------------------------------------------------------------------------------------------|------------------------------------------------------|
| `.editorconfig`, `.gitattributes`, `LICENSE`, `configs/renovate.json`                                            | all releaseable repos                                |
| `.golangci.yaml`, `configs/gremlins.yaml` (→ `.gremlins.yaml`)                                                   | Go repos                                             |
| `configs/eslint.config.base.mjs`, `configs/prettier.json`, `configs/stylelint.json`, `configs/htmlvalidate.json` | TS repos (incl. hybrids)                             |
| `configs/cliff-stable.toml` / `configs/cliff-alpha.toml` (→ `cliff.toml`)                                        | releaseable repos, tier by latest tag (v0.x → alpha) |
| `configs/ruff.toml` (→ `ruff.toml`)                                                                              | Python repos                                         |
| `configs/image-smoke.sh` (→ `tests/image-smoke.sh`)                                                              | image repos opting in via `tests/image-smoke.conf`   |

The unified-CI group also syncs six workflow files into each consumer repo:
`ci.yaml`, `codeql.yml`, `security.yml`, `scorecard.yml`, `coverage.yml`, and
`release.yaml`. `scorecard.yml` (OpenSSF Scorecard, self-contained) feeds the
README OpenSSF badge.

## README badges

See [`BADGES.md`](BADGES.md) for the canonical badge block per repo type (Go
lib, TS lib, hybrid, Docker image). The rule of thumb: prefer dynamic badges,
never hardcode a version (the base-image badge is name-only), and keep one
badge order across the repos. The badge row is per-repo (not
synced) because it carries per-repo URLs.

## Composite actions

- `actions/git-cliff-version` — installs git-cliff and outputs `version` + a
  `release` boolean from conventional commits. Used by `release.yaml`; callable
  directly.
- `actions/publish-badge` — writes a shields.io endpoint JSON to the orphan
  `badges` branch, preserving sibling badge files. Used by `coverage.yaml`,
  `docker-release.yaml` (image size), and `weekly-gremlins.yaml` (mutation
  score).

## Local tooling

- `ci-local.sh [app-dir]` — replays the CI battery locally (mirrors the gate).
- `scripts/install-local-tools.sh` — installs the CI-pinned tool versions
  locally so local lint/scan results match CI.

## Disclaimer

This project is built with care and follows security best practices, but it is intended for personal / self-hosted use. No guarantees of fitness for production environments. Use at your own risk.

This project was built with AI-assisted tooling using [Claude](https://claude.com), [GPT](https://openai.com), and [Kiro](https://kiro.dev). The human maintainer defines architecture, supervises implementation, and makes all final decisions.
