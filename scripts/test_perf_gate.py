"""perf_gate.py: the measure plan, the results merge, and the pull request guard's
compare and verdict of a red cell."""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import perf_gate

RUNNER = 'ubuntu-26.04'
HEAD = 'c0ffee'
CELL = {
    'scenario': 'files',
    'scale': 'x10',
    'engine': 'chromium',
    'width': 'desktop',
    'window': 'cold',
}
DOM = 'files/x10/chromium/desktop/cold/dom'
CPU = 'files/x10/chromium/desktop/cold/cpu_ms'
# The artifact prefix of a repository cplieger/a-measure-x, whose shards a
# `perf-results-cplieger-a-measure-*` download pattern also matches.
OTHER = 'perf-results-cplieger-a-measure-x'


def cell(metric='cpu_ms', reps=(1000.0, 1000.0, 1000.0), kind='time', **kw):
    return {**CELL, 'metric': metric, 'kind': kind, 'reps': list(reps), **kw}


def results(cells, runner=RUNNER):
    return {'schema': 1, 'runner': runner, 'cpu_model': 'AMD EPYC 7763', 'cells': cells}


def keyof(d):
    return perf_gate.key(d)


def trend_file(entries, runner=RUNNER):
    """The perf-trend branch's series/dev/baseline.json holding entries."""
    return {'schema': 1, 'series': 'dev', 'runner': runner, 'commits': [HEAD], 'cells': entries}


def baselined(metric='cpu_ms', baseline=1000.0, **kw):
    return {**CELL, 'metric': metric, 'baseline': baseline, **kw}


def guarded(entries, cells, runner=RUNNER, units=None, windows=None):
    """guard() of cells, measured on runner, against the trend baselines of entries."""
    base_runner, baselines = perf_gate.baselines_from_trend(trend_file(entries))
    return perf_gate.guard(
        baselines, base_runner, results(cells, runner=runner), units or {}, windows=windows or {}
    )


def decided(out, entries):
    """compare's remeasure.json for out: each red cell with its trend baseline."""
    _, baselines = perf_gate.baselines_from_trend(trend_file(entries))
    return {'cells': [{**c, 'baseline': baselines[keyof(c)]} for c in out.remeasure]}


class Guard(unittest.TestCase):
    """The pull request guard: a timing cell fails only far past dev's trend baseline."""

    def test_the_quadratic_scan_regression_is_red_and_fails_on_six_reps(self):
        entries = [baselined(baseline=3000.0)]
        first = [cell(reps=(9700.0, 9800.0, 9900.0))]
        out = guarded(entries, first)
        self.assertEqual(out.failures, [])
        self.assertEqual([keyof(c) for c in out.remeasure], [keyof(entries[0])])
        again = results([cell(reps=(9750.0, 9850.0, 9800.0))])
        verdict = perf_gate.guard_verdict(decided(out, entries), results(first), again)
        self.assertEqual(len(verdict.failures), 1)
        self.assertIn(
            f'{CPU}: median of six 9800.0 is 6800 over its baseline 3000.0 (3.27x), past the '
            'guard of 2.0x and 250',
            verdict.failures[0],
        )

    def test_runner_noise_of_half_again_passes(self):
        out = guarded([baselined(baseline=3000.0)], [cell(reps=(4600.0, 4620.0, 4650.0))])
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertEqual([r['status'] for r in out.rows], ['ok'])

    def test_the_floor_keeps_a_tiny_cell_from_failing(self):
        for metric, baseline, value, red in (
            ('cpu_ms', 9.0, 18.0, False),
            ('cpu_ms', 9.0, 200.0, False),
            ('cpu_ms', 9.0, 259.0, False),
            ('cpu_ms', 9.0, 260.0, True),
            ('server_cpu_ms:GET /api/chats', 6.0, 12.0, False),
            ('server_cpu_ms:GET /api/chats', 6.0, 15.5, False),
            ('server_cpu_ms:GET /api/chats', 6.0, 16.0, False),
            ('server_cpu_ms:GET /api/chats', 22.0, 46.0, True),
            ('server_cpu_ms:GET /api/chats', 0.0, 9.0, False),
        ):
            with self.subTest(metric=metric, baseline=baseline, value=value):
                kind = 'server_cpu' if metric.startswith('server') else 'time'
                out = guarded([baselined(metric, baseline)], [cell(metric, (value,) * 3, kind)])
                self.assertEqual(bool(out.remeasure), red)
                self.assertEqual(out.failures, [])

    def test_memory_fails_at_a_quarter_more_past_its_floor(self):
        for metric, baseline, value, red in (
            ('heap_mb', 40.0, 52.0, True),
            ('heap_mb', 40.0, 48.0, False),
            ('heap_mb', 4.0, 5.5, False),
            ('rss_mb', 200.0, 260.0, True),
            ('rss_mb', 120.0, 165.0, False),
        ):
            with self.subTest(metric=metric, baseline=baseline, value=value):
                out = guarded([baselined(metric, baseline)], [cell(metric, (value,) * 3)])
                self.assertEqual(bool(out.remeasure), red)

    def test_a_count_off_dev_never_fails_and_is_listed_as_information(self):
        entries = [baselined(), baselined('dom', 640)]
        out = guarded(entries, [cell(), cell('dom', (700,) * 3, kind='count')])
        self.assertEqual((out.failures, out.remeasure), ([], []))
        text = perf_gate.guard_summary(out)
        counts = text.split('### Counts that differ from dev (information)', 1)[1]
        self.assertIn(f'| {DOM} | 640 | 700 |', counts)
        self.assertNotIn(CPU, counts)

    def test_a_report_kind_and_an_unknown_metric_are_never_guarded(self):
        entries = [baselined(), baselined('long_task_ms', 50.0), {**baselined(), 'window': 'live'}]
        cells = [
            cell(),
            cell('long_task_ms', (900.0,) * 3, kind='report'),
            {**cell(reps=(900.0,) * 3, kind='report'), 'window': 'live'},
        ]
        out = guarded(entries, cells)
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertEqual([r['status'] for r in out.rows], ['ok', 'reported', 'reported'])

    def test_a_trend_baseline_file_reads_each_cell_s_baseline_and_nothing_else(self):
        one = {**baselined(), 'kind': 'time', 'extra': ['C1', 'X2']}
        two = {**baselined('dom', 640), 'kind': 'count'}
        doc = {**trend_file([one, two]), 'commits': ['a', 'b', 'c'], 'more': 1}
        runner, baselines = perf_gate.baselines_from_trend(doc)
        self.assertEqual(runner, RUNNER)
        self.assertEqual(baselines, {keyof(one): 1000.0, keyof(two): 640})

    def test_a_trend_cell_with_no_numeric_baseline_holds_no_baseline(self):
        doc = trend_file(
            [
                {**baselined(), 'baseline': 'fast'},
                {**baselined(), 'window': 'live', 'baseline': None},
            ]
        )
        self.assertEqual(perf_gate.baselines_from_trend(doc)[1], {})
        for broken in (
            {'cells': {}},
            {'cells': ['files']},
            {'cells': [{'metric': 'cpu_ms'}]},
        ):
            with self.subTest(broken=broken), self.assertRaises((TypeError, ValueError)):
                perf_gate.baselines_from_trend(broken)

    def test_a_trend_file_that_names_no_runner_is_refused(self):
        for runner in (None, '', 26.04):
            doc = trend_file([baselined()], runner=runner)
            with (
                self.subTest(runner=runner),
                self.assertRaisesRegex(
                    ValueError, 'runner must name the runner the baselines were measured on'
                ),
            ):
                perf_gate.baselines_from_trend(doc)

    def test_another_runner_reports_every_cell_and_fails_nothing(self):
        out = guarded([baselined()], [cell(reps=(9000.0,) * 3)], runner='self-hosted')
        self.assertTrue(out.runner_mismatch)
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertIn('every metric is reported and none is guarded', perf_gate.guard_summary(out))

    def test_a_guard_with_no_baseline_for_any_measured_timing_cell_fails(self):
        for entries, cells in (
            ([], [cell()]),
            ([baselined('dom', 640)], [cell('dom', (640,) * 3, kind='count')]),
            ([baselined()], []),
        ):
            with self.subTest(entries=entries, cells=cells):
                out = guarded(entries, cells)
                self.assertEqual(len(out.failures), 1, out.failures)
                self.assertIn('nothing to guard', out.failures[0])

    def test_a_planned_baselined_timing_cell_no_shard_measured_fails(self):
        units = perf_gate.planned_windows([unit('files', 60)])
        entries = [baselined(), {**baselined(), 'window': 'live'}, {**baselined(), 'scale': 'x1'}]
        entries.append({**baselined('dom', 640), 'window': 'live'})
        out = guarded(entries, [cell()], units=units)
        self.assertEqual(
            out.failures,
            [
                f"{CPU.replace('cold', 'live')}: dev's trend has a baseline for it, and no shard measured it"
            ],
        )

    def test_a_baselined_server_route_no_shard_measured_is_information(self):
        units = perf_gate.planned_windows([unit('files', 60)])
        route = 'server_cpu_ms:GET /api/runs/{run}?view=header'
        out = guarded([baselined(), baselined(route, 0.0, window='replay')], [cell()], units=units)
        self.assertEqual(out.failures, [])
        notice = (
            f"files/x10/chromium/desktop/replay/{route}: dev's trend has a baseline for this route, "
            'and no shard measured it'
        )
        self.assertEqual(out.notices, [notice])
        self.assertIn(f'### Notices\n\n- {out.notices[0]}\n', perf_gate.guard_summary(out))

    def test_a_baselined_window_the_head_plan_no_longer_lists_is_information(self):
        units = perf_gate.planned_windows([unit('files', 60)])
        live = {**baselined(), 'window': 'live'}
        entries = [baselined(), live, baselined('heap_mb', 40.0)]
        out = guarded(entries, [cell()], units=units, windows={'files': {'cold'}})
        self.assertEqual(
            out.failures,
            [
                (
                    "files/x10/chromium/desktop/cold/heap_mb: dev's trend has a baseline for it, "
                    'and no shard measured it'
                )
            ],
        )
        self.assertEqual(
            out.notices,
            [
                (
                    f"{CPU.replace('cold', 'live')}: dev's trend has a baseline for it, and the "
                    'head plan.json no longer measures window live'
                )
            ],
        )

    def test_a_timing_cell_with_no_reps_fails(self):
        out = guarded([baselined()], [cell(reps=())])
        self.assertEqual(out.failures, [f'{CPU}: measured no reps'])

    def test_a_red_cell_fails_the_verdict_only_on_its_median_of_six(self):
        entries = [baselined(baseline=1000.0)]
        for reps, more, fails in (
            ((2400.0, 2500.0, 2600.0), (1000.0,) * 3, False),
            ((2400.0, 2500.0, 2600.0), (2300.0, 2400.0, 2500.0), True),
            ((3000.0,) * 3, (1900.0,) * 3, True),
        ):
            with self.subTest(reps=reps, more=more):
                first = [cell(reps=reps)]
                out = guarded(entries, first)
                v = perf_gate.guard_verdict(
                    decided(out, entries), results(first), results([cell(reps=more)])
                )
                self.assertEqual(bool(v.failures), fails, v.failures)
                self.assertEqual([s['reps'] for s in v.six], [[*reps, *more]])

    def test_a_red_cell_with_no_re_measured_reps_fails_the_verdict(self):
        entries = [baselined()]
        first = [cell(reps=(3000.0,) * 3)]
        out = guarded(entries, first)
        for again in (results([]), results([cell(reps=())])):
            with self.subTest(again=again):
                v = perf_gate.guard_verdict(decided(out, entries), results(first), again)
                self.assertEqual(
                    v.failures, [f'{CPU}: listed for a re-measure but no reps came back']
                )

    def test_a_red_server_route_the_re_measure_did_not_request_fails_on_its_first_reps(self):
        route = 'server_cpu_ms:GET /api/chats'
        entries = [baselined(), baselined(route, 22.0, window='replay')]
        first = [cell(), cell(route, (46.0, 47.0, 48.0), 'server_cpu', window='replay')]
        out = guarded(entries, first)
        self.assertEqual(len(out.remeasure), 1)
        v = perf_gate.guard_verdict(decided(out, entries), results(first), results([cell()]))
        self.assertEqual(
            v.failures,
            [
                (
                    f'files/x10/chromium/desktop/replay/{route}: the re-measure did not request '
                    'this route, so its first three reps judge it: median 47.0 is 25 over its '
                    'baseline 22.0 (2.14x), past the guard of 2.0x and 10'
                )
            ],
        )

    def test_a_decision_without_a_numeric_baseline_is_an_error(self):
        first = results([cell(reps=(3000.0,) * 3)])
        again = results([cell(reps=(3000.0,) * 3)])
        for baseline in ({}, {'baseline': 'slow'}):
            with self.subTest(baseline=baseline), self.assertRaises(ValueError):
                perf_gate.guard_verdict({'cells': [{**cell(), **baseline}]}, first, again)

    def test_the_summary_tables_the_guarded_cells_and_the_re_measure(self):
        entries = [baselined(baseline=3000.0)]
        first = [cell(reps=(9700.0, 9800.0, 9900.0))]
        out = guarded(entries, first)
        text = perf_gate.guard_summary(out)
        self.assertIn('## Performance guard', text)
        self.assertIn(f'| {CPU} | 3000.0 | 9800.0 | 3.27 | 2.0x and 250 | remeasure |', text)
        again = results([cell(reps=(9750.0, 9850.0, 9800.0))])
        v = perf_gate.guard_verdict(decided(out, entries), results(first), again)
        text = perf_gate.guard_summary(v)
        self.assertIn('### Failures', text)
        self.assertIn(
            f'| {CPU} | 3000.0 | 9700.0, 9800.0, 9900.0, 9750.0, 9850.0, 9800.0 | 9800.0 | 3.27 | failed |',
            text,
        )


def unit(scenario, seconds, scale='x10', engine='chromium', width='desktop', **kw):
    return {
        'scenario': scenario,
        'scale': scale,
        'engine': engine,
        'width': width,
        'seconds': seconds,
        **kw,
    }


PLAN = {
    'schema': 1,
    'node': 'static-src',
    'sets': {
        'pr': {
            'shards': 2,
            'cells': [unit('run-page', 540), unit('files', 80), unit('docs', 75), unit('tabs', 70)],
        },
        'full': {'shards': 20, 'cells': [unit('files', 80), unit('files', 60, scale='x1')]},
    },
}


def shard_cells(matrix):
    return [[c['scenario'] for c in m['cells']] for m in matrix['include']]


class Plan(unittest.TestCase):
    def test_the_longest_cell_goes_first_onto_the_least_loaded_shard(self):
        matrix = perf_gate.plan(PLAN, 'pr')
        self.assertEqual(shard_cells(matrix), [['run-page'], ['files', 'docs', 'tabs']])
        self.assertEqual([m['name'] for m in matrix['include']], ['shard-1-of-2', 'shard-2-of-2'])

    def test_no_more_shards_than_cells(self):
        self.assertEqual(len(perf_gate.plan(PLAN, 'full')['include']), 2)

    def test_only_keeps_the_named_scenarios(self):
        matrix = perf_gate.plan(PLAN, 'pr', only=['docs', 'tabs'])
        self.assertEqual(shard_cells(matrix), [['docs'], ['tabs']])
        self.assertEqual(matrix['scenarios'], ['docs', 'tabs'])

    def test_an_only_naming_no_scenario_of_the_set_fails(self):
        with self.assertRaisesRegex(ValueError, 'shell'):
            perf_gate.plan(PLAN, 'pr', only=['docs', 'shell'])

    def test_a_remeasure_plans_only_the_listed_cells_once_each(self):
        cpu = {**CELL, 'scenario': 'docs', 'metric': 'cpu_ms', 'window': 'live'}
        heap = {**cpu, 'metric': 'heap_mb', 'window': 'cold'}
        matrix = perf_gate.plan(PLAN, 'pr', remeasure={'cells': [cpu, heap]})
        self.assertEqual(shard_cells(matrix), [['docs']])

    def test_an_empty_remeasure_plans_nothing(self):
        self.assertEqual(perf_gate.plan(PLAN, 'pr', remeasure={'cells': []})['include'], [])

    def test_a_remeasure_plans_the_set_s_own_unit_of_each_red_cell(self):
        cold = {
            **PLAN,
            'sets': {
                **PLAN['sets'],
                'pr': {
                    'shards': 2,
                    'cells': [unit('files', 25, windows=['cold']), unit('docs', 75)],
                },
            },
        }
        red = {**CELL, 'metric': 'cpu_ms'}
        matrix = perf_gate.plan(cold, 'pr', remeasure={'cells': [red, red]})
        planned = [c for job in matrix['include'] for c in job['cells']]
        self.assertEqual(planned, [unit('files', 25, windows=['cold'])])

    def test_a_plan_that_breaks_its_shape_fails(self):
        cases = {
            'unknown set': (PLAN, 'weekly'),
            'no cells': ({'sets': {'pr': {'shards': 1, 'cells': []}}}, 'pr'),
            'no seconds': ({'sets': {'pr': {'shards': 1, 'cells': [unit('a', 0)]}}}, 'pr'),
            'twice': ({'sets': {'pr': {'shards': 1, 'cells': [unit('a', 1)] * 2}}}, 'pr'),
            'no shards': ({'sets': {'pr': {'shards': 0, 'cells': [unit('a', 1)]}}}, 'pr'),
            'no node': ({'sets': {'pr': {'shards': 1, 'cells': [unit('a', 1)]}}}, 'pr'),
        }
        for name, (doc, set_name) in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                perf_gate.plan(doc, set_name)

    def test_a_unit_s_runs_measure_it_again_in_later_runs_only(self):
        doc = {
            'node': '.',
            'sets': {
                'full': {
                    'shards': 2,
                    'cells': [
                        unit('files', 80),
                        unit('docs', 75),
                        unit('tabs', 70, runs=2),
                        unit('shell', 60, runs=3),
                    ],
                },
            },
        }
        jobs = perf_gate.plan(doc, 'full')['include']
        by_run = {}
        for job in jobs:
            by_run.setdefault(job['run'], []).extend(c['scenario'] for c in job['cells'])
        self.assertEqual(
            {r: sorted(s) for r, s in by_run.items()},
            {
                1: ['docs', 'files', 'shell', 'tabs'],
                2: ['shell', 'tabs'],
                3: ['shell'],
            },
        )
        # A later run takes as few shards as hold its units at the first run's mean load.
        self.assertEqual(
            [j['name'] for j in jobs],
            [
                'run-01-shard-1-of-2',
                'run-01-shard-2-of-2',
                'run-02-shard-1-of-1',
                'run-03-shard-1-of-1',
            ],
        )

    def test_a_later_run_takes_more_shards_when_its_units_need_them(self):
        cells = [unit(f's{i}', 100, runs=2) for i in range(4)] + [unit('one', 100)]
        doc = {'node': '.', 'sets': {'full': {'shards': 5, 'cells': cells}}}
        names = [j['name'] for j in perf_gate.plan(doc, 'full')['include']]
        self.assertEqual(names[5:], [f'run-02-shard-{i}-of-4' for i in range(1, 5)])

    def test_a_unit_s_runs_is_a_whole_number_from_one_to_three(self):
        for runs in (0, 4, 1.5, True, '2', None):
            doc = {'node': '.', 'sets': {'pr': {'shards': 1, 'cells': [unit('a', 1, runs=runs)]}}}
            with self.subTest(runs=runs), self.assertRaisesRegex(ValueError, 'runs'):
                perf_gate.plan(doc, 'pr')
        doc = {'node': '.', 'sets': {'pr': {'shards': 1, 'cells': [unit('a', 1, runs=1)]}}}
        self.assertEqual(
            [j['name'] for j in perf_gate.plan(doc, 'pr')['include']], ['shard-1-of-1']
        )

    def test_a_gate_plan_is_one_run_and_refuses_a_unit_s_runs(self):
        doc = {'node': '.', 'sets': {'pr': {'shards': 1, 'cells': [unit('a', 1, runs=2)]}}}
        with self.assertRaisesRegex(
            ValueError, r'a/x10/chromium/desktop: runs belongs to the full set'
        ):
            perf_gate.plan(doc, 'pr', gate=True)
        self.assertEqual(len(perf_gate.plan(doc, 'pr')['include']), 2)
        self.assertEqual(len(perf_gate.plan(PLAN, 'pr', gate=True)['include']), 2)

    def test_a_cell_field_with_whitespace_or_angle_brackets_fails(self):
        for name in ('x <!-- perf-trend:dev:files -->', 'a b', 'a>b', 'a\tb'):
            doc = {'node': '.', 'sets': {'pr': {'shards': 1, 'cells': [unit(name, 1)]}}}
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'whitespace, < or >'):
                perf_gate.plan(doc, 'pr')
        cell = {**unit('a', 1), 'engine': 'chro<mium'}
        doc = {'node': '.', 'sets': {'pr': {'shards': 1, 'cells': [cell]}}}
        with self.assertRaisesRegex(ValueError, 'whitespace, < or >'):
            perf_gate.plan(doc, 'pr')

    def test_a_remeasure_plans_a_unit_with_runs_once(self):
        cells = [unit('files', 80, runs=2), unit('docs', 75)]
        doc = {'node': '.', 'sets': {'full': {'shards': 2, 'cells': cells}}}
        red = {**CELL, 'metric': 'cpu_ms'}
        jobs = perf_gate.plan(doc, 'full', remeasure={'cells': [red]})['include']
        self.assertEqual([j['name'] for j in jobs], ['shard-1-of-1'])

    def test_more_jobs_than_one_matrix_holds_fails(self):
        cells = [unit(f's{i}', 1) for i in range(257)]
        doc = {'node': '.', 'sets': {'full': {'shards': 257, 'cells': cells}}}
        with self.assertRaisesRegex(ValueError, '257 jobs is over the 256'):
            perf_gate.plan(doc, 'full')

    def test_a_line_break_in_node_or_a_cell_field_fails(self):
        cells = [unit('files', 80)]
        cases = {
            'node': {'node': 'static-src\nsha=0', 'sets': {'pr': {'shards': 1, 'cells': cells}}},
            'scenario': {
                'node': '.',
                'sets': {'pr': {'shards': 1, 'cells': [unit('files\nsha=0', 80)]}},
            },
            'extra field': {
                'node': '.',
                'sets': {'pr': {'shards': 1, 'cells': [unit('files', 80, note='a\rb')]}},
            },
        }
        for name, doc in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, 'line break'):
                perf_gate.plan(doc, 'pr')

    def test_a_cell_s_windows_are_a_list_of_distinct_window_names(self):
        for windows in ([], 'cold', [''], [1], ['cold', 'cold']):
            cells = [unit('files', 80, windows=windows)]
            doc = {'node': '.', 'sets': {'pr': {'shards': 1, 'cells': cells}}}
            with self.subTest(windows=windows), self.assertRaisesRegex(ValueError, 'windows'):
                perf_gate.plan(doc, 'pr')
        cells = [unit('files', 80, windows=['cold', 'back'])]
        doc = {'node': '.', 'sets': {'pr': {'shards': 1, 'cells': cells}}}
        planned = perf_gate.plan(doc, 'pr')['include'][0]['cells'][0]
        self.assertEqual(planned['windows'], ['cold', 'back'])

    def test_a_set_or_cell_that_is_not_an_object_fails_the_plan(self):
        for sets in ({'pr': []}, {'pr': {'shards': 1, 'cells': ['files']}}):
            with self.subTest(sets=sets), self.assertRaisesRegex(TypeError, 'must be an object'):
                perf_gate.plan({'node': '.', 'sets': sets}, 'pr')

    def test_a_cell_keeps_the_fields_the_harness_reads(self):
        doc = {
            'node': '.',
            'sets': {'full': {'shards': 1, 'cells': [unit('files', 9, profile=True)]}},
        }
        self.assertTrue(perf_gate.plan(doc, 'full')['include'][0]['cells'][0]['profile'])


class ResultFiles(unittest.TestCase):
    NAMES = (
        'perf-results-cplieger-a-measure-shard-1-of-2-attempt-1',
        'perf-results-cplieger-a-measure-shard-2-of-2-attempt-1',
        'perf-results-cplieger-a-measure-x-measure-shard-1-of-1-attempt-1',
        'perf-results-cplieger-a-remeasure-shard-1-of-1-attempt-1',
        'perf-results-cplieger-a-remeasure-x-remeasure-shard-1-of-1-attempt-1',
        'perf-results-cplieger-.kiro-measure-shard-1-of-1-attempt-1',
        'perf-results-cplieger-xkiro-measure-shard-1-of-1-attempt-1',
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        for name in self.NAMES:
            (self.dir / name).mkdir()
            (self.dir / name / 'results.json').write_text('{}')

    def names(self, *args):
        return sorted(d.name for d in perf_gate.newest_shards(self.dir, *args).values())

    def test_only_this_call_s_shard_artifacts_of_the_stage_are_read(self):
        self.assertEqual(self.names('cplieger-a'), sorted(self.NAMES[:2]))
        self.assertEqual(self.names('cplieger-a', 'remeasure'), [self.NAMES[3]])

    def test_the_prefix_is_matched_literally(self):
        self.assertEqual(self.names('cplieger-.kiro'), [self.NAMES[5]])

    def test_no_shard_of_this_call_is_no_results(self):
        with self.assertRaisesRegex(ValueError, 'no results'):
            perf_gate.merge_results(self.dir, 'cplieger-b')

    def test_a_shard_without_its_attempt_is_not_read(self):
        for name, scenario in (('shard-1-of-2', 'legacy'), ('shard-1-of-1-attempt-1', 'files')):
            d = self.dir / f'perf-results-cplieger-b-measure-{name}'
            d.mkdir()
            (d / 'results.json').write_text(json.dumps(results([cell(scenario=scenario)])))
        merged = perf_gate.merge_results(self.dir, 'cplieger-b')
        self.assertEqual([c['scenario'] for c in merged['cells']], ['files'])

    def test_every_name_the_plan_writes_is_read(self):
        twice = {**PLAN, 'sets': {'pr': {'shards': 2, 'cells': [unit('files', 80, runs=2)]}}}
        for runs, doc in ((1, PLAN), (2, twice)):
            jobs = perf_gate.plan(doc, 'pr')['include']
            for job in jobs:
                (
                    self.dir / f'perf-results-cplieger-p{runs}-measure-{job["name"]}-attempt-1'
                ).mkdir()
            with self.subTest(runs=runs):
                self.assertEqual(
                    len(perf_gate.newest_shards(self.dir, f'cplieger-p{runs}')), len(jobs)
                )


class Attempts(unittest.TestCase):
    """merge_results across a run's attempts: each artifact name ends `-attempt-<n>`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)

    def shard(self, stage, i, m, attempt, metric='cpu_ms', run=''):
        name = f'perf-results-cplieger-a-{stage}-{run}shard-{i}-of-{m}-attempt-{attempt}'
        doc = results([cell(metric, (1000.0 + attempt,) * 3, scenario=f'{run}s{i}')])
        (self.dir / name).mkdir()
        (self.dir / name / 'results.json').write_text(json.dumps(doc))

    def scenarios(self, stage='remeasure', since=1):
        merged = perf_gate.merge_results(self.dir, 'cplieger-a', stage, since=since)
        return sorted((c['scenario'], c['reps'][0]) for c in merged['cells'])

    def test_a_rerun_reads_only_the_shards_of_the_compare_it_follows(self):
        for i in (1, 2, 3):
            self.shard('remeasure', i, 3, 1)
        for i in (1, 2):
            self.shard('remeasure', i, 2, 2)
        self.assertEqual(self.scenarios(since=2), [('s1', 1002.0), ('s2', 1002.0)])

    def test_the_incident_re_plan_reads_the_re_run_s_shards_and_refuses_both(self):
        # Attempt 1 planned three shards and attempt 2 two; w1 and w2 sit in both plans.
        for i, m, attempt in ((1, 3, 1), (2, 3, 1), (3, 3, 1), (1, 2, 2), (2, 2, 2)):
            name = f'perf-results-cplieger-a-remeasure-shard-{i}-of-{m}-attempt-{attempt}'
            (self.dir / name).mkdir()
            reps = (1000.0 + attempt,) * 3
            doc = results([cell(reps=reps, scenario='same', window=f'w{i}')])
            (self.dir / name / 'results.json').write_text(json.dumps(doc))
        merged = perf_gate.merge_results(self.dir, 'cplieger-a', 'remeasure', since=2)
        self.assertEqual(sorted(c['reps'][0] for c in merged['cells']), [1002.0, 1002.0])
        with self.assertRaisesRegex(ValueError, 'mixes shard counts'):
            perf_gate.merge_results(self.dir, 'cplieger-a', 'remeasure', since=1)

    def test_a_shard_folder_with_no_results_file_fails(self):
        self.shard('measure', 1, 2, 1)
        (self.dir / 'perf-results-cplieger-a-measure-shard-2-of-2-attempt-1').mkdir()
        with self.assertRaisesRegex(ValueError, 'shard-2-of-2-attempt-1 holds no results file'):
            self.scenarios('measure')

    def test_a_partial_rerun_takes_each_shard_from_its_newest_attempt(self):
        for i in range(1, 6):
            self.shard('measure', i, 5, 1)
        self.shard('measure', 3, 5, 2)
        got = self.scenarios('measure')
        self.assertEqual(
            got, [('s1', 1001.0), ('s2', 1001.0), ('s3', 1002.0), ('s4', 1001.0), ('s5', 1001.0)]
        )

    def test_a_missing_shard_fails(self):
        self.shard('measure', 1, 3, 1)
        self.shard('measure', 3, 3, 1)
        with self.assertRaisesRegex(ValueError, 'shard 2 of 3'):
            self.scenarios('measure')

    def test_shards_of_two_plans_fail(self):
        self.shard('remeasure', 1, 2, 1)
        self.shard('remeasure', 1, 3, 1)
        with self.assertRaisesRegex(ValueError, 'shard counts'):
            self.scenarios()

    def test_runs_are_grouped_and_each_must_be_whole(self):
        for run in ('run-01-', 'run-02-'):
            for i in (1, 2):
                self.shard('measure', i, 2, 1, run=run)
        groups = perf_gate.shard_groups(self.dir, 'cplieger-a')
        self.assertEqual(sorted(groups), ['run-01', 'run-02'])
        self.assertEqual([len(v) for v in groups.values()], [2, 2])

    def test_one_results_file_is_never_merged_from_two_runs(self):
        self.shard('measure', 1, 2, 1, run='run-01-')
        self.shard('measure', 2, 2, 1, run='run-01-')
        self.shard('measure', 1, 1, 1, run='run-02-')
        with self.assertRaisesRegex(ValueError, 'one run, not 2: run-01, run-02'):
            perf_gate.merge_results(self.dir, 'cplieger-a', 'measure')


X1 = {'scale': 'x1'}


def github_outputs(text):
    """$GITHUB_OUTPUT as the runner reads it: `name=value` lines and
    `name<<DELIMITER` blocks, a later name replacing an earlier one."""
    out, lines = {}, iter(text.splitlines())
    for line in lines:
        name, sep, value = line.partition('<<')
        if sep and '=' not in name:
            block = []
            for more in lines:
                if more == value:
                    break
                block.append(more)
            out[name] = '\n'.join(block)
        else:
            name, _, value = line.partition('=')
            out[name] = value
    return out


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        # Text put() replaces in every file it writes, for JSON that
        # json.dumps cannot produce.
        self.swap = {}

    def put(self, name, doc):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(doc)
        for old, new in self.swap.items():
            text = text.replace(old, new)
        path.write_text(text)
        return str(path)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = perf_gate.main(list(argv))
        self.output = out.getvalue() + err.getvalue()
        return rc

    def guard(self, cells, *extra, entries=None, base=True, plan=PLAN, shard=None, other=None):
        """The default mode, the guard's compare, of cells against a trend baseline file of
        entries: files x10 cold CPU at 1000 ms and its DOM count at 640 when entries is
        None. base False passes a path that holds no file."""
        if entries is None:
            entries = [baselined(), baselined('dom', 640)]
        was = str(self.dir / 'baseline.json')
        if base:
            doc = base if isinstance(base, dict) else trend_file(entries)
            was = self.put('baseline.json', doc)
        own = 'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json'
        self.put(own, results(cells) if shard is None else shard)
        if other is not None:
            self.put(f'results/{OTHER}-measure-shard-1-of-1-attempt-1/results.json', results(other))
        argv = ('--trend', was, '--results', str(self.dir / 'results'), '--prefix', 'cplieger-a')
        argv += ('--plan-doc', self.put('plan.json', plan), '--set', 'pr')
        argv += ('--remeasure-out', str(self.dir / 'remeasure.json'))
        argv += ('--summary', str(self.dir / 'summary.md'))
        return self.main(*argv, *extra)

    def decision(self):
        return json.loads((self.dir / 'remeasure.json').read_text())

    def verdict(self, first, again, decision, attempt='1'):
        self.put(
            'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json', first
        )
        self.put(
            f'again/perf-results-cplieger-a-remeasure-shard-1-of-1-attempt-{attempt}/results.json',
            again,
        )
        argv = ('--verdict', str(self.dir / 'again'), '--results', str(self.dir / 'results'))
        argv += ('--prefix', 'cplieger-a', '--remeasure-attempt', attempt)
        argv += ('--remeasure', self.put('decision.json', decision))
        return self.main(*argv, '--summary', str(self.dir / 'summary.md'))

    def test_compare_writes_a_red_cell_and_its_baseline_to_remeasure_and_passes(self):
        red = cell(reps=(2600.0,) * 3)
        rc = self.guard([red, cell('dom', (640,) * 3, kind='count')])
        self.assertEqual(rc, 0, self.output)
        self.assertEqual(
            self.decision(),
            {'cells': [{**red, 'cpu_model': 'AMD EPYC 7763', 'baseline': 1000.0}]},
        )

    def test_compare_passes_a_count_off_dev_and_re_measures_nothing(self):
        rc = self.guard([cell(), cell('dom', (700,) * 3, kind='count')])
        self.assertEqual(rc, 0, self.output)
        self.assertEqual(self.decision()['cells'], [])
        summary = (self.dir / 'summary.md').read_text()
        self.assertIn(f'| {DOM} | 640 | 700 |', summary)

    def test_compare_fails_when_the_trend_branch_holds_no_baseline_file(self):
        self.assertEqual(self.guard([cell()], base=False), 1)
        self.assertIn(
            '::error::the perf-trend branch holds no series/dev/baseline.json, so the guard '
            'has no baseline',
            self.output,
        )
        self.assertFalse((self.dir / 'remeasure.json').exists())

    def test_compare_fails_a_baselined_cell_of_the_measured_windows_no_shard_measured(self):
        # The pr set measures WebKit x10 cold only; the trend holds the live CPU too.
        webkit = {'engine': 'webkit'}
        plan = {
            'node': '.',
            'sets': {
                'pr': {'shards': 1, 'cells': [unit('files', 25, windows=['cold'], **webkit)]},
                'full': {'shards': 1, 'cells': [unit('files', 25, **webkit)]},
            },
        }
        cold = {**baselined('rss_mb', 200.0), **webkit}
        live = {**baselined(), **webkit, 'window': 'live'}
        heap = {**baselined('heap_mb', 40.0), **webkit}
        measured = [
            {**cell('rss_mb', (200.0,) * 3), **webkit},
            {**cell('heap_mb', (40.0,) * 3), **webkit},
        ]
        self.assertEqual(
            self.guard(measured, entries=[cold, live, heap], plan=plan), 0, self.output
        )
        self.assertEqual(self.guard(measured[:1], entries=[cold, live, heap], plan=plan), 1)
        errors = [line for line in self.output.splitlines() if line.startswith('::error::')]
        self.assertEqual(
            errors,
            [
                "::error::files/x10/webkit/desktop/cold/heap_mb: dev's trend has a baseline for it, and no shard measured it"
            ],
        )

    def test_compare_passes_a_baselined_window_the_head_plan_no_longer_lists(self):
        plan = {**PLAN, 'scenarios': {'files': {'windows': ['cold']}}}
        live = {**baselined(), 'window': 'live'}
        rc = self.guard([cell()], entries=[baselined(), live], plan=plan)
        self.assertEqual(rc, 0, self.output)
        summary = (self.dir / 'summary.md').read_text()
        self.assertIn(
            f"- {CPU.replace('cold', 'live')}: dev's trend has a baseline for it, and the head "
            'plan.json no longer measures window live\n',
            summary,
        )
        self.assertEqual(self.guard([cell()], entries=[baselined(), live]), 1)

    def test_compare_refuses_a_non_finite_number_in_the_base_or_the_results(self):
        cases = {
            'NaN baseline': ([cell()], [baselined(baseline=float('nan'))], {}),
            'Infinity rep': ([cell(reps=(float('inf'),) * 3)], None, {}),
            '1e999 rep': ([cell(reps=(-123.25,) * 3)], None, {'-123.25': '-1e999'}),
        }
        for name, (cells, entries, swap) in cases.items():
            with self.subTest(name):
                self.swap = swap
                self.assertEqual(self.guard(cells, entries=entries), 1)
                self.assertIn('is not a finite number', self.output)

    def test_a_document_of_the_wrong_shape_is_an_error_line(self):
        cases = {
            'list plan.json': ({'plan': []}, 'must be a JSON object'),
            'list results': ({'shard': []}, 'must be a JSON object'),
            'list trend': ({'base': {'cells': 'all'}}, 'cells must be a list'),
            'string entry': ({'entries': ['files']}, 'cells[0]: must be an object'),
        }
        for name, (kw, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.guard([cell()], **kw), 1)
                self.assertIn(want, self.output)
                self.assertFalse((self.dir / 'remeasure.json').exists())

    def test_the_verdict_fails_a_red_cell_on_its_median_of_six(self):
        for first, more, rc in (
            ((2400.0, 2500.0, 2600.0), (1000.0,) * 3, 0),
            ((9700.0, 9800.0, 9900.0), (9750.0, 9850.0, 9800.0), 1),
        ):
            with self.subTest(first=first, more=more):
                red = cell(reps=first)
                self.assertEqual(self.guard([red, cell('dom', (640,) * 3, kind='count')]), 0)
                again = results([cell(reps=more)])
                self.assertEqual(self.verdict(results([red]), again, self.decision()), rc)
        self.assertIn(
            f'::error::{CPU}: median of six 9800.0 is 8800 over its baseline 1000.0 (9.80x)',
            self.output,
        )
        self.assertIn('### Re-measured', (self.dir / 'summary.md').read_text())

    def test_compare_reads_only_its_own_repository_s_shards(self):
        own = [cell(), cell('dom', (640,) * 3, kind='count')]
        other = [cell(reps=(9000.0,) * 3)]
        self.assertEqual(self.guard(own, other=other), 0, self.output)
        self.assertEqual(self.decision()['cells'], [])

    def test_verdict_reads_only_its_own_repository_s_remeasured_shards(self):
        red = cell(reps=(3000.0,) * 3)
        decision = {'cells': [{**red, 'baseline': 1000.0}]}
        other = 'again/perf-results-cplieger-a-remeasure-x-remeasure-shard-1-of-1-attempt-2/results.json'
        self.put(other, results([red]))
        stale = 'again/perf-results-cplieger-a-remeasure-shard-1-of-2-attempt-1/results.json'
        self.put(stale, results([red]))
        self.assertEqual(
            self.verdict(results([red]), results([cell()]), decision, attempt='2'), 0, self.output
        )
        argv = ('--verdict', str(self.dir / 'again'), '--results', str(self.dir / 'results'))
        argv += ('--prefix', 'cplieger-a', '--remeasure', str(self.dir / 'decision.json'))
        self.assertEqual(self.main(*argv, '--remeasure-attempt', '1'), 1)
        self.assertIn('shard counts', self.output)

    def test_only_the_plan_the_guard_and_the_verdict_are_modes(self):
        for flag in (
            '--calibrate',
            '--check-in',
            '--take',
            '--check-file',
            '--bench',
            '--runs',
            '--budgets',
            '--base',
            '--sources',
            '--accepted',
            '--base-sha',
            '--reason',
            '--cells',
            '--pr-body',
            '--commit',
            '--runner',
            '--run-id',
            '--run-url',
            '--repo',
            '--head',
        ):
            err = io.StringIO()
            with self.subTest(flag), redirect_stderr(err), self.assertRaises(SystemExit):
                perf_gate.main([flag, 'x'])
            self.assertIn(f'unrecognized arguments: {flag}', err.getvalue())

    def test_compare_needs_the_plan(self):
        argv = ('--trend', 'b.json', '--results', 'r', '--prefix', 'cplieger-a')
        argv += ('--remeasure-out', 'remeasure.json')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.main(*argv)

    def test_the_results_readers_need_their_prefix_and_the_verdict_its_attempt(self):
        d, f = str(self.dir), str(self.dir / 'f.json')
        cases = {
            'compare': (
                ('--trend', f, '--results', d, '--prefix', 'p', '--plan-doc', f, '--set', 'pr'),
                ['--remeasure-out'],
            ),
            'compare without results': (
                ('--trend', f, '--remeasure-out', f, '--plan-doc', f, '--set', 'pr'),
                ['--results', '--prefix'],
            ),
            'verdict': (
                ('--verdict', d, '--results', d),
                ['--prefix', '--remeasure-attempt', '--remeasure'],
            ),
            'plan': (('--plan', f), ['--set', '--out']),
        }
        for name, (argv, missing) in cases.items():
            with self.subTest(name):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit):
                    perf_gate.main(list(argv))
                self.assertIn(f'missing {", ".join(missing)}', err.getvalue())

    def test_plan_writes_the_matrix(self):
        twice = [unit('run-page', 540), unit('docs', 75, runs=2), unit('tabs', 70, runs=2)]
        p = self.put('plan.json', {**PLAN, 'sets': {'pr': {'shards': 2, 'cells': twice}}})
        out = self.dir / 'matrix.json'
        argv = ('--plan', p, '--set', 'pr', '--only', 'docs, tabs')
        outputs = self.dir / 'github-output'
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.main(*argv, '--out', str(out)), 1)
        self.assertIn('a gated set is measured in one run', self.output)
        argv += ('--mode', 'measure')
        self.assertEqual(self.main(*argv, '--out', str(out), '--outputs', str(outputs)), 0)
        matrix = json.loads(out.read_text())
        self.assertEqual(list(matrix), ['include'])
        self.assertEqual(len(matrix['include']), 4)
        written = github_outputs(outputs.read_text())
        self.assertEqual(written['matrix'], json.dumps(matrix))
        self.assertEqual((written['node'], written['scenarios']), ('static-src', 'docs tabs'))

    def test_plan_writes_no_output_line_from_a_line_break_in_plan_json(self):
        injected = {
            'node': {**PLAN, 'node': 'static-src\nsha=0'},
            'scenario': {**PLAN, 'sets': {'pr': {'shards': 1, 'cells': [unit('a\nsha=0', 9)]}}},
        }
        for name, doc in injected.items():
            with self.subTest(name):
                outputs = self.dir / f'github-output-{name}'
                outputs.write_text('sha=c0ffee\n')
                argv = ('--plan', self.put(f'{name}.json', doc), '--set', 'pr')
                argv += ('--out', str(self.dir / 'm.json'), '--outputs', str(outputs))
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(self.main(*argv), 1)
                self.assertEqual(github_outputs(outputs.read_text()), {'sha': 'c0ffee'})

    def test_plan_fails_on_an_unknown_scenario(self):
        p = self.put('plan.json', PLAN)
        argv = ('--plan', p, '--set', 'pr', '--only', 'nope', '--out', str(self.dir / 'm.json'))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.main(*argv), 1)


if __name__ == '__main__':
    unittest.main()
