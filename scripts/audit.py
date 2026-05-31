#!/usr/bin/env python3
"""Cross-public-repo compliance audit for the cplieger account.

Lists every public repo and checks shared-standard compliance:
  hard (fail if an *adopted* repo is missing): license, default branch = main,
        CI wired to cplieger/ci, renovate extending the shared preset.
  soft (report only): description, topics.

A repo counts as "adopted" if it references cplieger/ci (via the reusable
workflow or the renovate preset). Legacy repos that have not adopted the
standard are reported for visibility but never fail the audit.

Requires `gh` authenticated. Run: scripts/audit.py
"""

import base64
import json
import subprocess
import sys

OWNER = "cplieger"
PRESET = "github>cplieger/ci"
REUSABLE = "cplieger/ci/.github/workflows"
HARD = ["license", "main", "ci", "renovate"]
SOFT = ["desc", "topics"]


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def file_contents(repo, path):
    r = gh("api", f"repos/{OWNER}/{repo}/contents/{path}", "--jq", ".content")
    if r.returncode != 0:
        return None
    try:
        return base64.b64decode("".join(r.stdout.split())).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return ""


def main():
    r = gh("repo", "list", OWNER, "--visibility", "public", "--limit", "100", "--json",
           "name,description,defaultBranchRef,repositoryTopics,licenseInfo,isArchived")
    if r.returncode != 0:
        sys.stderr.write(f"gh repo list failed: {r.stderr}\n")
        sys.exit(2)
    skip = {".github", "ci"}  # infrastructure repos: they define the standard
    repos = [x for x in json.loads(r.stdout) if not x["isArchived"] and x["name"] not in skip]

    rows, failures = [], []
    for repo in sorted(repos, key=lambda x: x["name"]):
        name = repo["name"]
        ci = file_contents(name, ".github/workflows/ci.yaml") or ""
        renovate = file_contents(name, "renovate.json") or ""
        checks = {
            "desc": bool((repo.get("description") or "").strip()),
            "license": repo.get("licenseInfo") is not None,
            "main": (repo.get("defaultBranchRef") or {}).get("name") == "main",
            "topics": len(repo.get("repositoryTopics") or []) > 0,
            "ci": REUSABLE in ci,
            "renovate": PRESET in renovate,
        }
        adopted = checks["ci"] or checks["renovate"]
        rows.append((name, adopted, checks))
        if adopted:
            missing = [k for k in HARD if not checks[k]]
            if missing:
                failures.append((name, missing))

    cols = HARD + SOFT
    print(f"{'repo':24} {'adopted':8}" + "".join(f"{c:>10}" for c in cols))
    print("-" * (24 + 8 + 10 * len(cols)))
    for name, adopted, checks in rows:
        cells = "".join(f"{('ok' if checks[c] else '·'):>10}" for c in cols)
        print(f"{name:24} {('yes' if adopted else 'no'):8}{cells}")

    adopted_n = sum(1 for _, a, _ in rows if a)
    print(f"\n{len(rows)} public repos · {adopted_n} adopted the shared standard")
    if failures:
        print("\nNON-COMPLIANT (adopted repos missing hard requirements):")
        for name, missing in failures:
            print(f"  {name}: missing {', '.join(missing)}")
        sys.exit(1)
    print("All adopted repos satisfy the hard requirements.")


if __name__ == "__main__":
    main()
