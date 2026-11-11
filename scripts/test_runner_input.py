"""The `runner` input moves every job that can leave GitHub's hosted runners, and no other.

A runner built from GitHub's runner container image has no pipx, Node or Go on
PATH, no shellcheck and no envsubst, so a job that moves sets up what it runs.
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
TEMPLATES = ROOT / '.github' / 'workflow-templates'
TAKES_RUNNER = (
    'ci.yaml',
    'go-ci.yaml',
    'ts-ci.yaml',
    'shell-ci.yaml',
    'deadset-ci.yaml',
    'security-scan.yaml',
)
POOL = "${{ inputs.runner || 'ubuntu-26.04' }}"
FORWARD = '${{ inputs.runner }}'
HOSTED = {
    ('ci.yaml', 'docker'): 'ubuntu-26.04',
    ('ci.yaml', 'docker-arm64'): 'ubuntu-26.04-arm',
    ('security-scan.yaml', 'trivy'): 'ubuntu-26.04',
}
CONDITIONAL = {
    (
        'shell-ci.yaml',
        'validate',
    ): "${{ (inputs.dockerfile == '' && inputs.runner) || 'ubuntu-26.04' }}",
}
# go-os-ci.yaml's own `runner` input names its Windows or macOS runner.
OS_RUNNER = 'go-os-ci.yaml'
LOCAL_CALL = re.compile(r'^\./\.github/workflows/([\w.-]+)$')
PINNED_CALL = re.compile(r'^cplieger/ci/\.github/workflows/([\w.-]+)@([0-9a-f]{40})$')
# Actions whose own scripts call a binary GitHub's runner image lacks.
NEEDS_HOSTED_IMAGE = {'sigstore/cosign-installer@': 'envsubst'}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def inputs(name: str) -> dict:
    # PyYAML reads the bare `on` key as True.
    return (load(WORKFLOWS / name)[True]['workflow_call'] or {}).get('inputs') or {}


def code(run: str) -> str:
    return '\n'.join(line for line in run.splitlines() if not line.lstrip().startswith('#'))


class Evaluate:
    """The `||`, `&&`, `==`, parenthesis and string subset of GitHub's expression syntax."""

    TOKEN = re.compile(r"\s*(\|\||&&|==|\(|\)|'[^']*'|[A-Za-z_][\w.-]*)")

    def __init__(self, expression: str, context: dict[str, str]):
        body = re.fullmatch(r'\$\{\{(.*)\}\}', expression).group(1).strip()
        self.tokens = []
        pos = 0
        while pos < len(body):
            m = self.TOKEN.match(body, pos)
            if not m:
                raise ValueError(f'unsupported expression {expression!r}')
            self.tokens.append(m.group(1))
            pos = m.end()
        self.context = context

    def value(self) -> str:
        out = self.either()
        if self.tokens:
            raise ValueError(f'trailing tokens {self.tokens}')
        return out

    def either(self) -> str:
        left = self.both()
        while self.tokens[:1] == ['||']:
            self.tokens.pop(0)
            right = self.both()
            left = left or right
        return left

    def both(self) -> str:
        left = self.equal()
        while self.tokens[:1] == ['&&']:
            self.tokens.pop(0)
            right = self.equal()
            left = right if left else left
        return left

    def equal(self) -> str:
        left = self.atom()
        if self.tokens[:1] == ['==']:
            self.tokens.pop(0)
            return 'true' if left == self.atom() else ''
        return left

    def atom(self) -> str:
        tok = self.tokens.pop(0)
        if tok == '(':
            out = self.either()
            close = self.tokens.pop(0)
            if close != ')':
                raise ValueError(f'expected ) in the runs-on expression, got {close!r}')
            return out
        if tok.startswith("'"):
            return tok[1:-1]
        return self.context[tok]


class RunnerInput(unittest.TestCase):
    def test_each_workflow_takes_an_optional_string_runner(self):
        for name in TAKES_RUNNER:
            with self.subTest(workflow=name):
                spec = inputs(name).get('runner')
                self.assertIsNotNone(spec)
                self.assertEqual(spec.get('type'), 'string')
                self.assertEqual(spec.get('default'), '')
                self.assertFalse(spec.get('required', False))

    def test_every_job_runs_on_the_input_unless_it_needs_a_hosted_runner(self):
        for name in TAKES_RUNNER:
            for job, spec in load(WORKFLOWS / name)['jobs'].items():
                if 'uses' in spec:
                    continue
                with self.subTest(workflow=name, job=job):
                    want = HOSTED.get((name, job)) or CONDITIONAL.get((name, job), POOL)
                    self.assertEqual(spec['runs-on'], want)

    def test_every_call_to_a_workflow_that_takes_it_passes_it_on(self):
        calls = 0
        for name in TAKES_RUNNER:
            for job, spec in load(WORKFLOWS / name)['jobs'].items():
                m = LOCAL_CALL.match(spec.get('uses', ''))
                if not m:
                    continue
                with self.subTest(workflow=name, job=job, calls=m.group(1)):
                    given = (spec.get('with') or {}).get('runner')
                    if m.group(1) == OS_RUNNER:
                        self.assertNotEqual(given, FORWARD)
                    else:
                        self.assertIn(m.group(1), TAKES_RUNNER)
                        self.assertEqual(given, FORWARD)
                        calls += 1
        self.assertGreaterEqual(calls, 8)

    def pinned_inputs(self, sha: str, name: str) -> dict:
        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True)

        if git('cat-file', '-e', f'{sha}^{{commit}}').returncode != 0:
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                self.fail(f'{sha} is missing: self-ci fetches the pinned commits before the suite')
            self.skipTest(f'{sha} is not in this clone; fetch it to check the templates')
        shown = git('show', f'{sha}:.github/workflows/{name}')
        self.assertEqual(shown.returncode, 0, f'{name} does not exist at its pin {sha}')
        return (yaml.safe_load(shown.stdout)[True]['workflow_call'] or {}).get('inputs') or {}

    def test_each_synced_caller_passes_only_inputs_its_pinned_callee_declares(self):
        calls = 0
        for path in sorted(TEMPLATES.glob('*.yml')):
            for job, spec in load(path)['jobs'].items():
                m = PINNED_CALL.match(spec.get('uses', ''))
                if not m:
                    continue
                calls += 1
                given = spec.get('with') or {}
                with self.subTest(template=path.name, job=job):
                    declared = self.pinned_inputs(m.group(2), m.group(1))
                    self.assertLessEqual(set(given), set(declared))
        self.assertGreaterEqual(calls, 4)


class Routing(unittest.TestCase):
    def runs_on(self, workflow: str, job: str, **context: str) -> str:
        spec = load(WORKFLOWS / workflow)['jobs'][job]
        names = {f'inputs.{k}': v for k, v in context.items()}
        return Evaluate(spec['runs-on'], names).value()

    def test_an_empty_input_runs_on_github(self):
        self.assertEqual(self.runs_on('go-ci.yaml', 'validate', runner=''), 'ubuntu-26.04')
        self.assertEqual(self.runs_on('go-ci.yaml', 'validate', runner='pool'), 'pool')

    def test_shell_ci_stays_on_github_when_it_lints_a_dockerfile(self):
        for dockerfile, runner, want in (
            ('', '', 'ubuntu-26.04'),
            ('', 'pool', 'pool'),
            ('./Dockerfile', 'pool', 'ubuntu-26.04'),
            ('./Dockerfile', '', 'ubuntu-26.04'),
        ):
            with self.subTest(dockerfile=dockerfile, runner=runner):
                got = self.runs_on(
                    'shell-ci.yaml', 'validate', dockerfile=dockerfile, runner=runner
                )
                self.assertEqual(got, want)

    def test_the_evaluator_follows_github_short_circuit_rules(self):
        cases = (
            ("${{ a || 'x' }}", {'a': ''}, 'x'),
            ("${{ a || 'x' }}", {'a': 'y'}, 'y'),
            ("${{ (a == '' && b) || 'x' }}", {'a': '', 'b': ''}, 'x'),
            ("${{ (a == '' && b) || 'x' }}", {'a': 'z', 'b': 'y'}, 'x'),
            ("${{ (a == '' && b) || 'x' }}", {'a': '', 'b': 'y'}, 'y'),
        )
        for expression, context, want in cases:
            with self.subTest(expression=expression, context=context):
                self.assertEqual(Evaluate(expression, context).value(), want)


NODE = re.compile(r'(?<![\w./-])(?:npm|npx|node)\s')
GO = re.compile(r'(?<![\w./-])go\s+(?:build|test|vet|install|mod|run|tool|work)\b')
SETUP = {'node': 'actions/setup-node@', 'go': 'actions/setup-go@'}


class PoolReady(unittest.TestCase):
    def moving_jobs(self):
        for name in TAKES_RUNNER:
            for job, spec in load(WORKFLOWS / name)['jobs'].items():
                if 'steps' in spec and (name, job) not in HOSTED:
                    yield name, job, spec['steps']

    def test_no_moving_job_uses_an_action_that_needs_github_s_hosted_image(self):
        for name, job, steps in self.moving_jobs():
            for s in steps:
                for action, binary in NEEDS_HOSTED_IMAGE.items():
                    with self.subTest(workflow=name, job=job, step=s.get('name'), needs=binary):
                        self.assertFalse(s.get('uses', '').startswith(action))
        hosted = load(WORKFLOWS / 'security-scan.yaml')['jobs']['trivy']['steps']
        self.assertTrue(
            any(s.get('uses', '').startswith(tuple(NEEDS_HOSTED_IMAGE)) for s in hosted)
        )

    def test_no_moving_job_installs_with_pipx(self):
        for name, job, steps in self.moving_jobs():
            for s in steps:
                with self.subTest(workflow=name, job=job, step=s.get('name')):
                    self.assertNotRegex(code(s.get('run', '')), r'\bpipx\b')

    def test_node_and_go_run_only_after_their_setup_step(self):
        seen = {'node': 0, 'go': 0}
        for name, job, steps in self.moving_jobs():
            ready = set()
            for s in steps:
                ready |= {
                    tool for tool, uses in SETUP.items() if s.get('uses', '').startswith(uses)
                }
                body = code(s.get('run', ''))
                for tool, pattern in (('node', NODE), ('go', GO)):
                    if pattern.search(body):
                        seen[tool] += 1
                        with self.subTest(workflow=name, job=job, step=s.get('name'), tool=tool):
                            self.assertIn(tool, ready)
        self.assertGreater(seen['node'], 0)
        self.assertGreater(seen['go'], 0)

    def test_actionlint_runs_with_shellcheck_on_the_path(self):
        found = 0
        for name, job, steps in self.moving_jobs():
            installed = False
            for s in steps:
                installed = installed or s.get('name') == 'Install shellcheck'
                if re.search(r'^\s*actionlint\b', code(s.get('run', '')), re.MULTILINE):
                    found += 1
                    with self.subTest(workflow=name, job=job):
                        self.assertTrue(installed)
        self.assertEqual(found, 1)


if __name__ == '__main__':
    unittest.main()
