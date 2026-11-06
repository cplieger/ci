"""Every job and composite action that runs Python first sets up the fleet's Python floor.

The runner's own python3 is older than the floor, so a job that skips
actions/setup-python runs the scripts on an interpreter they do not target. The
floor is configs/ruff.toml's target-version. The set-up interpreter carries no
third-party package, so a job installs every one its scripts import.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
import tomllib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
# A python3 call (an option, a script path or a variable after it) or a pip install,
# which lands in whichever interpreter is first on PATH.
PYTHON_CALL = re.compile(r'(?<![\w./-])python3?\s+(-[A-Za-z]|["\']?\$|[\w./{}$"-]*\.py\b)')
PIP_INSTALL = re.compile(r'(?<![\w./-])pip3?\s+install\b')
SETUP = 'actions/setup-python@'
SCRIPT_TOKEN = re.compile(r'([\w./-]+)\.py\b')
# A string literal naming a script file: a test that runs a sibling as a subprocess.
SCRIPT_LITERAL = re.compile(r'[\w.-]+\.py')
# The importable module each pinned pip package provides, where the names differ;
# yamllint depends on PyYAML.
PROVIDES = {'pyyaml': {'yaml'}, 'yamllint': {'yaml', 'yamllint'}}
PIN = re.compile(r'(?<![\w./-])pip3?\s+install[^\n]*?"?([A-Za-z][\w.-]*)==')


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


def third_party(path: pathlib.Path, seen: set[pathlib.Path]) -> set[str]:
    """Top-level modules `path` imports that are neither stdlib nor a sibling script, transitively."""
    if path in seen:
        return set()
    seen.add(path)
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SCRIPT_LITERAL.fullmatch(node.value)
            and (path.parent / node.value).is_file()
        ):
            found |= third_party(path.parent / node.value, seen)
            continue
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        else:
            continue
        for name in (n.split('.')[0] for n in names):
            if name in sys.stdlib_module_names or name == '__future__':
                continue
            local = next(
                (
                    d / f'{name}.py'
                    for d in (path.parent, ROOT / 'scripts')
                    if (d / f'{name}.py').is_file()
                ),
                None,
            )
            found |= third_party(local, seen) if local else {name}
    return found


def installed(steps: list[dict]) -> set[str]:
    pins = {m.lower() for s in steps for m in PIN.findall(s.get('run') or '')}
    return set().union(*(PROVIDES.get(p, {p}) for p in pins))


def scripts_run(steps: list[dict]) -> set[pathlib.Path]:
    run = '\n'.join(
        line
        for s in steps
        for line in (s.get('run') or '').splitlines()
        if not line.lstrip().startswith('#')
    )
    out = set()
    for stem in SCRIPT_TOKEN.findall(run):
        name = pathlib.Path(stem).name + '.py'
        out |= {p for p in ROOT.glob(f'**/{name}') if '.git' not in p.parts}
    if 'unittest discover' in run:
        out |= set(ROOT.glob('scripts/test_*.py'))
    return out


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

    def test_every_python_job_installs_the_packages_its_scripts_import(self):
        problems = []
        for rel, job, steps in step_lists():
            if not any(runs_python(s) for s in steps):
                continue
            needed = set().union(*(third_party(p, set()) for p in scripts_run(steps)))
            missing = sorted(needed - installed(steps))
            if missing:
                problems.append(f'{rel} {job}: {missing}')
        self.assertFalse(
            problems,
            'add a pinned `pip install "<package>==<version>"` step with a `# renovate:` '
            'comment after Setup Python for each module:\n' + '\n'.join(problems),
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

    def test_the_detector_follows_subprocess_scripts_and_skips_comments(self):
        self.assertEqual(scripts_run([{'run': '# see gremlins-aggregate.py\necho hi'}]), set())
        self.assertIn('yaml', third_party(ROOT / 'scripts' / 'test-tracker-body.py', set()))


if __name__ == '__main__':
    unittest.main()
