"""The deadset dead-code steps of go-ci.yaml and ts-ci.yaml, run against stubs.

Each step body is read from the workflow and run with bash the way the runner
runs it, with `deadset` and `go` replaced by stubs that record what they were
given and exit with a chosen status.
"""

from __future__ import annotations

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
GO_CI = ROOT / '.github' / 'workflows' / 'go-ci.yaml'
TS_CI = ROOT / '.github' / 'workflows' / 'ts-ci.yaml'
STEP = 'Dead code (deadset)'
KNIP_STEP = 'Import cycles and undeclared dependencies (knip)'
MARKER = '/tmp/_ci_failures'

# The generic workflow pin manager of the shared Renovate preset
# (cplieger/.github default.json), which is what bumps these pins.
RENOVATE_PIN = re.compile(
    r'#\s*renovate:\s*datasource=(?P<datasource>[a-z-]+)\s+depName=(?P<dep>[^\s]+)'
    r'(\s+versioning=[a-z-]+)?\s*\n\s*(?P<var>[A-Z_]*VERSION)[=:]\s*[\'"]?(?P<ver>[^\'"\s]+)'
)

DEADSET_STUB = """#!/usr/bin/env bash
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
exit "${DEADSET_RC:-0}"
"""

GO_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG/go"
exit "${GO_RC:-0}"
"""


def workflow_step(path: Path, name: str) -> dict:
    doc = yaml.safe_load(path.read_text())
    for step in doc['jobs']['validate']['steps']:
        if step.get('name') == name:
            return step
    raise AssertionError(f'{path.name} has no step named {name!r}')


def step_names(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text())
    return [step.get('name', '') for step in doc['jobs']['validate']['steps']]


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class StepRun:
    def __init__(self, proc: subprocess.CompletedProcess, log: Path, marker: Path):
        self.rc = proc.returncode
        self.out = proc.stdout + proc.stderr
        self.log = log
        self.marker = marker

    def logged(self, name: str) -> str:
        path = self.log / name
        return path.read_text() if path.exists() else ''

    def recorded(self) -> str:
        return self.marker.read_text() if self.marker.exists() else ''


class Harness(unittest.TestCase):
    workflow: Path
    step_name = STEP

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='deadset-step-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.log = self.tmp / 'log'
        self.work = self.tmp / 'work'
        for d in (self.bin, self.log, self.work):
            d.mkdir()
        self.step = workflow_step(self.workflow, self.step_name)

    def stub(self, deadset: bool = True) -> None:
        if deadset:
            write_exec(self.bin / 'deadset', DEADSET_STUB)
        write_exec(self.bin / 'go', GO_STUB)

    def run_step(self, env: dict[str, str] | None = None, deadset: bool = True) -> StepRun:
        self.stub(deadset)
        marker = self.tmp / 'ci_failures'
        marker.unlink(missing_ok=True)
        for old in self.log.iterdir():
            old.unlink()
        body = self.step['run'].replace(MARKER, str(marker))
        script = self.tmp / 'step.sh'
        script.write_text(body)
        node = shutil.which('node')
        if node and not (self.bin / 'node').exists():
            (self.bin / 'node').symlink_to(node)
        full_env = {
            'PATH': os.pathsep.join([str(self.bin), '/usr/bin', '/bin']),
            'HOME': str(self.tmp),
            'TMPDIR': str(self.tmp),
            'STUB_LOG': str(self.log),
            **(env or {}),
        }
        proc = subprocess.run(
            ['bash', '--noprofile', '--norc', '-eo', 'pipefail', str(script)],
            cwd=self.work,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return StepRun(proc, self.log, marker)

    def assert_failed(self, run: StepRun, message: str) -> None:
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.recorded(), 'deadset\n')
        self.assertIn(f'::error::deadset {message}', run.out)

    def assert_exit_codes(self, env: dict[str, str]) -> None:
        cases = {
            '1': 'reported dead code',
            '2': 'refused the run (exit 2)',
            '3': 'produced no answer (exit 3)',
            '4': 'exited 4, which is no verdict',
        }
        for rc, message in cases.items():
            with self.subTest(rc=rc):
                self.assert_failed(self.run_step({**env, 'DEADSET_RC': rc}), message)


class GoStep(Harness):
    workflow = GO_CI

    def test_the_step_is_soft_gated_and_reads_the_profile(self):
        self.assertTrue(self.step.get('continue-on-error') is True)
        self.assertEqual(self.step['env'], {'APP_PROFILE': '${{ steps.profile.outputs.app }}'})

    def test_a_clean_run_passes_and_records_nothing(self):
        run = self.run_step({'APP_PROFILE': 'true', 'DEADSET_RC': '0'})
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.recorded(), '')
        self.assertNotIn('::error::', run.out)

    def test_every_nonzero_exit_fails_with_its_own_message(self):
        self.assert_exit_codes({'APP_PROFILE': 'true'})

    def test_a_missing_deadset_fails_and_names_the_installer(self):
        run = self.run_step({'APP_PROFILE': 'true'}, deadset=False)
        self.assert_failed(run, 'is not on PATH')
        self.assertIn('install-local-tools.sh', run.out)

    def test_it_analyzes_go_only_from_the_module_root(self):
        run = self.run_step({'APP_PROFILE': 'false'})
        argv = run.logged('argv').splitlines()
        self.assertEqual(argv[:3], ['analyze', '--target=.', '--languages=go'])
        self.assertTrue(any(a.startswith('--central=/') for a in argv), argv)
        self.assertTrue(any(a.startswith('--run-dir=/') for a in argv), argv)
        self.assertEqual(len(argv), 5, argv)

    def test_the_run_dir_is_new_under_an_existing_parent_and_is_removed(self):
        run = self.run_step({'APP_PROFILE': 'true'})
        self.assertEqual(run.logged('rundir'), '')
        self.assertEqual(run.logged('rundir-parent'), 'parent\n')
        central = next(a for a in run.logged('argv').splitlines() if a.startswith('--central='))
        self.assertFalse(Path(central.split('=', 1)[1]).parent.exists())

    def test_the_kind_follows_the_profile(self):
        for profile, kind in (('true', 'application'), ('false', 'library'), ('', 'library')):
            with self.subTest(profile=profile):
                run = self.run_step({'APP_PROFILE': profile})
                self.assertEqual(
                    yaml.safe_load(run.logged('central.json')), {'target': {'kind': kind}}
                )

    def test_the_module_cache_is_filled_before_deadset_runs(self):
        run = self.run_step({'APP_PROFILE': 'true'})
        self.assertEqual(run.logged('go'), 'mod download\n')

    def test_a_failed_module_download_fails_without_running_deadset(self):
        run = self.run_step({'APP_PROFILE': 'true', 'GO_RC': '1'})
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.recorded(), 'deadset\n')
        self.assertEqual(run.logged('argv'), '')


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class TsStep(Harness):
    workflow = TS_CI

    def manifest(self, text: str) -> None:
        (self.work / 'package.json').write_text(text)

    def test_the_step_is_soft_gated(self):
        self.assertTrue(self.step.get('continue-on-error') is True)

    def test_a_clean_run_passes_and_never_calls_go(self):
        self.manifest('{"name": "x"}')
        run = self.run_step()
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.recorded(), '')
        self.assertEqual(run.logged('go'), '')

    def test_every_nonzero_exit_fails_with_its_own_message(self):
        self.manifest('{"name": "x"}')
        self.assert_exit_codes({})

    def test_a_missing_deadset_fails_and_names_the_installer(self):
        self.manifest('{"name": "x"}')
        run = self.run_step(deadset=False)
        self.assert_failed(run, 'is not on PATH')
        self.assertIn('install-local-tools.sh', run.out)

    def test_an_unreadable_manifest_fails_without_running_deadset(self):
        for text in (None, '{not json'):
            with self.subTest(manifest=text):
                (self.work / 'package.json').unlink(missing_ok=True)
                if text is not None:
                    self.manifest(text)
                run = self.run_step()
                self.assert_failed(run, 'did not run: package.json could not be read')
                self.assertEqual(run.logged('argv'), '')

    def test_it_analyzes_typescript_only(self):
        self.manifest('{"name": "x"}')
        argv = self.run_step().logged('argv').splitlines()
        self.assertEqual(argv[:3], ['analyze', '--target=.', '--languages=ts'])

    def test_an_exports_map_makes_a_library(self):
        cases = (
            ('{"name": "@x/y", "exports": {".": "./src/index.ts"}}', 'library'),
            ('{"name": "@x/y", "exports": "./src/index.ts"}', 'library'),
            ('{"private": true}', 'application'),
            ('{"name": "@x/y"}', 'application'),
        )
        for text, kind in cases:
            with self.subTest(manifest=text):
                self.manifest(text)
                run = self.run_step()
                self.assertEqual(
                    yaml.safe_load(run.logged('central.json')), {'target': {'kind': kind}}
                )


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
    """The non-dead-code knip checks: cycles, unlisted dependencies and binaries, duplicates."""

    workflow = TS_CI
    step_name = KNIP_STEP

    def run_knip(self, files: dict[str, str], rc: str = '0') -> StepRun:
        for old in self.work.iterdir():
            old.unlink()
        for name, text in files.items():
            (self.work / name).write_text(text)
        write_exec(self.bin / 'npx', NPX_STUB)
        return self.run_step({'NPX_RC': rc})

    def test_the_step_is_soft_gated_and_runs_after_the_install(self):
        self.assertTrue(self.step.get('continue-on-error') is True)
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
        self.assertEqual(run.recorded(), 'knip\n')


class Pins(unittest.TestCase):
    def pins(self, path: Path) -> dict[str, tuple[str, str, str]]:
        return {
            m['dep']: (m['datasource'], m['var'], m['ver'])
            for m in RENOVATE_PIN.finditer(path.read_text())
        }

    def test_renovate_tracks_every_deadset_pin(self):
        go, ts = self.pins(GO_CI), self.pins(TS_CI)
        self.assertEqual(go['github.com/cplieger/deadset'][:2], ('go', 'DEADSET_VERSION'))
        self.assertEqual(go['github.com/cplieger/deadset-go'][:2], ('go', 'DEADSET_GO_VERSION'))
        self.assertEqual(ts['@cplieger/deadset-ts'][:2], ('npm', 'DEADSET_TS_VERSION'))
        self.assertEqual(
            ts['github.com/cplieger/deadset'],
            go['github.com/cplieger/deadset'],
            'go-ci.yaml and ts-ci.yaml must pin the same deadset',
        )

    def test_each_install_uses_its_pin(self):
        go, ts = GO_CI.read_text(), TS_CI.read_text()
        self.assertIn('go install "github.com/cplieger/deadset/cmd/deadset@${DEADSET_VERSION}"', go)
        self.assertIn(
            'go install "github.com/cplieger/deadset-go/cmd/deadset-go@${DEADSET_GO_VERSION}"', go
        )
        self.assertIn('go install "github.com/cplieger/deadset/cmd/deadset@${DEADSET_VERSION}"', ts)
        self.assertIn('"@cplieger/deadset-ts@${DEADSET_TS_VERSION}"', ts)

    def test_the_go_installs_may_fetch_a_newer_toolchain(self):
        for line in GO_CI.read_text().splitlines():
            if 'cplieger/deadset' in line and 'go install' in line:
                self.assertIn('GOTOOLCHAIN=auto go install', line)
        install = workflow_step(TS_CI, 'Install deadset')['run']
        self.assertLess(install.index('export GOTOOLCHAIN=auto'), install.index('go install'))
        self.assertIn('echo "GOTOOLCHAIN=auto" >> "$GITHUB_ENV"', install)

    def test_the_tools_are_installed_before_the_step(self):
        go = step_names(GO_CI)
        self.assertLess(go.index('Install tools'), go.index(STEP))
        ts = step_names(TS_CI)
        self.assertLess(ts.index('Install deps'), ts.index('Setup Go (deadset)'))
        self.assertLess(ts.index('Setup Go (deadset)'), ts.index('Install deadset'))
        self.assertLess(ts.index('Install deadset'), ts.index(STEP))


class Replaced(unittest.TestCase):
    def test_no_language_workflow_runs_a_retired_tool(self):
        retired = re.compile(r'punused|deadcode|gopls|knip|XTOOLS_VERSION', re.IGNORECASE)
        go = GO_CI.read_text()
        self.assertEqual(retired.findall(go), [])
        doc = yaml.safe_load(TS_CI.read_text())
        for step in doc['jobs']['validate']['steps']:
            if step.get('name') == KNIP_STEP:
                continue
            with self.subTest(step=step.get('name')):
                self.assertEqual(retired.findall(yaml.safe_dump(step)), [])

    def test_golangci_drops_only_the_linters_deadset_gates(self):
        linters = yaml.safe_load((ROOT / '.golangci.yaml').read_text())['linters']
        self.assertEqual(linters['default'], 'standard')
        self.assertEqual(sorted(linters['disable']), ['ineffassign', 'unused'])
        self.assertNotIn('wastedassign', linters['enable'])
        self.assertIn('unparam', linters['enable'])

    def test_eslint_keeps_no_unused_vars(self):
        base = (ROOT / 'configs' / 'eslint.config.base.mjs').read_text()
        self.assertIn('"@typescript-eslint/no-unused-vars": [\n        "error"', base)


class LocalMirror(unittest.TestCase):
    """ci-local's version preflight reads the deadset pins and the installed builds."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        import _ci_local

        cls.ci_local = _ci_local

    def test_the_preflight_reads_all_three_pins(self):
        bodies = [workflow_step(GO_CI, 'Install tools')['run']]
        bodies.append(workflow_step(TS_CI, 'Install deadset')['run'])
        pins = self.ci_local.collect_pinned_versions(bodies)
        go, ts = Pins().pins(GO_CI), Pins().pins(TS_CI)
        self.assertEqual(pins['deadset'][0], go['github.com/cplieger/deadset'][2].removeprefix('v'))
        self.assertEqual(
            pins['deadset-go'][0], go['github.com/cplieger/deadset-go'][2].removeprefix('v')
        )
        self.assertEqual(pins['deadset-ts'][0], ts['@cplieger/deadset-ts'][2])

    def test_each_installed_version_is_read_the_way_the_tool_prints_it(self):
        tools = self.ci_local._GO_INSTALLED  # noqa: SLF001
        self.assertLessEqual({'deadset', 'deadset-go'}, tools)
        self.assertEqual(self.ci_local._VERSION_ARGV['deadset-ts'], ['version'])  # noqa: SLF001
        tmp = Path(tempfile.mkdtemp(prefix='deadset-version-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        write_exec(
            tmp / 'deadset-ts',
            '#!/bin/sh\n[ "$1" = version ] || exit 2\necho "deadset-ts 6.0.1"\n'
            'echo "contract 6.0.0"\n',
        )
        old_path = os.environ['PATH']
        os.environ['PATH'] = f'{tmp}{os.pathsep}{old_path}'
        self.addCleanup(os.environ.__setitem__, 'PATH', old_path)
        self.ci_local._LOCAL_VERSION_CACHE.pop('deadset-ts', None)  # noqa: SLF001
        self.addCleanup(self.ci_local._LOCAL_VERSION_CACHE.pop, 'deadset-ts', None)  # noqa: SLF001
        self.assertEqual(self.ci_local._local_tool_version('deadset-ts'), '6.0.1')  # noqa: SLF001


NPM_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG/npm"
exit "${NPM_RC:-0}"
"""

# Every installer except deadset-ts's becomes a no-op, so main's own call list runs.
INSTALLER_DRIVER = """set -euo pipefail
. "$INSTALLER_BODY"
WF_DIR="$WF_DIR_UNDER_TEST"
for f in $(declare -F | awk '{print $3}'); do
  case "$f" in
    install_deadset_ts) ;;
    install_* | advise_gotoolchain) eval "$f() { :; }" ;;
  esac
done
main
"""


class LocalInstaller(unittest.TestCase):
    """install-local-tools.sh installs deadset-ts at ts-ci.yaml's npm pin."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='deadset-installer-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.log = self.tmp / 'log'
        for d in (self.bin, self.log):
            d.mkdir()
        for tool in ('bash', 'awk', 'grep', 'head', 'dirname'):
            found = shutil.which(tool)
            self.assertIsNotNone(found, f'{tool} is needed to run the installer')
            (self.bin / tool).symlink_to(found)
        text = (ROOT / 'scripts' / 'install-local-tools.sh').read_text().rstrip()
        self.assertTrue(text.endswith('\nmain "$@"'), 'the installer no longer ends in main "$@"')
        self.body = self.tmp / 'installer.sh'
        self.body.write_text(text.removesuffix('main "$@"'))
        self.driver = self.tmp / 'driver.sh'
        self.driver.write_text(INSTALLER_DRIVER)
        self.pin = Pins().pins(TS_CI)['@cplieger/deadset-ts'][2]

    def install(
        self,
        installed: str | None = None,
        npm: bool = True,
        wf_dir: Path | None = None,
        npm_rc: str = '0',
    ) -> tuple[int, str, str]:
        if npm:
            write_exec(self.bin / 'npm', NPM_STUB)
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
                'INSTALLER_BODY': str(self.body),
                'WF_DIR_UNDER_TEST': str(wf_dir or GO_CI.parent),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        npm_log = self.log / 'npm'
        return (
            proc.returncode,
            proc.stdout + proc.stderr,
            npm_log.read_text() if npm_log.exists() else '',
        )

    def summary_line(self, version: str, status: str) -> re.Pattern[str]:
        return re.compile(
            rf'^  deadset-ts\s+{re.escape(version)}\s+{re.escape(status)}$', re.MULTILINE
        )

    def test_main_installs_the_pinned_build_without_install_scripts(self):
        rc, out, npm = self.install()
        self.assertEqual(rc, 0, out)
        self.assertEqual(npm, f'install -g --ignore-scripts @cplieger/deadset-ts@{self.pin}\n')
        self.assertRegex(out, self.summary_line(self.pin, 'npm -g'))

    def test_the_version_comes_from_the_workflow_pin(self):
        wf = self.tmp / 'workflows'
        wf.mkdir()
        (wf / 'ts-ci.yaml').write_text(
            '# renovate: datasource=npm depName=@cplieger/deadset-ts\nDEADSET_TS_VERSION=9.8.7\n'
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
        (unpinned / 'ts-ci.yaml').write_text('DEADSET_TS_VERSION=1.2.3\n')
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
