"""Tests for audit.py's grading of the two branch models, on hand-built settings."""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import audit
import yaml

HOST = 'deploy.example'
DEV_RULESET = audit.expected_ruleset('dev')
MAIN_RULESET = audit.expected_ruleset('main')
MAIN_DEFAULT = Path(__file__).resolve().parent / 'testdata' / 'audit' / 'main-default.json'
RENOVATE_JSON = '{"extends": ["github>cplieger/.github:two-branch"]}\n'


def live(ruleset: dict) -> dict:
    """A ruleset as the API returns it: the committed body plus filled-in fields."""
    rs = copy.deepcopy(ruleset)
    rs.update(
        {
            'id': 42,
            'source': 'cplieger/x',
            'source_type': 'Repository',
            'current_user_can_bypass': 'always',
        }
    )
    for rule in rs['rules']:
        if rule['type'] == 'pull_request':
            rule['parameters']['allowed_merge_methods'] = ['squash', 'rebase']
            rule['parameters']['automatic_copilot_code_review_enabled'] = False
    return rs


def hook(events: list[str]) -> dict:
    return {
        'events': sorted(events),
        'content_type': 'json',
        'insecure_ssl': '0',
        'has_secret': True,
        'bad_delivery': None,
    }


def base_settings(name: str, default_branch: str) -> dict:
    """A private repo (no docs, license or topic checks) that grades clean."""
    s = {
        'name': name,
        'infra': False,
        'errors': [],
        'fatal': False,
        'visibility': 'private',
        'private': True,
        'admin_visible': True,
        'default_branch': default_branch,
        'two_channel': False,
        'license': None,
        'desc_present': True,
        'desc_len': 20,
        'topics': ['a', 'b'],
        'secret_scanning': 'enabled',
        'secret_scanning_push_protection': 'enabled',
        'dependabot_security_updates': 'disabled',
        'vuln_alerts': True,
        'private_vuln_reporting': True,
        'has_protection': False,
        'main_protection': None,
        'required_checks': [],
        'required_check_apps': {},
        'strict': None,
        'enforce_admins': None,
        'allow_force_pushes': None,
        'allow_deletions': None,
        'required_reviews_present': False,
        'required_review_count': 0,
        'required_conversation_resolution': None,
        'required_linear_history': None,
        'required_signatures': None,
        'lock_branch': None,
        'push_restrictions': False,
        'observed_checks': ['ci / validate'],
        'observed_complete': True,
        'custom_rulesets': [],
        'ruleset_bypass_actors': [],
        'rulesets_full': {},
        'stable_tags_without_release': [],
        'hand_made_stable_tags': [],
        'dev_tags_without_receipt': [],
        'default_workflow_permissions': 'read',
        'workflows_can_approve_prs': False,
        'has_codeql': True,
        'has_security_scan': True,
        'publishes': True,
        'has_dockerfile': False,
        'go_module': None,
        'expected_package': None,
        'used_by_package': None,
        'used_by_selectable': [],
        'used_by_attempted': False,
        'used_by_readable': False,
        'has_dependabot_yml': False,
        'dockerhub_secrets': None,
        'synced_drift': [],
        'readme_text': None,
        'compose_example': None,
        'ci_wired': True,
        'renovate_preset': None,
        'adopted': True,
        'webhook_readable': True,
        'webhooks': [hook(['release'])],
    }
    s.update(audit.GOV_HARD)
    s.update(audit.GOV_SOFT)
    return s


def two_channel(name: str = 'httpx') -> dict:
    """A public dev-default repo, graded clean when the branch model enrols it."""
    s = base_settings(name, 'dev')
    s.update(
        {
            'visibility': 'public',
            'private': False,
            'license': audit.LICENSE_OVERRIDES.get(name, audit.LICENSE_DEFAULT),
        }
    )
    s['two_channel'] = audit.release_channels.is_two_branch(
        {
            'name': name,
            'default_branch': 'dev',
            'visibility': 'public',
            'fork': False,
            'archived': False,
        }
    )
    s.update(audit.GOV_HARD)
    s['rulesets_full'] = {'dev': live(DEV_RULESET), 'main': live(MAIN_RULESET)}
    s['custom_rulesets'] = [
        {'name': 'dev', 'enforcement': 'active'},
        {'name': 'main', 'enforcement': 'active'},
    ]
    s['ruleset_bypass_actors'] = [('main', 'RepositoryRole', 5, 'always')]
    s['required_checks'] = ['ci / validate']
    s['required_check_apps'] = {'ci / validate': 15368}
    s['webhooks'] = []
    s['stable_tags_without_receipt'] = []
    s['renovate_json'] = RENOVATE_JSON
    return s


def legacy(name: str = 'httpx') -> dict:
    s = base_settings(name, 'main')
    s['has_protection'] = True
    s['required_checks'] = ['ci / validate']
    s['required_check_apps'] = {'ci / validate': 15368}
    s['strict'] = False
    s['enforce_admins'] = False
    s['allow_force_pushes'] = False
    s['allow_deletions'] = False
    return s


class TwoChannel(unittest.TestCase):
    def setUp(self):
        self.saved_host = audit.WEBHOOK_HOST
        audit.WEBHOOK_HOST = HOST

    def tearDown(self):
        audit.WEBHOOK_HOST = self.saved_host

    def test_clean_two_channel_repo(self):
        hard, warn, _ = audit.compliance(two_channel())
        self.assertEqual(hard, [])
        self.assertEqual(warn, [])

    def test_api_filled_parameter_keys_are_tolerated(self):
        self.assertEqual(audit.ruleset_matches(DEV_RULESET, live(DEV_RULESET)), [])
        self.assertEqual(audit.ruleset_matches(MAIN_RULESET, live(MAIN_RULESET)), [])

    def test_missing_main_ruleset_is_hard(self):
        s = two_channel()
        del s['rulesets_full']['main']
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any("ruleset 'main' missing" in h for h in hard), hard)

    def collect_rulesets_with(self, answers: dict) -> dict:
        original = audit.gh_json
        audit.gh_json = lambda *args: answers[args[-1]]
        s = two_channel()
        try:
            audit.collect_rulesets('httpx', s)
        finally:
            audit.gh_json = original
        return s

    def test_a_failed_ruleset_list_read_is_one_error_and_no_finding(self):
        s = self.collect_rulesets_with({'repos/cplieger/httpx/rulesets': audit.API_ERROR})
        self.assertEqual(s['errors'], ['rulesets unreadable (API); rulesets not graded'])
        self.assertIsNone(s['rulesets_full'])
        hard, warn, _ = audit.compliance(s)
        self.assertEqual([h for h in hard if 'ruleset' in h], [])
        self.assertEqual([w for w in warn if 'ruleset' in w], [])

    def test_a_failed_full_ruleset_read_grades_no_ruleset(self):
        s = self.collect_rulesets_with(
            {
                'repos/cplieger/httpx/rulesets': [
                    {'id': 1, 'name': 'dev'},
                    {'id': 2, 'name': 'main'},
                ],
                'repos/cplieger/httpx/rulesets/1': live(DEV_RULESET),
                'repos/cplieger/httpx/rulesets/2': audit.API_ERROR,
            }
        )
        self.assertEqual(s['errors'], ["ruleset 'main' unreadable (API); rulesets not graded"])
        self.assertIsNone(s['rulesets_full'])
        self.assertIsNone(s['ruleset_bypass_actors'])
        hard, warn, _ = audit.compliance(s)
        self.assertEqual([h for h in hard if 'ruleset' in h], [])
        self.assertEqual([w for w in warn if 'ruleset' in w], [])

    def test_a_complete_ruleset_read_still_grades(self):
        s = self.collect_rulesets_with(
            {
                'repos/cplieger/httpx/rulesets': [
                    {'id': 1, 'name': 'dev'},
                    {'id': 2, 'name': 'tags'},
                ],
                'repos/cplieger/httpx/rulesets/1': live(DEV_RULESET),
                'repos/cplieger/httpx/rulesets/2': {'enforcement': 'active', 'rules': []},
            }
        )
        self.assertEqual(s['errors'], [])
        hard, warn, _ = audit.compliance(s)
        self.assertTrue(any("ruleset 'main' missing" in h for h in hard), hard)
        self.assertTrue(any("unexpected custom ruleset 'tags'" in w for w in warn), warn)

    def test_extra_rule_is_hard(self):
        s = two_channel()
        s['rulesets_full']['dev']['rules'].append({'type': 'required_signatures'})
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any("ruleset 'dev' differs" in h and 'rule types' in h for h in hard), hard)

    def test_changed_parameter_is_hard(self):
        s = two_channel()
        for rule in s['rulesets_full']['dev']['rules']:
            if rule['type'] == 'pull_request':
                rule['parameters']['required_approving_review_count'] = 1
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any('required_approving_review_count=1' in h for h in hard), hard)

    def test_status_check_integration_id_is_compared(self):
        s = two_channel()
        for rule in s['rulesets_full']['dev']['rules']:
            if rule['type'] == 'required_status_checks':
                rule['parameters']['required_status_checks'][0]['integration_id'] = -1
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any('required_status_checks=' in h for h in hard), hard)

    def test_bypass_actor_drift_is_hard(self):
        s = two_channel()
        s['rulesets_full']['main']['bypass_actors'] = []
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any('bypass actors' in h for h in hard), hard)

    def test_disabled_enforcement_is_hard(self):
        s = two_channel()
        s['rulesets_full']['dev']['enforcement'] = 'disabled'
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any("enforcement='disabled'" in h for h in hard), hard)

    def test_classic_protection_on_dev_is_hard(self):
        s = two_channel()
        s['has_protection'] = True
        hard, _, _ = audit.compliance(s)
        self.assertIn(
            'classic branch protection on dev (two-channel repos use the dev ruleset)', hard
        )

    def test_classic_protection_on_main_is_hard(self):
        s = two_channel()
        s['main_protection'] = True
        hard, _, _ = audit.compliance(s)
        self.assertIn(
            'classic branch protection on main (two-channel repos use the main ruleset)', hard
        )

    def test_phantom_context_from_the_dev_ruleset(self):
        s = two_channel()
        s['observed_checks'] = ['ci / go / validate']
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any('phantom' in h for h in hard), hard)

    def test_deployed_repo_needs_registry_package_hook(self):
        s = two_channel('knell')
        s['webhooks'] = [hook(['release'])]
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any("lack 'registry_package'" in h for h in hard), hard)
        s['webhooks'] = [hook(['registry_package'])]
        hard, warn, _ = audit.compliance(s)
        self.assertEqual(hard, [])
        self.assertEqual(warn, [])

    def test_deployed_repo_without_hook_is_hard(self):
        s = two_channel('knell')
        hard, _, _ = audit.compliance(s)
        self.assertIn("no deploy-trigger webhook (releases won't reach the orchestrator)", hard)

    def test_library_release_hook_is_a_stale_warning(self):
        s = two_channel('httpx')
        s['webhooks'] = [hook(['release'])]
        hard, warn, _ = audit.compliance(s)
        self.assertEqual(hard, [])
        self.assertTrue(any(w.startswith('stale hook') for w in warn), warn)

    def test_stable_tag_without_release_is_hard(self):
        s = two_channel()
        s['stable_tags_without_release'] = ['v5.0.4']
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any('stable tag v5.0.4 has no GitHub Release' in h for h in hard), hard)

    def test_hand_made_version_tags_of_either_shape_are_hard(self):
        s = two_channel()
        s['hand_made_stable_tags'] = ['v9.0.0']
        s['dev_tags_without_receipt'] = ['v5.1.0-dev.3']
        hard, _, _ = audit.compliance(s)
        self.assertTrue(
            any(
                'stable tag v9.0.0 and its Release were not created by the release pipeline' in h
                for h in hard
            ),
            hard,
        )
        self.assertTrue(
            any('dev tag v5.1.0-dev.3 carries no release/tag receipt' in h for h in hard), hard
        )

    def test_excluding_the_protected_branch_is_hard(self):
        s = two_channel()
        s['rulesets_full']['dev']['conditions']['ref_name']['exclude'] = ['refs/heads/dev']
        hard, _, _ = audit.compliance(s)
        self.assertTrue(
            any("ruleset 'dev' differs" in h and 'branch exclude' in h for h in hard), hard
        )

    def test_unexpected_condition_keys_are_hard(self):
        actual = live(MAIN_RULESET)
        actual['conditions']['repository_name'] = {'include': ['~ALL']}
        self.assertTrue(
            any('condition keys' in r for r in audit.ruleset_matches(MAIN_RULESET, actual))
        )
        actual = live(MAIN_RULESET)
        actual['conditions']['ref_name']['protected'] = True
        self.assertTrue(
            any(
                'unexpected ref_name keys' in r for r in audit.ruleset_matches(MAIN_RULESET, actual)
            )
        )

    def test_single_main_repo_on_dev_is_hard(self):
        s = two_channel('ci')
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any('single-main repo' in h for h in hard), hard)

    def test_other_custom_ruleset_still_warns(self):
        s = two_channel()
        s['custom_rulesets'].append({'name': 'tags', 'enforcement': 'active'})
        _, warn, _ = audit.compliance(s)
        self.assertTrue(any("unexpected custom ruleset 'tags'" in w for w in warn), warn)

    def test_integration_bypass_stays_hard(self):
        s = two_channel()
        s['ruleset_bypass_actors'].append(('main', 'Integration', 1234, 'always'))
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any('Integration bypass actor' in h for h in hard), hard)


def committed(when: datetime) -> dict:
    """A commit as the API returns it, reduced to the field the audit reads."""
    return {'commit': {'committer': {'date': when.isoformat().replace('+00:00', 'Z')}}}


def listing(*runs: dict) -> dict:
    """A workflow-runs listing as the API returns it, reduced to the two keys
    the audit requires of one."""
    return {'total_count': len(runs), 'workflow_runs': list(runs)}


def gh_ok(body) -> subprocess.CompletedProcess:
    """`gh api -i` on HTTP 200: the status line, a header, a blank line, the body."""
    raw = body if isinstance(body, str) else json.dumps(body)
    return subprocess.CompletedProcess(
        ['gh'], 0, stdout=f'HTTP/2.0 200 OK\nContent-Type: application/json\r\n\r\n{raw}', stderr=''
    )


def gh_http(status: int) -> subprocess.CompletedProcess:
    """`gh api -i` on an HTTP error: exit 1, the response on stdout as on a
    success, and gh's one stderr line."""
    body = json.dumps({'message': 'from the API', 'status': str(status)})
    return subprocess.CompletedProcess(
        ['gh'],
        1,
        stdout=f'HTTP/2.0 {status} Error\nContent-Type: application/json\r\n\r\n{body}',
        stderr=f'gh: from the API (HTTP {status})',
    )


def gh_path(args) -> str:
    """The API path of a faked `gh` call: the last word of a REST read (`api -i
    PATH`), the second of the pre-REST form a baseline module makes."""
    return args[-1] if '-i' in args else args[1]


def legacy_reply(answer, args) -> subprocess.CompletedProcess:
    """The pre-REST `gh api PATH [--jq .content]` reply, for a baseline module:
    the bare body on stdout, an HTTP error's status on stderr alone."""
    if answer is None:
        return subprocess.CompletedProcess(
            ['gh'], 1, stdout='{}', stderr='gh: from the API (HTTP 404)'
        )
    out = answer['content'] if '--jq' in args else json.dumps(answer)
    return subprocess.CompletedProcess(['gh'], 0, stdout=out, stderr='')


def audit_over_answers(module, scenario: dict) -> dict:
    """`module`'s collect() and compliance() for one repo whose every API path
    answers from scenario['answers'] (a path it lacks is a 404). The module is
    passed in so a baseline copy of audit.py, which made the pre-REST calls, can
    be driven through the same replies."""
    answers, reads = scenario['answers'], []

    def fake_gh(*args):
        path = gh_path(args)
        reads.append(path)
        if '-i' not in args:
            return legacy_reply(answers.get(path), args)
        if path not in answers:
            return gh_http(404)
        return gh_ok(answers[path])

    saved = (module.gh, module.used_by_package_scrape, module.WEBHOOK_HOST)
    module.gh = fake_gh
    module.used_by_package_scrape = lambda name: tuple(scenario['used_by'])
    module.WEBHOOK_HOST = HOST
    module._canonical_cache.clear()  # noqa: SLF001 (a cached canonical would leak across scenarios)
    try:
        s = module.collect(scenario['meta'])
        hard, warn, accepted = module.compliance(s)
    finally:
        module.gh, module.used_by_package_scrape, module.WEBHOOK_HOST = saved
    return {'hard': hard, 'warn': warn, 'accepted': accepted, 'errors': s['errors'], 'reads': reads}


class TagProvenance(unittest.TestCase):
    A, B, C = 'a' * 40, 'b' * 40, 'c' * 40
    BOT: ClassVar[dict] = {'author': {'login': 'github-actions[bot]'}}
    HUMAN: ClassVar[dict] = {'author': {'login': 'cplieger'}}
    NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    # Three hours old: past the receipt grace window, so graded.
    OLD: ClassVar[dict] = committed(NOW - timedelta(hours=3))

    def commits(self, repo: str, *shas: str) -> dict:
        return {f'repos/cplieger/{repo}/commits/{sha}': self.OLD for sha in shas}

    def runs(
        self,
        repo: str,
        newest_ended: datetime | None = None,
        live: str = '',
        workflow: str = 'release.yaml',
    ) -> dict:
        """The three reads of the repo's release workflow runs: nothing queued
        or in progress unless `live` names that status, the newest run ended
        three hours ago unless `newest_ended` says otherwise."""
        ended = newest_ended or self.NOW - timedelta(hours=3)
        run = {'status': 'completed', 'updated_at': ended.isoformat().replace('+00:00', 'Z')}
        base = f'repos/cplieger/{repo}/actions/workflows/{workflow}/runs'
        return {
            f'{base}?status=in_progress&per_page=1': listing(
                *([{'status': 'in_progress'}] if live == 'in_progress' else [])
            ),
            f'{base}?status=queued&per_page=1': listing(
                *([{'status': 'queued'}] if live == 'queued' else [])
            ),
            f'{base}?per_page=1': listing(run),
        }

    def collect(self, repo: str, answers: dict, asked: list | None = None) -> dict:
        """collect_version_tags over stubbed JSON reads: every path, the runs
        listing's three included, answers from `answers` (None is a 404, so a
        runs path answering None is a repo without the workflow file); `asked`
        records the paths in order."""

        def fake(*args):
            if asked is not None:
                asked.append(args[-1])
            return answers[args[-1]]

        originals = (audit.gh_json, audit.gh_json_strict)
        audit.gh_json = audit.gh_json_strict = fake
        s = {'errors': []}
        try:
            audit.collect_version_tags(repo, s, self.NOW)
        finally:
            audit.gh_json, audit.gh_json_strict = originals
        return s

    def collect_over_gh(self, repo: str, replies: dict, asked: list | None = None) -> dict:
        """collect_version_tags with the `gh` process faked: `replies` maps each
        API path to its CompletedProcess, so the REST policy and both JSON readers run
        as they do live; a path with no reply fails the test by name."""

        def fake_gh(*args):
            path = gh_path(args)
            if asked is not None:
                asked.append(path)
            self.assertIn(path, replies, f'unexpected read {path}')
            return replies[path]

        original = audit.gh
        audit.gh = fake_gh
        s = {'errors': []}
        try:
            audit.collect_version_tags(repo, s, self.NOW)
        finally:
            audit.gh = original
        return s

    def test_a_stable_tag_with_a_pipeline_authored_release_is_clean(self):
        without, hand_made = audit.grade_stable_tags(['v1.3.0'], lambda tag: self.BOT)
        self.assertEqual((without, hand_made), ([], []))

    def test_a_hand_made_stable_tag_at_a_commit_with_a_release_run_is_still_hand_made(self):
        # The witness: v9.0.0 created by hand at a commit whose docs-only
        # release run created no tag; a hand-made Release does not launder it.
        releases = {'v1.3.0': self.BOT, 'v9.0.0': self.HUMAN}
        without, hand_made = audit.grade_stable_tags(['v9.0.0', 'v1.3.0'], releases.get)
        self.assertEqual(without, [])
        self.assertEqual(hand_made, ['v9.0.0'])

    def test_a_stable_tag_without_a_release_is_reported_as_such(self):
        without, hand_made = audit.grade_stable_tags(['v9.0.0'], lambda tag: None)
        self.assertEqual((without, hand_made), (['v9.0.0'], []))

    def test_an_unreadable_release_skips_the_tag_rather_than_flagging_it(self):
        without, hand_made = audit.grade_stable_tags(['v1.3.0'], lambda tag: audit.API_ERROR)
        self.assertEqual((without, hand_made), ([], []))

    def test_a_dev_tag_needs_its_own_receipt_on_its_commit(self):
        statuses = {
            self.A: [{'context': 'release/tag/v1.4.0-dev.1', 'state': 'success'}],
            self.B: [{'context': 'release/tag/v1.4.0-dev.1', 'state': 'success'}],
            self.C: [],
        }
        missing = audit.tags_without_receipt(
            [('v1.4.0-dev.1', self.A), ('v1.4.0-dev.2', self.B), ('v1.4.0-dev.3', self.C)],
            statuses.get,
        )
        self.assertEqual(missing, ['v1.4.0-dev.2', 'v1.4.0-dev.3'])

    def test_unreadable_statuses_skip_the_dev_tag(self):
        missing = audit.tags_without_receipt(
            [('v1.4.0-dev.1', self.A)], lambda sha: audit.API_ERROR
        )
        self.assertEqual(missing, [])

    def test_collect_reads_releases_and_commit_statuses_for_the_newest_tags(self):
        tags = [
            {'name': 'v1.4.0-dev.2', 'commit': {'sha': self.B}},
            {'name': 'v1.4.0-dev.1', 'commit': {'sha': self.A}},
            {'name': 'v9.0.0', 'commit': {'sha': self.C}},
            {'name': 'v1.3.0', 'commit': {'sha': self.A}},
        ]
        answers = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            'repos/cplieger/httpx/releases/tags/v9.0.0': self.HUMAN,
            'repos/cplieger/httpx/releases/tags/v1.3.0': self.BOT,
            f'repos/cplieger/httpx/commits/{self.A}/status?per_page=100': {
                'statuses': [{'context': 'release/tag/v1.4.0-dev.1', 'state': 'success'}]
            },
            f'repos/cplieger/httpx/commits/{self.B}/status?per_page=100': {'statuses': []},
            **self.commits('httpx', self.A, self.B, self.C),
            **self.runs('httpx'),
        }
        asked = []
        s = self.collect('httpx', answers, asked)
        self.assertEqual(s['stable_tags_without_release'], [])
        self.assertEqual(s['hand_made_stable_tags'], ['v9.0.0'])
        self.assertEqual(s['dev_tags_without_receipt'], ['v1.4.0-dev.2'])
        self.assertEqual(s['tags_in_grace'], 0)
        self.assertEqual(s['errors'], [])
        # No run is read as provenance evidence: the only Actions reads are the
        # deferral's three, which say whether a receipt is still on its way.
        self.assertEqual([a for a in asked if '/actions/' in a], list(self.runs('httpx')))

    def test_a_tag_on_a_commit_inside_the_grace_window_is_not_graded(self):
        # v1.5.0 was tagged seconds ago and its Release is not there yet; v1.4.0
        # is three hours old with no Release, so only it is a finding.
        tags = [
            {'name': 'v1.5.0', 'commit': {'sha': self.B}},
            {'name': 'v1.4.0', 'commit': {'sha': self.A}},
            {'name': 'v1.5.1-dev.1', 'commit': {'sha': self.C}},
        ]
        answers = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            **self.runs('httpx'),
            f'repos/cplieger/httpx/commits/{self.B}': committed(self.NOW),
            f'repos/cplieger/httpx/commits/{self.C}': committed(self.NOW - timedelta(minutes=5)),
            f'repos/cplieger/httpx/commits/{self.A}': self.OLD,
            'repos/cplieger/httpx/releases/tags/v1.4.0': None,
        }
        asked = []
        s = self.collect('httpx', answers, asked)
        self.assertEqual(s['stable_tags_without_release'], ['v1.4.0'])
        self.assertEqual(s['dev_tags_without_receipt'], [])
        self.assertEqual(s['tags_in_grace'], 2)
        self.assertEqual(s['errors'], [])
        self.assertNotIn('repos/cplieger/httpx/releases/tags/v1.5.0', asked)
        self.assertNotIn(f'repos/cplieger/httpx/commits/{self.C}/status?per_page=100', asked)

    def test_an_unreadable_commit_skips_its_tag_as_an_error(self):
        tags = [{'name': 'v1.4.0', 'commit': {'sha': self.A}}]
        answers = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            **self.runs('httpx'),
            f'repos/cplieger/httpx/commits/{self.A}': audit.API_ERROR,
        }
        s = self.collect('httpx', answers)
        self.assertEqual(s['stable_tags_without_release'], [])
        self.assertEqual(s['errors'], ['commit of tag v1.4.0 unreadable (API)'])

    def test_lane_tags_are_graded_with_the_same_two_receipts(self):
        # yamlenv/v9.0.0 has no Release and yamlenv/v9.0.1-dev.1 no status; the
        # root tags are clean, so every finding below names a lane tag.
        tags = [
            {'name': 'yamlenv/v9.0.1-dev.1', 'commit': {'sha': self.B}},
            {'name': 'yamlenv/v9.0.0', 'commit': {'sha': self.A}},
            {'name': 'v1.4.0-dev.1', 'commit': {'sha': self.A}},
            {'name': 'v1.3.0', 'commit': {'sha': self.C}},
        ]
        answers = {
            'repos/cplieger/envx/tags?per_page=100&page=1': tags,
            'repos/cplieger/envx/releases/tags/yamlenv/v9.0.0': None,
            'repos/cplieger/envx/releases/tags/v1.3.0': self.BOT,
            f'repos/cplieger/envx/commits/{self.A}/status?per_page=100': {
                'statuses': [{'context': 'release/tag/v1.4.0-dev.1', 'state': 'success'}]
            },
            f'repos/cplieger/envx/commits/{self.B}/status?per_page=100': {
                'statuses': [{'context': 'release/tag/v9.0.1-dev.1', 'state': 'success'}]
            },
            f'repos/cplieger/envx/commits/{self.C}/status?per_page=100': {
                'statuses': [{'context': 'release/complete/v1.3.0', 'state': 'success'}]
            },
            **self.commits('envx', self.A, self.B, self.C),
            **self.runs('envx'),
        }
        s = self.collect('envx', answers)
        self.assertEqual(s['stable_tags_without_release'], ['yamlenv/v9.0.0'])
        self.assertEqual(s['hand_made_stable_tags'], [])
        self.assertEqual(s['dev_tags_without_receipt'], ['yamlenv/v9.0.1-dev.1'])
        self.assertEqual(s['stable_tags_without_receipt'], [])
        s2 = two_channel('envx')
        s2['stable_tags_without_release'] = s['stable_tags_without_release']
        s2['dev_tags_without_receipt'] = s['dev_tags_without_receipt']
        hard, _, _ = audit.compliance(s2)
        self.assertTrue(
            any('stable tag yamlenv/v9.0.0 has no GitHub Release' in h for h in hard), hard
        )
        self.assertTrue(
            any('dev tag yamlenv/v9.0.1-dev.1 carries no release/tag receipt' in h for h in hard),
            hard,
        )

    def test_a_lane_below_a_root_satisfying_page_is_still_graded(self):
        # Page 1 holds five root stable and five root dev tags plus filler, so
        # the root counts are satisfied there; the lane's only tags, both
        # without their receipt, sit on page 2.
        page1 = [{'name': f'v1.{n}.0', 'commit': {'sha': self.C}} for n in range(5)]
        page1 += [{'name': f'v1.5.0-dev.{n}', 'commit': {'sha': self.A}} for n in range(1, 6)]
        page1 += [{'name': f'other-{n}', 'commit': {'sha': self.C}} for n in range(90)]
        page2 = [
            {'name': 'yamlenv/v9.0.1-dev.1', 'commit': {'sha': self.B}},
            {'name': 'yamlenv/v9.0.0', 'commit': {'sha': self.B}},
        ]
        answers = {
            'repos/cplieger/envx/tags?per_page=100&page=1': page1,
            'repos/cplieger/envx/tags?per_page=100&page=2': page2,
            'repos/cplieger/envx/releases/tags/yamlenv/v9.0.0': None,
            f'repos/cplieger/envx/commits/{self.A}/status?per_page=100': {
                'statuses': [
                    {'context': f'release/tag/v1.5.0-dev.{n}', 'state': 'success'}
                    for n in range(1, 6)
                ]
            },
            f'repos/cplieger/envx/commits/{self.B}/status?per_page=100': {'statuses': []},
            f'repos/cplieger/envx/commits/{self.C}/status?per_page=100': {
                'statuses': [{'context': 'release/complete/v1.4.0', 'state': 'success'}]
            },
            **self.commits('envx', self.A, self.B, self.C),
            **self.runs('envx'),
        }
        answers.update({f'repos/cplieger/envx/releases/tags/v1.{n}.0': self.BOT for n in range(5)})
        asked = []
        s = self.collect('envx', answers, asked)
        self.assertIn('repos/cplieger/envx/tags?per_page=100&page=2', asked)
        self.assertEqual(s['stable_tags_without_release'], ['yamlenv/v9.0.0'])
        self.assertEqual(s['dev_tags_without_receipt'], ['yamlenv/v9.0.1-dev.1'])
        self.assertEqual(s['errors'], [])

    def test_a_truncated_tag_listing_is_an_error_line_not_a_grade(self):
        original = audit.release_channels.collect_all_tags

        def truncated(*args, **kwargs):
            raise audit.release_channels.TagListingTruncatedError('20 pages')

        audit.release_channels.collect_all_tags = truncated
        try:
            s = self.collect('httpx', self.runs('httpx'))
        finally:
            audit.release_channels.collect_all_tags = original
        self.assertTrue(any('truncated' in e for e in s['errors']), s)
        self.assertNotIn('hand_made_stable_tags', s)

    def test_a_workflow_run_in_progress_defers_the_repo_s_version_tags(self):
        # v1.5.0 was just created on a commit made a day earlier, so the
        # commit's age says nothing; the live release run is what says the
        # Release is still on its way.
        tags = [{'name': 'v1.5.0', 'commit': {'sha': self.A}}]
        answers = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            **self.runs('httpx', live='in_progress'),
        }
        asked = []
        s = self.collect('httpx', answers, asked)
        self.assertTrue(s['version_tags_deferred'])
        self.assertNotIn('stable_tags_without_release', s)
        self.assertEqual(s['errors'], [])
        self.assertEqual(
            asked,
            [
                'repos/cplieger/httpx/actions/workflows/release.yaml/runs?status=in_progress&per_page=1'
            ],
        )

    def test_a_queued_run_or_one_that_ended_inside_the_grace_window_defers_too(self):
        for runs in (
            self.runs('httpx', live='queued'),
            self.runs('httpx', newest_ended=self.NOW - timedelta(minutes=30)),
        ):
            s = self.collect('httpx', runs)
            self.assertTrue(s.get('version_tags_deferred'), runs)

    def test_a_run_that_ended_three_hours_ago_does_not_defer_grading(self):
        # The same old commit with its release run over and no Release
        # is the finding the audit exists for.
        tags = [{'name': 'v1.5.0', 'commit': {'sha': self.A}}]
        answers = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            'repos/cplieger/httpx/releases/tags/v1.5.0': None,
            **self.commits('httpx', self.A),
            **self.runs('httpx', newest_ended=self.NOW - timedelta(hours=3)),
        }
        s = self.collect('httpx', answers)
        self.assertNotIn('version_tags_deferred', s)
        self.assertEqual(s['stable_tags_without_release'], ['v1.5.0'])

    def test_a_run_of_another_workflow_inside_the_window_does_not_defer(self):
        # The daily security dispatch ends inside the window in every repo at
        # the audit's hour; only the release workflow's runs say whether a
        # receipt is on its way, so the repo-wide listings are never read.
        tags = [{'name': 'v1.5.0', 'commit': {'sha': self.A}}]
        young = (self.NOW - timedelta(minutes=30)).isoformat().replace('+00:00', 'Z')
        repo_wide = 'repos/cplieger/httpx/actions/runs'
        answers = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            'repos/cplieger/httpx/releases/tags/v1.5.0': None,
            **self.commits('httpx', self.A),
            **self.runs('httpx'),
            f'{repo_wide}?status=in_progress&per_page=1': listing(),
            f'{repo_wide}?status=queued&per_page=1': listing(),
            f'{repo_wide}?per_page=1': listing(
                {'name': 'Security', 'status': 'completed', 'updated_at': young}
            ),
        }
        asked = []
        s = self.collect('httpx', answers, asked)
        self.assertNotIn('version_tags_deferred', s)
        self.assertEqual(s['stable_tags_without_release'], ['v1.5.0'])
        self.assertFalse(any(a.startswith(f'{repo_wide}?') for a in asked), asked)

    def test_an_own_publish_repo_is_deferred_on_its_publish_workflow(self):
        tags = [{'name': 'v1.1.3', 'commit': {'sha': self.A}}]
        answers = {
            'repos/cplieger/web-terminal-glyphs/tags?per_page=100&page=1': tags,
            **self.runs('web-terminal-glyphs', live='queued', workflow='publish.yaml'),
        }
        asked = []
        s = self.collect('web-terminal-glyphs', answers, asked)
        self.assertTrue(s['version_tags_deferred'])
        self.assertTrue(all('/actions/workflows/publish.yaml/runs?' in a for a in asked), asked)

    def test_a_repo_without_the_workflow_file_is_graded(self):
        # A definitive 404 on the workflow's runs is no run, not an error.
        tags = [{'name': 'v1.5.0', 'commit': {'sha': self.A}}]
        base = 'repos/cplieger/httpx/actions/workflows/release.yaml/runs'
        answers = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            'repos/cplieger/httpx/releases/tags/v1.5.0': None,
            **self.commits('httpx', self.A),
            f'{base}?status=in_progress&per_page=1': None,
            f'{base}?status=queued&per_page=1': None,
            f'{base}?per_page=1': None,
        }
        s = self.collect('httpx', answers)
        self.assertNotIn('version_tags_deferred', s)
        self.assertEqual(s['stable_tags_without_release'], ['v1.5.0'])
        self.assertEqual(s['errors'], [])

    def test_unreadable_workflow_runs_are_an_error_and_no_grade(self):
        answers = {
            'repos/cplieger/httpx/actions/workflows/release.yaml/runs?status=in_progress&per_page=1': (
                audit.API_ERROR
            )
        }
        s = self.collect('httpx', answers)
        self.assertEqual(s['errors'], ['workflow runs unreadable (API); version tags not graded'])
        self.assertNotIn('stable_tags_without_release', s)

    RUNS = 'repos/cplieger/httpx/actions/workflows/release.yaml/runs'
    RUN_READS: ClassVar[tuple[str, ...]] = (
        f'{RUNS}?status=in_progress&per_page=1',
        f'{RUNS}?status=queued&per_page=1',
        f'{RUNS}?per_page=1',
    )

    def test_an_http_404_on_the_workflow_runs_grades_the_old_stable_tag_hard(self):
        # Driven through the real gh process boundary: gh exits 1 with the
        # status on stderr, and only a 404 may read as a missing workflow file.
        tags = [{'name': 'v1.5.0', 'commit': {'sha': self.A}}]
        replies = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': gh_ok(tags),
            f'repos/cplieger/httpx/commits/{self.A}': gh_ok(self.OLD),
            'repos/cplieger/httpx/releases/tags/v1.5.0': gh_http(404),
            **{path: gh_http(404) for path in self.RUN_READS},
        }
        s = self.collect_over_gh('httpx', replies)
        self.assertNotIn('version_tags_deferred', s)
        self.assertEqual(s['stable_tags_without_release'], ['v1.5.0'])
        self.assertEqual(s['errors'], [])
        s2 = two_channel()
        s2['stable_tags_without_release'] = s['stable_tags_without_release']
        hard, _, _ = audit.compliance(s2)
        self.assertTrue(any('stable tag v1.5.0 has no GitHub Release' in h for h in hard), hard)

    def test_an_http_403_or_422_on_the_workflow_runs_is_an_error_that_reads_no_tags(self):
        for status in (403, 422):
            asked = []
            s = self.collect_over_gh('httpx', {self.RUN_READS[0]: gh_http(status)}, asked)
            self.assertEqual(
                s['errors'], ['workflow runs unreadable (API); version tags not graded'], status
            )
            self.assertNotIn('stable_tags_without_release', s)
            self.assertEqual(asked, [self.RUN_READS[0]], status)

    def test_a_workflow_runs_body_that_is_not_json_is_an_error(self):
        reply = gh_ok('<html>maintenance</html>')
        asked = []
        s = self.collect_over_gh('httpx', {self.RUN_READS[0]: reply}, asked)
        self.assertEqual(s['errors'], ['workflow runs unreadable (API); version tags not graded'])
        self.assertNotIn('stable_tags_without_release', s)
        self.assertEqual(asked, [self.RUN_READS[0]])

    def assert_first_runs_read_is_an_error(self, body):
        """The in-progress listing answers HTTP success with `body`: the error
        line is recorded, nothing is graded, and no further read is made."""
        asked = []
        s = self.collect_over_gh('httpx', {self.RUN_READS[0]: gh_ok(body)}, asked)
        self.assertEqual(
            s['errors'], ['workflow runs unreadable (API); version tags not graded'], body
        )
        self.assertNotIn('stable_tags_without_release', s, body)
        self.assertEqual(asked, [self.RUN_READS[0]], body)

    def test_a_null_workflow_runs_body_is_an_error_not_a_missing_workflow(self):
        # JSON null parses to None, the value the 404 arm answers; a success
        # carrying it must not grade the repo as one without the workflow file.
        self.assert_first_runs_read_is_an_error(None)

    def test_an_empty_object_workflow_runs_body_is_an_error(self):
        self.assert_first_runs_read_is_an_error({})

    def test_a_workflow_runs_body_without_the_runs_list_is_an_error(self):
        self.assert_first_runs_read_is_an_error({'total_count': 0})

    def test_a_bare_array_workflow_runs_body_is_an_error(self):
        self.assert_first_runs_read_is_an_error([{'status': 'in_progress'}])

    def test_a_newest_run_whose_updated_at_does_not_parse_is_an_error(self):
        for updated_at in ('not-a-date', 7, None):
            with self.subTest(updated_at=updated_at):
                row = {'status': 'completed'}
                if updated_at is not None:
                    row['updated_at'] = updated_at
                replies = {
                    self.RUN_READS[0]: gh_ok(listing()),
                    self.RUN_READS[1]: gh_ok(listing()),
                    self.RUN_READS[2]: gh_ok(listing(row)),
                }
                asked = []
                s = self.collect_over_gh('httpx', replies, asked)
                self.assertEqual(
                    s['errors'], ['workflow runs unreadable (API); version tags not graded']
                )
                self.assertNotIn('stable_tags_without_release', s)
                self.assertEqual(asked, list(self.RUN_READS))

    def test_a_commit_dated_in_the_future_is_graded_rather_than_kept_in_grace(self):
        tags = [{'name': 'v9.0.0', 'commit': {'sha': self.A}}]
        answers = {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            f'repos/cplieger/httpx/commits/{self.A}': committed(self.NOW + timedelta(days=3650)),
            'repos/cplieger/httpx/releases/tags/v9.0.0': None,
            **self.runs('httpx'),
        }
        s = self.collect('httpx', answers)
        self.assertEqual(s['stable_tags_without_release'], ['v9.0.0'])
        self.assertEqual(s['tags_in_grace'], 0)

    def test_a_tag_without_a_commit_sha_is_an_error_not_a_traceback(self):
        tags = [{'name': 'v1.4.0', 'commit': {}}]
        answers = {'repos/cplieger/httpx/tags?per_page=100&page=1': tags, **self.runs('httpx')}
        s = self.collect('httpx', answers)
        self.assertEqual(s['stable_tags_without_release'], [])
        self.assertEqual(s['errors'], ['tag v1.4.0 carries no commit sha; not graded'])

    def test_an_own_publish_repo_passes_on_its_own_release(self):
        # web-terminal-glyphs tags and releases through its publish.yaml with
        # the Actions token, so its Release carries the same author.
        without, hand_made = audit.grade_stable_tags(['v1.1.2'], lambda tag: self.BOT)
        self.assertEqual((without, hand_made), ([], []))
        self.assertIn('web-terminal-glyphs', audit.release_channels.OWN_PUBLISH_REPOS)

    def receipt_answers(self, statuses_c, tags=None, **extra) -> dict:
        """v1.3.0 (commit C, the highest) and v1.2.0 (commit A), both with a
        pipeline Release; C's statuses are `statuses_c`, A has none."""
        tags = tags or [
            {'name': 'v1.3.0', 'commit': {'sha': self.C}},
            {'name': 'v1.2.0', 'commit': {'sha': self.A}},
        ]
        return {
            'repos/cplieger/httpx/tags?per_page=100&page=1': tags,
            'repos/cplieger/httpx/releases/tags/v1.3.0': self.BOT,
            'repos/cplieger/httpx/releases/tags/v1.2.0': self.BOT,
            f'repos/cplieger/httpx/commits/{self.C}/status?per_page=100': statuses_c,
            f'repos/cplieger/httpx/commits/{self.A}/status?per_page=100': {'statuses': []},
            **self.commits('httpx', self.A, self.C),
            **self.runs('httpx'),
            **extra,
        }

    def test_the_highest_stable_tag_without_its_completion_receipt_is_hard(self):
        for statuses in (
            [],
            [{'context': 'release/complete/v1.3.0', 'state': 'pending'}],
            [{'context': 'release/complete/v1.2.0', 'state': 'success'}],
            [{'context': 'release/tag/v1.3.0', 'state': 'success'}],
        ):
            with self.subTest(statuses=statuses):
                s = self.collect('httpx', self.receipt_answers({'statuses': statuses}))
                self.assertEqual(s['stable_tags_without_receipt'], ['v1.3.0'])
                self.assertEqual(s['errors'], [])
                s2 = two_channel()
                s2['stable_tags_without_receipt'] = s['stable_tags_without_receipt']
                hard, _, _ = audit.compliance(s2)
                self.assertTrue(
                    any(
                        'stable tag v1.3.0 carries no release/complete/v1.3.0 receipt' in h
                        for h in hard
                    ),
                    hard,
                )

    def test_only_the_highest_stable_tag_needs_a_receipt(self):
        receipted = {'statuses': [{'context': 'release/complete/v1.3.0', 'state': 'success'}]}
        asked = []
        s = self.collect('httpx', self.receipt_answers(receipted), asked)
        self.assertEqual(s['stable_tags_without_receipt'], [])
        self.assertNotIn(f'repos/cplieger/httpx/commits/{self.A}/status?per_page=100', asked)

    def test_a_highest_tag_in_grace_grades_no_receipt_on_the_tag_below(self):
        answers = self.receipt_answers({'statuses': []})
        answers[f'repos/cplieger/httpx/commits/{self.C}'] = committed(self.NOW)
        asked = []
        s = self.collect('httpx', answers, asked)
        self.assertEqual(s['stable_tags_without_receipt'], [])
        self.assertEqual(s['tags_in_grace'], 1)
        self.assertFalse(any('/status?' in a for a in asked), asked)

    def test_a_commit_without_a_usable_date_grades_nothing_and_reads_no_status(self):
        responses = {
            'no committer': {'commit': {}},
            'no date': {'commit': {'committer': {}}},
            'unparsable date': {'commit': {'committer': {'date': 'yesterday'}}},
            'date without a zone': {'commit': {'committer': {'date': '2026-10-01T00:00:00'}}},
        }
        for label, body in responses.items():
            with self.subTest(label):
                answers = self.receipt_answers({'statuses': []})
                answers[f'repos/cplieger/httpx/commits/{self.C}'] = body
                asked = []
                s = self.collect('httpx', answers, asked)
                self.assertEqual(s['stable_tags_without_receipt'], [])
                self.assertEqual(s['stable_tags_without_release'], [])
                self.assertEqual(s['tags_in_grace'], 0)
                self.assertEqual(s['errors'], ['commit of tag v1.3.0 unreadable (API)'])
                self.assertNotIn(
                    f'repos/cplieger/httpx/commits/{self.C}/status?per_page=100', asked
                )

    def test_a_highest_tag_without_its_release_is_one_finding_not_two(self):
        answers = self.receipt_answers({'statuses': []})
        answers['repos/cplieger/httpx/releases/tags/v1.3.0'] = None
        s = self.collect('httpx', answers)
        self.assertEqual(s['stable_tags_without_release'], ['v1.3.0'])
        self.assertEqual(s['stable_tags_without_receipt'], [])

    def test_an_unreadable_release_grades_no_receipt_and_reads_no_status(self):
        answers = self.receipt_answers({'statuses': []})
        answers['repos/cplieger/httpx/releases/tags/v1.3.0'] = audit.API_ERROR
        asked = []
        s = self.collect('httpx', answers, asked)
        self.assertEqual(s['stable_tags_without_receipt'], [])
        self.assertEqual(s['errors'], ['release for tag v1.3.0 unreadable (API)'])
        self.assertNotIn(f'repos/cplieger/httpx/commits/{self.C}/status?per_page=100', asked)

    def test_each_lane_s_highest_stable_tag_needs_its_own_receipt(self):
        tags = [
            {'name': 'yamlenv/v1.1.0', 'commit': {'sha': self.B}},
            {'name': 'v1.3.0', 'commit': {'sha': self.C}},
        ]
        root_only = {'statuses': [{'context': 'release/complete/v1.3.0', 'state': 'success'}]}
        answers = self.receipt_answers(
            root_only,
            tags=tags,
            **{
                'repos/cplieger/httpx/releases/tags/yamlenv/v1.1.0': self.BOT,
                f'repos/cplieger/httpx/commits/{self.B}/status?per_page=100': {
                    'statuses': [{'context': 'release/tag/yamlenv/v1.1.0', 'state': 'success'}]
                },
                **self.commits('httpx', self.B),
            },
        )
        s = self.collect('httpx', answers)
        self.assertEqual(s['stable_tags_without_receipt'], ['yamlenv/v1.1.0'])
        s2 = two_channel()
        s2['stable_tags_without_receipt'] = s['stable_tags_without_receipt']
        hard, _, _ = audit.compliance(s2)
        self.assertTrue(
            any('carries no release/complete/yamlenv/v1.1.0 receipt' in h for h in hard), hard
        )

    def test_a_truncated_status_list_is_an_error_not_a_missing_receipt(self):
        truncated = {'total_count': 101, 'statuses': [{'context': 'x', 'state': 'success'}] * 100}
        s = self.collect('httpx', self.receipt_answers(truncated))
        self.assertEqual(s['stable_tags_without_receipt'], [])
        self.assertEqual(
            s['errors'], [f'statuses of {self.C[:12]} truncated at 100, so receipts not graded']
        )

    def test_unreadable_statuses_grade_no_receipt(self):
        s = self.collect('httpx', self.receipt_answers(audit.API_ERROR))
        self.assertEqual(s['stable_tags_without_receipt'], [])
        self.assertEqual(s['errors'], [f'statuses of {self.C[:12]} unreadable (API)'])


class Legacy(unittest.TestCase):
    def setUp(self):
        self.saved_host = audit.WEBHOOK_HOST
        audit.WEBHOOK_HOST = HOST

    def tearDown(self):
        audit.WEBHOOK_HOST = self.saved_host

    def test_clean_main_repo_is_graded_as_before(self):
        hard, warn, _ = audit.compliance(legacy())
        self.assertEqual(hard, [])
        self.assertEqual(warn, [])

    def test_main_repo_without_protection_is_hard(self):
        s = legacy()
        s['has_protection'] = False
        hard, _, _ = audit.compliance(s)
        self.assertIn('no branch protection on default branch', hard)

    def test_main_repo_custom_ruleset_is_drift(self):
        s = legacy()
        s['custom_rulesets'] = [{'name': 'dev', 'enforcement': 'active'}]
        _, warn, _ = audit.compliance(s)
        self.assertTrue(any("unexpected custom ruleset 'dev'" in w for w in warn), warn)

    def test_a_required_smoke_check_is_drift_in_every_repo(self):
        for name in ('docker-radvd', 'web-terminal-server'):
            with self.subTest(repo=name):
                s = legacy(name)
                s['required_checks'] = ['ci / validate', 'smoke']
                s['observed_checks'] = ['ci / validate', 'smoke']
                _, warn, accepted = audit.compliance(s)
                self.assertIn(
                    "unexpected extra required check 'smoke' (standard is the validate gate alone)",
                    warn,
                )
                self.assertEqual(accepted, [])

    def test_main_repo_release_hook_required(self):
        s = legacy('knell')
        s['webhooks'] = [hook(['registry_package'])]
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any("lack 'release'" in h for h in hard), hard)

    def public_main(self, name: str = 'httpx', publishes=True) -> dict:
        s = legacy(name)
        s['private'] = False
        s['visibility'] = 'public'
        s['publishes'] = publishes
        return s

    def stable_only_warning(self, s: dict) -> list:
        _, warn, _ = audit.compliance(s)
        return [w for w in warn if 'releases straight to the stable channel' in w]

    def test_a_public_repo_still_releasing_from_main_is_named(self):
        self.assertEqual(len(self.stable_only_warning(self.public_main())), 1)

    def test_a_single_main_repo_is_not_named(self):
        self.assertEqual(self.stable_only_warning(self.public_main('tool-catalog')), [])

    def test_a_repo_without_a_release_workflow_is_not_named(self):
        self.assertEqual(self.stable_only_warning(self.public_main(publishes=False)), [])

    def test_an_unreadable_workflow_listing_names_nothing(self):
        self.assertEqual(self.stable_only_warning(self.public_main(publishes=None)), [])

    def test_a_private_repo_on_main_is_not_named(self):
        s = self.public_main()
        s['private'] = True
        self.assertEqual(self.stable_only_warning(s), [])

    def test_a_two_channel_repo_is_not_named(self):
        s = two_channel()
        s['private'] = False
        s['publishes'] = True
        self.assertEqual(self.stable_only_warning(s), [])

    def test_other_default_branch_is_hard(self):
        s = legacy()
        s['default_branch'] = 'master'
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any(h.startswith('default_branch=master') for h in hard), hard)


class ModulePath(unittest.TestCase):
    def public(self, name: str, module: str, image: bool) -> dict:
        s = legacy(name)
        s['private'] = False
        s['visibility'] = 'public'
        s['has_dockerfile'] = image
        s['go_module'] = module
        return s

    def module_warnings(self, s: dict) -> list:
        _, warn, _ = audit.compliance(s)
        return [w for w in warn if w.startswith('go.mod module')]

    def test_an_image_repo_on_the_plain_path_is_clean(self):
        s = self.public('docker-age', 'github.com/cplieger/docker-age', image=True)
        self.assertEqual(self.module_warnings(s), [])

    def test_an_image_repo_with_a_major_suffix_warns_to_drop_it(self):
        s = self.public('docker-age', 'github.com/cplieger/docker-age/v4', image=True)
        warn = self.module_warnings(s)
        self.assertEqual(len(warn), 1, warn)
        self.assertIn('carries a /vN suffix', warn[0])
        self.assertIn('drop the suffix', warn[0])

    def test_an_image_repo_naming_another_repo_warns(self):
        s = self.public('docker-age', 'github.com/cplieger/age-decrypt', image=True)
        warn = self.module_warnings(s)
        self.assertEqual(len(warn), 1, warn)
        self.assertIn("want 'github.com/cplieger/docker-age'", warn[0])

    def test_a_library_with_a_major_suffix_is_clean(self):
        s = self.public('toolbelt', 'github.com/cplieger/toolbelt/v3', image=False)
        self.assertEqual(self.module_warnings(s), [])

    def test_a_library_naming_another_repo_warns(self):
        s = self.public('toolbelt', 'github.com/cplieger/tools/v3', image=False)
        self.assertEqual(len(self.module_warnings(s)), 1)

    def test_an_image_repo_has_no_used_by_package_to_expect(self):
        self.assertIsNone(
            audit.expected_used_by_package(
                'github.com/cplieger/docker-age', '{"name": "web"}', image=True
            )
        )

    def test_a_library_expects_its_module_path_then_its_npm_name(self):
        self.assertEqual(
            audit.expected_used_by_package('github.com/cplieger/toolbelt/v3', None, image=False),
            'github.com/cplieger/toolbelt/v3',
        )
        self.assertEqual(
            audit.expected_used_by_package(None, '{"name": "@cplieger/fetch"}', image=False),
            '@cplieger/fetch',
        )
        self.assertIsNone(audit.expected_used_by_package(None, '{not json', image=False))


class RulesetBodies(unittest.TestCase):
    def test_committed_bodies_match_the_documented_tables(self):
        dev_rules = {r['type'] for r in DEV_RULESET['rules']}
        self.assertEqual(
            dev_rules,
            {
                'pull_request',
                'required_status_checks',
                'required_linear_history',
                'non_fast_forward',
                'deletion',
            },
        )
        self.assertEqual(DEV_RULESET['bypass_actors'], [])
        main_rules = [r['type'] for r in MAIN_RULESET['rules']]
        self.assertEqual(len(main_rules), len(set(main_rules)), main_rules)
        self.assertEqual(set(main_rules), dev_rules | {'creation'})
        self.assertNotIn('update', main_rules)
        self.assertEqual(
            MAIN_RULESET['bypass_actors'],
            [{'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'}],
        )
        self.assertEqual(
            json.dumps(DEV_RULESET['conditions']['ref_name']['include']), '["refs/heads/dev"]'
        )
        self.assertEqual(
            MAIN_RULESET['conditions'],
            {'ref_name': {'include': ['refs/heads/main'], 'exclude': []}},
        )

    def test_both_branches_gate_merges_on_the_same_pull_request_and_validate_rules(self):
        def rule(ruleset, rtype):
            return next(r for r in ruleset['rules'] if r['type'] == rtype)

        for ruleset in (DEV_RULESET, MAIN_RULESET):
            with self.subTest(ruleset['name']):
                self.assertEqual(ruleset['enforcement'], 'active')
                self.assertEqual(
                    rule(ruleset, 'pull_request')['parameters']['required_approving_review_count'],
                    0,
                )
                checks = rule(ruleset, 'required_status_checks')['parameters']
                self.assertEqual(
                    checks['required_status_checks'],
                    [{'context': 'ci / validate', 'integration_id': audit.ACTIONS_APP_ID}],
                )
                self.assertFalse(checks['strict_required_status_checks_policy'])
        for rtype in ('pull_request', 'required_status_checks'):
            self.assertEqual(rule(MAIN_RULESET, rtype), rule(DEV_RULESET, rtype))


class MainDefaultIdentity(unittest.TestCase):
    """Main-default repos are graded as before two-branch grading apart from the
    squash-only merge model: each recorded scenario's findings, errors and API
    reads, in order, are the ones the baseline audit.py gave for it
    (testdata/audit/main-default.json), with its merge-model lines regraded."""

    SCENARIOS: ClassVar[list] = json.loads(MAIN_DEFAULT.read_text(encoding='utf-8'))['scenarios']

    @staticmethod
    def squash_only(sc: dict) -> dict:
        """The baseline's findings with its merge-model lines graded squash-only."""
        repo = sc['answers'][f'repos/cplieger/{sc["meta"]["name"]}']
        keys = set(audit.GOV_HARD) | set(audit.GOV_SOFT)
        want = copy.deepcopy(sc['expected'])
        for kind, table in (('hard', audit.GOV_HARD), ('warn', audit.GOV_SOFT)):
            rest = [line for line in want[kind] if line.split('=', 1)[0] not in keys]
            want[kind] = [
                f'{k}={repo.get(k)} (want {v})' for k, v in table.items() if repo.get(k) != v
            ] + rest
        return want

    def test_every_recorded_scenario_grades_and_reads_as_the_baseline(self):
        self.assertGreaterEqual(len(self.SCENARIOS), 7)
        for sc in self.SCENARIOS:
            with self.subTest(sc['name']):
                self.assertEqual(audit_over_answers(audit, sc), self.squash_only(sc))

    def test_the_scenarios_cover_the_merge_model_squash_only_would_reject(self):
        repos = [sc['answers'][f'repos/cplieger/{sc["meta"]["name"]}'] for sc in self.SCENARIOS]
        self.assertTrue(any(r['allow_rebase_merge'] for r in repos))
        self.assertTrue(any(r['squash_merge_commit_title'] == 'COMMIT_OR_PR_TITLE' for r in repos))
        self.assertTrue(all(r['default_branch'] == 'main' for r in repos))


class SingleMainOnDev(unittest.TestCase):
    """A SINGLE_MAIN_REPOS member on dev is graded and read as the same repo on
    main, with the dev branch in its paths, plus the one wrong-branch finding."""

    @staticmethod
    def scenario(branch: str) -> dict:
        sc = copy.deepcopy(MainDefaultIdentity.SCENARIOS[0])
        old = f'repos/cplieger/{sc["meta"]["name"]}'
        sc['meta']['name'] = 'tool-catalog'
        sc['answers'] = {
            on_branch(k.replace(old, 'repos/cplieger/tool-catalog'), branch): v
            for k, v in sc['answers'].items()
        }
        sc['answers']['repos/cplieger/tool-catalog']['default_branch'] = branch
        return sc

    def test_no_two_branch_read_or_grade(self):
        on_main = audit_over_answers(audit, self.scenario('main'))
        on_dev = audit_over_answers(audit, self.scenario('dev'))
        self.assertNotIn('repos/cplieger/tool-catalog/branches/main/protection', on_dev['reads'])
        self.assertEqual(on_dev['reads'], [on_branch(r, 'dev') for r in on_main['reads']])
        self.assertEqual(
            sorted(on_dev['hard']),
            sorted(
                [
                    *on_main['hard'],
                    'default_branch=dev on a single-main repo (want main; tool-catalog publishes from main directly)',
                ]
            ),
        )
        self.assertEqual((on_dev['warn'], on_dev['errors']), (on_main['warn'], on_main['errors']))


class PrivateOnDev(unittest.TestCase):
    """A private repo on dev is graded and read as the same repo on main, with
    the dev branch in its paths, plus the one wrong-branch finding: the
    two-branch model enrols public repositories only."""

    @staticmethod
    def scenario(branch: str) -> dict:
        sc = copy.deepcopy(
            next(s for s in MainDefaultIdentity.SCENARIOS if s['meta']['name'] == 'notes')
        )
        sc['answers'] = {on_branch(k, branch): v for k, v in sc['answers'].items()}
        sc['answers']['repos/cplieger/notes'].update(
            {'default_branch': branch, 'fork': False, 'archived': False}
        )
        return sc

    def test_no_two_branch_read_or_grade(self):
        on_main = audit_over_answers(audit, self.scenario('main'))
        on_dev = audit_over_answers(audit, self.scenario('dev'))
        self.assertEqual(on_dev['reads'], [on_branch(r, 'dev') for r in on_main['reads']])
        self.assertFalse([r for r in on_dev['reads'] if '/contents/renovate.json' in r])
        self.assertEqual(
            sorted(on_dev['hard']),
            sorted(
                [
                    *on_main['hard'],
                    (
                        'default_branch=dev on a private repo (want main, because the two-branch model '
                        'enrols public repos only)'
                    ),
                ]
            ),
        )
        self.assertEqual((on_dev['warn'], on_dev['errors']), (on_main['warn'], on_main['errors']))


def on_branch(path: str, branch: str) -> str:
    """`path` with its default-branch segment (`/branches/main/`, `/commits/main/`,
    `ref=main`) naming `branch`."""
    for a, b in (
        ('/branches/main/', f'/branches/{branch}/'),
        ('/commits/main/', f'/commits/{branch}/'),
    ):
        path = path.replace(a, b)
    return path.replace('ref=main', f'ref={branch}')


class SquashOnlyMergeModel(unittest.TestCase):
    def test_squash_only_with_the_pr_title_is_hard_on_a_two_channel_repo(self):
        for key, value in (
            ('allow_rebase_merge', True),
            ('allow_merge_commit', True),
            ('allow_squash_merge', False),
            ('squash_merge_commit_title', 'COMMIT_OR_PR_TITLE'),
            ('squash_merge_commit_message', 'PR_BODY'),
            ('delete_branch_on_merge', False),
            ('allow_auto_merge', False),
        ):
            with self.subTest(key=key):
                s = two_channel()
                s[key] = value
                hard, warn, _ = audit.compliance(s)
                want = audit.GOV_HARD[key]
                self.assertEqual(hard, [f'{key}={value} (want {want})'])
                self.assertFalse(any(w.startswith(f'{key}=') for w in warn), warn)

    def test_a_main_default_repo_is_graded_squash_only_too(self):
        s = legacy()
        s['allow_rebase_merge'] = True
        s['squash_merge_commit_title'] = 'COMMIT_OR_PR_TITLE'
        hard, warn, _ = audit.compliance(s)
        self.assertEqual(
            hard,
            [
                'allow_rebase_merge=True (want False)',
                'squash_merge_commit_title=COMMIT_OR_PR_TITLE (want PR_TITLE)',
            ],
        )
        self.assertFalse(any(w.startswith(('allow_', 'squash_merge_')) for w in warn), warn)

    def test_the_hard_and_soft_tables_share_no_key(self):
        self.assertEqual(set(audit.GOV_HARD) & set(audit.GOV_SOFT), set())


class TwoBranchRulesets(unittest.TestCase):
    def grade(self, ruleset_name: str, change) -> tuple[list, list]:
        s = two_channel()
        change(s['rulesets_full'][ruleset_name])
        s['ruleset_bypass_actors'] = [
            (name, a.get('actor_type'), a.get('actor_id'), a.get('bypass_mode'))
            for name, rs in s['rulesets_full'].items()
            for a in rs.get('bypass_actors') or []
        ]
        hard, warn, _ = audit.compliance(s)
        return hard, warn

    def test_the_update_rule_back_on_main_is_hard(self):
        hard, _ = self.grade('main', lambda rs: rs['rules'].append({'type': 'update'}))
        self.assertTrue(
            any("ruleset 'main' differs" in h and 'rule types' in h for h in hard), hard
        )

    def test_main_without_creation_is_hard(self):
        def drop(rs):
            rs['rules'] = [r for r in rs['rules'] if r['type'] != 'creation']

        hard, _ = self.grade('main', drop)
        self.assertTrue(
            any("ruleset 'main' differs" in h and 'rule types' in h for h in hard), hard
        )

    def test_only_main_s_committed_admin_bypass_is_exempt(self):
        def mode(rs):
            rs['bypass_actors'][0]['bypass_mode'] = 'pull_request'

        def other_role(rs):
            rs['bypass_actors'][0]['actor_id'] = 2

        for change, actor in ((mode, 'RepositoryRole id 5'), (other_role, 'RepositoryRole id 2')):
            with self.subTest(actor=actor, change=change.__name__):
                hard, warn = self.grade('main', change)
                self.assertTrue(any('bypass actors' in h for h in hard), hard)
                self.assertIn(f"ruleset 'main' has a bypass actor ({actor})", warn)
        hard, warn = self.grade('main', lambda rs: None)
        self.assertEqual((hard, warn), ([], []))

    def test_the_admin_bypass_on_dev_is_not_exempt(self):
        admin = {'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'always'}
        hard, warn = self.grade('dev', lambda rs: rs['bypass_actors'].append(admin))
        self.assertTrue(
            any("ruleset 'dev' differs" in h and 'bypass actors' in h for h in hard), hard
        )
        self.assertIn("ruleset 'dev' has a bypass actor (RepositoryRole id 5)", warn)

    def test_collect_records_each_bypass_mode(self):
        rs = live(MAIN_RULESET)
        answers = {
            'repos/cplieger/httpx/rulesets': [{'id': 2, 'name': 'main'}],
            'repos/cplieger/httpx/rulesets/2': rs,
        }
        original = audit.gh_json
        audit.gh_json = lambda *args: answers[args[-1]]
        s = {'errors': []}
        try:
            audit.collect_rulesets('httpx', s)
        finally:
            audit.gh_json = original
        self.assertEqual(s['ruleset_bypass_actors'], [('main', 'RepositoryRole', 5, 'always')])


class TwoBranchRenovateConfig(unittest.TestCase):
    WANT = '{"extends": ["github>cplieger/.github:two-branch"]}'

    def test_the_synced_config_extends_only_the_two_branch_preset(self):
        self.assertEqual(
            audit.two_branch_renovate(),
            ('renovate.json', {'extends': ['github>cplieger/.github:two-branch']}),
        )

    def hard_for(self, text) -> list:
        s = two_channel()
        s['renovate_json'] = text
        hard, _, _ = audit.compliance(s)
        return hard

    def test_the_synced_copy_in_any_layout_is_clean(self):
        for text in (
            RENOVATE_JSON,
            '{\n  "extends": [\n    "github>cplieger/.github:two-branch"\n  ]\n}\n',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.hard_for(text), [])

    def test_a_missing_or_unparsable_config_is_hard(self):
        self.assertEqual(
            self.hard_for(''),
            [f'renovate.json missing on dev (want {self.WANT}, which the ci sync writes)'],
        )
        self.assertEqual(
            self.hard_for('{"extends": ['),
            [f'renovate.json on dev is not JSON (want {self.WANT}, which the ci sync writes)'],
        )

    def test_any_other_content_is_hard(self):
        for got in (
            {'extends': ['github>cplieger/.github']},
            {'extends': ['github>cplieger/.github', 'github>cplieger/.github:two-branch']},
            {'extends': ['github>cplieger/.github:two-branch'], 'automerge': False},
            ['github>cplieger/.github:two-branch'],
        ):
            with self.subTest(got=got):
                self.assertEqual(
                    self.hard_for(json.dumps(got)),
                    [
                        (
                            f'renovate.json on dev is {json.dumps(got)} '
                            f'(want {self.WANT}, which the ci sync writes)'
                        )
                    ],
                )

    def test_an_unread_config_grades_nothing(self):
        self.assertEqual(self.hard_for(None), [])

    def test_a_single_main_repo_on_dev_is_neither_read_nor_graded_for_it(self):
        sc = copy.deepcopy(MainDefaultIdentity.SCENARIOS[0])
        old = sc['meta']['name']
        sc['meta']['name'] = 'tool-catalog'
        sc['answers'] = {
            k.replace(f'repos/cplieger/{old}', 'repos/cplieger/tool-catalog'): v
            for k, v in sc['answers'].items()
        }
        sc['answers']['repos/cplieger/tool-catalog'].update(
            {'name': 'tool-catalog', 'default_branch': 'dev', 'fork': False, 'archived': False}
        )
        got = audit_over_answers(audit, sc)
        self.assertFalse([r for r in got['reads'] if '/contents/renovate.json' in r])
        self.assertFalse([h for h in got['hard'] if 'renovate.json' in h])
        self.assertIn(
            'default_branch=dev on a single-main repo (want main; tool-catalog publishes from main directly)',
            got['hard'],
        )
        sc['meta']['name'] = 'httpx'
        sc['answers'] = {
            k.replace('repos/cplieger/tool-catalog', 'repos/cplieger/httpx'): v
            for k, v in sc['answers'].items()
        }
        sc['answers']['repos/cplieger/httpx']['name'] = 'httpx'
        got = audit_over_answers(audit, sc)
        self.assertIn('repos/cplieger/httpx/contents/renovate.json?ref=dev', got['reads'])

    def collect_with(self, reply: subprocess.CompletedProcess) -> tuple[dict, list]:
        asked = []

        def fake_gh(*args):
            asked.append(gh_path(args))
            return reply

        original = audit.gh
        audit.gh = fake_gh
        s = {'errors': []}
        try:
            audit.collect_renovate_config('httpx', 'dev', s)
        finally:
            audit.gh = original
        return s, asked

    def test_collect_reads_the_file_on_the_default_branch(self):
        body = {'content': base64.b64encode(RENOVATE_JSON.encode()).decode()}
        s, asked = self.collect_with(gh_ok(body))
        self.assertEqual(asked, ['repos/cplieger/httpx/contents/renovate.json?ref=dev'])
        self.assertEqual((s['renovate_json'], s['errors']), (RENOVATE_JSON, []))

    def test_a_404_is_a_missing_file_and_any_other_failure_an_error(self):
        s, _ = self.collect_with(gh_http(404))
        self.assertEqual((s['renovate_json'], s['errors']), ('', []))
        for reply in (gh_http(403), gh_ok([{'name': 'x'}]), gh_ok({'content': 7})):
            with self.subTest(reply=reply.stdout):
                s, _ = self.collect_with(reply)
                self.assertIsNone(s['renovate_json'])
                self.assertEqual(s['errors'], ['renovate.json on dev unreadable (API), so not graded'])

    def test_content_that_does_not_decode_is_an_error_not_a_finding(self):
        for name, content in (
            ('bad base64', '!!!!'),
            ('bad utf-8', base64.b64encode(b'{"extends": ["\xff"]}').decode()),
        ):
            with self.subTest(name):
                s, _ = self.collect_with(gh_ok({'content': content}))
                self.assertIsNone(s['renovate_json'])
                self.assertEqual(s['errors'], ['renovate.json on dev unreadable (API), so not graded'])
        body = {'content': base64.encodebytes(RENOVATE_JSON.encode()).decode()}
        s, _ = self.collect_with(gh_ok(body))
        self.assertEqual((s['renovate_json'], s['errors']), (RENOVATE_JSON, []))


class RestTransport(unittest.TestCase):
    """The audit's readers keep their None / API_ERROR contracts on the shared
    REST policy, driven through the `gh` process boundary."""

    def replies(self, *replies: subprocess.CompletedProcess) -> tuple[list, list]:
        """Answer the next `gh` calls in order; (paths asked, sleeps taken)."""
        queue, asked, slept = list(replies), [], []

        def fake_gh(*args):
            asked.append(gh_path(args))
            return queue.pop(0)

        saved = (audit.gh, audit.REST.sleep)
        audit.gh, audit.REST.sleep = fake_gh, slept.append
        self.addCleanup(setattr, audit, 'gh', saved[0])
        self.addCleanup(setattr, audit.REST, 'sleep', saved[1])
        return asked, slept

    def test_a_5xx_is_retried_and_a_later_success_is_definitive(self):
        _, slept = self.replies(gh_http(502), gh_ok({'a': 1}))
        self.assertEqual(audit.gh_json('repos/cplieger/x'), {'a': 1})
        self.assertEqual(slept, [2])

    def test_a_5xx_that_outlasts_the_retries_is_an_api_error(self):
        asked, slept = self.replies(*(gh_http(503) for _ in range(4)))
        self.assertIs(audit.gh_json('repos/cplieger/x'), audit.API_ERROR)
        self.assertEqual((len(asked), slept), (4, [2, 4, 8]))

    def test_a_rate_limit_past_the_cap_is_an_api_error_never_absence(self):
        limited = subprocess.CompletedProcess(
            ['gh'],
            1,
            stdout='HTTP/2.0 403 Forbidden\nX-Ratelimit-Remaining: 0\r\n'
            'X-Ratelimit-Reset: 4102444800\r\n\r\n{"message": "API rate limit exceeded"}',
            stderr='',
        )
        _, slept = self.replies(limited, limited, limited)
        self.assertIs(audit.gh_json('repos/cplieger/x'), audit.API_ERROR)
        self.assertIs(audit.gh_json_strict('repos/cplieger/x'), audit.API_ERROR)
        self.assertIsNone(audit.file_text('x', 'go.mod'))
        self.assertEqual(slept, [])

    def test_a_definitive_4xx_is_absence_for_gh_json_and_an_error_for_strict(self):
        self.replies(gh_http(403))
        self.assertIsNone(audit.gh_json('repos/cplieger/x'))
        self.replies(gh_http(403))
        self.assertIs(audit.gh_json_strict('repos/cplieger/x'), audit.API_ERROR)
        self.replies(gh_http(404))
        self.assertIsNone(audit.gh_json_strict('repos/cplieger/x'))

    def test_api_status_reads_the_status(self):
        self.replies(subprocess.CompletedProcess(['gh'], 0, stdout='HTTP/2.0 204 No Content\n\r\n'))
        self.assertEqual(audit.api_status('repos/cplieger/x/vulnerability-alerts'), (True, True))
        self.replies(gh_http(404))
        self.assertEqual(audit.api_status('repos/cplieger/x/vulnerability-alerts'), (False, True))

    def test_file_text_decodes_a_file_and_reads_a_404_as_absent(self):
        body = {'content': base64.encodebytes(b'module github.com/cplieger/x\n').decode()}
        asked, _ = self.replies(gh_ok(body))
        self.assertEqual(audit.file_text('x', 'go.mod'), 'module github.com/cplieger/x\n')
        self.assertEqual(asked, ['repos/cplieger/x/contents/go.mod'])
        self.replies(gh_http(404))
        self.assertEqual(audit.file_text('x', 'go.mod'), '')

    def test_file_text_of_a_directory_is_unknown_not_content(self):
        self.replies(gh_ok([{'name': 'a.go'}]))
        self.assertIsNone(audit.file_text('x', 'cmd'))
        self.replies(gh_ok({'type': 'submodule'}))
        self.assertIsNone(audit.file_text('x', 'vendor'))

    def test_file_text_reads_content_that_does_not_decode_as_empty(self):
        for body in (
            {'content': '!!!!', 'encoding': 'base64'},
            {'content': 'a', 'encoding': 'base64'},
            {'content': '', 'encoding': 'none'},
        ):
            with self.subTest(body=body):
                self.replies(gh_ok(body))
                self.assertEqual(audit.file_text('x', 'go.mod'), '')


class Discovery(unittest.TestCase):
    """main()'s REST repo listing."""

    REPOS: ClassVar[list] = [
        {
            'name': 'httpx',
            'archived': False,
            'fork': False,
            'visibility': 'public',
            'default_branch': 'main',
            'created_at': '2026-01-01T00:00:00Z',
            'has_issues': True,
            'owner': {'login': 'cplieger'},
        },
        {'name': 'old', 'archived': True, 'fork': False, 'visibility': 'public'},
        {'name': 'loki', 'archived': False, 'fork': True, 'visibility': 'public'},
    ]

    def run_main(self, reply: subprocess.CompletedProcess) -> tuple[int, list, list]:
        """(exit code, paths asked, metas collect() received) of main() with no
        repo readable, so it stops right after the listing."""
        asked, metas = [], []

        def fake_gh(*args):
            asked.append(gh_path(args))
            return reply

        def fake_collect(meta):
            metas.append(meta)
            return {'name': meta['name'], 'fatal': True, 'admin_visible': False}

        saved = (audit.gh, audit.collect, audit.sys.argv)
        audit.gh, audit.collect, audit.sys.argv = fake_gh, fake_collect, ['audit.py']
        try:
            with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
                audit.main()
        finally:
            audit.gh, audit.collect, audit.sys.argv = saved
        return caught.exception.code, asked, metas

    def test_the_listing_is_rest_and_drops_archived_repos_and_forks(self):
        code, asked, metas = self.run_main(gh_ok(self.REPOS))
        self.assertEqual(code, 2)
        self.assertEqual(asked, ['user/repos?affiliation=owner&per_page=100&page=1'])
        self.assertEqual(
            metas,
            [
                {
                    'name': 'httpx',
                    'archived': False,
                    'fork': False,
                    'visibility': 'public',
                    'default_branch': 'main',
                    'created_at': '2026-01-01T00:00:00Z',
                    'has_issues': True,
                }
            ],
            'the meta carries the listing fields and nothing else',
        )

    def test_a_failed_listing_exits_2(self):
        code, asked, metas = self.run_main(gh_http(401))
        self.assertEqual((code, len(asked), metas), (2, 1, []))


RUN_ENV: dict = {
    'GITHUB_SERVER_URL': 'https://github.com',
    'GITHUB_REPOSITORY': 'cplieger/ci',
    'GITHUB_RUN_ID': '7',
}
RUN_LINK = 'https://github.com/cplieger/ci/actions/runs/7'
TITLE = 'Repository audit findings'


class FakeIssues:
    """The issues API of every cplieger repo, in memory, behind the ghrest calls
    tracker_issue makes: has_issues, the open-issue listing, labels, create,
    edit, comment and close."""

    def __init__(self, disabled=(), fail=()):
        self.disabled, self.fail = set(disabled), set(fail)
        self.issues: dict[str, list[dict]] = {}
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[dict] = []
        self.reads: list[str] = []
        self.writes: list[tuple[str, str]] = []

    def repo_of(self, path: str) -> str:
        return path.split('/')[2]

    def get(self, path, *_args, **_kw):
        self.reads.append(path)
        name = self.repo_of(path)
        return {'name': name, 'has_issues': name not in self.disabled}

    def pages(self, path, *_args, **_kw):
        self.reads.append(path)
        name = self.repo_of(path)
        if name in self.fail:
            raise audit.ghrest.ApiError(502, 'Bad Gateway')
        return [i for i in self.issues.get(name, []) if i['state'] == 'open']

    def send(self, method, path, body=None):
        self.writes.append((method, path))
        name, rest = self.repo_of(path), path.split('/', 3)[3]
        issues = self.issues.setdefault(name, [])
        if rest == 'labels':
            self.labels.append(body)
            return body
        if rest == 'issues':
            issue = {
                'number': len(issues) + 1,
                'state': 'open',
                'title': body['title'],
                'body': body['body'],
                'labels': [{'name': n} for n in body['labels']],
            }
            issues.append(issue)
            return issue
        number = int(rest.split('/')[1])
        issue = next(i for i in issues if i['number'] == number)
        if rest.endswith('/comments'):
            self.comments.append((name, number, body['body']))
        else:
            issue.update(body)
        return issue

    def __enter__(self):
        self.patches = [
            unittest.mock.patch.object(audit.ghrest, attr, getattr(self, attr))
            for attr in ('get', 'pages', 'send')
        ]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


def row(name: str, hard=(), warnings=(), **over) -> dict:
    return {
        'name': name,
        'visibility': 'public',
        'has_issues': True,
        'in_grace': False,
        'errors': False,
        'hard': list(hard),
        'warnings': list(warnings),
        **over,
    }


class IssueFiling(unittest.TestCase):
    """--file-issues: one rolling `repo-audit` issue per graded repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = unittest.mock.patch.dict(os.environ, RUN_ENV)
        env.start()
        self.addCleanup(env.stop)

    def findings(self, *rows, adopted=False, scope=None, visibility='all') -> str:
        path = Path(self.tmp.name) / 'findings.json'
        path.write_text(
            json.dumps(
                {
                    'scope': scope,
                    'visibility': visibility,
                    'adoption': {audit.STABLE_ONLY_WARNING: adopted},
                    'repos': list(rows),
                }
            ),
            encoding='utf-8',
        )
        return str(path)

    def file(self, fake: FakeIssues, path: str, *, dry_run=False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with fake, redirect_stdout(out), redirect_stderr(err):
            code = audit.file_issues(path, dry_run)
        return code, out.getvalue(), err.getvalue()

    def open_issues(self, fake: FakeIssues, name: str) -> list[dict]:
        return [i for i in fake.issues.get(name, []) if i['state'] == 'open']

    def test_findings_open_one_labelled_issue_with_each_line_as_printed(self):
        fake = FakeIssues()
        path = self.findings(
            row('httpx', hard=['no branch protection on default branch'], warnings=['0 topics'])
        )
        code, _, err = self.file(fake, path)
        self.assertEqual(code, 0, err)
        [issue] = self.open_issues(fake, 'httpx')
        self.assertEqual(issue['title'], TITLE)
        self.assertEqual({n['name'] for n in issue['labels']}, {'repo-audit', 'auto-generated'})
        body = issue['body']
        self.assertIn('## HARD findings\n\n```text\n[HARD] no branch protection', body)
        self.assertIn('## Warnings\n\n```text\n[warn] 0 topics\n```', body)
        self.assertTrue(body.endswith(f'Run: {RUN_LINK}\n'), body)
        self.assertIn(
            {
                'name': 'repo-audit',
                'color': 'd93f0b',
                'description': 'Repository governance audit findings',
            },
            fake.labels,
        )

    def test_the_held_warning_is_the_line_compliance_prints(self):
        s = legacy()
        s.update({'private': False, 'visibility': 'public', 'publishes': True})
        _, warn, _ = audit.compliance(s)
        self.assertIn(audit.STABLE_ONLY_WARNING, warn)
        self.assertIn(audit.STABLE_ONLY_WARNING, audit.FILED_AFTER_ADOPTION)

    def test_a_second_run_updates_the_same_issue_in_place(self):
        fake = FakeIssues()
        self.file(fake, self.findings(row('httpx', warnings=['0 topics'])))
        code, _, _ = self.file(fake, self.findings(row('httpx', warnings=['1 topics'])))
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.issues['httpx']), 1, 'no second issue')
        self.assertIn('[warn] 1 topics', fake.issues['httpx'][0]['body'])
        self.assertNotIn('[warn] 0 topics', fake.issues['httpx'][0]['body'])
        self.assertNotIn('## HARD findings', fake.issues['httpx'][0]['body'])

    def test_a_clean_repo_closes_its_issue_with_a_comment_naming_the_run(self):
        fake = FakeIssues()
        self.file(fake, self.findings(row('httpx', hard=['x'])))
        code, _, _ = self.file(fake, self.findings(row('httpx')))
        self.assertEqual(code, 0)
        self.assertEqual(fake.issues['httpx'][0]['state'], 'closed')
        self.assertEqual(len(fake.comments), 1)
        self.assertIn(RUN_LINK, fake.comments[0][2])

    def test_a_clean_repo_without_an_issue_opens_nothing(self):
        fake = FakeIssues()
        self.file(fake, self.findings(row('httpx')))
        self.assertEqual(fake.issues.get('httpx', []), [])
        self.assertEqual(fake.writes, [])

    def test_a_repo_in_grace_is_neither_opened_nor_closed(self):
        fake = FakeIssues()
        fake.issues['old'] = [
            {'number': 1, 'state': 'open', 'title': TITLE, 'body': 'b', 'labels': []}
        ]
        path = self.findings(row('new', hard=['x'], in_grace=True), row('old', in_grace=True))
        code, out, _ = self.file(fake, path)
        self.assertEqual(code, 0)
        self.assertEqual((fake.reads, fake.writes), ([], []))
        self.assertEqual(fake.issues['old'][0]['state'], 'open')
        self.assertIn('2 in grace', out)

    def test_a_repo_whose_read_hit_an_api_error_keeps_its_issue_untouched(self):
        fake = FakeIssues()
        fake.issues['httpx'] = [
            {'number': 1, 'state': 'open', 'title': TITLE, 'body': 'b', 'labels': []}
        ]
        for findings in (row('httpx', errors=True), row('httpx', hard=['x'], errors=True)):
            with self.subTest(hard=findings['hard']):
                code, out, _ = self.file(fake, self.findings(findings))
                self.assertEqual(code, 0)
                self.assertEqual((fake.reads, fake.writes), ([], []))
                self.assertEqual(fake.issues['httpx'][0]['body'], 'b')
                self.assertEqual(fake.issues['httpx'][0]['state'], 'open')
                self.assertIn('skipped on API errors', out)

    def test_a_repo_with_issues_disabled_reaches_the_tracker_notice_and_writes_nothing(self):
        fake = FakeIssues(disabled={'httpx'})
        code, _, err = self.file(fake, self.findings(row('httpx', hard=['x'], has_issues=False)))
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes, [])
        self.assertIn('::notice::cplieger/httpx has issues disabled', err)

    def test_the_stable_only_warning_is_held_back_until_a_repo_defaults_to_dev(self):
        fake = FakeIssues()
        fake.issues['only'] = [
            {'number': 1, 'state': 'open', 'title': TITLE, 'body': 'b', 'labels': []}
        ]
        path = self.findings(
            row('both', warnings=[audit.STABLE_ONLY_WARNING, '0 topics']),
            row('only', warnings=[audit.STABLE_ONLY_WARNING]),
            row('fresh', warnings=[audit.STABLE_ONLY_WARNING]),
        )
        self.file(fake, path)
        [both] = self.open_issues(fake, 'both')
        self.assertIn('[warn] 0 topics', both['body'])
        self.assertNotIn('stable channel', both['body'])
        self.assertEqual(fake.issues['only'][0]['state'], 'closed', 'its only finding is held')
        self.assertEqual(fake.issues.get('fresh', []), [], 'nothing opened for it')

    def test_the_hold_ends_once_any_repo_defaults_to_dev(self):
        fake = FakeIssues()
        self.file(
            fake, self.findings(row('only', warnings=[audit.STABLE_ONLY_WARNING]), adopted=True)
        )
        [issue] = self.open_issues(fake, 'only')
        self.assertIn(f'[warn] {audit.STABLE_ONLY_WARNING}', issue['body'])

    def test_a_scoped_findings_file_is_refused_before_any_call(self):
        fake = FakeIssues()
        for scope, visibility in ((['httpx'], 'all'), (None, 'public')):
            with self.subTest(scope=scope, visibility=visibility):
                path = self.findings(row('httpx', hard=['x']), scope=scope, visibility=visibility)
                code, _, err = self.file(fake, path)
                self.assertEqual(code, 2)
                self.assertIn('scoped run', err)
                self.assertEqual((fake.reads, fake.writes), ([], []))

    def test_an_unusable_findings_file_exits_2(self):
        bad = Path(self.tmp.name) / 'bad.json'
        for text in ('{not json', '{"repos": {}}'):
            with self.subTest(text=text):
                bad.write_text(text, encoding='utf-8')
                self.assertEqual(self.file(FakeIssues(), str(bad))[0], 2)
        self.assertEqual(self.file(FakeIssues(), str(bad) + '.absent')[0], 2)

    def test_only_public_repos_are_filed_and_each_other_repo_says_so(self):
        fake = FakeIssues()
        fake.issues['secret'] = [
            {'number': 1, 'state': 'open', 'title': TITLE, 'body': 'b', 'labels': []}
        ]
        path = self.findings(
            row('open', hard=['x']),
            row('secret', visibility='private'),
            row('hidden', hard=['y'], visibility='private'),
            row('legacy', hard=['z'], visibility=None),
        )
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                code, out, err = self.file(fake, path, dry_run=dry_run)
                self.assertEqual(code, 0, err)
                for name in ('secret', 'hidden', 'legacy'):
                    self.assertIn(f'::notice::{name}: not public', out)
                    self.assertNotIn(f'{name}: would', out)
                self.assertIn('3 not public', out)
                self.assertLessEqual({fake.repo_of(p) for p in fake.reads}, {'open'})
                self.assertLessEqual({fake.repo_of(p) for _, p in fake.writes}, {'open'})
        self.assertEqual({fake.repo_of(p) for _, p in fake.writes}, {'open'})
        self.assertEqual(fake.issues['secret'][0]['state'], 'open')
        self.assertEqual(fake.issues.get('hidden', []), [])
        self.assertEqual(len(self.open_issues(fake, 'open')), 1)

    def test_one_repo_failing_to_file_does_not_stop_the_others_and_exits_1(self):
        fake = FakeIssues(fail={'a'})
        path = self.findings(row('a', hard=['x']), row('b', hard=['y']))
        code, _, err = self.file(fake, path)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.open_issues(fake, 'b')), 1)
        self.assertIn('filing failed for a', err)

    def test_a_dry_run_reads_and_writes_nothing(self):
        fake = FakeIssues()
        path = self.findings(row('a', hard=['x']), row('b'), row('c', has_issues=False, hard=['z']))
        code, out, _ = self.file(fake, path, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual((fake.reads, fake.writes), ([], []))
        self.assertIn('a: would upsert (1 HARD, 0 warnings)', out)
        self.assertIn('b: would close-when-clean', out)
        self.assertIn('c: would upsert (1 HARD, 0 warnings) (issues disabled', out)
        self.assertIn('dry run: 2 upsert · 1 close-when-clean', out)

    def test_a_finding_carrying_an_html_comment_or_backticks_renders_literally(self):
        marker = "README carries no '<!-- hub-overview BEGIN -->' pair, see ```x```"
        body = audit.issue_body([marker], [], RUN_LINK)
        self.assertIn(f'````text\n[HARD] {marker}\n````', body)


class Grace(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def created(self, age: timedelta) -> str:
        return (self.NOW - age).isoformat().replace('+00:00', 'Z')

    def test_the_boundary_is_24_hours(self):
        self.assertTrue(audit.in_grace(self.created(timedelta(hours=23, minutes=59)), self.NOW))
        self.assertFalse(audit.in_grace(self.created(timedelta(hours=24)), self.NOW))

    def test_an_unparseable_or_unzoned_creation_time_is_unknown(self):
        for value in (None, '', 'yesterday', '2026-10-05T00:00:00'):
            with self.subTest(value=value):
                self.assertIsNone(audit.in_grace(value, self.NOW))


class FindingsOut(unittest.TestCase):
    """main() --findings-out: what the grading run hands the filing step."""

    def run_main(self, listing: list, settings: dict, *argv: str) -> tuple[int, str, dict | None]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_path = Path(tmp.name) / 'findings.json'
        filed = []
        saved = (audit.gh, audit.collect, audit.sys.argv, audit.tracker_issue.main)
        audit.gh = lambda *_args: gh_ok(listing)
        audit.collect = lambda meta: copy.deepcopy(settings[meta['name']])
        audit.sys.argv = ['audit.py', '--findings-out', str(out_path), *argv]
        audit.tracker_issue.main = lambda *a, **k: filed.append(a) or 0
        for phase in ('collect_first_party_majors', 'collect_codeowners'):
            patch = unittest.mock.patch.object(audit, phase)
            patch.start()
            self.addCleanup(patch.stop)
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                try:
                    audit.main()
                    code = 0
                except SystemExit as stop:
                    code = stop.code
        finally:
            audit.gh, audit.collect, audit.sys.argv, audit.tracker_issue.main = saved
        self.assertEqual(filed, [], 'a grading run never files')
        findings = json.loads(out_path.read_text()) if out_path.exists() else None
        return code, out.getvalue(), findings

    @staticmethod
    def meta(name: str, branch: str = 'main', age: timedelta = timedelta(days=30)) -> dict:
        created = datetime.now(UTC) - age
        return {
            'name': name,
            'archived': False,
            'fork': False,
            'visibility': 'private',
            'default_branch': branch,
            'created_at': created.isoformat().replace('+00:00', 'Z'),
            'has_issues': True,
        }

    def test_each_graded_repo_is_recorded_with_its_lines_grace_and_errors(self):
        broken, failing = legacy('broken'), legacy('failing')
        broken['has_protection'] = False
        failing['errors'] = ['vulnerability-alerts unreadable (API)']
        listing = [
            self.meta('broken'),
            self.meta('failing'),
            {**self.meta('young', age=timedelta(hours=23, minutes=59)), 'has_issues': False},
        ]
        settings = {'broken': broken, 'failing': failing, 'young': legacy('young')}
        code, out, findings = self.run_main(listing, settings)
        self.assertEqual(code, 1)
        self.assertEqual(findings['scope'], None)
        self.assertEqual(findings['visibility'], 'all')
        self.assertEqual(findings['adoption'], {audit.STABLE_ONLY_WARNING: False})
        self.assertEqual(
            findings['repos'],
            [
                row(
                    'broken',
                    hard=['no branch protection on default branch'],
                    visibility='private',
                ),
                row('failing', errors=True, visibility='private'),
                row('young', in_grace=True, has_issues=False, visibility='private'),
            ],
        )
        self.assertIn(' · 1 repos in grace (created < 24h, nothing filed)', out)

    def test_adoption_reads_every_discovered_repo_not_the_scoped_set(self):
        listing = [self.meta('a'), self.meta('b', branch='dev')]
        settings = {'a': legacy('a'), 'b': legacy('b')}
        _, _, findings = self.run_main(listing, settings, '--repo', 'a')
        self.assertEqual(findings['scope'], ['a'])
        self.assertEqual(findings['adoption'], {audit.STABLE_ONLY_WARNING: True})
        self.assertEqual([r['name'] for r in findings['repos']], ['a'])

    def test_an_unreadable_creation_time_is_an_error_so_nothing_is_filed(self):
        meta = self.meta('a')
        meta['created_at'] = None
        code, out, findings = self.run_main([meta], {'a': legacy('a')})
        self.assertEqual(code, 2)
        self.assertEqual(findings['repos'][0]['errors'], True)
        self.assertIn('[error] creation time unreadable', out)

    def test_file_issues_takes_no_audit_option(self):
        saved = audit.sys.argv
        audit.sys.argv = ['audit.py', '--file-issues', 'x.json', '--repo', 'a']
        try:
            with self.assertRaises(SystemExit) as stop, redirect_stderr(io.StringIO()) as err:
                audit.main()
        finally:
            audit.sys.argv = saved
        self.assertEqual(stop.exception.code, 2)
        self.assertIn('takes no audit option', err.getvalue())


CODEOWNERS_LINE = (
    "CODEOWNERS: '*' requests review from @cplieger on every pull request, "
    'bot-authored ones included'
)


class Codeowners(unittest.TestCase):
    """The CODEOWNERS file GitHub uses: a warning when its last `*` rule
    requests the owner's review, so a bot-authored pull request does too."""

    def setUp(self):
        self.asked = []
        self.files = {}
        saved = (audit.gh, audit.REST.sleep)
        audit.gh, audit.REST.sleep = self.fake_gh, lambda _seconds: None
        self.addCleanup(setattr, audit, 'gh', saved[0])
        self.addCleanup(setattr, audit.REST, 'sleep', saved[1])

    def fake_gh(self, *args):
        path = gh_path(args)
        self.asked.append(path)
        prefix = 'repos/cplieger/httpx/contents/'
        self.assertTrue(path.startswith(prefix), f'unexpected read {path}')
        answer = self.files.get(path.removeprefix(prefix), 404)
        if isinstance(answer, dict):
            return gh_ok(answer)
        return gh_http(answer) if isinstance(answer, int) else gh_ok(file_body(answer))

    def grade(self, files: dict | None = None) -> tuple[dict, list]:
        """Grade httpx with `files` ({path: text, a raw contents body, or the
        HTTP status its read fails with}); any other path is a 404."""
        self.files = files or {}
        self.asked.clear()
        s = legacy()
        audit.collect_codeowners(s)
        _, warn, _ = audit.compliance(s)
        return s, [w for w in warn if 'CODEOWNERS' in w]

    def read(self, *paths: str) -> list:
        return [f'repos/cplieger/httpx/contents/{p}' for p in paths]

    def test_no_file_at_any_location_is_compliant(self):
        s, warn = self.grade()
        self.assertEqual((s['codeowners_wildcard'], s['errors'], warn), (None, [], []))
        self.assertEqual(
            self.asked, self.read('.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS')
        )

    def test_a_star_rule_naming_the_owner_warns(self):
        for text in (
            '* @cplieger\n',
            '# default owner\n*\t@cplieger   # the maintainer\n',
            '* @someone @CPlieger\n',
        ):
            with self.subTest(text=text):
                s, warn = self.grade({'CODEOWNERS': text})
                self.assertEqual((s['codeowners_wildcard'], s['errors']), ('CODEOWNERS', []))
                self.assertEqual(warn, [CODEOWNERS_LINE])

    def test_the_last_star_rule_decides(self):
        _, warn = self.grade({'CODEOWNERS': '* @cplieger\n/docs/ @cplieger\n* @someone\n'})
        self.assertEqual(warn, [])
        _, warn = self.grade({'CODEOWNERS': '* @someone\n* @cplieger\n'})
        self.assertEqual(warn, [CODEOWNERS_LINE])

    def test_path_scoped_rules_alone_do_not_warn(self):
        for text in (
            '/docs/ @cplieger\n*.go @cplieger\n',
            '# * @cplieger\n',
            '* @cplieger-renovate\n',
            '* @someone # was @cplieger\n',
            '*\n',
        ):
            with self.subTest(text=text):
                s, warn = self.grade({'CODEOWNERS': text})
                self.assertEqual((s['codeowners_wildcard'], s['errors'], warn), (None, [], []))

    def test_the_first_file_present_is_the_only_one_read(self):
        s, warn = self.grade(
            {'.github/CODEOWNERS': '/docs/ @someone\n', 'CODEOWNERS': '* @cplieger\n'}
        )
        self.assertEqual((s['codeowners_wildcard'], warn), (None, []))
        self.assertEqual(self.asked, self.read('.github/CODEOWNERS'))
        s, warn = self.grade({'.github/CODEOWNERS': '* @cplieger\n', 'CODEOWNERS': '* @someone\n'})
        self.assertEqual(s['codeowners_wildcard'], '.github/CODEOWNERS')
        self.assertEqual(warn, [CODEOWNERS_LINE.replace('CODEOWNERS:', '.github/CODEOWNERS:')])
        s, warn = self.grade({'docs/CODEOWNERS': '* @cplieger\n'})
        self.assertEqual(s['codeowners_wildcard'], 'docs/CODEOWNERS')
        self.assertEqual(len(self.asked), 3)

    def test_an_empty_file_is_present_and_ends_the_lookup(self):
        s, warn = self.grade({'.github/CODEOWNERS': '', 'CODEOWNERS': '* @cplieger\n'})
        self.assertEqual((s['codeowners_wildcard'], s['errors'], warn), (None, [], []))
        self.assertEqual(self.asked, self.read('.github/CODEOWNERS'))
        s, warn = self.grade({'CODEOWNERS': '', 'docs/CODEOWNERS': '* @cplieger\n'})
        self.assertEqual((s['codeowners_wildcard'], s['errors'], warn), (None, [], []))
        self.assertEqual(self.asked, self.read('.github/CODEOWNERS', 'CODEOWNERS'))

    def test_an_unreadable_file_is_an_error_and_never_a_warning(self):
        s, warn = self.grade({'.github/CODEOWNERS': 403, 'CODEOWNERS': '* @cplieger\n'})
        self.assertEqual(s['errors'], ['.github/CODEOWNERS unreadable (API)'])
        self.assertEqual((s['codeowners_wildcard'], warn), (None, []))
        self.assertNotIn(self.read('CODEOWNERS')[0], self.asked)
        s, warn = self.grade({'.github/CODEOWNERS': 502, 'CODEOWNERS': '* @cplieger\n'})
        self.assertEqual(s['errors'], ['.github/CODEOWNERS unreadable (API)'])
        self.assertEqual((s['codeowners_wildcard'], warn), (None, []))
        self.assertNotIn(self.read('CODEOWNERS')[0], self.asked)
        s, warn = self.grade({'CODEOWNERS': 503, 'docs/CODEOWNERS': '* @cplieger\n'})
        self.assertEqual(s['errors'], ['CODEOWNERS unreadable (API)'])
        self.assertEqual((s['codeowners_wildcard'], warn), (None, []))

    def test_a_body_that_is_not_decodable_base64_is_an_error_and_ends_the_lookup(self):
        for body in UNDECODABLE_BODIES:
            with self.subTest(body=body):
                s, warn = self.grade({'.github/CODEOWNERS': body, 'CODEOWNERS': '* @cplieger\n'})
                self.assertEqual(s['errors'], ['.github/CODEOWNERS unreadable (API)'])
                self.assertEqual((s['codeowners_wildcard'], warn), (None, []))
                self.assertEqual(self.asked, self.read('.github/CODEOWNERS'))

    def test_the_warning_is_filed_without_waiting_for_adoption(self):
        self.assertNotIn(CODEOWNERS_LINE, audit.FILED_AFTER_ADOPTION)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'findings.json'
        adoption = dict.fromkeys(audit.FILED_AFTER_ADOPTION, False)
        path.write_text(
            json.dumps(
                {
                    'scope': None,
                    'visibility': 'all',
                    'adoption': adoption,
                    'repos': [row('httpx', warnings=[CODEOWNERS_LINE])],
                }
            ),
            encoding='utf-8',
        )
        fake = FakeIssues()
        with (
            unittest.mock.patch.dict(os.environ, RUN_ENV),
            fake,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = audit.file_issues(str(path), dry_run=False)
        self.assertEqual(code, 0)
        [issue] = fake.issues['httpx']
        self.assertIn(f'[warn] {CODEOWNERS_LINE}', issue['body'])

    def test_main_reads_each_graded_repo_and_prints_the_line(self):
        listing = [{**FindingsOut.meta('httpx'), 'visibility': 'public'}]
        self.files = {'CODEOWNERS': '* @cplieger\n'}

        def fake_gh(*args):
            if gh_path(args).startswith('user/repos?'):
                return gh_ok(listing)
            return self.fake_gh(*args)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_path = Path(tmp.name) / 'findings.json'
        saved = (audit.gh, audit.collect, audit.sys.argv)
        audit.gh, audit.collect = fake_gh, lambda meta: legacy(meta['name'])
        audit.sys.argv = ['audit.py', '--findings-out', str(out_path)]
        patch = unittest.mock.patch.object(audit, 'collect_first_party_majors')
        patch.start()
        self.addCleanup(patch.stop)
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                audit.main()
        finally:
            audit.gh, audit.collect, audit.sys.argv = saved
        self.assertIn(f'httpx  (priv)\n  [warn] {CODEOWNERS_LINE}\n', out.getvalue())
        self.assertEqual(
            json.loads(out_path.read_text())['repos'][0]['warnings'], [CODEOWNERS_LINE]
        )


class AuditWorkflow(unittest.TestCase):
    """audit.yaml grades with AUDIT_PAT and files with CI_SCHEDULE, each in its
    own step's env."""

    WORKFLOW = Path(__file__).resolve().parent.parent / '.github' / 'workflows' / 'audit.yaml'

    def test_the_filing_token_is_scoped_to_the_filing_step(self):
        text = self.WORKFLOW.read_text(encoding='utf-8')
        doc = yaml.safe_load(text)
        job = doc['jobs']['audit']
        steps = {s.get('name'): s for s in job['steps']}
        filing = steps['File audit findings as issues']
        self.assertEqual(filing['env'], {'GH_TOKEN': '${{ secrets.CI_SCHEDULE }}'})
        rest = copy.deepcopy(doc)
        next(s for s in rest['jobs']['audit']['steps'] if s.get('name') == filing['name']).pop(
            'env'
        )
        self.assertNotIn('CI_SCHEDULE', json.dumps(rest), 'nowhere else in the workflow')
        self.assertNotIn('env', doc)
        self.assertNotIn('env', job)
        self.assertIn('!cancelled()', filing['if'])
        self.assertIn("github.event_name == 'schedule'", filing['if'])
        self.assertIn('--file-issues "$findings"', filing['run'])
        grading = steps['Run cross-repo audit']
        self.assertIn('--findings-out "$RUNNER_TEMP/audit-findings.json"', grading['run'])
        self.assertNotIn('CI_SCHEDULE', json.dumps(grading))
        self.assertNotIn('issues', json.dumps(job.get('permissions', {})))
        self.assertEqual(doc['concurrency'], {'group': 'audit', 'cancel-in-progress': False})


def tag_rows(*names: str) -> list:
    """A tag listing page as the API returns it."""
    return [{'name': n, 'commit': {'sha': 'a' * 40}} for n in names]


def file_body(text: str) -> dict:
    return {'content': base64.b64encode(text.encode()).decode(), 'encoding': 'base64'}


# A lax decode reads each of these as '' or as text the body does not validly
# carry, so grading on it would pass a file nobody could read.
UNDECODABLE_BODIES = (
    {'content': '!!!!', 'encoding': 'base64'},
    {'content': base64.b64encode(b'* @cplieger\n').decode() + '!', 'encoding': 'base64'},
    {'content': '', 'encoding': 'none'},
    {'content': 'a', 'encoding': 'base64'},
)


def go_mod(*requires: str, tail: str = '') -> str:
    lines = ''.join(f'\t{r}\n' for r in requires)
    return f'module github.com/cplieger/consumer\n\ngo 1.27\n\nrequire (\n{lines})\n{tail}'


def package_json(**deps: dict) -> str:
    return json.dumps({'name': '@cplieger/consumer', **deps})


class FirstPartyMajors(unittest.TestCase):
    """Each go.mod and package.json on the default branch against the latest
    stable major of the first-party module it requires."""

    OWNED: ClassVar[set] = {'consumer', 'httpx', 'slogx', 'envx', 'reactive'}

    def setUp(self):
        self.asked = []
        self.answers = {}
        self.slow_tags = 0.0
        saved = (audit.gh, audit.REST.sleep)
        audit.gh, audit.REST.sleep = self.fake_gh, lambda _seconds: None
        self.addCleanup(setattr, audit, 'gh', saved[0])
        self.addCleanup(setattr, audit.REST, 'sleep', saved[1])

    def fake_gh(self, *args):
        path = gh_path(args)
        self.asked.append(path)
        self.assertIn(path, self.answers, f'unexpected read {path}')
        if '/tags?' in path and self.slow_tags:
            time.sleep(self.slow_tags)
        answer = self.answers[path]
        return gh_http(answer) if isinstance(answer, int) else gh_ok(answer)

    def serve(self, files: dict, tags: dict, name: str = 'consumer', tree=None) -> None:
        """Answer `name`'s tree (or `tree`, a body or an HTTP status) and each
        of `files` ({path: text}); `tags` maps a module repo to its tag names,
        or to the HTTP status its listing fails with."""
        self.answers[f'repos/cplieger/{name}/git/trees/main?recursive=1'] = (
            tree
            if tree is not None
            else {'tree': [{'path': p, 'type': 'blob'} for p in files], 'truncated': False}
        )
        for path, text in files.items():
            self.answers[f'repos/cplieger/{name}/contents/{path}'] = (
                text if isinstance(text, (int, dict)) else file_body(text)
            )
        for repo, names in tags.items():
            self.answers[f'repos/cplieger/{repo}/tags?per_page=100&page=1'] = (
                names if isinstance(names, int) else tag_rows(*names)
            )

    def grade(self, files: dict, tags: dict, *, root=None, latest=None, name='consumer', tree=None):
        """(stale_majors, errors) of the check on `name`; `root` stands in for
        the root manifests collect() already read."""
        self.serve(files, tags, name, tree)
        s = {'name': name, 'default_branch': 'main', 'errors': [], 'root_manifests': root or {}}
        audit.collect_first_party_majors(s, latest or audit.LatestMajors(), self.OWNED | {name})
        return s['stale_majors'], s['errors']

    def tag_reads(self) -> list:
        return [p for p in self.asked if '/tags?' in p]

    def test_a_pin_behind_the_latest_major_warns(self):
        got = self.grade(
            {'go.mod': go_mod('github.com/cplieger/httpx/v4 v4.2.0')},
            {'httpx': ['v5.0.1', 'v5.0.0', 'v4.2.0']},
        )
        self.assertEqual(got, (['go.mod: requires github.com/cplieger/httpx/v4, latest is v5'], []))

    def test_a_pin_on_the_latest_major_is_current(self):
        got = self.grade(
            {'go.mod': go_mod('github.com/cplieger/httpx/v5 v5.0.1 // indirect')},
            {'httpx': ['v5.0.1', 'v4.2.0']},
        )
        self.assertEqual(got, ([], []))

    def test_a_path_without_a_suffix_is_current_until_a_v2_exists(self):
        for tags, want in (
            (['v1.4.0', 'v1.0.0'], []),
            (['v0.3.0'], []),
            (['v2.0.0', 'v1.4.0'], ['go.mod: requires github.com/cplieger/slogx, latest is v2']),
        ):
            with self.subTest(tags=tags):
                got = self.grade(
                    {'go.mod': go_mod('github.com/cplieger/slogx v1.4.0')}, {'slogx': tags}
                )
                self.assertEqual(got, (want, []))

    def test_an_npm_range_is_compared_by_the_major_it_pins(self):
        for key, spec, warns in (
            ('dependencies', '^2.1.0', True),
            ('devDependencies', '2.1.2', True),
            ('peerDependencies', '~2.1.0', True),
            ('optionalDependencies', '2.x', True),
            ('dependencies', '^3.0.0', False),
        ):
            with self.subTest(key=key, spec=spec):
                got = self.grade(
                    {
                        'web/package.json': package_json(
                            **{key: {'@cplieger/reactive': spec, 'lit': '^3'}}
                        )
                    },
                    {'reactive': ['v3.0.0', 'v2.1.2']},
                )
                want = [f'web/package.json: requires @cplieger/reactive {spec}, latest is v3']
                self.assertEqual(got, (want if warns else [], []))

    def test_an_npm_v0_range_is_behind_a_v1_and_current_on_v0(self):
        for spec in ('^0.9.0', '0.9.2', '~0.9.0'):
            for tags, warns in ((['v0.9.2', 'v0.8.0'], False), (['v1.0.0', 'v0.9.2'], True)):
                with self.subTest(spec=spec, latest=tags[0]):
                    got = self.grade(
                        {'package.json': package_json(dependencies={'@cplieger/reactive': spec})},
                        {'reactive': tags},
                    )
                    want = [f'package.json: requires @cplieger/reactive {spec}, latest is v1']
                    self.assertEqual(got, (want if warns else [], []))

    def test_an_npm_spec_that_pins_no_single_major_reads_no_tags(self):
        for spec in (
            '>=2',
            'npm:@cplieger/reactive@2.0.0',
            'file:../reactive',
            'workspace:*',
            'github:cplieger/reactive',
            '^2 || ^3',
            'latest',
        ):
            with self.subTest(spec=spec):
                self.asked.clear()
                got = self.grade(
                    {'package.json': package_json(dependencies={'@cplieger/reactive': spec})},
                    {'reactive': ['v3.0.0']},
                )
                self.assertEqual((got, self.tag_reads()), (([], []), []))

    def test_a_nested_module_is_judged_by_its_lane_tags(self):
        mod = go_mod(
            'github.com/cplieger/envx/v3 v3.0.0', 'github.com/cplieger/envx/yamlenv/v2 v2.0.1'
        )
        lanes = ['v3.0.0', 'v2.0.3', 'yamlenv/v2.0.1', 'yamlenv/v1.2.3']
        self.assertEqual(self.grade({'go.mod': mod}, {'envx': lanes}), ([], []))
        got = self.grade({'go.mod': mod}, {'envx': ['yamlenv/v3.0.0', *lanes]})
        self.assertEqual(
            got, (['go.mod: requires github.com/cplieger/envx/yamlenv/v2, latest is v3'], [])
        )

    def test_single_line_and_quoted_requirements_are_read(self):
        mod = (
            'module github.com/cplieger/consumer\n\n'
            'require github.com/cplieger/httpx/v4 v4.0.0 // indirect\n'
            'require "github.com/cplieger/slogx" v1.0.0\n'
        )
        got = self.grade({'go.mod': mod}, {'httpx': ['v5.0.0'], 'slogx': ['v2.0.0']})
        self.assertEqual(
            got,
            (
                [
                    'go.mod: requires github.com/cplieger/httpx/v4, latest is v5',
                    'go.mod: requires github.com/cplieger/slogx, latest is v2',
                ],
                [],
            ),
        )

    def test_a_nested_go_mod_is_read_and_named_in_the_line(self):
        got = self.grade(
            {'go.mod': go_mod(), 'tools/go.mod': go_mod('github.com/cplieger/httpx/v4 v4.0.0')},
            {'httpx': ['v5.0.0']},
        )
        self.assertEqual(
            got, (['tools/go.mod: requires github.com/cplieger/httpx/v4, latest is v5'], [])
        )

    def test_a_dev_tag_above_the_latest_stable_is_ignored(self):
        got = self.grade(
            {'go.mod': go_mod('github.com/cplieger/httpx/v5 v5.1.0')},
            {'httpx': ['v6.0.0-dev.3', 'v6.0.0-dev.2', 'v5.1.0']},
        )
        self.assertEqual(got, ([], []))

    def test_a_requirement_replaced_by_a_local_directory_is_skipped(self):
        for target in ('../httpx', './third_party/httpx', '/src/httpx'):
            with self.subTest(target=target):
                self.asked.clear()
                mod = go_mod(
                    'github.com/cplieger/httpx/v4 v4.0.0',
                    tail=f'\nreplace github.com/cplieger/httpx/v4 => {target}\n',
                )
                self.assertEqual(self.grade({'go.mod': mod}, {'httpx': ['v5.0.0']}), ([], []))
                self.assertEqual(self.tag_reads(), [])
        mod = go_mod(
            'github.com/cplieger/httpx/v4 v4.0.0',
            tail='\nreplace (\n\tgithub.com/cplieger/httpx/v4 v4.0.0 => example.com/fork/httpx/v4 v4.0.1\n)\n',
        )
        got = self.grade({'go.mod': mod}, {'httpx': ['v5.0.0']})
        self.assertEqual(got, (['go.mod: requires github.com/cplieger/httpx/v4, latest is v5'], []))

    def test_unreadable_tags_are_an_error_and_never_a_warning(self):
        got = self.grade({'go.mod': go_mod('github.com/cplieger/httpx/v4 v4.0.0')}, {'httpx': 500})
        self.assertEqual(
            got,
            (
                [],
                ['go.mod: github.com/cplieger/httpx/v4 not graded, because tags of httpx unreadable (API)'],
            ),
        )

    def test_a_module_s_tags_are_read_once_for_every_consumer(self):
        latest = audit.LatestMajors()
        for name in ('a', 'b'):
            got = self.grade(
                {'go.mod': go_mod('github.com/cplieger/httpx/v4 v4.0.0')},
                {'httpx': ['v5.0.0']},
                latest=latest,
                name=name,
            )
            self.assertEqual(
                got[0], ['go.mod: requires github.com/cplieger/httpx/v4, latest is v5']
            )
        self.assertEqual(len(self.tag_reads()), 1)

    def test_concurrent_consumers_share_one_tag_read(self):
        self.slow_tags = 0.05
        names = [f'c{i}' for i in range(8)]
        for name in names:
            self.serve(
                {'go.mod': go_mod('github.com/cplieger/httpx/v4 v4.0.0')},
                {'httpx': ['v5.0.0']},
                name,
            )
        latest = audit.LatestMajors()

        def grade(name):
            s = {'name': name, 'default_branch': 'main', 'errors': [], 'root_manifests': {}}
            audit.collect_first_party_majors(s, latest, self.OWNED | set(names))
            return s['stale_majors']

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(grade, names))
        self.assertEqual(
            results, [['go.mod: requires github.com/cplieger/httpx/v4, latest is v5']] * 8
        )
        self.assertEqual(len(self.tag_reads()), 1)

    def test_manifests_under_skipped_directories_are_not_read(self):
        skipped = [
            'vendor/github.com/cplieger/httpx/v4/go.mod',
            'web/node_modules/@cplieger/reactive/package.json',
            'internal/testdata/go.mod',
            '.github/package.json',
            '_scratch/go.mod',
        ]
        tree = {'tree': [{'path': p, 'type': 'blob'} for p in skipped], 'truncated': False}
        self.assertEqual(self.grade({}, {}, tree=tree), ([], []))
        self.assertEqual(self.asked, ['repos/cplieger/consumer/git/trees/main?recursive=1'])

    def test_the_root_manifests_collect_read_are_not_read_again(self):
        text = go_mod('github.com/cplieger/httpx/v4 v4.0.0')
        tree = {'tree': [{'path': 'go.mod', 'type': 'blob'}], 'truncated': False}
        got = self.grade({}, {'httpx': ['v5.0.0']}, root={'go.mod': text}, tree=tree)
        self.assertEqual(got, (['go.mod: requires github.com/cplieger/httpx/v4, latest is v5'], []))
        self.assertNotIn('repos/cplieger/consumer/contents/go.mod', self.asked)
        # collect() already recorded the unreadable root file as an error.
        self.assertEqual(self.grade({}, {}, root={'go.mod': None}, tree=tree), ([], []))

    def test_a_requirement_naming_no_repository_is_an_error(self):
        got = self.grade(
            {
                'go.mod': go_mod('github.com/cplieger/gone/v2 v2.0.0'),
                'web/package.json': package_json(dependencies={'@cplieger/gone': '^1.0.0'}),
            },
            {},
        )
        self.assertEqual(
            got,
            (
                [],
                [
                    'go.mod: requires github.com/cplieger/gone/v2, which names no cplieger repository',
                    'web/package.json: requires @cplieger/gone ^1.0.0, which names no cplieger repository',
                ],
            ),
        )
        self.assertEqual(self.tag_reads(), [])

    def test_an_unreadable_or_truncated_tree_grades_nothing(self):
        for tree, errors in (
            (500, ['file tree unreadable (API), so first-party majors not graded']),
            ([{'path': 'go.mod'}], ['file tree unreadable (API), so first-party majors not graded']),
            ({'sha': 'a' * 40}, ['file tree unreadable (API), so first-party majors not graded']),
            (
                {'tree': [], 'truncated': True},
                ['file tree truncated, so first-party majors not graded'],
            ),
            (409, []),
        ):
            with self.subTest(tree=tree):
                self.assertEqual(self.grade({}, {}, tree=tree), ([], errors))

    def test_an_unreadable_manifest_is_an_error(self):
        for status in (500, 404):
            with self.subTest(status=status):
                got = self.grade({'tools/go.mod': status}, {})
                self.assertEqual(
                    got, ([], ['tools/go.mod unreadable (API), so its first-party majors not graded'])
                )

    def test_a_manifest_body_that_is_not_decodable_base64_is_an_error(self):
        error = 'tools/go.mod unreadable (API), so its first-party majors not graded'
        for body in UNDECODABLE_BODIES:
            with self.subTest(body=body):
                self.assertEqual(self.grade({'tools/go.mod': body}, {}), ([], [error]))

    def test_an_empty_root_read_is_read_again_strictly(self):
        tree = {'tree': [{'path': 'go.mod', 'type': 'blob'}], 'truncated': False}
        error = 'go.mod unreadable (API), so its first-party majors not graded'
        for body in UNDECODABLE_BODIES:
            with self.subTest(body=body):
                got = self.grade({'go.mod': body}, {}, root={'go.mod': ''}, tree=tree)
                self.assertEqual(got, ([], [error]))
        self.assertEqual(self.grade({'go.mod': ''}, {}, root={'go.mod': ''}, tree=tree), ([], []))

    def test_a_package_json_that_is_not_an_object_is_an_error(self):
        for text in ('[1, 2]', '{"dependencies": '):
            with self.subTest(text=text):
                got = self.grade({'web/package.json': text}, {})
                self.assertEqual(
                    got,
                    (
                        [],
                        [
                            'web/package.json is not a JSON object, so its first-party majors not graded'
                        ],
                    ),
                )

    def test_main_runs_the_check_for_every_repo_and_prints_its_warnings(self):
        listing = [
            {**FindingsOut.meta('consumer'), 'visibility': 'public'},
            {**FindingsOut.meta('httpx'), 'archived': True},
        ]
        self.answers['user/repos?affiliation=owner&per_page=100&page=1'] = listing
        for path in audit.CODEOWNERS_PATHS:
            self.answers[f'repos/cplieger/consumer/contents/{path}'] = 404
        self.serve({'go.mod': go_mod('github.com/cplieger/httpx/v4 v4.0.0')}, {'httpx': ['v5.0.0']})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_path = Path(tmp.name) / 'findings.json'
        saved = (audit.collect, audit.sys.argv)
        audit.collect = lambda meta: legacy(meta['name'])
        audit.sys.argv = ['audit.py', '--findings-out', str(out_path)]
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                audit.main()
        finally:
            audit.collect, audit.sys.argv = saved
        line = 'go.mod: requires github.com/cplieger/httpx/v4, latest is v5'
        self.assertIn(f'consumer  (priv)\n  [warn] {line}\n', out.getvalue())
        self.assertEqual(json.loads(out_path.read_text())['repos'][0]['warnings'], [line])


if __name__ == '__main__':
    unittest.main()
