"""The meta ci.yaml's shell routing: which job lints a repository's shell scripts,
when hadolint runs, and that both shell jobs run a committed tests/shell/run.sh."""

from __future__ import annotations

import unittest

import workflow_replay as wr
import yaml
from test_deadset_steps import META, WORKFLOWS, Harness, git_repo, run_detect, workflow_step

SHELL_CI = WORKFLOWS / 'shell-ci.yaml'
GO = {'go.mod': 'module x\n', 'main.go': 'package main\n'}
TS = {'jsr.json': '{}', 'package.json': '{}', 'package-lock.json': '{}', 'mod.ts': ''}
DOCKERFILE = {'Dockerfile': 'FROM scratch\n'}
SH = '#!/bin/sh\n'

# (name, files, run_shell, run_scripts)
ROUTES = (
    ('a Go repository with a script and no Dockerfile', {**GO, 'scripts/x.sh': SH}, True, False),
    (
        'a TypeScript repository with tests/shell/run.sh',
        {**TS, 'tests/shell/run.sh': SH},
        True,
        False,
    ),
    ('a Go repository with no shell', GO, False, False),
    ('a Dockerfile repository with no shell', DOCKERFILE, True, False),
    ('a Go repository with a Dockerfile', {**GO, **DOCKERFILE}, True, False),
    ('a repository with scripts and no language', {'README.md': 'x\n', 'x.sh': SH}, False, True),
    *(
        (f'a Go repository whose only script is under {d}/', {**GO, f'{d}/x.sh': SH}, False, False)
        for d in ('node_modules', 'vendor', 'testdata', 'dist', 'a/vendor')
    ),
)


def shell_job(outputs: dict[str, str]) -> tuple[bool, str]:
    """Whether the meta `shell` job runs on these detect outputs, and its dockerfile input."""
    workflow = yaml.safe_load(META.read_text())
    contexts = {'needs': {'detect': {'result': 'success', 'outputs': outputs}}}
    runs = wr.job_condition(workflow, 'shell', contexts, [])
    dockerfile = wr.Scope(contexts, {}, []).render(workflow['jobs']['shell']['with']['dockerfile'])
    return runs, dockerfile


def hadolint_runs(dockerfile: str) -> bool:
    step = workflow_step(SHELL_CI, 'Validate Dockerfile')
    return wr.Scope({'inputs': {'dockerfile': dockerfile}}, {}, []).condition(step.get('if'))


def flag(value: bool) -> str:
    return 'true' if value else 'false'


class Routing(Harness):
    def test_each_repository_shape_lints_its_shell_in_one_expected_job(self):
        for name, files, shell, scripts in ROUTES:
            with self.subTest(name):
                out = run_detect(self, files)
                self.assertEqual(
                    (out['run_shell'], out['run_scripts']), (flag(shell), flag(scripts))
                )

    def test_the_shell_job_runs_exactly_when_detect_routes_shell_there(self):
        for name, files, shell, _ in ROUTES:
            with self.subTest(name):
                self.assertEqual(shell_job(run_detect(self, files))[0], shell)

    def test_hadolint_runs_exactly_when_the_repository_has_a_dockerfile(self):
        for name, files, shell, _ in ROUTES:
            if not shell:
                continue
            with self.subTest(name):
                _, dockerfile = shell_job(run_detect(self, files))
                self.assertEqual(hadolint_runs(dockerfile), 'Dockerfile' in files, dockerfile)

    def test_a_direct_caller_keeps_hadolint_on_the_default_dockerfile(self):
        # PyYAML reads the bare `on` key as the boolean true (YAML 1.1).
        inputs = yaml.safe_load(SHELL_CI.read_text())[True]['workflow_call']['inputs']
        self.assertEqual(inputs['dockerfile']['default'], './Dockerfile')
        self.assertTrue(hadolint_runs(inputs['dockerfile']['default']))


SUITE_STEPS = ((SHELL_CI, 'validate'), (META, 'scripts'))


class ShellSuite(Harness):
    def run_suite_step(self, path, job: str, files: dict[str, str]):
        body = workflow_step(path, 'Run shell unit tests', job)['run']
        return self.run_body(body, git_repo(self.tmp / f'suite-{job}', files))

    def test_a_failing_suite_fails_the_step_and_names_it(self):
        for path, job in SUITE_STEPS:
            with self.subTest(job):
                run = self.run_suite_step(
                    path, job, {'tests/shell/run.sh': 'echo suite-ran; exit 1\n'}
                )
                self.assertNotEqual(run.rc, 0, run.out)
                self.assertIn('suite-ran', run.out)
                self.assertEqual(run.recorded, 'Run shell unit tests\n')

    def test_a_passing_suite_committed_without_the_execute_bit_runs(self):
        for path, job in SUITE_STEPS:
            with self.subTest(job):
                run = self.run_suite_step(path, job, {'tests/shell/run.sh': 'echo suite-ran\n'})
                self.assertEqual(run.rc, 0, run.out)
                self.assertIn('suite-ran', run.out)
                self.assertEqual(run.recorded, '')

    def test_a_repository_without_a_suite_skips(self):
        for path, job in SUITE_STEPS:
            with self.subTest(job):
                run = self.run_suite_step(path, job, {'README.md': 'x\n'})
                self.assertEqual(run.rc, 0, run.out)
                self.assertIn('no shell unit tests', run.out)
                self.assertEqual(run.recorded, '')


if __name__ == '__main__':
    unittest.main()
