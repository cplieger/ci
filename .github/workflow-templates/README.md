# Workflow Templates

Canonical thin-caller workflows, synced into consumer repos by type via `.github/sync.yml`.

## Sync-managed (uniform across repos of the same type)

- **ci-go.yml** — Go CI caller (profile auto-detected: app-mode + hadolint activate based on repo contents)
- **security.yml** — Trivy scans (fs always; image when Dockerfile present — auto-detected)
- **codeql.yml** — CodeQL analysis (languages auto-detected)
- **release.yml** — Unified release caller. One template for ALL releaseable repos (Docker, Go, TS, hybrid). The central `release.yaml` reusable workflow detects the repo type, classifies which paths changed, and dispatches to the right downstream branch (Docker build/sign/publish, Go tag, TS publish to npm+JSR, or any combination for hybrid repos with subpackages). Per-repo policy overrides (registries, platforms, sbom mode) live centrally in that workflow's `Resolve repo policy` step — never in consumer repos.

## Not synced (per-repo customization expected)

- **ci-ts-lib.yml / ci-shell.yml** — CI callers that vary per repo (extra web jobs, working-directory overrides, custom shellcheck-paths, etc.). Bootstrap new repos by copying the matching `ci-*.yml` to `.github/workflows/ci.yaml`.
