"""stryker-shard.py's discover, plan, matrix, merge and costs, the ci reporter, and their wiring in
mutation-stryker-repo.yaml and weekly-stryker.yaml."""

from __future__ import annotations

import gzip
import importlib.util
import io
import itertools
import json
import os
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from typing import ClassVar

import yaml

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
SHARD = SCRIPTS / 'stryker-shard.py'
WORKFLOW = SCRIPTS.parent / '.github' / 'workflows' / 'weekly-stryker.yaml'
REPO_WORKFLOW = SCRIPTS.parent / '.github' / 'workflows' / 'mutation-stryker-repo.yaml'
SHA = '0123456789abcdef0123456789abcdef01234567'


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ss = load('stryker_shard', SHARD)
mutation_fuzz = load('mutation_fuzz', SCRIPTS / 'mutation_fuzz.py')
workflow_replay = load('workflow_replay', SCRIPTS / 'workflow_replay.py')


def mutant(id_: str, status: str, covered=(), killed=(), line: int = 1) -> dict:
    m = {
        'id': id_,
        'mutatorName': 'ConditionalExpression',
        'replacement': 'true',
        'status': status,
        'location': {'start': {'line': line, 'column': 1}, 'end': {'line': line, 'column': 5}},
    }
    if status in ('Killed', 'Survived', 'Timeout', 'NoCoverage'):
        m['coveredBy'] = list(covered)
    if killed:
        m['killedBy'] = list(killed)
    return m


def test(id_: str, name: str, line: int) -> dict:
    return {'id': id_, 'name': name, 'location': {'start': {'line': line, 'column': 3}}}


def report() -> dict:
    return {
        'schemaVersion': '2',
        'thresholds': {'high': 80, 'low': 60},
        'projectRoot': '/w',
        'files': {
            'src/a.ts': {
                'language': 'typescript',
                'source': 'a',
                'mutants': [
                    mutant('1', 'Killed', ['t1', 't2'], ['t1'], 1),
                    mutant('2', 'Survived', ['t2'], line=2),
                ],
            },
            'src/b.ts': {
                'language': 'typescript',
                'source': 'b',
                'mutants': [mutant('3', 'Killed', ['t3'], ['t3'])],
            },
            'src/c.ts': {
                'language': 'typescript',
                'source': 'c',
                'mutants': [mutant('4', 'CompileError'), mutant('5', 'NoCoverage', line=2)],
            },
        },
        'testFiles': {
            'src/a.test.ts': {
                'source': 'ta',
                'tests': [test('t1', 'a one', 1), test('t2', 'a two', 5)],
            },
            'src/b.test.ts': {'source': 'tb', 'tests': [test('t3', 'b one', 1)]},
        },
    }


def entry(repo: str = 'r', dir_: str = 'web') -> dict:
    return {'repo': repo, 'sha': SHA, 'dir': dir_, 'key': ss.mutation_cost.entry_key(repo, dir_)}


ENTRY = entry()


def plan_pass(files: dict[str, tuple[int, int, int]], **extra) -> dict:
    return {
        'workers': 4,
        'setup_ms': 30000,
        'ranged': False,
        'files': {p: {'run': r, 'early': e, 'net_ms': n} for p, (r, e, n) in files.items()},
        **extra,
    }


class PlanAndMatrix(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_prior_is_net_time_on_the_test_runners_plus_the_measured_overhead_per_run_mutant(
        self,
    ):
        doc = ss.sizing(
            'r',
            'web',
            SHA,
            {'thresholds': {'break': 50}},
            plan_pass({'src/a.ts': (10, 2, 5000), 'src/b c.ts': (1, 0, 0)}),
        )
        self.assertEqual(
            doc['files']['src/a.ts'],
            {'run': 10, 'early': 2, 'prior_secs': round(5 + 10 * ss.PRIOR_OVERHEAD_SECS, 3)},
        )
        self.assertEqual(
            (doc['setup_secs'], doc['break'], doc['runners']), (30 + ss.JOB_OVERHEAD_SECS, 50, 4)
        )
        self.assertFalse(doc['shardable'])
        self.assertEqual(ss.valid_sizing(doc, ENTRY), '')
        for bad, why in (
            ({'break': 'x'}, 'break threshold'),
            ({'workers': 0}, 'workers'),
            ({'runners': 0}, 'runners'),
            ({'runners': 5}, 'runners'),
            ({'runners': '2'}, 'runners'),
            ({'sha': 'f' * 40}, 'sha'),
            ({'files': {'a': {'run': -1, 'early': 0, 'prior_secs': 1}}}, 'file list'),
        ):
            with self.subTest(bad=bad):
                self.assertIn(why, ss.valid_sizing({**doc, **bad}, ENTRY))

    def test_with_checkers_half_the_workers_run_tests_so_net_time_costs_double(self):
        doc = ss.sizing('r', 'web', SHA, {}, plan_pass({'src/a.ts': (10, 2, 5000)}, runners=2))
        self.assertEqual(
            doc['files']['src/a.ts']['prior_secs'],
            round(5 * 4 / 2 + 10 * ss.PRIOR_OVERHEAD_SECS, 3),
        )
        self.assertEqual((doc['runners'], ss.valid_sizing(doc, ENTRY)), (2, ''))
        forged = ss.sizing('r', 'web', SHA, {}, plan_pass({'src/a.ts': (10, 2, 5000)}, runners='x'))
        self.assertIn('runners', ss.valid_sizing(forged, ENTRY))

    def test_a_ranged_mutate_entry_runs_whole_on_its_config(self):
        files = {f'src/f{i}.ts': (100, 0, 14_400_000) for i in range(6)}
        for ranged in (None, 'yes'):
            with self.subTest(ranged=ranged):
                self.assertFalse(
                    ss.sizing('r', 'web', SHA, {}, plan_pass(files, ranged=ranged))['shardable']
                )
        doc = ss.sizing('r', 'web', SHA, {}, plan_pass(files, ranged=True))
        self.assertFalse(doc['shardable'])
        run, _ = self.matrix({'r~web': doc})
        self.assertEqual(run, [{**ENTRY, 'shard': 1, 'shards': 1}])
        self.assertEqual(
            ss.mutate_list(json.loads((self.tmp / 'out' / 'r~web.json').read_text()), 1), ''
        )

    def matrix(
        self,
        docs: dict[str, dict],
        entries=(ENTRY,),
        history: dict[str, str] | None = None,
    ) -> tuple[list, list]:
        plans = self.tmp / 'plans'
        plans.mkdir(parents=True, exist_ok=True)
        for key, doc in docs.items():
            (plans / f'{key}.json').write_text(doc if isinstance(doc, str) else json.dumps(doc))
        hist = self.tmp / 'history'
        hist.mkdir(exist_ok=True)
        for key, text in (history or {}).items():
            (hist / f'{key}.json').write_text(text)
        return ss.matrix(list(entries), plans, self.tmp / 'out', hist)

    def big(self) -> dict:
        files = {f'src/f{i}.ts': (100, 0, 14_400_000) for i in range(6)}
        return ss.sizing('r', 'web', SHA, {}, plan_pass(files))

    def test_an_entry_over_the_target_shards_by_file_and_names_each_shard_s_artifact(self):
        run, merge = self.matrix({'r~web': self.big()})
        k = run[0]['shards']
        self.assertGreater(k, 1)
        self.assertEqual(run, [{**ENTRY, 'shard': n, 'shards': k} for n in range(1, k + 1)])
        self.assertEqual(
            merge,
            [
                {
                    **ENTRY,
                    'shards': k,
                    'artifacts': '{'
                    + ','.join(f'strykershard-r~web-{n}' for n in range(1, k + 1))
                    + '}',
                }
            ],
        )
        plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
        self.assertEqual(
            sorted({f['shard'] for f in plan['files'].values()}), list(range(1, k + 1))
        )
        lists = [ss.mutate_list(plan, n).split(',') for n in range(1, k + 1)]
        self.assertEqual(sorted(p for lst in lists for p in lst), sorted(plan['files']))
        self.assertTrue(all(p.endswith('.ts') for lst in lists for p in lst))

    def test_history_prices_a_file_by_its_measured_cost_and_rides_in_the_run_plan(self):
        # Each big() file's probe is 144.96 s per run mutant.
        hist = {
            'files': {
                'src/f0.ts': {'secs': 14000.0, 'mutants': 100},
                'src/x.ts': {'secs': 1.0, 'mutants': 1},
                'src/f2.ts': {'secs': 0.0, 'mutants': 100},
                'src/f3.ts': {'secs': 1e9, 'mutants': 100},
            },
            'setup_secs': 100,
        }
        self.matrix({'r~web': self.big()}, history={'r~web': json.dumps(hist)})
        plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
        self.assertEqual(plan['files']['src/f0.ts']['cost'], 'measured')
        self.assertEqual(plan['files']['src/f1.ts']['cost'], 'prior')
        self.assertEqual(
            (plan['files']['src/f2.ts']['cost'], plan['files']['src/f3.ts']['cost']),
            ('refused', 'refused'),
        )
        self.assertEqual(
            plan['files']['src/f2.ts']['work_secs'], plan['files']['src/f1.ts']['work_secs']
        )
        self.assertEqual(plan['history']['files']['src/f0.ts'], {'secs': 14000.0, 'mutants': 100})
        self.assertEqual(plan['history']['files']['src/x.ts'], {'secs': 1.0, 'mutants': 1})

    def test_a_history_with_any_malformed_part_is_ignored_whole_and_named(self):
        good = {'src/f0.ts': {'secs': 3000.0, 'mutants': 100}}
        for bad in (
            {'files': {**good, 'src/x.ts': {'secs': -1, 'mutants': 1}}},
            {'files': {**good, 'src/x.ts': {'secs': 1.0, 'mutants': 0}}},
            {'files': {**good, 'src/x.ts': {'secs': 1.0, 'mutants': 1, 'extra': 1}}},
            {'files': good, 'setup_secs': 'x'},
            {'files': good, 'other': 1},
        ):
            with self.subTest(bad=bad):
                shutil.rmtree(self.tmp, ignore_errors=True)
                with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                    self.matrix({'r~web': self.big()}, history={'r~web': json.dumps(bad)})
                plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
                self.assertEqual(
                    ({f['cost'] for f in plan['files'].values()}, plan['history']),
                    ({'prior'}, None),
                )
                self.assertIn('::warning::r:web: its cost history failed to load', err.getvalue())

    def test_a_measured_setup_outside_the_factor_of_its_probe_is_refused(self):
        doc = self.big()
        for setup, priced in (
            (300.0, 300.0),
            (doc['setup_secs'] * ss.mutation_cost.PRICE_FACTOR * 2, None),
            (0.0, None),
        ):
            with self.subTest(setup=setup):
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.matrix(
                    {'r~web': doc},
                    history={'r~web': json.dumps({'files': {}, 'setup_secs': setup})},
                )
                plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
                one = (
                    sum(f['work_secs'] for f in plan['files'].values()) / 60
                    + (priced or doc['setup_secs']) / 60
                )
                self.assertAlmostEqual(plan['one_job_minutes'], one, delta=0.1)

    def test_a_forged_sizing_with_numbers_a_float_cannot_hold_or_an_unsafe_shardable_path_is_refused(
        self,
    ):
        doc = self.big()
        for bad, why in (
            ({'workers': 10**400}, 'workers'),
            ({'runners': 10**400}, 'runners'),
            ({'files': {'src/a.ts': {'run': 10**400, 'early': 0, 'prior_secs': 1}}}, 'file list'),
            ({'files': {'src/a.ts': {'run': 1, 'early': 0, 'prior_secs': 10**400}}}, 'file list'),
            ({'setup_secs': 10**400}, 'setup_secs'),
            ({'files': {'src/a,b.ts': {'run': 1, 'early': 0, 'prior_secs': 1}}}, 'src/a,b.ts'),
        ):
            with self.subTest(bad=bad):
                self.assertIn(why, ss.valid_sizing({**doc, **bad}, ENTRY))
        unsafe = {
            **doc,
            'shardable': False,
            'files': {'src/a,b.ts': {'run': 1, 'early': 0, 'prior_secs': 1}},
        }
        self.assertEqual(ss.valid_sizing(unsafe, ENTRY), '')

    def test_an_entry_past_max_shards_runs_at_it_and_says_so(self):
        files = {f'src/f{i}.ts': (100, 0, 14_400_000) for i in range(300)}
        doc = ss.sizing('r', 'web', SHA, {}, plan_pass(files))
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            run, _ = self.matrix({'r~web': doc})
        plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
        self.assertEqual(
            (len(run), plan['shards']), (ss.mutation_cost.MAX_SHARDS, ss.mutation_cost.MAX_SHARDS)
        )
        self.assertEqual(plan['capped'], f'a cap of {ss.mutation_cost.MAX_SHARDS} shards')
        self.assertIn('::warning::r:web: runs in 16 shard(s), capped by', err.getvalue())

    def test_hostile_plans_and_histories_keep_the_matrix_within_256_jobs_and_end(self):
        prices = (1e300, 0, -1, float('nan'), 10**400, None, 'x', 1.7e308)
        entries = [entry('r', f'd{i}') for i in range(ss.mutation_cost.MAX_SHARDS)]
        plans, hist = self.tmp / 'plans', self.tmp / 'history'
        hist.mkdir(parents=True)
        plans.mkdir()
        for i, e in enumerate(entries):
            price = prices[i % len(prices)]
            doc = ss.sizing(
                e['repo'],
                e['dir'],
                SHA,
                {},
                plan_pass({f'src/f{n}.ts': (100, 0, 14_400_000) for n in range(300)}),
            )
            for f in doc['files'].values():
                if i % 2 and price is None:
                    del f['prior_secs']
                elif i % 2:
                    f['prior_secs'] = price
                if i % 3 == 0:
                    f['run'] = 2**53
            doc.update(
                workers=10**400 if i == 5 else doc['workers'],
                setup_secs=1e308 if i % 4 == 1 else 90.0,
            )
            (plans / f'{e["key"]}.json').write_text(json.dumps(doc))
            hp = prices[(i + 3) % len(prices)]
            (hist / f'{e["key"]}.json').write_text(
                json.dumps(
                    {
                        'files': {
                            f'src/f{n}.ts': {'secs': hp, 'mutants': 10**400 if n % 5 == 0 else 1}
                            for n in range(300)
                        },
                        'setup_secs': hp,
                    }
                )
            )
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SHARD),
                    'matrix',
                    '--entries',
                    json.dumps(entries),
                    '--plans',
                    str(plans),
                    '--history',
                    str(hist),
                    '--out-plans',
                    str(self.tmp / 'out'),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            self.fail('the matrix did not end within 120 s')
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        out = json.loads(proc.stdout)
        self.assertLessEqual(len(out['run']), 256)
        self.assertEqual([m['key'] for m in out['merge']], [e['key'] for e in entries])
        for m in out['merge']:
            with self.subTest(entry=m['key']):
                self.assertTrue(1 <= m['shards'] <= ss.mutation_cost.MAX_SHARDS)
                self.assertEqual(sum(1 for r in out['run'] if r['key'] == m['key']), m['shards'])
        self.assertIn('its cost history failed to load', proc.stderr)

    def test_an_unreadable_history_prices_from_the_plan_pass(self):
        for text in ('[1]', 'not json', '{"files": [1]}'):
            with self.subTest(text=text):
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.matrix({'r~web': self.big()}, history={'r~web': text})
                plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
                self.assertEqual({f['cost'] for f in plan['files'].values()}, {'prior'})

    def test_a_run_that_fits_runs_whole_on_its_config_s_patterns(self):
        small = ss.sizing('r', 'web', SHA, {}, plan_pass({'src/a.ts': (1, 0, 10)}))
        run, merge = self.matrix({'r~web': small})
        self.assertEqual(run, [{**ENTRY, 'shard': 1, 'shards': 1}])
        self.assertEqual(merge[0]['artifacts'], 'strykershard-r~web-1')
        plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
        self.assertIsNone(plan['files'])
        self.assertEqual(ss.mutate_list(plan, 1), '')

    def test_an_entry_with_no_usable_plan_runs_in_one_job_and_one_not_enrolled_not_at_all(self):
        other = entry('r', 'o')
        skipped = entry('r', 's')
        forged = {**self.big(), 'repo': 'victim'}
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            run, merge = self.matrix(
                {'r~web': forged, 'r~s': {**skipped, 'skipped': True}, 'r~o': '{"torn'},
                entries=(ENTRY, other, skipped),
            )
        self.assertEqual(
            run, [{**ENTRY, 'shard': 1, 'shards': 1}, {**other, 'shard': 1, 'shards': 1}]
        )
        self.assertEqual([m['dir'] for m in merge], ['web', 'o'])
        self.assertIn("::warning::r:web: its repo 'victim' is not the entry's 'r'", err.getvalue())
        self.assertIn('::warning::r:o: its plan failed to load (', err.getvalue())
        self.assertIn('- r:s: not enrolled, so it does not run', err.getvalue())
        self.assertIsNone(json.loads((self.tmp / 'out' / 'r~web.json').read_text())['files'])

    def test_an_entry_list_discover_did_not_write_fails_the_call(self):
        for bad in ({'repo': 'n'}, 'x', {**ENTRY, 'key': 'r-web'}, {**ENTRY, 'dir': '../q'}):
            with self.subTest(bad=bad), self.assertRaises((SystemExit, ValueError)):
                self.matrix({}, entries=[bad])

    def test_an_unsafe_file_name_keeps_the_entry_in_one_job(self):
        doc = self.big()
        doc['files']['src/a,b.ts'] = {'run': 1, 'early': 0, 'prior_secs': 1.0}
        doc['shardable'] = False
        run, _ = self.matrix({'r~web': doc})
        self.assertEqual(len(run), 1)


def layout(seed: int) -> list[list]:
    """A file's mutant spans as Stryker places them: nested blocks, one-line runs, early results,
    now and then one mutant spanning the whole file."""
    rng = random.Random(seed)
    spans, line = [], 1
    while line < 400:
        size = rng.choice((1, 1, 2, 5, 20, 60))
        first, last = line, line + size - 1
        spans.append([first, last, rng.choice((None, 0.96, 3.0, 40.0))])
        for _ in range(rng.randrange(0, 4)):
            a = rng.randint(first, last)
            spans.append([a, rng.randint(a, last), rng.choice((None, 1.5, 9.0))])
        line = last + rng.choice((0, 1, 3))
    if seed % 7 == 0:
        spans.append([1, line, 2.0])
    return spans


def inside(first: int, last: int, unit: str) -> bool:
    """Stryker's rule (instrumenter babel-transformer.ts shouldMutate): a range wholly includes the mutant."""
    m = ss.RANGE.fullmatch(unit)
    return int(m['first']) <= first and last <= int(m['last'])


def holds(unit: str, span: list) -> bool:
    """Whether UNIT, a whole file or a range of it, holds SPAN's mutant."""
    first, last = ss.lines_of(unit)
    return first <= span[0] and span[1] <= last


def ids(spans: list[list]) -> list[list]:
    """The plan pass's id of each span's mutant: an early one is ignored by a directive."""
    return [
        [
            a,
            1,
            b,
            5,
            'ConditionalExpression',
            *((None, None) if p is not None else ('Ignored', 'x')),
        ]
        for a, b, p in spans
    ]


def heavy(spans: list[list]) -> dict:
    run = sum(1 for sp in spans if sp[2] is not None)
    return {
        'run': run,
        'early': len(spans) - run,
        'spans': spans,
        'ids': ids(spans),
        'directives': [],
        'blob': 'a' * 40,
    }


class Ranges(unittest.TestCase):
    """A file past one shard's work runs as line ranges of it (stryker-js --mutate file:first-last)."""

    def test_a_split_file_s_ranges_hold_each_of_its_mutants_exactly_once(self):
        for seed in range(120):
            spans = layout(seed)
            f = heavy(spans)
            with self.subTest(seed=seed):
                pieces, _ = ss.split('src/s.ts', f, 1000.0, 120.0, None)
                units = [u for u, *_ in pieces]
                if seed % 7 == 0:
                    self.assertEqual(len(units), 1)
                else:
                    self.assertGreater(len(units), 1)
                for first, last, _ in spans:
                    self.assertEqual(sum(inside(first, last, u) for u in units), 1, (first, last))
                for unit, run, early, _ in pieces:
                    held = [sp for sp in spans if inside(sp[0], sp[1], unit)]
                    self.assertEqual(
                        (run, early),
                        (sum(sp[2] is not None for sp in held), sum(sp[2] is None for sp in held)),
                    )
                lines = sorted(ss.lines_of(u) for u in units)
                self.assertTrue(all(a[1] < b[0] for a, b in itertools.pairwise(lines)), lines)
                self.assertAlmostEqual(sum(w for *_, w in pieces), 1000.0)

    def test_the_cut_keeps_every_range_inside_the_room_its_atoms_allow(self):
        spans = [[n, n, 10.0] for n in range(1, 101)]
        pieces, atom = ss.split('src/s.ts', heavy(spans), 1000.0, 260.0, None)
        self.assertEqual(len(pieces), 4)
        self.assertTrue(all(w <= 260.0 for *_, w in pieces), [w for *_, w in pieces])
        self.assertAlmostEqual(atom, 10.0)
        whole, atom = ss.split('src/s.ts', heavy([[1, 100, 10.0], *spans]), 1000.0, 260.0, None)
        self.assertEqual([u for u, *_ in whole], ['src/s.ts:1-100'])
        self.assertAlmostEqual(atom, 1000.0)
        fits, _ = ss.split('src/s.ts', heavy(spans), 200.0, 260.0, None)
        self.assertEqual([u for u, *_ in fits], ['src/s.ts'])

    def test_the_cut_takes_more_pieces_when_uneven_atoms_need_them_to_fit_the_room(self):
        room = ss.mutation_cost.TOP_SECS - 31.2 * 60
        # Five one-line atoms of 80 min: four pieces can do no better than 160/160/80.
        five = [[10 * n + 1, 10 * n + 1, 10.0] for n in range(5)]
        # Three methods of 100, 170 and 100 mutants, each one atom through its body's
        # BlockStatement mutant: 221.7 min of work as assign() prices them at 72.4 s a
        # mutant with 2 test runners on 4 workers; two pieces give 161.8/59.9.
        methods, line = [], 1
        for size in (100, 170, 100):
            methods.append([line, line + size, 10.0])
            methods += [[line + 1 + i, line + 1 + i, 10.0] for i in range(size - 1)]
            line += size + 2
        rows = [(five, 400 * 60.0, 5), (methods, 221.7 * 60, 3)]
        for spans, work, count in rows:
            with self.subTest(atoms=count):
                pieces, atom = ss.split('src/s.ts', heavy(spans), work, room, None)
                self.assertLessEqual(atom, room)
                self.assertTrue(all(w <= room for *_, w in pieces), [w / 60 for *_, w in pieces])
                self.assertAlmostEqual(sum(w for *_, w in pieces), work)

    def test_a_cut_no_piece_count_fits_keeps_the_smallest_largest_piece(self):
        spans = [[1, 1, 10.0], [3, 3, 10.0], [5, 5, 30.0]]
        pieces, atom = ss.split('src/s.ts', heavy(spans), 500.0, 200.0, None)
        self.assertEqual([u for u, *_ in pieces], ['src/s.ts:1-3', 'src/s.ts:5-5'])
        self.assertAlmostEqual(atom, 300.0)

    def test_an_unchanged_file_cuts_by_its_measured_ranges_and_a_changed_one_by_its_prior(self):
        spans = [[n, n, 10.0] for n in range(1, 101)]
        history = {
            'files': {
                'src/s.ts:1-50': {'secs': 9000.0, 'mutants': 50},
                'src/s.ts:51-100': {'secs': 1000.0, 'mutants': 50},
            },
            'blobs': {'src/s.ts': 'a' * 40},
        }
        measured, _ = ss.split('src/s.ts', heavy(spans), 1000.0, 520.0, history)
        self.assertEqual([u for u, *_ in measured], ['src/s.ts:1-28', 'src/s.ts:29-100'])
        changed = {**history, 'blobs': {'src/s.ts': 'b' * 40}}
        even, _ = ss.split('src/s.ts', heavy(spans), 1000.0, 520.0, changed)
        self.assertEqual([u for u, *_ in even], ['src/s.ts:1-50', 'src/s.ts:51-100'])

    def test_a_file_past_one_shard_is_planned_as_ranges_across_shards(self):
        # scroll-controller.ts in run 43153701425: 1,672 run mutants at 72.4 worker-seconds each.
        mutants = [[2 * n - 1, 2 * n, 72_400 - 960] for n in range(1, 1673)]
        files = {'src/s.ts': (1672, 0, sum(m[2] for m in mutants)), 'src/t.ts': (10, 0, 1000)}
        doc = ss.sizing('r', 'web', SHA, {}, plan_pass(files), {'src/s.ts': 'a' * 40})
        spans = ss.spans_of(mutants, 1.0)
        doc['files']['src/s.ts'].update(spans=spans, ids=ids(spans), directives=[[1, 0], [9, 4]])
        self.assertEqual(ss.valid_sizing(doc, ENTRY), '')
        run, merge = self.matrix(doc)
        plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
        ranges = [u for u in plan['files'] if u.startswith('src/s.ts:')]
        self.assertGreater(len(ranges), 1)
        self.assertGreater(len({plan['files'][u]['shard'] for u in ranges}), 1)
        self.assertEqual(plan['blobs'], {'src/s.ts': 'a' * 40})
        self.assertTrue(all(m <= plan['top_minutes'] for m in plan['projected_minutes']))
        self.assertEqual(plan['top_minutes'], round(ss.mutation_cost.TOP_SECS / 60, 1))
        self.assertEqual(plan['ids'], {'src/s.ts': ids(spans)})
        listed = [
            u for n in range(1, plan['shards'] + 1) for u in ss.mutate_list(plan, n).split(',')
        ]
        openers = [u for u in listed if ss.OPENER.fullmatch(u)]
        self.assertEqual(sorted(u for u in listed if u not in openers), sorted(plan['files']))
        # Each shard holding a range of the file also passes each directive's opener.
        shards = {plan['files'][u]['shard'] for u in ranges}
        self.assertEqual(
            sorted(openers), sorted(['src/s.ts:1:0-1:2', 'src/s.ts:9:4-9:6'] * len(shards))
        )
        self.assertEqual(sum(plan['files'][u]['run'] for u in ranges), 1672)
        self.assertEqual(len(run), merge[0]['shards'])

    def test_a_span_no_cut_shortens_is_planned_whole_and_named(self):
        mutants = [[1, 3344, 71_440], *[[2 * n - 1, 2 * n, 71_440] for n in range(1, 1672)]]
        doc = ss.sizing('r', 'web', SHA, {}, plan_pass({'src/s.ts': (1672, 0, 1672 * 71_440)}))
        spans = ss.spans_of(mutants, 1.0)
        doc['files']['src/s.ts'].update(spans=spans, ids=ids(spans), directives=[])
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.matrix(doc)
        plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
        self.assertEqual(list(plan['files'] or {'src/s.ts:1-3344': 0}), ['src/s.ts:1-3344'])
        self.assertIn(
            "::warning::r:web: src/s.ts holds one mutant's span of 1513 min", err.getvalue()
        )

    def matrix(self, doc: dict) -> tuple[list, list]:
        self.tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.tmp / 'plans').mkdir()
        (self.tmp / 'plans' / 'r~web.json').write_text(json.dumps(doc))
        return ss.matrix([ENTRY], self.tmp / 'plans', self.tmp / 'out', self.tmp / 'history')

    def test_the_plan_pass_s_spans_and_blobs_ride_in_the_sizing_and_are_checked(self):
        pass_ = plan_pass({'src/a.ts': (2, 1, 3000)})
        pass_['files']['src/a.ts']['mutants'] = [[1, 4, 1000], [2, 2, None], [6, 6, 2000]]
        doc = ss.sizing('r', 'web', SHA, {}, pass_, {'src/a.ts': 'c' * 40, 'src/x.ts': 'd' * 40})
        f = doc['files']['src/a.ts']
        self.assertEqual(
            (f['spans'], f['blob']),
            (
                [
                    [1, 4, 1 + ss.PRIOR_OVERHEAD_SECS],
                    [2, 2, None],
                    [6, 6, 2 + ss.PRIOR_OVERHEAD_SECS],
                ],
                'c' * 40,
            ),
        )
        self.assertEqual(ss.valid_sizing(doc, ENTRY), '')
        for bad in (
            [[1, 4, 1.0]],
            [[4, 1, 1.0], [2, 2, None], [6, 6, 2.0]],
            [[0, 1, 1.0], [2, 2, None], [6, 6, 1.0]],
            [[1, 4, None], [2, 2, None], [6, 6, 2.0]],
            'x',
        ):
            with self.subTest(bad=bad):
                self.assertIn(
                    'spans',
                    ss.valid_sizing({**doc, 'files': {'src/a.ts': {**f, 'spans': bad}}}, ENTRY),
                )
        self.assertIn(
            'blob', ss.valid_sizing({**doc, 'files': {'src/a.ts': {**f, 'blob': 'zz'}}}, ENTRY)
        )
        torn = plan_pass({'src/a.ts': (2, 1, 3000)})
        torn['files']['src/a.ts']['mutants'] = [[1, 4, 1000]]
        self.assertNotIn('spans', ss.sizing('r', 'web', SHA, {}, torn)['files']['src/a.ts'])

    def test_the_plan_pass_s_ids_and_directives_ride_in_the_sizing_and_are_checked(self):
        pass_ = plan_pass({'src/a.ts': (2, 1, 3000)})
        pass_['files']['src/a.ts']['mutants'] = [[1, 4, 1000], [2, 2, None], [6, 6, 2000]]
        rows = [
            [1, 3, 4, 2, 'BlockStatement', None, None],
            [2, 5, 2, 9, 'StringLiteral', 'Ignored', 'Ignored using a comment'],
            [6, 1, 6, 4, 'ArithmeticOperator', None, None],
        ]
        pass_['files']['src/a.ts']['ids'] = rows
        marks = {'src/a.ts': [[1, 0]]}
        f = ss.sizing('r', 'web', SHA, {}, pass_, {'src/a.ts': 'c' * 40}, marks)['files'][
            'src/a.ts'
        ]
        self.assertEqual((f['ids'], f['directives']), (rows, [[1, 0]]))
        doc = ss.sizing('r', 'web', SHA, {}, pass_, {'src/a.ts': 'c' * 40}, marks)
        self.assertEqual(ss.valid_sizing(doc, ENTRY), '')
        for bad in (
            rows[:2],
            [rows[1], rows[0], rows[2]],
            [[0, *rows[0][1:]], *rows[1:]],
            [[*rows[0][:4], 7, None, None], *rows[1:]],
        ):
            with self.subTest(bad=bad):
                torn = json.loads(json.dumps(pass_))
                torn['files']['src/a.ts']['ids'] = bad
                self.assertNotIn(
                    'ids', ss.sizing('r', 'web', SHA, {}, torn, None, marks)['files']['src/a.ts']
                )
                self.assertIn(
                    'ids', ss.valid_sizing({**doc, 'files': {'src/a.ts': {**f, 'ids': bad}}}, ENTRY)
                )
        for marked in ([[0, 0]], [[1, -1]], [[1]], [[1, 0]] * (ss.MAX_DIRECTIVES + 1)):
            with self.subTest(marked=marked):
                self.assertIn(
                    'directives',
                    ss.valid_sizing(
                        {**doc, 'files': {'src/a.ts': {**f, 'directives': marked}}}, ENTRY
                    ),
                )
        # Past MAX_DIRECTIVES, or with no scan of its source, a file keeps no ids and runs whole.
        many = {'src/a.ts': [[n, 0] for n in range(1, ss.MAX_DIRECTIVES + 2)]}
        for got in (
            ss.sizing('r', 'web', SHA, {}, pass_, None, many),
            ss.sizing('r', 'web', SHA, {}, pass_),
        ):
            self.assertNotIn('ids', got['files']['src/a.ts'])

    def test_a_file_past_one_shard_with_no_ids_runs_whole_and_is_named(self):
        f = heavy([[n, n, 10.0] for n in range(1, 101)])
        del f['ids'], f['directives']
        self.assertEqual(ss.split('src/s.ts', f, 1000.0, 260.0, None)[0][0][0], 'src/s.ts')
        mutants = [[2 * n - 1, 2 * n, 72_400 - 960] for n in range(1, 1673)]
        doc = ss.sizing('r', 'web', SHA, {}, plan_pass({'src/s.ts': (1672, 0, 1672 * 71_440)}))
        doc['files']['src/s.ts']['spans'] = ss.spans_of(mutants, 1.0)
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.matrix(doc)
        plan = json.loads((self.tmp / 'out' / 'r~web.json').read_text())
        self.assertEqual((plan['shards'], plan['ids']), (1, {}))
        self.assertIn("src/s.ts projects 1513 min, past a shard's 135 min", err.getvalue())
        self.assertIn('gave no mutant ids', err.getvalue())

    def test_openers_are_the_file_scope_directives_where_babel_counts_them(self):
        text = (
            '// Stryker disable all\r\n'
            'const s = "\U0001f600"; /* Stryker restore ArithmeticOperator: why */\n'
            '  // Stryker disable next-line all\n'
            '  //Stryker restore all\u2028'
            '/** Stryker disable all */ x; // stryker disable all\n'
            '\t/* Stryker disable StringLiteral,BooleanLiteral */\n'
        )
        # The emoji is two UTF-16 columns; next-line, JSDoc and lower-case forms are not file-scope.
        self.assertEqual(ss.openers(text), [[1, 0], [2, 16], [4, 2], [6, 1]])
        self.assertEqual(ss.openers('let a = 1;\n'), [])

    def test_each_file_s_directives_are_read_from_its_blob_at_head(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        (tmp / 'src').mkdir()
        (tmp / 'src' / 'a.ts').write_text('a;\n  // Stryker disable all\nb;\n')
        (tmp / 'src' / 'b.ts').write_text('b;\n')
        for cmd in (
            ['init', '-q'],
            ['add', '.'],
            ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'x'],
        ):
            subprocess.run(['git', '-C', str(tmp), *cmd], check=True, env=env)
        (tmp / 'src' / 'a.ts').write_text('instrumented in place\n')
        versions = ss.blobs(tmp, ['src/a.ts', 'src/b.ts'])
        self.assertEqual(ss.directives(tmp, versions), {'src/a.ts': [[2, 2]], 'src/b.ts': []})
        self.assertEqual(ss.directives(tmp, {'src/a.ts': '0' * 40}), {})
        self.assertEqual(ss.directives(tmp / 'nowhere', versions), {})

    def test_each_file_s_blob_is_its_version_at_head(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        (tmp / 'web' / 'src').mkdir(parents=True)
        (tmp / 'web' / 'src' / 'a.ts').write_text('export const a = 1;\n')
        for cmd in (
            ['init', '-q'],
            ['add', '.'],
            ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'x'],
        ):
            subprocess.run(['git', '-C', str(tmp), *cmd], check=True, env=env)
        want = subprocess.run(
            ['git', '-C', str(tmp), 'rev-parse', 'HEAD:web/src/a.ts'],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()
        (tmp / 'web' / 'src' / 'a.ts').write_text('instrumented in place\n')
        self.assertEqual(ss.blobs(tmp / 'web', ['src/a.ts', 'src/none.ts']), {'src/a.ts': want})
        self.assertEqual(ss.blobs(tmp / 'nowhere', ['src/a.ts']), {})


def ranged_report(shard: int, lines: list[int], statuses: dict[int, str] | None = None) -> dict:
    rep = {'schemaVersion': '2', 'thresholds': {'high': 80, 'low': 60}, 'testFiles': {}}
    status = {n: 'Killed' if n % 2 else 'Survived' for n in lines} | (statuses or {})
    rep['files'] = {
        'src/s.ts': {
            'language': 'typescript',
            'source': 's',
            'mutants': [
                {
                    **mutant(
                        f'{shard}{n}', status[n], killed=['t'] * (status[n] == 'Killed'), line=n
                    ),
                    'coveredBy': [],
                }
                for n in lines
            ],
        }
    }
    return rep


def plan_ids(early: dict[int, str] | None = None, lines: range = range(1, 5)) -> list[list]:
    """The plan pass's ids of ranged_report's mutants, line N ignored with reason EARLY[N]."""
    early = early or {}
    return [
        [
            n,
            1,
            n,
            5,
            'ConditionalExpression',
            *(('Ignored', early[n]) if n in early else (None, None)),
        ]
        for n in lines
    ]


class RangeMerge(unittest.TestCase):
    PLAN: ClassVar[dict] = {
        'repo': 'r',
        'dir': 'web',
        'sha': SHA,
        'shards': 2,
        'break': None,
        'files': {
            'src/s.ts:1-2': {'shard': 1, 'run': 2, 'early': 0},
            'src/s.ts:3-4': {'shard': 2, 'run': 2, 'early': 0},
        },
        'mutants': {'src/s.ts:1-2': 2, 'src/s.ts:3-4': 2},
        'ids': {'src/s.ts': plan_ids()},
        'directives': {'src/s.ts': []},
        'blobs': {'src/s.ts': 'a' * 40},
        'history': None,
    }

    def write(self, reports: dict[int, list[int]], statuses: dict[int, str] | None = None) -> None:
        self.tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        for n, lines in reports.items():
            d = self.tmp / 'shards' / f'strykershard-r~web-{n}'
            d.mkdir(parents=True)
            (d / 'result.json').write_text(json.dumps(ranged_report(n, lines, statuses)))
            (d / 'meta.json').write_text(
                json.dumps(
                    {
                        'repo': 'r',
                        'dir': 'web',
                        'shard': n,
                        'shards': 2,
                        'complete': True,
                        'skipped': False,
                    }
                )
            )
            unit = 'src/s.ts:1-2' if n == 1 else 'src/s.ts:3-4'
            (d / 'costs.json').write_text(
                json.dumps({'setup_secs': 50.0, 'files': {unit: {'secs': 10.0 * n, 'mutants': 2}}})
            )

    def merge(self, plan: dict | None = None) -> tuple[int, str]:
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            rc = ss.merge(
                plan or self.PLAN, self.tmp / 'shards', self.tmp / 'stage', self.tmp / 'costs'
            )
        return rc, err.getvalue()

    def test_a_split_file_merges_into_one_file_with_each_mutant_once(self):
        self.write({1: [1, 2], 2: [3, 4]})
        rc, err = self.merge()
        self.assertEqual(rc, 0, err)
        merged = json.loads((self.tmp / 'stage' / 'mutation.json').read_text())
        lines = [m['location']['start']['line'] for m in merged['files']['src/s.ts']['mutants']]
        self.assertEqual(sorted(lines), [1, 2, 3, 4])
        self.assertEqual(len({m['id'] for m in merged['files']['src/s.ts']['mutants']}), 4)
        self.assertEqual(
            json.loads((self.tmp / 'stage' / 'meta.json').read_text())['score'], '50.0'
        )
        history = json.loads((self.tmp / 'costs' / 'costs.json').read_text())
        self.assertEqual(
            history,
            {
                'files': {
                    'src/s.ts:1-2': {'secs': 10.0, 'mutants': 2},
                    'src/s.ts:3-4': {'secs': 20.0, 'mutants': 2},
                },
                'setup_secs': 50.0,
                'blobs': {'src/s.ts': 'a' * 40},
                'prior_error': None,
            },
        )

    def test_a_mutant_lost_or_counted_twice_publishes_nothing(self):
        overlapping = {
            **self.PLAN,
            'files': {
                'src/s.ts:1-3': {'shard': 1, 'run': 3, 'early': 0},
                'src/s.ts:3-4': {'shard': 2, 'run': 2, 'early': 0},
            },
        }
        for reports, plan, why in (
            ({1: [1, 2], 2: [3]}, None, 'shard 2 reports 1 mutants in its ranges'),
            ({1: [1, 2], 2: [2, 3, 4]}, None, 'shard 2 reports 3 mutants'),
            ({1: [1, 2], 2: [1, 4]}, None, 'shard 2 reports a mutant of'),
            ({1: [1, 2, 3], 2: [3, 4]}, overlapping, 'shards 1 and 2 both report one mutant'),
        ):
            with self.subTest(why=why):
                self.write(reports)
                rc, err = self.merge(plan)
                self.assertEqual(rc, 1)
                self.assertIn(why, err)
                self.assertFalse((self.tmp / 'stage' / 'meta.json').exists())

    def test_the_plan_pass_decides_which_mutants_a_directive_ignores(self):
        # `/* Stryker disable all */` leads the file and `// Stryker restore all` sits between
        # lines 3 and 4, inside shard 2's range: the whole-file plan pass ignores lines 1 to 3.
        # Each ranged run lost the disable in a subtree its range skipped and ran them.
        why = 'Ignored using a comment'
        plan = {**self.PLAN, 'ids': {'src/s.ts': plan_ids({1: why, 2: why, 3: why})}}
        self.write({1: [1, 2], 2: [3, 4]}, {4: 'Killed'})
        rc, err = self.merge(plan)
        self.assertEqual(rc, 0, err)
        merged = json.loads((self.tmp / 'stage' / 'mutation.json').read_text())
        got = {
            m['location']['start']['line']: (m['status'], m.get('statusReason'), 'killedBy' in m)
            for m in merged['files']['src/s.ts']['mutants']
        }
        self.assertEqual(
            got,
            {1: ('Ignored', why, False), 2: ('Ignored', why, False), 3: ('Ignored', why, False)}
            | {4: ('Killed', None, True)},
        )
        self.assertEqual(
            json.loads((self.tmp / 'stage' / 'meta.json').read_text())['score'], '100.0'
        )

    def test_a_range_s_mutants_that_are_not_the_plan_pass_s_publish_nothing(self):
        moved = plan_ids()
        moved[3][1] = 2
        for plan, statuses, why in (
            (
                {**self.PLAN, 'ids': {'src/s.ts': moved}},
                None,
                (
                    "shard 2 reports a mutant of 'src/s.ts' at 4:1 ('ConditionalExpression') its "
                    'plan pass did not find'
                ),
            ),
            (
                {**self.PLAN, 'ids': {'src/s.ts': [*plan_ids(), plan_ids(lines=range(4, 5))[0]]}},
                None,
                "no shard reports the mutant of 'src/s.ts' its plan pass found at 4:1",
            ),
            (
                self.PLAN,
                {3: 'Ignored'},
                (
                    "shard 2 ignored the mutant of 'src/s.ts' at 3:1 ('ConditionalExpression') "
                    'that its plan pass runs'
                ),
            ),
            ({**self.PLAN, 'ids': {}}, None, "the plan holds no mutant ids for 'src/s.ts'"),
        ):
            with self.subTest(why=why):
                self.write({1: [1, 2], 2: [3, 4]}, statuses)
                rc, err = self.merge(plan)
                self.assertEqual(rc, 1)
                self.assertIn(why, err)
                self.assertFalse((self.tmp / 'stage' / 'meta.json').exists())


class RangeHistory(unittest.TestCase):
    def write(self, plan: dict, measured: dict) -> dict:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        ss.write_history(tmp, [{'setup_secs': 60.0}], plan, measured, set(plan['mutants']))
        return json.loads((tmp / 'costs.json').read_text())

    def plan(self, blob: str, old: dict) -> dict:
        return {
            'mutants': {'src/s.ts:1-40': 4, 'src/s.ts:41-90': 4, 'src/t.ts': 1},
            'blobs': {'src/s.ts': blob, 'src/t.ts': 'e' * 40},
            'history': old,
        }

    OLD: ClassVar[dict] = {
        'files': {
            'src/s.ts:1-30': {'secs': 1.0, 'mutants': 3},
            'src/s.ts:31-60': {'secs': 2.0, 'mutants': 3},
            'src/s.ts:61-90': {'secs': 3.0, 'mutants': 3},
            'src/t.ts': {'secs': 4.0, 'mutants': 1},
        },
        'setup_secs': 40.0,
        'blobs': {'src/s.ts': 'a' * 40, 'src/t.ts': 'f' * 40},
    }

    def test_an_unchanged_file_keeps_its_old_ranges_that_no_new_measure_overlaps(self):
        got = self.write(
            self.plan('a' * 40, self.OLD), {'src/s.ts:1-40': {'secs': 9.0, 'mutants': 4}}
        )
        self.assertEqual(sorted(got['files']), ['src/s.ts:1-40', 'src/s.ts:61-90', 'src/t.ts'])
        self.assertEqual(got['blobs'], {'src/s.ts': 'a' * 40, 'src/t.ts': 'f' * 40})

    def test_a_changed_file_keeps_only_this_run_s_measures(self):
        got = self.write(
            self.plan('b' * 40, self.OLD), {'src/s.ts:1-40': {'secs': 9.0, 'mutants': 4}}
        )
        self.assertEqual(sorted(got['files']), ['src/s.ts:1-40', 'src/t.ts'])
        self.assertEqual(got['blobs'], {'src/s.ts': 'b' * 40, 'src/t.ts': 'f' * 40})

    def test_a_short_sample_keeps_the_last_measure_and_a_whole_file_row_replaces_its_ranges(self):
        got = self.write(
            self.plan('a' * 40, self.OLD), {'src/s.ts:1-40': {'secs': 9.0, 'mutants': 1}}
        )
        self.assertEqual(
            sorted(got['files']), ['src/s.ts:1-30', 'src/s.ts:31-60', 'src/s.ts:61-90', 'src/t.ts']
        )
        whole = {**self.plan('a' * 40, self.OLD), 'mutants': {'src/s.ts': 8, 'src/t.ts': 1}}
        got = self.write(whole, {'src/s.ts': {'secs': 9.0, 'mutants': 8}})
        self.assertEqual(sorted(got['files']), ['src/s.ts', 'src/t.ts'])

    def test_a_history_with_blobs_reads_back_and_one_with_bad_blobs_is_refused(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'h.json').write_text(json.dumps(self.OLD))
        self.assertEqual(ss.read_history(tmp / 'h.json')['blobs'], self.OLD['blobs'])
        (tmp / 'h.json').write_text(json.dumps({**self.OLD, 'blobs': {'src/s.ts': 'nope'}}))
        with self.assertRaises(ValueError):
            ss.read_history(tmp / 'h.json')


class Fuzz(unittest.TestCase):
    """Generated bad versions of an entry's plan or history still plan the entry, and say so."""

    VICTIM = entry('r', 'web')

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plan = json.dumps(
            ss.sizing(
                'r',
                'web',
                SHA,
                {'thresholds': {'break': 50}},
                plan_pass({f'src/f{n}.ts': (100, 1, 14_400_000) for n in range(6)}),
            )
        ).encode()
        self.history = json.dumps(
            {'files': {'src/f0.ts': {'secs': 14000.0, 'mutants': 100}}, 'setup_secs': 100.0}
        ).encode()
        self.n = 0

    def matrix(self, plan: bytes | None, history: bytes | None) -> tuple:
        self.n += 1
        root = self.tmp / str(self.n)
        key = self.VICTIM['key']
        for kind, data in (('plans', plan), ('history', history)):
            (root / kind).mkdir(parents=True)
            if data is not None:
                (root / kind / f'{key}.json').write_bytes(data)
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            run, merge = ss.matrix([self.VICTIM], root / 'plans', root / 'out', root / 'history')
        out = root / 'out' / f'{key}.json'
        return run, merge, out.read_bytes() if out.exists() else None, err.getvalue()

    def assert_planned(self, clean: tuple, got: tuple, label: str, *, accepted: bool) -> None:
        run, merge, out, err = got
        self.assertEqual([m['key'] for m in merge], [self.VICTIM['key']], label)
        self.assertEqual(len(run), merge[0]['shards'], label)
        if not accepted:
            self.assertTrue(
                out == clean[2] or '::warning::r:web:' in err,
                f'{label}: not reported\n{err[-500:]}',
            )

    def test_a_bad_plan_file_still_plans_the_entry_and_is_reported(self):
        clean = self.matrix(self.plan, self.history)
        self.assertGreater(clean[1][0]['shards'], 1)
        valid = json.loads(self.plan)
        for label, data in mutation_fuzz.bad_inputs(valid, seed=1):
            with self.subTest(label=label):
                got = self.matrix(data, self.history)
                doc = json.loads(data) if mutation_fuzz.usable(data) else None
                # A break of any number, or none, is a sizing the plan pass can write.
                accepted = (
                    doc is not None
                    and ss.valid_sizing(doc, self.VICTIM) == ''
                    and {k for k in doc if doc[k] != valid.get(k)} <= {'break'}
                )
                self.assert_planned(clean, got, label, accepted=accepted)

    def test_a_bad_cost_history_still_plans_the_entry_and_is_reported(self):
        clean = self.matrix(self.plan, self.history)
        for label, data in mutation_fuzz.bad_inputs(json.loads(self.history), seed=2):
            with self.subTest(label=label):
                got = self.matrix(self.plan, data)
                # A missing history is an entry's first run.
                self.assert_planned(clean, got, label, accepted=data is None)


class Costs(unittest.TestCase):
    def test_each_planned_mutant_is_charged_its_gap_from_the_start(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 't').write_text(
            '#workers\t2\n#launch\t1000\n#start\t31000\n32000\tsrc/a.ts\n'
            '35000\tsrc/b.ts\n36000\tsrc/a.ts\n#done\n'
        )
        got = ss.costs(tmp / 't')
        self.assertEqual(
            got,
            {
                'setup_secs': 30 + ss.JOB_OVERHEAD_SECS,
                'files': {
                    'src/a.ts': {'secs': 4.0, 'mutants': 2},
                    'src/b.ts': {'secs': 6.0, 'mutants': 1},
                },
            },
        )
        (tmp / 't').write_text('#workers\t2\n#start\t31000\n32000\tsrc/a.ts\n')
        with self.assertRaises(ValueError):
            ss.costs(tmp / 't')


def unsharded() -> dict:
    """What one job reports: files a, b, c, tests t1..t3 with Stryker's own ids."""
    rep = report()
    rep['files']['src/c.ts']['mutants'][1] = mutant('5', 'Survived', ['t1'], line=2)
    return rep


def shard_report(owned: set[str], ids: dict[str, str], stale: str) -> dict:
    """One shard's report: its own files as one job reports them, and the others' with STALE status, which the target's code could write."""
    rep = unsharded()
    for name, f in rep['files'].items():
        for m in f['mutants']:
            m['coveredBy'] = [ids[t] for t in m.get('coveredBy', [])]
            if 'killedBy' in m:
                m['killedBy'] = [ids[t] for t in m['killedBy']]
            if name not in owned:
                m['status'] = stale
    for tf in rep['testFiles'].values():
        for t in tf['tests']:
            t['id'] = ids[t['id']]
    return rep


class Merge(unittest.TestCase):
    PLAN: ClassVar[dict] = {
        'repo': 'r',
        'dir': 'web',
        'sha': SHA,
        'shards': 2,
        'break': 70,
        'files': {
            'src/a.ts': {'shard': 1, 'run': 2, 'early': 0},
            'src/b.ts': {'shard': 2, 'run': 1, 'early': 0},
            'src/c.ts': {'shard': 2, 'run': 1, 'early': 1},
        },
        'history': {
            'files': {
                'src/c.ts': {'secs': 5.0, 'mutants': 1},
                'src/gone.ts': {'secs': 1.0, 'mutants': 1},
            },
            'setup_secs': 40.0,
        },
    }

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.shards = self.tmp / 'shards'
        owned = {1: {'src/a.ts'}, 2: {'src/b.ts', 'src/c.ts'}}
        ids = {1: {'t1': '0', 't2': '1', 't3': '2'}, 2: {'t1': '7', 't2': '8', 't3': '9'}}
        for n in (1, 2):
            d = self.shards / f'strykershard-r~web-{n}'
            d.mkdir(parents=True)
            (d / 'result.json').write_text(json.dumps(shard_report(owned[n], ids[n], 'Killed')))
            (d / 'meta.json').write_text(
                json.dumps(
                    {
                        'repo': 'r',
                        'dir': 'web',
                        'shard': n,
                        'shards': 2,
                        'complete': True,
                        'skipped': False,
                    }
                )
            )
            (d / 'costs.json').write_text(
                json.dumps(
                    {
                        'setup_secs': 50 + n,
                        'files': {p: {'secs': 10.0 * n, 'mutants': 1} for p in owned[n]}
                        | {'src/a.ts' if n == 2 else 'src/b.ts': {'secs': 999.0, 'mutants': 1}},
                    }
                )
            )

    def merge(self, plan=None) -> int:
        for d in ('stage', 'costs'):
            shutil.rmtree(self.tmp / d, ignore_errors=True)
        self.summary = self.tmp / 'summary.md'
        self.summary.unlink(missing_ok=True)
        os.environ['GITHUB_STEP_SUMMARY'] = str(self.summary)
        self.addCleanup(os.environ.pop, 'GITHUB_STEP_SUMMARY', None)
        return ss.merge(plan or self.PLAN, self.shards, self.tmp / 'stage', self.tmp / 'costs')

    def history(self) -> dict:
        return json.loads((self.tmp / 'costs' / 'costs.json').read_text())

    def tracker(self, artifacts: pathlib.Path) -> str:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / 'stryker-aggregate.py'),
                '--repo',
                'r',
                '--artifacts-dir',
                str(artifacts),
                '--week',
                '2026-11-14 22:00',
                '--run-url',
                'https://example.invalid/run/1',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def test_the_merged_report_reads_as_one_job_s_in_the_tracker_and_the_badge(self):
        self.assertEqual(self.merge(), 0)
        one = self.tmp / 'one'
        one.mkdir()
        rep = unsharded()
        for f in rep['files'].values():
            f.pop('source')
        for f in rep['testFiles'].values():
            f.pop('source')
        (one / 'mutation.json').write_text(json.dumps(rep))
        (one / 'meta.json').write_text(json.dumps({'repo': 'r', 'dir': 'web'}))
        self.assertEqual(self.tracker(self.tmp / 'stage'), self.tracker(one))
        meta = json.loads((self.tmp / 'stage' / 'meta.json').read_text())
        # 2 killed of 2 killed + 2 survived, as the run job's metrics parse scored one job.
        self.assertEqual(meta, {'repo': 'r', 'dir': 'web', 'score': '50.0'})
        self.assertEqual(
            self.summary.read_text(),
            '### `r:web`: mutation score `50.0`% over `2` shard(s), `2` of `4` detected\n',
        )
        self.assertEqual((self.tmp / 'stage' / 'below.txt').read_text(), 'true')

    def test_the_break_threshold_is_the_one_stryker_resolved(self):
        for resolved, planned, below in (
            (55, None, 'true'),
            (None, 70, 'false'),
            (45, 70, 'false'),
            ('x', 70, 'true'),
        ):
            with self.subTest(resolved=resolved, planned=planned):
                for d in self.shards.iterdir():
                    rep = json.loads((d / 'result.json').read_text())
                    rep['thresholds'] = {'high': 80, 'low': 60, 'break': resolved}
                    (d / 'result.json').write_text(json.dumps(rep))
                self.assertEqual(self.merge({**self.PLAN, 'break': planned}), 0)
                self.assertEqual((self.tmp / 'stage' / 'below.txt').read_text(), below)

    def test_the_break_compares_the_unrounded_score(self):
        lone = {**self.PLAN, 'shards': 1, 'files': None, 'break': 60}
        shutil.rmtree(self.shards / 'strykershard-r~web-2')
        rep = unsharded()
        rep['files'] = {
            'src/a.ts': {
                'language': 'typescript',
                'source': 'a',
                'mutants': [
                    mutant(
                        str(i),
                        'Killed' if i < 1999 else 'Survived',
                        ['t1'],
                        ['t1'] if i < 1999 else (),
                        i + 1,
                    )
                    for i in range(3334)
                ],
            }
        }
        (self.shards / 'strykershard-r~web-1' / 'result.json').write_text(json.dumps(rep))
        self.assertEqual(self.merge(plan=lone), 0)
        # 1999 of 3334 is 59.958%, shown as 60.0 and still under a break of 60.
        self.assertEqual(
            json.loads((self.tmp / 'stage' / 'meta.json').read_text())['score'], '60.0'
        )
        self.assertEqual((self.tmp / 'stage' / 'below.txt').read_text(), 'true')

    def test_a_report_with_no_valid_mutant_is_not_below_a_break(self):
        lone = {**self.PLAN, 'shards': 1, 'files': None, 'break': 50}
        shutil.rmtree(self.shards / 'strykershard-r~web-2')
        rep = unsharded()
        rep['files'] = {
            'src/a.ts': {
                'language': 'typescript',
                'source': 'a',
                'mutants': [mutant('1', 'CompileError'), mutant('2', 'Ignored')],
            }
        }
        (self.shards / 'strykershard-r~web-1' / 'result.json').write_text(json.dumps(rep))
        self.assertEqual(self.merge(plan=lone), 0)
        self.assertEqual((self.tmp / 'stage' / 'below.txt').read_text(), 'false')

    def test_a_capped_plan_is_named_with_a_warning_in_the_summary(self):
        capped = {
            **self.PLAN,
            'projected_minutes': [250.0, 240.0],
            'top_minutes': 157.0,
            'capped': 'a cap of 16 shards',
        }
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(self.merge(plan=capped), 0)
        top = '157.0'
        note = (
            'r:web: runs in 2 shard(s), capped by a cap of 16 shards, so its longest sized '
            f'shard projects 250.0 min, past the {top} min the band allows'
        )
        self.assertIn(f'::warning::{note}', err.getvalue())
        self.assertIn(
            '### `r:web`: runs in `2` shard(s), capped by `a cap of 16 shards`, so its '
            f'longest sized shard projects `250.0` min, past the `{top}` min the band allows\n',
            self.summary.read_text(),
        )
        self.merge()
        self.assertNotIn('capped', self.summary.read_text())

    def test_ids_stay_unique_and_coverage_points_at_the_merged_tests(self):
        self.merge()
        merged = json.loads((self.tmp / 'stage' / 'mutation.json').read_text())
        tests = {t['id']: t['name'] for f in merged['testFiles'].values() for t in f['tests']}
        self.assertEqual(sorted(tests.values()), ['a one', 'a two', 'b one'])
        mutants = [m for f in merged['files'].values() for m in f['mutants']]
        self.assertEqual(len({m['id'] for m in mutants}), len(mutants))
        by_status = {(tests[m['killedBy'][0]] if m.get('killedBy') else None) for m in mutants}
        self.assertEqual(by_status, {'a one', 'b one', None})
        self.assertTrue(all(i in tests for m in mutants for i in m.get('coveredBy', [])))
        self.assertNotIn('source', merged['files']['src/a.ts'])

    def test_the_costs_keep_each_owned_measure_and_the_last_one_of_files_still_planned(self):
        self.merge()
        self.assertEqual(
            self.history(),
            {
                'files': {
                    'src/a.ts': {'secs': 10.0, 'mutants': 1},
                    'src/b.ts': {'secs': 20.0, 'mutants': 1},
                    'src/c.ts': {'secs': 20.0, 'mutants': 1},
                },
                'setup_secs': 51.5,
                'blobs': {},
                'prior_error': None,
            },
        )

    def test_a_shard_that_did_not_finish_publishes_nothing_and_records_its_costs(self):
        meta = self.shards / 'strykershard-r~web-2' / 'meta.json'
        meta.write_text(json.dumps({**json.loads(meta.read_text()), 'complete': False}))
        self.assertEqual(self.merge(), 1)
        self.assertFalse((self.tmp / 'stage' / 'meta.json').exists())
        self.assertEqual(
            self.summary.read_text(), '### `r:web`: no score this week (`shard 2 did not finish`)\n'
        )
        self.assertEqual(sorted(self.history()['files']), ['src/a.ts', 'src/b.ts', 'src/c.ts'])

    def test_a_missing_shard_keeps_the_last_measure_of_its_files(self):
        shutil.rmtree(self.shards / 'strykershard-r~web-2')
        self.assertEqual(self.merge(), 1)
        self.assertFalse((self.tmp / 'stage' / 'meta.json').exists())
        self.assertEqual(
            self.summary.read_text(), '### `r:web`: no score this week (`shard 2 has no result`)\n'
        )
        self.assertEqual(
            self.history(),
            {
                'files': {
                    'src/a.ts': {'secs': 10.0, 'mutants': 1},
                    'src/c.ts': {'secs': 5.0, 'mutants': 1},
                },
                'setup_secs': 51.0,
                'blobs': {},
                'prior_error': None,
            },
        )

    def test_a_missing_shard_beside_one_that_killed_nothing_is_named_and_its_costs_kept(self):
        shutil.rmtree(self.shards / 'strykershard-r~web-2')
        d = self.shards / 'strykershard-r~web-1'
        rep = json.loads((d / 'result.json').read_text())
        for f in rep['files'].values():
            for m in f['mutants']:
                if m['status'] == 'Killed':
                    m['status'] = 'Survived'
        (d / 'result.json').write_text(json.dumps(rep))
        self.assertEqual(self.merge(), 1)
        self.assertEqual(
            self.summary.read_text(), '### `r:web`: no score this week (`shard 2 has no result`)\n'
        )
        self.assertEqual(self.history()['files']['src/a.ts'], {'secs': 10.0, 'mutants': 1})

    def test_a_shard_s_short_sample_of_a_file_keeps_its_last_measure(self):
        plan = {
            **self.PLAN,
            'mutants': {'src/a.ts': 10, 'src/b.ts': 1, 'src/c.ts': 1},
            'history': {
                **self.PLAN['history'],
                'files': {
                    **self.PLAN['history']['files'],
                    'src/a.ts': {'secs': 50.0, 'mutants': 10},
                },
            },
        }
        self.assertEqual(self.merge(plan), 0)
        self.assertEqual(self.history()['files']['src/a.ts'], {'secs': 50.0, 'mutants': 10})
        self.assertEqual(self.history()['files']['src/b.ts'], {'secs': 20.0, 'mutants': 1})

    def test_a_missing_owned_file_publishes_nothing(self):
        rep = json.loads((self.shards / 'strykershard-r~web-2' / 'result.json').read_text())
        del rep['files']['src/c.ts']
        (self.shards / 'strykershard-r~web-2' / 'result.json').write_text(json.dumps(rep))
        self.assertEqual(self.merge(), 1)
        self.assertFalse((self.tmp / 'stage' / 'meta.json').exists())

    def test_shards_that_left_no_result_still_record_their_costs(self):
        for d in self.shards.iterdir():
            (d / 'result.json').unlink()
        self.assertEqual(self.merge(), 1)
        self.assertFalse((self.tmp / 'stage' / 'meta.json').exists())
        self.assertEqual(self.history()['files']['src/a.ts'], {'secs': 10.0, 'mutants': 1})
        self.assertNotIn('src/gone.ts', self.history()['files'])

    def test_a_lone_shard_that_left_nothing_keeps_every_last_measure(self):
        lone = {**self.PLAN, 'shards': 1, 'files': None}
        shutil.rmtree(self.shards)
        self.assertEqual(self.merge(plan=lone), 1)
        self.assertEqual(
            self.history(),
            {
                'files': self.PLAN['history']['files'],
                'setup_secs': 40.0,
                'blobs': {},
                'prior_error': None,
            },
        )

    def test_a_run_that_killed_nothing_publishes_and_records_nothing(self):
        for d in self.shards.iterdir():
            rep = json.loads((d / 'result.json').read_text())
            for f in rep['files'].values():
                for m in f['mutants']:
                    if m['status'] == 'Killed':
                        m['status'] = 'Survived'
            (d / 'result.json').write_text(json.dumps(rep))
        self.assertEqual(self.merge(), 1)
        self.assertFalse((self.tmp / 'stage' / 'meta.json').exists())
        self.assertFalse((self.tmp / 'costs').exists())

    def test_a_lone_shard_s_report_is_staged_as_stryker_wrote_it(self):
        lone = {**self.PLAN, 'shards': 1, 'files': None, 'break': None}
        shutil.rmtree(self.shards / 'strykershard-r~web-2')
        self.assertEqual(self.merge(plan=lone), 0)
        written = json.loads((self.shards / 'strykershard-r~web-1' / 'result.json').read_text())
        for f in [*written['files'].values(), *written['testFiles'].values()]:
            f.pop('source')
        self.assertEqual(json.loads((self.tmp / 'stage' / 'mutation.json').read_text()), written)
        self.assertEqual((self.tmp / 'stage' / 'below.txt').read_text(), 'false')
        self.assertEqual(sorted(self.history()['files']), ['src/a.ts', 'src/b.ts', 'src/c.ts'])

    def test_an_entry_whose_every_shard_skipped_publishes_nothing_and_fails(self):
        for d in self.shards.iterdir():
            meta = json.loads((d / 'meta.json').read_text())
            (d / 'meta.json').write_text(json.dumps({**meta, 'skipped': True}))
        self.assertEqual(self.merge(), 1)
        self.assertFalse((self.tmp / 'stage').exists())

    def test_a_shard_naming_another_entry_is_not_read(self):
        meta = self.shards / 'strykershard-r~web-2' / 'meta.json'
        meta.write_text(json.dumps({**json.loads(meta.read_text()), 'repo': 'victim'}))
        self.assertEqual(self.merge(), 1)
        self.assertFalse((self.tmp / 'stage' / 'meta.json').exists())


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class Reporter(unittest.TestCase):
    HARNESS = """
import { pathToFileURL } from 'node:url';
const { strykerPlugins } = await import(pathToFileURL(process.argv[1]));
const [plugin] = strykerPlugins;
if (plugin.kind !== 'Reporter' || plugin.name !== 'ci') process.exit(9);
const cwd = process.cwd();
const R = plugin.injectableClass;
const options = JSON.parse(process.env.HARNESS_OPTIONS || '{"concurrency": 4, "checkers": []}');
// The plan's mutants carry Stryker's 0-based lines: these sit on lines 1 to 3, 5 and 2 of the file.
const at = (first, last) => ({ start: { line: first - 1, column: 2 }, end: { line: last - 1, column: 9 } });
// onMutantTested gets mutation-testing-report-schema locations, which are 1-based.
const schema = (first, last) => ({ start: { line: first, column: 3 }, end: { line: last, column: 10 } });
const r = new R(options, { [`${cwd}/src/a.ts`]: { mutate: true }, [`${cwd}/src/z.ts`]: { mutate: true },
  [`${cwd}/src/skip.ts`]: { mutate: false },
  ...(process.env.HARNESS_RANGED ? { [`${cwd}/src/r.ts`]: { mutate: [
    // A directive's opener on line 9 (stryker-shard.py mutate_list), listed first.
    { start: { line: 8, column: 4 }, end: { line: 8, column: 6 } },
    { start: { line: 0, column: 0 }, end: { line: 1, column: Number.MAX_SAFE_INTEGER } },
    { start: { line: 2, column: 0 }, end: { line: 4, column: Number.MAX_SAFE_INTEGER } },
    { start: { line: 5, column: 0 }, end: { line: 8, column: Number.MAX_SAFE_INTEGER } }] } } : {}) });
r.onMutationTestingPlanReady({ mutantPlans: [
  { plan: 'Run', netTime: 120, mutant: { id: '1', fileName: `${cwd}/src/a.ts`, mutatorName: 'BlockStatement', location: at(1, 3) } },
  { plan: 'Run', netTime: 30, mutant: { id: '3', fileName: `${cwd}/src/a.ts`, mutatorName: 'StringLiteral', location: at(5, 5) } },
  { plan: 'EarlyResult', mutant: { id: '2', fileName: `${cwd}/src/a.ts`, mutatorName: 'StringLiteral', status: 'Ignored',
    statusReason: 'Ignored using a comment', location: at(2, 2) } },
  ...(process.env.HARNESS_RANGED ? [
    { plan: 'Run', netTime: 7, mutant: { id: '4', fileName: `${cwd}/src/r.ts`, location: at(7, 8) } },
    { plan: 'Run', netTime: 5, mutant: { id: '5', fileName: `${cwd}/src/r.ts`, location: at(2, 2) } },
    { plan: 'Run', netTime: 5, mutant: { id: '6', fileName: `${cwd}/src/r.ts`, location: at(3, 3) } },
    { plan: 'Run', netTime: 5, mutant: { id: '7', fileName: `${cwd}/src/r.ts`, location: at(9, 9) } }] : [])] });
r.onMutantTested({ id: '2', fileName: `${cwd}/src/a.ts`, location: schema(2, 2) });
r.onMutantTested({ id: '1', fileName: `${cwd}/src/a.ts`, location: schema(1, 3) });
if (process.env.HARNESS_RANGED) {
  // Each range's last line and the adjacent range's first line.
  for (const [id, first, last] of [['4', 7, 8], ['5', 2, 2], ['6', 3, 3], ['7', 9, 9]]) {
    r.onMutantTested({ id, fileName: `${cwd}/src/r.ts`, location: schema(first, last) });
  }
}
r.onMutationTestReportReady({ schemaVersion: '2', files: { 'src/a.ts': { mutants: [] } } }, {});
"""

    def run_harness(
        self, env: dict[str, str], extra: dict[str, str] | None = None
    ) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        proc = subprocess.run(
            [
                'node',
                '--input-type=module',
                '-e',
                self.HARNESS,
                str(SCRIPTS / 'stryker-ci-reporter.mjs'),
            ],
            cwd=tmp,
            env={**os.environ, **{k: str(tmp / v) for k, v in env.items()}, **(extra or {})},
            capture_output=True,
            text=True,
            check=False,
        )
        return proc, tmp

    def test_the_plan_pass_writes_every_mutated_file_and_exits_before_any_mutant_runs(self):
        proc, tmp = self.run_harness(
            {
                'CI_STRYKER_PLAN': 'plan.json',
                'CI_STRYKER_TIMES': 'times',
                'CI_STRYKER_RESULT': 'result.json',
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads((tmp / 'plan.json').read_text())
        self.assertEqual((plan['workers'], plan['runners'], plan['ranged']), (4, 4, False))
        self.assertGreaterEqual(plan['setup_ms'], 0)
        self.assertEqual(
            plan['files'],
            {
                'src/a.ts': {
                    'run': 2,
                    'early': 1,
                    'net_ms': 150,
                    'mutants': [[1, 3, 120], [5, 5, 30], [2, 2, None]],
                    # Schema locations, as the shards' reports carry them.
                    'ids': [
                        [1, 3, 3, 10, 'BlockStatement', None, None],
                        [5, 3, 5, 10, 'StringLiteral', None, None],
                        [2, 3, 2, 10, 'StringLiteral', 'Ignored', 'Ignored using a comment'],
                    ],
                },
                'src/z.ts': {'run': 0, 'early': 0, 'net_ms': 0, 'mutants': [], 'ids': []},
            },
        )
        self.assertFalse((tmp / 'times').exists())
        self.assertFalse((tmp / 'result.json').exists())

    def test_with_checkers_stryker_runs_tests_on_half_the_workers(self):
        for concurrency, runners in ((4, 2), (5, 2), (1, 1)):
            with self.subTest(concurrency=concurrency):
                proc, tmp = self.run_harness(
                    {'CI_STRYKER_PLAN': 'plan.json'},
                    {
                        'HARNESS_OPTIONS': json.dumps(
                            {'concurrency': concurrency, 'checkers': ['typescript']}
                        )
                    },
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                plan = json.loads((tmp / 'plan.json').read_text())
                self.assertEqual((plan['workers'], plan['runners']), (concurrency, runners))

    def test_a_ranged_mutate_entry_is_marked(self):
        proc, tmp = self.run_harness({'CI_STRYKER_PLAN': 'plan.json'}, {'HARNESS_RANGED': '1'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads((tmp / 'plan.json').read_text())
        self.assertTrue(plan['ranged'] is True)
        self.assertIn('src/r.ts', plan['files'])

    def test_a_ranged_shard_records_each_finish_under_the_range_holding_it(self):
        proc, tmp = self.run_harness(
            {'CI_STRYKER_TIMES': 'times', 'CI_STRYKER_RESULT': 'result.json'},
            {'HARNESS_RANGED': '1'},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [r.split('\t')[1] for r in (tmp / 'times').read_text().splitlines()[3:-1]]
        self.assertEqual(
            rows, ['src/a.ts', 'src/r.ts:6-9', 'src/r.ts:1-2', 'src/r.ts:3-5', 'src/r.ts:6-9']
        )
        self.assertEqual(
            sorted(ss.costs(tmp / 'times')['files']),
            ['src/a.ts', 'src/r.ts:1-2', 'src/r.ts:3-5', 'src/r.ts:6-9'],
        )

    def test_a_run_records_each_planned_mutant_s_finish_its_report_and_its_end(self):
        proc, tmp = self.run_harness(
            {'CI_STRYKER_TIMES': 'times', 'CI_STRYKER_RESULT': 'result.json'}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = (tmp / 'times').read_text().splitlines()
        self.assertEqual([r.split('\t')[0] for r in rows[:3]], ['#workers', '#launch', '#start'])
        self.assertEqual(rows[0], '#workers\t4')
        self.assertEqual(rows[3].split('\t')[1], 'src/a.ts')
        self.assertEqual(rows[4:], ['#done'])
        self.assertEqual(ss.costs(tmp / 'times')['files']['src/a.ts']['mutants'], 1)
        self.assertEqual(
            json.loads((tmp / 'result.json').read_text()),
            {'schemaVersion': '2', 'files': {'src/a.ts': {'mutants': []}}},
        )


class Wiring(unittest.TestCase):
    jobs = yaml.safe_load(REPO_WORKFLOW.read_text())['jobs']
    weekly = yaml.safe_load(WORKFLOW.read_text())['jobs']

    def step(self, job: str, name: str) -> dict:
        return next(s for s in self.jobs[job]['steps'] if s.get('name') == name)

    def render(self, template: str, matrix: dict) -> str:
        return workflow_replay.Scope({'matrix': matrix}, {}, []).render(template)

    def tmp_dir(self) -> pathlib.Path:
        return pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_the_jobs_run_in_order_and_the_result_waits_for_all(self):
        self.assertEqual(self.jobs['plan']['needs'], 'discover')
        self.assertEqual(self.jobs['shard']['needs'], ['discover', 'plan'])
        self.assertEqual(self.jobs['run']['needs'], ['discover', 'shard'])
        self.assertEqual(self.jobs['merge']['needs'], ['discover', 'shard', 'run'])
        for reader in ('badge', 'aggregate'):
            self.assertEqual(self.jobs[reader]['needs'], ['discover', 'shard', 'merge'])
        self.assertEqual(sorted(self.jobs['result']['needs']), sorted(set(self.jobs) - {'result'}))

    def test_the_weekly_run_is_a_fan_out_of_one_call_per_repo(self):
        self.assertEqual(set(self.weekly), {'fanout', 'repo', 'summary', 'notify'})
        self.assertEqual(sorted(self.weekly['notify']['needs']), ['fanout', 'repo', 'summary'])
        repo = self.weekly['repo']
        self.assertEqual(repo['uses'], './.github/workflows/mutation-stryker-repo.yaml')
        self.assertEqual(
            (repo['strategy']['fail-fast'], repo['strategy']['max-parallel']), (False, 2)
        )
        self.assertEqual(
            repo['with'],
            {
                'repository': '${{ matrix.repo }}',
                'honour-opt-out': "${{ github.event_name == 'schedule' }}",
            },
        )
        self.assertEqual(repo['secrets'], {'CI_SCHEDULE': '${{ secrets.CI_SCHEDULE }}'})
        down = next(
            s for s in self.weekly['summary']['steps'] if 'download-artifact' in s.get('uses', '')
        )
        self.assertEqual(
            down['with'], {'path': 'results', 'pattern': 'strykerresult-*', 'merge-multiple': True}
        )

    def test_every_week_runs_in_full_with_no_carried_results(self):
        for path in (WORKFLOW, REPO_WORKFLOW):
            text = path.read_text()
            for word in ('incremental', 'carry', 'INCREMENTAL_FILE'):
                with self.subTest(path=path.name, word=word):
                    self.assertNotIn(word, text)

    def test_a_refused_file_list_fails_the_step_and_sets_no_mutate(self):
        body = self.step('run', "Read this shard's files")['run']
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'scripts').mkdir()
            (pathlib.Path(tmp) / 'scripts' / 'stryker-shard.py').write_text(
                "import sys\nsys.exit('stryker-shard: shard 2 has no file list it can pass to --mutate')\n"
            )
            out = pathlib.Path(tmp) / 'output'
            env = {'KEY': 'r~web', 'SHARD': '2', 'CAP_MINUTES': '350'}
            proc = subprocess.run(
                ['bash', '-e', '-c', body],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, **env, 'GITHUB_OUTPUT': str(out)},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(
                out.exists() and 'mutate=' in out.read_text(),
                out.read_text() if out.exists() else '',
            )
            (pathlib.Path(tmp) / 'scripts' / 'stryker-shard.py').write_text(
                "print('src/a.ts,src/b.ts')\n"
            )
            before = int(time.time())
            proc = subprocess.run(
                ['bash', '-e', '-c', body],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, **env, 'GITHUB_OUTPUT': str(out)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs = dict(line.split('=', 1) for line in out.read_text().splitlines())
            self.assertEqual(outputs['mutate'], 'src/a.ts,src/b.ts')
            self.assertTrue(
                before + 325 * 60 <= int(outputs['deadline']) <= int(time.time()) + 325 * 60,
                outputs,
            )

    def test_the_shard_s_budget_is_reserved_on_the_job_clock_before_the_install(self):
        steps = [s.get('name') for s in self.jobs['run']['steps']]
        self.assertLess(
            steps.index("Read this shard's files"), steps.index('Clone target repo at pinned SHA')
        )
        run = self.step('run', 'Run stryker')
        self.assertEqual(run['env']['RUN_DEADLINE'], '${{ steps.shard.outputs.deadline }}')
        self.assertNotIn('BUDGET_SECS', REPO_WORKFLOW.read_text())
        self.assertEqual(
            self.step('run', "Read this shard's files")['env']['CAP_MINUTES'],
            '${{ needs.discover.outputs.run_timeout_minutes }}',
        )
        self.assertEqual(
            self.jobs['run']['timeout-minutes'],
            '${{ fromJSON(needs.discover.outputs.run_timeout_minutes) }}',
        )

    def discover(
        self, tree: list[str], *, dir_: str = '', opt_out: bool = True, files: dict | None = None
    ) -> tuple[int, dict, str]:
        """Replay the discover job's steps on a stub clone holding TREE's paths and FILES' contents."""
        tmp = self.tmp_dir()
        (tmp / 'scripts').symlink_to(SCRIPTS)
        (tmp / 'bin').mkdir()
        (tmp / 'tree').write_text(''.join(f'{c}\n' for c in tree))
        for rel, text in (files or {}).items():
            (tmp / 'contents' / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / 'contents' / rel).write_text(text)
        (tmp / 'bin' / 'git').write_text(
            '#!/bin/bash\n'
            'if [ "$1" = clone ]; then mkdir -p scan; while IFS= read -r c; do [ -n "$c" ] || continue\n'
            '  mkdir -p "scan/$(dirname "$c")"; echo "{}" > "scan/$c"; done < tree\n'
            '  [ -d contents ] && cp -R contents/. scan/; exit 0; fi\n'
            f'echo {SHA}\n'
        )
        (tmp / 'bin' / 'git').chmod(0o755)
        out = tmp / 'output'
        env = {
            **os.environ,
            'PATH': f'{tmp / "bin"}:{os.environ["PATH"]}',
            'REPO': 'a',
            'DIR': dir_,
            'OPT_OUT': 'true' if opt_out else 'false',
            'GITHUB_OUTPUT': str(out),
        }
        log = ''
        for name in ('Key this repository and dir', 'Find the stryker configs'):
            proc = subprocess.run(
                ['bash', '-e', '-c', self.step('discover', name)['run']],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            log += proc.stdout + proc.stderr
            if proc.returncode:
                break
        outputs = (
            dict(line.split('=', 1) for line in out.read_text().splitlines())
            if out.exists()
            else {}
        )
        return proc.returncode, outputs, log

    def test_discover_gives_each_layout_its_own_key_and_runs_a_dir_once(self):
        rc, outputs, log = self.discover(
            [
                'b/c/stryker.config.json',
                'b_c/stryker.config.json',
                'stryker.config.json',
                'root/stryker.config.json',
                'w/stryker.config.json',
                'w/stryker.config.mjs',
                'node_modules/x/stryker.config.json',
                'd1/d2/d3/d4/stryker.config.json',
                'a b/stryker.config.json',
            ]
        )
        self.assertEqual(rc, 0, log[-1000:])
        got = [(e['dir'], e['key']) for e in json.loads(outputs['entries'])]
        self.assertEqual(
            got,
            [('.', 'a~'), ('b/c', 'a~b=c'), ('b_c', 'a~b_c'), ('root', 'a~root'), ('w', 'a~w')],
        )
        self.assertEqual(outputs['key'], 'a')
        self.assertEqual(
            outputs['plans'],
            '{strykerplan-a~,strykerplan-a~b=c,strykerplan-a~b_c,strykerplan-a~root,strykerplan-a~w}',
        )
        self.assertIn("::error::a: directory 'a b' is not a relative path", log)
        self.assertEqual(
            json.loads(outputs['refused']),
            [
                {
                    'dir': 'a b',
                    'reason': "directory 'a b' is not a relative path of [A-Za-z0-9._-] names",
                }
            ],
        )
        _, merge = ss.matrix(
            json.loads(outputs['entries']), self.tmp_dir(), self.tmp_dir(), self.tmp_dir()
        )
        self.assertEqual([m['key'] for m in merge], [k for _, k in got])

    def test_discover_honours_the_opt_out_only_when_asked_and_tests_one_dir_when_given(self):
        tree = ['x/stryker.config.json', 'y/stryker.config.json', 'z/stryker.config.json']
        files = {
            'x/stryker.weekly.json': '{"weekly": false}',
            'z/stryker.weekly.json': '{"weekly": 0}',
        }
        rc, outputs, log = self.discover(tree, files=files)
        self.assertEqual(rc, 0, log)
        self.assertEqual([e['dir'] for e in json.loads(outputs['entries'])], ['y', 'z'])
        self.assertIn('::notice::a:x: stryker.weekly.json sets weekly=false', log)
        self.assertIn('::warning::a:z: stryker.weekly.json is not exactly', log)
        rc, outputs, _ = self.discover(tree, files=files, opt_out=False)
        self.assertEqual([e['dir'] for e in json.loads(outputs['entries'])], ['x', 'y', 'z'])
        rc, outputs, _ = self.discover(tree, dir_='y')
        self.assertEqual(
            (rc, outputs['key'], [e['dir'] for e in json.loads(outputs['entries'])]),
            (0, 'a~y', ['y']),
        )
        self.assertEqual(outputs['plans'], 'strykerplan-a~y')
        rc, _, log = self.discover(tree, dir_='nope')
        self.assertNotEqual(rc, 0)
        self.assertIn('has no stryker config to test', log)

    def test_discover_fails_past_max_shards_dirs_and_reports_none(self):
        many = [f'p{i}/stryker.config.json' for i in range(ss.mutation_cost.MAX_SHARDS + 1)]
        rc, _, log = self.discover(many)
        self.assertNotEqual(rc, 0)
        self.assertIn(f'more than the {ss.mutation_cost.MAX_SHARDS} one call shards', log)
        rc, outputs, log = self.discover([])
        self.assertEqual((rc, outputs.get('entries')), (0, '[]'), log)
        self.assertIn('::notice::a: no stryker entry to test this week', log)
        self.assertEqual(self.jobs['plan']['if'], "${{ needs.discover.outputs.entries != '[]' }}")

    def test_the_weekly_fan_out_finds_repos_by_their_tree_and_runs_an_unreadable_one(self):
        tmp = self.tmp_dir()
        (tmp / 'bin').mkdir()
        (tmp / 'bin' / 'gh').write_text(
            '#!/bin/bash\n'
            'case "$*" in *user/repos*) printf "a\\nb\\nc\\nd\\ne\\n" ;;\n'
            '  */a/git/*) echo 1 ;; */b/git/*) echo 0 ;; */c/git/*) exit 1 ;;\n'
            '  */d/git/*) echo truncated ;; */e/git/*) echo 0 ;; esac\n'
        )
        (tmp / 'bin' / 'gh').chmod(0o755)
        out, summary = tmp / 'output', tmp / 'summary'
        body = next(s['run'] for s in self.weekly['fanout']['steps'] if s.get('id') == 'fanout')
        env = {
            **os.environ,
            'PATH': f'{tmp / "bin"}:{os.environ["PATH"]}',
            'ONLY': '',
            'GITHUB_OUTPUT': str(out),
            'GITHUB_STEP_SUMMARY': str(summary),
        }
        proc = subprocess.run(
            ['bash', '-c', body], cwd=tmp, capture_output=True, text=True, check=False, env=env
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        outputs = dict(line.split('=', 1) for line in out.read_text().splitlines())
        self.assertEqual(json.loads(outputs['repos']), ['a', 'c', 'd'])
        self.assertIn('::warning::c: its source tree could not be read', proc.stdout)
        self.assertIn('::warning::d: its source tree is truncated', proc.stdout)
        out.unlink()
        proc = subprocess.run(
            ['bash', '-c', body],
            cwd=tmp,
            capture_output=True,
            text=True,
            check=False,
            env={**env, 'ONLY': 'b e'},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('::error::no repo with a stryker config was found to run', proc.stdout)
        self.assertIn('### no repo with a stryker config was found', summary.read_text())

    def test_the_fan_out_filter_counts_configs_as_discover_does(self):
        body = next(s['run'] for s in self.weekly['fanout']['steps'] if s.get('id') == 'fanout')
        start = body.index("--jq '", body.index('git/trees/HEAD')) + 6
        program = body[start : body.index("' 2>/dev/null", start)]
        tree = {
            'truncated': False,
            'tree': [
                {'type': 'blob', 'path': p}
                for p in (
                    'stryker.config.json',
                    'a/b/c/stryker.config.mjs',
                    'a/b/c/d/stryker.config.json',
                    'node_modules/x/stryker.config.json',
                    'x/stryker.configs',
                    'x/stryker.config.json/y',
                )
            ],
        }
        got = subprocess.run(
            ['jq', '-r', program],
            input=json.dumps(tree),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(got.stdout.strip(), '2')

    def test_only_the_shard_job_fetches_an_earlier_run_s_artifact(self):
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                perms = job.get('permissions', {})
                self.assertEqual(perms.get('actions') == 'read', name == 'shard')
        steps = [s.get('uses', '') for s in self.jobs['shard']['steps']]
        self.assertFalse(any(u.startswith('actions/setup-node') for u in steps))

    def test_only_the_badge_and_aggregate_jobs_see_the_schedule_token(self):
        holders = sorted(j for j, d in self.jobs.items() if 'CI_SCHEDULE' in json.dumps(d))
        self.assertEqual(holders, ['aggregate', 'badge'])
        on = yaml.safe_load(REPO_WORKFLOW.read_text())[True]['workflow_call']
        self.assertEqual(list(on['secrets']), ['CI_SCHEDULE'])
        self.assertFalse(on['secrets']['CI_SCHEDULE']['required'])

    def test_every_job_checks_out_the_called_workflow_s_own_ci(self):
        for name, job in self.jobs.items():
            for st in job.get('steps', []):
                if st.get('uses', '').startswith('actions/checkout'):
                    with self.subTest(job=name):
                        self.assertEqual(
                            (st['with'].get('repository'), st['with'].get('ref')),
                            ('${{ job.workflow_repository }}', '${{ job.workflow_sha }}'),
                        )

    def test_the_merge_downloads_exactly_the_names_each_shard_uploads(self):
        upload = self.step('run', "Upload this shard's result")['with']['name']
        one = entry('a-b', 'x/y')
        with tempfile.TemporaryDirectory() as tmp:
            plans = pathlib.Path(tmp) / 'plans'
            plans.mkdir()
            doc = ss.sizing(
                'a-b',
                'x/y',
                SHA,
                {},
                plan_pass({f'src/f{i}.ts': (100, 0, 14_400_000) for i in range(6)}),
            )
            (plans / 'a-b~x=y.json').write_text(json.dumps(doc))
            run, merge = ss.matrix(
                [one], plans, pathlib.Path(tmp) / 'o', pathlib.Path(tmp) / 'history'
            )
        k = merge[0]['shards']
        names = {self.render(upload, r) for r in run}
        self.assertEqual(names, set(merge[0]['artifacts'][1:-1].split(',')))
        self.assertEqual(len(names), k)
        download = self.step('merge', "Download this entry's shards")['with']
        self.assertEqual(download, {'path': 'shards', 'pattern': '${{ matrix.artifacts }}'})
        up = self.step('plan', 'Upload the plan')['with']
        self.assertEqual(
            (up['name'], up['path']),
            ('strykerplan-${{ matrix.key }}', 'plan/${{ matrix.key }}.json'),
        )
        self.assertEqual(
            self.step('shard', 'Download the plans')['with'],
            {
                'path': 'plans',
                'pattern': '${{ needs.discover.outputs.plans }}',
                'merge-multiple': True,
            },
        )

    def test_no_ci_owned_artifact_shares_the_report_family_s_names(self):
        uploads = [
            (job, s['with']['name'])
            for job, j in self.jobs.items()
            for s in j.get('steps', [])
            if s.get('uses', '').startswith('actions/upload-artifact')
        ]
        self.assertIn(('merge', 'stryker-${{ matrix.key }}'), uploads)
        for job, name in uploads:
            if (job, name) != ('merge', 'stryker-${{ matrix.key }}'):
                with self.subTest(name=name):
                    self.assertFalse(name.startswith('stryker-'))
                    self.assertTrue(name.startswith('stryker'))

    def test_each_entry_s_costs_are_kept_90_days_and_read_back_on_the_run_s_own_ref(self):
        fetch = self.step('shard', "Fetch each entry's cost history")
        self.assertEqual(fetch['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertEqual(fetch['env']['REF'], '${{ github.ref_name }}')
        self.assertEqual((fetch['timeout-minutes'], fetch['continue-on-error']), (4, True))
        work = self.tmp_dir()
        (work / 'scripts').mkdir()
        (work / 'scripts' / 'mutation-history.sh').write_text(
            'printf \'%s \' "$@" >>calls\necho >>calls\n[ "$1" = strykercosts-a~ ] && exit 3\n'
            'mkdir -p "$3" && echo "{}" >"$3/$4"\n'
        )
        entries = [entry('a', '.'), entry('b-c', 'web/x')]
        proc = subprocess.run(
            ['bash', '-c', fetch['run']],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, 'ENTRIES': json.dumps(entries), 'REF': 'main'},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            (work / 'calls').read_text().splitlines(),
            [
                'strykercosts-a~ main history/a~ costs.json ',
                'strykercosts-b-c~web=x main history/b-c~web=x costs.json ',
            ],
        )
        self.assertEqual(
            sorted(p.name for p in (work / 'history').iterdir() if p.is_file()),
            ['b-c~web=x.json'],
        )
        self.assertIn(
            '--history history', self.step('shard', 'Build the run and merge matrices')['run']
        )
        merge = self.step('merge', 'Merge the shards')['run']
        self.assertIn('--stage stage --costs costs', merge)
        up = self.step('merge', "Upload this entry's cost history (90-day retention)")
        self.assertEqual(
            up['with'],
            {
                'name': 'strykercosts-${{ matrix.key }}',
                'path': 'costs/costs.json',
                'if-no-files-found': 'error',
                'retention-days': 90,
            },
        )
        self.assertEqual(up['if'], "${{ !cancelled() && hashFiles('costs/costs.json') != '' }}")

    def test_the_shard_step_builds_both_matrices_and_the_report_names(self):
        body = self.step('shard', 'Build the run and merge matrices')['run']
        work = self.tmp_dir()
        (work / 'scripts').symlink_to(SCRIPTS)
        out = work / 'output'
        entries = [ENTRY, entry('r', '.')]
        proc = subprocess.run(
            ['bash', '-c', body],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                'ENTRIES': json.dumps(entries),
                'GITHUB_OUTPUT': str(out),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-500:] + proc.stderr[-500:])
        outputs = dict(line.split('=', 1) for line in out.read_text().splitlines())
        self.assertEqual([r['key'] for r in json.loads(outputs['run'])], ['r~web', 'r~'])
        self.assertEqual(outputs['reports'], '{stryker-r~web,stryker-r~}')
        self.assertIn('::warning::r:web: no plan, so it runs in one job', proc.stderr)
        self.assertEqual(
            self.step('aggregate', "Download this repo's reports")['with']['pattern'],
            '${{ needs.shard.outputs.reports }}',
        )

    def test_a_shard_stages_the_report_its_ci_reporter_wrote(self):
        run = self.step('run', 'Run stryker')['run']
        self.assertIn('CI_STRYKER_RESULT="${RUNNER_TEMP}/result.json"', run)
        stage = self.step('run', "Stage this shard's result")['run']
        self.assertIn('cp "${RUNNER_TEMP}/result.json" /tmp/stage/result.json', stage)

    def test_a_one_dir_call_publishes_no_badge_or_tracker(self):
        for job in ('badge', 'aggregate'):
            with self.subTest(job=job):
                self.assertIn("inputs.dir == ''", self.jobs[job]['if'])


if __name__ == '__main__':
    unittest.main()


class RecordedRun(unittest.TestCase):
    """Run 43328238671 (marotte static-src, 7 shards) replayed: its plan pass's sizing and the cost
    each of its units measured, spread over the unit's mutants by their prior."""

    FIXTURE: ClassVar[dict] = json.loads(
        gzip.open(SCRIPTS / 'testdata' / 'mutation-cost' / 'marotte-43328238671.json.gz').read()
    )

    def actual(self) -> dict[str, list[float]]:
        """Each file's measured worker-seconds per mutant, in its spans' order."""
        sizing, costs = self.FIXTURE['sizing'], self.FIXTURE['costs']
        out = {}
        for path, f in sizing['files'].items():
            spans, got = f['spans'], [0.0] * len(f['spans'])
            for unit, e in costs['files'].items():
                if ss.file_of(unit) != path:
                    continue
                held = [i for i, s in enumerate(spans) if holds(unit, s) and s[2]]
                total = sum(spans[i][2] for i in held)
                for i in held:
                    got[i] += e['secs'] * spans[i][2] / total
            out[path] = got
        return out

    def slowest(self, history: dict | None) -> tuple[dict, list[float]]:
        """The plan from HISTORY, and each of its shards' measured minutes on a SLOWDOWN-times slower runner."""
        sizing, costs = self.FIXTURE['sizing'], self.FIXTURE['costs']
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO):
            plan, _ = ss.assign(sizing, history)
        files = plan['files'] or {p: {'shard': 1} for p in sizing['files']}
        per = self.actual()
        jobs = [costs['setup_secs']] * plan['shards']
        for unit, f in files.items():
            path = ss.file_of(unit)
            spans = sizing['files'][path]['spans']
            jobs[f['shard'] - 1] += (
                sum(c for s, c in zip(spans, per[path], strict=True) if holds(unit, s))
                / sizing['workers']
            )
        return plan, [j / 60 * ss.mutation_cost.SLOWDOWN for j in jobs]

    def test_every_measured_unit_is_one_the_plan_pass_found(self):
        units = self.FIXTURE['costs']['files']
        self.assertEqual(
            {ss.file_of(u) for u in units}, set(self.FIXTURE['sizing']['files']), sorted(units)
        )
        for path, got in self.actual().items():
            want = sum(e['secs'] for u, e in units.items() if ss.file_of(u) == path)
            self.assertAlmostEqual(sum(got), want, places=3, msg=path)

    def test_a_first_week_entry_ends_every_shard_by_the_safe_limit_on_the_slowest_runner(self):
        limit = ss.mutation_cost.SAFE_JOB_MINUTES
        for name, history in (
            ('no history', None),
            ('the partial sample this run planned from', self.FIXTURE['planned_from']),
        ):
            with self.subTest(history=name):
                plan, jobs = self.slowest(history)
                self.assertGreater(plan['shards'], 1)
                self.assertLessEqual(max(jobs), limit, [round(j, 1) for j in jobs])

    def test_the_run_s_own_history_plans_shards_within_the_bar(self):
        costs = self.FIXTURE['costs']
        plan, jobs = self.slowest({'files': costs['files'], 'setup_secs': costs['setup_secs']})
        self.assertLessEqual(max(jobs), ss.mutation_cost.SAFE_JOB_MINUTES)
        measured = [j / ss.mutation_cost.SLOWDOWN for j in jobs]
        self.assertEqual(
            ss.mutation_cost.bar_faults(plan['projected_minutes'], [measured]), [], measured
        )


class Counts(unittest.TestCase):
    """count_rows judges a later run's statuses per file, only where the file's blob is unchanged."""

    @staticmethod
    def report(**files: dict[str, int]) -> dict:
        return {
            'files': {
                p: {'mutants': [{'status': s} for s, n in c.items() for _ in range(n)]}
                for p, c in files.items()
            }
        }

    def test_each_file_is_matched_differs_or_skipped_by_its_blob(self):
        a, b, c = 'a' * 40, 'b' * 40, 'c' * 40
        rows = [
            # file, baseline, proof, baseline blob, proof blob, verdict
            (
                'same',
                {'Killed': 435, 'Survived': 69},
                {'Killed': 437, 'Survived': 67},
                a,
                a,
                'match',
            ),
            (
                'moved',
                {'Killed': 400, 'Survived': 100},
                {'Killed': 480, 'Survived': 20},
                a,
                a,
                'differs',
            ),
            ('grown', {'Killed': 10}, {'Killed': 11}, a, a, 'differs'),
            ('small', {'Killed': 5, 'Timeout': 2}, {'Killed': 7}, a, a, 'match'),
            ('edited', {'Killed': 10}, {'Killed': 90}, a, b, 'skipped'),
            ('unknown', {'Killed': 10}, {'Killed': 90}, None, c, 'skipped'),
            ('deleted', {'Killed': 10}, None, a, None, 'skipped'),
            ('added', None, {'Killed': 10}, None, a, 'skipped'),
        ]
        old = self.report(**{f: x for f, x, *_ in rows if x})
        new = self.report(**{f: y for f, _, y, *_ in rows if y})
        before = {f: p for f, _, _, p, _, _ in rows if p}
        after = {f: q for f, _, _, _, q, _ in rows if q}
        got = {r['file']: r['verdict'] for r in ss.count_rows(old, new, before, after)}
        self.assertEqual(got, {f: v for f, *_, v in rows})

    def test_the_cli_exits_1_only_when_an_unchanged_file_differs(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        blobs = {'x.ts': 'a' * 40}
        for name, doc in (
            ('base.json', self.report(**{'x.ts': {'Killed': 10}})),
            ('ok.json', self.report(**{'x.ts': {'Killed': 9, 'Survived': 1}})),
            ('bad.json', self.report(**{'x.ts': {'Survived': 10}})),
            ('blobs.json', blobs),
        ):
            (tmp / name).write_text(json.dumps(doc))
        for proof, rc in (('ok.json', 0), ('bad.json', 1)):
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SHARD),
                    'counts',
                    *('--baseline', str(tmp / 'base.json'), '--proof', str(tmp / proof)),
                    *('--baseline-blobs', str(tmp / 'blobs.json')),
                    *('--proof-blobs', str(tmp / 'blobs.json')),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(proof=proof):
                self.assertEqual(proc.returncode, rc, proc.stderr)
