"""The meta workflow enrols a repository in perf-ci.yaml by tests/perf/plan.json."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import perf_gate
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
TEMPLATE = ROOT / '.github' / 'workflow-templates' / 'ci.yml'
READS = {'contents': 'read'}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def code_of(run: str) -> str:
    return '\n'.join(line for line in run.splitlines() if not line.lstrip().startswith('#'))


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

    def test_the_plan_is_made_in_the_call_s_mode(self):
        plan = next(s for s in self.jobs['plan']['steps'] if s.get('id') == 'plan')
        self.assertEqual(plan['env']['MODE'], '${{ inputs.mode }}')
        self.assertIn('--runs "$RUNS" --mode "$MODE"', plan['run'])

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


class ResultArtifacts(unittest.TestCase):
    """Each results artifact holds a directory named after it, and every download merges,
    so one artifact lands where many do; its name carries the run attempt."""

    def downloads(self):
        for path in sorted(WORKFLOWS.glob('*.yaml')):
            for job, spec in load(path.name)['jobs'].items():
                for step in spec.get('steps', []):
                    pattern = (step.get('with') or {}).get('pattern', '')
                    if str(step.get('uses', '')).startswith('actions/download-artifact@') and (
                        str(pattern).startswith('perf-results-')
                    ):
                        yield path.name, job, step

    def test_every_results_download_merges_its_artifacts(self):
        found = list(self.downloads())
        self.assertGreaterEqual(len(found), 5)
        for name, job, step in found:
            with self.subTest(workflow=name, job=job, step=step['name']):
                self.assertIs(step['with'].get('merge-multiple'), expr2=True)

    def test_the_upload_holds_a_directory_named_after_the_attempt_s_artifact(self):
        steps = load('perf-measure.yaml')['jobs']['measure']['steps']
        upload = next(s for s in steps if s['name'] == 'Upload the results')
        name = 'perf-results-${{ inputs.name }}-attempt-${{ github.run_attempt }}'
        self.assertEqual(upload['with']['name'], name)
        self.assertEqual(upload['with']['path'], '${{ runner.temp }}/perf-upload')
        measure = next(s for s in steps if s['name'] == 'Measure')
        self.assertIn('$RUNNER_TEMP/perf-upload/$ARTIFACT/results.json', measure['run'])
        self.assertEqual(measure['env']['ARTIFACT'], name)

    def test_the_verdict_reads_the_remeasure_of_the_compare_it_follows(self):
        jobs = load('perf-ci.yaml')['jobs']
        self.assertEqual(jobs['compare']['outputs']['attempt'], '${{ github.run_attempt }}')
        step = next(s for s in jobs['verdict']['steps'] if s['name'] == 'Verdict')
        self.assertEqual(step['env']['COMPARED'], '${{ needs.compare.outputs.attempt }}')
        self.assertIn('--remeasure-attempt "$COMPARED"', step['run'])

    def test_calibrate_reads_the_artifacts_by_prefix_and_counts_its_runs(self):
        steps = load('perf-calibrate.yaml')['jobs']['propose']['steps']
        run = next(s for s in steps if s['name'] == 'Propose and sign')['run']
        self.assertIn('--calibrate "$RUNNER_TEMP/artifacts" --prefix "$PREFIX" --runs 10', run)
        self.assertIn('--plan-doc measured/tests/perf/plan.json', run)


def step(job: dict, name: str) -> dict:
    return next(s for s in job['steps'] if s.get('name') == name)


def run_body(
    case: unittest.TestCase,
    body: str,
    env: dict,
    stubs: dict[str, str],
    cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Runs a step body under bash -e, in cwd or else a temp dir, with stub commands first
    on PATH."""
    tmp = Path(case.enterContext(tempfile.TemporaryDirectory(prefix='perf-wiring-')))
    (tmp / 'bin').mkdir()
    (tmp / 'runner').mkdir()
    for name, text in stubs.items():
        (tmp / 'bin' / name).write_text(text)
        (tmp / 'bin' / name).chmod(0o755)
    full = {
        'PATH': f'{tmp / "bin"}:{os.environ["PATH"]}',
        'RUNNER_TEMP': str(tmp / 'runner'),
        'GITHUB_OUTPUT': str(tmp / 'output'),
        'GITHUB_STEP_SUMMARY': str(tmp / 'summary'),
        'STUB_LOG': str(tmp / 'log'),
        **env,
    }
    proc = subprocess.run(
        ['bash', '-e', '-c', body], cwd=cwd or tmp, env=full, capture_output=True, text=True
    )
    return proc, tmp


def outputs(tmp: Path) -> dict[str, str]:
    path = tmp / 'output'
    lines = path.read_text().splitlines() if path.exists() else []
    return dict(line.split('=', 1) for line in lines if '=' in line)


# perf_gate.py as the compare step calls it: the gate writes remeasure.json unless GATE
# is `crash`, then exits GATE's code; --plan writes a matrix of PLANNED jobs.
GATE_STUB = """#!/usr/bin/env bash
echo "python3 $*" >> "$STUB_LOG"
out='' matrix=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --remeasure-out) out=$2; shift ;;
    --out) matrix=$2; shift ;;
  esac
  shift
done
if [ -n "$matrix" ]; then
  printf '{"include": [%s]}' "$PLANNED" > "$matrix"
  exit 0
fi
[ "$GATE" = crash ] && exit 1
echo '{"cells": []}' > "$out"
exit "$GATE"
"""
GIT_STUB = """#!/usr/bin/env bash
case "$1" in rev-parse) echo f00d ;; show) exit 128 ;; esac
"""


class Gate(unittest.TestCase):
    jobs = load('perf-ci.yaml')['jobs']

    def compare(self, gate: str, planned: str) -> tuple[int, dict, Path]:
        s = step(self.jobs['compare'], 'Compare')
        env = dict.fromkeys(s['env'], '') | {'MEASURED': 'success', 'CI_TOOLS': 'ci'}
        env |= {'GATE': gate, 'PLANNED': planned}
        proc, tmp = run_body(self, s['run'], env, {'python3': GATE_STUB, 'git': GIT_STUB})
        return proc.returncode, outputs(tmp), tmp

    def test_a_failed_compare_still_plans_its_re_measure(self):
        for gate, rc in (('0', 0), ('1', 1)):
            for planned, again in (('{"name": "shard-1-of-1"}', 'true'), ('', 'false')):
                with self.subTest(gate=gate, planned=planned):
                    got, out, _ = self.compare(gate, planned)
                    self.assertEqual(got, rc)
                    self.assertEqual(out['remeasure'], again)
                    self.assertEqual('matrix' in out, again == 'true')

    def test_a_gate_that_wrote_no_decision_plans_nothing(self):
        got, out, tmp = self.compare('crash', '{"name": "shard-1-of-1"}')
        self.assertEqual((got, out), (1, {'remeasure': 'false'}))
        self.assertNotIn('--remeasure ', (tmp / 'log').read_text())

    def test_the_decision_is_uploaded_after_a_failed_compare_and_read_by_the_verdict(self):
        upload = step(self.jobs['compare'], 'Upload the decision')
        self.assertEqual(
            upload['if'], "${{ !cancelled() && steps.compare.outputs.remeasure == 'true' }}"
        )
        name = 'perf-compare-${{ needs.plan.outputs.prefix }}-attempt-${{ github.run_attempt }}'
        self.assertEqual(upload['with']['name'], name)
        self.assertEqual(upload['with']['path'], '${{ runner.temp }}/remeasure.json')
        download = step(self.jobs['verdict'], 'Download the decision')
        self.assertEqual(
            download['with']['name'],
            'perf-compare-${{ needs.plan.outputs.prefix }}-attempt-${{ needs.compare.outputs.attempt }}',
        )
        verdict = step(self.jobs['verdict'], 'Verdict')['run']
        self.assertIn('--remeasure "$RUNNER_TEMP/compare/remeasure.json"', verdict)

    def test_a_failed_compare_is_still_re_measured_and_judged(self):
        want = "${{ !cancelled() && needs.compare.outputs.remeasure == 'true' }}"
        for job in ('remeasure', 'verdict'):
            with self.subTest(job=job):
                self.assertEqual(self.jobs[job]['if'], want)

    def test_compare_guards_against_dev_s_trend_baselines_alone(self):
        run = step(self.jobs['compare'], 'Compare')['run']
        gate = run.split('--remeasure-out')[0]
        self.assertIn('--trend .perf-trend/series/dev/baseline.json', gate)
        for flag in ('--base', '--budgets', '--sources', '--accepted', '--repo', '--head'):
            with self.subTest(flag=flag):
                self.assertNotRegex(gate, rf'{flag}\b')
        self.assertNotIn('git ', run)

    def test_compare_reads_only_the_baselines_file_of_the_perf_trend_branch(self):
        steps = self.jobs['compare']['steps']
        trend = step(self.jobs['compare'], 'Check out the trend baselines')
        self.assertTrue(trend['uses'].startswith('actions/checkout@'))
        self.assertNotIn('continue-on-error', trend)
        self.assertEqual(
            trend['with'],
            {
                'repository': '${{ inputs.repository || github.repository }}',
                'ref': 'perf-trend',
                'path': '.perf-trend',
                'sparse-checkout': 'series/dev/baseline.json',
                'sparse-checkout-cone-mode': False,
                'persist-credentials': False,
            },
        )
        measured = steps[0]
        self.assertEqual(measured['with']['ref'], '${{ needs.plan.outputs.sha }}')
        self.assertNotIn('fetch-depth', measured['with'])
        self.assertLess(steps.index(measured), steps.index(trend))

    def test_only_the_gate_mode_compares(self):
        on = load('perf-ci.yaml')[True]['workflow_call']['inputs']
        self.assertIn('`measure`', on['mode']['description'])
        self.assertNotIn('calibrate', on['mode']['description'])
        self.assertIn("inputs.mode == 'gate'", self.jobs['compare']['if'])
        for path in sorted(WORKFLOWS.glob('*.y*ml')):
            for job, spec in yaml.safe_load(path.read_text())['jobs'].items():
                if spec.get('uses') == './.github/workflows/perf-ci.yaml':
                    with self.subTest(caller=path.name, job=job):
                        self.assertIn(
                            (spec.get('with') or {}).get('mode', 'gate'), ('gate', 'measure')
                        )

    def test_a_full_set_with_per_unit_runs_is_measured_never_compared(self):
        # The full set runs some units twice, and compare reads one run, so a caller of
        # the full set measures it and leaves the judging to the trend.
        for path in sorted(WORKFLOWS.glob('*.y*ml')):
            for job, spec in yaml.safe_load(path.read_text())['jobs'].items():
                given = spec.get('with') or {}
                if (
                    spec.get('uses') == './.github/workflows/perf-ci.yaml'
                    and given.get('set') == 'full'
                ):
                    with self.subTest(caller=path.name, job=job):
                        self.assertEqual(given.get('mode'), 'measure')

    def test_the_verdict_judges_the_re_measure_and_records_nothing(self):
        verdict = self.jobs['verdict']
        self.assertNotIn('permissions', verdict)
        names = [s.get('name') for s in verdict['steps']]
        for gone in (
            'Checkout',
            'Install cosign',
            'Sign the measured file',
            'Upload the measured file',
        ):
            with self.subTest(step=gone):
                self.assertNotIn(gone, names)
        self.assertNotIn('head', self.jobs['plan']['outputs'])
        s = step(verdict, 'Verdict')
        stub = '#!/usr/bin/env bash\necho "python3 $*" >> "$STUB_LOG"\n'
        for remeasured, rc in (('success', 0), ('failure', 1), ('cancelled', 1)):
            with self.subTest(remeasured=remeasured):
                env = dict.fromkeys(s['env'], '') | {'REMEASURED': remeasured}
                proc, tmp = run_body(self, s['run'], env, {'python3': stub})
                self.assertEqual(proc.returncode, rc, proc.stdout + proc.stderr)
                log = (tmp / 'log').read_text() if rc == 0 else ''
                self.assertEqual('--verdict' in log, rc == 0)
                self.assertNotIn('--budgets', log)
                self.assertNotIn('--record-out', log)


class NoIdToken(unittest.TestCase):
    """The guard signs nothing, so no perf workflow or caller of one holds an id token."""

    def test_no_job_of_perf_ci_requests_an_id_token_or_installs_cosign(self):
        doc = load('perf-ci.yaml')
        self.assertEqual(doc['permissions'], READS)
        for job, spec in doc['jobs'].items():
            with self.subTest(job=job):
                self.assertIsNone(spec.get('permissions'))
        for name in ('perf-ci.yaml', 'perf-measure.yaml'):
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text()
                self.assertNotIn('id-token', text)
                self.assertNotIn('cosign', text.lower())

    def test_no_caller_of_perf_ci_or_the_meta_workflow_grants_an_id_token(self):
        callers = []
        for path in [*sorted(WORKFLOWS.glob('*.y*ml')), TEMPLATE]:
            for job, spec in yaml.safe_load(path.read_text())['jobs'].items():
                called = re.search(r'workflows/([\w.-]+?)(?:@|$)', spec.get('uses', ''))
                if called and called[1] in ('perf-ci.yaml', 'ci.yaml'):
                    callers.append((path.name, job))
                    with self.subTest(caller=path.name, job=job):
                        self.assertNotIn('id-token', spec.get('permissions') or {})
        self.assertEqual(
            sorted(callers),
            [
                ('ci.yaml', 'perf'),
                ('ci.yml', 'ci'),
                ('perf-calibrate.yaml', 'measure'),
                ('perf-trend.yaml', 'measure'),
                ('self-ci.yaml', 'ci'),
                ('weekly-perf.yaml', 'perf'),
            ],
        )

    def test_no_job_of_the_meta_workflow_is_granted_the_id_token(self):
        for job, spec in load('ci.yaml')['jobs'].items():
            with self.subTest(job=job):
                self.assertNotIn('id-token', spec.get('permissions') or {})
        self.assertEqual(load('ci.yaml')['jobs']['perf']['permissions'], READS)


class Calibration(unittest.TestCase):
    doc = load('perf-calibrate.yaml')
    jobs = doc['jobs']

    def test_a_calibration_measures_the_full_set_ten_times_at_the_default_branch_head(self):
        on = self.doc[True]
        self.assertEqual(set(on), {'workflow_dispatch', 'workflow_call'})
        self.assertEqual(set(on['workflow_dispatch']['inputs']), {'repo'})
        self.assertEqual(set(on['workflow_call']['inputs']), {'repo'})
        self.assertEqual(
            set(on['workflow_call']['secrets']), {'SYNC_APP_ID', 'SYNC_APP_PRIVATE_KEY'}
        )
        measure = self.jobs['measure']
        self.assertEqual(measure['permissions'], READS)
        self.assertEqual(
            measure['with'],
            {
                'repository': '${{ needs.check.outputs.repository }}',
                'ref': '${{ needs.check.outputs.sha }}',
                'set': 'full',
                'runs': 10,
                'mode': 'measure',
                'max-parallel': 10,
            },
        )
        self.assertIn('default_branch', step(self.jobs['check'], 'Check the repository')['run'])

    def test_only_the_check_in_holds_a_secret_and_it_runs_none_of_the_repository_s_code(self):
        held = {j for j, spec in self.jobs.items() if 'secrets.' in json.dumps(spec)}
        self.assertEqual(held, {'check-in'})
        job = self.jobs['check-in']
        self.assertEqual(job['permissions'], {'contents': 'read'})
        code = re.compile(
            r'\btests/[\w/.-]*\.sh\b|\bnpm\b|\bnpx\b|\bgo (build|run|test)\b|\bmake\b|\./'
        )
        for s in job['steps']:
            with self.subTest(step=s.get('name')):
                self.assertFalse(
                    str(s.get('uses', '')).startswith(('actions/setup-node@', 'actions/setup-go@'))
                )
                body = '\n'.join(
                    line
                    for line in s.get('run', '').splitlines()
                    if not line.lstrip().startswith('#')
                )
                self.assertNotRegex(body, code)
                for call in re.findall(r'python3 (\S+)', body):
                    self.assertEqual(call, '"$CI_TOOLS/perf_gate.py"')

    def test_the_check_in_opens_a_pull_request_from_its_run_s_branch(self):
        job = self.jobs['check-in']
        token = step(job, 'Mint the App token')
        self.assertEqual(token['with']['repositories'], '${{ needs.check.outputs.name }}')
        self.assertEqual(token['with']['permission-contents'], 'write')
        self.assertEqual(token['with']['permission-pull-requests'], 'write')
        run = step(job, 'Check in the calibration')['run']
        self.assertIn('--check-in "$RUNNER_TEMP/proposed/${RUN_ID}.json"', run)
        self.assertIn('perf/calibration-${RUN_ID}', run)
        self.assertIn('chore(perf): calibrate budgets at ${COMMIT:0:12}', run)
        self.assertNotIn('extraheader', json.dumps(step(job, 'Checkout')))

    def test_the_weekly_calibration_runs_only_when_dispatched(self):
        on = load('weekly-perf-calibrate.yaml')[True]
        self.assertEqual(set(on), {'workflow_dispatch'})
        weekly = load('weekly-perf-calibrate.yaml')['jobs']
        self.assertEqual(weekly['calibrate']['uses'], './.github/workflows/perf-calibrate.yaml')
        self.assertEqual(weekly['calibrate']['strategy']['max-parallel'], 1)
        self.assertEqual(set(weekly['notify']['needs']), set(weekly) - {'notify'})


class Trend(unittest.TestCase):
    """perf-trend.yaml: plan, measure, record, issues and notify, each holding only its token."""

    doc = load('perf-trend.yaml')
    jobs = doc['jobs']

    def test_each_job_holds_only_its_permissions(self):
        self.assertEqual(self.doc['permissions'], READS)
        want = {
            'plan': {'contents': 'read', 'issues': 'read'},
            'measure': READS,
            'record': {'contents': 'write', 'issues': 'read'},
            'issues': {'contents': 'read', 'issues': 'write'},
            'notify': {'actions': 'read', 'contents': 'read', 'issues': 'write'},
        }
        self.assertEqual({j: spec.get('permissions') for j, spec in self.jobs.items()}, want)

    def test_the_writing_jobs_check_out_and_run_none_of_the_measured_code(self):
        code = re.compile(r'\btests/|\bnpm\b|\bnpx\b|\bgo (build|run|test)\b|\bmake\b|\./')
        for job in ('plan', 'record', 'issues'):
            for s in self.jobs[job]['steps']:
                with self.subTest(job=job, step=s.get('name')):
                    if str(s.get('uses', '')).startswith('actions/checkout@'):
                        self.assertEqual(s['with']['repository'], '${{ job.workflow_repository }}')
                        self.assertEqual(s['with']['ref'], '${{ job.workflow_sha }}')
                        self.assertIs(s['with']['persist-credentials'], expr2=False)
                    self.assertFalse(
                        str(s.get('uses', '')).startswith(
                            ('actions/setup-node@', 'actions/setup-go@')
                        )
                    )
                    body = code_of(s.get('run', ''))
                    self.assertNotRegex(body, code)
                    for call in re.findall(r'python3 (\S+)', body):
                        self.assertEqual(call, '"$CI_TOOLS/perf_trend.py"')

    def test_the_token_reaches_git_through_its_environment_never_its_argv(self):
        body = code_of(step(self.jobs['record'], 'Record')['run'])
        self.assertIn('export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.https://github.com/', body)
        self.assertNotRegex(body, r'git -c\b|\$auth\b')
        for line in body.splitlines():
            if 'GH_TOKEN' in line:
                self.assertTrue(line.lstrip().startswith('GIT_CONFIG_VALUE_0='), line)

    def test_no_job_names_a_secret_and_no_run_body_expands_an_expression(self):
        self.assertNotIn('secrets.', (WORKFLOWS / 'perf-trend.yaml').read_text())
        for job, spec in self.jobs.items():
            for s in spec.get('steps', []):
                with self.subTest(job=job, step=s.get('name')):
                    self.assertNotIn('${{', s.get('run', ''))

    def test_measure_is_the_full_set_at_the_planned_commit_eight_jobs_at_a_time(self):
        m = self.jobs['measure']
        self.assertEqual(m['uses'], './.github/workflows/perf-ci.yaml')
        self.assertEqual(m['if'], "${{ needs.plan.outputs.run == 'true' }}")
        self.assertEqual(
            m['with'],
            {
                'ref': '${{ needs.plan.outputs.sha }}',
                'set': 'full',
                'only': '${{ inputs.only }}',
                'mode': 'measure',
                'max-parallel': 8,
                'runner': '${{ inputs.runner }}',
            },
        )

    def test_a_point_is_recorded_only_from_a_whole_measure_and_issues_follow_the_record(self):
        self.assertEqual(self.jobs['record']['needs'], ['plan', 'measure'])
        self.assertEqual(
            self.jobs['record']['if'], "${{ !cancelled() && needs.measure.result == 'success' }}"
        )
        self.assertEqual(self.jobs['issues']['needs'], ['record'])
        self.assertEqual(
            self.jobs['issues']['if'], "${{ !cancelled() && needs.record.result == 'success' }}"
        )
        download = step(self.jobs['issues'], 'Download the actions')
        self.assertEqual(
            download['with']['name'],
            'perf-trend-actions-attempt-${{ needs.record.outputs.attempt }}',
        )
        upload = step(self.jobs['record'], 'Upload the actions')
        self.assertEqual(
            upload['with']['name'], 'perf-trend-actions-attempt-${{ github.run_attempt }}'
        )

    def test_notify_reads_every_other_job(self):
        notify = self.jobs['notify']
        self.assertEqual(notify['uses'], './.github/workflows/notify-failure.yaml')
        self.assertEqual(notify['if'], 'always()')
        self.assertEqual(set(notify['needs']), set(self.jobs) - {'notify'})
        self.assertEqual(notify['with']['scope'], '${{ inputs.only }}')

    def resolve(self, ref='', series='', only='', state=None, default='dev', issues=()):
        s = step(self.jobs['plan'], 'Resolve')
        gh = f"""#!/usr/bin/env bash
case "$*" in
  'api -i repos/cplieger/a/issues?state=all&labels=performance&per_page=100&page=1')
    printf 'HTTP/2.0 200 OK\r\n\r\n%s' "$ISSUES" ;;
  'api repos/cplieger/a --jq .default_branch') echo {default} ;;
  'api repos/cplieger/a/git/ref/heads/'*' --jq .object.sha') echo "${{2#repos/cplieger/a/git/ref/heads/}}" | sha1sum | cut -c1-40 ;;
  *contents/series/*) [ -n "$STATE" ] && {{ printf '%s' "$STATE"; exit 0; }}; echo '{{"message": "Not Found"}}'; exit 1 ;;
  *) echo "unexpected gh $*" >&2; exit 9 ;;
esac
"""
        env = {'REPO': 'cplieger/a', 'REF': ref, 'SERIES': series, 'ONLY': only}
        env |= {'CI_TOOLS': str(ROOT / 'scripts'), 'GH_TOKEN': 't', 'STATE': state or ''}
        env |= {'ISSUES': json.dumps(list(issues))}
        proc, tmp = run_body(self, s['run'], env, {'gh': gh})
        return proc, outputs(tmp)

    def test_resolve_names_the_commit_and_series_and_refuses_what_dev_may_not_take(self):
        proc, out = self.resolve()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((out['run'], out['series'], out['ref']), ('true', 'dev', 'dev'))
        self.assertRegex(out['sha'], r'^[0-9a-f]{40}$')
        self.assertRegex(out['date'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        proc, out = self.resolve('perf-proof/trend', 'proof', 'settings')
        self.assertEqual(
            (proc.returncode, out['series'], out['ref']), (0, 'proof', 'perf-proof/trend')
        )
        for ref, series, only, why in (
            ('feature', '', '', 'only dev joins the dev series'),
            ('', 'dev', 'settings', 'only a series other than dev'),
            ('a..b', 'proof', '', 'not a branch name'),
            ('x', 'Proof', '', 'series Proof must match ^'),
            ('x', 'a' * 32, '', 'must match ^[a-z0-9]'),
        ):
            with self.subTest(ref=ref, series=series, only=only):
                proc, out = self.resolve(ref, series, only)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(why, proc.stdout + proc.stderr)
                self.assertNotIn('run', out)

    def test_resolve_skips_a_measured_commit_with_nothing_pending(self):
        _, first = self.resolve()
        sha = first['sha']
        done = {'schema': 1, 'series': 'dev', 'last': {'commit': sha}, 'scenarios': {}}
        _, out = self.resolve(state=json.dumps(done))
        self.assertEqual(out['run'], 'false')
        pending = {**done, 'scenarios': {'files': {'status': 'suspect'}}}
        _, out = self.resolve(state=json.dumps(pending))
        self.assertEqual(out['run'], 'true')
        body = '<!-- perf-trend:dev:files -->\nbody'
        tracked = {'status': 'open', 'opened': '2026-11-18T00:00:00Z', 'body': body}
        tracked |= {'first_bad': {'date': '2026-11-17T16:00:00Z'}}
        opened = {**done, 'scenarios': {'files': tracked}}
        filed = {'number': 1, 'state': 'open', 'created_at': '2026-11-18T00:05:00Z', 'body': body}
        _, out = self.resolve(state=json.dumps(opened), issues=[filed])
        self.assertEqual(out['run'], 'false')
        _, out = self.resolve(state=json.dumps(opened))
        self.assertEqual(out['run'], 'true')

    def record(self, remote: Path, sha: str, date: str, *, race: bool = False):
        """The Record step against remote, a local bare repository standing in for GitHub,
        on a one-shard measure of sha; race pushes another commit to perf-trend first."""
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='perf-record-')))
        shard = (
            tmp / 'runner' / 'results' / 'perf-results-cplieger-a-measure-shard-1-of-1-attempt-1'
        )
        shard.mkdir(parents=True)
        cell = {'scenario': 'files', 'scale': 'x10', 'engine': 'chromium', 'width': 'desktop'}
        cell |= {'window': 'cold', 'metric': 'cpu_ms', 'kind': 'time', 'reps': [1000, 1000, 1000]}
        doc = {'schema': 1, 'runner': 'ubuntu-26.04', 'cpu_model': 'x', 'cells': [cell]}
        (shard / 'results.json').write_text(json.dumps(doc))
        real = shutil.which('git')
        git = f"""#!/usr/bin/env bash
args=()
for a in "$@"; do args+=("${{a//https:\\/\\/github.com\\/cplieger\\/a.git/{remote}}}"); done
if [[ " $* " == *" push "* ]] && [ -f "$RACE" ]; then
  rm "$RACE"
  w=$(mktemp -d)
  {real} clone -q --branch perf-trend "{remote}" "$w"
  {real} -C "$w" -c user.name=o -c user.email=o@o commit -q --allow-empty -m other
  {real} -C "$w" push -q origin perf-trend
fi
exec {real} "${{args[@]}}"
"""
        gh = """#!/usr/bin/env bash
printf 'HTTP/2.0 200 OK\\r\\n\\r\\n[]'
"""
        if race:
            (tmp / 'race').write_text('')
        s = step(self.jobs['record'], 'Record')
        env = dict.fromkeys(s['env'], '')
        env |= {'GH_TOKEN': 't', 'REPO': 'cplieger/a', 'SERIES': 'dev', 'REF': 'dev', 'SHA': sha}
        env |= {'DATE': date, 'PREFIX': 'cplieger-a'}
        env |= {'RUN_ID': '1', 'RUN_URL': 'https://github.com/cplieger/a/actions/runs/1'}
        env |= {'CI_TOOLS': str(ROOT / 'scripts'), 'RUNNER_TEMP': str(tmp / 'runner')}
        env |= {'RACE': str(tmp / 'race'), 'HOME': str(tmp)}
        proc, _ = run_body(self, s['run'], env, {'git': git, 'gh': gh})
        return proc, tmp / 'runner'

    def test_record_bootstraps_the_branch_then_appends_fast_forward_and_retries_a_race(self):
        remote = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='perf-remote-')))
        subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)

        def log():
            done = subprocess.run(
                ['git', '-C', str(remote), 'log', '--format=%s', 'perf-trend'],
                capture_output=True,
                text=True,
                check=True,
            )
            return done.stdout.splitlines()

        def files():
            done = subprocess.run(
                ['git', '-C', str(remote), 'ls-tree', '-r', '--name-only', 'perf-trend'],
                capture_output=True,
                text=True,
                check=True,
            )
            return done.stdout.splitlines()

        proc, runner = self.record(remote, 'a' * 40, '2026-11-18T00:00:00Z')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(log(), [f'perf: dev point at {"a" * 12}'])
        self.assertEqual(
            files(),
            [
                'series/dev/baseline.json',
                f'series/dev/points/20261118T000000Z-{"a" * 12}.json',
                'series/dev/state.json',
            ],
        )
        self.assertEqual(json.loads((runner / 'actions.json').read_text()), [])
        proc, _ = self.record(remote, 'b' * 40, '2026-11-18T08:00:00Z', race=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('refused', proc.stdout)
        self.assertEqual(
            log(), [f'perf: dev point at {"b" * 12}', 'other', f'perf: dev point at {"a" * 12}']
        )
        self.assertEqual(len([f for f in files() if '/points/' in f]), 2)
        proc, runner = self.record(remote, 'b' * 40, '2026-11-18T08:00:00Z')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('already recorded by this run', proc.stdout)
        self.assertEqual(log()[0], f'perf: dev point at {"b" * 12}')
        self.assertEqual(json.loads((runner / 'actions.json').read_text()), [])

    def test_the_push_is_fast_forward_only(self):
        run = step(self.jobs['record'], 'Record')['run']
        self.assertNotRegex(run, r'--force|\s-f\s|\+HEAD|\+refs')
        self.assertIn('push -q origin HEAD:refs/heads/perf-trend', run)


class TrendCaller(unittest.TestCase):
    """The synced caller of perf-trend.yaml."""

    doc = yaml.safe_load((ROOT / '.github' / 'workflow-templates' / 'perf-trend.yml').read_text())

    def test_it_runs_every_eight_hours_clear_of_the_weekly_runs_and_on_dispatch(self):
        on = self.doc[True]
        self.assertEqual(on['schedule'], [{'cron': '17 1,9,17 * * *'}])
        self.assertEqual(set(on['workflow_dispatch']['inputs']), {'ref', 'series', 'only'})
        weekly = {
            s['cron']
            for path in WORKFLOWS.glob('weekly-*.yaml')
            for s in (load(path.name)[True].get('schedule') or [])
        }
        self.assertTrue(weekly)
        self.assertTrue(all(c.split()[1] == '22' for c in weekly))
        self.assertNotIn('22', on['schedule'][0]['cron'].split()[1].split(','))

    def test_its_one_job_grants_what_the_called_jobs_narrow(self):
        self.assertEqual(self.doc['permissions'], READS)
        (job,) = self.doc['jobs'].values()
        self.assertEqual(
            job['permissions'], {'actions': 'read', 'contents': 'write', 'issues': 'write'}
        )
        self.assertRegex(
            job['uses'], r'^cplieger/ci/\.github/workflows/perf-trend\.yaml@[0-9a-f]{40}$'
        )
        self.assertEqual(
            job['with'],
            {
                'ref': '${{ inputs.ref }}',
                'series': '${{ inputs.series }}',
                'only': '${{ inputs.only }}',
                'runner': '${{ vars.CI_RUNNER }}',
            },
        )
        level = {'none': 0, 'read': 1, 'write': 2}
        granted = job['permissions']
        for name, spec in load('perf-trend.yaml')['jobs'].items():
            for scope, access in (spec.get('permissions') or {}).items():
                with self.subTest(job=name, scope=scope):
                    self.assertLessEqual(level[access], level[granted.get(scope, 'none')])

    def test_points_of_one_series_queue_behind_each_other(self):
        self.assertEqual(
            self.doc['concurrency'],
            {'group': "perf-trend-${{ inputs.series || 'dev' }}", 'cancel-in-progress': False},
        )


class Release(unittest.TestCase):
    def internal(self) -> set[str]:
        run = '\n'.join(
            s.get('run', '') for s in load('self-release.yaml')['jobs']['release']['steps']
        )
        return set(re.search(r'INTERNAL_WORKFLOWS="([^"]+)"', run).group(1).split())

    def test_only_the_workflows_that_run_inside_this_repository_cut_no_release(self):
        internal = self.internal()
        self.assertLessEqual(
            {'perf-calibrate.yaml', 'weekly-perf.yaml', 'weekly-perf-calibrate.yaml'}, internal
        )
        self.assertFalse({'perf-ci.yaml', 'perf-measure.yaml', 'perf-trend.yaml'} & internal)

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
