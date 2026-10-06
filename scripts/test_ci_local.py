"""The local mirror: its step gate runs the image test suite exactly when CI does, and it maps the dashboard check to the sibling script."""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _ci_local  # noqa: E402

SUITE_IF = (
    "${{ !cancelled() && steps.build.outcome == 'success' "
    "&& hashFiles('tests/image-test.sh') != '' }}"
)


def suite_ran(build_run: str, harness_run: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / 'tests').mkdir()
        (root / 'tests' / 'image-test.sh').write_text('x')
        steps = [
            {'id': 'build', 'name': 'Build', 'run': build_run},
            {'name': 'Image smoke test', 'run': harness_run},
            {'name': 'Image test suite', 'if': SUITE_IF, 'run': 'touch suite-ran'},
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            _ci_local.process_reusable_steps(
                'docker', steps, root, '.', {}, dry_run=False, ignore_unknown=False
            )
        return (root / 'suite-ran').exists()


class SuiteGate(unittest.TestCase):
    def test_the_suite_runs_after_a_red_harness(self):
        self.assertTrue(suite_ran('true', 'exit 1'))

    def test_the_suite_runs_after_a_green_harness(self):
        self.assertTrue(suite_ran('true', 'true'))

    def test_a_failed_build_skips_the_suite(self):
        self.assertFalse(suite_ran('false', 'true'))

    def test_a_status_function_drops_the_implicit_success(self):
        self.assertTrue(_ci_local.overrides_implicit_success('!cancelled() && x'))
        self.assertTrue(_ci_local.overrides_implicit_success('always()'))
        self.assertFalse(_ci_local.overrides_implicit_success("steps.build.outcome == 'success'"))


class PullRequestPolicy(unittest.TestCase):
    def test_the_pr_policy_job_is_not_planned_locally(self):
        for jobname in ('pr-policy', 'ci/pr-policy'):
            self.assertFalse(_ci_local.job_applies_locally(jobname, ROOT), jobname)
        self.assertTrue(_ci_local.job_applies_locally('markdown', ROOT))


class DashboardCheckMapping(unittest.TestCase):
    def classify(self, uses: str, with_script: bool = True, with_: dict | None = None):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'app' / '.git').mkdir(parents=True)
        if with_script:
            script = tmp / 'ci' / 'actions' / 'dashboard-check' / 'check.sh'
            script.parent.mkdir(parents=True)
            script.write_text('')
        step = {'name': 'Dashboard check', 'uses': uses}
        if with_:
            step['with'] = with_
        with contextlib.chdir(tmp / 'app'):
            return _ci_local.classify_step(step)

    def test_the_checked_out_action_runs_the_sibling_script(self):
        kind, _, detail = self.classify('./.cplieger-ci/actions/dashboard-check')
        self.assertEqual(kind, 'LOCAL')
        self.assertIn('/ci/actions/dashboard-check/check.sh', detail)
        self.assertIn('DASHBOARD_PATH=grafana-dashboard.json', detail)
        self.assertIn('BASE_REF= ', detail)
        self.assertIn('echo "Dashboard check" >> /tmp/_ci_failures', detail)

    def test_the_pinned_action_runs_the_sibling_script(self):
        kind, _, detail = self.classify('cplieger/ci/actions/dashboard-check@' + 'a' * 40)
        self.assertEqual(kind, 'LOCAL')
        self.assertIn('/ci/actions/dashboard-check/check.sh', detail)

    def test_the_path_input_is_passed_through(self):
        _, _, detail = self.classify(
            './.cplieger-ci/actions/dashboard-check',
            with_={'path': 'grafana-dashboards/live operations.json'},
        )
        self.assertIn("DASHBOARD_PATH='grafana-dashboards/live operations.json'", detail)

    def test_a_missing_script_is_not_run_locally(self):
        kind, _, detail = self.classify('./.cplieger-ci/actions/dashboard-check', with_script=False)
        self.assertEqual(kind, 'NOLOCAL')
        self.assertIn('check.sh not found', detail)


if __name__ == '__main__':
    unittest.main()
