# cplieger/ci

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cplieger/ci/badge)](https://scorecard.dev/viewer/?uri=github.com/cplieger/ci)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13201/badge)](https://www.bestpractices.dev/projects/13201)

Shared CI/CD for the `cplieger` repos: reusable GitHub Actions workflows, a
composite versioning action, canonical lint/format configs, and a Renovate
preset. One source of truth — consumer repos reference it instead of carrying
duplicate copies.

> Pin every reference to a tag (`@v1`). Tag this repo `v1` after the first
> commit so consumers can resolve `@v1`.

## Reusable workflows

| Workflow                         | Purpose                                                                                    |
| -------------------------------- | ------------------------------------------------------------------------------------------ |
| `.github/workflows/go-ci.yaml`   | Go-library checks: vet, golangci-lint, race tests, govulncheck, fieldalignment, gitleaks   |
| `.github/workflows/ts-ci.yaml`   | Build-less TS checks: knip, eslint, tsgo typecheck, vitest, prettier (+ optional web-lint) |
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

Replace each repo's `renovate.json` with a one-liner — Renovate fetches the
preset (`default.json`) natively:

```json
{ "extends": ["github>cplieger/ci"] }
```

## Canonical configs (synced)

Tools without remote-config support get their config pushed here as PRs by
`sync.yaml` (see `.github/sync.yml` for the repo↔file mapping):

| File                                                                           | Consumed by                                              |
| ------------------------------------------------------------------------------ | -------------------------------------------------------- |
| `.golangci.yaml`                                                               | Go repos (golangci-lint)                                 |
| `cliff.toml`                                                                   | all (git-cliff changelog/version)                        |
| `.editorconfig`                                                                | all                                                      |
| `configs/eslint.config.base.mjs`                                               | TS repos — `import base from "./eslint.config.base.mjs"` |
| `configs/prettier.json`, `configs/stylelint.json`, `configs/htmlvalidate.json` | TS repos                                                 |

Syncing needs a `SYNC_PAT` repo secret (fine-grained PAT, Contents:write +
Pull-requests:write on the targets).

The unified-CI group also syncs three workflows into each consumer repo:
`.github/workflows/{ci,codeql,security,scorecard}.yml`. `scorecard.yml`
(OpenSSF Scorecard, self-contained) feeds the README OpenSSF badge.

## README badges

See [`BADGES.md`](BADGES.md) for the canonical badge block per repo type (Go
lib, TS lib, hybrid, Docker image). The rule of thumb: prefer dynamic badges,
never hardcode a version (the base-image badge is name-only), and keep one
badge order across the fleet. The badge row is per-repo (not
synced) because it carries per-repo URLs.

## Composite action

`actions/git-cliff-version` installs git-cliff and outputs `version` + a
`release` boolean from conventional commits. Used by `release.yaml`; callable
directly if needed.

## Cross-repo audit

`scripts/audit.py` lists every public `cplieger` repo and checks shared-standard
compliance (license, default branch, CI wired to `cplieger/ci`, renovate preset;
description + topics as warnings). Repos that have adopted the standard must pass
the hard checks; legacy repos are reported for visibility only.

```
gh auth login        # once
python3 scripts/audit.py
```

`.github/workflows/audit.yaml` runs it weekly (and on demand) and writes the
table to the run summary. It uses the job token for public repos; add an
`AUDIT_PAT` secret to extend coverage to private repos.
