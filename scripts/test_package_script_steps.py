"""The shared steps that run a package's own declared script when it has one.

Each step body runs under bash in a fixture package, with real node and npm (so
`npm run` resolves and forwards arguments as it does on the runner) and stubs for
everything that would install, download or lint.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
MARKER = '/tmp/_ci_failures'
NODE = shutil.which('node')
NPM = shutil.which('npm')

# Logs how the step started it, writes a schema report, and records a SIGINT.
STRYKER_STUB = """#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
fs.writeFileSync(path.join(process.env.STUB_LOG, 'stryker.json'), JSON.stringify({
  argv: process.argv.slice(2),
  execArgv: process.execArgv,
  nodeOptions: process.env.NODE_OPTIONS || '',
  githubActions: process.env.GITHUB_ACTIONS,
  preloaded: Object.keys(require.cache),
  niceness: require('node:os').getPriority(),
}));
process.on('SIGINT', () => {
  fs.appendFileSync(path.join(process.env.STUB_LOG, 'interrupted'), 'SIGINT\\n');
  if (!process.env.STRYKER_IGNORE_INT) process.exit(130);
});
if (process.env.STRYKER_SELF_KILL) process.kill(process.pid, 'SIGKILL');
const killed = Number(process.env.STRYKER_KILLED ?? 3);
const mutants = [];
for (let i = 0; i < killed; i++) mutants.push({ status: 'Killed' });
mutants.push({ status: 'Survived' });
fs.mkdirSync('reports/mutation', { recursive: true });
fs.writeFileSync('reports/mutation/mutation.json',
  JSON.stringify({ schemaVersion: '2', files: { 'a.ts': { mutants } } }));
const sleep = Number(process.env.STRYKER_SLEEP ?? 0);
setTimeout(() => process.exit(Number(process.env.STRYKER_RC ?? 0)), sleep * 1000);
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


def session(sid: int) -> list[int]:
    """The live processes of session SID, which a step's own process groups all belong to."""
    out = []
    for name in os.listdir('/proc'):
        if name.isdigit():
            try:
                if os.getsid(int(name)) == sid and int(name) != sid:
                    out.append(int(name))
            except ProcessLookupError, PermissionError:
                continue
    return out


class StepRun:
    def __init__(self, rc: int, out: str, tmp: Path, stray: list[int]):
        self.rc = rc
        self.out = out
        # Processes the step left running when it ended, a timer it failed to stop among them.
        self.stray = stray
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
        # A file, not a pipe, takes the output: a process the step leaves behind would hold a
        # pipe open, and the read would wait on it instead of the step. Its own session lets
        # the harness find and end every process the step started, in any process group.
        with tempfile.TemporaryFile(dir=self.tmp) as log:
            proc = subprocess.Popen(
                ['bash', '--noprofile', '--norc', shell, str(script)],
                cwd=self.pkg,
                env=full_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                rc = proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                rc = None
            stray = session(proc.pid)
            deadline = time.monotonic() + 1
            while stray and time.monotonic() < deadline:
                time.sleep(0.05)
                stray = session(proc.pid)
            for pid in [proc.pid] * (rc is None) + stray:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()
            log.seek(0)
            out = log.read().decode(errors='replace')
        if rc is None:
            self.fail(
                f'the step {self.STEP["run"][:80]!r} did not end within 120 s:\n{out[-2000:]}'
            )
        return StepRun(rc, out, self.tmp, stray)


class RunStryker(Harness):
    """scripts/stryker-run.sh, which weekly-stryker.yaml's plan and run jobs call from the package dir."""

    STEP: ClassVar[dict] = {'run': f'bash {ROOT / "scripts" / "stryker-run.sh"}'}

    def setUp(self):
        super().setUp()
        bin_dir = self.pkg / 'node_modules' / '.bin'
        bin_dir.mkdir(parents=True)
        write_exec(bin_dir / 'stryker', STRYKER_STUB)
        self.reporter = str(self.tmp / 'ci' / 'stryker-ci-reporter.mjs')

    def run_stryker(self, env: dict[str, str] | None = None) -> StepRun:
        base = {
            'ENTRY': 'repo:.',
            'REPORTER': self.reporter,
            'CI_STRYKER_TIMES': str(self.tmp / 'times'),
            'RUN_DEADLINE': str(int(time.time()) + 600),
            'CI_STRYKER_RESULT': str(self.tmp / 'result.json'),
            'MUTATE': 'src/a.ts,src/b.ts',
        }
        return self.run_step({**base, **(env or {})}, shell='-e')

    def shard_argv(self) -> list[str]:
        return [
            'run',
            '--reporters',
            'progress,ci',
            '--appendPlugins',
            self.reporter,
            '--maxTestRunnerReuse',
            '200',
            '--mutate',
            'src/a.ts,src/b.ts',
        ]

    def test_a_declared_script_is_what_runs_with_the_shard_flags(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'NODE_OPTIONS': '--max-old-space-size=4096'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertIn(
            'run test:mutation -- --reporters progress,ci --appendPlugins', run.logged('npm')
        )
        started = run.stryker()
        self.assertEqual(started['argv'], self.shard_argv())
        self.assertEqual(started['githubActions'], 'false')
        self.assertEqual(started['execArgv'], [])
        self.assertIn('--max-old-space-size=4096', started['nodeOptions'])
        preload = str(self.tmp / 'runner' / 'no-json-stringify-fast-path.cjs')
        self.assertIn(f'--require={preload}', started['nodeOptions'])
        self.assertIn(preload, started['preloaded'])
        self.assertIn(
            "setFlagsFromString('--no-json-stringify-fast-path')", Path(preload).read_text()
        )

    def test_without_a_declared_script_stryker_runs_under_the_node_flag(self):
        self.package({'test': 'vitest run'})
        run = self.run_stryker()
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('npm'), 'ci\n')
        started = run.stryker()
        self.assertEqual(started['argv'], self.shard_argv())
        self.assertEqual(started['execArgv'], ['--no-json-stringify-fast-path'])
        self.assertEqual(started['githubActions'], 'false')

    def test_the_plan_pass_runs_only_the_ci_reporter_with_no_mutate_list(self):
        self.package({'test': 'vitest run'})
        run = self.run_step(
            {
                'ENTRY': 'repo:.',
                'REPORTER': self.reporter,
                'CI_STRYKER_PLAN': str(self.tmp / 'pass.json'),
            },
            shell='-e',
        )
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(
            run.stryker()['argv'], ['run', '--reporters', 'ci', '--appendPlugins', self.reporter]
        )

    def test_a_shard_without_a_result_file_runs_nothing(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'CI_STRYKER_RESULT': ''})
        self.assertNotEqual(run.rc, 0, run.out)
        self.assertIn('CI_STRYKER_RESULT', run.out)
        self.assertEqual(run.stryker(), {})

    def test_stryker_s_exit_status_is_the_script_s(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'STRYKER_RC': '2'})
        self.assertEqual(run.rc, 2, run.out)

    def test_a_shard_past_its_budget_is_interrupted_so_stryker_can_save_its_results(self):
        # ubuntu-26.04's timeout is uutils', which sends SIGKILL at once after `-s INT -k N`.
        uutils = shutil.which('coreutils')
        for timeout_bin in (None, uutils) if uutils else (None,):
            for scripts in ({'test:mutation': 'stryker run'}, {'test': 'vitest run'}):
                with self.subTest(timeout=timeout_bin or 'system', scripts=scripts):
                    (self.bin / 'timeout').unlink(missing_ok=True)
                    if timeout_bin:
                        (self.bin / 'timeout').symlink_to(timeout_bin)
                    self.package(scripts)
                    (self.tmp / 'log' / 'interrupted').unlink(missing_ok=True)
                    run = self.run_stryker(
                        {'RUN_DEADLINE': str(int(time.time()) + 3), 'STRYKER_SLEEP': '30'}
                    )
                    self.assertEqual(run.rc, 124, run.out)
                    self.assertEqual(run.logged('interrupted'), 'SIGINT\n')
                    self.assertIn('::error::repo:.: the shard reached its time budget, so', run.out)

    def test_a_shard_that_ignores_the_interrupt_is_killed_after_its_grace(self):
        self.package({'test': 'vitest run'})
        started = time.monotonic()
        run = self.run_stryker(
            {
                'RUN_DEADLINE': str(int(time.time()) + 2),
                'KILL_GRACE': '2',
                'STRYKER_SLEEP': '60',
                'STRYKER_IGNORE_INT': '1',
            }
        )
        self.assertEqual(run.rc, 137, run.out)
        self.assertEqual(run.logged('interrupted'), 'SIGINT\n')
        self.assertIn('did not end 2 s after the interrupt, so it was killed', run.out)
        self.assertLess(time.monotonic() - started, 30)

    def test_a_shard_killed_from_outside_before_its_deadline_is_not_named_as_its_budget(self):
        self.package({'test': 'vitest run'})
        run = self.run_stryker({'STRYKER_SELF_KILL': '1'})
        self.assertEqual(run.rc, 137, run.out)
        self.assertIn('::error::repo:.: Stryker ended on signal 9 before the deadline', run.out)
        self.assertNotIn('time budget', run.out)
        self.assertEqual(run.stray, [])

    def test_a_shard_that_ends_before_its_deadline_keeps_its_status_and_leaves_no_timer(self):
        self.package({'test:mutation': 'stryker run'})
        started = time.monotonic()
        run = self.run_stryker({'STRYKER_RC': '1'})
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.logged('interrupted'), '')
        self.assertEqual(run.stray, [])
        self.assertLess(time.monotonic() - started, 30)

    def test_stryker_runs_below_the_runner_s_own_priority(self):
        for scripts in ({'test:mutation': 'stryker run'}, {'test': 'vitest run'}):
            with self.subTest(scripts=scripts):
                self.package(scripts)
                want = min(19, os.getpriority(os.PRIO_PROCESS, 0) + 10)
                self.assertEqual(self.run_stryker().stryker()['niceness'], want)

    def test_the_install_s_time_comes_out_of_the_shard_s_budget(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'RUN_DEADLINE': str(int(time.time()) - 1)})
        self.assertEqual(run.rc, 124, run.out)
        self.assertIn("used the shard's whole budget", run.out)
        self.assertEqual(run.logged('npm'), 'ci\n')
        self.assertEqual(run.stryker(), {})

    def test_a_deadline_that_is_not_an_epoch_second_runs_nothing(self):
        self.package({'test:mutation': 'stryker run'})
        for bad in ('1+1', 'x', '-5', '1e9'):
            with self.subTest(bad=bad):
                run = self.run_stryker({'RUN_DEADLINE': bad})
                self.assertEqual(run.rc, 2, run.out)
                self.assertIn('is not an epoch second', run.out)
                self.assertEqual(run.stryker(), {})

    def test_an_entry_without_stryker_is_not_enrolled_and_runs_nothing(self):
        self.package({'test:mutation': 'stryker run'})
        run = self.run_stryker({'NPX_STRYKER_RC': '1'})
        self.assertEqual(run.rc, 3, run.out)
        self.assertIn('is not a devDependency', run.out)
        self.assertEqual(run.logged('npm'), 'ci\n')
        self.assertEqual(run.stryker(), {})

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
