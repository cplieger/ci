#!/usr/bin/env python3
"""The performance trend of a web app: each point of the full perf set measured on its
default branch, stored on the measured repository's perf-trend branch, judged against
each cell's trailing baseline, and one issue per regressed scenario opened and closed.

The perf-trend branch holds, per series (`dev` for the default branch):
  series/<series>/points/<YYYYMMDDTHHMMSSZ>-<sha12>.json   one point (`point`)
  series/<series>/state.json                               each scenario's lifecycle
  series/dev/baseline.json                                 the pull request guard's baselines
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import statistics
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import ghrest
import perf_gate

SCHEMA = 1
STAMP = '%Y%m%dT%H%M%SZ'
DATE = '%Y-%m-%dT%H:%M:%SZ'
DEV = 'dev'
SERIES_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,30}$')
MARKER_RE = re.compile(r'<!-- perf-trend:([a-z0-9-]+):([^\s<>]+) -->')
LABEL = 'performance'
LABEL_COLOR = 'd93f0b'
LABEL_DESCRIPTION = 'A performance regression the scheduled trend found'
# A cell's trend baseline is the median of its last BASELINE_POINTS good points. The
# record job checks out the last HISTORY points, so a streak up to HISTORY -
# BASELINE_POINTS long still finds its baseline among them; past that the newest
# baseline a point carries is carried on.
BASELINE_POINTS = 7
HISTORY = 21
# The pull request guard's baseline: the median of the last GUARD_POINTS dev points. Over
# five ten-run calibrations a three-point median failed one pr-set cell's median of six in
# 54,600 one-job samples and five points none; a deliberate change reaches it in three.
GUARD_POINTS = 5
# Scenario group rule, family to the median ratio over a scenario's cells whose
# baseline clears the family's guard floor. Over 900 one-run group samples of four
# calibrations the worst median ratio was 1.294 for cpu_ms, 1.022 for heap_mb and
# 1.018 for rss_mb; no scenario has three server routes over the 10 ms floor.
GROUP = {'cpu_ms': 1.3, 'heap_mb': 1.1, 'rss_mb': 1.1}
GROUP_CELLS = 3
BAD_RUNS_SHOWN = 20


# --- points ----------------------------------------------------------------


def point(
    groups: dict[str, list[pathlib.Path]],
    *,
    repo: str,
    series: str,
    ref: str,
    commit: str,
    date: str,
    run_id: str,
    run_url: str,
) -> dict:
    """One point of shard_groups' runs. A timing or report cell's value is the median of
    its runs' medians, and a count's the lower middle of every rep, a count some rep
    measured. A timing cell keeps each run's reps and CPU model, in run order. A cell
    with no reps has value None."""
    runs = [perf_gate.merge(paths) for _, paths in sorted(groups.items())]
    runners = {r['runner'] for r in runs}
    if len(runners) != 1:
        raise ValueError(f'the runs of one point share a runner, not {sorted(map(str, runners))}')
    found: dict[tuple, dict] = {}
    for run in runs:
        for c in run['cells']:
            k = perf_gate.key(c)
            seen = found.setdefault(k, {'kind': c.get('kind'), 'reps': [], 'cpu_models': []})
            if seen['kind'] != c.get('kind'):
                raise ValueError(
                    f'{perf_gate.key_text(k)}: measured as kind {seen["kind"]} and {c.get("kind")}'
                )
            seen['reps'].append(list(c.get('reps') or []))
            seen['cpu_models'].append(c.get('cpu_model', ''))
    cells = []
    for k in sorted(found):
        seen = found[k]
        cell = {**perf_gate.key_dict(k), 'kind': seen['kind'], 'value': cell_value(seen)}
        if seen['kind'] in perf_gate.TIMING_KINDS:
            cell['reps'] = seen['reps']
            cell['cpu_models'] = seen['cpu_models']
        cells.append(cell)
    return {
        'schema': SCHEMA,
        'repo': repo,
        'series': series,
        'ref': ref,
        'commit': commit,
        'date': date,
        'runner': runners.pop(),
        'runs': len(runs),
        'run_id': run_id,
        'run_url': run_url,
        'cells': cells,
    }


def cell_value(seen: dict) -> Any:
    if seen['kind'] == 'count':
        every = [v for reps in seen['reps'] for v in reps]
        return statistics.median_low(every) if every else None
    medians = [statistics.median(reps) for reps in seen['reps'] if reps]
    return statistics.median(medians) if medians else None


def parse_date(text: str) -> datetime:
    return datetime.strptime(text, DATE).replace(tzinfo=UTC)


def point_name(p: dict) -> str:
    return f'{parse_date(p["date"]).strftime(STAMP)}-{p["commit"][:12]}.json'


def dumps(doc: dict) -> str:
    """A point as JSON with one cell a line, so a diff of two points reads; any other
    document indented."""
    if 'cells' not in doc:
        return json.dumps(doc, indent=1, sort_keys=True) + '\n'
    head = json.dumps({k: v for k, v in doc.items() if k != 'cells'}, indent=1)
    lines = [json.dumps(c, separators=(',', ':')) for c in doc['cells']]
    return head[:-2] + ',\n "cells": [\n' + ',\n'.join(lines) + '\n ]\n}\n'


def seen(p: dict) -> dict:
    """What the judge reads of an earlier, judged point."""
    return {
        'commit': p['commit'],
        'date': p['date'],
        'runner': p['runner'],
        'run_url': p['run_url'],
        'cells': {perf_gate.key(c): c for c in p['cells']},
    }


# --- the judge -------------------------------------------------------------


@dataclasses.dataclass
class Judged:
    state: dict
    point: dict
    actions: list[dict]
    summary: str


def new_state(series: str) -> dict:
    return {'schema': SCHEMA, 'series': series, 'last': None, 'scenarios': {}}


def marker(series: str, scenario: str) -> str:
    return f'perf-trend:{series}:{scenario}'


def title(series: str, scenario: str) -> str:
    suffix = '' if series == DEV else f' ({series} series)'
    return f'Performance regression in {scenario}{suffix}'


def ref_of(p: dict) -> dict:
    return {'commit': p['commit'], 'date': p['date'], 'run_url': p['run_url']}


def baseline(history: list[dict], k: tuple, runner: str, accepted: dict | None) -> float | None:
    """The median of cell k's last BASELINE_POINTS values on runner that were not bad,
    or else the baseline the newest such point carried. Under an accepted regression
    the points before its first bad point are no baseline, and those up to its
    acceptance count however they were judged."""
    good, carried = [], None
    for h in reversed(history):
        c = h['cells'].get(k)
        if h['runner'] != runner or c is None or c.get('value') is None:
            continue
        if accepted and h['date'] < accepted['from']:
            break
        if carried is None and perf_gate.is_number(c.get('baseline')):
            carried = c['baseline']
        forgiven = bool(accepted) and h['date'] <= accepted['at']
        if c.get('bad') and not forgiven:
            continue
        good.append(c['value'])
        if len(good) == BASELINE_POINTS:
            break
    return statistics.median(good) if good else carried


def judge(
    history: list[dict],
    p: dict,
    state: dict,
    *,
    repo: str,
    fallback: list[dict] = (),
) -> Judged:
    """Judge point p against history (seen points of its series, oldest first; fallback's
    dev points go before them while the series has fewer than BASELINE_POINTS) and step
    each measured scenario's lifecycle in state. Returns the new state, p with each
    judged timing cell's `baseline` and `bad`, and the issue actions."""
    series = state['series']
    past = [*(fallback if len(history) < BASELINE_POINTS else ()), *history]
    tracked = state['scenarios']
    cells = [dict(c) for c in p['cells']]
    for c in cells:
        if not perf_gate.is_timing(c) or c.get('value') is None:
            continue
        accepted = tracked.get(c['scenario'], {}).get('accepted')
        b = baseline(past, perf_gate.key(c), p['runner'], accepted)
        if b is not None:
            c['baseline'] = b
            c['bad'] = perf_gate.over_guard(c['metric'], c['value'], b)
    scenarios = sorted({c['scenario'] for c in cells})
    bad: dict[str, set[str]] = {}
    groups: dict[str, list[str]] = {}
    for s in scenarios:
        mine = [c for c in cells if c['scenario'] == s and 'baseline' in c]
        bad[s] = {perf_gate.key_text(perf_gate.key(c)) for c in mine if c['bad']}
        groups[s] = []
        for fam, threshold in GROUP.items():
            floor = perf_gate.GUARD[fam][1]
            members = [
                c for c in mine if perf_gate.family(c['metric']) == fam and c['baseline'] >= floor
            ]
            if len(members) < GROUP_CELLS:
                continue
            ratio = statistics.median(c['value'] / c['baseline'] for c in members)
            if ratio > threshold:
                groups[s].append(
                    f'{fam} of {len(members)} cells over {floor:g} {unit_of(fam)}: median '
                    f'{ratio:.2f}x its baseline, past {threshold}x'
                )
                bad[s].add(f'group:{fam}')
                for c in members:
                    c['bad'] = True
    judged = {**p, 'cells': cells}
    previous = past[-1] if past else None
    new = {**state, 'scenarios': {k: dict(v) for k, v in tracked.items()}}
    new['last'] = {**ref_of(p), 'runner': p['runner']}
    actions = []
    unjudged: dict[str, str] = {}
    for s in scenarios:
        before = new['scenarios'].get(s, {'status': 'ok'})
        # A point that judged none of a scenario's cells, or one of another runner than
        # its regression's, says nothing about it: stepping on it would close an issue
        # against values no baseline was taken for.
        if before['status'] != 'ok' and before['runner'] != p['runner']:
            unjudged[s] = f'its regression was found on `{before["runner"]}`'
            continue
        if not any(c['scenario'] == s and 'baseline' in c for c in cells):
            if before['status'] != 'ok':
                unjudged[s] = 'no timing cell has a baseline on this runner'
            continue
        after, op = step(before, bad[s], p, previous)
        if op is not None:
            action = {'op': op[0], 'marker': marker(series, s), 'title': title(series, s)}
            action['labels'] = [LABEL]
            rows = [c for c in cells if c['scenario'] == s and c.get('bad')]
            if op[0] == 'upsert':
                counts = counts_since(past, after, judged, s)
                action['body'] = render_issue(
                    after, s, rows, groups[s], counts, series=series, repo=repo
                )
                after['body'] = action['body']
            if op[1] == 'still':
                action['comment'] = still_text(p, rows, groups[s])
            elif op[1]:
                action['comment'] = op[1]
            actions.append(action)
        if after == {'status': 'ok'}:
            new['scenarios'].pop(s, None)
        else:
            new['scenarios'][s] = after
    summary = render_summary(judged, new, bad, previous, actions, unjudged)
    return Judged(new, judged, actions, summary)


def step(
    before: dict, bad: set[str], p: dict, previous: dict | None
) -> tuple[dict, tuple[str, str | None] | None]:
    """One scenario's lifecycle step on point p, given its bad cells and groups: ok to
    suspect on a bad point; suspect to open (an upsert) when a cell or group of the
    suspicion is bad again, else a new suspicion; open and recovering to open on a bad
    point (an upsert with a comment); open to recovering, and recovering to ok (a
    close), on a good one. A tracked entry holds the runner it was found on. Returns
    the new entry and the (op, comment) to send, comment 'still' for judge's line
    naming the bad cells."""
    status = before['status']
    accepted = {'accepted': before['accepted']} if 'accepted' in before else {}
    if not bad:
        if status == 'open':
            return {**before, 'status': 'recovering'}, None
        if status == 'recovering':
            comment = (
                f'Within the thresholds again at {commit_link(p)} and at the point before it, '
                'so this regression is closed.'
            )
            return {'status': 'ok', **accepted}, ('close', comment)
        return {'status': 'ok', **accepted}, None
    confirms = status == 'suspect' and bool(bad & set(before['items']))
    if status in ('ok', 'suspect') and not confirms:
        return {
            'status': 'suspect',
            **accepted,
            'runner': p['runner'],
            'items': sorted(bad),
            'last_good': ref_of(previous) if previous else None,
            'first_bad': ref_of(p),
            'bad_runs': [p['run_url']],
        }, None
    after = {**before, 'status': 'open', 'items': sorted(bad)}
    after['bad_runs'] = [*before['bad_runs'], p['run_url']]
    if status == 'suspect':
        after['opened'] = p['date']
        return after, ('upsert', None)
    return after, ('upsert', 'still')


def still_text(p: dict, rows: list[dict], groups: list[str]) -> str:
    named = [
        f'{perf_gate.key_text(perf_gate.key(c))} {num(c["value"])} against '
        f'{num(c["baseline"])} ({ratio(c)})'
        for c in rows
    ]
    return f'Still over the thresholds at {commit_link(p)}: {"; ".join([*groups, *named])}.'


def commit_link(p: dict) -> str:
    return f'`{p["commit"][:12]}` ({p["run_url"]})'


def unit_of(fam: str) -> str:
    return 'MB' if fam.endswith('_mb') else 'ms'


def num(v: float) -> str:
    r = round(v, 1)
    return str(int(r)) if r == int(r) else str(r)


def ratio(c: dict) -> str:
    return f'{c["value"] / c["baseline"]:.2f}x' if c['baseline'] > 0 else ''


def counts_since(past: list[dict], tracked: dict, p: dict, scenario: str) -> list[str]:
    """The scenario's counts that differ between the regression's last good point and p."""
    good = tracked.get('last_good')
    then = next((h for h in reversed(past) if good and h['commit'] == good['commit']), None)
    if then is None:
        return []
    out = []
    for c in p['cells']:
        was = then['cells'].get(perf_gate.key(c))
        if (
            c['scenario'] == scenario
            and c['kind'] == 'count'
            and was
            and was['value'] != c['value']
        ):
            out.append(f'`{perf_gate.key_text(perf_gate.key(c))}` {was["value"]} to {c["value"]}')
    return out


def render_issue(
    tracked: dict,
    scenario: str,
    rows: list[dict],
    groups: list[str],
    counts: list[str],
    *,
    series: str,
    repo: str,
) -> str:
    where = '`dev`' if series == DEV else f'the `{series}` series'
    lines = [
        f'<!-- {marker(series, scenario)} -->',
        (
            f'The scheduled performance trend of {where} measured **{scenario}** past its '
            'thresholds at two consecutive points.'
        ),
        '',
    ]
    if groups:
        lines += [*(f'- Scenario group: {g}.' for g in groups), '']
    lines += perf_gate.table(
        None,
        ('Cell', 'Before', 'After', 'Ratio'),
        (
            (perf_gate.key_text(perf_gate.key(c)), num(c['baseline']), num(c['value']), ratio(c))
            for c in rows
        ),
    )
    good, first = tracked.get('last_good'), tracked['first_bad']
    if good:
        lines.append(
            f'Commits from the last good point `{good["commit"][:12]}` to the first bad point '
            f'`{first["commit"][:12]}`: '
            f'https://github.com/{repo}/compare/{good["commit"]}...{first["commit"]}'
        )
    else:
        lines.append(f'First bad point: `{first["commit"][:12]}`, the first point measured.')
    lines += ['', 'Runs of the bad points:', '']
    lines += [f'- {u}' for u in tracked['bad_runs'][-BAD_RUNS_SHOWN:]]
    lines.append('')
    if counts:
        lines += [f'Counts that changed: {", ".join(counts)}.', '']
    rules = ', '.join(
        f'{fam} over {r}x and more than {floor:g} {unit_of(fam)} over'
        for fam, (r, floor) in perf_gate.GUARD.items()
    )
    lines.append(
        f"Before is the cell's baseline, the median of up to {BASELINE_POINTS} earlier good "
        f'points; After is the latest point. A cell is bad at {rules}. This issue closes '
        'itself after two consecutive good points; closing it by hand accepts the new '
        'values as the baseline.'
    )
    return '\n'.join(lines) + '\n'


def render_summary(
    p: dict,
    state: dict,
    bad: dict[str, set[str]],
    previous: dict | None,
    actions: list[dict],
    unjudged: dict[str, str],
) -> str:
    judged = sum(1 for c in p['cells'] if 'baseline' in c)
    runs = '1 run' if p['runs'] == 1 else f'up to {p["runs"]} runs a cell'
    lines = [
        '## Performance trend',
        '',
        (
            f'Point `{p["commit"][:12]}` of the `{p["series"]}` series: {runs} on '
            f'`{p["runner"]}`, {len(p["cells"])} cells, {judged} timing cells judged against '
            'their trailing baseline.'
        ),
        '',
    ]
    lines += perf_gate.table(
        'Scenarios not ok',
        ('Scenario', 'Status', 'Bad at this point'),
        (
            (s, v['status'], ', '.join(sorted(bad.get(s, ()))))
            for s, v in sorted(state['scenarios'].items())
            if v['status'] != 'ok'
        ),
    )
    lines += perf_gate.table(
        'Scenarios not judged at this point',
        ('Scenario', 'Status', 'Why'),
        ((s, state['scenarios'][s]['status'], why) for s, why in sorted(unjudged.items())),
    )
    lines += perf_gate.table(
        'Bad cells',
        ('Cell', 'Baseline', 'Value', 'Ratio'),
        (
            (perf_gate.key_text(perf_gate.key(c)), num(c['baseline']), num(c['value']), ratio(c))
            for c in p['cells']
            if c.get('bad')
        ),
    )
    lines += perf_gate.table(
        'Issue actions', ('Action', 'Issue'), ((a['op'], a['title']) for a in actions)
    )
    changed = []
    for c in p['cells'] if previous else ():
        was = previous['cells'].get(perf_gate.key(c))
        if c['kind'] == 'count' and was and was['value'] != c['value']:
            changed.append((perf_gate.key_text(perf_gate.key(c)), was['value'], c['value']))
    if changed:
        lines += perf_gate.table(
            'Counts changed since the previous point (information)',
            ('Cell', 'Before', 'After'),
            changed,
        )
    elif previous:
        lines += ['No count changed since the previous point.', '']
    return '\n'.join(lines) + '\n'


def observe(state: dict, issues: list[dict], *, series: str) -> tuple[dict, list[dict]]:
    """Fold the repository's `performance` issues into state: a tracked regression whose
    every issue since it opened a person closed is accepted (status ok, its cells'
    baselines restart from its first bad point); one with no issue since it opened is
    sent the body it was last sent again, which repairs a run that stored its point and
    lost its issue writes; and an open issue of this series whose scenario tracks no
    regression is closed."""
    new = {**state, 'scenarios': {k: dict(v) for k, v in state['scenarios'].items()}}
    actions = []
    for s, v in sorted(state['scenarios'].items()):
        if v['status'] not in ('open', 'recovering'):
            continue
        mine = [
            i
            for i in issues
            if marked(i.get('body')) == (series, s) and i.get('created_at', '') >= v['opened']
        ]
        if mine and all(i.get('state') == 'closed' for i in mine):
            at = state['last']['date'] if state.get('last') else v['first_bad']['date']
            new['scenarios'][s] = {
                'status': 'ok',
                'accepted': {'from': v['first_bad']['date'], 'at': at},
            }
        elif not mine:
            actions.append(
                {
                    'op': 'upsert',
                    'marker': marker(series, s),
                    'title': title(series, s),
                    'labels': [LABEL],
                    'body': v['body'],
                }
            )
    for s in sorted(open_markers(issues, series)):
        if new['scenarios'].get(s, {}).get('status', 'ok') in ('ok', 'suspect'):
            actions.append(
                {
                    'op': 'close',
                    'marker': marker(series, s),
                    'title': title(series, s),
                    'labels': [LABEL],
                    'comment': 'The trend tracks no regression of this scenario, so this '
                    'issue is closed.',
                }
            )
    return new, actions


def first_line(body: str | None) -> str:
    return (body or '').split('\n', 1)[0].rstrip('\r')


def marked(body: str | None) -> tuple[str, str] | None:
    """The (series, scenario) of the marker an issue body opens with, the whole first
    line, or None."""
    found = MARKER_RE.fullmatch(first_line(body))
    return (found[1], found[2]) if found else None


def open_markers(issues: list[dict], series: str) -> set[str]:
    out = set()
    for i in issues:
        found = marked(i.get('body'))
        if i.get('state') == 'open' and found and found[0] == series:
            out.add(found[1])
    return out


def skip(state: dict, commit: str, issues: list[dict]) -> bool:
    """Whether a point of commit adds nothing: the series' last point measured it, no
    scenario is suspect, waiting on a confirming point, or recovering, waiting on its
    second good one, and observe has no issue write to repair."""
    last = state.get('last')
    pending = any(v['status'] in ('suspect', 'recovering') for v in state['scenarios'].values())
    repair = bool(observe(state, issues, series=state['series'])[1])
    return bool(last) and last['commit'] == commit and not pending and not repair


def guard_baseline(points: list[dict]) -> dict:
    """The pull request guard's baselines from dev's points, oldest first: each timing
    cell's median over the last GUARD_POINTS points on the newest point's runner, and
    each count as the newest of them measured it, which the guard only reports."""
    runner = points[-1]['runner']
    last = [p for p in points if p['runner'] == runner][-GUARD_POINTS:]
    values: dict[tuple, tuple[str, list]] = {}
    for p in last:
        for c in p['cells']:
            counted = c['kind'] == 'count' and c.get('value') is not None
            if counted or (perf_gate.is_timing(c) and perf_gate.is_number(c.get('value'))):
                values.setdefault(perf_gate.key(c), (c['kind'], []))[1].append(c['value'])
    cells = []
    for k, (kind, v) in sorted(values.items()):
        value = v[-1] if kind == 'count' else statistics.median(v)
        cells.append({**perf_gate.key_dict(k), 'kind': kind, 'baseline': value})
    return {
        'schema': SCHEMA,
        'series': DEV,
        'runner': runner,
        'commits': [p['commit'] for p in last],
        'cells': cells,
    }


# --- the perf-trend branch -------------------------------------------------


def series_dir(root: pathlib.Path, series: str) -> pathlib.Path:
    return root / 'series' / series


def load_points(root: pathlib.Path, series: str) -> list[dict]:
    """The points of a series the checkout holds, oldest first."""
    d = series_dir(root, series) / 'points'
    return [perf_gate.load(f) for f in sorted(d.glob('*.json'))] if d.is_dir() else []


def needs(listing: list[str], series: str) -> list[str]:
    """The paths of the perf-trend branch (listing, `git ls-tree -r --name-only`) the
    record job checks out to judge a point of series: its state, its last HISTORY points
    and, while it has fewer than BASELINE_POINTS, the last HISTORY dev points at or
    before its first one."""

    def points_of(s: str) -> list[str]:
        prefix = f'series/{s}/points/'
        return sorted(p for p in listing if p.startswith(prefix) and p.endswith('.json'))

    own = points_of(series)
    out = [f'series/{series}/state.json', *own[-HISTORY:]]
    if series != DEV and len(own) < BASELINE_POINTS:
        first = pathlib.PurePosixPath(own[0]).name if own else None
        dev = [p for p in points_of(DEV) if first is None or pathlib.PurePosixPath(p).name <= first]
        out += dev[-HISTORY:]
    return out


def record(
    root: pathlib.Path, p: dict, *, repo: str, issues: list[dict] | None
) -> tuple[list[dict], str]:
    """Judge point p against the checkout at root and write it there: the judged point,
    the series' state and, for dev, the guard's baseline.json. Returns the issue actions
    and the run summary."""
    series = p['series']
    if not SERIES_RE.match(series):
        raise ValueError(f'series {series!r} must match {SERIES_RE.pattern}')
    d = series_dir(root, series)
    target = d / 'points' / point_name(p)
    state_file = d / 'state.json'
    state = perf_gate.load(state_file) if state_file.is_file() else new_state(series)
    if target.exists():
        if perf_gate.load(target).get('run_id') != p['run_id']:
            raise ValueError(f'{target.name} is already recorded')
        # A re-run of the run that stored it: the branch holds its point and state, and
        # only the issue writes may be missing.
        actions = observe(state, issues, series=series)[1] if issues is not None else []
        return actions, f'{target.name} is already recorded by this run, so nothing is written.\n'
    actions: list[dict] = []
    if issues is not None:
        state, actions = observe(state, issues, series=series)
    history = load_points(root, series)
    fallback = []
    if series != DEV:
        first = min([h['date'] for h in history] + [p['date']])
        fallback = [h for h in load_points(root, DEV) if h['date'] <= first]
    judged = judge(
        [seen(h) for h in history],
        p,
        state,
        repo=repo,
        fallback=[seen(h) for h in fallback],
    )
    stepped = {a['marker'] for a in judged.actions}
    actions = [a for a in actions if a['marker'] not in stepped] + judged.actions
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(judged.point))
    state_file.write_text(dumps(judged.state))
    if series == DEV:
        (d / 'baseline.json').write_text(dumps(guard_baseline([*history, judged.point])))
    return actions, judged.summary


# --- issues ----------------------------------------------------------------


def fetch_issues(repo: str, client: ghrest.Client | None = None) -> list[dict]:
    """Every issue of repo labelled `performance`, open or closed, as observe reads them."""
    client = client or ghrest.DEFAULT
    path = f'repos/{repo}/issues?state=all&labels={quote(LABEL, safe="")}'
    return [
        {k: i.get(k) for k in ('number', 'state', 'created_at', 'body')}
        for i in client.pages(path)
        if 'pull_request' not in i
    ]


def apply(actions: list[dict], repo: str, client: ghrest.Client | None = None) -> list[str]:
    """Send the judge's actions as issue writes: an upsert edits the oldest open
    `performance` issue holding its marker (closing any newer one as its duplicate) or
    creates it, assigned to the repository owner; a close comments on and closes each
    open one. Returns a line per write."""
    client = client or ghrest.DEFAULT
    issues = client.pages(f'repos/{repo}/issues?state=open&labels={quote(LABEL, safe="")}')
    found = [i for i in issues if 'pull_request' not in i]
    done = []
    for a in actions:
        tag = f'<!-- {a["marker"]} -->'
        mine = sorted(
            (i for i in found if first_line(i.get('body')) == tag), key=lambda i: i['number']
        )
        if a['op'] == 'close':
            for i in mine:
                close(client, repo, i['number'], a['comment'], 'completed')
                found.remove(i)
                done.append(f'closed #{i["number"]}: {a["title"]}')
            continue
        if not mine:
            ensure_label(client, repo)
            owner = repo.split('/', 1)[0]
            created = client.send(
                'POST',
                f'repos/{repo}/issues',
                {
                    'title': a['title'],
                    'body': a['body'],
                    'labels': a['labels'],
                    'assignees': [owner],
                },
            )
            found.append(created)
            done.append(f'opened #{created["number"]}: {a["title"]}')
            continue
        keep, *dupes = mine
        for i in dupes:
            close(client, repo, i['number'], f'A duplicate of #{keep["number"]}.', 'not_planned')
            found.remove(i)
            done.append(f'closed #{i["number"]} as a duplicate of #{keep["number"]}')
        if keep.get('body') != a['body'] or keep.get('title') != a['title']:
            client.send(
                'PATCH',
                f'repos/{repo}/issues/{keep["number"]}',
                {'title': a['title'], 'body': a['body']},
            )
            done.append(f'updated #{keep["number"]}: {a["title"]}')
        if a.get('comment'):
            client.send(
                'POST', f'repos/{repo}/issues/{keep["number"]}/comments', {'body': a['comment']}
            )
            done.append(f'commented on #{keep["number"]}')
    return done


def close(client: ghrest.Client, repo: str, number: int, comment: str, reason: str) -> None:
    client.send('POST', f'repos/{repo}/issues/{number}/comments', {'body': comment})
    client.send(
        'PATCH', f'repos/{repo}/issues/{number}', {'state': 'closed', 'state_reason': reason}
    )


def ensure_label(client: ghrest.Client, repo: str) -> None:
    path = f'repos/{repo}/labels/{quote(LABEL, safe="")}'
    if client.get_or_none(path) is not None:
        return
    try:
        client.send(
            'POST',
            f'repos/{repo}/labels',
            {'name': LABEL, 'color': LABEL_COLOR, 'description': LABEL_DESCRIPTION},
        )
    except ghrest.ApiError as exc:
        # Another series' run, in its own concurrency group, created it first.
        if exc.status != 422 or client.get_or_none(path) is None:
            raise


# --- command line ----------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest='command', required=True)
    pt = sub.add_parser('point', help='write the point of a measure call')
    pt.add_argument(
        '--results', required=True, help='the downloaded perf-results-<prefix>-measure-* folders'
    )
    pt.add_argument('--prefix', required=True, help="perf-ci.yaml's artifact prefix")
    pt.add_argument('--repo', required=True, help='owner/name of the measured repository')
    pt.add_argument('--series', required=True)
    pt.add_argument('--ref', required=True, help='the ref the point measured')
    pt.add_argument('--commit', required=True, help="the ref's measured commit")
    pt.add_argument('--date', required=True, help='when it was measured, as 2026-11-17T06:09:26Z')
    pt.add_argument('--run-id', required=True)
    pt.add_argument('--run-url', required=True)
    pt.add_argument('--out', required=True)
    sk = sub.add_parser('skip', help='print true when a point of --commit would add nothing')
    sk.add_argument(
        '--state',
        required=True,
        help="the series' state.json, where a missing file is a series never measured",
    )
    sk.add_argument('--commit', required=True)
    sk.add_argument('--issues', required=True, help="fetch-issues' file")
    nd = sub.add_parser('needs', help='print the perf-trend paths judging a point reads')
    nd.add_argument('--listing', required=True, help='`git ls-tree -r --name-only` of the branch')
    nd.add_argument('--series', required=True)
    jd = sub.add_parser('judge', help='judge a point and write it into the perf-trend checkout')
    jd.add_argument('--root', required=True, help='the perf-trend checkout')
    jd.add_argument('--point', required=True, help="the point command's file")
    jd.add_argument('--series', required=True, help='the series the point must belong to')
    jd.add_argument('--repo', required=True)
    jd.add_argument(
        '--issues', help="the repository's `performance` issues as a JSON list, and none when unset"
    )
    jd.add_argument('--actions', required=True, help='where the issue actions are written')
    jd.add_argument('--summary', help='a file the Markdown summary is appended to')
    fi = sub.add_parser('fetch-issues', help="write the repository's `performance` issues")
    fi.add_argument('--repo', required=True)
    fi.add_argument('--out', required=True)
    iss = sub.add_parser('issues', help="apply judge's actions to the repository's issues")
    iss.add_argument('--actions', required=True)
    iss.add_argument('--repo', required=True)
    return p.parse_args(argv)


def run(a: argparse.Namespace) -> int:
    if a.command == 'point':
        made = point(
            perf_gate.shard_groups(a.results, a.prefix),
            repo=a.repo,
            series=a.series,
            ref=a.ref,
            commit=a.commit,
            date=a.date,
            run_id=a.run_id,
            run_url=a.run_url,
        )
        pathlib.Path(a.out).write_text(dumps(made))
        print(f'{a.out}: {len(made["cells"])} cells from {made["runs"]} runs')
    elif a.command == 'skip':
        path = pathlib.Path(a.state)
        state = perf_gate.load(path) if path.is_file() else new_state('')
        issues = json.loads(pathlib.Path(a.issues).read_text())
        print('true' if skip(state, a.commit, issues) else 'false')
    elif a.command == 'needs':
        listing = pathlib.Path(a.listing).read_text().splitlines()
        print('\n'.join(needs(listing, a.series)))
    elif a.command == 'judge':
        p = perf_gate.load(a.point)
        if p.get('series') != a.series:
            raise ValueError(
                f'{a.point} is a point of series {p.get("series")!r}, not {a.series!r}'
            )
        issues = json.loads(pathlib.Path(a.issues).read_text()) if a.issues else None
        actions, summary = record(pathlib.Path(a.root), p, repo=a.repo, issues=issues)
        pathlib.Path(a.actions).write_text(json.dumps(actions, indent=1) + '\n')
        print(summary)
        if a.summary:
            with pathlib.Path(a.summary).open('a') as f:
                f.write(summary)
    elif a.command == 'fetch-issues':
        issues = fetch_issues(a.repo)
        pathlib.Path(a.out).write_text(json.dumps(issues, indent=1) + '\n')
        print(f'{a.out}: {len(issues)} issues labelled {LABEL}')
    else:
        actions = json.loads(pathlib.Path(a.actions).read_text())
        for line in apply(actions, a.repo):
            print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        return run(a)
    except (OSError, ValueError, KeyError, TypeError, ghrest.ApiError) as exc:
        print(f'::error::perf_trend: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
