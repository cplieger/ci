"""The shared steps that run a package's own declared script when it has one.

Each step body runs under bash in a fixture package, with real node and npm (so
`npm run` resolves and forwards arguments as it does on the runner) and stubs for
everything that would install, download or lint.
"""

from __future__ import annotations

import json
import os
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
MARKER = '/tmp/_ci_failures'
NODE = shutil.which('node')
NPM = shutil.which('npm')

# Logs how the step started it, then writes a schema report whose counts the
# step's metrics parse turns into a score.
STRYKER_STUB = """#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
fs.writeFileSync(path.join(process.env.STUB_LOG, 'stryker.json'), JSON.stringify({
  argv: process.argv.slice(2),
  execArgv: process.execArgv,
  nodeOptions: process.env.NODE_OPTIONS || '',
  githubActions: process.env.GITHUB_ACTIONS,
  preloaded: Object.keys(require.cache),
}));
const killed = Number(process.env.STRYKER_KILLED ?? 3);
const mutants = [];
for (let i = 0; i < killed; i++) mutants.push({ status: 'Killed' });
mutants.push({ status: 'Survived' });
fs.mkdirSync('reports/mutation', { recursive: true });
fs.writeFileSync('reports/mutation/mutation.json',
  JSON.stringify({ schemaVersion: '2', files: { 'a.ts': { mutants } } }));
process.exit(Number(process.env.STRYKER_RC ?? 0));
"""

# `npm ci` is the only npm call that would touch the network; the rest is real npm.
NPM_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG/npm"
if [ "$1" = ci ]; then exit "${NPM_CI_RC:-0}"; fi
exec "$REAL_NPM" "$@"
"""

NPX_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG/npx"
case "$*" in
  '--no-install stryker --version') exit "${NPX_STRYKER_RC:-0}" ;;
  stylelint*) exit "${NPX_STYLELINT_RC:-0}" ;;
  html-validate*) exit "${NPX_HTMLVALIDATE_RC:-0}" ;;
esac
exit 0
"""

PATCH_STUB = 'print("stub patch applied")\n'


def workflow_step(path: Path, job: str, name: str) -> dict:
    for step in yaml.safe_load(path.read_text())['jobs'][job]['steps']:
        if step.get('name') == name:
            return step
    raise AssertionError(f'{path.name} {job} has no step named {name!r}')


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def read_outputs(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)


class StepRun:
    def __init__(self, proc: subprocess.CompletedProcess, tmp: Path):
        self.rc = proc.returncode
        self.out = proc.stdout + proc.stderr
        self.outputs = read_outputs(tmp / 'github_output')
        marker = tmp / 'ci_failures'
        self.recorded = marker.read_text() if marker.exists() else ''
        self.log = tmp / 'log'

    def logged(self, name: str) -> str:
        path = self.log / name
        return path.read_text() if path.exists() else ''

    def stryker(self) -> dict:
        text = self.logged('stryker.json')
        return json.loads(text) if text else {}


@unittest.skipUnless(NODE and NPM, 'node and npm are not installed')
class Harness(unittest.TestCase):
    STEP: dict

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='package-script-step-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.pkg = self.tmp / 'pkg'
        self.workspace = self.tmp / 'workspace'
        for d in (self.bin, self.pkg, self.tmp / 'log', self.tmp / 'runner', self.workspace):
            d.mkdir()
        write_exec(self.bin / 'npm', NPM_STUB)
        write_exec(self.bin / 'npx', NPX_STUB)
        (self.bin / 'node').symlink_to(NODE)
        (self.bin / 'python3').symlink_to(sys.executable)

    def package(self, scripts: dict[str, str] | None = None) -> None:
        manifest = {'name': 'fixture', 'version': '0.0.0', 'private': True}
        if scripts is not None:
            manifest['scripts'] = scripts
        (self.pkg / 'package.json').write_text(json.dumps(manifest))

    def run_step(self, env: dict[str, str] | None = None, shell: str = '-e') -> StepRun:
        script = self.tmp / 'step.sh'
        script.write_text(self.STEP['run'].replace(MARKER, str(self.tmp / 'ci_failures')))
        full_env = {
            'PATH': os.pathsep.join([str(self.bin), '/usr/bin', '/bin']),
            'HOME': str(self.tmp),
            'TMPDIR': str(self.tmp),
            'RUNNER_TEMP': str(self.tmp / 'runner'),
            'GITHUB_WORKSPACE': str(self.workspace),
            'GITHUB_OUTPUT': str(self.tmp / 'github_output'),
            'GITHUB_STEP_SUMMARY': str(self.tmp / 'github_summary'),
            'GITHUB_ACTIONS': 'true',
            'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_CONFIG_NOSYSTEM': '1',
            'STUB_LOG': str(self.tmp / 'log'),
            'REAL_NPM': NPM,
            'npm_config_update_notifier': 'false',
            **(env or {}),
        }
        proc = subprocess.run(
            ['bash', '--noprofile', '--norc', shell, str(script)],
            cwd=self.pkg,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return StepRun(proc, self.tmp)


class RunStryker(Harness):
    STEP = workflow_step(WORKFLOWS / 'weekly-stryker.yaml', 'run', 'Run stryker')
    REPORTERS = ('run', '--reporters', 'json,progress')

    def setUp(self):
        super().setUp()
        scripts = self.workspace / 'scripts'
        scripts.mkdir()
        for name in ('patch-vitest-runner-separator.py', 'patch-vitest-browser-bail.py'):
            (scripts / name).write_text(PATCH_STUB)
        bin_dir = self.pkg / 'node_modules' / '.bin'
        bin_dir.mkdir(parents=True)
        write_exec(bin_dir / 'stryker', STRYKER_STUB)

    def run_stryker(self, env: dict[str, str] | None = None) -> StepRun:
        return self.run_step({'ENTRY': 'repo:.', **(env or {})}, shell='-e')

    def test_a_declared_script_is_what_runs_with_the_step_flags(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'NODE_OPTIONS': '--max-old-space-size=4096'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertIn('run test:mutation -- --reporters json,progress', run.logged('npm'))
        started = run.stryker()
        self.assertEqual(started['argv'], list(self.REPORTERS))
        self.assertEqual(started['githubActions'], 'false')
        self.assertEqual(started['execArgv'], [])
        self.assertIn('--max-old-space-size=4096', started['nodeOptions'])
        preload = str(self.tmp / 'runner' / 'no-json-stringify-fast-path.cjs')
        self.assertIn(f'--require={preload}', started['nodeOptions'])
        self.assertIn(preload, started['preloaded'])
        self.assertIn(
            "setFlagsFromString('--no-json-stringify-fast-path')", Path(preload).read_text()
        )
        self.assertEqual(run.outputs.get('score'), '75.0')
        self.assertEqual(run.outputs.get('rc'), '0')

    def test_without_a_declared_script_stryker_runs_under_the_node_flag(self):
        self.package({'test': 'vitest run'})
        run = self.run_stryker()
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('npm'), 'ci\n')
        started = run.stryker()
        self.assertEqual(started['argv'], list(self.REPORTERS))
        self.assertEqual(started['execArgv'], ['--no-json-stringify-fast-path'])
        self.assertEqual(started['githubActions'], 'false')
        self.assertEqual(run.outputs.get('score'), '75.0')

    def test_a_declared_script_below_the_break_threshold_carries_its_exit_code(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'STRYKER_RC': '2'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.outputs.get('rc'), '2')
        self.assertEqual(run.outputs.get('score'), '75.0')

    def test_a_declared_script_still_passes_the_enrolment_gate_first(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'NPX_STRYKER_RC': '1'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.outputs.get('skipped'), 'true')
        self.assertEqual(run.logged('npm'), 'ci\n')
        self.assertEqual(run.stryker(), {})

    def test_a_declared_script_that_kills_nothing_fails(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'STRYKER_KILLED': '0'})
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('killed 0 of 1 mutants', run.out)
        self.assertNotIn('score', run.outputs)

    def test_a_failed_install_stops_before_any_script(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'NPM_CI_RC': '1'})
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.stryker(), {})


class LintWeb(Harness):
    STEP = workflow_step(WORKFLOWS / 'ts-ci.yaml', 'validate', 'Lint web (CSS/HTML)')
    LINT_WEB = 'echo ran >> "$STUB_LOG/lint-web"; exit "${LINT_WEB_RC:-0}"'

    def setUp(self):
        super().setUp()
        (self.pkg / 'a.css').write_text('a {}\n')
        (self.pkg / 'index.html').write_text('<!doctype html>\n')

    def commit_fixture(self) -> None:
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        for argv in (['init', '-q'], ['add', '-f', '--', '.']):
            subprocess.run(['git', *argv], cwd=self.pkg, check=True, env=env, capture_output=True)

    def lint(self, scripts: dict[str, str] | None, env: dict[str, str] | None = None) -> StepRun:
        self.package(scripts)
        self.commit_fixture()
        return self.run_step(env)

    def test_a_declared_lint_web_replaces_the_inline_linters(self):
        run = self.lint({'lint:web': self.LINT_WEB})
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('lint-web'), 'ran\n')
        self.assertEqual(run.logged('npx'), '')
        self.assertEqual(run.recorded, '')

    def test_a_failing_declared_lint_web_records_the_step(self):
        run = self.lint({'lint:web': self.LINT_WEB}, {'LINT_WEB_RC': '1'})
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.recorded, 'web-lint\n')
        self.assertEqual(run.logged('npx'), '')

    def test_without_lint_web_both_inline_linters_run(self):
        run = self.lint({'test': 'vitest run'})
        self.assertEqual(run.rc, 0, run.out)
        calls = run.logged('npx').splitlines()
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0].startswith('stylelint **/*.css --config '), calls)
        self.assertEqual(calls[1], 'html-validate index.html')
        self.assertEqual(run.logged('lint-web'), '')

    def test_without_lint_web_a_failing_linter_records_the_step(self):
        run = self.lint(None, {'NPX_STYLELINT_RC': '1'})
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.recorded, 'web-lint\n')
        self.assertEqual(len(run.logged('npx').splitlines()), 2)


if __name__ == '__main__':
    unittest.main()
