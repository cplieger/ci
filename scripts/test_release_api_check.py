"""Tests for release.yaml's API compatibility step: its skip paths, run as the runner runs them."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

RELEASE = Path(__file__).resolve().parent.parent / '.github/workflows/release.yaml'
STEP = 'API compatibility (gorelease)'


def step_body() -> str:
    for step in yaml.safe_load(RELEASE.read_text())['jobs']['go']['steps']:
        if step.get('name') == STEP:
            return re.sub(r'\$\{\{[^}]*\}\}', '', step['run'])
    raise AssertionError(f'release.yaml go has no step named {STEP!r}')


GIT_ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(repo: Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=GIT_ENV)


class SkipPaths(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.repo)], check=False)
        git(self.repo, 'init', '-q')
        (self.repo / 'go.mod').write_text('module example.com/lib\n')
        git(self.repo, 'add', 'go.mod')
        git(
            self.repo,
            '-c',
            'user.name=t',
            '-c',
            'user.email=t@example.com',
            'commit',
            '-qm',
            'init',
        )

    def run_step(self, version: str) -> subprocess.CompletedProcess:
        # GitHub runs a run block as `bash -e {0}`, so -e is part of the contract under test.
        return subprocess.run(
            ['bash', '-e', '-c', step_body()],
            cwd=self.repo,
            env={**os.environ, 'VERSION': version},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_first_release_with_no_stable_tag_skips(self):
        git(self.repo, 'tag', 'v1.0.0-dev.1')
        run = self.run_step('v1.0.0')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('no previous stable tag', run.stdout)

    def test_a_major_bump_skips_after_finding_the_previous_tag(self):
        git(self.repo, 'tag', 'v1.2.0')
        git(self.repo, 'tag', 'v1.10.0')
        run = self.run_step('v2.0.0')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('major bump v1.10.0 -> v2.0.0', run.stdout)


if __name__ == '__main__':
    unittest.main()
