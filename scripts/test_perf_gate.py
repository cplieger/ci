"""perf_gate.py: the tier 2 budget file's rule, the compare and verdict of a
red cell, the ten-run calibration and the provenance of a changed entry."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

import perf_gate

SCRIPTS = pathlib.Path(__file__).resolve().parent
TESTDATA = SCRIPTS / 'testdata' / 'perf-gate'

REPO = 'cplieger/marotte'
RUNNER = 'ubuntu-26.04'
HEAD = 'c0ffee'
# FakeGit's root commit, an ancestor of every commit.
ROOT = 'r00t'
CELL = {
    'scenario': 'files',
    'scale': 'x10',
    'engine': 'chromium',
    'width': 'desktop',
    'window': 'cold',
}
RUN_URL = 'https://github.com/cplieger/ci/actions/runs/'
MEASURED_URL = f'https://github.com/{REPO}/actions/runs/'
DOM = 'files/x10/chromium/desktop/cold/dom'
CPU = 'files/x10/chromium/desktop/cold/cpu_ms'
PREFIX = 'cplieger-marotte'
# The artifact prefix of a repository cplieger/a-measure-x, whose shards a
# `perf-results-cplieger-a-measure-*` download pattern also matches.
OTHER = 'perf-results-cplieger-a-measure-x'


def entry(metric='cpu_ms', tier='gated', baseline=1000.0, budget=None, **kw):
    e = {**CELL, 'metric': metric, 'tier': tier, 'baseline': baseline}
    if budget is None and tier == 'gated' and metric == 'cpu_ms':
        budget = max(50.0, baseline * kw.get('headroom', 1.5))
    if budget is None and tier == 'exact':
        budget = baseline
    if budget is not None:
        e['budget'] = budget
    e['source'] = kw.pop('source', RUN_URL + '111')
    e.update(kw)
    return e


def budgets(entries, **kw):
    doc = {
        'schema': 2,
        'runner': RUNNER,
        'headroom': {'cpu_ms': 1.5, 'heap_mb': 1.25, 'rss_mb': 1.25},
        'floor': {'cpu_ms': 50, 'heap_mb': 1},
        'target': {'server_cpu_ms': 63.0},
        'budgets': entries,
    }
    doc.update(kw)
    return doc


def cell(metric='cpu_ms', reps=(1000.0, 1000.0, 1000.0), kind='time', **kw):
    return {**CELL, 'metric': metric, 'kind': kind, 'reps': list(reps), **kw}


def results(cells, runner=RUNNER):
    return {'schema': 1, 'runner': runner, 'cpu_model': 'AMD EPYC 7763', 'cells': cells}


def keyof(d):
    return perf_gate.key(d)


class TheRule(unittest.TestCase):
    def test_a_gated_budget_is_the_baseline_times_the_headroom_over_the_floor(self):
        self.assertEqual(perf_gate.check_rule(budgets([entry(budget=1500.0)])), [])
        self.assertTrue(perf_gate.check_rule(budgets([entry(budget=1600.0)])))
        self.assertEqual(perf_gate.check_rule(budgets([entry(baseline=20.0, budget=50.0)])), [])
        self.assertTrue(perf_gate.check_rule(budgets([entry(baseline=20.0, budget=30.0)])))
        heap = entry(metric='heap_mb', baseline=0.5, budget=1.0)
        self.assertEqual(perf_gate.check_rule(budgets([heap])), [])

    def test_an_entrys_own_headroom_replaces_the_files(self):
        self.assertEqual(perf_gate.check_rule(budgets([entry(headroom=1.6, budget=1600.0)])), [])
        self.assertTrue(perf_gate.check_rule(budgets([entry(headroom=1.6, budget=1500.0)])))

    def test_an_exact_count_budget_is_its_baseline(self):
        self.assertEqual(
            perf_gate.check_rule(budgets([entry(metric='dom', tier='exact', baseline=640)])), []
        )
        bad = entry(metric='dom', tier='exact', baseline=640, budget=700)
        self.assertTrue(perf_gate.check_rule(budgets([bad])))

    def test_server_cpu_per_request_budget_is_its_familys_target(self):
        good = entry(metric='server_cpu_ms:GET /api/chats', baseline=20.0, budget=63.0)
        self.assertEqual(perf_gate.check_rule(budgets([good])), [])
        bad = entry(metric='server_cpu_ms:GET /api/chats', baseline=20.0, budget=30.0)
        self.assertIn(
            'is its target 63.0, not 30.0', ' '.join(perf_gate.check_rule(budgets([bad])))
        )
        untargeted = perf_gate.check_rule(budgets([good], target={}))
        self.assertIn('no target for server_cpu_ms', ' '.join(untargeted))

    def test_the_target_map_holds_a_number_above_0_per_family(self):
        for target in ([], {'server_cpu_ms': 0}, {'server_cpu_ms': '63'}):
            with self.subTest(target=target):
                doc = budgets([entry()], target=target)
                self.assertTrue(perf_gate.check_shape(doc))
                self.assertIn('target', ' '.join(perf_gate.check_rule(doc)))

    def test_a_file_of_another_schema_fails(self):
        for schema in (1, 3, None):
            with self.subTest(schema=schema):
                errors = perf_gate.check_rule(budgets([entry()], schema=schema))
                self.assertIn('budgets.json: schema must be 2', errors)

    def test_an_entry_missing_its_source_fails(self):
        self.assertTrue(perf_gate.check_rule(budgets([entry(source='')])))
        e = entry()
        del e['source']
        self.assertTrue(perf_gate.check_rule(budgets([e])))

    def test_an_entry_field_outside_the_schema_fails(self):
        for field, value in (('rows', ['F1']), ('target', 63.0), ('accepted', 'why'), ('x', 1)):
            with self.subTest(field=field):
                errors = perf_gate.check_rule(budgets([entry(**{field: value})]))
                self.assertIn(
                    f'files/x10/chromium/desktop/cold/cpu_ms: unknown field {field}', errors
                )

    def test_a_file_field_outside_the_schema_fails(self):
        errors = perf_gate.check_rule(budgets([entry()], headrom={'cpu_ms': 2.0}))
        self.assertIn('budgets.json: unknown field headrom', errors)

    def test_a_reason_is_one_non_empty_line(self):
        self.assertEqual(perf_gate.check_rule(budgets([entry(reason='a slower parse')])), [])
        for reason in ('', '  ', 'one\ntwo', 'one\rtwo', 7):
            with self.subTest(reason=reason):
                errors = perf_gate.check_rule(budgets([entry(reason=reason)]))
                self.assertIn('reason', ' '.join(errors))

    def test_a_reason_on_an_entry_no_file_edit_can_move_fails(self):
        exact = entry(metric='dom', tier='exact', baseline=640, reason='a new view')
        reported = entry(tier='reported', reason='noisy')
        for e in (exact, reported):
            with self.subTest(tier=e['tier']):
                errors = perf_gate.check_rule(budgets([e]))
                self.assertTrue(any('carries no reason' in x for x in errors), errors)

    def test_a_measured_file_sources_an_exact_entry_with_its_reason(self):
        url = MEASURED_URL + '900/attempts/1'
        moved = entry('dom', 'exact', 2056, source=url, reason='one more node in the view')
        self.assertEqual(perf_gate.check_rule(budgets([moved])), [])
        silent = entry('dom', 'exact', 2056, source=url)
        self.assertEqual(
            perf_gate.check_rule(budgets([silent])),
            [f'{DOM}: a value from a measured file needs its one-line reason'],
        )
        gated = entry(source=url, reason='slower')
        self.assertEqual(
            perf_gate.check_rule(budgets([gated])),
            [f'{CPU}: only an exact entry takes its value from a measured file'],
        )

    def test_a_source_url_is_a_calibration_run_or_a_measured_run_attempt(self):
        cases = {
            RUN_URL + '111': ('calibration', 'cplieger/ci', '111', '', 'calibration 111'),
            MEASURED_URL + '900/attempts/2': ('measured', REPO, '900', '2', 'measured file 900-2'),
        }
        for url, (kind, repository, run_id, attempt, label) in cases.items():
            with self.subTest(url=url):
                s = perf_gate.parse_source(url)
                self.assertEqual(
                    (s.kind, s.repository, s.run_id, s.attempt), (kind, repository, run_id, attempt)
                )
                self.assertEqual((s.url, s.label), (url, label))
        ci = perf_gate.parse_source(RUN_URL + '111/attempts/1')
        self.assertEqual((ci.kind, ci.repository, ci.stem), ('measured', 'cplieger/ci', '111-1'))
        for url in (
            'https://github.com/cplieger/other/actions/runs/111',
            MEASURED_URL + '900',
            MEASURED_URL + '900/attempts/x',
            'https://example.com/cplieger/ci/actions/runs/111',
            None,
        ):
            with self.subTest(url=url):
                self.assertIsNone(perf_gate.parse_source(url))

    def test_an_unknown_tier_fails(self):
        self.assertTrue(perf_gate.check_rule(budgets([entry(tier='maybe', budget=1500.0)])))

    def test_a_duplicate_key_fails(self):
        self.assertTrue(perf_gate.check_rule(budgets([entry(), entry()])))

    def test_a_retired_cell_budgets_still_lists_fails(self):
        retired = [{**CELL, 'metric': 'cpu_ms', 'reason': 'the view was removed'}]
        self.assertEqual(
            perf_gate.check_rule(budgets([entry()], retired=retired)),
            [f'{CPU}: retired, yet budgets lists it'],
        )
        self.assertEqual(perf_gate.check_rule(budgets([], retired=retired)), [])

    def test_a_headroom_of_zero_or_below_fails(self):
        for h in (0, -1.5):
            with self.subTest(own=h):
                self.assertTrue(perf_gate.check_rule(budgets([entry(headroom=h, budget=50.0)])))
            with self.subTest(file=h):
                doc = budgets([entry(budget=50.0)], headroom={'cpu_ms': h})
                self.assertTrue(perf_gate.check_rule(doc))

    def test_a_non_finite_budget_baseline_headroom_or_floor_fails(self):
        nan, inf = float('nan'), float('inf')
        cases = {
            'NaN budget': budgets([entry(budget=nan)]),
            'NaN baseline': budgets([entry(baseline=nan, budget=50.0)]),
            'Infinity headroom': budgets([entry(budget=inf)], headroom={'cpu_ms': inf}),
            'Infinity floor': budgets([entry(budget=inf)], floor={'cpu_ms': inf}),
            'NaN server target': budgets(
                [entry('server_cpu_ms', baseline=20.0, budget=nan)], target={'server_cpu_ms': nan}
            ),
        }
        for name, doc in cases.items():
            with self.subTest(name):
                self.assertTrue(perf_gate.check_rule(doc))

    def test_a_file_or_entry_of_the_wrong_shape_is_an_error_line(self):
        cases = {
            'list file': ([], 'budgets.json: must be a JSON object'),
            'string entry': (budgets(['files']), 'budgets[0]: must be an object'),
            'string retired': (budgets([], retired=['files']), 'retired[0]: must be an object'),
            'budgets object': (budgets({'files': entry()}), 'budgets.json: budgets must be a list'),
            'headroom list': (budgets([entry()], headroom=[]), 'budgets.json: headroom must map'),
            'numeric metric': (budgets([entry(metric=1)]), 'budgets[0]: missing metric'),
            'list tier': (budgets([entry(tier=['gated'], budget=1500.0)]), "tier ['gated']"),
            'string budget': (budgets([entry(budget='1500')]), 'must be numbers'),
        }
        for name, (doc, want) in cases.items():
            with self.subTest(name):
                errors = perf_gate.check_rule(doc)
                self.assertTrue(errors)
                self.assertTrue(perf_gate.check_shape(doc))
                self.assertIn(want, ' '.join(errors))
        self.assertEqual(perf_gate.check_shape(budgets([entry(budget=1600.0)])), [])


class Compare(unittest.TestCase):
    def test_an_exact_count_whose_middle_rep_is_its_budget_passes(self):
        b = budgets([entry(metric='dom', tier='exact', baseline=640)])
        for reps in ((640, 640, 640), (641, 640, 640), (640, 639, 640), (639, 640, 641)):
            with self.subTest(reps=reps):
                out = perf_gate.compare(b, results([cell('dom', reps, kind='count')]))
                self.assertEqual((out.failures, out.remeasure), ([], []))

    def test_an_exact_count_whose_middle_rep_is_off_is_remeasured_not_failed(self):
        b = budgets([entry(metric='dom', tier='exact', baseline=640)])
        for reps in ((641, 641, 640), (639, 640, 639), (641, 641, 641), (639, 639, 639)):
            with self.subTest(reps=reps):
                out = perf_gate.compare(b, results([cell('dom', reps, kind='count')]))
                self.assertEqual(out.failures, [])
                self.assertEqual([keyof(c) for c in out.remeasure], [keyof(entry('dom'))])

    def test_an_exact_entry_the_harness_measured_as_a_time_fails_at_compare(self):
        b = budgets([entry(metric='dom', tier='exact', baseline=640)])
        out = perf_gate.compare(b, results([cell('dom', (640.0,) * 3, kind='time')]))
        self.assertEqual(len(out.failures), 1)
        self.assertIn('needs a count', out.failures[0])
        self.assertEqual(out.remeasure, [])

    def test_a_gated_cell_is_red_only_when_its_median_of_three_is_over(self):
        b = budgets([entry(budget=1500.0)])
        red = perf_gate.compare(b, results([cell(reps=(1600.0, 1400.0, 1600.0))]))
        self.assertEqual(red.failures, [])
        self.assertEqual([keyof(c) for c in red.remeasure], [keyof(entry())])
        one = perf_gate.compare(b, results([cell(reps=(1600.0, 1400.0, 1400.0))]))
        self.assertEqual((one.failures, one.remeasure), ([], []))

    def test_a_cell_under_its_budget_is_never_remeasured(self):
        out = perf_gate.compare(budgets([entry()]), results([cell(reps=(1490.0, 1490.0, 1490.0))]))
        self.assertEqual((out.failures, out.remeasure), ([], []))

    def test_a_cell_under_baseline_over_headroom_yields_a_lowering_notice(self):
        out = perf_gate.compare(budgets([entry()]), results([cell(reps=(600.0, 600.0, 600.0))]))
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertEqual(len(out.notices), 1)
        none = perf_gate.compare(budgets([entry()]), results([cell(reps=(700.0, 700.0, 700.0))]))
        self.assertEqual(none.notices, [])

    def test_server_cpu_per_request_gates_at_its_target(self):
        b = budgets([entry(metric='server_cpu_ms', baseline=20.0, budget=63.0)])
        over = perf_gate.compare(
            b, results([cell('server_cpu_ms', (70.0, 70.0, 70.0), kind='server_cpu')])
        )
        self.assertEqual(len(over.remeasure), 1)
        under = perf_gate.compare(
            b, results([cell('server_cpu_ms', (40.0, 40.0, 40.0), kind='server_cpu')])
        )
        self.assertEqual((under.failures, under.remeasure), ([], []))

    def test_another_runner_reports_every_metric_and_gates_none(self):
        b = budgets([entry(), entry(metric='dom', tier='exact', baseline=640)])
        cells = [cell(reps=(9000.0, 9000.0, 9000.0)), cell('dom', (9, 9, 9), kind='count')]
        out = perf_gate.compare(b, results(cells, runner='self-hosted-x'))
        self.assertTrue(out.runner_mismatch)
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertEqual(len(out.reported), 2)
        self.assertIn('calibrate', perf_gate.summary(b, out).lower())

    def test_an_empty_budget_list_reports_every_cell_and_fails_nothing(self):
        cells = [cell(reps=(9000.0, 9000.0, 9000.0)), cell('dom', (9, 9, 9), kind='count')]
        out = perf_gate.compare(budgets([]), results(cells))
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertEqual(len(out.reported), 2)

    def test_a_measured_cell_with_no_entry_is_reported(self):
        other = {**cell(reps=(9000.0, 9000.0, 9000.0)), 'window': 'rest'}
        out = perf_gate.compare(budgets([entry()]), results([cell(), other]))
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertEqual([keyof(r) for r in out.reported], [keyof(other)])

    def test_a_reported_entry_never_gates(self):
        e = entry(tier='reported')
        out = perf_gate.compare(budgets([e]), results([cell(reps=(9000.0, 9000.0, 9000.0))]))
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertEqual(len(out.reported), 1)

    def test_an_entrys_own_headroom_sets_its_budget(self):
        wide = entry(headroom=1.6, budget=1600.0)
        out = perf_gate.compare(budgets([wide]), results([cell(reps=(1550.0, 1550.0, 1550.0))]))
        self.assertEqual(out.remeasure, [])
        low = perf_gate.compare(budgets([wide]), results([cell(reps=(640.0, 640.0, 640.0))]))
        self.assertEqual(low.notices, [])

    def test_a_cell_over_its_familys_target_is_flagged_and_does_not_fail(self):
        srv = entry('server_cpu_ms:GET /api/tools', tier='reported')
        out = perf_gate.compare(
            budgets([srv]), results([cell(srv['metric'], (70.0, 70.0, 70.0), kind='server_cpu')])
        )
        self.assertEqual(out.failures, [])
        self.assertEqual([keyof(c) for c in out.over_target], [keyof(srv)])
        self.assertIn('| 63.0 | reported (over target) |', perf_gate.summary(budgets([srv]), out))

    def test_a_zero_headroom_never_divides(self):
        b = budgets([entry(budget=50.0)], headroom={'cpu_ms': 0})
        out = perf_gate.compare(b, results([cell(reps=(40.0, 40.0, 40.0))]))
        self.assertEqual((out.failures, out.notices), ([], []))

    def test_a_gated_or_exact_entry_the_set_lists_and_no_shard_measured_fails(self):
        b = budgets([entry(), entry('dom', 'exact', 640), entry('frames', 'reported', 9.0)])
        units = perf_gate.planned_windows([unit('files', 80)])
        out = perf_gate.compare(b, results([]), units)
        self.assertEqual(len(out.failures), 2)
        self.assertTrue(all('no shard measured' in f for f in out.failures))
        measured = [cell(), cell('dom', (640,) * 3, kind='count')]
        self.assertEqual(perf_gate.compare(b, results(measured), units).failures, [])
        self.assertEqual(perf_gate.compare(b, results([]), {}).failures, [])
        self.assertEqual(perf_gate.compare(b, results([], runner='x'), units).failures, [])

    def test_a_cell_that_lists_windows_needs_a_result_only_for_those(self):
        # The pr set measures WebKit x10 cold only; budgets.json holds the
        # full set's WebKit live CPU and back count too.
        webkit = {'engine': 'webkit'}
        doc = {
            'node': '.',
            'sets': {
                'pr': {'shards': 1, 'cells': [unit('files', 25, windows=['cold'], **webkit)]},
            },
        }
        matrix = perf_gate.plan(doc, 'pr')
        units = perf_gate.planned_windows(c for job in matrix['include'] for c in job['cells'])
        cold = {**entry('rss_mb', baseline=200.0, budget=250.0), **webkit}
        live = {**entry(), **webkit, 'window': 'live'}
        back = {**entry('dom', 'exact', 640), **webkit, 'window': 'back'}
        b = budgets([cold, live, back])
        measured = results([{**cell('rss_mb', (200.0,) * 3), **webkit}])
        self.assertEqual(perf_gate.compare(b, measured, units).failures, [])
        dropped = perf_gate.compare(b, results([]), units).failures
        self.assertEqual(len(dropped), 1)
        self.assertTrue(dropped[0].startswith('files/x10/webkit/desktop/cold/rss_mb:'))


class Verdict(unittest.TestCase):
    def test_a_red_cell_fails_only_on_its_median_of_six(self):
        b = budgets([entry(budget=1500.0)])
        first = results([cell(reps=(1600.0, 1600.0, 1400.0))])
        for more, fails in (((1400.0, 1400.0, 1400.0), False), ((1600.0, 1550.0, 1600.0), True)):
            with self.subTest(more=more):
                out = perf_gate.verdict(b, first, results([cell(reps=more)]))
                self.assertEqual(bool(out.failures), fails)

    def test_a_cell_that_was_not_remeasured_is_not_judged_again(self):
        b = budgets([entry(budget=1500.0)])
        out = perf_gate.verdict(b, results([cell(reps=(1400.0, 1400.0, 1400.0))]), results([]))
        self.assertEqual(out.failures, [])

    def test_an_exact_count_fails_only_when_the_middle_of_its_six_reps_is_off(self):
        b = budgets([entry(metric='dom', tier='exact', baseline=640)])
        first = results([cell('dom', (641, 641, 640), kind='count')])
        for more, fails in (
            ((640, 640, 640), False),
            ((640, 639, 640), False),
            ((641, 640, 640), True),
            ((641, 641, 641), True),
        ):
            with self.subTest(more=more):
                again = results([cell('dom', more, kind='count')])
                out = perf_gate.verdict(b, first, again)
                self.assertEqual(bool(out.failures), fails)
                self.assertEqual(len(out.six), 1)

    def test_an_exact_count_whose_six_reps_straddle_its_budget_fails(self):
        # Their median is the budget, 2, though no rep measured it.
        b = budgets([entry(metric='reads', tier='exact', baseline=2)])
        first = results([cell('reads', (1, 1, 3), kind='count')])
        out = perf_gate.verdict(b, first, results([cell('reads', (3, 3, 1), kind='count')]))
        self.assertEqual(len(out.failures), 1)
        self.assertIn('| 1, 3 |', perf_gate.summary(b, out))

    def test_a_count_every_rep_moved_fails_on_its_six_reps(self):
        b = budgets([entry(metric='dom', tier='exact', baseline=640)])
        first = results([cell('dom', (641,) * 3, kind='count')])
        self.assertEqual(perf_gate.compare(b, first).failures, [])
        out = perf_gate.verdict(b, first, results([cell('dom', (641,) * 3, kind='count')]))
        self.assertEqual(len(out.failures), 1)
        self.assertIn('exact budget 640', out.failures[0])

    def test_the_held_out_runs_that_failed_an_agreed_count_pass(self):
        entries, first, again = [], [], []
        for name, budget, reps, more in HELD_OUT:
            k = dict(zip(perf_gate.KEY_FIELDS, name.split('/', 5), strict=True))
            entries.append({**entry(tier='exact', baseline=budget), **k})
            first.append({**cell(reps=reps, kind='count'), **k})
            again.append({**cell(reps=more, kind='count'), **k})
        b = budgets(entries)
        out = perf_gate.compare(b, results(first))
        self.assertEqual(out.failures, [])
        self.assertEqual(len(out.remeasure), 3)
        self.assertEqual(perf_gate.verdict(b, results(first), results(again)).failures, [])


def accepted(budget=1700.0, reason='a slower parse on purpose', file='slow.json', **kw):
    """compare's view of one accepted slowdown, {key: {budget, reason, file}}."""
    return {keyof({**entry(), **kw}): {'budget': budget, 'reason': reason, 'file': file}}


def decision(accepted_cells=None, stale=(), extend=(), reported=()):
    """compare's remeasure.json as the verdict reads it."""
    return {
        'cells': [],
        'stale': [perf_gate.key_dict(k) for k in stale],
        'extend': list(extend),
        'accepted': [{**perf_gate.key_dict(k), **a} for k, a in (accepted_cells or {}).items()],
        'reported': list(reported),
    }


class Accepted(unittest.TestCase):
    """A gated cell slower on purpose, judged against the budget its accepted file gives."""

    def test_the_one_seam_judging_a_time_gives_the_larger_accepted_budget(self):
        b, e, c = budgets([entry(budget=1500.0)]), entry(budget=1500.0), cell()
        self.assertEqual(perf_gate.gated_budget(b, e, [c], {}), (1500.0, None))
        a = accepted()
        self.assertEqual(perf_gate.gated_budget(b, e, [c], a), (1700.0, a[keyof(e)]))
        self.assertEqual(perf_gate.gated_budget(b, e, [c], accepted(1400.0)), (1500.0, None))

    def test_the_seam_sees_every_shard_whose_reps_are_judged(self):
        b = budgets([entry(budget=1500.0)])
        first = {**cell(reps=(1600.0,) * 3), 'cpu_model': 'AMD EPYC 7763'}
        more = {**cell(reps=(1400.0,) * 3), 'cpu_model': 'Intel Xeon 8370C'}
        seen = []

        def seam(budgets_doc, e, cells, accepted):
            seen.append((budgets_doc is b, [c['cpu_model'] for c in cells]))
            return e['budget'], None

        with unittest.mock.patch.object(perf_gate, 'gated_budget', seam):
            perf_gate.verdict(b, results([first]), results([more]))
        self.assertEqual(
            seen, [(True, ['AMD EPYC 7763']), (True, ['AMD EPYC 7763', 'Intel Xeon 8370C'])]
        )

    def test_an_accepted_slowdown_passes_with_its_note(self):
        b = budgets([entry(budget=1500.0)])
        out = perf_gate.compare(b, results([cell(reps=(1600.0,) * 3)]), accepted=accepted())
        self.assertEqual((out.failures, out.remeasure), ([], []))
        self.assertEqual(len(out.slower), 1)
        text = perf_gate.summary(b, out)
        self.assertIn('### Slower on purpose', text)
        self.assertIn(f'| {CPU} | 1500.0 | 1700.0 | 1600.0 | a slower parse on purpose |', text)
        self.assertIn('slow.json', text)
        six = perf_gate.verdict(
            b,
            results([cell(reps=(1800.0,) * 3)]),
            results([cell(reps=(1600.0,) * 3)]),
            decision(accepted()),
        )
        self.assertEqual(six.failures, [])
        self.assertIn('### Slower on purpose', perf_gate.summary(b, six))

    def test_a_timing_regression_still_fails(self):
        b = budgets([entry(budget=1500.0)])
        first = results([cell(reps=(1600.0, 1600.0, 1400.0))])
        again = results([cell(reps=(1600.0, 1550.0, 1600.0))])
        for name, d in (
            ('no accepted file', decision()),
            ('an accepted file of another cell', decision(accepted(window='live'))),
        ):
            with self.subTest(name):
                out = perf_gate.verdict(b, first, again, d)
                self.assertEqual(len(out.failures), 1, out.failures)
                self.assertIn('median of six 1600.0 is over its budget 1500.0', out.failures[0])
        over = perf_gate.verdict(
            b,
            results([cell(reps=(1800.0,) * 3)]),
            results([cell(reps=(1800.0,) * 3)]),
            decision(accepted()),
        )
        self.assertEqual(len(over.failures), 1, over.failures)
        self.assertIn('over its budget 1700.0', over.failures[0])

    def test_an_accepted_budget_at_or_under_the_entrys_is_an_unused_notice(self):
        b = budgets([entry(budget=1500.0)])
        out = perf_gate.compare(b, results([cell()]), accepted=accepted(1500.0))
        self.assertEqual((out.failures, out.slower), ([], []))
        self.assertEqual(len(out.notices), 1, out.notices)
        self.assertIn('slow.json', out.notices[0])
        self.assertIn('changes nothing', out.notices[0])


class Record(unittest.TestCase):
    """The exact counts a verdict measured off their budgets, for the measured file."""

    def recorded(self, out):
        return [(keyof(r['cell']), r['value'], r['reps']) for r in out.recorded]

    def test_a_moved_count_is_recorded_with_its_six_reps_and_fails(self):
        b = budgets([entry('dom', 'exact', 640)])
        first = results([cell('dom', (641,) * 3, kind='count')])
        out = perf_gate.verdict(b, first, results([cell('dom', (641,) * 3, kind='count')]))
        self.assertEqual(self.recorded(out), [(keyof(entry('dom')), 641, [641] * 6)])
        self.assertEqual(len(out.failures), 1, out.failures)
        text = perf_gate.summary(b, out)
        self.assertIn('### Measured moves to check in', text)
        self.assertIn(f'| {DOM} | 640 | 641 | 641, 641, 641, 641, 641, 641 |', text)
        self.assertIn(
            'This run wrote no signed measured file: a merge queue run measures no pull '
            'request head, and a pull request from a fork gets no id token. Take the counts '
            'from a run of the head on a branch of the repository.',
            text,
        )
        self.assertNotIn('--take', text)

    def test_an_unmoved_or_noisy_count_is_not_recorded(self):
        b = budgets([entry('dom', 'exact', 640), entry('reads', 'exact', 2)])
        first = results(
            [cell('dom', (641, 641, 640), kind='count'), cell('reads', (1, 1, 3), kind='count')]
        )
        again = results(
            [cell('dom', (640,) * 3, kind='count'), cell('reads', (3, 3, 1), kind='count')]
        )
        out = perf_gate.verdict(b, first, again)
        self.assertEqual(out.recorded, [])
        self.assertEqual(len(out.failures), 1, out.failures)
        self.assertIn('reads', out.failures[0])

    def test_six_reps_whose_middle_agrees_off_the_budget_record_nothing(self):
        b = budgets([entry('dom', 'exact', 640)])
        first = results([cell('dom', (640, 641, 641), kind='count')])
        again = results([cell('dom', (641, 641, 642), kind='count')])
        out = perf_gate.verdict(b, first, again)
        self.assertEqual(out.recorded, [])
        self.assertEqual(
            out.failures,
            [
                (
                    f'{DOM}: reps [640, 641, 641, 641, 641, 642] disagree (exact budget 640), '
                    'so no exact value can be recorded and the cell may no longer be exact'
                )
            ],
        )
        on_budget = perf_gate.verdict(
            b,
            results([cell('dom', (639, 641, 641), kind='count')]),
            results([cell('dom', (640,) * 3, kind='count')]),
        )
        self.assertEqual((on_budget.recorded, on_budget.failures), ([], []))
        self.assertEqual(on_budget.six[0]['judged'], [640, 640])

    def test_a_stale_key_is_re_measured_and_recorded_though_it_equals_its_budget(self):
        b = budgets([entry('dom', 'exact', 640)])
        first = results([cell('dom', (640,) * 3, kind='count')])
        again = results([cell('dom', (640,) * 3, kind='count')])
        out = perf_gate.verdict(b, first, again, decision(stale=[keyof(entry('dom'))]))
        self.assertEqual(self.recorded(out), [(keyof(entry('dom')), 640, [640] * 6)])
        self.assertEqual(len(out.failures), 1, out.failures)
        self.assertIn('check in the measured file this run wrote', out.failures[0])
        spread = perf_gate.verdict(
            b,
            results([cell('dom', (640, 640, 641), kind='count')]),
            again,
            decision(stale=[keyof(entry('dom'))]),
        )
        # Its middle values are its budget, so recording the budget moves nothing.
        self.assertEqual(
            self.recorded(spread), [(keyof(entry('dom')), 640, [640, 640, 641, 640, 640, 640])]
        )
        self.assertEqual(
            spread.failures,
            [
                (
                    f'{DOM}: reps [640, 640, 641, 640, 640, 640] is stale; '
                    'check in the measured file this run wrote'
                )
            ],
        )
        moved = perf_gate.verdict(
            b,
            results([cell('dom', (641, 641, 640), kind='count')]),
            results([cell('dom', (641,) * 3, kind='count')]),
            decision(stale=[keyof(entry('dom'))]),
        )
        self.assertEqual(moved.recorded, [])
        self.assertEqual(len(moved.failures), 1, moved.failures)
        self.assertIn('so no exact value can be recorded', moved.failures[0])
        missing = perf_gate.verdict(b, first, results([]), decision(stale=[keyof(entry('dom'))]))
        self.assertEqual(self.recorded(missing), [])
        self.assertIn('no reps came back', missing.failures[0])

    def test_an_extension_cell_is_judged_on_its_three_reps_and_its_times_are_reported(self):
        x1 = {'scale': 'x1'}
        b = budgets(
            [
                {**entry('dom', 'exact', 640), **x1},
                {**entry('reads', 'exact', 2), **x1},
                {**entry('nodes', 'exact', 7), **x1},
                {**entry(budget=1500.0), **x1},
            ]
        )
        d = decision(extend=[unit('files', 60, scale='x1')])
        again = results(
            [
                {**cell('dom', (641,) * 3, kind='count'), **x1},
                {**cell('reads', (3, 2, 2), kind='count'), **x1},
                {**cell('nodes', (8, 9, 10), kind='count'), **x1},
                {**cell(reps=(9000.0,) * 3), **x1},
            ]
        )
        out = perf_gate.verdict(b, results([]), again, d)
        x1_dom = keyof({**entry('dom'), **x1})
        self.assertEqual(self.recorded(out), [(x1_dom, 641, [641] * 3)])
        self.assertEqual(len(out.failures), 2, out.failures)
        self.assertTrue(out.failures[0].startswith('files/x1/chromium/desktop/cold/dom:'))
        self.assertEqual(
            out.failures[1],
            (
                'files/x1/chromium/desktop/cold/nodes: reps [8, 9, 10] disagree (exact budget 7), '
                'so no exact value can be recorded and the cell may no longer be exact'
            ),
        )
        self.assertEqual([keyof(c) for c in out.reported], [keyof({**entry(), **x1})])
        missing = perf_gate.verdict(b, results([]), results(again['cells'][1:]), d)
        self.assertIn(
            'files/x1/chromium/desktop/cold/dom: an exact entry of the extension that no shard '
            'measured',
            missing.failures,
        )
        # A cell of a unit the extension does not list is no extension cell.
        self.assertEqual(perf_gate.verdict(b, results([]), again, decision()).failures, [])

    def test_merge_keeps_the_cpu_model_of_each_cells_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, model in enumerate(('AMD EPYC 7763', 'Intel Xeon 8370C')):
                p = pathlib.Path(tmp) / f'{i}.json'
                doc = {**results([{**cell(), 'scale': f'x{i}'}]), 'cpu_model': model}
                p.write_text(json.dumps(doc))
                paths.append(p)
            merged = perf_gate.merge(paths)
        self.assertEqual(
            [c['cpu_model'] for c in merged['cells']], ['AMD EPYC 7763', 'Intel Xeon 8370C']
        )


class Extension(unittest.TestCase):
    def test_a_scenario_extends_to_the_full_units_its_set_does_not_cover(self):
        units = perf_gate.planned_windows(PLAN['sets']['pr']['cells'])
        self.assertEqual(
            perf_gate.extension(PLAN, units, {'files'}), [unit('files', 60, scale='x1')]
        )
        self.assertEqual(perf_gate.extension(PLAN, units, set()), [])
        cold = perf_gate.planned_windows([unit('files', 80, windows=['cold'])])
        self.assertEqual(
            perf_gate.extension(PLAN, cold, {'files'}),
            [unit('files', 80), unit('files', 60, scale='x1')],
        )
        full = perf_gate.planned_windows(PLAN['sets']['full']['cells'])
        self.assertEqual(perf_gate.extension(PLAN, full, {'files'}), [])
        with self.assertRaisesRegex(TypeError, 'no full set'):
            perf_gate.extension({'sets': {'pr': PLAN['sets']['pr']}}, units, {'files'})

    def test_a_retired_cell_the_extension_measured_fails(self):
        retired = [{**CELL, 'scale': 'x1', 'metric': 'dom', 'reason': 'the list is virtual now'}]
        b = budgets([entry()], retired=retired)
        x1 = {**cell('dom', (640,) * 3, kind='count'), 'scale': 'x1'}
        extend = [unit('files', 60, scale='x1')]
        out = perf_gate.verdict(b, results([cell()]), results([x1]), decision(extend=extend))
        why = "retired, yet a shard measured it; a measured cell's tier is the scheduled calibration's"
        self.assertEqual(out.failures, [f'files/x1/chromium/desktop/cold/dom: {why}'])
        held = decision(extend=extend, reported=[{'metric': 'dom'}])
        self.assertEqual(perf_gate.verdict(b, results([cell()]), results([x1]), held).failures, [])
        # A metric the extension no longer emits is a retirement.
        none = perf_gate.verdict(b, results([cell()]), results([]), decision(extend=extend))
        self.assertEqual(none.failures, [])


# Count cells of marotte's calibrations 43591500420 and 43598120088 that every
# rep of the other runs agreed on, as one held-out run and the next run
# measured them: (cell, the agreed count, its reps, the re-measure's reps).
HELD_OUT = (
    ('chat-long/x10/chromium/desktop/live/sse_frames', 226, (227, 226, 226), (226,) * 3),
    ('history-runs/x10/chromium/desktop/cold/reads:GET /api/chats', 2, (2, 1, 2), (2,) * 3),
    (
        'run-page/x10/chromium/desktop/back/reads:GET /api/runs/{run}?view=glance',
        2,
        (3, 2, 2),
        (2,) * 3,
    ),
    (
        'git-prs/x1/chromium/phone/cold/max_bytes:GET /api/forges/inventory',
        69180,
        (69180, 69180, 68200),
        (69180,) * 3,
    ),
    ('chat-stream/x1/chromium/phone/back/dom', 6570, (6570, 8102, 6570), (6570,) * 3),
    ('chat-long/x1/chromium/phone/cold/sse_frames', 3, (4, 4, 3), (3,) * 3),
    ('run-page/x1/chromium/desktop/cold/sse_frames', 3, (4, 4, 3), (3,) * 3),
    ('tabs/x1/chromium/desktop/cold/sse_frames', 3, (4, 3, 4), (3,) * 3),
)


def run_doc(reps_by_cell, seconds=70.0):
    """One calibration run's results: {metric or (metric, kind): reps}."""
    cells = []
    for spec, reps in reps_by_cell.items():
        metric, kind = spec if isinstance(spec, tuple) else (spec, 'time')
        cells.append(cell(metric, reps, kind=kind))
    doc = results(cells)
    doc['seconds'] = [
        {**{k: CELL[k] for k in ('scenario', 'scale', 'engine', 'width')}, 'seconds': seconds}
    ]
    return doc


def runs_with(metric, base, slow, slow_runs, n=10, kind='time'):
    return [
        run_doc({(metric, kind): (slow,) * 3 if i in slow_runs else (base,) * 3}) for i in range(n)
    ]


def calibrate(runs, **kw):
    return perf_gate.calibrate(
        runs,
        **kw,
        headroom={'cpu_ms': 1.5, 'heap_mb': 1.25, 'rss_mb': 1.25},
        floor={'cpu_ms': 50, 'heap_mb': 1},
        repo=REPO,
        commit=HEAD,
        runner=RUNNER,
        run_id='111',
        run_url=RUN_URL + '111',
    )


def only_cell(proposed, metric='cpu_ms'):
    (found,) = [c for c in proposed['cells'] if c['metric'] == metric]
    return found


class Calibrate(unittest.TestCase):
    def test_one_slow_run_trips_only_the_first_stage_and_stays_gated(self):
        p = calibrate(runs_with('cpu_ms', 1000.0, 1600.0, {3}))
        c = only_cell(p)
        self.assertEqual(c['tier'], 'gated')
        self.assertNotIn('headroom', c)
        self.assertEqual(c['baseline'], 1000.0)
        self.assertEqual(p['whole_pr']['first_stage_runs'], [3])
        self.assertEqual(p['whole_pr']['widened'], [])
        self.assertEqual(p['whole_pr']['demoted'], [])

    def test_a_second_stage_trip_widens_by_the_smallest_step_that_clears_it(self):
        p = calibrate(runs_with('cpu_ms', 1000.0, 1580.0, {3, 4}))
        c = only_cell(p)
        self.assertEqual(c['tier'], 'gated')
        self.assertEqual(c['headroom'], 1.6)
        self.assertEqual([w['headroom'] for w in p['whole_pr']['widened']], [1.6])

    def test_a_cell_still_tripping_at_the_cap_is_reported(self):
        for metric, base, slow in (('cpu_ms', 1000.0, 2500.0), ('heap_mb', 20.0, 35.0)):
            with self.subTest(metric=metric):
                p = calibrate(runs_with(metric, base, slow, {3, 4}))
                c = only_cell(p, metric)
                self.assertEqual(c['tier'], 'reported')
                self.assertNotIn('headroom', c)
                self.assertEqual(len(p['whole_pr']['demoted']), 1)

    def test_heap_widens_up_to_its_own_cap(self):
        p = calibrate(runs_with('heap_mb', 20.0, 29.5, {3, 4}))
        c = only_cell(p, 'heap_mb')
        self.assertEqual((c['tier'], c['headroom']), ('gated', 1.5))

    def test_a_calibration_needs_ten_runs(self):
        with self.assertRaisesRegex(ValueError, 'needs 10 runs, not 9'):
            calibrate(runs_with('cpu_ms', 1000.0, 1580.0, {1, 2}, n=9))

    def test_a_cell_plan_json_reports_is_reported_though_its_reps_agree(self):
        runs = [
            run_doc({('dom', 'count'): (640,) * 3, ('max_bytes:GET /x', 'count'): (9,) * 3})
            for _ in range(10)
        ]
        cases = {
            'a metric': ([{'metric': 'dom'}], 'reported'),
            'its window': ([{'metric': 'dom', 'window': 'cold'}], 'reported'),
            'another window': ([{'metric': 'dom', 'window': 'live'}], 'exact'),
        }
        for name, (patterns, tier) in cases.items():
            with self.subTest(name):
                p = calibrate(runs, reported=patterns)
                self.assertEqual(only_cell(p, 'dom')['tier'], tier)
                self.assertEqual(only_cell(p, 'max_bytes:GET /x')['tier'], 'exact')

    def test_an_exact_count_gates_only_when_the_ten_agree(self):
        same = calibrate([run_doc({('dom', 'count'): (640, 640, 640)}) for _ in range(10)])
        self.assertEqual(only_cell(same, 'dom')['tier'], 'exact')
        runs = [run_doc({('dom', 'count'): (640, 640, 640 + (i == 7))}) for i in range(10)]
        self.assertEqual(only_cell(calibrate(runs), 'dom')['tier'], 'reported')

    def test_a_report_only_metric_is_reported(self):
        p = calibrate(
            [run_doc({('frame_p95_ms', 'report'): (16.0, 17.0, 16.0)}) for _ in range(10)]
        )
        self.assertEqual(only_cell(p, 'frame_p95_ms')['tier'], 'reported')

    def test_the_file_names_its_provenance_and_the_seconds_per_cell(self):
        runs = [run_doc({'cpu_ms': (1000.0,) * 3}, seconds=60.0 + i) for i in range(10)]
        p = calibrate(runs)
        self.assertEqual(
            (p['schema'], p['kind'], p['repo'], p['commit'], p['runner'], p['runs'], p['run_id']),
            (2, 'calibration', REPO, HEAD, RUNNER, 10, '111'),
        )
        self.assertEqual(p['seconds'][0]['seconds'], 64.5)
        self.assertEqual(p['cpu_models'], ['AMD EPYC 7763'])


def calibration_file(cells, runs=10, **kw):
    doc = {
        'schema': 1,
        'repo': REPO,
        'commit': HEAD,
        'runner': RUNNER,
        'runs': runs,
        'run_id': '111',
        'run_url': RUN_URL + '111',
        'cells': cells,
        'whole_pr': {'first_stage_runs': [], 'widened': [], 'demoted': []},
    }
    doc.update(kw)
    return doc


def cal_cell(metric='cpu_ms', baseline=1000.0, tier='gated', runs=10, slow_runs=(), **kw):
    reps = [[1600.0] * 3 if i in slow_runs else [baseline] * 3 for i in range(runs)]
    c = {
        **CELL,
        'metric': metric,
        'kind': 'time',
        'baseline': baseline,
        'spread': 1.0,
        'reps_by_run': reps,
    }
    if tier is not None:
        c['tier'] = tier
    c.update(kw)
    return c


def measured_file(cells, commit='pr-head', run_id='900', attempt='1', **kw):
    """A perf-ci verdict's measured file of the pull request head commit."""
    doc = {
        'schema': 2,
        'kind': 'measured',
        'repo': REPO,
        'commit': commit,
        'measured_sha': 'merge',
        'runner': RUNNER,
        'run_id': run_id,
        'run_attempt': attempt,
        'run_url': f'{MEASURED_URL}{run_id}/attempts/{attempt}',
        'workflow_repository': REPO,
        'cpu_models': ['AMD EPYC 7763'],
        'cells': cells,
    }
    doc.update(kw)
    return doc


def m_cell(value, metric='dom', **kw):
    return {**CELL, 'metric': metric, 'value': value, 'reps': [value] * 6, **kw}


MEASURED_SIGNER = perf_gate.Signer(
    identity_regexp=perf_gate.MEASURED_IDENTITY, workflow_repository=REPO
)


class FakeGit:
    """perf_gate.Git over a made-up history. lineage holds (ancestor, descendant) pairs
    besides each commit's own; added maps a calibration file's name to the commit that
    added it, the head when unnamed, and a commit's parent `c^` has c's ancestors; same
    holds the (measured, added) pairs whose code matches, and same_raises is what
    same_change raises; a commit in broken is one git cannot answer for. show reads a
    base file from shown by name, else the file on disk, else a source of ROOT, the
    commit every commit descends from; has finds every name but those in absent."""

    def __init__(self, lineage=()):
        self.lineage = set(lineage)
        self.added = {}
        self.same = set()
        self.same_raises = None
        self.broken = set()
        self.shown = {}
        self.absent = set()

    def has(self, sha, path):
        if sha in self.broken:
            raise perf_gate.GitError(f'git ls-tree exited 128: no such commit {sha}')
        return pathlib.Path(path).name not in self.absent

    def commit(self, sha):
        if sha in self.broken:
            raise perf_gate.GitError(f'git fetch exited 128: no such commit {sha}')
        return sha

    def is_ancestor(self, sha, of):
        of = of.removesuffix('^')
        if {sha, of} & self.broken:
            raise perf_gate.GitError('git merge-base exited 128')
        return sha in (of, ROOT) or (sha, of) in self.lineage

    def show(self, sha, path):
        p = pathlib.Path(path)
        if self.shown.get(p.name) is perf_gate.GitError:
            raise perf_gate.GitError(f'git cat-file exited 128: {sha}:{path} does not exist')
        if p.name in self.shown:
            return self.shown[p.name]
        return p.read_text() if p.is_file() else json.dumps({'commit': ROOT})

    def added_by(self, path, head):
        return self.added.get(pathlib.Path(path).name, head)

    def same_change(self, measured, added):
        if self.same_raises:
            raise self.same_raises
        return (measured, added) in self.same


class Provenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.verified = []
        self.signers = []
        self.bad_bundles = set()
        # The (identity, workflow repository) of the certificate each stem's bundle
        # carries, matched as cosign matches a Signer; a stem it lacks verifies.
        self.signed_by = {}
        self.git = FakeGit()
        self.stale = set()
        accepted = tempfile.TemporaryDirectory()
        self.addCleanup(accepted.cleanup)
        self.accepted = pathlib.Path(accepted.name)
        self.checked = None

    def write(self, stem, doc):
        if '-' not in stem:
            doc = {**doc, 'run_id': stem, 'run_url': RUN_URL + stem}
        (self.dir / f'{stem}.json').write_text(json.dumps(doc))
        (self.dir / f'{stem}.sigstore.json').write_text('{}')

    def verify(self, blob, bundle, signer):
        self.verified.append((pathlib.Path(blob).name, pathlib.Path(bundle).name))
        self.signers.append(signer)
        if pathlib.Path(bundle).name in self.bad_bundles:
            return 'bundle does not verify'
        made = self.signed_by.get(pathlib.Path(blob).stem)
        if made is None:
            return None
        identity, repository = made
        ok = (
            signer.identity in (None, identity)
            and (signer.identity_regexp is None or re.match(signer.identity_regexp, identity))
            and signer.workflow_repository in (None, repository)
        )
        return None if ok else f'no matching CertificateIdentity for {identity} in {repository}'

    def check(self, head, base, base_sha=HEAD, verify=None):
        self.checked = perf_gate.check_provenance(
            head,
            base,
            sources=self.dir,
            accepted=self.accepted,
            repo=REPO,
            head_sha=HEAD,
            base_sha=base_sha,
            verify=verify or self.verify,
            git=self.git,
        )
        self.stale = self.checked.stale
        return self.checked.errors

    def accept(self, name, cells, reason='a slower parse on purpose'):
        """An accepted file, added after the calibrations of HEAD the tests check in."""
        self.git.added.setdefault(name, 'ovr')
        (self.accepted / name).write_text(json.dumps({'reason': reason, 'cells': cells}))

    def test_an_accepted_slowdown_is_read_with_its_reason_and_file(self):
        self.write('111', calibration_file([cal_cell()]))
        self.accept('slow.json', [{**CELL, 'metric': 'cpu_ms', 'budget': 1700.0}])
        self.assertEqual(self.unchanged(entry()), [])
        self.assertEqual(
            self.checked.accepted,
            {
                keyof(entry()): {
                    'budget': 1700.0,
                    'reason': 'a slower parse on purpose',
                    'file': 'slow.json',
                }
            },
        )

    def test_an_accepted_file_names_gated_entries_once_with_one_reason_line(self):
        dom_cell = cal_cell('dom', 640.0, 'exact', kind='count')
        self.write('111', calibration_file([cal_cell(), dom_cell]))
        dom = {**CELL, 'metric': 'dom', 'budget': 700}
        cpu = {**CELL, 'metric': 'cpu_ms', 'budget': 1700.0}
        exact = entry('dom', 'exact', 640.0)
        cases = {
            'an exact entry': ({'a.json': ([dom], 'x')}, 'has no gated entry'),
            'no entry': ({'a.json': ([{**cpu, 'window': 'live'}], 'x')}, 'has no gated entry'),
            'two files': ({'a.json': ([cpu], 'x'), 'b.json': ([cpu], 'y')}, 'also accepted by'),
            'a blank reason': ({'a.json': ([cpu], ' ')}, 'reason'),
            'two reason lines': ({'a.json': ([cpu], 'one\ntwo')}, 'reason'),
            'no cells': ({'a.json': ([], 'x')}, 'cells'),
            'a budget of 0': ({'a.json': ([{**cpu, 'budget': 0}], 'x')}, 'budget'),
            'an unknown cell field': ({'a.json': ([{**cpu, 'tier': 'gated'}], 'x')}, 'tier'),
            'no json name': ({'a.txt': ([cpu], 'x')}, 'not an accepted file'),
        }
        for name, (files, want) in cases.items():
            with self.subTest(name):
                for p in self.accepted.iterdir():
                    p.unlink()
                for file, (cells, reason) in files.items():
                    self.accept(file, cells, reason)
                errors = self.unchanged(entry(), {**exact, 'source': RUN_URL + '111'})
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith('accepted/'), errors)
                self.assertIn(want, errors[0])
        (self.accepted / 'a.json').write_text('[]')
        self.assertIn('must be a JSON object', ' '.join(self.unchanged(entry())))

    def test_an_accepted_slowdown_expires_when_a_calibration_holding_it_measured_the_cell(self):
        self.git.added = {'slow.json': 'ovr'}
        self.git.lineage = {('ovr', HEAD), ('0ld', HEAD), ('0ld', 'ovr')}
        self.write('111', calibration_file([cal_cell()], commit='0ld'))
        self.accept('slow.json', [{**CELL, 'metric': 'cpu_ms', 'budget': 1700.0}])
        self.assertEqual(self.unchanged(entry()), [])
        # The scheduled dev calibration after the pull request merged.
        for p in self.dir.iterdir():
            p.unlink()
        self.git.lineage |= {('ovr', 'new'), ('new', HEAD), ('0ld', 'new')}
        self.write('222', calibration_file([cal_cell()], commit='new'))
        errors = self.unchanged(entry(source=RUN_URL + '222'))
        self.assertEqual(
            errors,
            [
                (
                    f'accepted/slow.json: {CPU}: calibration 222 measured this cell at new, '
                    'which holds the override; delete accepted/slow.json'
                )
            ],
        )
        self.assertEqual(self.checked.accepted, {})

    def test_the_moved_exact_counts_list_each_change_with_its_reason_and_check(self):
        self.pr_measured(m_cell(2056))
        self.check(budgets([self.moved(2056)]), self.moved_from())
        self.assertEqual(
            self.checked.moves,
            [
                {
                    'key': keyof(entry('dom')),
                    'base': 2055.0,
                    'head': 2056,
                    'source': f'{MEASURED_URL}900/attempts/1',
                    'reason': 'one more node in the view',
                    'refused': [],
                }
            ],
        )
        self.check(budgets([self.moved(2057)]), self.moved_from())
        (move,) = self.checked.moves
        self.assertEqual(move['head'], 2057)
        self.assertIn('is not what run', move['refused'][0])
        text = perf_gate.summary(budgets([]), perf_gate.Outcome(exact_moves=self.checked.moves))
        self.assertIn('### Exact counts this pull request moves', text)
        self.assertIn(f'| {DOM} | 2055.0 | 2057 |', text)
        self.assertIn('one more node in the view', text)
        self.assertIn('take the value with perf_gate.py --take', text)

    def test_a_recorded_verdict_is_a_measured_file_the_gate_accepts(self):
        b = budgets([entry('dom', 'exact', 2055.0)])
        out = perf_gate.verdict(
            b,
            results([cell('dom', (2056,) * 3, kind='count')]),
            results([cell('dom', (2056,) * 3, kind='count')]),
        )
        doc = perf_gate.record(
            out,
            repo=REPO,
            commit='pr-head',
            measured_sha='merge',
            run_id='900',
            run_attempt='1',
            run_url=f'{MEASURED_URL}900/attempts/1',
            workflow_repository=REPO,
            cpu_models=['AMD EPYC 7763'],
        )
        self.assertEqual(doc, measured_file([m_cell(2056)]))
        self.git.lineage.add(('pr-head', HEAD))
        self.write('900-1', doc)
        self.assertEqual(self.check(budgets([self.moved(2056)]), self.moved_from()), [])

    def dom(self, run_id, baseline, commit, *more):
        exact = {'metric': 'dom', 'kind': 'count', 'tier': 'exact'}
        cells = [cal_cell(**exact, baseline=baseline), *more]
        self.write(run_id, calibration_file(cells, commit=commit))

    def exact(self, run_id, baseline, **kw):
        return entry(metric='dom', tier='exact', baseline=baseline, source=RUN_URL + run_id, **kw)

    def two_commits(self):
        """Calibration 200 of an older commit and 100 of the head, dispatched later and
        earlier, so run-id order says the opposite of commit order. The x1 entry names 100."""
        self.git.lineage = {('0ld', HEAD)}
        self.dom('200', 4384.0, '0ld')
        x1 = cal_cell(metric='dom', kind='count', tier='exact', baseline=1.0, scale='x1')
        self.dom('100', 4400.0, HEAD, x1)
        return self.exact('100', 1.0, scale='x1')

    def unchanged(self, *entries):
        b = budgets(list(entries))
        return self.check(b, b)

    def test_an_entry_sourced_from_an_ancestor_of_a_calibration_of_its_cell_fails(self):
        x1 = self.two_commits()
        errors = self.check(budgets([self.exact('200', 4384.0), x1]), budgets([]))
        self.assertTrue(any('take the entry from 100' in x for x in errors), errors)

    def test_an_entry_sourced_from_the_newest_commit_of_its_cell_passes(self):
        self.git.lineage = {('0ld', HEAD)}
        self.dom('200', 4384.0, '0ld', cal_cell(baseline=900.0))
        self.dom('100', 4400.0, HEAD)
        head = budgets([self.exact('100', 4400.0), entry(baseline=900.0, source=RUN_URL + '200')])
        self.assertEqual(self.check(head, budgets([])), [])

    def test_calibrations_of_commits_neither_descends_from_pass(self):
        self.git.lineage = {('0ld', HEAD), ('sibling', HEAD)}
        self.dom('200', 4384.0, '0ld')
        x1 = cal_cell(metric='dom', kind='count', tier='exact', baseline=1.0, scale='x1')
        self.dom('100', 4400.0, 'sibling', x1)
        self.assertEqual(
            self.unchanged(self.exact('200', 4384.0), self.exact('100', 1.0, scale='x1')), []
        )

    def test_an_unchanged_entry_a_newer_calibration_supersedes_fails(self):
        x1 = self.two_commits()
        errors = self.unchanged(self.exact('200', 4384.0), x1)
        self.assertTrue(any('take the entry from 100' in x for x in errors), errors)

    def test_a_newer_calibration_that_did_not_measure_the_cell_supersedes_nothing(self):
        self.git.lineage = {('0ld', HEAD)}
        self.dom('200', 4384.0, '0ld', cal_cell(baseline=900.0))
        self.dom('100', 4400.0, HEAD)
        gated = entry(baseline=900.0, source=RUN_URL + '200')
        self.assertEqual(self.unchanged(gated, self.exact('100', 4400.0)), [])

    def test_the_newest_of_several_later_calibrations_is_named(self):
        self.git.lineage = {('a', 'b'), ('a', HEAD), ('b', HEAD)}
        x1 = cal_cell(metric='dom', kind='count', tier='exact', baseline=1.0, scale='x1')
        self.dom('100', 4384.0, 'a')
        self.dom('200', 4392.0, 'b', x1)
        self.dom('300', 4400.0, HEAD, {**x1, 'scale': 'x2'})
        errors = self.unchanged(
            self.exact('100', 4384.0),
            self.exact('200', 1.0, scale='x1'),
            self.exact('300', 1.0, scale='x2'),
        )
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('take the entry from 300', errors[0])

    def test_a_squash_merged_calibration_is_placed_at_its_squash_commit(self):
        # pr-tip is in no commit of the head after its pull request squash-merged as squash.
        self.git.lineage = {('squash', HEAD)}
        self.git.added = {'200.json': 'squash'}
        self.git.same = {('pr-tip', 'squash')}
        x1 = self.two_commits()
        self.git.lineage.add(('squash', HEAD))
        self.dom('200', 4384.0, 'pr-tip')
        errors = self.unchanged(self.exact('200', 4384.0), x1)
        self.assertTrue(any('take the entry from 100' in x for x in errors), errors)
        self.git.lineage = {('0ld', 'squash'), ('0ld', HEAD), ('squash', HEAD)}
        self.dom(
            '100',
            4400.0,
            '0ld',
            cal_cell(metric='dom', kind='count', tier='exact', baseline=1.0, scale='x1'),
        )
        self.assertEqual(
            self.unchanged(self.exact('200', 4384.0), self.exact('100', 1.0, scale='x1')), []
        )

    def test_a_squash_merge_holding_other_code_than_its_calibration_measured_fails(self):
        self.git.added = {'200.json': 'squash'}
        self.git.lineage = {('squash', HEAD)}
        self.dom('200', 4384.0, 'pr-tip')
        errors = self.unchanged(self.exact('200', 4384.0))
        tail = 'take entries only from a calibration of the default branch'
        self.assertTrue(any(x.startswith('calibration 200:') and tail in x for x in errors), errors)
        changed = self.check(budgets([self.exact('200', 4384.0)]), budgets([]))
        self.assertTrue(
            any(x.startswith('calibration 200:') and tail in x for x in changed), changed
        )

    def moved(self, value, stem='900-1', reason='one more node in the view', **kw):
        run_id, attempt = stem.split('-')
        url = f'{MEASURED_URL}{run_id}/attempts/{attempt}'
        return entry(metric='dom', tier='exact', baseline=value, source=url, reason=reason, **kw)

    def pr_measured(self, *cells, stem='900-1', commit='pr-head', **kw):
        """stem's measured file of commit, checked in on top of it."""
        self.git.lineage.add((commit, HEAD))
        run_id, attempt = stem.split('-')
        doc = measured_file(list(cells), commit=commit, run_id=run_id, attempt=attempt, **kw)
        self.write(stem, doc)

    def moved_from(self, base_value=2055.0):
        return budgets([self.exact('100', base_value)])

    def test_an_exact_change_from_the_pull_requests_own_measured_file_passes(self):
        self.pr_measured(m_cell(2056))
        head = budgets([self.moved(2056)])
        self.assertEqual(self.check(head, self.moved_from(), base_sha='ba5e'), [])
        self.assertEqual(self.stale, set())
        self.assertEqual(self.verified, [('900-1.json', '900-1.sigstore.json')])
        self.assertEqual(self.signers, [MEASURED_SIGNER])

    def test_a_hand_edited_exact_value_fails_naming_the_measured_one(self):
        self.pr_measured(m_cell(2056))
        errors = self.check(budgets([self.moved(2057)]), self.moved_from())
        self.assertEqual(
            errors,
            [
                (
                    f'{DOM}: baseline 2057 is not what run {MEASURED_URL}900/attempts/1 measured '
                    '(2056); take the value with perf_gate.py --take'
                )
            ],
        )
        self.assertEqual(self.stale, {keyof(self.moved(2057))})

    def test_a_measured_file_of_another_head_fails_unless_patch_equivalent(self):
        # The merge ref's own change is not the measured head's: a push after the run.
        self.write('900-1', measured_file([m_cell(2056)], commit='other-head'))
        head = budgets([self.moved(2056)])
        errors = self.check(head, self.moved_from())
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith('measured file 900-1: '), errors)
        self.assertIn('take the measured file this run wrote', errors[0])
        self.assertEqual(self.stale, {keyof(self.moved(2056))})
        self.git.same = {('other-head', HEAD)}
        self.assertEqual(self.check(head, self.moved_from()), [])
        self.assertEqual(self.stale, set())

    def test_a_measured_value_without_verifiable_provenance_fails_closed(self):
        head = budgets([self.moved(2056)])
        perf_ci = 'https://github.com/cplieger/ci/.github/workflows/perf-ci.yaml@refs/heads/main'
        # stale: a fresh measured file of this head cures the refusal, so compare
        # re-measures the cell; a bundle that does not verify is no such refusal.
        cases = {
            'no file': ({}, lambda: self.dir.joinpath('900-1.json').unlink(), 'checked in'),
            'no bundle': (
                {},
                lambda: self.dir.joinpath('900-1.sigstore.json').unlink(),
                'checked in',
            ),
            'a bundle that fails': (
                {},
                lambda: self.bad_bundles.add('900-1.sigstore.json'),
                'the bundle does not verify',
                False,
            ),
            'signed by perf-calibrate': (
                {},
                lambda: self.signed_by.update(
                    {'900-1': (perf_gate.CALIBRATION_IDENTITY, perf_gate.CI_REPO)}
                ),
                'the bundle does not verify',
                False,
            ),
            'signed in another repository': (
                {},
                lambda: self.signed_by.update({'900-1': (perf_ci, 'cplieger/other')}),
                'the bundle does not verify',
                False,
            ),
            'another repository': ({'repo': 'cplieger/other'}, None, 'repo is'),
            'another runner': ({'runner': 'self-hosted'}, None, 'runner is'),
            'a run outside the repository and ci': (
                {'workflow_repository': 'cplieger/other'},
                None,
                'workflow_repository',
            ),
            'a calibration doc': ({'kind': 'calibration'}, None, 'kind'),
            'another schema': ({'schema': 1}, None, 'schema'),
            'another run': ({'run_id': '901'}, None, 'run_id'),
            'another attempt': ({'run_attempt': '2'}, None, 'run_attempt'),
            'a cell without a value': (
                {'cells': [{**m_cell(2056), 'value': None}]},
                None,
                'value',
            ),
        }
        for name, (doc, setup, want, *curable) in cases.items():
            with self.subTest(name):
                self.bad_bundles.clear()
                self.signed_by.clear()
                self.write('900-1', {**measured_file([m_cell(2056)]), **doc})
                self.git.lineage.add(('pr-head', HEAD))
                if setup:
                    setup()
                errors = self.check(head, self.moved_from())
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith('measured file 900-1: '), errors)
                self.assertIn(want, errors[0])
                stale = {keyof(self.moved(2056))} if curable != [False] else set()
                self.assertEqual(self.stale, stale)

    def test_a_calibration_without_verifiable_provenance_fails_closed(self):
        self.assertEqual(
            perf_gate.CALIBRATION_SIGNER,
            perf_gate.Signer(
                identity=perf_gate.CALIBRATION_IDENTITY, workflow_repository=perf_gate.CI_REPO
            ),
        )
        b = budgets([entry()])
        cases = {
            'signed in another repository': (
                lambda: self.signed_by.update(
                    {'111': (perf_gate.CALIBRATION_IDENTITY, 'cplieger/other')}
                ),
                'calibration 111: the bundle does not verify',
            ),
            'a run URL of another run': (
                lambda: self.retarget('111', run_url=RUN_URL + '999'),
                f"calibration 111: run_url is '{RUN_URL}999', not '{RUN_URL}111'",
            ),
            'no run URL': (
                lambda: self.retarget('111', run_url=None),
                f"calibration 111: run_url is None, not '{RUN_URL}111'",
            ),
        }
        for name, (setup, want) in cases.items():
            with self.subTest(name):
                self.signed_by.clear()
                self.write('111', calibration_file([cal_cell()]))
                setup()
                errors = self.check(b, b)
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith(want), errors)

    def retarget(self, stem, **fields):
        path = self.dir / f'{stem}.json'
        path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))

    def test_a_calibration_refusal_marks_no_exact_key_stale(self):
        self.write('111', calibration_file([cal_cell('dom', 640.0, 'exact', kind='count')], runs=5))
        b = budgets([entry('dom', 'exact', 640.0)])
        errors = self.check(b, b)
        self.assertEqual(errors, ['calibration 111: 5 runs decide no tier; a calibration needs 10'])
        self.assertEqual(self.stale, set())

    def test_a_source_checked_without_cosign_fails_closed(self):
        self.pr_measured(m_cell(2056))
        with unittest.mock.patch.dict(os.environ, {'PATH': ''}):
            errors = self.check(
                budgets([self.moved(2056)]), self.moved_from(), verify=perf_gate.cosign_verify
            )
        self.assertEqual(
            errors, ['measured file 900-1: the bundle does not verify: cosign is not installed']
        )
        self.assertEqual(self.stale, set())

    def test_a_source_url_names_the_repository_whose_run_wrote_the_file(self):
        self.pr_measured(m_cell(2056))
        moved = self.moved(2056)
        elsewhere = {
            **moved,
            'source': 'https://github.com/cplieger/ci/actions/runs/900/attempts/1',
        }
        errors = self.check(budgets([elsewhere]), self.moved_from())
        self.assertEqual(
            errors,
            [
                (
                    f'{DOM}: source https://github.com/cplieger/ci/actions/runs/900/attempts/1 is not '
                    f'the run that wrote 900-1.json, {MEASURED_URL}900/attempts/1'
                )
            ],
        )
        self.pr_measured(
            m_cell(2056), workflow_repository='cplieger/ci', run_url=elsewhere['source']
        )
        self.assertEqual(self.check(budgets([elsewhere]), self.moved_from()), [])
        self.assertEqual(
            self.signers[-1],
            dataclasses.replace(MEASURED_SIGNER, workflow_repository='cplieger/ci'),
        )

    def test_a_measured_file_never_adds_an_entry_or_moves_a_tier(self):
        self.pr_measured(m_cell(2056))
        self.assertEqual(
            self.check(budgets([self.moved(2056)]), budgets([])),
            [f'{DOM}: measured file 900-1 moves the value of an exact entry, and adds none'],
        )
        gated = entry('dom', 'gated', 2055.0, budget=2100.0, source=RUN_URL + '100')
        self.assertEqual(
            self.check(budgets([self.moved(2056)]), budgets([gated])),
            [f'{DOM}: measured file 900-1 moves the value of an exact entry, not a gated one'],
        )

    def test_a_pull_request_branch_calibration_moves_no_entry(self):
        self.git.lineage = {('pr-head', HEAD)}
        self.dom('200', 2056.0, 'pr-head')
        head = budgets([self.exact('200', 2056.0)])
        errors = self.check(head, self.moved_from(), base_sha='ba5e')
        self.assertEqual(
            errors,
            [
                (
                    f'{DOM}: calibration 200 measured pr-head, which the base ba5e does not hold; '
                    'a calibration of a pull request moves no entry'
                )
            ],
        )
        self.assertEqual(self.stale, {keyof(self.exact('200', 2056.0))})
        self.assertEqual(self.check(head, head, base_sha='ba5e'), [])
        self.git.lineage.add(('pr-head', 'ba5e'))
        self.assertEqual(self.check(head, self.moved_from(), base_sha='ba5e'), [])

    def test_the_newest_source_spans_calibrations_and_measured_files(self):
        x1 = cal_cell(metric='dom', kind='count', tier='exact', baseline=1.0, scale='x1')
        x1_entry = self.exact('100', 1.0, scale='x1')
        self.git.lineage = {('0ld', HEAD), ('0ld', 'pr-head')}
        self.dom('100', 2055.0, '0ld', x1)
        self.pr_measured(m_cell(2056), m_cell(5, scale='x2'))
        x2 = self.moved(5, scale='x2')
        errors = self.unchanged(self.exact('100', 2055.0), x1_entry, x2)
        self.assertEqual(
            errors,
            [
                (
                    f'{DOM}: measured file 900-1 measured this cell at pr-head, which descends from '
                    "source calibration 100's 0ld; take the entry from 900-1"
                )
            ],
        )
        self.assertEqual(self.unchanged(self.moved(2056), x1_entry, x2), [])
        # The scheduled calibration of the merged change measured it again.
        self.git.lineage = {('pr-head', '0ld'), ('pr-head', HEAD), ('0ld', HEAD)}
        errors = self.unchanged(self.moved(2056), x1_entry, x2)
        self.assertEqual(
            errors,
            [
                (
                    f'{DOM}: calibration 100 measured this cell at 0ld, which descends from '
                    "source measured file 900-1's pr-head; take the entry from 100"
                )
            ],
        )

    def base_holds(self, stem):
        """stem's file as the base tree holds it, deleted from the head's sources."""
        for name in (f'{stem}.json', f'{stem}.sigstore.json'):
            self.git.shown[name] = (self.dir / name).read_text()
            (self.dir / name).unlink()

    def older(self, head_label, at, base_label, place):
        return (
            f"{head_label} measured {at}, older than the base's {base_label} at {place}; "
            'an entry moves only to a newer source'
        )

    def test_an_entry_never_moves_to_a_source_older_than_its_base_entrys(self):
        # Calibration 100 measured 0ld, and the base's 200 measured new, which descends from it.
        self.git.lineage = {('0ld', 'new'), ('0ld', HEAD), ('new', HEAD)}
        dom = {'metric': 'dom', 'kind': 'count'}
        self.write(
            '100',
            calibration_file(
                [cal_cell(**dom, tier='reported', baseline=640.5), cal_cell(baseline=1400.0)],
                commit='0ld',
            ),
        )
        self.write(
            '200',
            calibration_file(
                [cal_cell(**dom, tier='exact', baseline=640.0), cal_cell()], commit='new'
            ),
        )
        base = budgets([self.exact('200', 640.0), entry(source=RUN_URL + '200')])
        self.base_holds('200')
        demoted = entry('dom', 'reported', 640.5, source=RUN_URL + '100')
        raised = entry(baseline=1400.0, source=RUN_URL + '100')
        cases = {
            'an exact cell demoted to reported': (demoted, DOM),
            'a gated baseline raised': (raised, CPU),
        }
        for name, (e, cell_name) in cases.items():
            with self.subTest(name):
                kept = [x for x in base['budgets'] if keyof(x) != keyof(e)]
                errors = self.check(budgets([e, *kept]), base)
                self.assertIn(
                    f'{cell_name}: '
                    + self.older('calibration 100', '0ld', 'calibration 200', 'new'),
                    errors,
                )
                self.assertEqual(self.stale, set())

    def test_an_exact_value_never_moves_back_to_an_older_measured_file(self):
        self.git.lineage = {('m-old', 'm-new'), ('m-old', HEAD), ('m-new', HEAD)}
        self.pr_measured(m_cell(2055), stem='900-1', commit='m-old')
        self.pr_measured(m_cell(2056), stem='901-1', commit='m-new')
        base = budgets([self.moved(2056, stem='901-1')])
        self.base_holds('901-1')
        errors = self.check(budgets([self.moved(2055)]), base)
        self.assertEqual(
            errors,
            [
                f'{DOM}: '
                + self.older('measured file 900-1', 'm-old', 'measured file 901-1', 'm-new')
            ],
        )
        self.assertEqual(self.stale, set())
        (move,) = self.checked.moves
        self.assertIn('an entry moves only to a newer source', move['refused'][0])

    def test_a_newer_or_concurrent_source_than_the_base_entrys_holds(self):
        self.git.lineage = {('0ld', 'new'), ('0ld', HEAD), ('new', HEAD), ('pr-head', HEAD)}
        self.dom('100', 640.0, '0ld', cal_cell(baseline=1000.0))
        base = budgets([self.exact('100', 640.0), entry(source=RUN_URL + '100')])
        # The scheduled check-in: calibration 200 of a commit after 100's.
        self.dom('200', 640.0, 'new', cal_cell(baseline=1100.0))
        self.base_holds('100')
        weekly = budgets([self.exact('200', 640.0), entry(baseline=1100.0, source=RUN_URL + '200')])
        self.assertEqual(self.check(weekly, base), [])
        # A pull request head that forked before new: neither descends from the other.
        base = weekly
        self.base_holds('200')
        self.pr_measured(m_cell(641))
        self.write('200', json.loads(self.git.shown['200.json']))
        head = budgets([self.moved(641), entry(baseline=1100.0, source=RUN_URL + '200')])
        self.assertEqual(self.check(head, base), [])

    def test_a_base_source_git_cannot_place_fails_closed(self):
        self.pr_measured(m_cell(2056))
        cases = {
            'no file in the base tree': (
                lambda: self.git.shown.update({'100.json': perf_gate.GitError}),
                '100.json does not exist',
            ),
            'a commit git cannot answer for': (
                lambda: self.git.broken.add('gone'),
                'no such commit gone',
            ),
        }
        for name, (setup, want) in cases.items():
            with self.subTest(name):
                self.git.shown = {'100.json': json.dumps(calibration_file([], commit='gone'))}
                self.git.broken = set()
                setup()
                errors = self.check(budgets([self.moved(2056)]), self.moved_from())
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(
                    errors[0].startswith(f"{DOM}: the base's calibration 100: "), errors
                )
                self.assertIn(want, errors[0])
                self.assertEqual(self.stale, set())

    def schema_1_base(self, *entries):
        """A base budgets.json of schema 1, whose sources the base tree holds under another
        directory: git shows none of them under sources."""
        base = budgets(list(entries), schema=1)
        del base['target']
        for e in entries:
            self.git.shown[f'{e["source"].rsplit("/", 1)[1]}.json'] = perf_gate.GitError
        return base

    def resourced(self):
        """A head whose exact entry leaves the base's calibration 100 for calibration 200 of
        the base commit, ba5e."""
        self.git.lineage = {('ba5e', HEAD)}
        self.dom('200', 2056.0, 'ba5e')
        return budgets([self.exact('200', 2056.0)])

    def test_a_schema_1_base_holds_no_entry_to_its_base_entrys_source(self):
        head = self.resourced()
        errors = self.check(head, self.schema_1_base(self.exact('100', 2055.0)), base_sha='ba5e')
        self.assertEqual(errors, [])
        self.assertEqual(
            self.checked.notices,
            [
                (
                    'the base budgets.json is schema 1, which names no source this gate can '
                    "place, so no entry was held to a source as new as its base entry's"
                )
            ],
        )
        # The same base at schema 2 places its source, or fails closed.
        base = self.moved_from()
        self.git.shown['100.json'] = perf_gate.GitError
        errors = self.check(head, base, base_sha='ba5e')
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith(f"{DOM}: the base's calibration 100: "), errors)
        self.assertEqual(self.checked.notices, [])

    def test_a_schema_1_base_keeps_every_head_side_check(self):
        cases = {
            'a bundle that does not verify': (
                lambda: self.bad_bundles.add('200.sigstore.json'),
                'calibration 200: the bundle does not verify',
            ),
            'a calibration of a pull request': (
                lambda: self.dom('200', 2056.0, 'pr-tip'),
                'which the base ba5e does not hold',
            ),
            'a baseline its calibration did not measure': (
                lambda: self.dom('200', 2057.0, 'ba5e'),
                "baseline 2056.0 is not calibration 200's 2057.0",
            ),
        }
        for name, (setup, want) in cases.items():
            with self.subTest(name):
                self.bad_bundles = set()
                head = self.resourced()
                self.git.lineage.add(('pr-tip', HEAD))
                setup()
                errors = self.check(
                    head, self.schema_1_base(self.exact('100', 2055.0)), base_sha='ba5e'
                )
                self.assertTrue(any(want in x for x in errors), errors)

    def test_a_base_with_no_entries_logs_no_skip(self):
        head = self.resourced()
        self.assertEqual(self.check(head, {'budgets': []}, base_sha='ba5e'), [])
        self.assertEqual(self.checked.notices, [])

    def test_an_unplaceable_calibration_is_one_error_for_the_entries_naming_it(self):
        self.git.added = {'200.json': 'squash'}
        self.git.lineage = {('squash', HEAD)}
        self.dom('200', 4384.0, 'pr-tip')
        errors = self.unchanged(self.exact('200', 4384.0))
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith('calibration 200:'), errors)

    def test_a_change_check_that_cannot_read_the_plan_fails(self):
        self.git.added = {'200.json': 'squash'}
        self.git.lineage = {('squash', HEAD)}
        self.git.same_raises = ValueError('Expecting property name')
        self.dom('200', 4384.0, 'pr-tip')
        errors = self.unchanged(self.exact('200', 4384.0))
        self.assertTrue(any(x.startswith('calibration 200:') for x in errors), errors)

    def test_a_source_path_that_is_a_directory_fails(self):
        x1 = self.two_commits()
        (self.dir / '100.json').unlink()
        (self.dir / '100.json').mkdir()
        errors = self.unchanged(self.exact('200', 4384.0), x1)
        self.assertIn(
            'calibration 100: 100.json and 100.sigstore.json must both be checked in', errors
        )

    def test_a_cell_key_field_that_is_not_a_string_fails(self):
        x1 = self.two_commits()
        bad = json.dumps(calibration_file([cal_cell(), {**cal_cell(), 'metric': 1}], commit=HEAD))
        (self.dir / '100.json').write_text(bad)
        errors = self.unchanged(self.exact('200', 4384.0), x1)
        self.assertTrue(any('does not name a cell' in x for x in errors), errors)

    def test_a_named_calibration_that_does_not_load_fails(self):
        x1 = self.two_commits()
        bad_cell = json.dumps(calibration_file([cal_cell(), {'metric': 'dom'}], commit=HEAD))
        for text in ('{', bad_cell, json.dumps(calibration_file({}, commit=HEAD))):
            with self.subTest(text=text[:20]):
                (self.dir / '100.json').write_text(text)
                errors = self.unchanged(self.exact('200', 4384.0), x1)
                self.assertTrue(any(x.startswith('calibration 100:') for x in errors), errors)
                changed = self.check(budgets([self.exact('200', 4384.0), x1]), budgets([]))
                self.assertEqual(sum(x.startswith('calibration 100:') for x in changed), 1, changed)

    def test_a_commit_git_cannot_answer_for_fails(self):
        x1 = self.two_commits()
        self.git.broken = {'0ld'}
        for head, base in (
            (budgets([self.exact('200', 4384.0), x1]),) * 2,
            (budgets([self.exact('200', 4384.0), x1]), budgets([])),
        ):
            with self.subTest(changed=base != head):
                errors = self.check(head, base)
                self.assertTrue(
                    any(x.startswith('calibration 200:') and '128' in x for x in errors), errors
                )

    def test_an_unchanged_entry_its_calibration_did_not_measure_fails(self):
        self.dom('100', 4400.0, HEAD)
        errors = self.unchanged(self.exact('100', 4400.0), self.exact('100', 1.0, scale='x1'))
        self.assertTrue(any('x1' in x and 'did not measure this cell' in x for x in errors), errors)

    def test_a_raise_naming_a_file_whose_baseline_differs_fails(self):
        self.write('111', calibration_file([cal_cell(baseline=1000.0)]))
        base = budgets([entry(baseline=1000.0)])
        head = budgets([entry(baseline=1200.0, reason='a new view costs this')])
        self.assertTrue(self.check(head, base))

    def test_a_raise_naming_a_file_whose_bundle_does_not_verify_fails(self):
        self.write('222', calibration_file([cal_cell(baseline=1200.0)]))
        self.bad_bundles.add('222.sigstore.json')
        base = budgets([entry(baseline=1000.0)])
        head = budgets([entry(baseline=1200.0, reason='why', source=RUN_URL + '222')])
        errors = self.check(head, base)
        self.assertTrue(errors)
        self.assertIn(('222.json', '222.sigstore.json'), self.verified)

    def test_a_file_for_another_repository_runner_or_commit_fails(self):
        for field, value in (
            ('repo', 'cplieger/other'),
            ('runner', 'self-hosted'),
            ('commit', 'deadbeef'),
        ):
            with self.subTest(field=field):
                self.write('111', calibration_file([cal_cell(baseline=900.0)], **{field: value}))
                head = budgets([entry(baseline=900.0)])
                self.assertTrue(self.check(head, budgets([entry(baseline=1000.0)])))

    def test_a_file_whose_run_id_is_not_its_source_fails(self):
        (self.dir / '111.json').write_text(
            json.dumps({**calibration_file([cal_cell(baseline=900.0)]), 'run_id': '9'})
        )
        (self.dir / '111.sigstore.json').write_text('{}')
        self.assertTrue(
            self.check(budgets([entry(baseline=900.0)]), budgets([entry(baseline=1000.0)]))
        )

    def test_a_calibration_raises_a_baseline_without_a_reason(self):
        self.write('111', calibration_file([cal_cell(baseline=1200.0)]))
        base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        self.assertEqual(self.check(budgets([entry(baseline=1200.0)]), base), [])

    def test_a_reason_on_a_calibration_entry_needs_a_raise_this_change_makes(self):
        self.write('111', calibration_file([cal_cell()]))
        why = budgets([entry(reason='slower parse')])
        self.assertEqual(
            self.check(why, budgets([entry()])),
            [
                (
                    f'{CPU}: a reason belongs on a measured value, or on a budget a headroom, floor '
                    'or target edit raises'
                )
            ],
        )
        self.assertEqual(self.check(why, why), [])
        lower = {'cpu_ms': 50, 'heap_mb': 0.5}
        self.assertEqual(self.check(budgets(why['budgets'], floor=lower), why), [])

    def test_a_lowered_entry_matching_its_signed_file_passes(self):
        self.write('111', calibration_file([cal_cell(baseline=900.0)]))
        base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        self.assertEqual(self.check(budgets([entry(baseline=900.0)]), base), [])
        self.assertEqual(self.verified, [('111.json', '111.sigstore.json')])

    def test_a_ten_run_file_sets_tier_and_headroom(self):
        self.write('111', calibration_file([cal_cell(baseline=900.0, headroom=1.6)]))
        base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        self.assertEqual(self.check(budgets([entry(baseline=900.0, headroom=1.6)]), base), [])
        self.assertTrue(self.check(budgets([entry(baseline=900.0)]), base))

    def test_a_calibration_of_fewer_than_ten_runs_fails(self):
        self.write('111', calibration_file([cal_cell(baseline=900.0, tier=None, runs=5)], runs=5))
        base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        want = ['calibration 111: 5 runs decide no tier; a calibration needs 10']
        self.assertEqual(self.check(budgets([entry(baseline=900.0)]), base), want)
        self.assertEqual(self.unchanged(entry(baseline=900.0)), want)

    def test_a_gated_entry_dropped_without_retired_fails(self):
        base = budgets([entry(source=RUN_URL + '100')])
        self.assertTrue(self.check(budgets([]), base))
        retired = [{**CELL, 'metric': 'cpu_ms', 'reason': 'the view was removed'}]
        self.assertEqual(self.check(budgets([], retired=retired), base), [])
        no_reason = [{**CELL, 'metric': 'cpu_ms', 'reason': ''}]
        self.assertTrue(self.check(budgets([], retired=no_reason), base))

    def test_a_retired_cell_is_listed_with_its_tier_and_reason(self):
        base = budgets([entry(source=RUN_URL + '100'), entry('dom', 'exact', 640.0)])
        retired = [
            {**CELL, 'metric': 'cpu_ms', 'reason': 'the view was removed'},
            {**CELL, 'metric': 'dom', 'reason': 'the list is virtual now'},
        ]
        self.assertEqual(self.check(budgets([], retired=retired), base), [])
        self.assertEqual(
            self.checked.retires,
            [
                {'key': keyof(entry()), 'tier': 'gated', 'reason': 'the view was removed'},
                {'key': keyof(entry('dom')), 'tier': 'exact', 'reason': 'the list is virtual now'},
            ],
        )
        text = perf_gate.summary(budgets([]), perf_gate.Outcome(retires=self.checked.retires))
        self.assertIn('### Cells this pull request retires', text)
        self.assertIn('| Cell | Tier | Reason |', text)
        self.assertIn(f'| {DOM} | exact | the list is virtual now |', text)
        self.assertIn(f'| {CPU} | gated | the view was removed |', text)

    def test_a_reported_entry_dropped_needs_no_retired(self):
        base = budgets([entry(tier='reported', source=RUN_URL + '100')])
        self.assertEqual(self.check(budgets([]), base), [])

    def test_an_unchanged_entry_whose_file_is_absent_fails(self):
        b = budgets([entry(source=RUN_URL + '100')])
        self.assertEqual(
            self.check(b, b),
            ['calibration 100: 100.json and 100.sigstore.json must both be checked in'],
        )
        self.assertEqual(self.verified, [])

    def test_a_source_an_unchanged_entry_names_still_verifies(self):
        self.write('111', calibration_file([cal_cell()]))
        b = budgets([entry()])
        self.assertEqual(self.check(b, b), [])
        self.assertEqual(self.verified, [('111.json', '111.sigstore.json')])
        self.assertEqual(self.signers, [perf_gate.CALIBRATION_SIGNER])
        self.bad_bundles.add('111.sigstore.json')
        self.assertEqual(
            self.check(b, b),
            ['calibration 111: the bundle does not verify: bundle does not verify'],
        )
        self.assertEqual(self.stale, set())

    def test_an_unchanged_entry_whose_source_is_not_a_run_url_fails(self):
        b = budgets([entry(source='https://example.com/1')])
        self.assertEqual(
            self.check(b, b),
            [f"{CPU}: source 'https://example.com/1' is not a calibration or measured run URL"],
        )

    def test_a_target_edit_rechecks_an_entry_whose_fields_did_not_change(self):
        srv = entry('server_cpu_ms', baseline=20.0, budget=63.0, source=RUN_URL + '100')
        self.write('100', calibration_file([cal_cell()]))
        base = budgets([entry(source=RUN_URL + '100'), srv])
        head = budgets(
            [entry(source=RUN_URL + '100'), {**srv, 'budget': 50.0}],
            target={'server_cpu_ms': 50.0},
        )
        errors = self.check(head, base)
        self.assertEqual(
            errors,
            [
                'files/x10/chromium/desktop/cold/server_cpu_ms: calibration 100 did not measure this cell'
            ],
        )

    def test_a_source_file_no_entry_names_fails(self):
        self.write('111', calibration_file([cal_cell()]))
        self.write('333', calibration_file([cal_cell()]))
        self.write('901-1', measured_file([m_cell(2056)], run_id='901'))
        (self.dir / 'notes.txt').write_text('')
        b = budgets([entry()])
        self.assertEqual(
            self.check(b, b),
            [
                '333.json: no budget entry names source 333',
                '333.sigstore.json: no budget entry names source 333',
                '901-1.json: no budget entry names source 901-1',
                '901-1.sigstore.json: no budget entry names source 901-1',
                'notes.txt: not a source file or bundle',
            ],
        )

    def test_a_changed_entry_whose_file_is_missing_fails(self):
        base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        self.assertTrue(self.check(budgets([entry(baseline=900.0)]), base))

    def test_a_source_that_is_not_a_calibration_run_fails(self):
        base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        head = budgets([entry(baseline=900.0, source='https://example.com/runs/111')])
        self.assertTrue(self.check(head, base))

    def test_a_runner_edit_alone_rechecks_every_entry(self):
        self.write('111', calibration_file([cal_cell()]))
        base = budgets([entry(), entry('dom', 'exact', 640)])
        head = budgets(base['budgets'], runner='self-hosted-x')
        errors = self.check(head, base)
        self.assertTrue(errors)
        self.assertIn('self-hosted-x', ' '.join(errors))
        dom = cal_cell('dom', baseline=640, tier='exact', kind='count')
        self.write('111', calibration_file([cal_cell(), dom], runner='self-hosted-x'))
        self.assertEqual(self.check(head, base), [])

    def test_a_headroom_or_floor_edit_needs_a_reason_for_every_budget_it_raises(self):
        wide = {'cpu_ms': 2.0, 'heap_mb': 1.25, 'rss_mb': 1.25}
        high = {'cpu_ms': 100, 'heap_mb': 1}
        cases = (
            ('headroom', 1000.0, 2000.0, {'headroom': wide}),
            ('floor', 20.0, 100.0, {'floor': high}),
        )
        for name, baseline, raised, edit in cases:
            for doc_edit in ({}, edit):
                with self.subTest(name, calibration_carries_it=bool(doc_edit)):
                    cal = calibration_file([cal_cell(baseline=baseline)], **doc_edit)
                    self.write('111', cal)
                    base = budgets([entry(baseline=baseline)])
                    head = budgets([entry(baseline=baseline, budget=raised)], **edit)
                    self.assertEqual(perf_gate.check_rule(head), [])
                    errors = self.check(head, base)
                    self.assertTrue(errors)
                    self.assertIn('without a reason', ' '.join(errors))
                    why = entry(baseline=baseline, budget=raised, reason='slower parse')
                    self.assertEqual(self.check(budgets([why], **edit), base), [])

    def test_a_file_edit_asks_a_reason_only_of_the_budgets_its_family_moves(self):
        srv = entry('server_cpu_ms', baseline=20.0, budget=63.0)
        dom = {'tier': 'exact', 'kind': 'count'}
        cells = [
            cal_cell(baseline=1100.0),
            cal_cell('dom', baseline=641, **dom),
            cal_cell('server_cpu_ms', baseline=20.0, kind='server_cpu'),
        ]
        self.write('111', calibration_file(cells))
        base = budgets(
            [entry(), entry('dom', 'exact', 640), {**srv, 'budget': 70.0}],
            target={'server_cpu_ms': 70.0},
        )
        # A target edit moves no cpu_ms budget, and no file edit moves an exact one.
        head = budgets([entry(baseline=1100.0), entry('dom', 'exact', 641), srv])
        self.assertEqual(self.check(head, base), [])
        wide = {'cpu_ms': 1.6, 'heap_mb': 1.25, 'rss_mb': 1.25}
        head = budgets(
            [entry(baseline=1100.0, budget=1760.0), entry('dom', 'exact', 641), srv],
            headroom=wide,
        )
        self.assertEqual(
            self.check(head, base),
            [
                (
                    f'{CPU}: the file headroom or floor raises its budget from 1500.0 to 1760.0 '
                    'without a reason'
                )
            ],
        )

    def test_a_headroom_edit_rechecks_an_entry_whose_fields_did_not_change(self):
        lowered = {'cpu_ms': 1.4, 'heap_mb': 1.25, 'rss_mb': 1.25}
        heap = entry('heap_mb', baseline=20.0, budget=25.0, source=RUN_URL + '100')
        base = budgets([entry(), heap])
        head = budgets([entry(budget=1400.0), heap], headroom=lowered)
        self.write('111', calibration_file([cal_cell()]))
        errors = self.check(head, base)
        self.assertTrue(errors)
        self.assertIn('calibration 100', ' '.join(errors))

    def test_a_server_cpu_target_and_budget_raised_together_need_a_reason(self):
        def server(target, **kw):
            e = entry('server_cpu_ms', baseline=20.0, budget=target, **kw)
            return budgets([e], target={'server_cpu_ms': target})

        srv = cal_cell('server_cpu_ms', baseline=20.0, kind='server_cpu')
        self.write('111', calibration_file([srv]))
        base = server(63.0)
        head = server(1000.0)
        self.assertEqual(perf_gate.check_rule(head), [])
        errors = self.check(head, base)
        self.assertTrue(errors)
        self.assertIn('target', ' '.join(errors))
        self.assertIn('without a reason', ' '.join(errors))
        why = server(1000.0, reason='the export reads the whole log')
        self.assertEqual(self.check(why, base), [])
        self.assertEqual(self.check(server(40.0), base), [])

    def test_a_calibration_file_holding_nan_or_infinity_fails(self):
        for constant in (float('nan'), float('inf')):
            with self.subTest(constant=constant):
                self.write('111', calibration_file([cal_cell(baseline=900.0, spread=constant)]))
                base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
                errors = self.check(budgets([entry(baseline=900.0)]), base)
                self.assertTrue(errors)
                self.assertIn('not a finite number', ' '.join(errors))


class GitHistory(unittest.TestCase):
    """perf_gate.Git on a real repository: a pull request's tip, which changes app.txt and
    the last line of lib.txt, and its squash merge with the calibration's check-in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.sh('init', '-q', '-b', 'main')
        self.put('tests/perf/plan.json', {'sets': {'pr': {'shards': 5, 'cells': [{'seconds': 9}]}}})
        self.put('app.txt', 'one')
        self.put('lib.txt', ''.join(f'l{i}\n' for i in range(1, 11)))
        self.base = self.commit('base')
        self.sh('checkout', '-q', '-b', 'pr')
        self.put('app.txt', 'two')
        self.put('lib.txt', (self.root / 'lib.txt').read_text().replace('l10', 'L10'))
        self.tip = self.commit('the change')
        self.sh('checkout', '-q', 'main')

    def sh(self, *args):
        env = {
            **os.environ,
            'GIT_AUTHOR_NAME': 't',
            'GIT_AUTHOR_EMAIL': 't@t',
            'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't@t',
        }
        done = subprocess.run(
            ['git', *args], cwd=self.root, env=env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def put(self, path, value):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            p.write_bytes(value)
        else:
            p.write_text(value if isinstance(value, str) else json.dumps(value))

    def commit(self, message):
        self.sh('add', '-A')
        self.sh('commit', '-q', '-m', message)
        return self.sh('rev-parse', 'HEAD')

    def squash(self, *, dev=None, app='two', plan=None):
        """The squash of the pull request onto main after dev's commit, which writes the
        files of dev ({path: text}) first."""
        for path, text in (dev or {}).items():
            self.put(path, text)
            self.commit('dev moved')
        self.put('app.txt', app)
        self.put('lib.txt', (self.root / 'lib.txt').read_text().replace('l10', 'L10'))
        self.put('tests/perf/sources/200.json', '{}')
        self.put('tests/perf/budgets.json', '{}')
        self.put(
            'tests/perf/plan.json',
            plan or {'sets': {'pr': {'shards': 6, 'cells': [{'seconds': 12}]}}},
        )
        return self.commit('squash')

    def git(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        return perf_gate.Git()

    def test_a_squash_of_the_measured_tip_holds_its_code(self):
        squash = self.squash()
        git = self.git()
        self.assertFalse(git.is_ancestor(self.tip, squash))
        self.assertEqual(git.added_by('tests/perf/sources/200.json', squash), squash)
        self.assertTrue(git.same_change(self.tip, squash))

    def test_a_squash_after_an_unrelated_dev_merge_brings_the_measured_change(self):
        squash = self.squash(dev={'other.txt': 'dev'})
        self.assertTrue(self.git().same_change(self.tip, squash))

    def test_a_squash_after_a_dev_edit_elsewhere_in_a_changed_file_brings_it(self):
        lines = ''.join(f'l{i}\n' for i in range(1, 11))
        squash = self.squash(dev={'lib.txt': 'dev\n' * 3 + lines})
        self.assertTrue(self.git().same_change(self.tip, squash))

    def test_a_squash_of_other_pull_request_code_than_was_measured_is_refused(self):
        squash = self.squash(app='three')
        self.assertFalse(self.git().same_change(self.tip, squash))

    def test_a_squash_after_a_dev_merge_of_other_pull_request_code_is_refused(self):
        squash = self.squash(dev={'other.txt': 'dev'}, app='three')
        self.assertFalse(self.git().same_change(self.tip, squash))

    def pr_tip(self, app):
        """A second pull request off base whose tip writes app (bytes) to app.txt."""
        self.sh('checkout', '-q', '-b', f'pr-{len(app)}-{app[:1].hex()}', self.base)
        self.put('app.txt', app)
        tip = self.commit('another change')
        self.sh('checkout', '-q', 'main')
        return tip

    def squash_of(self, app):
        self.put('app.txt', app)
        self.put('tests/perf/sources/200.json', '{}')
        return self.commit('squash')

    def test_a_squash_differing_by_a_carriage_return_is_refused(self):
        tip = self.pr_tip(b'two\n')
        self.assertFalse(self.git().same_change(tip, self.squash_of(b'two\r\n')))

    def test_a_squash_of_a_measured_crlf_change_is_accepted(self):
        tip = self.pr_tip(b'two\r\n')
        self.assertTrue(self.git().same_change(tip, self.squash_of(b'two\r\n')))

    def test_a_squash_of_a_measured_latin_1_change_is_accepted(self):
        tip = self.pr_tip(b'caf\xe9\n')
        self.assertTrue(self.git().same_change(tip, self.squash_of(b'caf\xe9\n')))

    def test_a_squash_differing_by_whitespace_is_refused(self):
        self.assertFalse(self.git().same_change(self.tip, self.squash(app='two ')))

    def test_a_push_after_the_calibration_is_refused_at_the_merge_ref(self):
        self.sh('checkout', '-q', 'pr')
        self.put('app.txt', 'three')
        self.commit('a push after calibrating')
        self.put('tests/perf/sources/200.json', '{}')
        self.commit('check in')
        self.sh('checkout', '-q', 'main')
        self.put('other.txt', 'dev')
        self.commit('dev moved')
        self.sh('merge', '-q', '--no-ff', '-m', 'merge', 'pr')
        merge = self.sh('rev-parse', 'HEAD')
        path = pathlib.Path('tests/perf/sources/200.json')
        at, why = perf_gate.placed_commit({'commit': self.tip}, path, self.git(), merge)
        self.assertIsNone(at)
        self.assertIn('take entries only from a calibration of the default branch', why)

    def test_a_calibration_of_the_pull_requests_final_head_is_placed_at_the_merge_ref(self):
        self.sh('checkout', '-q', 'pr')
        self.put('tests/perf/sources/200.json', '{}')
        self.commit('check in')
        self.sh('checkout', '-q', 'main')
        self.put('other.txt', 'dev')
        self.commit('dev moved')
        self.sh('merge', '-q', '--no-ff', '-m', 'merge', 'pr')
        merge = self.sh('rev-parse', 'HEAD')
        path = pathlib.Path('tests/perf/sources/200.json')
        self.assertEqual(
            perf_gate.placed_commit({'commit': self.tip}, path, self.git(), merge),
            (self.tip, None),
        )

    def test_a_calibration_no_commit_of_head_added_is_refused_naming_it(self):
        self.put('tests/perf/sources/300.json', '{}')
        path = pathlib.Path('tests/perf/sources/300.json')
        at, why = perf_gate.placed_commit({'commit': self.tip}, path, self.git(), self.base)
        self.assertIsNone(at)
        self.assertIn('no first-parent commit', why)

    def test_a_squash_that_drops_part_of_the_measured_change_is_refused(self):
        squash = self.squash(app='one')
        self.assertFalse(self.git().same_change(self.tip, squash))

    def test_a_squash_that_changes_the_plan_beyond_its_seconds_holds_other_code(self):
        squash = self.squash(
            plan={'sets': {'pr': {'shards': 5, 'cells': [{'seconds': 9, 'x': 1}]}}}
        )
        self.assertFalse(self.git().same_change(self.tip, squash))

    def test_a_commit_the_checkout_lacks_is_fetched_from_origin(self):
        clone = self.root / 'clone'
        self.sh(
            'clone',
            '-q',
            '--no-local',
            '--single-branch',
            '--branch',
            'main',
            str(self.root),
            str(clone),
        )
        cwd = os.getcwd()
        os.chdir(clone)
        self.addCleanup(os.chdir, cwd)
        git = perf_gate.Git()
        self.assertFalse(git.ok('rev-parse', '--verify', '--quiet', f'{self.tip}^{{commit}}'))
        self.assertEqual(git.commit(self.tip), self.tip)
        self.assertTrue(git.ok('rev-parse', '--verify', '--quiet', f'{self.tip}^{{commit}}'))
        with self.assertRaises(perf_gate.GitError):
            git.commit('f' * 40)

    def test_the_file_a_merged_branch_added_is_placed_at_the_merge(self):
        self.sh('checkout', '-q', 'pr')
        self.put('tests/perf/sources/200.json', '{}')
        self.commit('check in')
        self.sh('checkout', '-q', 'main')
        self.sh('merge', '-q', '--no-ff', '-m', 'merge', 'pr')
        merge = self.sh('rev-parse', 'HEAD')
        self.assertEqual(self.git().added_by('tests/perf/sources/200.json', merge), merge)

    def test_a_moved_calibration_is_placed_at_its_measured_commit(self):
        self.put('tests/perf/calibrations/200.json', json.dumps({'commit': self.base}))
        self.commit('check in')
        self.sh('mv', 'tests/perf/calibrations', 'tests/perf/sources')
        moved = self.commit('move the sources')
        path = pathlib.Path('tests/perf/sources/200.json')
        git = self.git()
        follow = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'log.follow'}
        for config in ({}, {**follow, 'GIT_CONFIG_VALUE_0': 'true'}):
            with self.subTest(config=config), unittest.mock.patch.dict(os.environ, config):
                self.assertEqual(git.added_by(str(path), moved), moved)
                self.assertEqual(
                    perf_gate.placed_commit({'commit': self.base}, path, git, moved),
                    (self.base, None),
                )

    def measured(self, commit, stem):
        run_id, attempt = stem.split('-')
        doc = measured_file([m_cell(2056)], commit=commit, run_id=run_id, attempt=attempt)
        self.put(f'tests/perf/sources/{stem}.json', doc)
        self.put(f'tests/perf/sources/{stem}.sigstore.json', '{}')

    def merge_ref(self):
        """GitHub's merge ref of pr: main with pr merged into it, checked out."""
        self.sh('checkout', '-q', '--detach', 'main')
        self.sh('merge', '-q', '--no-ff', '-m', 'merge ref', 'pr')
        return self.sh('rev-parse', 'HEAD')

    def provenance(self, git, stem, head_sha):
        run_id, attempt = stem.split('-')
        url = f'{MEASURED_URL}{run_id}/attempts/{attempt}'
        head = budgets([entry('dom', 'exact', 2056, source=url, reason='one more node')])
        return perf_gate.check_provenance(
            head,
            budgets([entry('dom', 'exact', 2055, source=RUN_URL + '100')]),
            sources=pathlib.Path('tests/perf/sources'),
            accepted=pathlib.Path('tests/perf/accepted'),
            repo=REPO,
            head_sha=head_sha,
            base_sha=self.sh('rev-parse', 'main'),
            verify=lambda blob, bundle, signer: None,
            git=git,
        )

    def test_an_accepted_slowdown_expires_at_the_first_calibration_of_a_commit_holding_it(self):
        git = self.git()
        slow = {'reason': 'a slower parse', 'cells': [{**CELL, 'metric': 'cpu_ms', 'budget': 1700}]}
        self.put('tests/perf/accepted/slow.json', slow)
        merged = self.commit('accept a slowdown')
        self.put('other.txt', 'dev')
        later = self.commit('dev moved')
        for run_id, measured, errors in (
            ('300', self.base, 0),
            ('301', merged, 1),
            ('302', later, 1),
        ):
            with self.subTest(measured=measured):
                self.sh('rm', '-q', '-r', '--ignore-unmatch', 'tests/perf/sources')
                doc = calibration_file(
                    [cal_cell()], commit=measured, run_id=run_id, run_url=RUN_URL + run_id
                )
                self.put(f'tests/perf/sources/{run_id}.json', doc)
                self.put(f'tests/perf/sources/{run_id}.sigstore.json', '{}')
                head = self.commit(f'check in {run_id}')
                b = budgets([entry(source=RUN_URL + run_id)])
                found = perf_gate.check_provenance(
                    b,
                    b,
                    sources=pathlib.Path('tests/perf/sources'),
                    accepted=pathlib.Path('tests/perf/accepted'),
                    repo=REPO,
                    head_sha=head,
                    base_sha=head,
                    verify=lambda blob, bundle, signer: None,
                    git=git,
                )
                self.assertEqual(len(found.errors), errors, found.errors)
                if errors:
                    self.assertIn(f'calibration {run_id} measured this cell at', found.errors[0])
                    self.assertIn('delete accepted/slow.json', found.errors[0])
                else:
                    self.assertEqual(next(iter(found.accepted.values()))['file'], 'slow.json')

    def test_a_measured_file_of_the_head_before_a_dev_merge_near_the_change_is_retaken(self):
        git = self.git()
        # The base entry's calibration, which the head drops once its entry moves.
        cal = calibration_file(
            [cal_cell('dom', 2055, 'exact', kind='count')], commit=self.base, run_id='100'
        )
        self.put('tests/perf/sources/100.json', {**cal, 'run_url': RUN_URL + '100'})
        self.put('tests/perf/sources/100.sigstore.json', '{}')
        self.commit('check in calibration 100')
        self.sh('checkout', '-q', 'pr')
        self.measured(self.tip, '900-1')
        self.commit('take the measured file')
        self.sh('checkout', '-q', 'main')
        # Generated code regenerated on dev two lines above the pull request's change.
        self.put('lib.txt', (self.root / 'lib.txt').read_text().replace('l8', 'L8'))
        self.commit('dev regenerates')
        self.sh('checkout', '-q', 'pr')
        self.sh('merge', '-q', '--no-edit', 'main')
        self.sh('rm', '-q', 'tests/perf/sources/100.json', 'tests/perf/sources/100.sigstore.json')
        merged = self.commit('drop the calibration no entry names')
        found = self.provenance(git, '900-1', self.merge_ref())
        errors, stale = found.errors, found.stale
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith('measured file 900-1: '), errors)
        self.assertIn(f'than measured {self.tip} made', errors[0])
        self.assertIn('take the measured file this run wrote', errors[0])
        self.assertEqual(stale, {keyof(entry('dom'))})
        self.sh('checkout', '-q', 'pr')
        self.sh(
            'rm', '-q', 'tests/perf/sources/900-1.json', 'tests/perf/sources/900-1.sigstore.json'
        )
        self.measured(merged, '901-1')
        self.commit('take the new head measured file')
        found = self.provenance(git, '901-1', self.merge_ref())
        self.assertEqual((found.errors, found.stale), ([], set()))

    def test_the_base_plan_is_read_from_the_base_tree(self):
        git = self.git()
        plan = {'sets': {'pr': {'shards': 5, 'cells': [{'seconds': 9}]}}}
        self.assertEqual(perf_gate.base_plan(git, self.base, 'tests/perf/plan.json'), plan)
        self.assertEqual(perf_gate.base_plan(git, self.base, 'tests/perf/none.json'), {})
        self.put('tests/perf/plan.json', '[]')
        listed = self.commit('a plan that is no object')
        with self.assertRaisesRegex(TypeError, 'not a JSON object'):
            perf_gate.base_plan(git, listed, 'tests/perf/plan.json')
        with self.assertRaises(perf_gate.GitError):
            perf_gate.base_plan(git, '0' * 40, 'tests/perf/plan.json')

    def test_a_pull_request_passes_after_a_calibration_lands_on_its_base(self):
        """The weekly check-in merges after the pull request opened: the merge ref's first
        parent holds its calibration, and the base the pull request opened on does not."""
        git = self.git()

        def check_in(run_id, commit, cpu):
            cells = [cal_cell('cpu_ms', cpu), cal_cell('dom', 2055, 'exact', kind='count')]
            doc = calibration_file(cells, commit=commit, run_id=run_id, run_url=RUN_URL + run_id)
            self.put(f'tests/perf/sources/{run_id}.json', doc)
            self.put(f'tests/perf/sources/{run_id}.sigstore.json', '{}')
            dom = entry('dom', 'exact', 2055, source=RUN_URL + run_id)
            gated = entry(baseline=cpu, source=RUN_URL + run_id)
            self.put('tests/perf/budgets.json', budgets([gated, dom]))

        check_in('100', self.base, 1000.0)
        opened = self.commit('check in calibration 100')
        self.sh('checkout', '-q', '-b', 'pr2')
        self.put('app.txt', 'two')
        tip = self.commit('the change')
        self.measured(tip, '900-1')
        moved = entry('dom', 'exact', 2056, source=MEASURED_URL + '900/attempts/1', reason='+1')
        self.put('tests/perf/budgets.json', budgets([entry(source=RUN_URL + '100'), moved]))
        self.commit('take the measured file')
        self.sh('checkout', '-q', 'main')
        self.put('other.txt', 'dev')
        dev = self.commit('dev moved')
        self.sh('rm', '-q', 'tests/perf/sources/100.json', 'tests/perf/sources/100.sigstore.json')
        check_in('200', dev, 1010.0)
        later = self.commit('the weekly check-in')
        # GitHub's merge ref, budgets.json merged as its one-entry-per-line form merges.
        self.sh('checkout', '-q', '--detach', later)
        self.sh('merge', '-q', '--no-ff', '--no-commit', '-X', 'theirs', 'pr2')
        merged = budgets([entry(baseline=1010.0, source=RUN_URL + '200'), moved])
        self.put('tests/perf/budgets.json', merged)
        merge = self.commit('merge ref')
        self.assertEqual(self.sh('rev-parse', 'HEAD^1'), later)

        def check(base_sha):
            return perf_gate.check_provenance(
                merged,
                json.loads(git.show(base_sha, 'tests/perf/budgets.json')),
                sources=pathlib.Path('tests/perf/sources'),
                accepted=pathlib.Path('tests/perf/accepted'),
                repo=REPO,
                head_sha=merge,
                base_sha=base_sha,
                verify=lambda blob, bundle, signer: None,
                git=git,
            )

        found = check(later)
        self.assertEqual((found.errors, found.stale), ([], set()))
        self.assertEqual([m['key'] for m in found.moves], [keyof(moved)])
        frozen = check(opened)
        self.assertIn(
            f'{CPU}: calibration 200 measured {dev}, which the base {opened} does not hold; ',
            '\n'.join(frozen.errors),
        )

    def test_git_errors_raise_and_a_short_sha_is_refused(self):
        git = self.git()
        self.assertEqual(git.show(self.base, 'app.txt'), 'one')
        self.assertTrue(git.has(self.base, 'app.txt'))
        self.assertFalse(git.has(self.base, 'tests/perf/sources/100.json'))
        with self.assertRaises(perf_gate.GitError):
            git.has('0' * 40, 'app.txt')
        with self.assertRaises(perf_gate.GitError):
            git.show(self.base, 'tests/perf/sources/100.json')
        with self.assertRaises(perf_gate.GitError):
            git.is_ancestor('0' * 40, self.base)
        with self.assertRaises(perf_gate.GitError):
            git.commit(self.tip[:12])
        self.assertEqual(git.commit(self.tip), self.tip)


class Coverage(unittest.TestCase):
    def check(self, doc, plan, base=None):
        """check_plan with the patterns plan and base, plan itself when unset, both hold."""
        held = perf_gate.held_patterns(plan, plan if base is None else base)
        return perf_gate.check_plan(doc, plan, reported=held)

    def test_a_pattern_only_the_head_plan_lists_decides_no_tier(self):
        frames = entry('sse_frames', 'exact', 3)
        doc = budgets([entry(), frames])
        head = {**PLAN, 'reported': [{'metric': 'sse_frames'}]}
        self.assertEqual(perf_gate.held_patterns(head, head), [{'metric': 'sse_frames'}])
        self.assertEqual(perf_gate.held_patterns(head, PLAN), [])
        narrower = {**PLAN, 'reported': [{'metric': 'sse_frames', 'window': 'cold'}]}
        self.assertEqual(perf_gate.held_patterns(head, narrower), [])
        self.assertEqual(self.check(doc, head, base=PLAN), [])
        self.assertEqual(len(self.check(doc, head)), 1)
        # The head's own malformed items still fail, whatever the base holds.
        broken = {**PLAN, 'reported': ['sse_frames']}
        self.assertTrue(self.check(doc, broken, base=PLAN)[0].startswith('plan.json: reported'))

    def test_a_gated_or_exact_entry_no_set_lists_fails(self):
        gone = {**CELL, 'scenario': 'gone'}
        dropped = {**entry(), **gone}
        exact = {**entry('dom', 'exact', 640), **gone}
        reported = {**entry('frames', 'reported', 9.0), **gone}
        doc = budgets([entry(), dropped, exact, reported])
        errors = self.check(doc, PLAN)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e.startswith('gone/') for e in errors))
        self.assertIn('no set of plan.json measures', errors[0])
        # A retired item does not keep an entry the plan dropped: the entry leaves.
        retired = [
            {**gone, 'metric': m, 'reason': 'the view was removed'} for m in ('cpu_ms', 'dom')
        ]
        self.assertEqual(self.check({**doc, 'retired': retired}, PLAN), errors)
        left = budgets([entry(), reported], retired=retired)
        self.assertEqual(self.check(left, PLAN), [])

    def test_a_unit_only_a_set_no_workflow_runs_lists_fails(self):
        doc = budgets([entry()])
        docs = {'shards': 1, 'cells': [unit('docs', 75)]}
        files = {'shards': 1, 'cells': [unit('files', 80)]}
        spare = {'node': '.', 'sets': {'pr': docs, 'full': docs, 'spare': files}}
        errors = self.check(doc, spare)
        self.assertEqual(len(errors), 1)
        self.assertIn('no set of plan.json measures', errors[0])
        weekly = {'node': '.', 'sets': {'pr': docs, 'full': files}}
        self.assertEqual(self.check(doc, weekly), [])

    def test_a_window_no_run_set_lists_for_its_unit_fails(self):
        webkit = {'engine': 'webkit'}
        cold = {**entry('rss_mb', baseline=200.0, budget=250.0), **webkit}
        live = {**entry(), **webkit, 'window': 'live'}
        doc = budgets([cold, live])
        pr = {'shards': 1, 'cells': [unit('files', 25, windows=['cold'], **webkit)]}
        chromium = {'shards': 1, 'cells': [unit('files', 80)]}
        errors = self.check(doc, {'node': '.', 'sets': {'pr': pr, 'full': chromium}})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith('files/x10/webkit/desktop/live/cpu_ms:'))
        full = {'shards': 1, 'cells': [unit('files', 25, **webkit)]}
        self.assertEqual(self.check(doc, {'node': '.', 'sets': {'pr': pr, 'full': full}}), [])

    def test_a_gated_or_exact_entry_of_a_cell_the_plan_reports_fails(self):
        frames = entry('sse_frames', 'exact', 3)
        live = {**frames, 'window': 'live'}
        engine = entry('max_bytes:GET /api/engine', 'exact', 900)
        doc = budgets([entry(), frames, live, engine])
        reported = [
            {'metric': 'max_bytes:GET /api/engine'},
            {'metric': 'sse_frames', 'window': 'cold'},
        ]
        errors = self.check(doc, {**PLAN, 'reported': reported})
        tail = ': plan.json reports this cell, so its entry is reported, not exact'
        self.assertEqual(
            sorted(errors),
            [
                f'files/x10/chromium/desktop/cold/max_bytes:GET /api/engine{tail}',
                f'files/x10/chromium/desktop/cold/sse_frames{tail}',
            ],
        )
        quiet = budgets([entry(), {**frames, 'tier': 'reported'}, live])
        self.assertEqual(self.check(quiet, {**PLAN, 'reported': reported}), [])

    def test_a_reported_pattern_of_the_wrong_shape_is_an_error_line(self):
        for reported in (
            {},
            ['sse_frames'],
            [{'window': 'cold'}],
            [{'metric': ''}],
            [{'metric': 'dom', 'window': 1}],
            [{'metric': 'dom', 'scale': 'x1'}],
        ):
            with self.subTest(reported=reported):
                errors = self.check(budgets([entry()]), {**PLAN, 'reported': reported})
                self.assertTrue(any(e.startswith('plan.json: reported') for e in errors), errors)

    def test_a_set_or_cell_that_is_not_an_object_is_an_error_line(self):
        for sets in (
            [],
            {'full': []},
            {'full': {'cells': 'files'}},
            {'full': {'cells': ['files']}},
            {'full': {'cells': [unit('files', 80, windows='cold')]}},
        ):
            with self.subTest(sets=sets):
                errors = self.check(budgets([entry()]), {'node': '.', 'sets': sets})
                self.assertTrue(any(e.startswith('plan.json: ') for e in errors), errors)


@unittest.skipUnless(shutil.which('cosign') or os.environ.get('CI'), 'cosign is not installed')
class RealCosign(unittest.TestCase):
    BLOB = TESTDATA / 'trivy_0.76.2_checksums.txt'
    BUNDLE = TESTDATA / 'trivy_0.76.2_checksums.txt.sigstore.json'

    def test_a_bundle_signed_by_another_identity_fails(self):
        for signer in (perf_gate.CALIBRATION_SIGNER, MEASURED_SIGNER):
            with self.subTest(signer=signer):
                self.assertIsNotNone(perf_gate.cosign_verify(self.BLOB, self.BUNDLE, signer))

    def test_the_same_bundle_verifies_for_its_own_signer(self):
        trivy = 'https://github.com/aquasecurity/trivy/.github/workflows/reusable-release.yaml@refs/tags/v0.76.2'
        pattern = r'^https://github\.com/aquasecurity/trivy/\.github/workflows/[a-z-]+\.yaml@refs/tags/v[0-9.]+$'
        for signer in (
            perf_gate.Signer(identity=trivy),
            perf_gate.Signer(identity_regexp=pattern),
            perf_gate.Signer(identity=trivy, workflow_repository='aquasecurity/trivy'),
        ):
            with self.subTest(signer=signer):
                self.assertIsNone(perf_gate.cosign_verify(self.BLOB, self.BUNDLE, signer))
        elsewhere = perf_gate.Signer(identity=trivy, workflow_repository='cplieger/ci')
        self.assertIn(
            'GithubWorkflowRepository', perf_gate.cosign_verify(self.BLOB, self.BUNDLE, elsewhere)
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

    def test_runs_repeat_every_shard_and_name_the_run(self):
        matrix = perf_gate.plan(PLAN, 'pr', runs=3)
        self.assertEqual(len(matrix['include']), 6)
        self.assertEqual(matrix['include'][-1]['name'], 'run-03-shard-2-of-2')
        self.assertEqual({m['run'] for m in matrix['include']}, {1, 2, 3})

    def test_a_remeasure_plans_only_the_listed_cells_once_each(self):
        cpu = {**CELL, 'scenario': 'docs', 'metric': 'cpu_ms', 'window': 'live'}
        heap = {**cpu, 'metric': 'heap_mb', 'window': 'cold'}
        matrix = perf_gate.plan(PLAN, 'pr', remeasure={'cells': [cpu, heap]})
        self.assertEqual(shard_cells(matrix), [['docs']])

    def test_an_empty_remeasure_plans_nothing(self):
        self.assertEqual(perf_gate.plan(PLAN, 'pr', remeasure={'cells': []})['include'], [])

    def test_a_remeasure_adds_the_extension_s_full_units_over_a_unit_they_cover(self):
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
        extend = [unit('files', 80), unit('files', 60, scale='x1')]
        matrix = perf_gate.plan(cold, 'pr', remeasure={'cells': [red], 'extend': extend})
        planned = sorted(
            (c['scale'], c.get('windows'), c['seconds'])
            for job in matrix['include']
            for c in job['cells']
        )
        self.assertEqual(planned, [('x1', None, 60), ('x10', None, 80)])
        bad = {'cells': [], 'extend': [unit('files', 0)]}
        with self.assertRaisesRegex(
            ValueError, 'extend: files/x10/chromium/desktop needs its seconds'
        ):
            perf_gate.plan(cold, 'pr', remeasure=bad)

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

    def test_more_jobs_than_one_matrix_holds_fails(self):
        with self.assertRaisesRegex(ValueError, '256'):
            perf_gate.plan(PLAN, 'pr', runs=129)

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


class Bench(unittest.TestCase):
    def test_every_measured_cell_charts_its_median(self):
        doc = results([cell(reps=(900.0, 1000.0, 1300.0)), cell('dom', (640,) * 3, kind='count')])
        points = perf_gate.bench(doc)
        self.assertEqual(
            points,
            [
                {
                    'name': 'files/x10/chromium/desktop/cold/cpu_ms',
                    'unit': 'cpu_ms',
                    'value': 1000.0,
                },
                {'name': 'files/x10/chromium/desktop/cold/dom', 'unit': 'dom', 'value': 640},
            ],
        )


class ResultFiles(unittest.TestCase):
    NAMES = (
        'perf-results-cplieger-a-measure-shard-1-of-2-attempt-1',
        'perf-results-cplieger-a-measure-run-03-shard-2-of-2-attempt-1',
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
        return sorted(p.parent.name for p in perf_gate.result_files(self.dir, *args))

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
        for runs in (1, 2):
            jobs = perf_gate.plan(PLAN, 'pr', runs=runs)['include']
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


def run_main(*argv):
    """perf_gate.main's exit status and everything it printed."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        rc = perf_gate.main(list(argv))
    return rc, out.getvalue()


class PlacedGit(FakeGit):
    """FakeGit with the pull request head of measured_file() and the base's calibrated
    commit 0ld, which neither descends from, in HEAD's history."""

    def __init__(self):
        super().__init__(lineage={('pr-head', HEAD), ('0ld', HEAD)})


class Take(unittest.TestCase):
    """--take: a measured file's counts into budgets.json with their reason."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.sources = self.dir / 'sources'
        self.sources.mkdir()
        self.budgets = self.dir / 'budgets.json'
        x1 = {'scale': 'x1'}
        self.old = budgets(
            [
                {**entry('dom', 'exact', 975.0, source=RUN_URL + '111'), **x1},
                entry('dom', 'exact', 2055.0, source=MEASURED_URL + '800/attempts/1', reason='r'),
                entry(source=RUN_URL + '111'),
            ]
        )
        self.budgets.write_text(json.dumps(self.old))
        self.put('111', calibration_file([cal_cell()], commit='0ld'))
        self.put('800-1', measured_file([m_cell(2055.0)], run_id='800', commit='m-old'))
        self.put('900-1', measured_file([m_cell(2056.0), m_cell(976.0, scale='x1')]))

    def put(self, stem, doc):
        (self.sources / f'{stem}.json').write_text(json.dumps(doc))
        (self.sources / f'{stem}.sigstore.json').write_text('{}')

    def take(self, *argv, stem='900-1'):
        path = str(self.sources / f'{stem}.json')
        return run_main('--take', path, '--budgets', str(self.budgets), *argv)

    def entries(self):
        return {keyof(e): e for e in json.loads(self.budgets.read_text())['budgets']}

    def test_take_writes_value_source_and_reason_and_deletes_an_unnamed_source(self):
        rc, output = self.take('--reason', 'one more node')
        self.assertEqual(rc, 0, output)
        url = MEASURED_URL + '900/attempts/1'
        got = self.entries()
        x1 = keyof({**entry('dom'), 'scale': 'x1'})
        for k, value in ((keyof(entry('dom')), 2056.0), (x1, 976.0)):
            want = {'baseline': value, 'budget': value, 'source': url, 'reason': 'one more node'}
            self.assertEqual({f: got[k][f] for f in want}, want)
        self.assertEqual(got[keyof(entry())], entry(source=RUN_URL + '111'))
        kept = ['111.json', '111.sigstore.json', '900-1.json', '900-1.sigstore.json']
        self.assertEqual(sorted(p.name for p in self.sources.iterdir()), kept)
        text = self.budgets.read_text()
        self.assertIn(f'    {json.dumps(got[keyof(entry())])}\n', text)
        self.assertEqual(json.loads(text)['budgets'][2], entry(source=RUN_URL + '111'))
        with (
            unittest.mock.patch.object(perf_gate, 'Git', PlacedGit),
            unittest.mock.patch.object(perf_gate, 'cosign_verify', return_value=None),
        ):
            base = self.dir / 'base.json'
            base.write_text(json.dumps(self.old))
            argv = ('--check-file', str(self.budgets), '--base', str(base))
            argv += ('--sources', str(self.sources), '--repo', REPO, '--head', HEAD)
            self.assertEqual(run_main(*argv, '--base-sha', HEAD), (0, ''))

    def test_take_with_cells_takes_only_the_cells_its_glob_matches(self):
        self.assertEqual(self.take('--reason', 'a', '--cells', 'files/x1/*')[0], 0)
        got = self.entries()
        self.assertEqual(got[keyof(entry('dom'))]['reason'], 'r')
        self.assertIn('800-1.json', {p.name for p in self.sources.iterdir()})
        self.assertEqual(self.take('--reason', 'b', '--cells', 'files/x10/*')[0], 0)
        reasons = sorted(e.get('reason', '') for e in self.entries().values())
        self.assertEqual(reasons, ['', 'a', 'b'])
        self.assertNotIn('800-1.json', {p.name for p in self.sources.iterdir()})

    def test_take_refuses_and_writes_nothing(self):
        self.put('901-1', measured_file([m_cell(2000.0, 'cpu_ms')], run_id='901'))
        before = self.budgets.read_text()
        cases = {
            'two reason lines': (('--reason', 'a\nb'), '900-1', 'one non-empty line'),
            'a glob of no cell': (('--reason', 'a', '--cells', 'no/*'), '900-1', 'matches no cell'),
            'a cell with no exact entry': (('--reason', 'a'), '901-1', 'has no exact entry'),
            'a calibration': (('--reason', 'a'), '111', 'is not a measured file'),
        }
        for name, (argv, stem, want) in cases.items():
            with self.subTest(name):
                rc, output = self.take(*argv, stem=stem)
                self.assertEqual(rc, 1, output)
                self.assertIn(want, output)
                self.assertEqual(self.budgets.read_text(), before)
        (self.sources / '900-1.sigstore.json').unlink()
        rc, output = self.take('--reason', 'a')
        self.assertEqual(rc, 1)
        self.assertIn('900-1.sigstore.json', output)
        self.assertEqual(self.budgets.read_text(), before)


X1 = {'scale': 'x1'}


def cal_222(*cells, **kw):
    """The scheduled calibration 222 of the default branch at commit `cal`."""
    seconds = [{**{f: CELL[f] for f in ('scenario', 'scale', 'engine', 'width')}, 'seconds': 64.5}]
    doc = calibration_file(list(cells), schema=2, kind='calibration', commit='cal', seconds=seconds)
    return {**doc, 'run_id': '222', 'run_url': RUN_URL + '222', **kw}


class CheckIn(unittest.TestCase):
    """--check-in: every cell the scheduled calibration measured from it, a newer measured
    value kept, absorbed or unused slowdowns and unnamed sources removed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.sources, self.accepted = self.dir / 'sources', self.dir / 'accepted'
        self.sources.mkdir()
        self.accepted.mkdir()
        # 0ld and m-old came before the calibrated commit `cal`; m-new and ovr2 after it.
        self.git = FakeGit()
        self.git.lineage = {
            *((c, HEAD) for c in ('0ld', 'm-old', 'cal', 'm-new', 'ovr', 'ovr2')),
            *((c, 'cal') for c in ('0ld', 'm-old', 'ovr')),
            *((c, 'm-new') for c in ('0ld', 'm-old', 'cal', 'ovr')),
        }
        self.git.added = {'slow.json': 'ovr', 'mixed.json': 'ovr2'}
        x1 = X1
        self.old = budgets(
            [
                entry(),
                entry(window='live'),
                entry('dom', 'exact', 640.0),
                entry('heap_mb', baseline=20.0, budget=25.0),
                {**entry('reads', 'exact', 2.0, source=MEASURED_URL + '800/attempts/1'), **x1},
                {**entry('dom', 'exact', 976.0, source=MEASURED_URL + '900/attempts/1'), **x1},
            ]
        )
        for e in self.old['budgets'][4:]:
            e['reason'] = 'a measured move'
        old_cells = [
            cal_cell(),
            cal_cell(window='live'),
            cal_cell('dom', 640.0, 'exact', kind='count'),
            cal_cell('heap_mb', 20.0),
        ]
        self.put('111', calibration_file(old_cells, commit='0ld'))
        self.put('800-1', measured_file([m_cell(2.0, 'reads', **x1)], run_id='800', commit='m-old'))
        self.put('900-1', measured_file([m_cell(976.0, **x1)], commit='m-new'))
        self.cal = cal_222(
            cal_cell(baseline=1100.0),
            cal_cell('dom', 640.5, 'reported', kind='count'),
            cal_cell('reads', 3.0, 'exact', kind='count', **x1),
            cal_cell('dom', 975.0, 'exact', kind='count', **x1),
            cal_cell('rss_mb', 200.0),
            cal_cell('heap_mb', 24.0),
        )
        self.put('222', self.cal)
        cpu, heap = {**CELL, 'metric': 'cpu_ms'}, {**CELL, 'metric': 'heap_mb'}
        self.slowdown('slow.json', {**cpu, 'budget': 1700.0})
        self.slowdown('mixed.json', {**cpu, 'window': 'live', 'budget': 1700.0})
        self.slowdown('mixed.json', {**heap, 'budget': 28.0})
        self.plan = {
            'node': '.',
            'sets': {
                'pr': {'shards': 1, 'cells': [unit('files', 60, windows=['cold'])]},
                'full': {'shards': 1, 'cells': [unit('files', 60), unit('files', 60, **x1)]},
            },
        }

    def put(self, stem, doc):
        (self.sources / f'{stem}.json').write_text(json.dumps(doc))
        (self.sources / f'{stem}.sigstore.json').write_text('{}')

    def slowdown(self, name, c):
        p = self.accepted / name
        doc = json.loads(p.read_text()) if p.is_file() else {'reason': 'slower', 'cells': []}
        doc['cells'].append(c)
        p.write_text(json.dumps(doc))

    def check_in(self, cal=None, old=None):
        return perf_gate.check_in(
            cal or self.cal,
            old or self.old,
            self.plan,
            sources=self.sources,
            accepted=self.accepted,
            repo=REPO,
            head_sha=HEAD,
            git=self.git,
        )

    def test_check_in_takes_every_measured_cell_but_a_newer_measured_value(self):
        done = self.check_in()
        got = {keyof(e): e for e in done.budgets['budgets']}
        self.assertEqual([keyof(e) for e in done.budgets['budgets']], sorted(got))
        url = RUN_URL + '222'
        x1 = X1
        self.assertEqual(got[keyof(entry())], entry(baseline=1100.0, budget=1650.0, source=url))
        self.assertEqual(got[keyof(entry(window='live'))], entry(window='live'))
        dom = {**entry('dom', 'reported', 640.5, source=url)}
        self.assertEqual(got[keyof(dom)], dom)
        reads = {**entry('reads', 'exact', 3.0, source=url), **x1}
        self.assertEqual(got[keyof(reads)], reads)
        self.assertEqual(got[keyof({**entry('dom'), **x1})], self.old['budgets'][5])
        self.assertEqual(
            got[keyof(entry('rss_mb'))], entry('rss_mb', baseline=200.0, budget=250.0, source=url)
        )
        self.assertEqual(got[keyof(entry('heap_mb'))]['budget'], 30.0)
        self.assertEqual(sorted(done.drop), ['800-1.json', '800-1.sigstore.json'])
        cpu_live = {**CELL, 'metric': 'cpu_ms', 'window': 'live', 'budget': 1700.0}
        self.assertEqual(
            done.accepted,
            {'slow.json': None, 'mixed.json': {'reason': 'slower', 'cells': [cpu_live]}},
        )
        self.assertEqual(
            [[u['seconds'] for u in s['cells']] for s in done.plan['sets'].values()],
            [[60], [65, 60]],
        )
        for want in (
            '## Calibration 222 of cplieger/marotte at `cal`',
            '### Tier changes',
            f'| {DOM} | exact | reported |',
            '| files/x10/chromium/desktop/cold/rss_mb | none | gated |',
            '### Raised gated baselines',
            f'| {CPU} | 1000.0 | 1100.0 | 1.1 |',
            '| files/x10/chromium/desktop/cold/heap_mb | 20.0 | 24.0 | 1.2 |',
            '### Measured values kept',
            '| files/x1/chromium/desktop/cold/dom | 976.0 | 975.0 | measured file 900-1 |',
            '| accepted/slow.json | files/x10/chromium/desktop/cold/cpu_ms | calibration 222 measured it |',
            '| accepted/mixed.json | files/x10/chromium/desktop/cold/heap_mb | budget 30.0 is at or over 28.0 |',
            '### Sources dropped',
            '- measured file 800-1',
        ):
            self.assertIn(want, done.body)

    def test_a_check_in_passes_the_gate_against_the_file_it_rewrote(self):
        done = self.check_in()
        for name in done.drop:
            (self.sources / name).unlink()
        for name, doc in done.accepted.items():
            p = self.accepted / name
            p.unlink() if doc is None else p.write_text(json.dumps(doc))
        self.assertEqual(perf_gate.check_rule(done.budgets), [])
        checked = perf_gate.check_provenance(
            done.budgets,
            self.old,
            sources=self.sources,
            accepted=self.accepted,
            repo=REPO,
            head_sha=HEAD,
            base_sha=HEAD,
            verify=lambda *_: None,
            git=self.git,
        )
        self.assertEqual(checked.errors, [])
        self.assertEqual(list(checked.accepted), [keyof(entry(window='live'))])

    def test_check_in_keeps_a_measured_value_only_where_it_can_place_its_file(self):
        x1_dom = keyof({**entry('dom'), **X1})
        # A measured file the gate would refuse (a squash of other code) gives way.
        self.put('900-1', measured_file([m_cell(976.0, **X1)], commit='pr-x'))
        got = {keyof(e): e for e in self.check_in().budgets['budgets']}
        self.assertEqual(got[x1_dom]['source'], RUN_URL + '222')
        # Git failing to place it stops the check-in, which writes nothing.
        self.put('900-1', measured_file([m_cell(976.0, **X1)], commit='m-new'))
        self.git.broken = {'m-new'}
        with self.assertRaisesRegex(perf_gate.GitError, 'measured file 900-1: .*no such commit'):
            self.check_in()

    def test_check_in_drops_a_retired_item_whose_cell_it_measured(self):
        gone = {**CELL, 'scenario': 'gone', 'metric': 'cpu_ms', 'reason': 'the view was removed'}
        rss = {**CELL, 'metric': 'rss_mb', 'reason': 'the export left'}
        done = self.check_in(old={**self.old, 'retired': [gone, rss]})
        self.assertEqual(done.budgets['retired'], [gone])
        self.assertIn('### Retired cells measured again', done.body)
        self.assertIn('| files/x10/chromium/desktop/cold/rss_mb | the export left |', done.body)
        self.assertEqual(perf_gate.check_rule(done.budgets), [])

    def test_check_in_refuses_a_file_that_is_no_ten_run_calibration_of_this_repository(self):
        cases = {
            'nine runs': ({'runs': 9}, 'needs 10 runs'),
            'no kind': ({'kind': None}, 'kind'),
            'schema 1': ({'schema': 1}, 'schema'),
            'another runner': ({'runner': 'ubuntu-24.04'}, 'runner'),
            'another repository': ({'repo': 'cplieger/other'}, 'repo'),
            'a run url of another run': ({'run_url': RUN_URL + '333'}, 'run_url'),
        }
        for name, (swap, want) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, want):
                self.check_in({**self.cal, **swap})

    def test_check_in_writes_the_files_and_its_pull_request_body(self):
        artifacts = self.dir / 'proposed'
        artifacts.mkdir()
        for name in ('222.json', '222.sigstore.json'):
            (self.sources / name).rename(artifacts / name)
        b, plan = self.dir / 'budgets.json', self.dir / 'plan.json'
        b.write_text(json.dumps(self.old))
        plan.write_text(json.dumps(self.plan))
        git = self.git
        argv = ('--check-in', str(artifacts / '222.json'), '--budgets', str(b))
        argv += ('--plan-doc', str(plan), '--sources', str(self.sources))
        argv += ('--accepted', str(self.accepted), '--repo', REPO, '--head', HEAD)
        argv += ('--pr-body', str(self.dir / 'body.md'))
        with unittest.mock.patch.object(perf_gate, 'Git', lambda: git):
            rc, output = run_main(*argv)
        self.assertEqual(rc, 0, output)
        self.assertEqual(
            (self.sources / '222.json').read_bytes(), (artifacts / '222.json').read_bytes()
        )
        self.assertEqual(
            sorted(p.name for p in self.sources.iterdir()),
            [
                '111.json',
                '111.sigstore.json',
                '222.json',
                '222.sigstore.json',
                '900-1.json',
                '900-1.sigstore.json',
            ],
        )
        self.assertEqual(sorted(p.name for p in self.accepted.iterdir()), ['mixed.json'])
        self.assertEqual(len(json.loads((self.accepted / 'mixed.json').read_text())['cells']), 1)
        self.assertEqual(json.loads(plan.read_text())['sets']['full']['cells'][0]['seconds'], 65)
        self.assertIn('### Tier changes', (self.dir / 'body.md').read_text())
        self.assertEqual(
            {e['source'] for e in json.loads(b.read_text())['budgets']},
            {RUN_URL + '111', RUN_URL + '222', MEASURED_URL + '900/attempts/1'},
        )


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
        (self.dir / 'sources').mkdir()
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

    def compare(
        self,
        cells,
        *extra,
        other=None,
        entries=(),
        listed=True,
        plan=PLAN,
        shard=None,
        git=FakeGit,
        cosign=None,
        base=None,
        **doc,
    ):
        own = [entry(source=RUN_URL + '100'), entry('dom', 'exact', 640)] if listed else []
        b = self.put('budgets.json', budgets([*own, *entries], **doc))
        # The base branch's file is the head's unless base lists its entries or is a document.
        if base is not None and not isinstance(base, dict):
            base = budgets(base)
        was = b if base is None else self.put('base.json', base)
        # Each entry's calibration, checked in at the head, as the provenance check needs.
        for e in [*own, *entries]:
            if not isinstance(e, dict) or not str(e.get('source', '')).startswith(RUN_URL):
                continue
            run_id = e['source'].rsplit('/', 1)[1]
            path = self.dir / 'sources' / f'{run_id}.json'
            held = json.loads(path.read_text())['cells'] if path.is_file() else []
            c = cal_cell(e['metric'], e.get('baseline', 0.0), e['tier'], **{f: e[f] for f in CELL})
            doc = calibration_file([*held, c], run_id=run_id, run_url=RUN_URL + run_id)
            self.put(f'sources/{run_id}.json', doc)
            self.put(f'sources/{run_id}.sigstore.json', {})
        for patch in (
            unittest.mock.patch.object(perf_gate, 'Git', git),
            unittest.mock.patch.object(perf_gate, 'cosign_verify', return_value=cosign),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        own = 'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json'
        self.put(own, results(cells) if shard is None else shard)
        if other is not None:
            self.put(f'results/{OTHER}-measure-shard-1-of-1-attempt-1/results.json', results(other))
        argv = ('--budgets', b, '--base', was, '--results', str(self.dir / 'results'))
        argv += ('--prefix', 'cplieger-a')
        argv += ('--sources', str(self.dir / 'sources'), '--repo', REPO, '--head', HEAD)
        argv += ('--base-sha', HEAD, '--plan-doc', self.put('plan.json', plan), '--set', 'pr')
        argv += (
            '--remeasure-out',
            str(self.dir / 'remeasure.json'),
            '--summary',
            str(self.dir / 'summary.md'),
        )
        return self.main(*argv, *extra)

    def test_compare_writes_a_red_cell_to_remeasure_and_passes(self):
        rc = self.compare(
            [cell(reps=(1600.0, 1600.0, 1600.0)), cell('dom', (640,) * 3, kind='count')]
        )
        self.assertEqual(rc, 0)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual([keyof(c) for c in remeasure['cells']], [keyof(entry())])
        self.assertEqual(remeasure['scenarios'], ['files'])

    def test_compare_writes_an_exact_count_off_its_budget_to_remeasure_and_passes(self):
        rc = self.compare([cell(), cell('dom', (700,) * 3, kind='count')])
        self.assertEqual(rc, 0)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual([keyof(c) for c in remeasure['cells']], [keyof(entry('dom'))])

    def test_verdict_fails_an_exact_count_whose_six_reps_are_off(self):
        b = self.put('budgets.json', budgets([entry('dom', 'exact', 640)]))
        first = 'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json'
        self.put(first, results([cell('dom', (700, 640, 700), kind='count')]))
        argv = ('--budgets', b, '--results', str(self.dir / 'results'), '--prefix', 'cplieger-a')
        argv += ('--remeasure-attempt', '1', '--remeasure', self.put('remeasure.json', decision()))
        for more, rc in (((640,) * 3, 0), ((700, 640, 700), 1)):
            with self.subTest(more=more):
                again = results([cell('dom', more, kind='count')])
                self.put(
                    'again/perf-results-cplieger-a-remeasure-shard-1-of-1-attempt-1/results.json',
                    again,
                )
                self.assertEqual(self.main('--verdict', str(self.dir / 'again'), *argv), rc)

    def test_verdict_records_the_moved_counts_in_a_measured_file_named_for_its_run(self):
        b = self.put('budgets.json', budgets([entry('dom', 'exact', 640), entry()]))
        first = 'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json'
        again = 'again/perf-results-cplieger-a-remeasure-shard-1-of-1-attempt-2/results.json'
        argv = ('--verdict', str(self.dir / 'again'), '--budgets', b, '--prefix', 'cplieger-a')
        argv += ('--results', str(self.dir / 'results'), '--remeasure-attempt', '2')
        argv += ('--remeasure', self.put('remeasure.json', decision()))
        argv += ('--record-out', str(self.dir / 'measured'), '--commit', 'pr-head')
        argv += ('--head', 'merge', '--repo', REPO, '--run-id', '900', '--run-attempt', '2')
        argv += ('--workflow-repository', REPO, '--summary', str(self.dir / 'summary.md'))
        argv += ('--workflow-sha', 'c1' * 20)
        self.put(first, results([cell('dom', (641,) * 3, kind='count'), cell()]))
        self.put(again, results([cell('dom', (641,) * 3, kind='count')]))
        self.assertEqual(self.main(*argv), 1)
        written = json.loads((self.dir / 'measured' / '900-2.json').read_text())
        self.assertEqual(written, measured_file([m_cell(641)], attempt='2'))
        text = (self.dir / 'summary.md').read_text()
        self.assertIn(
            f'`gh run download 900 -R {REPO} -n perf-measured-cplieger-a-attempt-2 '
            '-D tests/perf/sources`',
            text,
        )
        self.assertIn(f'cplieger/ci checkout at {"c1" * 20}', text)
        self.assertIn('scripts/perf_gate.py --take tests/perf/sources/900-2.json', text)
        self.assertNotIn('<run>', text)
        (self.dir / 'measured' / '900-2.json').unlink()
        self.put(first, results([cell('dom', (640,) * 3, kind='count'), cell()]))
        self.assertEqual(self.main(*argv), 0, self.output)
        self.assertEqual(list((self.dir / 'measured').iterdir()), [])

    def test_verdict_judges_against_the_slowdowns_compare_accepted(self):
        b = self.put('budgets.json', budgets([entry(budget=1500.0)]))
        first = 'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json'
        self.put(first, results([cell(reps=(1600.0,) * 3)]))
        again = 'again/perf-results-cplieger-a-remeasure-shard-1-of-1-attempt-1/results.json'
        self.put(again, results([cell(reps=(1600.0,) * 3)]))
        argv = ('--verdict', str(self.dir / 'again'), '--budgets', b, '--prefix', 'cplieger-a')
        argv += ('--results', str(self.dir / 'results'), '--remeasure-attempt', '1')
        self.assertEqual(self.main(*argv, '--remeasure', self.put('none.json', decision())), 1)
        decided = self.put('remeasure.json', decision(accepted()))
        self.assertEqual(self.main(*argv, '--remeasure', decided), 0, self.output)
        self.assertIn('Slower on purpose', self.output)

    def test_compare_lists_the_exact_keys_a_fresh_measured_file_would_cure(self):
        url = MEASURED_URL + '900/attempts/1'
        moved = {**entry('dom', 'exact', 2056, source=url, reason='one more node'), 'scale': 'x1'}
        measured = [
            cell(),
            cell('dom', (640,) * 3, kind='count'),
            {**cell('dom', (2056,) * 3, kind='count'), 'scale': 'x1'},
        ]
        self.assertEqual(self.compare(measured, entries=[moved]), 1)
        self.assertIn('measured file 900-1: 900-1.json and 900-1.sigstore.json', self.output)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual(remeasure['stale'], [perf_gate.key_dict(keyof(moved))])
        self.assertEqual(remeasure['cells'], [])
        self.assertEqual(remeasure['extend'], [unit('files', 60, scale='x1')])
        self.assertEqual(remeasure['accepted'], [])

    def test_compare_extends_nothing_for_a_source_that_cannot_be_verified(self):
        url = MEASURED_URL + '900/attempts/1'
        moved = {**entry('dom', 'exact', 2056, source=url, reason='one more node'), 'scale': 'x1'}
        self.put('sources/900-1.json', measured_file([{**m_cell(2056), 'scale': 'x1'}]))
        self.put('sources/900-1.sigstore.json', {})
        measured = [cell(), cell('dom', (640,) * 3, kind='count')]
        rc = self.compare(measured, entries=[moved], cosign='cosign is not installed')
        self.assertEqual(rc, 1)
        self.assertIn('the bundle does not verify: cosign is not installed', self.output)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual((remeasure['stale'], remeasure['extend']), ([], []))
        self.assertEqual(remeasure['cells'], [])

    def test_compare_re_measures_a_stale_key_the_set_measured_though_nothing_extends(self):
        url = MEASURED_URL + '900/attempts/1'
        moved = entry('dom', 'exact', 641, source=url, reason='one more node')
        plan = {
            **PLAN,
            'sets': {**PLAN['sets'], 'full': {'shards': 1, 'cells': [unit('files', 80)]}},
        }
        rc = self.compare(
            [cell(), cell('dom', (641,) * 3, kind='count')],
            plan=plan,
            listed=False,
            entries=[entry(source=RUN_URL + '100'), moved],
        )
        self.assertEqual(rc, 1)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual(remeasure['stale'], [perf_gate.key_dict(keyof(moved))])
        self.assertEqual(remeasure['extend'], [])
        self.assertEqual([keyof(c) for c in remeasure['cells']], [keyof(moved)])

    def test_compare_fails_a_retired_cell_a_shard_still_measured(self):
        dom = {**CELL, 'metric': 'dom', 'reason': 'the list is virtual now'}
        own = [entry(source=RUN_URL + '100')]
        measured = [cell(), cell('dom', (700,) * 3, kind='count')]
        rc = self.compare(measured, listed=False, entries=own, retired=[dom])
        self.assertEqual(rc, 1)
        self.assertIn(
            f"::error::{DOM}: retired, yet a shard measured it; a measured cell's tier is the "
            "scheduled calibration's",
            self.output,
        )
        # A metric the harness no longer emits is a retirement.
        self.assertEqual(self.compare([cell()], listed=False, entries=own, retired=[dom]), 0)
        # So is a cell plan.json reports, whose tier is the plan's.
        reports = {**PLAN, 'reported': [{'metric': 'dom'}]}
        rc = self.compare(measured, listed=False, entries=own, retired=[dom], plan=reports)
        self.assertEqual(rc, 0, self.output)

    def test_compare_extends_the_full_units_of_a_cell_it_retires(self):
        dom = {**CELL, 'scale': 'x1', 'metric': 'dom', 'reason': 'the list is virtual now'}
        own = [entry(source=RUN_URL + '100')]
        gone = {**entry('dom', 'exact', 640), 'scale': 'x1'}
        rc = self.compare([cell()], listed=False, entries=own, retired=[dom], base=[*own, gone])
        self.assertEqual(rc, 0, self.output)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual(remeasure['extend'], [unit('files', 60, scale='x1')])
        self.assertEqual(remeasure['scenarios'], ['files'])
        # A retired item the base already holds leaves no entry, so nothing extends.
        rc = self.compare([cell()], listed=False, entries=own, retired=[dom])
        self.assertEqual(rc, 0, self.output)
        self.assertEqual(json.loads((self.dir / 'remeasure.json').read_text())['extend'], [])

    def test_a_reported_pattern_only_the_head_plan_lists_decides_no_tier(self):
        class Base(FakeGit):
            def __init__(self):
                super().__init__()
                self.shown = {'plan.json': json.dumps(PLAN)}

        reports = {**PLAN, 'reported': [{'metric': 'dom'}]}
        dom = {**CELL, 'metric': 'dom', 'reason': 'the list is virtual now'}
        own = [entry(source=RUN_URL + '100')]
        measured = [cell(), cell('dom', (700,) * 3, kind='count')]
        # Retiring the cells of a pattern the pull request adds retires measured cells.
        rc = self.compare(
            measured, listed=False, entries=own, retired=[dom], plan=reports, git=Base
        )
        self.assertEqual(rc, 1)
        self.assertIn(f'::error::{DOM}: retired, yet a shard measured it', self.output)
        # Kept, the entry is judged as before: its count off the budget is re-measured.
        self.assertEqual(self.compare(measured, plan=reports, git=Base), 0, self.output)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual([keyof(c) for c in remeasure['cells']], [keyof(entry('dom'))])
        self.assertEqual(remeasure['reported'], [])
        # A pattern the base plan holds too decides the tier.
        self.assertEqual(self.compare(measured, plan=reports), 1)
        self.assertIn(
            'plan.json reports this cell, so its entry is reported, not exact', self.output
        )
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual(remeasure['reported'], [{'metric': 'dom'}])

    def test_a_base_with_no_plan_holds_no_pattern_and_a_git_failure_fails(self):
        class Absent(FakeGit):
            def __init__(self):
                super().__init__()
                self.absent = {'plan.json'}

        class Broken(FakeGit):
            def __init__(self):
                super().__init__()
                self.shown = {'plan.json': perf_gate.GitError}

        reports = {**PLAN, 'reported': [{'metric': 'dom'}]}
        measured = [cell(), cell('dom', (640,) * 3, kind='count')]
        self.assertEqual(self.compare(measured, plan=reports, git=Absent), 0, self.output)
        self.assertEqual(json.loads((self.dir / 'remeasure.json').read_text())['reported'], [])
        (self.dir / 'remeasure.json').unlink()
        self.assertEqual(self.compare(measured, plan=reports, git=Broken), 1)
        self.assertIn('perf_gate: GitError', self.output)
        self.assertFalse((self.dir / 'remeasure.json').exists())

    def test_compare_marks_nothing_stale_on_a_runner_the_budgets_do_not_name(self):
        url = MEASURED_URL + '900/attempts/1'
        moved = {**entry('dom', 'exact', 2056, source=url, reason='one more node'), 'scale': 'x1'}
        shard = results([cell(), cell('dom', (640,) * 3, kind='count')], runner='self-hosted')
        self.assertEqual(self.compare([], entries=[moved], shard=shard), 1)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual(
            (remeasure['stale'], remeasure['extend'], remeasure['cells']), ([], [], [])
        )

    def test_verdict_records_nothing_on_a_runner_the_budgets_do_not_name(self):
        b = self.put('budgets.json', budgets([entry('dom', 'exact', 640)]))
        first = 'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json'
        again = 'again/perf-results-cplieger-a-remeasure-shard-1-of-1-attempt-1/results.json'
        for path in (first, again):
            self.put(path, results([cell('dom', (641,) * 3, kind='count')], runner='self-hosted'))
        stale = self.put('remeasure.json', decision(stale=[keyof(entry('dom'))]))
        argv = ('--verdict', str(self.dir / 'again'), '--budgets', b, '--prefix', 'cplieger-a')
        argv += ('--results', str(self.dir / 'results'), '--remeasure-attempt', '1')
        argv += ('--remeasure', stale, '--record-out', str(self.dir / 'measured'))
        argv += ('--commit', 'pr-head', '--head', 'merge', '--repo', REPO, '--run-id', '900')
        argv += ('--run-attempt', '1', '--workflow-repository', REPO, '--workflow-sha', 'c1' * 20)
        self.assertEqual(self.main(*argv), 0, self.output)
        self.assertFalse((self.dir / 'measured').exists())

    def test_compare_judges_an_accepted_slowdown_and_hands_it_to_the_verdict(self):
        class Git(FakeGit):
            def __init__(self):
                super().__init__()
                self.added = {'slow.json': 'ovr'}

        slow = {
            'reason': 'a slower parse on purpose',
            'cells': [{**CELL, 'metric': 'cpu_ms', 'budget': 1700.0}],
        }
        self.put('accepted/slow.json', slow)
        measured = [cell(reps=(1600.0,) * 3), cell('dom', (640,) * 3, kind='count')]
        argv = ('--accepted', str(self.dir / 'accepted'))
        self.assertEqual(self.compare(measured, *argv, git=Git), 0, self.output)
        remeasure = json.loads((self.dir / 'remeasure.json').read_text())
        self.assertEqual(remeasure['cells'], [])
        self.assertEqual(remeasure['accepted'], decision(accepted())['accepted'])
        self.assertIn('Slower on purpose', self.output)
        self.assertEqual(self.compare(measured, *argv), 1)
        self.assertIn('delete accepted/slow.json', self.output)

    def test_compare_names_an_accepted_slowdown_of_a_cell_it_did_not_measure(self):
        class Git(FakeGit):
            def __init__(self):
                super().__init__()
                self.added = {'slow.json': 'ovr'}

        # The full set alone lists files x1, so the pr set never measures it.
        x1 = {**entry(source=RUN_URL + '100'), 'scale': 'x1'}
        slow = {
            'reason': 'a slower parse on purpose',
            'cells': [{**CELL, 'scale': 'x1', 'metric': 'cpu_ms', 'budget': 1700.0}],
        }
        self.put('accepted/slow.json', slow)
        measured = [cell(), cell('dom', (640,) * 3, kind='count')]
        argv = ('--accepted', str(self.dir / 'accepted'))
        self.assertEqual(self.compare(measured, *argv, entries=[x1], git=Git), 0, self.output)
        text = (self.dir / 'summary.md').read_text()
        notices = text.split('### Notices', 1)[1]
        self.assertIn(
            'files/x1/chromium/desktop/cold/cpu_ms: accepted/slow.json gives budget 1700.0 '
            '(a slower parse on purpose); this run did not measure the cell',
            notices,
        )
        self.assertNotIn('Slower on purpose', text)

    def test_compare_fails_on_a_listed_cell_no_shard_measured(self):
        self.assertEqual(self.compare([cell()]), 1)
        self.assertIn('no shard measured', (self.dir / 'summary.md').read_text())

    def test_compare_fails_a_gated_entry_no_set_of_the_plan_measures(self):
        gone = {**entry(source=RUN_URL + '100'), 'scenario': 'gone'}
        measured = [cell(), cell('dom', (640,) * 3, kind='count')]
        self.assertEqual(self.compare(measured, entries=[gone]), 1)
        self.assertIn('no set of plan.json measures', (self.dir / 'summary.md').read_text())
        retired = [{**CELL, 'scenario': 'gone', 'metric': 'cpu_ms', 'reason': 'view removed'}]
        self.assertEqual(self.compare(measured, entries=[gone], retired=retired), 1)
        self.assertIn(f'gone/{CPU.split("/", 1)[1]}: retired, yet budgets lists it', self.output)
        self.assertEqual(self.compare(measured, retired=retired), 0, self.output)

    def test_compare_needs_a_result_only_for_the_windows_a_pr_cell_lists(self):
        # The pr set measures WebKit x10 cold only; budgets.json holds the
        # full set's WebKit live CPU and back count too.
        webkit = {'engine': 'webkit', 'source': RUN_URL + '100'}
        plan = {
            'node': '.',
            'sets': {
                'pr': {'shards': 1, 'cells': [unit('files', 25, windows=['cold'], **webkit)]},
                'full': {'shards': 1, 'cells': [unit('files', 25, **webkit)]},
            },
        }
        cold = {**entry('rss_mb', baseline=200.0, budget=250.0), **webkit}
        live = {**entry(), **webkit, 'window': 'live'}
        back = {**entry('dom', 'exact', 640), **webkit, 'window': 'back'}
        measured = [{**cell('rss_mb', (200.0,) * 3), 'engine': 'webkit'}]
        doc = {'entries': [cold, live, back], 'listed': False, 'plan': plan}
        self.assertEqual(self.compare(measured, **doc), 0, self.output)
        self.assertEqual(self.compare([], **doc), 1)
        errors = [line for line in self.output.splitlines() if line.startswith('::error::')]
        self.assertEqual(len(errors), 1, self.output)
        self.assertIn('::error::files/x10/webkit/desktop/cold/rss_mb:', errors[0])
        self.assertIn('no shard measured', errors[0])

    def test_compare_refuses_a_non_finite_number_in_budgets_or_results(self):
        measured = [cell(), cell('dom', (640,) * 3, kind='count')]
        cases = {
            'NaN budget': ([*measured], [entry(budget=float('nan'), window='live')], {}),
            'Infinity rep': ([cell(reps=(float('inf'),) * 3), measured[1]], [], {}),
            '1e999 rep': ([cell(reps=(-123.25,) * 3), measured[1]], [], {'-123.25': '-1e999'}),
            '400-digit budget': (
                [*measured],
                [entry(budget=987.25, window='live')],
                {'987.25': '1' + '0' * 400},
            ),
        }
        for name, (cells, entries, swap) in cases.items():
            with self.subTest(name):
                self.swap = swap
                self.assertEqual(self.compare(cells, entries=entries), 1)
                self.assertIn('is not a finite number', self.output)

    def test_a_document_or_entry_of_the_wrong_shape_is_an_error_line(self):
        measured = [cell(), cell('dom', (640,) * 3, kind='count')]
        cases = {
            'list plan.json': (lambda: self.compare(measured, plan=[]), 'must be a JSON object'),
            'list results': (lambda: self.compare(measured, shard=[]), 'must be a JSON object'),
            'string entry': (
                lambda: self.compare(measured, entries=['files']),
                '::error::budgets[2]: must be an object',
            ),
            'list plan for --plan': (
                lambda: self.main(
                    '--plan',
                    self.put('p.json', []),
                    '--set',
                    'pr',
                    '--out',
                    str(self.dir / 'm.json'),
                ),
                'must be a JSON object',
            ),
        }
        for name, (call, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(call(), 1)
                self.assertIn(want, self.output)

    def test_check_file_reports_a_numeric_metric_and_reads_no_calibration_for_it(self):
        numeric = cal_cell(metric=1)
        self.put('sources/111.json', calibration_file([numeric]))
        self.put('sources/111.sigstore.json', {})
        base = self.put('base.json', budgets([]))
        head = self.put('budgets.json', budgets([entry(metric=1, source=RUN_URL + '111')]))
        argv = ('--check-file', head, '--base', base, '--sources', str(self.dir / 'sources'))
        with (
            unittest.mock.patch.object(perf_gate, 'cosign_verify', return_value=None),
            unittest.mock.patch.object(perf_gate.Git, 'is_ancestor', return_value=True),
        ):
            self.assertEqual(
                self.main(*argv, '--repo', REPO, '--head', HEAD, '--base-sha', HEAD), 1
            )
        self.assertIn('::error::budgets[0]: missing metric', self.output)

    def test_a_schema_1_base_is_a_notice_of_compare_and_check_file(self):
        class Git(FakeGit):
            def __init__(self):
                super().__init__()
                self.shown = {'100.json': perf_gate.GitError}

        base = budgets([entry('dom', 'exact', 640, source=RUN_URL + '100')], schema=1)
        del base['target']
        e = entry('dom', 'exact', 640, source=RUN_URL + '200')
        counts = [cell(), cell('dom', (640,) * 3, kind='count')]
        rc = self.compare(counts, entries=[e], listed=False, git=Git, base=base)
        self.assertEqual(rc, 0, self.output)
        skip = (
            'the base budgets.json is schema 1, which names no source this gate can place, '
            "so no entry was held to a source as new as its base entry's"
        )
        self.assertIn(f'### Notices\n\n- {skip}\n', (self.dir / 'summary.md').read_text())
        argv = (
            '--check-file',
            str(self.dir / 'budgets.json'),
            '--base',
            str(self.dir / 'base.json'),
        )
        argv += ('--sources', str(self.dir / 'sources'), '--repo', REPO, '--head', HEAD)
        self.assertEqual(self.main(*argv, '--base-sha', HEAD), 0, self.output)
        self.assertEqual(self.output, f'::notice::{skip}\n')

    def test_check_file_refuses_a_list_budgets_json(self):
        head = self.put('budgets.json', [entry(source=RUN_URL + '100')])
        argv = ('--check-file', head, '--base', head, '--sources', str(self.dir / 'sources'))
        self.assertEqual(self.main(*argv, '--repo', REPO, '--head', HEAD, '--base-sha', HEAD), 1)
        self.assertIn('must be a JSON object', self.output)

    def test_check_file_refuses_a_nan_budget(self):
        base = self.put('base.json', budgets([entry(source=RUN_URL + '100')]))
        head = self.put(
            'budgets.json', budgets([entry(budget=float('nan'), source=RUN_URL + '100')])
        )
        argv = ('--check-file', head, '--base', base, '--sources', str(self.dir / 'sources'))
        self.assertEqual(self.main(*argv, '--repo', REPO, '--head', HEAD, '--base-sha', HEAD), 1)
        self.assertIn('NaN is not a finite number', self.output)

    def test_the_results_readers_need_their_prefix_and_the_verdict_its_attempt(self):
        d, f = str(self.dir), str(self.dir / 'f.json')
        cases = {
            'compare': (
                (
                    '--budgets',
                    f,
                    '--base',
                    f,
                    '--results',
                    d,
                    '--sources',
                    d,
                    '--repo',
                    REPO,
                    '--head',
                    HEAD,
                    '--base-sha',
                    HEAD,
                    '--remeasure-out',
                    f,
                    '--plan-doc',
                    f,
                    '--set',
                    'pr',
                ),
                ['--prefix'],
            ),
            'verdict': (
                ('--verdict', d, '--budgets', f, '--results', d),
                ['--prefix', '--remeasure-attempt', '--remeasure'],
            ),
            'verdict recording': (
                (
                    '--verdict',
                    d,
                    '--budgets',
                    f,
                    '--results',
                    d,
                    '--prefix',
                    'p',
                    '--record-out',
                    d,
                ),
                [
                    '--remeasure-attempt',
                    '--remeasure',
                    '--repo',
                    '--head',
                    '--commit',
                    '--run-id',
                    '--run-attempt',
                    '--workflow-repository',
                    '--workflow-sha',
                ],
            ),
            'take': (('--take', f, '--budgets', f), ['--reason']),
            'check-in': (
                ('--check-in', f, '--budgets', f, '--sources', d),
                ['--plan-doc', '--repo', '--head', '--pr-body'],
            ),
            'bench': (('--bench', d, '--out', f), ['--prefix']),
            'check-file': (
                ('--check-file', f, '--base', f, '--sources', d, '--repo', REPO, '--head', HEAD),
                ['--base-sha'],
            ),
        }
        for name, (argv, missing) in cases.items():
            with self.subTest(name):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit):
                    perf_gate.main(list(argv))
                self.assertIn(f'missing {", ".join(missing)}', err.getvalue())

    def test_compare_needs_the_plan(self):
        b = self.put('budgets.json', budgets([]))
        self.put(
            'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json',
            results([cell()]),
        )
        argv = ('--budgets', b, '--base', b, '--results', str(self.dir / 'results'))
        argv += ('--prefix', 'cplieger-a')
        argv += ('--sources', str(self.dir / 'sources'), '--repo', REPO, '--head', HEAD)
        argv += ('--base-sha', HEAD, '--remeasure-out', str(self.dir / 'remeasure.json'))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.main(*argv)

    def test_check_file_fails_a_rule_break(self):
        b = self.put('budgets.json', budgets([entry(budget=1600.0, source=RUN_URL + '100')]))
        argv = (
            '--check-file',
            b,
            '--base',
            b,
            '--sources',
            str(self.dir / 'sources'),
            '--repo',
            REPO,
        )
        self.assertEqual(self.main(*argv, '--head', HEAD, '--base-sha', HEAD), 1)

    def test_calibrate_writes_the_proposed_file(self):
        for i in range(10):
            name = f'perf-results-{PREFIX}-measure-run-{i + 1:02d}-shard-1-of-1-attempt-1'
            self.put(f'runs/{name}/results.json', run_doc({'cpu_ms': (1000.0,) * 3}))
        out = self.dir / 'budgets.proposed.json'
        argv = (
            '--calibrate',
            str(self.dir / 'runs'),
            '--prefix',
            PREFIX,
            '--runs',
            '10',
            '--out',
            str(out),
            '--repo',
            REPO,
            '--commit',
            HEAD,
        )
        argv += ('--runner', RUNNER, '--run-id', '111', '--run-url', RUN_URL + '111')
        self.assertEqual(self.main(*argv), 0)
        proposed = json.loads(out.read_text())
        self.assertEqual((proposed['runs'], only_cell(proposed)['tier']), (10, 'gated'))
        reported = self.put('plan.json', {**PLAN, 'reported': [{'metric': 'cpu_ms'}]})
        self.assertEqual(self.main(*argv, '--plan-doc', reported), 0, self.output)
        self.assertEqual(only_cell(json.loads(out.read_text()))['tier'], 'reported')
        broken = self.put('plan.json', {**PLAN, 'reported': [{'window': 'cold'}]})
        self.assertEqual(self.main(*argv, '--plan-doc', broken), 1)
        self.assertIn('is not a {metric, window} pattern', self.output)
        self.assertEqual(self.main(*argv[:5], '9', *argv[6:]), 1)
        self.assertIn('10 runs, not the 9 dispatched', self.output)

    def test_plan_writes_the_matrix(self):
        p = self.put('plan.json', PLAN)
        out = self.dir / 'matrix.json'
        argv = ('--plan', p, '--set', 'pr', '--only', 'docs, tabs', '--runs', '2')
        outputs = self.dir / 'github-output'
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

    def test_bench_writes_the_chart_points(self):
        self.put(
            'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json',
            results([cell()]),
        )
        out = self.dir / 'bench.json'
        argv = ('--bench', str(self.dir / 'results'), '--prefix', 'cplieger-a')
        self.assertEqual(self.main(*argv, '--out', str(out)), 0)
        self.assertEqual(json.loads(out.read_text())[0]['value'], 1000.0)

    def test_bench_with_no_shard_of_this_repository_charts_nothing(self):
        self.put(f'results/{OTHER}-measure-shard-1-of-1-attempt-1/results.json', results([cell()]))
        out = self.dir / 'bench.json'
        argv = ('--bench', str(self.dir / 'results'), '--prefix', 'cplieger-a')
        self.assertEqual(self.main(*argv, '--out', str(out)), 0)
        self.assertEqual(json.loads(out.read_text()), [])

    def test_compare_reads_only_its_own_repository_s_shards(self):
        own = [cell(), cell('dom', (640,) * 3, kind='count')]
        other = [cell(), cell('dom', (700,) * 3, kind='count')]
        self.assertEqual(self.compare(own, '--prefix', 'cplieger-a', other=other), 0)
        self.assertEqual(json.loads((self.dir / 'remeasure.json').read_text())['cells'], [])

    def test_verdict_reads_only_its_own_repository_s_remeasured_shards(self):
        b = self.put('budgets.json', budgets([entry(source=RUN_URL + '100')]))
        red = cell(reps=(1600.0, 1600.0, 1600.0))
        self.put(
            'results/perf-results-cplieger-a-measure-shard-1-of-1-attempt-1/results.json',
            results([red]),
        )
        again = 'again/perf-results-cplieger-a-remeasure-shard-1-of-1-attempt-2/results.json'
        self.put(again, results([cell(reps=(1000.0,) * 3)]))
        other = 'again/perf-results-cplieger-a-remeasure-x-remeasure-shard-1-of-1-attempt-2/results.json'
        self.put(other, results([red]))
        stale = 'again/perf-results-cplieger-a-remeasure-shard-1-of-2-attempt-1/results.json'
        self.put(stale, results([red]))
        argv = ('--verdict', str(self.dir / 'again'), '--budgets', b)
        argv += ('--results', str(self.dir / 'results'), '--prefix', 'cplieger-a')
        argv += ('--remeasure', self.put('remeasure.json', decision()))
        self.assertEqual(self.main(*argv, '--remeasure-attempt', '2'), 0)
        self.assertEqual(self.main(*argv, '--remeasure-attempt', '1'), 1)
        self.assertIn('shard counts', self.output)


if __name__ == '__main__':
    unittest.main()
