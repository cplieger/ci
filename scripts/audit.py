#!/usr/bin/env python3
"""Cross-repo governance audit for the cplieger account.

Polls every (non-archived) repo for its full settings surface and reports a
compliance report against the documented standard in
.kiro/steering/repo-governance.md, split into HARD failures and soft WARNINGS,
with N/A handling where a setting cannot apply (e.g. GitHub Advanced Security
features on free private repos).

Checks cover merge model, repo features, branch protection, vulnerability
reporting, secret/code scanning, CI wiring, Renovate preset, license, default
branch, description (presence + <=100 chars), and topics.

Exits non-zero if ANY repo has a HARD failure; warnings never fail the run.

Requires `gh` authenticated with admin on the repos: branch protection and the
security endpoints need admin, and private repos need a token with repo scope.

Run:
  scripts/audit.py                  # full fleet (public + private)
  scripts/audit.py --visibility public
  scripts/audit.py --dump out.json  # also write raw collected settings as JSON
"""

import argparse
import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

OWNER = "cplieger"
PRESET = "github>cplieger/.github"
REUSABLE = "cplieger/ci/.github/workflows"
INFRA = {".github", "ci"}  # they define the standard; CI-wiring check is N/A for them

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
        # with a local CI (homelab, .kiro) surface a bare 'validate'. Accept either.
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

    return hard, warn


def main():
    ap = argparse.ArgumentParser(description="cplieger fleet governance audit")
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

    hard_total, warn_total, clean = 0, 0, 0
    print(f"GOVERNANCE COMPLIANCE — {len(settings)} repos "
          f"(visibility={args.visibility})")
    print("Legend: [HARD] blocks compliance · [warn] advisory · "
          "GHAS scanning N/A on free private repos\n")
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
