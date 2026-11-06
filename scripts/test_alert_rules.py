"""The alert-rules check: the meta ci.yaml's `alerts` routing, and
actions/alert-rules over the fixture repositories in testdata/alert-rules.

The promtool and lokitool cases run only when ALERT_RULES_TOOLS names a
directory the action's install.sh may fill, as self-ci.yaml's alert-rules job
sets it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deadset_steps import (
    META,
    RENOVATE_PIN,
    Harness,
    git_repo,
    load,
    run_detect,
    workflow_step,
)

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / 'actions' / 'alert-rules'
FIXTURES = Path(__file__).resolve().parent / 'testdata' / 'alert-rules'
SELF_CI = ROOT / '.github' / 'workflows' / 'self-ci.yaml'
TOOLS = os.environ.get('ALERT_RULES_TOOLS', '')
PROMQL = {'alerts/promql.yaml': 'groups: []\n'}
GIT_ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}


def action() -> dict:
    return yaml.safe_load((ACTION / 'action.yml').read_text())


def fixture(name: str, into: Path) -> Path:
    dest = into / name
    shutil.copytree(FIXTURES / name, dest)
    return dest


class Detect(Harness):
    def test_a_root_rule_file_routes_the_alerts_job(self):
        for files in (PROMQL, {'alerts/logql.yaml': 'groups: []\n'}):
            with self.subTest(files=sorted(files)):
                self.assertEqual(
                    run_detect(self, {'README.md': 'x\n', **files})['run_alerts'], 'true'
                )

    def test_no_root_rule_file_skips_the_alerts_job(self):
        cases = {
            'no alerts folder': {'README.md': 'x\n', 'docs/monitoring.md': '# M\n'},
            'a nested rule file': {'README.md': 'x\n', 'alerts/old/promql.yaml': 'groups: []\n'},
            'a rule file in another folder': {'docs/alerts/promql.yaml': 'groups: []\n'},
        }
        for name, files in cases.items():
            with self.subTest(case=name):
                self.assertEqual(run_detect(self, files)['run_alerts'], 'false')

    def test_a_change_to_the_page_alone_still_routes_the_alerts_job(self):
        repo = git_repo(
            self.tmp / 'pr', {'README.md': 'x\n', 'docs/monitoring.md': '# M\n', **PROMQL}
        )
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com']
        subprocess.run([*git, 'commit', '-qm', 'base'], cwd=repo, check=True, env=GIT_ENV)
        base = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=repo,
            check=True,
            env=GIT_ENV,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (repo / 'docs' / 'monitoring.md').write_text('# M\n\nmore\n')
        subprocess.run([*git, 'commit', '-qam', 'page'], cwd=repo, check=True, env=GIT_ENV)
        body = workflow_step(META, 'Detect repo surfaces', 'detect')['run']
        body = body.replace('${{ github.event_name }}', 'pull_request')
        body = body.replace('${{ github.event.pull_request.base.sha }}', base)
        body = re.sub(r'\$\{\{[^}]*\}\}', '', body)
        run = self.run_body(body, repo)
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.outputs['code_changed'], 'false')
        self.assertEqual(run.outputs['run_alerts'], 'true')


class Wiring(unittest.TestCase):
    jobs = load(META)['jobs']

    def test_the_job_runs_the_action_of_this_workflow_commit(self):
        job = self.jobs['alerts']
        self.assertEqual(job['needs'], 'detect')
        self.assertEqual(job['if'], "${{ needs.detect.outputs.run_alerts == 'true' }}")
        self.assertIn('run_alerts', self.jobs['detect']['outputs'])
        source = next(s for s in job['steps'] if s.get('name') == 'Check out the ci source')
        self.assertEqual(source['with']['repository'], '${{ job.workflow_repository }}')
        self.assertEqual(source['with']['ref'], '${{ job.workflow_sha }}')
        self.assertEqual(source['with']['path'], '.cplieger-ci')
        check = next(
            s for s in job['steps'] if s.get('uses') == './.cplieger-ci/actions/alert-rules'
        )
        self.assertIs(check['continue-on-error'], expr2=True)
        self.assertEqual(job['steps'][-1]['name'], 'Check results')
        self.assertIn('/tmp/_ci_failures', job['steps'][-1]['run'])

    def test_validate_fails_on_a_failed_alerts_job(self):
        validate = self.jobs['validate']
        self.assertIn('alerts', validate['needs'])
        step = validate['steps'][0]
        self.assertEqual(step['env']['ALERTS'], '${{ needs.alerts.result }}')
        self.assertIn('"alerts=$ALERTS"', step['run'])

    def test_both_tools_are_pinned_where_renovate_reads_them(self):
        pins = {
            m['var']: (m['dep'], m['ver'])
            for m in RENOVATE_PIN.finditer((ACTION / 'action.yml').read_text())
        }
        self.assertEqual(pins['PROMTOOL_VERSION'][0], 'prometheus/prometheus')
        self.assertEqual(pins['LOKITOOL_VERSION'][0], 'grafana/loki')
        for var in ('PROMTOOL_VERSION', 'LOKITOOL_VERSION'):
            self.assertRegex(pins[var][1], r'^v\d+\.\d+\.\d+$', var)
        env = {
            k: v for step in action()['runs']['steps'] for k, v in (step.get('env') or {}).items()
        }
        self.assertEqual(env['PROMTOOL_VERSION'], pins['PROMTOOL_VERSION'][1])
        self.assertEqual(env['LOKITOOL_VERSION'], pins['LOKITOOL_VERSION'][1])

    def test_self_ci_runs_the_tool_cases(self):
        job = load(SELF_CI)['jobs']['alert-rules']
        run = '\n'.join(s.get('run', '') for s in job['steps'])
        self.assertIn('scripts.test_alert_rules', run)
        self.assertIn('ALERT_RULES_TOOLS', run + str([s.get('env') for s in job['steps']]))


class AlertTable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='alert-table-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def table(self, name: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ACTION / 'alert-table.py')],
            cwd=fixture(name, self.tmp),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_page_listing_every_rule_once_passes(self):
        proc = self.table('pass')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn('::error', proc.stdout)

    def test_a_page_naming_an_alert_no_rule_defines_fails(self):
        proc = self.table('table-extra')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('::error file=docs/monitoring.md::', proc.stdout)
        self.assertIn('`FixtureStalled`', proc.stdout)
        self.assertNotIn('`FixtureDown`', proc.stdout)

    def test_a_rule_the_page_does_not_list_fails(self):
        proc = self.table('table-missing')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('`FixtureErrorLogged`', proc.stdout)
        self.assertIn('alerts/promql.yaml', proc.stdout)

    def test_a_page_with_rules_but_no_alert_table_fails(self):
        repo = fixture('table-missing', self.tmp)
        (repo / 'docs' / 'monitoring.md').write_text('# Monitoring and alerts\n\nNo table.\n')
        proc = subprocess.run(
            [sys.executable, str(ACTION / 'alert-table.py')],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('no alert table', proc.stdout)

    def test_without_both_the_page_and_a_rule_file_the_check_skips(self):
        for name in ('no-alerts', 'test-fails'):
            with self.subTest(fixture=name):
                proc = self.table(name)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn('skipping', proc.stdout)


@unittest.skipUnless(TOOLS, 'ALERT_RULES_TOOLS is not set')
class RuleTools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='alert-rules-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def check(self, name: str) -> subprocess.CompletedProcess:
        env = {
            k: v for step in action()['runs']['steps'] for k, v in (step.get('env') or {}).items()
        }
        env = {k: v for k, v in env.items() if k.endswith('_VERSION')}
        return subprocess.run(
            ['bash', str(ACTION / 'check.sh')],
            cwd=fixture(name, self.tmp),
            env={**os.environ, **env, 'ALERT_RULES_TOOLS': TOOLS},
            capture_output=True,
            text=True,
            timeout=600,
        )

    def test_a_correct_rule_and_test_pair_passes_and_keeps_the_log_rules_intact(self):
        proc = self.check('pass')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('SUCCESS', proc.stdout + proc.stderr)
        for name in ('promql.yaml', 'logql.yaml'):
            self.assertEqual(
                (self.tmp / 'pass' / 'alerts' / name).read_text(),
                (FIXTURES / 'pass' / 'alerts' / name).read_text(),
            )

    def test_a_test_expecting_an_alert_the_rule_never_fires_fails(self):
        proc = self.check('test-fails')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('FixtureDown', proc.stdout + proc.stderr)
        self.assertIn('::error file=alerts/promql_test.yaml::', proc.stdout)

    def test_a_metric_rule_that_does_not_parse_fails(self):
        proc = self.check('promql-bad')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('::error file=alerts/promql.yaml::', proc.stdout)

    def test_a_log_rule_that_does_not_parse_fails(self):
        proc = self.check('logql-bad')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('could not parse expression', proc.stdout + proc.stderr)
        self.assertIn('::error file=alerts/logql.yaml::', proc.stdout)

    def test_a_repository_with_no_alerts_folder_skips(self):
        proc = self.check('no-alerts')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('skipping', proc.stdout)


if __name__ == '__main__':
    unittest.main()
