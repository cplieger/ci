"""The `go-test-timeout` input reaches every race-test step of a Go module.

ci.yaml forwards it to each job that calls go-ci.yaml or go-os-ci.yaml, and
each of those passes it to `go test -race` as `-timeout` only when it is set, so
an empty input keeps the go command's default.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
INPUT = 'go-test-timeout'
FORWARD = '${{ inputs.go-test-timeout }}'
GO_CALLEES = ('./.github/workflows/go-ci.yaml', './.github/workflows/go-os-ci.yaml')


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def declared(doc: dict) -> dict:
    # PyYAML reads the bare `on` key as True.
    return (doc[True]['workflow_call'] or {}).get('inputs') or {}


def race_step(name: str) -> dict:
    steps = load(name)['jobs']['validate']['steps']
    (step,) = [s for s in steps if s.get('id') == 'test-race']
    return step


class GoTestTimeoutTest(unittest.TestCase):
    def test_each_workflow_declares_it_empty_by_default(self) -> None:
        for name in ('ci.yaml', 'go-ci.yaml', 'go-os-ci.yaml'):
            with self.subTest(workflow=name):
                spec = declared(load(name)).get(INPUT)
                self.assertIsNotNone(spec, f'{name} declares no {INPUT} input')
                self.assertEqual(spec.get('type'), 'string')
                self.assertEqual(spec.get('default'), '')

    def test_ci_forwards_it_to_every_go_job(self) -> None:
        jobs = load('ci.yaml')['jobs']
        callers = {k: j for k, j in jobs.items() if j.get('uses') in GO_CALLEES}
        # The canary tests other repositories at fixed commits, under their defaults.
        self.assertEqual(set(callers) - {'go-os-canary'}, {'go', 'go-nested', 'go-os'})
        callers.pop('go-os-canary', None)
        for key, job in callers.items():
            with self.subTest(job=key):
                self.assertEqual((job.get('with') or {}).get(INPUT), FORWARD)

    def test_the_race_step_passes_it_only_when_set(self) -> None:
        for name in ('go-ci.yaml', 'go-os-ci.yaml'):
            with self.subTest(workflow=name):
                step = race_step(name)
                self.assertEqual(step['env'].get('GO_TEST_TIMEOUT'), FORWARD)
                self.assertIn('${GO_TEST_TIMEOUT:+"-timeout=$GO_TEST_TIMEOUT"}', step['run'])
                self.assertNotIn(
                    'inputs.', step['run'], 'the input reaches the shell through env only'
                )

    def test_the_expansion_gives_no_flag_when_empty_and_one_word_when_set(self) -> None:
        script = 'set -- ${GO_TEST_TIMEOUT:+"-timeout=$GO_TEST_TIMEOUT"}; printf "%s|" "$#" "$@"'
        for value, want in (('', '0|'), ('20m', '1|-timeout=20m|'), ('1h 5m', '1|-timeout=1h 5m|')):
            with self.subTest(value=value):
                out = subprocess.run(
                    ['bash', '-c', script],
                    env={'GO_TEST_TIMEOUT': value},
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                self.assertEqual(out, want)


if __name__ == '__main__':
    unittest.main()
