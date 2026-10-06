from __future__ import annotations

import contextlib
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _ci_local  # noqa: E402


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
