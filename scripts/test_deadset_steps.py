from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
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
DEPS = ('github.com/cplieger/deadset', 'github.com/cplieger/deadset-go', '@cplieger/deadset-ts')

# The generic workflow pin manager of the shared Renovate preset
# (cplieger/.github default.json), which is what bumps these pins.
RENOVATE_PIN = re.compile(
    r'#\s*renovate:\s*datasource=(?P<datasource>[a-z-]+)\s+depName=(?P<dep>[^\s]+)'
    r'(\s+versioning=[a-z-]+)?\s*\n\s*(?P<var>[A-Z_]*VERSION)[=:]\s*[\'"]?(?P<ver>[^\'"\s]+)'
)

DEADSET_STUB = """#!/usr/bin/env bash
if [ "$1" = print-config ]; then
  printf '%s\\n' "$@" > "$STUB_LOG/print-config"
  echo '{"target":{"kind":"library"},"analysis":{"languages":["go"]},'\\
'"provenance":{"target.kind":"central","analysis.languages":"central"}}'
  exit 0
fi
printf '%s\\n' "$@" > "$STUB_LOG/argv"
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
TS_ONLY = {'jsr.json': '{}', 'package.json': '{}', 'package-lock.json': '{}', 'mod.ts': ''}
IMAGE_ONLY = {'Dockerfile': 'FROM scratch\n', 'run.sh': '#!/bin/sh\n'}
TS_WITHOUT_JSR = {'package.json': '{}', 'package-lock.json': '{}', 'tsconfig.json': '{}'}
ROOT_TARGET_CASES = (
    (TS_WITHOUT_JSR, True),
    ({'package.json': '{}', 'src/a.mts': ''}, True),
    ({'cmd/x.go': ''}, True),
    ({'README.md': 'x\n', 'deadset.json': '{"analysis":{"languages":["ts"]}}'}, True),
    ({'README.md': 'x\n', 'tsconfig.json': Link('README.md')}, True),
    ({'conf/x': '', 'lib.go': Link('conf')}, True),
    ({'README.md': 'x\n', 'tools/a.ts': ''}, False),
    ({'README.md': 'x\n', 'node_modules/p/tsconfig.json': '{}'}, False),
    ({'README.md': 'x\n', 'testdata/m/go.mod': 'module m\n'}, False),
    ({'README.md': 'x\n', '.github/t/tsconfig.json': '{}'}, False),
    ({'static-src/package.json': '{}', 'static-src/app.js': ''}, False),
)


class Detect(Harness):
    def test_a_mixed_repository_gets_one_root_target(self):
        out = run_detect(self, MIXED_WEB)
        self.assertEqual(json.loads(out['deadset_targets']), ['.'])
        self.assertEqual(out['run_deadset'], 'true')
        self.assertEqual(out['code_changed'], 'true')

    def test_a_nested_module_is_its_own_target_and_a_sentinel_is_not(self):
        out = run_detect(self, NESTED)
        self.assertEqual(json.loads(out['deadset_targets']), ['.', 'yamlenv'])

    def test_a_typescript_root_is_a_target(self):
        self.assertEqual(json.loads(run_detect(self, TS_ONLY)['deadset_targets']), ['.'])

    def test_the_root_is_a_target_exactly_when_deadset_finds_a_language_there(self):
        sys.path.insert(0, str(ROOT))
        import _ci_local

        for i, (files, expected) in enumerate(ROOT_TARGET_CASES):
            with self.subTest(case=i, files=sorted(files)):
                want = ['.'] if expected else []
                self.assertEqual(json.loads(run_detect(self, files)['deadset_targets']), want)
                repo = git_repo(self.tmp / f'local-{i}', files)
                self.assertEqual(
                    json.loads(_ci_local.compute_local_detect(repo)['deadset_targets']), want
                )

    def test_a_root_target_is_one_the_check_accepts(self):
        for i, (files, expected) in enumerate(ROOT_TARGET_CASES):
            if not expected:
                continue
            with self.subTest(case=i, files=sorted(files)):
                repo = git_repo(self.tmp / f'resolve-{i}', files)
                run = self.run_step('Resolve target', repo, {'TARGET': '.'})
                self.assertNotIn('nothing to analyze', run.out)

    def test_a_repository_without_go_or_typescript_runs_no_check(self):
        out = run_detect(self, IMAGE_ONLY)
        self.assertEqual(out['deadset_targets'], '[]')
        self.assertEqual(out['run_deadset'], 'false')


class MetaJobs(unittest.TestCase):
    jobs = load(META)['jobs']

    def test_one_deadset_job_per_target_calls_the_check(self):
        job = self.jobs['deadset']
        self.assertEqual(job['uses'], './.github/workflows/deadset-ci.yaml')
        self.assertEqual(job['needs'], 'detect')
        self.assertIn("needs.detect.outputs.run_deadset == 'true'", job['if'])
        self.assertEqual(
            job['strategy']['matrix']['target'],
            '${{ fromJSON(needs.detect.outputs.deadset_targets) }}',
        )
        self.assertIs(job['strategy']['fail-fast'], expr2=False)
        self.assertEqual(job['with'], {'target': '${{ matrix.target }}'})

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
    'TS',
    'WEB',
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
        return self.run_step('Resolve target', repo / target, {'TARGET': target})

    def central(self, run: StepRun) -> dict:
        self.assertEqual(run.rc, 0, run.out)
        return json.loads((Path(run.outputs['work']) / 'central.json').read_text())

    def languages(self, run: StepRun) -> tuple[str, str, str, str]:
        self.assertEqual(run.rc, 0, run.out)
        out = run.outputs
        return out['languages'], out['languages_from'], out['go'], out['ts']

    def test_the_central_file_carries_only_the_kind(self):
        run = self.resolve({'go.mod': 'module x\n', 'cmd/tool/main.go': 'package main\n'})
        self.assertEqual(self.central(run), {'target': {'kind': 'library'}})
        self.assertEqual(self.languages(run), ('go', 'detected', 'true', 'false'))
        self.assertEqual(run.outputs['npm_dirs'], '')

    def test_a_dockerfile_makes_an_application(self):
        run = self.resolve({'go.mod': 'module x\n', 'Dockerfile': 'FROM scratch\n'})
        self.assertEqual(self.central(run), {'target': {'kind': 'application'}})

    def test_languages_are_detected_the_way_deadset_detects_them(self):
        lock = {'web/package-lock.json': '{}'}
        cases = (
            ({'go.mod': 'module x\n'}, 'go'),
            ({'go.mod': 'module x\n', 'cmd/x.go': 'package main\n'}, 'go'),
            ({**lock, 'web/tsconfig.app.json': '{}'}, 'ts'),
            ({**lock, 'web/src/a.mts': '', 'web/package.json': '{}'}, 'ts'),
            ({'go.mod': 'module x\n', **lock, 'web/tsconfig.json': '{}'}, 'go,ts'),
            ({'go.mod': 'module x\n', 'tools/a.ts': ''}, 'go'),
            ({'go.mod': 'module x\n', 'node_modules/p/tsconfig.json': '{}'}, 'go'),
            ({'go.mod': 'module x\n', 'testdata/f/tsconfig.json': '{}'}, 'go'),
            ({'go.mod': 'module x\n', 'vendor/v/tsconfig.json': '{}'}, 'go'),
            ({'go.mod': 'module x\n', '.github/t/tsconfig.json': '{}'}, 'go'),
            ({'go.mod': 'module x\n', **lock, 'web/tsconfig.json': Link('../go.mod')}, 'go,ts'),
            (
                {'go.mod': 'module x\n', **lock, 'conf/x': '', 'tsconfig.b.json': Link('conf')},
                'go,ts',
            ),
        )
        for i, (files, expected) in enumerate(cases):
            with self.subTest(case=i, files=sorted(files)):
                run = self.resolve(files)
                self.assertEqual(self.languages(run)[:2], (expected, 'detected'))

    def test_the_repository_file_decides_the_languages(self):
        files = {
            'go.mod': 'module x\n',
            'corpus/fixtures/a/tsconfig.json': '{}',
            'deadset.json': '{"analysis":{"languages":["go"]}}',
        }
        run = self.resolve(files)
        self.assertEqual(self.languages(run), ('go', 'repository: deadset.json', 'true', 'false'))
        self.assertEqual(self.central(run), {'target': {'kind': 'library'}})
        files['deadset.json'] = '{"target":{"kind":"application"}}'
        files['corpus/fixtures/a/package-lock.json'] = '{}'
        self.assertEqual(self.languages(self.resolve(files))[:2], ('go,ts', 'detected'))

    def test_a_language_whose_setup_cannot_be_derived_fails(self):
        run = self.resolve({'go.mod': 'module x\n', 'web/tsconfig.json': '{}'})
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error::TypeScript is in scope at . (detected)', run.out)
        self.assertIn('no package-lock.json is tracked', run.out)
        run = self.resolve({'package-lock.json': '{}', 'tsconfig.json': '{}', 'tool/x.go': ''})
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error::Go is in scope at . (detected), but . has no go.mod', run.out)
        run = self.resolve(
            {'go.mod': 'module x\n', 'deadset.json': '{"analysis":{"languages":["ts"]}}'}
        )
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('TypeScript is in scope at . (repository: deadset.json)', run.out)

    def test_only_tracked_lockfiles_outside_vendored_trees_count(self):
        run = self.resolve(
            {
                'go.mod': 'module x\n',
                'web/tsconfig.json': '{}',
                'web/package-lock.json': '{}',
                'node_modules/x/package-lock.json': '{}',
                'web/node_modules/y/package-lock.json': '{}',
                'testdata/y/package-lock.json': '{}',
                'vendor/z/package-lock.json': '{}',
                '.cache/w/package-lock.json': '{}',
            }
        )
        self.assertEqual(self.languages(run)[0], 'go,ts')
        self.assertEqual(run.outputs['npm_dirs'], 'web')
        self.assertEqual(run.outputs['lockfiles'], 'web/package-lock.json')
        untracked = self.tmp / 'repo' / 'ui' / 'package-lock.json'
        untracked.parent.mkdir()
        untracked.write_text('{}')
        run = self.run_step('Resolve target', self.tmp / 'repo', {'TARGET': '.'})
        self.assertEqual(run.outputs['npm_dirs'], 'web')

    def test_a_root_project_caches_on_its_own_lockfile(self):
        run = self.resolve({'package-lock.json': '{}', 'tsconfig.json': '{}'})
        self.assertEqual(self.languages(run), ('ts', 'detected', 'false', 'true'))
        self.assertEqual(
            (run.outputs['npm_dirs'], run.outputs['lockfiles']), ('.', 'package-lock.json')
        )

    def test_no_npm_project_is_installed_when_typescript_is_out_of_scope(self):
        run = self.resolve(
            {
                'go.mod': 'module x\n',
                'web/tsconfig.json': '{}',
                'web/package-lock.json': '{}',
                'deadset.json': '{"analysis":{"languages":["go"]}}',
            }
        )
        self.assertEqual(self.languages(run)[2:], ('true', 'false'))
        self.assertEqual((run.outputs['npm_dirs'], run.outputs['lockfiles']), ('', ''))

    def test_a_root_with_neither_language_fails(self):
        run = self.resolve({'README.md': 'x\n'})
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error::nothing to analyze at .', run.out)

    def test_a_nested_target_resolves_inside_its_module_and_analyzes_its_go(self):
        files = {
            'go.mod': 'module x\n',
            'Dockerfile': 'FROM scratch\n',
            'package-lock.json': '{}',
            'tsconfig.json': '{}',
            'yamlenv/go.mod': 'module x/yamlenv\n',
            'yamlenv/go.sum': '',
            'yamlenv/web/tsconfig.json': '{}',
            'yamlenv/web/package-lock.json': '{}',
        }
        run = self.resolve(files, target='yamlenv')
        self.assertEqual(
            self.central(run), {'target': {'kind': 'library'}, 'analysis': {'languages': ['go']}}
        )
        self.assertEqual(self.languages(run), ('go', 'central: nested module', 'true', 'false'))
        self.assertEqual((run.outputs['npm_dirs'], run.outputs['lockfiles']), ('', ''))
        self.assertEqual(run.outputs['go_sum'], 'yamlenv/go.sum')
        root = self.resolve(files)
        self.assertEqual(self.languages(root)[:2], ('go,ts', 'detected'))
        self.assertEqual(root.outputs['npm_dirs'], '.\nyamlenv/web')
        files['yamlenv/deadset.json'] = '{"analysis":{"languages":["go","ts"]}}'
        run = self.resolve(files, target='yamlenv')
        self.assertEqual(self.languages(run), ('go,ts', 'repository: deadset.json', 'true', 'true'))
        self.assertEqual(self.central(run), {'target': {'kind': 'library'}})
        self.assertEqual(run.outputs['lockfiles'], 'yamlenv/web/package-lock.json')

    def test_the_go_cache_keys_on_the_target_go_sum(self):
        self.assertEqual(
            self.resolve({'go.mod': 'module x\n', 'go.sum': ''}).outputs['go_sum'], 'go.sum'
        )
        self.assertEqual(self.resolve({'go.mod': 'module x\n'}).outputs['go_sum'], '')
        run = self.resolve({'package-lock.json': '{}', 'tsconfig.json': '{}', 'go.sum': ''})
        self.assertEqual(run.outputs['go_sum'], '')
        setup = analyze_step('Setup Go')['with']
        self.assertEqual(setup['cache'], "${{ steps.target.outputs.go_sum != '' }}")
        self.assertEqual(setup['cache-dependency-path'], '${{ steps.target.outputs.go_sum }}')

    def test_the_local_plan_predicts_what_the_step_resolves(self):
        sys.path.insert(0, str(ROOT))
        import _ci_local

        fixtures = (
            ({'go.mod': 'module x\n'}, '.'),
            ({'package-lock.json': '{}', 'src/a.ts': '', 'package.json': '{}'}, '.'),
            (
                {
                    'go.mod': 'module x\n',
                    'web/tsconfig.json': '{}',
                    'web/package-lock.json': '{}',
                    'web/node_modules/y/package-lock.json': '{}',
                    '.cache/w/package-lock.json': '{}',
                    'testdata/t/package-lock.json': '{}',
                    'testdata/t/tsconfig.json': '{}',
                },
                '.',
            ),
            (
                {
                    'go.mod': 'module x\n',
                    'web/tsconfig.json': '{}',
                    'web/package-lock.json': '{}',
                    'deadset.json': '{"analysis":{"languages":["go"]}}',
                },
                '.',
            ),
            (
                {
                    'yamlenv/go.mod': 'module y\n',
                    'yamlenv/ui/tsconfig.json': '{}',
                    'yamlenv/ui/package-lock.json': '{}',
                },
                'yamlenv',
            ),
            (
                {
                    'go.mod': 'module x\n',
                    'package-lock.json': '{}',
                    'conf/x': '',
                    'tsconfig.base.json': Link('conf'),
                },
                '.',
            ),
        )
        for i, (files, target) in enumerate(fixtures):
            with self.subTest(fixture=i):
                run = self.resolve(files, target)
                self.assertEqual(run.rc, 0, run.out)
                plan = _ci_local.predict_profile_outputs(
                    analyze_step('Resolve target'), self.tmp / 'repo' / target, self.tmp / 'repo'
                )
                for key in ('go', 'ts', 'npm_dirs'):
                    self.assertEqual(plan[key], run.outputs[key], key)

    def test_the_job_runs_at_the_target_and_reads_its_go_mod(self):
        job = load(DEADSET_CI)['jobs']['analyze']
        self.assertEqual(job['defaults']['run']['working-directory'], '${{ inputs.target }}')
        self.assertEqual(
            analyze_step('Setup Go')['with']['go-version-file'], '${{ inputs.target }}/go.mod'
        )
        node = analyze_step('Setup Node.js')['with']
        self.assertEqual(node['cache-dependency-path'], '${{ steps.target.outputs.lockfiles }}')


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
            'GO': 'true',
            'NPM_DIRS': '',
            'LANGUAGES': 'go',
            'LANGUAGES_FROM': 'detected',
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
        after_setup = names[names.index('Install dependencies') + 1 :]
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

    def test_the_resolved_precedence_is_printed(self):
        run = self.analyze()
        self.assertIn('target.kind library (central); analysis.languages go (detected)', run.out)
        run = self.analyze(LANGUAGES='go,ts', LANGUAGES_FROM='repository: deadset.json')
        self.assertIn('analysis.languages go,ts (repository: deadset.json)', run.out)
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

    def test_a_missing_deadset_fails_and_names_the_installer(self):
        run = self.analyze(deadset=False)
        self.assert_failed(run, 'is not on PATH')
        self.assertIn('install-local-tools.sh', run.out)

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
        for name in ('deadset.json', 'deadset-ignore.json', 'deadset-edges.json'):
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

    def test_each_tool_installs_at_its_pin_and_only_where_its_language_is(self):
        pins = self.pins()
        run = self.run_step('Install deadset', self.tmp, {'GO': 'true', 'TS': 'true'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(
            run.logged('go').splitlines(),
            [
                f'install github.com/cplieger/deadset/cmd/deadset@{pins[DEPS[0]]} GOTOOLCHAIN=auto',
                (
                    f'install github.com/cplieger/deadset-go/cmd/deadset-go@{pins[DEPS[1]]} '
                    'GOTOOLCHAIN=auto'
                ),
            ],
        )
        self.assertEqual(
            run.logged('npm').split(' ', 1)[1],
            f'install --global --ignore-scripts --no-audit --no-fund {DEPS[2]}@{pins[DEPS[2]]}\n',
        )
        self.assertIn('GOTOOLCHAIN=auto', run.github_env.splitlines())
        run = self.run_step('Install deadset', self.tmp, {'GO': 'false', 'TS': 'false'})
        self.assertEqual(len(run.logged('go').splitlines()), 1, run.logged('go'))
        self.assertEqual(run.logged('npm'), '')

    def test_dependencies_are_installed_for_every_project(self):
        for name in ('web', 'ui'):
            (self.tmp / name).mkdir()
        run = self.run_step('Install dependencies', self.tmp, {'GO': 'true', 'NPM_DIRS': 'web\nui'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('go'), 'mod download GOTOOLCHAIN=\n')
        self.assertEqual(
            run.logged('npm').splitlines(),
            [
                'web ci --ignore-scripts --no-audit --no-fund',
                'ui ci --ignore-scripts --no-audit --no-fund',
            ],
        )
        run = self.run_step('Install dependencies', self.tmp, {'GO': 'false', 'NPM_DIRS': ''})
        self.assertEqual((run.rc, run.logged('go'), run.logged('npm')), (0, '', ''))

    def test_a_failed_install_stops_the_job(self):
        run = self.run_step(
            'Install dependencies', self.tmp, {'GO': 'false', 'NPM_DIRS': 'x', 'NPM_RC': '1'}
        )
        self.assertNotEqual(run.rc, 0)

    def test_the_setup_follows_the_resolved_languages(self):
        self.assertEqual(analyze_step('Setup Go')['if'], "${{ steps.target.outputs.go == 'true' }}")
        self.assertEqual(
            analyze_step('Setup Go (deadset only)')['if'],
            "${{ steps.target.outputs.go != 'true' }}",
        )
        self.assertEqual(
            analyze_step('Setup Node.js')['if'], "${{ steps.target.outputs.ts == 'true' }}"
        )
        names = step_names(DEADSET_CI, 'analyze')
        self.assertLess(names.index('Resolve target'), names.index('Setup Go'))
        self.assertLess(names.index('Setup Node.js'), names.index('Install deadset'))
        self.assertLess(names.index('Install dependencies'), names.index(STEP))


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


class LocalMirror(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        import _ci_local

        cls.ci_local = _ci_local

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='deadset-local-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.ci_local._DETECT_CACHE.clear)  # noqa: SLF001

    def test_local_detection_matches_the_ci_detect_body(self):
        harness = Detect('test_a_mixed_repository_gets_one_root_target')
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        for i, files in enumerate((MIXED_WEB, NESTED, TS_ONLY, IMAGE_ONLY)):
            with self.subTest(fixture=i):
                ci = run_detect(harness, files)
                local = self.ci_local.compute_local_detect(git_repo(self.tmp / str(i), files))
                for key in ('deadset_targets', 'run_deadset', 'code_changed'):
                    self.assertEqual(json.loads(local[key]), json.loads(ci[key]), key)

    def test_the_check_runs_once_per_target_and_the_canary_never_runs(self):
        os.symlink(ROOT, self.tmp / 'ci')
        job = load(META)['jobs']['deadset']
        cases = ((GO_ONLY, ['ci/deadset[.]/analyze']), (IMAGE_ONLY, []))
        cases += ((NESTED, ['ci/deadset[.]/analyze', 'ci/deadset[yamlenv]/analyze']),)
        for i, (files, names) in enumerate(cases):
            with self.subTest(fixture=i):
                repo = git_repo(self.tmp / f'r{i}', files)
                expanded = self.ci_local._expand_job('ci/deadset', job, {}, repo)  # noqa: SLF001
                self.assertEqual([name for name, *_ in expanded], names)
                targets = [name.split('[')[1].split(']')[0] for name in names]
                self.assertEqual([wd for _, _, wd, _ in expanded], targets)
        applies = self.ci_local.job_applies_locally
        image = git_repo(self.tmp / 'image', IMAGE_ONLY)
        self.assertFalse(applies('ci/deadset/analyze', image))
        self.assertTrue(applies('ci/deadset/analyze', git_repo(self.tmp / 'go', GO_ONLY)))
        self.assertFalse(applies('ci/deadset-canary/analyze', self.tmp / 'go'))

    def test_an_include_matrix_is_left_unexpanded(self):
        job = {'strategy': {'matrix': {'include': [{'a': 1}, {'a': 2}]}}, 'steps': []}
        expanded = self.ci_local._expand_job('deadset-canary', job, None, self.tmp)  # noqa: SLF001
        self.assertEqual([name for name, *_ in expanded], ['deadset-canary'])

    def test_the_preflight_reads_the_env_pins_where_the_check_applies(self):
        os.symlink(ROOT, self.tmp / 'ci')
        meta = {'jobs': {'deadset': {'uses': './.github/workflows/deadset-ci.yaml'}}}
        chain = self.ci_local._workflow_env_chain  # noqa: SLF001
        go = git_repo(self.tmp / 'go', GO_ONLY)
        pins = self.ci_local.collect_pinned_versions([], chain(meta, go))
        env = load(DEADSET_CI)['env']
        self.assertEqual(pins['deadset'][0], env['DEADSET_VERSION'].removeprefix('v'))
        self.assertEqual(pins['deadset-go'][0], env['DEADSET_GO_VERSION'].removeprefix('v'))
        self.assertEqual(pins['deadset-ts'][0], env['DEADSET_TS_VERSION'])
        image = git_repo(self.tmp / 'image', IMAGE_ONLY)
        self.assertEqual(chain(meta, image), [])

    def test_the_canary_is_reported_as_not_validated_in_cplieger_ci(self):
        os.symlink(ROOT, self.tmp / 'ci')
        repo = git_repo(self.tmp / 'repo', GO_ONLY)
        canary = load(META)['jobs']['deadset-canary']
        path = self.tmp / 'wf.yaml'
        path.write_text(yaml.safe_dump({'jobs': {'deadset-canary': canary}}))
        report = self.ci_local.REPORT
        for repository, expected in (('cplieger/ci', 1), ('cplieger/marotte', 0)):
            with self.subTest(repository=repository):
                before = len(report.not_validated)
                old = os.environ.get('GITHUB_REPOSITORY')
                os.environ['GITHUB_REPOSITORY'] = repository
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.ci_local.process_workflow_file(
                            path, repo, dry_run=True, ignore_unknown=False, no_codeql=True
                        )
                finally:
                    if old is None:
                        os.environ.pop('GITHUB_REPOSITORY')
                    else:
                        os.environ['GITHUB_REPOSITORY'] = old
                added = report.not_validated[before:]
                del report.not_validated[before:]
                self.assertEqual(
                    len([e for e in added if e[0].startswith('deadset-canary')]), expected, added
                )

    def test_each_installed_version_is_read_the_way_the_tool_prints_it(self):
        tools = self.ci_local._GO_INSTALLED  # noqa: SLF001
        self.assertLessEqual({'deadset', 'deadset-go'}, tools)
        self.assertEqual(self.ci_local._VERSION_ARGV['deadset-ts'], ['version'])  # noqa: SLF001
        write_exec(
            self.tmp / 'deadset-ts',
            '#!/bin/sh\n[ "$1" = version ] || exit 2\necho "deadset-ts 6.0.1"\n'
            'echo "contract 6.0.0"\n',
        )
        old_path = os.environ['PATH']
        os.environ['PATH'] = f'{self.tmp}{os.pathsep}{old_path}'
        self.addCleanup(os.environ.__setitem__, 'PATH', old_path)
        self.ci_local._LOCAL_VERSION_CACHE.pop('deadset-ts', None)  # noqa: SLF001
        self.addCleanup(self.ci_local._LOCAL_VERSION_CACHE.pop, 'deadset-ts', None)  # noqa: SLF001
        self.assertEqual(self.ci_local._local_tool_version('deadset-ts'), '6.0.1')  # noqa: SLF001


INSTALLER_NPM_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG/npm"
exit "${NPM_RC:-0}"
"""

INSTALLER_GO_STUB = """#!/usr/bin/env bash
printf '%s GOTOOLCHAIN=%s\\n' "$*" "${GOTOOLCHAIN:-}" >> "$STUB_LOG/go"
"""

INSTALLER_DRIVER = """set -euo pipefail
. "$INSTALLER_BODY"
WF_DIR="$WF_DIR_UNDER_TEST"
for f in $(declare -F | awk '{print $3}'); do
  [ "$f" = "$KEEP" ] && continue
  case "$f" in
    install_* | advise_gotoolchain) eval "$f() { :; }" ;;
  esac
done
main
"""


class LocalInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='deadset-installer-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.log = self.tmp / 'log'
        for d in (self.bin, self.log):
            d.mkdir()
        for tool in ('bash', 'awk', 'grep', 'head', 'dirname', 'cat'):
            found = shutil.which(tool)
            self.assertIsNotNone(found, f'{tool} is needed to run the installer')
            (self.bin / tool).symlink_to(found)
        text = (ROOT / 'scripts' / 'install-local-tools.sh').read_text().rstrip()
        self.assertTrue(text.endswith('\nmain "$@"'), 'the installer no longer ends in main "$@"')
        self.body = self.tmp / 'installer.sh'
        self.body.write_text(text.removesuffix('main "$@"'))
        self.driver = self.tmp / 'driver.sh'
        self.driver.write_text(INSTALLER_DRIVER)
        self.env = load(DEADSET_CI)['env']
        self.pin = self.env['DEADSET_TS_VERSION']

    def install(
        self,
        installed: str | None = None,
        npm: bool = True,
        wf_dir: Path | None = None,
        npm_rc: str = '0',
        keep: str = 'install_deadset_ts',
    ) -> tuple[int, str, str]:
        if npm:
            write_exec(self.bin / 'npm', INSTALLER_NPM_STUB)
        write_exec(self.bin / 'go', INSTALLER_GO_STUB)
        if installed is not None:
            write_exec(
                self.bin / 'deadset-ts',
                f'#!/bin/sh\n[ "$1" = version ] || exit 2\necho "deadset-ts {installed}"\n',
            )
        proc = subprocess.run(
            [str(self.bin / 'bash'), '--noprofile', '--norc', str(self.driver)],
            env={
                'PATH': str(self.bin),
                'HOME': str(self.tmp),
                'STUB_LOG': str(self.log),
                'NPM_RC': npm_rc,
                'KEEP': keep,
                'INSTALLER_BODY': str(self.body),
                'WF_DIR_UNDER_TEST': str(wf_dir or WORKFLOWS),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        tool = 'go' if keep == 'install_go_tools' else 'npm'
        tool_log = self.log / tool
        return (
            proc.returncode,
            proc.stdout + proc.stderr,
            tool_log.read_text() if tool_log.exists() else '',
        )

    def summary_line(self, version: str, status: str) -> re.Pattern[str]:
        return re.compile(
            rf'^  deadset-ts\s+{re.escape(version)}\s+{re.escape(status)}$', re.MULTILINE
        )

    def test_the_go_tools_install_at_the_workflow_pins(self):
        rc, out, go = self.install(keep='install_go_tools')
        self.assertEqual(rc, 0, out)
        installs = go.splitlines()
        for pkg, var in (
            ('github.com/cplieger/deadset/cmd/deadset', 'DEADSET_VERSION'),
            ('github.com/cplieger/deadset-go/cmd/deadset-go', 'DEADSET_GO_VERSION'),
        ):
            self.assertIn(f'install {pkg}@{self.env[var]} GOTOOLCHAIN=auto', installs)
        self.assertTrue(any('golang.org/x/vuln/cmd/govulncheck@v' in i for i in installs), go)
        self.assertNotIn('FAILED', out)

    def test_main_installs_the_pinned_build_without_install_scripts(self):
        rc, out, npm = self.install()
        self.assertEqual(rc, 0, out)
        self.assertEqual(npm, f'install -g --ignore-scripts @cplieger/deadset-ts@{self.pin}\n')
        self.assertRegex(out, self.summary_line(self.pin, 'npm -g'))

    def test_the_version_comes_from_the_workflow_pin(self):
        wf = self.tmp / 'workflows'
        wf.mkdir()
        (wf / 'deadset-ci.yaml').write_text(
            'env:\n  # renovate: datasource=npm depName=@cplieger/deadset-ts\n'
            '  DEADSET_TS_VERSION: 9.8.7\n'
        )
        rc, out, npm = self.install(wf_dir=wf)
        self.assertEqual(rc, 0, out)
        self.assertEqual(npm, 'install -g --ignore-scripts @cplieger/deadset-ts@9.8.7\n')

    def test_a_current_build_is_left_alone(self):
        rc, out, npm = self.install(installed=self.pin)
        self.assertEqual(rc, 0, out)
        self.assertEqual(npm, '')
        self.assertRegex(out, self.summary_line(self.pin, 'already current'))

    def test_a_stale_build_is_replaced(self):
        rc, out, npm = self.install(installed='0.0.1')
        self.assertEqual(rc, 0, out)
        self.assertEqual(npm, f'install -g --ignore-scripts @cplieger/deadset-ts@{self.pin}\n')

    def test_each_failure_fails_the_run_and_names_deadset_ts(self):
        empty = self.tmp / 'no-workflows'
        empty.mkdir()
        unpinned = self.tmp / 'unpinned-workflows'
        unpinned.mkdir()
        (unpinned / 'deadset-ci.yaml').write_text('DEADSET_TS_VERSION: 1.2.3\n')
        cases = (
            ('npm failed', {'npm_rc': '1'}),
            ('npm not found', {'npm': False}),
            ('no pin found', {'wf_dir': unpinned}),
            ('no pin found', {'wf_dir': empty}),
        )
        for reason, kwargs in cases:
            with self.subTest(reason=reason, **{k: str(v) for k, v in kwargs.items()}):
                (self.bin / 'npm').unlink(missing_ok=True)
                rc, out, _ = self.install(**kwargs)
                self.assertEqual(rc, 1, out)
                self.assertRegex(out, self.summary_line('-', f'FAILED: {reason}'))
                self.assertIn('WARNING: 1 tool(s) not installed: deadset-ts', out)


if __name__ == '__main__':
    unittest.main()
