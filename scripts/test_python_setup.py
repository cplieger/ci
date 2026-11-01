"""Every job and composite action that runs Python first sets up the fleet's Python floor.

The runner's own python3 is older than the floor, so a job that skips
actions/setup-python runs the scripts on an interpreter they do not target. The
floor is configs/ruff.toml's target-version.
"""

from __future__ import annotations

import pathlib
import re
import tomllib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
# A python3 call (an option, a script path or a variable after it) or a pip install,
# which lands in whichever interpreter is first on PATH.
PYTHON_CALL = re.compile(r'(?<![\w./-])python3?\s+(-[A-Za-z]|["\']?\$|[\w./{}$"-]*\.py\b)')
PIP_INSTALL = re.compile(r'(?<![\w./-])pip3?\s+install\b')
SETUP = 'actions/setup-python@'


def floor() -> str:
    target = tomllib.loads((ROOT / 'configs/ruff.toml').read_text())['target-version']
    major, minor = re.fullmatch(r'py(\d)(\d+)', target).groups()
    return f'{major}.{minor}'


def runs_python(step: dict) -> bool:
    run = step.get('run') or ''
    return any(
        PYTHON_CALL.search(line) or PIP_INSTALL.search(line)
        for line in run.splitlines()
        if not line.lstrip().startswith('#')
    )


def step_lists():
    for path in sorted(
        [*ROOT.glob('.github/workflows/*.yaml'), *ROOT.glob('actions/*/action.yml')]
    ):
        doc = yaml.safe_load(path.read_text())
        rel = path.relative_to(ROOT).as_posix()
        if 'runs' in doc:
            yield rel, '(composite)', doc['runs'].get('steps') or []
        for name, job in (doc.get('jobs') or {}).items():
            yield rel, name, job.get('steps') or []


class TestPythonSetup(unittest.TestCase):
    def test_every_python_job_sets_up_the_floor_first(self):
        want = floor()
        problems = []
        for rel, job, steps in step_lists():
            first = next((i for i, s in enumerate(steps) if runs_python(s)), None)
            if first is None:
                continue
            setups = [s for s in steps[:first] if str(s.get('uses', '')).startswith(SETUP)]
            versions = {str((s.get('with') or {}).get('python-version')) for s in setups}
            if versions != {want}:
                problems.append(f'{rel} {job}: python-version {sorted(versions) or "missing"}')
        self.assertFalse(
            problems,
            f'add an {SETUP} step with python-version "{want}" before the first step '
            'that runs Python; when the floor moves, bump configs/ruff.toml target-version '
            'and every python-version together:\n' + '\n'.join(problems),
        )

    def test_the_detector_sees_the_call_shapes_in_use(self):
        for run in (
            'python3 scripts/audit.py --file-issues',
            'if ! python3 "${GITHUB_WORKSPACE}/scripts/x.py"; then',
            'metrics=$(python3 -c "',
            'python3 -m unittest discover -s scripts',
            'python3 "$tracker" --repo "$GITHUB_REPOSITORY"',
            'pip install -q --break-system-packages "ruff==${VERSION}"',
        ):
            assert runs_python({'run': run}), run
        for run in (
            'echo "| python   | $run_python | . |"',
            '# python3 scripts/x.py',
            'npm install',
        ):
            assert not runs_python({'run': run}), run


if __name__ == '__main__':
    unittest.main()
