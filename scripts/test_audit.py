"""Tests for audit.py's grading of the two branch models, on hand-built settings."""

from __future__ import annotations

import copy
import json
import subprocess
import unittest
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import audit

HOST = 'komodo.example'
DEV_RULESET = audit.expected_ruleset('dev')
MAIN_RULESET = audit.expected_ruleset('main')


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
        'two_channel': default_branch == 'dev',
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
    s = base_settings(name, 'dev')
    s['rulesets_full'] = {'dev': live(DEV_RULESET), 'main': live(MAIN_RULESET)}
    s['custom_rulesets'] = [
        {'name': 'dev', 'enforcement': 'active'},
        {'name': 'main', 'enforcement': 'active'},
    ]
    s['ruleset_bypass_actors'] = [('main', 'RepositoryRole', 5)]
    s['required_checks'] = ['ci / validate']
    s['required_check_apps'] = {'ci / validate': 15368}
    s['webhooks'] = []
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
        s['ruleset_bypass_actors'].append(('main', 'Integration', 1234))
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
    return subprocess.CompletedProcess(['gh'], 0, stdout=json.dumps(body), stderr='')


def gh_http(status: int) -> subprocess.CompletedProcess:
    """`gh api` on an HTTP error: exit 1, the body on stdout, the status in
    the one stderr line gh_retry and gh_json_strict read it from."""
    body = json.dumps({'message': 'from the API', 'status': str(status)})
    return subprocess.CompletedProcess(
        ['gh'], 1, stdout=body, stderr=f'gh: from the API (HTTP {status})'
    )


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
        API path to its CompletedProcess, so gh_retry and both JSON readers run
        as they do live; a path with no reply fails the test by name."""

        def fake_gh(*args):
            path = args[1]
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
        missing = audit.dev_tags_without_receipt(
            [('v1.4.0-dev.1', self.A), ('v1.4.0-dev.2', self.B), ('v1.4.0-dev.3', self.C)],
            statuses.get,
        )
        self.assertEqual(missing, ['v1.4.0-dev.2', 'v1.4.0-dev.3'])

    def test_unreadable_statuses_skip_the_dev_tag(self):
        missing = audit.dev_tags_without_receipt(
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
            f'repos/cplieger/httpx/commits/{self.A}/status': {
                'statuses': [{'context': 'release/tag/v1.4.0-dev.1', 'state': 'success'}]
            },
            f'repos/cplieger/httpx/commits/{self.B}/status': {'statuses': []},
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
        self.assertNotIn(f'repos/cplieger/httpx/commits/{self.C}/status', asked)

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
            f'repos/cplieger/envx/commits/{self.A}/status': {
                'statuses': [{'context': 'release/tag/v1.4.0-dev.1', 'state': 'success'}]
            },
            f'repos/cplieger/envx/commits/{self.B}/status': {
                'statuses': [{'context': 'release/tag/v9.0.1-dev.1', 'state': 'success'}]
            },
            **self.commits('envx', self.A, self.B, self.C),
            **self.runs('envx'),
        }
        s = self.collect('envx', answers)
        self.assertEqual(s['stable_tags_without_release'], ['yamlenv/v9.0.0'])
        self.assertEqual(s['hand_made_stable_tags'], [])
        self.assertEqual(s['dev_tags_without_receipt'], ['yamlenv/v9.0.1-dev.1'])
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
            f'repos/cplieger/envx/commits/{self.A}/status': {
                'statuses': [
                    {'context': f'release/tag/v1.5.0-dev.{n}', 'state': 'success'}
                    for n in range(1, 6)
                ]
            },
            f'repos/cplieger/envx/commits/{self.B}/status': {'statuses': []},
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
        # v1.5.0 was just created on a commit that soaked for a day, so the
        # commit's age says nothing; the promotion's release run is what says
        # the Release is still on its way.
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
        # The same old soaked commit with its release run over and no Release
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
        reply = subprocess.CompletedProcess(['gh'], 0, stdout='<html>maintenance</html>', stderr='')
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

    def test_main_repo_release_hook_required(self):
        s = legacy('knell')
        s['webhooks'] = [hook(['registry_package'])]
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any("lack 'release'" in h for h in hard), hard)

    def test_other_default_branch_is_hard(self):
        s = legacy()
        s['default_branch'] = 'master'
        hard, _, _ = audit.compliance(s)
        self.assertTrue(any(h.startswith('default_branch=master') for h in hard), hard)


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
        main_rules = {r['type'] for r in MAIN_RULESET['rules']}
        self.assertEqual(
            main_rules,
            {'update', 'creation', 'deletion', 'required_linear_history', 'non_fast_forward'},
        )
        self.assertEqual(
            MAIN_RULESET['bypass_actors'],
            [{'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'}],
        )
        self.assertEqual(
            json.dumps(DEV_RULESET['conditions']['ref_name']['include']), '["refs/heads/dev"]'
        )


if __name__ == '__main__':
    unittest.main()
