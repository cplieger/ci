# Workflow Templates

Canonical thin-caller workflows, synced into consumer repos via `.github/sync.yml`.

## Sync-managed (uniform across all releaseable repos)

- **ci.yml** — Unified CI caller. The central `ci.yaml` reusable workflow auto-detects repo surfaces (`go.mod` / `jsr.json` / `Dockerfile` / nested web frontend at `static-src/` or `web/` or `internal/server/static-src/`) and dispatches to the right reusable workflows in parallel. Hybrid repos (Go + TS web frontend) get both jobs running automatically — no per-repo configuration. To extend the auto-detection (new web-frontend path pattern, new language type), edit `cplieger/ci/.github/workflows/ci.yaml`.
- **codeql.yml** — CodeQL analysis (languages auto-detected by GitHub).
- **security.yml** — Trivy scans (fs always; image when Dockerfile present — auto-detected).
- **release.yml** — Unified release caller. Mirrors the same architecture as `ci.yml`: the central `release.yaml` reusable workflow detects release type and dispatches to docker / go / ts / subpackage branches.

The previous per-type CI templates (`ci-go.yml`, `ci-shell.yml`, `ci-ts-lib.yml`) were removed in favor of the unified `ci.yml`. There is no longer a "bespoke per-repo ci.yaml" tier.
