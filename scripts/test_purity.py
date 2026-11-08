"""purity.py: the per-lane hold rule and the `held-lanes` CLI release-state.sh calls."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import inventory
import promote
import purity

HERE = pathlib.Path(__file__).resolve().parent
GO_MOD = 'module github.com/cplieger/demo{sub}\n\ngo 1.27\n\n'
DEV_PIN = 'require github.com/cplieger/go/filesafe v1.0.0-dev.6\n'
STABLE_PIN = 'require github.com/cplieger/go/filesafe v1.0.0\n'


class LaneHolds(unittest.TestCase):
    def test_a_nested_lane_with_a_dev_pin_is_held_with_its_violations(self):
        files = {
            'wiregen/go.mod': GO_MOD.format(sub='/wiregen') + DEV_PIN,
            'filesafe/go.mod': GO_MOD.format(sub='/filesafe'),
        }
        self.assertEqual(
            purity.lane_holds(files, ['filesafe', 'wiregen']),
            {'wiregen': ['wiregen/go.mod: github.com/cplieger/go/filesafe v1.0.0-dev.6']},
        )

    def test_a_root_pin_is_no_hold(self):
        # The root cannot be held back; promote.py refuses it instead.
        files = {'go.mod': GO_MOD.format(sub='') + DEV_PIN}
        self.assertEqual(purity.lane_holds(files, ['wiregen']), {})

    def test_a_lane_half_counts_for_its_lane(self):
        files = {
            'sse/go.mod': GO_MOD.format(sub='/sse'),
            'sse/web/package.json': json.dumps({'dependencies': {'@cplieger/ui': '1.2.0-dev.3'}}),
        }
        self.assertEqual(
            purity.lane_holds(files, ['sse']),
            {'sse': ['sse/web/package.json: dependencies @cplieger/ui 1.2.0-dev.3']},
        )

    def test_stable_pins_and_non_purity_files_hold_nothing(self):
        files = {
            'wiregen/go.mod': GO_MOD.format(sub='/wiregen') + STABLE_PIN,
            'wiregen/notes.txt': 'github.com/cplieger/go/filesafe v1.0.0-dev.6',
            'wiregen/web/node_modules/x/package.json': json.dumps(
                {'dependencies': {'@cplieger/ui': '1.2.0-dev.3'}}
            ),
        }
        self.assertEqual(purity.lane_holds(files, ['wiregen']), {})

    def test_a_lane_is_matched_by_its_directory_not_its_prefix(self):
        files = {'wiregenx/go.mod': GO_MOD.format(sub='/wiregenx') + DEV_PIN}
        self.assertEqual(purity.lane_holds(files, ['wiregen']), {})

    def test_an_unreadable_manifest_raises(self):
        with self.assertRaises(purity.PurityInputError):
            purity.lane_holds({'sse/web/package.json': '{'}, ['sse'])

    def test_purity_file_selection(self):
        self.assertTrue(purity.is_purity_file('go.mod'))
        self.assertTrue(purity.is_purity_file('web/jsr.json'))
        self.assertTrue(purity.is_purity_file('Dockerfile.arm64'))
        self.assertFalse(purity.is_purity_file('static-src/node_modules/x/package.json'))
        self.assertFalse(purity.is_purity_file('main.go'))


class HeldLanesCli(unittest.TestCase):
    """The CLI reads a commit's tree, not the work tree, as release-state.sh needs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        env = {
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_SYSTEM': os.devnull,
            'GIT_AUTHOR_NAME': 't',
            'GIT_AUTHOR_EMAIL': 't@example.invalid',
            'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't@example.invalid',
        }
        self.env = {**os.environ, **env}
        self.git('init', '-q', '-b', 'main')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ['git', *args], cwd=self.repo, env=self.env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, files):
        for path, text in files.items():
            p = self.repo / path
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                p.write_bytes(text)
            else:
                p.write_text(text)
            self.git('add', path)
        self.git('commit', '-q', '-m', 'c')
        return self.git('rev-parse', 'HEAD')

    def cli(self, commit, lanes='["filesafe","wiregen"]'):
        return subprocess.run(
            [sys.executable, str(HERE / 'purity.py'), 'held-lanes', commit],
            cwd=self.repo,
            env={**self.env, 'GO_LANES_JSON': lanes},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_it_prints_the_held_lanes_of_the_named_commit(self):
        held = self.commit(
            {
                'filesafe/go.mod': GO_MOD.format(sub='/filesafe'),
                'wiregen/go.mod': GO_MOD.format(sub='/wiregen') + DEV_PIN,
            }
        )
        pure = self.commit({'wiregen/go.mod': GO_MOD.format(sub='/wiregen') + STABLE_PIN})
        got = self.cli(held)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(
            json.loads(got.stdout),
            {'wiregen': ['wiregen/go.mod: github.com/cplieger/go/filesafe v1.0.0-dev.6']},
        )
        got = self.cli(pure)
        self.assertEqual((got.returncode, got.stdout.strip()), (0, '{}'), got.stderr)

    def test_an_unreadable_manifest_exits_2(self):
        c = self.commit(
            {'wiregen/go.mod': GO_MOD.format(sub='/wiregen'), 'wiregen/web/jsr.json': '['}
        )
        got = self.cli(c)
        self.assertEqual(got.returncode, 2, got.stdout)
        self.assertIn('wiregen/web/jsr.json', got.stderr)

    def assert_agrees_with_promote(self, commit):
        """The CLI holds exactly the lanes promote.py's preview names at `commit`."""
        repo = inventory.Repo(str(self.repo))
        lanes = repo.lanes(commit)
        got = self.cli(commit, lanes=json.dumps(lanes))
        self.assertEqual(got.returncode, 0, got.stderr)
        want = promote.held_lanes(repo, commit)
        self.assertTrue(want, 'the fixture must hold a lane')
        self.assertEqual(json.loads(got.stdout), want)
        return want

    def test_a_path_git_quotes_is_read_as_promote_reads_it(self):
        c = self.commit(
            {
                'wiregen/go.mod': GO_MOD.format(sub='/wiregen') + STABLE_PIN,
                'wiregen/w.go': 'package wiregen\n',
                'wiregen/wéb "x"/package.json': '{"dependencies": {"@cplieger/x": "1.0.0-dev.1"}}',
            }
        )
        self.assert_agrees_with_promote(c)

    def test_a_manifest_with_a_non_utf8_byte_is_read_as_promote_reads_it(self):
        c = self.commit(
            {
                'wiregen/go.mod': GO_MOD.format(sub='/wiregen').encode()
                + b'// \xff\xfe\n'
                + DEV_PIN.encode(),
                'wiregen/w.go': 'package wiregen\n',
            }
        )
        self.assert_agrees_with_promote(c)
        c = self.commit(
            {
                'wiregen/go.mod': GO_MOD.format(sub='/wiregen') + STABLE_PIN,
                'wiregen/web/package.json': (
                    b'{"description": "\xff", "dependencies": {"@cplieger/x": "1.0.0-dev.1"}}'
                ),
            }
        )
        held = self.assert_agrees_with_promote(c)
        self.assertEqual(
            held, {'wiregen': ['wiregen/web/package.json: dependencies @cplieger/x 1.0.0-dev.1']}
        )

    def test_a_file_name_that_is_not_utf8_does_not_hide_the_tree(self):
        c = self.commit(
            {
                'wiregen/go.mod': GO_MOD.format(sub='/wiregen') + DEV_PIN,
                'wiregen/w.go': 'package wiregen\n',
                os.fsdecode(b'docs/caf\xff.txt'): 'x\n',
            }
        )
        self.assert_agrees_with_promote(c)

    def test_a_bad_commit_or_lane_list_exits_1(self):
        self.commit({'wiregen/go.mod': GO_MOD.format(sub='/wiregen')})
        self.assertEqual(self.cli('0' * 40).returncode, 1)
        self.assertEqual(self.cli('HEAD', lanes='{"wiregen": 1}').returncode, 1)
        self.assertEqual(self.cli('HEAD', lanes='not json').returncode, 1)


if __name__ == '__main__':
    unittest.main()
