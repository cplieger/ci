#!/usr/bin/env python3
"""The tier 2 performance gate: a web app's tests/perf/budgets.json against
the results its harness measured, the provenance of every budget a pull
request changes, and the ten-run calibration that proposes the budgets.

A results file (`results.json`, one per measure shard) is
  {"schema": 1, "runner": str, "cpu_model": str,
   "cells": [{scenario, scale, engine, width, window, metric, kind, reps}],
   "seconds": [{scenario, scale, engine, width, seconds}]}
where a metric is `<family>[:<qualifier>]` (`reads:/api/chats`) and kind is
`time` (gated through its family's headroom), `server_cpu` (gated at its
target), `count` (exact) or `report` (never gated).
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import math
import pathlib
import re
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
ENTRY_FIELDS = (*KEY_FIELDS, *CHANGE_FIELDS, 'reason')
FILE_FIELDS = ('schema', 'runner', 'headroom', 'floor', 'target', 'budgets', 'retired')
SCHEMA = 2
SERVER_CPU = 'server_cpu_ms'
DEFAULT_HEADROOM = {'cpu_ms': 1.5, 'heap_mb': 1.25, 'rss_mb': 1.25}
DEFAULT_FLOOR = {'cpu_ms': 50, 'heap_mb': 1}
HEADROOM_STEP = 0.05
TIER_RUNS = 10
IDENTITY = 'https://github.com/cplieger/ci/.github/workflows/perf-calibrate.yaml@refs/heads/main'
ISSUER = 'https://token.actions.githubusercontent.com'
SOURCE_RE = re.compile(r'^https://github\.com/cplieger/ci/actions/runs/(\d+)$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
# What a calibration's check-in writes; the rest of the tree is the code it measured.
PLAN_PATH = 'tests/perf/plan.json'
CHECK_IN_PATHS = ('tests/perf/budgets.json', 'tests/perf/calibrations', PLAN_PATH)
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
    if e['tier'] == 'exact' and SOURCE_RE.match(str(e.get('source', ''))):
        return ['an exact entry from a calibration carries no reason']
    return []


# --- compare and verdict ---------------------------------------------------


@dataclasses.dataclass
class Outcome:
    failures: list[str] = dataclasses.field(default_factory=list)
    remeasure: list[dict] = dataclasses.field(default_factory=list)
    notices: list[str] = dataclasses.field(default_factory=list)
    reported: list[dict] = dataclasses.field(default_factory=list)
    over_target: list[dict] = dataclasses.field(default_factory=list)
    runner_mismatch: bool = False
    runner: str = ''
    # Every measured cell for the summary: {cell, entry, value, status}.
    rows: list[dict] = dataclasses.field(default_factory=list)
    # Each re-measured cell: {cell, reps, judged}, judged the values verdict
    # held against the budget.
    six: list[dict] = dataclasses.field(default_factory=list)


def middle(reps: list) -> list:
    """The middle rep, or both middle reps of an even count. An exact count is
    judged on these, never on their mean: statistics.median of [1, 1, 1, 3, 3, 3]
    is 2, a count no rep measured."""
    s = sorted(reps)
    return s[(len(s) - 1) // 2 : len(s) // 2 + 1]


def compare(budgets: dict, results: dict, units: dict | None = None) -> Outcome:
    """Judge each measured cell on its median of three. A gated cell over its
    budget and an exact count whose middle rep is off its budget are listed in
    `remeasure`, never failed here. An exact entry measured as no count fails,
    and so does a gated or exact entry no shard measured whose unit and window
    `units` (`planned_windows`) holds."""
    out = Outcome(runner=results.get('runner', ''))
    out.runner_mismatch = out.runner != budgets.get('runner')
    entries = {key(e): e for e in budgets.get('budgets', [])}
    for c in results.get('cells', []):
        e = entries.get(key(c))
        reps = c.get('reps') or []
        value = statistics.median(reps) if reps else None
        target = file_target(budgets, c['metric'])
        if is_number(target) and value is not None and value > target:
            out.over_target.append(c)
        if out.runner_mismatch or e is None or e['tier'] == 'reported':
            out.reported.append(c)
            status = 'reported'
        else:
            status = judge(budgets, e, c, value, out)
        out.rows.append({'cell': c, 'entry': e, 'value': value, 'status': status})
    if not out.runner_mismatch and units:
        measured = {key(c) for c in results.get('cells', [])}
        out.failures.extend(
            f'{key_text(k)}: a {e["tier"]} entry of the measured set that no shard measured'
            for k, e in entries.items()
            if e.get('tier') != 'reported' and planned(units, e) and k not in measured
        )
    return out


def judge(budgets: dict, e: dict, c: dict, value: float | None, out: Outcome) -> str:
    name = key_text(key(c))
    kind = c.get('kind')
    if value is None:
        out.failures.append(f'{name}: measured no reps')
        return 'failed'
    if e['tier'] == 'exact':
        if kind != 'count':
            out.failures.append(
                f'{name}: an exact entry needs a count, the harness measured {kind}'
            )
            return 'failed'
        if any(r != e['budget'] for r in middle(c['reps'])):
            out.remeasure.append(c)
            return 'remeasure'
        return 'ok'
    if kind not in ('time', 'server_cpu'):
        out.failures.append(f'{name}: a gated entry needs a time, the harness measured {kind}')
        return 'failed'
    if value > e['budget']:
        out.remeasure.append(c)
        return 'remeasure'
    h = entry_headroom(budgets, e)
    if kind == 'time' and is_number(h) and h > 0 and value < e['baseline'] / h:
        out.notices.append(
            f'{name}: median {value} is under baseline {e["baseline"]} / {h}, '
            'so its budget can come down'
        )
    return 'ok'


def verdict(budgets: dict, results: dict, remeasured: dict) -> Outcome:
    """Fail each cell compare sent to re-measure that its six reps, its first
    three and the three re-measured, still hold off its budget: a gated cell
    whose median is over it, an exact count whose middle reps are not it."""
    out = compare(budgets, results)
    entries = {key(e): e for e in budgets.get('budgets', [])}
    again = {key(c): c for c in remeasured.get('cells', [])}
    for c in out.remeasure:
        name = key_text(key(c))
        more = again.get(key(c))
        if more is None or not more.get('reps'):
            out.failures.append(f'{name}: listed for a re-measure but no reps came back')
            continue
        six = [*c['reps'], *more['reps']]
        budget = entries[key(c)]['budget']
        if entries[key(c)]['tier'] == 'exact':
            judged = middle(six)
            if any(r != budget for r in judged):
                out.failures.append(f'{name}: six reps {six} hold off its exact budget {budget}')
        else:
            judged = [statistics.median(six)]
            if judged[0] > budget:
                out.failures.append(
                    f'{name}: median of six {judged[0]} is over its budget {budget} ({six})'
                )
        out.six.append({'cell': c, 'reps': six, 'judged': judged})
    return out


def summary(budgets: dict, out: Outcome) -> str:
    lines = ['## Performance gate', '']
    if out.runner_mismatch:
        lines += [
            (
                f'The results come from runner `{out.runner}`, and `budgets.json` is calibrated '
                f'on `{budgets.get("runner")}`, so every metric is reported and none gates. '
                'Calibrate on this runner (`perf-calibrate.yaml`) to gate it.'
            ),
            '',
        ]
    for title, items in (('Failures', out.failures), ('Budgets that can come down', out.notices)):
        if items:
            lines += [f'### {title}', '', *(f'- {i}' for i in items), '']
    if out.six:
        lines += [
            '### Re-measured',
            '',
            "| Cell | Six reps | Median, or a count's middle reps |",
            '|---|---|---|',
        ]
        lines += [
            f'| {key_text(key(s["cell"]))} | {", ".join(str(r) for r in s["reps"])} '
            f'| {", ".join(str(r) for r in s["judged"])} |'
            for s in out.six
        ]
        lines.append('')
    over = {key(c) for c in out.over_target}
    lines += [
        '### Every measured cell',
        '',
        '| Cell | Tier | Median | Budget | Baseline | Target | Status |',
        '|---|---|---|---|---|---|---|',
    ]
    for r in out.rows:
        e = r['entry'] or {}
        target = file_target(budgets, r['cell']['metric'])
        target = '' if target is None else target
        flag = ' (over target)' if key(r['cell']) in over else ''
        lines.append(
            f'| {key_text(key(r["cell"]))} | {e.get("tier", "none")} | {r["value"]} '
            f'| {e.get("budget", "")} | {e.get("baseline", "")} | {target} '
            f'| {r["status"]}{flag} |'
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
) -> dict:
    """The proposed budget file of `runs` measurements of one commit. Only a
    file of at least ten runs carries a tier, a widened headroom and
    `whole_pr`; a smaller one re-measures baselines."""
    if not runs:
        raise ValueError('calibrate: no runs')
    n = len(runs)
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
        if n >= TIER_RUNS:
            complete = len(reps_by_run) == n and cell['kind'] != 'mixed'
            decide_tier(cell, reps_by_run, complete, headroom, floor, whole)
        cells.append(cell)
    doc = {
        'schema': 1,
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
    }
    if n >= TIER_RUNS:
        whole['first_stage_runs'] = sorted(whole['first_stage_runs'])
        doc['whole_pr'] = whole
    return doc


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


def plan_reports(patterns: list[dict], c: dict) -> bool:
    return any(
        p['metric'] == c['metric'] and p.get('window', c['window']) == c['window'] for p in patterns
    )


def check_plan(budgets: dict, doc: dict) -> list[str]:
    """A gated or exact entry whose unit and window no set of RUN_SETS lists
    leaves the gate unmeasured, so it fails unless `retired` names its key; one
    of a cell `reported` lists fails. A set or cell of any set that is not an
    object, and a `reported` item that is no pattern, is an error line."""
    sets = doc.get('sets', {})
    if not isinstance(sets, dict):
        return ['plan.json: sets must be an object of named sets']
    patterns, errors = reported_patterns(doc)
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
    retired = {key(r) for r in budgets.get('retired', []) if all(f in r for f in KEY_FIELDS)}
    gating = [
        e
        for e in budgets.get('budgets', [])
        if e.get('tier') != 'reported' and all(f in e for f in KEY_FIELDS)
    ]
    return (
        errors
        + [
            f'{key_text(key(e))}: a {e["tier"]} entry no set of plan.json measures '
            f'({", ".join(RUN_SETS)}), and retired does not name it'
            for e in gating
            if not planned(units, e) and key(e) not in retired
        ]
        + [
            f'{key_text(key(e))}: plan.json reports this cell, so its entry is reported, '
            f'not {e["tier"]}'
            for e in gating
            if plan_reports(patterns, e)
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
    seen = set()
    for u in units:
        if not isinstance(u, dict):
            raise TypeError(f'set {set_name}: a cell must be an object, not {u!r}')
        # Cell fields reach $GITHUB_OUTPUT and the job's shell.
        if has_line_break(u):
            raise ValueError(f'set {set_name}: a cell field holds a line break: {u!r}')
        k = tuple(u.get(f) for f in UNIT_FIELDS)
        if not all(isinstance(x, str) and x for x in k):
            raise ValueError(f'set {set_name}: a cell needs {", ".join(UNIT_FIELDS)}: {u}')
        if k in seen:
            raise ValueError(f'set {set_name}: {key_text(k)} is listed twice')
        seen.add(k)
        if not is_number(u.get('seconds')) or u['seconds'] <= 0:
            raise ValueError(f'set {set_name}: {key_text(k)} needs its seconds per cell')
        if windows_error(u):
            raise ValueError(f'set {set_name}: {key_text(k)}: {windows_error(u)}')
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
        wanted = {tuple(c[f] for f in UNIT_FIELDS) for c in remeasure.get('cells', [])}
        units = [u for u in units if tuple(u[f] for f in UNIT_FIELDS) in wanted]
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


def check_provenance(
    head: dict,
    base: dict,
    *,
    calibrations: pathlib.Path,
    repo: str,
    head_sha: str,
    verify: Callable[[pathlib.Path, pathlib.Path], str | None],
    git: Git,
) -> list[str]:
    """Every entry the head adds or whose baseline, budget, tier, headroom or
    source differs from the base must match a signed calibration file, every
    entry when the file's runner, headroom or floor differs; every entry comes
    from the newest checked-in calibration of its cell (superseded_entries); a
    gated or exact entry leaves only through `retired`; every calibration file
    is named by an entry."""
    errors = []
    base_entries = {key(e): e for e in base.get('budgets', [])}
    head_entries = {key(e): e for e in head.get('budgets', [])}
    # The file-level maps sit outside CHANGE_FIELDS, and each changes how every entry gates.
    moved = any(head.get(f) != base.get(f) for f in ('headroom', 'floor', 'target'))
    file_edit = moved or head.get('runner') != base.get('runner')
    files: dict[str, tuple[dict | None, str | None]] = {}
    named = set()
    unchanged = set()
    for k, e in head_entries.items():
        m = SOURCE_RE.match(str(e.get('source', '')))
        if m:
            named.add(m.group(1))
        b = base_entries.get(k)
        if b is not None and not file_edit and all(e.get(f) == b.get(f) for f in CHANGE_FIELDS):
            unchanged.add(k)
            continue
        name = key_text(k)
        if not m:
            errors.append(f'{name}: source {e.get("source")!r} is not a perf-calibrate run URL')
            continue
        run_id = m.group(1)
        if run_id not in files:
            files[run_id] = load_calibration(
                run_id, calibrations, repo, head.get('runner'), head_sha, verify, git
            )
            if files[run_id][1]:
                errors.append(files[run_id][1])
        doc = files[run_id][0]
        if doc is not None:
            errors.extend(f'{name}: {x}' for x in entry_provenance(head, e, b, doc, moved))
    retired = {key(r): r for r in head.get('retired', []) if all(f in r for f in KEY_FIELDS)}
    for k, b in base_entries.items():
        if k in head_entries or b.get('tier') == 'reported':
            continue
        if not str(retired.get(k, {}).get('reason', '')).strip():
            errors.append(f'{key_text(k)}: a {b.get("tier")} entry left without a retired reason')
    if calibrations.is_dir():
        for p in sorted(calibrations.iterdir()):
            stem = p.name.removesuffix('.sigstore.json').removesuffix('.json')
            if stem == p.name:
                errors.append(f'{p.name}: not a calibration file or bundle')
            elif stem not in named:
                errors.append(f'{p.name}: no budget entry names calibration run {stem}')
    errors += superseded_entries(
        head_entries, unchanged, calibrations, git, head_sha, {r for r, f in files.items() if f[1]}
    )
    return errors


def superseded_entries(
    entries: dict,
    unchanged: set,
    calibrations: pathlib.Path,
    git: Git,
    head_sha: str,
    failed: set,
) -> list[str]:
    """Why an entry is not from the newest checked-in calibration of its cell, and why a
    calibration cannot be placed in head's history; failed holds the run ids whose load
    error the caller reported. placed_commit decides each file's place. Run ids order
    dispatches, not commits, so only that place says which file is newer."""
    errors: list[str] = []
    placed: dict[str, tuple[str, set]] = {}
    reported = set(failed)
    if calibrations.is_dir():
        for p in sorted(calibrations.glob('*.json')):
            if p.stem.isdigit() and p.stem not in failed:
                found = place_calibration(p, git, head_sha)
                if isinstance(found, str):
                    errors.append(f'calibration {p.stem}: {found}')
                    reported.add(p.stem)
                else:
                    placed[p.stem] = found
    newer = functools.cache(lambda old, new: old != new and git.is_ancestor(old, new))
    for k, e in entries.items():
        m = SOURCE_RE.match(str(e.get('source', '')))
        if k in unchanged and not m:
            errors.append(
                f'{key_text(k)}: source {e.get("source")!r} is not a perf-calibrate run URL'
            )
        if not m or m.group(1) in reported:
            continue
        if m.group(1) not in placed:
            errors.append(f'{key_text(k)}: calibration {m.group(1)} is not checked in')
            continue
        mine, cells = placed[m.group(1)]
        if k in unchanged and k not in cells:
            errors.append(f'{key_text(k)}: calibration {m.group(1)} did not measure this cell')
        later = [r for r, (at, cs) in placed.items() if k in cs and newer(mine, at)]
        newest = [r for r in later if not any(newer(placed[r][0], placed[o][0]) for o in later)]
        if newest:
            r = newest[0]
            errors.append(
                f'{key_text(k)}: calibration {r} measured this cell at {placed[r][0]}, which '
                f"descends from source {m.group(1)}'s {mine}; take the entry from {r}"
            )
    return errors


def place_calibration(path: pathlib.Path, git: Git, head_sha: str) -> tuple[str, set] | str:
    """The commit in head's history whose code the calibration at path measured, and the
    cells it measured; or why it has no such commit."""
    try:
        doc = load(path)
    except (OSError, ValueError, TypeError) as exc:
        return str(exc)
    cells = doc.get('cells')
    if not isinstance(cells, list):
        return 'cells is not a list'
    keys = set()
    for i, c in enumerate(cells):
        if not isinstance(c, dict) or any(not isinstance(c.get(f), str) for f in KEY_FIELDS):
            return f'cells[{i}] does not name a cell by {", ".join(KEY_FIELDS)}'
        keys.add(key(c))
    at, why = placed_commit(doc, path, git, head_sha)
    return why if at is None else (at, keys)


def placed_commit(doc: dict, path: pathlib.Path, git: Git, head_sha: str) -> tuple:
    """(the commit in head's history whose code calibration doc at path measured, None), or
    (None, why it has none). That is its `commit` when the commit that added path already
    held it. Otherwise the calibration measured the pull request that added path, and the
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
    return None, (
        f'{added}, which added the file, makes another change than measured {measured} '
        'made; re-calibrate at the final commit'
    )


def load_calibration(run_id, calibrations, repo, runner, head_sha, verify, git):
    blob = calibrations / f'{run_id}.json'
    bundle = calibrations / f'{run_id}.sigstore.json'
    if not blob.is_file() or not bundle.is_file():
        return None, f'calibration {run_id}: {blob.name} and {bundle.name} must both be checked in'
    failure = verify(blob, bundle)
    if failure:
        return None, f'calibration {run_id}: the bundle does not verify: {failure}'
    try:
        doc = load(blob)
    except (ValueError, TypeError) as exc:
        return None, f'calibration {run_id}: {exc}'
    for field, want in (('run_id', run_id), ('repo', repo), ('runner', runner)):
        if str(doc.get(field)) != str(want):
            return None, f'calibration {run_id}: {field} is {doc.get(field)!r}, not {want!r}'
    _, why = placed_commit(doc, blob, git, head_sha)
    return (None, f'calibration {run_id}: {why}') if why else (doc, None)


def entry_provenance(head: dict, e: dict, b: dict | None, doc: dict, file_moved: bool) -> list[str]:
    """Why entry e of head does not match calibration doc. With file_moved
    (the file's headroom, floor or target differs from the base's), a budget
    above the base's needs a `reason` even when its baseline did not rise."""
    cells = {key(c): c for c in doc.get('cells', [])}
    c = cells.get(key(e))
    run_id = doc.get('run_id')
    if c is None:
        return [f'calibration {run_id} did not measure this cell']
    out = []
    if e.get('baseline') != c.get('baseline'):
        out.append(f"baseline {e.get('baseline')} is not calibration {run_id}'s {c['baseline']}")
    runs = doc.get('runs', 0)
    if runs >= TIER_RUNS:
        want_tier, want_h = c.get('tier'), c.get('headroom')
        decided = f'calibration {run_id}'
    elif b is not None:
        want_tier, want_h = b.get('tier'), b.get('headroom')
        decided = f'the base, since {runs} runs decide no tier'
    else:
        want_tier, want_h = 'reported', None
        decided = f'a new entry from {runs} runs'
    if (e.get('tier'), e.get('headroom')) != (want_tier, want_h):
        out.append(f'tier and headroom must be {want_tier}, {want_h} ({decided})')
    if runs < TIER_RUNS and e.get('tier') == 'gated' and c.get('kind') == 'time':
        h = entry_headroom(head, e)
        if is_number(h):
            _, second, _ = leave_one_out(c.get('reps_by_run', []), h, entry_floor(head, e))
            if second:
                out.append(
                    f'run(s) {second} of calibration {run_id} trip the second stage; '
                    f'calibrate {e["scenario"]} with ten runs'
                )
    if b is not None and is_number(b.get('baseline')) and is_number(e.get('baseline')):
        if e['baseline'] > b['baseline'] and not str(e.get('reason', '')).strip():
            out.append(f'raises its baseline from {b["baseline"]} without a reason')
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


def cosign_verify(blob, bundle, identity: str = IDENTITY) -> str | None:
    """None when the keyless bundle verifies for identity, else why not."""
    cmd = ['cosign', 'verify-blob', '--bundle', str(bundle)]
    cmd += ['--certificate-identity', identity, '--certificate-oidc-issuer', ISSUER, str(blob)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return 'cosign is not installed'
    except subprocess.TimeoutExpired:
        return 'cosign verify-blob timed out'
    if done.returncode == 0:
        return None
    return (done.stderr or done.stdout).strip() or f'cosign exited {done.returncode}'


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
            merged['cells'].append(c)
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
    p.add_argument('--set', help='the plan.json set to measure')
    p.add_argument(
        '--plan-doc',
        help='plan.json: fail a gated or exact entry neither the pr nor the full set lists, '
        'or that --set and --only list and no shard measured',
    )
    p.add_argument('--only', default='', help='comma or space separated scenarios to keep')
    p.add_argument('--runs', type=int, default=1, help='how many times to measure the set')
    p.add_argument('--remeasure', help="compare's remeasure.json: plan only its cells")
    p.add_argument('--budgets', help='the head budgets.json')
    p.add_argument(
        '--base', help="the base ref's budgets.json, where a missing file is an empty one"
    )
    p.add_argument('--results', help='the downloaded perf-results-<prefix>-measure-* folders')
    p.add_argument(
        '--prefix',
        help="perf-ci.yaml's artifact prefix: read only that call's shard directories",
    )
    p.add_argument('--calibrations', help='tests/perf/calibrations')
    p.add_argument('--repo', help='owner/name of the measured repository')
    p.add_argument('--head', help='the pull request head commit')
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
    a = p.parse_args(argv)
    if a.calibrate:
        need = ('prefix', 'out', 'repo', 'commit', 'runner', 'run_id', 'run_url')
    elif a.plan:
        need = ('set', 'out')
    elif a.bench:
        need = ('prefix', 'out')
    elif a.check_file:
        need = ('base', 'calibrations', 'repo', 'head')
    elif a.verdict:
        need = ('budgets', 'results', 'prefix', 'remeasure_attempt')
    else:
        need = ('budgets', 'base', 'results', 'prefix', 'calibrations', 'repo', 'head')
        need += ('remeasure_out',)
        need += ('plan_doc', 'set')
    missing = [f'--{n.replace("_", "-")}' for n in need if not getattr(a, n)]
    if missing:
        p.error(f'missing {", ".join(missing)}')
    return a


def load_base(path: str) -> dict:
    p = pathlib.Path(path)
    return load(p) if p.is_file() else {'budgets': []}


def file_errors(a: argparse.Namespace, head: dict) -> list[str]:
    errors = check_rule(head)
    if check_shape(head):
        return errors
    if a.plan_doc:
        errors += check_plan(head, load(a.plan_doc))
    errors += check_provenance(
        head,
        load_base(a.base),
        calibrations=pathlib.Path(a.calibrations),
        repo=a.repo,
        head_sha=a.head,
        verify=cosign_verify,
        git=Git(),
    )
    return errors


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


def run(a: argparse.Namespace) -> int:
    if a.calibrate:
        return run_calibrate(a)
    if a.plan:
        return run_plan(a)
    if a.bench:
        files = result_files(a.bench, a.prefix)
        points = bench(merge(files)) if files else []
        pathlib.Path(a.out).write_text(json.dumps(points, indent=2) + '\n')
        return 0
    if a.check_file:
        errors = file_errors(a, load(a.check_file))
        for e in errors:
            print(f'::error::{e}')
        return 1 if errors else 0
    budgets = load(a.budgets)
    if check_shape(budgets):
        out = Outcome(failures=check_rule(budgets))
    elif a.verdict:
        results = merge_results(a.results, a.prefix)
        again = merge_results(a.verdict, a.prefix, 'remeasure', since=a.remeasure_attempt)
        out = verdict(budgets, results, again)
    else:
        errors = file_errors(a, budgets)
        only = a.only.replace(',', ' ').split()
        matrix = plan(load(a.plan_doc), a.set, only=only)
        units = planned_windows(c for job in matrix['include'] for c in job['cells'])
        out = compare(budgets, merge_results(a.results, a.prefix), units)
        out.failures = errors + out.failures
        scenarios = sorted({c['scenario'] for c in out.remeasure})
        doc = {'cells': out.remeasure, 'scenarios': scenarios}
        pathlib.Path(a.remeasure_out).write_text(json.dumps(doc, indent=2) + '\n')
    report(a, summary(budgets, out))
    for f in out.failures:
        print(f'::error::{f}')
    return 1 if out.failures else 0


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        return run(a)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'::error::perf_gate: {exc!r}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
