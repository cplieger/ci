# Synced files

This page lists every file cplieger/ci copies into other cplieger repositories, where each one lands and which repositories receive it. It is for anyone who wants to copy one of these configs, or to know why a file in a cplieger repository says it is synced.

## How the sync works

`sync.yaml` runs on a push to `main` that changes a synced file, every day at 02:00 UTC, and on demand. It first runs `scripts/classify-repos.py`, which reads each cplieger repository's files and tags to decide what it receives. Then it opens one `chore(sync):` pull request per repository with the new contents and turns on auto-merge. The pull request merges once that repository's `ci / validate` check passes. Forks and archived repositories are skipped.

A synced file is overwritten on the next sync, so change it here, never in the repository that received it. The workflow callers carry a `DO NOT EDIT` header that says so.

## What goes where

| Source in this repository | Lands as | Received by |
| --- | --- | --- |
| `.github/workflow-templates/ci.yml`, `codeql.yml`, `security.yml` | `.github/workflows/ci.yaml`, `codeql.yml`, `security.yml` | repositories with a root `go.mod`, `jsr.json` or `Dockerfile`, plus repositories that publish from their own `.github/workflows/publish.yaml` |
| `.github/workflow-templates/release.yml` | `.github/workflows/release.yaml` | repositories with a root `go.mod`, `jsr.json` or `Dockerfile` |
| `.editorconfig`, `.gitattributes` | the same names | every repository in the two rows above, plus Python repositories |
| `.golangci.yaml`, `configs/gremlins.yaml` | `.golangci.yaml`, `.gremlins.yaml` | Go repositories |
| `configs/eslint.config.base.mjs`, `configs/prettier.json`, `configs/stylelint.json`, `configs/htmlvalidate.json` | `eslint.config.base.mjs`, `.prettierrc.json`, `.stylelintrc.json`, `.htmlvalidate.json` | TypeScript repositories, and Go repositories with a TypeScript or web folder |
| `configs/cliff-stable.toml` or `configs/cliff-alpha.toml` | `cliff.toml` | Go, TypeScript and Docker repositories. A repository whose latest tag starts with `v0.` gets the alpha file |
| `configs/ruff.toml` | `ruff.toml` | Python repositories |
| `configs/image-smoke.sh` | `tests/image-smoke.sh` | image repositories that commit a `tests/image-smoke.conf` |
| `configs/shell/lib.sh`, `configs/shell/harness_test.sh` | `tests/shell/lib.sh`, `tests/shell/harness_test.sh` | repositories that commit a `tests/shell/run.sh` |
| `configs/repin-sha.sh`, `configs/collect-licenses.sh` | `scripts/repin-sha.sh`, `scripts/collect-licenses.sh` | repositories with a root `Dockerfile` |

`LICENSE` is never synced. Each repository's license depends on what the repository is, so each one keeps its own.

## Renovate settings

The Renovate preset is `default.json` in [cplieger/.github](https://github.com/cplieger/.github), not in this repository. Renovate applies it to every cplieger repository through its inherited-config file there, `org-inherited-config.json`, so a repository needs no `renovate.json` of its own.

## README badges

Badges are not synced, because each badge row carries the repository's own URLs. [BADGES.md](../BADGES.md) has the badge block for each kind of cplieger repository.
