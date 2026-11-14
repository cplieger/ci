"""The soft-gated jobs fail on every failed check, however the check died.

A soft-gated job runs each check with continue-on-error, so one failure does not
hide the others, and a last "Check results" step fails the job. A check that
fails normally names itself in a marker file; a check killed whole writes none,
since the OOM killer takes its shell with the test. So "Check results" also
reads each step's outcome, which GitHub records only for a step with an `id`.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / '.github' / 'workflows').glob('*.yaml'))
MARKERS = ('"$RUNNER_TEMP/_ci_failures"', '/tmp/_ci_failures')


def gated_jobs():
    """Yields (file, job name, steps, check step) for each job with a Check results step."""
    for path in WORKFLOWS:
        jobs = (yaml.safe_load(path.read_text()) or {}).get('jobs') or {}
        for name, job in jobs.items():
            steps = job.get('steps') or []
            for step in steps:
                if step.get('name') == 'Check results':
                    yield path.name, name, steps, step


def soft(step):
    return step.get('continue-on-error') in (True, 'true')


def run_check(step, outcomes, marker_lines=()):
    """Runs a Check results body with the given step outcomes and marker lines."""
    with tempfile.TemporaryDirectory() as tmp:
        marker = pathlib.Path(tmp) / '_ci_failures'
        if marker_lines:
            marker.write_text(''.join(f'{line}\n' for line in marker_lines))
        body = step['run']
        for m in MARKERS:
            body = body.replace(m, f'"{marker}"')
        steps = {
            sid: {'outputs': {}, 'outcome': o, 'conclusion': 'success'}
            for sid, o in outcomes.items()
        }
        env = {**os.environ, 'STEP_OUTCOMES': json.dumps(steps, indent=2)}
        return subprocess.run(
            ['bash', '-c', body], env=env, capture_output=True, text=True, check=False
        )


class SoftGateContract(unittest.TestCase):
    def test_the_gated_jobs_are_found(self):
        names = {(f, j) for f, j, _, _ in gated_jobs()}
        for want in (
            ('go-ci.yaml', 'validate'),
            ('ts-ci.yaml', 'validate'),
            ('shell-ci.yaml', 'validate'),
            ('go-os-ci.yaml', 'validate'),
            ('ci.yaml', 'repo'),
            ('ci.yaml', 'scripts'),
        ):
            self.assertIn(want, names)

    def test_every_soft_step_has_an_id(self):
        for f, job, steps, _ in gated_jobs():
            for step in steps:
                if soft(step):
                    with self.subTest(file=f, job=job, step=step.get('name')):
                        self.assertIn(
                            'id',
                            step,
                            'a soft step without an id has no outcome Check results can read',
                        )

    def test_every_check_reads_the_step_outcomes(self):
        for f, job, steps, check in gated_jobs():
            if not any(soft(s) for s in steps):
                continue
            with self.subTest(file=f, job=job):
                env = check.get('env') or {}
                reads_all = env.get('STEP_OUTCOMES') == '${{ toJSON(steps) }}'
                ids = [s['id'] for s in steps if soft(s) and 'id' in s]
                reads_each = all(f'steps.{sid}.outcome' in json.dumps(env) for sid in ids)
                self.assertTrue(
                    reads_all or reads_each, "Check results must read every soft step's outcome"
                )

    def test_an_attempt_is_retried_by_a_later_step(self):
        for f, job, steps, _ in gated_jobs():
            for i, step in enumerate(steps):
                sid = step.get('id', '')
                if not sid.endswith('-attempt'):
                    continue
                with self.subTest(file=f, job=job, step=sid):
                    later = [s.get('if', '') for s in steps[i + 1 :]]
                    self.assertTrue(
                        any(f'steps.{sid}.outcome' in c for c in later),
                        'a failed -attempt step is ignored, so a later step must retry it',
                    )


class CheckResults(unittest.TestCase):
    def checks(self):
        for f, job, _, check in gated_jobs():
            if (check.get('env') or {}).get('STEP_OUTCOMES'):
                yield f, job, check

    def test_some_check_is_covered(self):
        self.assertGreaterEqual(len(list(self.checks())), 6)

    def test_a_step_killed_without_its_marker_fails_the_job(self):
        for f, job, check in self.checks():
            with self.subTest(file=f, job=job):
                r = run_check(check, {'vet': 'success', 'test-race': 'failure'})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn('step test-race', r.stderr)

    def test_a_marker_alone_fails_the_job(self):
        for f, job, check in self.checks():
            with self.subTest(file=f, job=job):
                r = run_check(check, {'vet': 'success'}, ['vet'])
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn('vet', r.stderr)

    def test_passing_skipped_and_retried_steps_pass(self):
        for f, job, check in self.checks():
            with self.subTest(file=f, job=job):
                r = run_check(
                    check,
                    {
                        'vet': 'success',
                        'wiregen-drift': 'skipped',
                        'dependency-review-attempt': 'failure',
                    },
                )
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn('All validation steps passed', r.stdout)


if __name__ == '__main__':
    unittest.main()
