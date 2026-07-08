#!/usr/bin/env bash
# Local equivalent of `.github/workflows/ci.yaml` (or validate.yaml, legacy) — parses the workflow
# that runs in CI and executes each step locally.
#
# Usage:
#   bash ci-local.sh                  # run from repo root
#   bash ci-local.sh --path apps/age  # restrict to a subdirectory (a private repo)
#   bash ci-local.sh --plan-only      # show plan, no execution
#   bash ci-local.sh --workflow PATH  # use a different workflow file
#   bash ci-local.sh --ignore-unknown # don't fail on unrecognized actions
#
# When invoked with --path SUBDIR, the script tries to use that subdirectory's
# own `.github/workflows/ci.yaml` if present; otherwise it falls back to
# autodetect mode (Go suite + hadolint + shellcheck + shfmt + gitleaks based on
# what files are in SUBDIR).

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/_ci_local.py" "$@"
