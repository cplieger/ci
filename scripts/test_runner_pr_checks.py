"""A pull request lints and builds each images/<name>/Dockerfile the release publishes
(ci.yaml's docker job, shell-ci.yaml's hadolint step), and a repository without one
gets the docker and hadolint steps it had before."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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


class DockerJob(unittest.TestCase):
    def setUp(self):
        self.job = job('ci.yaml', 'docker')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_without_runner_images_the_job_runs_the_steps_it_had(self):
        unguarded = [
            s.get('name', s.get('uses', ''))
            for s in self.job['steps']
            if GUARD not in str(s.get('if', ''))
        ]
        self.assertEqual(
            [n.split('@')[0] for n in unguarded],
            [
                'actions/checkout',
                'docker/setup-buildx-action',
                'Resolve OS package refresh key',
                'Build image (no push, no scan)',
                'Image smoke test',
                'Image test suite',
            ],
        )
        guarded = [s['name'] for s in self.job['steps'] if GUARD in str(s.get('if', ''))]
        self.assertEqual(
            guarded,
            [
                'Check out the ci source',
                'Move the ci source out of the build context',
                'Setup Python',
                'Build the runner images (no push)',
            ],
        )
        for s in self.job['steps']:
            if GUARD in str(s.get('if', '')):
                with self.subTest(step=s['name']):
                    self.assertEqual(s['if'], f'${{{{ !cancelled() && {GUARD} }}}}')
        self.assertEqual(
            self.job['timeout-minutes'],
            "${{ needs.detect.outputs.runner_images == 'true' && 60 || 15 }}",
        )

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
                'output': ['type=cacheonly'],
            },
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
