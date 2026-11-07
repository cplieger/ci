"""go-embeds.py's pattern matching against Go's path.Match and embed rules.

test-path-significance.sh, test-cliff-bump-semantics.sh and test-render-notes.sh
run it end to end; these cases pin the matcher's edges.
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SPEC = importlib.util.spec_from_file_location(
    'go_embeds', pathlib.Path(__file__).resolve().parent / 'go-embeds.py'
)
assert SPEC and SPEC.loader
ge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ge)


class ElementMatch(unittest.TestCase):
    def test_path_match_shapes(self):
        for pattern, name, want in (
            ('*.md', 'manual.md', True),
            ('*.md', 'manual.txt', False),
            ('?.md', 'a.md', True),
            ('[a-c].md', 'b.md', True),
            ('[^a].md', 'b.md', True),
            ('[^a].md', 'a.md', False),
            ('[\\]].md', '].md', True),
            ('a\\*', 'a*', True),
            ('a\\*', 'ab', False),
        ):
            with self.subTest(pattern=pattern, name=name):
                self.assertEqual(bool(ge.element_re(pattern).match(name)), want)

    def test_a_pattern_path_match_rejects_matches_nothing(self):
        # path.Match's getEsc refuses `]` or `-` as a class's first character
        # and an unterminated class or trailing backslash; Go's build refuses
        # such a directive, so it embeds nothing rather than aborting detection.
        for pattern in ('[]a]', '[-a]', '[a', 'a\\', '[a-]'):
            with self.subTest(pattern=pattern):
                self.assertIs(ge.element_re(pattern), ge.NEVER)


class Embeds(unittest.TestCase):
    def test_a_directory_skips_dot_and_underscore_names_unless_all(self):
        self.assertTrue(ge.embeds('static', 'static/index.md'))
        self.assertFalse(ge.embeds('static', 'static/.hidden.md'))
        self.assertFalse(ge.embeds('static', 'static/_draft/x.md'))
        self.assertTrue(ge.embeds('all:static', 'static/.hidden.md'))

    def test_quoted_patterns(self):
        self.assertEqual(
            ge.patterns('"a b.md" `c.md` d.md all:e'), ['a b.md', 'c.md', 'd.md', 'all:e']
        )

    def test_files_go_never_builds(self):
        for name, want in (
            ('cli/manual.go', False),
            ('tools/testdata/t.go', True),
            ('vendor/x/y.go', True),
            ('_old/o.go', True),
            ('.hidden/h.go', True),
        ):
            with self.subTest(name=name):
                self.assertEqual(ge.go_skips(name), want)


if __name__ == '__main__':
    unittest.main()
