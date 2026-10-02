# Contributing to cplieger/ci

This repo is the shared CI/CD source of truth: the reusable workflows,
composite actions, and canonical lint configs that every other `cplieger`
repo consumes instead of duplicating. Changes here ripple
outward, so the conventions below are about not breaking downstream.

## Repository layout

- `.github/workflows/`: the reusable workflows consumers call plus this repo's
  own self-CI:
  - `ci.yaml`: meta detect-and-dispatch (`on: workflow_call`). Auto-detects a
    repo's surfaces (`go.mod` / `jsr.json` / `Dockerfile` / nested web frontend)
    and fans out to the language workflows below; the `validate` job is the
    aggregate check name branch protection targets. A repo with a `Dockerfile`
    gets a `docker` job that builds the image and runs two opt-in image tests
    against it, both blocking: the synced harness `tests/image-smoke.sh`
    (opted into by `tests/image-smoke.conf`), then a repo-owned
    `tests/image-test.sh`, executed directly with the image ref as `$1`, which
    runs even when the harness failed. The suite must be tracked executable; a
    present but non-executable one fails the job with a message naming the fix.
  - `go-ci.yaml`, `ts-ci.yaml`, `shell-ci.yaml`: the per-language reusable
    workflows.
  - `release.yaml`: unified release (channel from the branch → git-cliff
    version → publish → tag → GitHub Release on the stable channel).
  - `self-ci.yaml`: this repo's _own_ CI; calls the meta `ci.yaml` via a local
    `./` ref on push/PR to `main`. For this repo that dispatches the `markdown`,
    `python` (ruff), and `scripts` (actionlint, shellcheck, shfmt, yamllint,
    zizmor, TOML validation, gitleaks) jobs.
  - `move-major-tag.yaml`, `sync.yaml`, `audit.yaml`, and the scheduled
    security/fuzz/gremlins jobs.
- `.github/workflow-templates/`: thin caller workflows synced verbatim into
  consumer repos. Each carries a `DO NOT EDIT` header because it is overwritten
  on the next sync.
- `.github/sync.yml` is **not committed**: `scripts/classify-repos.py` generates
  it fresh at sync time (gitignored), so the script is the mapping's source of
  truth.
- `actions/git-cliff-version/`: composite action that installs git-cliff and
  runs `compute.sh`: the next stable version, its dev and patch-floor variants
  and a `release` boolean. Consumed by `release.yaml`.
- `actions/publish-badge/`: composite action that publishes a shields endpoint
  JSON to the orphan `badges` branch (preserving sibling badges). Consumed by
  `docker-release.yaml` and `weekly-gremlins.yaml`.
- `configs/`: canonical configs without native remote-config support
  (`eslint.config.base.mjs`, `prettier.json`, `stylelint.json`,
  `htmlvalidate.json`, `gremlins.yaml`, `ruff.toml`, `renovate.json`,
  `image-smoke.sh`, `cliff-stable.toml`, `cliff-alpha.toml`; the last two sync
  to consumers as `cliff.toml`, tiered by latest stable tag) and
  `configs/rulesets/` (the `dev` and `main` ruleset bodies `audit.py` grades
  two-channel repos against and `scripts/apply-rulesets.sh` applies). Root-level
  `.golangci.yaml`, `.editorconfig`, and `.gitattributes` are synced
  too. `LICENSE` is **not** synced: each repo's license depends on what the
  repo is, so it is repo-owned.
- The Renovate preset is **not** in this repo; it lives in `cplieger/.github`
  (`default.json`) and is extended via `{ "extends": ["github>cplieger/.github"] }`.
- `ci-local.sh` / `_ci_local.py`: the local mirror of the CI battery.
- `scripts/`: `audit.py` (cross-repo compliance), `classify-repos.py` (sync
  map generator), `sync-files.py` (the sync engine that pushes the mapped
  files into consumers as PRs), `release_channels.py` (the tag shapes and repo
  tables the two-channel scripts share), `promote.py` (the checks behind
  `promote.yaml`, which fast-forwards a repo's `main` to a `dev` commit: the
  target is on `dev`'s first-parent history with `main` an ancestor, no
  first-party dependency is pinned at a `-dev.` version, the dev release run
  at the target succeeded, and a deployed image repo carries a `homelab/soak`
  success on the target or on the built ancestor it inherits; the scheduled
  run promotes only a delta merged entirely from `renovate/`, `repo-sync/` or
  `rebuild/` pull requests whose newest commit is a day old, and a
  `skip_soak` reason is recorded as a `promotion/soak-override` commit status
  that the stable release notes repeat),
  `ghcr_retention.py` (deletes aged dev-channel GHCR versions for
  `ghcr-retention.yaml`; a deleted version loses its `sha-<commit>` tag too, so
  a promotion of a commit older than the retention window builds from source
  and the release run's summary says so), `apply-rulesets.sh` (creates or
  updates one repo's `dev` and `main` rulesets from `configs/rulesets/`), their
  `test_*.py` suites plus `test_workflow_shell.py` (every bash `run:` block of
  the release-channel workflows opens with `set -euo pipefail`; run them all
  with `python3 -m unittest discover -s scripts -p 'test_*.py'`),
  `tracker_issue.py` (the one issue transport
  every scheduled writer calls: it owns the issues-disabled guard, label
  creation and the fail-closed API handling), `trackerlib.py` (the tracker
  body skeleton: sentinel blocks, the rolling history table, notes carry-over)
  with `gremlins-aggregate.py`, `stryker-aggregate.py`, `bench-aggregate.py`
  and `links-body.py` rendering the per-tracker bodies on it,
  `test-tracker-issue.py` and `test-tracker-body.py` (their tests; the golden
  bodies under `scripts/testdata/tracker/` pin every rendered issue byte for
  byte, so regenerate them only when a body change is intended),
  `test-cliff-bump-semantics.sh` (contract test for the git-cliff behaviors
  the release gate relies on; runs in the scripts CI job, so a git-cliff pin
  bump or cliff-config edit must keep it green), `test-lane-semantics.sh` (the
  sibling shell-contract test for the nested-module lane discovery and
  classification logic, same scripts-job opt-in), `test-docker-release.sh`
  (the same shape for `docker-release.yaml`'s promotion path: the channel tag,
  the digest walk and the `BUILD_VERSION` stamp, executed out of the workflow
  file against a stub registry), `test-image-test-slot.sh` (the same shape
  for the meta `ci.yaml` docker job's two image-test steps, executed in
  fixture checkouts), `backfill-release-notes.py`
  (dry-run-first regeneration of historical release bodies under the current
  cliff config), and `install-local-tools.sh` (installs the CI-pinned tool
  versions locally). The badge-branch writer lives with its action at
  `actions/publish-badge/publish-badge.sh`.

## How changes reach consumer repos

Your change travels one of three independent propagation paths:

- **Reusable workflows and the composite actions** are referenced by Git ref.
  Consumers pin a commit SHA with a moving-tag comment (`@<sha> # v2`) and let
  Renovate follow the tag. On a `vX.Y.Z` release tag, `move-major-tag.yaml`
  force-repoints the `vX` and `vX.Y` tags at that commit, which is what
  actually ships the change. Consumers pick it up on their next Renovate digest
  bump.
- **Lint/format configs** have no remote-config mechanism, so `sync.yaml` pushes
  them into each consumer as a PR (and enables auto-merge once that repo's CI is
  green). It needs the `SYNC_PAT` secret (fine-grained PAT, Contents:write +
  Pull-requests:write on the targets). `sync.yaml` first regenerates
  `.github/sync.yml` by running `classify-repos.py`, then runs the in-house
  sync engine (`scripts/sync-files.py`; test locally with
  `--dry-run`, limit targets with `--only`). Forks are skipped: the generator
  leaves them out of the manifest, and the engine checks again before it writes.
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
local runner from that consumer's checkout: it parses the workflow and executes
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

## The shell probes and what each one pins

Five probes in `scripts/` execute shell the release pipeline or the docker job
embeds or depends on; each runs in the `scripts` CI job when its file is
present, so a change to the pinned behaviour cannot merge unnoticed. Their
headers name the subject; the cases are here. `test-lane-semantics.sh`
extracts the nested-module lane discovery and classification shell out of
`release.yaml` and the meta `ci.yaml` and pins eligibility, changed-path
classification (a lane with a tag on the channel is measured from that tag, so
a later root or docs commit does not republish it, and its anchor is the one
`compute.sh` computes), module-path verification, the version guards and the
regex escaping; the other four follow.

`test-cliff-bump-semantics.sh` (`CLIFF_BIN=/path/to/git-cliff` skips the
download) pins git-cliff behaviours that upstream does not document and that
sit in a known-buggy area (git-cliff issues #816 and #1570), as states A to R:

1. `exclude_paths` glob semantics: bare patterns are root-anchored, `**/`
   matches at any depth, and a commit touching both excluded and shipped paths
   still counts.
2. Version-base anchoring: `--unreleased --bumped-version` returns the latest
   tag when the unreleased set is fully excluded, anchors on the latest tag
   even when that tag's own window is fully filtered, and bumps past such tags
   without colliding with an existing version.
3. Bump levels hold through the filter (`fix` gives a patch, `feat` a minor).
4. At a checkout behind a newer tag, cliff anchors on the newest repo tag, not
   on `describe`'s reachable one; the tag-create guard turns that into a loud
   failure.
5. A repo with no tags falls back to `[bump].initial_tag`.
6. Section ordering: the `<!-- N -->` sort prefixes render Added, Fixed,
   Security, Dependencies in that order with no comment residue.
7. Tag-pattern anchoring: a prefixed component tag (`yamlenv/v9.9.9`) is
   invisible to the root version base.
8. Nested-module lanes (states H to L): lane commits never bump the root, the
   explicit `--tag-pattern` defends against a stale unanchored consumer config,
   a tagless repo with lanes falls back to `initial_tag`, a lane computes from
   its own `<dir>/vX.Y.Z` universe with `GIT_CLIFF__BUMP__INITIAL_TAG` for its
   first release, notes are cross-lane clean both ways, and finalize-mode
   `--current` rendering at a tagged HEAD stays lane-scoped on the lane side
   and the root side. The lane discovery and classification shell is pinned by
   `test-lane-semantics.sh`.
9. The two release channels (states M to R), through the action's own
   `compute.sh`: a `-dev.N` tag is invisible to the stable base, the dev
   counter continues from the existing `<base>-dev.*` tags, a stable base equal
   to the latest tag gets a patch floor, the anchor commit follows the
   channel's tag universe, a lane computes its own dev version, and an `rc` or
   `beta` tag at HEAD is outside both channels' tag universes (the latest
   stable tag, the anchor and the release decision all ignore it).

`test-verify-publish.sh` extracts release.yaml's `Verify published artifacts`
step and pins: the Go-proxy classification (an `unknown revision` negative
cache warns and keeps the release green, every other refusal stays red), the
six-tries retry loop, the npm and JSR probes with the exact URLs each receives,
nested-lane discovery through `git tag --points-at HEAD`, that a warning never
masks a real failure in the same run, and the channel rules (a dev build probes
npm at its dev version and never JSR; a lane probes only its own channel's tag
shape).

`test-docker-release.sh` extracts docker-release.yaml's `Derive version tags`,
`Resolve promoted digest` and `Resolve build args` and release.yaml's `Detect
changed paths`, `Select version` and `Read promotion record`, and pins: the
channel tag and publish decision on both channels; that the stable channel
promotes this commit's own dev digest, inherits an ancestor's only when every
first-parent commit since changed excluded paths (a shipped change later
reverted, and a commit changing no file, both build from source), and builds
from source when no dev build or no exclusion list exists; the release
decision on a Go root with a TS subpackage (a change under the subpackage
alone is not a root change, releases on both channels through the root tag
job the subpackage job waits on, and never rebuilds the image; a commit
already tagged on its channel releases nothing); that `BUILD_VERSION` is
stamped with the channel's own tag; the soak-override handoff into every
stable notes step; and the `release/tag/<tag>` receipt every dev tag step
(docker, go, ts, lane) records for the audit, executed against a stub `gh`: a
failed receipt POST deletes the tag the attempt created so a rerun makes both,
and a tag that predated the attempt is never deleted.

`test-image-test-slot.sh` extracts the docker job's `Image smoke test` and
`Image test suite` steps from the meta `ci.yaml`, evaluates their `if:`
expressions with `_ci_local.py`'s evaluator and runs their bodies under
`bash -e` in fixture checkouts. It pins: each step runs only when its file is
present and neither runs `continue-on-error`; the build (step id `build`)
loads `ci-smoke:latest` and the harness then the suite follow it in that
order; the suite's gate is `!cancelled()`, a successful build and the file, so
a red harness does not skip it while a failed or skipped build does; a suite
exiting 0 passes and receives exactly `ci-smoke:latest` as its one argument; a
non-zero exit fails the step with that status; a mode 644 suite fails with the
message naming `git update-index --chmod=+x` and never runs; the shebang picks
the interpreter (a bash-only suite and a python3 suite both run, and a missing
interpreter fails); the harness runs under `sh` at mode 644; the job's
15-minute budget; and that `docker-arm64` runs no image test. The evaluator
models a status function the way GitHub does: `always()` or `cancelled()` in
an `if:` lets a step run after a failed one, and `steps.<id>.outcome` reads
the recorded result.

## Changing this repo affects every consumer

A breaking change to a reusable workflow, a composite action, or a synced
config lands in every consumer repo the moment the `vX` tag moves (workflows)
or the sync PR auto-merges (configs). Treat the reusable workflow inputs and the
`validate` aggregate check name as a public API:

- Keep reusable workflows backward-compatible _within a major_. A breaking
  change is a new major tag, not an in-place edit of `v2`.
- Don't rename or drop the `validate` job in `ci.yaml`: consumer branch
  protection rules target the `ci / validate` check by name.
- When adding a surface (new web-frontend path, new language), extend the
  detection arrays in `ci.yaml` centrally rather than asking consumers to
  configure anything.

## Gotchas

- **`.github/sync.yml` is not committed.** `classify-repos.py` regenerates it
  fresh at sync time (gitignored). To change the mapping, edit the script.
- **Don't edit synced files in a consumer repo.** Files carrying a
  `Synced from cplieger/ci … DO NOT EDIT` header (the workflow templates, the
  configs) are overwritten on the next sync. Change the canonical copy here.
- **Tool versions are Renovate-pinned in place.** Reusable workflows and the
  composite actions pin tool versions as literals next to a
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
against the governance standard. Hard failures (merge model, branch protection
or the `dev` and `main` rulesets with the `validate` check, phantom required
contexts, Actions tokens able to approve PRs, CI wiring, publish secrets, the
deploy webhook, …) block
compliance; soft warnings (repo features, protection toggles, Renovate preset,
license/description/topics, scanning toggles, the public docs standard, the
dependency-graph "Used by" package, …) are advisory. A repo whose default
branch is `dev` is graded on the two-channel model instead: the committed
ruleset bodies, no classic protection, a `registry_package` deploy hook when
the homelab deploys it, and the pipeline's receipt on the newest version tags
of every lane (a pipeline-authored Release on a stable tag, a `release/tag/<tag>`
commit status on a dev tag; a tag whose commit is younger than two hours is
not graded, and a repo with a workflow run live or ended within those two
hours has no version tag graded that run, because the pipeline creates the
tag before its receipt). A public repo that still releases from a `main`
default branch, and is not listed in `SINGLE_MAIN_REPOS` in
`scripts/release_channels.py`, gets a warning naming it: a new repo passes
through that state while it is bootstrapped, and a repo the cutover missed
would otherwise stay there unnoticed. The script's checks are the
authoritative list.
Known-accepted deviations are encoded in its `ACCEPTED` table so a compliant
repo set reports clean. Exit codes: 0 compliant, 1 at least one hard failure,
2 usage or infrastructure (an under-scoped token, or API errors that kept a
check from running; those are reported as `[error]` lines, never as findings).

```bash
gh auth login        # once (needs a CLASSIC PAT with repo scope)
python3 scripts/audit.py
python3 scripts/audit.py --repo <name>   # scope to one repo (repeatable)
```

`.github/workflows/audit.yaml` runs it daily (and on demand), writes the
table to the run summary, and opens a `weekly-ci-failure` tracker issue when a
scheduled run fails. The `AUDIT_PAT` secret must be a classic PAT:
fine-grained PATs don't serialize the merge-model fields; the script aborts
rather than emit false negatives.

## Commits and PRs

Commits follow [Conventional Commits](https://www.conventionalcommits.org/);
git-cliff parses them for the changelog and version bump (`feat:`, `fix:`,
`sec:`, `chore(deps):` release; `chore:`, `ci:`, `docs:`, `test:` and friends
are skipped, and commits that only touch non-shipping paths (workflows,
docs, tests, lockfiles) are path-excluded from both the notes and the bump
by the consumer `cliff.toml`). Branch from `main`,
keep the change focused, and open a PR; never push to `main` directly.

## Conduct & security

By participating you agree to the
[Code of Conduct](https://github.com/cplieger/.github/blob/main/CODE_OF_CONDUCT.md).
Report security issues through the
[security policy](https://github.com/cplieger/.github/blob/main/SECURITY.md),
never in a public issue.
