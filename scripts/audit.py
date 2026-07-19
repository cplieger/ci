#!/usr/bin/env python3
"""Cross-repo governance audit for the cplieger account.

Polls every non-archived, non-fork repo for its full settings surface and
reports a
compliance report against the documented standard in
.kiro/steering/repo-governance.md, split into HARD failures and soft WARNINGS,
with N/A handling where a setting cannot apply (e.g. GitHub Advanced Security
features on free private repos).

Checks cover merge model (including the squash-commit title/message
defaults auto-merged PRs are squashed with), repo features (wiki, projects,
issues, discussions, update-branch suggestion, web commit signoff), branch
protection (a validate check pinned to the GitHub Actions app, phantom
required contexts that no workflow ever reports, unexpected extra required
checks, review/conversation-resolution/linear-history/signature/lock/
push-restriction toggles), rulesets (unexpected custom rulesets +
bypass-actor drift), Actions token defaults (read-only workflow permissions;
workflows cannot approve PRs), vulnerability reporting, secret/code scanning,
stray .github/dependabot.yml (Renovate owns dependency updates), CI wiring,
coverage-workflow presence on Go/TS repos, Docker Hub dual-publish secrets on
image repos, Renovate preset, license (present AND GPL-3.0), default branch,
description (presence + <=100 chars), topics (at least 2), the
dependency-graph "Used by counter" package on public repos (it is pinned to
one package and does NOT follow Go major-version module-path bumps or module
renames — no REST/GraphQL surface exposes the Settings -> Advanced Security
selection, so it is read from the public dependents page and the fix itself
stays a manual dropdown click), and the per-repo
deploy-trigger webhook that reaches the self-hosted orchestrator — presence,
signing secret, exact event set (push for the infra repos, release everywhere
else), JSON payload, TLS verification on, and exactly one hook (only when
AUDIT_WEBHOOK_HOST is set — the host is private infra, injected via env,
never hardcoded here).

Branch protection is the documented standard (classic protection); any
custom repository ruleset is treated as drift, and an Integration bypass actor
on any ruleset is a HARD failure (it is the stale-decommissioned-app rot class
— e.g. a former hosted-Renovate GitHub App left able to bypass protection).

Robustness: every GitHub API call retries transient failures (rate limits,
5xx) with backoff, and a definitive 404 is distinguished from an API error —
a flaky call can therefore never manufacture a false HARD failure (observed
2026-07: a transient contents-read failure reported a correctly-wired repo as
"CI not wired"). If a call still fails after retries, the affected check is
skipped and reported as [error]; the run exits 2 (infra trouble), never 1
(compliance), for API errors alone.

Known-accepted deviations live in the ACCEPTED table below with a reason;
they are suppressed from the report (counted, not listed) so the steady-state
fleet reports clean and any NEW warning is signal, not noise. Cosmetic checks
(license, description, topics) apply to public repos only — private repos
have no audience for them.

Exit codes: 0 = compliant; 1 = at least one HARD failure; 2 = usage/infra
(under-scoped token, or API errors that prevented a full audit).

Requires `gh` authenticated with a CLASSIC PAT carrying the 'repo' scope: the
merge-model fields (allow_merge_commit etc.) are only serialized onto the repo
object for a classic-scope token. A fine-grained PAT does NOT expose them even
with Administration:read and an owner role, and the default GITHUB_TOKEN is
under-scoped — in both cases the audit aborts (exit 2) rather than emit false
negatives.

Run:
  scripts/audit.py                  # all repos (public + private)
  scripts/audit.py --visibility public
  scripts/audit.py --repo <name>    # one repo only (repeatable) — e.g. the
                                    # bootstrap-repo skill's post-creation gate
  scripts/audit.py --dump out.json  # also write raw collected settings as JSON
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OWNER = "cplieger"
PRESET = "github>cplieger/.github"
REUSABLE = "cplieger/ci/.github/workflows"
INFRA = {".github", "ci"}  # they define the standard; CI-wiring check is N/A for them
# Repos with a deliberate repo-local CI instead of the reusable-workflow thin
# caller: they validate surfaces the shared workflows don't cover and gate on a
# bespoke `validate` job (which the branch-protection check accepts as the bare
# 'validate' context). The CI-wiring check is N/A for them, same as INFRA.
BESPOKE_CI = {".kiro", "homelab"}
# Host of the self-hosted deploy/dependency orchestrator that each repo pings
# via a per-repo webhook (a release, or a push on infra repos, reaches it so the
# change redeploys / re-runs dependency updates). It is private infrastructure,
# so it is injected via the AUDIT_WEBHOOK_HOST env/secret rather than hardcoded
# in this public repo. When unset, the webhook check is skipped entirely so a
# local run without the secret does not report every repo as non-compliant.
WEBHOOK_HOST = os.environ.get("AUDIT_WEBHOOK_HOST", "").strip()
# GitHub auto-creates and manages this ruleset when code-scanning merge
# protection is enabled. It is not user-authored, so it is whitelisted from the
# "unexpected custom ruleset" check. Every other ruleset is drift.
MANAGED_RULESETS = {"code-scanning-merge-protection"}
# Repos whose deploy-trigger webhook fires on push@main instead of release:
# the non-releaseable infra/config repos (repo-governance.md "Apply"). Every
# other repo releases, so its hook must carry the release event or releases
# silently never reach the orchestrator.
PUSH_WEBHOOK_REPOS = {".github", ".kiro", "ci", "homelab"}
# Image repos that publish to GHCR only (no Docker Hub mirror), so the
# DOCKERHUB_* secrets check is N/A. MUST mirror the per-repo policy overrides
# in .github/workflows/release.yaml (the `policy` step) — update both together.
GHCR_ONLY = {"subflux", "vibekit"}
# The required check every repo carries, and the GitHub App expected to report
# it (15368 = GitHub Actions). A validate context restricted to a different
# app — or to none (-1, any app may report it) — weakens the gate: an
# arbitrary integration could satisfy the merge requirement.
ACTIONS_APP_ID = 15368

# Known-accepted deviations from the standard: {repo: {warning-prefix: reason}}.
# A warning whose text starts with a listed prefix is suppressed from the
# report (counted under "accepted", not listed), so the steady-state fleet
# reports clean and a new warning stands out. Every entry needs a reason;
# remove entries when the deviation is fixed.
ACCEPTED = {
    "homelab": {
        "renovate preset not extended":
            "deliberate: standalone renovate.json consumed directly by the "
            "resident Renovate container (not the fleet preset)",
    },
    "docker-radvd": {
        "unexpected extra required check 'smoke'":
            "deliberate: repo-local smoke signal-contract job required in "
            "addition to ci / validate (repo-governance.md, 2026-07)",
    },
    "web-terminal-server": {
        "unexpected extra required check 'smoke'":
            "deliberate: repo-local smoke signal-contract job required in "
            "addition to ci / validate (same pattern as docker-radvd)",
    },
}

# Documented governance standard (repo-governance.md).
# HARD merge-model settings: any deviation fails the audit (exit 1). These have
# real consequences — stray merge-commit history, un-mergeable or non-auto-merging
# PRs, lost branch hygiene.
GOV_HARD = {
    "allow_merge_commit": False,
    "allow_squash_merge": True,
    "allow_rebase_merge": True,
    "delete_branch_on_merge": True,
    "allow_auto_merge": True,
}
# Repo-feature settings: advisory only (cosmetic), reported as warnings.
# The squash-commit defaults matter because sync and Renovate PRs land as
# auto-squash merges: COMMIT_OR_PR_TITLE keeps the conventional-commit subject
# git-cliff builds the changelog from (single-commit PRs keep their commit
# title; multi-commit PRs fall back to the PR title).
GOV_SOFT = {
    "has_wiki": False,
    "has_projects": False,
    "has_issues": True,
    "has_discussions": False,
    "allow_update_branch": False,
    "web_commit_signoff_required": False,
    "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
    "squash_merge_commit_message": "COMMIT_MESSAGES",
}

def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


# Sentinel: the API call kept failing transiently after retries. Distinct from
# None (definitive absence, e.g. HTTP 404) so a flaky call can never be
# mistaken for "the thing is missing" and manufacture a false HARD failure.
API_ERROR = object()


def gh_retry(*args, tries=4):
    """Run gh, retrying transient failures with exponential backoff.

    Returns (result, definitive). definitive=True means the outcome can be
    trusted: success, or a real 4xx (404 absence, 403 permission). False means
    the call still failed after all retries for a transient-looking reason
    (rate limit, 5xx, network) and MUST NOT be interpreted as absence.
    """
    delay, r = 2, None
    for attempt in range(tries):
        r = gh(*args)
        if r.returncode == 0:
            return r, True
        stderr = r.stderr or ""
        m = re.search(r"HTTP (\d{3})", stderr)
        code = int(m.group(1)) if m else None
        rate_limited = "rate limit" in stderr.lower()
        transient = rate_limited or code is None or code >= 500 or code == 429
        if not transient:
            return r, True  # definitive 4xx — absence or permission; trust it
        if attempt < tries - 1:
            time.sleep(delay)
            delay *= 2
    return r, False


def gh_json(*args):
    """Parsed JSON on success; None on definitive absence; API_ERROR on a
    transient failure that survived retries."""
    r, definitive = gh_retry(*args)
    if not definitive:
        return API_ERROR
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def api_status(path):
    """(ok, definitive) for endpoints that signal via HTTP status
    (204 enabled -> rc 0, 404 disabled -> rc != 0)."""
    r, definitive = gh_retry("api", path)
    return r.returncode == 0, definitive


def file_text(repo, path):
    """Decoded file content; '' when the file is definitively absent;
    None on an API error (unknown — do not treat as absent)."""
    r, definitive = gh_retry(
        "api", f"repos/{OWNER}/{repo}/contents/{path}", "--jq", ".content"
    )
    if not definitive:
        return None
    if r.returncode != 0:
        return ""
    try:
        return base64.b64decode("".join(r.stdout.split())).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return ""


AUDIT_UA = "Mozilla/5.0 (compatible; cplieger-governance-audit)"


def used_by_package_scrape(name):
    """The package a repo's "Used by" counter currently represents, plus the
    package set the counter could be switched to.

    No REST or GraphQL surface exposes the "Used by counter" selection
    (Settings -> Advanced Security; verified absent 2026-07), so both are read
    from the public dependents page /<owner>/<repo>/network/dependents:

    - current: the og:title meta names the selected package ('Network
      Dependents · owner/repo · <package> repositories'; no third segment when
      the repo publishes no package). og:title is the social-embed surface,
      far more redesign-stable than the page markup.
    - selectable: the package-switcher menu anchors (?package_id=...). Repos
      with one package render no menu -> empty set. A Go app's /vN module
      path is often never indexed at all (nothing imports an app), so the
      "right" package may not exist to select — the caller must only flag
      drift the settings dropdown can actually fix.

    Returns (current, selectable, definitive). definitive=False means the
    page could not be read — skip the check, never infer drift.
    """
    url = f"https://github.com/{OWNER}/{name}/network/dependents"
    req = urllib.request.Request(url, headers={"User-Agent": AUDIT_UA})  # noqa: S310 — fixed https:// URL, host is github.com
    delay = 5
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — same fixed https URL
                body = resp.read(512 * 1024).decode("utf-8", "replace")
            m = re.search(r'property="og:title" content="Network Dependents '
                          r'· [^"]+? · (.+?) repositories"', body)
            names = set()
            for mm in re.finditer(r'href="/[^"]+/network/dependents\?package_id='
                                  r'[^"]+"[^>]*>(.*?)</a>', body, re.DOTALL):
                # anchor bodies may nest tags; reduce to text before judging
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", mm.group(1))).strip()
                if text and not re.fullmatch(r"[\d,]+ Repositor(?:y|ies)", text):
                    names.add(text)
            return (m.group(1) if m else None), sorted(names), True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, [], True
            if attempt < 2 and e.code in (429, 502, 503):
                time.sleep(delay)
                delay *= 3
                continue
            return None, [], False
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(delay)
                delay *= 3
                continue
            return None, [], False
    return None, [], False


def collect(meta):
    """Gather the full governance-relevant settings surface for one repo.

    s["errors"] records every check whose API reads failed transiently after
    retries; compliance() skips those checks instead of failing them, and
    main() reports them as [error] with exit 2.
    """
    name = meta["name"]
    s = {"name": name, "infra": name in INFRA, "errors": [], "fatal": False}
    repo = gh_json("api", f"repos/{OWNER}/{name}")
    if repo is API_ERROR:
        # Without the repo object there is nothing meaningful to audit.
        s["errors"].append("repo settings unreadable (API)")
        s["fatal"] = True
        s.update({"visibility": None, "private": bool(meta.get("visibility") == "private"),
                  "admin_visible": False})
        return s
    repo = repo or {}
    s["visibility"] = repo.get("visibility")
    s["private"] = bool(repo.get("private"))
    branch = repo.get("default_branch") or "main"
    s["default_branch"] = branch
    # The merge-model fields are only serialized onto the repo object for a
    # token with the classic `repo` scope. A fine-grained PAT — even one with
    # Administration:read and an admin role (permissions.admin=true) — does NOT
    # expose them, so they come back absent (-> None) and the audit would
    # report all repos as non-compliant. Key the guard off actual field
    # presence, NOT permissions.admin, so a fine-grained token is correctly
    # detected as under-scoped rather than trusted.
    s["admin_visible"] = "allow_merge_commit" in repo
    for k in ("allow_merge_commit", "allow_squash_merge", "allow_rebase_merge",
              "allow_auto_merge", "delete_branch_on_merge",
              "has_wiki", "has_projects", "has_issues", "has_discussions",
              "allow_update_branch", "web_commit_signoff_required",
              "squash_merge_commit_title", "squash_merge_commit_message"):
        s[k] = repo.get(k)
    lic = repo.get("license")
    s["license"] = lic.get("spdx_id") if lic else None
    desc = (repo.get("description") or "").strip()
    s["desc_present"] = bool(desc)
    s["desc_len"] = len(desc)
    s["topics"] = repo.get("topics") or []

    sa = repo.get("security_and_analysis") or {}
    s["secret_scanning"] = (sa.get("secret_scanning") or {}).get("status")
    s["secret_scanning_push_protection"] = (sa.get("secret_scanning_push_protection") or {}).get("status")
    s["dependabot_security_updates"] = (sa.get("dependabot_security_updates") or {}).get("status")

    ok, definitive = api_status(f"repos/{OWNER}/{name}/vulnerability-alerts")
    s["vuln_alerts"] = ok if definitive else None
    if not definitive:
        s["errors"].append("vulnerability-alerts unreadable (API)")

    pvr = gh_json("api", f"repos/{OWNER}/{name}/private-vulnerability-reporting")
    if pvr is API_ERROR:
        s["private_vuln_reporting"] = None
        s["errors"].append("private-vulnerability-reporting unreadable (API)")
    else:
        s["private_vuln_reporting"] = bool((pvr or {}).get("enabled"))

    prot = gh_json("api", f"repos/{OWNER}/{name}/branches/{branch}/protection")
    if prot is API_ERROR:
        prot = None
        s["has_protection"] = None
        s["errors"].append("branch protection unreadable (API)")
    else:
        s["has_protection"] = isinstance(prot, dict) and "url" in prot
    if s["has_protection"]:
        rsc = prot.get("required_status_checks") or {}
        contexts = list(rsc.get("contexts") or [])
        contexts += [c.get("context") for c in (rsc.get("checks") or []) if c.get("context") not in contexts]
        s["required_checks"] = contexts
        # context -> app_id, to verify the validate gate is pinned to the
        # GitHub Actions app (a -1 / other-app pin lets any integration
        # satisfy the merge requirement).
        s["required_check_apps"] = {c.get("context"): c.get("app_id")
                                    for c in (rsc.get("checks") or [])}
        s["strict"] = rsc.get("strict")
        s["enforce_admins"] = (prot.get("enforce_admins") or {}).get("enabled")
        s["allow_force_pushes"] = (prot.get("allow_force_pushes") or {}).get("enabled")
        s["allow_deletions"] = (prot.get("allow_deletions") or {}).get("enabled")
        # The rest of the classic-protection surface. The standard sets none
        # of these; presence/enabled is drift (and a locked branch, or an
        # approving-review floor a single-maintainer account can never satisfy,
        # is outright breakage — no one can self-approve a PR).
        reviews = prot.get("required_pull_request_reviews")
        s["required_reviews_present"] = reviews is not None
        s["required_review_count"] = (reviews or {}).get("required_approving_review_count", 0)
        s["required_conversation_resolution"] = (prot.get("required_conversation_resolution") or {}).get("enabled")
        s["required_linear_history"] = (prot.get("required_linear_history") or {}).get("enabled")
        s["required_signatures"] = (prot.get("required_signatures") or {}).get("enabled")
        s["lock_branch"] = (prot.get("lock_branch") or {}).get("enabled")
        s["push_restrictions"] = prot.get("restrictions") is not None
    else:
        s["required_checks"] = []
        s["required_check_apps"] = {}
        s["strict"] = s["enforce_admins"] = s["allow_force_pushes"] = s["allow_deletions"] = None
        s["required_reviews_present"] = s["push_restrictions"] = False
        s["required_review_count"] = 0
        s["required_conversation_resolution"] = s["required_linear_history"] = None
        s["required_signatures"] = s["lock_branch"] = None

    # Phantom required contexts. Branch protection matches a required context
    # against reported check-run NAMES: for a reusable-workflow job that is
    # 'caller / nested' (e.g. 'ci / validate'), but for a plain workflow job it
    # is the job name alone — the PR checks UI displays 'Workflow / job', which
    # is NOT the context name. A context nothing ever reports blocks every PR
    # forever as "Expected — waiting for status to be reported" while all real
    # checks are green (bit docker-radvd PR #248, 2026-07: required as
    # 'Smoke / smoke' what reports as 'smoke'). Verify every required context
    # against names actually observed: on the default-branch HEAD first; only
    # if something is still unobserved, escalate to the head commits of the 3
    # most recently updated PRs (some repos run CI on PRs only, so their main
    # HEAD carries no check runs) plus the HEAD's legacy combined status (a
    # non-Actions integration would report there, not as a check run).
    s["observed_checks"] = []
    s["observed_complete"] = True
    if s["required_checks"]:
        names, complete = set(), True

        def check_names(ref):
            nonlocal complete
            cr = gh_json("api",
                         f"repos/{OWNER}/{name}/commits/{ref}/check-runs?per_page=100")
            if cr is API_ERROR:
                complete = False
                return set()
            return {c.get("name") for c in (cr or {}).get("check_runs") or []
                    if c.get("name")}

        names |= check_names(branch)
        if not set(s["required_checks"]) <= names:
            prs = gh_json("api", f"repos/{OWNER}/{name}/pulls"
                                 "?state=all&sort=updated&direction=desc&per_page=3")
            if prs is API_ERROR:
                complete = False
            else:
                for pr in prs if isinstance(prs, list) else []:
                    sha = (pr.get("head") or {}).get("sha")
                    if sha:
                        names |= check_names(sha)
            st = gh_json("api", f"repos/{OWNER}/{name}/commits/{branch}/status")
            if st is API_ERROR:
                complete = False
            else:
                names |= {c.get("context") for c in (st or {}).get("statuses") or []
                          if c.get("context")}
        s["observed_checks"] = sorted(names)
        s["observed_complete"] = complete
        if not complete and not set(s["required_checks"]) <= names:
            s["errors"].append("check-run names unreadable (API) — "
                               "phantom-required-context check skipped")

    # Repository rulesets. The standard is classic branch protection, so
    # any non-managed ruleset is drift. The bypass-actor list is the precise
    # surface that classic-protection checks miss: a decommissioned GitHub App
    # (e.g. the former hosted-Renovate app) can linger as an Integration bypass
    # actor that nothing else flags. The list endpoint returns only id/name/
    # enforcement, so each ruleset is fetched in detail for its bypass_actors.
    s["custom_rulesets"] = []
    s["ruleset_bypass_actors"] = []  # (ruleset_name, actor_type, actor_id)
    rulesets = gh_json("api", f"repos/{OWNER}/{name}/rulesets")
    if rulesets is API_ERROR:
        rulesets = []
        s["errors"].append("rulesets unreadable (API)")
    for rs in rulesets if isinstance(rulesets, list) else []:
        rname = rs.get("name", "")
        full = gh_json("api", f"repos/{OWNER}/{name}/rulesets/{rs.get('id')}")
        if full is API_ERROR:
            s["errors"].append(f"ruleset '{rname}' unreadable (API)")
            continue
        full = full or {}
        if rname not in MANAGED_RULESETS:
            s["custom_rulesets"].append({"name": rname, "enforcement": full.get("enforcement")})
        for a in full.get("bypass_actors") or []:
            s["ruleset_bypass_actors"].append((rname, a.get("actor_type"), a.get("actor_id")))

    # Actions token defaults. All synced workflows declare explicit
    # `permissions:` blocks (zizmor gates that), so the repo-level default is
    # defense-in-depth for repo-local extras — it should stay read-only. A
    # GITHUB_TOKEN that can approve PRs is an attack-chain enabler on repos
    # with auto-merge on: a malicious workflow could approve and land itself.
    wperm = gh_json("api", f"repos/{OWNER}/{name}/actions/permissions/workflow")
    if wperm is API_ERROR:
        s["default_workflow_permissions"] = None
        s["workflows_can_approve_prs"] = None
        s["errors"].append("actions workflow permissions unreadable (API)")
    else:
        wperm = wperm or {}
        s["default_workflow_permissions"] = wperm.get("default_workflow_permissions")
        s["workflows_can_approve_prs"] = wperm.get("can_approve_pull_request_reviews")

    wf = gh_json("api", f"repos/{OWNER}/{name}/contents/.github/workflows")
    if wf is API_ERROR:
        s["has_codeql"] = s["has_security_scan"] = s["has_scorecard"] = None
        s["has_coverage"] = None
        s["errors"].append("workflow listing unreadable (API)")
    else:
        wf_names = {f["name"] for f in wf} if isinstance(wf, list) else set()
        s["has_codeql"] = bool({"codeql.yml", "codeql.yaml"} & wf_names)
        s["has_security_scan"] = bool({"security.yml", "security.yaml"} & wf_names)
        s["has_scorecard"] = bool({"scorecard.yml", "scorecard.yaml"} & wf_names)
        s["has_coverage"] = bool({"coverage.yml", "coverage.yaml"} & wf_names)

    # Surface detection, mirroring scripts/classify-repos.sh: a root go.mod or
    # package.json means measurable coverage (sync delivers coverage.yml), a
    # root Dockerfile means the release pipeline publishes an image (and, for
    # dual-publish repos, needs the Docker Hub secrets).
    probe_texts = {}
    for probe_file, key in (("go.mod", "has_gomod"), ("package.json", "has_packagejson"),
                            ("Dockerfile", "has_dockerfile")):
        txt = file_text(name, probe_file)
        probe_texts[probe_file] = txt
        if txt is None:
            s[key] = None
            s["errors"].append(f"{probe_file} probe unreadable (API)")
        else:
            s[key] = bool(txt)

    # Used-by counter. Expected package = the root go.mod module path (Go
    # majors move the path, which is exactly the drift being caught), else the
    # npm package name (catches module/package renames); neither -> N/A. The
    # current selection comes from the public dependents page (no API — see
    # used_by_package_scrape). Public repos only: the counter has no audience
    # on a private repo, and the page needs to be publicly rendered anyway.
    m = re.search(r"^module\s+(\S+)", probe_texts.get("go.mod") or "", re.MULTILINE)
    s["go_module"] = m.group(1) if m else None
    s["expected_package"] = s["go_module"]
    if not s["expected_package"] and probe_texts.get("package.json"):
        try:
            s["expected_package"] = (json.loads(probe_texts["package.json"]) or {}).get("name")
        except json.JSONDecodeError:
            # malformed package.json: no npm name to expect; the used-by
            # check skips this repo (expected_package stays None)
            s["expected_package"] = None
    s["used_by_package"] = None
    s["used_by_selectable"] = []
    s["used_by_attempted"] = False
    s["used_by_readable"] = False
    if s["expected_package"] and not s["private"]:
        s["used_by_attempted"] = True
        pkg, selectable, definitive = used_by_package_scrape(name)
        s["used_by_readable"] = definitive
        s["used_by_package"] = pkg
        s["used_by_selectable"] = selectable

    # A committed dependabot.yml enables Dependabot VERSION update PRs, which
    # compete with Renovate (the settings twin of the security-updates check).
    dep_txt = file_text(name, ".github/dependabot.yml")
    if dep_txt is None:
        s["has_dependabot_yml"] = None
        s["errors"].append("dependabot.yml probe unreadable (API)")
    else:
        s["has_dependabot_yml"] = bool(dep_txt)

    # Docker Hub dual-publish secrets. Image repos (root Dockerfile) publish to
    # GHCR + Docker Hub by default (release.yaml policy step; GHCR_ONLY lists
    # the exceptions), and the Docker Hub login needs per-repo secrets —
    # cplieger is a user account, so there are no org-level secrets. A missing
    # secret fails the next release at the Docker Hub login step.
    s["dockerhub_secrets"] = None
    if s.get("has_dockerfile") and name not in GHCR_ONLY:
        sec = gh_json("api", f"repos/{OWNER}/{name}/actions/secrets")
        if isinstance(sec, dict) and "secrets" in sec:
            names_ = {x.get("name") for x in sec.get("secrets") or []}
            s["dockerhub_secrets"] = {"DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"} <= names_
        else:
            s["errors"].append("actions secrets unreadable (API)")

    ci_txt = file_text(name, ".github/workflows/ci.yaml")
    if ci_txt is None:
        s["ci_wired"] = None  # unknown — never report "not wired" on an API error
        s["errors"].append("ci.yaml unreadable (API)")
    else:
        s["ci_wired"] = REUSABLE in ci_txt

    ren_parts = [file_text(name, p) for p in
                 ("renovate.json", "org-inherited-config.json", "default.json")]
    if any(PRESET in (p or "") for p in ren_parts) or name == ".github":
        s["renovate_preset"] = True
    elif any(p is None for p in ren_parts):
        s["renovate_preset"] = None  # unknown — a read failed and none matched
        s["errors"].append("renovate config unreadable (API)")
    else:
        s["renovate_preset"] = False
    s["adopted"] = bool(s["ci_wired"]) or bool(s["renovate_preset"])

    # Deploy-trigger webhook. Read repo hooks and collect every active one
    # pointing at the orchestrator host, with the full config surface each
    # carries (events, payload type, TLS verification, signing secret, last
    # delivery). webhook_readable distinguishes "no matching hook" (readable,
    # empty/other hooks) from "could not read hooks" (token lacks the classic
    # 'repo'/hook scope) so the latter is a global skip, not per-repo false
    # failures. Only meaningful when WEBHOOK_HOST is set.
    s["webhook_readable"] = False
    s["webhooks"] = []
    if WEBHOOK_HOST:
        hooks = gh_json("api", f"repos/{OWNER}/{name}/hooks")
        if hooks is API_ERROR:
            hooks = None
            s["errors"].append("webhooks unreadable (API)")
        if isinstance(hooks, list):
            s["webhook_readable"] = True
            for h in hooks:
                cfg = h.get("config") or {}
                if WEBHOOK_HOST not in (cfg.get("url") or "") or not h.get("active"):
                    continue
                code = (h.get("last_response") or {}).get("code")
                s["webhooks"].append({
                    "events": sorted(h.get("events") or []),
                    "content_type": cfg.get("content_type"),
                    "insecure_ssl": str(cfg.get("insecure_ssl", "")),
                    "has_secret": bool(cfg.get("secret")),
                    "bad_delivery": code if isinstance(code, int) and code >= 400 else None,
                })
    return s


def compliance(s):
    """Return (hard_failures, warnings, accepted) for one repo's settings dict.

    Checks whose underlying API read failed (value None + an s["errors"]
    entry) are skipped — an API error must never masquerade as
    non-compliance. `accepted` holds warnings matched by the ACCEPTED table:
    known, documented deviations that would otherwise be permanent noise.
    """
    hard, warn = [], []
    if s.get("fatal"):
        return hard, warn, []

    for k, exp in GOV_HARD.items():
        if s.get(k) != exp:
            hard.append(f"{k}={s.get(k)} (want {exp})")
    for k, exp in GOV_SOFT.items():
        if s.get(k) != exp:
            warn.append(f"{k}={s.get(k)} (want {exp})")

    if s["default_branch"] != "main":
        hard.append(f"default_branch={s['default_branch']} (want main)")

    # License: public repos only — a private personal repo has no audience
    # that needs a license grant. The fleet standard is GPL-3.0, apps and
    # libraries alike (the synced LICENSE); anything else is drift.
    if not s["private"]:
        if s["license"] is None:
            (hard if s["adopted"] else warn).append("license missing")
        elif s["license"] != "GPL-3.0":
            warn.append(f"license {s['license']} (standard is GPL-3.0)")

    if s["has_protection"] is False:
        hard.append("no branch protection on default branch")
    elif s["has_protection"]:
        # App repos surface 'ci / validate' (the cplieger/ci meta job); repos
        # with a local CI surface a bare 'validate'. Accept either.
        validate_ctxs = [c for c in (s["required_checks"] or []) if "validate" in (c or "")]
        if not validate_ctxs:
            hard.append(f"required checks={s['required_checks']} (want a 'validate' check)")
        # The validate gate must be pinned to the GitHub Actions app. An
        # app_id of -1 (or another app) lets any integration report a
        # 'validate' check and satisfy the merge requirement.
        for ctx in validate_ctxs:
            app = (s.get("required_check_apps") or {}).get(ctx)
            if app != ACTIONS_APP_ID:
                warn.append(f"required check '{ctx}' pinned to app_id={app} "
                            f"(want {ACTIONS_APP_ID} = GitHub Actions)")
        # The standard is exactly the validate gate. Any other required check
        # is drift worth eyeballing — a typo'd or abandoned context is one
        # workflow rename away from the phantom class below. Deliberate extras
        # (docker-radvd's smoke job) live in ACCEPTED.
        for ctx in s["required_checks"] or []:
            if ctx not in validate_ctxs:
                warn.append(f"unexpected extra required check '{ctx}' "
                            "(standard is the validate gate alone)")
        # A required context that no recent commit ever reported is a phantom:
        # protection waits on it forever ("Expected"), blocking every PR while
        # all real checks are green. Judged only on complete data — when the
        # check-run reads failed, collect() already recorded an [error] and the
        # check is skipped here.
        if s.get("observed_complete"):
            observed = set(s.get("observed_checks") or [])
            for ctx in s["required_checks"] or []:
                if ctx not in observed:
                    hard.append(f"required context '{ctx}' never reported by any "
                                "recent check run (phantom — blocks every PR as "
                                "'Expected'; the context must equal the check-run "
                                "name, e.g. 'smoke', not 'Smoke / smoke')")
        if s["strict"]:
            warn.append("branch protection strict=on (want off)")
        if s["enforce_admins"]:
            warn.append("enforce_admins=on (want off)")
        if s["allow_force_pushes"]:
            warn.append("allow_force_pushes=on (want off)")
        if s["allow_deletions"]:
            warn.append("allow_deletions=on (want off)")
        # Review requirements: a single-maintainer account can never
        # self-approve, so an approving-review floor > 0 blocks every PR
        # (auto-merge included) — breakage, not drift. The toggle with
        # count=0 gates nothing but still deviates from the standard.
        if s.get("required_review_count"):
            hard.append(f"required approving reviews="
                        f"{s['required_review_count']} — a single-maintainer "
                        "repo cannot self-approve; every PR blocks (want off)")
        elif s.get("required_reviews_present"):
            warn.append("required_pull_request_reviews on (count=0, gates "
                        "nothing; standard is off)")
        if s.get("required_conversation_resolution"):
            warn.append("required_conversation_resolution=on (want off)")
        if s.get("required_linear_history"):
            warn.append("required_linear_history=on (want off; the merge "
                        "model already guarantees linear PR merges)")
        if s.get("required_signatures"):
            warn.append("required_signatures=on (want off; fleet commits "
                        "are unsigned, so this would block every merge)")
        if s.get("push_restrictions"):
            warn.append("push restrictions set (standard is none)")
        if s.get("lock_branch"):
            hard.append("branch locked (read-only — nothing can merge; want unlocked)")

    # Rulesets. Classic protection is the standard, so any custom ruleset is
    # drift (warn). A bypass actor weakens whatever ruleset carries it (warn);
    # an Integration bypass actor is a HARD failure — it is the stale-app rot
    # class (a decommissioned GitHub App left able to bypass protection, which
    # the GitHub API also refuses to rewrite on a user-owned repo, so it festers
    # invisibly until something like a history rewrite trips over it).
    for rs in s.get("custom_rulesets") or []:
        warn.append(f"unexpected custom ruleset '{rs['name']}' ({rs['enforcement']}) "
                    "(standard is classic branch protection)")
    for rname, atype, aid in s.get("ruleset_bypass_actors") or []:
        if atype == "Integration":
            hard.append(f"ruleset '{rname}' has an Integration bypass actor (id {aid}) "
                        "— likely a stale/decommissioned app; remove it")
        else:
            warn.append(f"ruleset '{rname}' has a bypass actor ({atype} id {aid})")

    if s["vuln_alerts"] is False:
        hard.append("dependabot vulnerability alerts off (want on)")
    # Private vulnerability reporting is a public-repo feature; N/A on private.
    if not s["private"] and s["private_vuln_reporting"] is False:
        hard.append("private vulnerability reporting off (want on)")
    if s["dependabot_security_updates"] == "enabled":
        hard.append("dependabot security UPDATES on (want off; Renovate owns deps)")
    if s.get("has_dependabot_yml"):
        warn.append("stray .github/dependabot.yml enables Dependabot version "
                    "PRs (want absent; Renovate owns deps)")

    # Actions token defaults. The workflows declare explicit `permissions:`
    # blocks, so the read default is defense-in-depth for anything repo-local;
    # PR-approval ability is a hard no — with auto-merge on, a workflow that
    # can approve PRs can land its own code.
    if s.get("default_workflow_permissions") not in (None, "read"):
        warn.append(f"default workflow permissions="
                    f"{s['default_workflow_permissions']} (want read)")
    if s.get("workflows_can_approve_prs"):
        hard.append("workflows can approve PRs (want off — with auto-merge "
                    "enabled this lets a workflow land its own code)")

    # Secret scanning / push protection: free on public repos; needs GHAS on
    # private (N/A on the free plan), so only enforced on public repos.
    if not s["private"]:
        if s["secret_scanning"] != "enabled":  # noqa: S105 — API state, not a password
            warn.append("secret scanning off (want on)")
        if s["secret_scanning_push_protection"] != "enabled":  # noqa: S105 — API state
            warn.append("secret scanning push protection off (want on)")

    # Scanning workflows arrive via sync for adopted repos; codeql + scorecard
    # are public-only features (CodeQL needs GHAS on private repos, Scorecard
    # only evaluates public repos), so both are N/A on private ones.
    if s["adopted"] and not s["infra"] and not s["private"]:
        if s["has_codeql"] is False:
            warn.append("codeql.yml missing")
        if s["has_security_scan"] is False:
            warn.append("security.yml missing")
        if s["has_scorecard"] is False:
            warn.append("scorecard.yml missing")
        # Go/TS repos have measurable statement coverage; sync delivers
        # coverage.yml to exactly those (classify-repos.sh), so a missing one
        # means the sync PR never landed or was reverted.
        if (s.get("has_gomod") or s.get("has_packagejson")) and s["has_coverage"] is False:
            warn.append("coverage.yml missing (Go/TS repos publish a coverage badge)")

    # Docker Hub dual-publish secrets: None = N/A (no root Dockerfile, a
    # GHCR-only repo, or the read failed and was recorded as an [error]).
    if s.get("dockerhub_secrets") is False:
        hard.append("DOCKERHUB_USERNAME/DOCKERHUB_TOKEN secrets missing "
                    "(dual-publish image repo — the next release fails at "
                    "the Docker Hub login)")

    # ci_wired is None when the contents read failed (already an [error]);
    # only a DEFINITIVE "file exists without the reusable ref / file absent"
    # is a hard failure. Bespoke-CI repos are exempt by design (see BESPOKE_CI).
    if not s["infra"] and s["name"] not in BESPOKE_CI and s["adopted"] and s["ci_wired"] is False:
        hard.append("CI not wired to cplieger/ci")
    if s["renovate_preset"] is False:
        warn.append("renovate preset not extended")

    # Description + topics: public repos only — discovery metadata has no
    # audience on a private repo. The house standard is 2-4 topics.
    if not s["private"]:
        if not s["desc_present"]:
            warn.append("description empty")
        elif s["desc_len"] > 100:
            warn.append(f"description {s['desc_len']} chars (>100; Docker Hub short-desc limit)")
        if len(s["topics"]) < 2:
            warn.append(f"{len(s['topics'])} topics (want at least 2)")
        # Used-by counter package: pinned per repo and never follows a Go
        # /vN module-path bump or a module rename, so the sidebar keeps
        # counting the stale package after every major. Only judged when the
        # dependents page definitively named a package (used_by_package set)
        # AND the expected package is actually in the switcher menu — a Go
        # app's new /vN path is often never indexed (nothing imports an app),
        # and warning about a package the dropdown cannot select is
        # unactionable noise. An unreadable page is silently skipped — a
        # scrape wobble must never manufacture drift, and this cosmetic check
        # is not worth an [error]-tier red run.
        exp = (s.get("expected_package") or "").lstrip("@")
        selectable = {p.lstrip("@") for p in s.get("used_by_selectable") or []}
        if (s.get("used_by_package") and exp
                and s["used_by_package"].lstrip("@") != exp
                and exp in selectable):
            warn.append(f"used-by counter shows '{s['used_by_package']}' "
                        f"(want '{s['expected_package']}'; no API — fix by "
                        "hand: Settings -> Advanced Security -> Used by counter)")
        # Module-path standard (go.md): a Go module lives at
        # github.com/<owner>/<repo>, plus /vN once majors move. Anything else
        # is unfetchable by Go tooling (module path must match the repo URL)
        # and indexes a phantom dependency-graph package that the used-by
        # counter then represents forever (the cert-watcher / age-decrypt /
        # fclones-wrapper / vibecli class, caught 2026-07).
        if s.get("go_module"):
            want = f"github.com/{OWNER}/{s['name']}"
            if not re.fullmatch(re.escape(want) + r"(/v\d+)?", s["go_module"]):
                warn.append(f"go.mod module '{s['go_module']}' is not the repo "
                            f"path (want '{want}' [+/vN]; unfetchable by Go "
                            "tooling and indexes a phantom dependency-graph "
                            "package)")

    # Deploy-trigger webhook. Enforced only when the host is configured AND this
    # repo's hooks were readable (see collect); an unreadable token is handled as
    # a global skip in main(), not as per-repo failures. Every non-archived repo
    # is expected to reach the orchestrator: a missing hook means releases never
    # propagate; a hook without a secret is rejected (the orchestrator validates
    # an HMAC signature over the payload); a hook subscribed to the wrong event
    # never fires at all — each is a silent, deploy-breaking gap. The full
    # config surface is checked: exact event set (push for the infra repos,
    # release everywhere else), JSON payload, TLS verification on.
    if WEBHOOK_HOST and s["webhook_readable"]:
        hooks = s.get("webhooks") or []
        want_event = "push" if s["name"] in PUSH_WEBHOOK_REPOS else "release"
        if not hooks:
            hard.append("no deploy-trigger webhook (releases won't reach the orchestrator)")
        elif len(hooks) > 1:
            warn.append(f"{len(hooks)} deploy-trigger webhooks (want exactly 1; "
                        "duplicates double-fire the orchestrator)")
        for h in hooks:
            if not h["has_secret"]:
                hard.append("deploy-trigger webhook has no secret "
                            "(the orchestrator rejects unsigned deliveries)")
            if want_event not in (h["events"] or []):
                hard.append(f"deploy-trigger webhook events={h['events']} lack "
                            f"'{want_event}' (it never fires, so deploys never "
                            "trigger)")
            elif set(h["events"]) != {want_event}:
                warn.append(f"deploy-trigger webhook events={h['events']} "
                            f"(want exactly ['{want_event}'])")
            if h["insecure_ssl"] != "0":
                hard.append(f"deploy-trigger webhook insecure_ssl="
                            f"{h['insecure_ssl']!r} (TLS verification disabled; "
                            "want '0')")
            if h["content_type"] != "json":
                warn.append(f"deploy-trigger webhook content_type="
                            f"{h['content_type']} (want json; the orchestrator "
                            "parses a JSON body)")
            if h["bad_delivery"]:
                warn.append(f"deploy-trigger webhook last delivery failed "
                            f"(HTTP {h['bad_delivery']})")

    # Filter known-accepted deviations (warnings only — a HARD failure is
    # never silently acceptable) so the steady-state report is clean.
    rules = ACCEPTED.get(s["name"], {})
    kept, accepted = [], []
    for w in warn:
        if any(w.startswith(prefix) for prefix in rules):
            accepted.append(w)
        else:
            kept.append(w)

    return hard, kept, accepted


def main():
    ap = argparse.ArgumentParser(description="cplieger governance audit")
    ap.add_argument("--visibility", choices=["all", "public", "private"], default="all")
    ap.add_argument("--repo", action="append", metavar="NAME",
                    help="audit only this repo (repeatable). The bootstrap-repo "
                         "skill runs this against a freshly created repo as its "
                         "settings gate.")
    ap.add_argument("--dump", metavar="PATH", help="write raw collected settings as JSON")
    args = ap.parse_args()

    r, definitive = gh_retry("repo", "list", OWNER, "--limit", "300",
                             "--json", "name,isArchived,visibility,isFork")
    if r.returncode != 0 or not definitive:
        sys.stderr.write(f"gh repo list failed: {r.stderr}\n")
        sys.exit(2)
    all_metas = json.loads(r.stdout)
    # Forks exist to carry upstream PRs: they keep upstream's merge model,
    # branch protection, and go.mod module path, so the governance standard
    # does not apply. Skipped with a visible note, never audited.
    forks = sorted(m["name"] for m in all_metas if m.get("isFork") and not m["isArchived"])
    metas = [m for m in all_metas if not m["isArchived"] and not m.get("isFork")]
    if args.visibility != "all":
        metas = [m for m in metas if (m.get("visibility") or "").lower() == args.visibility]
    if args.repo:
        want = set(args.repo)
        known = {m["name"] for m in metas}
        unknown = sorted(want - known)
        if unknown:
            sys.stderr.write(f"error: unknown (or archived/fork/filtered) repo(s): "
                             f"{', '.join(unknown)}\n")
            sys.exit(2)
        metas = [m for m in metas if m["name"] in want]
    metas.sort(key=lambda m: m["name"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        settings = list(pool.map(collect, metas))

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2, sort_keys=True)

    # Total-outage guard: if not a single repo object could be read, this is
    # network/API trouble, not a token problem — bail before the under-scope
    # guard below misdiagnoses it.
    if settings and all(s.get("fatal") for s in settings):
        sys.stderr.write(
            "ERROR: no repo could be read at all (API outage or network "
            "failure). No compliance conclusions drawn.\n"
        )
        sys.exit(2)

    # Permission guard: the merge-model, branch-protection, and security checks
    # need a token with admin read across the cplieger repos. If NO repo returned
    # admin-scoped fields, the token is under-scoped (e.g. the default
    # GITHUB_TOKEN instead of AUDIT_PAT) — abort rather than flag every repo as
    # non-compliant, which would be a false-negative storm masking real drift.
    if settings and not any(s["admin_visible"] for s in settings):
        sys.stderr.write(
            "ERROR: no repo returned the merge-model fields (allow_merge_commit "
            "etc.). The audit token is under-scoped.\n"
            "These fields are only exposed to a CLASSIC PAT with the 'repo' "
            "scope. A fine-grained PAT does NOT serialize them, even with "
            "Administration:read and an owner role. Set the AUDIT_PAT secret to "
            "a classic PAT with 'repo' scope. The default GITHUB_TOKEN also "
            "cannot read these fields.\n"
        )
        sys.exit(2)

    hard_total, warn_total, accepted_total, error_total, clean = 0, 0, 0, 0, 0
    scope = f", repos={','.join(sorted(set(args.repo)))}" if args.repo else ""
    print(f"GOVERNANCE COMPLIANCE — {len(settings)} repos "
          f"(visibility={args.visibility}{scope})")
    print("Legend: [HARD] blocks compliance · [warn] advisory · [error] API "
          "read failed (check skipped) · GHAS scanning N/A on free private "
          "repos · accepted deviations suppressed (see ACCEPTED)\n")

    # Deploy-trigger webhook check status. When the host is configured but no
    # repo's hooks were readable, the token is under-scoped for the hook endpoint
    # (needs classic 'repo' or admin:repo_hook) — surface it instead of silently
    # skipping. When the host is unset, the check does not run at all.
    if forks:
        print(f"Note: {len(forks)} fork(s) skipped (upstream governance applies): "
              f"{', '.join(forks)}\n")
    if not WEBHOOK_HOST:
        print("Note: deploy-trigger webhook check skipped (AUDIT_WEBHOOK_HOST unset).\n")
    elif not any(s["webhook_readable"] for s in settings):
        print("WARNING: AUDIT_WEBHOOK_HOST is set but no repo's webhooks were "
              "readable; the deploy-trigger webhook check was skipped. The audit "
              "token needs the classic 'repo' scope (or admin:repo_hook).\n")
    # Used-by counter check status: when EVERY attempted dependents-page read
    # failed, github.com HTML is unreachable from this network (throttled or
    # blocked) — say so once instead of silently skipping fleet-wide.
    attempted = [s for s in settings if s.get("used_by_attempted")]
    if attempted and not any(s["used_by_readable"] for s in attempted):
        print("Note: used-by counter check skipped (github.com dependents "
              "pages unreadable from this network).\n")
    for s in settings:
        hard, warn, accepted = compliance(s)
        errors = s.get("errors") or []
        accepted_total += len(accepted)
        tag = "infra" if s["infra"] else ("priv" if s["private"] else "pub")
        if not hard and not warn and not errors:
            clean += 1
            continue
        print(f"{s['name']}  ({tag})")
        for h in hard:
            print(f"  [HARD] {h}")
            hard_total += 1
        for w in warn:
            print(f"  [warn] {w}")
            warn_total += 1
        for e in errors:
            print(f"  [error] {e}")
            error_total += 1
        print()

    print("-" * 60)
    print(f"{clean} clean · {hard_total} hard failures · {warn_total} warnings"
          f" · {accepted_total} accepted deviations · {error_total} API errors")
    if hard_total:
        sys.exit(1)
    if error_total:
        # No compliance failures, but the audit could not fully verify some
        # checks — infra trouble, not drift. Distinct exit code so the weekly
        # run goes red for the right reason.
        sys.exit(2)


if __name__ == "__main__":
    main()
