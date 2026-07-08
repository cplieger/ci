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

Exits non-zero if ANY repo has a HARD failure; warnings never fail the run.

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
import subprocess
import sys
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


def gh_json(*args):
    r = gh(*args)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def api_status_ok(path):
    """For endpoints that signal via HTTP status (204 enabled -> rc 0, 404 -> rc !=0)."""
    return gh("api", path).returncode == 0


def file_text(repo, path):
    r = gh("api", f"repos/{OWNER}/{repo}/contents/{path}", "--jq", ".content")
    if r.returncode != 0:
        return None
    try:
        return base64.b64decode("".join(r.stdout.split())).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return ""


def collect(meta):
    """Gather the full governance-relevant settings surface for one repo."""
    name = meta["name"]
    s = {"name": name, "infra": name in INFRA}
    repo = gh_json("api", f"repos/{OWNER}/{name}") or {}
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

    s["vuln_alerts"] = api_status_ok(f"repos/{OWNER}/{name}/vulnerability-alerts")
    pvr = gh_json("api", f"repos/{OWNER}/{name}/private-vulnerability-reporting") or {}
    s["private_vuln_reporting"] = pvr.get("enabled")

    prot = gh_json("api", f"repos/{OWNER}/{name}/branches/{branch}/protection")
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
    for rs in rulesets if isinstance(rulesets, list) else []:
        rname = rs.get("name", "")
        full = gh_json("api", f"repos/{OWNER}/{name}/rulesets/{rs.get('id')}") or {}
        if rname not in MANAGED_RULESETS:
            s["custom_rulesets"].append({"name": rname, "enforcement": full.get("enforcement")})
        for a in full.get("bypass_actors") or []:
            s["ruleset_bypass_actors"].append((rname, a.get("actor_type"), a.get("actor_id")))

    wf = gh_json("api", f"repos/{OWNER}/{name}/contents/.github/workflows")
    wf_names = {f["name"] for f in wf} if isinstance(wf, list) else set()
    s["has_codeql"] = bool({"codeql.yml", "codeql.yaml"} & wf_names)
    s["has_security_scan"] = bool({"security.yml", "security.yaml"} & wf_names)
    s["has_scorecard"] = bool({"scorecard.yml", "scorecard.yaml"} & wf_names)

    ci_txt = file_text(name, ".github/workflows/ci.yaml") or ""
    s["ci_wired"] = REUSABLE in ci_txt
    ren = "".join(file_text(name, p) or "" for p in
                  ("renovate.json", "org-inherited-config.json", "default.json"))
    s["renovate_preset"] = (PRESET in ren) or (name == ".github")
    s["adopted"] = s["ci_wired"] or s["renovate_preset"]

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
    """Return (hard_failures, warnings) for one repo's settings dict."""
    hard, warn = [], []

    for k, exp in GOV_HARD.items():
        if s.get(k) != exp:
            hard.append(f"{k}={s.get(k)} (want {exp})")
    for k, exp in GOV_SOFT.items():
        if s.get(k) != exp:
            warn.append(f"{k}={s.get(k)} (want {exp})")

    if s["default_branch"] != "main":
        hard.append(f"default_branch={s['default_branch']} (want main)")

    if s["license"] is None:
        (hard if s["adopted"] else warn).append("license missing")

    if not s["has_protection"]:
        hard.append("no branch protection on default branch")
    else:
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

    if not s["vuln_alerts"]:
        hard.append("dependabot vulnerability alerts off (want on)")
    # Private vulnerability reporting is a public-repo feature; N/A on private.
    if not s["private"] and not s["private_vuln_reporting"]:
        hard.append("private vulnerability reporting off (want on)")
    if s["dependabot_security_updates"] == "enabled":
        hard.append("dependabot security UPDATES on (want off; Renovate owns deps)")

    # Secret scanning / push protection: free on public repos; needs GHAS on
    # private (N/A on the free plan), so only enforced on public repos.
    if not s["private"]:
        if s["secret_scanning"] != "enabled":
            warn.append("secret scanning off (want on)")
        if s["secret_scanning_push_protection"] != "enabled":
            warn.append("secret scanning push protection off (want on)")

    # Scanning workflows arrive via sync for adopted repos; scorecard is public-only.
    if s["adopted"] and not s["infra"]:
        if not s["has_codeql"]:
            warn.append("codeql.yml missing")
        if not s["has_security_scan"]:
            warn.append("security.yml missing")
        if not s["private"] and not s["has_scorecard"]:
            warn.append("scorecard.yml missing")

    if not s["infra"] and s["adopted"] and not s["ci_wired"]:
        hard.append("CI not wired to cplieger/ci")
    if not s["renovate_preset"]:
        warn.append("renovate preset not extended")

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

    return hard, warn


def main():
    ap = argparse.ArgumentParser(description="cplieger governance audit")
    ap.add_argument("--visibility", choices=["all", "public", "private"], default="all")
    ap.add_argument("--dump", metavar="PATH", help="write raw collected settings as JSON")
    args = ap.parse_args()

    r = gh("repo", "list", OWNER, "--limit", "200", "--json", "name,isArchived,visibility")
    if r.returncode != 0:
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

    hard_total, warn_total, clean = 0, 0, 0
    print(f"GOVERNANCE COMPLIANCE — {len(settings)} repos "
          f"(visibility={args.visibility})")
    print("Legend: [HARD] blocks compliance · [warn] advisory · "
          "GHAS scanning N/A on free private repos\n")

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
        hard, warn = compliance(s)
        tag = "infra" if s["infra"] else ("priv" if s["private"] else "pub")
        if not hard and not warn:
            clean += 1
            continue
        print(f"{s['name']}  ({tag})")
        for h in hard:
            print(f"  [HARD] {h}")
            hard_total += 1
        for w in warn:
            print(f"  [warn] {w}")
            warn_total += 1
        print()

    print("-" * 60)
    print(f"{clean} clean · {hard_total} hard failures · {warn_total} warnings")
    if hard_total:
        sys.exit(1)


if __name__ == "__main__":
    main()
