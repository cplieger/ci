# Contributing to ci

The [shared contributing rules](https://github.com/cplieger/.github/blob/main/CONTRIBUTING.md) apply here. This repository has no `cliff.toml`, and a change under `.github/workflows/` or `actions/` can release, so Releases below says when a change cuts a tag.

## Rules

- A change to `configs/`, `.github/workflow-templates/` or the root `.golangci.yaml`, `.editorconfig` or `.gitattributes` reaches every repository that receives the file as a sync pull request, which merges itself once that repository's checks pass.
- Synced files travel on one branch per repository, or per base branch where the default branch is `dev`. A change that turns that branch red holds back every later synced change on it until the repository is fixed.
- Other repositories depend on the inputs, outputs and job names of the workflows and actions here. Keep them compatible within a major. A removal or rename is breaking, and each repository stays on the old major until it is repointed.
- A reusable workflow calls another one through a local `./.github/workflows/` reference. A job that runs a script or composite action from here first checks this repository out at `job.workflow_sha` into `.cplieger-ci`. Both run the commit the caller pinned.
- Five steps pin an action as `cplieger/ci/actions/<name>@<sha> # v3`: `comment-audit`, `notice-audit`, `publish-badge`, `render-hub-overview` and `publish-surface`. They run that commit's code until they move to the `.cplieger-ci` checkout.
- When no tag carries a pinned action yet, its step fails the whole job, even behind an `if:`. In this repository those steps run `./actions/<name>` behind `if: github.repository == 'cplieger/ci'`, as the `NOTICE audit` steps in `ci.yaml` show.
- The release workflows run only in a public, non-fork repository whose default branch is `dev`. A `main`-default repository keeps its existing CI path. A CI workflow change reaches it only if sync gives it the CI templates.
- A workflow that runs only inside this repository goes into `INTERNAL_WORKFLOWS` in `self-release.yaml`, or each edit to it cuts a needless release. A workflow that `ci.yaml` or `release.yaml` calls stays off that list, or its edits never reach other repositories.
- Only `scripts/test_*.py` runs automatically, through unittest discovery in `self-ci.yaml`. A new `test-*.sh` or `test-*.py` probe runs only once you add a step that names it, in `self-ci.yaml` or in the `scripts` job of `ci.yaml`.
- A tool pinned in more than one workflow, such as shellcheck or shfmt, keeps the same version at every pin. A split pin lets one workflow pass a file that another fails.
- A `run:` block that writes a helper script writes it with a quoted heredoc (`<<'EOF'`) whose terminator sits at the block's base indent. A `printf '%s\n'` of the same text trips shellcheck SC2016, which actionlint reports as an error.
- Prove a new check on a tree with known findings, and watch it fail there before trusting its pass. A pattern that matches nothing, an empty matrix or a threshold no step reads passes while checking nothing.
- Before a new lint step lands, or an existing one reaches more files or repositories, run its exact command against the committed revision of every repository it will reach, and fix what it finds in the same batch. Otherwise it lands red for repositories whose authors never touched the files.
- Download a tool archive with `curl --retry 7 --retry-max-time 150 --retry-all-errors -o <file>`, then extract the file, as the existing steps do. Piping curl into `tar` breaks the retry, because curl cannot take back bytes `tar` already read.
- Install a tool into `$RUNNER_TEMP/bin` and append that directory to `$GITHUB_PATH`, without `sudo` or `/usr/local/bin`: on a self-hosted runner the job's user may have neither `sudo` nor write access there. `scripts/test_root_free_installs.py` fails a job in `ci.yaml`, `go-ci.yaml`, `ts-ci.yaml`, `shell-ci.yaml`, `deadset-ci.yaml` or `security-scan.yaml` that runs `sudo` or installs into `/usr/local`. It does not catch `playwright install --with-deps`, which runs `apt-get` as root. `ts-ci.yaml` runs that only on a GitHub-hosted runner, and `scripts/test_ts_ci_shards.py` checks it.
- A job the `runner` input can move uses no action whose own script needs a binary that GitHub's runner image lacks, such as `sigstore/cosign-installer`, which runs `envsubst`. `scripts/test_runner_input.py` lists those actions.
- A caller template passes only the inputs its pinned callee declares. A new input reaches the templates after the tag that adds it and the Renovate bump of their pins; `scripts/test_runner_input.py` reads each callee at the commit its template pins.
- The weekly runs take only repositories that are public, not archived and not forks, because anyone signed in to GitHub can read this repository's run logs and artifacts. A listing entry that does not state `"visibility": "public"` is left out. A private repository that wants these runs runs them in its own CI.
- A weekly job that runs another repository's code clones it anonymously and references no secret. `CI_SCHEDULE` goes only to jobs that run none, such as the listing, badge, tracker and publish jobs. Those jobs take each repository's name from their own matrix, and download only the artifacts that repository's run entries uploaded, by exact name, because the target's code wrote the artifact. A staged Stryker report drops the files' and test files' `source`. `scripts/test_schedule_token_scope.py` enforces all of it.

## Checks

- Most jobs run on `ubuntu-26.04`, whose `sort` and `realpath` differ from GNU's, so a probe that passes locally can fail in CI. `sort` rejects an attached separator such as `-t=` and prints nothing: write `sort -t '='`. `realpath -m` exits 1 on a symlink loop: test its status under `set -e`.

## Releases

- A merge to `main` cuts a tag when it changes a file under `actions/` or a workflow outside `INTERNAL_WORKFLOWS`, whatever its commit type. A `ci:` or `chore:` commit there releases a patch.
- A change to `configs/` or `.github/workflow-templates/` cuts no tag, because it reaches other repositories through sync.
