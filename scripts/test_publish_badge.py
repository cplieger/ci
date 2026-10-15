"""actions/publish-badge/publish-badge.sh against a local bare repository standing in
for github.com, with a `git` wrapper playing a second writer that moves `badges`
between this writer's fetch and its push."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'actions' / 'publish-badge' / 'publish-badge.sh'
URL = 'https://x-access-token:t0k@github.com/o/r.git'

# Before each of the script's first RACE_PUSHES pushes, the wrapper commits
# OTHER_FILE (content: the push's number) on top of the remote's badges tip, or
# as its first commit, straight to the bare repository; each of the first
# FAIL_PUSHES pushes it refuses instead. Every other call is the real git.
GIT_WRAPPER = r"""#!/usr/bin/env bash
set -eu
case " $* " in *" push "*) ;; *) exec "$REAL_GIT" "$@" ;; esac
n=$(( $(cat "$WRAP_STATE/pushes" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$WRAP_STATE/pushes"
if [ "$n" -le "${FAIL_PUSHES:-0}" ]; then
  echo "remote: fatal error in commit_refs" >&2
  exit 1
fi
if [ "$n" -le "${RACE_PUSHES:-0}" ]; then
  w=$(mktemp -d "$WRAP_STATE/other.XXXXXX")
  g() { "$REAL_GIT" -C "$w" "$@"; }
  g init -q
  blob=$(printf '%s\n' "$n" | g hash-object -w --stdin)
  entries=$(printf '100644 blob %s\t%s' "$blob" "$OTHER_FILE")
  if g fetch -q "$BARE" refs/heads/badges 2>/dev/null; then
    entries=$(g ls-tree FETCH_HEAD | grep -v "	$OTHER_FILE\$" || true; echo "$entries")
  fi
  tree=$(printf '%s\n' "$entries" | sed '/^$/d' | g mktree)
  commit=$(g -c user.name=w -c user.email=w@x commit-tree "$tree" -m other)
  g push -q -f "$BARE" "$commit:refs/heads/badges"
fi
exec "$REAL_GIT" "$@"
"""


class PublishBadge(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='publish-badge-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bare = self.tmp / 'remote.git'
        self.git('init', '-q', '--bare', str(self.bare))
        self.state = self.tmp / 'wrapper'
        self.state.mkdir()
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.scratch = self.tmp / 'tmpdir'
        self.scratch.mkdir()
        for name, body in (('git', GIT_WRAPPER), ('sleep', '#!/bin/sh\nexit 0\n')):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def env(self, **extra) -> dict:
        return {
            **os.environ,
            'PATH': f'{self.bin}:{os.environ["PATH"]}',
            'REAL_GIT': shutil.which('git'),
            'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_CONFIG_COUNT': '1',
            'GIT_CONFIG_KEY_0': f'url.{self.bare}.insteadOf',
            'GIT_CONFIG_VALUE_0': URL,
            'BARE': str(self.bare),
            'WRAP_STATE': str(self.state),
            'OTHER_FILE': 'mutation.json',
            'TMPDIR': str(self.scratch),
            **extra,
        }

    def git(self, *args: str) -> str:
        res = subprocess.run(
            ['git', *args],
            env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'},
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout

    def publish(self, json: str = '{"m":"1 MB"}', **extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['bash', str(SCRIPT), 'o/r', 't0k', 'size.json', json],
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )

    def seed(self, files: dict[str, str]):
        """Makes `badges` one commit holding `files`."""
        work = self.tmp / 'seed'
        self.git('init', '-q', str(work))
        for name, body in files.items():
            (work / name).write_text(body)
        self.git('-C', str(work), 'add', '-A')
        self.git('-C', str(work), '-c', 'user.name=s', '-c', 'user.email=s@x', 'commit', '-qm', 's')
        self.git('-C', str(work), 'push', '-q', str(self.bare), 'HEAD:refs/heads/badges')

    def badges(self) -> dict[str, str]:
        names = self.git('-C', str(self.bare), 'ls-tree', '--name-only', 'badges').split()
        return {n: self.git('-C', str(self.bare), 'show', f'badges:{n}').strip() for n in names}

    def commits(self) -> int:
        return int(self.git('-C', str(self.bare), 'rev-list', '--count', 'badges'))

    def test_a_first_badge_creates_the_branch_as_one_commit(self):
        res = self.publish()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.badges(), {'size.json': '{"m":"1 MB"}'})
        self.assertEqual(self.commits(), 1)
        self.assertIn('published size.json to o/r@badges (size.json )', res.stdout)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_siblings_are_kept_ours_replaced_and_other_files_dropped(self):
        self.seed({'mutation.json': 'm', 'size.json': 'old', 'notes.txt': 'x'})
        res = self.publish()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.badges(), {'mutation.json': 'm', 'size.json': '{"m":"1 MB"}'})
        self.assertEqual(self.commits(), 1)

    def test_a_writer_that_moved_the_branch_after_the_fetch_keeps_its_file(self):
        self.seed({'size.json': 'old'})
        res = self.publish(RACE_PUSHES='1')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.badges(), {'mutation.json': '1', 'size.json': '{"m":"1 MB"}'})
        self.assertEqual(self.commits(), 1)
        self.assertIn('the branch moved (attempt 1/5), so it is read again', res.stderr)

    def test_a_writer_that_created_the_branch_first_keeps_its_file(self):
        res = self.publish(RACE_PUSHES='1')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.badges(), {'mutation.json': '1', 'size.json': '{"m":"1 MB"}'})

    def test_a_refused_push_is_retried(self):
        res = self.publish(FAIL_PUSHES='2')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.badges(), {'size.json': '{"m":"1 MB"}'})
        self.assertIn('(attempt 2/5)', res.stderr)

    def test_a_branch_that_keeps_moving_fails_after_five_attempts_and_drops_nothing(self):
        self.seed({'size.json': 'old'})
        res = self.publish(RACE_PUSHES='5')
        self.assertEqual(res.returncode, 1)
        self.assertIn('push to o/r@badges failed after 5 attempts', res.stderr)
        self.assertEqual(self.badges(), {'mutation.json': '5', 'size.json': 'old'})
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_an_unreadable_remote_publishes_nothing(self):
        res = self.publish(GIT_CONFIG_KEY_0=f'url.{self.tmp}/missing.git.insteadOf')
        self.assertEqual(res.returncode, 1)
        self.assertIn('failed after 5 attempts', res.stderr)
        self.assertEqual(self.git('-C', str(self.bare), 'for-each-ref'), '')


if __name__ == '__main__':
    unittest.main()
