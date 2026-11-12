"""stryker-shard.py's discover, plan, matrix, merge and costs, the ci reporter, and their wiring in
mutation-stryker-repo.yaml and weekly-stryker.yaml."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
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
        files = {f'src/f{i}.ts': (100, 0, 3_600_000) for i in range(6)}
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
        target: float = 60,
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
        return ss.matrix(list(entries), plans, target, self.tmp / 'out', hist)

    def big(self) -> dict:
        files = {f'src/f{i}.ts': (100, 0, 3_600_000) for i in range(6)}
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
        # Each big() file's probe is 36.96 s per run mutant.
        hist = {
            'files': {
                'src/f0.ts': {'secs': 3000.0, 'mutants': 100},
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
        self.assertEqual(plan['history']['files']['src/f0.ts'], {'secs': 3000.0, 'mutants': 100})
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
        files = {f'src/f{i}.ts': (100, 0, 3_600_000) for i in range(300)}
        doc = ss.sizing('r', 'web', SHA, {}, plan_pass(files))
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            run, _ = self.matrix({'r~web': doc}, target=200)
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
                plan_pass({f'src/f{n}.ts': (100, 0, 3_600_000) for n in range(300)}),
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
                    '--target-minutes',
                    '200',
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
                plan_pass({f'src/f{n}.ts': (100, 1, 3_600_000) for n in range(6)}),
            )
        ).encode()
        self.history = json.dumps(
            {'files': {'src/f0.ts': {'secs': 3000.0, 'mutants': 100}}, 'setup_secs': 100.0}
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
            run, merge = ss.matrix(
                [self.VICTIM], root / 'plans', 60, root / 'out', root / 'history'
            )
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
            'capped': 'a cap of 16 shards',
        }
        with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(self.merge(plan=capped), 0)
        top = f'{ss.mutation_cost.TOP_MINUTES:.1f}'
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
            self.history(), {'files': self.PLAN['history']['files'], 'setup_secs': 40.0}
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
const r = new R(options, { [`${cwd}/src/a.ts`]: { mutate: true }, [`${cwd}/src/z.ts`]: { mutate: true },
  [`${cwd}/src/skip.ts`]: { mutate: false },
  ...(process.env.HARNESS_RANGED ? { [`${cwd}/src/r.ts`]: { mutate: [{ start: { line: 1, column: 0 }, end: { line: 2, column: 0 } }] } } : {}) });
r.onMutationTestingPlanReady({ mutantPlans: [
  { plan: 'Run', netTime: 120, mutant: { id: '1', fileName: `${cwd}/src/a.ts` } },
  { plan: 'Run', netTime: 30, mutant: { id: '3', fileName: `${cwd}/src/a.ts` } },
  { plan: 'EarlyResult', mutant: { id: '2', fileName: `${cwd}/src/a.ts`, status: 'Ignored' } }] });
r.onMutantTested({ id: '2', fileName: `${cwd}/src/a.ts` });
r.onMutantTested({ id: '1', fileName: `${cwd}/src/a.ts` });
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
                'src/a.ts': {'run': 2, 'early': 1, 'net_ms': 150},
                'src/z.ts': {'run': 0, 'early': 0, 'net_ms': 0},
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
            json.loads(outputs['entries']), self.tmp_dir(), 200, self.tmp_dir(), self.tmp_dir()
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
                plan_pass({f'src/f{i}.ts': (100, 0, 3_600_000) for i in range(6)}),
            )
            (plans / 'a-b~x=y.json').write_text(json.dumps(doc))
            run, merge = ss.matrix(
                [one], plans, 60, pathlib.Path(tmp) / 'o', pathlib.Path(tmp) / 'history'
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
                'TARGET_MINUTES': '200',
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
