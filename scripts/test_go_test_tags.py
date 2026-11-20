"""go-ci.yaml's "Test (tags)" step runs a module's opted-in build tags.

A module names the build tags its CI also tests under in a `.go-test-tags`
file beside its go.mod; with no file the step skips. The step body is read from
the workflow and run with a real `go` over scratch modules, so what is tested is
what ships; golangci-lint is a stub that records its arguments.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'go-ci.yaml'
MARKER = '/tmp/_ci_failures'
SKIP = 'no .go-test-tags beside go.mod; skipping'
RUNS = 'changes the files these packages leave out'
FORMAT = 'must hold one line of comma-separated Go build tags'
TAGGED_TEST = """\
    //go:build integration

    package app

    import "testing"

    func TestIntegration(t *testing.T) {
    \tif Answer() != WANT {
    \t\tt.Fatal("the integration test ran and failed")
    \t}
    }
    """


def steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())['jobs']['validate']['steps']


def step() -> dict:
    (found,) = [s for s in steps() if s.get('id') == 'test-tags']
    return found


@unittest.skipUnless(shutil.which('go') or os.environ.get('CI'), 'needs a go toolchain on PATH')
class TagsStepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.mod = self.tmp / 'mod'
        self.mod.mkdir()
        self.write('go.mod', 'module example.com/app\n\ngo 1.24\n')
        self.write('app.go', 'package app\n\nfunc Answer() int { return 42 }\n')
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.lint_log = self.tmp / 'lint.log'
        lint = self.bin / 'golangci-lint'
        lint.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.lint_log}"\n')
        lint.chmod(0o755)
        self.marker = self.tmp / 'failures'

    def write(self, rel: str, text: str) -> None:
        path = self.mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))

    def tagged_test(self, want: int = 42) -> None:
        self.write('integration_test.go', TAGGED_TEST.replace('WANT', str(want)))

    def run_step(self, timeout: str = '') -> subprocess.CompletedProcess[str]:
        body = step()['run'].replace(MARKER, str(self.marker))
        env = {
            **os.environ,
            'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}',
            'GO_TEST_TIMEOUT': timeout,
            'GOFLAGS': '-mod=mod',
            'GOPROXY': 'off',
            'GOWORK': 'off',
            'GOTOOLCHAIN': 'local',
            'GOCACHE': str(self.tmp / 'cache'),
        }
        # go-ci.yaml sets no `shell:`, so the runner runs each block as `bash -e {0}`.
        return subprocess.run(
            ['bash', '-e', '-c', body], cwd=self.mod, env=env, capture_output=True, text=True
        )

    def linted(self) -> list[str]:
        return self.lint_log.read_text().splitlines() if self.lint_log.exists() else []

    def assert_fails(self, proc: subprocess.CompletedProcess[str], message: str) -> None:
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(message, proc.stdout + proc.stderr)
        self.assertEqual(self.marker.read_text(), 'tags\n')

    def test_a_module_with_no_file_skips(self) -> None:
        self.tagged_test()
        proc = self.run_step()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(SKIP, proc.stdout)
        self.assertEqual(self.linted(), [])
        self.assertFalse(self.marker.exists())

    def test_the_named_tag_runs_vet_lint_and_its_tests(self) -> None:
        self.tagged_test()
        self.write('.go-test-tags', '\n  integration  \n\n')
        proc = self.run_step()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f'tags: integration {RUNS}', proc.stdout)
        self.assertIn('< example.com/app: integration_test.go', proc.stdout)
        self.assertIn('ok  \texample.com/app', proc.stdout)
        self.assertEqual(
            self.linted(),
            ['run --allow-parallel-runners --timeout=5m --build-tags=integration ./...'],
        )
        self.assertFalse(self.marker.exists())

    def test_a_failing_tagged_test_fails_the_step(self) -> None:
        self.tagged_test(want=41)
        self.write('.go-test-tags', 'integration\n')
        self.assert_fails(self.run_step(), 'the integration test ran and failed')

    def test_several_tags_are_one_comma_list(self) -> None:
        self.tagged_test()
        self.write('e2e_test.go', '//go:build e2e\n\npackage app\n')
        self.write('.go-test-tags', 'integration,e2e\n')
        proc = self.run_step()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('e2e_test.go integration_test.go', proc.stdout)
        self.assertIn('--build-tags=integration,e2e', self.linted()[0])

    def test_the_test_timeout_reaches_go_test(self) -> None:
        self.write(
            'slow_test.go',
            """\
            //go:build integration

            package app

            import (
            \t"testing"
            \t"time"
            )

            func TestSlow(t *testing.T) { time.Sleep(5 * time.Second) }
            """,
        )
        self.write('.go-test-tags', 'integration\n')
        self.assert_fails(self.run_step(timeout='200ms'), 'test timed out after 200ms')

    def test_tags_that_change_no_file_fail(self) -> None:
        self.tagged_test()
        self.write('.go-test-tags', 'integratoin\n')
        self.assert_fails(self.run_step(), 'change no file of this module')
        self.assertEqual(self.linted(), [])

    def test_a_malformed_file_fails_without_echoing_it(self) -> None:
        self.tagged_test()
        for text in (
            'integration e2e\n',
            'integration\ne2e\n',
            '::warning::injected\n',
            '\n',
            '-tags\n',
        ):
            with self.subTest(text=text):
                self.marker.unlink(missing_ok=True)
                self.write('.go-test-tags', text)
                proc = self.run_step()
                self.assert_fails(proc, FORMAT)
                self.assertNotIn('injected', proc.stdout + proc.stderr)
                self.assertEqual(self.linted(), [])

    def test_noui_is_left_to_its_own_step(self) -> None:
        self.write('noui.go', '//go:build noui\n\npackage app\n')
        self.write('.go-test-tags', 'integration,noui\n')
        self.assert_fails(self.run_step(), 'the Test (noui) step runs the noui build')


class TagsStepWiringTest(unittest.TestCase):
    def test_node_is_set_up_before_the_step_exactly_when_the_file_exists(self) -> None:
        names = [s.get('name') for s in steps()]
        (setup,) = [s for s in steps() if s.get('name') == 'Setup Node.js for tagged tests']
        self.assertLess(names.index(setup['name']), names.index('Test (tags)'))
        self.assertEqual(
            setup['if'],
            "${{ hashFiles(format('{0}/.go-test-tags', inputs.working-directory)) != '' }}",
        )

    def test_node_is_the_major_ts_ci_tests_on(self) -> None:
        ts = yaml.safe_load((WORKFLOW.parent / 'ts-ci.yaml').read_text())['jobs']['validate']
        (ts_node,) = [s for s in ts['steps'] if s.get('name') == 'Setup Node.js']
        (setup,) = [s for s in steps() if s.get('name') == 'Setup Node.js for tagged tests']
        self.assertEqual(setup['uses'], ts_node['uses'])
        self.assertEqual(setup['with']['node-version'], ts_node['with']['node-version'])

    def test_the_input_reaches_the_shell_through_env_only(self) -> None:
        s = step()
        self.assertEqual(s['env'].get('GO_TEST_TIMEOUT'), '${{ inputs.go-test-timeout }}')
        self.assertNotIn('${{', s['run'])

    def test_check_results_reads_the_step(self) -> None:
        (check,) = [s for s in steps() if s.get('name') == 'Check results']
        self.assertIn('test-tags ${{ steps.test-tags.outcome }}', check['env']['STEP_OUTCOMES'])


if __name__ == '__main__':
    unittest.main()
