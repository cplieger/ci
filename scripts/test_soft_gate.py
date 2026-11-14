"""The soft-gated jobs fail on every failed check, however the check died.

A soft-gated job runs each check with continue-on-error, so one failure does not
hide the others, and a last "Check results" step fails the job. A check that
fails normally names itself in a marker file; a check killed whole writes none,
since the OOM killer takes its shell with the test. So "Check results" also
reads each step's outcome, which GitHub records only for a step with an `id`.
It reads the outcomes alone, one `<id> <outcome>` line per step: the whole
steps context carries every step's outputs, and one variable over the kernel's
128 KiB limit (MAX_ARG_STRLEN) stops bash before the check can run.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / '.github' / 'workflows').glob('*.yaml'))
MARKERS = ('"$RUNNER_TEMP/_ci_failures"', '/tmp/_ci_failures')
# The kernel's limit on one argument or environment string (MAX_ARG_STRLEN).
ARG_LIMIT = 128 * 1024
OUTCOME_LINE = re.compile(r'^(?P<id>[A-Za-z0-9_-]+) \$\{\{ steps\.(?P=id)\.outcome \}\}$')


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


def listed(check):
    """The step ids a Check results step reads the outcome of, in order."""
    text = ((check.get('env') or {}).get('STEP_OUTCOMES') or '').strip()
    ids = []
    for line in text.splitlines():
        m = OUTCOME_LINE.match(line.strip())
        if m is None:
            raise AssertionError(
                f'STEP_OUTCOMES line {line!r} is not `<id> ${{{{ steps.<id>.outcome }}}}`'
            )
        ids.append(m.group('id'))
    return ids


def run_check(step, outcomes, marker_lines=()):
    """Runs a Check results body with STEP_OUTCOMES rendered as the runner
    renders it, each listed step taking its outcome from outcomes (skipped by
    default), and the given marker lines."""
    with tempfile.TemporaryDirectory() as tmp:
        marker = pathlib.Path(tmp) / '_ci_failures'
        if marker_lines:
            marker.write_text(''.join(f'{line}\n' for line in marker_lines))
        body = step['run']
        for m in MARKERS:
            body = body.replace(m, f'"{marker}"')
        rendered = ''.join(f'{sid} {outcomes.get(sid, "skipped")}\n' for sid in listed(step))
        env = {**os.environ, 'STEP_OUTCOMES': rendered}
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

    def test_every_check_reads_each_soft_steps_outcome(self):
        for f, job, steps, check in gated_jobs():
            if not any(soft(s) for s in steps):
                continue
            with self.subTest(file=f, job=job):
                want = [
                    s['id'] for s in steps if soft(s) and not s.get('id', '').endswith('-attempt')
                ]
                env = check.get('env') or {}
                if 'STEP_OUTCOMES' not in env:  # pr-policy reads each outcome in its own variable
                    for sid in want:
                        self.assertIn(f'steps.{sid}.outcome', repr(env))
                    continue
                self.assertEqual(
                    listed(check),
                    want,
                    'STEP_OUTCOMES lists every soft step but an -attempt, in order',
                )

    # The whole steps context, outputs and all, can pass ARG_LIMIT (one
    # dependency review's diff output reached 524 KB), so only outcomes go in.
    def test_no_check_reads_step_outputs(self):
        for f, job, _, check in gated_jobs():
            with self.subTest(file=f, job=job):
                env = repr(check.get('env') or {})
                self.assertNotIn('toJSON(steps)', env)
                self.assertNotIn('.outputs', env)

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
                killed = listed(check)[-1]
                r = run_check(check, {killed: 'failure'})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'step {killed}', r.stderr)

    def test_a_marker_alone_fails_the_job(self):
        for f, job, check in self.checks():
            with self.subTest(file=f, job=job):
                r = run_check(check, dict.fromkeys(listed(check), 'success'), ['vet'])
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn('vet', r.stderr)

    def test_passing_and_skipped_steps_pass(self):
        for f, job, check in self.checks():
            with self.subTest(file=f, job=job):
                outcomes = {
                    sid: ('success' if i % 2 else 'skipped') for i, sid in enumerate(listed(check))
                }
                r = run_check(check, outcomes)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn('All validation steps passed', r.stdout)

    def test_the_rendered_outcomes_stay_far_under_the_limit(self):
        for f, job, check in self.checks():
            with self.subTest(file=f, job=job):
                worst = ''.join(f'{sid} cancelled\n' for sid in listed(check))
                self.assertLess(len(worst.encode()), ARG_LIMIT // 64)


if __name__ == '__main__':
    unittest.main()
