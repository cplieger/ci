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
| a `package.json` or `jsr.json` in `static-src/`, `web/`, `internal/server/static-src/` or `internal/webui/static-src/` | `web` | `ts-ci.yaml` in the first folder found, with the CSS and HTML lints on |
| a `Dockerfile`, or a root `go.mod` or `jsr.json` and a tracked `.sh` file outside `node_modules`, `vendor`, `testdata` and `dist` | `shell` | `shell-ci.yaml`, with hadolint only when there is a `Dockerfile` |
| Go or TypeScript that deadset detects, or a root `deadset.json` naming languages, or a `go-nested` module | `deadset` | `deadset-ci.yaml` at the repository root, and once per nested module folder |
| a `Dockerfile` | `docker`, `docker-arm64` | an image build on `amd64`, and on `arm64` in public repositories, with no push and no scan |
| `*.py` files | `python` | Ruff |
| shell scripts or workflow files, and no `go.mod`, `jsr.json` or `Dockerfile` | `scripts` | shellcheck, shfmt, `tests/shell/run.sh` when the repository has one, yamllint, a TOML check, and the probe scripts present in the repository |
| a pull request, in a repository whose default branch is `dev` | `pr-policy` | a conventional-commit check of the title, and on a pull request into `main` a check of what its head branch may change |

The `shell` and `scripts` jobs never both run, so no shell script is checked twice.

After the `amd64` build, the `docker` job runs two optional image tests. The shared smoke harness runs when the repository has `tests/image-smoke.conf`. A repository's own `tests/image-test.sh` runs with the image reference as its first argument, and it must be committed as executable. The release workflow runs both again on every image it builds, as [the release workflow](#the-release-workflow) describes.

## The language workflows

| Workflow | What it runs |
| --- | --- |
| `go-ci.yaml` | `go vet`, golangci-lint, `go test -race`, govulncheck, `go mod verify`, a wiregen drift check, dependency review on public pull requests |
| `ts-ci.yaml` | `npm ci`, ESLint, `tsc`, knip for import cycles and undeclared dependencies, vitest, fast-check fuzz tests, Prettier, `package.json` and `jsr.json` version parity, a publish-surface check, dependency review |
| `ts-ci.yaml` with `web-lint: true` | the above, plus Stylelint, html-validate and an import-map coverage check |
| `shell-ci.yaml` | shellcheck, shfmt, hadolint on the `dockerfile` input unless it is empty, and `tests/shell/run.sh` when the repository has one |

With `web-lint: true`, a package that declares a `lint:web` script in `package.json` runs that script in place of Stylelint and html-validate, so a local `npm run lint:web` is the same check. The script must lint every tracked CSS file with Stylelint and the synced `.stylelintrc.json`, lint every tracked HTML file with html-validate, and exit non-zero when either fails.

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

A dev build is numbered as the next promotion, so a library's dev run whose code is the same as its highest stable release publishes nothing. Otherwise that pre-release would rank above the stable release and move every consumer's `dev` onto it. This covers a Go or TypeScript root together with the TypeScript subpackages that share its version, and each nested Go module. A library whose dev pre-releases already rank above its stable release still builds, because only a newer pre-release brings that stable release's code to the consumers on them. An image repository still builds, because a promotion re-tags that build.

`release.yaml` publishes only from a public, non-fork repository whose default branch is `dev`, which follows [the two-branch release model](#the-two-branch-release-model). For any other caller its first job fails with an error, before anything is built or published. The sync gives a repository with only `main` no release workflow, and such a repository publishes, if at all, from its own workflow.

`docker-release.yaml` is called by `release.yaml` and never directly. Its first job refuses the same callers, in a repair as in a release. Its work splits by channel.

On the dev channel it builds the image on native `amd64` and `arm64` runners and pushes it to GitHub Container Registry. It signs the image with cosign, attaches an SBOM and runs a Trivy scan. A repository with a root `grafana-dashboard.json` also gets the dashboard pushed as an OCI artifact.

On the stable channel it re-tags the image the dev channel built for that commit as `vX.Y.Z`, `vX.Y`, `vX` and `latest`. It copies the image to Docker Hub with its signatures and creates the GitHub Release with its assets, described below. When it promotes a build, the Release's SBOM is the one that build signed into the image, read only once its signature checks out. When no dev build matches, it builds from source and says so in the run summary. It also writes the Docker Hub overview page from the README.

Every image it builds, on either channel, then runs the `docker` job's two image tests in an `image-test` job, against the `amd64` digest the build pushed and before anything is tagged. A failure, or a run cancelled before the tests pass, leaves that digest without a tag. A rebuild-only range relies on this: its pull request changes no file, so the `docker` job skipped it, and the base packages it refreshes are tested only here. The job has read access to packages only, so the repository's test scripts never run beside the build's write token.

A stable image release carries these GitHub Release assets:

- `sbom.spdx.json`, the image's SBOM;
- `grafana-dashboard.json`, when the release commit holds one at the repository root;
- `openapi.json`, the release commit's `docs/openapi.json`, when it holds one.

The SBOM, the dashboard and the API document each have a cosign bundle, `<file>.sigstore.json`, signed by `docker-release.yaml`, and the dashboard and the API document also come with a `.sha256` checksum. So you can verify an asset without pulling the image. The run reads the Release back and fails if an asset is missing. A repository with neither file gets the SBOM alone. When a stable run completes an earlier stable tag whose Release is missing, it creates that Release with the same assets: the SBOM attested to the tag's published image, and the files from the tag's own commit. It publishes nothing if one cannot be made. Before creating either Release, `scripts/release-assets.sh cleanup-drafts` deletes any draft Release of its tag, the kind an asset upload that died leaves behind.

A job that runs a script or action from cplieger/ci first checks out cplieger/ci at the commit you pinned. The copy goes to `.cplieger-ci` beside your checkout and keeps no token on disk. So the commit you pin decides every line of cplieger/ci code the run executes. The `pr-policy` job of `ci.yaml` works the same way.

## The two-branch release model

In a repository whose default branch is `dev`, human work merges into `dev`, and `main` takes machine updates and promotions only. A promotion moves `main` to a tested `dev` commit, and `promote.yaml`, described below, is the one way to start it.

- A `dev` build is numbered above every stable release. Its version is at least the next minor above the highest stable tag, or the next major when its changes include a breaking one. Below 1.0.0, a breaking change takes the next minor instead.
- A commit on `main` that is not a promotion publishes the next patch and never more. Its image is built from source, and its notes open with "Built from `main` for dependency and system-package updates".
- A range whose only reason to release is a rebuild request from the rebuild job builds the `amd64` image first, without pushing it. When its packages equal those of the release it follows (the previous stable release on `main`, the newest `-dev.N` build on `dev`), the run publishes nothing and says so in a notice and its summary. Both images are scanned the same way, and the earlier one is first checked against its release's attestation. If that check, either scan or the comparison fails, the rebuild publishes.
- A repository's root and each nested Go module are separate lanes, each with its own versions. The release run at a promotion publishes a lane only when a file the lane ships changed since the lane's last stable tag. For an image, an empty rebuild commit merged into `main` itself since then also counts. A lane with no such change gets no tag, no Release and no stable registry tag.
- A lane whose shipped files the promotion itself changes publishes the next minor, or the next major when it carries a breaking change. Below 1.0.0, a breaking promotion takes the next minor. Its image is the `dev` image its promotion commit names in a `Promoted-Digest:` trailer, after a check that the digest is in the repository's own package. Its notes open with "Promoted from" and the `dev` version. A lane whose only unreleased shipped change came from a `main` commit publishes that commit's patch.
- When another commit reaches `main` before a promotion has released, that commit builds from source and publishes the promotion's version.
- Before a promotion's first tag or registry write, the run waits for every `dev` release run that started earlier. If `dev` then holds newer work and no pre-release above the version, the run dispatches `release.yaml` on `dev` with `mode=renumber` and waits for it. A renumber run tags the existing `dev` build under a new pre-release version and builds nothing. The caller template grants `actions: write` for this dispatch.
- A stable lane is complete once its GitHub Release exists and its published artifacts read back from the registries. npm scans each new version for malware before it serves it. The read-back therefore waits up to 20 minutes for npm and up to 10 minutes for every other registry. The run then sets a `release/complete/<tag>` commit status. Every stable run first completes each lane's highest stable tag that lacks this status, at the tag's own commit.

`scripts/render-notes.sh` writes the notes of each released lane from the lane's previous stable tag to the release commit. After the opening line come breaking changes, then Security, Added, Fixed, Performance and Changed, then a compare link to the previous version. Commits scoped `deps` or `devdeps`, which Renovate writes, stay out of these lists but still count for the version.

A collapsed Dependencies block follows. It lists what changed in `go.mod`, `package-lock.json`, `uv.lock`, Dockerfile `FROM` lines and pinned versions, and marks updates from a Renovate security pull request. An image release ends with a System packages block from the two signed SBOMs.

`promote.yaml` in this repository promotes a chosen `dev` commit to `main`, on demand only. Its inputs are `repo`, `target` and `dry_run`, and `target` defaults to the head of `dev`. It refuses the promotion unless all five checks pass:

- The commit pins no first-party dependency at a `-dev.` version.
- Each image lane still has a signed `dev` image on GitHub Container Registry, built from that commit or from an ancestor with no shipped change since.
- Every file that changed on `main` since the branches split is a dependency file or a synced file. The commit has each dependency at the same version, a newer one or none, and each synced file equal to `main`'s copy or the current one here.
- The commit's image publishes every platform the image of `main` publishes, and has no fixable `HIGH` or `CRITICAL` finding, on any platform, that the image of `main` lacks.
- The commit's tree differs from `main`'s.

A promotion that changes no path any lane ships still moves `main`. Its preview names the version each lane will publish, or says the lane has no release. The preview starts once every release run of `main`'s head has finished, so it counts the tags those runs create. The promotion waits up to 45 minutes for them, then stops and asks for a rerun. On `main`, an empty rebuild commit counts as a change only when it was merged into `main` itself, not when a promotion brings in one from `dev`.

`promote.yaml` then writes a promotion commit whose tree is the `dev` commit's and whose parents are `main` and that commit. For an image, it next tags the promoted `dev` image `promoted-<promotion commit>` on GitHub Container Registry, even when no lane will publish, and the cleanup of aged `dev` images never deletes an image with that tag. `main` moves to the promotion commit only after that tag exists and only if `main` has not moved since the checks. `dev` is never written. A dry run stops after the checks.

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
| `actions/dashboard-check` | checks a Grafana dashboard file against the schema v2 dashboard format and layout, reference and description rules. A classic dashboard passes with a notice |
| `actions/notice-audit` | fails when the root `NOTICE` departs from its three-line template, or a published package folder lacks identical `LICENSE` and `NOTICE` copies |

## Workflows for this repository only

Every other file in `.github/workflows/` runs inside cplieger/ci and is not meant to be called. These workflows sync the configs, cut and move release tags, run the daily settings audit, and run the scheduled mutation, fuzz, benchmark, link and security jobs. Others rebuild images older than seven days by default, promote a `dev` commit to `main` in a two-branch repository, delete aged dev images from GitHub Container Registry, and run this repository's own CI.

In a two-branch repository, the rebuild job reads when the published `:latest` and `:dev` images were built. For each one older than the limit, it opens a pull request into `main` or `dev`, unless a rebuild request merged into that branch within the limit. A rebuild that published nothing is therefore not requested again the next day. The daily security job runs each two-branch repository's security workflow on `dev` and again on `main`.

`renovate-heartbeat.yaml` runs daily at 07:41 UTC, and on demand with an `hours` input. It asks GitHub search for pull requests that the resident Renovate opened, from a `renovate/` branch as the `tribble-trouble` GitHub App, in any `cplieger` repository in the last 48 hours. The sync and rebuild pull requests that this repository's workflows open as the same App do not count. When it finds none, it opens or updates one `Renovate stopped opening pull requests` issue in docker-renovate-scheduler, labelled `renovate-heartbeat` and assigned to the owner. The first run that finds a pull request closes the issue. A search that fails, or that GitHub marks incomplete and that finds nothing, fails the run instead of deciding. The check sees the deployment stop for any reason, but not a fault that only another authentication mode meets, which the image smoke test covers.

The daily settings audit holds a two-branch repository to squash merges titled by the pull request title, and to the two rulesets in `configs/rulesets/`. It also requires the synced `renovate.json` and a `release/complete/<tag>` status on each lane's highest stable tag.

On its schedule and when started by hand, the audit files its findings as issues. Each public repository it grades that has issues turned on keeps one `Repository audit findings` issue, labelled `repo-audit`. The findings of any other repository stay in the run summary. The issue lists the repository's HARD findings and warnings. It is updated in place on each run and closed once the repository audits clean.

A repository created less than 24 hours ago is graded but gets no issue yet. When a read for a repository fails, its issue is left as it is for that run. A public repository outside `SINGLE_MAIN_REPOS` whose default branch is `main` and which calls `release.yaml` is a HARD finding, because that workflow refuses it. One that publishes from its own `publish.yaml` is a warning until it is added to `SINGLE_MAIN_REPOS`.

`community-data-refresh.yaml` runs on the 3rd of each month at 08:17 UTC, and on demand. It opens the pull requests that refresh the community data some repositories ship, such as langtag's CLDR tables. The repositories, their base branches and the paths each may change are listed in `REPOS` in `scripts/community_data_refresh.py`. A run started by hand can name repositories in `only`.

Each listed repository runs its own refresh in its own `community-data-refresh.yaml`, on its base branch, on the 3rd of each month at 05:17 UTC. That workflow holds no secret and uploads a `community-data-refresh` artifact, the proposal:

- `proposal.json` holds `{"changes": [...]}`, the paths the refresh changed, which is empty when there is nothing to refresh.
- `files/<path>` holds the new content of each listed path, and nothing else.
- `title` holds a one-line pull request title and `body.md` its body, when a path changed.

This workflow runs none of the repository's code. It reads the newest completed refresh run on the base branch, fails when that run failed, and refuses a proposal that lists a path outside the row's paths, holds a link or an unlisted file, or exceeds the size and text limits. When the proposal changes anything, it opens or updates one `community-data-refresh` pull request against the base branch, with an App token scoped to that repository. The pull request carries one commit on the commit the refresh ran on, changing only the paths the proposal lists. Base-branch commits made since then to other paths are expected, and the pull request's merge takes them in. The run fails without writing in two cases. First, the commit the refresh ran on is no longer in the base branch's history, because the branch was rewritten. Second, a path the proposal lists differs between that commit and the base branch's tip, so the proposal would overwrite a later edit; the error names each such path and both of its git object ids. In either case run the repository's refresh again, then this workflow, or wait for the next scheduled refresh. While the pull request holds a commit someone else pushed, it is left alone: the run proposes nothing and shows a warning on its summary page, so merge or close that pull request to let the next refresh through. Nothing merges it: it waits for `ci / validate` and a human merge.

`release-maintenance.yaml` runs every hour for two-branch repositories only. It merges a Renovate security pull request on `dev` or `main` when `ci / validate` is green and the pull request carries a patch, minor, digest or pin label.

For a fixable finding of the daily scan of `main`, it asks Renovate to run the weekly `main` group early through the Dependency Dashboard. It does so when the group updates the package or, for an image that installs no packages, the base image its final stage is built on. The dashboard lists a group's packages only when the group holds two or more, so a finding against a group with no list is reported in the issue instead. It acts only on a complete scan that started in the last 36 hours.

For an OS package of an image whose final stage installs packages, it opens a rebuild pull request into `main` instead. The rebuild refreshes those packages only when an install runs after an `ARG PKG_REFRESH` line, in the same stage or in a stage it is built from. Otherwise the finding is listed in the issue.

It also keeps one issue per repository, labelled `release-blocked`, and closes it once every section is clear. The title names the sections the issue holds, such as `Vulnerability fix has no automatic route`, so a finding that blocks no release does not read as a blocked one. The issue lists the Saturday update when it has not shipped by 12:00 UTC. It lists any security, expedited or rebuild pull request into `main` that has not shipped within three hours. It also lists every fixable finding with no automatic route, and a missing, older or incomplete scan. When a read fails, the issue keeps the sections that read would have checked as they were, and the issue is not closed that hour. It also stays open in an hour when a planned merge, rebuild or dashboard tick did not happen, for example because the dashboard changed after it was read.
