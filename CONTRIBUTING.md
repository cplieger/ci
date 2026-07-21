# Contributing to cplieger/ci

This repo is the shared CI/CD source of truth: the reusable workflows, composite
action, and canonical lint configs that every other `cplieger`
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
    `./` ref on push/PR to `main`. For this repo that dispatches the `markdown`,
    `python` (ruff), and `scripts` (actionlint, shellcheck, shfmt, yamllint,
    zizmor, TOML validation, gitleaks) jobs.
  - `move-major-tag.yaml`, `sync.yaml`, `audit.yaml`, and the scheduled
    security/fuzz/gremlins jobs.
- `.github/workflow-templates/` — thin caller workflows synced verbatim into
  consumer repos. Each carries a `DO NOT EDIT` header because it is overwritten
  on the next sync.
- `.github/sync.yml` is **not committed**: `scripts/classify-repos.py` generates
  it fresh at sync time (gitignored), so the script is the mapping's source of
  truth.
- `actions/git-cliff-version/` — composite action: installs git-cliff and
  outputs `version` + a `release` boolean. Consumed by `release.yaml`.
- `actions/publish-badge/` — composite action: publishes a shields endpoint
  JSON to the orphan `badges` branch (preserving sibling badges). Consumed by
  `coverage.yaml`, `docker-release.yaml`, and `weekly-gremlins.yaml`.
- `configs/` — canonical configs without native remote-config support
  (`eslint.config.base.mjs`, `prettier.json`, `stylelint.json`,
  `htmlvalidate.json`, `gremlins.yaml`, `ruff.toml`, `renovate.json`,
  `image-smoke.sh`, `cliff-stable.toml`, `cliff-alpha.toml` — the last two sync
  to consumers as `cliff.toml`, tiered by latest tag). Root-level
  `.golangci.yaml`, `.editorconfig`, `.gitattributes`, and `LICENSE` are synced
  too.
- The Renovate preset is **not** in this repo; it lives in `cplieger/.github`
  (`default.json`) and is extended via `{ "extends": ["github>cplieger/.github"] }`.
- `ci-local.sh` / `_ci_local.py` — the local mirror of the CI battery.
- `scripts/` — `audit.py` (cross-repo compliance), `classify-repos.py` (sync
  map generator), `sync-files.py` (the sync engine that pushes the mapped
  files into consumers as PRs), `gremlins-aggregate.py` and
  `stryker-aggregate.py` (mutation tracker issues),
  `test-cliff-bump-semantics.sh` (contract test for the git-cliff behaviors
  the release gate relies on; runs in the scripts CI job, so a git-cliff pin
  bump or cliff-config edit must keep it green), `backfill-release-notes.py`
  (dry-run-first regeneration of historical release bodies under the current
  cliff config), and `install-local-tools.sh` (installs the CI-pinned tool
  versions locally). The badge-branch writer lives with its action at
  `actions/publish-badge/publish-badge.sh`.

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
  green). It needs the `SYNC_PAT` secret (fine-grained PAT, Contents:write +
  Pull-requests:write on the targets). `sync.yaml` first regenerates
  `.github/sync.yml` by running `classify-repos.py` (the file is gitignored,
  never committed), then runs the in-house sync engine
  (`scripts/sync-files.py` — it replaced the unmaintained
  BetaHuhn/repo-file-sync-action; test locally with `--dry-run`, limit targets
  with `--only`).
- **The Renovate preset** (`default.json`) is fetched natively by Renovate from
  each consumer's one-line `extends`; no sync needed.

## Validating locally

This repo's own CI (`self-ci.yaml`) runs the meta battery on itself: markdown
lint, ruff over the Python helpers, and the `scripts` job (actionlint,
shellcheck, shfmt, yamllint, zizmor, TOML validation, gitleaks). Replay the
whole thing locally before pushing:

```bash
bash ci-local.sh   # from this repo's root
```

Or run the two most load-bearing linters directly after a workflow or
composite-action change:

```bash
actionlint
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

If you change `audit.py` or `classify-repos.py`, run them directly (both need
`gh` authenticated):

```bash
python3 scripts/audit.py
python3 scripts/classify-repos.py    # prints a regenerated sync.yml to stdout
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

- **`.github/sync.yml` is not committed.** It's generated fresh at sync time by
  `classify-repos.py` (gitignored), so a stale committed copy can't drift out of
  sync with the live repo set. To change the mapping, edit the script.
- **Don't edit synced files in a consumer repo.** Files carrying a
  `Synced from cplieger/ci … DO NOT EDIT` header (the workflow templates, the
  configs) are overwritten on the next sync. Change the canonical copy here.
- **Tool versions are Renovate-pinned in place.** Reusable workflows and the
  composite action pin tool versions as literals next to a
  `# renovate: datasource=… depName=…` comment (golangci-lint, gitleaks,
  git-cliff, actionlint, markdownlint-cli2). Let Renovate bump them; only edit
  by hand when changing the pinning itself.
- **The per-language workflows collect failures instead of failing fast.**
  `go-ci.yaml`, `ts-ci.yaml`, and `shell-ci.yaml` run every check with
  `continue-on-error: true`, append failures to `/tmp/_ci_failures`, and fail in
  a final `Check results` step. Keep that pattern when adding a step so one
  failure doesn't mask the rest.

## Cross-repo audit

`scripts/audit.py` audits every non-archived `cplieger` repo (public + private)
against the governance standard: hard failures (merge model, default branch,
branch protection with the `validate` check pinned to GitHub Actions, phantom
required contexts, review floors or locked branches, Actions tokens able to
approve PRs, CI wired to `cplieger/ci`, registry publish secrets, the deploy
webhook's presence/secret/event/TLS, …) block compliance; soft warnings (repo
features, squash-commit defaults, protection toggles, workflow-permission
defaults, stray `dependabot.yml`, Renovate preset, license/description/topics,
scanning toggles, coverage workflow presence) are advisory. Known-accepted
deviations are encoded in the script's `ACCEPTED` table so a clean fleet
reports clean.

```bash
gh auth login        # once (needs a CLASSIC PAT with repo scope)
python3 scripts/audit.py
python3 scripts/audit.py --repo <name>   # scope to one repo (repeatable)
```

`.github/workflows/audit.yaml` runs it weekly (and on demand) and writes the
table to the run summary. The `AUDIT_PAT` secret must be a classic PAT —
fine-grained PATs don't serialize the merge-model fields; the script aborts
rather than emit false negatives.

## Commits and PRs

Commits follow [Conventional Commits](https://www.conventionalcommits.org/);
git-cliff parses them for the changelog and version bump (`feat:`, `fix:`,
`sec:`, `chore(deps):` release; `chore:`, `ci:`, `docs:`, `test:` and friends
are skipped, and commits that only touch non-shipping paths — workflows,
docs, tests, lockfiles — are path-excluded from both the notes and the bump
by the consumer `cliff.toml`). Branch from `main`,
keep the change focused, and open a PR — never push to `main` directly.

## Conduct & security

By participating you agree to the
[Code of Conduct](https://github.com/cplieger/.github/blob/main/CODE_OF_CONDUCT.md).
Report security issues through the
[security policy](https://github.com/cplieger/.github/blob/main/SECURITY.md),
never in a public issue.
