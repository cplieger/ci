"""perf_gate.py: the tier 2 budget file's rule, the compare and verdict of a
red cell, the ten-run calibration and the provenance of a changed entry."""

from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import perf_gate

SCRIPTS = pathlib.Path(__file__).resolve().parent
TESTDATA = SCRIPTS / 'testdata' / 'perf-gate'

REPO = 'cplieger/marotte'
RUNNER = 'ubuntu-26.04'
HEAD = 'c0ffee'
CELL = {
    'scenario': 'files',
    'scale': 'x10',
    'engine': 'chromium',
    'width': 'desktop',
    'window': 'cold',
}
RUN_URL = 'https://github.com/cplieger/ci/actions/runs/'
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
    e['rows'] = kw.pop('rows', ['F1'])
    e.update(kw)
    return e


def budgets(entries, **kw):
    doc = {
        'schema': 1,
        'runner': RUNNER,
        'headroom': {'cpu_ms': 1.5, 'heap_mb': 1.25, 'rss_mb': 1.25},
        'floor': {'cpu_ms': 50, 'heap_mb': 1},
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

    def test_server_cpu_per_request_budget_is_its_target(self):
        good = entry(metric='server_cpu_ms', baseline=20.0, budget=63.0, target=63.0)
        self.assertEqual(perf_gate.check_rule(budgets([good])), [])
        bad = entry(metric='server_cpu_ms', baseline=20.0, budget=30.0, target=63.0)
        self.assertTrue(perf_gate.check_rule(budgets([bad])))

    def test_an_entry_missing_source_or_rows_fails(self):
        for field, value in (('source', ''), ('rows', [])):
            with self.subTest(field=field):
                self.assertTrue(perf_gate.check_rule(budgets([entry(**{field: value})])))
            with self.subTest(field=field, removed=True):
                e = entry()
                del e[field]
                self.assertTrue(perf_gate.check_rule(budgets([e])))

    def test_an_unknown_tier_fails(self):
        self.assertTrue(perf_gate.check_rule(budgets([entry(tier='maybe', budget=1500.0)])))

    def test_a_duplicate_key_fails(self):
        self.assertTrue(perf_gate.check_rule(budgets([entry(), entry()])))

    def test_a_headroom_of_zero_or_below_fails(self):
        for h in (0, -1.5):
            with self.subTest(own=h):
                self.assertTrue(perf_gate.check_rule(budgets([entry(headroom=h, budget=50.0)])))
            with self.subTest(file=h):
                doc = budgets([entry(budget=50.0)], headroom={'cpu_ms': h})
                self.assertTrue(perf_gate.check_rule(doc))


class Compare(unittest.TestCase):
    def test_an_exact_count_that_differs_from_its_baseline_fails_at_compare(self):
        b = budgets([entry(metric='dom', tier='exact', baseline=640)])
        for reps in ((641, 641, 641), (639, 639, 639)):
            with self.subTest(reps=reps):
                out = perf_gate.compare(b, results([cell('dom', reps, kind='count')]))
                self.assertTrue(out.failures)
                self.assertEqual(out.remeasure, [])
        out = perf_gate.compare(b, results([cell('dom', (640, 640, 640), kind='count')]))
        self.assertEqual(out.failures, [])

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
        b = budgets([entry(metric='server_cpu_ms', baseline=20.0, budget=63.0, target=63.0)])
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

    def test_a_cell_over_its_target_is_flagged_and_does_not_fail(self):
        out = perf_gate.compare(
            budgets([entry(target=300.0)]), results([cell(reps=(900.0, 900.0, 900.0))])
        )
        self.assertEqual(out.failures, [])
        self.assertEqual([keyof(c) for c in out.over_target], [keyof(entry())])

    def test_a_zero_headroom_never_divides(self):
        b = budgets([entry(budget=50.0)], headroom={'cpu_ms': 0})
        out = perf_gate.compare(b, results([cell(reps=(40.0, 40.0, 40.0))]))
        self.assertEqual((out.failures, out.notices), ([], []))

    def test_a_gated_or_exact_entry_the_set_lists_and_no_shard_measured_fails(self):
        b = budgets([entry(), entry('dom', 'exact', 640), entry('frames', 'reported', 9.0)])
        units = {perf_gate.unit_key(CELL)}
        out = perf_gate.compare(b, results([]), units)
        self.assertEqual(len(out.failures), 2)
        self.assertTrue(all('no shard measured' in f for f in out.failures))
        measured = [cell(), cell('dom', (640,) * 3, kind='count')]
        self.assertEqual(perf_gate.compare(b, results(measured), units).failures, [])
        self.assertEqual(perf_gate.compare(b, results([]), set()).failures, [])
        self.assertEqual(perf_gate.compare(b, results([], runner='x'), units).failures, [])


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


def calibrate(runs):
    return perf_gate.calibrate(
        runs,
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

    def test_fewer_than_ten_runs_write_no_tier_or_headroom(self):
        p = calibrate(runs_with('cpu_ms', 1000.0, 1580.0, {1, 2}, n=5))
        self.assertEqual(p['runs'], 5)
        c = only_cell(p)
        self.assertNotIn('tier', c)
        self.assertNotIn('headroom', c)
        self.assertEqual(c['baseline'], 1000.0)
        self.assertEqual(len(c['reps_by_run']), 5)

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
            (p['repo'], p['commit'], p['runner'], p['runs'], p['run_id']),
            (REPO, HEAD, RUNNER, 10, '111'),
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


class Provenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.verified = []
        self.bad_bundles = set()

    def write(self, run_id, doc):
        doc = {**doc, 'run_id': run_id, 'run_url': RUN_URL + run_id}
        (self.dir / f'{run_id}.json').write_text(json.dumps(doc))
        (self.dir / f'{run_id}.sigstore.json').write_text('{}')

    def verify(self, blob, bundle):
        self.verified.append((pathlib.Path(blob).name, pathlib.Path(bundle).name))
        return 'bundle does not verify' if pathlib.Path(bundle).name in self.bad_bundles else None

    def check(self, head, base):
        return perf_gate.check_provenance(
            head,
            base,
            calibrations=self.dir,
            repo=REPO,
            head_sha=HEAD,
            verify=self.verify,
            is_ancestor=lambda sha: sha == HEAD,
        )

    def test_a_raise_naming_a_file_whose_baseline_differs_fails(self):
        self.write('111', calibration_file([cal_cell(baseline=1000.0)]))
        base = budgets([entry(baseline=1000.0)])
        head = budgets([entry(baseline=1200.0, accepted='a new view costs this')])
        self.assertTrue(self.check(head, base))

    def test_a_raise_naming_a_file_whose_bundle_does_not_verify_fails(self):
        self.write('222', calibration_file([cal_cell(baseline=1200.0)]))
        self.bad_bundles.add('222.sigstore.json')
        base = budgets([entry(baseline=1000.0)])
        head = budgets([entry(baseline=1200.0, accepted='why', source=RUN_URL + '222')])
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

    def test_a_raised_baseline_needs_accepted(self):
        self.write('111', calibration_file([cal_cell(baseline=1200.0)]))
        base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        self.assertTrue(self.check(budgets([entry(baseline=1200.0)]), base))
        self.assertEqual(
            self.check(budgets([entry(baseline=1200.0, accepted='costs 200 ms for X')]), base), []
        )

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

    def test_a_five_run_file_lowers_a_gated_baseline_and_keeps_its_tier(self):
        self.write('111', calibration_file([cal_cell(baseline=900.0, tier=None, runs=5)], runs=5))
        base = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        self.assertEqual(self.check(budgets([entry(baseline=900.0)]), base), [])

    def test_a_five_run_file_never_promotes_demotes_or_widens(self):
        self.write('111', calibration_file([cal_cell(baseline=900.0, tier=None, runs=5)], runs=5))
        gated = budgets([entry(baseline=1000.0, source=RUN_URL + '100')])
        reported = budgets([entry(baseline=1000.0, tier='reported', source=RUN_URL + '100')])
        cases = (
            ('promotes', budgets([entry(baseline=900.0)]), reported),
            ('demotes', budgets([entry(baseline=900.0, tier='reported')]), gated),
            ('widens', budgets([entry(baseline=900.0, headroom=1.6)]), gated),
        )
        for name, head, base in cases:
            with self.subTest(name):
                self.assertTrue(self.check(head, base))

    def test_a_five_run_file_adds_only_reported_entries(self):
        self.write('111', calibration_file([cal_cell(baseline=900.0, tier=None, runs=5)], runs=5))
        self.assertTrue(self.check(budgets([entry(baseline=900.0)]), budgets([])))
        self.assertEqual(
            self.check(budgets([entry(baseline=900.0, tier='reported')]), budgets([])), []
        )

    def test_a_five_run_file_whose_leave_one_out_trips_a_gated_cell_fails_naming_ten_runs(self):
        noisy = cal_cell(baseline=1000.0, tier=None, runs=5, slow_runs=(1, 2))
        self.write('111', calibration_file([noisy], runs=5))
        base = budgets([entry(baseline=1100.0, source=RUN_URL + '100')])
        errors = self.check(budgets([entry(baseline=1000.0)]), base)
        self.assertTrue(errors)
        self.assertIn('ten', ' '.join(errors))

    def test_a_gated_entry_dropped_without_retired_fails(self):
        base = budgets([entry(source=RUN_URL + '100')])
        self.assertTrue(self.check(budgets([]), base))
        retired = [{**CELL, 'metric': 'cpu_ms', 'reason': 'the view was removed'}]
        self.assertEqual(self.check(budgets([], retired=retired), base), [])
        no_reason = [{**CELL, 'metric': 'cpu_ms', 'reason': ''}]
        self.assertTrue(self.check(budgets([], retired=no_reason), base))

    def test_a_reported_entry_dropped_needs_no_retired(self):
        base = budgets([entry(tier='reported', source=RUN_URL + '100')])
        self.assertEqual(self.check(budgets([]), base), [])

    def test_an_unchanged_entry_whose_file_is_absent_is_not_rechecked(self):
        b = budgets([entry(source=RUN_URL + '100')])
        self.assertEqual(self.check(b, b), [])
        self.assertEqual(self.verified, [])

    def test_a_change_to_rows_or_target_alone_is_not_rechecked(self):
        base = budgets([entry(source=RUN_URL + '100')])
        head = budgets([entry(source=RUN_URL + '100', rows=['F1', 'F2'], target=300.0)])
        self.assertEqual(self.check(head, base), [])

    def test_a_calibration_file_no_entry_names_fails(self):
        self.write('333', calibration_file([cal_cell()]))
        b = budgets([entry(source=RUN_URL + '100')])
        errors = self.check(b, b)
        self.assertTrue(errors)
        self.assertIn('333', ' '.join(errors))

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

    def test_a_headroom_or_floor_edit_needs_accepted_for_every_budget_it_raises(self):
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
                    self.assertIn('accepted', ' '.join(errors))
                    why = entry(baseline=baseline, budget=raised, accepted='slower parse')
                    self.assertEqual(self.check(budgets([why], **edit), base), [])

    def test_a_headroom_edit_rechecks_an_entry_whose_fields_did_not_change(self):
        lowered = {'cpu_ms': 1.4, 'heap_mb': 1.25, 'rss_mb': 1.25}
        heap = entry('heap_mb', baseline=20.0, budget=25.0, source=RUN_URL + '100')
        base = budgets([entry(), heap])
        head = budgets([entry(budget=1400.0), heap], headroom=lowered)
        self.write('111', calibration_file([cal_cell()]))
        errors = self.check(head, base)
        self.assertTrue(errors)
        self.assertIn('calibration 100', ' '.join(errors))


class Coverage(unittest.TestCase):
    def test_a_gated_or_exact_entry_no_set_lists_fails_unless_retired(self):
        gone = {**CELL, 'scenario': 'gone'}
        dropped = {**entry(), **gone}
        exact = {**entry('dom', 'exact', 640), **gone}
        reported = {**entry('frames', 'reported', 9.0), **gone}
        doc = budgets([entry(), dropped, exact, reported])
        errors = perf_gate.check_plan(doc, PLAN)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e.startswith('gone/') for e in errors))
        retired = [
            {**gone, 'metric': m, 'reason': 'the view was removed'} for m in ('cpu_ms', 'dom')
        ]
        self.assertEqual(perf_gate.check_plan({**doc, 'retired': retired}, PLAN), [])


@unittest.skipUnless(shutil.which('cosign') or os.environ.get('CI'), 'cosign is not installed')
class RealCosign(unittest.TestCase):
    BLOB = TESTDATA / 'trivy_0.76.2_checksums.txt'
    BUNDLE = TESTDATA / 'trivy_0.76.2_checksums.txt.sigstore.json'

    def test_a_bundle_signed_by_another_identity_fails(self):
        self.assertIsNotNone(perf_gate.cosign_verify(self.BLOB, self.BUNDLE))

    def test_the_same_bundle_verifies_for_its_own_signer(self):
        trivy = 'https://github.com/aquasecurity/trivy/.github/workflows/reusable-release.yaml@refs/tags/v0.76.2'
        self.assertIsNone(perf_gate.cosign_verify(self.BLOB, self.BUNDLE, identity=trivy))


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
        'perf-results-cplieger-a-measure-shard-1-of-2',
        'perf-results-cplieger-a-measure-run-03-shard-2-of-2',
        'perf-results-cplieger-a-measure-x-measure-shard-1-of-1',
        'perf-results-cplieger-a-remeasure-shard-1-of-1',
        'perf-results-cplieger-a-remeasure-x-remeasure-shard-1-of-1',
        'perf-results-cplieger-.kiro-measure-shard-1-of-1',
        'perf-results-cplieger-xkiro-measure-shard-1-of-1',
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

    def test_no_prefix_reads_every_file(self):
        self.assertEqual(self.names(), sorted(self.NAMES))

    def test_no_shard_of_this_call_is_no_results(self):
        with self.assertRaisesRegex(ValueError, 'no results'):
            perf_gate.merge_results(self.dir, 'cplieger-b')

    def test_every_name_the_plan_writes_is_read(self):
        for runs in (1, 2):
            for job in perf_gate.plan(PLAN, 'pr', runs=runs)['include']:
                with self.subTest(name=job['name']):
                    self.assertRegex(job['name'], rf'^{perf_gate.SHARD_NAME}$')


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
        (self.dir / 'cal').mkdir()

    def put(self, name, doc):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))
        return str(path)

    def main(self, *argv):
        with redirect_stdout(io.StringIO()):
            return perf_gate.main(list(argv))

    def compare(self, cells, *extra, other=None):
        b = self.put(
            'budgets.json', budgets([entry(source=RUN_URL + '100'), entry('dom', 'exact', 640)])
        )
        if other is None:
            self.put('results/shard-1/results.json', results(cells))
        else:
            own = 'results/perf-results-cplieger-a-measure-shard-1-of-1/results.json'
            self.put(own, results(cells))
            self.put(f'results/{OTHER}-measure-shard-1-of-1/results.json', results(other))
        argv = ('--budgets', b, '--base', b, '--results', str(self.dir / 'results'))
        argv += ('--calibrations', str(self.dir / 'cal'), '--repo', REPO, '--head', HEAD)
        argv += ('--plan-doc', self.put('plan.json', PLAN), '--set', 'pr')
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

    def test_compare_fails_on_an_exact_count(self):
        rc = self.compare([cell(), cell('dom', (700,) * 3, kind='count')])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads((self.dir / 'remeasure.json').read_text())['cells'], [])

    def test_compare_fails_on_a_listed_cell_no_shard_measured(self):
        self.assertEqual(self.compare([cell()]), 1)
        self.assertIn('no shard measured', (self.dir / 'summary.md').read_text())

    def test_compare_needs_the_plan(self):
        b = self.put('budgets.json', budgets([]))
        self.put('results/shard-1/results.json', results([cell()]))
        argv = ('--budgets', b, '--base', b, '--results', str(self.dir / 'results'))
        argv += ('--calibrations', str(self.dir / 'cal'), '--repo', REPO, '--head', HEAD)
        argv += ('--remeasure-out', str(self.dir / 'remeasure.json'))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.main(*argv)

    def test_check_file_fails_a_rule_break(self):
        b = self.put('budgets.json', budgets([entry(budget=1600.0, source=RUN_URL + '100')]))
        argv = (
            '--check-file',
            b,
            '--base',
            b,
            '--calibrations',
            str(self.dir / 'cal'),
            '--repo',
            REPO,
        )
        self.assertEqual(self.main(*argv, '--head', HEAD), 1)

    def test_calibrate_writes_the_proposed_file(self):
        for i in range(10):
            self.put(f'runs/run-{i:02d}/shard-1/results.json', run_doc({'cpu_ms': (1000.0,) * 3}))
        out = self.dir / 'budgets.proposed.json'
        argv = (
            '--calibrate',
            str(self.dir / 'runs'),
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
        self.put('results/shard-1/results.json', results([cell()]))
        out = self.dir / 'bench.json'
        self.assertEqual(self.main('--bench', str(self.dir / 'results'), '--out', str(out)), 0)
        self.assertEqual(json.loads(out.read_text())[0]['value'], 1000.0)

    def test_bench_with_no_shard_of_this_repository_charts_nothing(self):
        self.put(f'results/{OTHER}-measure-shard-1-of-1/results.json', results([cell()]))
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
            'results/perf-results-cplieger-a-measure-shard-1-of-1/results.json', results([red])
        )
        again = 'again/perf-results-cplieger-a-remeasure-shard-1-of-1/results.json'
        self.put(again, results([cell(reps=(1000.0,) * 3)]))
        other = 'again/perf-results-cplieger-a-remeasure-x-remeasure-shard-1-of-1/results.json'
        self.put(other, results([red]))
        argv = ('--verdict', str(self.dir / 'again'), '--budgets', b)
        argv += ('--results', str(self.dir / 'results'), '--prefix', 'cplieger-a')
        self.assertEqual(self.main(*argv), 0)


if __name__ == '__main__':
    unittest.main()
