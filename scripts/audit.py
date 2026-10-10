#!/usr/bin/env python3
"""Cross-repo governance audit for the cplieger account.

Grades every non-archived, non-fork repo against the governance standard as HARD
failures and soft WARNINGS; compliance() owns the checks. ACCEPTED deviations are
counted, not listed, and a failed API read is an [error] line, never a finding.

Exit codes: 0 compliant; 1 a HARD failure; 2 usage or infra (an under-scoped token,
or API errors that prevented a full audit). Needs a CLASSIC PAT with the `repo`
scope: a fine-grained PAT does not serialize the merge-model fields, so it aborts.
--file-issues files a full run's --findings-out file as one `repo-audit` issue per
public repo; it exits 1 when a repo failed to file and 2 on an unusable file.
"""

import argparse
import base64
import functools
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ghrest
import release_channels
import tracker_issue

OWNER = 'cplieger'
PRESET = 'github>cplieger/.github'
REUSABLE = 'cplieger/ci/.github/workflows'
INFRA = {'.github', 'ci'}  # they define the standard; CI-wiring check is N/A for them
# Repos gating on a repo-local `validate` job instead of the reusable thin
# caller; the CI-wiring check is N/A for them, as for INFRA.
BESPOKE_CI = {'.kiro', 'homelab', 'AWS'}
# The deploy orchestrator's host is private, so it comes from AUDIT_WEBHOOK_HOST;
# unset, the webhook check is skipped so a local run does not fail every repo.
WEBHOOK_HOST = os.environ.get('AUDIT_WEBHOOK_HOST', '').strip()
# GitHub creates and manages this ruleset for code-scanning merge protection,
# so it is never drift.
MANAGED_RULESETS = {'code-scanning-merge-protection'}
REPO_ROOT = Path(__file__).resolve().parent.parent
RULESETS_DIR = REPO_ROOT / 'configs' / 'rulesets'
CHANNEL_RULESETS = ('dev', 'main')
# A dev build creates no Release, so a two-channel repo's hook fires on the package push.
REGISTRY_EVENT = 'registry_package'
_expected_rulesets_cache = {}


def expected_ruleset(name):
    """The committed ruleset body for `name`, read once."""
    if name not in _expected_rulesets_cache:
        with open(RULESETS_DIR / f'{name}.json', encoding='utf-8') as fh:
            _expected_rulesets_cache[name] = json.load(fh)
    return _expected_rulesets_cache[name]


def ruleset_matches(expected, actual):
    """Reasons the live ruleset `actual` differs from the committed body
    `expected`; empty when it matches. Ids, timestamps, links, `source`,
    `current_user_can_bypass` and parameter keys the API fills in on its own
    (allowed_merge_methods and the like) are ignored on purpose."""
    reasons = []
    for key in ('enforcement', 'target'):
        if actual.get(key) != expected.get(key):
            reasons.append(f'{key}={actual.get(key)!r} (want {expected.get(key)!r})')
    # An exclude naming the branch disables the ruleset while the include still
    # matches, and another condition key changes the targeting.
    want_cond = expected.get('conditions') or {}
    got_cond = actual.get('conditions') or {}
    if set(got_cond) != set(want_cond):
        reasons.append(f'condition keys {sorted(got_cond)} (want {sorted(want_cond)})')
    want_ref = want_cond.get('ref_name') or {}
    got_ref = got_cond.get('ref_name') or {}
    for key in ('include', 'exclude'):
        want_list, got_list = set(want_ref.get(key) or []), set(got_ref.get(key) or [])
        if want_list != got_list:
            reasons.append(f'branch {key} {sorted(got_list)} (want {sorted(want_list)})')
    extra_ref_keys = set(got_ref) - {'include', 'exclude'}
    if extra_ref_keys:
        reasons.append(f'unexpected ref_name keys {sorted(extra_ref_keys)}')
    want_rules = {r['type']: r for r in expected.get('rules') or []}
    got_rules = {r.get('type'): r for r in actual.get('rules') or []}
    if set(want_rules) != set(got_rules):
        reasons.append(f'rule types {sorted(got_rules)} (want {sorted(want_rules)})')
    for rtype, want in want_rules.items():
        got = got_rules.get(rtype)
        if got is None:
            continue
        got_params = got.get('parameters') or {}
        for k, v in (want.get('parameters') or {}).items():
            if got_params.get(k) != v:
                reasons.append(f'rule {rtype} parameter {k}={got_params.get(k)!r} (want {v!r})')
    got_actors, want_actors = _bypass_actor_set(actual), _bypass_actor_set(expected)
    if got_actors != want_actors:
        reasons.append(
            f'bypass actors {sorted(got_actors, key=str)} (want {sorted(want_actors, key=str)})'
        )
    return reasons


def _bypass_actor_set(ruleset):
    return {
        (a.get('actor_type'), a.get('actor_id'), a.get('bypass_mode'))
        for a in ruleset.get('bypass_actors') or []
    }


@functools.cache
def two_branch_renovate():
    """(dest, parsed body) of the Renovate config classify-repos.py syncs into
    every two-branch repo, read from this checkout."""
    spec = importlib.util.spec_from_file_location(
        'classify_repos', Path(__file__).with_name('classify-repos.py')
    )
    classify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(classify)
    [(source, dest)] = classify.pairs(classify.TWO_BRANCH_RENOVATE)
    with open(REPO_ROOT / source, encoding='utf-8') as fh:
        return dest, json.load(fh)


def collect_renovate_config(name, branch, s):
    """The two-branch renovate.json on `branch` into s["renovate_json"]: its
    text, '' on a 404, None with an error line on any other failure."""
    dest, _ = two_branch_renovate()
    s['renovate_json'] = None
    body = gh_json_strict(f'repos/{OWNER}/{name}/contents/{dest}?ref={branch}')
    if body is None:
        s['renovate_json'] = ''
        return
    if body is API_ERROR or not isinstance(body, dict) or not isinstance(body.get('content'), str):
        s['errors'].append(f'{dest} on {branch} unreadable (API), so not graded')
        return
    try:
        content = base64.b64decode(''.join(body['content'].split()), validate=True)
        s['renovate_json'] = content.decode('utf-8')
    except ValueError:
        s['errors'].append(f'{dest} on {branch} unreadable (API), so not graded')


def renovate_config_findings(text, branch):
    """HARD findings for a two-branch repo's renovate.json `text` ('' when absent)."""
    dest, want = two_branch_renovate()
    shown = json.dumps(want)
    if not text.strip():
        return [f'{dest} missing on {branch} (want {shown}, which the ci sync writes)']
    try:
        got = json.loads(text)
    except json.JSONDecodeError:
        return [f'{dest} on {branch} is not JSON (want {shown}, which the ci sync writes)']
    if got != want:
        return [f'{dest} on {branch} is {json.dumps(got)} (want {shown}, which the ci sync writes)']
    return []


# Non-releaseable repos whose deploy hook fires on push@main instead of release.
PUSH_WEBHOOK_REPOS = {'.github', '.kiro', 'ci', 'homelab'}
# Repos with no orchestrator relationship, so no deploy hook is graded; a
# missing hook stays HARD everywhere else. A denylist, so a new repo that needs
# a hook is flagged, and each entry says why it needs none: the one entry is a
# private workspace that ships nothing.
NO_DEPLOY_HOOK = {'AWS'}
# The GitHub App that must report the validate check (15368 = GitHub Actions):
# a context open to any app (-1) or pinned to another lets an integration satisfy it.
ACTIONS_APP_ID = 15368

# Matched as whitespace-normalized substrings, so a reflow passes but the text
# must stay verbatim; `.github` carries the AI note only.
FOOTER_DISCLAIMER = (
    'This project is built with care and follows security best practices, '
    'but it is intended for personal / self-hosted use. No guarantees of '
    'fitness for production environments. Use at your own risk.'
)
FOOTER_AI_NOTE = (
    'This project was built with AI-assisted tooling using '
    '[Claude](https://claude.com), [GPT](https://openai.com), and '
    '[Kiro](https://kiro.dev). The human maintainer defines architecture, '
    'supervises implementation, and makes all final decisions.'
)
# The pair the Docker Hub overview renderer (actions/render-hub-overview)
# extracts an image repo's summary from.
HUB_MARKER_BEGIN = '<!-- hub-overview BEGIN -->'
HUB_MARKER_END = '<!-- hub-overview END -->'

# {repo: {warning-prefix: reason}}: matching warnings are counted, not listed.
# Every entry needs a reason; remove it once the deviation is fixed.
ACCEPTED = {
    'homelab': {},
    'animap': {
        # baseline.yaml runs on every pull request and fails one that adds an
        # entry to checks/collision-baseline.json; required, so a new
        # collision cannot be accepted by growing the baseline.
        "unexpected extra required check 'shrink-only'": 'the collision baseline may only shrink',
    },
}

# Expected spdx_id per repo; a repo not listed is Apache-2.0.
# Values are what GitHub's licensee reports: it cannot tell -only from
# -or-later, so a GPL-3.0-or-later repo reads GPL-3.0.
LICENSE_DEFAULT = 'Apache-2.0'
LICENSE_OVERRIDES = {
    # The differentiated product. File-level copyleft asks for improvements
    # back while keeping the component embeddable in a closed larger work.
    'web-terminal-engine': 'MPL-2.0',
    'web-terminal-ui': 'MPL-2.0',
    # Thin hosts over the engine and ui: permissive here would ship the wired
    # app around their copyleft.
    'web-terminal-kiro': 'MPL-2.0',
    'web-terminal-server': 'MPL-2.0',
    # First-party applications. Nobody imports an app, so it never enters a
    # consumer's dependency tree and copyleft costs no adoption.
    'cert-converter': 'GPL-3.0',
    'github-scout': 'GPL-3.0',
    'knell': 'GPL-3.0',
    'plex-exporter': 'GPL-3.0',
    'plex-language-sync': 'GPL-3.0',
    'registry-stats': 'GPL-3.0',
    'seadex-scout': 'GPL-3.0',
    'tautulli-remap': 'GPL-3.0',
    # Separate-process tools, so the application rule applies; deadset-spec stays
    # Apache-2.0 so anyone can write a conforming analyzer.
    'deadset-go': 'GPL-3.0',
    'deadset-ts': 'GPL-3.0',
    'deadset': 'GPL-3.0',
    # Services a competitor could resell hosted, the one thing AGPL section 13
    # adds; rationed because it also puts AGPL on corporate blocklists.
    'subflux': 'AGPL-3.0',
    'marotte': 'AGPL-3.0',
}

# HARD merge model: squash only, the PR title as the changelog line, and
# COMMIT_MESSAGES, since a BREAKING CHANGE quoted in a Renovate PR body cuts a major.
GOV_HARD = {
    'allow_merge_commit': False,
    'allow_squash_merge': True,
    'allow_rebase_merge': False,
    'delete_branch_on_merge': True,
    'allow_auto_merge': True,
    'squash_merge_commit_title': 'PR_TITLE',
    'squash_merge_commit_message': 'COMMIT_MESSAGES',
}
GOV_SOFT = {
    'has_wiki': False,
    'has_projects': False,
    'has_issues': True,
    'has_discussions': False,
    'allow_update_branch': False,
    'web_commit_signoff_required': False,
}


def gh(*args):
    return subprocess.run(['gh', *args], capture_output=True, text=True)


# Every read runs through `gh`, so a test that fakes it drives the real policy.
REST = ghrest.Client(run=lambda args, *_: gh(*args))


# A read still failing transiently after retries; distinct from None (a
# definitive absence) so a flaky call never manufactures a HARD failure.
API_ERROR = object()


def gh_retry(path):
    """One GET under ghrest's retry policy: (response, definitive), where the
    response is the ghrest.ApiError when the read failed. definitive=True means
    the outcome can be trusted: success, or a 4xx that is not a rate limit (404
    absence, 403 permission). False means it still failed after the retries for
    a transient reason (rate limit, 5xx, network) and MUST NOT be read as absence.
    """
    try:
        return REST.request('GET', path), True
    except ghrest.ApiError as err:
        return err, err.definitive


def gh_json(path):
    """Parsed JSON on success; None on definitive absence or a body that is not
    JSON; API_ERROR on a transient failure that survived retries."""
    r, definitive = gh_retry(path)
    if not definitive:
        return API_ERROR
    if isinstance(r, ghrest.ApiError):
        return None
    try:
        return json.loads(r.body)
    except ValueError:
        return None


def gh_json_strict(path):
    """Parsed JSON on success; None on an HTTP 404 alone; API_ERROR on every
    other failure, a success body that is not JSON or is JSON null included.
    For a read whose absence arm grades something: a 403 or 422 must not pass
    as absence, and neither may a null body."""
    r, definitive = gh_retry(path)
    if not definitive:
        return API_ERROR
    if isinstance(r, ghrest.ApiError):
        return None if r.status == 404 else API_ERROR
    try:
        body = json.loads(r.body)
    except ValueError:
        return API_ERROR
    return API_ERROR if body is None else body


def api_status(path):
    """(ok, definitive) for endpoints that signal via HTTP status
    (204 enabled, 404 disabled)."""
    r, definitive = gh_retry(path)
    return not isinstance(r, ghrest.ApiError), definitive


def file_text(repo, path):
    """Decoded file content; '' when the file is definitively absent;
    None on an API error or a body that is not a file (unknown — do not treat
    as absent)."""
    r, definitive = gh_retry(f'repos/{OWNER}/{repo}/contents/{path}')
    if not definitive:
        return None
    if isinstance(r, ghrest.ApiError):
        return ''
    return decoded_file(r.body)


# A file definitively absent (HTTP 404), for a read where an empty file and a
# missing one grade differently.
ABSENT = object()


def present_file_text(repo, path):
    """Decoded content of a present file ('' when it is empty); ABSENT on an
    HTTP 404 alone; None on every other failure, including a body that is not
    a base64 file that decodes, as for a file above 1 MB:
    https://docs.github.com/rest/repos/contents#get-repository-content"""
    r, definitive = gh_retry(f'repos/{OWNER}/{repo}/contents/{path}')
    if not definitive:
        return None
    if isinstance(r, ghrest.ApiError):
        return ABSENT if r.status == 404 else None
    return decoded_file(r.body, strict=True)


def decoded_file(raw, strict=False):
    """A contents-API body's file text; None when the body is not a file. Content
    that does not decode is '' (file_text's contract), or None when `strict`,
    which also requires `encoding: base64` and refuses non-alphabet characters."""
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    content = body.get('content') if isinstance(body, dict) else None
    if not isinstance(content, str):
        return None
    if strict and body.get('encoding') != 'base64':
        return None
    try:
        decoded = base64.b64decode(''.join(content.split()), validate=strict)
        return decoded.decode('utf-8', 'replace')
    except ValueError, UnicodeError:
        return None if strict else ''


AUDIT_UA = 'Mozilla/5.0 (compatible; cplieger-governance-audit)'


# Synced files with no per-repo content, keyed on a root Dockerfile as in
# classify-repos.py. Opt-in synced files are absent: no copy is not drift.
SYNCED_BYTE_IDENTICAL = {
    'scripts/repin-sha.sh': 'configs/repin-sha.sh',
    'scripts/collect-licenses.sh': 'configs/collect-licenses.sh',
}

_canonical_cache = {}


def canonical_text(path):
    """This repo's own copy of a synced canonical, read once per run.

    Read through the API rather than off disk on purpose: the audit compares
    against what is actually on ci's default branch, so a run from a feature
    branch or a stale checkout cannot report every repo as drifted against an
    unpublished canonical.
    """
    if path not in _canonical_cache:
        _canonical_cache[path] = file_text('ci', path)
    return _canonical_cache[path]


def expected_used_by_package(go_module, package_json_text, *, image):
    """The package a repo's "Used by" counter should represent, or None to
    skip the check.

    An image repo (root Dockerfile) is never imported, so its counter counts
    nothing and is skipped. Otherwise the root go.mod module path (a library
    major moves it, which is the drift being caught), else the npm package
    name (catches renames); a malformed package.json names nothing.
    """
    if image:
        return None
    if go_module:
        return go_module
    if not package_json_text:
        return None
    try:
        return (json.loads(package_json_text) or {}).get('name')
    except json.JSONDecodeError:
        return None


def used_by_package_scrape(name):
    """(current, selectable, definitive) for the repo's "Used by" counter, scraped from
    the public dependents page because no API exposes that setting. `current` is the
    package og:title names (None when it names none), `selectable` the package-switcher
    anchors (empty for a single package; an unindexed Go /vN path is often absent, so
    flag only drift the dropdown can fix). definitive=False: the page was unreadable,
    so skip the check and never infer drift.
    """
    url = f'https://github.com/{OWNER}/{name}/network/dependents'
    req = urllib.request.Request(
        url, headers={'User-Agent': AUDIT_UA}
    )  # fixed https:// URL, host is github.com
    delay = 5
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — same fixed https URL
                body = resp.read(512 * 1024).decode('utf-8', 'replace')
            m = re.search(
                r'property="og:title" content="Network Dependents '
                r'· [^"]+? · (.+?) repositories"',
                body,
            )
            names = set()
            for mm in re.finditer(
                r'href="/[^"]+/network/dependents\?package_id='
                r'[^"]+"[^>]*>(.*?)</a>',
                body,
                re.DOTALL,
            ):
                # anchor bodies may nest tags; reduce to text before judging
                text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', mm.group(1))).strip()
                if text and not re.fullmatch(r'[\d,]+ Repositor(?:y|ies)', text):
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
        except urllib.error.URLError, TimeoutError, OSError:
            if attempt < 2:
                time.sleep(delay)
                delay *= 3
                continue
            return None, [], False
    return None, [], False


# How many of the newest tags of each shape are graded for provenance.
PROVENANCE_TAGS = 5
# Every publishing path creates the tag before its receipt, so a tag on a younger
# commit is not graded: an audit overlapping a release must not fail it.
RECEIPT_GRACE = timedelta(hours=2)


def tag_page_fetcher(name, sha_by_tag):
    """A page reader for release_channels.collect_all_tags that also records
    each tag's commit; None from a page means the API failed after retries."""

    def fetch(page):
        tags = gh_json(
            f'repos/{OWNER}/{name}/tags?per_page={release_channels.TAG_PAGE_SIZE}&page={page}'
        )
        if tags is API_ERROR:
            return None
        names_ = []
        for tg in tags if isinstance(tags, list) else []:
            tag = tg.get('name') or ''
            names_.append(tag)
            sha_by_tag[tag] = ((tg.get('commit') or {}).get('sha')) or ''
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


def tags_without_receipt(tagged, statuses_of, has_receipt=release_channels.has_tag_receipt):
    """The tags of `tagged` ([(tag, sha)]) whose commit statuses fail
    `has_receipt(tag, statuses)`. `statuses_of(sha)` is the commit's status
    list, API_ERROR when unreadable (the tag is then skipped)."""
    missing = []
    for tag, sha in tagged:
        if not sha:
            continue
        statuses = statuses_of(sha)
        if statuses is API_ERROR:
            continue
        if not has_receipt(tag, statuses):
            missing.append(tag)
    return missing


def commit_date(name, sha):
    """The committer date of `sha`; API_ERROR when unreadable or when the
    response carries no parsable date, since the grace interval is then unknown."""
    data = gh_json(f'repos/{OWNER}/{name}/commits/{sha}')
    if data is API_ERROR:
        return API_ERROR
    iso = (((data or {}).get('commit') or {}).get('committer') or {}).get('date')
    try:
        date = datetime.fromisoformat(iso)
    except TypeError, ValueError:
        return API_ERROR
    return date if date.tzinfo else API_ERROR


def workflow_runs(path):
    """The rows of the workflow-runs listing at `path`; None on an HTTP 404
    (a repo without the workflow file); API_ERROR on every other failure, a
    success body that is not a listing (an object carrying an integer
    `total_count` and a `workflow_runs` list) included."""
    body = gh_json_strict(path)
    if body is None or body is API_ERROR:
        return body
    if not isinstance(body, dict) or not isinstance(body.get('total_count'), int):
        return API_ERROR
    runs = body.get('workflow_runs')
    return runs if isinstance(runs, list) else API_ERROR


def release_in_flight(name, now):
    """Whether a run of the repo's release.yaml is queued or in
    progress, or ended within RECEIPT_GRACE; API_ERROR when a read failed. The
    commit's age cannot show this: a renumbered dev build tags a commit made
    long before, and a repaired stable tag gets its Release long after its
    commit, so the receipt follows outside the grace window. Other workflows
    are not read: the daily security dispatch puts a run inside the window in
    every repo, and it says nothing about a tag. A 404 on the listing is a
    repo without the workflow file: no run, graded."""
    runs_path = f'repos/{OWNER}/{name}/actions/workflows/release.yaml/runs'
    for status in ('in_progress', 'queued'):
        runs = workflow_runs(f'{runs_path}?status={status}&per_page=1')
        if runs is API_ERROR:
            return API_ERROR
        if runs:
            return True
    runs = workflow_runs(f'{runs_path}?per_page=1')
    if runs is API_ERROR:
        return API_ERROR
    for run in runs or []:
        try:
            recent = abs(now - datetime.fromisoformat(run['updated_at'])) < RECEIPT_GRACE
        except KeyError, TypeError, ValueError:
            return API_ERROR
        if recent:
            return True
    return False


def collect_version_tags(name, s, now=None):
    now = now or datetime.now(UTC)
    live = release_in_flight(name, now)
    if live is API_ERROR:
        s['errors'].append('workflow runs unreadable (API); version tags not graded')
        return
    if live:
        s['version_tags_deferred'] = True
        return
    sha_by_tag = {}
    try:
        names_ = release_channels.collect_all_tags(tag_page_fetcher(name, sha_by_tag))
    except release_channels.TagListingTruncatedError as err:
        s['errors'].append(f'tags listing truncated ({err}); version tags not graded')
        return
    if names_ is None:
        s['errors'].append('tags unreadable (API)')
        return

    unreadable = set()

    def release_of(tag):
        rel = gh_json(f'repos/{OWNER}/{name}/releases/tags/{tag}')
        if rel is API_ERROR:
            s['errors'].append(f'release for tag {tag} unreadable (API)')
            unreadable.add(tag)
        return rel

    def statuses_of(sha):
        data = gh_json(f'repos/{OWNER}/{name}/commits/{sha}/status?per_page=100')
        if data is API_ERROR:
            s['errors'].append(f'statuses of {sha[:12]} unreadable (API)')
            return API_ERROR
        statuses = (data or {}).get('statuses') or []
        total = (data or {}).get('total_count')
        if isinstance(total, int) and total > len(statuses):
            s['errors'].append(
                f'statuses of {sha[:12]} truncated at {len(statuses)}, so receipts not graded'
            )
            return API_ERROR
        return statuses

    def settled(tags):
        """The tags of `tags` whose commit is more than RECEIPT_GRACE from now
        in either direction; the younger ones are counted, a tag without a
        commit or with an unreadable one is skipped with an error."""
        kept = []
        for tag in tags:
            sha = sha_by_tag.get(tag, '')
            if not sha:
                s['errors'].append(f'tag {tag} carries no commit sha; not graded')
                continue
            date = commit_date(name, sha)
            if date is API_ERROR:
                s['errors'].append(f'commit of tag {tag} unreadable (API)')
            elif abs(now - date) < RECEIPT_GRACE:
                s['tags_in_grace'] += 1
            else:
                kept.append(tag)
        return kept

    # Each lane is graded on its own newest tags, so the whole listing is read.
    s['stable_tags_without_release'], s['hand_made_stable_tags'] = [], []
    s['dev_tags_without_receipt'] = []
    s['stable_tags_without_receipt'] = []
    s['tags_in_grace'] = 0

    def lane_releases(lane):
        """Whether `lane` still has a module on main, the branch stable runs
        release from. Only a live lane gets its receipt repaired (release-state.sh
        lane_keys), so a retired lane's last tag would stay a finding forever."""
        if not lane:
            return True
        unread = f'{lane}/go.mod on main unreadable (API), so its receipt is not graded'
        body = gh_json_strict(f'repos/{OWNER}/{name}/contents/{lane}/go.mod?ref=main')
        if body is API_ERROR:
            s['errors'].append(unread)
            return False
        if body is not None:
            return True
        # A missing ref also answers 404, so a repo without main must not read as retired.
        branch = gh_json_strict(f'repos/{OWNER}/{name}/branches/main')
        if branch is None:
            s['errors'].append(
                f'{name} has no main branch, so the receipt of lane {lane} is not graded'
            )
        elif branch is API_ERROR:
            s['errors'].append(unread)
        return False

    for lane, (stable, dev) in sorted(release_channels.tags_by_lane(names_).items()):
        graded = settled(stable[:PROVENANCE_TAGS])
        without, hand_made = grade_stable_tags(graded, release_of)
        s['stable_tags_without_release'] += without
        s['hand_made_stable_tags'] += hand_made
        s['dev_tags_without_receipt'] += tags_without_receipt(
            [(t, sha_by_tag.get(t, '')) for t in settled(dev[:PROVENANCE_TAGS])], statuses_of
        )
        # Only the highest: a receipt covers the history below it, so older
        # tags (every one made before the receipt existed) carry none.
        if (
            stable
            and stable[0] in graded
            and stable[0] not in {*without, *hand_made, *unreadable}
            and lane_releases(lane)
        ):
            s['stable_tags_without_receipt'] += tags_without_receipt(
                [(stable[0], sha_by_tag.get(stable[0], ''))],
                statuses_of,
                release_channels.has_completion_receipt,
            )


def collect_rulesets(name, s):
    """Every repository ruleset in full: the list endpoint returns only id, name
    and enforcement, and both the bypass actors (the stale-app rot class) and
    the two-channel comparison need the body. One failed read leaves the three
    ruleset keys None, so compliance() grades no ruleset finding from a partial
    list; a present ruleset must never be reported missing off a flaky read."""
    s['custom_rulesets'] = []
    s['ruleset_bypass_actors'] = []  # (ruleset_name, actor_type, actor_id, bypass_mode)
    s['rulesets_full'] = {}
    rulesets = gh_json(f'repos/{OWNER}/{name}/rulesets')
    if rulesets is API_ERROR:
        s['errors'].append('rulesets unreadable (API); rulesets not graded')
        s['custom_rulesets'] = s['ruleset_bypass_actors'] = s['rulesets_full'] = None
        return
    for rs in rulesets if isinstance(rulesets, list) else []:
        rname = rs.get('name', '')
        full = gh_json(f'repos/{OWNER}/{name}/rulesets/{rs.get("id")}')
        if full is API_ERROR:
            s['errors'].append(f"ruleset '{rname}' unreadable (API); rulesets not graded")
            s['custom_rulesets'] = s['ruleset_bypass_actors'] = s['rulesets_full'] = None
            return
        full = full or {}
        s['rulesets_full'][rname] = full
        if rname not in MANAGED_RULESETS:
            s['custom_rulesets'].append({'name': rname, 'enforcement': full.get('enforcement')})
        for a in full.get('bypass_actors') or []:
            s['ruleset_bypass_actors'].append(
                (rname, a.get('actor_type'), a.get('actor_id'), a.get('bypass_mode'))
            )


def collect(meta):
    """Gather the full governance-relevant settings surface for one repo.

    s["errors"] records every check whose API reads failed transiently after
    retries; compliance() skips those checks instead of failing them, and
    main() reports them as [error] with exit 2.
    """
    name = meta['name']
    s = {'name': name, 'infra': name in INFRA, 'errors': [], 'fatal': False}
    repo = gh_json(f'repos/{OWNER}/{name}')
    if repo is API_ERROR:
        # Without the repo object there is nothing meaningful to audit.
        s['errors'].append('repo settings unreadable (API)')
        s['fatal'] = True
        s.update(
            {
                'visibility': None,
                'private': bool(meta.get('visibility') == 'private'),
                'admin_visible': False,
            }
        )
        return s
    repo = repo or {}
    s['visibility'] = repo.get('visibility')
    s['private'] = bool(repo.get('private'))
    branch = repo.get('default_branch') or 'main'
    s['default_branch'] = branch
    s['two_channel'] = release_channels.is_two_branch(repo)
    # Only a classic `repo`-scope token gets the merge-model fields; a fine-grained
    # one, admin or not, gets none, so their presence is the guard.
    s['admin_visible'] = 'allow_merge_commit' in repo
    for k in (
        'allow_merge_commit',
        'allow_squash_merge',
        'allow_rebase_merge',
        'allow_auto_merge',
        'delete_branch_on_merge',
        'has_wiki',
        'has_projects',
        'has_issues',
        'has_discussions',
        'allow_update_branch',
        'web_commit_signoff_required',
        'squash_merge_commit_title',
        'squash_merge_commit_message',
    ):
        s[k] = repo.get(k)
    lic = repo.get('license')
    s['license'] = lic.get('spdx_id') if lic else None
    desc = (repo.get('description') or '').strip()
    s['desc_present'] = bool(desc)
    s['desc_len'] = len(desc)
    s['topics'] = repo.get('topics') or []

    sa = repo.get('security_and_analysis') or {}
    s['secret_scanning'] = (sa.get('secret_scanning') or {}).get('status')
    s['secret_scanning_push_protection'] = (sa.get('secret_scanning_push_protection') or {}).get(
        'status'
    )
    s['dependabot_security_updates'] = (sa.get('dependabot_security_updates') or {}).get('status')

    ok, definitive = api_status(f'repos/{OWNER}/{name}/vulnerability-alerts')
    s['vuln_alerts'] = ok if definitive else None
    if not definitive:
        s['errors'].append('vulnerability-alerts unreadable (API)')

    pvr = gh_json(f'repos/{OWNER}/{name}/private-vulnerability-reporting')
    if pvr is API_ERROR:
        s['private_vuln_reporting'] = None
        s['errors'].append('private-vulnerability-reporting unreadable (API)')
    else:
        s['private_vuln_reporting'] = bool((pvr or {}).get('enabled'))

    prot = gh_json(f'repos/{OWNER}/{name}/branches/{branch}/protection')
    if prot is API_ERROR:
        prot = None
        s['has_protection'] = None
        s['errors'].append('branch protection unreadable (API)')
    else:
        s['has_protection'] = isinstance(prot, dict) and 'url' in prot
    if s['has_protection']:
        rsc = prot.get('required_status_checks') or {}
        contexts = list(rsc.get('contexts') or [])
        contexts += [
            c.get('context') for c in (rsc.get('checks') or []) if c.get('context') not in contexts
        ]
        s['required_checks'] = contexts
        # context -> app_id, for the validate gate's app pin.
        s['required_check_apps'] = {
            c.get('context'): c.get('app_id') for c in (rsc.get('checks') or [])
        }
        s['strict'] = rsc.get('strict')
        s['enforce_admins'] = (prot.get('enforce_admins') or {}).get('enabled')
        s['allow_force_pushes'] = (prot.get('allow_force_pushes') or {}).get('enabled')
        s['allow_deletions'] = (prot.get('allow_deletions') or {}).get('enabled')
        # The standard sets none of these, so each is drift or, below, breakage.
        reviews = prot.get('required_pull_request_reviews')
        s['required_reviews_present'] = reviews is not None
        s['required_review_count'] = (reviews or {}).get('required_approving_review_count', 0)
        s['required_conversation_resolution'] = (
            prot.get('required_conversation_resolution') or {}
        ).get('enabled')
        s['required_linear_history'] = (prot.get('required_linear_history') or {}).get('enabled')
        s['required_signatures'] = (prot.get('required_signatures') or {}).get('enabled')
        s['lock_branch'] = (prot.get('lock_branch') or {}).get('enabled')
        s['push_restrictions'] = prot.get('restrictions') is not None
    else:
        s['required_checks'] = []
        s['required_check_apps'] = {}
        s['strict'] = s['enforce_admins'] = s['allow_force_pushes'] = s['allow_deletions'] = None
        s['required_reviews_present'] = s['push_restrictions'] = False
        s['required_review_count'] = 0
        s['required_conversation_resolution'] = s['required_linear_history'] = None
        s['required_signatures'] = s['lock_branch'] = None

    # A two-channel repo's main is governed by its ruleset alone.
    s['main_protection'] = None
    if s['two_channel']:
        mprot = gh_json(f'repos/{OWNER}/{name}/branches/main/protection')
        if mprot is API_ERROR:
            s['errors'].append('main branch protection unreadable (API)')
        else:
            s['main_protection'] = isinstance(mprot, dict) and 'url' in mprot

    collect_rulesets(name, s)

    # With no classic protection, a two-channel repo's required contexts are
    # the dev ruleset's, and the phantom check below grades those.
    if s['two_channel'] and not s['has_protection']:
        dev_rs = (s['rulesets_full'] or {}).get('dev') or {}
        for rule in dev_rs.get('rules') or []:
            if rule.get('type') != 'required_status_checks':
                continue
            checks = (rule.get('parameters') or {}).get('required_status_checks') or []
            s['required_checks'] = [c.get('context') for c in checks if c.get('context')]
            s['required_check_apps'] = {c.get('context'): c.get('integration_id') for c in checks}

    # The newest tags of each shape need the pipeline's receipt, or a hand-made
    # tag becomes git-cliff's version base.
    s['stable_tags_without_release'] = []
    s['hand_made_stable_tags'] = []
    s['dev_tags_without_receipt'] = []
    if s['two_channel']:
        collect_version_tags(name, s)

    # Phantom required contexts: protection matches a context against check-run
    # names ('ci / validate', or a plain job's bare name, never the UI's
    # 'Workflow / job'), and one nothing reports blocks every PR as "Expected".
    # Names come from the default-branch HEAD, then, while one is unseen, from
    # the 3 newest PR heads (PR-only CI) and HEAD's combined status (a
    # non-Actions integration).
    s['observed_checks'] = []
    s['observed_complete'] = True
    if s['required_checks']:
        names, complete = set(), True

        def check_names(ref):
            nonlocal complete
            cr = gh_json(f'repos/{OWNER}/{name}/commits/{ref}/check-runs?per_page=100')
            if cr is API_ERROR:
                complete = False
                return set()
            return {c.get('name') for c in (cr or {}).get('check_runs') or [] if c.get('name')}

        names |= check_names(branch)
        if not set(s['required_checks']) <= names:
            prs = gh_json(
                f'repos/{OWNER}/{name}/pulls?state=all&sort=updated&direction=desc&per_page=3'
            )
            if prs is API_ERROR:
                complete = False
            else:
                for pr in prs if isinstance(prs, list) else []:
                    sha = (pr.get('head') or {}).get('sha')
                    if sha:
                        names |= check_names(sha)
            st = gh_json(f'repos/{OWNER}/{name}/commits/{branch}/status')
            if st is API_ERROR:
                complete = False
            else:
                names |= {
                    c.get('context') for c in (st or {}).get('statuses') or [] if c.get('context')
                }
        s['observed_checks'] = sorted(names)
        s['observed_complete'] = complete
        if not complete and not set(s['required_checks']) <= names:
            s['errors'].append(
                'check-run names unreadable (API) — phantom-required-context check skipped'
            )

    wperm = gh_json(f'repos/{OWNER}/{name}/actions/permissions/workflow')
    if wperm is API_ERROR:
        s['default_workflow_permissions'] = None
        s['workflows_can_approve_prs'] = None
        s['errors'].append('actions workflow permissions unreadable (API)')
    else:
        wperm = wperm or {}
        s['default_workflow_permissions'] = wperm.get('default_workflow_permissions')
        s['workflows_can_approve_prs'] = wperm.get('can_approve_pull_request_reviews')

    wf = gh_json(f'repos/{OWNER}/{name}/contents/.github/workflows')
    if wf is API_ERROR:
        s['has_codeql'] = s['has_security_scan'] = None
        s['release_caller'] = s['own_publisher'] = None
        s['errors'].append('workflow listing unreadable (API)')
    else:
        wf_names = {f['name'] for f in wf} if isinstance(wf, list) else set()
        s['has_codeql'] = bool({'codeql.yml', 'codeql.yaml'} & wf_names)
        s['has_security_scan'] = bool({'security.yml', 'security.yaml'} & wf_names)
        s['release_caller'] = bool({'release.yaml', 'release.yml'} & wf_names)
        s['own_publisher'] = bool({'publish.yaml', 'publish.yml'} & wf_names)

    # Surface probes as in classify-repos.py: go.mod and package.json are read for
    # their text below, the Dockerfile for its presence.
    probe_texts = {}
    for probe_file in ('go.mod', 'package.json', 'Dockerfile'):
        txt = file_text(name, probe_file)
        probe_texts[probe_file] = txt
        if txt is None:
            s['errors'].append(f'{probe_file} probe unreadable (API)')
    dockerfile = probe_texts['Dockerfile']
    s['has_dockerfile'] = None if dockerfile is None else bool(dockerfile)
    s['root_manifests'] = {p: probe_texts[p] for p in ('go.mod', 'package.json')}

    # Used-by counter: the selection is scraped (no API), on public repos only;
    # expected_used_by_package owns which package that should be.
    m = re.search(r'^module\s+(\S+)', probe_texts.get('go.mod') or '', re.MULTILINE)
    s['go_module'] = m.group(1) if m else None
    s['expected_package'] = expected_used_by_package(
        s['go_module'], probe_texts.get('package.json'), image=bool(s['has_dockerfile'])
    )
    s['used_by_package'] = None
    s['used_by_selectable'] = []
    s['used_by_attempted'] = False
    s['used_by_readable'] = False
    if s['expected_package'] and not s['private']:
        s['used_by_attempted'] = True
        pkg, selectable, definitive = used_by_package_scrape(name)
        s['used_by_readable'] = definitive
        s['used_by_package'] = pkg
        s['used_by_selectable'] = selectable

    # A committed dependabot.yml enables Dependabot VERSION update PRs, which
    # compete with Renovate (the settings twin of the security-updates check).
    dep_txt = file_text(name, '.github/dependabot.yml')
    if dep_txt is None:
        s['has_dependabot_yml'] = None
        s['errors'].append('dependabot.yml probe unreadable (API)')
    else:
        s['has_dependabot_yml'] = bool(dep_txt)

    # Image repos dual-publish, and a user account has no org secrets, so each
    # needs its own; a missing one fails the next release's Docker Hub login. A
    # GHCR-only exemption needs a skip here and a REGISTRIES arm in release.yaml.
    s['dockerhub_secrets'] = None
    if s.get('has_dockerfile'):
        sec = gh_json(f'repos/{OWNER}/{name}/actions/secrets')
        if isinstance(sec, dict) and 'secrets' in sec:
            names_ = {x.get('name') for x in sec.get('secrets') or []}
            s['dockerhub_secrets'] = {'DOCKERHUB_USERNAME', 'DOCKERHUB_TOKEN'} <= names_
        else:
            s['errors'].append('actions secrets unreadable (API)')

    # Synced copies that must be byte-identical to their canonical: an edit to
    # one is lost on the next sync, so it is reported here first.
    s['synced_drift'] = []
    for dest, canon_path in SYNCED_BYTE_IDENTICAL.items():
        if not s.get('has_dockerfile'):
            continue
        canon = canonical_text(canon_path)
        got = file_text(name, dest)
        if canon is None or got is None:
            s['errors'].append(f'{dest} byte-identity probe unreadable (API)')
        elif got and got != canon:
            s['synced_drift'].append(dest)

    # One README read serves every docs check; public repos only.
    s['readme_text'] = None
    s['compose_example'] = None
    if not s['private']:
        rd = gh_json(f'repos/{OWNER}/{name}/contents/README.md')
        if rd is API_ERROR:
            s['errors'].append('README.md unreadable (API)')
        elif isinstance(rd, dict) and rd.get('content') is not None:
            try:
                s['readme_text'] = base64.b64decode(''.join(rd['content'].split())).decode(
                    'utf-8', 'replace'
                )
            except ValueError, UnicodeError:
                s['readme_text'] = ''
        else:
            s['readme_text'] = ''  # definitively absent
        if s.get('has_dockerfile'):
            comp = file_text(name, 'compose.yaml')
            if comp is None:
                s['errors'].append('compose.yaml probe unreadable (API)')
            else:
                s['compose_example'] = bool(comp)

    ci_txt = file_text(name, '.github/workflows/ci.yaml')
    if ci_txt is None:
        s['ci_wired'] = None  # unknown — never report "not wired" on an API error
        s['errors'].append('ci.yaml unreadable (API)')
    else:
        s['ci_wired'] = REUSABLE in ci_txt

    # Renovate reaches every repo through the inherited config in .github, so only
    # that file is graded, HARD: without it every repo runs with no preset.
    if name == '.github':
        inherited = file_text(name, 'org-inherited-config.json')
        if inherited is None:
            s['renovate_preset'] = None
            s['errors'].append('org-inherited-config.json unreadable (API)')
        else:
            s['renovate_preset'] = PRESET in inherited
    else:
        s['renovate_preset'] = None  # N/A — nothing per-repo to grade
    if s['two_channel']:
        collect_renovate_config(name, branch, s)
    s['adopted'] = bool(s['ci_wired']) or s['name'] in BESPOKE_CI

    # Every active hook pointing at the orchestrator, with the fields graded later;
    # webhook_readable tells "no hook" from "hooks unreadable" (a global skip).
    s['webhook_readable'] = False
    s['webhooks'] = []
    if WEBHOOK_HOST:
        hooks = gh_json(f'repos/{OWNER}/{name}/hooks')
        if hooks is API_ERROR:
            hooks = None
            s['errors'].append('webhooks unreadable (API)')
        if isinstance(hooks, list):
            s['webhook_readable'] = True
            for h in hooks:
                cfg = h.get('config') or {}
                if WEBHOOK_HOST not in (cfg.get('url') or '') or not h.get('active'):
                    continue
                code = (h.get('last_response') or {}).get('code')
                s['webhooks'].append(
                    {
                        'events': sorted(h.get('events') or []),
                        'content_type': cfg.get('content_type'),
                        'insecure_ssl': str(cfg.get('insecure_ssl', '')),
                        'has_secret': bool(cfg.get('secret')),
                        'bad_delivery': code if isinstance(code, int) and code >= 400 else None,
                    }
                )
    return s


GO_FIRST_PARTY = f'github.com/{OWNER}/'
NPM_SCOPE = f'@{OWNER}/'
NPM_DEPENDENCY_KEYS = (
    'dependencies',
    'devDependencies',
    'peerDependencies',
    'optionalDependencies',
)
MANIFESTS = ('go.mod', 'package.json')
# Go skips testdata and directories starting with '.' or '_' when it walks a
# module tree; vendor and node_modules hold other modules' manifests.
SKIPPED_DIRS = ('vendor', 'node_modules', 'testdata')
GO_MAJOR_SUFFIX = re.compile(r'v([2-9]|[1-9]\d+)')
# One version or one caret, tilde or x-range: each pins a single major. A lower
# bound alone admits every later major, so it never pins one behind.
NPM_SINGLE_MAJOR = re.compile(r'[\^~=]?v?(\d+)(?:\.(?:\d+|[xX*])){0,2}(?:-[0-9A-Za-z.-]+)?')


def manifest_paths(tree):
    """The go.mod and package.json paths in a recursive tree listing that the
    repository builds with."""
    paths = []
    for entry in tree:
        *dirs, base = (entry.get('path') or '').split('/')
        if entry.get('type') != 'blob' or base not in MANIFESTS:
            continue
        if any(d in SKIPPED_DIRS or d.startswith(('.', '_')) for d in dirs):
            continue
        paths.append('/'.join([*dirs, base]))
    return sorted(paths)


def go_requirements(text):
    """The module paths go.mod `text` requires, single-line and block forms,
    minus those a replace directive points at a local directory."""
    required, local, block = [], set(), None
    for raw in text.splitlines():
        line = raw.split('//', 1)[0].strip()
        if not line:
            continue
        if block and line == ')':
            block = None
            continue
        verb, rest = (block, line) if block else [*line.split(None, 1), ''][:2]
        if rest == '(':
            block = verb
            continue
        words = rest.split()
        if verb == 'require' and words:
            required.append(words[0].strip('"'))
        elif verb == 'replace' and '=>' in words:
            target = words[words.index('=>') + 1 :]
            if target and target[0].strip('"').startswith(('./', '../', '/')):
                local.add(words[0].strip('"'))
    return [p for p in required if p not in local]


def go_module_target(path):
    """(repo, lane, major) a first-party module path names, major None below
    v2, which carries no suffix; None for any other path."""
    if not path.startswith(GO_FIRST_PARTY):
        return None
    repo, *rest = path[len(GO_FIRST_PARTY) :].split('/')
    major = None
    if rest and GO_MAJOR_SUFFIX.fullmatch(rest[-1]):
        major = int(rest.pop()[1:])
    return repo, '/'.join(rest), major


def npm_requirements(text):
    """[(name, range)] of every first-party dependency in package.json
    `text`; None when it is not a JSON object."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    found = []
    for key in NPM_DEPENDENCY_KEYS:
        deps = data.get(key)
        for dep, spec in deps.items() if isinstance(deps, dict) else ():
            if dep.startswith(NPM_SCOPE) and isinstance(spec, str) and (dep, spec) not in found:
                found.append((dep, spec))
    return found


def npm_major(spec):
    """The one major an npm range pins; None when it pins none, as a lower
    bound alone, a compound range, an alias, a path or a URL do."""
    m = NPM_SINGLE_MAJOR.fullmatch(spec.strip())
    return int(m.group(1)) if m else None


def latest_stable_majors(repo):
    """{lane: latest stable major} from the repo's tag listing, the root lane
    being ''; a string naming the failure when the listing is unusable."""
    try:
        tags = release_channels.collect_all_tags(tag_page_fetcher(repo, {}))
    except release_channels.TagListingTruncatedError as err:
        return f'tags of {repo} truncated ({err})'
    if tags is None:
        return f'tags of {repo} unreadable (API)'
    return {
        lane: release_channels.semver_key(stable[0].rsplit('/', 1)[-1])[0]
        for lane, (stable, _dev) in release_channels.tags_by_lane(tags).items()
        if stable
    }


class LatestMajors:
    """latest_stable_majors per repo, read once per run however many
    consumers, on however many threads, ask for it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def of(self, repo):
        with self._lock:
            entry = self._entries.setdefault(repo, {'lock': threading.Lock()})
        with entry['lock']:
            if 'value' not in entry:
                entry['value'] = latest_stable_majors(repo)
            return entry['value']


def manifest_tree(name, branch, s):
    """The manifest paths on `branch`; [] for a repository without a commit;
    None, with an error line, when the tree is unreadable or truncated."""
    r, definitive = gh_retry(f'repos/{OWNER}/{name}/git/trees/{branch}?recursive=1')
    if isinstance(r, ghrest.ApiError):
        if definitive and r.status in (404, 409):
            return []
        s['errors'].append('file tree unreadable (API), so first-party majors not graded')
        return None
    try:
        body = json.loads(r.body)
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get('tree'), list):
        s['errors'].append('file tree unreadable (API), so first-party majors not graded')
        return None
    if body.get('truncated'):
        s['errors'].append('file tree truncated, so first-party majors not graded')
        return None
    return manifest_paths(body['tree'])


def manifest_targets(path, text, s, repos):
    """[(shown requirement, repo, lane, major)] of manifest `text` at `path`;
    a requirement naming no repository of `repos` is an error line instead."""
    targets = []
    if path.rsplit('/', 1)[-1] == 'go.mod':
        for mod in go_requirements(text):
            target = go_module_target(mod)
            if target:
                targets.append((mod, *target))
    else:
        deps = npm_requirements(text)
        if deps is None:
            s['errors'].append(f'{path} is not a JSON object, so its first-party majors not graded')
            return []
        for dep, spec in deps:
            major = npm_major(spec)
            if major is not None:
                targets.append((f'{dep} {spec}', dep[len(NPM_SCOPE) :], '', major))
    known = []
    for target in targets:
        if target[1] in repos:
            known.append(target)
        else:
            s['errors'].append(f'{path}: requires {target[0]}, which names no {OWNER} repository')
    return known


def collect_first_party_majors(s, latest, repos):
    """s["stale_majors"]: a warning line per first-party requirement on the
    default branch whose major is below its module's latest stable one.
    `latest` is the run's LatestMajors, `repos` every repository name the owner
    has. A failed read is an error line and grades nothing it covers."""
    s['stale_majors'] = []
    paths = manifest_tree(s['name'], s['default_branch'], s)
    root = s.get('root_manifests') or {}
    for path in paths or []:
        text = root.get(path)
        if path in root and text is None:
            continue  # collect() recorded the unreadable root file
        if not text:
            # file_text reads an undecodable body as '', so only a non-empty
            # root read is reused.
            text = present_file_text(s['name'], path)
        if text is None or text is ABSENT:
            s['errors'].append(f'{path} unreadable (API), so its first-party majors not graded')
            continue
        for shown, repo, lane, major in manifest_targets(path, text, s, repos):
            majors = latest.of(repo)
            if isinstance(majors, str):
                s['errors'].append(f'{path}: {shown} not graded, because {majors}')
                continue
            newest = majors.get(lane)
            # No suffix is a v0 or v1 path, current until a v2 exists.
            required = 1 if major is None else major
            if newest is not None and required < newest:
                s['stale_majors'].append(f'{path}: requires {shown}, latest is v{newest}')


# GitHub's lookup order; the first file present is the only one it reads:
# https://docs.github.com/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners#codeowners-file-location
CODEOWNERS_PATHS = ('.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS')


def wildcard_owners(text):
    """The owners of a CODEOWNERS file's last `*` rule; [] when it has none.
    The last matching rule wins, so a later `*` line replaces an earlier one."""
    owners = []
    for line in text.splitlines():
        words = line.split('#', 1)[0].split()
        if words and words[0] == '*':
            owners = words[1:]
    return owners


def collect_codeowners(s):
    """s["codeowners_wildcard"]: the path of the CODEOWNERS file GitHub uses
    when its last `*` rule names the owner, else None. An unreadable file is an
    error line, and an empty one ends the lookup: a later location cannot stand
    in for either."""
    s['codeowners_wildcard'] = None
    for path in CODEOWNERS_PATHS:
        text = present_file_text(s['name'], path)
        if text is ABSENT:
            continue
        if text is None:
            s['errors'].append(f'{path} unreadable (API)')
        elif f'@{OWNER}' in (o.lower() for o in wildcard_owners(text)):
            s['codeowners_wildcard'] = path
        return


def codeowners_warning(path):
    return (
        f"{path}: '*' requests review from @{OWNER} on every pull request, "
        'bot-authored ones included'
    )


def compliance(s):
    """Return (hard_failures, warnings, accepted) for one repo's settings dict.

    Checks whose underlying API read failed (value None + an s["errors"]
    entry) are skipped — an API error must never masquerade as
    non-compliance. `accepted` holds warnings matched by the ACCEPTED table:
    known, documented deviations that would otherwise be permanent noise.
    """
    hard, warn = [], []
    if s.get('fatal'):
        return hard, warn, []

    two_channel = bool(s.get('two_channel'))
    for k, exp in GOV_HARD.items():
        if s.get(k) != exp:
            hard.append(f'{k}={s.get(k)} (want {exp})')
    for k, exp in GOV_SOFT.items():
        if s.get(k) != exp:
            warn.append(f'{k}={s.get(k)} (want {exp})')

    if s['default_branch'] == 'dev' and s['name'] in release_channels.SINGLE_MAIN_REPOS:
        hard.append(
            f'default_branch=dev on a single-main repo (want main; {s["name"]} publishes from main directly)'
        )
    elif s['default_branch'] == 'dev' and s['private']:
        hard.append(
            'default_branch=dev on a private repo (want main, because the two-branch model enrols public repos only)'
        )
    elif s['default_branch'] not in ('main', 'dev'):
        hard.append(
            f'default_branch={s["default_branch"]} (want dev, or main for a private or single-main repo)'
        )
    # A public main-default repo outside SINGLE_MAIN_REPOS: a release.yaml
    # caller is HARD because release.yaml refuses it and so it publishes
    # nothing; one publishing through its own publish.yaml only lacks the listing.
    if (
        s['default_branch'] == 'main'
        and not s['private']
        and s['name'] not in release_channels.SINGLE_MAIN_REPOS
    ):
        if s.get('release_caller'):
            hard.append(
                'default_branch=main with a release.yaml caller '
                '(want dev, because release.yaml publishes only from a dev default)'
            )
        elif s.get('own_publisher'):
            warn.append(
                'default_branch=main publishing through its own publish.yaml '
                '(list the repo in SINGLE_MAIN_REPOS)'
            )

    # Public repos only; the expected license is per repo (LICENSE_OVERRIDES).
    if not s['private']:
        want = LICENSE_OVERRIDES.get(s['name'], LICENSE_DEFAULT)
        if s['license'] is None:
            (hard if s['adopted'] else warn).append('license missing')
        elif s['license'] != want:
            warn.append(f'license {s["license"]} (want {want})')

    for dest in s.get('synced_drift') or []:
        warn.append(
            f'{dest} differs from its canonical in cplieger/ci '
            '(synced file, edit the canonical — the next sync overwrites this copy)'
        )
    warn += s.get('stale_majors') or []
    if s.get('codeowners_wildcard'):
        warn.append(codeowners_warning(s['codeowners_wildcard']))

    if two_channel:
        # Rulesets replace classic protection; a classic rule on main would also
        # block the promotion's fast-forward through the main ruleset's bypass.
        if s['has_protection']:
            hard.append('classic branch protection on dev (two-channel repos use the dev ruleset)')
        if s.get('main_protection'):
            hard.append(
                'classic branch protection on main (two-channel repos use the main ruleset)'
            )
        # rulesets_full is None when a ruleset read failed (already an [error]).
        for rname in CHANNEL_RULESETS if s.get('rulesets_full') is not None else ():
            actual = s['rulesets_full'].get(rname)
            if actual is None:
                hard.append(
                    f"ruleset '{rname}' missing (want the body in configs/rulesets/{rname}.json)"
                )
                continue
            for reason in ruleset_matches(expected_ruleset(rname), actual):
                hard.append(
                    f"ruleset '{rname}' differs from configs/rulesets/{rname}.json: {reason}"
                )
        if not s['has_protection'] and s.get('observed_complete'):
            observed = set(s.get('observed_checks') or [])
            for ctx in s['required_checks'] or []:
                if ctx not in observed:
                    hard.append(
                        f"required context '{ctx}' never reported by any "
                        'recent check run (phantom — blocks every PR as '
                        "'Expected'; the context must equal the check-run "
                        'name)'
                    )
        for tag in s.get('stable_tags_without_release') or []:
            hard.append(
                f'stable tag {tag} has no GitHub Release (the stable release run '
                'did not finish; rerun it or dispatch release.yaml on main)'
            )
        for tag in s.get('hand_made_stable_tags') or []:
            hard.append(
                f'stable tag {tag} and its Release were not created by the release '
                "pipeline (a hand-made tag becomes git-cliff's version base; delete both)"
            )
        for tag in s.get('dev_tags_without_receipt') or []:
            hard.append(
                f'dev tag {tag} carries no release/tag receipt on its commit (a hand-made '
                'dev tag skews the -dev.N counter and the change anchor; delete it)'
            )
        for tag in s.get('stable_tags_without_receipt') or []:
            hard.append(
                f'stable tag {tag} carries no {release_channels.completion_receipt_context(tag)} '
                'receipt on its commit, because its Release or registry readback did not finish. '
                'The next stable release run repairs it first, or rerun this one'
            )
        if s.get('renovate_json') is not None:
            hard += renovate_config_findings(s['renovate_json'], s['default_branch'])
    elif s['has_protection'] is False:
        hard.append('no branch protection on default branch')
    elif s['has_protection']:
        # 'ci / validate' from the meta workflow, or a bespoke CI's bare 'validate'.
        validate_ctxs = [c for c in (s['required_checks'] or []) if 'validate' in (c or '')]
        if not validate_ctxs:
            hard.append(f"required checks={s['required_checks']} (want a 'validate' check)")
        # An unpinned app (-1) or another app could satisfy the gate.
        for ctx in validate_ctxs:
            app = (s.get('required_check_apps') or {}).get(ctx)
            if app != ACTIONS_APP_ID:
                warn.append(
                    f"required check '{ctx}' pinned to app_id={app} "
                    f'(want {ACTIONS_APP_ID} = GitHub Actions)'
                )
        # The standard is the validate gate alone; deliberate extras go in ACCEPTED.
        for ctx in s['required_checks'] or []:
            if ctx not in validate_ctxs:
                warn.append(
                    f"unexpected extra required check '{ctx}' (standard is the validate gate alone)"
                )
        # A phantom context (see collect()), judged on complete reads only.
        if s.get('observed_complete'):
            observed = set(s.get('observed_checks') or [])
            for ctx in s['required_checks'] or []:
                if ctx not in observed:
                    hard.append(
                        f"required context '{ctx}' never reported by any "
                        'recent check run (phantom — blocks every PR as '
                        "'Expected'; the context must equal the check-run "
                        "name, e.g. 'smoke', not 'Smoke / smoke')"
                    )
        if s['strict']:
            warn.append('branch protection strict=on (want off)')
        if s['enforce_admins']:
            warn.append('enforce_admins=on (want off)')
        if s['allow_force_pushes']:
            warn.append('allow_force_pushes=on (want off)')
        if s['allow_deletions']:
            warn.append('allow_deletions=on (want off)')
        # No one can self-approve, so a review floor blocks every PR; the toggle at
        # count 0 gates nothing but is still drift.
        if s.get('required_review_count'):
            hard.append(
                f'required approving reviews='
                f'{s["required_review_count"]} — a single-maintainer '
                'repo cannot self-approve; every PR blocks (want off)'
            )
        elif s.get('required_reviews_present'):
            warn.append(
                'required_pull_request_reviews on (count=0, gates nothing; standard is off)'
            )
        if s.get('required_conversation_resolution'):
            warn.append('required_conversation_resolution=on (want off)')
        if s.get('required_linear_history'):
            warn.append(
                'required_linear_history=on (want off; the merge '
                'model already guarantees linear PR merges)'
            )
        if s.get('required_signatures'):
            warn.append(
                'required_signatures=on. Set it to off: the commits '
                'are unsigned, so this would block every merge'
            )
        if s.get('push_restrictions'):
            warn.append('push restrictions set (standard is none)')
        if s.get('lock_branch'):
            hard.append('branch locked (read-only — nothing can merge; want unlocked)')

    # Rulesets: drift on a single-main repo, and on a two-channel repo any but
    # the two compared above. A bypass actor warns unless the committed body
    # lists it; an Integration bypass is HARD everywhere (a stale app the API
    # cannot remove from a user-owned repo).
    expected_names = set(CHANNEL_RULESETS) if two_channel else set()
    for rs in s.get('custom_rulesets') or []:
        if rs['name'] in expected_names:
            continue
        standard = 'the dev and main rulesets' if two_channel else 'classic branch protection'
        warn.append(
            f"unexpected custom ruleset '{rs['name']}' ({rs['enforcement']}) "
            f'(standard is {standard})'
        )
    for rname, atype, aid, mode in s.get('ruleset_bypass_actors') or []:
        if atype == 'Integration':
            hard.append(
                f"ruleset '{rname}' has an Integration bypass actor (id {aid}) "
                '— likely a stale/decommissioned app; remove it'
            )
        elif (
            two_channel
            and rname in CHANNEL_RULESETS
            and (atype, aid, mode) in _bypass_actor_set(expected_ruleset(rname))
        ):
            continue
        else:
            warn.append(f"ruleset '{rname}' has a bypass actor ({atype} id {aid})")

    if s['vuln_alerts'] is False:
        hard.append('dependabot vulnerability alerts off (want on)')
    # Private vulnerability reporting is a public-repo feature; N/A on private.
    if not s['private'] and s['private_vuln_reporting'] is False:
        hard.append('private vulnerability reporting off (want on)')
    if s['dependabot_security_updates'] == 'enabled':
        hard.append('dependabot security UPDATES on (want off; Renovate owns deps)')
    if s.get('has_dependabot_yml'):
        warn.append(
            'stray .github/dependabot.yml enables Dependabot version '
            'PRs (want absent; Renovate owns deps)'
        )

    # The read default is defense in depth behind explicit `permissions:` blocks;
    # with auto-merge on, a token that approves PRs lands its own code.
    if s.get('default_workflow_permissions') not in (None, 'read'):
        warn.append(f'default workflow permissions={s["default_workflow_permissions"]} (want read)')
    if s.get('workflows_can_approve_prs'):
        hard.append(
            'workflows can approve PRs (want off — with auto-merge '
            'enabled this lets a workflow land its own code)'
        )

    # Free on public repos, GHAS on private ones.
    if not s['private']:
        if s['secret_scanning'] != 'enabled':  # noqa: S105 — API state, not a password
            warn.append('secret scanning off (want on)')
        if s['secret_scanning_push_protection'] != 'enabled':  # noqa: S105 — API state
            warn.append('secret scanning push protection off (want on)')

    # Synced to adopted repos; CodeQL is public-only without GHAS.
    if s['adopted'] and not s['infra'] and not s['private']:
        if s['has_codeql'] is False:
            warn.append('codeql.yml missing')
        if s['has_security_scan'] is False:
            warn.append('security.yml missing')
    # None: no root Dockerfile, or the read failed (already an [error]). Only a
    # two-channel repo publishes an image: release.yaml refuses the rest.
    if two_channel and s.get('dockerhub_secrets') is False:
        hard.append(
            'DOCKERHUB_USERNAME/DOCKERHUB_TOKEN secrets missing '
            '(dual-publish image repo — the next release fails at '
            'the Docker Hub login)'
        )

    # ci_wired None is an unread file (already an [error]); BESPOKE_CI is exempt.
    if not s['infra'] and s['name'] not in BESPOKE_CI and s['adopted'] and s['ci_wired'] is False:
        hard.append('CI not wired to cplieger/ci')
    if s['renovate_preset'] is False:
        hard.append(
            'org-inherited-config.json does not extend the preset '
            '(this file is what delivers Renovate to every repo)'
        )

    # Public repos only; the house standard is 2-4 topics.
    if not s['private']:
        if not s['desc_present']:
            warn.append('description empty')
        elif s['desc_len'] > 100:
            warn.append(f'description {s["desc_len"]} chars (>100; Docker Hub short-desc limit)')
        if len(s['topics']) < 2:
            warn.append(f'{len(s["topics"])} topics (want at least 2)')
        # The counter never follows a /vN bump or a rename. Judged only when the
        # page named a package and the expected one is selectable (a new /vN path
        # is often never indexed); an unreadable page is skipped.
        exp = (s.get('expected_package') or '').lstrip('@')
        selectable = {p.lstrip('@') for p in s.get('used_by_selectable') or []}
        if (
            s.get('used_by_package')
            and exp
            and s['used_by_package'].lstrip('@') != exp
            and exp in selectable
        ):
            warn.append(
                f"used-by counter shows '{s['used_by_package']}' "
                f"(want '{s['expected_package']}'; no API — fix by "
                'hand: Settings -> Advanced Security -> Used by counter)'
            )
        # Module path: github.com/<owner>/<repo>, plus /vN for a library (never for an
        # image app), else Go tooling cannot fetch it and it indexes a phantom
        # dependency-graph package.
        if s.get('go_module'):
            want = f'github.com/{OWNER}/{s["name"]}'
            mod = s['go_module']
            if s.get('has_dockerfile'):
                if re.fullmatch(re.escape(want) + r'/v\d+', mod):
                    warn.append(
                        f"go.mod module '{mod}' carries a /vN suffix "
                        f"(want '{want}'; apps use the plain module "
                        'path at every major — drop the suffix and '
                        'rewrite internal imports)'
                    )
                elif mod != want:
                    warn.append(
                        f"go.mod module '{mod}' is not the repo path "
                        f"(want '{want}'; unfetchable by Go tooling "
                        'and indexes a phantom dependency-graph '
                        'package)'
                    )
            elif not re.fullmatch(re.escape(want) + r'(/v\d+)?', mod):
                warn.append(
                    f"go.mod module '{mod}' is not the repo "
                    f"path (want '{want}' [+/vN]; unfetchable by Go "
                    'tooling and indexes a phantom dependency-graph '
                    'package)'
                )

    # README presence and the Hub markers are HARD (without the markers the Hub
    # page stops updating); the footer and License-last are warnings.
    # readme_text None is an unread file; '' is absent.
    if not s['private'] and s.get('readme_text') is not None:
        txt = s['readme_text']
        if not txt.strip():
            hard.append('README.md missing')
        else:
            norm = ' '.join(txt.split())
            if FOOTER_AI_NOTE not in norm:
                warn.append(
                    'README missing the canonical AI-assistance note. Copy FOOTER_AI_NOTE exactly'
                )
            if s['name'] != '.github':
                if FOOTER_DISCLAIMER not in norm:
                    warn.append(
                        'README missing the canonical Disclaimer block. '
                        'Copy FOOTER_DISCLAIMER exactly'
                    )
                headings = re.findall(r'^## +(.+?)\s*$', txt, re.MULTILINE)
                if headings and headings[-1] != 'License':
                    warn.append(
                        f"README's last section is '{headings[-1]}'. Make License the last section"
                    )
            if s.get('has_dockerfile') and not (HUB_MARKER_BEGIN in txt and HUB_MARKER_END in txt):
                hard.append(
                    f"README carries no '{HUB_MARKER_BEGIN}' / "
                    f"'{HUB_MARKER_END}' pair, so the release cannot "
                    'build the Docker Hub overview page and the Hub '
                    'listing stops being updated'
                )
        if s.get('compose_example') is False:
            warn.append(
                'compose.yaml example missing. Every image repo ships a reference compose file'
            )

    # Each hook defect here is silent and stops deploys; NO_DEPLOY_HOOK repos have
    # no orchestrator, and an unreadable token is a global skip in main().
    if WEBHOOK_HOST and s['webhook_readable'] and s['name'] not in NO_DEPLOY_HOOK:
        hooks = s.get('webhooks') or []
        if s['name'] in PUSH_WEBHOOK_REPOS:
            want_event = 'push'
        elif two_channel and s['name'] in release_channels.DEPLOYED_IMAGE_REPOS:
            want_event = REGISTRY_EVENT
        elif two_channel:
            # A library on the dev channel triggers nothing at the orchestrator:
            # no hook is expected, and one left on the release event is stale.
            want_event = None
        else:
            want_event = 'release'
        if want_event is None:
            for h in hooks:
                if 'release' in (h['events'] or []):
                    warn.append(
                        f'stale hook: deploy-trigger webhook events={h["events"]} '
                        '(a dev-channel library needs no deploy hook; remove it)'
                    )
            hooks = []
        if want_event is not None and not hooks:
            hard.append("no deploy-trigger webhook (releases won't reach the orchestrator)")
        elif len(hooks) > 1:
            warn.append(
                f'{len(hooks)} deploy-trigger webhooks (want exactly 1; '
                'duplicates double-fire the orchestrator)'
            )
        for h in hooks:
            if not h['has_secret']:
                hard.append(
                    'deploy-trigger webhook has no secret '
                    '(the orchestrator rejects unsigned deliveries)'
                )
            if want_event not in (h['events'] or []):
                hard.append(
                    f'deploy-trigger webhook events={h["events"]} lack '
                    f"'{want_event}' (it never fires, so deploys never "
                    'trigger)'
                )
            elif set(h['events']) != {want_event}:
                warn.append(
                    f"deploy-trigger webhook events={h['events']} (want exactly ['{want_event}'])"
                )
            if h['insecure_ssl'] != '0':
                hard.append(
                    f'deploy-trigger webhook insecure_ssl='
                    f'{h["insecure_ssl"]!r} (TLS verification disabled; '
                    "want '0')"
                )
            if h['content_type'] != 'json':
                warn.append(
                    f'deploy-trigger webhook content_type='
                    f'{h["content_type"]} (want json; the orchestrator '
                    'parses a JSON body)'
                )
            if h['bad_delivery']:
                warn.append(
                    f'deploy-trigger webhook last delivery failed (HTTP {h["bad_delivery"]})'
                )

    # Only warnings can be accepted.
    rules = ACCEPTED.get(s['name'], {})
    kept, accepted = [], []
    for w in warn:
        if any(w.startswith(prefix) for prefix in rules):
            accepted.append(w)
        else:
            kept.append(w)

    return hard, kept, accepted


# A repo younger than this is graded and printed, but nothing is filed or
# closed for it while it is still being set up.
GRACE = timedelta(hours=24)
ISSUE_LABEL = 'repo-audit'
ISSUE_TITLE = 'Repository audit findings'


def in_grace(created_at, now):
    """True when the repo was created less than GRACE before `now`; None when
    `created_at` is not a zoned ISO timestamp."""
    if not isinstance(created_at, str):
        return None
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if created.tzinfo is None:
        return None
    return now - created < GRACE


def fenced(lines):
    """`lines` inside a code fence longer than any backtick run they hold, so a
    finding renders literally (an HTML comment in one would otherwise vanish)."""
    longest = max((len(run) for line in lines for run in re.findall(r'`+', line)), default=0)
    fence = '`' * max(3, longest + 1)
    return [fence + 'text', *lines, fence]


def run_url():
    parts = [
        os.environ.get(k, '') for k in ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID')
    ]
    return '{}/{}/actions/runs/{}'.format(*parts) if all(parts) else 'a local run'


ISSUE_INTRO = (
    'Findings of the governance audit in cplieger/ci. This issue is updated on '
    'each scheduled run and closed when the repository audits clean.'
)


def issue_body(hard, warn, run):
    lines = [ISSUE_INTRO, '']
    if hard:
        lines += ['## HARD findings', '', *fenced([f'[HARD] {h}' for h in hard]), '']
    if warn:
        lines += ['## Warnings', '', *fenced([f'[warn] {w}' for w in warn]), '']
    return '\n'.join([*lines, f'Run: {run}', ''])


def write_issue(name, mode, text):
    """tracker_issue's exit code for one repo's `mode` (upsert or
    close-when-clean) with `text` as the body or the closing comment."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'text.md'
        path.write_text(text, encoding='utf-8')
        flag = '--body-file' if mode == 'upsert' else '--comment-file'
        argv = [
            *('--repo', f'{OWNER}/{name}', '--label', ISSUE_LABEL, '--title', ISSUE_TITLE),
            *('--mode', mode, flag, str(path)),
        ]
        try:
            return tracker_issue.main(argv, allow_reserved=True)
        except Exception as err:  # one repo's failure must not stop the others
            print(f'::error::{name}: {err}', file=sys.stderr)
            return 1


def load_findings(path):
    """The findings dict --findings-out wrote; None, with the reason on stderr,
    when it is unreadable or from a scoped run."""
    try:
        findings = json.loads(Path(path).read_text(encoding='utf-8'))
    except OSError as err:
        sys.stderr.write(f'error: findings file unreadable: {err}\n')
        return None
    except ValueError as err:
        sys.stderr.write(f'error: findings file is not JSON: {err}\n')
        return None
    if not isinstance(findings, dict) or not isinstance(findings.get('repos'), list):
        sys.stderr.write('error: findings file has no repos list\n')
        return None
    # A scoped run's file names only some repos, and the bootstrap gate's
    # --repo run must never file.
    if findings.get('scope') is not None or findings.get('visibility') != 'all':
        sys.stderr.write(
            'error: the findings come from a scoped run (--repo or --visibility). '
            'Only a full run files issues.\n'
        )
        return None
    return findings


def file_issues(path, dry_run):
    """Open, update or close each graded repo's audit issue from a findings
    file. Exit code: 0 done, 1 some repo failed to file, 2 unusable file."""
    findings = load_findings(path)
    if findings is None:
        return 2
    run = run_url()
    counts = {
        'upsert': 0,
        'close-when-clean': 0,
        'grace': 0,
        'errors': 0,
        'disabled': 0,
        'private': 0,
    }
    failed = []
    for row in findings['repos']:
        name = row['name']
        # Public repos only: the filing token is not known to write issues on
        # private ones.
        if row.get('visibility') != 'public':
            counts['private'] += 1
            print(f'::notice::{name}: not public, so its findings stay in the run summary')
            continue
        if row.get('in_grace') is not False:
            counts['grace'] += 1
            print(f'{name}: skipped, created less than {GRACE // timedelta(hours=1)}h ago')
            continue
        # A partial read never opens, edits or closes an issue.
        if row.get('errors') is not False:
            counts['errors'] += 1
            print(f"{name}: skipped, this run's read hit API errors (its issue is left as it is)")
            continue
        hard = row.get('hard') or []
        warn = row.get('warnings') or []
        mode = 'upsert' if hard or warn else 'close-when-clean'
        counts[mode] += 1
        if not row.get('has_issues'):
            counts['disabled'] += 1
        if dry_run:
            note = '' if row.get('has_issues') else ' (issues disabled, so the tracker skips it)'
            print(f'{name}: would {mode} ({len(hard)} HARD, {len(warn)} warnings){note}')
            continue
        text = (
            issue_body(hard, warn, run)
            if mode == 'upsert'
            else f'The governance audit found nothing to report in {run}.\n'
        )
        if write_issue(name, mode, text) != 0:
            failed.append(name)
    print(
        f'{"dry run: " if dry_run else ""}{counts["upsert"]} upsert · '
        f'{counts["close-when-clean"]} close-when-clean · {counts["grace"]} in grace · '
        f'{counts["errors"]} skipped on API errors · {counts["disabled"]} with issues disabled'
        f' · {counts["private"]} not public · {len(failed)} failed'
    )
    if failed:
        sys.stderr.write(f'error: filing failed for {", ".join(failed)}\n')
        return 1
    return 0


# The listing fields a repo's meta carries into collect() and the summary.
DISCOVERED = (
    'name',
    'archived',
    'fork',
    'visibility',
    'default_branch',
    'created_at',
    'has_issues',
)


def main():
    ap = argparse.ArgumentParser(description='cplieger governance audit')
    ap.add_argument('--visibility', choices=['all', 'public', 'private'], default='all')
    ap.add_argument(
        '--repo',
        action='append',
        metavar='NAME',
        help='audit only this repo. Pass it more than once for several repos',
    )
    ap.add_argument('--dump', metavar='PATH', help='write raw collected settings as JSON')
    ap.add_argument(
        '--findings-out',
        metavar='PATH',
        help="write each graded repo's findings as JSON, for --file-issues",
    )
    ap.add_argument(
        '--file-issues',
        metavar='PATH',
        help="open, update or close each repo's audit issue from a full run's "
        '--findings-out file, then exit (needs a token that writes issues)',
    )
    ap.add_argument(
        '--dry-run',
        action='store_true',
        help="with --file-issues: print each repo's planned action, write nothing",
    )
    args = ap.parse_args()
    if args.file_issues:
        if args.repo or args.dump or args.findings_out or args.visibility != 'all':
            ap.error('--file-issues reads its findings file and takes no audit option')
        sys.exit(file_issues(args.file_issues, args.dry_run))
    if args.dry_run:
        ap.error('--dry-run applies to --file-issues only')

    try:
        all_metas = REST.pages('user/repos?affiliation=owner')
    except ghrest.ApiError as err:
        sys.stderr.write(f'repo listing failed: {err}\n')
        sys.exit(2)
    # Forks keep upstream's governance, so they are listed and skipped.
    forks = sorted(m['name'] for m in all_metas if m.get('fork') and not m['archived'])
    metas = [
        {k: m.get(k) for k in DISCOVERED}
        for m in all_metas
        if not m['archived'] and not m.get('fork')
    ]
    if args.visibility != 'all':
        metas = [m for m in metas if (m.get('visibility') or '').lower() == args.visibility]
    if args.repo:
        want = set(args.repo)
        known = {m['name'] for m in metas}
        unknown = sorted(want - known)
        if unknown:
            sys.stderr.write(
                f'error: unknown (or archived/fork/filtered) repo(s): {", ".join(unknown)}\n'
            )
            sys.exit(2)
        metas = [m for m in metas if m['name'] in want]
    metas.sort(key=lambda m: m['name'])
    latest = LatestMajors()
    owned = {m['name'] for m in all_metas}

    def audit_repo(meta):
        s = collect(meta)
        if not s.get('fatal'):
            collect_first_party_majors(s, latest, owned)
            collect_codeowners(s)
        return s

    with ThreadPoolExecutor(max_workers=8) as pool:
        settings = list(pool.map(audit_repo, metas))

    if args.dump:
        with open(args.dump, 'w', encoding='utf-8') as fh:
            json.dump(settings, fh, indent=2, sort_keys=True)

    # No repo readable at all is an outage, not an under-scoped token.
    if settings and all(s.get('fatal') for s in settings):
        sys.stderr.write(
            'ERROR: no repo could be read at all (API outage or network '
            'failure). No compliance conclusions drawn.\n'
        )
        sys.exit(2)

    # No repo exposing the merge-model fields means an under-scoped token: abort
    # rather than flag every repo.
    if settings and not any(s['admin_visible'] for s in settings):
        sys.stderr.write(
            'ERROR: no repo returned the merge-model fields (allow_merge_commit '
            'etc.). The audit token is under-scoped.\n'
            "These fields are only exposed to a CLASSIC PAT with the 'repo' "
            'scope. A fine-grained PAT does NOT serialize them, even with '
            'Administration:read and an owner role. Set the AUDIT_PAT secret to '
            "a classic PAT with 'repo' scope. The default GITHUB_TOKEN also "
            'cannot read these fields.\n'
        )
        sys.exit(2)

    hard_total, warn_total, accepted_total, error_total, clean = 0, 0, 0, 0, 0
    grace_total = sum(s.get('tags_in_grace') or 0 for s in settings)
    scope = f', repos={",".join(sorted(set(args.repo)))}' if args.repo else ''
    print(f'GOVERNANCE COMPLIANCE — {len(settings)} repos (visibility={args.visibility}{scope})')
    print(
        'Legend: [HARD] blocks compliance · [warn] advisory · [error] API '
        'read failed (check skipped) · GHAS scanning N/A on free private '
        'repos · accepted deviations suppressed (see ACCEPTED)\n'
    )

    # A set host with no readable hooks anywhere is an under-scoped token (it
    # needs the classic 'repo' scope or admin:repo_hook).
    if forks:
        print(
            f'Note: {len(forks)} fork(s) skipped (upstream governance applies): '
            f'{", ".join(forks)}\n'
        )
    if not WEBHOOK_HOST:
        print('Note: deploy-trigger webhook check skipped (AUDIT_WEBHOOK_HOST unset).\n')
    elif not any(s['webhook_readable'] for s in settings):
        print(
            "WARNING: AUDIT_WEBHOOK_HOST is set but no repo's webhooks were "
            'readable; the deploy-trigger webhook check was skipped. The audit '
            "token needs the classic 'repo' scope (or admin:repo_hook).\n"
        )
    # Every dependents page unreadable means github.com HTML is unreachable here.
    attempted = [s for s in settings if s.get('used_by_attempted')]
    if attempted and not any(s['used_by_readable'] for s in attempted):
        print(
            'Note: used-by counter check skipped (github.com dependents '
            'pages unreadable from this network).\n'
        )
    deferred = sorted(s['name'] for s in settings if s.get('version_tags_deferred'))
    if deferred:
        print(
            f'Note: version tags not graded on {len(deferred)} repo(s) with a workflow '
            f'run live or ended within {RECEIPT_GRACE // timedelta(hours=1)}h: '
            f'{", ".join(deferred)}\n'
        )
    now = datetime.now(UTC)
    rows = []
    for meta, s in zip(metas, settings, strict=True):
        hard, warn, accepted = compliance(s)
        grace = in_grace(meta.get('created_at'), now)
        if grace is None:
            s.setdefault('errors', []).append(
                'creation time unreadable (listing), so nothing filed'
            )
        errors = s.get('errors') or []
        rows.append(
            {
                'name': s['name'],
                'visibility': meta.get('visibility'),
                'has_issues': meta.get('has_issues'),
                'in_grace': bool(grace),
                'errors': bool(errors),
                'hard': hard,
                'warnings': warn,
            }
        )
        accepted_total += len(accepted)
        tag = 'infra' if s['infra'] else ('priv' if s['private'] else 'pub')
        if not hard and not warn and not errors:
            clean += 1
            continue
        print(f'{s["name"]}  ({tag})')
        for h in hard:
            print(f'  [HARD] {h}')
            hard_total += 1
        for w in warn:
            print(f'  [warn] {w}')
            warn_total += 1
        for e in errors:
            print(f'  [error] {e}')
            error_total += 1
        print()

    print('-' * 60)
    print(
        f'{clean} clean · {hard_total} hard failures · {warn_total} warnings'
        f' · {accepted_total} accepted deviations · {error_total} API errors'
        f' · {grace_total} version tags younger than {RECEIPT_GRACE // timedelta(hours=1)}h'
        ' not graded'
        f' · {sum(r["in_grace"] for r in rows)} repos in grace'
        f' (created < {GRACE // timedelta(hours=1)}h, nothing filed)'
    )
    if args.findings_out:
        findings = {
            'scope': sorted(set(args.repo)) if args.repo else None,
            'visibility': args.visibility,
            'repos': rows,
        }
        with open(args.findings_out, 'w', encoding='utf-8') as fh:
            json.dump(findings, fh, indent=2, sort_keys=True)
    if hard_total:
        sys.exit(1)
    if error_total:
        # Exit 2: some checks could not be verified, which is infra, not drift.
        sys.exit(2)


if __name__ == '__main__':
    main()
