#!/usr/bin/env python3
"""Pin the contract of scripts/tracker_issue.py against a stub `gh`.

The stub sits first on PATH, answers from a JSON scenario (issue setting, open
issues, which commands fail and how) and appends every invocation's argv to a
log, so each case asserts both what the transport wrote and what it did not.

Run: python3 scripts/test-tracker-issue.py     (exit 0 = pass)
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACKER = HERE / 'tracker_issue.py'

STUB_GH = r"""#!/usr/bin/env python3
import json, os, sys
scenario = json.load(open(os.environ['GH_STUB_SCENARIO']))
args = sys.argv[1:]
with open(os.environ['GH_STUB_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
cmd = ' '.join(args[:2])
if cmd in scenario.get('fail', {}):
    sys.stderr.write(scenario['fail'][cmd] + '\n')
    sys.exit(1)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
if cmd == 'repo view':
    print('true' if scenario.get('issues_enabled', True) else 'false')
elif cmd == 'issue list':
    issues = scenario.get('open_issues', [])
    if opt('--label'):
        issues = [i for i in issues if opt('--label') in [l['name'] for l in i['labels']]]
    if opt('--search'):
        needle = opt('--search').removesuffix(' in:title')
        issues = [i for i in issues if needle in i['title']]
    fields = opt('--json').split(',')
    print(json.dumps([{k: i[k] for k in fields} for i in issues]))
elif cmd == 'issue create':
    print(f"https://github.com/{opt('-R')}/issues/{scenario.get('next_number', 101)}")
elif cmd == 'label create':
    if args[2] in scenario.get('existing_labels', []):
        sys.stderr.write(f'label with name "{args[2]}" already exists; use --force to update\n')
        sys.exit(1)
elif cmd not in ('issue edit', 'issue comment', 'issue close'):
    sys.stderr.write(f'stub gh: unhandled {cmd}\n')
    sys.exit(2)
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


class TrackerIssueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix='tracker-issue-'))
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

    def run_tracker(self, scenario: dict, *args: str) -> subprocess.CompletedProcess:
        self.scenario.write_text(json.dumps(scenario))
        self.log.write_text('')
        env = {
            **os.environ,
            'PATH': f'{self.tmp / "bin"}{os.pathsep}{os.environ["PATH"]}',
            'GH_STUB_SCENARIO': str(self.scenario),
            'GH_STUB_LOG': str(self.log),
        }
        return subprocess.run(
            [sys.executable, str(TRACKER), '--repo', 'cplieger/x', *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def commands(self) -> list[str]:
        return [' '.join(c[:2]) for c in self.calls()]

    def writes(self) -> list[str]:
        """Issue writes only: label creation is idempotent and asserted separately."""
        return [
            c
            for c in self.commands()
            if c in ('issue create', 'issue edit', 'issue comment', 'issue close')
        ]

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
            ['repo view', 'issue list', 'label create', 'label create', 'issue create'],
        )
        created = self.calls()[-1]
        self.assertEqual(created[created.index('--label') + 1], 'gremlins-tracker,auto-generated')
        self.assertEqual(created[created.index('--body-file') + 1], str(self.body))
        self.assertIn('created #101', proc.stdout)

    def test_upsert_edits_the_open_issue_found_by_label_and_title(self) -> None:
        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['issue edit'])
        edit = self.calls()[-1]
        self.assertEqual(edit[2], '42')
        self.assertEqual(edit[edit.index('--body-file') + 1], str(self.body))

    def test_upsert_ignores_a_same_label_issue_with_another_title(self) -> None:
        other = {**TRACKER_ISSUE, 'title': 'Something else entirely'}
        proc = self.run_tracker(
            {'open_issues': [other]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['issue create'])

    def test_upsert_flag_on_adds_the_label_once(self) -> None:
        flag = ('--flag-label', 'mutation-regression', '--flag', 'on')
        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body), *flag),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['issue edit', 'issue edit'])
        add = self.calls()[-1]
        self.assertEqual(add[add.index('--add-label') + 1], 'mutation-regression')
        self.assertIn(
            [
                'label',
                'create',
                'mutation-regression',
                '-R',
                'cplieger/x',
                '--color',
                'b60205',
                '--description',
                'Mutation efficacy regression',
            ],
            self.calls(),
        )

        proc = self.run_tracker(
            {'open_issues': [FLAGGED_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body), *flag),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['issue edit'], 'a present flag is not re-added')

    def test_upsert_flag_off_removes_only_a_present_label(self) -> None:
        flag = ('--flag-label', 'mutation-regression', '--flag', 'off')
        proc = self.run_tracker(
            {'open_issues': [FLAGGED_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body), *flag),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        remove = self.calls()[-1]
        self.assertEqual(remove[remove.index('--remove-label') + 1], 'mutation-regression')

        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body), *flag),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['issue edit'], 'an absent flag is not removed')

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
        self.assertEqual(self.writes(), ['issue create'])
        created = self.calls()[-1]
        self.assertEqual(
            created[created.index('--label') + 1],
            'gremlins-tracker,auto-generated,mutation-regression',
        )

    def test_extra_labels_are_added_on_create(self) -> None:
        proc = self.run_tracker(
            {},
            *self.tracker_args(
                '--mode', 'upsert', '--body-file', str(self.body), '--extra-label', 'needs-triage'
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        created = self.calls()[-1]
        self.assertEqual(
            created[created.index('--label') + 1], 'gremlins-tracker,auto-generated,needs-triage'
        )

    # --- recur ------------------------------------------------------------

    def test_recur_comments_on_the_issue_found_by_title_search(self) -> None:
        proc = self.run_tracker(
            {'open_issues': [FUZZ_ISSUE]},
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
        self.assertEqual(self.writes(), ['issue comment'])
        listing = next(c for c in self.calls() if c[:2] == ['issue', 'list'])
        self.assertNotIn('--label', listing, 'recur matches by title, not by label')
        self.assertEqual(listing[listing.index('--search') + 1], f'{FUZZ_ISSUE["title"]} in:title')
        comment = self.calls()[-1]
        self.assertEqual(comment[2], '7')
        self.assertEqual(comment[comment.index('--body-file') + 1], str(self.comment))

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
        self.assertEqual(self.writes(), ['issue create'])
        created = self.calls()[-1]
        self.assertEqual(created[created.index('--label') + 1], 'fuzz-finding,auto-generated')

    # --- close-when-clean -------------------------------------------------

    def test_close_when_clean_closes_with_the_comment(self) -> None:
        proc = self.run_tracker(
            {'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'close-when-clean', '--comment-file', str(self.comment)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['issue close'])
        close = self.calls()[-1]
        self.assertEqual(close[2], '42')
        self.assertEqual(close[close.index('--comment') + 1], 'Still failing.\n')
        self.assertEqual(close[close.index('--reason') + 1], 'completed')

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
                self.assertEqual(self.commands(), ['repo view'])
                self.assertIn('::notice::cplieger/x has issues disabled', proc.stderr)
                self.assertEqual(proc.stdout, '')
        self.assertEqual(out.read_text(), '', 'fetch leaves an empty file, not a stale body')

    def test_a_failed_setting_read_falls_through_to_the_issue_ops(self) -> None:
        proc = self.run_tracker(
            {
                'fail': {'repo view': 'HTTP 500: Internal Server Error'},
                'open_issues': [TRACKER_ISSUE],
            },
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('attempting the issue ops anyway', proc.stderr)
        self.assertEqual(self.writes(), ['issue edit'])

    def test_missing_label_is_created_and_an_existing_one_is_silent(self) -> None:
        proc = self.run_tracker(
            {'existing_labels': ['auto-generated']},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(), ['issue create'])
        self.assertEqual(proc.stderr, '', 'an "already exists" answer is not reported')
        labels = [c for c in self.calls() if c[:2] == ['label', 'create']]
        self.assertEqual([c[2] for c in labels], ['gremlins-tracker', 'auto-generated'])
        self.assertEqual(labels[0][labels[0].index('--color') + 1], '5319e7')

    def test_a_real_label_create_failure_is_reported_and_the_create_still_runs(self) -> None:
        proc = self.run_tracker(
            {'fail': {'label create': 'HTTP 403: Resource not accessible by integration'}},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("label 'gremlins-tracker' not created", proc.stderr)
        self.assertEqual(self.writes(), ['issue create'])

    def test_a_502_on_the_lookup_exits_non_zero_and_writes_nothing(self) -> None:
        for mode, extra in (
            ('upsert', ('--body-file', str(self.body))),
            ('recur', ('--body-file', str(self.body), '--comment-file', str(self.comment))),
            ('close-when-clean', ('--comment-file', str(self.comment))),
            ('fetch', ('--body-file', str(self.tmp / 'existing.md'))),
            ('list', ()),
        ):
            with self.subTest(mode=mode):
                proc = self.run_tracker(
                    {
                        'fail': {'issue list': 'HTTP 502: Bad Gateway'},
                        'open_issues': [TRACKER_ISSUE],
                    },
                    *self.tracker_args('--mode', mode, *extra),
                )
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(self.writes(), [])
                self.assertIn('::error::cplieger/x: gh issue list failed: HTTP 502', proc.stderr)

    def test_a_failed_write_exits_non_zero(self) -> None:
        proc = self.run_tracker(
            {'fail': {'issue edit': 'HTTP 502: Bad Gateway'}, 'open_issues': [TRACKER_ISSUE]},
            *self.tracker_args('--mode', 'upsert', '--body-file', str(self.body)),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('gh issue edit failed', proc.stderr)

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
