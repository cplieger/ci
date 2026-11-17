"""go-ci.yaml's "Test (noui)" step runs the build without the web UI where an app has one.

`noui` is the one build tag for an app built without its web UI. The step runs
the module's vet, lint and tests again under it whenever the tag changes which
files `go list` selects, and skips any other module. The step body is read from
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
SKIP = 'changes no file of this module; skipping'
RUNS = 'changes the files these packages leave out'
LINT = 'run --allow-parallel-runners --timeout=5m --build-tags=noui ./...'


def step() -> dict:
    steps = yaml.safe_load(WORKFLOW.read_text())['jobs']['validate']['steps']
    (found,) = [s for s in steps if s.get('id') == 'test-noui']
    return found


@unittest.skipUnless(shutil.which('go') or os.environ.get('CI'), 'needs a go toolchain on PATH')
class NouiStepTest(unittest.TestCase):
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
        lint.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.lint_log}"\nexit "${{LINT_RC:-0}}"\n'
        )
        lint.chmod(0o755)
        self.marker = self.tmp / 'failures'

    def write(self, rel: str, text: str) -> None:
        path = self.mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))

    def run_step(self, timeout: str = '', lint_rc: int = 0) -> subprocess.CompletedProcess[str]:
        body = step()['run'].replace(MARKER, str(self.marker))
        env = {
            **os.environ,
            'PATH': f'{self.bin}{os.pathsep}{os.environ["PATH"]}',
            'GO_TEST_TIMEOUT': timeout,
            'LINT_RC': str(lint_rc),
            'GOFLAGS': '-mod=mod',
            'GOPROXY': 'off',
            'GOWORK': 'off',
            'GOTOOLCHAIN': 'local',
            'GOCACHE': str(self.tmp / 'cache'),
        }
        # go-ci.yaml sets no `shell:`, so the runner runs each block as `bash -e {0}`
        # (actions/runner src/Runner.Worker/Handlers/ScriptHandler.cs), without pipefail.
        return subprocess.run(
            ['bash', '-e', '-c', body],
            cwd=self.mod,
            env=env,
            capture_output=True,
            text=True,
        )

    def linted(self) -> list[str]:
        return self.lint_log.read_text().splitlines() if self.lint_log.exists() else []

    def assert_runs(self, proc: subprocess.CompletedProcess[str], *paths: str) -> None:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(RUNS, proc.stdout)
        for path in paths:
            self.assertIn(path, proc.stdout)
        self.assertEqual(self.linted(), [LINT])

    def assert_skips(self, proc: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(SKIP, proc.stdout)
        self.assertEqual(self.linted(), [])
        self.assertFalse(self.marker.exists())

    def test_a_module_the_tag_does_not_change_skips(self) -> None:
        self.assert_skips(self.run_step())

    def test_a_noui_only_package_runs_under_the_tag(self) -> None:
        # Every file of the package is excluded by default, so only the -tags noui listing has it.
        self.write(
            'internal/webui/noui.go', '//go:build noui\n\npackage webui\n\nconst UI = false\n'
        )
        self.write(
            'internal/webui/noui_test.go',
            """\
            //go:build noui

            package webui

            import "testing"

            func TestNoUI(t *testing.T) {
            \tif UI {
            \t\tt.Fatal("the noui build compiled the UI in")
            \t}
            }
            """,
        )
        proc = self.run_step()
        self.assert_runs(proc, '> example.com/app/internal/webui:')
        self.assertIn('ok  \texample.com/app/internal/webui', proc.stdout)

    def test_a_package_whose_every_file_is_negated_runs(self) -> None:
        self.write(
            'internal/webui/webui.go', '//go:build !noui\n\npackage webui\n\nconst UI = true\n'
        )
        # Under the tag the package has no file left, so only the untagged listing has it.
        self.assert_runs(self.run_step(), '< example.com/app/internal/webui:')

    def test_every_constraint_form_go_honours_counts(self) -> None:
        for rel, text in (
            ('expr/z.go', '//go:build (linux && noui) || plan9\n\npackage expr\n'),
            ('plus/p.go', '// +build noui\n\npackage plus\n'),
            ('indented/i.go', '\t//go:build noui\n\npackage indented\n'),
        ):
            with self.subTest(form=rel):
                self.setUp()
                self.write(rel, text)
                self.write(
                    str(pathlib.Path(rel).parent / 'plain.go'),
                    f'package {pathlib.Path(rel).parent}\n',
                )
                self.assert_runs(self.run_step(), rel.split('/')[0])

    def test_a_constraint_on_a_non_go_source_file_counts(self) -> None:
        # Assembly needs no C compiler, so this case runs on every host.
        self.write('asm/asm.go', 'package asm\n')
        self.write('asm/plus.s', '// +build noui\n\n')
        self.assert_runs(self.run_step(), '< example.com/app/asm: plus.s')

    @unittest.skipUnless(
        shutil.which('cc') or shutil.which('gcc') or os.environ.get('CI'), 'needs a C compiler'
    )
    def test_a_noui_c_file_in_a_cgo_package_counts(self) -> None:
        self.write(
            'cg/cg.go',
            'package cg\n\n// int f(void);\nimport "C"\n\nfunc F() int { return int(C.f()) }\n',
        )
        self.write('cg/ui.c', '//go:build !noui\n\nint f(void) { return 1; }\n')
        self.write('cg/stub.c', '//go:build noui\n\nint f(void) { return 0; }\n')
        self.assert_runs(
            self.run_step(), '< example.com/app/cg: stub.c', '> example.com/app/cg: ui.c'
        )

    def test_a_noui_only_external_test_package_counts(self) -> None:
        self.write(
            'ext_test.go',
            '//go:build noui\n\npackage app_test\n\nimport "testing"\n\nfunc TestExt(t *testing.T) {}\n',
        )
        self.assert_runs(self.run_step(), '< example.com/app: ext_test.go')

    def test_a_package_error_under_the_tag_fails_at_vet(self) -> None:
        self.write('other_noui.go', '//go:build noui\n\npackage other\n')
        proc = self.run_step()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn('::error::noui: go list', proc.stdout)
        self.assertIn('found packages', proc.stdout + proc.stderr)
        self.assertEqual(self.linted(), [])
        self.assertEqual(self.marker.read_text(), 'noui\n')

    def test_a_package_error_without_the_tag_leaves_the_noui_build_green(self) -> None:
        # The untagged Vet step owns this error; the noui step reports the noui build only.
        self.write('o.go', '//go:build !noui\n\npackage other\n')
        self.assert_runs(self.run_step(), '> example.com/app: o.go')

    def test_lines_go_does_not_read_as_the_tag_skip(self) -> None:
        self.write('other/w.go', 'package other\n')
        self.write('other/x.go', '//go:build linux && nouix\n\npackage other\n')
        self.write('other/y.go', '//go:build xnoui || go_noui || foo.noui\n\npackage other\n')
        self.write('glued.go', '//go:buildnoui\n\npackage app\n')
        self.write(
            'late.go',
            """\
            package app

            const doc = `
            //go:build noui
            `
            """,
        )
        self.assert_skips(self.run_step())

    def test_testdata_and_a_nested_module_are_not_this_modules_packages(self) -> None:
        self.write('testdata/fixture/noui.go', '//go:build noui\n\npackage fixture\n')
        self.write('nested/go.mod', 'module example.com/nested\n\ngo 1.24\n')
        self.write('nested/noui.go', '//go:build noui\n\npackage nested\n')
        self.assert_skips(self.run_step())

    def test_a_go_list_failure_fails_the_step(self) -> None:
        self.write('go.mod', 'module example.com/app\n\ngo 1.999\n')
        proc = self.run_step()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('::error::noui: go list', proc.stdout)
        self.assertNotIn(SKIP, proc.stdout)
        self.assertEqual(self.marker.read_text(), 'noui\n')

    def test_a_vet_failure_under_the_tag_fails_before_lint(self) -> None:
        self.write(
            'bad_noui.go',
            """\
            //go:build noui

            package app

            import "fmt"

            func Bad() { fmt.Printf("%d\\n", "x") }
            """,
        )
        proc = self.run_step()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.linted(), [])
        self.assertEqual(self.marker.read_text(), 'noui\n')

    def test_a_lint_failure_fails_before_the_tests(self) -> None:
        self.write('noui.go', '//go:build noui\n\npackage app\n')
        self.write(
            'noui_test.go',
            '//go:build noui\n\npackage app\n\nimport "testing"\n\nfunc TestX(t *testing.T) {}\n',
        )
        proc = self.run_step(lint_rc=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.linted(), [LINT])
        self.assertNotIn('ok  \texample.com/app', proc.stdout)
        self.assertEqual(self.marker.read_text(), 'noui\n')

    def test_a_failing_noui_test_fails_the_step(self) -> None:
        self.write(
            'noui_test.go',
            '//go:build noui\n\npackage app\n\nimport "testing"\n\nfunc TestFails(t *testing.T) { t.Fatal("ran under noui") }\n',
        )
        proc = self.run_step()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('ran under noui', proc.stdout)
        self.assertEqual(self.marker.read_text(), 'noui\n')

    def test_the_timeout_input_reaches_the_noui_tests(self) -> None:
        self.write(
            'slow_noui_test.go',
            """\
            //go:build noui

            package app

            import (
            \t"testing"
            \t"time"
            )

            func TestSlow(t *testing.T) { time.Sleep(5 * time.Second) }
            """,
        )
        proc = self.run_step(timeout='1s')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('test timed out after 1s', proc.stdout)

    def test_the_input_reaches_the_shell_through_env_only(self) -> None:
        s = step()
        self.assertEqual(s['env'].get('GO_TEST_TIMEOUT'), '${{ inputs.go-test-timeout }}')
        self.assertNotIn('inputs.', s['run'])


if __name__ == '__main__':
    unittest.main()
