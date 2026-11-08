"""go-embeds.py's pattern matching against Go's path.Match and embed rules.

test-path-significance.sh, test-cliff-bump-semantics.sh and test-render-notes.sh
run it end to end; these cases pin the matcher's edges.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    'go_embeds', pathlib.Path(__file__).resolve().parent / 'go-embeds.py'
)
assert SPEC and SPEC.loader
ge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ge)
SCRIPT = pathlib.Path(__file__).resolve().parent / 'go-embeds.py'


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


class GlobMatch(unittest.TestCase):
    def test_cliff_glob_shapes(self):
        # git-cliff's glob: `**/` may match no directory, `*` crosses slashes,
        # and only `!` negates a class.
        for pattern, path, want in (
            ('**/*.md', 'help.md', True),
            ('**/*.md', 'docs/a/help.md', True),
            ('**/*.md', 'help.txt', False),
            ('*.md', 'docs/help.md', True),
            ('docs/', 'docs/a/help.md', True),
            ('[^a]x', '^x', True),
            ('[^a]x', 'ax', True),
            ('[^a]x', 'bx', False),
            ('[!a]x', 'bx', True),
            ('[!a]x', 'ax', False),
        ):
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bool(ge.glob_re(pattern).match(path)), want)


class WithCommits(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = pathlib.Path(tmp.name)
        self.env = {
            **os.environ,
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_SYSTEM': os.devnull,
            'GIT_AUTHOR_NAME': 'probe',
            'GIT_AUTHOR_EMAIL': 'probe@ci.local',
            'GIT_COMMITTER_NAME': 'probe',
            'GIT_COMMITTER_EMAIL': 'probe@ci.local',
        }
        self.git('init', '-q', '-b', 'main')
        for name, text in (
            ('go.mod', 'module example.com/w\n\ngo 1.26\n'),
            ('help.go', 'package main\n\nimport _ "embed"\n\n//go:embed help.md\nvar h string\n'),
            (
                'cli/cli.go',
                'package cli\n\nimport _ "embed"\n\n//go:embed manual.md\nvar m string\n',
            ),
        ):
            self.write(name, text)
        self.commit('feat: initial')

    def git(self, *args):
        return subprocess.run(
            ['git', *args], cwd=self.repo, env=self.env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message, *files):
        for name in files:
            self.write(name, message)
        self.git('add', '-A')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def run_mode(self, *args):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--with-commits', 'HEAD~3..HEAD', *args],
            cwd=self.repo,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return [m for m in proc.stdout.split('\0') if m]

    def test_config_include_paths_scope_the_embedded_commits(self):
        self.commit('fix: reword the help', 'help.md')
        self.commit('fix: reword the manual', 'cli/manual.md')
        self.commit('fix: a kept change', 'cli/run.go')
        self.write('cliff.toml', '[git]\nexclude_paths = ["**/*.md"]\n')
        self.assertEqual(
            self.run_mode('--config', 'cliff.toml'),
            ['fix: reword the help', 'fix: reword the manual'],
        )
        self.write('cliff.toml', '[git]\nexclude_paths = ["**/*.md"]\ninclude_paths = ["cli/**"]\n')
        self.assertEqual(self.run_mode('--config', 'cliff.toml'), ['fix: reword the manual'])

    def test_positions_give_the_range_order_by_sha_and_message(self):
        one = self.commit('fix: one', 'help.md')
        two = self.commit('fix: two', 'main.go')
        three = self.commit('fix: three', 'help.md')
        self.write('cliff.toml', '[git]\nexclude_paths = ["**/*.md"]\n')
        out = self.repo / 'positions.json'
        self.assertEqual(
            self.run_mode('--config', 'cliff.toml', '--positions', str(out)),
            ['fix: one', 'fix: three'],
        )
        self.assertEqual(
            json.loads(out.read_text()),
            {
                'commits': {one: 0, two: 1, three: 2},
                'messages': {'fix: one': 0, 'fix: three': 2},
            },
        )


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
