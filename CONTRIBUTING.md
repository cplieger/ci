# Contributing to ci

The [shared contributing rules](https://github.com/cplieger/.github/blob/main/CONTRIBUTING.md) apply here. This repository has no `cliff.toml`, and a change under `.github/workflows/` or `actions/` can release, so Releases below says when a change cuts a tag.

## Rules

- A change to `configs/`, `.github/workflow-templates/` or the root `.golangci.yaml`, `.editorconfig` or `.gitattributes` reaches every repository that receives the file as a sync pull request, which merges itself once that repository's checks pass.
- All synced files travel on one branch per repository. A change that turns one repository red therefore holds back every later synced change to it until that repository is fixed.
- Other repositories depend on the inputs, outputs and job names of the workflows and actions here. Keep them compatible within a major. A removal or rename is breaking, and each repository stays on the old major until it is repointed.
- A reusable workflow calls another one through a local `./.github/workflows/` reference, which resolves to the commit the caller pinned. A composite action is referenced as `cplieger/ci/actions/<name>@<sha>`, because a local `./actions/` reference resolves against the calling repository's checkout and fails there.
- Until a tag carries a new composite action, a step that pins it fails the whole job, even behind an `if:`. Reference it as `./actions/<name>` behind `if: github.repository == 'cplieger/ci'` until then, as the `NOTICE audit` steps in `ci.yaml` show.
- A workflow that runs only inside this repository goes into `INTERNAL_WORKFLOWS` in `self-release.yaml`, or each edit to it cuts a needless release. A workflow that `ci.yaml` or `release.yaml` calls stays off that list, or its edits never reach other repositories.
- Only `scripts/test_*.py` runs automatically, through unittest discovery in `self-ci.yaml`. A new `test-*.sh` or `test-*.py` probe runs only once you add a step that names it, in `self-ci.yaml` or in the `scripts` job of `ci.yaml`.
- A tool pinned in more than one workflow, such as shellcheck or shfmt, keeps the same version at every pin. `scripts/install-local-tools.sh` installs the first pin it finds, so a split pin makes local results differ from CI.
- Download a tool archive with `curl --retry 7 --retry-max-time 150 --retry-all-errors -o <file>`, then extract the file, as the existing steps do. Piping curl into `tar` breaks the retry, because curl cannot take back bytes `tar` already read.

## Checks

- ci-local reads the reusable workflows from the `ci/` checkout beside a repository, not from the commit that repository pins. Run it in another repository to try a workflow change there before it is released.
- Most jobs run on `ubuntu-26.04`, whose `sort` rejects an attached separator such as `-t=` and prints nothing. Write `sort -t '='` or use `awk`, because a probe that passes with GNU `sort` locally can read empty output in CI.

## Releases

- A merge to `main` cuts a tag when it changes a file under `actions/` or a workflow outside `INTERNAL_WORKFLOWS`, whatever its commit type. A `ci:` or `chore:` commit there releases a patch.
- A change to `configs/` or `.github/workflow-templates/` cuts no tag, because it reaches other repositories through sync.
