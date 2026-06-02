# Workflow Templates

Canonical thin-caller workflows, synced into consumer repos by type via `.github/sync.yml`.

## Sync-managed (uniform across repos of the same type)

- **codeql-go.yml / codeql-ts.yml** — CodeQL analysis by language
- **security-fs.yml / security-image.yml** — Trivy scans (fs-only for libraries, fs+image for apps/Docker repos)
- **release-go.yml / release-ts.yml** — Release workflow by language

## Not synced (per-repo customization expected)

- **ci-go-lib.yml / ci-go-app.yml / ci-ts-lib.yml / ci-shell.yml** — CI callers vary per repo (extra web jobs, working-directory overrides, custom shellcheck-paths, etc.). Bootstrap new repos by copying the matching `ci-*.yml` to `.github/workflows/ci.yaml`.
