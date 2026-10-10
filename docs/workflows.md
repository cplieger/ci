# Reusable workflows

This page lists what each reusable workflow and composite action in cplieger/ci runs. It is for anyone calling one from their own repository or copying one out.

## The CI entry point

`ci.yaml` checks which files the repository has and runs one job per match. A repository with Go and a web frontend runs both jobs in parallel. The `repo` and `markdown` jobs always run, and `pr-policy` runs on every pull request it applies to. The other jobs run only when the change touches a file other than Markdown, `LICENSE` or `.gitignore`. The `validate` job collects every result, so branch protection needs one required check, `ci / validate`.

| The repository has | Job | What runs |
| --- | --- | --- |
| any files | `repo` | actionlint, zizmor, a gitleaks scan of the working tree, the comment-ratio check, the NOTICE check |
| any files | `markdown` | markdownlint-cli2, and lychee offline for internal links and heading anchors |
| `go.mod` at the root | `go` | `go-ci.yaml` |
| a `go.mod` in a subfolder, with tracked Go files | `go-nested` | `go-ci.yaml` once per module folder |
| `jsr.json` at the root | `ts` | `ts-ci.yaml` |
| a `package.json` or `jsr.json` in `static-src/`, `web/` or `internal/server/static-src/` | `web` | `ts-ci.yaml` in the first folder found, with the CSS and HTML lints on |
| a `Dockerfile` | `shell` | `shell-ci.yaml` |
| Go or TypeScript that deadset detects, or a root `deadset.json` naming languages, or a `go-nested` module | `deadset` | `deadset-ci.yaml` at the repository root, and once per nested module folder |
| a `Dockerfile` | `docker`, `docker-arm64` | an image build on `amd64`, and on `arm64` in public repositories, with no push and no scan |
| `*.py` files | `python` | Ruff |
| shell scripts or workflow files, and no `go.mod`, `jsr.json` or `Dockerfile` | `scripts` | shellcheck, shfmt, yamllint, a TOML check, and the probe scripts present in the repository |
| a pull request, in a repository whose default branch is `dev` | `pr-policy` | a conventional-commit check of the title, and on a pull request into `main` a check of what its head branch may change |

After the `amd64` build, the `docker` job runs two optional image tests. The shared smoke harness runs when the repository has `tests/image-smoke.conf`. A repository's own `tests/image-test.sh` runs with the image reference as its first argument, and it must be committed as executable.

## The language workflows

| Workflow | What it runs |
| --- | --- |
| `go-ci.yaml` | `go vet`, golangci-lint, `go test -race`, govulncheck, `go mod verify`, a wiregen drift check, dependency review on public pull requests |
| `ts-ci.yaml` | `npm ci`, ESLint, `tsc`, knip for import cycles and undeclared dependencies, vitest, fast-check fuzz tests, Prettier, `package.json` and `jsr.json` version parity, a publish-surface check, dependency review |
| `ts-ci.yaml` with `web-lint: true` | the above, plus Stylelint, html-validate and an import-map coverage check |
| `shell-ci.yaml` | shellcheck, shfmt, hadolint, and `tests/shell/run.sh` when the repository has one |

`ts-ci.yaml` also runs the tests again on the oldest Node major that `package.json` `engines.node` allows. A package with no `engines.node` skips that job with a notice.

In a TypeScript package with a knip configuration, `ts-ci.yaml` also runs [knip](https://knip.dev) for four checks that are not dead code. They are import cycles, imports of packages that `package.json` does not declare, binaries that `package.json` does not declare, and the same value exported twice. Unused code is left to the dead-code check. A cycle fails only when the configuration sets `"rules": { "cycles": "error" }`.

## The dead-code check

`deadset-ci.yaml` runs [deadset](https://github.com/cplieger/deadset) at the repository root when deadset finds Go or TypeScript there, and once in each nested Go module folder. The root run covers every language deadset finds there, Go with deadset-go and TypeScript with deadset-ts, so deadset can match an edge that crosses from one language to the other. deadset-go stops at a nested module's `go.mod`, so each nested module gets its own run for its Go. deadset-ts at the root already reads every TypeScript project below it, nested modules included, so a nested run leaves TypeScript out.

A run reads its settings from `deadset.json` at its root. Every key that file sets wins, except `reporters.formats`: the workflow always asks for text and SARIF. When the file leaves `target.kind` out, it is `application` if the folder has a `Dockerfile` and `library` otherwise. deadset then reports no unused export from a library's published API, because callers outside the repository may use it.

The languages are the `analysis.languages` of `deadset.json`. When the file names none, a nested module's run takes Go alone, and the root run lets deadset detect them from file names, as its [commands page](https://github.com/cplieger/deadset/blob/HEAD/docs/commands.md#how-languages-are-detected) describes, so TypeScript test fixtures count too. A repository that holds fixtures in a language it does not ship lists its own languages. The workflow installs what each language needs, and fails before the analysis when it cannot:

- Go needs a `go.mod` at the run's root.
- TypeScript needs a committed `package-lock.json` for each npm project, outside `node_modules`, `testdata`, `vendor` and hidden folders.

Any finding at `deny` severity fails the check. So does a run that gives no answer, such as a missing or refused analyzer, and a run that ends without writing its SARIF report. To keep a symbol, put a `deadset:ignore` directive with its code and a reason on the line above it. Two repository layouts fail the check on their own:

- A Go module that reaches into an npm project's `node_modules` fails before deadset runs, because an npm package can ship Go source. Add an `ignore ./<folder>/node_modules` line to `go.mod` (Go 1.25 or later), or put a `go.mod` in the npm project to fence it off.
- A `deadset.json`, `deadset-ignore.json` or `deadset-edges.json` in an npm project folder below the root is never read, so the check fails and names it. Move it to the root and make its paths relative to the root.

Each run also writes SARIF and uploads it to code scanning, under the category `deadset`, or `deadset/<folder>` for a nested module. Each analyzer's findings stay a separate analysis inside that category. The upload needs `security-events: write`, which the synced `ci.yml` grants. It is skipped in private repositories, and a failed upload never fails the check.

In `cplieger/ci` itself, a `deadset-canary` job runs the same workflow with the pinned deadset, deadset-go and deadset-ts over four public repositories at fixed commits, and ignores their findings. A version bump that makes deadset refuse a run or give no answer fails that job before a release carries it.

## The release workflow

`release.yaml` reads the release type from the repository root. A `Dockerfile` wins over `jsr.json`, and `jsr.json` wins over `go.mod`. It computes the next version with git-cliff from conventional commits and the repository's `cliff.toml`. A push that touches only paths such as Markdown, tests, lockfiles or `.github/` publishes nothing. An eligible Go module in a subfolder gets its own version and its own `<folder>/vX.Y.Z` tag. Its module path must start with a domain name, its folder must hold tracked Go files and sit outside `internal/`, and it must not contain or sit inside another module's folder.

Before it tags a release or pushes an image, the workflow checks the root `go.mod` module path. A library's path must be `github.com/<owner>/<repo>`, with a `/vN` suffix from major version 2 on. An image repository's path must be the plain `github.com/<owner>/<repo>` at every major version, because nothing imports an app. A repository with no root `go.mod` skips the check.

The branch picks the channel. A push to `dev` publishes the dev channel, with `vX.Y.Z-dev.N` tags, `:dev` images on GitHub Container Registry, the npm `dev` dist-tag and no GitHub Release. A push to `main` publishes the stable channel, with a `vX.Y.Z` tag, GitHub Container Registry and Docker Hub images, npm and JSR packages, and a GitHub Release. A run on any other branch, a tag or a pull request fails with an error and publishes nothing.

`release.yaml` publishes only from a public, non-fork repository whose default branch is `dev`, which follows [the two-branch release model](#the-two-branch-release-model). For any other caller its first job fails with an error, before anything is built or published. The sync gives a repository with only `main` no release workflow, and such a repository publishes, if at all, from its own workflow.

`docker-release.yaml` is called by `release.yaml` and never directly. Its first job refuses the same callers, in a repair as in a release. Its work splits by channel.

On the dev channel it builds the image on native `amd64` and `arm64` runners and pushes it to GitHub Container Registry. It signs the image with cosign, attaches an SBOM and runs a Trivy scan. A repository with a root `grafana-dashboard.json` also gets the dashboard pushed as an OCI artifact.

On the stable channel it re-tags the image the dev channel built for that commit as `vX.Y.Z`, `vX.Y`, `vX` and `latest`. It copies the image to Docker Hub with its signatures and creates the GitHub Release with the SBOM and the dashboard. When no dev build matches, it builds from source and says so in the run summary. It also writes the Docker Hub overview page from the README.

A job that runs a script or action from cplieger/ci first checks out cplieger/ci at the commit you pinned. The copy goes to `.cplieger-ci` beside your checkout and keeps no token on disk. So the commit you pin decides every line of cplieger/ci code the run executes. The `pr-policy` job of `ci.yaml` works the same way.

## The two-branch release model

In a repository whose default branch is `dev`, human work merges into `dev`, and `main` takes machine updates and promotions only. A promotion moves `main` to a tested `dev` commit, and `promote.yaml`, described below, is the one way to start it.

- A `dev` build is numbered above every stable release. Its version is at least the next minor above the highest stable tag, or the next major when its changes include a breaking one. Below 1.0.0, a breaking change takes the next minor instead.
- A commit on `main` that is not a promotion publishes the next patch and never more. Its image is built from source, and its notes open with "Built from `main` for dependency and system-package updates".
- A promotion publishes the next minor, or the next major when it carries a breaking change. Below 1.0.0, a breaking promotion takes the next minor. Its image is the `dev` image its promotion commit names in a `Promoted-Digest:` trailer, after a check that the digest is in the repository's own package. Its notes open with "Promoted from" and the `dev` version.
- When another commit reaches `main` before a promotion has released, that commit builds from source and publishes the promotion's version.
- Before a promotion's first tag or registry write, the run waits for every `dev` release run that started earlier. If `dev` then holds newer work and no pre-release above the version, the run dispatches `release.yaml` on `dev` with `mode=renumber` and waits for it. A renumber run tags the existing `dev` build under a new pre-release version and builds nothing. The caller template grants `actions: write` for this dispatch.
- A stable lane is complete once its GitHub Release exists and its published artifacts read back from the registries. The run then sets a `release/complete/<tag>` commit status. Every stable run first completes each lane's highest stable tag that lacks this status, at the tag's own commit.

`scripts/render-notes.sh` writes the notes of each released lane from the lane's previous stable tag to the release commit. After the opening line come breaking changes, then Security, Added, Fixed, Performance and Changed, then a compare link to the previous version. Commits scoped `deps` or `devdeps`, which Renovate writes, stay out of these lists but still count for the version.

A collapsed Dependencies block follows. It lists what changed in `go.mod`, `package-lock.json`, `uv.lock`, Dockerfile `FROM` lines and pinned versions, and marks updates from a Renovate security pull request. An image release ends with a System packages block from the two signed SBOMs.

`promote.yaml` in this repository promotes a chosen `dev` commit to `main`, on demand only. Its inputs are `repo`, `target` and `dry_run`, and `target` defaults to the head of `dev`. It refuses the promotion unless all five checks pass:

- The commit pins no first-party dependency at a `-dev.` version.
- Each image lane still has a signed `dev` image on GitHub Container Registry, built from that commit or from an ancestor with no shipped change since.
- Every file that changed on `main` since the branches split is a dependency file or a synced file. The commit has each dependency at the same version, a newer one or none, and each synced file equal to `main`'s copy or the current one here.
- The commit's image publishes every platform the image of `main` publishes, and has no fixable `HIGH` or `CRITICAL` finding, on any platform, that the image of `main` lacks.
- At least one lane would publish.

It then writes a promotion commit whose tree is the `dev` commit's and whose parents are `main` and that commit. For an image, it next tags the promoted `dev` image `promoted-<promotion commit>` on GitHub Container Registry, and the cleanup of aged `dev` images never deletes an image with that tag. `main` moves to the promotion commit only after that tag exists and only if `main` has not moved since the checks. `dev` is never written. A dry run stops after the checks.

Each run is named `Promote <repo>`, with `(dry run)` after a dry run. Runs for one repository run one at a time, and up to 100 can wait for their turn without any being cancelled. Runs for different repositories run in parallel.

## The security workflows

- `codeql.yaml` runs CodeQL with the `security-extended` and `security-and-quality` query suites on public repositories. It detects the languages itself.
- `security-scan.yaml` runs Trivy filesystem, misconfiguration and image scans for `HIGH` and `CRITICAL` findings, plus a gitleaks scan of the full git history. Every one of them is advisory. The findings go to the Security tab and never fail the run.
- In a two-branch repository, a run of `security-scan.yaml` on `main` that is not a pull request builds nothing. It scans the published `:latest` image on every platform, the checkout's lockfiles and `go.mod` files, and each Go module with `govulncheck`. It writes the findings to a `security-main` artifact and the run summary.
- Components the image builds from source are listed as not covered, because no scanner has been shown to match their advisories. The list comes from the published image's signed SBOM, which names every CycloneDX fragment the image ships, a fragment a build script writes included. The Dockerfile's fragments, followed through its build stages, and a list of names kept per fragment path are added to it.
- The run fails when a report is missing, when the signed SBOM cannot be verified or read, or when a fragment in the image has no name any of those ways, so a partial scan never reads as clean.
- The `security-main` artifact holds one `security-main.json` record, schema 1. Its fields are `schema`, `repo`, `commit`, `image`, `findings`, `not_covered`, `complete` and `errors`.
  - `image` is null, or the image's `ref`, its `digest` and a digest per platform.
  - Each finding has an `id`, a `package`, the `installed` and `fixed` versions, a `severity`, a `class`, and the `targets`, `platforms` and `sources` it was found in. `fixed` is null when no fix exists, and `severity` is null for a govulncheck finding.
  - The `class` is `os` for an image OS package, `stdlib` for the Go standard library, `manifest` for a shipped manifest or a package inside the image, and `lockfile` for a lockfile entry.
- Trivy reports only HIGH and CRITICAL findings. govulncheck reports only the vulnerabilities whose code the module calls.

## Composite actions

| Action | What it does |
| --- | --- |
| `actions/git-cliff-version` | installs git-cliff and outputs the next stable version under the two-branch numbering, its dev variant, and a `release` boolean. Callable directly |
| `actions/publish-badge` | writes one shields.io endpoint JSON to the repository's `badges` branch and keeps the other badge files there |
| `actions/publish-surface` | checks that npm and JSR publish exactly the files the package's `exports` reach, and no undeclared package |
| `actions/render-hub-overview` | builds the Docker Hub overview page from the README's marked summary and `compose.yaml` |
| `actions/comment-audit` | fails when source carries more than 0.55 comment lines per code line |
| `actions/intake` | runs the pull-request title and `main` intake checks of the `pr-policy` job |
| `actions/notice-audit` | fails when the root `NOTICE` departs from its three-line template, or a published package folder lacks identical `LICENSE` and `NOTICE` copies |

## Workflows for this repository only

Every other file in `.github/workflows/` runs inside cplieger/ci and is not meant to be called. These workflows sync the configs, cut and move release tags, run the daily settings audit, and run the scheduled mutation, fuzz, benchmark, link and security jobs. Others rebuild images older than seven days by default, promote a `dev` commit to `main` in a two-branch repository, delete aged dev images from GitHub Container Registry, and run this repository's own CI.

In a two-branch repository, the rebuild job reads when the published `:latest` and `:dev` images were built. For each one older than the limit, it opens a pull request into `main` or `dev`. The daily security job runs each two-branch repository's security workflow on `dev` and again on `main`.

The daily settings audit holds a two-branch repository to squash merges titled by the pull request title, and to the two rulesets in `configs/rulesets/`. It also requires the synced `renovate.json` and a `release/complete/<tag>` status on each lane's highest stable tag.

On its schedule and when started by hand, the audit files its findings as issues. Each public repository it grades that has issues turned on keeps one `Repository audit findings` issue, labelled `repo-audit`. The findings of any other repository stay in the run summary. The issue lists the repository's HARD findings and warnings. It is updated in place on each run and closed once the repository audits clean.

A repository created less than 24 hours ago is graded but gets no issue yet. When a read for a repository fails, its issue is left as it is for that run. A public repository outside `SINGLE_MAIN_REPOS` whose default branch is `main` and which calls `release.yaml` is a HARD finding, because that workflow refuses it. One that publishes from its own `publish.yaml` is a warning until it is added to `SINGLE_MAIN_REPOS`.

`release-maintenance.yaml` runs every hour for two-branch repositories only. It merges a Renovate security pull request on `dev` or `main` when `ci / validate` is green and the pull request carries a patch, minor, digest or pin label.

For a fixable finding of the daily scan of `main`, it asks Renovate to run the weekly `main` group early through the Dependency Dashboard. It does so when the group updates the package or, for an image that installs no packages, the base image its final stage is built on. The dashboard lists a group's packages only when the group holds two or more, so a finding against a group with no list is reported in the issue instead. It acts only on a complete scan that started in the last 36 hours.

For an OS package of an image whose final stage installs packages, it opens a rebuild pull request into `main` instead. The rebuild refreshes those packages only when an install runs after an `ARG PKG_REFRESH` line, in the same stage or in a stage it is built from. Otherwise the finding is listed in the issue.

It also keeps one `Release blocked` issue per repository and closes it once nothing is blocked. The issue lists the Saturday update when it has not shipped by 12:00 UTC. It lists any security, expedited or rebuild pull request into `main` that has not shipped within three hours. It also lists every fixable finding with no automatic route, and a missing, older or incomplete scan. When a read fails, the issue keeps the sections that read would have checked as they were, and the issue is not closed that hour. It also stays open in an hour when a planned merge, rebuild or dashboard tick did not happen, for example because the dashboard changed after it was read.
