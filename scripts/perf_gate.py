#!/usr/bin/env python3
"""The tier 2 performance gate: the guard of the timing cells a web app's harness
measured against the baselines of its base tests/perf/budgets.json, the provenance
check of that file, and the ten-run calibration that proposes it.

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
import copy
import dataclasses
import fnmatch
import functools
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

KEY_FIELDS = ('scenario', 'scale', 'engine', 'width', 'window', 'metric')
TIERS = ('gated', 'exact', 'reported')
CHANGE_FIELDS = ('baseline', 'budget', 'tier', 'headroom', 'source')
# What a calibration writes: budget follows from these and the file maps (check_rule).
CALIBRATED_FIELDS = ('baseline', 'tier', 'headroom', 'source')
ENTRY_FIELDS = (*KEY_FIELDS, *CHANGE_FIELDS, 'reason')
FILE_FIELDS = ('schema', 'runner', 'headroom', 'floor', 'target', 'budgets', 'retired')
FILE_MAPS = ('headroom', 'floor', 'target')
SCHEMA = 2
SERVER_CPU = 'server_cpu_ms'
DEFAULT_HEADROOM = {'cpu_ms': 1.5, 'heap_mb': 1.25, 'rss_mb': 1.25}
DEFAULT_FLOOR = {'cpu_ms': 50, 'heap_mb': 1}
HEADROOM_STEP = 0.05
TIER_RUNS = 10
CI_REPO = 'cplieger/ci'
CALIBRATION_IDENTITY = (
    'https://github.com/cplieger/ci/.github/workflows/perf-calibrate.yaml@refs/heads/main'
)
# A measured file an earlier perf-ci.yaml verdict signed, at the ref its caller pinned: a full
# sha, or main for weekly-perf.
MEASURED_IDENTITY = (
    r'^https://github\.com/cplieger/ci/\.github/workflows/perf-ci\.yaml'
    r'@(refs/heads/main|[0-9a-f]{40})$'
)
ISSUER = 'https://token.actions.githubusercontent.com'
CALIBRATION_RE = re.compile(r'^https://github\.com/cplieger/ci/actions/runs/(\d+)$')
MEASURED_RE = re.compile(
    r'^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/(\d+)/attempts/(\d+)$'
)
SOURCE_FILE_RE = re.compile(r'^(\d+(?:-\d+)?)(?:\.sigstore)?\.json$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
# What checking in a source writes; the rest of the tree is the code it measured.
PLAN_PATH = 'tests/perf/plan.json'
CHECK_IN_PATHS = (
    'tests/perf/budgets.json',
    'tests/perf/sources',
    'tests/perf/accepted',
    PLAN_PATH,
)
# Budgets are written to two decimals, and a widened headroom times a baseline
# is not exact in binary floating point.
TOLERANCE = 0.005


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
    every comparison a budget makes and so would ungate a cell, and a larger
    integer is an OverflowError wherever it meets a float."""
    return json.loads(
        text,
        parse_constant=lambda s: finite(s, float),
        parse_float=lambda s: finite(s, float),
        parse_int=lambda s: finite(s, int),
    )


def headroom_cap(metric: str) -> float:
    """The widest headroom a calibration may give a cell before demoting it."""
    return 2.0 if 'cpu' in family(metric) else 1.5


def budget_for(baseline: float, headroom: float, floor: float) -> float:
    return round(max(floor, baseline * headroom), 2)


def entry_headroom(budgets: dict, e: dict) -> float | None:
    if 'headroom' in e:
        return e['headroom']
    return budgets.get('headroom', {}).get(family(e['metric']))


def entry_floor(budgets: dict, e: dict) -> float:
    return budgets.get('floor', {}).get(family(e['metric']), 0)


def file_target(budgets: dict, metric: str) -> float | None:
    return budgets.get('target', {}).get(family(metric))


# --- the rule --------------------------------------------------------------


def check_rule(budgets: object) -> list[str]:
    """Every way the budget file breaks its own rule, one line each. A file
    with a check_shape error gets those and its schema and runner lines only,
    since entry_errors reads its maps and entries."""
    shape = check_shape(budgets)
    if not isinstance(budgets, dict):
        return shape
    errors = []
    if budgets.get('schema') != SCHEMA:
        errors.append(f'budgets.json: schema must be {SCHEMA}')
    if not isinstance(budgets.get('runner'), str) or not budgets['runner']:
        errors.append('budgets.json: runner must name the runner the budgets were calibrated on')
    errors.extend(f'budgets.json: unknown field {f}' for f in budgets if f not in FILE_FIELDS)
    if shape:
        return errors + shape
    seen = set()
    for e in budgets.get('budgets', []):
        name = key_text(key(e))
        if key(e) in seen:
            errors.append(f'{name}: listed twice')
        seen.add(key(e))
        errors.extend(f'{name}: {m}' for m in entry_errors(budgets, e))
    errors.extend(
        f'{key_text(key(r))}: retired, yet budgets lists it'
        for r in budgets.get('retired', [])
        if key(r) in seen
    )
    return errors


def check_shape(budgets: object) -> list[str]:
    """The rule errors that stop the gate before it reads an entry, each a value
    the code reading one would raise on or misjudge: a document, list, map or
    item of the wrong type, an entry without its key or a known tier, a gated
    or exact entry whose baseline or budget is no number, a headroom, floor
    or target value out of range, and a retired item without its key and reason."""
    if not isinstance(budgets, dict):
        return ['budgets.json: must be a JSON object']
    errors = [
        f'budgets.json: {field} must be a list'
        for field in ('budgets', 'retired')
        if not isinstance(budgets.get(field, []), list)
    ]
    for field, ok, want in (
        ('headroom', lambda x: is_number(x) and x > 0, 'a number above 0'),
        ('floor', lambda x: is_number(x) and x >= 0, 'a number of 0 or more'),
        ('target', lambda x: is_number(x) and x > 0, 'a number above 0'),
    ):
        values = budgets.get(field, {})
        if not isinstance(values, dict):
            errors.append(f'budgets.json: {field} must map a metric family to {want}')
            continue
        errors.extend(
            f'budgets.json: {field}.{fam} is {v!r}, not {want}'
            for fam, v in values.items()
            if not ok(v)
        )
    errors.extend(
        filter(None, (entry_shape(i, e) for i, e in enumerate(items(budgets, 'budgets'))))
    )
    for i, r in enumerate(items(budgets, 'retired')):
        if not isinstance(r, dict):
            errors.append(f'retired[{i}]: must be an object')
            continue
        named = all(isinstance(r.get(f), str) and r[f] for f in KEY_FIELDS)
        if not named or not str(r.get('reason', '')).strip():
            errors.append(f'retired[{i}]: needs the retired key and a reason')
    return errors


def items(budgets: dict, field: str) -> list:
    """budgets[field] when it is a list, else none: check_shape reports it."""
    values = budgets.get(field, [])
    return values if isinstance(values, list) else []


def entry_shape(i: int, e: object) -> str | None:
    if not isinstance(e, dict):
        return f'budgets[{i}]: must be an object'
    missing = [f for f in KEY_FIELDS if not isinstance(e.get(f), str) or not e[f]]
    if missing:
        return f'budgets[{i}]: missing {", ".join(missing)}'
    name, tier = key_text(key(e)), e.get('tier')
    if tier not in TIERS:
        return f'{name}: tier {tier!r} is not one of {", ".join(TIERS)}'
    if tier != 'reported' and not (is_number(e.get('baseline')) and is_number(e.get('budget'))):
        return f'{name}: baseline and budget must be numbers'
    return None


def entry_errors(budgets: dict, e: dict) -> list[str]:
    """The rule errors of an entry check_shape passed."""
    tier = e['tier']
    out = [f'unknown field {f}' for f in e if f not in ENTRY_FIELDS]
    if not isinstance(e.get('source'), str) or not e['source']:
        out.append('no source')
    src = parse_source(e.get('source'))
    if src and src.kind == 'measured':
        if tier != 'exact':
            out.append('only an exact entry takes its value from a measured file')
        elif 'reason' not in e:
            out.append('a value from a measured file needs its one-line reason')
    if 'reason' in e:
        out.extend(reason_errors(e))
    if 'headroom' in e and tier != 'gated':
        out.append('only a gated entry carries its own headroom')
    if tier == 'reported':
        return out
    baseline, budget = e['baseline'], e['budget']
    if tier == 'exact':
        if budget != baseline:
            out.append(f'an exact budget is its baseline {baseline}, not {budget}')
    elif family(e['metric']) == SERVER_CPU:
        target = file_target(budgets, e['metric'])
        if target is None:
            out.append(f'no target for {SERVER_CPU}')
        elif budget != target:
            out.append(f'a server CPU budget is its target {target}, not {budget}')
    else:
        h = entry_headroom(budgets, e)
        if not is_number(h):
            out.append(f'no headroom for {family(e["metric"])}')
        elif h <= 0:
            out.append(f'headroom {h} is not above 0')
        else:
            want = budget_for(baseline, h, entry_floor(budgets, e))
            if abs(budget - want) > TOLERANCE:
                out.append(f'budget {budget} is not max(floor, {baseline} x {h}) = {want}')
    return out


def reason_errors(e: dict) -> list[str]:
    """Why e's reason breaks the rule that reads no base: one non-empty line, and none on
    an entry no headroom, floor or target edit can move."""
    r = e['reason']
    if not isinstance(r, str) or not r.strip() or has_line_break(r):
        return [f'reason must be one non-empty line, not {r!r}']
    if e['tier'] == 'reported':
        return ['a reported entry carries no reason']
    src = parse_source(e.get('source'))
    if e['tier'] == 'exact' and src and src.kind == 'calibration':
        return ['an exact entry from a calibration carries no reason']
    return []


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
# Four ten-run calibrations over seven runner classes put the worst one-job ratio of a pr
# set cell at 1.88 for cpu_ms over 50 ms and 1.64 for server_cpu_ms over 10 ms, and of
# any memory cell at 1.09.
GUARD = {
    'cpu_ms': (2.0, 250.0),
    SERVER_CPU: (2.0, 10.0),
    'heap_mb': (1.25, 2.0),
    'rss_mb': (1.25, 50.0),
}
TIMING_KINDS = ('time', 'server_cpu')


def baselines_from_budgets(doc: dict) -> tuple[str, dict[tuple, float]]:
    """A budgets.json of any schema as its runner and each entry's numeric baseline by
    key; every other field is ignored."""
    entries = doc.get('budgets', [])
    if not isinstance(entries, list):
        raise TypeError('budgets.json: budgets must be a list')
    out = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or not all(isinstance(e.get(f), str) for f in KEY_FIELDS):
            raise ValueError(f'budgets[{i}]: must be an object naming {", ".join(KEY_FIELDS)}')
        if is_number(e.get('baseline')):
            out[key(e)] = e['baseline']
    runner = doc.get('runner')
    if not isinstance(runner, str) or not runner:
        raise ValueError('budgets.json: runner must name the runner the baselines were measured on')
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
    units: dict | None = None,
    *,
    windows: dict[str, set[str]] | None = None,
) -> Outcome:
    """Judge each measured timing cell's median of three against its base baseline: one
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
    if units:
        measured = {key(c) for c in results.get('cells', [])}
        missed = [
            k
            for k in sorted(baselines)
            if family(k[5]) in GUARD and planned(units, key_dict(k)) and k not in measured
        ]
        for k in missed:
            dropped = windows is not None and k[0] in windows and k[4] not in windows[k[0]]
            # A server cell exists only when its window requested the route, so a missing one
            # is a change in what the page requests, not a cell the harness lost.
            if family(k[5]) == SERVER_CPU:
                out.notices.append(
                    f'{key_text(k)}: the base has a baseline for this route, and no shard '
                    'measured it'
                )
            elif dropped:
                out.notices.append(
                    f'{key_text(k)}: the base has a baseline for it, and the head plan.json no '
                    f'longer measures window {k[4]}'
                )
            else:
                out.failures.append(
                    f'{key_text(k)}: the base has a baseline for it, and no shard measured it'
                )
    if not guarded:
        out.failures.append(
            'nothing to guard: the base has a baseline for none of the timing cells this run '
            'measured'
        )
    return out


def guard_verdict(decided: dict, results: dict, remeasured: dict) -> Outcome:
    """Fail each cell of compare's decision (its remeasure.json, every cell carrying its
    base baseline) whose median of six reps, its first three and the three re-measured,
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
                f'The results come from runner `{out.runner}`, which the base baselines were '
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
        'Counts that differ from the base (information)',
        ('Cell', 'Base', 'Measured'),
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
        ('Cell', 'Kind', 'Median', 'Base'),
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


# --- calibration -----------------------------------------------------------


def leave_one_out(
    reps_by_run: list[list[float]], headroom: float, floor: float
) -> tuple[list[int], list[int], float]:
    """Take each run in turn as a pull request against the median of the
    others' medians: the runs whose median of three is over that budget (the
    first stage), those whose median of six with run (i + 1) mod n is over it
    too (the second stage), and the worst median-of-six ratio."""
    n = len(reps_by_run)
    if n < 2:
        return [], [], 0.0
    medians = [statistics.median(r) for r in reps_by_run]
    first, second, worst = [], [], 0.0
    for i in range(n):
        base = statistics.median(medians[:i] + medians[i + 1 :])
        six = statistics.median([*reps_by_run[i], *reps_by_run[(i + 1) % n]])
        if base > 0:
            worst = max(worst, six / base)
        budget = budget_for(base, headroom, floor)
        if medians[i] > budget:
            first.append(i)
            if six > budget:
                second.append(i)
    return first, second, round(worst, 3)


def spread(medians: list[float]) -> float | None:
    lo, hi = min(medians), max(medians)
    if lo > 0:
        return round(hi / lo, 3)
    return 1.0 if hi == 0 else None


def decide_tier(cell: dict, reps_by_run: list, complete: bool, headroom, floor, whole) -> None:
    kind, metric = cell['kind'], cell['metric']
    fam = family(metric)
    if not complete or kind == 'report':
        cell['tier'] = 'reported'
    elif kind == 'count':
        agree = len({r for reps in reps_by_run for r in reps}) == 1
        cell['tier'] = 'exact' if agree else 'reported'
    elif kind == 'server_cpu':
        cell['tier'] = 'gated'
    elif kind != 'time' or fam not in headroom:
        cell['tier'] = 'reported'
    else:
        h0, fl = headroom[fam], floor.get(fam, 0)
        first, second, ratio = leave_one_out(reps_by_run, h0, fl)
        h, step = h0, 0
        while second:
            step += 1
            h = round(h0 + step * HEADROOM_STEP, 2)
            if h > headroom_cap(metric):
                cell['tier'] = 'reported'
                whole['demoted'].append({**key_dict(key(cell)), 'ratio': ratio})
                return
            first, second, _ = leave_one_out(reps_by_run, h, fl)
        cell['tier'] = 'gated'
        if h != h0:
            cell['headroom'] = h
            whole['widened'].append({**key_dict(key(cell)), 'headroom': h, 'ratio': ratio})
        whole['first_stage_runs'].update(first)


def calibrate(
    runs: list[dict],
    *,
    headroom: dict,
    floor: dict,
    repo: str,
    commit: str,
    runner: str,
    run_id: str,
    run_url: str,
    reported: list[dict] | None = None,
) -> dict:
    """The calibration file of TIER_RUNS or more measurements of one commit: each cell's
    baseline, tier and widened headroom, and `whole_pr`. A cell a `reported` pattern of
    plan.json names (reported_patterns) is reported whatever its reps."""
    n = len(runs)
    if n < TIER_RUNS:
        raise ValueError(f'calibrate: a calibration needs {TIER_RUNS} runs, not {n}')
    found: dict[tuple, list[dict]] = {}
    for run in runs:
        for c in run.get('cells', []):
            if c.get('reps'):
                found.setdefault(key(c), []).append(c)
    whole: dict[str, Any] = {'first_stage_runs': set(), 'widened': [], 'demoted': []}
    cells = []
    for k in sorted(found):
        reps_by_run = [list(c['reps']) for c in found[k]]
        kinds = {c.get('kind') for c in found[k]}
        medians = [statistics.median(r) for r in reps_by_run]
        cell = {
            **key_dict(k),
            'kind': kinds.pop() if len(kinds) == 1 else 'mixed',
            'baseline': statistics.median(medians),
            'spread': spread(medians),
            'reps_by_run': reps_by_run,
        }
        if plan_reports(reported or [], cell):
            cell['tier'] = 'reported'
        else:
            complete = len(reps_by_run) == n and cell['kind'] != 'mixed'
            decide_tier(cell, reps_by_run, complete, headroom, floor, whole)
        cells.append(cell)
    whole['first_stage_runs'] = sorted(whole['first_stage_runs'])
    return {
        'schema': SCHEMA,
        'kind': 'calibration',
        'repo': repo,
        'commit': commit,
        'runner': runner,
        'runs': n,
        'run_id': str(run_id),
        'run_url': run_url,
        'cpu_models': sorted(cpu_models(runs)),
        'headroom': headroom,
        'floor': floor,
        'seconds': seconds_per_cell(runs),
        'cells': cells,
        'whole_pr': whole,
    }


def cpu_models(runs: list[dict]) -> set[str]:
    models = set()
    for run in runs:
        models.update(run.get('cpu_models', []))
        if run.get('cpu_model'):
            models.add(run['cpu_model'])
    return models


def seconds_per_cell(runs: list[dict]) -> list[dict]:
    fields = KEY_FIELDS[:4]
    found: dict[tuple, list[float]] = {}
    for run in runs:
        for s in run.get('seconds', []):
            found.setdefault(tuple(s[f] for f in fields), []).append(s['seconds'])
    return [
        {**dict(zip(fields, k, strict=True)), 'seconds': statistics.median(v)}
        for k, v in sorted(found.items())
    ]


# --- the plan and the chart ------------------------------------------------

UNIT_FIELDS = KEY_FIELDS[:4]
# GitHub runs at most 256 jobs from one matrix.
MAX_JOBS = 256


# The sets a workflow gates: perf-ci.yaml's pull-request default and
# weekly-perf.yaml's; test_perf_wiring holds this equal to both.
RUN_SETS = ('pr', 'full')


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


def reported_patterns(doc: dict) -> tuple[list[dict], list[str]]:
    """plan.json's `reported`: the {metric, window?} patterns of cells the consumer never
    gates, and an error line for each item that is no such pattern."""
    found = doc.get('reported', [])
    if not isinstance(found, list):
        return [], ['plan.json: reported must be a list of {metric, window} patterns']
    patterns, errors = [], []
    for i, p in enumerate(found):
        named = isinstance(p, dict) and set(p) <= {'metric', 'window'} and 'metric' in p
        if not named or not all(isinstance(v, str) and v for v in p.values()):
            errors.append(f'plan.json: reported[{i}] is not a {{metric, window}} pattern: {p!r}')
        else:
            patterns.append(p)
    return patterns, errors


def base_plan(git: Git, base_sha: str, path: str) -> dict:
    """plan.json as base_sha's tree holds it at path, {} when it holds none."""
    if not git.has(base_sha, path):
        return {}
    doc = loads(git.show(base_sha, path))
    if not isinstance(doc, dict):
        raise TypeError(f'{path} at {base_sha} is not a JSON object')
    return doc


def held_patterns(head: dict, base: dict) -> list[dict]:
    """The `reported` patterns of plan head that plan base lists too: a pattern decides a
    tier only once the base lists it, so a pull request cannot demote a class by adding
    one."""
    found, _ = reported_patterns(base)
    return [p for p in reported_patterns(head)[0] if p in found]


def plan_reports(patterns: list[dict], c: dict) -> bool:
    return any(
        p['metric'] == c['metric'] and p.get('window', c['window']) == c['window'] for p in patterns
    )


def plan_refusal(tier: str) -> str:
    return f'plan.json reports this cell, so its entry is reported, not {tier}'


def check_plan(budgets: dict, doc: dict, *, reported: list[dict], base: dict) -> list[str]:
    """A gated or exact entry whose unit and window no set of RUN_SETS lists
    leaves the gate unmeasured, so it fails; one of a cell a pattern of reported
    (held_patterns) matches fails when base holds no entry for it or one whose
    CALIBRATED_FIELDS differ, since only a calibration writes those. A set or cell of
    any set that is not an object, and a `reported` item of doc that is no pattern, is
    an error line."""
    sets = doc.get('sets', {})
    if not isinstance(sets, dict):
        return ['plan.json: sets must be an object of named sets']
    patterns, errors = reported, reported_patterns(doc)[1]
    was = {key(e): e for e in base.get('budgets', [])}
    cells = []
    for name, spec in sets.items():
        if not isinstance(spec, dict):
            errors.append(f'plan.json: set {name} must be an object')
            continue
        units = spec.get('cells') or []
        if not isinstance(units, list):
            errors.append(f'plan.json: set {name}: cells must be a list')
            continue
        for u in units:
            if not isinstance(u, dict):
                errors.append(f'plan.json: set {name}: a cell must be an object, not {u!r}')
            elif windows_error(u):
                errors.append(f'plan.json: set {name}: {key_text(unit_key(u))}: {windows_error(u)}')
            elif name in RUN_SETS:
                cells.append(u)
    units = planned_windows(cells)
    gating = [
        e
        for e in budgets.get('budgets', [])
        if e.get('tier') != 'reported' and all(f in e for f in KEY_FIELDS)
    ]
    return (
        errors
        + [
            f'{key_text(key(e))}: a {e["tier"]} entry no set of plan.json measures '
            f'({", ".join(RUN_SETS)}); remove it and name it in retired'
            for e in gating
            if not planned(units, e)
        ]
        + [
            f'{key_text(key(e))}: {plan_refusal(e["tier"])}'
            for e in gating
            if plan_reports(patterns, e)
            and (
                key(e) not in was or any(e.get(f) != was[key(e)].get(f) for f in CALIBRATED_FIELDS)
            )
        ]
    )


def plan(
    doc: dict,
    set_name: str,
    *,
    only: list[str] | None = None,
    runs: int = 1,
    remeasure: dict | None = None,
) -> dict:
    """The measure matrix of one set of plan.json: its cells (a scenario at a
    scale, engine and width, every window or those its `windows` lists) split
    into at most `shards` jobs, longest first onto the least-loaded shard,
    repeated `runs` times. With `remeasure`, only the cells compare listed,
    once each. `include` alone is the GitHub matrix: any other key there
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
    if not isinstance(runs, int) or runs < 1:
        raise ValueError(f'runs must be a positive integer, not {runs!r}')
    units = spec.get('cells') or []
    if not units or not isinstance(units, list):
        raise ValueError(f'set {set_name} lists no cells')
    check_units(f'set {set_name}', units)
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
    count = min(shards, len(units))
    bins: list[list[dict]] = [[] for _ in range(count)]
    load = [0.0] * count
    for u in sorted(units, key=lambda u: -u['seconds']):
        i = load.index(min(load))
        bins[i].append(dict(u))
        load[i] += u['seconds']
    if count * runs > MAX_JOBS:
        raise ValueError(f'{count} shards x {runs} runs is over the {MAX_JOBS} jobs of a matrix')
    include = []
    for r in range(1, runs + 1):
        for i, cells in enumerate(bins, 1):
            name = f'shard-{i}-of-{count}'
            if runs > 1:
                name = f'run-{r:02d}-{name}'
            include.append({'name': name, 'run': r, 'seconds': load[i - 1], 'cells': cells})
    return {
        'include': include,
        'scenarios': sorted({u['scenario'] for u in units}),
        'node': node,
    }


def check_units(label: str, units: list) -> None:
    """Raise on a unit plan() cannot shard: not an object, a line break, a missing key
    field, a unit listed twice, no positive seconds, or a bad windows list."""
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
        if k in seen:
            raise ValueError(f'{label}: {key_text(k)} is listed twice')
        seen.add(k)
        if not is_number(u.get('seconds')) or u['seconds'] <= 0:
            raise ValueError(f'{label}: {key_text(k)} needs its seconds per cell')
        if windows_error(u):
            raise ValueError(f'{label}: {key_text(k)}: {windows_error(u)}')


def bench(results: dict) -> list[dict]:
    """Each measured cell's median as a github-action-benchmark
    `customSmallerIsBetter` point."""
    points = [
        {
            'name': key_text(key(c)),
            'unit': family(c['metric']),
            'value': statistics.median(c['reps']),
        }
        for c in results.get('cells', [])
        if c.get('reps')
    ]
    return sorted(points, key=lambda p: p['name'])


# --- provenance ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Signer:
    """The certificate a keyless bundle must carry: an exact identity or a pattern of one,
    and the repository whose workflow run requested it when set."""

    identity: str | None = None
    identity_regexp: str | None = None
    workflow_repository: str | None = None


# Any public repository can call perf-calibrate.yaml, and its run's certificate names the
# same workflow ref, so only the workflow repository says the run was cplieger/ci's.
CALIBRATION_SIGNER = Signer(identity=CALIBRATION_IDENTITY, workflow_repository=CI_REPO)


@dataclasses.dataclass(frozen=True)
class Source:
    """The signed file an entry's source URL names: `kind` calibration (a perf-calibrate
    run, `<run id>.json`) or measured (a perf-ci run attempt, `<run id>-<attempt>.json`)."""

    kind: str
    repository: str
    run_id: str
    attempt: str
    url: str

    @property
    def stem(self) -> str:
        return self.run_id if self.kind == 'calibration' else f'{self.run_id}-{self.attempt}'

    @property
    def label(self) -> str:
        return f'{"calibration" if self.kind == "calibration" else "measured file"} {self.stem}'


def parse_source(url: object) -> Source | None:
    if not isinstance(url, str):
        return None
    if m := CALIBRATION_RE.match(url):
        return Source('calibration', CI_REPO, m[1], '', url)
    if m := MEASURED_RE.match(url):
        return Source('measured', m[1], m[2], m[3], url)
    return None


class Curable(str):
    """A refusal a measured file of this head cures, so its exact entry is stale: a
    measured file's content, a source made for another change or a pull request, a
    superseded entry or a hand-edited value. A bundle that does not verify, or git
    failing, is no such refusal: re-measuring cures neither."""

    __slots__ = ()


@dataclasses.dataclass
class Loaded:
    """A source file that verified, with the commit placed_commit put it at and its cells."""

    source: Source
    doc: dict
    at: str
    cells: dict


@dataclasses.dataclass
class Checked:
    """check_provenance's answer. notices: summary lines, such as a base of another schema
    skipping moves_to_older."""

    errors: list[str]
    notices: list[str] = dataclasses.field(default_factory=list)


def check_provenance(
    head: dict,
    base: dict,
    *,
    sources: pathlib.Path,
    accepted: pathlib.Path | None,
    repo: str,
    head_sha: str,
    base_sha: str,
    verify: Callable[[pathlib.Path, pathlib.Path, Signer], str | None],
    git: Git,
) -> Checked:
    """The provenance of head against base. Every source an entry names must load
    (load_source) and list its cell; an entry the head adds or whose baseline, budget,
    tier, headroom or source differs from the base, every entry when the file's runner,
    headroom, floor or target differs, must match it (entry_provenance); every entry
    comes from the newest source of its cell (superseded_entries) and, over a base of this
    schema, from none older than its base entry's (moves_to_older), else notices says so;
    a gated or exact entry leaves only through `retired`; every file under sources is named
    by an entry; and every accepted file holds (load_accepted)."""
    errors = []
    notices = []
    base_entries = {key(e): e for e in base.get('budgets', [])}
    head_entries = {key(e): e for e in head.get('budgets', [])}
    # The file-level maps sit outside CHANGE_FIELDS, and each changes how every entry gates.
    moved = any(head.get(f) != base.get(f) for f in FILE_MAPS)
    file_edit = moved or head.get('runner') != base.get('runner')
    # A base of another schema keeps its sources outside `sources`, so none can be placed.
    base_placeable = base.get('schema') == SCHEMA
    if base_entries and not base_placeable:
        notices.append(
            f'the base budgets.json is schema {base.get("schema")!r}, which names no source '
            "this gate can place, so no entry was held to a source as new as its base entry's"
        )
    named: dict[str, Source] = {}
    for e in head_entries.values():
        src = parse_source(e.get('source'))
        if src:
            named.setdefault(src.stem, src)
    loaded: dict[str, Loaded] = {}
    unloaded: dict[str, str] = {}
    base_places: dict[str, tuple[str | None, str | None]] = {}

    def placed_at_base(src: Source) -> tuple[str | None, str | None]:
        if src.stem not in base_places:
            base_places[src.stem] = base_place(src, sources, git, base_sha)
        return base_places[src.stem]

    for stem, src in sorted(named.items()):
        found = load_source(
            src,
            sources,
            repo=repo,
            runner=head.get('runner'),
            head_sha=head_sha,
            verify=verify,
            git=git,
        )
        if isinstance(found, str):
            unloaded[stem] = type(found)(f'{src.label}: {found}')
            errors.append(unloaded[stem])
        else:
            loaded[stem] = found
    for k, e in head_entries.items():
        name, b = key_text(k), base_entries.get(k)
        src = parse_source(e.get('source'))
        if src is None:
            errors.append(
                f'{name}: source {e.get("source")!r} is not a calibration or measured run URL'
            )
            continue
        mine = []
        raised_by_file = map_moved(head, base, e)
        if 'reason' in e:
            mine += reason_provenance(e, b, src, raised_by_file)
        if src.stem in loaded:
            changed = b is None or file_edit or any(e.get(f) != b.get(f) for f in CHANGE_FIELDS)
            found = loaded[src.stem]
            mine += entry_provenance(e, b, found, changed, raised_by_file, base_sha, git)
            if base_placeable:
                mine += moves_to_older(e, b, found, placed_at_base, git)
        errors.extend(f'{name}: {x}' for x in mine)
    retired = {key(r): r for r in head.get('retired', []) if all(f in r for f in KEY_FIELDS)}
    for k, b in sorted(base_entries.items()):
        if k in head_entries or b.get('tier') == 'reported':
            continue
        if not str(retired.get(k, {}).get('reason', '')).strip():
            errors.append(f'{key_text(k)}: a {b.get("tier")} entry left without a retired reason')
    if sources.is_dir():
        for p in sorted(sources.iterdir()):
            m = SOURCE_FILE_RE.match(p.name)
            if m is None:
                errors.append(f'{p.name}: not a source file or bundle')
            elif m[1] not in named:
                errors.append(f'{p.name}: no budget entry names source {m[1]}')
    errors.extend(
        f'{key_text(k)}: {why}' for k, why in superseded_entries(head_entries, loaded, git)
    )
    errors += load_accepted(accepted, head_entries, loaded, head_sha, git)
    return Checked(errors, notices)


def map_moved(head: dict, base: dict, e: dict) -> bool:
    """Whether a file-level map moves entry e's budget: e is gated and its family's headroom,
    floor or target in head differs from base's. An exact budget is its baseline."""
    if e.get('tier') != 'gated':
        return False
    fam = family(e['metric'])

    def value(doc: dict, field: str) -> object:
        m = doc.get(field)
        return m.get(fam) if isinstance(m, dict) else None

    return any(value(head, f) != value(base, f) for f in FILE_MAPS)


ACCEPTED_FIELDS = ('reason', 'cells')
ACCEPTED_CELL_FIELDS = (*KEY_FIELDS, 'budget')


def load_accepted(
    directory: pathlib.Path | None,
    entries: dict,
    loaded: dict[str, Loaded],
    head_sha: str,
    git: Git,
) -> list[str]:
    """The error of each accepted file or cell under directory that breaks the rule:
    `<name>.json` holding {reason: one line, cells: [{six key fields, budget above 0}]},
    each cell a gated entry of entries and in one file. A cell a loaded calibration
    measured at a commit holding the commit that added its file is absorbed, so it is an
    error."""
    found: dict[tuple, str] = {}
    errors: list[str] = []
    if directory is None or not directory.is_dir():
        return errors
    for p in sorted(directory.iterdir()):
        label = f'accepted/{p.name}'
        _, cells, why = accepted_file(p)
        if why:
            errors.append(f'{label}: {why}')
            continue
        at = None
        for c in cells:
            k, name = key(c), key_text(key(c))
            if entries.get(k, {}).get('tier') != 'gated':
                errors.append(f'{label}: {name}: has no gated entry')
            elif k in found:
                errors.append(f'{label}: {name}: also accepted by accepted/{found[k]}')
            else:
                try:
                    at = at or git.added_by(str(p), head_sha)
                    absorbed = absorbed_by(k, at, loaded, git)
                except GitError as exc:
                    errors.append(f'{label}: {exc}')
                    break
                if absorbed:
                    errors.append(
                        f'{label}: {name}: {absorbed.source.label} measured this cell at '
                        f'{absorbed.at}, which holds the override; delete {label}'
                    )
                else:
                    found[k] = p.name
    return errors


def accepted_file(p: pathlib.Path) -> tuple[str, list[dict], str | None]:
    """The reason and cells of accepted file p, or why it is not one."""
    if not p.is_file() or not p.name.endswith('.json'):
        return '', [], 'not an accepted file, a <name>.json'
    try:
        doc = loads(p.read_text())
    except (OSError, ValueError) as exc:
        return '', [], str(exc)
    if not isinstance(doc, dict):
        return '', [], 'must be a JSON object'
    unknown = [f for f in doc if f not in ACCEPTED_FIELDS]
    if unknown:
        return '', [], f'unknown field {", ".join(unknown)}'
    r = doc.get('reason')
    if not isinstance(r, str) or not r.strip() or has_line_break(r):
        return '', [], f'reason must be one non-empty line, not {r!r}'
    cells = doc.get('cells')
    if not isinstance(cells, list) or not cells:
        return r, [], 'cells must be a non-empty list of cells'
    for i, c in enumerate(cells):
        if not isinstance(c, dict) or any(not isinstance(c.get(f), str) for f in KEY_FIELDS):
            return r, [], f'cells[{i}] does not name a cell by {", ".join(KEY_FIELDS)}'
        name = key_text(key(c))
        unknown = [f for f in c if f not in ACCEPTED_CELL_FIELDS]
        if unknown:
            return r, [], f'{name}: unknown field {", ".join(unknown)}'
        if not is_number(c.get('budget')) or c['budget'] <= 0:
            return r, [], f'{name}: budget {c.get("budget")!r} is not a number above 0'
    return r, cells, None


def absorbed_by(k: tuple, at: str, loaded: dict[str, Loaded], git: Git) -> Loaded | None:
    """A loaded calibration that measured cell k at a commit holding at, if any."""
    for s in loaded.values():
        if s.source.kind == 'calibration' and k in s.cells and git.is_ancestor(at, s.at):
            return s
    return None


def load_source(
    src: Source,
    directory: pathlib.Path,
    *,
    repo: str,
    runner: object,
    head_sha: str,
    verify: Callable[[pathlib.Path, pathlib.Path, Signer], str | None],
    git: Git,
) -> Loaded | str:
    """src's checked-in file, once its bundle verifies for the workflow that signs its kind,
    it names this repository, runner and run, and placed_commit puts it in head's history;
    else why not, Curable for what a fresh measured file replaces."""

    def refuse(why: str) -> str:
        return Curable(why) if src.kind == 'measured' else why

    blob = directory / f'{src.stem}.json'
    bundle = directory / f'{src.stem}.sigstore.json'
    if not blob.is_file() or not bundle.is_file():
        return refuse(f'{blob.name} and {bundle.name} must both be checked in')
    try:
        doc = load(blob)
    except (OSError, ValueError, TypeError) as exc:
        return refuse(str(exc))
    cells, why = source_cells(doc, src.kind)
    if why:
        return refuse(why)
    if src.kind == 'measured':
        for field, want in (('kind', 'measured'), ('schema', SCHEMA)):
            if doc.get(field) != want:
                return refuse(f'{field} is {doc.get(field)!r}, not {want!r}')
        signed_in = doc.get('workflow_repository')
        if signed_in not in (repo, CI_REPO):
            return refuse(f'workflow_repository is {signed_in!r}, not {repo} or {CI_REPO}')
        signer = Signer(identity_regexp=MEASURED_IDENTITY, workflow_repository=signed_in)
        url = f'https://github.com/{signed_in}/actions/runs/{src.run_id}/attempts/{src.attempt}'
        fields = (('run_id', src.run_id), ('run_attempt', src.attempt), ('run_url', url))
    else:
        signer = CALIBRATION_SIGNER
        fields = (('run_id', src.run_id), ('run_url', src.url))
    failure = verify(blob, bundle, signer)
    if failure:
        return f'the bundle does not verify: {failure}'
    for field, want in (*fields, ('repo', repo), ('runner', runner)):
        if str(doc.get(field)) != str(want):
            return refuse(f'{field} is {doc.get(field)!r}, not {want!r}')
    runs = doc.get('runs')
    if src.kind == 'calibration' and not (is_number(runs) and runs >= TIER_RUNS):
        return f'{runs} runs decide no tier; a calibration needs {TIER_RUNS}'
    at, why = placed_commit(doc, blob, git, head_sha)
    return why if at is None else Loaded(src, doc, at, cells)


def source_cells(doc: dict, kind: str) -> tuple[dict, str | None]:
    """A source doc's cells by key, or why they are not cells: a measured cell needs its value."""
    cells = doc.get('cells')
    if not isinstance(cells, list):
        return {}, 'cells is not a list'
    out = {}
    for i, c in enumerate(cells):
        if not isinstance(c, dict) or any(not isinstance(c.get(f), str) for f in KEY_FIELDS):
            return {}, f'cells[{i}] does not name a cell by {", ".join(KEY_FIELDS)}'
        if kind == 'measured' and not is_number(c.get('value')):
            return {}, f'cells[{i}]: value {c.get("value")!r} is not a number'
        out[key(c)] = c
    return out, None


def entry_provenance(
    e: dict,
    b: dict | None,
    found: Loaded,
    changed: bool,
    file_moved: bool,
    base_sha: str,
    git: Git,
) -> list[str]:
    """Why entry e, b its base entry, does not come from source found: the source lists its
    cell, and a changed entry matches it (measured_provenance, calibration_provenance)."""
    src, c = found.source, found.cells.get(key(e))
    if c is None:
        return [f'{src.label} did not measure this cell']
    # A stem names one run attempt, and the URL's repository is the part it leaves out.
    if src.kind == 'measured' and e['source'] != found.doc['run_url']:
        return [
            (
                f'source {e["source"]} is not the run that wrote {src.stem}.json, '
                f'{found.doc["run_url"]}'
            )
        ]
    if not changed:
        return []
    if src.kind == 'measured':
        return measured_provenance(e, b, src, c)
    return calibration_provenance(e, b, found, c, file_moved, base_sha, git)


def measured_provenance(e: dict, b: dict | None, src: Source, c: dict) -> list[str]:
    """A measured file moves the value of an exact base entry to the value it measured, and
    never adds an entry or moves a tier: those come from the scheduled calibration."""
    if b is None:
        return [f'{src.label} moves the value of an exact entry, and adds none']
    if b.get('tier') != 'exact':
        return [f'{src.label} moves the value of an exact entry, not a {b.get("tier")} one']
    if e.get('baseline') != c['value']:
        return [
            Curable(
                f'baseline {e.get("baseline")} is not what run {src.url} measured ({c["value"]}); '
                'take the value with perf_gate.py --take'
            )
        ]
    return []


def calibration_provenance(
    e: dict,
    b: dict | None,
    found: Loaded,
    c: dict,
    file_moved: bool,
    base_sha: str,
    git: Git,
) -> list[str]:
    """A changed entry takes baseline, tier and headroom from a calibration the base holds,
    since only the default branch is calibrated. With file_moved (map_moved), a budget above
    the base's needs a `reason` even when its baseline did not rise."""
    label = found.source.label
    out = []
    if e.get('baseline') != c.get('baseline'):
        out.append(Curable(f"baseline {e.get('baseline')} is not {label}'s {c['baseline']}"))
    if (e.get('tier'), e.get('headroom')) != (c.get('tier'), c.get('headroom')):
        out.append(f'tier and headroom must be {c.get("tier")}, {c.get("headroom")} ({label})')
    try:
        if not git.is_ancestor(found.at, base_sha):
            out.append(
                Curable(
                    f'{label} measured {found.at}, which the base {base_sha} does not hold; '
                    'a calibration of a pull request moves no entry'
                )
            )
    except GitError as exc:
        out.append(str(exc))
    raised = b is not None and is_number(b.get('budget')) and is_number(e.get('budget'))
    if file_moved and raised and e['budget'] > b['budget']:
        if not str(e.get('reason', '')).strip():
            what = (
                'raises its target and budget'
                if family(e['metric']) == SERVER_CPU
                else 'the file headroom or floor raises its budget'
            )
            out.append(f'{what} from {b["budget"]} to {e["budget"]} without a reason')
    return out


def reason_provenance(e: dict, b: dict | None, src: Source, file_moved: bool) -> list[str]:
    """Why a gated calibration entry may not carry its reason: one the base entry does not
    already carry is allowed only on a budget a file-level edit raises (map_moved)."""
    if src.kind != 'calibration' or e['tier'] != 'gated':
        return []
    if b is not None and b.get('reason') == e['reason']:
        return []
    if file_moved and b is not None and is_number(b.get('budget')) and e['budget'] > b['budget']:
        return []
    return [
        (
            'a reason belongs on a measured value, or on a budget a headroom, floor or target '
            'edit raises'
        )
    ]


def superseded_entries(
    entries: dict, loaded: dict[str, Loaded], git: Git
) -> list[tuple[tuple, str]]:
    """(key, why) for each entry not from the newest loaded source of its cell. Run ids
    order dispatches, not commits, so only the place of each source in head's history
    says which is newer."""
    newer = functools.cache(lambda old, new: old != new and git.is_ancestor(old, new))
    errors = []
    for k, e in entries.items():
        src = parse_source(e.get('source'))
        mine = loaded.get(src.stem) if src else None
        if mine is None or k not in mine.cells:
            continue
        try:
            later = [s for s in loaded.values() if k in s.cells and newer(mine.at, s.at)]
            newest = [s for s in later if not any(newer(s.at, o.at) for o in later)]
        except GitError as exc:
            errors.append((k, str(exc)))
            continue
        if newest:
            r = newest[0]
            errors.append(
                (
                    k,
                    Curable(
                        f'{r.source.label} measured this cell at {r.at}, which descends from '
                        f"source {mine.source.label}'s {mine.at}; "
                        f'take the entry from {r.source.stem}'
                    ),
                )
            )
    return errors


def base_place(
    src: Source, sources: pathlib.Path, git: Git, base_sha: str
) -> tuple[str | None, str | None]:
    """(where placed_commit puts src's file in base_sha's tree, None), or (None, why not)."""
    path = sources / f'{src.stem}.json'
    try:
        doc = loads(git.show(base_sha, str(path)))
    except (GitError, ValueError) as exc:
        return None, str(exc)
    if not isinstance(doc, dict):
        return None, f'{path.name} is not a JSON object'
    at, why = placed_commit(doc, path, git, base_sha)
    return at, None if why is None else str(why)


def moves_to_older(
    e: dict,
    b: dict | None,
    found: Loaded,
    placed_at_base: Callable[[Source], tuple[str | None, str | None]],
    git: Git,
) -> list[str]:
    """Why entry e may not leave its base entry b's source for found: superseded_entries
    sees only the sources the head names, so deleting the base's newer source would let
    an entry demote, raise or move back through an older one."""
    bs = parse_source(b.get('source')) if b else None
    if bs is None or b['source'] == e['source']:
        return []
    label = f"the base's {bs.label}"
    place, why = placed_at_base(bs)
    if place is None:
        return [f'{label}: {why}']
    try:
        older = found.at != place and git.is_ancestor(found.at, place)
    except GitError as exc:
        return [f'{label}: {exc}']
    if not older:
        return []
    return [
        (
            f'{found.source.label} measured {found.at}, older than {label} at {place}; '
            'an entry moves only to a newer source'
        )
    ]


def placed_commit(doc: dict, path: pathlib.Path, git: Git, head_sha: str) -> tuple:
    """(the commit in head's history whose code source doc at path measured, None), or
    (None, why it has none), Curable unless git failed. That is its `commit` when the commit that added path already
    held it. Otherwise the source measured the pull request that added path, and the
    adding commit (its merge ref, or its squash) must make the change it measured; the
    place is then its `commit` when head contains it, else the squash."""
    try:
        measured = git.commit(str(doc.get('commit')))
        added = git.added_by(str(path), head_sha)
        if git.is_ancestor(measured, f'{added}^'):
            return measured, None
        if git.same_change(measured, added):
            return (measured if git.is_ancestor(measured, head_sha) else added), None
    except (GitError, ValueError) as exc:
        return None, str(exc)
    retake = (
        'take the measured file this run wrote'
        if doc.get('kind') == 'measured'
        else 'take entries only from a calibration of the default branch'
    )
    return None, Curable(
        f'{added}, which added the file, makes another change than measured {measured} '
        f'made; {retake}'
    )


def cosign_verify(blob, bundle, signer: Signer) -> str | None:
    """None when the keyless bundle verifies for signer, else why not."""
    cmd = ['cosign', 'verify-blob', '--bundle', str(bundle), '--certificate-oidc-issuer', ISSUER]
    if signer.identity:
        cmd += ['--certificate-identity', signer.identity]
    if signer.identity_regexp:
        cmd += ['--certificate-identity-regexp', signer.identity_regexp]
    if signer.workflow_repository:
        cmd += ['--certificate-github-workflow-repository', signer.workflow_repository]
    cmd.append(str(blob))
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return 'cosign is not installed'
    except subprocess.TimeoutExpired:
        return 'cosign verify-blob timed out'
    if done.returncode == 0:
        return None
    return (done.stderr or done.stdout).strip() or f'cosign exited {done.returncode}'


# --- take and check-in -----------------------------------------------------


def dump_budgets(doc: dict) -> str:
    """budgets.json with one top-level field, and one item of a list, per line, so that a
    moved value is a one-line diff."""
    lines = []
    for k, v in doc.items():
        if isinstance(v, list) and v:
            body = ',\n'.join(f'    {json.dumps(x)}' for x in v)
            lines.append(f'  {json.dumps(k)}: [\n{body}\n  ]')
        else:
            lines.append(f'  {json.dumps(k)}: {json.dumps(v)}')
    return '{\n' + ',\n'.join(lines) + '\n}\n'


def unnamed_sources(directory: pathlib.Path, budgets: dict) -> list[str]:
    """The source files and bundles under directory that no entry of budgets names."""
    named = {s.stem for e in budgets.get('budgets', []) if (s := parse_source(e.get('source')))}
    if not directory.is_dir():
        return []
    return sorted(
        p.name
        for p in directory.iterdir()
        if (m := SOURCE_FILE_RE.match(p.name)) and m[1] not in named
    )


def take(budgets: dict, doc: dict, *, reason: str, cells: list[str]) -> list[tuple]:
    """The keys of measured file doc whose key text a glob of cells matches, each written
    into its budgets entry: the value as baseline and budget, the run as source, and
    reason. A file that is no measured file, a reason that is not one line, globs that
    match no cell and a cell with no exact entry raise before any entry changes."""
    if doc.get('kind') != 'measured':
        raise ValueError(f'kind {doc.get("kind")!r}: it is not a measured file')
    src = parse_source(doc.get('run_url'))
    if src is None or src.kind != 'measured':
        raise ValueError(f'run_url {doc.get("run_url")!r} is not a perf-ci run attempt')
    if not isinstance(reason, str) or not reason.strip() or has_line_break(reason):
        raise ValueError(f'the reason must be one non-empty line, not {reason!r}')
    found, why = source_cells(doc, 'measured')
    if why:
        raise ValueError(why)
    entries = {key(e): e for e in budgets.get('budgets', [])}
    chosen = [k for k in found if any(fnmatch.fnmatchcase(key_text(k), g) for g in cells)]
    if not chosen:
        raise ValueError(f'{", ".join(cells)} matches no cell of the measured file')
    for k in chosen:
        if entries.get(k, {}).get('tier') != 'exact':
            raise ValueError(f'{key_text(k)} has no exact entry to take its value')
    for k in chosen:
        value = found[k]['value']
        entries[k].update(baseline=value, budget=value, source=src.url, reason=reason)
    return chosen


@dataclasses.dataclass
class CheckedIn:
    """check_in's answer: the new budgets and plan, each changed accepted file's new doc
    (None deletes it), the source files to delete and the pull request body."""

    budgets: dict
    plan: dict
    accepted: dict[str, dict | None]
    drop: list[str]
    body: str


def check_in(
    cal: dict,
    budgets: dict,
    plan_doc: dict,
    *,
    sources: pathlib.Path,
    accepted: pathlib.Path | None,
    repo: str,
    head_sha: str,
    git: Git,
) -> CheckedIn:
    """The default branch's files once calibration cal is checked in under sources. Each
    cell cal measured takes its entry from it (calibrated_entry), but one whose entry comes
    from a measured file placed after cal's commit; every other entry stays. An accepted
    cell cal absorbed (absorbed_by's rule) or whose budget no longer exceeds its entry's
    goes, plan.json takes cal's seconds, a source no entry names is dropped, and so is a
    retired item of a cell cal measured, whose tier is cal's again."""
    src = calibration_source(cal, budgets, repo)
    at = git.commit(str(cal.get('commit')))
    measured, why = source_cells(cal, 'calibration')
    if why:
        raise ValueError(f'the calibration: {why}')
    entries = {key(e): e for e in budgets.get('budgets', [])}
    places = functools.cache(lambda stem: measured_place(sources, stem, head_sha, git))
    tiers, raises, kept = [], [], []
    for k, c in measured.items():
        old = entries.get(k)
        s = parse_source(old.get('source')) if old else None
        if s and s.kind == 'measured' and (m := places(s.stem)) and not git.is_ancestor(m, at):
            kept.append((key_text(k), old['baseline'], c.get('baseline'), s.label))
            continue
        new = entries[k] = calibrated_entry(budgets, c, src.url)
        before = old['tier'] if old else 'none'
        if before != new['tier']:
            tiers.append((key_text(k), before, new['tier']))
        if old and old['tier'] == new['tier'] == 'gated' and new['baseline'] > old['baseline']:
            ratio = round(new['baseline'] / old['baseline'], 3) if old['baseline'] else ''
            raises.append((key_text(k), old['baseline'], new['baseline'], ratio))
    head = {**budgets, 'budgets': [entries[k] for k in sorted(entries)]}
    back = [r for r in budgets.get('retired', []) if key(r) in measured]
    if back:
        head['retired'] = [r for r in budgets['retired'] if key(r) not in measured]
    files, removed = check_in_accepted(accepted, entries, (src, measured, at), head_sha, git)
    drop = unnamed_sources(sources, head)
    dropped = sorted({SOURCE_FILE_RE.match(n)[1] for n in drop})
    lines = [
        f'## Calibration {src.run_id} of {repo} at `{str(cal["commit"])[:12]}`',
        '',
        (
            f'{cal["runs"]} runs on `{cal["runner"]}` '
            f'({", ".join(cal.get("cpu_models") or []) or "CPU model unknown"}), '
            f'{len(measured)} cells: {src.url}'
        ),
        '',
        *table('Tier changes', ('Cell', 'Before', 'After'), tiers),
        *table('Raised gated baselines', ('Cell', 'Before', 'After', 'Ratio'), raises),
        *table('Measured values kept', ('Cell', 'Measured', 'Calibrated', 'Source'), kept),
        *table('Accepted slowdowns removed', ('File', 'Cell', 'Why'), removed),
        *table(
            'Retired cells measured again',
            ('Cell', 'Reason'),
            ((key_text(key(r)), r['reason']) for r in back),
        ),
    ]
    if dropped:
        labels = (f'{"measured file" if "-" in d else "calibration"} {d}' for d in dropped)
        lines += ['### Sources dropped', '', *(f'- {x}' for x in labels), '']
    return CheckedIn(head, check_in_plan(plan_doc, cal), files, drop, '\n'.join(lines) + '\n')


def calibration_source(cal: dict, budgets: dict, repo: str) -> Source:
    """The source of cal, once it is a ten-run calibration of repo on budgets' runner."""
    for field, want in (
        ('kind', 'calibration'),
        ('schema', SCHEMA),
        ('repo', repo),
        ('runner', budgets.get('runner')),
    ):
        if cal.get(field) != want:
            raise ValueError(f'the calibration: {field} is {cal.get(field)!r}, not {want!r}')
    runs = cal.get('runs')
    if not (is_number(runs) and runs >= TIER_RUNS):
        raise ValueError(
            f'the calibration has {runs!r} runs, and a calibration needs {TIER_RUNS} runs'
        )
    src = parse_source(cal.get('run_url'))
    if src is None or src.kind != 'calibration' or src.run_id != str(cal.get('run_id')):
        raise ValueError(
            f'the calibration: run_url {cal.get("run_url")!r} is not run {cal.get("run_id")!r}'
        )
    return src


def calibrated_entry(budgets: dict, c: dict, url: str) -> dict:
    """The entry calibration cell c gives under budgets' headroom, floor and target, which
    entry_errors and calibration_provenance hold it to; it carries no reason."""
    tier, baseline, name = c.get('tier'), c.get('baseline'), key_text(key(c))
    if tier not in TIERS or not is_number(baseline):
        raise ValueError(f'{name}: the calibration gives tier {tier!r}, baseline {baseline!r}')
    e = {**key_dict(key(c)), 'tier': tier, 'baseline': baseline}
    fam = family(c['metric'])
    if tier == 'exact':
        e['budget'] = baseline
    elif tier == 'gated' and fam == SERVER_CPU:
        target = file_target(budgets, c['metric'])
        if target is None:
            raise ValueError(f'{name}: no target for {SERVER_CPU}')
        e['budget'] = target
    elif tier == 'gated':
        h = c.get('headroom', budgets.get('headroom', {}).get(fam))
        if not is_number(h) or h <= 0:
            raise ValueError(f'{name}: no headroom for {fam}')
        e['budget'] = budget_for(baseline, h, entry_floor(budgets, c))
    if 'headroom' in c:
        e['headroom'] = c['headroom']
    e['source'] = url
    return e


def measured_place(sources: pathlib.Path, stem: str, head_sha: str, git: Git) -> str | None:
    """Where placed_commit puts measured file stem under sources, None when the file is
    absent or its place is a Curable refusal, which the gate holds against its entry too.
    A git failure raises, since reading it as no place would overwrite the file's value."""
    path = sources / f'{stem}.json'
    if not path.is_file():
        return None
    at, why = placed_commit(load(path), path, git, head_sha)
    if at is None and not isinstance(why, Curable):
        raise GitError(f'measured file {stem}: {why}')
    return at


def check_in_accepted(
    directory: pathlib.Path | None,
    entries: dict,
    cal: tuple[Source, dict, str],
    head_sha: str,
    git: Git,
) -> tuple[dict[str, dict | None], list[tuple]]:
    """Each accepted file check_in changes, with its new doc or None, and a (file, cell,
    why) line for each cell it removes. cal is the calibration's source, cells and commit."""
    src, measured, at = cal
    files: dict[str, dict | None] = {}
    removed = []
    if directory is None or not directory.is_dir():
        return files, removed
    for p in sorted(directory.iterdir()):
        reason, cells, why = accepted_file(p)
        if why:
            raise ValueError(f'accepted/{p.name}: {why}')
        added = git.added_by(str(p), head_sha)
        keep = []
        for c in cells:
            e = entries.get(key(c))
            if key(c) in measured and git.is_ancestor(added, at):
                gone = f'{src.label} measured it'
            elif e is None or e['tier'] != 'gated':
                gone = f'its entry is {e["tier"] if e else "gone"}, not gated'
            elif e['budget'] >= c['budget']:
                gone = f'budget {e["budget"]} is at or over {c["budget"]}'
            else:
                keep.append(c)
                continue
            removed.append((f'accepted/{p.name}', key_text(key(c)), gone))
        if len(keep) < len(cells):
            files[p.name] = {'reason': reason, 'cells': keep} if keep else None
    return files, removed


def check_in_plan(plan_doc: dict, cal: dict) -> dict:
    """plan_doc with each unit's seconds the calibration measured, rounded up."""
    seconds = {
        unit_key(s): s['seconds'] for s in cal.get('seconds', []) if is_number(s.get('seconds'))
    }
    doc = copy.deepcopy(plan_doc)
    sets = doc.get('sets')
    for spec in sets.values() if isinstance(sets, dict) else ():
        for u in spec.get('cells') or [] if isinstance(spec, dict) else ():
            # A unit listing some windows runs for less than the full measure timed.
            if isinstance(u, dict) and 'windows' not in u and unit_key(u) in seconds:
                u['seconds'] = math.ceil(seconds[unit_key(u)])
    return doc


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


def result_files(
    directory: str | pathlib.Path, prefix: str, stage: str = 'measure'
) -> list[pathlib.Path]:
    """The `*.json` results files of newest_shards."""
    dirs = newest_shards(directory, prefix, stage).values()
    return sorted(p for d in dirs for p in d.rglob('*.json'))


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
    """Every shard of shard_groups as one results file."""
    return merge([p for ps in shard_groups(directory, prefix, stage, since).values() for p in ps])


def merge(paths: list[pathlib.Path]) -> dict:
    """Results files as one: shards must share a runner and measure disjoint cells."""
    merged: dict[str, Any] = {'schema': 1, 'runner': None, 'cpu_models': [], 'cells': []}
    merged['seconds'] = []
    seen = set()
    for p in paths:
        doc = load(p)
        if merged['runner'] is None:
            merged['runner'] = doc.get('runner')
        elif doc.get('runner') != merged['runner']:
            raise ValueError(f'{p}: runner {doc.get("runner")!r} is not {merged["runner"]!r}')
        if doc.get('cpu_model') and doc['cpu_model'] not in merged['cpu_models']:
            merged['cpu_models'].append(doc['cpu_model'])
        for c in doc.get('cells', []):
            if key(c) in seen:
                raise ValueError(f'{p}: {key_text(key(c))} was measured by two shards')
            seen.add(key(c))
            merged['cells'].append(
                {**c, 'cpu_model': doc['cpu_model']} if doc.get('cpu_model') else c
            )
        merged['seconds'].extend(doc.get('seconds', []))
    return merged


def plan_shape(doc: Any) -> Any:
    """doc without the `seconds` and `shards` a calibration's check-in rewrites."""
    if isinstance(doc, dict):
        return {k: plan_shape(v) for k, v in doc.items() if k not in ('seconds', 'shards')}
    if isinstance(doc, list):
        return [plan_shape(v) for v in doc]
    return doc


class GitError(Exception):
    """git could not answer, which the provenance check reports instead of reading as a pass."""


class Git:
    """The history questions the provenance check asks of the checkout in the working
    directory."""

    def run(self, args: tuple, stdin: str | None, ok: tuple) -> tuple[int, str]:
        """git's exit status and stdout. Bytes go through surrogateescape, never text mode,
        whose newline translation drops a CR and whose strict decoding refuses non-UTF-8."""
        data = None if stdin is None else stdin.encode('utf-8', 'surrogateescape')
        done = subprocess.run(['git', *args], input=data, capture_output=True)
        if done.returncode not in ok:
            err = done.stderr.decode('utf-8', 'replace').strip()
            raise GitError(f'git {args[0]} exited {done.returncode}: {err}')
        return done.returncode, done.stdout.decode('utf-8', 'surrogateescape')

    def git(self, *args: str, stdin: str | None = None) -> str:
        return self.run(args, stdin, (0,))[1]

    def ok(self, *args: str) -> bool:
        """True on exit 0, False on 1, GitError on any other status: the convention of
        merge-base --is-ancestor and rev-parse --verify --quiet."""
        return self.run(args, None, (0, 1))[0] == 0

    def commit(self, sha: str) -> str:
        """sha, fetched from origin when the checkout lacks it: a squash merge leaves a pull
        request's own commits only under its refs/pull head, which a clone does not fetch."""
        if not SHA_RE.match(sha):
            raise GitError(f'commit {sha!r} is not a full lowercase SHA')
        if not self.ok('rev-parse', '--verify', '--quiet', f'{sha}^{{commit}}'):
            self.git('fetch', '--quiet', '--no-tags', 'origin', sha)
        return sha

    def is_ancestor(self, sha: str, of: str) -> bool:
        """True when sha is of or one of its ancestors."""
        return self.ok('merge-base', '--is-ancestor', sha, of)

    def show(self, sha: str, path: str) -> str:
        """The file at path, from the working directory, as commit sha's tree holds it."""
        return self.git('cat-file', 'blob', f'{sha}:./{os.path.relpath(path)}')

    def has(self, sha: str, path: str) -> bool:
        """Whether commit sha's tree holds path, from the working directory; GitError when
        git cannot read sha."""
        return bool(self.git('ls-tree', '--name-only', sha, '--', os.path.relpath(path)).strip())

    def added_by(self, path: str, head: str) -> str:
        """The newest first-parent commit of head that added path. A move adds its new
        path: rename detection, which `log.follow` turns on, would name the commit that
        added the old one."""
        args = ('log', '--no-follow', '--first-parent', '--diff-filter=A', '--format=%H')
        found = self.git(*args, '-1', head, '--', path).strip()
        if not found:
            raise GitError(f'no first-parent commit of {head} added {path}')
        return found

    def same_change(self, measured: str, added: str) -> bool:
        """Whether commit added makes, outside CHECK_IN_PATHS, the change measured makes
        from where it forked from added's parent, and both hold one plan but for its seconds
        and shards. patch-id --verbatim ignores hunk positions, so dev commits merged in
        between that only shift the changed lines pass; one that edits their three lines of
        context does not."""
        plans = [loads(self.git('show', f'{c}:{PLAN_PATH}')) for c in (measured, added)]
        if plan_shape(plans[0]) != plan_shape(plans[1]):
            return False
        fork = self.git('merge-base', measured, f'{added}^').strip()
        skip = [f':(top,exclude){p}' for p in CHECK_IN_PATHS]
        ids = []
        for old, new in ((fork, measured), (f'{added}^', added)):
            diff = self.git('diff', '--full-index', old, new, '--', *skip)
            ids.append(self.git('patch-id', '--verbatim', stdin=diff).split()[:1])
        return ids[0] == ids[1]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--check-file', metavar='BUDGETS', help='check the rule and provenance only')
    mode.add_argument('--verdict', metavar='DIR', help='judge the re-measured results in DIR')
    mode.add_argument(
        '--calibrate', metavar='DIR', help='the downloaded perf-results-<prefix>-measure-* folders'
    )
    mode.add_argument('--plan', metavar='PLAN', help='write the measure matrix of plan.json')
    mode.add_argument('--bench', metavar='DIR', help='write the chart points of the results in DIR')
    mode.add_argument(
        '--take',
        metavar='MEASURED',
        help='write the counts of a measured file under tests/perf/sources into --budgets, '
        'and delete the source files there no entry names',
    )
    mode.add_argument(
        '--check-in',
        metavar='CALIBRATION',
        help='check in a calibration of the default branch: --budgets, --plan-doc, '
        '--sources and --accepted are rewritten, and --pr-body says what moved',
    )
    p.add_argument('--set', help='the plan.json set to measure')
    p.add_argument(
        '--plan-doc',
        help='plan.json. The guard fails a baselined timing cell of a window --set and --only '
        'measure that no shard measured. --check-file fails a gated or exact entry neither '
        'the pr nor the full set lists, and --calibrate reads its `reported` patterns',
    )
    p.add_argument('--only', default='', help='comma or space separated scenarios to keep')
    p.add_argument('--runs', type=int, default=1, help='how many times to measure the set')
    p.add_argument(
        '--remeasure',
        help="compare's remeasure.json: --plan plans only the units of its cells, and "
        '--verdict judges each cell on the baseline it carries',
    )
    p.add_argument('--budgets', help='the head budgets.json')
    p.add_argument(
        '--base',
        help="the base ref's budgets.json: the guard's baselines, where a missing file "
        "fails, or --check-file's base, where it is an empty one",
    )
    p.add_argument('--results', help='the downloaded perf-results-<prefix>-measure-* folders')
    p.add_argument(
        '--prefix',
        help="perf-ci.yaml's artifact prefix: read only that call's shard directories",
    )
    p.add_argument('--sources', help='tests/perf/sources, the signed files entries come from')
    p.add_argument(
        '--accepted',
        help='tests/perf/accepted, the gated cells slower on purpose. Unset accepts none',
    )
    p.add_argument('--repo', help='owner/name of the measured repository')
    p.add_argument('--head', help='the commit the gate checks out: the merge ref of a pull request')
    p.add_argument(
        '--base-sha',
        help='the base branch commit: a changed entry takes only a calibration it holds',
    )
    p.add_argument('--remeasure-out', help='where compare writes the cells to re-measure')
    p.add_argument(
        '--remeasure-attempt',
        type=int,
        help='the run attempt of the compare whose remeasure.json --verdict judges',
    )
    p.add_argument('--summary', help='a file the Markdown summary is appended to')
    p.add_argument('--out', help='where --calibrate, --plan or --bench writes its file')
    p.add_argument(
        '--outputs', help='--plan: a $GITHUB_OUTPUT file to append matrix, node, scenarios to'
    )
    p.add_argument('--commit', help='the commit --calibrate measured')
    p.add_argument('--runner', help='the runner --calibrate measured on')
    p.add_argument('--run-id', help='the perf-calibrate run id')
    p.add_argument('--run-url', help='the perf-calibrate run URL')
    p.add_argument('--reason', help='--take: the one-line reason the counts moved')
    p.add_argument(
        '--cells',
        action='append',
        help='--take: a glob on the cell text (scenario/scale/engine/width/window/metric), '
        'repeatable, and every cell when unset',
    )
    p.add_argument('--pr-body', help='--check-in: where the pull request body is written')
    a = p.parse_args(argv)
    if a.calibrate:
        need = ('prefix', 'out', 'repo', 'commit', 'runner', 'run_id', 'run_url')
    elif a.plan:
        need = ('set', 'out')
    elif a.bench:
        need = ('prefix', 'out')
    elif a.check_file:
        need = ('base', 'sources', 'repo', 'head', 'base_sha')
    elif a.verdict:
        need = ('results', 'prefix', 'remeasure_attempt', 'remeasure')
    elif a.take:
        need = ('budgets', 'reason')
    elif a.check_in:
        need = ('budgets', 'plan_doc', 'sources', 'repo', 'head', 'pr_body')
    else:
        need = ('base', 'results', 'prefix', 'remeasure_out', 'plan_doc', 'set')
    missing = [f'--{n.replace("_", "-")}' for n in need if not getattr(a, n)]
    if missing:
        p.error(f'missing {", ".join(missing)}')
    return a


def load_base(path: str) -> dict:
    p = pathlib.Path(path)
    return load(p) if p.is_file() else {'budgets': []}


def held_reported(a: argparse.Namespace) -> list[dict]:
    """The `reported` patterns --plan-doc and its copy at --base-sha both list."""
    if not a.plan_doc:
        return []
    return held_patterns(load(a.plan_doc), base_plan(Git(), a.base_sha, a.plan_doc))


def file_errors(a: argparse.Namespace, head: dict, held: list[dict]) -> Checked:
    """check_provenance of head, its errors led by the rule and plan errors and its notices
    naming each gating entry a pattern only the head plan lists matches; held is
    held_reported's."""
    errors = check_rule(head)
    if check_shape(head):
        return Checked(errors)
    added = []
    if a.plan_doc:
        doc = load(a.plan_doc)
        errors += check_plan(head, doc, reported=held, base=load_base(a.base))
        added = [p for p in reported_patterns(doc)[0] if p not in held]
    found = check_provenance(
        head,
        load_base(a.base),
        sources=pathlib.Path(a.sources),
        accepted=pathlib.Path(a.accepted) if a.accepted else None,
        repo=a.repo,
        head_sha=a.head,
        base_sha=a.base_sha,
        verify=cosign_verify,
        git=Git(),
    )
    found.errors = errors + found.errors
    found.notices += [
        f'{key_text(key(e))}: plan.json adds a reported pattern that matches this '
        f'{e["tier"]} entry, so it stops gating once this pull request merges'
        for e in head.get('budgets', [])
        if e['tier'] != 'reported' and plan_reports(added, e)
    ]
    return found


def report(a: argparse.Namespace, text: str) -> None:
    print(text)
    if a.summary:
        with pathlib.Path(a.summary).open('a') as f:
            f.write(text)


def run_calibrate(a: argparse.Namespace) -> int:
    defaults = load(a.budgets) if a.budgets else {}
    groups = shard_groups(a.calibrate, a.prefix)
    if len(groups) != a.runs:
        raise ValueError(
            f'{a.calibrate} holds results of {len(groups)} runs, not the {a.runs} dispatched'
        )
    reported, errors = reported_patterns(load(a.plan_doc)) if a.plan_doc else ([], [])
    if errors:
        raise ValueError('; '.join(errors))
    runs = []
    for name, paths in sorted(groups.items()):
        run = merge(paths)
        if run['runner'] != a.runner:
            raise ValueError(
                f'{name or "the run"}: measured on {run["runner"]!r}, not {a.runner!r}'
            )
        runs.append(run)
    proposed = calibrate(
        runs,
        headroom=defaults.get('headroom', DEFAULT_HEADROOM),
        floor=defaults.get('floor', DEFAULT_FLOOR),
        repo=a.repo,
        commit=a.commit,
        runner=a.runner,
        run_id=a.run_id,
        run_url=a.run_url,
        reported=reported,
    )
    pathlib.Path(a.out).write_text(json.dumps(proposed, indent=2) + '\n')
    print(f'{a.out}: {len(proposed["cells"])} cells from {len(runs)} runs')
    return 0


def github_output(name: str, value: str) -> str:
    """One $GITHUB_OUTPUT value in the `name<<DELIMITER` form, whose random
    delimiter no value can end early."""
    delimiter = f'ghadelimiter_{uuid.uuid4()}'
    return f'{name}<<{delimiter}\n{value}\n{delimiter}\n'


def run_plan(a: argparse.Namespace) -> int:
    only = a.only.replace(',', ' ').split()
    remeasure = load(a.remeasure) if a.remeasure else None
    matrix = plan(load(a.plan), a.set, only=only, runs=a.runs, remeasure=remeasure)
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


def run_take(a: argparse.Namespace) -> int:
    path = pathlib.Path(a.take)
    doc, budgets = load(path), load(a.budgets)
    errors = check_shape(budgets)
    if errors:
        raise ValueError('; '.join(errors))
    taken = take(budgets, doc, reason=a.reason, cells=a.cells or ['*'])
    stem = parse_source(doc['run_url']).stem
    bundle = path.with_name(f'{stem}.sigstore.json')
    if path.name != f'{stem}.json' or not bundle.is_file():
        raise ValueError(f'{path} must be {stem}.json, beside its bundle {bundle.name}')
    pathlib.Path(a.budgets).write_text(dump_budgets(budgets))
    print(f'{a.budgets}: took {len(taken)} cells from {stem}')
    for name in unnamed_sources(path.parent, budgets):
        (path.parent / name).unlink()
        print(f'deleted {path.parent / name}: no entry names it')
    return 0


def run_check_in(a: argparse.Namespace) -> int:
    path = pathlib.Path(a.check_in)
    cal, budgets, sources = load(path), load(a.budgets), pathlib.Path(a.sources)
    errors = check_shape(budgets)
    if errors:
        raise ValueError('; '.join(errors))
    bundle = path.with_name(f'{path.stem}.sigstore.json')
    if path.name != f'{cal.get("run_id")}.json' or not bundle.is_file():
        raise ValueError(f'{path} must be <run id>.json, beside its bundle {bundle.name}')
    accepted = pathlib.Path(a.accepted) if a.accepted else None
    done = check_in(
        cal,
        budgets,
        load(a.plan_doc),
        sources=sources,
        accepted=accepted,
        repo=a.repo,
        head_sha=a.head,
        git=Git(),
    )
    errors = check_rule(done.budgets)
    if errors:
        raise ValueError('the checked-in budgets.json breaks the rule: ' + '; '.join(errors))
    sources.mkdir(parents=True, exist_ok=True)
    for p in (path, bundle):
        if p.resolve() != (sources / p.name).resolve():
            shutil.copyfile(p, sources / p.name)
    pathlib.Path(a.budgets).write_text(dump_budgets(done.budgets))
    pathlib.Path(a.plan_doc).write_text(json.dumps(done.plan, indent=2) + '\n')
    for name, doc in done.accepted.items():
        if doc is None:
            (accepted / name).unlink()
        else:
            (accepted / name).write_text(json.dumps(doc, indent=2) + '\n')
    for name in done.drop:
        (sources / name).unlink()
    pathlib.Path(a.pr_body).write_text(done.body)
    print(done.body)
    return 0


def run(a: argparse.Namespace) -> int:
    if a.calibrate:
        return run_calibrate(a)
    if a.take:
        return run_take(a)
    if a.check_in:
        return run_check_in(a)
    if a.plan:
        return run_plan(a)
    if a.bench:
        files = result_files(a.bench, a.prefix)
        points = bench(merge(files)) if files else []
        pathlib.Path(a.out).write_text(json.dumps(points, indent=2) + '\n')
        return 0
    if a.check_file:
        checked = file_errors(a, load(a.check_file), held_reported(a))
        for n in checked.notices:
            print(f'::notice::{n}')
        errors = checked.errors
        for e in errors:
            print(f'::error::{e}')
        return 1 if errors else 0
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
    """The guard's compare of --results against the baselines of --base, writing the red
    cells with their baselines to --remeasure-out; a base with no file writes nothing."""
    if not pathlib.Path(a.base).is_file():
        return Outcome(
            failures=['the base holds no tests/perf/budgets.json, so the guard has no baseline']
        )
    runner, baselines = baselines_from_budgets(load(a.base))
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
    except (OSError, ValueError, KeyError, TypeError, GitError) as exc:
        print(f'::error::perf_gate: {exc!r}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
