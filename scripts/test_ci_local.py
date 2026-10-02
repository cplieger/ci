"""The local mirror's step gate runs the image test suite exactly when CI does."""

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


if __name__ == '__main__':
    unittest.main()
