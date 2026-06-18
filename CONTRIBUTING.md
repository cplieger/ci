# Contributing to cplieger/ci

This repo is the shared CI/CD source of truth: the reusable workflows, composite
action, canonical lint configs, and Renovate preset that every other `cplieger`
repo consumes instead of duplicating. Changes here ripple
outward, so the conventions below are about not breaking downstream.

## Repository layout

- `.github/workflows/` — the reusable workflows consumers call plus this repo's
  own self-CI:
  - `ci.yaml` — meta detect-and-dispatch (`on: workflow_call`). Auto-detects a
    repo's surfaces (`go.mod` / `jsr.json` / `Dockerfile` / nested web frontend)
    and fans out to the language workflows below; the `validate` job is the
    aggregate check name branch protection targets.
  - `go-ci.yaml`, `ts-ci.yaml`, `shell-ci.yaml` — the per-language reusable
    workflows.
  - `release.yaml` — unified release (git-cliff version → publish → tag →
    GitHub Release).
  - `self-ci.yaml` — this repo's _own_ CI; calls the meta `ci.yaml` via a local
    `./` ref (markdown + python + actionlint/shellcheck) on push/PR to `main`.
  - `move-major-tag.yaml`, `sync.yaml`, `audit.yaml`, and the scheduled
    security/fuzz/gremlins jobs.
- `.github/workflow-templates/` — thin caller workflows synced verbatim into
  consumer repos. Each carries a `DO NOT EDIT` header because it is overwritten
  on the next sync.
- `.github/sync.yml` — the repo↔file mapping for the sync action. **Generated**
  by `scripts/classify-repos.sh`; do not hand-edit (see gotchas).
- `actions/git-cliff-version/` — composite action: installs git-cliff and
  outputs `version` + a `release` boolean. Consumed by `release.yaml`.
- `configs/` — canonical configs without native remote-config support
  (`eslint.config.base.mjs`, `prettier.json`, `stylelint.json`,
  `htmlvalidate.json`, `gremlins.yaml`, `ruff.toml`, `cliff-stable.toml`,
  `cliff-alpha.toml`). Root-level `.golangci.yaml`, `cliff.toml`,
  `.editorconfig`, and `.gitattributes` are synced too.
- `default.json` — the Renovate preset (extended via
  `{ "extends": ["github>cplieger/ci"] }`).
- `ci-local.sh` / `_ci_local.py` — the local mirror of the CI battery.
- `scripts/` — `audit.py` (cross-repo compliance), `classify-repos.sh` (sync
  map generator), `gremlins-aggregate.py`.

## How changes reach consumer repos

There are three independent propagation paths — know which one your change
travels:

- **Reusable workflows and the composite action** are referenced by Git ref.
  Consumers pin a commit SHA with a moving-tag comment (`@<sha> # v2`) and let
  Renovate follow the tag. On a `vX.Y.Z` release tag, `move-major-tag.yaml`
  force-repoints the `vX` and `vX.Y` tags at that commit, which is what
  actually ships the change. Consumers pick it up on their next Renovate digest
  bump.
- **Lint/format configs** have no remote-config mechanism, so `sync.yaml` pushes
  them into each consumer as a PR (and enables auto-merge once that repo's CI is
  green). It needs the `SYNC_PAT` secret. `sync.yaml` first regenerates
  `.github/sync.yml` by running `classify-repos.sh`, then runs the file-sync
  action.
- **The Renovate preset** (`default.json`) is fetched natively by Renovate from
  each consumer's one-line `extends`; no sync needed.

## Validating locally

This repo's own CI is just `actionlint`, so run it before pushing any workflow
or composite-action change:

```bash
actionlint
```

Markdown (this file, the README) is linted in CI by `markdownlint-cli2`. Run it
locally with the same rule set the `markdown` job in `ci.yaml` writes inline:

```bash
markdownlint-cli2 "**/*.md" "#node_modules" "#.git"
```

To exercise a reusable workflow end-to-end against a real consumer repo, use the
local runner from that consumer's checkout — it parses the workflow and executes
each step locally, resolving the `cplieger/ci` reusable workflow from the sibling
`ci/` checkout:

```bash
bash ci-local.sh              # run from a consumer repo root
bash ci-local.sh --plan-only  # show the resolved plan, execute nothing
bash ci-local.sh --path SUBDIR
```

If you change `audit.py` or `classify-repos.sh`, run them directly (both need
`gh` authenticated):

```bash
python3 scripts/audit.py
bash scripts/classify-repos.sh    # prints a regenerated sync.yml to stdout
```

## Changing this repo affects every consumer

A breaking change to a reusable workflow, the composite action, or a synced
config lands in every consumer repo the moment the `vX` tag moves (workflows)
or the sync PR auto-merges (configs). Treat the reusable workflow inputs and the
`validate` aggregate check name as a public API:

- Keep reusable workflows backward-compatible _within a major_. A breaking
  change is a new major tag, not an in-place edit of `v2`.
- Don't rename or drop the `validate` job in `ci.yaml` — consumer branch
  protection rules target the `ci / validate` check by name.
- When adding a surface (new web-frontend path, new language), extend the
  detection arrays in `ci.yaml` centrally rather than asking consumers to
  configure anything.

## Gotchas

- **Don't hand-edit `.github/sync.yml`.** It is regenerated by
  `classify-repos.sh` (the file says so in its header); edit the script and let
  it re-emit the mapping.
- **Don't edit synced files in a consumer repo.** Files carrying a
  `Synced from cplieger/ci … DO NOT EDIT` header (the workflow templates, the
  configs) are overwritten on the next sync. Change the canonical copy here.
- **Tool versions are Renovate-pinned in place.** Reusable workflows and the
  composite action pin tool versions as literals next to a
  `# renovate: datasource=… depName=…` comment (golangci-lint, gitleaks,
  git-cliff, actionlint, markdownlint-cli2). Let Renovate bump them; only edit
  by hand when changing the pinning itself.
- **The per-language workflows collect failures instead of failing fast.**
  `go-ci.yaml` and `shell-ci.yaml` run every check with
  `continue-on-error: true`, append failures to `/tmp/_ci_failures`, and fail in
  a final `Check results` step. Keep that pattern when adding a step so one
  failure doesn't mask the rest.

## Commits and PRs

Commits follow [Conventional Commits](https://www.conventionalcommits.org/);
git-cliff parses them for the changelog and version bump (`feat:`, `fix:`,
`sec:`, `chore(deps):`; anything else lands under Changed). Branch from `main`,
keep the change focused, and open a PR — never push to `main` directly.

## Conduct & security

By participating you agree to the
[Code of Conduct](https://github.com/cplieger/.github/blob/main/CODE_OF_CONDUCT.md).
Report security issues through the
[security policy](https://github.com/cplieger/.github/blob/main/SECURITY.md),
never in a public issue.
