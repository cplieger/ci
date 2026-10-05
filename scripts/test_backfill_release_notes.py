"""Tests for the carve-out list and pair selection of backfill-release-notes.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_backfill():
    spec = importlib.util.spec_from_file_location('backfill', HERE / 'backfill-release-notes.py')
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves the module's annotations through sys.modules.
    sys.modules['backfill'] = module
    spec.loader.exec_module(module)
    return module


backfill = load_backfill()

LIST = """\
carve_outs:
  - repo: owner/one
    tag: v2.0.0
    reason: hand-written migration steps
  - repo: owner/other
    tag: v1.1.0
    reason: provenance note
"""
PAIRS = [('v1.0.0', 'v1.1.0'), ('v1.1.0', 'v2.0.0'), ('v2.0.0', 'v2.1.0')]


class CarveOutListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'carve-outs.yaml'

    def load(self, text: str, repo: str) -> dict[str, str]:
        self.path.write_text(text, encoding='utf-8')
        return backfill.load_carve_outs(self.path, repo)

    def assert_refused(self, text: str) -> str:
        self.path.write_text(text, encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            backfill.load_carve_outs(self.path, 'owner/one')
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(str(self.path), err.getvalue())
        return err.getvalue()

    def test_only_this_repos_entries_are_returned(self):
        self.assertEqual(self.load(LIST, 'owner/one'), {'v2.0.0': 'hand-written migration steps'})
        self.assertEqual(self.load(LIST, 'owner/none'), {})

    def test_a_malformed_list_exits_2_and_names_the_file(self):
        self.assert_refused('carve_outs: {}\n')
        self.assert_refused('carve_outs: [\n')
        self.assertIn('entry 2', self.assert_refused(LIST.replace('tag: v1.1.0', 'tag: 1.1.0')))
        self.assertIn(
            'entry 1',
            self.assert_refused(LIST.replace('    reason: hand-written migration steps\n', '')),
        )

    def test_a_missing_list_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            backfill.load_carve_outs(self.path, 'owner/one')
        self.assertEqual(raised.exception.code, 2)

    def test_the_committed_list_loads(self):
        carved = backfill.load_carve_outs(backfill.CARVE_OUTS, 'cplieger/web-terminal-engine')
        self.assertEqual(sorted(carved), ['v2.7.0', 'v5.0.0'])
        self.assertTrue(all(carved.values()))


class SelectPairsTests(unittest.TestCase):
    def test_a_carved_tag_is_skipped_with_its_reason(self):
        selected, skipped = backfill.select_pairs(
            PAIRS, [], {'v2.0.0': 'why'}, include_carved=False
        )
        self.assertEqual(selected, [('v1.0.0', 'v1.1.0'), ('v2.0.0', 'v2.1.0')])
        self.assertEqual(skipped, [('v2.0.0', 'why')])

    def test_include_carved_plans_every_pair(self):
        selected, skipped = backfill.select_pairs(PAIRS, [], {'v2.0.0': 'why'}, include_carved=True)
        self.assertEqual(selected, PAIRS)
        self.assertEqual(skipped, [])

    def test_only_still_skips_a_carved_tag(self):
        selected, skipped = backfill.select_pairs(
            PAIRS, ['v2.0.0', 'v2.1.0'], {'v2.0.0': 'why'}, include_carved=False
        )
        self.assertEqual(selected, [('v2.0.0', 'v2.1.0')])
        self.assertEqual(skipped, [('v2.0.0', 'why')])

    def test_nothing_carved_selects_the_only_set(self):
        selected, skipped = backfill.select_pairs(PAIRS, ['v1.1.0'], {}, include_carved=False)
        self.assertEqual(selected, [('v1.0.0', 'v1.1.0')])
        self.assertEqual(skipped, [])


if __name__ == '__main__':
    unittest.main()
