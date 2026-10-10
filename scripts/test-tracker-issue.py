#!/usr/bin/env python3
"""Pin the contract of scripts/tracker_issue.py against a stub `gh`.

The stub sits first on PATH, answers `gh api -i` requests from a JSON scenario
(issue setting, open issues, which requests fail and how) and appends every
request's method, path and body to a log, so each case asserts both what the
transport wrote and what it did not.

Run: python3 scripts/test-tracker-issue.py     (exit 0 = pass)
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACKER = HERE / 'tracker_issue.py'
NOTIFY_FAILURE = HERE.parent / '.github' / 'workflows' / 'notify-failure.yaml'

# Each request is logged as {op, method, path, body}; `op` names the route
# (`GET issues`, `PATCH issue`, ...) so a case reads like the API it drives.
STUB_GH = r"""#!/usr/bin/env python3
import json, os, re, sys
from urllib.parse import parse_qs, urlsplit
scenario = json.load(open(os.environ['GH_STUB_SCENARIO']))
args = sys.argv[1:]
if args[:2] != ['api', '-i']:
    sys.stderr.write(f'stub gh: not a REST request {args}\n')
    sys.exit(2)
method, path, i = 'GET', None, 2
while i < len(args):
    if args[i] in ('-X', '-H', '--input'):
        if args[i] == '-X':
            method = args[i + 1]
        i += 2
    else:
        path, i = args[i], i + 1
body = json.load(sys.stdin) if '--input' in args else None
url = urlsplit(path)
query = {k: v[0] for k, v in parse_qs(url.query).items()}
routes = (
    ('GET', r'repos/[^/]+/[^/]+', 'GET repo'),
    ('GET', r'repos/[^/]+/[^/]+/issues', 'GET issues'),
    ('POST', r'repos/[^/]+/[^/]+/issues', 'POST issues'),
    ('POST', r'repos/[^/]+/[^/]+/labels', 'POST labels'),
    ('PATCH', r'repos/[^/]+/[^/]+/issues/\d+', 'PATCH issue'),
    ('POST', r'repos/[^/]+/[^/]+/issues/\d+/comments', 'POST comment'),
    ('POST', r'repos/[^/]+/[^/]+/issues/\d+/labels', 'POST issue-labels'),
    ('DELETE', r'repos/[^/]+/[^/]+/issues/\d+/labels/[^/]+', 'DELETE issue-label'),
)
op = next((name for m, rx, name in routes if m == method and re.fullmatch(rx, url.path)), None)
with open(os.environ['GH_STUB_LOG'], 'a') as log:
    log.write(json.dumps({'op': op, 'method': method, 'path': path, 'body': body}) + '\n')
def answer(status, doc):
    sys.stdout.write(f'HTTP/2.0 {status} Reason\nContent-Type: application/json\r\n\r\n')
    sys.stdout.write(json.dumps(doc))
    sys.exit(0 if status < 300 else 1)
if op is None:
    sys.stderr.write(f'stub gh: unhandled {method} {path}\n')
    sys.exit(2)
if op in scenario.get('fail', {}):
    status, message = scenario['fail'][op]
    answer(status, {'message': message})
if op == 'GET repo':
    answer(200, {'has_issues': scenario.get('issues_enabled', True)})
if op == 'GET issues':
    issues = scenario.get('open_issues', [])
    if 'labels' in query:
        issues = [i for i in issues if query['labels'] in [l['name'] for l in i['labels']]]
    per_page, page = int(query.get('per_page', 30)), int(query.get('page', 1))
    answer(200, issues[(page - 1) * per_page : page * per_page])
if op == 'POST issues':
    answer(201, {'number': scenario.get('next_number', 101)})
if op == 'POST labels' and body['name'] in scenario.get('existing_labels', []):
    answer(422, {'message': 'Validation Failed', 'errors': [{'code': 'already_exists'}]})
answer(204 if method == 'DELETE' else 200, {})
"""

TRACKER_ISSUE = {
    'number': 42,
    'title': 'Gremlins mutation testing tracker',
    'labels': [{'name': 'gremlins-tracker'}, {'name': 'auto-generated'}],
    'body': '# Gremlins mutation testing tracker\n\nold body\n',
}
FLAGGED_ISSUE = {
    **TRACKER_ISSUE,
    'labels': [*TRACKER_ISSUE['labels'], {'name': 'mutation-regression'}],
}
FUZZ_ISSUE = {
    'number': 7,
    'title': '[fuzz] FuzzParse regression — abc123',
    'labels': [{'name': 'fuzz-finding'}, {'name': 'auto-generated'}],
    'body': 'finding',
}
WRITES = ('POST issues', 'PATCH issue', 'POST comment', 'POST issue-labels', 'DELETE issue-label')
LISTING = 'repos/cplieger/x/issues?state=open&labels=gremlins-tracker&per_page=100&page=1'


class TrackerIssueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix='tracker-issue-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = self.tmp / 'bin' / 'gh'
        stub.parent.mkdir()
        stub.write_text(STUB_GH)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.log = self.tmp / 'gh.log'
        self.scenario = self.tmp / 'scenario.json'
        self.body = self.tmp / 'body.md'
        self.body.write_text('# new body\n')
        self.comment = self.tmp / 'comment.md'
        self.comment.write_text('Still failing.\n')

    def stub_env(self) -> dict:
        return {
            **os.environ,
            'PATH': f'{self.tmp / "bin"}{os.pathsep}{os.environ["PATH"]}',
            'GH_STUB_SCENARIO': str(self.scenario),
            'GH_STUB_LOG': str(self.log),
        }

    def run_tracker(self, scenario: dict, *args: str) -> subprocess.CompletedProcess:
        self.scenario.write_text(json.dumps(scenario))
        self.log.write_text('')
        return subprocess.run(
            [sys.executable, str(TRACKER), '--repo', 'cplieger/x', *args],
            capture_output=True,
            text=True,
            env=self.stub_env(),
            check=False,
        )

    def run_in_process(self, scenario: dict, *args: str) -> tuple[int, str, list]:
        """(exit code, stderr, sleeps) of main() in this process, so the retry
        policy's back-off is recorded instead of slept."""
        self.scenario.write_text(json.dumps(scenario))
        self.log.write_text('')
        sys.path.insert(0, str(HERE))
        self.addCleanup(sys.path.remove, str(HERE))
        import tracker_issue

        sleeps, err = [], io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, self.stub_env()),
            unittest.mock.patch.object(tracker_issue.ghrest.DEFAULT, 'sleep', sleeps.append),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = tracker_issue.main(['--repo', 'cplieger/x', *args])
        return code, err.getvalue(), sleeps

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def commands(self) -> list[str]:
        return [c['op'] for c in self.calls()]

    def writes(self) -> list[str]:
        """Issue writes only: label creation is idempotent and asserted separately."""
        return [c for c in self.commands() if c in WRITES]

    def last(self, op: str) -> dict:
        return [c for c in self.calls() if c['op'] == op][-1]

    def tracker_args(self, *extra: str) -> tuple[str, ...]:
        return ('--label', 'gremlins-tracker', '--title', TRACKER_ISSUE['title'], *extra)

    # --- upsert -----------------------------------------------------------

    def test_upsert_creates_with_labels_when_no_issue_is_open(self) -> None:
        proc = self.run_tracker(
            {}, *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body))
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.commands(),
            ['GET repo', 'GET issues', 'POST labels', 'POST labels', 'POST issues'],
        )
        self.assertEqual(self.calls()[0]['path'], 'repos/cplieger/x')
        self.assertEqual(self.calls()[1]['path'], LISTING)
        self.assertEqual(
            self.last('POST issues')['body'],
            {
                'title': TRACKER_ISSUE['title'],
                'body': '# new body\n',
                'labels': ['gremlins-tracker', 'auto-generated'],
            },
        )
        self.assertIn('created #101', proc.stdout)

    def test_upsert_edits_the_open_issue_found_by_label_and_title(self) -> None:
        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['PATCH issue'])
        edit = self.last('PATCH issue')
        self.assertEqual(edit['path'], 'repos/cplieger/x/issues/42')
        self.assertEqual(edit['body'], {'body': '# new body\n'})

    def test_upsert_ignores_a_same_label_issue_with_another_title(self) -> None:
        other = {**TRACKER_ISSUE, 'title': 'Something else entirely'}
        proc = self.run_tracker(
            {'open_issues': [other]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['POST issues'])

    def test_a_pull_request_with_the_title_is_not_the_issue(self) -> None:
        pull = {**TRACKER_ISSUE, 'number': 5, 'pull_request': {'url': 'x'}}
        proc = self.run_tracker(
            {'open_issues': [pull]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['POST issues'])

    def test_more_than_one_page_of_open_issues_is_read(self) -> None:
        filler = [{**TRACKER_ISSUE, 'number': 1000 + n, 'title': f'other {n}'} for n in range(120)]
        proc = self.run_tracker(
            {'open_issues': [*filler, TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listings = [c['path'] for c in self.calls() if c['op'] == 'GET issues']
        self.assertEqual(listings, [LISTING, LISTING.removesuffix('page=1') + 'page=2'])
        self.assertEqual(self.writes(), ['PATCH issue'])
        self.assertEqual(self.last('PATCH issue')['path'], 'repos/cplieger/x/issues/42')

    def test_upsert_flag_on_adds_the_label_once(self) -> None:
        flag = ('--flag-label', 'mutation-regression', '--flag', 'on')
        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body), *flag),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['PATCH issue', 'POST issue-labels'])
        add = self.last('POST issue-labels')
        self.assertEqual(add['path'], 'repos/cplieger/x/issues/42/labels')
        self.assertEqual(add['body'], {'labels': ['mutation-regression']})
        self.assertEqual(
            self.last('POST labels'),
            {
                'op': 'POST labels',
                'method': 'POST',
                'path': 'repos/cplieger/x/labels',
                'body': {
                    'name': 'mutation-regression',
                    'color': 'b60205',
                    'description': 'Mutation efficacy regression',
                },
            },
        )

        proc = self.run_tracker(
            {'open_issues': [FLAGGED_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body), *flag),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['PATCH issue'], 'a present flag is not re-added')

    def test_upsert_flag_off_removes_only_a_present_label(self) -> None:
        flag = ('--flag-label', 'mutation-regression', '--flag', 'off')
        proc = self.run_tracker(
            {'open_issues': [FLAGGED_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body), *flag),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['PATCH issue', 'DELETE issue-label'])
        self.assertEqual(
            self.last('DELETE issue-label')['path'],
            'repos/cplieger/x/issues/42/labels/mutation-regression',
        )

        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body), *flag),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['PATCH issue'], 'an absent flag is not removed')

    def test_upsert_flag_on_at_creation_rides_the_create_call(self) -> None:
        proc = self.run_tracker(
            {},
            *self.tracker_args(
                '--mode',
                'upsert',
                '--body-file',
                str(self.body),
                '--flag-label',
                'mutation-regression',
                '--flag',
                'on',
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['POST issues'])
        self.assertEqual(
            self.last('POST issues')['body']['labels'],
            ['gremlins-tracker', 'auto-generated', 'mutation-regression'],
        )

    def test_extra_labels_are_added_on_create(self) -> None:
        proc = self.run_tracker(
            {},
            *self.tracker_args(
                '--mode', 'upsert', '--body-file', str(self.body), '--extra-label', 'needs-triage'
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.last('POST issues')['body']['labels'],
            ['gremlins-tracker', 'auto-generated', 'needs-triage'],
        )

    # --- recur ------------------------------------------------------------

    def test_recur_comments_on_the_issue_found_by_title(self) -> None:
        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE, FUZZ_ISSUE]},
            '--label',
            'fuzz-finding',
            '--title',
            FUZZ_ISSUE['title'],
            '--mode',
            'recur',
            '--body-file',
            str(self.body),
            '--comment-file',
            str(self.comment),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['POST comment'])
        listing = self.last('GET issues')['path']
        self.assertEqual(
            listing,
            'repos/cplieger/x/issues?state=open&per_page=100&page=1',
            'recur matches by title among every open issue, not by label or search',
        )
        comment = self.last('POST comment')
        self.assertEqual(comment['path'], 'repos/cplieger/x/issues/7/comments')
        self.assertEqual(comment['body'], {'body': 'Still failing.\n'})

    def test_recur_creates_when_the_title_is_new(self) -> None:
        proc = self.run_tracker(
            {'open_issues': [FUZZ_ISSUE]},
            '--label',
            'fuzz-finding',
            '--title',
            '[fuzz] FuzzOther regression — def456',
            '--mode',
            'recur',
            '--body-file',
            str(self.body),
            '--comment-file',
            str(self.comment),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['POST issues'])
        self.assertEqual(
            self.last('POST issues')['body']['labels'], ['fuzz-finding', 'auto-generated']
        )

    # --- close-when-clean -------------------------------------------------

    def test_close_when_clean_closes_with_the_comment(self) -> None:
        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'close-when-clean', '--comment-file', str(self.comment)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['POST comment', 'PATCH issue'])
        self.assertEqual(self.last('POST comment')['path'], 'repos/cplieger/x/issues/42/comments')
        self.assertEqual(self.last('POST comment')['body'], {'body': 'Still failing.\n'})
        close = self.last('PATCH issue')
        self.assertEqual(close['path'], 'repos/cplieger/x/issues/42')
        self.assertEqual(close['body'], {'state': 'closed', 'state_reason': 'completed'})

    def test_a_failed_closing_comment_leaves_the_issue_open(self) -> None:
        proc = self.run_tracker(
            {'fail': {'POST comment': [422, 'Unprocessable']}, 'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'close-when-clean', '--comment-file', str(self.comment)),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn('PATCH issue', self.commands())

    def test_close_when_clean_without_an_issue_writes_nothing(self) -> None:
        proc = self.run_tracker(
            {},
            *self.tracker_args('--mode', 'close-when-clean', '--comment-file', str(self.comment)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), [])

    # --- fetch and list ---------------------------------------------------

    def test_fetch_writes_the_open_body_or_an_empty_file(self) -> None:
        out = self.tmp / 'existing.md'
        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'fetch', '--body-file', str(out)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out.read_text(), TRACKER_ISSUE['body'])
        self.assertEqual(self.writes(), [])

        proc = self.run_tracker({}, *self.tracker_args('--mode', 'fetch', '--body-file', str(out)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out.read_text(), '')

    def test_list_prints_number_and_title_for_the_label_only(self) -> None:
        proc = self.run_tracker(
            {'open_issues': [FUZZ_ISSUE, TRACKER_ISSUE]},
            '--label',
            'fuzz-finding',
            '--mode',
            'list',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f'7\t{FUZZ_ISSUE["title"]}\n')
        self.assertEqual(self.writes(), [])

    # --- the three invariants ---------------------------------------------

    def test_disabled_issues_skip_every_mode_with_a_notice(self) -> None:
        out = self.tmp / 'existing.md'
        out.write_text('stale')
        for mode, extra in (
            ('upsert', ('--body-file', str(self.body))),
            ('recur', ('--body-file', str(self.body), '--comment-file', str(self.comment))),
            ('close-when-clean', ('--comment-file', str(self.comment))),
            ('fetch', ('--body-file', str(out))),
            ('list', ()),
        ):
            with self.subTest(mode=mode):
                proc = self.run_tracker(
                    {'issues_enabled': False, 'open_issues': [TRACKER_ISSUE]},
                    *self.tracker_args('--mode', mode, *extra),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.commands(), ['GET repo'])
                self.assertIn('::notice::cplieger/x has issues disabled', proc.stderr)
                self.assertEqual(proc.stdout, '')
        self.assertEqual(out.read_text(), '', 'fetch leaves an empty file, not a stale body')

    def test_a_failed_setting_read_falls_through_to_the_issue_ops(self) -> None:
        proc = self.run_tracker(
            {
                'fail': {'GET repo': [403, 'Resource not accessible by integration']},
                'open_issues': [TRACKER_ISSUE],
            },
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('attempting the issue ops anyway', proc.stderr)
        self.assertEqual(self.writes(), ['PATCH issue'])

    def test_missing_label_is_created_and_an_existing_one_is_silent(self) -> None:
        proc = self.run_tracker(
            {'existing_labels': ['auto-generated']},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['POST issues'])
        self.assertEqual(proc.stderr, '', 'an "already exists" answer is not reported')
        labels = [c['body'] for c in self.calls() if c['op'] == 'POST labels']
        self.assertEqual([b['name'] for b in labels], ['gremlins-tracker', 'auto-generated'])
        self.assertEqual(labels[0]['color'], '5319e7')

    def test_a_real_label_create_failure_is_reported_and_the_create_still_runs(self) -> None:
        proc = self.run_tracker(
            {'fail': {'POST labels': [403, 'Resource not accessible by integration']}},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("label 'gremlins-tracker' not created", proc.stderr)
        self.assertEqual(self.writes(), ['POST issues'])

    def test_a_502_on_the_lookup_is_retried_then_exits_non_zero_and_writes_nothing(self) -> None:
        for mode, extra, listing in (
            ('upsert', ('--body-file', str(self.body)), LISTING),
            (
                'recur',
                ('--body-file', str(self.body), '--comment-file', str(self.comment)),
                'repos/cplieger/x/issues?state=open&per_page=100&page=1',
            ),
            ('close-when-clean', ('--comment-file', str(self.comment)), LISTING),
            ('fetch', ('--body-file', str(self.tmp / 'existing.md')), LISTING),
            ('list', (), LISTING),
        ):
            with self.subTest(mode=mode):
                code, err, sleeps = self.run_in_process(
                    {'fail': {'GET issues': [502, 'Bad Gateway']}, 'open_issues': [TRACKER_ISSUE]},
                    *self.tracker_args('--mode', mode, *extra),
                )
                self.assertEqual(code, 1)
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.commands().count('GET issues'), 4)
                self.assertEqual(sleeps, [2, 4, 8])
                self.assertIn(f'::error::cplieger/x: GET {listing}: HTTP 502 Bad Gateway', err)

    def test_a_failed_write_exits_non_zero(self) -> None:
        proc = self.run_tracker(
            {'fail': {'PATCH issue': [422, 'Validation Failed']}, 'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('PATCH repos/cplieger/x/issues/42: HTTP 422 Validation Failed', proc.stderr)

    # --- argument contract ------------------------------------------------

    def test_argument_contract(self) -> None:
        cases = (
            ('--label', 'x', '--mode', 'upsert', '--body-file', str(self.body)),
            ('--label', 'x', '--title', 't', '--mode', 'upsert'),
            ('--label', 'x', '--title', 't', '--mode', 'recur', '--body-file', str(self.body)),
            ('--label', 'x', '--title', 't', '--mode', 'close-when-clean'),
            (
                '--label',
                'x',
                '--title',
                't',
                '--mode',
                'upsert',
                '--body-file',
                str(self.body),
                '--flag',
                'on',
            ),
            ('--label', 'x', '--mode', 'list', '--flag-label', 'f', '--flag', 'on'),
        )
        for case in cases:
            with self.subTest(case=case):
                proc = self.run_tracker({}, *case)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(self.calls(), [], 'argument errors never reach gh')

    def test_the_reserved_labels_only_read_from_the_command_line(self) -> None:
        for label, title in (
            ('release-blocked', 'Release blocked'),
            ('repo-audit', 'Repository audit findings'),
        ):
            self.assert_reserved(('--label', label, '--title', title))

    def assert_reserved(self, reserved: tuple) -> None:
        for mode in (
            ('--mode', 'upsert', '--body-file', str(self.body)),
            ('--mode', 'recur', '--body-file', str(self.body), '--comment-file', str(self.comment)),
            ('--mode', 'close-when-clean', '--comment-file', str(self.comment)),
        ):
            with self.subTest(label=reserved[1], mode=mode[1]):
                proc = self.run_tracker({}, *reserved, *mode)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn('reserved', proc.stderr)
                self.assertEqual(self.calls(), [])
        proc = self.run_tracker({}, *reserved, '--mode', 'fetch', '--body-file', str(self.body))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_its_own_writer_may_close_under_the_reserved_label(self) -> None:
        issue = {
            **TRACKER_ISSUE,
            'title': 'Release blocked',
            'labels': [{'name': 'release-blocked'}],
        }
        self.scenario.write_text(json.dumps({'open_issues': [issue]}))
        self.log.write_text('')
        code = (
            'import sys; sys.path.insert(0, sys.argv[1]); import tracker_issue; '
            'sys.exit(tracker_issue.main(sys.argv[2:], allow_reserved=True))'
        )
        proc = subprocess.run(
            [
                sys.executable,
                '-c',
                code,
                str(HERE),
                '--repo',
                'cplieger/x',
                '--label',
                'release-blocked',
                '--title',
                'Release blocked',
                '--mode',
                'close-when-clean',
                '--comment-file',
                str(self.comment),
            ],
            capture_output=True,
            text=True,
            env=self.stub_env(),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['POST comment', 'PATCH issue'])


def _extract_run_block(yaml_text: str, step_name: str) -> str:
    """The dedented shell of the named step's `run: |` block, by text, not YAML.

    The tracker-scripts job that runs this suite installs no PyYAML, so the
    block is lifted with indentation arithmetic: find the step, then its
    `run: |`, then collect the lines indented past it until the block ends.
    """
    lines = yaml_text.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() != f'- name: {step_name}':
        i += 1
    if i == len(lines):
        raise AssertionError(f'step not found: {step_name!r}')
    while i < len(lines) and lines[i].strip() != 'run: |':
        i += 1
    if i == len(lines):
        raise AssertionError(f'no `run: |` under step {step_name!r}')
    body_indent = (len(lines[i]) - len(lines[i].lstrip())) + 2
    i += 1
    body: list[str] = []
    while i < len(lines):
        line = lines[i]
        if line.strip() == '':
            body.append('')
        elif len(line) - len(line.lstrip()) >= body_indent:
            body.append(line[body_indent:])
        else:
            break
        i += 1
    return '\n'.join(body)


# The gate decision — open/comment, close, or nothing — lives in this shell,
# not in tracker_issue.py, so it is exercised by running the real block against
# a stub `gh` (jobs list + run_started_at) and a stub tracker that logs its argv.
GATE_BLOCK = _extract_run_block(NOTIFY_FAILURE.read_text(), 'Track the result in an issue')

GATE_STUB_GH = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
scenario = json.load(open(os.environ['GH_GATE_SCENARIO']))
if not args or args[0] != 'api':
    sys.stderr.write('gate stub gh: unexpected %r\n' % (args,))
    sys.exit(2)
url = next(a for a in args[1:] if not a.startswith('-'))
jq = args[args.index('--jq') + 1] if '--jq' in args else ''
if '/jobs' in url:
    for job in scenario['jobs']:
        sys.stdout.write(json.dumps(job) + '\n')
elif jq == '.run_started_at':
    sys.stdout.write(scenario['run_started_at'] + '\n')
else:
    sys.stderr.write('gate stub gh: unhandled url %s\n' % url)
    sys.exit(2)
"""

GATE_STUB_TRACKER = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['GH_GATE_TRACKER_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
"""

SUCCESS_JOBS = [{'name': 'build', 'status': 'completed', 'conclusion': 'success', 'steps': []}]
FAILED_JOBS = [
    {'name': 'build', 'status': 'completed', 'conclusion': 'success', 'steps': []},
    {
        'name': 'test',
        'status': 'completed',
        'conclusion': 'failure',
        'steps': [{'name': 'unit', 'conclusion': 'failure'}],
    },
]


class GatePolicyTest(unittest.TestCase):
    """The notify-failure.yaml gate: which runs open/comment, close, or file nothing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix='tracker-gate-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        stub = bindir / 'gh'
        stub.write_text(GATE_STUB_GH)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.bindir = bindir
        self.workspace = self.tmp / 'ws'
        tracker_dir = self.workspace / 'ci' / 'scripts'
        tracker_dir.mkdir(parents=True)
        (tracker_dir / 'tracker_issue.py').write_text(GATE_STUB_TRACKER)
        self.runner_temp = self.tmp / 'runner-temp'
        self.runner_temp.mkdir()
        self.scenario = self.tmp / 'gate-scenario.json'
        self.tracker_log = self.tmp / 'tracker.log'

    def run_gate(
        self,
        *,
        event: str,
        jobs: list[dict],
        minutes_ago: int,
        ref_type: str = 'branch',
        ref_name: str = 'main',
        default_branch: str = 'main',
        scope: str = '',
        watched_minutes: int = 30,
    ) -> subprocess.CompletedProcess:
        started = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes_ago)
        self.scenario.write_text(
            json.dumps({'jobs': jobs, 'run_started_at': started.strftime('%Y-%m-%dT%H:%M:%SZ')})
        )
        self.tracker_log.write_text('')
        # A schedule event carries no repository object, so DEFAULT_BRANCH is
        # empty there, exactly as github.event.repository.default_branch resolves.
        default = '' if event == 'schedule' else default_branch
        env = {
            **os.environ,
            'PATH': f'{self.bindir}{os.pathsep}{os.environ["PATH"]}',
            'GH_GATE_SCENARIO': str(self.scenario),
            'GH_GATE_TRACKER_LOG': str(self.tracker_log),
            'GITHUB_WORKSPACE': str(self.workspace),
            'RUNNER_TEMP': str(self.runner_temp),
            'GH_TOKEN': 'x',
            'GH_REPO': 'cplieger/x',
            'RUN_ID': '12345',
            'RUN_URL': 'https://example/run',
            'WORKFLOW': 'Weekly gremlins',
            'DETAILS': '',
            'SCOPE': scope,
            'WATCHED_MINUTES': str(watched_minutes),
            'EVENT': event,
            'REF_TYPE': ref_type,
            'REF_NAME': ref_name,
            'GITHUB_REF_NAME': ref_name,
            'DEFAULT_BRANCH': default,
            'TITLE': 'Weekly gremlins run is failing',
        }
        return subprocess.run(
            ['bash', '-c', GATE_BLOCK], capture_output=True, text=True, env=env, check=False
        )

    def tracker_mode(self) -> str | None:
        """The --mode of the single tracker call, or None when nothing was filed."""
        lines = self.tracker_log.read_text().splitlines()
        if not lines:
            return None
        argv = json.loads(lines[-1])
        return argv[argv.index('--mode') + 1]

    def test_green_scoped_dispatch_does_not_close(self) -> None:
        proc = self.run_gate(
            event='workflow_dispatch', scope='reactive', jobs=SUCCESS_JOBS, minutes_ago=45
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(self.tracker_mode(), 'a scoped run never covered everything, so no close')

    def test_failed_scoped_dispatch_opens(self) -> None:
        proc = self.run_gate(
            event='workflow_dispatch', scope='reactive', jobs=FAILED_JOBS, minutes_ago=45
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tracker_mode(), 'recur', 'a long scoped failure is a real failure')

    def test_failed_short_manual_files_nothing(self) -> None:
        proc = self.run_gate(event='workflow_dispatch', jobs=FAILED_JOBS, minutes_ago=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(self.tracker_mode(), 'a short manual run is assumed watched')

    def test_failed_full_long_dispatch_opens(self) -> None:
        proc = self.run_gate(event='workflow_dispatch', jobs=FAILED_JOBS, minutes_ago=45)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tracker_mode(), 'recur', 'a long full-scope failure is unwatched')

    def test_schedule_failure_opens(self) -> None:
        proc = self.run_gate(event='schedule', jobs=FAILED_JOBS, minutes_ago=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tracker_mode(), 'recur')

    def test_schedule_green_closes(self) -> None:
        proc = self.run_gate(event='schedule', jobs=SUCCESS_JOBS, minutes_ago=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tracker_mode(), 'close-when-clean')

    def test_full_long_dispatch_green_closes(self) -> None:
        proc = self.run_gate(event='workflow_dispatch', jobs=SUCCESS_JOBS, minutes_ago=45)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tracker_mode(), 'close-when-clean')

    def test_full_short_dispatch_green_closes(self) -> None:
        # Closing is duration-independent: a green full-scope run covered
        # everything whatever its length.
        proc = self.run_gate(event='workflow_dispatch', jobs=SUCCESS_JOBS, minutes_ago=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tracker_mode(), 'close-when-clean')

    def test_dispatch_on_feature_branch_files_nothing(self) -> None:
        proc = self.run_gate(
            event='workflow_dispatch', ref_name='feature/x', jobs=FAILED_JOBS, minutes_ago=45
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(self.tracker_mode(), 'a non-default-branch run never touches the tracker')

    def test_green_scoped_on_feature_branch_does_not_close(self) -> None:
        proc = self.run_gate(
            event='workflow_dispatch',
            ref_name='feature/x',
            scope='reactive',
            jobs=SUCCESS_JOBS,
            minutes_ago=45,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(self.tracker_mode())

    def test_tag_push_failure_opens(self) -> None:
        proc = self.run_gate(
            event='push', ref_type='tag', ref_name='v2.1.3', jobs=FAILED_JOBS, minutes_ago=1
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tracker_mode(), 'recur', 'an automation-fired tag push is unwatched')

    def test_tag_push_green_closes(self) -> None:
        proc = self.run_gate(
            event='push', ref_type='tag', ref_name='v2.1.3', jobs=SUCCESS_JOBS, minutes_ago=1
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.tracker_mode(), 'close-when-clean', "a tag push is its workflow's whole run"
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
