#!/usr/bin/env python3
"""Cross-repo governance audit for the cplieger account.

Polls every (non-archived) repo for its full settings surface and reports a
compliance report against the documented standard in
.kiro/steering/repo-governance.md, split into HARD failures and soft WARNINGS,
with N/A handling where a setting cannot apply (e.g. GitHub Advanced Security
features on free private repos).

Checks cover merge model, repo features, branch protection, rulesets
(unexpected custom rulesets + bypass-actor drift), vulnerability reporting,
secret/code scanning, CI wiring, Renovate preset, license, default branch,
description (presence + <=100 chars), topics, and the per-repo deploy-trigger
webhook that reaches the self-hosted orchestrator (only when AUDIT_WEBHOOK_HOST
is set — the host is private infra, injected via env, never hardcoded here).

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
from concurrent.futures import ThreadPoolExecutor

OWNER = "cplieger"
PRESET = "github>cplieger/.github"
REUSABLE = "cplieger/ci/.github/workflows"
INFRA = {".github", "ci"}  # they define the standard; CI-wiring check is N/A for them
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
GOV_SOFT = {
    "has_wiki": False,
    "has_projects": False,
    "has_issues": True,
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
              "has_wiki", "has_projects", "has_issues", "has_discussions"):
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
        s["strict"] = rsc.get("strict")
        s["enforce_admins"] = (prot.get("enforce_admins") or {}).get("enabled")
        s["allow_force_pushes"] = (prot.get("allow_force_pushes") or {}).get("enabled")
        s["allow_deletions"] = (prot.get("allow_deletions") or {}).get("enabled")
    else:
        s["required_checks"] = []
        s["strict"] = s["enforce_admins"] = s["allow_force_pushes"] = s["allow_deletions"] = None

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

    wf = gh_json("api", f"repos/{OWNER}/{name}/contents/.github/workflows")
    if wf is API_ERROR:
        s["has_codeql"] = s["has_security_scan"] = s["has_scorecard"] = None
        s["errors"].append("workflow listing unreadable (API)")
    else:
        wf_names = {f["name"] for f in wf} if isinstance(wf, list) else set()
        s["has_codeql"] = bool({"codeql.yml", "codeql.yaml"} & wf_names)
        s["has_security_scan"] = bool({"security.yml", "security.yaml"} & wf_names)
        s["has_scorecard"] = bool({"scorecard.yml", "scorecard.yaml"} & wf_names)

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

    # Deploy-trigger webhook. Read repo hooks and look for an active one pointing
    # at the orchestrator host. webhook_readable distinguishes "no matching hook"
    # (readable, empty/other hooks) from "could not read hooks" (token lacks the
    # classic 'repo'/hook scope) so the latter is a global skip, not per-repo
    # false failures. Only meaningful when WEBHOOK_HOST is set.
    s["webhook_readable"] = False
    s["webhook_present"] = False
    s["webhook_has_secret"] = False
    s["webhook_bad_delivery"] = None
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
                s["webhook_present"] = True
                if cfg.get("secret"):
                    s["webhook_has_secret"] = True
                code = (h.get("last_response") or {}).get("code")
                if isinstance(code, int) and code >= 400:
                    s["webhook_bad_delivery"] = code
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
    # that needs a license grant.
    if not s["private"] and s["license"] is None:
        (hard if s["adopted"] else warn).append("license missing")

    if s["has_protection"] is False:
        hard.append("no branch protection on default branch")
    elif s["has_protection"]:
        # App repos surface 'ci / validate' (the cplieger/ci meta job); repos
        # with a local CI surface a bare 'validate'. Accept either.
        if not any("validate" in (c or "") for c in (s["required_checks"] or [])):
            hard.append(f"required checks={s['required_checks']} (want a 'validate' check)")
        if s["strict"]:
            warn.append("branch protection strict=on (want off)")
        if s["enforce_admins"]:
            warn.append("enforce_admins=on (want off)")
        if s["allow_force_pushes"]:
            warn.append("allow_force_pushes=on (want off)")
        if s["allow_deletions"]:
            warn.append("allow_deletions=on (want off)")

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

    # ci_wired is None when the contents read failed (already an [error]);
    # only a DEFINITIVE "file exists without the reusable ref / file absent"
    # is a hard failure.
    if not s["infra"] and s["adopted"] and s["ci_wired"] is False:
        hard.append("CI not wired to cplieger/ci")
    if s["renovate_preset"] is False:
        warn.append("renovate preset not extended")

    # Description + topics: public repos only — discovery metadata has no
    # audience on a private repo.
    if not s["private"]:
        if not s["desc_present"]:
            warn.append("description empty")
        elif s["desc_len"] > 100:
            warn.append(f"description {s['desc_len']} chars (>100; Docker Hub short-desc limit)")
        if not s["topics"]:
            warn.append("no topics")

    # Deploy-trigger webhook. Enforced only when the host is configured AND this
    # repo's hooks were readable (see collect); an unreadable token is handled as
    # a global skip in main(), not as per-repo failures. Every non-archived repo
    # is expected to reach the orchestrator: a missing hook means releases never
    # propagate; a hook without a secret is rejected (the orchestrator validates
    # an HMAC signature over the payload), which is a silent, deploy-breaking gap.
    if WEBHOOK_HOST and s["webhook_readable"]:
        if not s["webhook_present"]:
            hard.append("no deploy-trigger webhook (releases won't reach the orchestrator)")
        elif not s["webhook_has_secret"]:
            hard.append("deploy-trigger webhook has no secret "
                        "(the orchestrator rejects unsigned deliveries)")
        elif s["webhook_bad_delivery"]:
            warn.append(f"deploy-trigger webhook last delivery failed "
                        f"(HTTP {s['webhook_bad_delivery']})")

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
    ap.add_argument("--dump", metavar="PATH", help="write raw collected settings as JSON")
    args = ap.parse_args()

    r, definitive = gh_retry("repo", "list", OWNER, "--limit", "300",
                             "--json", "name,isArchived,visibility")
    if r.returncode != 0 or not definitive:
        sys.stderr.write(f"gh repo list failed: {r.stderr}\n")
        sys.exit(2)
    metas = [m for m in json.loads(r.stdout) if not m["isArchived"]]
    if args.visibility != "all":
        metas = [m for m in metas if (m.get("visibility") or "").lower() == args.visibility]
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
    print(f"GOVERNANCE COMPLIANCE — {len(settings)} repos "
          f"(visibility={args.visibility})")
    print("Legend: [HARD] blocks compliance · [warn] advisory · [error] API "
          "read failed (check skipped) · GHAS scanning N/A on free private "
          "repos · accepted deviations suppressed (see ACCEPTED)\n")

    # Deploy-trigger webhook check status. When the host is configured but no
    # repo's hooks were readable, the token is under-scoped for the hook endpoint
    # (needs classic 'repo' or admin:repo_hook) — surface it instead of silently
    # skipping. When the host is unset, the check does not run at all.
    if not WEBHOOK_HOST:
        print("Note: deploy-trigger webhook check skipped (AUDIT_WEBHOOK_HOST unset).\n")
    elif not any(s["webhook_readable"] for s in settings):
        print("WARNING: AUDIT_WEBHOOK_HOST is set but no repo's webhooks were "
              "readable; the deploy-trigger webhook check was skipped. The audit "
              "token needs the classic 'repo' scope (or admin:repo_hook).\n")
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
