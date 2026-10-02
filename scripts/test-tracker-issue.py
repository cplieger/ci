#!/usr/bin/env python3
"""Pin the contract of scripts/tracker_issue.py against a stub `gh`.

The stub sits first on PATH, answers from a JSON scenario (issue setting, open
issues, which commands fail and how) and appends every invocation's argv to a
log, so each case asserts both what the transport wrote and what it did not.

Run: python3 scripts/test-tracker-issue.py     (exit 0 = pass)
"""

from __future__ import annotations

import datetime
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
NOTIFY_FAILURE = HERE.parent / '.github' / 'workflows' / 'notify-failure.yaml'

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
