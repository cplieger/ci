# Workflow Templates

Canonical thin-caller workflows, synced into consumer repos by type via `.github/sync.yml`.

## Sync-managed (uniform across repos of the same type)

- **ci-go.yml** — Go CI caller (profile auto-detected: app-mode + hadolint activate based on repo contents)
- **security.yml** — Trivy scans (fs always; image when Dockerfile present — auto-detected)
- **codeql.yml** — CodeQL analysis (languages auto-detected)
- **release-go.yml / release-ts.yml** — Release workflow by language

## Not synced (per-repo customization expected)

- **ci-ts-lib.yml / ci-shell.yml** — CI callers that vary per repo (extra web jobs, working-directory overrides, custom shellcheck-paths, etc.). Bootstrap new repos by copying the matching `ci-*.yml` to `.github/workflows/ci.yaml`.
