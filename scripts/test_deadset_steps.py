from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
DEADSET_CI = WORKFLOWS / 'deadset-ci.yaml'
META = WORKFLOWS / 'ci.yaml'
GO_CI = WORKFLOWS / 'go-ci.yaml'
TS_CI = WORKFLOWS / 'ts-ci.yaml'
SELF_CI = WORKFLOWS / 'self-ci.yaml'
TEMPLATE = ROOT / '.github' / 'workflow-templates' / 'ci.yml'
STEP = 'Dead code (deadset)'
KNIP_STEP = 'Import cycles and undeclared dependencies (knip)'
MARKER = '/tmp/_ci_failures'
SHA = re.compile(r'^[0-9a-f]{40}$')
DEPS = (
    'github.com/cplieger/deadset/v2',
    'github.com/cplieger/deadset-go/v2',
    '@cplieger/deadset-ts',
)

# The generic workflow pin manager of the shared Renovate preset
# (cplieger/.github default.json), which is what bumps these pins.
RENOVATE_PIN = re.compile(
    r'#\s*renovate:\s*datasource=(?P<datasource>[a-z-]+)\s+depName=(?P<dep>[^\s]+)'
    r'(\s+versioning=[a-z-]+)?\s*\n\s*(?P<var>[A-Z_]*VERSION)[=:]\s*[\'"]?(?P<ver>[^\'"\s]+)'
)

DEADSET_STUB = """#!/usr/bin/env bash
if [ "$1" = print-languages ]; then
  printf '%s\\n' "$@" > "$STUB_LOG/print-languages"
  if [ -n "${CENSUS_ERR:-}" ]; then printf '%s\\n' "$CENSUS_ERR" >&2; fi
  printf '%s' "${CENSUS_OUT:-}"
  exit "${CENSUS_RC:-0}"
fi
if [ "$1" = print-config ]; then
  printf '%s\\n' "$@" > "$STUB_LOG/print-config"
  echo '{"target":{"kind":"library"},"analysis":{"languages":["go"]},'\\
'"provenance":{"target.kind":"central","analysis.languages":"central"}}'
  exit 0
fi
printf '%s\\n' "$@" > "$STUB_LOG/argv"
if [ -n "${ANALYZE_ERR:-}" ]; then printf '%s\\n' "$ANALYZE_ERR" >&2; fi
for a in "$@"; do
  case "$a" in
    --central=*) cp "${a#--central=}" "$STUB_LOG/central.json" ;;
    --run-dir=*)
      d="${a#--run-dir=}"
      if [ -e "$d" ]; then echo exists > "$STUB_LOG/rundir"; fi
      if [ -d "$(dirname "$d")" ]; then echo parent > "$STUB_LOG/rundir-parent"; fi
      ;;
  esac
done
rc="${DEADSET_RC:-0}"
if [ "${DEADSET_SARIF:-1}" = 1 ] && [ "$rc" -le 1 ]; then
  mkdir -p "$d"
  echo '{"version":"2.1.0","runs":[]}' > "$d/report.json.sarif"
fi
exit "$rc"
"""

GO_STUB = """#!/usr/bin/env bash
printf '%s GOTOOLCHAIN=%s\\n' "$*" "${GOTOOLCHAIN:-}" >> "$STUB_LOG/go"
if [ "$1" = list ]; then
  printf '%s' "${GO_LIST_OUT:-}"
  exit "${GO_LIST_RC:-0}"
fi
if [ -n "${GO_FAIL_ONCE:-}" ] && [ ! -e "$STUB_LOG/go-failed" ]; then
  : > "$STUB_LOG/go-failed"
  exit 1
fi
exit "${GO_RC:-0}"
"""

NPM_STUB = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$PWD")" "$*" >> "$STUB_LOG/npm"
exit "${NPM_RC:-0}"
"""


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def job_steps(path: Path, job: str) -> list[dict]:
    return load(path)['jobs'][job]['steps']


def workflow_step(path: Path, name: str, job: str = 'validate') -> dict:
    for step in job_steps(path, job):
        if step.get('name') == name:
            return step
    raise AssertionError(f'{path.name} {job} has no step named {name!r}')


WORKFLOW_ENV = {k: str(v) for k, v in yaml.safe_load(DEADSET_CI.read_text())['env'].items()}


def analyze_step(name: str) -> dict:
    return workflow_step(DEADSET_CI, name, 'analyze')


def step_names(path: Path, job: str = 'validate') -> list[str]:
    return [step.get('name', '') for step in job_steps(path, job)]


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Link(str):
    """A fixture entry written as a symlink to this relative path."""

    __slots__ = ()


def git_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, Link):
            path.symlink_to(text)
        else:
            path.write_text(text)
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
    for argv in (['init', '-q'], ['add', '-A', '-f']):
        subprocess.run(['git', *argv], cwd=root, check=True, env=env, capture_output=True)
    return root


def read_outputs(path: Path) -> dict[str, str]:
    outputs, lines, i = {}, path.read_text().splitlines() if path.exists() else [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        opener = re.fullmatch(r'([^=<]+)<<(\S+)', line)
        if opener:
            body = []
            while lines[i] != opener.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            outputs[opener.group(1)] = '\n'.join(body)
        elif '=' in line:
            key, value = line.split('=', 1)
            outputs[key] = value
    return outputs


class StepRun:
    def __init__(self, proc: subprocess.CompletedProcess, log: Path, tmp: Path):
        self.rc = proc.returncode
        self.out = proc.stdout + proc.stderr
        self.log = log
        self.outputs = read_outputs(tmp / 'github_output')
        env = tmp / 'github_env'
        self.github_env = env.read_text() if env.exists() else ''
        marker = tmp / 'ci_failures'
        self.recorded = marker.read_text() if marker.exists() else ''

    def logged(self, name: str) -> str:
        path = self.log / name
        return path.read_text() if path.exists() else ''


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='deadset-step-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.log = self.tmp / 'log'
        self.runner_temp = self.tmp / 'runner'
        for d in (self.bin, self.log, self.runner_temp):
            d.mkdir()

    def stub(self, deadset: bool = True) -> None:
        (self.bin / 'deadset').unlink(missing_ok=True)
        if deadset:
            write_exec(self.bin / 'deadset', DEADSET_STUB)
        write_exec(self.bin / 'go', GO_STUB)
        write_exec(self.bin / 'npm', NPM_STUB)

    def run_body(
        self, body: str, cwd: Path, env: dict[str, str] | None = None, deadset: bool = True
    ) -> StepRun:
        self.stub(deadset)
        for old in (*self.log.iterdir(), *self.tmp.glob('github_*'), self.tmp / 'ci_failures'):
            old.unlink(missing_ok=True)
        script = self.tmp / 'step.sh'
        script.write_text(body.replace(MARKER, str(self.tmp / 'ci_failures')))
        node = shutil.which('node')
        if node and not (self.bin / 'node').exists():
            (self.bin / 'node').symlink_to(node)
        full_env = {
            'PATH': os.pathsep.join([str(self.bin), '/usr/bin', '/bin']),
            'HOME': str(self.tmp),
            'TMPDIR': str(self.tmp),
            'RUNNER_TEMP': str(self.runner_temp),
            'GITHUB_OUTPUT': str(self.tmp / 'github_output'),
            'GITHUB_ENV': str(self.tmp / 'github_env'),
            'GITHUB_STEP_SUMMARY': str(self.tmp / 'github_summary'),
            'GIT_CONFIG_GLOBAL': '/dev/null',
            'STUB_LOG': str(self.log),
            **(env or {}),
        }
        proc = subprocess.run(
            ['bash', '--noprofile', '--norc', '-eo', 'pipefail', str(script)],
            cwd=cwd,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return StepRun(proc, self.log, self.tmp)

    def run_step(self, name: str, cwd: Path, env: dict[str, str] | None = None, **kw) -> StepRun:
        return self.run_body(analyze_step(name)['run'], cwd, {**WORKFLOW_ENV, **(env or {})}, **kw)


def run_detect(harness: Harness, files: dict[str, str]) -> dict[str, str]:
    """Run the meta detect body in a fixture on its fail-safe full-battery path."""
    body = workflow_step(META, 'Detect repo surfaces', 'detect')['run']
    body = re.sub(r'\$\{\{[^}]*\}\}', '', body)
    repo = git_repo(harness.tmp / f'detect-{len(list(harness.tmp.glob("detect-*")))}', files)
    run = harness.run_body(body, repo)
    harness.assertEqual(run.rc, 0, run.out)
    return run.outputs


GO_ONLY = {'go.mod': 'module x\n', 'main.go': 'package main\n'}
MIXED_WEB = {**GO_ONLY, 'static-src/package.json': '{}', 'static-src/package-lock.json': '{}'}
NESTED = {
    **GO_ONLY,
    'yamlenv/go.mod': 'module x/yamlenv\n',
    'yamlenv/x.go': 'package yamlenv\n',
    'web/go.mod': 'module web-ignore\n',
    'web/package.json': '{}',
}
TS_ONLY = {
    'jsr.json': '{}',
    'package.json': '{}',
    'package-lock.json': '{}',
    'tsconfig.json': '{}',
    'mod.ts': '',
}
IMAGE_ONLY = {'Dockerfile': 'FROM scratch\n', 'run.sh': '#!/bin/sh\n'}
CENSUS_STEP = 'Languages in scope (deadset)'
INSTALL_STEP = 'Install analyzers and dependencies'
# The class lines deadset v2.0.0-dev.24 prints for a tree with no program and
# for a tree holding only Protocol Buffers.
NO_PROGRAM = (
    'setup failure: no-program: no file under /w puts a language in scope, so it holds no '
    'program; run from the directory that holds a program'
)
# What deadset v2.0.0-dev.24 prints when deadset-ts finds no inputs in a lone
# tsconfig.json.
TS_NO_PROGRAM = (
    'setup failure: no-program: no configuration discovery derived could be built; '
    "tsconfig.json: TS18003: No inputs were found in config file 'tsconfig.json'."
)
UNSUPPORTED = (
    'setup failure: unsupported-language: the census of /w puts only "proto" in scope, and '
    'this build holds no reader of it; run a release of deadset that holds its reader'
)


def census(
    *languages: str, provenance: str = 'detected', analyzers: list[str] | None = None
) -> str:
    """A census document, contract/census.schema.json, naming the default analyzer of each language."""
    names = [f'deadset-{lang}' for lang in languages] if analyzers is None else analyzers
    return json.dumps(
        {
            'languages': [{'language': lang, 'provenance': provenance} for lang in languages],
            'analyzers': [{'name': n, 'languages': [n.removeprefix('deadset-')]} for n in names],
            'left_out': [],
        },
        indent=2,
    )


class Detect(Harness):
    def test_every_code_change_targets_the_root_and_each_nested_module(self):
        cases = (
            (MIXED_WEB, ['.']),
            (TS_ONLY, ['.']),
            (IMAGE_ONLY, ['.']),
            ({'README.md': 'x\n'}, ['.']),
            (NESTED, ['.', 'yamlenv']),
        )
        for files, want in cases:
            with self.subTest(files=sorted(files)):
                out = run_detect(self, files)
                self.assertEqual(json.loads(out['deadset_targets']), want)
                self.assertEqual(out['code_changed'], 'true')
                self.assertNotIn('run_deadset', out)

    def test_no_workflow_keeps_a_copy_of_deadset_detection(self):
        detect = workflow_step(META, 'Detect repo surfaces', 'detect')['run']
        self.assertNotIn('deadset.json', detect)
        for path in (META, DEADSET_CI):
            with self.subTest(workflow=path.name):
                text = path.read_text()
                for marker in ('jsconfig', '*.mjs', '*.min.js', 'analysis.languages //'):
                    self.assertNotIn(marker, text)


class MetaJobs(unittest.TestCase):
    jobs = load(META)['jobs']

    def test_one_deadset_job_per_target_calls_the_check(self):
        job = self.jobs['deadset']
        self.assertEqual(job['uses'], './.github/workflows/deadset-ci.yaml')
        self.assertEqual(job['needs'], 'detect')
        self.assertEqual(job['if'], "${{ needs.detect.outputs.code_changed == 'true' }}")
        self.assertEqual(
            job['strategy']['matrix']['target'],
            '${{ fromJSON(needs.detect.outputs.deadset_targets) }}',
        )
        self.assertIs(job['strategy']['fail-fast'], expr2=False)
        self.assertEqual(job['with'], {'target': '${{ matrix.target }}'})
        self.assertNotIn('run_deadset', self.jobs['detect']['outputs'])

    def test_no_language_workflow_runs_deadset(self):
        for path in (GO_CI, TS_CI):
            with self.subTest(workflow=path.name):
                text = path.read_text()
                self.assertNotIn('cplieger/deadset', text)
                self.assertNotIn('DEADSET', text)
                self.assertFalse([n for n in step_names(path) if 'deadset' in n.lower()])
        self.assertNotIn('Resolve profile', step_names(GO_CI))
        self.assertNotIn('steps.profile', GO_CI.read_text())

    def test_the_canary_runs_the_same_check_over_every_repository_shape(self):
        job = self.jobs['deadset-canary']
        self.assertEqual(job['uses'], './.github/workflows/deadset-ci.yaml')
        self.assertIn("github.repository == 'cplieger/ci'", job['if'])
        self.assertIn("needs.detect.outputs.code_changed == 'true'", job['if'])
        self.assertEqual(job['with']['exit-code'], 'off')
        self.assertIs(job['strategy']['fail-fast'], expr2=False)
        include = job['strategy']['matrix']['include']
        for entry in include:
            self.assertRegex(entry['ref'], SHA)
            self.assertTrue(entry['repository'].startswith('cplieger/'), entry)
        shapes = {(e['repository'], e['target']) for e in include}
        self.assertEqual(
            shapes,
            {
                ('cplieger/toolbelt', '.'),  # Go only, a library shipping a tool
                ('cplieger/actions', '.'),  # TypeScript only
                ('cplieger/keyenc', '.'),  # Go and TypeScript
                ('cplieger/envx', 'yamlenv'),  # a nested module
                ('cplieger/docker-renovate-scheduler', '.'),  # JavaScript, no npm project
                ('cplieger/docs', '.'),  # no program
            },
        )
        for key in ('repository', 'ref', 'target'):
            self.assertEqual(job['with'][key], f'${{{{ matrix.{key} }}}}')

    def test_every_caller_grants_what_the_analyze_job_requests(self):
        grant = {'contents': 'read', 'security-events': 'write'}
        self.assertEqual(load(DEADSET_CI)['jobs']['analyze']['permissions'], grant)
        callers = [
            (META.name, job)
            for job in self.jobs.values()
            if job.get('uses') == './.github/workflows/deadset-ci.yaml'
        ]
        self.assertEqual(len(callers), 2)
        callers += [(TEMPLATE.name, load(TEMPLATE)['jobs']['ci'])]
        callers += [(SELF_CI.name, load(SELF_CI)['jobs']['ci'])]
        for name, job in callers:
            with self.subTest(caller=name):
                self.assertEqual(job.get('permissions'), grant)


VALIDATE_JOBS = [
    'DETECT',
    'REPO',
    'GO',
    'GO_NESTED',
    'GO_WORK',
    'TS',
    'WEB',
    'TS_NESTED',
    'SHELL',
    'DEADSET',
    'DEADSET_CANARY',
    'DOCKER',
    'DOCKER_ARM64',
    'MARKDOWN',
    'PYTHON',
    'SCRIPTS',
    'PR_POLICY',
]
VALIDATE_ENV = dict.fromkeys(VALIDATE_JOBS, 'success')


class Validate(Harness):
    def run_validate(self, **env: str) -> StepRun:
        body = workflow_step(META, 'Aggregate CI dispatch results', 'validate')['run']
        merged = {**VALIDATE_ENV, 'TWO_BRANCH': 'false', 'CI_REPO': 'false', **env}
        return self.run_body(body, self.tmp, merged)

    def test_validate_waits_for_both_jobs_and_maps_their_results(self):
        job = load(META)['jobs']['validate']
        self.assertLessEqual({'deadset', 'deadset-canary'}, set(job['needs']))
        env = job['steps'][0]['env']
        self.assertEqual(env['DEADSET'], '${{ needs.deadset.result }}')
        self.assertEqual(env['DEADSET_CANARY'], '${{ needs.deadset-canary.result }}')
        self.assertEqual(env['CI_REPO'], "${{ github.repository == 'cplieger/ci' }}")

    def test_a_failed_check_fails_validate_everywhere(self):
        run = self.run_validate(DEADSET='failure')
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error::deadset job: failure', run.out)
        self.assertEqual(self.run_validate(DEADSET='skipped').rc, 0)

    def test_the_canary_counts_in_cplieger_ci_only(self):
        self.assertEqual(self.run_validate(DEADSET_CANARY='failure').rc, 0)
        self.assertNotIn('deadset-canary', self.run_validate().out)
        run = self.run_validate(DEADSET_CANARY='failure', CI_REPO='true')
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error::deadset-canary job: failure', run.out)


class ResolveTarget(Harness):
    def resolve(self, files: dict[str, str], target: str = '.') -> StepRun:
        shutil.rmtree(self.tmp / 'repo', ignore_errors=True)
        repo = git_repo(self.tmp / 'repo', files)
        run = self.run_step('Resolve target', repo / target, {'TARGET': target})
        self.assertEqual(run.rc, 0, run.out)
        return run

    def central(self, run: StepRun) -> dict:
        return json.loads((Path(run.outputs['work']) / 'central.json').read_text())

    def test_the_step_writes_the_central_file_and_names_no_language(self):
        run = self.resolve({'go.mod': 'module x\n', 'cmd/tool/main.go': 'package main\n'})
        self.assertEqual(self.central(run), {'target': {'kind': 'library'}})
        self.assertEqual(set(run.outputs), {'work', 'go_mod', 'go_sum'})

    def test_a_dockerfile_makes_an_application(self):
        run = self.resolve({'go.mod': 'module x\n', 'Dockerfile': 'FROM scratch\n'})
        self.assertEqual(self.central(run), {'target': {'kind': 'application'}})

    def test_a_repository_file_is_left_to_deadset(self):
        files = {'go.mod': 'module x\n', 'deadset.json': '{"analysis":{"languages":["ts"]}}'}
        self.assertEqual(self.central(self.resolve(files)), {'target': {'kind': 'library'}})

    def test_a_nested_target_hands_deadset_go_alone(self):
        files = {
            'go.mod': 'module x\n',
            'Dockerfile': 'FROM scratch\n',
            'yamlenv/go.mod': 'module x/yamlenv\n',
            'yamlenv/go.sum': '',
            'yamlenv/deadset.json': '{"analysis":{"languages":["go","ts"]}}',
        }
        run = self.resolve(files, target='yamlenv')
        self.assertEqual(
            self.central(run), {'target': {'kind': 'library'}, 'analysis': {'languages': ['go']}}
        )
        self.assertEqual((run.outputs['go_mod'], run.outputs['go_sum']), ('true', 'yamlenv/go.sum'))

    def test_the_go_toolchain_and_cache_key_on_the_target_go_mod(self):
        run = self.resolve({'go.mod': 'module x\n', 'go.sum': ''})
        self.assertEqual((run.outputs['go_mod'], run.outputs['go_sum']), ('true', 'go.sum'))
        run = self.resolve({'go.mod': 'module x\n'})
        self.assertEqual((run.outputs['go_mod'], run.outputs['go_sum']), ('true', ''))
        run = self.resolve({'package-lock.json': '{}', 'tsconfig.json': '{}', 'go.sum': ''})
        self.assertEqual((run.outputs['go_mod'], run.outputs['go_sum']), ('false', ''))
        setup = analyze_step('Setup Go')
        self.assertEqual(setup['if'], "${{ steps.target.outputs.go_mod == 'true' }}")
        self.assertEqual(setup['with']['cache'], "${{ steps.target.outputs.go_sum != '' }}")
        self.assertEqual(
            setup['with']['cache-dependency-path'], '${{ steps.target.outputs.go_sum }}'
        )
        self.assertEqual(
            analyze_step('Setup Go (deadset only)')['if'],
            "${{ steps.target.outputs.go_mod != 'true' }}",
        )

    def test_the_job_runs_at_the_target_and_reads_its_go_mod(self):
        job = load(DEADSET_CI)['jobs']['analyze']
        self.assertEqual(job['defaults']['run']['working-directory'], '${{ inputs.target }}')
        self.assertEqual(
            analyze_step('Setup Go')['with']['go-version-file'], '${{ inputs.target }}/go.mod'
        )
        node = analyze_step('Setup Node.js')
        self.assertEqual(node['if'], "${{ steps.census.outputs.ts == 'true' }}")
        self.assertEqual(
            node['with']['cache-dependency-path'], '${{ steps.census.outputs.lockfiles }}'
        )
        self.assertEqual(
            node['with']['cache'], "${{ steps.census.outputs.lockfiles != '' && 'npm' || '' }}"
        )


class Census(Harness):
    def ask(
        self, files: dict[str, str], document: str = '', target: str = '.', **env: str
    ) -> StepRun:
        shutil.rmtree(self.tmp / 'repo', ignore_errors=True)
        repo = git_repo(self.tmp / 'repo', files)
        self.work = Path(tempfile.mkdtemp(dir=self.runner_temp))
        (self.work / 'central.json').write_text('{"target":{"kind":"library"}}')
        merged = {'TARGET': target, 'WORK': str(self.work), 'CENSUS_OUT': document, **env}
        deadset = merged.pop('DEADSET', '1') == '1'
        return self.run_step(CENSUS_STEP, repo / target, merged, deadset=deadset)

    def installs(self, run: StepRun) -> tuple[str, str, str]:
        self.assertEqual(run.rc, 0, run.out)
        return run.outputs['analyze'], run.outputs['go'], run.outputs['ts']

    def assert_unknown(self, run: StepRun, message: str) -> None:
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn(message, run.out)
        self.assertNotIn('analyze', run.outputs)

    def test_deadset_is_asked_about_the_target_with_the_central_file(self):
        run = self.ask(GO_ONLY, census('go'))
        self.assertEqual(
            run.logged('print-languages').splitlines(),
            ['print-languages', '--target=.', f'--central={self.work}/central.json'],
        )
        self.assertEqual(run.logged('argv'), '')

    def test_the_analyzers_deadset_names_decide_what_is_installed(self):
        cases = (
            (census('go'), ('true', 'true', 'false')),
            (census('ts'), ('true', 'false', 'true')),
            (census('go', 'ts'), ('true', 'true', 'true')),
            (census('go', analyzers=[]), ('true', 'false', 'false')),
        )
        lock = {'package-lock.json': '{}'}
        for document, want in cases:
            with self.subTest(census=document):
                self.assertEqual(self.installs(self.ask({**GO_ONLY, **lock}, document)), want)

    def test_the_census_is_printed(self):
        run = self.ask({**GO_ONLY, 'package-lock.json': '{}'}, census('go', 'ts'))
        self.assertIn(
            'analysis.languages go (detected), ts (detected); analyzers deadset-go,deadset-ts',
            run.out,
        )
        run = self.ask(GO_ONLY, census('go', provenance='repository: deadset.json'))
        self.assertIn('analysis.languages go (repository: deadset.json)', run.out)

    def test_the_census_says_whether_deadset_detected_every_language(self):
        lock = {'package-lock.json': '{}'}
        configured = json.loads(census('go', 'ts'))
        configured['languages'][1]['provenance'] = 'repository: deadset.json'
        cases = (
            (census('go', 'ts'), 'true'),
            (census('go', provenance='central: /w/central.json'), 'false'),
            (json.dumps(configured), 'false'),
        )
        for document, want in cases:
            with self.subTest(census=document):
                run = self.ask({**GO_ONLY, **lock}, document)
                self.assertEqual(run.rc, 0, run.out)
                self.assertEqual(run.outputs['detected'], want)

    def test_no_program_is_the_one_answer_that_skips_the_check(self):
        run = self.ask({'README.md': 'x\n'}, CENSUS_RC='3', CENSUS_ERR=NO_PROGRAM)
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.outputs, {'analyze': 'false'})
        self.assertIn(
            '::notice::deadset finds no program at ., so there is nothing to check.', run.out
        )
        self.assertIn(NO_PROGRAM, run.out)

    def test_every_other_census_failure_fails_the_job(self):
        cases = (
            ('3', UNSUPPORTED),
            ('3', 'run failure: product-fault: gomod: go.mod: the file holds no module directive'),
            ('3', f'deadset-go: {NO_PROGRAM}'),
            ('3', ''),
            ('2', 'deadset: deadset.json: the document is not one strict JSON value'),
            ('1', NO_PROGRAM),
        )
        for rc, err in cases:
            with self.subTest(rc=rc, err=err):
                run = self.ask({'README.md': 'x\n'}, census('go'), CENSUS_RC=rc, CENSUS_ERR=err)
                self.assert_unknown(run, f'::error::deadset print-languages exited {rc}')
                self.assertIn('nothing was checked. Its error is above.', run.out)

    def test_a_missing_deadset_fails(self):
        run = self.ask(GO_ONLY, DEADSET='0')
        self.assert_unknown(run, '::error::deadset print-languages exited 127')

    def test_a_census_that_names_no_analyzers_fails(self):
        for document in ('', 'not json', '{}', '{"analyzers": null}'):
            with self.subTest(census=document):
                run = self.ask(GO_ONLY, document)
                self.assert_unknown(run, 'wrote no census naming its analyzers')

    def test_an_analyzer_the_check_cannot_install_fails(self):
        run = self.ask(GO_ONLY, census('go', analyzers=['deadset-go', 'golint-dead']))
        self.assert_unknown(run, '::error::deadset would run the analyzer golint-dead at .')

    def test_a_language_whose_setup_cannot_be_derived_fails(self):
        run = self.ask(
            {'go.mod': 'module x\n', 'web/tsconfig.json': '{}', 'web/package.json': '{}'},
            census('go', 'ts'),
        )
        self.assert_unknown(run, '::error::TypeScript is in scope at . (detected)')
        self.assertIn('no package-lock.json is tracked', run.out)
        run = self.ask({'package-lock.json': '{}', 'tool/x.go': ''}, census('go', 'ts'))
        self.assert_unknown(run, '::error::Go is in scope at . (detected), but . has no go.mod')
        run = self.ask(
            {'go.mod': 'module x\n', 'package.json': '{}'},
            census('ts', provenance='repository: deadset.json'),
        )
        self.assert_unknown(run, 'TypeScript is in scope at . (repository: deadset.json)')

    def test_javascript_without_an_npm_project_installs_nothing(self):
        run = self.ask({'go.mod': 'module x\n', 'tests/stub.mjs': ''}, census('go', 'ts'))
        self.assertEqual(self.installs(run), ('true', 'true', 'true'))
        self.assertEqual((run.outputs['npm_dirs'], run.outputs['lockfiles']), ('', ''))
        vendored = {
            'tsconfig.json': '{}',
            'node_modules/x/package.json': '{}',
            'testdata/y/package.json': '{}',
            'vendor/z/package.json': '{}',
            '.cache/w/package.json': '{}',
        }
        run = self.ask(vendored, census('ts'))
        self.assertEqual(self.installs(run)[2], 'true')
        self.assertEqual(run.outputs['npm_dirs'], '')

    def test_one_tracked_lockfile_lets_a_project_without_one_through(self):
        files = {
            'tsconfig.json': '{}',
            'package.json': '{}',
            'package-lock.json': '{}',
            'fixtures/a/target/package.json': '{}',
        }
        run = self.ask(files, census('ts'))
        self.assertEqual(self.installs(run)[2], 'true')
        self.assertEqual(run.outputs['npm_dirs'], '.')

    def test_only_tracked_lockfiles_outside_vendored_trees_count(self):
        files = {
            'go.mod': 'module x\n',
            'web/tsconfig.json': '{}',
            'web/package-lock.json': '{}',
            'node_modules/x/package-lock.json': '{}',
            'web/node_modules/y/package-lock.json': '{}',
            'testdata/y/package-lock.json': '{}',
            'vendor/z/package-lock.json': '{}',
            '.cache/w/package-lock.json': '{}',
        }
        run = self.ask(files, census('go', 'ts'))
        self.assertEqual(run.outputs['npm_dirs'], 'web')
        self.assertEqual(run.outputs['lockfiles'], 'web/package-lock.json')
        untracked = self.tmp / 'repo' / 'ui' / 'package-lock.json'
        untracked.parent.mkdir()
        untracked.write_text('{}')
        run = self.run_step(
            CENSUS_STEP,
            self.tmp / 'repo',
            {'TARGET': '.', 'WORK': str(self.work), 'CENSUS_OUT': census('go', 'ts')},
        )
        self.assertEqual(run.outputs['npm_dirs'], 'web')

    def test_a_root_project_caches_on_its_own_lockfile(self):
        run = self.ask({'package-lock.json': '{}', 'tsconfig.json': '{}'}, census('ts'))
        self.assertEqual(
            (run.outputs['npm_dirs'], run.outputs['lockfiles']), ('.', 'package-lock.json')
        )

    def test_no_npm_project_is_installed_when_deadset_ts_does_not_run(self):
        files = {'go.mod': 'module x\n', 'web/tsconfig.json': '{}', 'web/package-lock.json': '{}'}
        run = self.ask(files, census('go', provenance='repository: deadset.json'))
        self.assertEqual(self.installs(run), ('true', 'true', 'false'))
        self.assertEqual((run.outputs['npm_dirs'], run.outputs['lockfiles']), ('', ''))

    def test_a_nested_target_names_its_lockfiles_from_the_repository_root(self):
        files = {
            'go.mod': 'module x\n',
            'yamlenv/go.mod': 'module x/yamlenv\n',
            'yamlenv/web/tsconfig.json': '{}',
            'yamlenv/web/package-lock.json': '{}',
        }
        run = self.ask(files, census('go', 'ts'), target='yamlenv')
        self.assertEqual(run.outputs['npm_dirs'], 'web')
        self.assertEqual(run.outputs['lockfiles'], 'yamlenv/web/package-lock.json')

    def test_every_later_step_waits_for_the_census(self):
        names = step_names(DEADSET_CI, 'analyze')
        gated = {
            'Setup Node.js': "${{ steps.census.outputs.ts == 'true' }}",
            INSTALL_STEP: "${{ steps.census.outputs.analyze == 'true' }}",
            STEP: "${{ steps.census.outputs.analyze == 'true' }}",
        }
        for name in names[names.index(CENSUS_STEP) + 1 :]:
            with self.subTest(step=name):
                if name in gated:
                    self.assertEqual(analyze_step(name)['if'], gated[name])
                else:
                    self.assertIs(analyze_step(name).get('continue-on-error'), expr2=True)
        self.assertNotIn('continue-on-error', analyze_step(CENSUS_STEP))


class Analyze(Harness):
    def setUp(self):
        super().setUp()
        self.repo = git_repo(self.tmp / 'repo', {'go.mod': 'module x\n'})
        self.work = Path(tempfile.mkdtemp(dir=self.runner_temp))
        (self.work / 'central.json').write_text('{"target":{"kind":"library"}}')

    def analyze(self, deadset: bool = True, **env: str) -> StepRun:
        merged = {
            'TARGET': '.',
            'EXIT_CODE': 'on',
            'WORK': str(self.work),
            'DETECTED': 'true',
            'GO': 'true',
            'NPM_DIRS': '',
            **env,
        }
        return self.run_step(STEP, self.repo, merged, deadset=deadset)

    def assert_failed(self, run: StepRun, message: str) -> None:
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn(f'::error::deadset {message}', run.out)

    def test_the_step_is_the_job_gate(self):
        step = analyze_step(STEP)
        self.assertNotIn('continue-on-error', step)
        self.assertNotIn(MARKER, step['run'])
        names = step_names(DEADSET_CI, 'analyze')
        after_setup = names[names.index(INSTALL_STEP) + 1 :]
        hard = [n for n in after_setup if analyze_step(n).get('continue-on-error') is not True]
        self.assertEqual(hard, [STEP])

    def test_no_run_block_inlines_an_expression(self):
        for step in job_steps(DEADSET_CI, 'analyze'):
            with self.subTest(step=step.get('name')):
                self.assertNotIn('${{', step.get('run', ''))

    def test_one_run_covers_every_language_and_flags_only_the_report_formats(self):
        run = self.analyze()
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(
            run.logged('argv').splitlines(),
            [
                'analyze',
                '--target=.',
                f'--central={self.work}/central.json',
                f'--run-dir={self.work}/run',
                '--formats=text,sarif',
                '--exit-code=on',
            ],
        )
        self.assertEqual(run.logged('rundir'), '')
        self.assertEqual(run.logged('rundir-parent'), 'parent\n')

    def test_the_exit_code_input_reaches_deadset(self):
        run = self.analyze(EXIT_CODE='off')
        self.assertIn('--exit-code=off', run.logged('argv').splitlines())

    def test_the_resolved_kind_is_printed(self):
        run = self.analyze()
        self.assertIn('target.kind library (central)\n', run.out)
        self.assertEqual(
            run.logged('print-config').splitlines(),
            ['print-config', '--target=.', f'--central={self.work}/central.json'],
        )

    def test_every_nonzero_exit_fails_with_its_own_message(self):
        cases = {
            '1': 'reported dead code',
            '2': 'refused the run (exit 2)',
            '3': 'produced no answer (exit 3)',
            '4': 'left a cross-language finding pending (exit 4)',
            '5': 'exited 5, which is no verdict',
        }
        for rc, message in cases.items():
            with self.subTest(rc=rc):
                self.assert_failed(self.analyze(DEADSET_RC=rc), message)

    def test_the_step_reads_whether_the_census_detected_every_language(self):
        self.assertEqual(
            analyze_step(STEP)['env']['DETECTED'], '${{ steps.census.outputs.detected }}'
        )

    def test_no_program_in_detected_languages_passes(self):
        run = self.analyze(DEADSET_RC='3', ANALYZE_ERR=TS_NO_PROGRAM)
        self.assertEqual(run.rc, 0, run.out)
        self.assertIn(
            '::notice::deadset finds no program at ., so there is nothing to check.', run.out
        )
        self.assertIn(TS_NO_PROGRAM, run.out)
        self.assertNotIn('::error::', run.out)

    def test_no_program_in_configured_languages_fails(self):
        run = self.analyze(DEADSET_RC='3', ANALYZE_ERR=TS_NO_PROGRAM, DETECTED='false')
        self.assert_failed(run, 'produced no answer (exit 3)')
        self.assertNotIn('::notice::', run.out)

    def test_every_other_exit_3_fails_in_detected_languages(self):
        for err in (
            'run failure: analyzer-crashed: deadset-ts: exit status 2',
            f'deadset-ts: {TS_NO_PROGRAM}',
            '',
        ):
            with self.subTest(err=err):
                run = self.analyze(DEADSET_RC='3', ANALYZE_ERR=err)
                self.assert_failed(run, 'produced no answer (exit 3)')
        run = self.analyze(DEADSET_RC='2', ANALYZE_ERR=TS_NO_PROGRAM)
        self.assert_failed(run, 'refused the run (exit 2)')

    def test_a_misplaced_file_fails_a_run_with_no_program(self):
        self.repo = git_repo(self.tmp / 'repo', {'web/deadset.json': '{}'})
        run = self.analyze(DEADSET_RC='3', ANALYZE_ERR=TS_NO_PROGRAM, NPM_DIRS='web', GO='false')
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error file=web/deadset.json::', run.out)

    def test_a_complete_run_without_its_sarif_report_fails(self):
        for rc in ('0', '1'):
            with self.subTest(rc=rc):
                shutil.rmtree(self.work / 'run', ignore_errors=True)
                run = self.analyze(DEADSET_RC=rc, DEADSET_SARIF='0')
                self.assert_failed(run, f'exited {rc} but wrote no SARIF report')
                self.assertIn('cannot reach code scanning', run.out)
        shutil.rmtree(self.work / 'run', ignore_errors=True)
        (self.work / 'run').mkdir()
        (self.work / 'run' / 'report.json.sarif').write_text('')
        self.assert_failed(self.analyze(DEADSET_SARIF='0'), 'exited 0 but wrote no SARIF report')

    def test_a_missing_deadset_fails_and_names_the_install_step(self):
        run = self.analyze(deadset=False)
        self.assert_failed(run, 'is not on PATH')
        self.assertIn('Check the Install deadset step above', run.out)

    def test_a_go_module_reaching_into_node_modules_fails_before_analysis(self):
        leaked = f'{self.repo}/static-src/node_modules/flatted/golang/pkg/flatted'
        run = self.analyze(GO_LIST_OUT=f'{self.repo}\n{self.repo}/cmd\n{leaked}\n')
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn("Add an 'ignore ./<dir>/node_modules' line to go.mod", run.out)
        self.assertIn(f'  {leaked}', run.out)
        self.assertEqual(run.logged('argv'), '')
        self.assertIn('list -e -find -f {{.Dir}} ./...', run.logged('go'))

    def test_a_failed_package_listing_fails_before_analysis(self):
        run = self.analyze(GO_LIST_RC='1')
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error::go list ./... failed at .', run.out)
        self.assertEqual(run.logged('argv'), '')

    def test_a_typescript_only_target_never_calls_go(self):
        run = self.analyze(GO='false', GO_LIST_OUT='/x/node_modules/y\n')
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('go'), '')

    def test_a_configuration_file_below_the_root_fails_and_the_analysis_still_runs(self):
        for name in (
            'deadset.json',
            'deadset-ignore.json',
            'deadset-baseline.json',
            'deadset-edges.json',
        ):
            with self.subTest(name=name):
                shutil.rmtree(self.tmp / 'repo')
                self.repo = git_repo(
                    self.tmp / 'repo',
                    {'go.mod': 'module x\n', 'deadset.json': '{}', f'web/{name}': '{}'},
                )
                run = self.analyze(NPM_DIRS='.\nweb')
                self.assertEqual(run.rc, 1, run.out)
                self.assertIn(f'::error file=web/{name}::web/{name} is never read', run.out)
                self.assertNotIn('file=deadset.json', run.out)
                self.assertNotEqual(run.logged('argv'), '')

    def test_a_misplaced_file_is_named_from_the_repository_root(self):
        self.repo = git_repo(self.tmp / 'repo', {'yamlenv/web/deadset.json': '{}'}) / 'yamlenv'
        run = self.analyze(TARGET='yamlenv', NPM_DIRS='web', GO='false')
        self.assertIn('::error file=yamlenv/web/deadset.json::', run.out)
        self.assertIn('at the target root, yamlenv, only', run.out)


class InstallSteps(Harness):
    def pins(self) -> dict[str, str]:
        return {m['dep']: m['ver'] for m in RENOVATE_PIN.finditer(DEADSET_CI.read_text())}

    def test_deadset_installs_alone_at_its_pin_before_the_census(self):
        pins = self.pins()
        run = self.run_step('Install deadset', self.tmp, {'GO': 'true', 'TS': 'true'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(
            run.logged('go').splitlines(),
            [
                f'install github.com/cplieger/deadset/v2/cmd/deadset@{pins[DEPS[0]]} GOTOOLCHAIN=auto'
            ],
        )
        self.assertEqual(run.logged('npm'), '')
        self.assertIn('GOTOOLCHAIN=auto', run.github_env.splitlines())

    def test_each_analyzer_installs_at_its_pin_only_where_deadset_names_it(self):
        pins = self.pins()
        go_install = f'install github.com/cplieger/deadset-go/v2/cmd/deadset-go@{pins[DEPS[1]]} '
        npm_install = (
            f'install --global --ignore-scripts --no-audit --no-fund {DEPS[2]}@{pins[DEPS[2]]}'
        )
        for name in ('web', 'ui'):
            (self.tmp / name).mkdir()
        env = {'GO': 'true', 'TS': 'true', 'NPM_DIRS': 'web\nui'}
        run = self.run_step(INSTALL_STEP, self.tmp, env)
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(
            run.logged('go').splitlines(),
            [f'{go_install}GOTOOLCHAIN=', 'mod download GOTOOLCHAIN='],
        )
        self.assertEqual(
            [line.split(' ', 1)[1] for line in run.logged('npm').splitlines()],
            [
                npm_install,
                'ci --ignore-scripts --no-audit --no-fund',
                'ci --ignore-scripts --no-audit --no-fund',
            ],
        )
        self.assertEqual(
            [line.split(' ', 1)[0] for line in run.logged('npm').splitlines()[1:]], ['web', 'ui']
        )
        run = self.run_step(INSTALL_STEP, self.tmp, {'GO': 'true', 'TS': 'false', 'NPM_DIRS': ''})
        self.assertEqual(run.logged('npm'), '')
        run = self.run_step(INSTALL_STEP, self.tmp, {'GO': 'false', 'TS': 'true', 'NPM_DIRS': ''})
        self.assertEqual(
            (run.logged('go'), run.logged('npm').split(' ', 1)[1]), ('', f'{npm_install}\n')
        )
        run = self.run_step(INSTALL_STEP, self.tmp, {'GO': 'false', 'TS': 'false', 'NPM_DIRS': ''})
        self.assertEqual((run.rc, run.logged('go'), run.logged('npm')), (0, '', ''))

    def test_a_transient_go_failure_is_retried(self):
        for step in ('Install deadset', INSTALL_STEP):
            with self.subTest(step=step):
                env = {'GO': 'true', 'TS': 'false', 'NPM_DIRS': '', 'RETRY_SLEEP': '0'}
                run = self.run_step(step, self.tmp, {**env, 'GO_FAIL_ONCE': '1'})
                self.assertEqual(run.rc, 0, run.out)
                self.assertIn('attempt 1/4 failed', run.out)
                first = run.logged('go').splitlines()
                self.assertEqual(first[0], first[1])
                run = self.run_step(step, self.tmp, {**env, 'GO_RC': '1'})
                self.assertNotEqual(run.rc, 0)
                self.assertEqual(len(run.logged('go').splitlines()), 4, run.logged('go'))
                self.assertIn('failed after 4 attempts', run.out)

    def test_a_failed_install_stops_the_job(self):
        for env in (
            {'GO': 'false', 'TS': 'false', 'NPM_DIRS': 'x', 'NPM_RC': '1'},
            {'GO': 'false', 'TS': 'true', 'NPM_DIRS': '', 'NPM_RC': '1'},
        ):
            with self.subTest(env=env):
                self.assertNotEqual(self.run_step(INSTALL_STEP, self.tmp, env).rc, 0)

    def test_deadset_is_installed_before_it_is_asked_and_the_analyzers_after(self):
        names = step_names(DEADSET_CI, 'analyze')
        order = [
            'Resolve target',
            'Setup Go',
            'Setup Go (deadset only)',
            'Install deadset',
            CENSUS_STEP,
            'Setup Node.js',
            INSTALL_STEP,
            STEP,
        ]
        self.assertEqual([n for n in names if n in order], order)
        env = analyze_step(INSTALL_STEP)['env']
        self.assertEqual(
            env,
            {
                'GO': '${{ steps.census.outputs.go }}',
                'TS': '${{ steps.census.outputs.ts }}',
                'NPM_DIRS': '${{ steps.census.outputs.npm_dirs }}',
            },
        )
        self.assertEqual(analyze_step(STEP)['env']['GO'], '${{ steps.census.outputs.go }}')
        self.assertEqual(
            analyze_step(STEP)['env']['NPM_DIRS'], '${{ steps.census.outputs.npm_dirs }}'
        )


class Pins(unittest.TestCase):
    def test_the_three_pins_have_one_home_renovate_reads(self):
        found = {
            m['dep']: (m['datasource'], m['var'], m['ver'])
            for m in RENOVATE_PIN.finditer(DEADSET_CI.read_text())
        }
        self.assertEqual(found[DEPS[0]][:2], ('go', 'DEADSET_VERSION'))
        self.assertEqual(found[DEPS[1]][:2], ('go', 'DEADSET_GO_VERSION'))
        self.assertEqual(found[DEPS[2]][:2], ('npm', 'DEADSET_TS_VERSION'))
        env = load(DEADSET_CI)['env']
        for datasource, var, ver in found.values():
            self.assertEqual(env[var], ver)
        for path in WORKFLOWS.glob('*.y*ml'):
            if path == DEADSET_CI:
                continue
            with self.subTest(workflow=path.name):
                text = path.read_text()
                for dep in DEPS:
                    self.assertNotIn(f'depName={dep}\n', text)


def sarif_run(driver: str, lang: str, uri: str) -> dict:
    location = {'physicalLocation': {'artifactLocation': {'uri': uri, 'uriBaseId': '%SRCROOT%'}}}
    related = {
        'physicalLocation': {'artifactLocation': {'uri': f'sub/{uri}', 'uriBaseId': '%SRCROOT%'}}
    }
    return {
        'tool': {'driver': {'name': driver, 'version': '1.0.0', 'semanticVersion': '1.0.0'}},
        'automationDetails': {'id': f'deadset/{lang}/'},
        'originalUriBaseIds': {'%SRCROOT%': {'description': {'text': 'The target root.'}}},
        'results': [{'ruleId': 'DS1002', 'locations': [location], 'relatedLocations': [related]}],
    }


SARIF = {
    'version': '2.1.0',
    'runs': [sarif_run('deadset-go', 'go', 'x.go'), sarif_run('deadset-ts', 'ts', 'web/a.ts')],
}


def run_key(sarif_run: dict) -> str:
    """Two runs with one key in one upload are refused: https://github.com/github/codeql-action/blob/v4.38.3/src/sarif/index.ts (createRunKey)."""
    driver = sarif_run['tool']['driver']
    keys = ('name', 'fullName', 'version', 'semanticVersion', 'guid')
    automation = (sarif_run.get('automationDetails') or {}).get('id')
    return json.dumps([*(driver.get(k) for k in keys), automation])


def uris(sarif_run: dict) -> tuple[str, str]:
    result = sarif_run['results'][0]
    uri = result['locations'][0]['physicalLocation']['artifactLocation']['uri']
    related = result['relatedLocations'][0]['physicalLocation']['artifactLocation']['uri']
    return uri, related


class Sarif(Harness):
    def prepare(self, target: str, sarif: dict | None = SARIF) -> StepRun:
        work = Path(tempfile.mkdtemp(dir=self.runner_temp))
        if sarif is not None:
            (work / 'run').mkdir()
            (work / 'run' / 'report.json.sarif').write_text(json.dumps(sarif))
        run = self.run_step('Prepare SARIF', self.tmp, {'TARGET': target, 'WORK': str(work)})
        self.assertEqual(run.rc, 0, run.out)
        return run

    def test_a_root_report_is_uploaded_under_one_category(self):
        run = self.prepare('.')
        self.assertEqual(run.outputs['category'], 'deadset')
        doc = json.loads(Path(run.outputs['path']).read_text())
        self.assertEqual(len(doc['runs']), 2)
        for got, want in zip(doc['runs'], SARIF['runs'], strict=True):
            self.assertNotIn('automationDetails', got)
            self.assertEqual(uris(got), uris(want))
            self.assertEqual(got['originalUriBaseIds'], want['originalUriBaseIds'])
        self.assertEqual(len({run_key(r) for r in doc['runs']}), 2)

    def test_a_nested_report_gets_its_own_category_and_repository_paths(self):
        run = self.prepare('yamlenv')
        self.assertEqual(run.outputs['category'], 'deadset/yamlenv')
        doc = json.loads(Path(run.outputs['path']).read_text())
        go, ts = doc['runs']
        self.assertNotIn('automationDetails', go)
        self.assertEqual(uris(go), ('yamlenv/x.go', 'yamlenv/sub/x.go'))
        self.assertEqual(uris(ts), ('yamlenv/web/a.ts', 'yamlenv/sub/web/a.ts'))

    def test_a_run_that_ended_without_a_report_uploads_nothing(self):
        self.assertNotIn('path', self.prepare('.', sarif=None).outputs)

    def test_the_upload_never_gates_and_skips_where_it_cannot_land(self):
        prepare = analyze_step('Prepare SARIF')
        self.assertIs(prepare['continue-on-error'], expr2=True)
        self.assertIn('!cancelled()', prepare['if'])
        upload = analyze_step('Upload SARIF (deadset)')
        self.assertIs(upload['continue-on-error'], expr2=True)
        action, _, pin = upload['uses'].partition('@')
        self.assertEqual(action, 'github/codeql-action/upload-sarif')
        self.assertRegex(pin, SHA)
        for clause in (
            '!cancelled()',
            "steps.sarif.outputs.path != ''",
            "inputs.repository == ''",
            '!github.event.repository.private',
        ):
            self.assertIn(clause, upload['if'])
        self.assertEqual(
            upload['with'],
            {
                'sarif_file': '${{ steps.sarif.outputs.path }}',
                'category': '${{ steps.sarif.outputs.category }}',
            },
        )

    def test_the_report_is_requested(self):
        self.assertIn('--formats=text,sarif', analyze_step(STEP)['run'])


NPX_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$STUB_LOG/npx"
exit "${NPX_RC:-0}"
"""

KNIP_CONFIGS = (
    'knip.json',
    'knip.jsonc',
    '.knip.json',
    '.knip.jsonc',
    'knip.ts',
    'knip.js',
    'knip.config.ts',
    'knip.config.js',
)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class KnipStep(Harness):
    def run_knip(self, files: dict[str, str], rc: str = '0') -> StepRun:
        work = self.tmp / 'work'
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        for name, text in files.items():
            (work / name).write_text(text)
        write_exec(self.bin / 'npx', NPX_STUB)
        return self.run_body(workflow_step(TS_CI, KNIP_STEP)['run'], work, {'NPX_RC': rc})

    def test_the_step_is_soft_gated_and_runs_after_the_install(self):
        self.assertIs(workflow_step(TS_CI, KNIP_STEP).get('continue-on-error'), expr2=True)
        names = step_names(TS_CI)
        self.assertLess(names.index('Install deps'), names.index(KNIP_STEP))

    def test_every_config_location_enrolls_and_runs_only_the_four_checks(self):
        argv = ['--no-install', 'knip', '--include', 'cycles,unlisted,binaries,duplicates']
        argv.append('--no-config-hints')
        for name in KNIP_CONFIGS:
            with self.subTest(config=name):
                run = self.run_knip({'package.json': '{"name": "x"}', name: '{}'})
                self.assertEqual(run.rc, 0, run.out)
                self.assertEqual(run.logged('npx').splitlines(), argv)
        run = self.run_knip({'package.json': '{"name": "x", "knip": {}}'})
        self.assertEqual(run.logged('npx').splitlines(), argv)

    def test_a_package_without_a_config_skips(self):
        run = self.run_knip({'package.json': '{"name": "x"}'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('npx'), '')
        self.assertIn('no knip config; skipping', run.out)

    def test_a_finding_fails_and_records_the_step(self):
        run = self.run_knip({'package.json': '{"name": "x"}', 'knip.json': '{}'}, rc='1')
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.recorded, 'knip\n')


class Replaced(unittest.TestCase):
    def test_no_language_workflow_runs_a_retired_tool(self):
        retired = re.compile(r'punused|deadcode|gopls|knip|XTOOLS_VERSION', re.IGNORECASE)
        self.assertEqual(retired.findall(GO_CI.read_text()), [])
        for step in load(TS_CI)['jobs']['validate']['steps']:
            if step.get('name') == KNIP_STEP:
                continue
            with self.subTest(step=step.get('name')):
                self.assertEqual(retired.findall(yaml.safe_dump(step)), [])

    def test_golangci_drops_only_the_linters_deadset_gates(self):
        linters = load(ROOT / '.golangci.yaml')['linters']
        self.assertEqual(linters['default'], 'standard')
        self.assertEqual(sorted(linters['disable']), ['ineffassign', 'unused'])
        self.assertNotIn('wastedassign', linters['enable'])
        self.assertIn('unparam', linters['enable'])

    def test_eslint_keeps_no_unused_vars(self):
        base = (ROOT / 'configs' / 'eslint.config.base.mjs').read_text()
        self.assertIn('"@typescript-eslint/no-unused-vars": [\n        "error"', base)


if __name__ == '__main__':
    unittest.main()
