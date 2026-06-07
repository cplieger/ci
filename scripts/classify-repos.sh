#!/usr/bin/env bash
# classify-repos.sh — Auto-discover and profile all cplieger repos,
# outputting a .github/sync.yml for repo-file-sync-action.
#
# Note: per-type ci.yaml templates (ci-go, ci-shell, ci-ts-lib) were retired
# in favor of a single unified ci.yml that targets the central detect-and-
# dispatch ci.yaml workflow in cplieger/ci. ALL releaseable repos receive
# the same ci.yaml; the central workflow auto-detects surfaces (go.mod /
# jsr.json / Dockerfile / nested web frontend) and runs the right jobs in
# parallel. No more bespoke per-repo ci.yaml.
set -euo pipefail

OWNER="cplieger"
TIMEOUT=10 # seconds per API call

# Timeout wrapper
api() { timeout "${TIMEOUT}" gh api "$@"; }

# Collect non-archived repos
repos_json=$(timeout "${TIMEOUT}" gh repo list "${OWNER}" --limit 300 \
  --json name,isArchived,primaryLanguage --jq '[.[] | select(.isArchived == false)]')

declare -A LANG        # go|ts|shell|none
declare -A HAS_JSR
declare -A HAS_PKG
declare -A IS_WEB      # has static-src or web dir alongside go.mod
declare -A CLIFF_TIER  # stable|alpha
declare -A HAS_CODE    # for codeql decision
declare -A CAN_RELEASE # has go.mod or jsr.json or Dockerfile

repo_names=$(echo "${repos_json}" | jq -r '.[].name' | sort)

for repo in ${repo_names}; do
  # Skip the ci repo itself
  if [[ "${repo}" == "ci" ]]; then
    continue
  fi

  # Get root tree entries
  tree_json=$(api "repos/${OWNER}/${repo}/git/trees/HEAD?recursive=0" --jq '.tree[].path' 2>/dev/null || echo "")

  has_gomod=false; has_jsr=false; has_pkg=false; has_dockerfile=false; is_web=false; has_pyproject=false

  while IFS= read -r entry; do
    case "${entry}" in
      go.mod) has_gomod=true ;;
      jsr.json) has_jsr=true ;;
      package.json) has_pkg=true ;;
      Dockerfile) has_dockerfile=true ;;
      pyproject.toml) has_pyproject=true ;;
      static-src|web) is_web=true ;;
    esac
  done <<< "${tree_json}"

  # If go.mod present, also check for internal/server/static-src pattern
  if [[ "${has_gomod}" == "true" && "${is_web}" == "false" ]]; then
    # Check for deeper web indicators via recursive tree
    deep_check=$(api "repos/${OWNER}/${repo}/git/trees/HEAD?recursive=1" \
      --jq '[.tree[].path | select(test("static-src|/web/"))] | length' 2>/dev/null || echo "0")
    if [[ "${deep_check}" -gt 0 ]]; then
      is_web=true
    fi
  fi

  # Classify language
  lang="none"
  if [[ "${has_gomod}" == "true" ]]; then
    lang="go"
  elif [[ "${has_jsr}" == "true" ]]; then
    lang="ts"
  elif [[ "${has_pkg}" == "true" ]]; then
    lang="ts"
  elif [[ "${has_dockerfile}" == "true" ]]; then
    lang="shell"
  elif [[ "${has_pyproject}" == "true" ]]; then
    lang="python"
  fi

  # Has code (for codeql): go or ts repos
  has_code=false
  if [[ "${lang}" == "go" || "${lang}" == "ts" ]]; then
    has_code=true
  fi

  # Can release: has go.mod, jsr.json, or Dockerfile
  can_release=false
  if [[ "${has_gomod}" == "true" || "${has_jsr}" == "true" || "${has_dockerfile}" == "true" ]]; then
    can_release=true
  fi

  # Cliff tier: default to stable. Only fall back to alpha when the repo is
  # explicitly mid-0.x (latest tag is v0.x). This way fresh/untagged repos get
  # the stable policy so their first release naturally bumps to v1.0.0 instead
  # of being trapped at v0.1.0 by alpha's no-major-bump rule.
  latest_tag=$(api "repos/${OWNER}/${repo}/tags" --jq '.[0].name // ""' 2>/dev/null || echo "")
  cliff_tier="stable"
  if [[ "${latest_tag}" == v0.* ]]; then
    cliff_tier="alpha"
  fi

  LANG["${repo}"]="${lang}"
  HAS_JSR["${repo}"]="${has_jsr}"
  HAS_PKG["${repo}"]="${has_pkg}"
  IS_WEB["${repo}"]="${is_web}"
  CLIFF_TIER["${repo}"]="${cliff_tier}"
  HAS_CODE["${repo}"]="${has_code}"
  CAN_RELEASE["${repo}"]="${can_release}"

  >&2 printf "  classified: %-30s lang=%-5s web=%-5s cliff=%-6s release=%s\n" \
    "${repo}" "${lang}" "${is_web}" "${cliff_tier}" "${can_release}"
done

# --- Generate sync.yml ---

# Collect repos into groups
ci_repos=()        # ALL releaseable repos -> unified ci.yml
codeql_repos=()
security_repos=()
release_repos=()
cliff_stable=()
cliff_alpha=()
golangci_repos=()  # all go repos get .golangci.yaml
ts_config_repos=() # ts repos + go-cross-language repos get eslint/prettier/stylelint/htmlvalidate
python_repos=()    # python repos (pyproject.toml) -> ruff.toml + .editorconfig only

for repo in ${repo_names}; do
  [[ "${repo}" == "ci" ]] && continue
  lang="${LANG[${repo}]:-none}"
  [[ "${lang}" == "none" ]] && continue

  # Python repos (e.g. the tools methodology repo): lint baseline only —
  # ruff + .editorconfig. Not releaseable, not compiled, so no ci.yml/codeql/
  # release/cliff; they keep their own bespoke ci.yaml.
  if [[ "${lang}" == "python" ]]; then
    python_repos+=("${repo}")
    continue
  fi

  # Security: ALL repos with any detectable content
  security_repos+=("${repo}")

  # CodeQL: go and ts repos
  if [[ "${HAS_CODE[${repo}]}" == "true" ]]; then
    codeql_repos+=("${repo}")
  fi

  # Unified CI + golangci config
  if [[ "${CAN_RELEASE[${repo}]}" == "true" ]]; then
    ci_repos+=("${repo}")
  fi

  # .golangci.yaml: any repo with Go code (lang=go)
  if [[ "${lang}" == "go" ]]; then
    golangci_repos+=("${repo}")
  fi

  # TS lint configs: pure-TS repos
  if [[ "${lang}" == "ts" ]]; then
    ts_config_repos+=("${repo}")
  fi

  # Release: all releasable repos (unified auto-detect handles type)
  if [[ "${CAN_RELEASE[${repo}]}" == "true" ]]; then
    release_repos+=("${repo}")
  fi

  # Cliff tier
  if [[ "${CLIFF_TIER[${repo}]}" == "stable" ]]; then
    cliff_stable+=("${repo}")
  else
    cliff_alpha+=("${repo}")
  fi
done

# Cross-language Go repos that ALSO have TS surfaces (e.g. vterm: go.mod + web/jsr.json,
# vibekit/vibecli/subflux: go.mod + static-src/) — also need TS lint configs.
for repo in ${repo_names}; do
  [[ "${repo}" == "ci" ]] && continue
  if [[ "${LANG[${repo}]:-}" == "go" ]]; then
    if [[ "${HAS_JSR[${repo}]:-}" == "true" || "${HAS_PKG[${repo}]:-}" == "true" || "${IS_WEB[${repo}]:-}" == "true" ]]; then
      ts_config_repos+=("${repo}")
    fi
  fi
done

# Helper: emit repos block
emit_repos() {
  local -n arr=$1
  if [[ ${#arr[@]} -eq 0 ]]; then return 1; fi
  printf "  - repos: |\n"
  for r in "${arr[@]}"; do
    printf "      %s/%s\n" "${OWNER}" "${r}"
  done
}

# Output
cat << 'HEADER'
# Auto-generated by scripts/classify-repos.sh — DO NOT EDIT MANUALLY.
# Re-run the script (or let the daily sync workflow do it) to regenerate.

group:
HEADER

# Unified CI (all releaseable repos): ci.yml + codeql + security
if [[ ${#ci_repos[@]} -gt 0 ]]; then
  echo "  # Unified CI (auto-detects go/ts/web/shell surfaces)"
  emit_repos ci_repos
  cat << 'EOF'
    files:
      - .editorconfig
      - source: .github/workflow-templates/ci.yml
        dest: .github/workflows/ci.yaml
      - source: .github/workflow-templates/codeql.yml
        dest: .github/workflows/codeql.yml
      - source: .github/workflow-templates/security.yml
        dest: .github/workflows/security.yml
EOF
fi

# .golangci.yaml + .gremlins.yaml — Go-having repos
if [[ ${#golangci_repos[@]} -gt 0 ]]; then
  echo ""
  echo "  # Go-tooling configs (Go-having repos)"
  emit_repos golangci_repos
  cat << 'EOF'
    files:
      - .golangci.yaml
      - source: configs/gremlins.yaml
        dest: .gremlins.yaml
EOF
fi

# Python repos (pyproject.toml): canonical ruff config + editorconfig.
# Lint baseline only — these repos are not releaseable and keep a bespoke ci.yaml.
if [[ ${#python_repos[@]} -gt 0 ]]; then
  echo ""
  echo "  # Python repos (ruff + editorconfig; bespoke ci.yaml)"
  emit_repos python_repos
  cat << 'EOF'
    files:
      - .editorconfig
      - source: configs/ruff.toml
        dest: ruff.toml
EOF
fi

# TS config files (eslint, prettier, stylelint, htmlvalidate) for ts + cross-language
if [[ ${#ts_config_repos[@]} -gt 0 ]]; then
  echo ""
  echo "  # TypeScript lint/format configs (TS-having repos, including hybrids)"
  emit_repos ts_config_repos
  cat << 'EOF'
    files:
      - source: configs/eslint.config.base.mjs
        dest: eslint.config.base.mjs
      - source: configs/prettier.json
        dest: .prettierrc.json
      - source: configs/stylelint.json
        dest: .stylelintrc.json
      - source: configs/htmlvalidate.json
        dest: .htmlvalidate.json
EOF
fi

# Release (unified auto-detect)
if [[ ${#release_repos[@]} -gt 0 ]]; then
  echo ""
  echo "  # Release (unified auto-detect)"
  emit_repos release_repos
  cat << 'EOF'
    files:
      - source: .github/workflow-templates/release.yml
        dest: .github/workflows/release.yaml
EOF
fi

# Cliff stable
if [[ ${#cliff_stable[@]} -gt 0 ]]; then
  echo ""
  echo "  # Cliff config (stable — v1.x+)"
  emit_repos cliff_stable
  cat << 'EOF'
    files:
      - source: configs/cliff-stable.toml
        dest: cliff.toml
EOF
fi

# Cliff alpha
if [[ ${#cliff_alpha[@]} -gt 0 ]]; then
  echo ""
  echo "  # Cliff config (alpha — v0.x or no tags)"
  emit_repos cliff_alpha
  cat << 'EOF'
    files:
      - source: configs/cliff-alpha.toml
        dest: cliff.toml
EOF
fi
