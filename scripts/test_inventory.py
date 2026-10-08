"""Tests for inventory.py: comparators, parsers, the fixture histories and the CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import inventory
from inventory import Record

FIXTURES = Path(__file__).resolve().parent / 'testdata' / 'inventory'
HEX1 = '1' * 64
HEX2 = '2' * 64


_SAVED_ENV: dict[str, str | None] = {}


def setUpModule():
    for name, value in (('GIT_CONFIG_GLOBAL', os.devnull), ('GIT_CONFIG_NOSYSTEM', '1')):
        _SAVED_ENV[name] = os.environ.get(name)
        os.environ[name] = value


def tearDownModule():
    for name, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def load_history(name: str) -> list[tuple[str, str | None, dict[str, str | None]]]:
    """`@@ commit NAME [parent P]`, then `@@ file PATH` + content or `@@ delete PATH`."""
    commits, path, body = [], None, []

    def flush():
        if path is not None:
            commits[-1][2][path] = ''.join(line + '\n' for line in body)

    for line in (FIXTURES / name / 'history.txt').read_text(encoding='utf-8').splitlines():
        if not line.startswith('@@ '):
            body.append(line)
            continue
        flush()
        path, body = None, []
        words = line.split()
        if words[1] == 'commit':
            parent = words[4] if len(words) > 3 and words[3] == 'parent' else None
            commits.append((words[2], parent, {}))
        elif words[1] == 'file':
            path = words[2]
        elif words[1] == 'delete':
            commits[-1][2][words[2]] = None
    flush()
    return commits


class Fixture:
    def __init__(self, name: str):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.dir)], check=True)
        stream, marks, trees = bytearray(), {}, {}
        for i, (commit, parent, changes) in enumerate(load_history(name), 1):
            tree = dict(trees[parent]) if parent else {}
            for p, content in changes.items():
                if content is None:
                    tree.pop(p, None)
                else:
                    tree[p] = content
            trees[commit], marks[commit] = tree, i
            msg = commit.encode()
            stream += f'commit refs/heads/{commit}\nmark :{i}\n'.encode()
            stream += f'committer t <t@example.invalid> {1_700_000_000 + i} +0000\n'.encode()
            stream += f'data {len(msg)}\n'.encode() + msg + b'\n'
            if parent:
                stream += f'from :{marks[parent]}\n'.encode()
            stream += b'deleteall\n'
            for p in sorted(tree):
                data = tree[p].encode()
                stream += f'M 100644 inline {p}\ndata {len(data)}\n'.encode() + data + b'\n'
        subprocess.run(
            ['git', '-C', str(self.dir), 'fast-import', '--quiet'], input=bytes(stream), check=True
        )
        self.repo = inventory.Repo(str(self.dir))
        self.sha = {c: self.repo.commit(c) for c in marks}

    def records(self, commit: str, lane: str | None = None) -> list[Record]:
        return self.repo.records(self.sha[commit], lane)

    def dominance(self, base, main, target, owned=('.editorconfig',), canonical=None) -> dict:
        with tempfile.TemporaryDirectory() as cdir:
            for p, text in (canonical or {}).items():
                (Path(cdir) / p).parent.mkdir(parents=True, exist_ok=True)
                (Path(cdir) / p).write_text(text, encoding='utf-8')
            patterns = [inventory.glob_pattern(p) for p in owned]
            return inventory.dominance(
                self.repo, self.sha[base], self.sha[main], self.sha[target], patterns, Path(cdir)
            )

    def diff(self, a, b, lane=None, security=()) -> dict:
        return inventory.diff(self.repo, self.sha[a], self.sha[b], lane, set(security))

    def lose_blob(self, commit: str, path: str) -> None:
        """Delete one file's object from the store, its tree entry kept."""
        git = ['git', '-C', str(self.dir)]
        oid = subprocess.run(
            [*git, 'rev-parse', f'{self.sha[commit]}:{path}'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        packs = self.dir / '.git' / 'objects' / 'pack'
        moved = []
        for pack in sorted(packs.iterdir()):
            if pack.suffix == '.pack':
                moved.append(pack.rename(self.dir / pack.name))
            else:
                pack.unlink()
        for pack in moved:
            with pack.open('rb') as data:
                subprocess.run([*git, 'unpack-objects', '-q'], stdin=data, check=True)
            pack.unlink()
        (self.dir / '.git' / 'objects' / oid[:2] / oid[2:]).unlink()

    def close(self):
        self._tmp.cleanup()


class FixtureCase(unittest.TestCase):
    fixture = ''

    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture(cls.fixture)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def failing(self, result: dict) -> list[tuple[str, str]]:
        out = []
        for p in result['paths']:
            if p['status'] != 'ok' and not p['records']:
                out.append((p['path'], p['reason']))
            out += [(p['path'], r['identity']) for r in p['records'] if r['status'] != 'ok']
        return out


class Comparators(unittest.TestCase):
    def table(self, cmp, rows):
        for a, b, want in rows:
            with self.subTest(a=a, b=b):
                self.assertEqual(cmp(a, b), want)

    def test_go_semver(self):
        self.table(
            inventory.cmp_go_semver,
            [
                ('v1.2.4', 'v1.2.3', 1),
                ('v1.10.0', 'v1.9.0', 1),
                ('v1.2.3', 'v1.2.3-rc.1', 1),
                ('v1.2.3-rc.2', 'v1.2.3-rc.10', -1),
                ('v1.2.3-alpha', 'v1.2.3-1', 1),
                ('v2.1.0+incompatible', 'v2.0.0+incompatible', 1),
                ('v2.0.0+incompatible', 'v2.0.0', 0),
                ('v1', 'v1.0.0', 0),
                ('v1.2', 'v1.2.0', 0),
                ('v0.0.0-20260201000000-bbbbbbbbbbbb', 'v0.0.0-20260101000000-aaaaaaaaaaaa', 1),
                ('v1.2.4-0.20260101000000-aaaaaaaaaaaa', 'v1.2.3', 1),
                ('v1.2.4-0.20260101000000-aaaaaaaaaaaa', 'v1.2.4', -1),
                ('1.2.3', 'v1.2.3', None),
                ('master', 'v1.0.0', None),
                ('master', 'master', None),
                ('v1.0.0+..', 'v1.0.0', None),
                ('v01.2.3', 'v1.2.3', None),
                ('v1.2-pre', 'v1.2.0', None),
            ],
        )

    def test_npm_semver(self):
        self.table(
            inventory.cmp_semver,
            [
                ('1.4.1', '1.3.0', 1),
                ('2.1.0-dev.3', '2.0.1', 1),
                ('2.1.0-dev.3', '2.1.0', -1),
                ('2.1.0-dev.10', '2.1.0-dev.9', 1),
                ('1.0.0+build.2', '1.0.0+build.1', 0),
                ('1.0.0+20260101.sha-1a2b', '1.0.0', 0),
                ('1.0.0-rc.1+b.1', '1.0.0-rc.1', 0),
                ('1.0.0+..', '1.0.0', None),
                ('1.0.0+a..b', '1.0.0', None),
                ('1.0.0+a.', '1.0.0', None),
                ('1.0.0+.a', '1.0.0', None),
                ('1.0.0+', '1.0.0', None),
                ('1.0.0+..', '1.0.0+..', None),
                ('1.0.0-01', '1.0.0', None),
                ('v1.0.0', '1.0.0', None),
                ('1.0', '1.0.0', None),
            ],
        )

    def test_pep440(self):
        self.table(
            inventory.cmp_pep440,
            [
                ('2.33.0rc1', '2.32.4', 1),
                ('2.32.4.dev1', '2.32.4', -1),
                ('2.32.4a1', '2.32.4.dev1', 1),
                ('2.32.4.post1', '2.32.4', 1),
                ('2.32.4-1', '2.32.4.post1', 0),
                ('1.0', '1.0.0', 0),
                ('1!0.1', '2.0', 1),
                ('1.0+local.1', '1.0', 1),
                ('1.0+abc', '1.0+1', -1),
                ('1.0.post1.dev2', '1.0.post1', -1),
                ('1.0rc1', '1.0c1', 0),
                ('1.0alpha2', '1.0a2', 0),
                ('1.0.dev', '1.0.dev0', 0),
                ('1.0+dev5', '1.0', 1),
                ('1.0+dev5', '1.0.dev5', 1),
                ('not-a-version', '1.0', None),
            ],
        )

    def test_go_version(self):
        self.table(
            inventory.cmp_go_version,
            [
                ('1.27.2', '1.27.1', 1),
                ('1.28.0', '1.27.9', 1),
                ('1.28rc1', '1.27.2', 1),
                ('1.28rc1', '1.28.0', -1),
                ('1.28', '1.28rc1', -1),
                ('1.28beta1', '1.28rc1', -1),
                ('go1.27.1', '1.27.1', 0),
                ('default', '1.27.1', None),
            ],
        )

    def test_docker_tags(self):
        self.table(
            inventory.cmp_docker,
            [
                ('3.24.3', '3.24.2', 1),
                ('3.25.0', '3.24.9', 1),
                ('2.12-builder', '2.11-builder', 1),
                ('1.27-alpine', '1.27-trixie', None),
                ('3.24', '3.24.2', None),
                ('trixie-slim', 'trixie-slim', None),
                ('latest', 'latest', None),
            ],
        )

    def test_loose_pins(self):
        self.table(
            inventory.cmp_loose,
            [
                ('v3.2.1', 'v3.2.0', 1),
                ('v5.9.5.3', 'v5.9.5.2', 1),
                ('8.1', '8.1.0', 0),
                ('9.0', '8.1.2', 1),
                ('v1.0.0', '1.0.0', 0),
                ('1.0.0-rc.2', '1.0.0-rc.1', 1),
                ('1.0.0', '1.0.0-rc.1', 1),
                ('v2026.10.03', 'v2026.9.30', 1),
                ('stable', 'stable', None),
                ('n8.1', '8.1', None),
            ],
        )


class Judge(unittest.TestCase):
    def rec(self, value, digest='', versioning='go-semver', kind='direct'):
        return Record(
            '.', 'go', 'go.mod', 'example.com/x', value, digest, kind=kind, versioning=versioning
        )

    @staticmethod
    def npm(*values):
        return [
            Record('.', 'npm', 'package-lock.json', 'minimatch', v, versioning='semver')
            for v in values
        ]

    def judge(self, main, candidates, history=(), base=(), every=False):
        return inventory.judge(
            main,
            list(base),
            candidates,
            {(r.slot, r.state) for r in history},
            every=every,
        )[0]

    def test_higher_target_dominates(self):
        self.assertEqual(
            self.judge([self.rec('v1.1.0')], [self.rec('v1.2.0')], base=[self.rec('v1.0.0')]), 'ok'
        )

    def test_lower_target_fails_even_when_main_value_was_held(self):
        m = self.rec('v1.1.0')
        self.assertEqual(self.judge([m], [self.rec('v1.0.0')], history=[m]), 'fail')

    def test_absent_target_with_no_history_passes_only_when_the_base_had_it(self):
        self.assertEqual(self.judge([self.rec('v1.1.0')], [], base=[self.rec('v1.0.0')]), 'ok')
        self.assertEqual(self.judge([self.rec('v1.1.0')], []), 'fail')

    def test_a_slot_main_added_may_be_absent_only_after_dev_held_a_carrier(self):
        m = self.rec('v1.1.0')
        other = Record('.', 'go', 'go.mod', 'example.com/y', 'v1.1.0', versioning='go-semver')
        self.assertEqual(
            inventory.judge([m], [], [], {(m.slot, m.state)}), ('ok', 'held on dev, then removed')
        )
        self.assertEqual(self.judge([m], [], history=[self.rec('v1.2.0')]), 'ok')
        for held in (self.rec('v1.0.0'), self.rec('v1.1.0', kind='dev'), self.rec('master'), other):
            with self.subTest(held=held):
                self.assertEqual(
                    inventory.judge([m], [], [], {(held.slot, held.state)}),
                    ('fail', 'absent at target'),
                )
        self.assertEqual(self.judge([m, self.rec('v1.3.0')], [], history=[m]), 'fail')

    def test_opaque_value_dominates_by_history_only(self):
        m = self.rec('', HEX2, versioning='')
        t = [self.rec('', HEX1, versioning='')]
        self.assertEqual(self.judge([m], t), 'fail')
        self.assertEqual(self.judge([m], t, history=[m]), 'ok')

    def test_strict_value_that_does_not_parse_is_uncomparable(self):
        self.assertEqual(self.judge([self.rec('v1.1.0')], [self.rec('master')]), 'uncomparable')

    def test_equal_strict_values_are_parsed_before_they_compare_equal(self):
        base = [self.rec('v1.0.0')]
        for value, versioning in (
            ('master', 'go-semver'),
            ('1.0.0+..', 'semver'),
            ('default', 'go-version'),
            ('not-a-version', 'pep440'),
        ):
            with self.subTest(value=value):
                m = self.rec(value, versioning=versioning)
                self.assertEqual(self.judge([m], [m], history=[m], base=base), 'uncomparable')
        valid = self.rec('v1.1.0')
        self.assertEqual(self.judge([valid], [valid], base=base), 'ok')

    def test_an_unparsed_value_on_main_or_at_base_is_uncomparable_before_any_shortcut(self):
        good, low, bad = self.rec('v1.1.0'), self.rec('v1.0.0'), self.rec('master')
        # (main, base, candidates, verdict once the unparsed value is a valid one).
        for main, base, candidates, valid in (
            ([bad], [], [], 'fail'),
            ([], [bad], [], 'ok'),
            ([bad], [low], [], 'ok'),
            ([good], [bad], [], 'ok'),
            ([good], [bad], [self.rec('v1.2.0')], 'ok'),
        ):
            with self.subTest(main=main, base=base, candidates=candidates):
                fixed_main = [good if r is bad else r for r in main]
                fixed_base = [low if r is bad else r for r in base]
                self.assertEqual(self.judge(fixed_main, candidates, base=fixed_base), valid)
                where = 'on main' if bad in main else 'at base'
                self.assertEqual(
                    inventory.judge(main, base, candidates, set()),
                    ('uncomparable', f"'master' {where} is not go-semver"),
                )

    def test_a_newer_copy_cannot_hide_an_untouched_retired_copy(self):
        (m,), (old,), (newer,) = self.npm('3.1.2'), self.npm('3.0.4'), self.npm('3.2.0')
        self.assertEqual(self.judge([m], [old, newer], base=[old]), 'fail')
        self.assertEqual(self.judge([m], [newer], base=[old]), 'ok')
        self.assertEqual(self.judge([m], [*self.npm('3.0.1'), newer], base=[old]), 'ok')

    def test_a_value_main_removed_must_be_gone_from_the_target(self):
        old = self.rec('v1.0.0')
        self.assertEqual(self.judge([], [old], base=[old]), 'fail')
        self.assertEqual(self.judge([], [self.rec('v1.1.0')], base=[old]), 'ok')
        self.assertEqual(self.judge([], [], base=[old]), 'ok')

    def test_a_value_main_removed_cannot_return_lower(self):
        old = self.rec('v2.0.0')
        lower = self.rec('v1.0.0')
        self.assertEqual(
            inventory.judge([], [old], [lower], set()),
            ('fail', 'target has v1.0.0, not a move forward from v2.0.0, which main removed'),
        )
        self.assertEqual(self.judge([], [lower], history=[old], base=[old]), 'fail')
        self.assertEqual(self.judge([], [self.rec('v2.0.0', kind='dev')], base=[old]), 'fail')
        self.assertEqual(self.judge([], [self.rec('master')], base=[old]), 'uncomparable')

    def test_a_value_main_removed_may_return_with_a_newer_digest_or_opaque_value(self):
        self.assertEqual(
            self.judge([], [self.rec('v1.0.0', HEX2)], base=[self.rec('v1.0.0', HEX1)]), 'ok'
        )
        opaque = [self.rec('', HEX1, versioning='')]
        self.assertEqual(self.judge([], [self.rec('', HEX2, versioning='')], base=opaque), 'ok')

    def test_a_copy_main_dropped_cannot_return_lower_beside_the_copies_main_kept(self):
        (v1,), (v2,), (v3,) = self.npm('1.0.0'), self.npm('2.0.0'), self.npm('3.0.0')
        self.assertEqual(self.judge([v2], [v2, v1], base=[v2, v2]), 'fail')
        self.assertEqual(self.judge([v2], [v2, v3], base=[v2, v2]), 'ok')
        self.assertEqual(self.judge([v1], [v1, v1], base=[v1, v2]), 'fail')
        self.assertEqual(self.judge([v1], [v1, v3], base=[v1, v2]), 'ok')
        self.assertEqual(self.judge([], [v3], base=[v2, v3]), 'fail')
        self.assertEqual(self.judge([], self.npm('2.5.0'), base=[v2, v3]), 'fail')
        self.assertEqual(self.judge([], self.npm('4.0.0'), base=[v2, v3]), 'ok')

    def test_copies_of_a_retired_state_count_against_the_copies_main_kept(self):
        (m,), (old,), (newer,) = self.npm('3.1.2'), self.npm('3.0.4'), self.npm('3.2.0')
        self.assertEqual(self.judge([old, m], [old, old, newer], base=[old, old]), 'fail')
        self.assertEqual(self.judge([old, m], [old, newer], base=[old, old]), 'ok')
        self.assertEqual(self.judge([old], [old, old], base=[old, old]), 'ok')

    def test_a_copy_main_left_unchanged_carries_nothing(self):
        base, main = self.npm('9.0.5', '3.0.4', '3.0.4'), self.npm('9.0.5', '3.1.2', '3.0.4')
        self.assertEqual(self.judge(main, self.npm('9.0.5', '3.0.4', '3.0.5'), base=base), 'fail')
        self.assertEqual(self.judge(main, self.npm('9.0.5', '3.0.5', '3.0.5'), base=base), 'fail')
        self.assertEqual(self.judge(main, self.npm('9.0.5', '3.2.0', '3.0.4'), base=base), 'ok')

    def test_each_gained_copy_needs_its_own_carrier(self):
        base, main = self.npm('9.0.5', '3.0.4', '4.0.0'), self.npm('9.0.5', '3.1.2', '4.0.1')
        self.assertEqual(self.judge(main, self.npm('9.0.5', '3.2.0', '4.0.2'), base=base), 'ok')
        self.assertEqual(self.judge(main, self.npm('9.0.5', '4.0.2', '3.0.9'), base=base), 'fail')

    def test_a_copy_dev_deduplicated_may_be_carried_by_any_copy(self):
        base, main = self.npm('9.0.5', '3.0.4'), self.npm('9.0.5', '3.1.2')
        self.assertEqual(self.judge(main, self.npm('9.0.5'), base=base), 'ok')
        self.assertEqual(self.judge(main, self.npm('3.0.5'), base=base), 'fail')

    def test_same_value_in_another_role_does_not_dominate(self):
        m = self.rec('v1.0.0')
        self.assertEqual(self.judge([m], [self.rec('v1.0.0', kind='dev')]), 'fail')

    def test_a_newer_value_in_another_role_does_not_dominate(self):
        base, m = [self.rec('v1.0.0')], self.rec('v1.1.0')
        for kind in ('dev', 'indirect'):
            with self.subTest(kind=kind):
                newer = self.rec('v1.2.0', kind=kind)
                self.assertEqual(
                    inventory.judge([m], base, [newer], set()),
                    ('fail', f'target has v1.2.0; v1.2.0 is {kind} at target, not direct'),
                )
                self.assertEqual(self.judge([m], [newer], history=[m], base=base), 'fail')
                self.assertEqual(
                    inventory.judge([], base, [newer], set()),
                    (
                        'fail',
                        (
                            'target has v1.2.0, not a move forward from v1.0.0, which main removed; '
                            f'v1.2.0 is {kind} at target, not direct'
                        ),
                    ),
                )
        self.assertEqual(self.judge([m], [self.rec('v1.2.0')], base=base), 'ok')

    def test_every_refusal_names_a_copy_in_another_role(self):
        dev = Record(
            '.', 'npm', 'package-lock.json', 'minimatch', '3.2.0', kind='dev', versioning='semver'
        )
        rows = [
            (
                [self.rec('v1.1.0')],
                [self.rec('v1.0.0', kind='indirect')],
                [self.rec('v1.2.0'), self.rec('v1.0.0', kind='indirect')],
                False,
                (
                    'target still holds v1.0.0, which main replaced; '
                    'v1.0.0 is indirect at target, not direct'
                ),
            ),
            (
                self.npm('9.0.5', '3.1.2', '4.0.1'),
                self.npm('9.0.5', '3.0.4', '4.0.0'),
                [*self.npm('9.0.5', '4.0.2'), dev],
                False,
                (
                    'target has 3.2.0, 4.0.2, 9.0.5, and its changed copies do not carry 3.1.2, 4.0.1; '
                    '3.2.0 is dev at target, not direct'
                ),
            ),
            (
                [self.rec('v1.1.0')],
                [self.rec('v1.0.0')],
                [self.rec('v1.2.0'), self.rec('v1.2.0', kind='dev')],
                True,
                'target has v1.2.0; v1.2.0 is dev at target, not direct',
            ),
            (
                [self.rec('v1.1.0')],
                [self.rec('v1.0.0')],
                [self.rec('v1.0.5')],
                False,
                'target has v1.0.5',
            ),
        ]
        for main, base, candidates, every, reason in rows:
            with self.subTest(reason=reason):
                self.assertEqual(
                    inventory.judge(main, base, candidates, set(), every=every), ('fail', reason)
                )
        # The `every` row's direct copy carries main's value on its own.
        status, _ = inventory.judge(
            [self.rec('v1.1.0')],
            [self.rec('v1.0.0')],
            [self.rec('v1.2.0'), self.rec('v1.2.0', kind='dev')],
            set(),
        )
        self.assertEqual(status, 'ok')

    def test_a_slot_found_elsewhere_needs_every_copy_to_dominate(self):
        m = self.rec('v1.1.0')
        both = [self.rec('v1.2.0'), self.rec('v1.0.5')]
        self.assertEqual(self.judge([m], both, base=[self.rec('v1.0.0')]), 'ok')
        self.assertEqual(self.judge([m], both, base=[self.rec('v1.0.0')], every=True), 'fail')


class Surfaces(unittest.TestCase):
    def test_kinds(self):
        rows = {
            'go.mod': 'gomod',
            'tools/go.mod': 'gomod',
            'go.sum': 'gosum',
            'web/package.json': 'package-json',
            'package-lock.json': 'npm-lock',
            'npm-shrinkwrap.json': 'npm-lock',
            'uv.lock': 'uv-lock',
            'Dockerfile': 'dockerfile',
            'Dockerfile.dev': 'dockerfile',
            'build/app.Dockerfile': 'dockerfile',
            'Containerfile': 'dockerfile',
            'entrypoint.sh': 'pins',
            'registries.env': 'pins',
            'bundled-tools.json': 'bundled-tools',
            'testdata/mod/go.mod': 'gomod',
            'tests/Dockerfile': None,
            'web/node_modules/x/package.json': None,
            'examples/go.mod': None,
            '.github/workflows/ci.yaml': None,
            '.github/Dockerfile': None,
            'jsr.json': None,
            'main.go': None,
            'notDockerfile.txt': None,
        }
        for path, want in rows.items():
            with self.subTest(path=path):
                self.assertEqual(inventory.surface_kind(path), want)

    def test_lanes_follow_the_release_discovery_rules(self):
        paths = [
            'go.mod',
            'a/go.mod',
            'a/b/go.mod',
            'internal/x/go.mod',
            'node_modules/m/go.mod',
            'bad dir/go.mod',
        ]
        self.assertEqual(inventory.discover_lanes(paths), ['a/b', 'a'])
        self.assertEqual(inventory.lane_of('a/b/c.go', ['a/b', 'a']), 'a/b')
        self.assertEqual(inventory.lane_of('a/x.go', ['a/b', 'a']), 'a')
        self.assertEqual(inventory.lane_of('ab/x.go', ['a/b', 'a']), '.')


GO_MOD = """module github.com/cplieger/app

go 1.27.1

toolchain go1.27.2

godebug default=go1.26

require (
\texample.com/a v1.2.0
\texample.com/b v0.3.0 // indirect
)

require example.com/c v2.0.0+incompatible

replace (
\texample.com/a v1.2.0 => example.com/fork v1.2.1
\texample.com/b => ../b
)

exclude example.com/a v1.1.0

retract v0.9.0 // published by mistake
"""


class GoMod(unittest.TestCase):
    def test_records(self):
        records, _ = inventory.parse_go_mod('go.mod', GO_MOD)
        got = {(r.identity, r.value, r.kind, r.versioning) for r in records}
        self.assertEqual(
            got,
            {
                ('go', '1.27.1', 'directive', 'go-version'),
                ('toolchain', '1.27.2', 'directive', 'go-version'),
                ('example.com/a', 'v1.2.0', 'direct', 'go-semver'),
                ('example.com/b', 'v0.3.0', 'indirect', 'go-semver'),
                ('example.com/c', 'v2.0.0+incompatible', 'direct', 'go-semver'),
                (
                    'replace example.com/a@v1.2.0 => example.com/fork',
                    'v1.2.1',
                    'replace',
                    'go-semver',
                ),
                ('replace example.com/b => ../b', '', 'replace', ''),
                ('exclude example.com/a v1.1.0', '', 'exclude', ''),
            },
        )

    def test_skeleton_ignores_dependency_lines_and_keeps_the_rest(self):
        _, base = inventory.parse_go_mod('go.mod', GO_MOD)
        bumped = GO_MOD.replace(
            'example.com/a v1.2.0\n', 'example.com/a v1.3.0\n\texample.com/d v1.0.0\n', 1
        )
        self.assertEqual(inventory.parse_go_mod('go.mod', bumped)[1], base)
        for edit in (
            'godebug default=go1.27',
            'retract v0.9.1',
            'module github.com/cplieger/other',
        ):
            key = edit.split()[0]
            line = next(line for line in GO_MOD.splitlines() if line.startswith(key))
            with self.subTest(edit=edit):
                self.assertNotEqual(
                    inventory.parse_go_mod('go.mod', GO_MOD.replace(line, edit))[1], base
                )

    def test_malformed_require_is_an_error(self):
        with self.assertRaises(inventory.InventoryError):
            inventory.parse_go_mod('go.mod', 'module m\n\nrequire example.com/a\n')


DOCKERFILE = f"""# renovate: datasource=github-tags depName=pkolaczk/fclones
ARG FCLONES_VERSION=v0.35.0

FROM rust:1.99-trixie@sha256:{HEX1} AS builder
# repin: dep=pkolaczk/fclones url=https://example.invalid/{{version}}.tar.gz
ARG FCLONES_SHA256=aaaa{'0' * 60}
# renovate: datasource=golang-version depName=golang
ARG GO_VERSION=1.27.1
# renovate: datasource=custom.golang-amd64 depName=golang-amd64
ARG GO_SHA256_AMD64={'b' * 64}  # go1.27.1
# renovate: datasource=github-tags depName=owner/tool digest=commit
ARG TOOL_REF=v2.0.0
ARG TOOL_COMMIT={'c' * 40}
# renovate: datasource=git-refs depName=videolan/x264 packageName=https://example.invalid/x264.git branch=stable
ARG X264_COMMIT={'d' * 40}
# renovate: datasource=docker depName=alpine versioning=docker
ARG ALPINE_TAG=3.24
ARG FFMPEG_VERSION=8.1
RUN xcaddy build --with github.com/caddy-dns/cloudflare@v0.2.4
FROM builder AS test
FROM --platform=$BUILDPLATFORM gcr.io/distroless/static:nonroot@sha256:{HEX2}
FROM ${{BASE_IMAGE}}
FROM scratch
"""


class Dockerfile(unittest.TestCase):
    def test_records(self):
        records, _ = inventory.parse_pins('Dockerfile', DOCKERFILE, dockerfile=True)
        got = {(r.ecosystem, r.identity, r.value, r.digest, r.kind, r.versioning) for r in records}
        self.assertEqual(
            got,
            {
                ('github-tags', 'pkolaczk/fclones', 'v0.35.0', '', 'pin', 'loose'),
                ('docker', 'rust', '1.99-trixie', f'sha256:{HEX1}', 'image', 'docker'),
                (
                    'github-tags',
                    'FCLONES_SHA256',
                    'v0.35.0',
                    'aaaa' + '0' * 60,
                    'checksum',
                    'loose',
                ),
                ('golang-version', 'golang', '1.27.1', '', 'pin', 'go-version'),
                ('custom.golang-amd64', 'golang-amd64', '1.27.1', 'b' * 64, 'pin', 'loose'),
                ('github-tags', 'owner/tool', 'v2.0.0', 'c' * 40, 'pin', 'loose'),
                ('git-refs', 'videolan/x264', 'stable', 'd' * 40, 'pin', ''),
                ('docker', 'alpine', '3.24', '', 'pin', 'docker'),
                ('github-tags', 'FFmpeg/FFmpeg', '8.1', '', 'pin', 'loose'),
                ('go', 'github.com/caddy-dns/cloudflare', 'v0.2.4', '', 'pin', 'go-semver'),
                (
                    'docker',
                    'gcr.io/distroless/static',
                    'nonroot',
                    f'sha256:{HEX2}',
                    'image',
                    'docker',
                ),
            },
        )

    def test_skeleton_masks_every_recorded_value_and_nothing_else(self):
        _, base = inventory.parse_pins('Dockerfile', DOCKERFILE, dockerfile=True)
        bumps = [
            ('v0.35.0', 'v0.36.0'),
            ('1.99-trixie', '1.100-trixie'),
            ('# go1.27.1', '# go1.27.2'),
            ('c' * 40, '7' * 40),
            ('@v0.2.4', '@v0.3.0'),
            ('FFMPEG_VERSION=8.1', 'FFMPEG_VERSION=9.0'),
        ]
        for old, new in bumps:
            self.assertIn(old, DOCKERFILE)
            with self.subTest(old=old):
                text = DOCKERFILE.replace(old, new)
                self.assertEqual(inventory.parse_pins('Dockerfile', text, dockerfile=True)[1], base)
        for old, new in (('RUN xcaddy build', 'RUN xcaddy build -v'), ('AS test', 'AS check')):
            with self.subTest(old=old):
                text = DOCKERFILE.replace(old, new)
                self.assertNotEqual(
                    inventory.parse_pins('Dockerfile', text, dockerfile=True)[1], base
                )

    def test_marker_owns_only_the_assignment_right_below_it(self):
        text = (
            '# renovate: datasource=npm depName=typescript\nARG TS_VERSION=7.0.2\nARG OTHER=1.0.0\n'
        )
        records, _ = inventory.parse_pins('Dockerfile', text, dockerfile=True)
        self.assertEqual(
            [(r.identity, r.value, r.digest) for r in records], [('typescript', '7.0.2', '')]
        )

    def test_a_digest_line_after_the_marker_digest_is_a_checksum_of_that_version(self):
        text = (
            '# renovate: datasource=custom.golang-amd64 depName=golang-amd64\n'
            f'ARG GO_SHA256_AMD64={"b" * 64}  # go1.27.1\n'
            f'ARG GO_SHA256_EXTRA={"c" * 64}\n'
        )
        records, skeleton = inventory.parse_pins('Dockerfile', text, dockerfile=True)
        self.assertEqual(
            [(r.identity, r.value, r.digest, r.kind) for r in records],
            [
                ('golang-amd64', '1.27.1', 'b' * 64, 'pin'),
                ('GO_SHA256_EXTRA', '1.27.1', 'c' * 64, 'checksum'),
            ],
        )
        moved = text.replace('c' * 64, '9' * 64)
        moved_records, moved_skeleton = inventory.parse_pins('Dockerfile', moved, dockerfile=True)
        self.assertEqual(moved_skeleton, skeleton)
        self.assertNotEqual(moved_records, records)

    def test_unpaired_checksum_compares_by_its_digest(self):
        text = f'# repin: dep=nobody url=https://example.invalid/x\nARG X_SHA256={HEX1}\n'
        (record,), _ = inventory.parse_pins('Dockerfile', text, dockerfile=True)
        self.assertEqual(
            (record.identity, record.value, record.digest, record.versioning),
            ('X_SHA256', '', HEX1, ''),
        )


ENTRYPOINT = f"""#!/bin/sh
# renovate: datasource=custom.kiro-cli depName=kiro-cli
KIRO_CLI_VERSION="2.27.1"
KIRO_CLI_SHA256="{'e' * 64}"
# renovate: datasource=custom.kiro-cli-arm64 depName=kiro-cli-arm64
KIRO_CLI_SHA256_ARM64="{'f' * 64}" # kiro-cli 2.27.1
export KIRO_CLI_VERSION
"""


class OtherSurfaces(unittest.TestCase):
    def test_entrypoint_pins(self):
        records, skeleton = inventory.parse_pins('entrypoint.sh', ENTRYPOINT)
        self.assertEqual(
            {(r.identity, r.value, r.digest) for r in records},
            {('kiro-cli', '2.27.1', 'e' * 64), ('kiro-cli-arm64', '2.27.1', 'f' * 64)},
        )
        bumped = ENTRYPOINT.replace('2.27.1', '2.28.0').replace('e' * 64, '9' * 64)
        self.assertEqual(inventory.parse_pins('entrypoint.sh', bumped)[1], skeleton)

    def test_package_json(self):
        text = json.dumps(
            {
                'name': 'x',
                'dependencies': {'a': '^1.0.0', 'ra': 'npm:@s/real@^2.1.0'},
                'peerDependencies': {'p': '>=2', 'pa': 'npm:pa'},
                'devDependencies': {'d': '1.0.0', 'same': 'npm:same@1.0.0'},
                'overrides': {'q': '1.2.3', 'r': {'s': '2.0.0'}},
                'packageManager': 'npm@11.6.0',
                'scripts': {'test': 'vitest'},
            }
        )
        records, skeleton = inventory.parse_package_json('package.json', text)
        self.assertEqual(
            {(r.ecosystem, r.identity, r.value, r.kind) for r in records},
            {
                ('npm-range', 'a', '^1.0.0', 'direct'),
                ('npm-range', 'ra@npm:@s/real', '^2.1.0', 'direct'),
                ('npm-range', 'p', '>=2', 'direct'),
                ('npm-range', 'pa', '', 'direct'),
                ('npm-range', 'd', '1.0.0', 'dev'),
                ('npm-range', 'same', '1.0.0', 'dev'),
                ('npm-range', 'overrides q', '1.2.3', 'direct'),
                ('npm-range', 'overrides r>s', '2.0.0', 'direct'),
                ('npm', 'packageManager npm', '11.6.0', 'pin'),
            },
        )
        self.assertEqual(json.loads(skeleton), {'name': 'x', 'scripts': {'test': 'vitest'}})

    def test_npm_lock_kinds(self):
        text = json.dumps(
            {
                'lockfileVersion': 3,
                'packages': {
                    '': {
                        'dependencies': {'a': '^1', 'ra': 'npm:real@^2'},
                        'devDependencies': {'d': '1', 'shipped': '1'},
                    },
                    'node_modules/a': {'version': '1.0.0', 'integrity': 'sha512-a'},
                    'node_modules/d': {'version': '1.0.0', 'dev': True},
                    'node_modules/shipped': {'version': '1.0.0'},
                    'node_modules/a/node_modules/t': {'version': '2.0.0'},
                    'node_modules/alias': {'name': 'real', 'version': '3.0.0'},
                    'node_modules/ra': {'name': 'real', 'version': '2.1.0'},
                    'node_modules/a/node_modules/ra': {'name': '@s/real', 'version': '1.0.0'},
                    'node_modules/same': {'name': 'same', 'version': '1.0.0'},
                    'node_modules/ws': {'resolved': 'web', 'link': True},
                    'web': {'name': 'ws', 'version': '0.1.0'},
                },
            }
        )
        records, _ = inventory.parse_npm_lock('package-lock.json', text)
        self.assertEqual(
            {(r.identity, r.value, r.digest, r.kind, r.versioning) for r in records},
            {
                ('a', '1.0.0', 'sha512-a', 'direct', 'semver'),
                ('d', '1.0.0', '', 'dev', 'semver'),
                ('shipped', '1.0.0', '', 'indirect', 'semver'),
                ('t', '2.0.0', '', 'indirect', 'semver'),
                ('alias@npm:real', '3.0.0', '', 'indirect', 'semver'),
                ('ra@npm:real', '2.1.0', '', 'direct', 'semver'),
                ('ra@npm:@s/real', '1.0.0', '', 'indirect', 'semver'),
                ('same', '1.0.0', '', 'indirect', 'semver'),
                ('ws', 'link:web', '', 'indirect', ''),
                ('ws', '0.1.0', '', 'indirect', 'semver'),
            },
        )

    def test_npm_lock_v1_is_refused(self):
        with self.assertRaises(inventory.InventoryError):
            inventory.parse_npm_lock(
                'package-lock.json', '{"lockfileVersion": 1, "dependencies": {}}'
            )

    def test_bundled_tools(self):
        text = json.dumps(
            {
                'entries': {
                    'typescript': {
                        'version': '7.0.2',
                        'upstream': {'datasource': 'npm', 'depName': 'typescript'},
                    },
                    'rustup': {'version': 'stable'},
                }
            }
        )
        records, skeleton = inventory.parse_bundled_tools('bundled-tools.json', text)
        self.assertEqual(
            [(r.ecosystem, r.identity, r.value) for r in records], [('npm', 'typescript', '7.0.2')]
        )
        bumped = text.replace('7.0.2', '7.1.0')
        self.assertEqual(inventory.parse_bundled_tools('bundled-tools.json', bumped)[1], skeleton)
        self.assertNotEqual(
            inventory.parse_bundled_tools('bundled-tools.json', text.replace('stable', 'beta'))[1],
            skeleton,
        )


class MalformedSections(unittest.TestCase):
    """A file whose structure is wrong for its surface is an input error (exit 2)."""

    CASES = (
        ('package.json', '{"dependencies": ["x"]}', 'dependencies is not a mapping'),
        ('package.json', '{"dependencies": {"x": 1}}', 'dependencies x is not a string'),
        ('package.json', '{"overrides": ["x"]}', 'overrides is not a mapping'),
        ('package.json', '{"overrides": {"a": {"b": 1}}}', 'overrides a>b is not a mapping'),
        ('package.json', '{"packageManager": 1}', 'packageManager is not a string'),
        ('package-lock.json', '{"packages": {"": []}}', 'packages[""] is not a mapping'),
        (
            'package-lock.json',
            '{"packages": {"": {"dependencies": ["x"]}}}',
            'packages[""].dependencies is not a mapping',
        ),
        (
            'package-lock.json',
            '{"packages": {"": {"dependencies": {"x": 1}}, "node_modules/x": {"version": "1.0.0"}}}',
            'packages[""].dependencies x is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"": {"optionalDependencies": {"x": null}}}}',
            'packages[""].optionalDependencies x is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"": {"peerDependencies": {"x": ["^1"]}}}}',
            'packages[""].peerDependencies x is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"": {"devDependencies": {"x": true}}, "node_modules/x": {"version": "1.0.0"}}}',
            'packages[""].devDependencies x is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": "1.0.0"}}',
            "packages['node_modules/x'] is not a mapping",
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"name": 1, "version": "1.0.0"}}}',
            'node_modules/x name is not a string',
        ),
        ('uv.lock', 'package = 1\n', 'package is not an array'),
        ('uv.lock', 'package = [1]\n', 'a package entry is not a mapping'),
        ('uv.lock', '[[package]]\nname = "a"\nsource = "x"\n', 'a source is not a mapping'),
        (
            'uv.lock',
            '[[package]]\nname = "app"\nsource = { virtual = "." }\ndependencies = ["a"]\n',
            'dependencies entry is not a mapping',
        ),
        (
            'uv.lock',
            '[[package]]\nname = "app"\nsource = { virtual = "." }\noptional-dependencies = ["a"]\n',
            'optional-dependencies is not a mapping',
        ),
        ('bundled-tools.json', '{"entries": []}', 'entries is not a mapping'),
        ('bundled-tools.json', '[]', 'not a JSON object'),
        ('bundled-tools.json', '{"entries": {"t": "v1"}}', 'entries t is not a mapping'),
        (
            'bundled-tools.json',
            '{"entries": {"t": {"version": "v1", "upstream": "npm"}}}',
            't upstream is not a mapping',
        ),
        (
            'bundled-tools.json',
            '{"entries": {"t": {"version": 1, "upstream": {"datasource": "npm"}}}}',
            't version is not a string',
        ),
        (
            'bundled-tools.json',
            '{"entries": {"t": {"upstream": {"datasource": "npm"}}}}',
            't version is not a string',
        ),
        (
            'bundled-tools.json',
            '{"entries": {"t": {"version": "v1", "upstream": {"datasource": 1}}}}',
            't datasource is not a string',
        ),
        (
            'bundled-tools.json',
            '{"entries": {"t": {"version": "v1", "upstream": {"versioning": ["docker"]}}}}',
            't versioning is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"": {"devDependencies": ["x"]}}}',
            'packages[""].devDependencies is not a mapping',
        ),
        (
            'package-lock.json',
            '{"packages": {}, "dependencies": []}',
            'dependencies is not a mapping',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"name": 0, "version": "1.0.0"}}}',
            'node_modules/x name is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"name": null, "version": "1.0.0"}}}',
            'node_modules/x name is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"version": "1.0.0", "integrity": null}}}',
            'node_modules/x integrity is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"version": "1.0.0", "integrity": {"a": 1}}}}',
            'node_modules/x integrity is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"link": true, "resolved": 1}}}',
            'node_modules/x resolved is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"version": "1.0.0", "resolved": ["git+x"]}}}',
            'node_modules/x resolved is not a string',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"link": "yes", "resolved": "x"}}}',
            'node_modules/x link is not a boolean',
        ),
        (
            'package-lock.json',
            '{"packages": {"node_modules/x": {"version": "1.0.0", "dev": 1}}}',
            'node_modules/x dev is not a boolean',
        ),
        (
            'uv.lock',
            '[[package]]\nname = 1\nsource = { virtual = "." }\n',
            'a workspace member name is not a string',
        ),
        (
            'package.json',
            '{"dependencies": {"a": "npm:"}}',
            'dependencies a is not a valid npm alias',
        ),
        (
            'package.json',
            '{"devDependencies": {"a": "npm:@s/real@"}}',
            'devDependencies a is not a valid npm alias',
        ),
        (
            'go.mod',
            'module m\n\nrequire (\n\texample.com/x v1.0.0\n',
            'require block is not closed',
        ),
        ('go.mod', 'module m\n\nreplace (\n\ta => b v1.0.0\n', 'replace block is not closed'),
        ('go.mod', 'module m\n\nexclude (\n', 'exclude block is not closed'),
    )

    def test_parsers_refuse_a_wrong_shape(self):
        for path, text, message in self.CASES:
            with self.subTest(path=path, text=text):
                with self.assertRaises(inventory.InventoryError) as caught:
                    inventory.PARSERS[inventory.surface_kind(path)](path, text)
                self.assertEqual(str(caught.exception), f'{path}: {message}')

    def test_the_cli_exits_2_without_a_traceback(self):
        for path, text, message in self.CASES:
            with self.subTest(path=path, text=text):
                self.assertEqual(
                    self.records_cli(path, text), (2, '', f'inventory: {path}: {message}\n')
                )

    @staticmethod
    def records_cli(path: str, text: str) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            git = ['git', '-C', tmp, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            (Path(tmp) / path).write_text(text, encoding='utf-8')
            subprocess.run([*git, 'add', '.'], check=True)
            subprocess.run([*git, 'commit', '-q', '-m', 'malformed'], check=True)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(inventory.__file__)),
                    'records',
                    '--git-dir',
                    tmp,
                    '--rev',
                    'HEAD',
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        return proc.returncode, proc.stdout, proc.stderr


class GroupedMajorMinorSplit(FixtureCase):
    fixture = 'grouped-major-minor-split'

    def test_records_carry_the_module_path_as_identity(self):
        self.assertEqual(
            self.fx.records('T'),
            [
                Record('.', 'go', 'go.mod', 'example.com/bar', 'v1.3.0', '', 'direct', 'go-semver'),
                Record(
                    '.', 'go', 'go.mod', 'example.com/foo/v2', 'v2.0.0', '', 'direct', 'go-semver'
                ),
                Record('.', 'go', 'go.mod', 'go', '1.27.1', '', 'directive', 'go-version'),
            ],
        )

    def test_dev_carrying_the_major_and_the_minor_dominates(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T')['verdict'], 'pass')

    def test_dev_missing_the_minor_half_fails_naming_it(self):
        result = self.fx.dominance('B', 'M', 'T_lag')
        self.assertEqual(result['verdict'], 'fail')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/bar')])

    def test_diff_reports_the_vn_path_change_as_remove_plus_add(self):
        changes = {(d['identity'], d['change']) for d in self.fx.diff('B', 'T')['direct']}
        self.assertEqual(
            changes,
            {
                ('example.com/foo', 'removed'),
                ('example.com/foo/v2', 'added'),
                ('example.com/bar', 'changed'),
            },
        )


class SameTagDifferentDigest(FixtureCase):
    fixture = 'same-tag-different-digest'

    def test_stage_and_scratch_references_are_not_records(self):
        self.assertEqual(
            self.fx.records('B'),
            [
                Record(
                    '.',
                    'docker',
                    'Dockerfile',
                    'alpine',
                    '3.24.2',
                    f'sha256:{HEX1}',
                    'image',
                    'docker',
                )
            ],
        )

    def test_main_repush_held_on_dev_then_moved_on_dominates(self):
        result = self.fx.dominance('B', 'M', 'T')
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(
            result['paths'][0]['records'][0]['reason'], 'same version, digest held on dev'
        )

    def test_main_repush_never_on_dev_fails(self):
        result = self.fx.dominance('B', 'M', 'T_never')
        self.assertEqual(self.failing(result), [('Dockerfile', 'alpine')])

    def test_a_moved_dockerfile_carrying_the_old_digest_fails(self):
        result = self.fx.dominance('B', 'M', 'T_moved')
        self.assertEqual(self.failing(result), [('Dockerfile', 'alpine')])
        self.assertTrue(result['paths'][0]['records'][0]['reason'].endswith('(in Dockerfile.app)'))

    def test_a_moved_dockerfile_carrying_main_digest_passes(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T_moved_fresh')['verdict'], 'pass')

    def test_an_image_dev_deleted_everywhere_is_removed_on_dev(self):
        result = self.fx.dominance('B', 'M', 'T_deleted')
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(result['paths'][0]['records'][0]['reason'], 'removed on dev')

    def test_diff_shows_the_digest_move(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B', 'M')),
            '<details>\n<summary>Dependencies: 1 change (alpine)</summary>\n\n'
            '- `alpine` 3.24.2 (111111111111) to 3.24.2 (222222222222) (image)\n\n</details>\n',
        )

    def test_each_checksum_below_one_marker_is_its_own_record(self):
        got = {(r.identity, r.value, r.digest, r.kind) for r in self.fx.records('B_sum')}
        self.assertEqual(
            got,
            {
                ('owner/tool', 'v1.2.3', 'a' * 64, 'pin'),
                ('TOOL_SHA256_ARM64', 'v1.2.3', 'b' * 64, 'checksum'),
                ('pkolaczk/fclones', 'v0.35.0', '', 'pin'),
                ('FCLONES_SHA256', 'v0.35.0', '1' * 64, 'checksum'),
            },
        )
        arm = next(r for r in self.fx.records('B_sum') if r.identity == 'TOOL_SHA256_ARM64')
        self.assertEqual((arm.ecosystem, arm.versioning), ('github-releases', 'loose'))

    def test_a_later_checksum_main_moved_must_be_carried(self):
        for target in ('T_sum_stale', 'T_sum_other'):
            with self.subTest(target=target):
                result = self.fx.dominance('B_sum', 'M_sum', target)
                self.assertEqual(self.failing(result), [('Dockerfile', 'TOOL_SHA256_ARM64')])
        self.assertEqual(self.fx.dominance('B_sum', 'M_sum', 'T_sum_carried')['verdict'], 'pass')
        later = self.fx.dominance('B_sum', 'M_sum', 'T_sum_later')
        self.assertEqual(later['verdict'], 'pass')
        self.assertEqual(
            [r['reason'] for r in later['paths'][0]['records']],
            ['same version, digest held on dev'],
        )
        self.assertEqual(self.fx.dominance('B_sum', 'M_sum', 'T_sum_bumped')['verdict'], 'pass')

    def test_a_checksum_changed_at_its_version_is_noted(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_sum', 'M_sum')),
            '<details>\n<summary>Dependencies: 1 change (TOOL_SHA256_ARM64)</summary>\n\n'
            '- `TOOL_SHA256_ARM64` v1.2.3 (bbbbbbbbbbbb) to v1.2.3 (cccccccccccc)'
            ' (github-releases)\n\n</details>\n',
        )
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_sum', 'M_sum_repin')),
            '<details>\n<summary>Dependencies: 1 change (FCLONES_SHA256)</summary>\n\n'
            '- `FCLONES_SHA256` v0.35.0 (111111111111) to v0.35.0 (222222222222)'
            ' (github-tags)\n\n</details>\n',
        )

    def test_checksums_that_move_with_their_version_are_told_by_the_pin(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_sum', 'T_sum_bumped')),
            '<details>\n<summary>Dependencies: 2 changes (owner/tool, pkolaczk/fclones)</summary>'
            '\n\n- `owner/tool` v1.2.3 (aaaaaaaaaaaa) to v1.3.0 (ffffffffffff) (github-releases)\n'
            '- `pkolaczk/fclones` v0.35.0 to v0.36.0 (github-tags)\n\n</details>\n',
        )
        self.assertEqual(self.fx.diff('B_sum', 'T_sum_bumped')['transitive'], {})

    def test_a_checksum_added_or_dropped_at_an_unchanged_version_is_no_line(self):
        for main in ('M_sum_arrived', 'M_sum_left'):
            with self.subTest(main=main):
                self.assertEqual(self.fx.diff('B_sum', main), {'direct': [], 'transitive': {}})
                self.assertEqual(inventory.render_markdown(self.fx.diff('B_sum', main)), '')
        result = self.fx.dominance('B_sum', 'M_sum_arrived', 'B_sum')
        self.assertEqual(
            self.failing(result), [('Dockerfile', 'changed outside its dependency records')]
        )


class RemovedDependency(FixtureCase):
    fixture = 'removed-dependency'

    def test_dependency_removed_on_dev_does_not_block(self):
        result = self.fx.dominance('B', 'M', 'T')
        self.assertEqual(result['verdict'], 'pass')
        reasons = {r['identity']: r['reason'] for r in result['paths'][0]['records']}
        self.assertEqual(reasons['example.com/old'], 'removed on dev')

    def test_dependency_removed_on_main_and_kept_on_dev_fails(self):
        result = self.fx.dominance('B', 'M_removed', 'B')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/old')])
        self.assertEqual(
            result['paths'][0]['records'][0]['reason'],
            'target still holds v1.0.0, which main removed',
        )

    def test_dependency_removed_on_main_and_on_dev_or_moved_on_passes(self):
        self.assertEqual(self.fx.dominance('B', 'M_removed', 'T')['verdict'], 'pass')
        self.assertEqual(self.fx.dominance('B', 'M_removed', 'T_moved_on')['verdict'], 'pass')

    def test_dependency_removed_on_main_and_lowered_on_dev_fails(self):
        result = self.fx.dominance('B', 'M_removed', 'T_moved_back')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/old')])
        self.assertEqual(
            result['paths'][0]['records'][0]['reason'],
            'target has v0.9.0, not a move forward from v1.0.0, which main removed',
        )

    def test_dependency_added_on_main_and_absent_on_dev_fails(self):
        result = self.fx.dominance('B', 'M_added', 'T')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/new')])
        self.assertEqual(result['paths'][0]['records'][0]['reason'], 'absent at target')

    def test_dependency_added_on_main_that_dev_held_then_removed_passes(self):
        for target in ('T_dropped_new', 'T_dropped_newer'):
            with self.subTest(target=target):
                result = self.fx.dominance('B', 'M_added', target)
                self.assertEqual(result['verdict'], 'pass')
                reasons = {r['identity']: r['reason'] for r in result['paths'][0]['records']}
                self.assertEqual(reasons, {'example.com/new': 'held on dev, then removed'})

    def test_dependency_added_on_main_that_dev_held_lower_then_removed_fails(self):
        result = self.fx.dominance('B', 'M_added', 'T_dropped_older')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/new')])
        self.assertEqual(result['paths'][0]['records'][0]['reason'], 'absent at target')

    def test_the_cli_exit_code_follows_dev_history_for_a_main_added_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp) / 'sync-owned.txt'
            sync.write_text('cliff.toml\n', encoding='utf-8')
            for target, want in (('T_dropped_new', 0), ('T_dropped_older', 1), ('T', 1)):
                out = io.StringIO()
                with self.subTest(target=target), contextlib.redirect_stdout(out):
                    rc = inventory.main(
                        [
                            *('dominance', '--git-dir', str(self.fx.dir), '--base', 'B'),
                            *('--main', 'M_added', '--target', target, '--sync-owned', str(sync)),
                            *('--canonical-dir', tmp),
                        ]
                    )
                self.assertEqual(rc, want, out.getvalue())
                if want:
                    self.assertIn(
                        'FAIL go.mod: example.com/new v1.0.0 on main; absent at target',
                        out.getvalue(),
                    )

    def unreadable(self, commit: str, target: str) -> dict:
        """dominance B M <target>, first passing, then with <commit>'s go.mod object lost."""
        fx = Fixture(self.fixture)
        self.addCleanup(fx.close)
        self.assertEqual(fx.dominance('B', 'M', target)['verdict'], 'pass')
        fx.lose_blob(commit, 'go.mod')
        fx.repo = inventory.Repo(str(fx.dir))
        return fx.dominance('B', 'M', target)

    def test_an_unreadable_target_file_is_uncomparable_not_removed(self):
        result = self.unreadable('T', 'T')
        self.assertEqual(result['verdict'], 'uncomparable')
        self.assertEqual(result['paths'][0]['status'], 'uncomparable')
        self.assertRegex(
            result['paths'][0]['reason'], r'^git cat-file blob [0-9a-f]{40}:go\.mod: fatal: '
        )
        self.assertEqual(result['paths'][0]['records'], [])

    def test_an_unreadable_target_file_exits_2(self):
        fx = Fixture(self.fixture)
        self.addCleanup(fx.close)
        fx.lose_blob('T', 'go.mod')
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp) / 'sync-owned.txt'
            sync.write_text('cliff.toml\n', encoding='utf-8')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = inventory.main(
                    [
                        *('dominance', '--git-dir', str(fx.dir), '--base', 'B', '--main', 'M'),
                        *('--target', 'T', '--sync-owned', str(sync), '--canonical-dir', tmp),
                    ]
                )
        self.assertEqual(rc, 2)
        self.assertTrue(out.getvalue().startswith('UNCOMPARABLE go.mod: git cat-file blob '))
        self.assertNotIn('removed on dev', out.getvalue())

    def test_an_unreadable_dev_revision_is_uncomparable_not_skipped(self):
        result = self.unreadable('D_step', 'T_after_step')
        self.assertEqual(result['verdict'], 'uncomparable')
        self.assertRegex(
            result['paths'][0]['reason'], r'^git cat-file blob [0-9a-f]{40}:go\.mod: fatal: '
        )

    def test_a_path_the_tree_lacks_is_absent_and_a_lost_object_raises(self):
        fx = Fixture(self.fixture)
        self.addCleanup(fx.close)
        fx.lose_blob('T', 'go.mod')
        repo = inventory.Repo(str(fx.dir))
        self.assertIsNone(repo.blob(fx.sha['T'], 'go.sum'))
        self.assertIsNotNone(repo.blob(fx.sha['B'], 'go.mod'))
        with self.assertRaises(inventory.GitReadError):
            repo.blob(fx.sha['T'], 'go.mod')
        with self.assertRaises(inventory.GitReadError):
            repo.records(fx.sha['T'])
        with self.assertRaises(inventory.GitReadError):
            inventory.diff(repo, fx.sha['B'], fx.sha['T'], None, set())


class NpmRangeChange(FixtureCase):
    fixture = 'npm-range-change'

    def test_higher_range_and_resolution_on_dev_dominate(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T')['verdict'], 'pass')

    def test_lower_resolution_fails_and_the_range_is_not_judged(self):
        result = self.fx.dominance('B', 'M', 'T_low')
        self.assertEqual(self.failing(result), [('package-lock.json', 'left-pad')])
        manifest = next(p for p in result['paths'] if p['path'] == 'package.json')
        self.assertEqual((manifest['status'], manifest['records']), ('ok', []))

    def test_a_dominating_resolution_passes_whatever_the_manifest_range(self):
        for target in ('T_range_op', 'T_range_low'):
            with self.subTest(target=target):
                result = self.fx.dominance('B', 'M', target)
                self.assertEqual(result['verdict'], 'pass')
                self.assertEqual(
                    {p['path']: [r['identity'] for r in p['records']] for p in result['paths']},
                    {'package.json': [], 'package-lock.json': ['left-pad'], 'lib/package.json': []},
                )

    def test_notes_show_the_resolution_and_a_range_with_no_lockfile(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B', 'M')),
            '<details>\n<summary>Dependencies: 2 changes (left-pad, peer-thing)</summary>\n\n'
            '- `left-pad` 1.2.0 to 1.3.0 (npm)\n- `peer-thing` ^2.0.0 to ^2.1.0 (npm)\n\n</details>\n',
        )

    def test_same_version_moved_out_of_dev_dependencies_must_follow(self):
        result = self.fx.dominance('B', 'M_role', 'B')
        self.assertEqual(self.failing(result), [('package-lock.json', 'vitest')])
        self.assertEqual(self.fx.dominance('B', 'M_role', 'T_role')['verdict'], 'pass')

    def test_a_newer_dev_only_copy_does_not_carry_a_dependency_main_ships(self):
        result = self.fx.dominance('B', 'M_role', 'T')
        self.assertEqual(self.failing(result), [('package-lock.json', 'vitest')])

    def test_dev_dependencies_are_not_noted(self):
        identities = {d['identity'] for d in self.fx.diff('B', 'T')['direct']}
        self.assertNotIn('vitest', identities)

    def test_a_root_alias_is_one_direct_dependency_under_its_install_name(self):
        self.assertEqual(
            {(r.file, r.identity, r.value, r.kind) for r in self.fx.records('M_alias')},
            {
                ('package-lock.json', 'left-pad', '1.2.0', 'direct'),
                ('package-lock.json', 'ra@npm:real', '2.2.0', 'direct'),
                ('package.json', 'left-pad', '^1.2.0', 'direct'),
                ('package.json', 'ra@npm:real', '^2.2.0', 'direct'),
            },
        )
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_alias', 'M_alias')),
            '<details>\n<summary>Dependencies: 1 change (ra@npm:real)</summary>\n\n'
            '- `ra@npm:real` 2.1.0 to 2.2.0 (npm)\n\n</details>\n',
        )

    def test_an_alias_bump_must_be_carried_under_the_same_alias(self):
        self.assertEqual(self.fx.dominance('B_alias', 'M_alias', 'T_alias')['verdict'], 'pass')
        result = self.fx.dominance('B_alias', 'M_alias', 'T_alias_low')
        self.assertEqual(self.failing(result), [('package-lock.json', 'ra@npm:real')])

    def test_a_git_dependency_records_its_locked_commit(self):
        lock = {r.identity: r for r in self.fx.records('M_git') if r.file == 'package-lock.json'}
        self.assertEqual(
            (lock['gitdep'].value, lock['gitdep'].digest),
            ('1.0.0', 'git+ssh://git@github.com/owner/gitdep.git#' + '2' * 40),
        )
        self.assertEqual(lock['left-pad'].digest, 'sha512-fixture-leftpad-120')

    def test_a_same_version_git_update_must_be_carried(self):
        result = self.fx.dominance('B_git', 'M_git', 'T_git_stale')
        self.assertEqual(self.failing(result), [('package-lock.json', 'gitdep')])
        result = self.fx.dominance('B_git', 'M_git', 'T_git_other')
        self.assertEqual(self.failing(result), [('package-lock.json', 'gitdep')])
        for target in ('T_git_carried', 'T_git_later'):
            with self.subTest(target=target):
                self.assertEqual(self.fx.dominance('B_git', 'M_git', target)['verdict'], 'pass')

    def test_a_same_version_git_update_is_a_resolution_change_in_the_notes(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_git', 'M_git')),
            '<details>\n<summary>Dependencies: 1 change (gitdep)</summary>\n\n'
            '- `gitdep` 1.0.0 resolution changed (npm)\n\n</details>\n',
        )

    def test_retargeting_an_alias_is_another_dependency_not_a_version_change(self):
        result = self.fx.dominance('B_alias', 'M_alias', 'T_alias_retarget')
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(
            {(p['path'], r['reason']) for p in result['paths'] for r in p['records']},
            {('package-lock.json', 'removed on dev')},
        )
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('M_alias', 'T_alias_retarget')),
            '<details>\n<summary>Dependencies: 2 changes (ra@npm:other, ra@npm:real)</summary>\n\n'
            '- Added `ra@npm:other` 1.0.0 (npm)\n- Removed `ra@npm:real` 2.2.0 (npm)\n\n'
            '</details>\n',
        )


class GoPseudoReplace(FixtureCase):
    fixture = 'go-pseudo-replace'

    def test_replace_record(self):
        replaces = [r for r in self.fx.records('M') if r.kind == 'replace']
        self.assertEqual(
            replaces,
            [
                Record(
                    '.',
                    'go',
                    'go.mod',
                    'replace example.com/a => example.com/a-fork',
                    'v0.0.0-20260201000000-bbbbbbbbbbbb',
                    '',
                    'replace',
                    'go-semver',
                )
            ],
        )

    def test_newer_pseudo_version_and_incompatible_dominate(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T')['verdict'], 'pass')

    def test_a_different_replace_target_on_dev_is_dev_removing_the_old_one(self):
        result = self.fx.dominance('B', 'M', 'T_other_target')
        self.assertEqual(result['verdict'], 'pass')
        reasons = {r['identity']: r['reason'] for r in result['paths'][0]['records']}
        self.assertEqual(reasons['replace example.com/a => example.com/a-fork'], 'removed on dev')

    def test_a_target_main_switched_to_passes_only_where_dev_held_it_then_moved_on(self):
        result = self.fx.dominance('B', 'M_fork', 'T_switch')
        self.assertEqual(result['verdict'], 'pass')
        reasons = {r['identity']: r['reason'] for r in result['paths'][0]['records']}
        self.assertEqual(
            reasons['replace example.com/a => example.com/a-other'], 'held on dev, then removed'
        )
        self.assertEqual(self.fx.dominance('B', 'M_fork', 'T_switch1')['verdict'], 'pass')
        self.assertEqual(
            self.failing(self.fx.dominance('B', 'M_fork', 'T')),
            [('go.mod', 'replace example.com/a => example.com/a-other')],
        )

    def test_an_older_require_on_dev_fails(self):
        result = self.fx.dominance('B', 'M', 'T_low')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/inc')])

    def test_an_unordered_require_is_uncomparable(self):
        result = self.fx.dominance('B', 'M', 'T_bad')
        self.assertEqual(result['verdict'], 'uncomparable')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/inc')])

    def test_an_unordered_require_dev_carries_verbatim_is_still_uncomparable(self):
        result = self.fx.dominance('B', 'M_bad', 'T_bad_same')
        self.assertEqual(result['verdict'], 'uncomparable')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/inc')])
        (record,) = result['paths'][0]['records']
        self.assertEqual(record['reason'], "'master' on main is not go-semver")

    def test_an_unordered_value_is_uncomparable_with_no_target_copy(self):
        for base, main, target, identity, reason in (
            ('B', 'M_bad_added', 'T', 'example.com/new', "'master' on main is not go-semver"),
            (
                'B_bad',
                'M_dropped',
                'T_dropped',
                'example.com/inc',
                "'master' at base is not go-semver",
            ),
            (
                'B_bad',
                'M_fixed',
                'T_dropped',
                'example.com/inc',
                "'master' at base is not go-semver",
            ),
        ):
            with self.subTest(main=main, target=target):
                result = self.fx.dominance(base, main, target)
                self.assertEqual(result['verdict'], 'uncomparable')
                records = {r['identity']: r for r in result['paths'][0]['records']}
                self.assertEqual(records[identity]['status'], 'uncomparable')
                self.assertEqual(records[identity]['reason'], reason)


class GoDirectiveBump(FixtureCase):
    fixture = 'go-directive-bump'

    def test_newer_go_and_toolchain_dominate(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T')['verdict'], 'pass')

    def test_older_go_and_toolchain_fail(self):
        result = self.fx.dominance('B', 'M', 'T_low')
        self.assertEqual(sorted(self.failing(result)), [('go.mod', 'go'), ('go.mod', 'toolchain')])


class NestedModule(FixtureCase):
    fixture = 'nested-module'

    def test_lanes(self):
        lanes = {(r.file, r.lane) for r in self.fx.records('B')}
        self.assertEqual(
            lanes, {('go.mod', '.'), ('tools/go.mod', 'tools'), ('internal/sub/go.mod', '.')}
        )

    def test_lane_filter(self):
        self.assertEqual(
            self.fx.records('M', 'tools'),
            [
                Record(
                    'tools',
                    'go',
                    'tools/go.mod',
                    'example.com/lane',
                    'v1.0.1',
                    '',
                    'direct',
                    'go-semver',
                ),
                Record(
                    'tools', 'go', 'tools/go.mod', 'go', '1.27.1', '', 'directive', 'go-version'
                ),
            ],
        )
        self.assertEqual(self.fx.diff('B', 'M', '.'), {'direct': [], 'transitive': {}})
        self.assertEqual(
            [d['identity'] for d in self.fx.diff('B', 'M', 'tools')['direct']], ['example.com/lane']
        )

    def test_dominance(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T')['verdict'], 'pass')

    def test_a_requirement_that_turns_direct_is_one_line_and_no_transitive_count(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_indirect', 'B')),
            '<details>\n<summary>Dependencies: 1 change (example.com/root)</summary>\n\n'
            '- `example.com/root` v1.0.0 now a direct dependency (Go)\n\n</details>\n',
        )

    def test_a_requirement_that_changes_role_and_version_is_one_line(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B', 'B_indirect_bump')),
            '<details>\n<summary>Dependencies: 1 change (example.com/root)</summary>\n\n'
            '- `example.com/root` v1.0.0 to v1.1.0 no longer a direct dependency (Go)\n\n'
            '</details>\n',
        )
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_indirect_bump', 'B')),
            '<details>\n<summary>Dependencies: 1 change (example.com/root)</summary>\n\n'
            '- `example.com/root` v1.1.0 to v1.0.0 now a direct dependency (Go)\n\n</details>\n',
        )

    def test_a_slot_dev_removed_is_not_sought_where_main_also_held_it(self):
        result = self.fx.dominance('B_sys', 'M_sys', 'T_sys')
        self.assertEqual(result['verdict'], 'pass')
        reasons = {r['identity']: r['reason'] for r in result['paths'][0]['records']}
        self.assertEqual(reasons['example.com/sys'], 'removed on dev')

    def test_a_slot_dev_removed_is_not_sought_in_another_lane(self):
        self.assertEqual(self.fx.dominance('B_sys', 'M_sys', 'T_sys_lane')['verdict'], 'pass')

    def test_a_newer_indirect_requirement_does_not_carry_a_direct_one(self):
        result = self.fx.dominance('B', 'M_root_bump', 'T_root_newer_indirect')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/root')])
        self.assertEqual(self.fx.dominance('B', 'M_root_bump', 'T_root_newer')['verdict'], 'pass')

    def test_a_slot_moved_to_a_new_surface_of_the_lane_must_dominate_there(self):
        result = self.fx.dominance('B_sys', 'M_sys', 'T_sys_split')
        self.assertEqual(self.failing(result), [('go.mod', 'example.com/sys')])
        self.assertEqual(
            result['paths'][0]['records'][0]['reason'],
            'target has v0.30.0 (in internal/other/go.mod)',
        )
        self.assertEqual(
            self.fx.dominance('B_sys', 'M_sys', 'T_sys_split_fresh')['verdict'], 'pass'
        )


class TypeScriptSubpackage(FixtureCase):
    fixture = 'ts-subpackage'

    def test_subpackage_shares_the_root_lane(self):
        self.assertEqual(
            [r for r in self.fx.records('B') if r.file.startswith('web/')],
            [
                Record(
                    '.',
                    'npm',
                    'web/package-lock.json',
                    '@cplieger/reactive',
                    '2.0.0',
                    'sha512-fixture-reactive-200',
                    'direct',
                    'semver',
                ),
                Record(
                    '.',
                    'npm',
                    'web/package-lock.json',
                    'tslib',
                    '2.8.0',
                    'sha512-fixture-tslib-280',
                    'indirect',
                    'semver',
                ),
                Record(
                    '.',
                    'npm-range',
                    'web/package.json',
                    '@cplieger/reactive',
                    '^2.0.0',
                    '',
                    'direct',
                    'range',
                ),
            ],
        )

    def test_dev_prerelease_dominates_the_main_patch(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T')['verdict'], 'pass')

    def test_diff_counts_the_transitive_change(self):
        result = self.fx.diff('B', 'T', '.')
        self.assertEqual([d['identity'] for d in result['direct']], ['@cplieger/reactive'])
        self.assertEqual(result['transitive'], {'npm': {'count': 1, 'security': 0}})


class PythonLockfile(FixtureCase):
    fixture = 'python-lockfile'

    def test_records(self):
        records = self.fx.records('B')
        self.assertEqual(
            [(r.identity, r.value, r.kind, r.versioning) for r in records],
            [
                ('pytest', '8.3.0', 'dev', 'pep440'),
                ('requests', '2.32.3', 'direct', 'pep440'),
                ('urllib3', '2.2.0', 'indirect', 'pep440'),
            ],
        )
        self.assertTrue(all(r.digest.startswith('lock-sha256:') for r in records))
        self.assertEqual(records, self.fx.records('M_markers')[:1] + records[1:])

    def test_release_candidate_above_the_patch_dominates(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T')['verdict'], 'pass')

    def test_dev_release_below_the_patch_fails(self):
        self.assertEqual(
            self.failing(self.fx.dominance('B', 'M', 'T_low')), [('uv.lock', 'requests')]
        )

    def test_a_changed_wheel_after_the_first_is_resolved_state(self):
        result = self.fx.diff('B_wheels', 'M_wheels')
        self.assertEqual(result['transitive'], {'Python': {'count': 1, 'security': 0}})
        stale = self.fx.dominance('B_wheels', 'M_wheels', 'B_wheels')
        self.assertEqual(self.failing(stale), [('uv.lock', 'urllib3')])
        self.assertEqual(self.fx.dominance('B_wheels', 'M_wheels', 'M_wheels')['verdict'], 'pass')

    def test_a_same_version_marker_change_is_resolved_state(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B', 'M_markers')),
            '<details>\n<summary>Dependencies: 1 change (requests)</summary>\n\n'
            '- `requests` 2.32.3 resolution changed (Python)\n\n</details>\n',
        )
        stale = self.fx.dominance('B', 'M_markers', 'B')
        self.assertEqual(self.failing(stale), [('uv.lock', 'requests')])

    def test_an_optional_dependency_of_the_project_is_direct(self):
        (item,) = self.fx.diff('B_opt', 'M_opt')['direct']
        self.assertEqual(
            (item['identity'], item['kind'], item['from'], item['to']),
            ('orjson', 'direct', '3.10.0', '3.10.1'),
        )


UV_EXTRAS = """version = 1

[[package]]
name = "app"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "fonttools", extra = ["woff"] }]

[package.optional-dependencies]
cli = [{ name = "click" }]

[package.dev-dependencies]
dev = [{ name = "click" }, { name = "pytest" }]

[[package]]
name = "fonttools"
version = "4.66.1"
source = { registry = "https://pypi.org/simple" }

[package.optional-dependencies]
woff = [{ name = "brotli" }]

[[package]]
name = "brotli"
version = "1.2.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "click"
version = "8.3.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "9.1.1"
source = { registry = "https://pypi.org/simple" }
"""


class UvLock(unittest.TestCase):
    def test_kinds(self):
        records, _ = inventory.parse_uv_lock('uv.lock', UV_EXTRAS)
        self.assertEqual(
            {r.identity: r.kind for r in records},
            {'fonttools': 'direct', 'click': 'direct', 'pytest': 'dev', 'brotli': 'indirect'},
        )

    def test_skeleton_ignores_entry_values_and_member_fields(self):
        _, base = inventory.parse_uv_lock('uv.lock', UV_EXTRAS)
        for old, new in (('version = "8.3.0"', 'version = "8.3.1"'), ('cli = [', 'cli2 = [')):
            with self.subTest(old=old):
                edited = UV_EXTRAS.replace(old, new)
                self.assertEqual(inventory.parse_uv_lock('uv.lock', edited)[1], base)
        moved = UV_EXTRAS.replace('source = { virtual = "." }', 'source = { virtual = "app" }')
        self.assertNotEqual(inventory.parse_uv_lock('uv.lock', moved)[1], base)


class RegeneratedTree(FixtureCase):
    fixture = 'regenerated-tree'

    def reasons(self, result: dict) -> dict[str, str]:
        return {
            p['path']: p['reason'] for p in result['paths'] if p['path'].startswith('licenses/')
        }

    def test_target_equal_to_main(self):
        result = self.fx.dominance('B', 'M', 'T_same')
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(
            set(self.reasons(result).values()),
            {'regenerated by pkolaczk/fclones, target equals main'},
        )

    def test_a_target_whose_pin_moved_on_carries_its_own_tree(self):
        result = self.fx.dominance('B', 'M', 'T_newer')
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(
            self.reasons(result)['licenses/crates/MANIFEST'],
            'regenerated by pkolaczk/fclones, follows that pin',
        )

    def test_a_target_without_the_pin_fails_on_the_pin(self):
        result = self.fx.dominance('B', 'M', 'T_stale')
        self.assertEqual(self.failing(result), [('Dockerfile', 'pkolaczk/fclones')])

    def test_a_tree_changed_on_main_without_its_pin_fails_closed(self):
        result = self.fx.dominance('B', 'M_alone', 'B')
        self.assertEqual(
            self.failing(result),
            [
                (
                    'licenses/crates/MANIFEST',
                    'regenerated by pkolaczk/fclones, changed on main without it',
                )
            ],
        )

    def test_only_paths_inside_a_tree_are_regenerated(self):
        self.assertEqual(
            inventory.regenerated_by('licenses/crates/libc/LICENSE'), 'pkolaczk/fclones'
        )
        self.assertIsNone(inventory.regenerated_by('licenses/crates'))
        self.assertIsNone(inventory.regenerated_by('licenses/other/LICENSE'))
        self.assertIsNone(inventory.regenerated_by('web/licenses/crates/x'))


class SyncOnlyChange(FixtureCase):
    fixture = 'sync-only-change'
    canonical = types.MappingProxyType({'.editorconfig': 'root = true\n# v3\n'})

    def test_target_equal_to_main(self):
        result = self.fx.dominance('B', 'M', 'T_same', canonical=self.canonical)
        self.assertEqual(
            (result['verdict'], result['paths'][0]['reason']),
            ('pass', 'sync-owned, target equals main'),
        )

    def test_target_equal_to_the_canonical_copy(self):
        result = self.fx.dominance('B', 'M', 'T_canonical', canonical=self.canonical)
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(
            result['paths'][0]['reason'], 'sync-owned, target equals the canonical copy'
        )

    def test_target_equal_to_neither_fails(self):
        result = self.fx.dominance('B', 'M', 'T_stale', canonical=self.canonical)
        self.assertEqual(result['verdict'], 'fail')

    def test_a_path_outside_the_sync_owned_set_fails_closed(self):
        result = self.fx.dominance(
            'B', 'M', 'T_same', owned=('cliff.toml',), canonical=self.canonical
        )
        self.assertEqual(
            self.failing(result), [('.editorconfig', 'neither an inventory surface nor sync-owned')]
        )

    def test_a_wildcard_entry_owns_the_path(self):
        for owned in (('.editor*',), ('**/.editorconfig',), ('**',)):
            with self.subTest(owned=owned):
                result = self.fx.dominance(
                    'B', 'M', 'T_canonical', owned=owned, canonical=self.canonical
                )
                self.assertEqual(result['verdict'], 'pass')
        for owned in (('*/.editorconfig',), ('.editor',), ('editorconfig',)):
            with self.subTest(owned=owned):
                result = self.fx.dominance(
                    'B', 'M', 'T_same', owned=owned, canonical=self.canonical
                )
                self.assertEqual(
                    self.failing(result),
                    [('.editorconfig', 'neither an inventory surface nor sync-owned')],
                )

    def test_the_canonical_copy_is_read_without_newline_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'a.cmd').write_bytes(b'x\r\ny\r\n')
            self.assertEqual(inventory.canonical_copy(Path(tmp), 'a.cmd'), 'x\r\ny\r\n')
            self.assertIsNone(inventory.canonical_copy(Path(tmp), 'missing/b'))

    def test_an_unreadable_canonical_copy_exits_2_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync, canon = Path(tmp) / 'sync-owned.txt', Path(tmp) / 'canonical'
            sync.write_text('.editorconfig\n', encoding='utf-8')
            (canon / '.editorconfig').mkdir(parents=True)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(inventory.__file__)),
                    'dominance',
                    '--git-dir',
                    str(self.fx.dir),
                    *('--base', 'B', '--main', 'M', '--target', 'T_stale'),
                    *('--sync-owned', str(sync), '--canonical-dir', str(canon)),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual((proc.returncode, proc.stdout), (2, ''))
        self.assertRegex(
            proc.stderr,
            r'\Ainventory: canonical copy \S+/canonical/\.editorconfig: \[Errno \d+\] [^\n]+\n\Z',
        )


class GlobPattern(unittest.TestCase):
    ROWS = (
        ('cliff.toml', 'cliff.toml', True),
        ('cliff.toml', 'sub/cliff.toml', False),
        ('cliff.toml', 'cliff.toml.bak', False),
        ('cliff.toml', 'cliffXtoml', False),
        ('scripts/*.sh', 'scripts/repin-sha.sh', True),
        ('scripts/*.sh', 'scripts/sub/repin-sha.sh', False),
        ('scripts/*.sh', 'scripts/.sh', True),
        ('*', 'a/b', False),
        ('.github/**', '.github/workflows/ci.yaml', True),
        ('.github/**', '.github', False),
        ('.github/**', 'x/.github/ci.yaml', False),
        ('**/lib.sh', 'lib.sh', True),
        ('**/lib.sh', 'tests/shell/lib.sh', True),
        ('**/lib.sh', 'tests/shell/xlib.sh', False),
        ('tests/**/lib.sh', 'tests/lib.sh', True),
        ('tests/**/lib.sh', 'tests/shell/deep/lib.sh', True),
        ('tests/**/lib.sh', 'testslib.sh', False),
        ('a**b', 'a/x/b', True),
        ('foo**/bar', 'foobar', False),
        ('foo**/bar', 'foo/bar', True),
        ('foo**/bar', 'foox/y/bar', True),
    )

    def test_wildmatch_rules(self):
        for pattern, path, want in self.ROWS:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bool(inventory.glob_pattern(pattern).fullmatch(path)), want)

    def test_the_cli_reads_globs_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp) / 'sync-owned.txt'
            sync.write_text('# synced\n\n  scripts/*.sh  \n', encoding='utf-8')
            (pattern,) = inventory.load_sync_owned(str(sync))
        self.assertTrue(pattern.fullmatch('scripts/a.sh'))


class EmptyRebuild(FixtureCase):
    fixture = 'empty-rebuild'

    def test_an_empty_commit_changes_nothing(self):
        self.assertEqual(self.fx.records('B'), self.fx.records('M'))
        result = self.fx.dominance('B', 'M', 'T')
        self.assertEqual((result['verdict'], result['paths']), ('pass', []))
        self.assertEqual(inventory.render_markdown(self.fx.diff('B', 'M')), '')


class LockfileOnlyTransitive(FixtureCase):
    fixture = 'lockfile-only-transitive'

    def test_rehoisted_newer_entry_on_dev_dominates(self):
        self.assertEqual(self.fx.dominance('B', 'M', 'T')['verdict'], 'pass')

    def test_notes_count_it_as_transitive_and_mark_the_security_route(self):
        plain = self.fx.diff('B', 'M')
        self.assertEqual(plain, {'direct': [], 'transitive': {'npm': {'count': 1, 'security': 0}}})
        marked = self.fx.diff('B', 'M', security=[self.fx.sha['M']])
        self.assertEqual(
            inventory.render_markdown(marked),
            '<details>\n<summary>Dependencies: 1 change</summary>\n\n'
            '- Transitive changes: 1 npm package (1 through security updates)\n\n</details>\n',
        )
        self.assertEqual(self.fx.diff('B', 'M', security=['0' * 40]), plain)


class RenderMarkdown(unittest.TestCase):
    def test_a_long_block_names_three_and_reads_as_plain_words(self):
        def item(identity, change, before, after, security=False):
            return {
                'ecosystem': 'go',
                'identity': identity,
                'change': change,
                'from': before,
                'to': after,
                'security': security,
            }

        result = {
            'direct': [
                item('example.com/a', 'changed', 'v1.0.0', 'v1.1.0'),
                item('example.com/b', 'now direct', 'v1.0.0', 'v1.2.0'),
                item('example.com/c', 'no longer direct', 'v2.0.0', 'v2.1.0'),
                item('example.com/d', 'added', '', 'v0.1.0', security=True),
            ],
            'transitive': {'Go': {'count': 2, 'security': 0}},
        }
        self.assertEqual(
            inventory.render_markdown(result),
            '<details>\n'
            '<summary>Dependencies: 6 changes (example.com/d, example.com/a, example.com/b)'
            '</summary>\n\n'
            '- `example.com/a` v1.0.0 to v1.1.0 (Go)\n'
            '- `example.com/b` v1.0.0 to v1.2.0 now a direct dependency (Go)\n'
            '- `example.com/c` v2.0.0 to v2.1.0 no longer a direct dependency (Go)\n'
            '- Added `example.com/d` v0.1.0 (Go, security update)\n'
            '- Transitive changes: 2 Go modules\n\n</details>\n',
        )


class LockfileTwoCopies(FixtureCase):
    fixture = 'lockfile-only-transitive'

    def test_an_untouched_nested_copy_beside_a_newer_hoisted_one_fails(self):
        result = self.fx.dominance('B_two', 'M_two', 'T_two')
        self.assertEqual(self.failing(result), [('package-lock.json', 'minimatch')])
        (record,) = result['paths'][0]['records']
        self.assertEqual(record['reason'], 'target still holds 3.0.4, which main replaced')

    def test_a_raised_or_deduplicated_nested_copy_passes(self):
        self.assertEqual(self.fx.dominance('B_two', 'M_two', 'T_two_fixed')['verdict'], 'pass')
        self.assertEqual(self.fx.dominance('B_two', 'M_two', 'T_two_dedup')['verdict'], 'pass')

    def test_one_of_two_equal_nested_copies_raised_on_main_must_be_raised_on_dev(self):
        result = self.fx.dominance('B_eq', 'M_eq', 'T_eq')
        self.assertEqual(self.failing(result), [('package-lock.json', 'minimatch')])
        (record,) = result['paths'][0]['records']
        self.assertEqual(record['reason'], 'target still holds 3.0.4, which main replaced')
        self.assertEqual(self.fx.dominance('B_eq', 'M_eq', 'T_eq_carried')['verdict'], 'pass')

    def test_a_copy_raised_to_a_value_another_copy_held_was_replaced_not_removed(self):
        result = self.fx.dominance('B_dup', 'M_dup', 'B_dup')
        self.assertEqual(self.failing(result), [('package-lock.json', 'minimatch')])
        (record,) = result['paths'][0]['records']
        self.assertEqual(record['reason'], 'target still holds 3.0.4, which main replaced')
        self.assertEqual(self.fx.dominance('B_dup', 'M_dup', 'M_dup')['verdict'], 'pass')

    def test_the_copy_main_raised_is_not_carried_by_a_copy_main_left_unchanged(self):
        result = self.fx.dominance('B_eq', 'M_eq', 'T_eq_other')
        self.assertEqual(self.failing(result), [('package-lock.json', 'minimatch')])
        (record,) = result['paths'][0]['records']
        self.assertEqual(
            record['reason'],
            'target has 3.0.4, 3.0.5, 9.0.5, and its changed copies do not carry 3.1.2',
        )

    def test_main_multiset_on_the_other_parent_passes_under_name_identity(self):
        self.assertEqual(self.fx.dominance('B_eq', 'M_eq', 'T_eq_swapped')['verdict'], 'pass')


class LockfileMixedRoles(FixtureCase):
    fixture = 'lockfile-only-transitive'

    def test_a_direct_bump_beside_an_unchanged_nested_copy_is_named(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_mixed', 'M_mixed')),
            '<details>\n<summary>Dependencies: 1 change (shared)</summary>\n\n'
            '- `shared` 1.0.0 to 2.0.0 (npm)\n\n</details>\n',
        )

    def test_a_direct_dependency_that_stays_transitive_is_not_called_removed(self):
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('B_mixed', 'M_mixed_drop')),
            '<details>\n<summary>Dependencies: 1 change (shared)</summary>\n\n'
            '- `shared` 1.0.0 no longer a direct dependency (npm)\n\n</details>\n',
        )
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('M_mixed_drop', 'B_mixed')),
            '<details>\n<summary>Dependencies: 1 change (shared)</summary>\n\n'
            '- `shared` 1.0.0 now a direct dependency (npm)\n\n</details>\n',
        )

    def test_a_role_move_beside_a_transitive_bump_counts_the_bump(self):
        drop = (
            '<details>\n<summary>Dependencies: 2 changes (shared)</summary>\n\n'
            '- `shared` 1.0.0 no longer a direct dependency (npm)\n'
            '- Transitive changes: 1 npm package\n\n</details>\n'
        )
        self.assertEqual(inventory.render_markdown(self.fx.diff('B_mixed', 'M_role_drop')), drop)
        self.assertEqual(inventory.render_markdown(self.fx.diff('B_mixed', 'M_role_mix')), drop)
        self.assertEqual(
            inventory.render_markdown(self.fx.diff('M_role_drop', 'M_role_add')),
            '<details>\n<summary>Dependencies: 2 changes (shared)</summary>\n\n'
            '- `shared` 1.0.0 now a direct dependency (npm)\n'
            '- Transitive changes: 1 npm package\n\n</details>\n',
        )


class WholeSurfaceAddedOrDeleted(FixtureCase):
    fixture = 'same-tag-different-digest'

    def test_a_surface_added_on_main_and_carried_on_dev_passes(self):
        self.assertEqual(self.fx.dominance('B', 'M_add', 'T_add')['verdict'], 'pass')
        self.assertEqual(self.fx.dominance('B', 'M_add', 'T_add_moved_on')['verdict'], 'pass')

    def test_a_surface_added_on_main_with_a_stale_record_on_dev_fails_naming_it(self):
        result = self.fx.dominance('B', 'M_add', 'T_add_stale')
        self.assertEqual(self.failing(result), [('Dockerfile.tools', 'golang')])
        self.assertEqual(
            result['paths'][0]['records'][0]['reason'], 'target has 1.27.0 (111111111111)'
        )

    def test_a_surface_added_on_main_and_not_carried_fails(self):
        self.assertEqual(
            self.failing(self.fx.dominance('B', 'M_add', 'B')),
            [('Dockerfile.tools', 'added on main, absent at target')],
        )
        self.assertEqual(
            self.failing(self.fx.dominance('B', 'M_add', 'T_add_other')),
            [
                (
                    'Dockerfile.tools',
                    'added on main, but target never held its content outside the dependency records',
                )
            ],
        )

    def test_a_surface_deleted_on_main_must_be_gone_from_dev(self):
        result = self.fx.dominance('B_del', 'M_del', 'T_del')
        self.assertEqual(
            (result['verdict'], result['paths'][0]['reason']),
            ('pass', 'deleted on main and on dev'),
        )
        for target in ('B_del', 'T_del_kept'):
            with self.subTest(target=target):
                self.assertEqual(
                    self.failing(self.fx.dominance('B_del', 'M_del', target)),
                    [('Dockerfile.tools', 'deleted on main, target still has it')],
                )


class NonMachinePath(FixtureCase):
    fixture = 'non-machine-path'

    def test_source_change_on_main_fails_closed(self):
        result = self.fx.dominance('B', 'M', 'T')
        self.assertEqual(
            self.failing(result), [('main.go', 'neither an inventory surface nor sync-owned')]
        )

    def test_dockerfile_change_beyond_its_pins_fails_closed(self):
        result = self.fx.dominance('B', 'M_dockerfile', 'T')
        self.assertEqual(
            self.failing(result), [('Dockerfile', 'changed outside its dependency records')]
        )

    def test_pin_only_change_dominated_on_dev_passes(self):
        self.assertEqual(self.fx.dominance('B', 'M_pin', 'T')['verdict'], 'pass')


class SecurityMarking(FixtureCase):
    fixture = 'same-tag-different-digest'

    def test_direct_change_from_a_security_commit_is_marked(self):
        (item,) = self.fx.diff('B', 'M', security=[self.fx.sha['M'][:12]])['direct']
        self.assertTrue(item['security'])
        self.assertIn(
            '(image, security update)',
            inventory.render_markdown({'direct': [item], 'transitive': {}}),
        )


class CoverageCorpus(unittest.TestCase):
    """inventory-corpus.sh against a stub Renovate and a local `owner/app` remote."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        remote = self.tmp / 'remote' / 'owner' / 'app.git'
        seed = self.tmp / 'seed'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(seed)], check=True)
        (seed / 'Dockerfile').write_text(f'FROM alpine:3.24.2@sha256:{HEX1}\n', encoding='utf-8')
        (seed / 'tools.json').write_text('{}\n', encoding='utf-8')
        (seed / 'yarn.lock').write_text('# yarn lockfile v1\n', encoding='utf-8')
        git = ['git', '-C', str(seed), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-q', '-m', 'seed'], check=True)
        subprocess.run([*git, 'checkout', '-q', '-b', 'dev'], check=True)
        (seed / 'package.json').write_text('{"dependencies": {"x": "^1.0.0"}}\n', encoding='utf-8')
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-q', '-m', 'dev only'], check=True)
        # A lockfile the engine refuses (v1) and a require it cannot order, beside a readable Dockerfile.
        subprocess.run([*git, 'checkout', '-q', '-b', 'broken', 'main'], check=True)
        (seed / 'package.json').write_text('{"dependencies": {"x": "^1.0.0"}}\n', encoding='utf-8')
        (seed / 'package-lock.json').write_text(
            '{"lockfileVersion": 1, "dependencies": {"x": {"version": "1.0.0"}}}\n',
            encoding='utf-8',
        )
        (seed / 'go.mod').write_text(
            'module example.com/app\n\ngo 1.26\n\nrequire example.com/x master\n', encoding='utf-8'
        )
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-q', '-m', 'broken surfaces'], check=True)
        # Surfaces with no dependency in them: the only revision an empty extraction fits.
        subprocess.run([*git, 'checkout', '-q', '-b', 'bare', 'main'], check=True)
        subprocess.run([*git, 'rm', '-q', 'Dockerfile'], check=True)
        (seed / 'package.json').write_text('{"name": "app"}\n', encoding='utf-8')
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-q', '-m', 'no dependencies'], check=True)
        subprocess.run(['git', 'clone', '-q', '--bare', str(seed), str(remote)], check=True)
        other = remote.with_name('other.git')
        subprocess.run(['git', 'clone', '-q', '--bare', str(seed), str(other)], check=True)
        self.stub = self.tmp / 'renovate-stub.sh'
        self.stub.write_text(
            '#!/bin/sh\nset -eu\ncp "$1" "$STUB_SEEN"\nshift\nprintf "%s\\n" "$@" >"$STUB_ARGS"\n'
            'for a in "$@"; do case $a in --report-path=*)\n'
            '  p=${a#*=}; b=${p##*/report-}; src=$STUB_REPORT.${b%.json}\n'
            '  [ -f "$src" ] || src=$STUB_REPORT\n  cp "$src" "$p" ;;\nesac; done\n',
            encoding='utf-8',
        )
        self.stub.chmod(0o755)
        # gh applies the script's own --jq filter to a fixture listing, and refuses any other call.
        gh = self.tmp / 'bin' / 'gh'
        gh.parent.mkdir()
        gh.write_text(
            '#!/bin/sh\nset -eu\nprintf "%s\\n" "$@" >"$STUB_GH_ARGS"\n'
            'if [ $# -ne 5 ] || [ "$1 $2 $3 $4" != '
            '"api --paginate user/repos?per_page=100&affiliation=owner --jq" ]; then\n'
            '  echo "unexpected gh call: $*" >&2\n  exit 64\nfi\n'
            'if [ -n "${STUB_GH_FAIL:-}" ]; then echo "gh: HTTP 401" >&2; exit 1; fi\n'
            'jq -r "$5" "$STUB_GH_REPOS"\n',
            encoding='utf-8',
        )
        gh.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def run_corpus(
        self,
        package_files: dict,
        problems=(),
        base: str | None = None,
        repos=('owner/app',),
        listing=(),
        gh_fail=False,
        bases=(),
        per_base: dict | None = None,
        entries: dict | None = None,
    ) -> subprocess.CompletedProcess:
        report = self.tmp / 'fixture-report.json'
        for path, files in (
            (report, package_files),
            *((report.with_name(f'{report.name}.{b}'), f) for b, f in (per_base or {}).items()),
        ):
            path.write_text(
                json.dumps(
                    {
                        'problems': list(problems),
                        'repositories': {
                            # None: Renovate reports a problem for the repo on that base.
                            name: {
                                'problems': [] if files is not None else ['boom'],
                                'packageFiles': files or {},
                            }
                            for name in ('owner/app', 'owner/other')
                        }
                        | (entries or {}),
                    }
                ),
                encoding='utf-8',
            )
        (self.tmp / 'listing.json').write_text(json.dumps(list(listing)), encoding='utf-8')
        env = {
            **os.environ,
            'PATH': f'{self.tmp / "bin"}{os.pathsep}{os.environ["PATH"]}',
            'RENOVATE_CMD': str(self.stub),
            'STUB_REPORT': str(report),
            'STUB_SEEN': str(self.tmp / 'seen.json'),
            'STUB_ARGS': str(self.tmp / 'args.txt'),
            'STUB_GH_ARGS': str(self.tmp / 'gh-args.txt'),
            'STUB_GH_REPOS': str(self.tmp / 'listing.json'),
            'CORPUS_GIT_BASE': f'file://{self.tmp / "remote"}',
        }
        if gh_fail:
            env['STUB_GH_FAIL'] = '1'
        script = Path(__file__).resolve().parent / 'inventory-corpus.sh'
        argv = ['bash', str(script), *(['--base', base] if base else [])]
        argv += [arg for b in bases for arg in ('--base', b)]
        return subprocess.run(
            [*argv, '--work', str(self.tmp / 'work'), *repos],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    @staticmethod
    def pf(path: str, updates: bool = True, skip: str = '', lock_files=()) -> dict:
        dep = {'depName': 'x', 'updates': [{'newVersion': '2'}] if updates else []}
        if skip:
            dep['skipReason'] = skip
        entry = {'packageFile': path, 'deps': [dep]}
        if lock_files:
            entry['lockFiles'] = list(lock_files)
        return entry

    def test_every_managed_file_read_passes_and_narrows_the_base(self):
        proc = self.run_corpus(
            {
                'dockerfile': [self.pf('Dockerfile', updates=False)],
                'regex': [
                    self.pf('tools.json', skip='disabled'),
                    self.pf('.github/workflows/ci.yaml'),
                ],
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('checked owner/app@main: 1 package file(s) Renovate manages', proc.stdout)
        self.assertIn('RESULT: PASS', proc.stdout)
        seen = json.loads((self.tmp / 'seen.json').read_text(encoding='utf-8'))
        self.assertEqual(seen, {'force': {'baseBranchPatterns': ['main']}})
        args = (self.tmp / 'args.txt').read_text(encoding='utf-8').split()
        self.assertEqual(
            args,
            [
                '--dry-run=lookup',
                '--report-type=file',
                f'--report-path={self.tmp / "work" / "report-main.json"}',
                '--autodiscover=false',
                'owner/app',
            ],
        )

    def test_an_updated_file_the_engine_does_not_read_fails(self):
        proc = self.run_corpus(
            {'regex': [self.pf('tools.json')], 'dockerfile': [self.pf('Dockerfile')]}
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('UNREAD owner/app@main: tools.json', proc.stdout)

    def test_an_up_to_date_file_the_engine_does_not_read_fails(self):
        no_updates_key = {'packageFile': 'renovate-pins.txt', 'deps': [{'depName': 'y'}]}
        proc = self.run_corpus(
            {
                'regex': [self.pf('tools.json', updates=False), no_updates_key],
                'dockerfile': [self.pf('Dockerfile', updates=False)],
            }
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(
            [line for line in proc.stdout.splitlines() if line.startswith('UNREAD')],
            ['UNREAD owner/app@main: renovate-pins.txt', 'UNREAD owner/app@main: tools.json'],
        )
        self.assertNotIn('RESULT: PASS', proc.stdout)

    def test_a_file_whose_dependencies_renovate_all_skips_is_not_managed(self):
        proc = self.run_corpus(
            {
                'regex': [
                    self.pf('tools.json', skip='disabled'),
                    self.pf('renovate-pins.txt', updates=False, skip='unsupported-version'),
                ]
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('checked owner/app@main: 0 package file(s) Renovate manages', proc.stdout)

    def test_one_dependency_renovate_does_not_skip_makes_the_file_managed(self):
        mixed = self.pf('tools.json', skip='disabled')
        mixed['deps'].append({'depName': 'y', 'updates': []})
        proc = self.run_corpus({'regex': [mixed]})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('UNREAD owner/app@main: tools.json', proc.stdout)

    def test_an_explicit_base_narrows_renovate_and_reads_that_branch(self):
        proc = self.run_corpus({'npm': [self.pf('package.json')]}, base='dev')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('checked owner/app@dev: 1 package file(s) Renovate manages', proc.stdout)
        seen = json.loads((self.tmp / 'seen.json').read_text(encoding='utf-8'))
        self.assertEqual(seen, {'force': {'baseBranchPatterns': ['dev']}})
        args = (self.tmp / 'args.txt').read_text(encoding='utf-8').split()
        self.assertIn(f'--report-path={self.tmp / "work" / "report-dev.json"}', args)

    def test_a_dev_only_package_file_is_unread_on_main(self):
        proc = self.run_corpus({'npm': [self.pf('package.json')]})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('UNREAD owner/app@main: package.json', proc.stdout)

    def tools_json(self, skip: str = '') -> dict:
        return {'regex': [self.pf('tools.json', skip=skip)]}

    def test_a_file_only_another_base_manages_is_dev_only_once_main_ran(self):
        proc = self.run_corpus(
            {},
            bases=('dev', 'main'),
            per_base={'main': self.tools_json(skip='disabled'), 'dev': self.tools_json()},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertLess(
            lines.index('== Renovate lookup on main over 1 repo(s)'),
            lines.index('== Renovate lookup on dev over 1 repo(s)'),
        )
        self.assertIn('DEV-ONLY owner/app@dev: tools.json', lines)
        self.assertEqual([line for line in lines if line.startswith('UNREAD')], [])
        self.assertIn('RESULT: PASS', lines)

    def test_a_file_main_also_manages_stays_unread_on_another_base(self):
        proc = self.run_corpus(
            {},
            bases=('main', 'dev'),
            per_base={'main': self.tools_json(), 'dev': self.tools_json()},
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(
            [line for line in proc.stdout.splitlines() if line.startswith(('UNREAD', 'DEV-ONLY'))],
            ['UNREAD owner/app@main: tools.json', 'UNREAD owner/app@dev: tools.json'],
        )

    def test_without_main_in_this_run_an_unread_file_fails_on_any_base(self):
        first = self.run_corpus(self.tools_json(skip='disabled'))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        proc = self.run_corpus(self.tools_json(), base='dev')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('UNREAD owner/app@dev: tools.json', proc.stdout.splitlines())
        self.assertNotIn('DEV-ONLY', proc.stdout)

    def test_main_without_a_clean_result_marks_nothing_dev_only(self):
        proc = self.run_corpus(
            {}, bases=('main', 'dev'), per_base={'main': None, 'dev': self.tools_json()}
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('owner/app: no clean Renovate result in the report', proc.stderr)
        self.assertIn('UNREAD owner/app@dev: tools.json', proc.stdout.splitlines())
        self.assertNotIn('DEV-ONLY', proc.stdout)
        self.assertFalse((self.tmp / 'work' / 'expected-main-owner_app.txt').exists())

    def test_a_lockfile_renovate_updates_with_its_package_file_must_be_read(self):
        proc = self.run_corpus(
            {'npm': [self.pf('package.json', lock_files=['yarn.lock'])]}, base='dev'
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(
            [line for line in proc.stdout.splitlines() if line.startswith('UNREAD')],
            ['UNREAD owner/app@dev: yarn.lock'],
        )

    def test_a_renovate_problem_fails_closed(self):
        proc = self.run_corpus(
            {'dockerfile': [self.pf('Dockerfile')]}, problems=[{'message': 'boom'}]
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no clean Renovate result', proc.stderr)

    def test_a_clean_report_that_extracted_nothing_fails(self):
        for package_files in (
            {},
            {'dockerfile': [], 'npm': []},
            {'dockerfile': [{'packageFile': 'Dockerfile', 'deps': []}]},
        ):
            with self.subTest(package_files=package_files):
                proc = self.run_corpus(package_files)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn(
                    'checked owner/app@main: 0 package file(s) Renovate manages', proc.stdout
                )
                self.assertIn(
                    '::error::Renovate extracted no dependency from owner/app@main, '
                    'where inventory.py reads 1 record(s)',
                    proc.stderr.splitlines(),
                )
                self.assertNotIn('RESULT: PASS', proc.stdout)

    def test_an_empty_extraction_fails_beside_a_live_repo(self):
        proc = self.run_corpus(
            {},
            repos=('owner/app', 'owner/other'),
            entries={
                'owner/other': {
                    'problems': [],
                    'packageFiles': {'dockerfile': [self.pf('Dockerfile', updates=False)]},
                }
            },
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('checked owner/other@main: 1 package file(s) Renovate manages', proc.stdout)
        self.assertEqual(
            [line for line in proc.stderr.splitlines() if line.startswith('::error::')],
            [
                (
                    '::error::Renovate extracted no dependency from owner/app@main, '
                    'where inventory.py reads 1 record(s)'
                )
            ],
        )
        self.assertNotIn('RESULT: PASS', proc.stdout)

    def test_workflow_pins_alone_do_not_count_as_an_extraction(self):
        proc = self.run_corpus({'github-actions': [self.pf('.github/workflows/ci.yaml')]})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(
            '::error::Renovate extracted no dependency from owner/app@main, '
            'where inventory.py reads 1 record(s)',
            proc.stderr.splitlines(),
        )
        self.assertNotIn('RESULT: PASS', proc.stdout)

    def test_a_revision_without_dependencies_may_extract_nothing(self):
        proc = self.run_corpus({}, base='bare')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('checked owner/app@bare: 0 package file(s) Renovate manages', proc.stdout)
        self.assertNotIn('::error::', proc.stderr)
        self.assertIn('RESULT: PASS', proc.stdout)

    def test_an_empty_extraction_from_an_unreadable_revision_fails(self):
        proc = self.run_corpus({}, base='broken')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(
            '::error::Renovate extracted no dependency from owner/app@broken, and inventory.py '
            'cannot read its records: inventory: package-lock.json: no `packages` map '
            '(lockfileVersion 2 or later required) ',
            proc.stderr.splitlines(),
        )
        self.assertNotIn('RESULT: PASS', proc.stdout)

    def test_a_result_without_package_file_data_fails(self):
        shapes = (
            {'problems': []},
            {'problems': [], 'packageFiles': []},
            {'problems': [], 'packageFiles': {'dockerfile': {}}},
            {'problems': [], 'packageFiles': {'dockerfile': [{'packageFile': 'Dockerfile'}]}},
            {'problems': [], 'packageFiles': {'dockerfile': [{'deps': [{'depName': 'x'}]}]}},
            {
                'problems': [],
                'packageFiles': {'dockerfile': [{'packageFile': 'Dockerfile', 'deps': ['x']}]},
            },
        )
        live = {'problems': [], 'packageFiles': {'dockerfile': [self.pf('Dockerfile')]}}
        for shape in shapes:
            with self.subTest(shape=shape):
                proc = self.run_corpus(
                    {},
                    repos=('owner/app', 'owner/other'),
                    entries={'owner/app': shape, 'owner/other': live},
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn(
                    'owner/app: report has no packageFiles.<manager>[].{packageFile,deps} result',
                    proc.stderr,
                )
                self.assertNotIn('checked owner/app@main', proc.stdout)
                self.assertIn('checked owner/other@main: 1 package file(s)', proc.stdout)
                self.assertNotIn('RESULT: PASS', proc.stdout)

    BROKEN = (
        (
            'UNREADABLE owner/app@broken: package-lock.json: no `packages` map '
            '(lockfileVersion 2 or later required)'
        ),
        "UNREADABLE owner/app@broken: go.mod: example.com/x 'master' is not go-semver",
    )

    def test_an_updated_file_the_engine_recognises_but_refuses_fails(self):
        proc = self.run_corpus(
            {
                'npm': [self.pf('package.json', lock_files=['package-lock.json'])],
                'gomod': [self.pf('go.mod')],
            },
            base='broken',
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        for line in self.BROKEN:
            self.assertIn(line, lines)
        self.assertIn('UNREAD owner/app@broken: package-lock.json', lines)
        self.assertIn('UNREAD owner/app@broken: go.mod', lines)
        self.assertNotIn('UNREAD owner/app@broken: package.json', lines)
        self.assertNotIn('RESULT: PASS', proc.stdout)

    def test_a_refused_file_renovate_does_not_manage_still_fails(self):
        proc = self.run_corpus({'dockerfile': [self.pf('Dockerfile')]}, base='broken')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        for line in self.BROKEN:
            self.assertIn(line, lines)
        self.assertEqual([line for line in lines if line.startswith('UNREAD ')], [])
        self.assertIn('checked owner/app@broken: 1 package file(s) Renovate manages', lines)
        self.assertNotIn('RESULT: PASS', proc.stdout)

    @staticmethod
    def listed(name, default='dev', visibility='public', *, archived=False, fork=False):
        return {
            'name': name,
            'full_name': f'owner/{name}',
            'archived': archived,
            'fork': fork,
            'visibility': visibility,
            'default_branch': default,
        }

    LISTING = (
        listed('app'),
        listed('old', archived=True),
        listed('copy', fork=True),
        listed('single', 'main'),
        listed('secret', visibility='private'),
        listed('other'),
        listed('tool-catalog'),
    )

    def test_an_explicit_repo_skips_discovery(self):
        proc = self.run_corpus({'dockerfile': [self.pf('Dockerfile')]}, listing=self.LISTING)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.tmp / 'gh-args.txt').exists())

    def test_without_repos_every_eligible_dev_default_repo_is_checked(self):
        proc = self.run_corpus(
            {'dockerfile': [self.pf('Dockerfile')]}, repos=(), listing=self.LISTING
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('over 2 repo(s)', proc.stdout)
        args = (self.tmp / 'args.txt').read_text(encoding='utf-8').split()
        self.assertEqual(args[-3:], ['--autodiscover=false', 'owner/app', 'owner/other'])
        for name in ('app', 'other'):
            self.assertIn(f'checked owner/{name}@main: 1 package file(s)', proc.stdout)
            self.assertTrue((self.tmp / 'work' / f'clone-main-owner_{name}' / '.git').is_dir())
        self.assertEqual(
            sorted(p.name for p in (self.tmp / 'work').iterdir() if p.name.startswith('clone-')),
            ['clone-main-owner_app', 'clone-main-owner_other'],
        )

    def test_no_eligible_repo_fails_before_renovate(self):
        proc = self.run_corpus(
            {'dockerfile': [self.pf('Dockerfile')]}, repos=(), listing=self.LISTING[1:5]
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('no repo to check', proc.stderr)
        self.assertTrue((self.tmp / 'gh-args.txt').exists())
        self.assertFalse((self.tmp / 'args.txt').exists())

    def test_a_failed_listing_fails_before_renovate(self):
        proc = self.run_corpus(
            {'dockerfile': [self.pf('Dockerfile')]}, repos=(), listing=self.LISTING, gh_fail=True
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('gh: HTTP 401', proc.stderr)
        self.assertNotIn('no repo to check', proc.stderr)
        self.assertFalse((self.tmp / 'args.txt').exists())


class Cli(FixtureCase):
    fixture = 'go-pseudo-replace'

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = inventory.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def dominance_cli(
        self, target: str, owned: str = 'cliff.toml\n', main: str = 'M'
    ) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp) / 'sync-owned.txt'
            sync.write_text(owned, encoding='utf-8')
            return self.run_cli(
                'dominance',
                '--git-dir',
                str(self.fx.dir),
                '--base',
                'B',
                '--main',
                main,
                '--target',
                target,
                '--sync-owned',
                str(sync),
                '--canonical-dir',
                tmp,
            )

    def test_exit_codes(self):
        self.assertEqual(self.dominance_cli('T')[0], 0)
        rc, out, _ = self.dominance_cli('T_low')
        self.assertEqual(rc, 1)
        self.assertIn(
            'FAIL go.mod: example.com/inc v2.1.0+incompatible on main; '
            'target has v2.0.0+incompatible',
            out,
        )
        self.assertEqual(self.dominance_cli('T_bad')[0], 2)

    def test_uncomparable_lines_keep_the_status_apart_from_the_path(self):
        rc, out, _ = self.dominance_cli('T_bad')
        self.assertEqual(rc, 2)
        self.assertEqual(
            out.splitlines()[0],
            'UNCOMPARABLE go.mod: example.com/inc v2.1.0+incompatible on main; '
            "go-semver cannot order 'master' against 'v2.1.0+incompatible'",
        )
        rc, out, _ = self.dominance_cli('T', main='M_malformed')
        self.assertEqual(rc, 2)
        self.assertEqual(
            out.splitlines()[0],
            'UNCOMPARABLE go.mod: malformed require: example.com/a',
        )

    def test_an_unparsed_main_value_with_no_target_copy_exits_2(self):
        rc, out, _ = self.dominance_cli('T', main='M_bad_added')
        self.assertEqual(rc, 2)
        self.assertEqual(
            out.splitlines(),
            [
                "UNCOMPARABLE go.mod: example.com/new master on main; 'master' on main is not go-semver",
                f'dominance: UNCOMPARABLE (1 path(s) main changed since {self.fx.sha["B"][:12]})',
            ],
        )

    def test_input_errors_exit_2(self):
        rc, _, err = self.dominance_cli('no-such-ref')
        self.assertEqual(rc, 2)
        self.assertIn('no-such-ref: not a commit', err)
        self.assertEqual(self.dominance_cli('T', owned='# nothing\n')[0], 2)

    def test_records_and_diff(self):
        rc, out, _ = self.run_cli('records', '--git-dir', str(self.fx.dir), '--rev', 'B')
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)), 4)
        rc, out, _ = self.run_cli(
            'diff',
            '--git-dir',
            str(self.fx.dir),
            '--from',
            'B',
            '--to',
            'M',
            '--format',
            'markdown',
        )
        self.assertEqual(rc, 0)
        self.assertIn('- `example.com/inc` v2.0.0+incompatible to v2.1.0+incompatible (Go)', out)

    def test_surfaces(self):
        rc, out, _ = self.run_cli('surfaces', '--git-dir', str(self.fx.dir), '--rev', 'B')
        self.assertEqual((rc, json.loads(out)), (0, ['go.mod']))

    def test_surfaces_names_each_file_it_cannot_parse_or_order(self):
        files = {
            'Dockerfile': f'FROM alpine:3.24.2@sha256:{HEX1}\n',
            'package.json': '{"dependencies": {"x": "^1.0.0"}}\n',
            'package-lock.json': '{"lockfileVersion": 1}\n',
            'go.mod': 'module example.com/app\n\ngo 1.26\n\nrequire (\n\texample.com/x master\n)\n',
            'web/uv.lock': '[[package]]\nname = "y"\nversion = "not a version"\n',
            'web/package-lock.json': (
                '{"packages": {"": {"dependencies": {"z": 1}}, "node_modules/z": {"version": "1.0.0"}}}'
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for path, text in files.items():
                (Path(tmp) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / path).write_text(text, encoding='utf-8')
            git = ['git', '-C', tmp, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            subprocess.run([*git, 'add', '.'], check=True)
            subprocess.run([*git, 'commit', '-q', '-m', 'x'], check=True)
            rc, out, err = self.run_cli('surfaces', '--git-dir', tmp, '--rev', 'HEAD')
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out), ['Dockerfile', 'package.json'])
        self.assertEqual(
            err.splitlines(),
            [
                "inventory: go.mod: example.com/x 'master' is not go-semver",
                (
                    'inventory: package-lock.json: no `packages` map '
                    '(lockfileVersion 2 or later required)'
                ),
                'inventory: web/package-lock.json: packages[""].dependencies z is not a string',
                "inventory: web/uv.lock: y 'not a version' is not pep440",
            ],
        )


if __name__ == '__main__':
    unittest.main()
