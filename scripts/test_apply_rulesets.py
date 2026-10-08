"""apply-rulesets.sh against a stub `gh` that answers from a fixture and logs every call."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
SCRIPT = SCRIPTS / 'apply-rulesets.sh'
RULESETS = ROOT / 'configs' / 'rulesets'
META_CI = ROOT / '.github' / 'workflows' / 'ci.yaml'

GH_STUB = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ['STUB_FIXTURE']))
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if args[0] != 'api':
    sys.exit(64)
method, path, jq, body, i = 'GET', None, None, None, 1
while i < len(args):
    if args[i] == '-X':
        method = args[i + 1]
    elif args[i] == '--jq':
        jq = args[i + 1]
    elif args[i] == '--input':
        body = json.load(open(args[i + 1]))
    elif args[i] == '-H':
        pass
    else:
        path = args[i]
        i += 1
        continue
    i += 2
if method == 'POST':
    out = {'id': 900 + len(body['name'])}
elif method == 'PUT':
    out = {}
else:
    entry = fx.get(path)
    if entry is None:
        sys.stderr.write('gh: Not Found (HTTP 404)\n')
        sys.exit(1)
    if isinstance(entry, str):
        sys.stdout.write(entry)
        sys.exit(0)
    out = entry
text = json.dumps(out)
if jq:
    sys.exit(subprocess.run(['jq', '-r', jq], input=text, text=True).returncode)
sys.stdout.write(text + '\n')
"""

PIN_OK = 'a' * 40
PIN_OLD = 'b' * 40


def consumer_ci(*pins: str) -> str:
    jobs = ''.join(
        f'  ci{n}:\n    uses: cplieger/ci/.github/workflows/ci.yaml@{pin} # v3\n'
        for n, pin in enumerate(pins)
    )
    return f'name: CI\non:\n  pull_request:\njobs:\n{jobs}'


# A meta workflow that predates the pull-request policy job.
META_CI_WITHOUT_POLICY = (
    'jobs:\n  detect:\n    runs-on: ubuntu-26.04\n  validate:\n    needs: [detect]\n'
)


# A real pr-policy job that validate, the ruleset's required context, does not need.
META_CI_ORPHAN_POLICY = (
    'jobs:\n  detect:\n    runs-on: ubuntu-26.04\n  pr-policy:\n    runs-on: ubuntu-26.04\n'
    '  validate:\n    needs: [detect]\n'
)


class ApplyRulesets(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        (self.tmp / 'bin').mkdir()
        gh = self.tmp / 'bin' / 'gh'
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.log = self.tmp / 'log'
        self.fixture = self.tmp / 'fixture.json'

    def fx(
        self,
        repo='httpx',
        default='dev',
        dev_ci=None,
        main_ci=None,
        pinned=None,
        rulesets=(),
        **meta,
    ):
        if pinned is None:
            pinned = {PIN_OK: META_CI.read_text(encoding='utf-8'), PIN_OLD: META_CI_WITHOUT_POLICY}
        fx = {
            f'repos/cplieger/{repo}': {
                'name': repo,
                'default_branch': default,
                'visibility': 'public',
                'fork': False,
                'archived': False,
                **meta,
            },
            f'repos/cplieger/{repo}/rulesets': list(rulesets),
        }
        for base, body in (('dev', dev_ci), ('main', main_ci)):
            if body is not None:
                fx[f'repos/cplieger/{repo}/contents/.github/workflows/ci.yaml?ref={base}'] = body
        for pin, body in pinned.items():
            fx[f'repos/cplieger/ci/contents/.github/workflows/ci.yaml?ref={pin}'] = body
        self.fixture.write_text(json.dumps(fx))

    def run_script(self, *args):
        self.log.write_text('')
        env = dict(
            os.environ,
            PATH=f'{self.tmp / "bin"}:{os.environ["PATH"]}',
            STUB_FIXTURE=str(self.fixture),
            STUB_LOG=str(self.log),
        )
        proc = subprocess.run(['bash', str(SCRIPT), *args], env=env, capture_output=True, text=True)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return proc, calls

    @staticmethod
    def writes(calls):
        out = []
        for call in calls:
            if '-X' in call:
                body = json.loads(pathlib.Path(call[call.index('--input') + 1]).read_text())
                out.append((call[call.index('-X') + 1], call[call.index('-X') + 2], body['name']))
        return out

    def test_both_pins_with_pr_policy_create_both_rulesets_then_update_them_by_name(self):
        self.fx(dev_ci=consumer_ci(PIN_OK), main_ci=consumer_ci(PIN_OK))
        proc, calls = self.run_script('httpx')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.writes(calls),
            [
                ('POST', 'repos/cplieger/httpx/rulesets', 'dev'),
                ('POST', 'repos/cplieger/httpx/rulesets', 'main'),
            ],
        )
        for call in calls:
            if '--input' in call:
                sent = json.loads(pathlib.Path(call[call.index('--input') + 1]).read_text())
                self.assertEqual(sent, json.loads((RULESETS / f'{sent["name"]}.json').read_text()))

        self.fx(
            dev_ci=consumer_ci(PIN_OK),
            main_ci=consumer_ci(PIN_OK),
            rulesets=[{'id': 11, 'name': 'dev'}, {'id': 12, 'name': 'main'}],
        )
        proc, calls = self.run_script('httpx')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.writes(calls),
            [
                ('PUT', 'repos/cplieger/httpx/rulesets/11', 'dev'),
                ('PUT', 'repos/cplieger/httpx/rulesets/12', 'main'),
            ],
        )

    def test_a_pin_without_pr_policy_refuses_main_and_still_applies_dev(self):
        for dev_pin, main_pin, base in ((PIN_OK, PIN_OLD, 'main'), (PIN_OLD, PIN_OK, 'dev')):
            with self.subTest(dev=dev_pin[0], main=main_pin[0]):
                self.fx(dev_ci=consumer_ci(dev_pin), main_ci=consumer_ci(main_pin))
                proc, calls = self.run_script('httpx')
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(
                    self.writes(calls), [('POST', 'repos/cplieger/httpx/rulesets', 'dev')]
                )
                self.assertIn(
                    f'main ruleset refused: {base}: pinned cplieger/ci {PIN_OLD} has no pr-policy job that validate needs',
                    proc.stderr,
                )

    def test_an_unreadable_or_ambiguous_pin_refuses_main(self):
        cases = {
            'consumer ci.yaml missing': (
                {'dev_ci': consumer_ci(PIN_OK)},
                'main: .github/workflows/ci.yaml unreadable',
            ),
            'no pin': (
                {'dev_ci': 'name: CI\n', 'main_ci': consumer_ci(PIN_OK)},
                "dev: want exactly one cplieger/ci ci.yaml pin, found ''",
            ),
            'two pins': (
                {'dev_ci': consumer_ci(PIN_OK, PIN_OLD), 'main_ci': consumer_ci(PIN_OK)},
                f"dev: want exactly one cplieger/ci ci.yaml pin, found '{PIN_OK} {PIN_OLD}'",
            ),
            'pin only in a comment or a run body': (
                {
                    'dev_ci': (
                        f'# cplieger/ci/.github/workflows/ci.yaml@{PIN_OK}\njobs:\n  ci:\n'
                        '    runs-on: ubuntu-26.04\n    steps:\n      - run: |\n'
                        f'          echo uses: cplieger/ci/.github/workflows/ci.yaml@{PIN_OK}\n'
                    ),
                    'main_ci': consumer_ci(PIN_OK),
                },
                "dev: want exactly one cplieger/ci ci.yaml pin, found ''",
            ),
            'not a workflow': (
                {'dev_ci': 'jobs: [\n', 'main_ci': consumer_ci(PIN_OK)},
                'dev: .github/workflows/ci.yaml does not parse as a workflow',
            ),
            'pinned meta unreadable': (
                {'dev_ci': consumer_ci(PIN_OK), 'main_ci': consumer_ci(PIN_OK), 'pinned': {}},
                f'dev: cplieger/ci ci.yaml at {PIN_OK} unreadable',
            ),
            'pr-policy only mentioned': (
                {
                    'dev_ci': consumer_ci(PIN_OK),
                    'main_ci': consumer_ci(PIN_OK),
                    'pinned': {
                        PIN_OK: 'jobs:\n  validate:\n    needs: [pr-policy]\n    # pr-policy:\n'
                    },
                },
                f'dev: pinned cplieger/ci {PIN_OK} has no pr-policy job that validate needs',
            ),
            'pr-policy only inside a multiline scalar': (
                {
                    'dev_ci': consumer_ci(PIN_OK),
                    'main_ci': consumer_ci(PIN_OK),
                    'pinned': {
                        PIN_OK: (
                            'name: |\n  pr-policy:\non:\n  workflow_call:\njobs:\n'
                            '  validate:\n    runs-on: ubuntu-26.04\n'
                        )
                    },
                },
                f'dev: pinned cplieger/ci {PIN_OK} has no pr-policy job that validate needs',
            ),
            'pr-policy a scalar, not a job': (
                {
                    'dev_ci': consumer_ci(PIN_OK),
                    'main_ci': consumer_ci(PIN_OK),
                    'pinned': {PIN_OK: 'jobs:\n  pr-policy: x\n'},
                },
                f'dev: pinned cplieger/ci {PIN_OK} has no pr-policy job that validate needs',
            ),
            'pr-policy a job that validate does not need': (
                {
                    'dev_ci': consumer_ci(PIN_OK),
                    'main_ci': consumer_ci(PIN_OK),
                    'pinned': {PIN_OK: META_CI_ORPHAN_POLICY},
                },
                f'dev: pinned cplieger/ci {PIN_OK} has no pr-policy job that validate needs',
            ),
            'validate not a job': (
                {
                    'dev_ci': consumer_ci(PIN_OK),
                    'main_ci': consumer_ci(PIN_OK),
                    'pinned': {
                        PIN_OK: 'jobs:\n  pr-policy:\n    runs-on: ubuntu-26.04\n  validate: pr-policy\n'
                    },
                },
                f'dev: pinned cplieger/ci {PIN_OK} has no pr-policy job that validate needs',
            ),
            'pinned meta not a workflow': (
                {
                    'dev_ci': consumer_ci(PIN_OK),
                    'main_ci': consumer_ci(PIN_OK),
                    'pinned': {PIN_OK: 'jobs: [\n  pr-policy:\n'},
                },
                f'dev: pinned cplieger/ci {PIN_OK} ci.yaml does not parse as a workflow',
            ),
        }
        for name, (kw, reason) in cases.items():
            with self.subTest(name):
                self.fx(**kw)
                proc, calls = self.run_script('httpx')
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(
                    self.writes(calls), [('POST', 'repos/cplieger/httpx/rulesets', 'dev')]
                )
                self.assertIn(f'main ruleset refused: {reason}', proc.stderr)

    def test_a_repo_that_is_not_two_branch_is_refused_before_any_other_read(self):
        for repo, default, meta in (
            ('tool-catalog', 'main', {}),
            ('httpx', 'main', {}),
            ('tool-catalog', 'dev', {}),
            ('ci', 'dev', {}),
            ('httpx', 'dev', {'visibility': 'private'}),
            ('httpx', 'dev', {'fork': True}),
            ('httpx', 'dev', {'archived': True}),
            ('httpx', 'dev', {'visibility': None}),
        ):
            with self.subTest(repo=repo, default=default, meta=meta):
                self.fx(
                    repo=repo,
                    default=default,
                    dev_ci=consumer_ci(PIN_OK),
                    main_ci=consumer_ci(PIN_OK),
                    **meta,
                )
                proc, calls = self.run_script(repo)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(calls, [['api', f'repos/cplieger/{repo}']])
                self.assertIn(f'refusing cplieger/{repo}: not a two-branch repository', proc.stderr)

    def test_dry_run_prints_the_bodies_and_writes_nothing(self):
        self.fx(dev_ci=consumer_ci(PIN_OK), main_ci=consumer_ci(PIN_OK))
        proc, calls = self.run_script('--dry-run', 'httpx')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.writes(calls), [])
        self.assertFalse(any(c[1].endswith('/rulesets') for c in calls))
        for name in ('dev', 'main'):
            body = json.dumps(
                json.loads((RULESETS / f'{name}.json').read_text()), separators=(',', ':')
            )
            self.assertIn(f'would apply cplieger/httpx ruleset {name}: {body}', proc.stdout)

        self.fx(dev_ci=consumer_ci(PIN_OLD), main_ci=consumer_ci(PIN_OLD))
        proc, calls = self.run_script('--dry-run', 'httpx')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.writes(calls), [])
        self.assertNotIn('ruleset main:', proc.stdout)

    def test_usage_errors_make_no_call(self):
        self.fx()
        for args in ((), ('bad/name',), ('httpx', 'extra'), ('--dry-run',)):
            with self.subTest(args=args):
                proc, calls = self.run_script(*args)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
