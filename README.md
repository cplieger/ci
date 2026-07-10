# cplieger/ci

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/ci/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/ci)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13201/badge)](https://www.bestpractices.dev/projects/13201)

Shared CI/CD for the `cplieger` repos: reusable GitHub Actions workflows, a
composite versioning action, and canonical lint/format configs. One source of
truth — consumer repos reference it instead of carrying duplicate copies.

> Pin every reference to a tag (`@v1`).

## Reusable workflows

| Workflow                         | Purpose                                                                                    |
| -------------------------------- | ------------------------------------------------------------------------------------------ |
| `.github/workflows/go-ci.yaml`   | Go-library checks: vet, golangci-lint, race tests, govulncheck, gitleaks                   |
| `.github/workflows/ts-ci.yaml`   | Build-less TS checks: knip, eslint, tsc typecheck, vitest, prettier (+ optional web-lint)  |
| `.github/workflows/release.yaml` | git-cliff version → (TS) npm + JSR publish → tag + GitHub Release                          |

### Consume in a Go library

```yaml
# .github/workflows/ci.yaml
name: CI
on: { pull_request: { branches: [main] }, push: { branches: [main] } }
jobs:
  ci:
    uses: cplieger/ci/.github/workflows/go-ci.yaml@v1
```

```yaml
# .github/workflows/release.yaml
name: Release
on: { push: { branches: [main] }, workflow_dispatch: {} }
jobs:
  release:
    uses: cplieger/ci/.github/workflows/release.yaml@v1
    with: { target: go }
```

### Consume in a TypeScript library

```yaml
jobs:
  ci:
    uses: cplieger/ci/.github/workflows/ts-ci.yaml@v1
    with: { working-directory: "." } # or web-lint: true for CSS/HTML
  release:
    uses: cplieger/ci/.github/workflows/release.yaml@v1
    with: { target: ts }
```

Publishing uses **OIDC trusted publishing** for npm and JSR — no token needed
once the package is linked to its repo on npmjs.com / jsr.io. (Optionally pass a
`NPM_TOKEN` secret instead.) `release.yaml` requires `id-token: write`, which it
declares itself.

## Renovate preset

The Renovate preset lives in [`cplieger/.github`](https://github.com/cplieger/.github)
as `default.json`, not in this repo; this repo provides the reusable workflows
and canonical lint/format configs. Each repo replaces its `renovate.json` with a
one-liner that extends the preset (Renovate fetches it natively):

```json
{ "extends": ["github>cplieger/.github"] }
```

## Canonical configs (synced)

Tools without remote-config support get their config pushed here as PRs by
`sync.yaml` (the repo↔file mapping is generated at sync time by
`scripts/classify-repos.sh`, not committed):

| File                                                                           | Consumed by                                              |
| ------------------------------------------------------------------------------ | -------------------------------------------------------- |
| `.golangci.yaml`                                                               | Go repos (golangci-lint)                                 |
| `cliff.toml`                                                                   | all (git-cliff changelog/version)                        |
| `.editorconfig`                                                                | all                                                      |
| `configs/eslint.config.base.mjs`                                               | TS repos — `import base from "./eslint.config.base.mjs"` |
| `configs/prettier.json`, `configs/stylelint.json`, `configs/htmlvalidate.json` | TS repos                                                 |
| `configs/image-smoke.sh`                                                       | image repos that opt in with `tests/image-smoke.conf` — synced to `tests/image-smoke.sh` (canonical runtime smoke harness) |

The unified-CI group also syncs four workflows into each consumer repo:
`.github/workflows/{ci,codeql,security,scorecard}.yml`. `scorecard.yml`
(OpenSSF Scorecard, self-contained) feeds the README OpenSSF badge.

## README badges

See [`BADGES.md`](BADGES.md) for the canonical badge block per repo type (Go
lib, TS lib, hybrid, Docker image). The rule of thumb: prefer dynamic badges,
never hardcode a version (the base-image badge is name-only), and keep one
badge order across the repos. The badge row is per-repo (not
synced) because it carries per-repo URLs.

## Composite action

`actions/git-cliff-version` installs git-cliff and outputs `version` + a
`release` boolean from conventional commits. Used by `release.yaml`; callable
directly if needed.

## Disclaimer

This project is built with care and follows security best practices, but it is intended for personal / self-hosted use. No guarantees of fitness for production environments. Use at your own risk.

This project was built with AI-assisted tooling using [Claude Opus](https://www.anthropic.com/claude) and [Kiro](https://kiro.dev). The human maintainer defines architecture, supervises implementation, and makes all final decisions.
