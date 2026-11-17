"""The meta workflow enrols a repository in perf-ci.yaml by tests/perf/plan.json."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import perf_gate
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
TEMPLATE = ROOT / '.github' / 'workflow-templates' / 'ci.yml'
SIGNS = {'contents': 'read', 'id-token': 'write'}


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
                self.assertTrue(step['with'].get('merge-multiple') is True)

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
echo '{"cells": [], "extend": []}' > "$out"
exit "$GATE"
"""
GIT_STUB = """#!/usr/bin/env bash
case "$1" in rev-parse) echo f00d ;; show) exit 128 ;; esac
"""
# perf_gate.py logging its arguments and keeping the --base budgets it was handed.
BASE_STUB = """#!/usr/bin/env bash
echo "python3 $*" >> "$STUB_LOG"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base) cp "$2" "$STUB_LOG.base" ;;
    --remeasure-out) echo '{}' > "$2" ;;
    --out) echo '{"include": []}' > "$2" ;;
  esac
  shift
done
"""


def merge_ref(case: unittest.TestCase) -> tuple[Path, dict[str, str]]:
    """A repository checked out at GitHub's merge ref of a pull request opened on commit
    `opened`, built on `later`, a base commit that changed budgets.json since."""
    repo = Path(case.enterContext(tempfile.TemporaryDirectory(prefix='perf-merge-')))
    env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t'}
    env |= {'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}

    def git(*args: str) -> str:
        done = subprocess.run(
            ['git', *args], cwd=repo, env=env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def commit(path: str, text: str) -> str:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
        git('add', path)
        git('commit', '-q', '-m', path)
        return git('rev-parse', 'HEAD')

    git('init', '-q', '-b', 'main')
    shas = {'opened': commit('tests/perf/budgets.json', '{"at": "opened"}\n')}
    git('checkout', '-q', '-b', 'pr')
    shas['pr'] = commit('app.txt', 'the change\n')
    git('checkout', '-q', 'main')
    shas['later'] = commit('tests/perf/budgets.json', '{"at": "later"}\n')
    git('checkout', '-q', '--detach')
    git('merge', '-q', '--no-ff', '-m', 'merge ref', 'pr')
    return repo, shas


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

    def test_compare_passes_the_base_commit_and_the_sources(self):
        run = step(self.jobs['compare'], 'Compare')['run']
        self.assertIn('--sources tests/perf/sources', run)
        self.assertIn('--accepted tests/perf/accepted', run)
        self.assertIn('--base-sha "$base"', run)

    def test_compare_judges_against_the_base_the_merge_ref_was_built_on(self):
        s = step(self.jobs['compare'], 'Compare')
        self.assertEqual(s['env']['EVENT'], '${{ github.event_name }}')
        repo, shas = merge_ref(self)
        merge = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        # The event's base SHA is the base tip when the pull request opened.
        for event, base_sha, want in (
            ('pull_request', shas['opened'], 'later'),
            ('merge_group', shas['opened'], 'opened'),
            ('workflow_dispatch', '', None),
        ):
            with self.subTest(event=event):
                env = dict.fromkeys(s['env'], '') | {'MEASURED': 'success', 'CI_TOOLS': 'ci'}
                env |= {'EVENT': event, 'BASE_SHA': base_sha}
                proc, tmp = run_body(self, s['run'], env, {'python3': BASE_STUB}, cwd=repo)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                log = (tmp / 'log').read_text()
                self.assertIn(f'--head {merge} ', log)
                self.assertIn(f'--base-sha {shas[want] if want else merge} ', log)
                held = (tmp / 'log.base').read_text()
                self.assertEqual(held, f'{{"at": "{want or "later"}"}}\n')

    def test_the_verdict_records_what_it_measured_for_the_pull_request_s_head(self):
        plan = self.jobs['plan']
        self.assertEqual(plan['outputs']['head'], '${{ steps.plan.outputs.head }}')
        env = step(plan, 'Plan')['env']
        self.assertEqual(env['HEAD_COMMIT'], '${{ github.event.pull_request.head.sha }}')
        s = step(self.jobs['verdict'], 'Verdict')
        self.assertEqual(s['env']['COMMIT'], '${{ needs.plan.outputs.head }}')
        self.assertEqual(s['env']['RUN_ID'], '${{ github.run_id }}')
        self.assertEqual(s['env']['RUN_ATTEMPT'], '${{ github.run_attempt }}')
        self.assertEqual(s['env']['WORKFLOW_REPOSITORY'], '${{ github.repository }}')
        self.assertEqual(s['env']['WORKFLOW_SHA'], '${{ job.workflow_sha }}')
        for flag in (
            '--record-out "$RUNNER_TEMP/measured"',
            '--commit "$COMMIT"',
            '--run-id "$RUN_ID"',
            '--run-attempt "$RUN_ATTEMPT"',
            '--workflow-repository "$WORKFLOW_REPOSITORY"',
            '--workflow-sha "$WORKFLOW_SHA"',
        ):
            self.assertIn(flag, s['run'])

    def test_a_merge_queue_run_names_no_head_and_records_nothing(self):
        s = step(self.jobs['plan'], 'Plan')
        for event, head_commit, want in (
            ('pull_request', 'abc', 'abc'),
            ('merge_group', '', ''),
            ('workflow_dispatch', '', 'f00d'),
        ):
            with self.subTest(event=event):
                env = dict.fromkeys(s['env'], '') | {'EVENT': event, 'HEAD_COMMIT': head_commit}
                proc, tmp = run_body(self, s['run'], env, {'git': GIT_STUB})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(outputs(tmp)['head'], want)
        v = step(self.jobs['verdict'], 'Verdict')
        token = {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://token'}
        for commit, records in (('abc', True), ('', False)):
            with self.subTest(commit=commit):
                env = dict.fromkeys(v['env'], '') | {'REMEASURED': 'success', 'COMMIT': commit}
                stub = '#!/usr/bin/env bash\necho "python3 $*" >> "$STUB_LOG"\n'
                proc, tmp = run_body(self, v['run'], env | token, {'python3': stub})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                log = (tmp / 'log').read_text()
                self.assertIn('--verdict', log)
                self.assertEqual('--record-out' in log, records, log)
                self.assertEqual('--commit' in log, records, log)
                self.assertNotIn('::warning::', proc.stdout)

    def test_a_run_with_no_id_token_records_nothing_and_says_why(self):
        v = step(self.jobs['verdict'], 'Verdict')
        env = dict.fromkeys(v['env'], '') | {'REMEASURED': 'success', 'COMMIT': 'abc'}
        stub = '#!/usr/bin/env bash\necho "python3 $*" >> "$STUB_LOG"\n'
        proc, tmp = run_body(self, v['run'], env, {'python3': stub})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = (tmp / 'log').read_text()
        self.assertIn('--verdict', log)
        self.assertNotIn('--record-out', log)
        self.assertEqual(outputs(tmp), {})
        self.assertIn('::warning::', proc.stdout)
        self.assertIn('fork', proc.stdout)


COSIGN_STUB = """#!/usr/bin/env bash
echo "cosign $*" >> "$STUB_LOG"
"""


class Signing(unittest.TestCase):
    jobs = load('perf-ci.yaml')['jobs']

    def sign(self, **env) -> tuple[subprocess.CompletedProcess, dict, Path]:
        s = step(self.jobs['verdict'], 'Sign the measured file')
        full = {'CI_TOOLS': str(ROOT / 'scripts'), 'RUN_ID': '900', 'RUN_ATTEMPT': '2', **env}
        proc, tmp = run_body(self, s['run'], full, {'cosign': COSIGN_STUB})
        return proc, outputs(tmp), tmp

    def test_only_the_verdict_of_perf_ci_requests_an_id_token(self):
        doc = load('perf-ci.yaml')
        self.assertEqual(doc['permissions'], {'contents': 'read'})
        for job, spec in doc['jobs'].items():
            with self.subTest(job=job):
                self.assertEqual(spec.get('permissions'), SIGNS if job == 'verdict' else None)
        self.assertNotIn('id-token', json.dumps(load('perf-measure.yaml')))

    def test_every_caller_of_a_signing_workflow_grants_the_id_token(self):
        callers = []
        for path in [*sorted(WORKFLOWS.glob('*.y*ml')), TEMPLATE]:
            for job, spec in yaml.safe_load(path.read_text())['jobs'].items():
                called = re.search(r'workflows/([\w.-]+?)(?:@|$)', spec.get('uses', ''))
                if called and called[1] in ('perf-ci.yaml', 'ci.yaml', 'perf-calibrate.yaml'):
                    callers.append((path.name, job))
                    with self.subTest(caller=path.name, job=job):
                        self.assertEqual((spec.get('permissions') or {}).get('id-token'), 'write')
        self.assertEqual(
            sorted(callers),
            [
                ('ci.yaml', 'perf'),
                ('ci.yml', 'ci'),
                ('perf-calibrate.yaml', 'measure'),
                ('self-ci.yaml', 'ci'),
                ('weekly-perf-calibrate.yaml', 'calibrate'),
                ('weekly-perf.yaml', 'perf'),
            ],
        )

    def test_no_other_job_of_the_meta_workflow_is_granted_the_id_token(self):
        for job, spec in load('ci.yaml')['jobs'].items():
            if job != 'perf':
                with self.subTest(job=job):
                    self.assertNotIn('id-token', spec.get('permissions') or {})
        self.assertEqual(load('ci.yaml')['jobs']['perf']['permissions'], SIGNS)

    def test_the_verdict_signs_the_file_under_its_run_attempt_s_name(self):
        proc, out, tmp = self.sign(ACTIONS_ID_TOKEN_REQUEST_URL='https://token')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out, {'signed': 'true'})
        measured = tmp / 'runner' / 'measured'
        self.assertEqual(
            (tmp / 'log').read_text().split(),
            [
                'cosign',
                'sign-blob',
                '--yes',
                f'{measured}/900-2.json',
                '--bundle',
                f'{measured}/900-2.sigstore.json',
            ],
        )
        upload = step(self.jobs['verdict'], 'Upload the measured file')
        self.assertEqual(upload['if'], "${{ !cancelled() && steps.sign.outputs.signed == 'true' }}")
        self.assertEqual(
            upload['with']['name'],
            'perf-measured-${{ needs.plan.outputs.prefix }}-attempt-${{ github.run_attempt }}',
        )
        self.assertEqual(upload['with']['path'], '${{ runner.temp }}/measured/')
        self.assertEqual(upload['with']['retention-days'], 90)
        sign = step(self.jobs['verdict'], 'Sign the measured file')
        self.assertEqual(
            sign['if'], "${{ !cancelled() && steps.verdict.outputs.recorded == 'true' }}"
        )

    def test_a_signature_that_fails_fails_the_step(self):
        failing = '#!/usr/bin/env bash\necho "cosign $*" >> "$STUB_LOG"\nexit 1\n'
        s = step(self.jobs['verdict'], 'Sign the measured file')
        env = {'CI_TOOLS': str(ROOT / 'scripts'), 'RUN_ID': '900', 'RUN_ATTEMPT': '2'}
        env |= {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://token', 'READBACK_SLEEP': '0'}
        proc, tmp = run_body(self, s['run'], env, {'cosign': failing})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(outputs(tmp), {})
        self.assertEqual(len((tmp / 'log').read_text().splitlines()), 5)


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
        self.assertEqual(measure['permissions'], SIGNS)
        self.assertEqual(
            measure['with'],
            {
                'repository': '${{ needs.check.outputs.repository }}',
                'ref': '${{ needs.check.outputs.sha }}',
                'set': 'full',
                'runs': 10,
                'mode': 'calibrate',
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

    def test_the_weekly_calibration_holds_an_evening_no_other_weekly_run_holds(self):
        crons: dict[str, list[str]] = {}
        for path in sorted(WORKFLOWS.glob('weekly-*.yaml')):
            for s in load(path.name)[True].get('schedule') or []:
                crons.setdefault(s['cron'], []).append(path.name)
        self.assertEqual(crons['0 22 * * 5'], ['weekly-perf-calibrate.yaml'])
        self.assertEqual({k: v for k, v in crons.items() if len(v) > 1}, {})
        weekly = load('weekly-perf-calibrate.yaml')['jobs']
        self.assertEqual(weekly['calibrate']['uses'], './.github/workflows/perf-calibrate.yaml')
        self.assertEqual(weekly['calibrate']['strategy']['max-parallel'], 1)
        self.assertEqual(set(weekly['notify']['needs']), set(weekly) - {'notify'})


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
