"""release_maintenance.py and release-maintenance.yaml, against a recorded-shape fake
of `gh` that answers REST paths from a fixture and logs every call."""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'release-maintenance.yaml'
DOCKER_RELEASE = ROOT / '.github' / 'workflows' / 'docker-release.yaml'
TESTDATA = SCRIPTS / 'testdata' / 'release-maintenance'
sys.path.insert(0, str(SCRIPTS))

import release_channels as rc  # noqa: E402
import release_maintenance as rm  # noqa: E402
import tracker_issue  # noqa: E402
import workflow_replay  # noqa: E402

R = 'subflux'
P = f'repos/cplieger/{R}'
NOW = dt.datetime(2026, 10, 10, 13, 0, tzinfo=dt.UTC)  # a Saturday
SHA = 'a' * 40
MAIN_HEAD = 'b' * 40
CHECKS = 'check-runs?check_name=ci%20%2F%20validate&filter=latest&per_page=100'
OPEN = f'GET {P}/pulls?state=open&per_page=100&page=1'
CLOSED = f'GET {P}/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=100&page=1'
SHIPPED = f'GET {P}/actions/workflows/release.yaml/runs?branch=main&status=success&per_page=1'
ARTIFACTS = f'GET {P}/actions/artifacts?name=security-main&per_page=100'
DASHBOARD = f'GET {P}/issues?state=open&labels=renovate&per_page=100&page=1'
DOCKERFILE = f'GET {P}/contents/Dockerfile?ref=main'
TWO_BRANCH_BOARD = (TESTDATA / 'dashboard-two-branch.md').read_text()


def request_key(args) -> str:
    """`METHOD path` of an `api -i [-X METHOD] [-H ...] path [--input -]` argv, the
    joined argv of any other command."""
    if args[0] != 'api':
        return ' '.join(args)
    method, path, i = 'GET', None, 1
    while i < len(args):
        if args[i] in ('-X', '-H', '--input'):
            method = args[i + 1] if args[i] == '-X' else method
            i += 2
        elif args[i] == '-i':
            i += 1
        else:
            path, i = args[i], i + 1
    return f'{method} {path}'


class Status:
    """A non-2xx answer for FakeGh, printed as `gh api -i` prints it."""

    def __init__(self, code: int, body=None):
        self.code, self.body = code, body or {'message': f'HTTP {code}'}


class FakeGh:
    """`gh` with REST answers keyed `METHOD path`; anything unlisted is a GhError.
    An API answer is printed as `gh api -i` prints a 200 (a Status as its own code);
    an exception value is raised, so it reaches the caller as it is."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(self, args, stdin=None, timeout=None):
        self.calls.append((list(args), stdin))
        key = request_key(args)
        if key not in self.responses:
            raise rm.GhError(f'unexpected call {key}')
        value = self.responses[key]
        if callable(value):
            value = value(args, stdin)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, Status):
            out = f'HTTP/2.0 {value.code} Error\n\r\n{json.dumps(value.body)}'.encode()
            return subprocess.CompletedProcess(args, 1, out, b'')
        out = value if isinstance(value, bytes) else json.dumps(value).encode()
        if args[0] == 'api':
            out = b'HTTP/2.0 200 OK\nContent-Type: application/json\r\n\r\n' + out
        return subprocess.CompletedProcess(args, 0, out, b'')

    def called(self) -> list[str]:
        return [request_key(args) for args, _ in self.calls]


def pr(
    number,
    head,
    base='main',
    labels=(),
    sha=SHA,
    created='2026-10-10T02:00:00Z',
    updated=None,
    merged=None,
    fork=False,
    draft=False,
    auto=None,
):
    return {
        'number': number,
        'state': 'closed' if merged else 'open',
        'head': {
            'ref': head,
            'sha': sha,
            'repo': {'full_name': 'someone/subflux' if fork else f'cplieger/{R}'},
        },
        'base': {'ref': base},
        'labels': [{'name': n} for n in labels],
        'draft': draft,
        'auto_merge': auto,
        'created_at': created,
        'updated_at': updated or merged or created,
        'merged_at': merged,
        'merge_commit_sha': f'{number:040d}' if merged else None,
    }


def checks(*runs):
    return {
        'check_runs': [
            {'app': {'id': app}, 'status': status, 'conclusion': conclusion}
            for app, status, conclusion in runs
        ]
    }


GREEN = checks((15368, 'completed', 'success'))


def scan_zip(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('security-main.json', json.dumps(doc))
    return buf.getvalue()


def other_file_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('other.json', '{}')
    return buf.getvalue()


def record(findings=(), *, complete=True, errors=(), not_covered=(), commit=MAIN_HEAD):
    return {
        'schema': 1,
        'repo': f'cplieger/{R}',
        'commit': commit,
        'image': None,
        'findings': list(findings),
        'not_covered': list(not_covered),
        'complete': complete,
        'errors': list(errors),
    }


def finding(id_, package, installed, fixed, klass):
    return {
        'id': id_,
        'package': package,
        'installed': installed,
        'fixed': fixed,
        'severity': 'HIGH',
        'class': klass,
        'targets': ['go.mod'] if klass != 'os' else ['image'],
        'platforms': [],
        'sources': ['trivy-fs'],
    }


def scan_run(run_id=900, started='2026-10-10T03:05:00Z', event='workflow_dispatch', **over):
    run = {
        'id': run_id,
        'path': '.github/workflows/security.yml',
        'event': event,
        'head_sha': MAIN_HEAD,
        'head_repository': {'full_name': f'cplieger/{R}'},
        'run_started_at': started,
    }
    run.update(over)
    return run


def dashboard_issue(
    number=3, login='cplieger', body=TWO_BRANCH_BOARD, title='Dependency Dashboard'
):
    return {'number': number, 'title': title, 'user': {'login': login}, 'body': body}


def world(**over) -> dict:
    """A quiet two-branch repo: nothing open, main shipped, a clean scan of main."""
    responses = {
        OPEN: [],
        SHIPPED: {
            'workflow_runs': [
                {
                    'created_at': '2026-10-01T00:00:00Z',
                    'updated_at': '2026-10-01T00:20:00Z',
                    'head_sha': MAIN_HEAD,
                }
            ]
        },
        CLOSED: [],
        ARTIFACTS: {
            'artifacts': [
                {'id': 77, 'expired': False, 'workflow_run': {'id': 900, 'head_branch': 'main'}}
            ]
        },
        f'GET {P}/actions/runs/900': scan_run(),
        f'GET {P}/actions/artifacts/77/zip': scan_zip(record()),
        DASHBOARD: [dashboard_issue()],
    }
    responses.update(over)
    return responses


def plan(responses: dict, now=NOW) -> tuple[dict, FakeGh]:
    gh = FakeGh(responses)
    return rm.plan_repo(rm.Api(gh), R, now), gh


class Discovery(unittest.TestCase):
    def test_only_public_dev_default_repos_outside_the_single_main_set(self):
        repos = [
            {
                'name': 'subflux',
                'archived': False,
                'fork': False,
                'visibility': 'public',
                'default_branch': 'dev',
            },
            {
                'name': 'envx',
                'archived': False,
                'fork': False,
                'visibility': 'public',
                'default_branch': 'main',
            },
            {
                'name': 'tool-catalog',
                'archived': False,
                'fork': False,
                'visibility': 'public',
                'default_branch': 'dev',
            },
            {
                'name': 'old',
                'archived': True,
                'fork': False,
                'visibility': 'public',
                'default_branch': 'dev',
            },
            {
                'name': 'loki',
                'archived': False,
                'fork': True,
                'visibility': 'public',
                'default_branch': 'dev',
            },
            {
                'name': 'private-repo',
                'archived': False,
                'fork': False,
                'visibility': 'private',
                'default_branch': 'dev',
            },
            {
                'name': 'knell',
                'archived': False,
                'fork': False,
                'visibility': 'public',
                'default_branch': 'dev',
            },
        ]
        gh = FakeGh({'GET user/repos?affiliation=owner&per_page=100&page=1': repos})
        self.assertEqual(rm.discover(rm.Api(gh), set()), ['knell', 'subflux'])
        self.assertEqual(rm.discover(rm.Api(gh), {'knell', 'envx'}), ['knell'])

    def test_every_main_default_repo_today_reads_nothing_beyond_the_listing(self):
        repos = [
            {
                'name': n,
                'archived': False,
                'fork': False,
                'visibility': 'public',
                'default_branch': 'main',
            }
            for n in ('subflux', 'tool-catalog', 'ci')
        ]
        gh = FakeGh({'GET user/repos?affiliation=owner&per_page=100&page=1': repos})
        with tempfile.TemporaryDirectory() as tmp:
            args = rm.argparse.Namespace(out=f'{tmp}/plan.json', only='', now=rm.iso(NOW))
            self.assertEqual(rm.cmd_plan(args, rm.Api(gh)), 0)
            self.assertEqual(json.loads(Path(args.out).read_text())['repos'], [])
        self.assertEqual(gh.called(), ['GET user/repos?affiliation=owner&per_page=100&page=1'])


class SecurityMerges(unittest.TestCase):
    def plan_with(self, prs, check=GREEN):
        responses = world(**{OPEN: prs})
        for p in prs:
            responses[f'GET {P}/commits/{p["head"]["sha"]}/{CHECKS}'] = check
        return plan(responses)

    def test_an_allowlisted_green_pull_request_is_planned_with_its_head_on_either_branch(self):
        row, _ = self.plan_with(
            [
                pr(
                    10,
                    'renovate/dev-golang.org-x-net-vulnerability',
                    'dev',
                    ['security', 'security-minor'],
                ),
                pr(
                    11,
                    'renovate/main-golang.org-x-net-vulnerability',
                    'main',
                    ['security', 'security-pinDigest'],
                    sha='c' * 40,
                ),
            ]
        )
        self.assertEqual(row['errors'], [])
        self.assertEqual(
            row['merge'],
            [
                {'number': 10, 'base': 'dev', 'sha': SHA, 'labels': ['security-minor']},
                {'number': 11, 'base': 'main', 'sha': 'c' * 40, 'labels': ['security-pinDigest']},
            ],
        )

    def test_labels_outside_the_allowlist_heads_and_states_are_never_planned(self):
        cases = {
            'security alone (the retag alarm)': pr(1, 'renovate/dev-x', 'dev', ['security']),
            'a major': pr(1, 'renovate/dev-x', 'dev', ['security', 'major-update']),
            'a major beside an allowlisted label': pr(
                1, 'renovate/dev-x', 'dev', ['security-major', 'security-minor']
            ),
            'a fork head': pr(1, 'renovate/dev-x', 'dev', ['security-patch'], fork=True),
            'a human head': pr(1, 'fix/x', 'dev', ['security-patch']),
            'a draft': pr(1, 'renovate/dev-x', 'dev', ['security-patch'], draft=True),
            'another base': pr(1, 'renovate/release-x', 'release', ['security-patch']),
        }
        for name, case in cases.items():
            with self.subTest(name):
                row, gh = self.plan_with([case])
                self.assertEqual(row['merge'], [])
                self.assertFalse(any('check-runs' in k for k in gh.called()), 'no check read')

    def test_only_a_completed_green_check_from_github_actions_counts(self):
        cases = {
            'red': checks((15368, 'completed', 'failure')),
            'pending': checks((15368, 'in_progress', None)),
            'missing': checks(),
            'another app': checks((99, 'completed', 'success')),
            'one of two red': checks(
                (15368, 'completed', 'success'), (15368, 'completed', 'failure')
            ),
        }
        for name, check in cases.items():
            with self.subTest(name):
                row, _ = self.plan_with([pr(1, 'renovate/dev-x', 'dev', ['security-patch'])], check)
                self.assertEqual(row['merge'], [])
                self.assertIn('is not green', row['notes'][0])

    def test_an_armed_pull_request_reads_no_check_and_is_noted(self):
        row, gh = self.plan_with(
            [pr(1, 'renovate/dev-x', 'dev', ['security-patch'], auto={'merge_method': 'squash'})]
        )
        self.assertEqual(row['merge'], [])
        self.assertEqual(row['notes'], ['#1 into dev: auto-merge already armed'])
        self.assertFalse(any('check-runs' in k for k in gh.called()))

    def test_a_merge_rereads_the_pull_request_and_its_check_and_is_never_armed(self):
        for base in ('dev', 'main'):
            with self.subTest(base=base):
                result, called, checked = self.fallback(GREEN, base=base)
                self.assertEqual(result, 'merged')
                self.assertEqual(
                    called,
                    [
                        f'GET {P}/pulls/7',
                        f'GET {P}/commits/{SHA}/{CHECKS}',
                        checked,
                        f'DELETE {P}/git/refs/heads/renovate/{base}-x',
                    ],
                )
                self.assertFalse([c for c in called if '--auto' in c])
        want = {'number': 7, 'base': 'dev', 'sha': SHA}
        checked = f'PUT {P}/pulls/7/merge'
        gh = FakeGh(
            {
                f'GET {P}/pulls/7': pr(7, 'renovate/dev-x', 'dev', ['security-patch']),
                f'GET {P}/commits/{SHA}/{CHECKS}': GREEN,
                checked: rm.GhError('head moved'),
            }
        )
        self.assertTrue(rm.merge_pr(rm.Api(gh), R, want, gh).startswith('failed: '))

    def test_a_dev_pull_request_retargeted_after_plan_is_never_merged_directly(self):
        for planned, now in (('dev', 'main'), ('main', 'dev')):
            with self.subTest(planned=planned, now=now):
                result, called, _ = self.fallback(GREEN, base=planned, now_base=now)
                self.assertTrue(result.startswith('failed: '), result)
                self.assertIn(f'is not renovate/ of cplieger/{R} into {planned}', result)
                self.assertEqual(called, [f'GET {P}/pulls/7'])

    def fallback(self, check, head_sha=SHA, labels=('security-patch',), base='main', now_base=''):
        want = {'number': 7, 'base': base, 'sha': SHA}
        checked = f'PUT {P}/pulls/7/merge'
        gh = FakeGh(
            {
                f'GET {P}/pulls/7': pr(
                    7, f'renovate/{base}-x', now_base or base, labels, sha=head_sha
                ),
                f'GET {P}/commits/{SHA}/{CHECKS}': check,
                checked: {'merged': True},
                f'DELETE {P}/git/refs/heads/renovate/{base}-x': b'',
            }
        )
        return rm.merge_pr(rm.Api(gh), R, want, gh), gh.called(), checked

    def main_fallback(self, check, head_sha=SHA, labels=('security-patch',)):
        return self.fallback(check, head_sha, labels)

    def test_a_security_pull_request_into_main_is_merged_directly_only_on_a_fresh_green(self):
        result, called, checked = self.main_fallback(checks((15368, 'completed', 'success')))
        self.assertEqual(result, 'merged')
        self.assertEqual(
            called[:3], [f'GET {P}/pulls/7', f'GET {P}/commits/{SHA}/{CHECKS}', checked]
        )

    def test_green_at_plan_red_at_merge_leaves_the_pull_request_open(self):
        for name, check in (
            ('red', checks((15368, 'completed', 'failure'))),
            ('rerun pending', checks((15368, 'in_progress', None))),
        ):
            with self.subTest(name):
                result, called, checked = self.main_fallback(check)
                self.assertTrue(result.startswith('failed: '), result)
                self.assertIn('is not green', result)
                self.assertNotIn(checked, called)
        result, called, checked = self.main_fallback(
            checks((15368, 'completed', 'success')), head_sha=MAIN_HEAD
        )
        self.assertIn('its head moved', result)
        self.assertNotIn(checked, called)

    def test_labels_changed_since_plan_leave_the_pull_request_open(self):
        green = checks((15368, 'completed', 'success'))
        for base in ('dev', 'main'):
            for labels in ((), ('security',), ('security-patch', 'security-major')):
                with self.subTest(base=base, labels=labels):
                    result, called, _ = self.fallback(green, labels=labels, base=base)
                    self.assertIn('labels no longer allow an unattended merge', result)
                    self.assertEqual(called, [f'GET {P}/pulls/7'])

    def test_a_dry_run_merges_nothing_and_opens_nothing(self):
        gh = FakeGh({})
        scripts = []
        with tempfile.TemporaryDirectory() as tmp:
            Path(f'{tmp}/plan.json').write_text(
                json.dumps(
                    {
                        'repos': [
                            {
                                'repo': R,
                                'merge': [{'number': 1, 'sha': SHA}],
                                'rebuild': {'reason': 'x'},
                            }
                        ]
                    }
                )
            )
            args = rm.argparse.Namespace(
                plan=f'{tmp}/plan.json', out=f'{tmp}/merged.json', dry_run=True
            )
            rm.cmd_merge(args, gh, scripts.append)
            self.assertEqual(
                json.loads(Path(args.out).read_text()),
                {R: {'merge': {'1': 'dry run'}, 'rebuild': 'dry run'}},
            )
        self.assertEqual((gh.calls, scripts), ([], []))

    def test_a_planned_rebuild_runs_the_rebuild_script_against_main(self):
        seen = []

        def run_script(argv):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, 'Opened x', '')

        with tempfile.TemporaryDirectory() as tmp:
            Path(f'{tmp}/plan.json').write_text(
                json.dumps({'repos': [{'repo': R, 'merge': [], 'rebuild': {'reason': 'Because.'}}]})
            )
            args = rm.argparse.Namespace(
                plan=f'{tmp}/plan.json', out=f'{tmp}/merged.json', dry_run=False
            )
            rm.cmd_merge(args, FakeGh({}), run_script)
            self.assertEqual(json.loads(Path(args.out).read_text())[R]['rebuild'], 'opened')
        self.assertEqual(
            seen, [['bash', str(SCRIPTS / 'rebuild-stale.sh'), 'open-pr', R, 'main', 'Because.']]
        )


class Schedule(unittest.TestCase):
    def test_the_saturday_window_is_hours_0_to_5_in_paris(self):
        self.assertTrue(
            rm.in_group_window(dt.datetime(2026, 10, 9, 22, 0, tzinfo=dt.UTC))
        )  # Sat 00:00 CEST
        self.assertTrue(
            rm.in_group_window(dt.datetime(2026, 10, 10, 3, 59, tzinfo=dt.UTC))
        )  # 05:59
        self.assertFalse(
            rm.in_group_window(dt.datetime(2026, 10, 10, 4, 0, tzinfo=dt.UTC))
        )  # 06:00
        self.assertFalse(rm.in_group_window(dt.datetime(2026, 10, 9, 21, 59, tzinfo=dt.UTC)))  # Fri

    def test_the_deadline_is_the_next_saturday_noon_utc(self):
        sat = dt.datetime(2026, 10, 10, 2, tzinfo=dt.UTC)
        self.assertEqual(rm.saturday_deadline(sat), dt.datetime(2026, 10, 10, 12, tzinfo=dt.UTC))
        self.assertEqual(
            rm.saturday_deadline(dt.datetime(2026, 10, 10, 12, tzinfo=dt.UTC)),
            dt.datetime(2026, 10, 17, 12, tzinfo=dt.UTC),
        )
        self.assertEqual(
            rm.saturday_deadline(dt.datetime(2026, 10, 7, 9, tzinfo=dt.UTC)),
            dt.datetime(2026, 10, 10, 12, tzinfo=dt.UTC),
        )


GROUP = rc.MAIN_GROUP_BRANCH


class Blockers(unittest.TestCase):
    def test_the_saturday_group_opens_a_blocker_at_noon_and_not_before(self):
        group = pr(12, GROUP, created='2026-10-10T02:00:00Z')
        row, _ = plan(
            world(**{OPEN: [group]}), now=dt.datetime(2026, 10, 10, 11, 59, tzinfo=dt.UTC)
        )
        self.assertEqual(row['blockers']['weekly'], [])
        row, _ = plan(world(**{OPEN: [group]}), now=dt.datetime(2026, 10, 10, 12, 1, tzinfo=dt.UTC))
        self.assertEqual(
            row['blockers']['weekly'],
            [f'#12 (Saturday group, `{GROUP}`) is still open. It was due by 2026-10-10 12:00 UTC.'],
        )

    def test_a_merged_group_with_no_release_since_is_a_blocker_and_a_shipped_one_is_not(self):
        merged = pr(12, GROUP, created='2026-10-10T02:00:00Z', merged='2026-10-10T03:00:00Z')
        row, _ = plan(world(**{CLOSED: [merged]}))
        self.assertEqual(len(row['blockers']['weekly']), 1)
        self.assertIn(
            'merged at 2026-10-10 03:00 UTC, and no release run on `main`',
            row['blockers']['weekly'][0],
        )
        shipped = world(
            **{
                CLOSED: [merged],
                SHIPPED: {
                    'workflow_runs': [
                        {
                            'created_at': '2026-10-10T03:01:00Z',
                            'updated_at': '2026-10-10T03:30:00Z',
                            'head_sha': MAIN_HEAD,
                        }
                    ]
                },
                f'GET {P}/compare/{12:040d}...{MAIN_HEAD}': {'status': 'ahead'},
            }
        )
        row, _ = plan(shipped)
        self.assertEqual((row['errors'], row['blockers']['weekly']), ([], []))

    def test_a_release_run_that_does_not_contain_the_merge_ships_nothing(self):
        merged = pr(12, GROUP, created='2026-10-10T02:00:00Z', merged='2026-10-10T03:00:00Z')
        responses = world(
            **{
                CLOSED: [merged],
                SHIPPED: {
                    'workflow_runs': [
                        {
                            'created_at': '2026-10-10T03:01:00Z',
                            'updated_at': '2026-10-10T03:30:00Z',
                            'head_sha': MAIN_HEAD,
                        }
                    ]
                },
                f'GET {P}/compare/{12:040d}...{MAIN_HEAD}': {'status': 'behind'},
            }
        )
        row, _ = plan(responses)
        self.assertEqual(len(row['blockers']['weekly']), 1)

    def test_an_old_merge_stays_blocked_while_no_release_has_ever_succeeded(self):
        old = pr(
            30,
            'rebuild/main-20260901',
            created='2026-09-01T06:00:00Z',
            merged='2026-09-01T07:00:00Z',
        )
        human = pr(31, 'feature/x', updated='2026-10-09T00:00:00Z', merged='2026-10-09T00:00:00Z')
        row, _ = plan(world(**{CLOSED: [human, old], SHIPPED: {'workflow_runs': []}}))
        self.assertEqual(row['errors'], [])
        self.assertEqual(
            row['blockers']['late'],
            [
                (
                    '#30 (rebuild, `rebuild/main-20260901`) merged at 2026-09-01 07:00 UTC, and no '
                    'release run on `main` has succeeded since. It was due by 2026-09-01 09:00 UTC.'
                )
            ],
        )

    def test_a_listing_past_the_page_cap_with_no_release_leaves_every_section_unread(self):
        page = [pr(100 + i, 'feature/x', merged='2026-10-09T00:00:00Z') for i in range(100)]
        listing = {
            f'GET {P}/pulls?state=closed&base=main&sort=updated&direction=desc'
            f'&per_page=100&page={n}': page
            for n in range(1, rm.PAGE_CAP + 1)
        }
        row, _ = plan(world(**{**listing, SHIPPED: {'workflow_runs': []}}))
        self.assertEqual(len(row['errors']), 1, row['errors'])
        self.assertTrue(row['errors'][0].startswith('pull requests into main: '), row['errors'])
        self.assertEqual(row['unread'], [key for key, _ in rm.SECTIONS])

    def test_after_a_successful_release_the_listing_stops_a_day_before_it(self):
        recent = pr(40, 'feature/x', merged='2026-10-09T00:00:00Z')
        before = pr(
            41,
            'rebuild/main-20260929',
            created='2026-09-29T06:00:00Z',
            merged='2026-09-29T07:00:00Z',
        )
        row, gh = plan(world(**{CLOSED: [recent, before]}))
        self.assertEqual((row['errors'], row['blockers']['late']), ([], []))
        self.assertNotIn(f'GET {P}/compare/{41:040d}...{MAIN_HEAD}', gh.called())

    def test_security_expedited_and_rebuild_pull_requests_get_three_hours(self):
        created = '2026-10-10T09:59:00Z'
        prs = [
            pr(
                20,
                'renovate/main-x-vulnerability',
                labels=['security', 'security-patch'],
                created=created,
                auto={'m': 1},
            ),
            pr(21, GROUP, created=created),
            pr(22, 'rebuild/main-20261010', created=created),
            pr(
                23,
                'renovate/main-y-vulnerability',
                labels=['security', 'security-major'],
                created=created,
            ),
            pr(24, 'renovate/main-z', created=created),
            pr(25, 'rebuild/main-20261010', created=created, fork=True),
        ]
        row, _ = plan(world(**{OPEN: prs}), now=dt.datetime(2026, 10, 10, 12, 58, tzinfo=dt.UTC))
        self.assertEqual(row['blockers']['late'], [])
        row, _ = plan(world(**{OPEN: prs}), now=dt.datetime(2026, 10, 10, 12, 59, tzinfo=dt.UTC))
        self.assertEqual(
            row['blockers']['late'],
            [
                '#20 (security, `renovate/main-x-vulnerability`) is still open. It was due by 2026-10-10 12:59 UTC.',
                f'#21 (expedited group, `{GROUP}`) is still open. It was due by 2026-10-10 12:59 UTC.',
                '#22 (rebuild, `rebuild/main-20261010`) is still open. It was due by 2026-10-10 12:59 UTC.',
                (
                    '#23 (security, `renovate/main-y-vulnerability`) is still open. It was due by '
                    '2026-10-10 12:59 UTC. Its labels allow no automatic merge, so it waits for you.'
                ),
            ],
        )


def board(text=TWO_BRANCH_BOARD):
    return {DASHBOARD: [dashboard_issue(body=text)]}


def with_scan(*findings, **extra):
    return world(**{f'GET {P}/actions/artifacts/77/zip': scan_zip(record(findings)), **extra})


NET = finding('GO-2026-1', 'golang.org/x/net', 'v0.30.0', 'v0.31.0', 'manifest')
OPEN_GROUP_LINE = (
    f' - [ ] <!-- rebase-branch={GROUP} -->[fix(deps): update weekly dependencies (main)](../pull/12) '
    '(`golang.org/x/net`, `filippo.io/age`)'
)


def open_group_board(line=OPEN_GROUP_LINE):
    text = TWO_BRANCH_BOARD.replace(
        f' - [ ] <!-- unschedule-branch={GROUP} -->fix(deps): update weekly dependencies (main) '
        '(`golang.org/x/net`, `github.com/cplieger/health`)\n',
        '',
    ).replace('/pull/41)\n', f'/pull/41)\n{line}\n')
    return board(text)


class Expedite(unittest.TestCase):
    AWAIT = f' - [ ] <!-- unschedule-branch={GROUP} -->fix(deps): update weekly dependencies (main) (`golang.org/x/net`, `github.com/cplieger/health`)'

    def test_an_absent_group_is_expedited_by_its_awaiting_schedule_checkbox(self):
        row, _ = plan(with_scan(NET))
        self.assertEqual((row['errors'], row['blockers']['unrouted']), ([], []))
        self.assertEqual(row['tick'], {'issue': 3, 'line': self.AWAIT, 'kind': 'unschedule'})

    def test_a_ticked_awaiting_checkbox_is_left_alone(self):
        ticked = TWO_BRANCH_BOARD.replace(
            f' - [ ] <!-- unschedule-branch={GROUP}', f' - [x] <!-- unschedule-branch={GROUP}'
        )
        row, _ = plan(with_scan(NET, **board(ticked)))
        self.assertEqual((row['tick'], row['blockers']['unrouted']), (None, []))

    def test_a_waiting_group_that_does_not_name_the_package_is_no_route(self):
        row, _ = plan(
            with_scan(finding('GO-2', 'golang.org/x/text', 'v0.20.0', 'v0.21.0', 'manifest'))
        )
        self.assertIsNone(row['tick'])
        self.assertEqual(
            row['blockers']['unrouted'],
            [
                '`GO-2` in `golang.org/x/text` v0.20.0, fixed in v0.21.0 (go.mod): the `main` group does not update this package.'
            ],
        )

    def test_the_standard_library_and_a_distroless_os_package_expedite_without_a_package_check(
        self,
    ):
        go = finding('GO-3', 'stdlib', 'v1.27.1', 'v1.27.3', 'stdlib')
        os_pkg = finding('CVE-1', 'libssl3', '3.0.1', '3.0.2', 'os')
        distroless = {
            DOCKERFILE: {'content': base64.b64encode(b'FROM gcr.io/distroless/static\n').decode()}
        }
        with_base = board(
            TWO_BRANCH_BOARD.replace('`golang.org/x/net`', '`gcr.io/distroless/static`')
        )
        for f, extra in ((go, {}), (os_pkg, with_base)):
            with self.subTest(f['class']):
                row, _ = plan(with_scan(f, **distroless, **extra))
                self.assertEqual((row['errors'], row['blockers']['unrouted']), ([], []))
                self.assertEqual(row['tick']['kind'], 'unschedule')
                self.assertIsNone(row['rebuild'])

    def test_a_distroless_os_package_needs_a_group_that_updates_a_base_image(self):
        os_pkg = finding('CVE-1', 'libssl3', '3.0.1', '3.0.2', 'os')
        text = (
            'FROM golang:1.27@sha256:aa AS build\n'
            'FROM build AS test\n'
            'FROM gcr.io/distroless/static:nonroot@sha256:bb\n'
        )
        self.assertEqual(rm.final_base_images(text), {'gcr.io/distroless/static'})
        self.assertEqual(rm.final_base_images('ARG BASE=alpine\nFROM ${BASE}\n'), {'alpine'})
        self.assertIsNone(rm.final_base_images('ARG B\nFROM ${B}\n'))
        self.assertEqual(
            rm.final_base_images('ARG R=ghcr.io\nARG T\nFROM $R/x/img:${T:-1} AS f\n'),
            {'ghcr.io/x/img'},
        )
        self.assertIsNone(rm.final_base_images('FROM alpine\nARG B=alpine\nFROM ${B}\n'))
        self.assertEqual(rm.final_base_images('FROM $B AS b\nFROM alpine:3\n'), {'alpine'})
        self.assertEqual(
            rm.final_base_images('FROM scratch\nFROM localhost:5000/img:1\n'),
            {'localhost:5000/img'},
        )
        chained = 'FROM golang:1 AS go\nFROM alpine:3@sha256:cc AS base\nFROM base AS final\n'
        self.assertEqual(rm.final_base_images(chained), {'alpine'})
        self.assertEqual(rm.final_base_images('FROM golang AS b\nFROM scratch\n'), set())
        shifted = 'FROM node:22 AS b\nRUN echo $((x<<n)) > /n\nFROM gcr.io/distroless/static\n'
        self.assertEqual(rm.final_base_images(shifted), {'gcr.io/distroless/static'})
        dockerfile = {DOCKERFILE: {'content': base64.b64encode(text.encode()).decode()}}
        unrouted_line = (
            '`CVE-1` in `libssl3` 3.0.1, fixed in 3.0.2 (image): '
            'the `main` group does not update this package.'
        )
        row, _ = plan(with_scan(os_pkg, **dockerfile))
        self.assertIsNone(row['tick'])
        self.assertEqual(row['blockers']['unrouted'], [unrouted_line])
        group = pr(12, GROUP, created='2026-10-07T10:00:00Z', updated='2026-10-10T02:00:00Z')
        row, _ = plan(with_scan(os_pkg, **{OPEN: [group]}, **dockerfile, **open_group_board()))
        self.assertIsNone(row['tick'])
        self.assertEqual(len(row['blockers']['unrouted']), 1)
        unknown = (
            "`CVE-1` in `libssl3` 3.0.1, fixed in 3.0.2 (image): the final image's base is "
            'named by an ARG with no default, so no `main` group update is known to reach '
            'this package.'
        )
        for name, text, unrouted, tick in (
            ('unresolved', b'ARG B\nFROM ${B}\n', [unknown], None),
            (
                'resolved to a base the group does not list',
                b'ARG B=gcr.io/distroless/static:nonroot\nFROM ${B}\n',
                [unrouted_line],
                None,
            ),
        ):
            with self.subTest(name):
                arg = {DOCKERFILE: {'content': base64.b64encode(text).decode()}}
                row, _ = plan(with_scan(os_pkg, **arg))
                self.assertEqual((row['blockers']['unrouted'], row['tick']), (unrouted, tick))
        listed = board(TWO_BRANCH_BOARD.replace('`golang.org/x/net`', '`gcr.io/distroless/static`'))
        arg = {
            DOCKERFILE: {
                'content': base64.b64encode(b'ARG B=gcr.io/distroless/static\nFROM $B\n').decode()
            }
        }
        row, _ = plan(with_scan(os_pkg, **arg, **listed))
        self.assertEqual((row['blockers']['unrouted'], row['tick']['kind']), ([], 'unschedule'))

    def test_a_builder_base_in_the_group_does_not_reach_a_distroless_os_package(self):
        os_pkg = finding('CVE-1', 'libssl3', '3.0.1', '3.0.2', 'os')
        text = b'FROM golang:1.27 AS build\nFROM gcr.io/distroless/static:nonroot\n'
        dockerfile = {DOCKERFILE: {'content': base64.b64encode(text).decode()}}
        awaiting = board(TWO_BRANCH_BOARD.replace('`golang.org/x/net`', '`golang`'))
        line = OPEN_GROUP_LINE.replace('`golang.org/x/net`', '`golang`')
        group = pr(12, GROUP, created='2026-10-07T10:00:00Z', updated='2026-10-10T02:00:00Z')
        not_in_group = (
            '`CVE-1` in `libssl3` 3.0.1, fixed in 3.0.2 (image): '
            'the `main` group does not update this package.'
        )
        for name, extra in (
            ('awaiting', awaiting),
            ('open', {OPEN: [group], **open_group_board(line)}),
        ):
            with self.subTest(name):
                row, _ = plan(with_scan(os_pkg, **dockerfile, **extra))
                self.assertIsNone(row['tick'])
                self.assertEqual(row['blockers']['unrouted'], [not_in_group])
        final = board(TWO_BRANCH_BOARD.replace('`golang.org/x/net`', '`gcr.io/distroless/static`'))
        row, _ = plan(with_scan(os_pkg, **dockerfile, **final))
        self.assertEqual((row['blockers']['unrouted'], row['tick']['kind']), ([], 'unschedule'))

    def test_an_existing_group_gets_its_rebase_checkbox_once_per_scan(self):
        group = pr(12, GROUP, created='2026-10-07T10:00:00Z', updated='2026-10-10T02:00:00Z')
        row, _ = plan(with_scan(NET, **{OPEN: [group]}, **open_group_board()))
        self.assertEqual(row['tick'], {'issue': 3, 'line': OPEN_GROUP_LINE, 'kind': 'rebase'})
        self.assertEqual(row['blockers']['unrouted'], [])
        later = pr(12, GROUP, created='2026-10-07T10:00:00Z', updated='2026-10-10T03:30:00Z')
        row, _ = plan(with_scan(NET, **{OPEN: [later]}, **open_group_board()))
        self.assertIsNone(row['tick'], 'the scan predates the last update of the group')
        self.assertEqual(row['blockers']['unrouted'], [])
        ticked = open_group_board(line=OPEN_GROUP_LINE.replace(' - [ ] ', ' - [x] '))
        row, _ = plan(with_scan(NET, **{OPEN: [group]}, **ticked))
        self.assertEqual((row['tick'], row['blockers']['unrouted']), (None, []))

    def test_an_open_group_that_does_not_name_the_package_is_no_route(self):
        group = pr(12, GROUP, created='2026-10-07T10:00:00Z', updated='2026-10-10T02:00:00Z')
        text = finding('GO-2', 'golang.org/x/text', 'v0.20.0', 'v0.21.0', 'manifest')
        row, _ = plan(with_scan(text, **{OPEN: [group]}, **open_group_board()))
        self.assertIsNone(row['tick'])
        self.assertEqual(
            row['blockers']['unrouted'],
            [
                '`GO-2` in `golang.org/x/text` v0.20.0, fixed in v0.21.0 (go.mod): the `main` group does not update this package.'
            ],
        )

    def test_a_group_entry_with_no_package_list_expedites_only_the_standard_library(self):
        group = pr(12, GROUP, created='2026-10-07T10:00:00Z', updated='2026-10-10T02:00:00Z')
        suffix = ' (`golang.org/x/net`, `github.com/cplieger/health`)'
        waiting = board(TWO_BRANCH_BOARD.replace(suffix, ''))
        bare_open = OPEN_GROUP_LINE.replace(' (`golang.org/x/net`, `filippo.io/age`)', '')
        open_board = {OPEN: [group], **open_group_board(bare_open)}
        self.assertIsNone(rm.parse_dashboard(waiting[DASHBOARD][0]['body'])[GROUP]['packages'])
        distroless = {
            DOCKERFILE: {'content': base64.b64encode(b'FROM gcr.io/distroless/static\n').decode()}
        }
        os_pkg = finding('CVE-1', 'libssl3', '3.0.1', '3.0.2', 'os')
        for name, extra in (('awaiting', waiting), ('open', open_board)):
            for f, more in ((NET, {}), (os_pkg, distroless)):
                with self.subTest(name, finding=f['class']):
                    row, _ = plan(with_scan(f, **extra, **more))
                    self.assertEqual(row['errors'], [])
                    self.assertIsNone(row['tick'])
                    self.assertEqual(
                        row['blockers']['unrouted'],
                        [
                            (
                                f'{rm.finding_text(f)}: the Dependency Dashboard names no '
                                'packages for the `main` group, which Renovate does when the '
                                'group holds fewer than two, so nothing was expedited.'
                            )
                        ],
                    )
            with self.subTest(name, finding='stdlib'):
                go = finding('GO-3', 'stdlib', 'v1.27.1', 'v1.27.3', 'stdlib')
                row, _ = plan(with_scan(go, **extra))
                self.assertEqual((row['errors'], row['blockers']['unrouted']), ([], []))
                self.assertEqual(
                    row['tick']['kind'], 'unschedule' if name == 'awaiting' else 'rebase'
                )

    def test_an_open_group_without_a_usable_rebase_checkbox_is_no_route(self):
        group = pr(12, GROUP, created='2026-10-07T10:00:00Z', updated='2026-10-10T02:00:00Z')
        unusable = OPEN_GROUP_LINE.replace('rebase-branch=', 'retry-branch=')
        for name, dashboard in (
            ('no entry', open_group_board(line='')),
            ('another kind', open_group_board(line=unusable)),
        ):
            for f in (NET, finding('GO-2', 'golang.org/x/text', 'v0.20.0', 'v0.21.0', 'manifest')):
                with self.subTest(name, package=f['package']):
                    row, _ = plan(with_scan(f, **{OPEN: [group]}, **dashboard))
                    self.assertEqual((row['tick'], row['errors']), (None, []))
                    self.assertEqual(
                        row['blockers']['unrouted'],
                        [
                            (
                                f'{rm.finding_text(f)}: the Dependency Dashboard offers no rebase '
                                'checkbox for the open `main` group.'
                            )
                        ],
                    )

    def test_a_merged_group_that_has_not_shipped_is_in_flight(self):
        merged = pr(12, GROUP, created='2026-10-10T02:00:00Z', merged='2026-10-10T12:30:00Z')
        for name, extra in (('a dashboard', {}), ('no dashboard', {DASHBOARD: []})):
            with self.subTest(name):
                row, gh = plan(with_scan(NET, **{CLOSED: [merged]}, **extra))
                self.assertEqual((row['tick'], row['blockers']['unrouted']), (None, []))
                self.assertNotIn(DASHBOARD, gh.called())

    def test_no_group_entry_no_dashboard_and_an_unhandled_section_are_no_route(self):
        no_entry = TWO_BRANCH_BOARD.replace(self.AWAIT + '\n', '')
        errored = no_entry.replace(
            '## Open',
            f'## Errored\n\n - [ ] <!-- retry-branch={GROUP} -->fix(deps): update weekly dependencies (main)\n\n## Open',
        )
        pending = no_entry.replace(
            '## Open',
            f'## Pending Status Checks\n\n - [ ] <!-- unpend-branch={GROUP} -->fix(deps): update weekly dependencies (main)\n\n## Open',
        )
        cases = {
            'no entry': (
                board(no_entry),
                'the Dependency Dashboard lists no waiting `main` group to expedite',
            ),
            'no dashboard': (
                {DASHBOARD: []},
                'the repository has no Dependency Dashboard to expedite through',
            ),
            'errored': (
                board(errored),
                'the `main` group is listed under "Errored", which this job does not act on',
            ),
            'pending checks': (board(pending), None),
        }
        for name, (extra, reason) in cases.items():
            with self.subTest(name):
                row, _ = plan(with_scan(NET, **extra))
                self.assertIsNone(row['tick'])
                self.assertEqual(row['errors'], [])
                want = (
                    [
                        f'`GO-2026-1` in `golang.org/x/net` v0.30.0, fixed in v0.31.0 (go.mod): {reason}.'
                    ]
                    if reason
                    else []
                )
                self.assertEqual(row['blockers']['unrouted'], want)

    def test_a_branch_listed_twice_is_an_error_and_never_a_tick(self):
        twice = TWO_BRANCH_BOARD.replace('## Open', f'## Open\n\n{self.AWAIT}')
        row, _ = plan(with_scan(NET, **board(twice)))
        self.assertIsNone(row['tick'])
        self.assertIn(f'lists {GROUP} twice', row['errors'][0])

    def test_breaking_and_uncomparable_fixes_are_no_route(self):
        cases = {
            'major': (
                finding('G', 'github.com/a/b', 'v1.4.0', 'v2.0.0', 'manifest'),
                'the fix is a major update',
            ),
            'go minor': (
                finding('G', 'stdlib', 'v1.27.1', 'v1.28.0', 'stdlib'),
                'the fix needs a new Go minor release',
            ),
            'unknown': (
                finding('G', 'github.com/a/b', 'unknown', 'v1.2.0', 'manifest'),
                'cannot be compared',
            ),
        }
        for name, (f, reason) in cases.items():
            with self.subTest(name):
                row, gh = plan(with_scan(f))
                self.assertIsNone(row['tick'])
                self.assertIn(reason, row['blockers']['unrouted'][0])
                self.assertNotIn(DASHBOARD, gh.called())

    def test_a_0x_minor_and_a_fix_listed_beside_an_older_line_stay_on_the_line(self):
        self.assertEqual(rm.fix_level(finding('G', 'm', 'v0.30.0', 'v0.31.0', 'manifest')), 'ok')
        self.assertEqual(
            rm.fix_level(finding('G', 'stdlib', '1.27.1', '1.26.9, 1.27.3', 'stdlib')), 'ok'
        )
        self.assertEqual(
            rm.fix_level(finding('G', 'stdlib', '1.27.4', '1.27.3', 'stdlib')), 'unknown'
        )

    def test_lockfile_and_not_covered_findings_are_notes_only(self):
        lock = finding('CVE-9', 'lodash', '4.17.20', '4.17.21', 'lockfile')
        responses = world(
            **{
                f'GET {P}/actions/artifacts/77/zip': scan_zip(
                    record([lock], not_covered=['ffmpeg', 'libx264'])
                )
            }
        )
        row, gh = plan(responses)
        self.assertEqual((row['tick'], row['blockers']['unrouted']), (None, []))
        self.assertEqual(
            row['notes'],
            [
                '1 fixable finding(s) only in lockfile entries, which ship nothing: CVE-9',
                'not covered by the scan: ffmpeg, libx264',
            ],
        )
        self.assertNotIn(DASHBOARD, gh.called())


EXPEDITE_TICK = {'issue': 3, 'line': Expedite.AWAIT, 'kind': 'unschedule'}


class DashboardLookup(unittest.TestCase):
    def test_a_dashboard_authored_by_the_renovate_app_or_the_owner_is_found(self):
        for login in ('tribble-trouble[bot]', 'cplieger'):
            with self.subTest(login):
                row, _ = plan(with_scan(NET, **{DASHBOARD: [dashboard_issue(login=login)]}))
                self.assertEqual((row['errors'], row['tick']), ([], EXPEDITE_TICK))

    def test_a_dashboard_by_anyone_else_a_pull_request_or_another_title_is_ignored(self):
        decoys = [
            dashboard_issue(number=5, login='someone-else'),
            {**dashboard_issue(number=6), 'pull_request': {'url': 'x'}},
            dashboard_issue(number=7, title='Dependency Dashboard (old)'),
        ]
        for name, rows, tick in (
            ('alone', decoys, None),
            ('beside the real one', [*decoys, dashboard_issue()], EXPEDITE_TICK),
        ):
            with self.subTest(name):
                row, _ = plan(with_scan(NET, **{DASHBOARD: rows}))
                self.assertEqual((row['errors'], row['tick']), ([], tick))

    def test_two_renovate_dashboards_are_refused_and_neither_is_ticked(self):
        rows = [dashboard_issue(), dashboard_issue(number=9, login='tribble-trouble[bot]')]
        row, _ = plan(with_scan(NET, **{DASHBOARD: rows}))
        self.assertIsNone(row['tick'])
        self.assertEqual(
            row['errors'], ['scan of main: more than one open Dependency Dashboard: #3, #9']
        )
        self.assertEqual(row['unread'], ['unrouted'])


REFRESHING = (
    b'FROM alpine\nARG PKG_REFRESH=static\n'
    b'RUN echo "refresh ${PKG_REFRESH}" && apk add --no-cache tini\n'
)
APK = {DOCKERFILE: {'content': base64.b64encode(REFRESHING).decode()}}
OS = finding('CVE-1', 'libssl3', '3.0.1', '3.0.2', 'os')
OS2 = finding('CVE-2', 'busybox', '1.37.0-r1', '1.37.0-r2', 'os')


class Rebuild(unittest.TestCase):
    def test_os_findings_in_an_installing_image_plan_one_rebuild_naming_them(self):
        row, _ = plan(with_scan(OS, OS2, **APK))
        self.assertEqual(row['errors'], [])
        self.assertEqual(
            row['rebuild'],
            {
                'reason': f'The daily scan of `main` at {MAIN_HEAD[:12]} found fixable vulnerabilities '
                'in the OS packages of the published image: `CVE-1`, `CVE-2`.'
            },
        )
        self.assertIsNone(row['tick'])

    def test_an_open_or_unshipped_rebuild_is_in_flight(self):
        opened = {OPEN: [pr(30, 'rebuild/main-20261010', created='2026-10-10T11:00:00Z')]}
        merged = {CLOSED: [pr(30, 'rebuild/main-20261010', merged='2026-10-10T11:00:00Z')]}
        for name, extra in (('open', opened), ('merged', merged)):
            with self.subTest(name):
                row, _ = plan(with_scan(OS, **APK, **extra))
                self.assertEqual((row['rebuild'], row['blockers']['unrouted']), (None, []))

    def test_a_rebuild_that_shipped_before_the_scan_and_did_not_clear_it_is_no_route(self):
        merged = pr(
            30,
            'rebuild/main-20261009',
            created='2026-10-09T05:00:00Z',
            merged='2026-10-09T05:10:00Z',
        )
        extra = {
            CLOSED: [merged],
            SHIPPED: {
                'workflow_runs': [
                    {
                        'created_at': '2026-10-09T05:11:00Z',
                        'updated_at': '2026-10-09T05:40:00Z',
                        'head_sha': MAIN_HEAD,
                    }
                ]
            },
            f'GET {P}/compare/{30:040d}...{MAIN_HEAD}': {'status': 'identical'},
        }
        row, _ = plan(with_scan(OS, **APK, **extra))
        self.assertIsNone(row['rebuild'])
        self.assertIn(
            'a rebuild of `main` shipped before this scan', row['blockers']['unrouted'][0]
        )
        stale = dict(
            extra, **{CLOSED: [pr(30, 'rebuild/main-20261008', merged='2026-10-08T06:00:00Z')]}
        )
        stale[f'GET {P}/compare/{30:040d}...{MAIN_HEAD}'] = {'status': 'identical'}
        row, _ = plan(with_scan(OS, **APK, **stale))
        self.assertIsNotNone(row['rebuild'], 'a rebuild older than a day is retried')

    def test_only_the_final_stage_and_the_stages_it_is_built_from_decide_a_rebuild(self):
        cases = {
            'builder installs, distroless final': (
                (
                    'FROM golang:1.27 AS build\nRUN apt-get install -y git\n'
                    'FROM --platform=$TARGETPLATFORM gcr.io/distroless/static AS final\n'
                    'COPY --from=build /app /app\n'
                ),
                False,
            ),
            'final stage installs': (
                'FROM golang:1.27 AS build\nFROM alpine\n# apk add is below\nRUN apk upgrade\n',
                True,
            ),
            'final stage built from an installing stage': (
                'from alpine as base\nRUN apk add tini\nFROM golang AS build\nFROM base\n',
                True,
            ),
            'only a comment mentions an install': (
                'FROM alpine\n# RUN apk add tini\n',
                False,
            ),
            'a continued install': ('FROM alpine\nRUN apk \\\n  add --no-cache tini\n', True),
            'a continued install with a comment line inside': (
                'FROM alpine\nRUN set -eux; \\\n# pinned below\n    apt-get \\\n install -y curl\n',
                True,
            ),
            'an exec-form install': (
                'FROM alpine\nRUN ["apk", "add", "--no-cache", "tini"]\n',
                True,
            ),
            'flags before the subcommand': (
                'FROM alpine\nRUN --mount=type=cache,target=/var/cache/apk apk --no-cache add tini\n',
                True,
            ),
            'a heredoc install': ('FROM alpine\nRUN <<EOF\nset -eu\napk add tini\nEOF\n', True),
            'a builder-only install beside a left shift': (
                'FROM alpine AS b\nRUN apk add gcc && echo $((x<<n))\nFROM alpine\nCOPY --from=b /x /x\n',
                False,
            ),
            'an option with a separate value before the subcommand': (
                'FROM alpine\nRUN apt-get -o Dpkg::Options::=--force-confold install -y curl\n',
                True,
            ),
            'a repository option before apk add': (
                'FROM alpine\nRUN apk -X https://dl-cdn.alpinelinux.org/alpine/edge add tini\n',
                True,
            ),
            'an install word that is not the subcommand': (
                'FROM alpine\nRUN apt-get --no-install-recommends update && apk info add-on\n',
                False,
            ),
            'an install word as an argument of another subcommand': (
                'FROM alpine\nRUN apk info add >/dev/null || true\n',
                False,
            ),
            'an install word after an option and another subcommand': (
                'FROM alpine\nRUN apk --no-cache info add && apt-get download install\n',
                False,
            ),
            'an install word as an option value': (
                'FROM alpine\nRUN apt-get -o install update && apk -X add info\n',
                False,
            ),
            'a continued install in a builder stage only': (
                'FROM alpine AS build\nRUN apk \\\n  add gcc\nFROM gcr.io/distroless/static\n',
                False,
            ),
            'an install named outside a RUN': (
                'FROM alpine\nLABEL note="apk add is not run"\n',
                False,
            ),
            'an install echoed as data': (
                "FROM alpine\nRUN echo 'apk add curl' >/usr/local/share/example\n",
                False,
            ),
            'an install printed as data': (
                'FROM alpine\nRUN printf "%s\\n" "then apt-get install -y x" > /x\n',
                False,
            ),
            'a heredoc read as data': (
                'FROM alpine\nRUN cat <<EOF > /etc/notes\napk add tini\nEOF\n',
                False,
            ),
            'an install in a shell -c script': (
                'FROM alpine\nRUN ["/bin/sh", "-ec", "apk add tini"]\n',
                True,
            ),
            'an install behind a keyword, a wrapper and an assignment': (
                'FROM alpine\nRUN if true; then DEBIAN_FRONTEND=x sudo -E apt-get install -y x; fi\n',
                True,
            ),
            'a heredoc a shell runs': (
                "FROM alpine\nRUN bash <<'EOF'\napk add tini\nEOF\n",
                True,
            ),
            'wrappers whose options take a value': (
                (
                    'FROM alpine\nRUN sudo -u root env -u HOME -C / nice -n 5 '
                    'xargs -n 1 -I {} time -f %e apk add tini\n'
                ),
                True,
            ),
            'a long wrapper option with a separate value': (
                'FROM alpine\nRUN sudo --user root -- /sbin/apk add tini\n',
                True,
            ),
            'an install in an env split string': (
                "FROM alpine\nRUN env -S 'apk add tini'\n",
                True,
            ),
            'an install in an attached env split string': (
                "FROM alpine\nRUN env --split-string='apk add tini'\n",
                True,
            ),
            'an install command -v only looks up': (
                'FROM alpine\nRUN command -v apk add || true\n',
                False,
            ),
            'a wrapper value that names the package manager': (
                'FROM alpine\nRUN sudo -u apk add tini\n',
                False,
            ),
            'a sudo cluster whose last letter takes the user': (
                'FROM alpine\nRUN sudo -Eu root apk add curl\n',
                True,
            ),
            'a sudo cluster whose user names the package manager': (
                'FROM alpine\nRUN sudo -Eu apk add tini\n',
                False,
            ),
            'an attached value inside a sudo cluster': (
                'FROM alpine\nRUN sudo -Euroot apk add curl\n',
                True,
            ),
            'an apt-get cluster whose last letter takes an option': (
                'FROM alpine\nRUN apt-get -yo Dpkg::Options::=--force-confold install curl\n',
                True,
            ),
            'an apt-get cluster whose option value is the install word': (
                'FROM alpine\nRUN apt-get -yo install update\n',
                False,
            ),
            'an apk cluster whose last letter takes a repository': (
                'FROM alpine\nRUN apk -vX https://dl-cdn.alpinelinux.org/alpine/edge add tini\n',
                True,
            ),
            'an xargs cluster whose last letter takes a count': (
                'FROM alpine\nRUN printf "%s\\0" tini | xargs -0n 1 apk add\n',
                True,
            ),
            'an env cluster ending in the split string': (
                'FROM alpine\nRUN env -iS "apk add x"\n',
                True,
            ),
            'a command cluster that only looks up': (
                'FROM alpine\nRUN command -pv apk add || true\n',
                False,
            ),
        }
        bases = board(
            TWO_BRANCH_BOARD.replace('`golang.org/x/net`', '`gcr.io/distroless/static`, `alpine`')
        )
        for name, (text, installs) in cases.items():
            with self.subTest(name):
                self.assertEqual(rm.final_stage_installs(text), installs)
                body = {DOCKERFILE: {'content': base64.b64encode(text.encode()).decode()}}
                row, _ = plan(with_scan(OS, **body, **bases))
                self.assertEqual(row['errors'], [])
                self.assertIsNone(row['rebuild'], 'no case declares PKG_REFRESH')
                self.assertEqual(row['tick'] is not None, not installs)
                no_refresh = [u for u in row['blockers']['unrouted'] if 'PKG_REFRESH' in u]
                self.assertEqual(bool(no_refresh), installs, row['blockers']['unrouted'])

    def test_a_rebuild_is_planned_only_when_an_install_runs_after_a_pkg_refresh_arg(self):
        cases = {
            'the install reads it': (
                'FROM alpine\nARG PKG_REFRESH\nRUN echo $PKG_REFRESH && apk upgrade\n',
                True,
            ),
            'an earlier RUN reads it': (
                'FROM alpine\nARG PKG_REFRESH=x\nRUN : "${PKG_REFRESH}"\nRUN apk add tini\n',
                True,
            ),
            'a parent stage declares it and names it only in a comment': (
                (
                    'FROM alpine AS base\nARG PKG_REFRESH\nRUN apk add tini # ${PKG_REFRESH}\n'
                    'FROM base\nCOPY app /app\n'
                ),
                True,
            ),
            'a stage the final one is built from reads it and installs': (
                (
                    'FROM alpine AS base\nARG PKG_REFRESH\nRUN echo $PKG_REFRESH && apk add tini\n'
                    'FROM base\nCOPY app /app\n'
                ),
                True,
            ),
            'a parent stage reads it before the final stage installs': (
                (
                    'FROM alpine AS base\nARG PKG_REFRESH\nRUN echo $PKG_REFRESH\n'
                    'FROM base\nRUN apk add tini\n'
                ),
                True,
            ),
            'a shell -c script reads it': (
                (
                    'FROM alpine\nARG PKG_REFRESH\n'
                    'RUN ["sh", "-c", "echo ${PKG_REFRESH:-none}; apk add tini"]\n'
                ),
                True,
            ),
            'declared and never read': ('FROM alpine\nARG PKG_REFRESH\nRUN apk add tini\n', True),
            'declared with a default and never read': (
                'FROM alpine\nARG PKG_REFRESH=static\nRUN apk add tini\n',
                True,
            ),
            'a global ARG redeclared in the stage': (
                'ARG PKG_REFRESH=x\nFROM alpine\nARG PKG_REFRESH\nRUN apk add tini\n',
                True,
            ),
            'a parent stage declares it after its last RUN': (
                (
                    'FROM alpine AS base\nRUN echo base\nARG PKG_REFRESH\n'
                    'FROM base\nRUN apk add tini\n'
                ),
                True,
            ),
            'behind a wrapper that takes a value': (
                'FROM alpine\nARG PKG_REFRESH\nRUN sudo -u root apk add tini\n',
                True,
            ),
            'behind option clusters whose last letter takes a value': (
                (
                    'FROM alpine\nARG PKG_REFRESH\n'
                    'RUN sudo -Eu root apt-get -yo Dpkg::Options::=--force-confold install curl\n'
                ),
                True,
            ),
            'read before its stage declares it': (
                'FROM alpine\nRUN echo $PKG_REFRESH && apk add tini\nARG PKG_REFRESH\n',
                False,
            ),
            'a global ARG only': (
                'ARG PKG_REFRESH\nFROM alpine\nRUN echo $PKG_REFRESH && apk add tini\n',
                False,
            ),
            'declared before the install and read only after it': (
                'FROM alpine\nARG PKG_REFRESH\nRUN apk add tini\nRUN echo $PKG_REFRESH\n',
                True,
            ),
            'declared in a later stage than the install': (
                (
                    'FROM alpine AS base\nRUN apk add tini\n'
                    'FROM base\nARG PKG_REFRESH\nRUN echo $PKG_REFRESH\n'
                ),
                False,
            ),
            'declared in a stage the final one is not built from': (
                (
                    'FROM alpine AS other\nARG PKG_REFRESH\nRUN echo $PKG_REFRESH\n'
                    'FROM alpine\nRUN apk add tini\n'
                ),
                False,
            ),
            'declared on a line the release build does not pass it for': (
                'FROM alpine\nARG BUILD_VERSION PKG_REFRESH\nRUN apk add tini\n',
                False,
            ),
            'another arg with the name as a prefix': (
                'FROM alpine\nARG PKG_REFRESH_X\nRUN echo $PKG_REFRESH_X && apk add tini\n',
                False,
            ),
        }
        for name, (text, rebuild) in cases.items():
            with self.subTest(name):
                self.assertTrue(rm.final_stage_installs(text))
                self.assertEqual(rm.final_stage_refreshes(text), rebuild)
                body = {DOCKERFILE: {'content': base64.b64encode(text.encode()).decode()}}
                row, _ = plan(with_scan(OS, **body))
                self.assertEqual(row['errors'], [])
                self.assertEqual(row['rebuild'] is not None, rebuild)
                self.assertIsNone(row['tick'])
                if not rebuild:
                    self.assertIn(rm.UNROUTED['no-refresh'], row['blockers']['unrouted'][0])

    def test_every_rebuild_the_router_plans_gets_pkg_refresh_from_the_release_build(self):
        step = next(
            s
            for s in yaml.safe_load(DOCKER_RELEASE.read_text())['jobs']['build']['steps']
            if s.get('id') == 'buildargs'
        )
        texts = {
            REFRESHING.decode(): True,
            'FROM alpine\nARG PKG_REFRESH\nRUN : $PKG_REFRESH\nRUN apk add x\n': True,
            'FROM alpine\nARG PKG_REFRESH\nRUN apk add x\n': True,
            'FROM alpine\n  ARG\tPKG_REFRESH=x\r\nRUN apk add x\r\n': True,
            'FROM alpine\nARG BUILD_VERSION PKG_REFRESH\nRUN apk add x\n': False,
            'FROM alpine\narg PKG_REFRESH\nRUN apk add x\n': False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            for text, refreshes in texts.items():
                with self.subTest(text=text):
                    self.assertEqual(rm.final_stage_refreshes(text), refreshes)
                    dockerfile, out = Path(tmp) / 'Dockerfile', Path(tmp) / 'out'
                    dockerfile.write_text(text)
                    out.write_text('')
                    env = {
                        **os.environ,
                        'DOCKERFILE': str(dockerfile),
                        'TAG': 'v1.2.4',
                        'GITHUB_OUTPUT': str(out),
                    }
                    subprocess.run(
                        ['bash', '-e', '-c', step['run']], env=env, check=True, capture_output=True
                    )
                    today = dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')
                    self.assertEqual(f'PKG_REFRESH={today}\n' in out.read_text(), refreshes)

    def test_rebuild_refreshes_reads_the_stages_the_default_target_needs_on_stdin(self):
        for text, code in (
            (
                'FROM alpine AS b\nARG PKG_REFRESH\nRUN : $PKG_REFRESH; apk \\\n  add gcc\nFROM scratch\n',
                3,
            ),
            ('FROM alpine AS unused\nARG PKG_REFRESH\nRUN apk add curl\nFROM scratch\n', 3),
            (
                'FROM alpine AS b\nARG PKG_REFRESH\nRUN apk add gcc\nFROM scratch\nCOPY --from=b /x /x\n',
                0,
            ),
            (
                'FROM alpine AS B\nARG PKG_REFRESH\nRUN apk add gcc\nFROM scratch\nCOPY --from=b /x /x\n',
                0,
            ),
            (
                'FROM alpine\nARG PKG_REFRESH\nRUN apk add gcc\nFROM scratch\nCOPY --from=0 /x /x\n',
                0,
            ),
            (
                (
                    'FROM alpine AS b\nARG PKG_REFRESH\nRUN apk add gcc\n'
                    'FROM alpine\nRUN --mount=type=bind,from=b,target=/b cp /b/x /x\n'
                ),
                0,
            ),
            (
                (
                    'FROM alpine AS b\nARG PKG_REFRESH\nRUN apk add gcc\n'
                    'FROM scratch AS mid\nCOPY --from=b /x /x\nFROM scratch\nCOPY --from=mid /x /x\n'
                ),
                0,
            ),
            (
                'FROM alpine AS b\nARG PKG_REFRESH\nRUN apk add gcc\nFROM b\nFROM scratch\n',
                3,
            ),
            (
                'FROM alpine AS b\nARG PKG_REFRESH\nRUN apk add gcc\nFROM b AS c\nFROM c\n',
                0,
            ),
            (
                'FROM alpine AS b\nRUN apk add gcc\nFROM scratch\nCOPY --from=alpine /x /x\n',
                3,
            ),
            (
                (
                    'FROM alpine\nARG PKG_REFRESH\n'
                    'RUN ["sh", "-c", "echo $PKG_REFRESH && apt-get install -y curl"]\n'
                ),
                0,
            ),
            ('FROM alpine AS b\nRUN apk \\\n  add gcc\nFROM scratch\n', 3),
            ('FROM alpine AS b\nRUN apk add gcc\nFROM scratch\nCOPY --from=b /x /x\n', 4),
            ('FROM alpine\nRUN ["apt-get", "install", "-y", "curl"]\n', 4),
            ('FROM alpine\n# RUN apk add tini\nRUN echo apk\n', 3),
            ('FROM alpine\nRUN echo "apk add tini" > /x\n', 3),
            ('FROM alpine\nARG PKG_REFRESH\nRUN echo $PKG_REFRESH && apk info add\n', 3),
            ('FROM alpine\nARG PKG_REFRESH\nRUN sudo -u root apk add tini\n', 0),
            ('FROM alpine\nARG PKG_REFRESH\nRUN sudo -Eu root apk add curl\n', 0),
            ('FROM alpine\nARG PKG_REFRESH\nRUN sudo -Eu apk add tini\n', 3),
            (
                (
                    'FROM alpine:3.24\nARG PKG_REFRESH\n'
                    'RUN apt-get -yo Dpkg::Options::=--force-confold install curl\n'
                ),
                0,
            ),
            ('FROM alpine\nARG PKG_REFRESH\nRUN xargs -0n 1 apk add </list\n', 0),
            ('FROM alpine\nARG PKG_REFRESH\nRUN env -iS "apk add x"\n', 0),
            ('FROM alpine\nARG PKG_REFRESH\nRUN apk add tini\n', 0),
            ('FROM alpine\nRUN apk add tini\nARG PKG_REFRESH\n', 4),
            ('', 3),
        ):
            with (
                self.subTest(text=text),
                mock.patch('sys.stdin', io.StringIO(text)),
            ):
                self.assertEqual(rm.main(['rebuild-refreshes']), code)

    def test_a_dockerfile_that_does_not_decode_is_an_unread_scan_section(self):
        for name, content in (
            ('bad base64', '!!!!'),
            ('bad utf-8', base64.b64encode(b'FROM alpine\n\xff\n').decode()),
        ):
            with self.subTest(name):
                row, _ = plan(with_scan(OS, **{DOCKERFILE: {'content': content}}))
                self.assertEqual((row['rebuild'], row['tick']), (None, None))
                self.assertEqual(row['unread'], ['unrouted'])
                self.assertTrue(row['errors'][0].startswith('scan of main: '), row['errors'])


class Scan(unittest.TestCase):
    def test_the_newest_record_from_a_dispatched_scan_of_main_is_read(self):
        responses = world(
            **{
                ARTIFACTS: {
                    'artifacts': [
                        {
                            'id': 79,
                            'expired': True,
                            'workflow_run': {'id': 903, 'head_branch': 'main'},
                        },
                        {
                            'id': 78,
                            'expired': False,
                            'workflow_run': {'id': 901, 'head_branch': 'main'},
                        },
                        {
                            'id': 76,
                            'expired': False,
                            'workflow_run': {'id': 902, 'head_branch': 'dev'},
                        },
                        {
                            'id': 77,
                            'expired': False,
                            'workflow_run': {'id': 900, 'head_branch': 'main'},
                        },
                    ]
                },
                f'GET {P}/actions/runs/901': scan_run(
                    901, event='pull_request', head_repository={'full_name': 'someone/subflux'}
                ),
            }
        )
        row, gh = plan(responses)
        self.assertEqual((row['errors'], row['blockers']['unrouted']), ([], []))
        self.assertNotIn(
            f'GET {P}/actions/artifacts/78/zip', gh.called(), 'a pull-request run is never read'
        )
        self.assertIn(f'GET {P}/actions/artifacts/77/zip', gh.called())

    def test_a_record_from_another_workflow_or_repository_is_skipped(self):
        for over in (
            {'path': '.github/workflows/ci.yaml'},
            {'head_repository': {'full_name': 'x/subflux'}},
            {'event': 'pull_request'},
        ):
            with self.subTest(over):
                row, _ = plan(world(**{f'GET {P}/actions/runs/900': scan_run(**over)}))
                self.assertEqual(
                    row['blockers']['unrouted'][0][:30], 'No scan of `main` was found. R'
                )

    def test_a_missing_stale_or_incomplete_scan_is_a_blocker(self):
        row, _ = plan(world(**{ARTIFACTS: {'artifacts': []}}))
        self.assertEqual(
            row['blockers']['unrouted'],
            [
                f'No scan of `main` was found. Run `gh workflow run security.yml -R cplieger/{R} --ref main`.'
            ],
        )
        row, _ = plan(
            world(**{f'GET {P}/actions/runs/900': scan_run(started='2026-10-09T00:59:00Z')})
        )
        self.assertEqual(
            row['blockers']['unrouted'],
            ['The newest scan of `main` started at 2026-10-09 00:59 UTC, more than 36 hours ago.'],
        )
        row, _ = plan(
            world(**{f'GET {P}/actions/runs/900': scan_run(started='2026-10-09T01:00:00Z')})
        )
        self.assertEqual(row['blockers']['unrouted'], [])
        incomplete = scan_zip(record(complete=False, errors=['govulncheck of .: failed']))
        row, _ = plan(world(**{f'GET {P}/actions/artifacts/77/zip': incomplete}))
        self.assertEqual(
            row['blockers']['unrouted'],
            [f'The scan of `main` at {MAIN_HEAD[:12]} is incomplete: govulncheck of .: failed.'],
        )

    def test_an_incomplete_or_stale_record_plans_no_tick_and_no_rebuild(self):
        cases = {
            'incomplete': {
                f'GET {P}/actions/artifacts/77/zip': scan_zip(
                    record([NET, OS], complete=False, errors=['govulncheck of .: failed'])
                )
            },
            'stale': {
                f'GET {P}/actions/artifacts/77/zip': scan_zip(record([NET, OS])),
                f'GET {P}/actions/runs/900': scan_run(started='2026-10-09T00:59:00Z'),
            },
        }
        for name, over in cases.items():
            with self.subTest(name):
                row, gh = plan(world(**over, **APK))
                self.assertEqual((row['tick'], row['rebuild'], row['errors']), (None, None, []))
                self.assertEqual(len(row['blockers']['unrouted']), 1, row['blockers']['unrouted'])
                self.assertNotIn(DASHBOARD, gh.called())
        row, _ = plan(
            world(**{f'GET {P}/actions/artifacts/77/zip': scan_zip(record([NET, OS]))}, **APK)
        )
        self.assertIsNotNone(row['rebuild'], 'the same record, complete and fresh, is routed')
        self.assertIsNotNone(row['tick'])

    def test_a_record_that_does_not_describe_its_run_is_an_error_not_a_clean_scan(self):
        cases = {
            'another commit': scan_zip(record(commit='c' * 40)),
            'not a zip': b'not a zip',
            'no record': other_file_zip(),
            'wrong schema': scan_zip({**record(), 'schema': 2}),
        }
        for name, blob in cases.items():
            with self.subTest(name):
                row, _ = plan(world(**{f'GET {P}/actions/artifacts/77/zip': blob}))
                self.assertIn('scan of main: ', row['errors'][0])
                self.assertEqual(row['blockers']['unrouted'], [])


class FailClosed(unittest.TestCase):
    def test_a_failed_pull_request_read_plans_no_merge_and_no_blocker(self):
        merged = pr(12, GROUP, created='2026-10-10T02:00:00Z', merged='2026-10-10T03:00:00Z')
        row, gh = plan(world(**{OPEN: rm.GhError('HTTP 502'), CLOSED: [merged]}))
        self.assertEqual(row['merge'], [])
        self.assertEqual(row['errors'], ['security pull requests: HTTP 502'])
        self.assertEqual(row['blockers'], {'weekly': [], 'late': [], 'unrouted': []})
        self.assertEqual(row['unread'], ['weekly', 'late', 'unrouted'])
        self.assertEqual(gh.called(), [OPEN])

    def test_a_failed_release_read_skips_the_scan_routes(self):
        row, gh = plan(with_scan(NET, **{SHIPPED: rm.GhError('HTTP 500')}))
        self.assertEqual(row['errors'], ['pull requests into main: HTTP 500'])
        self.assertEqual(row['unread'], ['weekly', 'late', 'unrouted'])
        self.assertIsNone(row['tick'])
        self.assertNotIn(ARTIFACTS, gh.called())

    def test_a_failed_scan_read_leaves_only_its_section_unread(self):
        group = pr(12, GROUP, created='2026-10-10T02:00:00Z')
        row, _ = plan(world(**{OPEN: [group], ARTIFACTS: rm.GhError('HTTP 502')}))
        self.assertEqual(row['errors'], ['scan of main: HTTP 502'])
        self.assertEqual(row['unread'], ['unrouted'])
        self.assertEqual(len(row['blockers']['weekly']), 1)

    def test_a_clean_plan_reads_every_section(self):
        row, _ = plan(world())
        self.assertEqual((row['errors'], row['unread']), ([], []))

    def test_a_truncated_listing_is_an_error(self):
        many = [pr(i, 'renovate/dev-x', 'dev') for i in range(100)]
        responses = world()
        for page in range(1, 11):
            responses[f'GET {P}/pulls?state=open&per_page=100&page={page}'] = many
        row, _ = plan(responses)
        self.assertIn('10 pages did not reach the end', row['errors'][0])


class Dashboard(unittest.TestCase):
    def test_the_live_shape_parses_with_its_heading_and_kind(self):
        live = (TESTDATA / 'dashboard-docker-age.md').read_text()
        entries = rm.parse_dashboard(live)
        self.assertEqual(list(entries), ['renovate/golang-toolchain'])
        self.assertEqual(entries['renovate/golang-toolchain']['heading'], 'Pending Status Checks')
        self.assertEqual(entries['renovate/golang-toolchain']['kind'], 'unpend')
        self.assertIsNone(entries['renovate/golang-toolchain']['packages'])

    def test_package_lists_and_bulk_checkboxes(self):
        entries = rm.parse_dashboard(TWO_BRANCH_BOARD)
        self.assertEqual(
            sorted(entries),
            ['renovate/dev-golang.org-x-net-0.x', 'renovate/dev-lock-file-maintenance', GROUP],
        )
        self.assertEqual(
            entries[GROUP]['packages'], ['golang.org/x/net', 'github.com/cplieger/health']
        )
        self.assertEqual(entries[GROUP]['heading'], 'Awaiting Schedule')

    def test_a_tick_rewrites_exactly_its_line_and_refuses_a_changed_body(self):
        line = Expedite.AWAIT
        gh = FakeGh(
            {
                f'GET {P}/issues/3': {'body': TWO_BRANCH_BOARD.replace('\n', '\r\n')},
                f'PATCH {P}/issues/3': b'{}',
            }
        )
        self.assertEqual(
            rm.tick(rm.Api(gh), R, {'issue': 3, 'line': line, 'kind': 'unschedule'}),
            'ticked unschedule',
        )
        sent = json.loads(gh.calls[-1][1])['body']
        self.assertEqual(
            sent,
            TWO_BRANCH_BOARD.replace('\n', '\r\n').replace(
                ' - [ ] <!-- unschedule-branch=renovate/main',
                ' - [x] <!-- unschedule-branch=renovate/main',
            ),
        )
        changed = (
            TWO_BRANCH_BOARD.replace(line, line.replace('[ ]', '[x]')),
            TWO_BRANCH_BOARD + line + '\n',
        )
        for body in changed:
            gh = FakeGh({f'GET {P}/issues/3': {'body': body}})
            got = rm.tick(rm.Api(gh), R, {'issue': 3, 'line': line, 'kind': 'unschedule'})
            self.assertTrue(got.startswith('skipped'))
            self.assertEqual([k for k in gh.called() if k.startswith('PATCH')], [])


class Issue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.calls = []
        self.existing = ''

    def tearDown(self):
        self.tmp.cleanup()

    def tracker(self, argv):
        self.calls.append(argv)
        mode = argv[argv.index('--mode') + 1]
        if mode == 'fetch':
            Path(argv[argv.index('--body-file') + 1]).write_text(self.existing)
        if mode == 'upsert':
            self.written = Path(argv[argv.index('--body-file') + 1]).read_text()
        return 0

    def row(self, **blockers):
        return {
            'errors': [],
            'unread': [],
            'blockers': {'weekly': [], 'late': [], 'unrouted': [], **blockers},
        }

    def modes(self):
        return [c[c.index('--mode') + 1] for c in self.calls]

    def test_each_open_section_is_written_inside_the_sentinel_with_the_notes_kept(self):
        self.existing = '<!-- release-blocked -->\nold\n<!-- /release-blocked -->\n\n## Free-form notes\n\nwaiting on upstream\n'
        got = rm.sync_issue(
            R, self.row(weekly=['w1'], unrouted=['u1', 'u2']), 'RUN', self.tracker, self.work
        )
        self.assertEqual(got, 'opened or updated')
        self.assertEqual(self.modes(), ['fetch', 'upsert'])
        self.assertEqual(
            self.calls[0][:6],
            ['--repo', f'cplieger/{R}', '--label', 'release-blocked', '--title', 'Release blocked'],
        )
        self.assertIn('### The Saturday update did not ship by 12:00 UTC\n\n- w1', self.written)
        self.assertIn(
            '### A fixable vulnerability on `main` has no automatic route\n\n- u1\n- u2',
            self.written,
        )
        self.assertNotIn('three hours', self.written)
        self.assertTrue(self.written.endswith('## Free-form notes\n\nwaiting on upstream\n'))
        self.assertTrue(self.written.startswith('<!-- release-blocked -->\n'))

    def test_a_new_issue_gets_plain_default_notes_and_an_unchanged_body_is_not_rewritten(self):
        rm.sync_issue(R, self.row(late=['l1']), 'RUN', self.tracker, self.work)
        self.assertTrue(self.written.endswith(f'## Free-form notes\n\n{rm.NOTES_DEFAULT}\n'))
        self.existing, self.calls = self.written, []
        self.assertEqual(
            rm.sync_issue(R, self.row(late=['l1']), 'RUN', self.tracker, self.work), 'unchanged'
        )
        self.assertEqual(self.modes(), ['fetch'])

    def test_a_clear_repository_closes_and_a_failed_read_leaves_the_issue(self):
        self.assertEqual(
            rm.sync_issue(R, self.row(), 'RUN', self.tracker, self.work), 'closed if open'
        )
        self.assertEqual(self.modes(), ['close-when-clean'])
        self.calls = []
        failed = {**self.row(), 'errors': ['scan of main: x']}
        self.assertEqual(
            rm.sync_issue(R, failed, 'RUN', self.tracker, self.work),
            'left as is: a read or a planned action failed',
        )
        self.assertEqual(self.calls, [])

    def test_a_section_a_failed_read_left_unknown_keeps_its_earlier_text(self):
        self.existing = (
            '<!-- release-blocked -->\nintro\n\n'
            '### A fixable vulnerability on `main` has no automatic route\n\n- CVE-1 old\n- CVE-2 old\n'
            '<!-- /release-blocked -->\n\n## Free-form notes\n\nmine\n'
        )
        failed = {
            **self.row(weekly=['w1'], unrouted=['partial']),
            'errors': ['scan of main: HTTP 502'],
            'unread': ['unrouted'],
        }
        self.assertEqual(
            rm.sync_issue(R, failed, 'RUN', self.tracker, self.work), 'opened or updated'
        )
        self.assertIn('### The Saturday update did not ship by 12:00 UTC\n\n- w1', self.written)
        self.assertIn(
            '### A fixable vulnerability on `main` has no automatic route\n\n- CVE-1 old\n- CVE-2 old\n',
            self.written,
        )
        self.assertNotIn('partial', self.written)
        self.assertTrue(self.written.endswith('## Free-form notes\n\nmine\n'))
        self.existing, self.calls = self.written, []
        self.assertEqual(rm.sync_issue(R, failed, 'RUN', self.tracker, self.work), 'unchanged')

    def test_only_unknown_sections_with_items_leave_the_issue_unopened_and_unclosed(self):
        failed = {
            **self.row(unrouted=['partial']),
            'errors': ['scan of main: HTTP 502'],
            'unread': ['unrouted'],
        }
        self.assertEqual(
            rm.sync_issue(R, failed, 'RUN', self.tracker, self.work),
            'left as is: a read or a planned action failed',
        )
        self.assertEqual(self.calls, [])

    def test_the_writer_reaches_the_reserved_label_through_the_transport(self):
        seen = {}

        def fake_main(argv, *, allow_reserved=False):
            seen['argv'], seen['allow'] = argv, allow_reserved
            return 0

        real, tracker_issue.main = tracker_issue.main, fake_main
        try:
            self.assertEqual(rm.tracker_main(['--repo', 'x']), 0)
        finally:
            tracker_issue.main = real
        self.assertEqual(seen, {'argv': ['--repo', 'x'], 'allow': True})


class Report(unittest.TestCase):
    def run_report(self, plan_doc, merged, *, dry_run=False, responses=None, tracker=None):
        with tempfile.TemporaryDirectory() as tmp:
            Path(f'{tmp}/plan.json').write_text(json.dumps(plan_doc))
            if merged is not None:
                Path(f'{tmp}/merged.json').write_text(json.dumps(merged))
            summary = Path(f'{tmp}/summary.md')
            args = rm.argparse.Namespace(
                plan=f'{tmp}/plan.json',
                merged=f'{tmp}/merged.json',
                run_url='RUN',
                summary=str(summary),
                dry_run=dry_run,
            )
            calls = []
            gh = FakeGh(responses or {})
            code = rm.cmd_report(
                args, rm.Api(gh), tracker or (lambda argv: calls.append(argv) or 0)
            )
            return code, summary.read_text(), calls, gh

    def row(self, **over):
        base = {
            'repo': R,
            'errors': [],
            'merge': [],
            'rebuild': None,
            'tick': None,
            'notes': [],
            'blockers': {'weekly': [], 'late': [], 'unrouted': []},
            'unread': [],
        }
        base.update(over)
        return base

    def test_a_clean_run_exits_0_and_lists_the_repository(self):
        code, summary, calls, _ = self.run_report(
            {'repos': [self.row()]}, {R: {'merge': {}, 'rebuild': None}}
        )
        self.assertEqual(code, 0)
        self.assertIn(f'| {R} | - | - | - | closed if open |', summary)
        self.assertEqual(len(calls), 1)

    def test_a_failed_read_merge_or_missing_merge_record_exits_1(self):
        cases = {
            'read': (
                {'repos': [self.row(errors=['scan of main: x'])]},
                {R: {'merge': {}, 'rebuild': None}},
            ),
            'merge': (
                {'repos': [self.row(merge=[{'number': 1}])]},
                {R: {'merge': {'1': 'failed: no'}, 'rebuild': None}},
            ),
            'rebuild': (
                {'repos': [self.row(rebuild={'reason': 'x'})]},
                {R: {'merge': {}, 'rebuild': 'failed: 403'}},
            ),
            'no merge record': ({'repos': [self.row(merge=[{'number': 1}])]}, None),
        }
        for name, (plan_doc, merged) in cases.items():
            with self.subTest(name):
                code, _, _, _ = self.run_report(plan_doc, merged)
                self.assertEqual(code, 1)

    def test_a_dry_run_ticks_nothing_and_writes_no_issue(self):
        row = self.row(
            tick={'issue': 3, 'line': 'x', 'kind': 'unschedule'},
            blockers={'weekly': ['w'], 'late': [], 'unrouted': []},
        )
        code, summary, calls, gh = self.run_report(
            {'repos': [row]}, {R: {'merge': {}, 'rebuild': None}}, dry_run=True
        )
        self.assertEqual((code, calls, gh.calls), (0, [], []))
        self.assertIn('blocked: w', summary)

    def test_the_planned_tick_is_made(self):
        row = self.row(tick={'issue': 3, 'line': Expedite.AWAIT, 'kind': 'unschedule'})
        responses = {f'GET {P}/issues/3': {'body': TWO_BRANCH_BOARD}, f'PATCH {P}/issues/3': b'{}'}
        code, summary, _, gh = self.run_report(
            {'repos': [row]}, {R: {'merge': {}, 'rebuild': None}}, responses=responses
        )
        self.assertEqual(code, 0)
        self.assertIn(f'PATCH {P}/issues/3', gh.called())
        self.assertIn('ticked unschedule', summary)

    def test_a_planned_action_that_did_not_happen_never_clears_the_issue(self):
        line = Expedite.AWAIT
        changed = {f'GET {P}/issues/3': {'body': TWO_BRANCH_BOARD.replace(line, line + ' x')}}
        tick = {'issue': 3, 'line': line, 'kind': 'unschedule'}
        cases = {
            'the dashboard changed after the plan': (
                self.row(tick=tick),
                {'merge': {}, 'rebuild': None},
                changed,
            ),
            'a planned merge failed': (
                self.row(merge=[{'number': 1}]),
                {'merge': {'1': 'failed: no'}, 'rebuild': None},
                {},
            ),
            'a planned rebuild failed': (
                self.row(rebuild={'reason': 'x'}),
                {'merge': {}, 'rebuild': 'failed: 403'},
                {},
            ),
        }
        existing = (
            '<!-- release-blocked -->\nintro\n\n### The Saturday update did not ship by 12:00 UTC'
            '\n\n- old\n<!-- /release-blocked -->\n\n## Free-form notes\n\nmine\n'
        )
        for name, (row, done, responses) in cases.items():
            for blockers in ([], ['w1']):
                with self.subTest(name, blockers=blockers):
                    planned = {**row, 'blockers': {'weekly': blockers, 'late': [], 'unrouted': []}}
                    calls, written = [], {}

                    def tracker(argv, calls=calls, written=written):
                        mode = argv[argv.index('--mode') + 1]
                        calls.append(mode)
                        if mode == 'fetch':
                            Path(argv[argv.index('--body-file') + 1]).write_text(existing)
                        if mode == 'upsert':
                            written['body'] = Path(argv[argv.index('--body-file') + 1]).read_text()
                        return 0

                    code, summary, _, gh = self.run_report(
                        {'repos': [planned]}, {R: done}, responses=responses, tracker=tracker
                    )
                    self.assertEqual(code, 1)
                    self.assertNotIn('close-when-clean', calls)
                    self.assertNotIn(f'PATCH {P}/issues/3', gh.called())
                    if blockers:
                        self.assertEqual(calls, ['fetch', 'upsert'])
                        self.assertIn('- w1', written['body'])
                    else:
                        self.assertEqual(calls, [])
                        self.assertIn('left as is: a read or a planned action failed', summary)


class Constants(unittest.TestCase):
    def test_the_group_branch_is_renovates_multi_base_name_for_the_group(self):
        slug = re.sub(r'[^a-z0-9]+', '-', rc.MAIN_GROUP_NAME.lower()).strip('-')
        self.assertEqual(rc.MAIN_GROUP_BRANCH, f'renovate/main-{slug}')
        self.assertEqual(rc.main_intake_kind(rc.MAIN_GROUP_BRANCH), 'renovate')

    def test_the_allowlist_is_the_non_breaking_update_types(self):
        self.assertEqual(
            rc.SECURITY_AUTOMERGE_LABELS,
            {
                'security-patch',
                'security-minor',
                'security-digest',
                'security-pin',
                'security-pinDigest',
            },
        )

    def test_only_this_writer_names_the_release_blocked_label(self):
        allowed = {
            'release_maintenance.py',
            'tracker_issue.py',
            'test_release_maintenance.py',
            'test-tracker-issue.py',
        }
        hits = []
        for path in [
            *(ROOT / '.github' / 'workflows').glob('*.y*ml'),
            *SCRIPTS.glob('*.py'),
            *SCRIPTS.glob('*.sh'),
        ]:
            if 'release-blocked' in path.read_text() and path.name not in allowed:
                hits.append(path.name)
        self.assertEqual(hits, [])


class MergeChecked(unittest.TestCase):
    def test_a_pull_request_that_is_not_open_is_never_merged(self):
        gh = FakeGh({f'GET {P}/pulls/5': {'state': 'closed', 'head': {'sha': SHA}}})
        self.assertEqual(
            rm.merge_checked(rm.Api(gh), R, 5, run=gh, base='main'),
            'not merged: the pull request is closed',
        )
        self.assertEqual(gh.called(), [f'GET {P}/pulls/5'])

    def test_only_this_repositorys_named_head_into_the_base_is_merged(self):
        green = {f'GET {P}/commits/{SHA}/{CHECKS}': checks((15368, 'completed', 'success'))}
        merge = f'PUT {P}/pulls/5/merge'
        gone = {
            f'DELETE {P}/git/refs/heads/{h}': b''
            for h in ('repo-sync/ci/main', 'repo-sync/ci/dev', 'rebuild/main-1')
        }
        for base, other in (('main', 'dev'), ('dev', 'main')):
            head = f'repo-sync/ci/{base}'
            cases = {
                'a fork head': (pr(5, head, base, fork=True), False),
                'the other base': (pr(5, head, other), False),
                'another head': (pr(5, f'repo-sync/ci/{other}', base), False),
                'the head': (pr(5, head, base), True),
            }
            for name, (got, merged) in cases.items():
                with self.subTest(base=base, case=name):
                    gh = FakeGh({f'GET {P}/pulls/5': got, **green, merge: {}, **gone})
                    result = rm.merge_checked(rm.Api(gh), R, 5, run=gh, base=base, head=head)
                    self.assertEqual(result == 'merged', merged, result)
                    self.assertEqual(merge in gh.called(), merged)
        gh = FakeGh({f'GET {P}/pulls/5': pr(5, 'rebuild/main-1'), **green, merge: {}, **gone})
        self.assertEqual(
            rm.merge_checked(rm.Api(gh), R, 5, run=gh, base='main', head_prefix='rebuild/main-'),
            'merged',
        )
        gh = FakeGh({f'GET {P}/pulls/5': pr(5, 'renovate/main-1'), **green})
        self.assertIn(
            'not merged',
            rm.merge_checked(rm.Api(gh), R, 5, run=gh, base='main', head_prefix='rebuild/main-'),
        )

    def test_a_direct_merge_is_a_squash_pinned_to_the_head_read_then_its_head_is_deleted(self):
        green = {f'GET {P}/commits/{SHA}/{CHECKS}': checks((15368, 'completed', 'success'))}
        merge, delete = f'PUT {P}/pulls/5/merge', f'DELETE {P}/git/refs/heads/repo-sync/ci/main'
        for answer, warned in ((b'', False), (Status(422), False), (Status(403), True)):
            with (
                self.subTest(delete=answer),
                mock.patch('sys.stdout', new_callable=io.StringIO) as out,
            ):
                gh = FakeGh(
                    {
                        f'GET {P}/pulls/5': pr(5, 'repo-sync/ci/main'),
                        **green,
                        merge: {},
                        delete: answer,
                    }
                )
                result = rm.merge_checked(
                    rm.Api(gh), R, 5, run=gh, base='main', head='repo-sync/ci/main', arm=False
                )
                self.assertEqual(result, 'merged')
                self.assertEqual(gh.called()[-2:], [merge, delete])
                (body,) = [
                    json.loads(stdin) for args, stdin in gh.calls if request_key(args) == merge
                ]
                self.assertEqual(body, {'merge_method': 'squash', 'sha': SHA})
                self.assertEqual(
                    'its head branch was not deleted' in out.getvalue(), warned, out.getvalue()
                )
        gh = FakeGh({f'GET {P}/pulls/5': pr(5, 'repo-sync/ci/main'), **green, merge: Status(409)})
        with self.assertRaises(rm.GhError):
            rm.merge_checked(
                rm.Api(gh), R, 5, run=gh, base='main', head='repo-sync/ci/main', arm=False
            )
        self.assertNotIn(delete, gh.called(), 'a refused merge deletes nothing')

    def test_auto_merge_is_armed_only_after_the_read_and_pinned_to_the_head_it_read(self):
        auto = (
            f'pr merge 5 -R cplieger/{R} --squash --delete-branch --auto --match-head-commit {SHA}'
        )
        gh = FakeGh({f'GET {P}/pulls/5': pr(5, 'repo-sync/ci/main'), auto: b''})
        with (
            mock.patch.object(rm, 'Api', return_value=rm.Api(gh)),
            mock.patch('sys.stdout', new_callable=io.StringIO) as out,
        ):
            argv = ['merge-checked', R, '5', '--base', 'main', '--head', 'repo-sync/ci/main']
            self.assertEqual(rm.main(argv), 0)
        self.assertEqual(out.getvalue(), f'cplieger/{R}#5: armed\n')
        self.assertEqual(gh.called(), [f'GET {P}/pulls/5', auto])
        gh = FakeGh({f'GET {P}/pulls/5': pr(5, 'repo-sync/ci/main', 'dev'), auto: b''})
        result = rm.merge_checked(rm.Api(gh), R, 5, run=gh, base='main', head='repo-sync/ci/main')
        self.assertTrue(result.startswith('not merged'), result)
        self.assertEqual(gh.called(), [f'GET {P}/pulls/5'], 'a retargeted head is never armed')

    def test_an_arm_refused_by_a_rate_limit_is_left_open_without_a_direct_merge(self):
        green = {f'GET {P}/commits/{SHA}/{CHECKS}': checks((15368, 'completed', 'success'))}
        gh = FakeGh({f'GET {P}/pulls/5': pr(5, 'repo-sync/ci/main'), **green})
        limited = subprocess.CompletedProcess([], 1, b'', b'GraphQL: API rate limit exceeded')

        def run(args, stdin=None, timeout=None):
            return limited if args[0] == 'pr' else gh(args, stdin, timeout)

        api = rm.Api(run)
        result = rm.merge_checked(api, R, 5, run=api.command, base='main', head='repo-sync/ci/main')
        self.assertTrue(result.startswith('not merged: auto-merge was refused by a rate limit'))
        self.assertEqual(
            gh.called(), [f'GET {P}/pulls/5'] * 2, 'a re-read, no check read, no direct merge'
        )

    def test_an_arm_refused_by_a_rate_limit_after_its_merge_reports_it_merged(self):
        reads = [pr(5, 'repo-sync/ci/main'), pr(5, 'repo-sync/ci/main', merged='2026-10-10T03:00Z')]
        gh = FakeGh({f'GET {P}/pulls/5': lambda _args, _stdin: reads.pop(0)})
        limited = subprocess.CompletedProcess([], 1, b'', b'GraphQL: API rate limit exceeded')

        def run(args, stdin=None, timeout=None):
            return limited if args[0] == 'pr' else gh(args, stdin, timeout)

        api = rm.Api(run)
        result = rm.merge_checked(api, R, 5, run=api.command, base='main', head='repo-sync/ci/main')
        self.assertEqual(result, 'merged')
        self.assertEqual(gh.called(), [f'GET {P}/pulls/5'] * 2)

    def test_a_command_failure_is_a_gh_error_and_a_rate_limit_its_own(self):
        cases = (
            ('rate limited', subprocess.CompletedProcess([], 1, b'', b'API rate limit exceeded')),
            ('pacer refused', rm.ghrest.ApiError(None, 'held', rate_limited=True)),
            ('refused', subprocess.CompletedProcess([], 1, b'', b'not mergeable')),
            ('gh did not start', FileNotFoundError('gh')),
        )
        for name, answer in cases:
            with self.subTest(name):

                def run(_args, _stdin=None, _timeout=None, answer=answer):
                    if isinstance(answer, BaseException):
                        raise answer
                    return answer

                with self.assertRaises(rm.GhError) as caught:
                    rm.Api(run).command(['pr', 'merge', '5'])
                self.assertEqual(
                    isinstance(caught.exception, rm.RateLimitedError),
                    name.startswith(('rate', 'pacer')),
                )

    def test_a_base_other_than_dev_or_main_is_refused_before_any_read(self):
        gh = FakeGh({})
        with self.assertRaises(ValueError):
            rm.merge_checked(rm.Api(gh), R, 5, run=gh, base='release', head='x')
        self.assertEqual(gh.called(), [])

    def test_a_read_failure_is_a_failed_merge(self):
        with (
            mock.patch.object(rm, 'Api', return_value=rm.Api(FakeGh({}))),
            mock.patch('sys.stdout', new_callable=io.StringIO) as out,
        ):
            self.assertEqual(rm.main(['merge-checked', R, '5', '--base', 'dev', '--head', 'x']), 1)
        self.assertIn(f'cplieger/{R}#5: failed: unexpected call GET {P}/pulls/5', out.getvalue())

    def test_a_repository_name_outside_the_charset_or_no_base_is_refused(self):
        for argv in (
            ['merge-checked', 'a/b', '5', '--base', 'main', '--head', 'x'],
            ['merge-checked', R, '5', '--base', 'main'],
            ['merge-checked', R, '5', '--head', 'x'],
            ['merge-checked', R, '5', '--base', 'release', '--head', 'x'],
        ):
            with (
                self.subTest(argv=argv),
                mock.patch('sys.stderr', new_callable=io.StringIO),
                self.assertRaises(SystemExit),
            ):
                rm.main(argv)


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = yaml.safe_load(WORKFLOW.read_text())
        cls.steps = {s['name']: s for s in cls.wf['jobs']['maintain']['steps']}

    def test_hourly_off_the_hour_with_one_run_at_a_time(self):
        self.assertEqual(self.wf[True]['schedule'], [{'cron': '23 * * * *'}])
        self.assertEqual(
            self.wf['concurrency'], {'group': 'release-maintenance', 'cancel-in-progress': False}
        )
        self.assertEqual(self.wf['permissions'], {'contents': 'read'})

    def test_each_secret_is_scoped_to_the_one_step_that_needs_it(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('SYNC_PAT', text)
        self.assertEqual(text.count('secrets.SYNC_APP_PRIVATE_KEY'), 1)
        mint = self.steps['Mint the App token']
        self.assertEqual(mint['id'], 'app-token')
        self.assertRegex(mint['uses'], r'^actions/create-github-app-token@[0-9a-f]{40}$')
        self.assertNotIn('continue-on-error', mint)
        self.assertNotIn('if', mint)
        self.assertEqual(
            mint['with'],
            {
                'client-id': '${{ secrets.SYNC_APP_ID }}',
                'private-key': '${{ secrets.SYNC_APP_PRIVATE_KEY }}',
                'owner': 'cplieger',
                'permission-checks': 'read',
                'permission-contents': 'write',
                'permission-metadata': 'read',
                'permission-pull-requests': 'write',
                'permission-workflows': 'write',
            },
        )
        self.assertEqual(
            self.steps['Merge security pull requests and open rebuilds']['env']['GH_TOKEN'],
            '${{ steps.app-token.outputs.token }}',
        )
        names = list(self.steps)
        self.assertEqual(
            names.index('Mint the App token') + 1,
            names.index('Merge security pull requests and open rebuilds'),
        )
        self.assertEqual(self.steps['Plan']['env']['GH_TOKEN'], '${{ secrets.CI_SCHEDULE }}')
        self.assertEqual(self.steps['Report']['env']['GH_TOKEN'], '${{ secrets.CI_SCHEDULE }}')
        self.assertNotIn('env', self.wf['jobs']['maintain'])
        self.assertEqual(self.steps['Checkout']['with'], {'persist-credentials': False})

    def test_report_runs_after_a_failed_merge_but_not_without_a_plan(self):
        cond = self.steps['Report']['if']
        for plan_outcome, failed, want in (
            ('success', True, True),
            ('success', False, True),
            ('failure', True, False),
        ):
            scope = workflow_replay.Scope(
                {'steps': {'plan': {'outcome': plan_outcome}}}, {'failed': failed}, []
            )
            self.assertEqual(scope.condition(cond), want)

    def test_notify_tracks_the_run_with_the_dispatch_scope(self):
        notify = self.wf['jobs']['notify']
        self.assertEqual(notify['needs'], [j for j in self.wf['jobs'] if j != 'notify'])
        self.assertEqual(notify['uses'], './.github/workflows/notify-failure.yaml')
        self.assertEqual(notify['with']['scope'], '${{ inputs.only }}')
        self.assertEqual(notify['if'], 'always()')

    def run_body(self, name, env):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp, 'bin')
            bin_dir.mkdir()
            log = Path(tmp, 'argv.json')
            stub = bin_dir / 'python3'
            stub.write_text(
                f'#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, "w").write(json.dumps(sys.argv[1:]))\n'
            )
            stub.chmod(0o755)
            full = {
                'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                'HOME': tmp,
                'RUNNER_TEMP': '/rt',
                'GITHUB_STEP_SUMMARY': '/sum',
                **env,
            }
            subprocess.run(['bash', '-e', '-c', self.steps[name]['run']], env=full, check=True)
            return json.loads(log.read_text())

    def test_the_step_bodies_pass_the_subcommands_their_arguments(self):
        self.assertEqual(
            self.run_body('Plan', {'ONLY': 'a,b'}),
            ['scripts/release_maintenance.py', 'plan', '--out', '/rt/plan.json', '--only', 'a,b'],
        )
        merge = [
            'scripts/release_maintenance.py',
            'merge',
            '--plan',
            '/rt/plan.json',
            '--out',
            '/rt/merged.json',
        ]
        self.assertEqual(
            self.run_body('Merge security pull requests and open rebuilds', {'DRY_RUN': ''}), merge
        )
        self.assertEqual(
            self.run_body('Merge security pull requests and open rebuilds', {'DRY_RUN': 'true'}),
            [*merge, '--dry-run'],
        )
        report = [
            'scripts/release_maintenance.py',
            'report',
            '--plan',
            '/rt/plan.json',
            '--merged',
            '/rt/merged.json',
            '--run-url',
            'U',
            '--summary',
            '/sum',
        ]
        self.assertEqual(self.run_body('Report', {'DRY_RUN': 'false', 'RUN_URL': 'U'}), report)
        self.assertEqual(
            self.run_body('Report', {'DRY_RUN': 'true', 'RUN_URL': 'U'}), [*report, '--dry-run']
        )


class ApiTransport(unittest.TestCase):
    """Api reads through scripts/ghrest.py and reports every failure as a GhError."""

    @staticmethod
    def api(*outs: tuple[int, bytes, bytes]) -> tuple[rm.Api, list]:
        queue, seen = list(outs), []

        def run(args, stdin=None, timeout=None):
            seen.append(list(args))
            rc, out, err = queue.pop(0)
            return subprocess.CompletedProcess(args, rc, out, err)

        api = rm.Api(run)
        api.rest.sleep = lambda _s: None
        return api, seen

    def test_a_404_is_none_and_any_other_failure_a_gh_error(self):
        api, seen = self.api((1, b'HTTP/2.0 404 Not Found\n\r\n{"message": "Not Found"}', b''))
        self.assertIsNone(api.get_or_none(f'{P}/contents/Dockerfile?ref=main'))
        self.assertEqual(seen, [['api', '-i', f'{P}/contents/Dockerfile?ref=main']])
        api, _ = self.api(*((1, b'HTTP/2.0 502 Bad Gateway\n\r\n{}', b''),) * 4)
        with self.assertRaises(rm.GhError) as caught:
            api.get(f'{P}/pulls/1')
        self.assertIn('HTTP 502', str(caught.exception))

    def test_a_command_with_a_non_zero_exit_is_a_gh_error(self):
        api, seen = self.api((1, b'', b'Pull request is not mergeable'))
        with self.assertRaises(rm.GhError) as caught:
            api.command(['pr', 'merge', '5', '-R', f'{rm.OWNER}/{R}', '--squash'])
        self.assertIn('Pull request is not mergeable', str(caught.exception))
        self.assertEqual(seen[0][:2], ['pr', 'merge'])


if __name__ == '__main__':
    unittest.main()
