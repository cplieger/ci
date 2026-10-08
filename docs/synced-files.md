# Synced files

This page lists every file cplieger/ci copies into other cplieger repositories, where each one lands and which repositories receive it. It is for anyone who wants to copy one of these configs, or to know why a file in a cplieger repository says it is synced.

## How the sync works

`sync.yaml` runs on a push to `main` that changes a synced file, every day at 02:00 UTC, and on demand. It first runs `scripts/classify-repos.py`, which reads each cplieger repository's files and tags to decide what it receives. Then it opens one `chore(sync):` pull request per repository with the new contents and turns on auto-merge. The pull request merges once that repository's `ci / validate` check passes. Forks and archived repositories are skipped.

A repository whose default branch is `dev` gets one pull request per branch, from `repo-sync/ci/dev` into `dev` and from `repo-sync/ci/main` into `main`. Each branch receives the files its own tree calls for, so an opt-in file committed only on `dev` reaches `dev` first. Either pull request gets auto-merge only while it still targets its branch from this repository. When GitHub refuses to arm it, it is merged directly once `ci / validate` is green at its head.

A synced file is overwritten on the next sync, so change it here, never in the repository that received it. The workflow callers carry a `DO NOT EDIT` header that says so.

A repository receives the files of the cplieger/ci major that its synced workflow files pin. A repository with none of them pinned yet, such as a new one, takes the major that the incoming workflow files pin. One pinned to the current major, or receiving no workflow file, gets the files as they are on `main`. A repository whose pins mix majors, or carry a pin that names no major, is not synced.

A repository pinned to an older major gets its files from the commit that `SYNC_SOURCES` in `scripts/sync-files.py` names for that major. Its caller templates then pass only inputs its pinned workflows have. A file missing at that commit is held back. A fix to a synced file reaches older-major repositories only when `SYNC_SOURCES` moves to a commit of that major's line that carries it.

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
| `configs/renovate-two-branch.json` | `renovate.json` | repositories whose default branch is `dev`, on both branches |

`configs/prettier.json` sets `requirePragma` for `*.md` files, so Prettier formats a Markdown file only when it opens with `<!-- @format -->`. Prettier would otherwise pad every table column to the width of its widest cell, and the Markdown in these repositories keeps its tables compact.

`LICENSE` is never synced. Each repository's license depends on what the repository is, so each one keeps its own.

## Renovate settings

The Renovate preset is `default.json` in [cplieger/.github](https://github.com/cplieger/.github), not in this repository. Renovate applies it to every cplieger repository through its inherited-config file there, `org-inherited-config.json`, so a repository needs no `renovate.json` of its own. A repository whose default branch is `dev` receives one from the sync. It extends only the `two-branch` preset there, which holds the rules for `dev` and `main`. Extending `default.json` again would apply its rules twice.

## README badges

Badges are not synced, because each badge row carries the repository's own URLs. [BADGES.md](../BADGES.md) has the badge block for each kind of cplieger repository.
