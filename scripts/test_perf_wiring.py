"""The meta workflow enrols a repository in perf-ci.yaml by tests/perf/plan.json."""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

import perf_gate
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def detect_script() -> str:
    steps = load('ci.yaml')['jobs']['detect']['steps']
    return next(s for s in steps if s.get('id') == 'detect')['run']


class MetaWorkflow(unittest.TestCase):
    jobs = load('ci.yaml')['jobs']

    def test_the_perf_job_calls_perf_ci_behind_run_perf(self):
        job = self.jobs['perf']
        self.assertEqual(job['needs'], 'detect')
        self.assertEqual(job['if'], "${{ needs.detect.outputs.run_perf == 'true' }}")
        self.assertEqual(job['uses'], './.github/workflows/perf-ci.yaml')
        self.assertEqual(job['with'], {'runner': '${{ inputs.runner }}'})
        self.assertEqual(
            self.jobs['detect']['outputs']['run_perf'], '${{ steps.detect.outputs.run_perf }}'
        )

    def test_run_perf_is_set_only_inside_the_code_changed_gate(self):
        script = detect_script()
        self.assertIn('run_perf=false\n', script)
        self.assertIn('echo "run_perf=$run_perf"', script)
        gate = script.index('if [ "$code_changed" = "true" ]; then')
        # The alerts probe is the first line after the gate's closing fi.
        after = script.index('# Outside the code_changed gate')
        sets = [m.start() for m in re.finditer(r'run_perf=true', script)]
        self.assertEqual(len(sets), 1)
        self.assertTrue(gate < sets[0] < after)
        self.assertIn('[ -f tests/perf/plan.json ] && run_perf=true', script)

    def test_validate_fails_on_a_failed_perf_job(self):
        validate = self.jobs['validate']
        self.assertIn('perf', validate['needs'])
        step = validate['steps'][0]
        self.assertEqual(step['env']['PERF'], '${{ needs.perf.result }}')
        self.assertIn('"perf=$PERF"', step['run'])
        for result, rc in (('success', 0), ('skipped', 0), ('failure', 1), ('cancelled', 1)):
            with self.subTest(result=result):
                env = dict.fromkeys(step['env'], 'success')
                env |= {'PERF': result, 'TWO_BRANCH': 'false', 'CI_REPO': 'false'}
                env |= {'PATH': os.environ['PATH'], 'GITHUB_STEP_SUMMARY': os.devnull}
                proc = subprocess.run(
                    ['bash', '-e', '-c', step['run']], env=env, capture_output=True, text=True
                )
                self.assertEqual(proc.returncode, rc, proc.stdout + proc.stderr)
                self.assertIn(f'perf: {result}' if rc == 0 else f'perf job: {result}', proc.stdout)


class PerfCi(unittest.TestCase):
    jobs = load('perf-ci.yaml')['jobs']

    def test_compare_and_verdict_read_only_this_call_s_shards(self):
        for job, name in (('compare', 'Compare'), ('verdict', 'Verdict')):
            with self.subTest(job=job):
                step = next(s for s in self.jobs[job]['steps'] if s.get('name') == name)
                self.assertEqual(step['env']['PREFIX'], '${{ needs.plan.outputs.prefix }}')
                self.assertIn('--prefix "$PREFIX"', step['run'])

    def test_compare_judges_against_the_set_the_plan_measured(self):
        step = next(s for s in self.jobs['compare']['steps'] if s.get('name') == 'Compare')
        self.assertEqual(step['env']['ONLY'], '${{ inputs.only }}')
        gate = step['run'].split('--remeasure-out')[0]
        self.assertIn('--plan-doc tests/perf/plan.json --set "$SET" --only "$ONLY"', gate)

    def test_the_sets_check_plan_counts_are_the_sets_the_workflows_gate(self):
        plan = next(s for s in self.jobs['plan']['steps'] if s.get('id') == 'plan')
        on_a_pull_request = re.findall(r"^\s*SET='([^']+)'$", plan['run'], re.MULTILINE)
        weekly = load('weekly-perf.yaml')['jobs']['perf']['with']['set']
        self.assertEqual(on_a_pull_request, ['pr'])
        self.assertEqual(set(perf_gate.RUN_SETS), {*on_a_pull_request, weekly})


class DispatchInputs(unittest.TestCase):
    def test_a_dispatched_number_reaches_a_called_workflow_as_a_number(self):
        # A called workflow's number input refuses the string a dispatch delivers,
        # failing the run before any of its jobs exists ("Unexpected value '10'").
        for path in sorted(WORKFLOWS.glob('*.yaml')):
            doc = yaml.safe_load(path.read_text())
            dispatch = (doc.get(True) or {}).get('workflow_dispatch') or {}
            numbers = {
                k for k, v in (dispatch.get('inputs') or {}).items() if v.get('type') == 'number'
            }
            for job, spec in doc['jobs'].items():
                if 'uses' not in spec:
                    continue
                for key, value in (spec.get('with') or {}).items():
                    for name in numbers:
                        with self.subTest(workflow=path.name, job=job, input=key):
                            self.assertNotEqual(value, f'${{{{ inputs.{name} }}}}')


class Release(unittest.TestCase):
    def internal(self) -> set[str]:
        run = '\n'.join(
            s.get('run', '') for s in load('self-release.yaml')['jobs']['release']['steps']
        )
        return set(re.search(r'INTERNAL_WORKFLOWS="([^"]+)"', run).group(1).split())

    def test_only_the_workflows_that_run_inside_this_repository_cut_no_release(self):
        internal = self.internal()
        self.assertLessEqual({'perf-calibrate.yaml', 'weekly-perf.yaml'}, internal)
        self.assertFalse({'perf-ci.yaml', 'perf-measure.yaml'} & internal)

    def test_self_ci_installs_cosign_before_the_suite(self):
        steps = load('self-ci.yaml')['jobs']['release-channel-scripts']['steps']
        names = [s.get('name') for s in steps]
        install = steps[names.index('Install cosign')]
        self.assertTrue(install['uses'].startswith('sigstore/cosign-installer@'))
        self.assertLess(
            names.index('Install cosign'), names.index('Probe the release-channel scripts')
        )


if __name__ == '__main__':
    unittest.main()
