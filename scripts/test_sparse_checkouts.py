"""A sparse checkout of this repo's scripts carries every module they import.

The suite runs from a full checkout, so a module missing from a workflow's
sparse list passes here and fails only when the scheduled job imports it.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'


def first_party_imports(path: pathlib.Path) -> set[str]:
    """The scripts/ modules a script imports by name, at any depth of its body."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split('.')[0])
    return {n for n in names if (SCRIPTS / f'{n}.py').is_file()}


def import_closure(entry: str) -> set[str]:
    """Repo-relative paths of every scripts/ module `entry` needs, itself included."""
    seen, todo = set(), [entry]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        todo.extend(f'scripts/{n}.py' for n in first_party_imports(ROOT / rel))
    return seen


def sparse_lists():
    """(file, step name, entries) for every checkout step with a sparse list."""
    files = sorted((ROOT / '.github/workflows').glob('*.y*ml'))
    files += sorted((ROOT / 'actions').glob('*/action.y*ml'))
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        steps = [s for job in (doc.get('jobs') or {}).values() for s in job.get('steps') or []]
        steps += (doc.get('runs') or {}).get('steps') or []
        for step in steps:
            sparse = (step.get('with') or {}).get('sparse-checkout')
            if str(step.get('uses', '')).startswith('actions/checkout@') and sparse:
                entries = [e.strip() for e in str(sparse).splitlines() if e.strip()]
                yield path.relative_to(ROOT).as_posix(), step.get('name', ''), entries


class SparseCheckouts(unittest.TestCase):
    def test_every_sparse_script_list_carries_its_import_closure(self):
        checked = 0
        for wf, step, entries in sparse_lists():
            for entry in entries:
                if not (entry.startswith('scripts/') and entry.endswith('.py')):
                    continue
                checked += 1
                with self.subTest(workflow=wf, step=step, script=entry):
                    self.assertTrue((ROOT / entry).is_file(), f'{entry} is not in this repo')
                    missing = sorted(import_closure(entry) - set(entries))
                    self.assertEqual(missing, [], f'{wf}: {entry} imports what is not checked out')
        self.assertGreater(checked, 0, 'no sparse checkout of a script was found')

    def test_the_closure_follows_imports_transitively(self):
        self.assertIn('scripts/ghrest.py', import_closure('scripts/tracker_issue.py'))
        self.assertIn('scripts/trackerlib.py', import_closure('scripts/links-body.py'))


if __name__ == '__main__':
    unittest.main()
