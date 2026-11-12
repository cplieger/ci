"""A pull request lints and builds each images/<name>/Dockerfile the release publishes and runs
the repository's integration tests against them (ci.yaml's docker job, shell-ci.yaml's hadolint
step), and a repository without one gets the docker and hadolint steps it had before."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import workflow_replay as wr
import yaml

SCRIPTS = Path(__file__).resolve().parent
WORKFLOWS = SCRIPTS.parent / '.github' / 'workflows'
PKG = 'ghcr.io/owner/app-runner'
GUARD = "needs.detect.outputs.runner_images == 'true'"
POOL = {
    'base': 'FROM scratch\nARG PKG_REFRESH=static\nARG BUILD_VERSION\n',
    'build': f'FROM {PKG}:base-dev\n',
    'docker': f'FROM {PKG}:build-dev\n',
}


def job(workflow: str, name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / workflow).read_text())['jobs'][name]


def step(steps: list[dict], name: str) -> dict:
    return next(s for s in steps if s.get('name') == name)


def git(root: Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    git(root, 'init', '-q')
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
        git(root, 'add', path)
    return root


def run_body(body: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['bash', '-eo', 'pipefail', '-c', body],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', **env},
        check=False,
    )


APP_STEPS = [
    'actions/checkout',
    'docker/setup-buildx-action',
    'Resolve OS package refresh key',
    'Build image (no push, no scan)',
    'Image smoke test',
    'Image test suite',
]
RUNNER_STEPS = [
    'Check out the ci source',
    'Move the ci source out of the build context',
    'Setup Python',
    'Build the runner images (no push)',
]
INTEGRATION_STEPS = [
    'Detect the integration tests',
    'Setup Go',
    'Run the integration tests',
]


def label(s: dict) -> str:
    return s.get('name', s.get('uses', '')).split('@')[0]


def replay(steps: list[dict], runner_images: str, outcomes: dict, outputs: dict) -> list[str]:
    """The steps that run, in order. A step that runs ends with outcomes[its label] (success by
    default) and sets outputs[its label]; a skipped one ends skipped, as on GitHub. Every
    hashFiles() finds its file."""
    contexts = {'needs': {'detect': {'outputs': {'runner_images': runner_images}}}, 'steps': {}}
    status: dict = {}
    scope = wr.Scope(contexts, status, [])
    functions = {**scope.functions(), 'hashFiles': lambda *_: 'h'}
    scope.functions = lambda: functions
    ran = []
    for s in steps:
        cond = s.get('if')
        runs = scope.condition(cond)
        if cond is not None and not wr.STATUS_CALL.search(str(cond)):
            runs = runs and not status.get('failed')
        outcome = outcomes.get(label(s), 'success') if runs else 'skipped'
        if runs:
            ran.append(label(s))
            status['failed'] = status.get('failed') or outcome == 'failure'
        if 'id' in s:
            contexts['steps'][s['id']] = {
                'outcome': outcome,
                'outputs': outputs.get(label(s), {}) if runs else {},
            }
    return ran


class DockerJob(unittest.TestCase):
    def setUp(self):
        self.job = job('ci.yaml', 'docker')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def ran(self, runner_images='true', outcomes=None, found='true') -> list[str]:
        outputs = {'Detect the integration tests': {'found': found}}
        return replay(self.job['steps'], runner_images, outcomes or {}, outputs)

    def test_without_runner_images_the_job_runs_the_steps_it_had(self):
        self.assertEqual(self.ran('false'), APP_STEPS)
        self.assertEqual(
            self.ran('false', {'Build image (no push, no scan)': 'failure'}), APP_STEPS[:4]
        )
        self.assertEqual(
            [label(s) for s in self.job['steps']], APP_STEPS + RUNNER_STEPS + INTEGRATION_STEPS
        )
        for s in self.job['steps']:
            if label(s) in RUNNER_STEPS:
                with self.subTest(step=s['name']):
                    self.assertEqual(s['if'], f'${{{{ !cancelled() && {GUARD} }}}}')
        self.assertEqual(
            self.job['timeout-minutes'],
            "${{ needs.detect.outputs.runner_images == 'true' && 120 || 15 }}",
        )

    def test_the_integration_tests_run_after_the_runner_images_whatever_the_app_image_says(self):
        everything = APP_STEPS + RUNNER_STEPS + INTEGRATION_STEPS
        self.assertEqual(self.ran(), everything)
        app_red = {'Build image (no push, no scan)': 'failure'}
        self.assertEqual(
            self.ran(outcomes=app_red), APP_STEPS[:4] + RUNNER_STEPS + INTEGRATION_STEPS
        )
        self.assertEqual(self.ran(outcomes={'Image test suite': 'failure'}), everything)

    def test_the_integration_tests_wait_on_the_runner_images_go_and_a_suite(self):
        build_red = {'Build the runner images (no push)': 'failure'}
        self.assertEqual(self.ran(outcomes=build_red), APP_STEPS + RUNNER_STEPS)
        self.assertEqual(self.ran(found='false'), APP_STEPS + RUNNER_STEPS + INTEGRATION_STEPS[:1])
        go_red = {'Setup Go': 'failure'}
        self.assertEqual(
            self.ran(outcomes=go_red), APP_STEPS + RUNNER_STEPS + INTEGRATION_STEPS[:2]
        )

    def test_go_comes_from_the_root_go_mod_at_go_ci_s_pin(self):
        setup = step(self.job['steps'], 'Setup Go')
        go_ci = next(
            s for s in job('go-ci.yaml', 'validate')['steps'] if s.get('name') == 'Setup Go'
        )
        self.assertEqual(setup['uses'], go_ci['uses'])
        self.assertEqual(setup['with']['go-version-file'], 'go.mod')

    def integration_found(
        self, files: dict[str, str], untracked: dict[str, str] | None = None
    ) -> str:
        body = step(self.job['steps'], 'Detect the integration tests')['run']
        n = len(list(self.tmp.iterdir()))
        out = self.tmp / f'out{n}'
        root = repo(self.tmp / f'r{n}', files)
        for path, text in (untracked or {}).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        r = run_body(body, root, {'GITHUB_OUTPUT': str(out)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return out.read_text()

    def test_detect_finds_a_tracked_integration_test_beside_a_root_go_mod(self):
        mod = {'go.mod': 'module example.com/app\n'}
        self.assertEqual(
            self.integration_found({**mod, 'internal/x/images_integration_test.go': 'package x'}),
            'found=true\n',
        )
        self.assertEqual(
            self.integration_found({**mod, 'a_integration_test.go': 'package a'}), 'found=true\n'
        )
        self.assertEqual(self.integration_found({**mod, 'a_test.go': 'package a'}), 'found=false\n')
        self.assertEqual(
            self.integration_found({'x/a_integration_test.go': 'package a'}), 'found=false\n'
        )
        self.assertEqual(
            self.integration_found(mod, {'a_integration_test.go': 'package a'}), 'found=false\n'
        )

    def test_the_integration_run_tags_bounds_and_covers_the_root_module(self):
        run = step(self.job['steps'], 'Run the integration tests')
        self.assertEqual(run['env'], {'GOWORK': 'off'})
        (self.tmp / 'bin').mkdir()
        (self.tmp / 'bin' / 'go').write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$STUB_LOG"\nexit "${STUB_RC:-0}"\n'
        )
        (self.tmp / 'bin' / 'go').chmod(0o755)
        log = self.tmp / 'go.log'
        env = {'PATH': f'{self.tmp / "bin"}:{os.environ["PATH"]}', 'STUB_LOG': str(log)}
        r = run_body(run['run'], self.tmp, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text(), 'test -tags integration -count=1 -timeout 45m ./...\n')
        self.assertEqual(run_body(run['run'], self.tmp, {**env, 'STUB_RC': '1'}).returncode, 1)

    def test_the_runner_image_budget_outlasts_the_builds_and_the_suite(self):
        budget = int(re.search(r"== 'true' && (\d+) \|\|", self.job['timeout-minutes'])[1])
        app = int(re.search(r'\|\| (\d+) }}$', self.job['timeout-minutes'])[1])
        runners = job('docker-release.yaml', 'runner-build')['timeout-minutes']
        run = step(self.job['steps'], 'Run the integration tests')['run']
        suite = int(re.search(r'-timeout (\d+)m', run)[1])
        self.assertGreaterEqual(budget, app + runners + suite)

    def detect(self, files: dict[str, str]) -> str:
        body = step(job('ci.yaml', 'detect')['steps'], 'Detect runner images')['run']
        out = self.tmp / f'out{len(list(self.tmp.iterdir()))}'
        root = repo(self.tmp / f'r{len(list(self.tmp.iterdir()))}', files)
        r = run_body(body, root, {'GITHUB_OUTPUT': str(out)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return out.read_text()

    def test_detect_finds_a_tracked_runner_image_beside_a_root_dockerfile(self):
        self.assertEqual(self.detect({'Dockerfile': 'FROM scratch\n'}), 'runner_images=false\n')
        self.assertEqual(
            self.detect(
                {'Dockerfile': 'FROM scratch\n', 'images/base/Dockerfile': 'FROM scratch\n'}
            ),
            'runner_images=true\n',
        )
        self.assertEqual(
            self.detect({'images/base/Dockerfile': 'FROM scratch\n'}), 'runner_images=false\n'
        )
        self.assertEqual(
            self.detect(
                {'Dockerfile': 'x', 'images/a/b/Dockerfile': 'x', 'images/Dockerfile': 'x'}
            ),
            'runner_images=false\n',
        )

    def test_the_build_bakes_every_image_in_plan_order_on_its_siblings_targets(self):
        root = repo(self.tmp / 'r', {f'images/{n}/Dockerfile': t for n, t in POOL.items()})
        (self.tmp / 'bin').mkdir()
        (self.tmp / 'bin' / 'docker').write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >"$STUB_LOG"\ncp "$4" "$STUB_LOG.json"\n'
        )
        (self.tmp / 'bin' / 'docker').chmod(0o755)
        build = step(self.job['steps'], 'Build the runner images (no push)')
        self.assertEqual(
            build['env']['PACKAGE'],
            'ghcr.io/${{ github.repository_owner }}/${{ github.event.repository.name }}-runner',
        )
        log = self.tmp / 'docker.log'
        r = run_body(
            build['run'],
            root,
            {
                'PATH': f'{self.tmp / "bin"}:{os.environ["PATH"]}',
                'STUB_LOG': str(log),
                'CI_TOOLS': str(SCRIPTS),
                'PACKAGE': PKG,
                'RUNNER_TEMP': str(self.tmp),
                'GITHUB_SHA': 'c' * 40,
                'GITHUB_REPOSITORY': 'owner/app',
                'GITHUB_WORKSPACE': str(root),
            },
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text(), f'buildx bake --file {self.tmp / "runner-images.json"}\n')
        bake = json.loads(Path(f'{log}.json').read_text())
        self.assertEqual(bake['group'], {'default': {'targets': ['base', 'build', 'docker']}})
        today = subprocess.run(
            ['date', '-u', '+%Y-%m-%d'], capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(
            bake['target']['base'],
            {
                'context': str(root),
                'dockerfile': 'images/base/Dockerfile',
                'platforms': ['linux/amd64'],
                'args': {'PKG_REFRESH': today},
                'contexts': {},
                'tags': [f'{PKG}:base-dev', f'{PKG}:base-ci'],
                'output': ['type=docker'],
            },
        )
        for name in ('build', 'docker'):
            with self.subTest(image=name):
                self.assertEqual(bake['target'][name]['output'], ['type=docker'])
                self.assertEqual(
                    bake['target'][name]['tags'], [f'{PKG}:{name}-dev', f'{PKG}:{name}-ci']
                )
        self.assertEqual(bake['target']['build']['contexts'], {f'{PKG}:base-dev': 'target:base'})
        self.assertEqual(bake['target']['docker']['contexts'], {f'{PKG}:build-dev': 'target:build'})

    def test_a_refused_plan_fails_the_build_before_bake(self):
        root = repo(self.tmp / 'r', {'images/base/Dockerfile': f'FROM {PKG}:base-v1.0.0\n'})
        build = step(self.job['steps'], 'Build the runner images (no push)')
        r = run_body(
            build['run'],
            root,
            {
                'PATH': f'{self.tmp}:{os.environ["PATH"]}',
                'CI_TOOLS': str(SCRIPTS),
                'PACKAGE': PKG,
                'RUNNER_TEMP': str(self.tmp),
                'GITHUB_SHA': 'c' * 40,
                'GITHUB_REPOSITORY': 'owner/app',
                'GITHUB_WORKSPACE': str(root),
            },
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'::error::images/base/Dockerfile names {PKG}:base-v1.0.0', r.stderr)
        self.assertFalse((self.tmp / 'runner-images.json').exists())


# docker pull succeeds; docker run logs the first line of the Dockerfile on stdin
# and fails when that line is STUB_BAD.
HADOLINT_DOCKER = r"""#!/usr/bin/env bash
[ "$1" = pull ] && exit 0
line=$(head -n 1)
printf '%s\n' "$line" >>"$STUB_LOG"
[ "$line" != "${STUB_BAD:-}" ]
"""


class Hadolint(unittest.TestCase):
    def lint(self, files: dict[str, str], **env) -> tuple[int, list[str], bool]:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            root = repo(t / 'r', files)
            (t / 'docker').write_text(HADOLINT_DOCKER)
            (t / 'docker').chmod(0o755)
            body = step(job('shell-ci.yaml', 'validate')['steps'], 'Validate Dockerfile')['run']
            marker = t / 'failures'
            r = run_body(
                body.replace('/tmp/_ci_failures', str(marker)),
                root,
                {
                    'PATH': f'{t}:{os.environ["PATH"]}',
                    'STUB_LOG': str(t / 'log'),
                    'HADOLINT_IGNORES': 'DL3018,DL3066',
                    'DOCKERFILE': './Dockerfile',
                    **env,
                },
            )
            linted = (t / 'log').read_text().splitlines()
            return r.returncode, linted, marker.exists()

    def test_without_runner_images_only_the_dockerfile_is_linted(self):
        self.assertEqual(self.lint({'Dockerfile': 'FROM app\n'}), (0, ['FROM app'], False))

    def test_every_runner_image_is_linted_after_the_dockerfile(self):
        files = {
            'Dockerfile': 'FROM app\n',
            'images/base/Dockerfile': 'FROM base\n',
            'images/build/Dockerfile': 'FROM build\n',
        }
        self.assertEqual(self.lint(files), (0, ['FROM app', 'FROM base', 'FROM build'], False))

    def test_a_failing_runner_image_fails_the_step_after_every_file_is_linted(self):
        files = {
            'Dockerfile': 'FROM app\n',
            'images/base/Dockerfile': 'FROM base\n',
            'images/build/Dockerfile': 'FROM build\n',
        }
        self.assertEqual(
            self.lint(files, STUB_BAD='FROM base'),
            (1, ['FROM app', 'FROM base', 'FROM build'], True),
        )


if __name__ == '__main__':
    unittest.main()
