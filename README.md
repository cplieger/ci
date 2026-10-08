# cplieger/ci

Reusable GitHub Actions workflows that lint, test, sign and release every cplieger repository. They are licensed under Apache-2.0, and any public repository can call them. The lint and versioning jobs read config files that a sync job copies only into cplieger repositories, so another repository copies those files first.

## What is here

- One CI entry point, `ci.yaml`, that looks at what a repository contains and runs the matching Go, TypeScript, shell, Docker, Python and Markdown checks. Every result feeds one required check, `ci / validate`.
- One release workflow, `release.yaml`, that computes the next version from conventional commits with git-cliff. It publishes a Docker image for `amd64` and `arm64` signed with cosign, npm and JSR packages, or a Go module tag.
- Security scans with CodeQL, Trivy and gitleaks. Trivy and the full-history gitleaks scan report to the Security tab and never block a merge.
- Lint, format and changelog configs for golangci-lint, ESLint, Prettier, Stylelint, html-validate, Ruff and git-cliff. A sync job copies them into each cplieger repository as pull requests.
- Seven composite actions, and `ci-local.sh`, which replays the CI checks on your machine.

[Reusable workflows](docs/workflows.md) lists what each workflow runs. [Synced files](docs/synced-files.md) lists every config and which repositories receive it.

## How a repository calls it

A repository calls the CI entry point from a short workflow file:

```yaml
# .github/workflows/ci.yaml
jobs:
  ci:
    uses: cplieger/ci/.github/workflows/ci.yaml@<40-hex-sha> # v3
```

Pin every reference to a full commit SHA with the tag in a comment, as above. Renovate reads the comment and updates the SHA when the tag moves. Never pin to a branch. To find the commit a tag points at, run `git ls-remote https://github.com/cplieger/ci refs/tags/v3`.

Neither entry point takes inputs. `ci.yaml` picks its jobs from the files it finds, and [Reusable workflows](docs/workflows.md) lists which file starts which job. The release workflow picks the release type from the files at the repository root. A `Dockerfile` publishes an image, a `jsr.json` publishes to npm and JSR, and a `go.mod` gets a Go tag. When more than one is present, a `Dockerfile` wins over `jsr.json`, and `jsr.json` wins over `go.mod`. The calling job grants the permissions and passes the two Docker Hub secrets:

```yaml
# .github/workflows/release.yaml
jobs:
  release:
    permissions:
      contents: write
      statuses: write
      packages: write
      id-token: write
      attestations: write
      security-events: write
    uses: cplieger/ci/.github/workflows/release.yaml@<40-hex-sha> # v3
    secrets:
      DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}
      DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}
```

An image release pushes to GitHub Container Registry and to Docker Hub under the `DOCKERHUB_USERNAME` account, so an image repository needs both secrets. A Go or TypeScript repository can leave them unset. npm and JSR publishing uses OIDC trusted publishing, so no registry token is needed once the package is linked to its repository on npmjs.com and jsr.io.

## Versions and compatibility

Each release gets a `vX.Y.Z` tag, and the `vX` and `vX.Y` tags move to it. A breaking change starts a new major tag, and callers stay on their major tag until they change the pin. Pin `v3`, the line every cplieger repository uses.

The `v3` line adds a dev channel to the release workflow. On `v3`, a push to a `dev` branch publishes pre-release versions with no GitHub Release. A public, non-fork repository whose default branch is `dev` also gets the [two-branch release model](docs/workflows.md#the-two-branch-release-model), where `main` publishes patches and a promotion from `dev`, started by hand, publishes the next minor or major. Merge such a repository's pull requests by squash, so each pull request title becomes one line in its release notes.

Both `v2` and `v3` get security fixes, and `v1` gets none, as the [security policy](SECURITY.md) states.

## Using it outside the cplieger repositories

GitHub lets any public repository call a reusable workflow stored in a public repository, and the runner minutes are billed to the caller. These workflows assume more than the call, though:

- Versioning reads a `cliff.toml`, and the lint jobs read the synced configs. Copy them from `configs/` and the repository root.
- The sync job, the daily settings audit and the scheduled mutation, fuzz and benchmark runs cover cplieger repositories only.
- The release workflow sets the image license label and the SBOM source for the cplieger repositories it names. Any other repository gets the defaults, which are both registries, `amd64` and `arm64` images, and its own license in the label.

Copying a workflow and adapting it is the other way to reuse one. To build your own, GitHub's [reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows) are the built-in way. If you only need to keep files in step across your repositories, [repo-file-sync-action](https://github.com/BetaHuhn/repo-file-sync-action) opens a pull request in each target repository from a `sync.yml` list.

## Running the checks locally

- `bash ci-local.sh`, from the root of a repository that calls `ci.yaml`, replays its CI checks. It reads the workflows from a `ci/` checkout beside the repository when one exists, and otherwise fetches them at the pinned commit with `gh`. `--path SUBDIR` limits the run to one folder, and `--plan-only` prints the plan without running it. Its summary lists every check it could not run on your machine, so a local pass with such checks is not a full CI pass.
- `scripts/install-local-tools.sh` installs the tool versions CI pins, so local results match CI.

## Documentation

- [Reusable workflows](docs/workflows.md) says what each workflow and composite action runs, for anyone calling or copying one.
- [Synced files](docs/synced-files.md) lists every synced config, where it lands and how Renovate gets its settings.
- [README badges](BADGES.md) has the badge block each kind of cplieger repository carries.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This project is built with care and follows security best practices, but it is intended for personal / self-hosted use. No guarantees of fitness for production environments. Use at your own risk.

This project was built with AI-assisted tooling using [Claude](https://claude.com), [GPT](https://openai.com), and [Kiro](https://kiro.dev). The human maintainer defines architecture, supervises implementation, and makes all final decisions.

## License

Apache-2.0. See [LICENSE](LICENSE).
