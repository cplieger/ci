#!/usr/bin/env python3
"""The tier 2 performance gate: the measure plan of a web app's tests/perf/plan.json
and the guard of the timing cells its harness measured against dev's trend baselines
(perf_trend.py).

A results file (`results.json`, one per measure shard) is
  {"schema": 1, "runner": str, "cpu_model": str,
   "cells": [{scenario, scale, engine, width, window, metric, kind, reps}],
   "seconds": [{scenario, scale, engine, width, seconds}]}
where a metric is `<family>[:<qualifier>]` (`reads:/api/chats`) and kind is
`time` or `server_cpu` (guarded when GUARD holds its family), `count` or `report`
(information to the guard).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import pathlib
import re
import statistics
import sys
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

KEY_FIELDS = ('scenario', 'scale', 'engine', 'width', 'window', 'metric')
SERVER_CPU = 'server_cpu_ms'


def key(d: dict) -> tuple:
    return tuple(d[f] for f in KEY_FIELDS)


def key_text(k: Iterable) -> str:
    return '/'.join(str(x) for x in k)


def key_dict(k: tuple) -> dict:
    return dict(zip(KEY_FIELDS, k, strict=True))


def family(metric: str) -> str:
    return metric.split(':', 1)[0]


def is_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def finite(text: str, number: Callable[[str], Any]) -> Any:
    x = number(text)
    try:
        ok = math.isfinite(x)
    except OverflowError:
        ok = False
    if not ok:
        raise ValueError(f'{text} is not a finite number')
    return x


def loads(text: str) -> Any:
    """JSON whose every number a float holds: NaN, an infinity and 1e999 pass
    every comparison the guard makes and so would ungate a cell, and a larger
    integer is an OverflowError wherever it meets a float."""
    return json.loads(
        text,
        parse_constant=lambda s: finite(s, float),
        parse_float=lambda s: finite(s, float),
        parse_int=lambda s: finite(s, int),
    )


# --- compare and verdict ---------------------------------------------------


@dataclasses.dataclass
class Outcome:
    failures: list[str] = dataclasses.field(default_factory=list)
    remeasure: list[dict] = dataclasses.field(default_factory=list)
    notices: list[str] = dataclasses.field(default_factory=list)
    runner_mismatch: bool = False
    runner: str = ''
    # Every measured cell for the summary: {cell, baseline, value, status}.
    rows: list[dict] = dataclasses.field(default_factory=list)
    # Each re-measured cell: {cell, baseline, reps, value, status}, value the median of six.
    six: list[dict] = dataclasses.field(default_factory=list)


def middle(reps: list) -> list:
    """The middle rep, or both middle reps of an even count. A count is compared on
    these, never on their mean: statistics.median of [1, 1, 1, 3, 3, 3] is 2, a count
    no rep measured."""
    s = sorted(reps)
    return s[(len(s) - 1) // 2 : len(s) // 2 + 1]


def table(title: str | None, head: tuple, rows: Iterable[tuple]) -> list[str]:
    """A Markdown table under its heading, or no lines when rows is empty. A `|` in a
    value is escaped, since a reason or a refusal can hold one."""
    body = ['| ' + ' | '.join(str(v).replace('|', '\\|') for v in row) + ' |' for row in rows]
    if not body:
        return []
    heading = [f'### {title}', ''] if title else []
    return [*heading, '| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head), *body, '']


# --- the pull request guard ------------------------------------------------

# Family to (ratio, floor): a cell fails over ratio x baseline and more than floor over it.
# Four ten-run measurements over seven runner classes put the worst one-job ratio of a pr
# set cell at 1.88 for cpu_ms over 50 ms and 1.64 for server_cpu_ms over 10 ms, and of
# any memory cell at 1.09.
GUARD = {
    'cpu_ms': (2.0, 250.0),
    SERVER_CPU: (2.0, 10.0),
    'heap_mb': (1.25, 2.0),
    'rss_mb': (1.25, 50.0),
}
TIMING_KINDS = ('time', 'server_cpu')
TREND_BASELINE = 'series/dev/baseline.json'


def baselines_from_trend(doc: dict) -> tuple[str, dict[tuple, float]]:
    """The perf-trend branch's series/dev/baseline.json (perf_trend.guard_baseline) as its
    runner and each cell's numeric baseline by key; every other field is ignored."""
    cells = doc.get('cells', [])
    if not isinstance(cells, list):
        raise TypeError(f'{TREND_BASELINE}: cells must be a list')
    out = {}
    for i, e in enumerate(cells):
        if not isinstance(e, dict) or not all(isinstance(e.get(f), str) for f in KEY_FIELDS):
            raise ValueError(f'cells[{i}]: must be an object naming {", ".join(KEY_FIELDS)}')
        if is_number(e.get('baseline')):
            out[key(e)] = e['baseline']
    runner = doc.get('runner')
    if not isinstance(runner, str) or not runner:
        raise ValueError(
            f'{TREND_BASELINE}: runner must name the runner the baselines were measured on'
        )
    return runner, out


def is_timing(c: dict) -> bool:
    return c.get('kind') in TIMING_KINDS and family(c['metric']) in GUARD


def over_guard(metric: str, value: float, baseline: float) -> bool:
    ratio, floor = GUARD[family(metric)]
    return value > ratio * baseline and value - baseline > floor


def guard_rule(metric: str) -> str:
    ratio, floor = GUARD[family(metric)]
    return f'{ratio}x and {floor:g}'


def ratio_text(value: float, baseline: float) -> str:
    return f'{value / baseline:.2f}' if baseline > 0 else ''


def over_text(metric: str, value: float, baseline: float) -> str:
    ratio = ratio_text(value, baseline)
    return (
        f'{value} is {value - baseline:g} over its baseline {baseline}'
        f'{f" ({ratio}x)" if ratio else ""}, past the guard of {guard_rule(metric)}'
    )


def guard(
    baselines: dict[tuple, float],
    runner: str,
    results: dict,
    units: dict,
    *,
    windows: dict[str, set[str]],
) -> Outcome:
    """Judge each measured timing cell's median of three against its trend baseline: one
    over the guard is listed in `remeasure`, never failed here. Counts and reports are
    information. On the baselines' runner, a baselined timing cell of `units`
    (planned_windows) that no shard measured fails, unless it is a server route or its
    window is one `windows` (scenario_windows) no longer lists, which are notices; and a
    run that guarded no cell fails, since a guard that checks nothing must not pass."""
    out = Outcome(runner=results.get('runner', ''))
    out.runner_mismatch = out.runner != runner
    guarded = 0
    for c in results.get('cells', []):
        reps = c.get('reps') or []
        value = statistics.median(reps) if reps else None
        baseline = baselines.get(key(c))
        status = 'reported'
        if not out.runner_mismatch and is_timing(c) and baseline is not None:
            guarded += 1
            if value is None:
                out.failures.append(f'{key_text(key(c))}: measured no reps')
                status = 'failed'
            elif over_guard(c['metric'], value, baseline):
                out.remeasure.append(c)
                status = 'remeasure'
            else:
                status = 'ok'
        out.rows.append({'cell': c, 'baseline': baseline, 'value': value, 'status': status})
    if out.runner_mismatch:
        return out
    measured = {key(c) for c in results.get('cells', [])}
    missed = [
        k
        for k in sorted(baselines)
        if family(k[5]) in GUARD and planned(units, key_dict(k)) and k not in measured
    ]
    for k in missed:
        dropped = k[0] in windows and k[4] not in windows[k[0]]
        # A server cell exists only when its window requested the route, so a missing one
        # is a change in what the page requests, not a cell the harness lost.
        if family(k[5]) == SERVER_CPU:
            out.notices.append(
                f"{key_text(k)}: dev's trend has a baseline for this route, and no shard "
                'measured it'
            )
        elif dropped:
            out.notices.append(
                f"{key_text(k)}: dev's trend has a baseline for it, and the head plan.json no "
                f'longer measures window {k[4]}'
            )
        else:
            out.failures.append(
                f"{key_text(k)}: dev's trend has a baseline for it, and no shard measured it"
            )
    if not guarded:
        out.failures.append(
            "nothing to guard: dev's trend has a baseline for none of the timing cells this run "
            'measured'
        )
    return out


def guard_verdict(decided: dict, results: dict, remeasured: dict) -> Outcome:
    """Fail each cell of compare's decision (its remeasure.json, every cell carrying its
    baseline) whose median of six reps, its first three and the three re-measured,
    is still over the guard."""
    out = Outcome(runner=results.get('runner', ''))
    first = {key(c): c for c in results.get('cells', [])}
    again = {key(c): c for c in remeasured.get('cells', [])}
    for d in decided.get('cells', []):
        name, baseline = key_text(key(d)), d.get('baseline')
        if not is_number(baseline):
            raise ValueError(f'{name}: the decision holds no numeric baseline for it')
        more = again.get(key(d))
        reps = (first.get(key(d)) or {}).get('reps') or []
        if reps and family(d['metric']) == SERVER_CPU and not (more or {}).get('reps'):
            # A route is measured only when the window requests it, so a re-measure can
            # miss one; the cell was over the guard on its first reps, which still judge it.
            out.failures.append(
                f'{name}: the re-measure did not request this route, so its first three reps '
                f'judge it: median {over_text(d["metric"], statistics.median(reps), baseline)}'
            )
            continue
        if more is None or not more.get('reps') or not reps:
            out.failures.append(f'{name}: listed for a re-measure but no reps came back')
            continue
        six = [*reps, *more['reps']]
        value = statistics.median(six)
        status = 'ok'
        if over_guard(d['metric'], value, baseline):
            status = 'failed'
            out.failures.append(
                f'{name}: median of six {over_text(d["metric"], value, baseline)}; reps {six}'
            )
        out.six.append(
            {'cell': d, 'baseline': baseline, 'reps': six, 'value': value, 'status': status}
        )
    return out


def differs(c: dict, baseline: float | None) -> bool:
    return (
        c.get('kind') == 'count'
        and baseline is not None
        and bool(c.get('reps'))
        and any(v != baseline for v in middle(c['reps']))
    )


def guard_summary(out: Outcome) -> str:
    lines = ['## Performance guard', '']
    if out.runner_mismatch:
        lines += [
            (
                f"The results come from runner `{out.runner}`, which dev's trend baselines were "
                'not measured on, so every metric is reported and none is guarded.'
            ),
            '',
        ]
    if out.failures:
        lines += ['### Failures', '', *(f'- {f}' for f in out.failures), '']
    if out.notices:
        lines += ['### Notices', '', *(f'- {n}' for n in out.notices), '']
    lines += table(
        'Re-measured',
        ('Cell', 'Baseline', 'Six reps', 'Median', 'Ratio', 'Status'),
        (
            (
                key_text(key(s['cell'])),
                s['baseline'],
                ', '.join(map(str, s['reps'])),
                s['value'],
                ratio_text(s['value'], s['baseline']),
                s['status'],
            )
            for s in out.six
        ),
    )
    timed = [r for r in out.rows if r['status'] != 'reported']
    lines += table(
        'Guarded timing cells',
        ('Cell', 'Baseline', 'Median', 'Ratio', 'Guard', 'Status'),
        (
            (
                key_text(key(r['cell'])),
                r['baseline'],
                r['value'],
                '' if r['value'] is None else ratio_text(r['value'], r['baseline']),
                guard_rule(r['cell']['metric']),
                r['status'],
            )
            for r in timed
        ),
    )
    lines += table(
        'Counts that differ from dev (information)',
        ('Cell', 'Dev', 'Measured'),
        (
            (
                key_text(key(r['cell'])),
                r['baseline'],
                ', '.join(map(str, middle(r['cell']['reps']))),
            )
            for r in out.rows
            if differs(r['cell'], r['baseline'])
        ),
    )
    lines += table(
        'Every other measured cell (information)',
        ('Cell', 'Kind', 'Median', 'Dev'),
        (
            (
                key_text(key(r['cell'])),
                r['cell'].get('kind', ''),
                r['value'],
                '' if r['baseline'] is None else r['baseline'],
            )
            for r in out.rows
            if r['status'] == 'reported'
        ),
    )
    return '\n'.join(lines) + '\n'


# --- the plan --------------------------------------------------------------

UNIT_FIELDS = KEY_FIELDS[:4]
# GitHub runs at most 256 jobs from one matrix.
MAX_JOBS = 256
# Past three runs a cell's noise is its window's shape (a bimodal window), not its rep count.
MAX_UNIT_RUNS = 3


def unit_key(d: dict) -> tuple:
    """A plan cell's scenario, scale, engine and width. A measure job runs
    every window and metric of it, or only the windows its `windows` lists."""
    return tuple(d.get(f) for f in UNIT_FIELDS)


def windows_error(u: dict) -> str | None:
    w = u.get('windows')
    if w is None:
        return None
    ok = isinstance(w, list) and w and all(isinstance(x, str) and x for x in w)
    if not ok or len(set(w)) != len(w):
        return f'windows must be a non-empty list of distinct window names, not {w!r}'
    return None


def planned_windows(cells: Iterable[dict]) -> dict[tuple, set[str] | None]:
    """Each planned unit with the windows its cells list, None for every window."""
    out: dict[tuple, set[str] | None] = {}
    for c in cells:
        k, w = unit_key(c), c.get('windows')
        if w is None or (k in out and out[k] is None):
            out[k] = None
        else:
            out[k] = {*out.get(k, set()), *w}
    return out


def scenario_windows(doc: dict) -> dict[str, set[str]]:
    """Each plan.json scenario with the windows the harness measures for a cell that lists
    none; a scenario without a list of window names is left out."""
    scenarios = doc.get('scenarios')
    out = {}
    for name, s in (scenarios if isinstance(scenarios, dict) else {}).items():
        w = s.get('windows') if isinstance(s, dict) else None
        if isinstance(w, list) and all(isinstance(x, str) for x in w):
            out[name] = set(w)
    return out


def planned(units: dict[tuple, set[str] | None], e: dict) -> bool:
    k = unit_key(e)
    return k in units and (units[k] is None or e.get('window') in units[k])


def has_line_break(x: object) -> bool:
    if isinstance(x, str):
        return '\n' in x or '\r' in x
    if isinstance(x, dict):
        return any(has_line_break(v) for v in x.values())
    if isinstance(x, list):
        return any(has_line_break(v) for v in x)
    return False


def plan(
    doc: dict,
    set_name: str,
    *,
    only: list[str] | None = None,
    remeasure: dict | None = None,
    gate: bool = False,
) -> dict:
    """The measure matrix of one set of plan.json: its cells (a scenario at a
    scale, engine and width, every window or those its `windows` lists) split
    into at most `shards` jobs, longest first onto the least-loaded shard. A cell
    is measured in as many runs as its own `runs` asks, run 2 on holding only the
    cells measured again; with more than one run each job is named
    `run-NN-shard-i-of-n`. With `remeasure`, only the cells
    compare listed, once each. A `gate` plan, whose results compare merges as one run,
    refuses more than one. `include` alone is the GitHub matrix: any other key there
    would be a matrix dimension."""
    sets = doc.get('sets', {})
    if not isinstance(sets, dict) or set_name not in sets:
        raise ValueError(f'plan.json has no set {set_name!r}')
    spec = sets[set_name]
    if not isinstance(spec, dict):
        raise TypeError(f'set {set_name} must be an object, not {spec!r}')
    shards = spec.get('shards')
    if not isinstance(shards, int) or isinstance(shards, bool) or shards < 1:
        raise ValueError(f'set {set_name}: shards must be a positive integer, not {shards!r}')
    units = spec.get('cells') or []
    if not units or not isinstance(units, list):
        raise ValueError(f'set {set_name} lists no cells')
    check_units(f'set {set_name}', units)
    for u in units if gate else ():
        if u.get('runs', 1) != 1:
            raise ValueError(
                f'set {set_name}: {key_text(unit_key(u))}: runs belongs to the full set the '
                'trend measures, and a gated set is measured in one run'
            )
    if only:
        missing = sorted(set(only) - {u['scenario'] for u in units})
        if missing:
            raise ValueError(f'set {set_name} has no scenario {", ".join(missing)}')
        units = [u for u in units if u['scenario'] in only]
    node = doc.get('node')
    if not isinstance(node, str) or not node:
        raise ValueError('plan.json needs `node`, the npm package directory that pins Playwright')
    if has_line_break(node):
        raise ValueError(f'plan.json: node {node!r} holds a line break')
    if remeasure is not None:
        wanted = {unit_key(c) for c in remeasure.get('cells', [])}
        units = [u for u in units if unit_key(u) in wanted]

    def runs_of(u: dict) -> int:
        return 1 if remeasure is not None else u.get('runs', 1)

    rounds = max((runs_of(u) for u in units), default=1)
    include = []
    mean_load = 0.0
    for r in range(1, rounds + 1):
        these = [u for u in units if runs_of(u) >= r]
        count = min(shards, len(these))
        if r > 1:
            # A later run re-measures only its own units, so it takes as few shards as
            # hold them at the first run's mean shard load.
            needed = math.ceil(sum(u['seconds'] for u in these) / mean_load)
            count = min(count, max(1, needed))
        bins: list[list[dict]] = [[] for _ in range(count)]
        load = [0.0] * count
        for u in sorted(these, key=lambda u: -u['seconds']):
            i = load.index(min(load))
            bins[i].append(dict(u))
            load[i] += u['seconds']
        if r == 1:
            mean_load = sum(load) / max(count, 1)
        for i, cells in enumerate(bins, 1):
            name = f'shard-{i}-of-{count}'
            if rounds > 1:
                name = f'run-{r:02d}-{name}'
            include.append({'name': name, 'run': r, 'seconds': load[i - 1], 'cells': cells})
    if len(include) > MAX_JOBS:
        raise ValueError(f'{len(include)} jobs is over the {MAX_JOBS} jobs of a matrix')
    return {
        'include': include,
        'scenarios': sorted({u['scenario'] for u in units}),
        'node': node,
    }


def check_units(label: str, units: list) -> None:
    """Raise on a unit plan() cannot shard: not an object, a line break, a missing key
    field or one holding whitespace, `<` or `>`, a unit listed twice, no positive
    seconds, a bad windows list, or bad runs."""
    seen = set()
    for u in units:
        if not isinstance(u, dict):
            raise TypeError(f'{label}: a cell must be an object, not {u!r}')
        # Cell fields reach $GITHUB_OUTPUT and the job's shell.
        if has_line_break(u):
            raise ValueError(f'{label}: a cell field holds a line break: {u!r}')
        k = unit_key(u)
        if not all(isinstance(x, str) and x for x in k):
            raise ValueError(f'{label}: a cell needs {", ".join(UNIT_FIELDS)}: {u}')
        # A scenario names its trend issue in a `<!-- ... -->` marker of the body.
        if any(re.search(r'[\s<>]', x) for x in k):
            raise ValueError(f'{label}: {key_text(k)}: a cell field holds whitespace, < or >')
        if k in seen:
            raise ValueError(f'{label}: {key_text(k)} is listed twice')
        seen.add(k)
        if not is_number(u.get('seconds')) or u['seconds'] <= 0:
            raise ValueError(f'{label}: {key_text(k)} needs its seconds per cell')
        if windows_error(u):
            raise ValueError(f'{label}: {key_text(k)}: {windows_error(u)}')
        r = u.get('runs', 1)
        if not isinstance(r, int) or isinstance(r, bool) or not 1 <= r <= MAX_UNIT_RUNS:
            raise ValueError(
                f'{label}: {key_text(k)}: runs must be a whole number from 1 to '
                f'{MAX_UNIT_RUNS}, not {r!r}'
            )


# --- files and the command line --------------------------------------------


def load(path: str | pathlib.Path) -> dict:
    """The JSON object at path, read by loads; any other document is a TypeError."""
    doc = loads(pathlib.Path(path).read_text())
    if not isinstance(doc, dict):
        raise TypeError(f'{path}: must be a JSON object')
    return doc


def newest_shards(
    directory: str | pathlib.Path, prefix: str, stage: str = 'measure', since: int = 1
) -> dict[tuple[str, int, int], pathlib.Path]:
    """perf-ci.yaml's `perf-results-<prefix>-<stage>-<shard>-attempt-<n>` artifact folders
    under directory, by (run, shard, shard count), each from its newest attempt from since
    on: a partial re-run repeats only some shards, and a re-run compare plans other ones.
    Inside one run a download pattern also matches a repository whose name extends this
    one's (`<repo>-measure-x`), which the full match leaves out."""
    name = re.compile(
        rf'perf-results-{re.escape(prefix)}-{stage}-(?:(run-\d+)-)?'
        r'shard-(\d+)-of-(\d+)-attempt-(\d+)'
    )
    found: dict[tuple[str, int, int], tuple[int, pathlib.Path]] = {}
    for d in pathlib.Path(directory).iterdir():
        m = name.fullmatch(d.name) if d.is_dir() else None
        if m is None or int(m[4]) < since:
            continue
        shard = (m[1] or '', int(m[2]), int(m[3]))
        if shard not in found or found[shard][0] < int(m[4]):
            found[shard] = (int(m[4]), d)
    return {k: d for k, (_, d) in sorted(found.items())}


def shard_groups(
    directory: str | pathlib.Path, prefix: str, stage: str = 'measure', since: int = 1
) -> dict[str, list[pathlib.Path]]:
    """newest_shards' results files by run (`run-NN`, or '' for a run of one), each run
    holding every shard of one shard count, each with a results file; anything else is an
    error."""
    runs: dict[str, dict[int, set]] = {}
    files: dict[str, list[pathlib.Path]] = {}
    for (run, i, m), d in newest_shards(directory, prefix, stage, since).items():
        found = sorted(d.rglob('*.json'))
        if not found:
            raise ValueError(f'{d.name} holds no results file under {directory}')
        runs.setdefault(run, {}).setdefault(m, set()).add(i)
        files.setdefault(run, []).extend(found)
    if not runs:
        raise ValueError(f'no results of perf-results-{prefix}-{stage}-* under {directory}')
    for run, counts in runs.items():
        label = f'{stage} {run}'.strip()
        if len(counts) > 1:
            raise ValueError(f'{label} mixes shard counts {sorted(counts)} under {directory}')
        ((m, got),) = counts.items()
        for i in sorted(set(range(1, m + 1)) - got):
            raise ValueError(f'{label} has no results of shard {i} of {m} under {directory}')
    return files


def merge_results(
    directory: str | pathlib.Path, prefix: str, stage: str = 'measure', since: int = 1
) -> dict:
    """Every shard of shard_groups as one results file, which holds one run: a cell two
    runs measured is two cells of one key."""
    groups = shard_groups(directory, prefix, stage, since)
    if len(groups) > 1:
        raise ValueError(
            f'{stage} results under {directory} must be one run, not {len(groups)}: '
            f'{", ".join(sorted(groups))}'
        )
    return merge([p for ps in groups.values() for p in ps])


def merge(paths: list[pathlib.Path]) -> dict:
    """Results files as one: shards must share a runner and measure disjoint cells."""
    merged: dict[str, Any] = {'schema': 1, 'runner': None, 'cells': []}
    seen = set()
    for p in paths:
        doc = load(p)
        if merged['runner'] is None:
            merged['runner'] = doc.get('runner')
        elif doc.get('runner') != merged['runner']:
            raise ValueError(f'{p}: runner {doc.get("runner")!r} is not {merged["runner"]!r}')
        for c in doc.get('cells', []):
            if key(c) in seen:
                raise ValueError(f'{p}: {key_text(key(c))} was measured by two shards')
            seen.add(key(c))
            merged['cells'].append(
                {**c, 'cpu_model': doc['cpu_model']} if doc.get('cpu_model') else c
            )
    return merged


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--verdict', metavar='DIR', help='judge the re-measured results in DIR')
    mode.add_argument('--plan', metavar='PLAN', help='write the measure matrix of plan.json')
    p.add_argument('--set', help='the plan.json set to measure')
    p.add_argument(
        '--plan-doc',
        help='plan.json: the guard fails a baselined timing cell of a window --set and --only '
        'measure that no shard measured',
    )
    p.add_argument('--only', default='', help='comma or space separated scenarios to keep')
    p.add_argument(
        '--mode',
        choices=('gate', 'measure'),
        default='gate',
        help="--plan: perf-ci.yaml's mode, where a gate plan is one run",
    )
    p.add_argument(
        '--remeasure',
        help="compare's remeasure.json: --plan plans only the units of its cells, and "
        '--verdict judges each cell on the baseline it carries',
    )
    p.add_argument(
        '--trend',
        help=f"the guard's baselines: {TREND_BASELINE} of the perf-trend branch, where a "
        'missing file fails',
    )
    p.add_argument('--results', help='the downloaded perf-results-<prefix>-measure-* folders')
    p.add_argument(
        '--prefix',
        help="perf-ci.yaml's artifact prefix: read only that call's shard directories",
    )
    p.add_argument('--remeasure-out', help='where compare writes the cells to re-measure')
    p.add_argument(
        '--remeasure-attempt',
        type=int,
        help='the run attempt of the compare whose remeasure.json --verdict judges',
    )
    p.add_argument('--summary', help='a file the Markdown summary is appended to')
    p.add_argument('--out', help='where --plan writes the matrix')
    p.add_argument(
        '--outputs', help='--plan: a $GITHUB_OUTPUT file to append matrix, node, scenarios to'
    )
    a = p.parse_args(argv)
    if a.plan:
        need = ('set', 'out')
    elif a.verdict:
        need = ('results', 'prefix', 'remeasure_attempt', 'remeasure')
    else:
        need = ('trend', 'results', 'prefix', 'remeasure_out', 'plan_doc', 'set')
    missing = [f'--{n.replace("_", "-")}' for n in need if not getattr(a, n)]
    if missing:
        p.error(f'missing {", ".join(missing)}')
    return a


def report(a: argparse.Namespace, text: str) -> None:
    print(text)
    if a.summary:
        with pathlib.Path(a.summary).open('a') as f:
            f.write(text)


def github_output(name: str, value: str) -> str:
    """One $GITHUB_OUTPUT value in the `name<<DELIMITER` form, whose random
    delimiter no value can end early."""
    delimiter = f'ghadelimiter_{uuid.uuid4()}'
    return f'{name}<<{delimiter}\n{value}\n{delimiter}\n'


def run_plan(a: argparse.Namespace) -> int:
    only = a.only.replace(',', ' ').split()
    remeasure = load(a.remeasure) if a.remeasure else None
    matrix = plan(load(a.plan), a.set, only=only, remeasure=remeasure, gate=a.mode == 'gate')
    pathlib.Path(a.out).write_text(json.dumps({'include': matrix['include']}) + '\n')
    if a.outputs:
        written = {
            'matrix': json.dumps({'include': matrix['include']}),
            'node': matrix['node'],
            'scenarios': ' '.join(matrix['scenarios']),
        }
        with pathlib.Path(a.outputs).open('a') as f:
            f.writelines(github_output(name, value) for name, value in written.items())
    print(
        f'{a.out}: {len(matrix["include"])} jobs over {", ".join(matrix["scenarios"]) or "nothing"}'
    )
    return 0


def run(a: argparse.Namespace) -> int:
    if a.plan:
        return run_plan(a)
    if a.verdict:
        results = merge_results(a.results, a.prefix)
        again = merge_results(a.verdict, a.prefix, 'remeasure', since=a.remeasure_attempt)
        out = guard_verdict(load(a.remeasure), results, again)
    else:
        out = run_guard(a)
    report(a, guard_summary(out))
    for f in out.failures:
        print(f'::error::{f}')
    return 1 if out.failures else 0


def run_guard(a: argparse.Namespace) -> Outcome:
    """The guard's compare of --results against the baselines of --trend, writing the red
    cells with their baselines to --remeasure-out; a missing --trend writes nothing."""
    if not pathlib.Path(a.trend).is_file():
        return Outcome(
            failures=[
                (
                    f'the perf-trend branch holds no {TREND_BASELINE}, so the guard has no '
                    'baseline: the scheduled perf-trend workflow writes it with each dev point'
                )
            ]
        )
    runner, baselines = baselines_from_trend(load(a.trend))
    doc = load(a.plan_doc)
    matrix = plan(doc, a.set, only=a.only.replace(',', ' ').split())
    units = planned_windows(c for job in matrix['include'] for c in job['cells'])
    results = merge_results(a.results, a.prefix)
    out = guard(baselines, runner, results, units, windows=scenario_windows(doc))
    decision = {'cells': [{**c, 'baseline': baselines[key(c)]} for c in out.remeasure]}
    pathlib.Path(a.remeasure_out).write_text(json.dumps(decision, indent=2) + '\n')
    return out


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        return run(a)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'::error::perf_gate: {exc!r}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
