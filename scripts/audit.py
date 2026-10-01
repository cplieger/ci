#!/usr/bin/env python3
"""Cross-repo governance audit for the cplieger account.

Grades every non-archived, non-fork repo against the governance standard as
HARD failures and soft WARNINGS: merge model, branch protection or the dev and
main rulesets, Actions token defaults, scanning, CI wiring, publish secrets,
version-tag receipts, the deploy webhook and the public-repo cosmetics;
CONTRIBUTING.md "Cross-repo audit" lists the surface. Deviations in the
ACCEPTED table are counted, not listed, and a transient API failure skips its
check as an [error] line rather than producing a finding.

Exit codes: 0 compliant; 1 at least one HARD failure; 2 usage or infra (an
under-scoped token, or API errors that prevented a full audit).
Needs `gh` authenticated with a CLASSIC PAT carrying the `repo` scope: a
fine-grained PAT does not serialize the merge-model fields, so the audit aborts.
Run: scripts/audit.py [--visibility public] [--repo <name>]... [--dump out.json]
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import release_channels

OWNER = "cplieger"
PRESET = "github>cplieger/.github"
REUSABLE = "cplieger/ci/.github/workflows"
INFRA = {".github", "ci"}  # they define the standard; CI-wiring check is N/A for them
# Repos with a deliberate repo-local CI instead of the reusable-workflow thin
# caller: they validate surfaces the shared workflows don't cover and gate on a
# bespoke `validate` job (which the branch-protection check accepts as the bare
# 'validate' context). The CI-wiring check is N/A for them, same as INFRA.
# AWS is a private workspace repo (markdown, Python, Office templates) with no
# compiled surface for the shared workflows to act on; its `validate` job is a
# lint plus secret plus data-boundary gate.
BESPOKE_CI = {".kiro", "homelab", "AWS"}
# Host of the self-hosted deploy/dependency orchestrator that each repo pings
# via a per-repo webhook (a release, or a push on infra repos, reaches it so the
# change redeploys / re-runs dependency updates). It is private infrastructure,
# so it is injected via the AUDIT_WEBHOOK_HOST env/secret rather than hardcoded
# in this public repo. When unset, the webhook check is skipped entirely so a
# local run without the secret does not report every repo as non-compliant.
WEBHOOK_HOST = os.environ.get("AUDIT_WEBHOOK_HOST", "").strip()
# GitHub auto-creates and manages this ruleset when code-scanning merge
# protection is enabled. It is not user-authored, so it is whitelisted from the
# "unexpected custom ruleset" check. Every other ruleset is drift on a
# single-main repo; a two-channel repo must carry exactly the two below.
MANAGED_RULESETS = {"code-scanning-merge-protection"}
RULESETS_DIR = Path(__file__).resolve().parent.parent / "configs" / "rulesets"
CHANNEL_RULESETS = ("dev", "main")
# The deploy-trigger event a two-channel repo's hook must carry: a dev build
# creates no GitHub Release, so the orchestrator learns about a new image from
# the package push instead.
REGISTRY_EVENT = "registry_package"
_expected_rulesets_cache = {}


def expected_ruleset(name):
    """The committed ruleset body for `name`, read once."""
    if name not in _expected_rulesets_cache:
        with open(RULESETS_DIR / f"{name}.json", encoding="utf-8") as fh:
            _expected_rulesets_cache[name] = json.load(fh)
    return _expected_rulesets_cache[name]


def ruleset_matches(expected, actual):
    """Reasons the live ruleset `actual` differs from the committed body
    `expected`; empty when it matches. Ids, timestamps, links, `source`,
    `current_user_can_bypass` and parameter keys the API fills in on its own
    (allowed_merge_methods and the like) are ignored on purpose."""
    reasons = []
    for key in ("enforcement", "target"):
        if actual.get(key) != expected.get(key):
            reasons.append(f"{key}={actual.get(key)!r} (want {expected.get(key)!r})")
    # Both condition lists decide which branches the rules reach: an exclude
    # naming the protected branch disables the ruleset while the include still
    # matches, and a condition key other than ref_name changes the targeting.
    want_cond = expected.get("conditions") or {}
    got_cond = actual.get("conditions") or {}
    if set(got_cond) != set(want_cond):
        reasons.append(f"condition keys {sorted(got_cond)} (want {sorted(want_cond)})")
    want_ref = want_cond.get("ref_name") or {}
    got_ref = got_cond.get("ref_name") or {}
    for key in ("include", "exclude"):
        want_list, got_list = set(want_ref.get(key) or []), set(got_ref.get(key) or [])
        if want_list != got_list:
            reasons.append(f"branch {key} {sorted(got_list)} (want {sorted(want_list)})")
    extra_ref_keys = set(got_ref) - {"include", "exclude"}
    if extra_ref_keys:
        reasons.append(f"unexpected ref_name keys {sorted(extra_ref_keys)}")
    want_rules = {r["type"]: r for r in expected.get("rules") or []}
    got_rules = {r.get("type"): r for r in actual.get("rules") or []}
    if set(want_rules) != set(got_rules):
        reasons.append(f"rule types {sorted(got_rules)} (want {sorted(want_rules)})")
    for rtype, want in want_rules.items():
        got = got_rules.get(rtype)
        if got is None:
            continue
        got_params = got.get("parameters") or {}
        for k, v in (want.get("parameters") or {}).items():
            if got_params.get(k) != v:
                reasons.append(f"rule {rtype} parameter {k}={got_params.get(k)!r} (want {v!r})")
    got_actors, want_actors = _bypass_actor_set(actual), _bypass_actor_set(expected)
    if got_actors != want_actors:
        reasons.append(f"bypass actors {sorted(got_actors, key=str)} (want {sorted(want_actors, key=str)})")
    return reasons


def _bypass_actor_set(ruleset):
    return {(a.get("actor_type"), a.get("actor_id"), a.get("bypass_mode"))
            for a in ruleset.get("bypass_actors") or []}
# Repos whose deploy-trigger webhook fires on push@main instead of release:
# the non-releaseable infra/config repos (repo-governance.md "Apply"). Every
# other repo releases, so its hook must carry the release event or releases
# silently never reach the orchestrator.
PUSH_WEBHOOK_REPOS = {".github", ".kiro", "ci", "homelab"}
# Repos with NO orchestrator relationship at all, so no deploy-trigger webhook
# is expected. Distinct from PUSH_WEBHOOK_REPOS, which only changes WHICH event
# a hook must carry: a missing hook is still a HARD failure there, correctly, so
# membership in that set cannot express "this repo should have none".
#
# The webhook check is a denylist by design (every repo needs a hook unless
# named here), because that direction fails safe: a new app repo that genuinely
# needs one is flagged rather than silently exempt. Adding a name is therefore a
# deliberate statement that the repo neither deploys nor triggers dependency
# runs, and the reason belongs in the comment below.
#
# AWS is a private work workspace. It ships nothing, cuts no release, and must
# not point a webhook at the self-hosted orchestrator.
NO_DEPLOY_HOOK = {"AWS"}
# Every image repo (root Dockerfile) dual-publishes to GHCR + Docker Hub, so
# every one of them needs the DOCKERHUB_* secrets. There is no GHCR-only
# exemption set any more: subflux and marotte were the last two, and their
# carve-out outlived the intent by ~20 releases while both READMEs advertised a
# Docker Hub image the pipeline had stopped pushing. A future exemption needs a
# skip here AND a policy override in .github/workflows/release.yaml.
# The required check every repo carries, and the GitHub App expected to report
# it (15368 = GitHub Actions). A validate context restricted to a different
# app — or to none (-1, any app may report it) — weakens the gate: an
# arbitrary integration could satisfy the merge requirement.
ACTIONS_APP_ID = 15368

# Public docs standard ("Canonical README footer"). The two footer blocks are
# matched as whitespace-normalized substrings so a reflow never false-positives; the
# TEXT itself must stay verbatim. `.github` is the documented exception
# (AI note only, no Disclaimer). Docker Hub hard-caps the mirrored full
# description at 25,000 bytes and the sync action truncates the overflow,
# so an image-repo README above the cap ships a truncated Hub page (the
# footer is what falls off) — that ceiling is a HARD check.
FOOTER_DISCLAIMER = (
    "This project is built with care and follows security best practices, "
    "but it is intended for personal / self-hosted use. No guarantees of "
    "fitness for production environments. Use at your own risk."
)
FOOTER_AI_NOTE = (
    "This project was built with AI-assisted tooling using "
    "[Claude](https://claude.com), [GPT](https://openai.com), and "
    "[Kiro](https://kiro.dev). The human maintainer defines architecture, "
    "supervises implementation, and makes all final decisions."
)
# Docker Hub gets a short generated overview page, not the README, so a README
# carries no byte budget. What an image repo must have instead is the marker pair
# the renderer extracts its summary from (ci/actions/render-hub-overview).
HUB_MARKER_BEGIN = "<!-- hub-overview BEGIN -->"
HUB_MARKER_END = "<!-- hub-overview END -->"

# Known-accepted deviations from the standard: {repo: {warning-prefix: reason}}.
# A warning whose text starts with a listed prefix is suppressed from the
# report (counted under "accepted", not listed), so the steady-state fleet
# reports clean and a new warning stands out. Every entry needs a reason;
# remove entries when the deviation is fixed.
ACCEPTED = {
    "homelab": {
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

# Expected license per repo (licensing.md's four-license scheme, 2026-08).
# Apache-2.0 is the default and covers every public repo not listed here:
# importable libraries, wrappers whose core value is the upstream software they
# package, and tooling/config/meta. The listed repos deviate for a stated
# reason. A repo absent from this table is NOT unclassified — the default
# applies — so a new repo needs an entry only when it is not Apache-2.0.
#
# Values are the spdx_id GitHub's licensee reports, which uses the legacy short
# IDs. The repos themselves declare GPL-3.0-or-later / AGPL-3.0-or-later in
# their READMEs and package.json; GitHub cannot distinguish the -only and
# -or-later variants from the license text, so it reports GPL-3.0 / AGPL-3.0
# and this table must match what the API returns, not what the repo declares.
LICENSE_DEFAULT = "Apache-2.0"
LICENSE_OVERRIDES = {
    # The differentiated product. File-level copyleft asks for improvements
    # back while keeping the component embeddable in a closed larger work.
    "web-terminal-engine": "MPL-2.0",
    "web-terminal-ui": "MPL-2.0",
    # Thin hosts over the engine + ui. Permissive here would be a bypass
    # around their copyleft: ship the already-wired app, change no covered
    # file, publish nothing. So they are at least as protective as what they
    # deploy.
    "web-terminal-kiro": "MPL-2.0",
    "web-terminal-server": "MPL-2.0",
    # First-party applications. Nobody imports an app, so it never enters a
    # consumer's dependency tree and copyleft costs no adoption.
    "cert-converter": "GPL-3.0",
    "github-scout": "GPL-3.0",
    "knell": "GPL-3.0",
    "plex-exporter": "GPL-3.0",
    "plex-language-sync": "GPL-3.0",
    "registry-stats": "GPL-3.0",
    "seadex-scout": "GPL-3.0",
    "tautulli-remap": "GPL-3.0",
    # The deadset dead-code analyzers and their orchestrator: command-line tools
    # run as separate processes, never linked, so the same rule applies. Their
    # shared contract repository (deadset-spec) stays on the Apache-2.0 default
    # so a third party can write a conforming analyzer freely.
    "deadset-go": "GPL-3.0",
    "deadset-ts": "GPL-3.0",
    "deadset": "GPL-3.0",
    # Network services a competitor could plausibly resell hosted, which is
    # the only thing AGPL section 13 buys over GPL-3.0. Rationed to these two
    # because section 13 is also what puts AGPL on corporate blocklists that
    # GPL-3.0 escapes.
    "subflux": "AGPL-3.0",
    "marotte": "AGPL-3.0",
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


def http_status(result):
    """The HTTP status gh reported on stderr (`gh: Not Found (HTTP 404)`),
    None when it reported none."""
    m = re.search(r"HTTP (\d{3})", result.stderr or "")
    return int(m.group(1)) if m else None


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
        code = http_status(r)
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


def gh_json_strict(*args):
    """Parsed JSON on success; None on an HTTP 404 alone; API_ERROR on every
    other failure, a success body that is not JSON or is JSON null included.
    For a read whose absence arm grades something: a 403 or 422 must not pass
    as absence, and neither may a null body."""
    r, definitive = gh_retry(*args)
    if not definitive:
        return API_ERROR
    if r.returncode != 0:
        return None if http_status(r) == 404 else API_ERROR
    try:
        body = json.loads(r.stdout)
    except json.JSONDecodeError:
        return API_ERROR
    return API_ERROR if body is None else body


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


# dest-in-consumer -> canonical-in-this-repo, for the synced files that carry no
# per-repo content and so must match their canonical exactly. Keyed on a root
# Dockerfile, which is the same condition classify-repos.py uses to decide who
# receives repin-sha.sh. Opt-in synced files (image-smoke.sh, shell/lib.sh) are
# deliberately absent: a repo that never opted in has no copy, and "absent" must
# not read as drift.
SYNCED_BYTE_IDENTICAL = {
    "scripts/repin-sha.sh": "configs/repin-sha.sh",
    "scripts/collect-licenses.sh": "configs/collect-licenses.sh",
}

_canonical_cache = {}


def canonical_text(path):
    """This repo's own copy of a synced canonical, read once per run.

    Read through the API rather than off disk on purpose: the audit compares
    against what is actually on ci's default branch, so a run from a feature
    branch or a stale checkout cannot report the fleet as drifted against an
    unpublished canonical.
    """
    if path not in _canonical_cache:
        _canonical_cache[path] = file_text("ci", path)
    return _canonical_cache[path]



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
    req = urllib.request.Request(url, headers={"User-Agent": AUDIT_UA})  # fixed https:// URL, host is github.com
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


# How many of the newest tags of each shape are graded for provenance.
PROVENANCE_TAGS = 5
# Every publishing path creates the tag ref before its receipt (the Release or
# the status), so a tag on a commit younger than this is not graded: an audit
# overlapping a live release must not open a false failure issue.
RECEIPT_GRACE = timedelta(hours=2)


def tag_page_fetcher(name, sha_by_tag):
    """A page reader for release_channels.collect_all_tags that also records
    each tag's commit; None from a page means the API failed after retries."""
    def fetch(page):
        tags = gh_json("api", f"repos/{OWNER}/{name}/tags?per_page={release_channels.TAG_PAGE_SIZE}&page={page}")
        if tags is API_ERROR:
            return None
        names_ = []
        for tg in tags if isinstance(tags, list) else []:
            tag = tg.get("name") or ""
            names_.append(tag)
            sha_by_tag[tag] = ((tg.get("commit") or {}).get("sha")) or ""
        return names_
    return fetch


def grade_stable_tags(tags, release_of):
    """(without_release, hand_made) over stable `tags`. `release_of(tag)` is the
    Release object, None when there is none, API_ERROR when unreadable (the tag
    is then skipped). A Release the release pipeline did not author means the
    tag and its Release were made by hand."""
    without, hand_made = [], []
    for tag in tags:
        rel = release_of(tag)
        if rel is API_ERROR:
            continue
        if rel is None:
            without.append(tag)
        elif not release_channels.release_is_pipeline_authored(rel):
            hand_made.append(tag)
    return without, hand_made


def dev_tags_without_receipt(tagged, statuses_of):
    """Dev tags whose commit carries no `release/tag/<tag>` success status.
    `tagged` is [(tag, sha)]; `statuses_of(sha)` is the commit's status list,
    API_ERROR when unreadable (the tag is then skipped)."""
    missing = []
    for tag, sha in tagged:
        if not sha:
            continue
        statuses = statuses_of(sha)
        if statuses is API_ERROR:
            continue
        if not release_channels.has_tag_receipt(tag, statuses):
            missing.append(tag)
    return missing


def commit_date(name, sha):
    """The committer date of `sha`, None when the response carries none,
    API_ERROR when unreadable."""
    data = gh_json("api", f"repos/{OWNER}/{name}/commits/{sha}")
    if data is API_ERROR:
        return API_ERROR
    iso = (((data or {}).get("commit") or {}).get("committer") or {}).get("date")
    return datetime.fromisoformat(iso) if iso else None


def workflow_runs(path):
    """The rows of the workflow-runs listing at `path`; None on an HTTP 404
    (a repo without the workflow file); API_ERROR on every other failure, a
    success body that is not a listing (an object carrying an integer
    `total_count` and a `workflow_runs` list) included."""
    body = gh_json_strict("api", path)
    if body is None or body is API_ERROR:
        return body
    if not isinstance(body, dict) or not isinstance(body.get("total_count"), int):
        return API_ERROR
    runs = body.get("workflow_runs")
    return runs if isinstance(runs, list) else API_ERROR


def release_in_flight(name, now):
    """Whether a run of the repo's tag-creating workflow is queued or in
    progress, or ended within RECEIPT_GRACE; API_ERROR when a read failed. The
    commit's age cannot show this: a promotion tags a commit that soaked for a
    day, so its tag is created outside the grace window and the receipt still
    follows. Other workflows are not read: the daily security dispatch puts a
    run inside the window in every repo, and it says nothing about a tag. A
    404 on the listing is a repo without the workflow file: no run, graded."""
    workflow = "publish.yaml" if name in release_channels.OWN_PUBLISH_REPOS else "release.yaml"
    runs_path = f"repos/{OWNER}/{name}/actions/workflows/{workflow}/runs"
    for status in ("in_progress", "queued"):
        runs = workflow_runs(f"{runs_path}?status={status}&per_page=1")
        if runs is API_ERROR:
            return API_ERROR
        if runs:
            return True
    runs = workflow_runs(f"{runs_path}?per_page=1")
    if runs is API_ERROR:
        return API_ERROR
    for run in runs or []:
        try:
            recent = abs(now - datetime.fromisoformat(run["updated_at"])) < RECEIPT_GRACE
        except (KeyError, TypeError, ValueError):
            return API_ERROR
        if recent:
            return True
    return False


def collect_version_tags(name, s, now=None):
    now = now or datetime.now(UTC)
    live = release_in_flight(name, now)
    if live is API_ERROR:
        s["errors"].append("workflow runs unreadable (API); version tags not graded")
        return
    if live:
        s["version_tags_deferred"] = True
        return
    sha_by_tag = {}
    try:
        names_ = release_channels.collect_all_tags(tag_page_fetcher(name, sha_by_tag))
    except release_channels.TagListingTruncatedError as err:
        s["errors"].append(f"tags listing truncated ({err}); version tags not graded")
        return
    if names_ is None:
        s["errors"].append("tags unreadable (API)")
        return

    def release_of(tag):
        rel = gh_json("api", f"repos/{OWNER}/{name}/releases/tags/{tag}")
        if rel is API_ERROR:
            s["errors"].append(f"release for tag {tag} unreadable (API)")
        return rel

    def statuses_of(sha):
        data = gh_json("api", f"repos/{OWNER}/{name}/commits/{sha}/status")
        if data is API_ERROR:
            s["errors"].append(f"statuses of {sha[:12]} unreadable (API)")
            return API_ERROR
        return (data or {}).get("statuses") or []

    def settled(tags):
        """The tags of `tags` whose commit is more than RECEIPT_GRACE from now
        in either direction; the younger ones are counted, a tag without a
        commit or with an unreadable one is skipped with an error."""
        kept = []
        for tag in tags:
            sha = sha_by_tag.get(tag, "")
            if not sha:
                s["errors"].append(f"tag {tag} carries no commit sha; not graded")
                continue
            date = commit_date(name, sha)
            if date is API_ERROR:
                s["errors"].append(f"commit of tag {tag} unreadable (API)")
            elif date is not None and abs(now - date) < RECEIPT_GRACE:
                s["tags_in_grace"] += 1
            else:
                kept.append(tag)
        return kept

    # Each lane (the root and every nested Go module) is graded on its own
    # newest tags, which is why the whole listing is read: a lane that has gone
    # quiet sits below any page that satisfies the root counts.
    s["stable_tags_without_release"], s["hand_made_stable_tags"] = [], []
    s["dev_tags_without_receipt"] = []
    s["tags_in_grace"] = 0
    for _lane, (stable, dev) in sorted(release_channels.tags_by_lane(names_).items()):
        without, hand_made = grade_stable_tags(settled(stable[:PROVENANCE_TAGS]), release_of)
        s["stable_tags_without_release"] += without
        s["hand_made_stable_tags"] += hand_made
        s["dev_tags_without_receipt"] += dev_tags_without_receipt(
            [(t, sha_by_tag.get(t, "")) for t in settled(dev[:PROVENANCE_TAGS])], statuses_of
        )


def collect_rulesets(name, s):
    """Every repository ruleset in full: the list endpoint returns only id, name
    and enforcement, and both the bypass actors (the stale-app rot class) and
    the two-channel comparison need the body. One failed read leaves the three
    ruleset keys None, so compliance() grades no ruleset finding from a partial
    list; a present ruleset must never be reported missing off a flaky read."""
    s["custom_rulesets"] = []
    s["ruleset_bypass_actors"] = []  # (ruleset_name, actor_type, actor_id)
    s["rulesets_full"] = {}
    rulesets = gh_json("api", f"repos/{OWNER}/{name}/rulesets")
    if rulesets is API_ERROR:
        s["errors"].append("rulesets unreadable (API); rulesets not graded")
        s["custom_rulesets"] = s["ruleset_bypass_actors"] = s["rulesets_full"] = None
        return
    for rs in rulesets if isinstance(rulesets, list) else []:
        rname = rs.get("name", "")
        full = gh_json("api", f"repos/{OWNER}/{name}/rulesets/{rs.get('id')}")
        if full is API_ERROR:
            s["errors"].append(f"ruleset '{rname}' unreadable (API); rulesets not graded")
            s["custom_rulesets"] = s["ruleset_bypass_actors"] = s["rulesets_full"] = None
            return
        full = full or {}
        s["rulesets_full"][rname] = full
        if rname not in MANAGED_RULESETS:
            s["custom_rulesets"].append({"name": rname, "enforcement": full.get("enforcement")})
        for a in full.get("bypass_actors") or []:
            s["ruleset_bypass_actors"].append((rname, a.get("actor_type"), a.get("actor_id")))


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
    s["two_channel"] = branch == "dev"
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

    # A two-channel repo must carry no classic protection on main either: the
    # main ruleset is what lets the promotion move the branch, and a classic
    # rule beside it would block or confuse that.
    s["main_protection"] = None
    if s["two_channel"]:
        mprot = gh_json("api", f"repos/{OWNER}/{name}/branches/main/protection")
        if mprot is API_ERROR:
            s["errors"].append("main branch protection unreadable (API)")
        else:
            s["main_protection"] = isinstance(mprot, dict) and "url" in mprot

    collect_rulesets(name, s)

    # With no classic protection, a two-channel repo's required contexts are
    # the dev ruleset's, and the phantom check below grades those.
    if s["two_channel"] and not s["has_protection"]:
        dev_rs = (s["rulesets_full"] or {}).get("dev") or {}
        for rule in dev_rs.get("rules") or []:
            if rule.get("type") != "required_status_checks":
                continue
            checks = (rule.get("parameters") or {}).get("required_status_checks") or []
            s["required_checks"] = [c.get("context") for c in checks if c.get("context")]
            s["required_check_apps"] = {c.get("context"): c.get("integration_id") for c in checks}

    # Version tags: the newest of each shape must carry the pipeline's receipt
    # (a bot-authored Release on stable, a release/tag status on dev), or a
    # hand-made tag becomes git-cliff's version base.
    s["stable_tags_without_release"] = []
    s["hand_made_stable_tags"] = []
    s["dev_tags_without_receipt"] = []
    if s["two_channel"]:
        collect_version_tags(name, s)

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
        s["has_codeql"] = s["has_security_scan"] = s["publishes"] = None
        s["errors"].append("workflow listing unreadable (API)")
    else:
        wf_names = {f["name"] for f in wf} if isinstance(wf, list) else set()
        s["has_codeql"] = bool({"codeql.yml", "codeql.yaml"} & wf_names)
        s["has_security_scan"] = bool({"security.yml", "security.yaml"} & wf_names)
        s["publishes"] = bool({"release.yaml", "release.yml", "publish.yaml", "publish.yml"} & wf_names)

    # Surface detection, mirroring scripts/classify-repos.py: a root Dockerfile
    # means the release pipeline publishes an image (and, for dual-publish
    # repos, needs the Docker Hub secrets).
    # go.mod and package.json are fetched for their TEXT (module path / package
    # name below); only the Dockerfile's presence is graded.
    probe_texts = {}
    for probe_file in ("go.mod", "package.json", "Dockerfile"):
        txt = file_text(name, probe_file)
        probe_texts[probe_file] = txt
        if txt is None:
            s["errors"].append(f"{probe_file} probe unreadable (API)")
    dockerfile = probe_texts["Dockerfile"]
    s["has_dockerfile"] = None if dockerfile is None else bool(dockerfile)

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

    # Docker Hub dual-publish secrets. Every image repo (root Dockerfile)
    # publishes to GHCR + Docker Hub (release.yaml policy step), and the Docker
    # Hub login needs per-repo secrets — cplieger is a user account, so there
    # are no org-level secrets. A missing secret fails the next release at the
    # Docker Hub login step.
    s["dockerhub_secrets"] = None
    if s.get("has_dockerfile"):
        sec = gh_json("api", f"repos/{OWNER}/{name}/actions/secrets")
        if isinstance(sec, dict) and "secrets" in sec:
            names_ = {x.get("name") for x in sec.get("secrets") or []}
            s["dockerhub_secrets"] = {"DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"} <= names_
        else:
            s["errors"].append("actions secrets unreadable (API)")

    # Synced files that must be BYTE-IDENTICAL to their canonical. sync.yaml
    # distributes them and nothing reported when a copy diverged, which is the gap
    # this closes. The fleet's default branches were verified clean when this
    # landed (2026-09-02, every Dockerfile repo byte-identical to
    # configs/repin-sha.sh), so this is a detector for a class rather than a
    # cleanup: the real instance was a comment-cleanup pass on a REVIEW branch
    # that read the synced copy as local code and trimmed it 201 lines to 148.
    # The next sync silently overwrites such an edit, so it is lost work either
    # way, and nothing said so. Beware of reasoning from a local checkout here —
    # several clones were many commits behind their remote, which looks exactly
    # like fleet-wide drift and is not.
    s["synced_drift"] = []
    for dest, canon_path in SYNCED_BYTE_IDENTICAL.items():
        if not s.get("has_dockerfile"):
            continue
        canon = canonical_text(canon_path)
        got = file_text(name, dest)
        if canon is None or got is None:
            s["errors"].append(f"{dest} byte-identity probe unreadable (API)")
        elif got and got != canon:
            s["synced_drift"].append(dest)

    # Public docs standard. One contents read serves every README check:
    # presence, the canonical footer blocks, License-last heading order, and
    # the Docker Hub overview markers. Public repos only — reader-facing docs
    # have no audience on a private repo.
    s["readme_text"] = None
    s["compose_example"] = None
    if not s["private"]:
        rd = gh_json("api", f"repos/{OWNER}/{name}/contents/README.md")
        if rd is API_ERROR:
            s["errors"].append("README.md unreadable (API)")
        elif isinstance(rd, dict) and rd.get("content") is not None:
            try:
                s["readme_text"] = base64.b64decode(
                    "".join(rd["content"].split())).decode("utf-8", "replace")
            except (ValueError, UnicodeError):
                s["readme_text"] = ""
        else:
            s["readme_text"] = ""  # definitively absent
        if s.get("has_dockerfile"):
            comp = file_text(name, "compose.yaml")
            if comp is None:
                s["errors"].append("compose.yaml probe unreadable (API)")
            else:
                s["compose_example"] = bool(comp)

    ci_txt = file_text(name, ".github/workflows/ci.yaml")
    if ci_txt is None:
        s["ci_wired"] = None  # unknown — never report "not wired" on an API error
        s["errors"].append("ci.yaml unreadable (API)")
    else:
        s["ci_wired"] = REUSABLE in ci_txt

    # Renovate reaches every repo through inheritConfig, not a per-repo file:
    # the scheduler sets inheritConfig + inheritConfigRepoName=cplieger/.github,
    # so cplieger/.github/org-inherited-config.json is the ONE place the preset
    # is referenced. The 61 per-repo shims were deleted in 2026-09; a repo
    # holding one again is drift, not compliance. Only .github is graded, and it
    # is graded HARD, because that single file is what delivers dependency
    # updates fleet-wide and its absence is silent (requireConfig=optional means
    # every repo would still be processed, just with no preset).
    if name == ".github":
        inherited = file_text(name, "org-inherited-config.json")
        if inherited is None:
            s["renovate_preset"] = None
            s["errors"].append("org-inherited-config.json unreadable (API)")
        else:
            s["renovate_preset"] = PRESET in inherited
    else:
        s["renovate_preset"] = None  # N/A — nothing per-repo to grade
    s["adopted"] = bool(s["ci_wired"]) or s["name"] in BESPOKE_CI

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

    two_channel = bool(s.get("two_channel"))
    if two_channel and s["name"] in release_channels.SINGLE_MAIN_REPOS:
        hard.append(f"default_branch=dev on a single-main repo (want main; {s['name']} publishes from main directly)")
    elif s["default_branch"] not in ("main", "dev"):
        hard.append(f"default_branch={s['default_branch']} (want dev, or main until the repo adopts the dev channel)")
    # A bootstrap run sits on main for minutes and a forgotten repo sits there
    # forever; the two look identical, so this names the repo instead of failing.
    if (s["default_branch"] == "main" and not s["private"] and s.get("publishes")
            and s["name"] not in release_channels.SINGLE_MAIN_REPOS):
        warn.append("default branch is main, so every merge releases straight to the stable channel "
                    "(adopt the dev channel, or list the repo in SINGLE_MAIN_REPOS)")

    # License: public repos only — a private personal repo has no audience
    # that needs a license grant. The expected license is per-category, not
    # fleet-wide: see LICENSE_OVERRIDES above and licensing.md for the rules.
    if not s["private"]:
        want = LICENSE_OVERRIDES.get(s["name"], LICENSE_DEFAULT)
        if s["license"] is None:
            (hard if s["adopted"] else warn).append("license missing")
        elif s["license"] != want:
            warn.append(f"license {s['license']} (want {want})")

    for dest in s.get("synced_drift") or []:
        warn.append(f"{dest} differs from its canonical in cplieger/ci "
                    "(synced file, edit the canonical — the next sync overwrites this copy)")

    if two_channel:
        # Rulesets replace classic protection here: a classic rule on either
        # branch is not the standard, and one on main would block the fast-forward
        # promotion the main ruleset's bypass exists to allow.
        if s["has_protection"]:
            hard.append("classic branch protection on dev (two-channel repos use the dev ruleset)")
        if s.get("main_protection"):
            hard.append("classic branch protection on main (two-channel repos use the main ruleset)")
        # rulesets_full is None when a ruleset read failed (already an [error]).
        for rname in CHANNEL_RULESETS if s.get("rulesets_full") is not None else ():
            actual = s["rulesets_full"].get(rname)
            if actual is None:
                hard.append(f"ruleset '{rname}' missing (want the body in configs/rulesets/{rname}.json)")
                continue
            for reason in ruleset_matches(expected_ruleset(rname), actual):
                hard.append(f"ruleset '{rname}' differs from configs/rulesets/{rname}.json: {reason}")
        if not s["has_protection"] and s.get("observed_complete"):
            observed = set(s.get("observed_checks") or [])
            for ctx in s["required_checks"] or []:
                if ctx not in observed:
                    hard.append(f"required context '{ctx}' never reported by any "
                                "recent check run (phantom — blocks every PR as "
                                "'Expected'; the context must equal the check-run "
                                "name)")
        for tag in s.get("stable_tags_without_release") or []:
            hard.append(f"stable tag {tag} has no GitHub Release (the stable release run "
                        "did not finish; rerun it or dispatch release.yaml on main)")
        for tag in s.get("hand_made_stable_tags") or []:
            hard.append(f"stable tag {tag} and its Release were not created by the release "
                        "pipeline (a hand-made tag becomes git-cliff's version base; delete both)")
        for tag in s.get("dev_tags_without_receipt") or []:
            hard.append(f"dev tag {tag} carries no release/tag receipt on its commit (a hand-made "
                        "dev tag skews the -dev.N counter and the change anchor; delete it)")
    elif s["has_protection"] is False:
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

    # Rulesets. On a single-main repo classic protection is the standard, so
    # any custom ruleset is drift (warn); on a two-channel repo the dev and
    # main rulesets are the standard (compared above) and only other names
    # are drift. A bypass actor weakens whatever ruleset carries it (warn),
    # except the RepositoryRole bypass the committed main body requires. An
    # Integration bypass actor is a HARD failure everywhere: the stale-app rot
    # class (a decommissioned GitHub App left able to bypass protection, which
    # the GitHub API also refuses to rewrite on a user-owned repo).
    expected_names = set(CHANNEL_RULESETS) if two_channel else set()
    for rs in s.get("custom_rulesets") or []:
        if rs["name"] in expected_names:
            continue
        standard = "the dev and main rulesets" if two_channel else "classic branch protection"
        warn.append(f"unexpected custom ruleset '{rs['name']}' ({rs['enforcement']}) "
                    f"(standard is {standard})")
    for rname, atype, aid in s.get("ruleset_bypass_actors") or []:
        if atype == "Integration":
            hard.append(f"ruleset '{rname}' has an Integration bypass actor (id {aid}) "
                        "— likely a stale/decommissioned app; remove it")
        elif two_channel and rname == "main" and atype == "RepositoryRole":
            continue
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

    # Scanning workflows arrive via sync for adopted repos; CodeQL is a
    # public-only feature (it needs GHAS on private repos), so it is N/A there.
    if s["adopted"] and not s["infra"] and not s["private"]:
        if s["has_codeql"] is False:
            warn.append("codeql.yml missing")
        if s["has_security_scan"] is False:
            warn.append("security.yml missing")
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
        hard.append("org-inherited-config.json does not extend the preset "
                    "(this file is what delivers Renovate to every repo)")

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

    # Public docs standard (public-docs.md). Presence is hard (a public repo
    # without a README is broken for its audience). The footer blocks and
    # License-last order are warnings: real drift, but aligned by the
    # docs-review skill rather than blocking the audit. The image-repo marker
    # check is hard because a release cannot build the Docker Hub overview page
    # without it, so the live Hub page silently stops being updated.
    # readme_text None means the read failed (already an [error]); '' means
    # definitively absent.
    if not s["private"] and s.get("readme_text") is not None:
        txt = s["readme_text"]
        if not txt.strip():
            hard.append("README.md missing")
        else:
            norm = " ".join(txt.split())
            if FOOTER_AI_NOTE not in norm:
                warn.append("README missing the canonical AI-assistance note "
                            "(verbatim block in repo-governance.md)")
            if s["name"] != ".github":
                if FOOTER_DISCLAIMER not in norm:
                    warn.append("README missing the canonical Disclaimer block "
                                "(verbatim block in repo-governance.md)")
                headings = re.findall(r"^## +(.+?)\s*$", txt, re.MULTILINE)
                if headings and headings[-1] != "License":
                    warn.append(f"README's last section is '{headings[-1]}' "
                                "(want License last — public-docs.md footer "
                                "invariant)")
            if s.get("has_dockerfile") and not (
                HUB_MARKER_BEGIN in txt and HUB_MARKER_END in txt
            ):
                hard.append(f"README carries no '{HUB_MARKER_BEGIN}' / "
                            f"'{HUB_MARKER_END}' pair, so the release cannot "
                            "build the Docker Hub overview page and the Hub "
                            "listing stops being updated (public-docs.md "
                            '"Docker Hub overview")')
        if s.get("compose_example") is False:
            warn.append("compose.yaml example missing (image repos ship a "
                        "reference compose — compose-examples.md)")

    # Deploy-trigger webhook, graded only when the host is configured and this
    # repo's hooks were readable (an unreadable token is a global skip in main).
    # Each defect here is silent and deploy-breaking: a missing hook, a hook
    # without the HMAC secret the orchestrator validates, or one subscribed to
    # the wrong event never fires. NO_DEPLOY_HOOK repos have no orchestrator
    # relationship, so nothing in this block applies to them.
    if WEBHOOK_HOST and s["webhook_readable"] and s["name"] not in NO_DEPLOY_HOOK:
        hooks = s.get("webhooks") or []
        if s["name"] in PUSH_WEBHOOK_REPOS:
            want_event = "push"
        elif two_channel and s["name"] in release_channels.DEPLOYED_IMAGE_REPOS:
            want_event = REGISTRY_EVENT
        elif two_channel:
            # A library on the dev channel triggers nothing at the orchestrator:
            # no hook is expected, and one left on the release event is stale.
            want_event = None
        else:
            want_event = "release"
        if want_event is None:
            for h in hooks:
                if "release" in (h["events"] or []):
                    warn.append(f"stale hook: deploy-trigger webhook events={h['events']} "
                                "(a dev-channel library needs no deploy hook; remove it)")
            hooks = []
        if want_event is not None and not hooks:
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
    grace_total = sum(s.get("tags_in_grace") or 0 for s in settings)
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
    deferred = sorted(s["name"] for s in settings if s.get("version_tags_deferred"))
    if deferred:
        print(f"Note: version tags not graded on {len(deferred)} repo(s) with a workflow "
              f"run live or ended within {RECEIPT_GRACE // timedelta(hours=1)}h: "
              f"{', '.join(deferred)}\n")
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
          f" · {accepted_total} accepted deviations · {error_total} API errors"
          f" · {grace_total} version tags younger than {RECEIPT_GRACE // timedelta(hours=1)}h"
          " not graded")
    if hard_total:
        sys.exit(1)
    if error_total:
        # No compliance failures, but the audit could not fully verify some
        # checks — infra trouble, not drift. Distinct exit code so the weekly
        # run goes red for the right reason.
        sys.exit(2)


if __name__ == "__main__":
    main()
