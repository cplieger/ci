"""perf_trend.py: the trend points of a measured repository's default branch, their
judgement against a trailing baseline, and the issues it opens and closes."""

from __future__ import annotations

import io
import json
import pathlib
import re
import subprocess
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

import ghrest
import perf_gate
import perf_trend

REPO = 'cplieger/marotte'
RUNNER = 'ubuntu-26.04'
PREFIX = 'cplieger-marotte'
SHA = 'fecc0bc00f7597c3d2e32b7279d47f1cf0299ed6'
RUN_URL = 'https://github.com/cplieger/marotte/actions/runs/'
LABEL = 'performance'
UNIT = {'scale': 'x10', 'engine': 'chromium', 'width': 'desktop'}


def rcell(scenario='files', window='cold', metric='cpu_ms', reps=(1000.0,) * 3, kind='time'):
    return {
        'scenario': scenario,
        **UNIT,
        'window': window,
        'metric': metric,
        'kind': kind,
        'reps': list(reps),
    }


def write_shard(directory, name, cells, cpu='AMD EPYC 7763', runner=RUNNER, seconds=()):
    d = pathlib.Path(directory) / f'perf-results-{PREFIX}-measure-{name}-attempt-1'
    d.mkdir()
    timed = [
        {'scenario': s, **UNIT, 'seconds': v, **({'profile_seconds': rest[0]} if rest else {})}
        for s, v, *rest in seconds
    ]
    doc = {'schema': 1, 'runner': runner, 'cpu_model': cpu, 'cells': cells, 'seconds': timed}
    (d / 'results.json').write_text(json.dumps(doc))


def made_point(directory, **kw):
    groups = perf_gate.shard_groups(directory, PREFIX)
    args = {
        'repo': REPO,
        'series': 'dev',
        'ref': 'dev',
        'commit': SHA,
        'date': '2026-11-17T06:09:26Z',
        'run_id': '44029121114',
        'run_url': RUN_URL + '44029121114',
    }
    return perf_trend.point(groups, **{**args, **kw})


def by_cell(point):
    return {
        '/'.join(c[f] for f in ('scenario', 'scale', 'engine', 'width', 'window', 'metric')): c
        for c in point['cells']
    }


class Point(unittest.TestCase):
    """A point: one measurement of the full set, each cell's value over its runs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)

    def test_one_run_makes_a_point_with_every_cell(self):
        write_shard(
            self.dir,
            'shard-1-of-2',
            [
                rcell(reps=(900.0, 1000.0, 1300.0)),
                rcell(metric='dom', reps=(3, 2, 2), kind='count'),
            ],
        )
        write_shard(
            self.dir,
            'shard-2-of-2',
            [
                rcell('docs', metric='long_task_ms', reps=(5.0, 9.0, 7.0), kind='report'),
                rcell(
                    'docs',
                    metric='server_cpu_ms:GET /api/chats',
                    reps=(4.0, 6.0, 5.0),
                    kind='server_cpu',
                ),
            ],
            cpu='Intel Xeon 6975P',
        )
        p = made_point(self.dir)
        self.assertEqual(
            {k: v for k, v in p.items() if k != 'cells'},
            {
                'schema': 1,
                'repo': REPO,
                'series': 'dev',
                'ref': 'dev',
                'commit': SHA,
                'date': '2026-11-17T06:09:26Z',
                'runner': RUNNER,
                'runs': 1,
                'run_id': '44029121114',
                'run_url': RUN_URL + '44029121114',
                'units': [],
            },
        )
        cells = by_cell(p)
        self.assertEqual(
            sorted(cells),
            sorted(
                [
                    'files/x10/chromium/desktop/cold/cpu_ms',
                    'files/x10/chromium/desktop/cold/dom',
                    'docs/x10/chromium/desktop/cold/long_task_ms',
                    'docs/x10/chromium/desktop/cold/server_cpu_ms:GET /api/chats',
                ]
            ),
        )
        cpu = cells['files/x10/chromium/desktop/cold/cpu_ms']
        self.assertEqual(
            (cpu['kind'], cpu['value'], cpu['reps'], cpu['cpu_models']),
            ('time', 1000.0, [[900.0, 1000.0, 1300.0]], ['AMD EPYC 7763']),
        )
        server = cells['docs/x10/chromium/desktop/cold/server_cpu_ms:GET /api/chats']
        self.assertEqual((server['value'], server['cpu_models']), (5.0, ['Intel Xeon 6975P']))
        dom = cells['files/x10/chromium/desktop/cold/dom']
        self.assertEqual(dom, {**dom, 'value': 2})
        self.assertFalse({'reps', 'cpu_models'} & set(dom))
        report = cells['docs/x10/chromium/desktop/cold/long_task_ms']
        self.assertEqual(report['value'], 7.0)
        self.assertFalse({'reps', 'cpu_models'} & set(report))

    def test_ten_runs_make_one_point_of_the_median_of_their_medians(self):
        for r in range(1, 11):
            cpu = 'AMD EPYC 9V74' if r % 2 else 'AMD EPYC 7763'
            write_shard(
                self.dir,
                f'run-{r:02d}-shard-1-of-1',
                [
                    rcell(reps=(1000.0 + r, 1000.0 + r, 5000.0)),
                    rcell(metric='dom', reps=(640, 640, 641 if r > 6 else 640), kind='count'),
                ],
                cpu=cpu,
            )
        p = made_point(self.dir)
        self.assertEqual(p['runs'], 10)
        cpu = by_cell(p)['files/x10/chromium/desktop/cold/cpu_ms']
        self.assertEqual(cpu['value'], 1005.5)
        self.assertEqual(len(cpu['reps']), 10)
        self.assertEqual(cpu['reps'][2], [1003.0, 1003.0, 5000.0])
        self.assertEqual(cpu['cpu_models'][:2], ['AMD EPYC 9V74', 'AMD EPYC 7763'])
        # The lower middle of thirty reps, twenty-six of them 640: a count some rep measured.
        self.assertEqual(by_cell(p)['files/x10/chromium/desktop/cold/dom']['value'], 640)

    def test_a_cell_a_later_run_measured_again_holds_both_runs(self):
        write_shard(self.dir, 'run-01-shard-1-of-1', [rcell(reps=(1000.0,) * 3), rcell('docs')])
        write_shard(self.dir, 'run-02-shard-1-of-1', [rcell(reps=(1200.0,) * 3)])
        p = made_point(self.dir)
        self.assertEqual(p['runs'], 2)
        cells = by_cell(p)
        files = cells['files/x10/chromium/desktop/cold/cpu_ms']
        self.assertEqual((files['value'], len(files['reps'])), (1100.0, 2))
        self.assertEqual(len(cells['docs/x10/chromium/desktop/cold/cpu_ms']['reps']), 1)

    def test_a_point_holds_each_unit_s_seconds_the_median_of_its_runs(self):
        write_shard(self.dir, 'run-01-shard-1-of-2', [rcell()], seconds=[('files', 100.0)])
        write_shard(self.dir, 'run-01-shard-2-of-2', [rcell('docs')], seconds=[('docs', 80.5)])
        write_shard(self.dir, 'run-02-shard-1-of-1', [rcell()], seconds=[('files', 120.0)])
        self.assertEqual(
            made_point(self.dir)['units'],
            [
                {'scenario': 'docs', **UNIT, 'seconds': 80.5},
                {'scenario': 'files', **UNIT, 'seconds': 110.0},
            ],
        )
        for seconds, why in (
            ([('files', 'x')], 'files/x10/chromium/desktop: its seconds are not a number'),
            ([('files', 1.0), ('files', 2.0)], 'files/x10/chromium/desktop: timed twice in run-01'),
        ):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as tmp:
                write_shard(tmp, 'run-01-shard-1-of-1', [rcell()], seconds=seconds)
                with self.assertRaisesRegex(ValueError, f'^{re.escape(why)}$'):
                    made_point(tmp)

    def test_a_unit_s_profiled_rep_is_timed_apart_as_the_median_of_the_runs_that_ran_it(self):
        write_shard(self.dir, 'run-01-shard-1-of-1', [rcell()], seconds=[('files', 100.0, 40.0)])
        write_shard(self.dir, 'run-02-shard-1-of-2', [rcell()], seconds=[('files', 120.0, 50.0)])
        write_shard(self.dir, 'run-02-shard-2-of-2', [rcell('docs')], seconds=[('docs', 80.5)])
        self.assertEqual(
            made_point(self.dir)['units'],
            [
                {'scenario': 'docs', **UNIT, 'seconds': 80.5},
                {'scenario': 'files', **UNIT, 'seconds': 110.0, 'profile_seconds': 45.0},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            write_shard(tmp, 'run-01-shard-1-of-1', [rcell()], seconds=[('files', 1.0, True)])
            why = 'files/x10/chromium/desktop: its profile_seconds are not a number'
            with self.assertRaisesRegex(ValueError, f'^{re.escape(why)}$'):
                made_point(tmp)

    def test_a_timing_cell_with_no_reps_has_no_value(self):
        write_shard(self.dir, 'shard-1-of-1', [rcell(reps=())])
        cpu = by_cell(made_point(self.dir))['files/x10/chromium/desktop/cold/cpu_ms']
        self.assertIsNone(cpu['value'])

    def test_a_cell_field_the_issue_text_cannot_carry_is_refused(self):
        for cell, why in (
            (rcell('settings @owner'), 'whitespace'),
            (rcell(window='<b>'), 'whitespace'),
            (rcell(''), 'empty'),
            (rcell(metric='server_cpu_ms:GET /<x>', kind='server_cpu'), '< or >'),
        ):
            with self.subTest(cell=cell), tempfile.TemporaryDirectory() as tmp:
                write_shard(tmp, 'shard-1-of-1', [cell])
                with self.assertRaisesRegex(ValueError, why):
                    made_point(tmp)

    def test_a_rep_that_is_not_a_number_is_refused(self):
        for cell in (rcell(metric='dom', kind='count', reps=(640, '<b>x</b>')), rcell(reps=['1'])):
            with self.subTest(cell=cell), tempfile.TemporaryDirectory() as tmp:
                write_shard(tmp, 'shard-1-of-1', [cell])
                with self.assertRaisesRegex(ValueError, r'^files/x10/.*: a rep is not a number'):
                    made_point(tmp)

    def test_runs_of_two_runners_or_a_cell_of_two_kinds_are_refused(self):
        write_shard(self.dir, 'run-01-shard-1-of-1', [rcell()])
        write_shard(self.dir, 'run-02-shard-1-of-1', [rcell()], runner='self-hosted')
        with self.assertRaisesRegex(ValueError, 'runner'):
            made_point(self.dir)
        with tempfile.TemporaryDirectory() as other:
            write_shard(other, 'run-01-shard-1-of-1', [rcell()])
            write_shard(other, 'run-02-shard-1-of-1', [rcell(kind='report')])
            with self.assertRaisesRegex(ValueError, 'kind'):
                made_point(other)

    def test_a_point_s_file_name_is_its_date_and_commit(self):
        write_shard(self.dir, 'shard-1-of-1', [rcell()])
        self.assertEqual(
            perf_trend.point_name(made_point(self.dir)), '20261117T060926Z-fecc0bc00f75.json'
        )

    def test_a_stored_point_reads_back_as_written_one_cell_a_line(self):
        write_shard(
            self.dir, 'shard-1-of-1', [rcell(), rcell('docs'), rcell(metric='dom', kind='count')]
        )
        p = made_point(self.dir)
        text = perf_trend.dumps(p)
        self.assertEqual(json.loads(text), p)
        cell_lines = [line for line in text.splitlines() if line.startswith('{"scenario"')]
        self.assertEqual(len(cell_lines), 3)

    def test_the_point_command_refuses_a_run_missing_a_shard(self):
        write_shard(self.dir, 'run-01-shard-1-of-2', [rcell()])
        write_shard(self.dir, 'run-01-shard-2-of-2', [rcell('docs')])
        write_shard(self.dir, 'run-02-shard-2-of-2', [rcell('docs')])
        out = self.dir / 'point.json'
        argv = [
            'point',
            '--results',
            str(self.dir),
            '--prefix',
            PREFIX,
            '--repo',
            REPO,
            '--series',
            'dev',
            '--ref',
            'dev',
            '--commit',
            SHA,
            '--date',
            '2026-11-17T06:09:26Z',
            '--run-id',
            '1',
            '--run-url',
            RUN_URL + '1',
            '--out',
            str(out),
        ]
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(perf_trend.main(argv), 1)
        self.assertIn('run-02 has no results of shard 1 of 2', err.getvalue())
        self.assertFalse(out.exists())
        (self.dir / f'perf-results-{PREFIX}-measure-run-02-shard-2-of-2-attempt-1').rename(
            self.dir / 'elsewhere'
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(perf_trend.main(argv), 0)
        self.assertEqual(json.loads(out.read_text())['runs'], 1)


PURE_SHA = '{:040x}'
GOOD = {('files', 'cold', 'cpu_ms'): 1000.0, ('files', 'live', 'cpu_ms'): 3000.0}


def kind_of(metric):
    if metric == 'dom':
        return 'count'
    return 'server_cpu' if metric.startswith('server_cpu_ms') else 'time'


def tp(values, n, *, series='dev', runner=RUNNER):
    """Point n of a series: values maps (scenario, window, metric) to a cell's value."""
    cells = []
    for (scenario, window, metric), value in sorted(values.items()):
        c = {'scenario': scenario, **UNIT, 'window': window, 'metric': metric}
        c['kind'] = kind_of(metric)
        c['value'] = value
        if c['kind'] != 'count':
            c['reps'] = [[value] * 3]
            c['cpu_models'] = ['AMD EPYC 7763']
        cells.append(c)
    return {
        'schema': 1,
        'repo': REPO,
        'series': series,
        'ref': 'dev',
        'commit': PURE_SHA.format(n),
        'date': f'2026-11-{1 + n // 24:02d}T{n % 24:02d}:00:00Z',
        'runner': runner,
        'runs': 1,
        'run_id': str(n),
        'run_url': f'{RUN_URL}{n}',
        'units': [],
        'cells': cells,
    }


class Trend:
    """A series judged point by point, as the record job judges it."""

    def __init__(self, series='dev', base=GOOD, points=7, history=()):
        self.series, self.n = series, 100 if series != 'dev' else 0
        self.history = list(history)
        self.state = perf_trend.new_state(series)
        for _ in range(points):
            self.add(base)

    def add(self, values, runner=RUNNER):
        self.n += 1
        judged = perf_trend.judge(
            self.history,
            tp(values, self.n, series=self.series, runner=runner),
            self.state,
            repo=REPO,
        )
        self.state = judged.state
        self.history.append(perf_trend.seen(judged.point))
        return judged

    def scenario(self, name='files'):
        return self.state['scenarios'].get(name, {}).get('status', 'ok')


PR_UNITS = perf_gate.planned_windows([{'scenario': 'files', **UNIT}])
PLAN = {
    'node': 'web',
    'sets': {'full': {'shards': 1, 'cells': [{'scenario': 'files', **UNIT, 'seconds': 100}]}},
}


def slow(cold=1000.0, live=3000.0, **more):
    return {('files', 'cold', 'cpu_ms'): cold, ('files', 'live', 'cpu_ms'): live, **more}


class Judge(unittest.TestCase):
    """A scenario's issue opens after two consecutive bad points and closes after two good."""

    def test_one_bad_point_opens_nothing(self):
        t = Trend()
        judged = t.add(slow(cold=3000.0))
        self.assertEqual(judged.actions, [])
        self.assertEqual(t.scenario(), 'suspect')

    def test_two_bad_points_open_one_issue_naming_cells_values_commits_and_runs(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        judged = t.add(slow(cold=3100.0))
        self.assertEqual(t.scenario(), 'open')
        self.assertEqual(len(judged.actions), 1)
        a = judged.actions[0]
        self.assertEqual(
            (a['op'], a['title'], a['marker'], a['labels']),
            ('upsert', 'Performance regression in files', 'perf-trend:dev:files', ['performance']),
        )
        self.assertNotIn('comment', a)
        body = a['body']
        self.assertTrue(body.startswith('<!-- perf-trend:dev:files -->\n'))
        self.assertIn('| files/x10/chromium/desktop/cold/cpu_ms | 1000 | 3100 | 3.10x |', body)
        self.assertNotIn('live', body)
        compare = f'https://github.com/{REPO}/compare/{PURE_SHA.format(7)}...{PURE_SHA.format(8)}'
        self.assertIn(compare, body)
        self.assertIn(f'{RUN_URL}8', body)
        self.assertIn(f'{RUN_URL}9', body)

    def test_a_different_cell_bad_at_the_next_point_is_a_new_suspicion(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        self.assertEqual(t.add(slow(live=9000.0)).actions, [])
        self.assertEqual(t.scenario(), 'suspect')
        judged = t.add(slow(live=9000.0))
        self.assertIn(
            f'https://github.com/{REPO}/compare/{PURE_SHA.format(8)}...{PURE_SHA.format(9)}',
            judged.actions[0]['body'],
        )

    def test_a_third_bad_point_with_a_new_cell_updates_the_same_issue(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        opened = t.add(slow(cold=3000.0))
        third = t.add(slow(cold=3000.0, live=9000.0))
        fourth = t.add(slow(cold=3200.0, live=9000.0))
        for judged in (third, fourth):
            self.assertEqual([a['op'] for a in judged.actions], ['upsert'])
            self.assertEqual(judged.actions[0]['marker'], 'perf-trend:dev:files')
            self.assertIn('Still over the thresholds at', judged.actions[0]['comment'])
        self.assertNotIn('/live/cpu_ms', opened.actions[0]['body'])
        self.assertIn('/live/cpu_ms', third.actions[0]['body'])
        body = fourth.actions[0]['body']
        self.assertIn('| files/x10/chromium/desktop/cold/cpu_ms | 1000 | 3200 | 3.20x |', body)
        self.assertIn('| files/x10/chromium/desktop/live/cpu_ms | 3000 | 9000 | 3.00x |', body)
        self.assertIn(
            'files/x10/chromium/desktop/cold/cpu_ms 3200 against 1000 (3.20x)',
            fourth.actions[0]['comment'],
        )

    def test_two_good_points_close_it(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        t.add(slow(cold=3000.0))
        self.assertEqual(t.add(GOOD).actions, [])
        self.assertEqual(t.scenario(), 'recovering')
        judged = t.add(GOOD)
        self.assertEqual(
            [(a['op'], a['marker']) for a in judged.actions], [('close', 'perf-trend:dev:files')]
        )
        self.assertIn('Within the thresholds again', judged.actions[0]['comment'])
        self.assertEqual(t.scenario(), 'ok')

    def test_good_then_bad_keeps_it_open(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        t.add(slow(cold=3000.0))
        t.add(GOOD)
        judged = t.add(slow(cold=3000.0))
        self.assertEqual([a['op'] for a in judged.actions], ['upsert'])
        self.assertEqual(t.scenario(), 'open')
        self.assertEqual(t.add(GOOD).actions, [])
        self.assertEqual([a['op'] for a in t.add(GOOD).actions], ['close'])

    def test_a_spread_slowdown_across_a_scenario_opens_through_the_group_rule(self):
        base = {('files', w, 'cpu_ms'): 1000.0 for w in ('cold', 'rest', 'live', 'away')}
        base[('files', 'back', 'cpu_ms')] = 100.0
        t = Trend(base=base)
        spread = {k: v * 1.35 for k, v in base.items()}
        self.assertEqual(t.add(spread).actions, [])
        judged = t.add(spread)
        self.assertEqual([a['op'] for a in judged.actions], ['upsert'])
        body = judged.actions[0]['body']
        self.assertIn('cpu_ms of 4 cells over 250 ms: median 1.35x its baseline, past 1.3x', body)
        for w in ('cold', 'rest', 'live', 'away'):
            self.assertIn(f'| files/x10/chromium/desktop/{w}/cpu_ms | 1000 | 1350 | 1.35x |', body)
        self.assertNotIn('/back/', body)
        under = {k: v * 1.25 for k, v in base.items()}
        u = Trend(base=base)
        self.assertEqual(u.add(under).actions + u.add(under).actions, [])

    def test_a_group_needs_three_cells_over_the_floor(self):
        base = {('files', w, 'cpu_ms'): 1000.0 for w in ('cold', 'rest')}
        t = Trend(base=base)
        spread = {k: v * 1.5 for k, v in base.items()}
        self.assertEqual(t.add(spread).actions + t.add(spread).actions, [])

    def test_a_tiny_cell_never_opens(self):
        tiny = {('history-runs', 'rest', 'cpu_ms'): 9.0}
        t = Trend(base=tiny)
        doubled = {('history-runs', 'rest', 'cpu_ms'): 18.0}
        self.assertEqual(t.add(doubled).actions + t.add(doubled).actions, [])
        self.assertEqual(t.scenario('history-runs'), 'ok')

    def test_a_changed_count_is_information_and_joins_an_open_issue(self):
        dom = ('files', 'cold', 'dom')
        t = Trend(base={**GOOD, dom: 640})
        for _ in range(3):
            judged = t.add({**GOOD, dom: 700})
            self.assertEqual(judged.actions, [])
        self.assertEqual(t.scenario(), 'ok')
        u = Trend(base={**GOOD, dom: 640})
        first = u.add({**slow(cold=3000.0), dom: 640})
        self.assertIn('No count changed since the previous point.', first.summary)
        changed = u.add({**slow(cold=3000.0), dom: 700})
        self.assertIn('### Counts changed since the previous point (information)', changed.summary)
        self.assertIn('| files/x10/chromium/desktop/cold/dom | 640 | 700 |', changed.summary)
        self.assertIn(
            'Counts that changed: `files/x10/chromium/desktop/cold/dom` 640 to 700.',
            changed.actions[0]['body'],
        )

    def test_an_issue_a_person_closed_accepts_the_new_values(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        opened = t.add(slow(cold=3000.0))
        marker = opened.actions[0]['marker']
        issue = {
            'number': 7,
            'state': 'closed',
            'closed_at': '2026-11-01T10:00:00Z',
            'body': opened.actions[0]['body'],
        }
        t.state, actions = perf_trend.observe(t.state, [issue], series='dev')
        self.assertEqual(actions, [])
        self.assertEqual(t.scenario(), 'ok')
        for _ in range(3):
            self.assertEqual(t.add(slow(cold=3000.0)).actions, [])
            self.assertEqual(t.scenario(), 'ok')
        # The accepted values are the baseline now, so a further doubling is a new regression.
        t.add(slow(cold=6500.0))
        self.assertEqual([a['marker'] for a in t.add(slow(cold=6500.0)).actions], [marker])

    def test_an_issue_still_open_or_never_created_is_not_accepted(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        opened = t.add(slow(cold=3000.0))
        body = opened.actions[0]['body']
        older = {'number': 3, 'state': 'closed', 'closed_at': '2026-10-01T00:00:00Z', 'body': body}
        mine = {**older, 'number': 9, 'state': 'open', 'closed_at': None}
        for issues in ([], [older], [mine]):
            with self.subTest(issues=issues):
                state, _ = perf_trend.observe(t.state, issues, series='dev')
                self.assertEqual(state['scenarios']['files']['status'], 'open')
        self.assertEqual(perf_trend.observe(t.state, [mine], series='dev')[1], [])

    def test_an_open_regression_whose_issue_was_never_filed_is_filed_again(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        opened = t.add(slow(cold=3000.0))
        t.add(GOOD)
        self.assertEqual(t.scenario(), 'recovering')
        older = {'number': 3, 'state': 'closed', 'closed_at': '2026-10-01T00:00:00Z'}
        for issues in ([], [{**older, 'body': opened.actions[0]['body']}]):
            with self.subTest(issues=issues):
                _, actions = perf_trend.observe(t.state, issues, series='dev')
                self.assertEqual(actions, [{**opened.actions[0]}])

    def test_an_older_open_issue_apply_adopted_is_the_regression_s_and_its_hand_close_accepts(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        opened = t.add(slow(cold=3000.0))
        adopted = {'number': 3, 'state': 'open', 'closed_at': None}
        adopted['body'] = '<!-- perf-trend:dev:files -->\nan earlier regression'
        gh = FakeGitHub([{**adopted, 'title': 'x'}])
        perf_trend.apply(opened.actions, REPO, ghrest.Client(run=gh, sleep=lambda _s: None))
        self.assertEqual([w[1] for w in gh.writes], ['issues/3'])
        self.assertEqual(perf_trend.observe(t.state, [adopted], series='dev')[1], [])
        self.assertTrue(perf_trend.skip(t.state, t.state['last']['commit'], RUNNER))
        closed = {**adopted, 'state': 'closed', 'closed_at': '2026-11-01T12:00:00Z'}
        state, actions = perf_trend.observe(t.state, [closed], series='dev')
        self.assertEqual(actions, [])
        self.assertEqual(state['scenarios']['files']['status'], 'ok')
        t.state = state
        for _ in range(2):
            self.assertEqual(t.add(slow(cold=3000.0)).actions, [])
        self.assertEqual(perf_trend.observe(t.state, [closed], series='dev')[1], [])

    def test_an_open_issue_of_a_scenario_within_the_thresholds_is_closed(self):
        t = Trend()
        issue = {
            'number': 4,
            'state': 'open',
            'closed_at': None,
            'body': '<!-- perf-trend:dev:files -->\nbody',
        }
        _, actions = perf_trend.observe(t.state, [issue], series='dev')
        self.assertEqual(
            [(a['op'], a['marker']) for a in actions], [('close', 'perf-trend:dev:files')]
        )
        _, other = perf_trend.observe(t.state, [issue], series='proof')
        self.assertEqual(other, [])

    def test_a_cell_new_to_the_series_is_not_judged(self):
        t = Trend()
        new = {**GOOD, ('files', 'back', 'cpu_ms'): 99999.0}
        self.assertEqual(t.add(new).actions + t.add(new).actions, [])

    def test_points_of_another_runner_are_no_baseline(self):
        t = Trend(points=0)
        for _ in range(7):
            t.add(slow(cold=100.0), runner='self-hosted')
        self.assertEqual(t.add(GOOD).actions + t.add(GOOD).actions, [])

    def test_a_streak_longer_than_the_history_keeps_its_baseline(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        t.add(slow(cold=3000.0))
        for _ in range(perf_trend.HISTORY + 2):
            t.add(slow(cold=3000.0))
            t.history = t.history[-perf_trend.HISTORY :]
        self.assertEqual(t.scenario(), 'open')

    def test_a_runner_change_ends_the_old_runner_s_regressions_and_judges_the_new_one_apart(self):
        other = 'self-hosted'
        t = Trend()
        t.add(slow(cold=3000.0))
        t.add(slow(cold=3000.0))
        self.assertEqual(t.scenario(), 'open')
        self.assertFalse(perf_trend.skip(t.state, t.state['last']['commit'], other))
        switched = t.add(slow(cold=3000.0), runner=other)
        self.assertEqual(
            [(a['op'], a['marker']) for a in switched.actions], [('close', 'perf-trend:dev:files')]
        )
        self.assertEqual(
            switched.actions[0]['comment'],
            'The runner changed to `self-hosted`, so this issue is closed and the scenario is '
            'judged again on that runner, against its own baselines.',
        )
        self.assertEqual(t.scenario(), 'ok')
        self.assertIn('### Regressions ended at this point', switched.summary)
        self.assertIn('| files | open | the runner changed to `self-hosted` |', switched.summary)
        for n in range(2, perf_gate.GUARD_POINTS + 1):
            judged = t.add(slow(cold=99999.0) if n == 3 else GOOD, runner=other)
            self.assertEqual(judged.actions, [])
            self.assertFalse(any('baseline' in c for c in judged.point['cells']), n)
            self.assertTrue(perf_trend.skip(t.state, t.state['last']['commit'], other))
        t.add(slow(cold=5000.0), runner=other)
        self.assertEqual(t.scenario(), 'suspect')
        self.assertFalse(perf_trend.skip(t.state, t.state['last']['commit'], other))
        opened = t.add(slow(cold=5000.0), runner=other)
        self.assertEqual([a['op'] for a in opened.actions], ['upsert'])
        self.assertIn(
            '| files/x10/chromium/desktop/cold/cpu_ms | 1000 | 5000 | 5.00x |',
            opened.actions[0]['body'],
        )
        filed = {'number': 2, 'state': 'open', 'closed_at': None}
        filed['body'] = opened.actions[0]['body']
        self.assertTrue(perf_trend.skip(t.state, t.state['last']['commit'], other))
        self.assertEqual(t.add(GOOD, runner=other).actions, [])
        self.assertEqual([a['op'] for a in t.add(GOOD, runner=other).actions], ['close'])
        self.assertTrue(perf_trend.skip(t.state, t.state['last']['commit'], other))

    def test_a_runner_change_drops_a_suspect_and_closes_a_recovering_regression(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        judged = t.add(GOOD, runner='self-hosted')
        self.assertEqual(judged.actions, [])
        self.assertEqual(t.state['scenarios'], {})
        self.assertTrue(perf_trend.skip(t.state, t.state['last']['commit'], 'self-hosted'))
        u = Trend()
        u.add(slow(cold=3000.0))
        u.add(slow(cold=3000.0))
        u.add(GOOD)
        self.assertEqual(u.scenario(), 'recovering')
        self.assertEqual([a['op'] for a in u.add(GOOD, runner='self-hosted').actions], ['close'])
        self.assertEqual(u.state['scenarios'], {})

    def test_a_scenario_the_full_set_no_longer_measures_is_dropped_from_dev(self):
        chat = ('chat', 'cold', 'cpu_ms')
        t = Trend(base={**GOOD, chat: 1000.0})
        t.add({**slow(cold=5000.0), chat: 1000.0})
        self.assertEqual(t.scenario(), 'suspect')
        renamed = {('files-v2', 'cold', 'cpu_ms'): 1000.0, chat: 1000.0}
        for _ in range(2):
            self.assertEqual(t.add(renamed).actions, [])
        self.assertEqual(t.state['scenarios'], {})
        self.assertTrue(perf_trend.skip(t.state, t.state['last']['commit'], RUNNER))
        u = Trend(base={**GOOD, chat: 1000.0})
        u.add({**slow(cold=5000.0), chat: 1000.0})
        u.add({**slow(cold=5000.0), chat: 1000.0})
        self.assertEqual(u.scenario(), 'open')
        gone = u.add(renamed)
        self.assertEqual(
            [(a['op'], a['marker'], a['comment']) for a in gone.actions],
            [
                (
                    'close',
                    'perf-trend:dev:files',
                    'The full set no longer measures files, so this issue is closed.',
                )
            ],
        )
        self.assertEqual(u.state['scenarios'], {})
        self.assertIn('| files | open | the full set no longer measures it |', gone.summary)
        proof = Trend('proof', base={**GOOD, chat: 1000.0})
        proof.add({**slow(cold=5000.0), chat: 1000.0})
        self.assertEqual(proof.add({chat: 1000.0}).actions, [])
        self.assertEqual(proof.scenario(), 'suspect')

    def test_a_cell_is_judged_once_five_points_of_its_runner_measured_it(self):
        t = Trend(points=perf_gate.GUARD_POINTS - 1)
        early = t.add(slow(cold=99999.0))
        self.assertFalse(any('baseline' in c for c in early.point['cells']))
        u = Trend(points=perf_gate.GUARD_POINTS)
        judged = u.add(slow(cold=99999.0))
        cold = next(c for c in judged.point['cells'] if c['window'] == 'cold')
        self.assertEqual((cold['baseline'], cold['bad']), (1000.0, True))

    def test_a_returning_runner_is_judged_only_on_its_points_since_it_last_took_over(self):
        other = 'self-hosted'
        t = Trend(points=perf_gate.GUARD_POINTS)
        for _ in range(8):
            t.add(slow(cold=3000.0), runner=other)
        for _ in range(perf_gate.GUARD_POINTS):
            judged = t.add(slow(cold=3000.0))
            self.assertFalse(any('baseline' in c for c in judged.point['cells']))
            self.assertEqual((judged.actions, t.scenario()), ([], 'ok'))
        judged = t.add(slow(cold=3000.0))
        cold = next(c for c in judged.point['cells'] if c['window'] == 'cold')
        self.assertEqual((cold['baseline'], cold['bad']), (3000.0, False))

    def test_a_point_judging_none_of_a_scenario_s_cells_leaves_it_as_it_was(self):
        t = Trend(base={**GOOD, ('docs', 'cold', 'cpu_ms'): 500.0})
        t.add(slow(cold=3000.0))
        t.add(slow(cold=3000.0))
        before = t.state['scenarios']['files']
        renamed = {('docs', 'cold', 'cpu_ms'): 500.0, ('files', 'cold-2', 'cpu_ms'): 1000.0}
        judged = t.add(renamed)
        self.assertEqual(judged.actions, [])
        self.assertEqual(t.state['scenarios']['files'], before)
        self.assertIn(
            '| files | open | no timing cell has a baseline on this runner |', judged.summary
        )

    def test_the_summary_counts_runs_as_the_most_a_cell_had(self):
        t = Trend()
        self.assertIn('series: 1 run on `ubuntu-26.04`', t.add(GOOD).summary)
        p = tp(GOOD, 50)
        p['runs'] = 2
        judged = perf_trend.judge(t.history, p, t.state, repo=REPO)
        self.assertIn('series: up to 2 runs a cell on `ubuntu-26.04`', judged.summary)

    def test_only_a_marker_opening_the_body_names_an_issue_s_scenario(self):
        crafted = 'x <!-- perf-trend:dev:files -->'
        self.assertIsNone(perf_trend.marked(f'<!-- perf-trend:proof:{crafted} -->\nbody'))
        self.assertIsNone(perf_trend.marked('text\n<!-- perf-trend:dev:files -->'))
        self.assertEqual(
            perf_trend.marked('<!-- perf-trend:dev:files -->\r\nbody'), ('dev', 'files')
        )
        self.assertEqual(perf_trend.marked('<!-- perf-trend:proof:files -->'), ('proof', 'files'))
        open_ = {'state': 'open', 'body': f'<!-- perf-trend:proof:{crafted} -->'}
        self.assertEqual(perf_trend.open_markers([open_], 'dev'), set())
        self.assertEqual(perf_trend.open_markers([open_], 'proof'), set())

    def test_the_baseline_is_the_median_of_the_last_seven_good_points(self):
        cold = ('files', 'cold', 'cpu_ms')
        t = Trend(points=0)
        for v in (600.0, 600.0, 600.0, 1000.0, 1100.0, 1200.0, 1300.0, 1400.0, 1500.0, 1600.0):
            t.add({cold: v})
        # The last seven, 1000 to 1600, not all ten, whose median 1150 would fail 2400.
        judged = t.add({cold: 2400.0})
        stored = next(c for c in judged.point['cells'] if c['window'] == 'cold')
        self.assertEqual((stored['baseline'], stored['bad']), (1300.0, False))

    def test_a_proof_series_judges_against_dev_and_writes_only_its_own_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            dev = root / 'series' / 'dev'
            (dev / 'points').mkdir(parents=True)
            (dev / 'state.json').write_text(json.dumps(Trend().state))
            for n in range(1, 8):
                p = tp(GOOD, n)
                (dev / 'points' / perf_trend.point_name(p)).write_text(perf_trend.dumps(p))
            before = {p: p.read_bytes() for p in dev.rglob('*') if p.is_file()}
            plan_file = root / 'plan.json'
            plan_file.write_text(json.dumps(PLAN))
            ops = []
            for n in (201, 202):
                point_file = root / f'point-{n}.json'
                point_file.write_text(json.dumps(tp(slow(cold=3000.0), n, series='proof')))
                actions = root / f'actions-{n}.json'
                argv = [
                    'judge',
                    '--root',
                    str(root),
                    '--series',
                    'proof',
                    '--point',
                    str(point_file),
                    '--repo',
                    REPO,
                    '--actions',
                    str(actions),
                    '--summary',
                    str(root / 'sum.md'),
                    '--plan',
                    str(plan_file),
                ]
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(perf_trend.main(argv), 0)
                ops += json.loads(actions.read_text())
            after = {p: p.read_bytes() for p in dev.rglob('*') if p.is_file()}
            self.assertEqual(after, before)
            proof = root / 'series' / 'proof'
            self.assertEqual(len(list((proof / 'points').iterdir())), 2)
            self.assertEqual(
                json.loads((proof / 'state.json').read_text())['scenarios']['files']['status'],
                'open',
            )
            self.assertEqual(
                [(a['title'], a['marker']) for a in ops],
                [('Performance regression in files (proof series)', 'perf-trend:proof:files')],
            )


class Skip(unittest.TestCase):
    def test_skip_only_an_unchanged_commit_with_nothing_pending(self):
        t = Trend()
        last = t.state['last']['commit']
        self.assertTrue(perf_trend.skip(t.state, last, RUNNER))
        self.assertFalse(perf_trend.skip(t.state, PURE_SHA.format(99), RUNNER))
        self.assertFalse(perf_trend.skip(perf_trend.new_state('dev'), last, RUNNER))
        t.add(slow(cold=3000.0))
        self.assertFalse(perf_trend.skip(t.state, t.state['last']['commit'], RUNNER))
        t.add(slow(cold=3000.0))
        self.assertEqual(t.scenario(), 'open')
        self.assertTrue(perf_trend.skip(t.state, t.state['last']['commit'], RUNNER))
        t.add(GOOD)
        self.assertEqual(t.scenario(), 'recovering')
        self.assertFalse(perf_trend.skip(t.state, t.state['last']['commit'], RUNNER))

    def test_a_lost_issue_write_is_skipped_and_repaired_without_a_point(self):
        t = Trend()
        t.add(slow(cold=3000.0))
        opened = t.add(slow(cold=3000.0))
        last = t.state['last']['commit']
        self.assertTrue(perf_trend.skip(t.state, last, RUNNER))
        self.assertEqual(perf_trend.observe(t.state, [], series='dev')[1], opened.actions)
        u = Trend()
        stray = {**opened.actions[0], 'number': 2, 'state': 'open'}
        self.assertTrue(perf_trend.skip(u.state, u.state['last']['commit'], RUNNER))
        self.assertEqual(
            [a['op'] for a in perf_trend.observe(u.state, [stray], series='dev')[1]], ['close']
        )

    def test_a_head_the_guard_baseline_does_not_hold_yet_is_measured_again(self):
        t = Trend()
        last = t.state['last']
        behind = {**t.state, 'last': {**last, 'guard_behind': '1 timing cell is past the guard'}}
        self.assertFalse(perf_trend.skip(behind, last['commit'], RUNNER))
        held = {**t.state, 'last': {**last, 'guard_behind': None}}
        self.assertTrue(perf_trend.skip(held, last['commit'], RUNNER))

    def test_a_point_on_another_runner_than_the_last_is_measured(self):
        t = Trend()
        self.assertFalse(perf_trend.skip(t.state, t.state['last']['commit'], 'self-hosted'))
        held = {**t.state, 'scenarios': {'files': {'status': 'suspect', 'runner': 'self-hosted'}}}
        self.assertTrue(perf_trend.skip(held, t.state['last']['commit'], RUNNER))


class FakeGitHub:
    """The issue endpoints apply and fetch_issues call, answering as `gh api -i` prints."""

    def __init__(self, issues=(), label=True, fail=None):
        self.issues = {i['number']: dict(i) for i in issues}
        self.label = label
        self.fail = fail or {}
        self.writes = []

    def reply(self, status, body=None):
        raw = b'' if body is None else json.dumps(body).encode()
        out = f'HTTP/2.0 {status} Reason\n\r\n'.encode() + raw
        return subprocess.CompletedProcess(['gh'], 0 if status < 300 else 1, out, b'')

    def __call__(self, args, stdin=None, timeout=None):
        method = args[args.index('-X') + 1] if '-X' in args else 'GET'
        path = next(a for a in args[2:] if a.startswith('repos/'))
        body = json.loads(stdin) if stdin else None
        for (m, prefix), status in self.fail.items():
            if m == method and path.startswith(prefix):
                return self.reply(status, {'message': 'Validation Failed'})
        if method != 'GET':
            self.writes.append((method, path.removeprefix(f'repos/{REPO}/'), body))
        route = path.split('?')[0].removeprefix(f'repos/{REPO}/')
        if route == 'labels/performance':
            return self.reply(200, {'name': LABEL}) if self.label else self.reply(404, {})
        if route == 'labels':
            self.label = True
            return self.reply(201, body)
        if route == 'issues' and method == 'GET':
            if 'page=1' not in path.split('&per_page')[1]:
                return self.reply(200, [])
            state = re.search(r'state=(\w+)', path)[1]
            rows = [i for i in self.issues.values() if state == 'all' or i['state'] == state]
            return self.reply(200, rows)
        if route == 'issues':
            n = max(self.issues, default=0) + 1
            self.issues[n] = {'number': n, 'state': 'open', 'closed_at': None, **body}
            return self.reply(201, self.issues[n])
        number = int(route.split('/')[1])
        if route.endswith('/comments'):
            return self.reply(201, {'id': 1})
        self.issues[number].update(body)
        return self.reply(200, self.issues[number])


def issue(number, scenario='files', state='open', series='dev', body='old'):
    return {
        'number': number,
        'state': state,
        'title': f'Performance regression in {scenario}',
        'closed_at': None,
        'body': f'<!-- perf-trend:{series}:{scenario} -->\n{body}',
        'labels': [{'name': LABEL}],
    }


def upsert(body='new', comment=None, scenario='files'):
    a = {
        'op': 'upsert',
        'marker': f'perf-trend:dev:{scenario}',
        'title': f'Performance regression in {scenario}',
        'labels': [LABEL],
        'body': f'<!-- perf-trend:dev:{scenario} -->\n{body}',
    }
    return {**a, 'comment': comment} if comment else a


class Issues(unittest.TestCase):
    def apply(self, gh, actions):
        return perf_trend.apply(actions, REPO, ghrest.Client(run=gh, sleep=lambda _s: None))

    def test_an_upsert_with_no_open_issue_creates_it_labelled_and_assigned(self):
        gh = FakeGitHub(label=False)
        self.assertEqual(self.apply(gh, [upsert()]), ['opened #1: Performance regression in files'])
        (label, created) = gh.writes
        self.assertEqual(label[:2], ('POST', 'labels'))
        self.assertEqual(label[2]['name'], LABEL)
        self.assertEqual(
            created,
            (
                'POST',
                'issues',
                {
                    'title': 'Performance regression in files',
                    'body': '<!-- perf-trend:dev:files -->\nnew',
                    'labels': [LABEL],
                    'assignees': ['cplieger'],
                },
            ),
        )
        again = FakeGitHub()
        self.apply(again, [upsert()])
        self.assertEqual([w[1] for w in again.writes], ['issues'])

    def test_an_upsert_edits_the_open_issue_and_never_opens_a_second(self):
        gh = FakeGitHub([issue(5), issue(6, 'docs')])
        lines = self.apply(gh, [upsert(comment='Still over.')])
        self.assertEqual(lines, ['updated #5: Performance regression in files', 'commented on #5'])
        self.assertEqual(
            gh.writes,
            [
                (
                    'PATCH',
                    'issues/5',
                    {
                        'title': 'Performance regression in files',
                        'body': '<!-- perf-trend:dev:files -->\nnew',
                    },
                ),
                ('POST', 'issues/5/comments', {'body': 'Still over.'}),
            ],
        )
        same = FakeGitHub([issue(5, body='new')])
        self.assertEqual(self.apply(same, [upsert()]), [])
        self.assertEqual(same.writes, [])

    def test_a_newer_duplicate_is_closed_and_the_oldest_kept(self):
        gh = FakeGitHub([issue(9), issue(4), issue(7, series='proof')])
        lines = self.apply(gh, [upsert()])
        self.assertEqual(lines[0], 'closed #9 as a duplicate of #4')
        self.assertIn(
            ('PATCH', 'issues/9', {'state': 'closed', 'state_reason': 'not_planned'}), gh.writes
        )
        self.assertEqual(lines[1], 'updated #4: Performance regression in files')
        self.assertNotIn(7, {int(w[1].split('/')[1]) for w in gh.writes if '/' in w[1]})

    def test_a_close_comments_on_and_closes_each_open_issue_of_its_marker(self):
        gh = FakeGitHub([issue(3), issue(8, 'docs'), {**issue(2), 'pull_request': {}}])
        close = {**upsert(), 'op': 'close', 'comment': 'Within the thresholds again.'}
        del close['body']
        self.assertEqual(
            self.apply(gh, [close, upsert()]),
            [
                'closed #3: Performance regression in files',
                'opened #9: Performance regression in files',
            ],
        )
        self.assertEqual(
            gh.writes[:2],
            [
                ('POST', 'issues/3/comments', {'body': 'Within the thresholds again.'}),
                ('PATCH', 'issues/3', {'state': 'closed', 'state_reason': 'completed'}),
            ],
        )

    def test_a_label_another_run_created_first_is_used(self):
        gh = FakeGitHub(label=False, fail={('POST', f'repos/{REPO}/labels'): 422})
        real = gh.__call__

        def racing(args, stdin=None, timeout=None):
            out = real(args, stdin, timeout)
            if next(a for a in args[2:] if a.startswith('repos/')).endswith('/labels'):
                gh.label = True
            return out

        client = ghrest.Client(run=racing, sleep=lambda _s: None)
        lines = perf_trend.apply([upsert()], REPO, client)
        self.assertEqual(lines, ['opened #1: Performance regression in files'])
        lost = FakeGitHub(label=False, fail={('POST', f'repos/{REPO}/labels'): 422})
        with self.assertRaisesRegex(ghrest.ApiError, '422'):
            self.apply(lost, [upsert()])

    def test_an_issue_naming_the_marker_past_its_first_line_is_not_adopted(self):
        crafted = {
            **issue(2, series='proof'),
            'body': '<!-- perf-trend:proof:x -->\n<!-- perf-trend:dev:files -->',
        }
        gh = FakeGitHub([crafted, issue(5)])
        self.assertEqual(
            self.apply(gh, [upsert()]), ['updated #5: Performance regression in files']
        )
        self.assertNotIn(2, {int(w[1].split('/')[1]) for w in gh.writes if '/' in w[1]})

    def test_an_api_error_fails_the_command_naming_its_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            actions = pathlib.Path(tmp) / 'actions.json'
            actions.write_text(json.dumps([upsert()]))
            gh = FakeGitHub(fail={('POST', f'repos/{REPO}/issues'): 422})
            client = ghrest.Client(run=gh, sleep=lambda _s: None)
            err = io.StringIO()
            with (
                unittest.mock.patch.object(perf_trend.ghrest, 'DEFAULT', client),
                redirect_stderr(err),
                redirect_stdout(io.StringIO()),
            ):
                rc = perf_trend.main(['issues', '--actions', str(actions), '--repo', REPO])
            self.assertEqual(rc, 1)
            self.assertIn('HTTP 422 Validation Failed', err.getvalue())

    def test_fetch_reads_open_and_closed_issues_and_no_pull_request(self):
        gh = FakeGitHub([issue(3), issue(4, state='closed'), {**issue(5), 'pull_request': {}}])
        got = perf_trend.fetch_issues(REPO, ghrest.Client(run=gh, sleep=lambda _s: None))
        self.assertEqual([(i['number'], i['state']) for i in got], [(3, 'open'), (4, 'closed')])
        self.assertEqual(set(got[0]), {'number', 'state', 'closed_at', 'body'})


class Record(unittest.TestCase):
    """The record job's writes into a perf-trend checkout."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)

    def record(self, values, n, series='dev', issues=None, runner=RUNNER):
        p = tp(values, n, series=series, runner=runner)
        return perf_trend.record(self.root, p, repo=REPO, issues=issues, pr_units=PR_UNITS)

    def guard_file(self):
        return json.loads((self.root / 'series/dev/baseline.json').read_text())

    def pr_guard(self, cold, runner=RUNNER):
        """perf_gate.guard of a pull request on runner whose files x10 cold CPU is cold,
        against the stored baseline.json."""
        base_runner, runs, baselines = perf_gate.baselines_from_trend(self.guard_file())
        pr = {'runner': runner, 'cells': [rcell(reps=(cold,) * 3)]}
        units = perf_gate.planned_windows([{'scenario': 'files', **UNIT, 'windows': ['cold']}])
        return perf_gate.guard(baselines, base_runner, pr, units, windows={}, runs=runs)

    def stored(self, series='dev'):
        d = self.root / 'series' / series
        return sorted(f.name for f in (d / 'points').iterdir()), json.loads(
            (d / 'state.json').read_text()
        )

    def test_a_point_is_stored_with_the_state_and_dev_s_guard_baseline(self):
        dom = ('files', 'cold', 'dom')
        colds = (1000.0, 1200.0, 900.0, 1100.0, 1000.0, 5000.0)
        for n, cold in enumerate(colds, 1):
            self.record({**GOOD, ('files', 'cold', 'cpu_ms'): cold, dom: 639 + n}, n)
        names, state = self.stored()
        self.assertEqual(names, [perf_trend.point_name(tp(GOOD, n)) for n in range(1, 7)])
        self.assertEqual(state['last']['commit'], PURE_SHA.format(6))
        self.assertEqual(state['scenarios']['files']['status'], 'suspect')
        stored = json.loads((self.root / 'series/dev/points' / names[-1]).read_text())
        cold = next(c for c in stored['cells'] if c['metric'] == 'cpu_ms' and c['window'] == 'cold')
        self.assertEqual((cold['baseline'], cold['bad']), (1000.0, True))
        baseline = json.loads((self.root / 'series/dev/baseline.json').read_text())
        self.assertEqual(baseline['commits'], [PURE_SHA.format(n) for n in range(2, 7)])
        self.assertEqual(baseline['runs'], 5)
        cells = {(c['window'], c['metric']): (c['kind'], c['baseline']) for c in baseline['cells']}
        self.assertEqual(cells[('cold', 'cpu_ms')], ('time', 1100.0))
        self.assertEqual(cells[('cold', 'dom')], ('count', 645))
        for n in range(7, 9):
            self.record(GOOD, n)
        baseline = json.loads((self.root / 'series/dev/baseline.json').read_text())
        self.assertEqual(baseline['commits'], [PURE_SHA.format(n) for n in range(4, 9)])
        runner, runs, read = perf_gate.baselines_from_trend(baseline)
        self.assertEqual((runner, runs, len(read)), (RUNNER, 5, 3))

    def test_a_new_runner_s_baseline_reports_a_pull_request_until_it_holds_five_runs(self):
        for n in range(1, 8):
            self.record(GOOD, n)
        for k in range(1, 1 + perf_gate.GUARD_POINTS):
            self.record(GOOD, 7 + k, runner='self-hosted')
            b = self.guard_file()
            self.assertEqual((b['runner'], b['runs']), ('self-hosted', k))
            self.assertEqual(b['commits'], [PURE_SHA.format(n) for n in range(8, 8 + k)])
            out = self.pr_guard(2400.0, runner='self-hosted')
            self.assertEqual(out.failures, [], k)
            self.assertEqual(bool(out.remeasure), k == perf_gate.GUARD_POINTS, k)
            self.assertEqual(self.pr_guard(2400.0).remeasure, [], k)

    def test_a_runner_back_from_an_excursion_reports_until_five_points_since_its_return(self):
        for k in range(1, perf_gate.GUARD_POINTS):
            with self.subTest(excursion=k):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                self.root = pathlib.Path(tmp.name)
                for n in range(1, 8):
                    self.record(GOOD, n)
                for n in range(8, 8 + k):
                    self.record(GOOD, n, runner='self-hosted')
                    self.assertEqual(self.pr_guard(2400.0).remeasure, [], n)
                for r in range(1, 1 + perf_gate.GUARD_POINTS):
                    n = 7 + k + r
                    self.record(GOOD, n)
                    b, (_, state) = self.guard_file(), self.stored()
                    self.assertEqual((b['runner'], b['runs']), (RUNNER, r), n)
                    out = self.pr_guard(2400.0)
                    self.assertEqual(out.failures, [], n)
                    if r < perf_gate.GUARD_POINTS:
                        self.assertEqual(out.remeasure, [], n)
                        self.assertEqual(
                            state['last']['guard_behind'],
                            f'it holds {r} run{"" if r == 1 else "s"} of `{RUNNER}`, fewer than 5',
                            n,
                        )
                        self.assertFalse(perf_trend.skip(state, PURE_SHA.format(n), RUNNER), n)
                self.assertEqual(len(out.remeasure), 1)
                self.assertIsNone(state['last']['guard_behind'])
                self.assertTrue(perf_trend.skip(state, PURE_SHA.format(n), RUNNER))

    def test_a_ten_run_point_holds_five_runs_alone_and_another_runner_s_point_starts_again(
        self,
    ):
        def ran(p, runs):
            p['runs'] = runs
            for c in p['cells']:
                c['reps'] = [[c['value']] * 3] * runs
            return p

        seed = ran(tp(GOOD, 1), 10)
        b = perf_trend.guard_baseline([seed])
        self.assertEqual((b['runner'], b['runs']), (RUNNER, 10))
        later = tp(slow(cold=4000.0), 2, runner='self-hosted')
        b = perf_trend.guard_baseline([seed, later])
        self.assertEqual(
            (b['runner'], b['runs'], b['commits']), ('self-hosted', 1, [later['commit']])
        )
        two = ran(tp(GOOD, 3, runner='self-hosted'), 2)
        b = perf_trend.guard_baseline([seed, later, two])
        self.assertEqual((b['runner'], b['runs']), ('self-hosted', 3))
        back = tp(GOOD, 4)
        b = perf_trend.guard_baseline([seed, later, two, back])
        self.assertEqual((b['runner'], b['runs'], b['commits']), (RUNNER, 1, [back['commit']]))

    def test_a_timing_cell_enters_the_baseline_only_once_five_runs_measured_it(self):
        new = ('files', 'replay', 'cpu_ms')
        points = [tp(GOOD, n) for n in range(1, 5)]
        points.append(tp({**GOOD, new: 500.0, ('files', 'replay', 'dom'): 40}, 5))
        b = perf_trend.guard_baseline(points)
        self.assertEqual(b['runs'], 5)
        windows = {(c['window'], c['metric']) for c in b['cells']}
        self.assertNotIn(('replay', 'cpu_ms'), windows)
        self.assertIn(('replay', 'dom'), windows)
        self.assertIn(('cold', 'cpu_ms'), windows)
        ten = tp({**GOOD, new: 500.0}, 6, runner='self-hosted')
        ten['runs'] = 10
        for c in ten['cells']:
            c['reps'] = [[c['value']] * 3] * 10
        b = perf_trend.guard_baseline([*points, ten])
        self.assertEqual(
            sorted((c['window'], c['baseline']) for c in b['cells']),
            [('cold', 1000.0), ('live', 3000.0), ('replay', 500.0)],
        )

    def test_a_slowdown_at_dev_s_head_is_measured_until_the_guard_baseline_takes_it(self):
        for n in range(1, 8):
            self.record(GOOD, n)
        head = PURE_SHA.format(8)
        pr = {'runner': RUNNER, 'cells': [rcell(reps=(3000.0,) * 3)]}
        units = perf_gate.planned_windows([{'scenario': 'files', **UNIT, 'windows': ['cold']}])

        def at_head(n, issues=None):
            p = tp(slow(cold=3000.0), n)
            p['commit'] = head
            actions, summary = perf_trend.record(
                self.root, p, repo=REPO, issues=issues, pr_units=PR_UNITS
            )
            _, state = self.stored()
            base = json.loads((self.root / 'series/dev/baseline.json').read_text())
            runner, runs, baselines = perf_gate.baselines_from_trend(base)
            red = perf_gate.guard(baselines, runner, pr, units, windows={}, runs=runs).remeasure
            skip = perf_trend.skip(state, head, RUNNER)
            return [a['op'] for a in actions], summary, state, bool(red), skip

        ops, summary, state, red, skip = at_head(8)
        self.assertEqual(
            (ops, state['scenarios']['files']['status'], red, skip), ([], 'suspect', True, False)
        )
        ops, summary, state, red, skip = at_head(9)
        self.assertEqual(
            (ops, state['scenarios']['files']['status'], red, skip),
            (['upsert'], 'open', True, False),
        )
        self.assertEqual(
            state['last']['guard_behind'], '1 timing cell is past the guard against it'
        )
        self.assertIn(
            "The pull request guard's baseline does not hold this point yet: 1 timing cell is "
            f'past the guard against it, so the next run measures `{head[:12]}` again.',
            summary,
        )
        ops, summary, state, red, skip = at_head(10)
        self.assertEqual(
            (ops, state['scenarios']['files']['status'], red, skip),
            (['upsert'], 'open', False, True),
        )
        self.assertIsNone(state['last']['guard_behind'])
        self.assertNotIn("guard's baseline does not hold", summary)

    def test_only_a_cell_a_pull_request_measures_holds_the_guard_behind(self):
        points = [tp(GOOD, n) for n in range(1, 6)]
        p = tp(slow(cold=1000.0, live=9000.0), 6)
        base = perf_trend.guard_baseline([*points, p])
        for units, want in (
            (PR_UNITS, '1 timing cell is past the guard against it'),
            (perf_gate.planned_windows([{'scenario': 'files', **UNIT, 'windows': ['cold']}]), None),
            ({}, None),
        ):
            with self.subTest(units=units):
                self.assertEqual(perf_trend.guard_behind(base, p, units), want)

    def test_the_judge_command_holds_the_guard_behind_only_for_the_plan_s_pr_set(self):
        for n in range(1, 8):
            self.record(GOOD, n)
        plan = {'node': 'web', 'sets': {'pr': {'shards': 1, 'cells': []}}}
        files = {'plan': self.root.parent / f'{self.root.name}-plan.json'}
        files['point'] = self.root.parent / f'{self.root.name}-point.json'
        for f in files.values():
            self.addCleanup(f.unlink, missing_ok=True)
        argv = ['judge', '--root', str(self.root), '--point', str(files['point'])]
        argv += ['--series', 'dev', '--repo', REPO, '--plan', str(files['plan'])]
        argv += ['--actions', str(self.root / 'a.json')]
        for n, cells, want in (
            (8, [{'scenario': 'docs', **UNIT, 'seconds': 100}], None),
            (9, [{'scenario': 'files', **UNIT, 'windows': ['live'], 'seconds': 100}], '1 timing'),
        ):
            with self.subTest(cells=cells):
                plan['sets']['pr']['cells'] = cells
                files['plan'].write_text(json.dumps(plan))
                p = tp(slow(cold=1000.0, live=9000.0), n)
                p['commit'] = PURE_SHA.format(8)
                files['point'].write_text(json.dumps(p))
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(perf_trend.main(argv), 0)
                behind = self.stored()[1]['last']['guard_behind']
                self.assertEqual(behind and behind[:8], want)

    def test_a_head_on_a_new_runner_is_measured_again(self):
        for n in range(1, 6):
            self.record(GOOD, n)
        self.record(GOOD, 6, runner='self-hosted')
        _, state = self.stored()
        self.assertEqual(
            state['last']['guard_behind'], 'it holds 1 run of `self-hosted`, fewer than 5'
        )
        self.assertFalse(perf_trend.skip(state, PURE_SHA.format(6), 'self-hosted'))

    def test_a_point_already_recorded_or_of_a_bad_series_is_refused(self):
        self.record(GOOD, 1)
        p = tp(GOOD, 1)
        p['run_id'] = 'another'
        with self.assertRaisesRegex(ValueError, 'already recorded'):
            perf_trend.record(self.root, p, repo=REPO, issues=None, pr_units=PR_UNITS)
        for series in ('Dev', '-x', 'a' * 32, '../dev'):
            with self.subTest(series=series), self.assertRaisesRegex(ValueError, 'series'):
                self.record(GOOD, 2, series=series)

    def test_a_re_run_of_the_recording_run_writes_nothing_and_repairs_its_issues(self):
        for n in range(1, 8):
            self.record(GOOD, n)
        self.record(slow(cold=3000.0), 8)
        actions, _ = self.record(slow(cold=3000.0), 9, issues=[])
        self.assertEqual([a['op'] for a in actions], ['upsert'])
        before = {f: f.read_bytes() for f in self.root.rglob('*') if f.is_file()}
        again, summary = self.record(slow(cold=3000.0), 9, issues=[])
        self.assertEqual(again, actions)
        self.assertIn('already recorded by this run, so nothing is written', summary)
        self.assertEqual({f: f.read_bytes() for f in self.root.rglob('*') if f.is_file()}, before)
        filed = {'number': 1, 'state': 'open', 'closed_at': None}
        self.assertEqual(self.record(slow(cold=3000.0), 9, issues=[filed | actions[0]])[0], [])
        later, _ = self.record(slow(cold=3000.0), 10, issues=[])
        self.assertEqual([a['op'] for a in later], ['upsert'])
        self.assertIn('comment', later[0])

    def test_a_point_older_than_the_series_newest_is_refused_and_writes_nothing(self):
        for n in range(1, 8):
            self.record(GOOD, n)
        self.record(slow(cold=3000.0), 8)
        self.record(slow(cold=3000.0), 9)
        _, state = self.stored()
        self.assertEqual(state['scenarios']['files']['status'], 'open')
        before = {f: f.read_bytes() for f in self.root.rglob('*') if f.is_file()}
        stale = tp(GOOD, 99)
        stale['date'] = '2026-11-01T08:30:00Z'
        point_file = self.root.parent / f'{self.root.name}-stale.json'
        self.addCleanup(point_file.unlink, missing_ok=True)
        point_file.write_text(json.dumps(stale))
        argv = ['judge', '--root', str(self.root), '--point', str(point_file), '--series', 'dev']
        issues = self.root.parent / f'{self.root.name}-issues.json'
        self.addCleanup(issues.unlink, missing_ok=True)
        issues.write_text('[]')
        argv += ['--repo', REPO, '--issues', str(issues), '--actions', str(self.root / 'a.json')]
        plan_file = self.root.parent / f'{self.root.name}-plan.json'
        self.addCleanup(plan_file.unlink, missing_ok=True)
        plan_file.write_text(json.dumps(PLAN))
        argv += ['--plan', str(plan_file)]
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(perf_trend.main(argv), 1)
        self.assertIn(
            '::error::perf_trend: 20261101T083000Z-000000000000.json is older than '
            '20261101T090000Z-000000000000.json, the newest point of the dev series, so it is '
            'not recorded: a later run judged the series past it',
            err.getvalue(),
        )
        self.assertEqual({f: f.read_bytes() for f in self.root.rglob('*') if f.is_file()}, before)

    def test_a_series_with_no_state_is_refused_while_an_issue_is_marked_with_it(self):
        marked = {
            'number': 12,
            'state': 'closed',
            'closed_at': '2026-11-01T10:00:00Z',
            'body': '<!-- perf-trend:dev:files -->\nbody',
        }
        with self.assertRaisesRegex(
            ValueError,
            r'^the dev series has no state\.json but issue #12 is marked with it, so its '
            r'history is lost: restore the perf-trend branch from a clone that holds it$',
        ):
            self.record(GOOD, 1, issues=[marked])
        self.assertEqual(list(self.root.iterdir()), [])
        other = {**marked, 'body': '<!-- perf-trend:proof:files -->\nbody'}
        actions, _ = self.record(GOOD, 1, issues=[other])
        self.assertEqual(actions, [])
        self.assertEqual(self.stored()[0], [perf_trend.point_name(tp(GOOD, 1))])
        self.record(GOOD, 2, issues=[marked])

    def test_a_series_other_than_dev_never_starts_a_branch_with_no_dev_series(self):
        with self.assertRaisesRegex(
            ValueError,
            '^the proof series cannot start a perf-trend branch that holds no dev series',
        ):
            self.record(GOOD, 1, series='proof', issues=[])
        self.assertEqual(list(self.root.iterdir()), [])
        self.record(GOOD, 1)
        self.record(GOOD, 101, series='proof', issues=[])

    def test_a_hand_closed_issue_is_written_back_as_accepted(self):
        for n in range(1, 8):
            self.record(GOOD, n)
        self.record(slow(cold=3000.0), 8)
        self.record(slow(cold=3000.0), 9)
        _, state = self.stored()
        body = f'<!-- perf-trend:dev:files -->\n{state["scenarios"]["files"]["status"]}'
        closed = {
            'number': 1,
            'state': 'closed',
            'closed_at': '2026-11-01T10:00:00Z',
            'body': body,
        }
        actions, _ = self.record(slow(cold=3000.0), 10, issues=[closed])
        self.assertEqual(actions, [])
        _, state = self.stored()
        self.assertEqual(
            state['scenarios']['files'],
            {
                'status': 'ok',
                'accepted': {'from': '2026-11-01T08:00:00Z', 'at': '2026-11-01T09:00:00Z'},
            },
        )

    def test_needs_names_the_state_the_last_points_and_dev_s_points_for_a_young_series(self):
        dev = [f'series/dev/points/2026110{d}T000000Z-aaa.json' for d in range(1, 10)]
        dev += [f'series/dev/points/2026111{d}T000000Z-aaa.json' for d in range(10)]
        dev += [f'series/dev/points/2026112{d}T000000Z-aaa.json' for d in range(5)]
        proof = ['series/proof/points/20261115T120000Z-bbb.json']
        listing = ['series/dev/state.json', 'series/dev/baseline.json', *dev, *proof]
        self.assertEqual(
            perf_trend.needs(listing, 'dev'), ['series/dev/state.json', *dev[-perf_trend.HISTORY :]]
        )
        young = perf_trend.needs(listing, 'proof')
        self.assertEqual(young[:3], ['series/proof/state.json', 'series/dev/state.json', *proof])
        self.assertEqual(
            young[3:],
            [p for p in dev if p < 'series/dev/points/20261115T120000Z'][-perf_trend.HISTORY :],
        )
        self.assertEqual(
            perf_trend.needs([], 'proof'), ['series/proof/state.json', 'series/dev/state.json']
        )

    def test_the_judge_command_lists_each_unit_measured_far_off_its_plan_seconds(self):
        def unit(scenario, seconds, **kw):
            return {'scenario': scenario, **UNIT, 'seconds': seconds, **kw}

        plan = {
            'node': 'web',
            'sets': {
                'full': {
                    'shards': 2,
                    'cells': [unit('files', 100), unit('docs', 100), unit('tabs', 100)],
                },
                'pr': {
                    'shards': 1,
                    'cells': [unit('files', 70), unit('docs', 30, windows=['cold'])],
                },
            },
        }
        p = tp(GOOD, 1)
        p['units'] = [
            {'scenario': 'files', **UNIT, 'seconds': 110.0},
            {'scenario': 'docs', **UNIT, 'seconds': 125.0},
            {'scenario': 'tabs', **UNIT, 'seconds': 79.0},
        ]
        files = {}
        for name, doc in (('point', p), ('plan', plan), ('issues', [])):
            files[name] = self.root.parent / f'{self.root.name}-{name}.json'
            self.addCleanup(files[name].unlink, missing_ok=True)
            files[name].write_text(json.dumps(doc))
        argv = ['judge', '--root', str(self.root), '--point', str(files['point'])]
        argv += ['--series', 'dev', '--repo', REPO, '--issues', str(files['issues'])]
        argv += ['--actions', str(self.root / 'a.json'), '--plan', str(files['plan'])]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(perf_trend.main(argv), 0)
        self.assertIn(
            '### Units measured more than 20 % off their plan.json seconds (information)\n\n'
            '| Set | Unit | plan.json | Measured |\n'
            '|---|---|---|---|\n'
            '| full | docs/x10/chromium/desktop | 100 | 125 |\n'
            '| full | tabs/x10/chromium/desktop | 100 | 79 |\n'
            '| pr | files/x10/chromium/desktop | 70 | 110 |\n',
            out.getvalue(),
        )
        p['units'] = [{'scenario': 'files', **UNIT, 'seconds': 110.0}]
        plan['sets']['pr']['cells'][0]['seconds'] = 100
        files['point'].write_text(json.dumps(tp(GOOD, 2) | {'units': p['units']}))
        files['plan'].write_text(json.dumps(plan))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(perf_trend.main(argv), 0)
        self.assertNotIn('off their plan.json seconds', out.getvalue())

    def test_a_unit_s_profiled_rep_counts_only_toward_a_cell_that_profiles(self):
        def unit(scenario, seconds, **kw):
            return {'scenario': scenario, **UNIT, 'seconds': seconds, **kw}

        p = tp(GOOD, 1)
        p['units'] = [
            {'scenario': 'git-prs', **UNIT, 'seconds': 900.0, 'profile_seconds': 300.0},
            {'scenario': 'docs', **UNIT, 'seconds': 180.0},
        ]
        full = [unit('git-prs', 1197, profile=True), unit('docs', 240, profile=True)]
        pr = [unit('git-prs', 896), unit('docs', 180)]
        plan = {'node': 'web', 'sets': {'full': {'cells': full}, 'pr': {'cells': pr}}}
        self.assertEqual(perf_trend.seconds_drift(plan, p), '')
        pr[0]['seconds'] = 1197
        full[0]['seconds'] = 900
        drift = perf_trend.seconds_drift(plan, p)
        self.assertIn('| full | git-prs/x10/chromium/desktop | 900 | 1200 |\n', drift)
        self.assertIn('| pr | git-prs/x10/chromium/desktop | 1197 | 900 |\n', drift)
        self.assertNotIn('docs', drift)

    def test_the_skip_command_reads_a_missing_state_as_never_measured(self):
        out = io.StringIO()
        argv = ['skip', '--state', str(self.root / 'none.json'), '--commit', SHA]
        with redirect_stdout(out):
            rc = perf_trend.main([*argv, '--runner', RUNNER])
        self.assertEqual((rc, out.getvalue()), (0, 'false\n'))
        state = str(self.root / 'series/dev/state.json')
        for n in range(1, perf_gate.GUARD_POINTS + 1):
            self.record(GOOD, n)
            want = 'true' if n == perf_gate.GUARD_POINTS else 'false'
            out = io.StringIO()
            argv = ['skip', '--state', state, '--commit', PURE_SHA.format(n), '--runner', RUNNER]
            with redirect_stdout(out):
                perf_trend.main(argv)
            self.assertEqual(out.getvalue(), f'{want}\n', n)

    def test_the_repair_command_writes_observe_s_actions_and_says_whether_any(self):
        for n in range(1, 8):
            self.record(GOOD, n)
        self.record(slow(cold=3000.0), 8)
        opened, _ = self.record(slow(cold=3000.0), 9)
        state = str(self.root / 'series/dev/state.json')
        issues, actions = self.root / 'issues.json', self.root / 'actions.json'
        for listed, want in (
            ([], 'true'),
            ([{**opened[0], 'number': 1, 'state': 'open'}], 'false'),
        ):
            with self.subTest(listed=listed):
                issues.write_text(json.dumps(listed))
                out = io.StringIO()
                argv = ['repair', '--state', state, '--issues', str(issues)]
                with redirect_stdout(out):
                    rc = perf_trend.main([*argv, '--out', str(actions)])
                self.assertEqual((rc, out.getvalue()), (0, f'{want}\n'))
                written = json.loads(actions.read_text())
                self.assertEqual(written, opened if want == 'true' else [])


if __name__ == '__main__':
    unittest.main()
