# Workflow Templates

Canonical thin-caller workflows, synced into consumer repos by `sync.yaml` (the
repo↔file mapping is generated at sync time by `scripts/classify-repos.py`).

## Sync-managed (uniform across all releaseable repos)

- **ci.yml** — Unified CI caller. The central `ci.yaml` reusable workflow auto-detects repo surfaces (`go.mod` / `jsr.json` / `Dockerfile` / nested web frontend at `static-src/` or `web/` or `internal/server/static-src/`) and dispatches to the right reusable workflows in parallel. Hybrid repos (Go + TS web frontend) get both jobs running automatically — no per-repo configuration. To extend the auto-detection (new web-frontend path pattern, new language type), edit `cplieger/ci/.github/workflows/ci.yaml`. Its `ci` job grants `security-events: write` because the dead-code check uploads its findings to code scanning, and GitHub refuses to start a run whose called job asks for a permission its caller does not grant.
- **codeql.yml** — CodeQL analysis (languages auto-detected by GitHub).
- `security.yml` runs the shared security scan. Trivy scans the filesystem always and the built image when a Dockerfile is present. A separate Gitleaks job scans the full commit history for secrets. Every finding is advisory, reported to the Security tab, and never blocks a merge.
- **release.yml** — Unified release caller. Mirrors the same architecture as `ci.yml`: the central `release.yaml` reusable workflow selects the channel from the branch (dev or stable) before dispatching, detects the release type and fans out to the docker / go / ts / subpackage jobs.
