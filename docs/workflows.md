# Reusable workflows

This page lists what each reusable workflow and composite action in cplieger/ci runs. It is for anyone calling one from their own repository or copying one out.

## The CI entry point

`ci.yaml` checks which files the repository has and runs one job per match. A repository with Go and a web frontend runs both jobs in parallel. The `repo` and `markdown` jobs always run. The other jobs run only when the change touches a file other than Markdown, `LICENSE` or `.gitignore`. The `validate` job collects every result, so branch protection needs one required check, `ci / validate`.

| The repository has | Job | What runs |
| --- | --- | --- |
| any files | `repo` | actionlint, zizmor, a gitleaks scan of the working tree, the comment-ratio check, the NOTICE check |
| any files | `markdown` | markdownlint-cli2, and lychee offline for internal links and heading anchors |
| `go.mod` at the root | `go` | `go-ci.yaml` |
| a `go.mod` in a subfolder, with tracked Go files | `go-nested` | `go-ci.yaml` once per module folder |
| `jsr.json` at the root | `ts` | `ts-ci.yaml` |
| a `package.json` or `jsr.json` in `static-src/`, `web/` or `internal/server/static-src/` | `web` | `ts-ci.yaml` in the first folder found, with the CSS and HTML lints on |
| a `Dockerfile` | `shell` | `shell-ci.yaml` |
| a `Dockerfile` | `docker`, `docker-arm64` | an image build on `amd64`, and on `arm64` in public repositories, with no push and no scan |
| `*.py` files | `python` | Ruff |
| shell scripts or workflow files, and no `go.mod`, `jsr.json` or `Dockerfile` | `scripts` | shellcheck, shfmt, yamllint, a TOML check, and the probe scripts present in the repository |

After the `amd64` build, the `docker` job runs two optional image tests. The shared smoke harness runs when the repository has `tests/image-smoke.conf`. A repository's own `tests/image-test.sh` runs with the image reference as its first argument, and it must be committed as executable.

## The language workflows

| Workflow | What it runs |
| --- | --- |
| `go-ci.yaml` | `go vet`, golangci-lint, `go test -race`, govulncheck, `go mod verify`, a wiregen drift check, deadcode and punused for apps, dependency review on public pull requests |
| `ts-ci.yaml` | `npm ci`, ESLint, `tsc`, knip when a knip config is committed, vitest, fast-check fuzz tests, Prettier, `package.json` and `jsr.json` version parity, a publish-surface check, dependency review |
| `ts-ci.yaml` with `web-lint: true` | the above, plus Stylelint, html-validate and an import-map coverage check |
| `shell-ci.yaml` | shellcheck, shfmt, hadolint, and `tests/shell/run.sh` when the repository has one |

`ts-ci.yaml` also runs the tests again on the oldest Node major that `package.json` `engines.node` allows. A package with no `engines.node` skips that job with a notice.

## The release workflow

`release.yaml` reads the release type from the repository root. A `Dockerfile` wins over `jsr.json`, and `jsr.json` wins over `go.mod`. It computes the next version with git-cliff from conventional commits and the repository's `cliff.toml`. A push that touches only paths such as Markdown, tests, lockfiles or `.github/` publishes nothing. An eligible Go module in a subfolder gets its own version and its own `<folder>/vX.Y.Z` tag. Its module path must start with a domain name, its folder must hold tracked Go files and sit outside `internal/`, and it must not contain or sit inside another module's folder.

On the `v3` line, the branch picks the channel. A push to `dev` publishes the dev channel, with `vX.Y.Z-dev.N` tags, `:dev` images on GitHub Container Registry, the npm `dev` dist-tag and no GitHub Release. A push to any other branch publishes the stable channel, with a `vX.Y.Z` tag, GitHub Container Registry and Docker Hub images, npm and JSR packages, and a GitHub Release. The `v2` line has no dev channel and publishes stable releases only.

`docker-release.yaml` is called by `release.yaml` and never directly. On the `v2` line, one run builds the image on native `amd64` and `arm64` runners and signs it with cosign. The same run attaches an SBOM, runs a Trivy scan, pushes the image to both registries and creates the GitHub Release. It also writes the Docker Hub overview page from the README. On the `v3` line the work splits by channel.

On the dev channel it builds the image on native `amd64` and `arm64` runners and pushes it to GitHub Container Registry. It signs the image with cosign, attaches an SBOM and runs a Trivy scan. A repository with a root `grafana-dashboard.json` also gets the dashboard pushed as an OCI artifact.

On the stable channel it re-tags the image the dev channel built for that commit as `vX.Y.Z`, `vX.Y`, `vX` and `latest`. It copies the image to Docker Hub with its signatures and creates the GitHub Release with the SBOM and the dashboard. When no dev build matches, it builds from source and says so in the run summary. It also writes the Docker Hub overview page from the README.

## The security workflows

- `codeql.yaml` runs CodeQL with the `security-extended` and `security-and-quality` query suites on public repositories. It detects the languages itself.
- `security-scan.yaml` runs Trivy filesystem, misconfiguration and image scans for `HIGH` and `CRITICAL` findings, plus a gitleaks scan of the full git history. Every one of them is advisory. The findings go to the Security tab and never fail the run.

## Composite actions

| Action | What it does |
| --- | --- |
| `actions/git-cliff-version` | installs git-cliff and outputs the next stable version, its dev and patch-floor variants, and a `release` boolean. Callable directly |
| `actions/publish-badge` | writes one shields.io endpoint JSON to the repository's `badges` branch and keeps the other badge files there |
| `actions/publish-surface` | checks that npm and JSR publish exactly the files the package's `exports` reach, and no undeclared package |
| `actions/render-hub-overview` | builds the Docker Hub overview page from the README's marked summary and `compose.yaml` |
| `actions/comment-audit` | fails when source carries more than 0.55 comment lines per code line |
| `actions/notice-audit` | fails when the root `NOTICE` departs from its three-line template, or a published package folder lacks identical `LICENSE` and `NOTICE` copies |

## Workflows for this repository only

Every other file in `.github/workflows/` runs inside cplieger/ci and is not meant to be called. These workflows sync the configs, cut and move release tags, run the daily settings audit, and run the scheduled mutation, fuzz, benchmark, link and security jobs. Others rebuild images older than seven days by default, promote a repository's `main` to a tested `dev` commit, delete aged dev images from GitHub Container Registry, and run this repository's own CI.
