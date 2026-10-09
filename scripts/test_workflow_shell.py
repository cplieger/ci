"""Every bash `run:` block of the covered workflows opens with strict mode.

GitHub's default bash invocation supplies errexit and pipefail but not nounset,
and shellcheck does not report a missing prologue, so this is the only gate.
"""

from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
COVERED = (
    '.github/workflows/release.yaml',
    '.github/workflows/docker-release.yaml',
    '.github/workflows/promote.yaml',
    '.github/workflows/ghcr-retention.yaml',
    '.github/workflows/rebuild-stale.yaml',
    '.github/workflows/deadset-ci.yaml',
    'actions/git-cliff-version/action.yml',
    'actions/intake/action.yml',
)
# Named steps in workflows whose other blocks are not held to the prologue.
COVERED_STEPS = (
    ('.github/workflows/ci.yaml', 'scripts', 'Probe docker-release shell semantics'),
    ('.github/workflows/ci.yaml', 'scripts', 'Probe image-test slot'),
    ('.github/workflows/ci.yaml', 'scripts', 'Probe release path significance'),
    ('.github/workflows/ci.yaml', 'scripts', 'Probe reconciliation recogniser'),
    ('.github/workflows/ci.yaml', 'scripts', 'Probe release notes renderer'),
    ('.github/workflows/ci.yaml', 'scripts', 'Probe release detect state machine'),
    ('.github/workflows/ci.yaml', 'scripts', 'Probe two-branch release model end to end'),
    ('.github/workflows/ci.yaml', 'pr-policy', 'Check results'),
    ('.github/workflows/ci.yaml', 'docker', 'Image smoke test'),
    ('.github/workflows/ci.yaml', 'docker', 'Image test suite'),
    ('.github/workflows/self-ci.yaml', 'release-channel-scripts', 'Install PyYAML'),
    (
        '.github/workflows/self-ci.yaml',
        'release-channel-scripts',
        'Probe the release-channel scripts (default python3)',
    ),
)
# `set -uo pipefail` is accepted for a block that inspects exit codes itself.
PROLOGUE = re.compile(r'^set -e?uo pipefail$')


def first_code_line(script: str) -> str:
    for line in script.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            return stripped
    return ''


def bash_run_blocks(path: pathlib.Path):
    doc = yaml.safe_load(path.read_text())
    if 'runs' in doc:
        groups = {'composite': doc['runs'].get('steps') or []}
    else:
        groups = {name: job.get('steps') or [] for name, job in (doc.get('jobs') or {}).items()}
    for group, steps in groups.items():
        for step in steps:
            run = step.get('run')
            if not isinstance(run, str):
                continue
            if not str(step.get('shell', 'bash')).startswith('bash'):
                continue
            yield f'{group}:{step.get("name") or step.get("id")}', run


class StrictMode(unittest.TestCase):
    def test_every_bash_run_block_opens_with_strict_mode(self):
        offenders = []
        seen = 0
        for rel in COVERED:
            for label, run in bash_run_blocks(ROOT / rel):
                seen += 1
                if not PROLOGUE.match(first_code_line(run)):
                    offenders.append(f'{rel} {label}')
        self.assertGreater(
            seen, 40, 'the workflow files changed shape; the scan found too few run blocks'
        )
        self.assertEqual(offenders, [])

    def test_named_steps_in_the_shared_workflows_open_with_strict_mode(self):
        for rel, job, name in COVERED_STEPS:
            with self.subTest(step=f'{rel} {job}:{name}'):
                blocks = dict(bash_run_blocks(ROOT / rel))
                run = blocks.get(f'{job}:{name}')
                self.assertIsNotNone(run, f'{rel} has no bash run block {job}:{name}')
                self.assertTrue(PROLOGUE.match(first_code_line(run)), first_code_line(run))

    def test_prologue_matcher(self):
        self.assertTrue(PROLOGUE.match('set -euo pipefail'))
        self.assertTrue(PROLOGUE.match('set -uo pipefail'))
        self.assertFalse(PROLOGUE.match('set -eo pipefail'))
        self.assertFalse(PROLOGUE.match('set -e'))
        self.assertEqual(first_code_line('# why\n\n  set -euo pipefail\nfoo'), 'set -euo pipefail')
        self.assertEqual(first_code_line('# only comments\n'), '')


if __name__ == '__main__':
    unittest.main()
