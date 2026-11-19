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
import itertools
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
# BASELINE_POINTS long still finds BASELINE_POINTS good points, a longer one fewer, and
# the baseline the newest point carried is used only when the checkout holds no good point.
BASELINE_POINTS = 7
HISTORY = 21
# Scenario group rule, family to the median ratio over a scenario's cells whose
# baseline clears the family's guard floor. Over 900 one-run group samples of four
# measurements the worst median ratio was 1.294 for cpu_ms, 1.022 for heap_mb and
# 1.018 for rss_mb; no scenario has three server routes over the 10 ms floor.
GROUP = {'cpu_ms': 1.3, 'heap_mb': 1.1, 'rss_mb': 1.1}
GROUP_CELLS = 3
BAD_RUNS_SHOWN = 20
# Over five ten-run measurements one run's seconds for a unit stayed within 0.895 and
# 1.193 times the median of the other nine runs (3,660 samples), so a unit further than
# this off its plan.json seconds has moved, not varied.
SECONDS_DRIFT = 0.2
# perf-ci.yaml measures and guards this set on a pull request.
PR_SET = 'pr'


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
    with no reps has value None. Each unit's seconds are the median of its runs', and its
    profile_seconds, the time of a profiled rep, the median of the runs that ran one.
    Raises on a cell whose fields identity_error refuses or with a rep that is not a
    number, and on a unit timed twice in one run or whose times are not numbers."""
    runs = [perf_gate.merge(paths) for _, paths in sorted(groups.items())]
    runners = {r['runner'] for r in runs}
    if len(runners) != 1:
        raise ValueError(f'the runs of one point share a runner, not {sorted(map(str, runners))}')
    found: dict[tuple, dict] = {}
    for run in runs:
        for c in run['cells']:
            k = perf_gate.key(c)
            if why := identity_error(k):
                raise ValueError(f'{perf_gate.key_text(k)}: {why}')
            if not all(perf_gate.is_number(r) for r in c.get('reps') or []):
                raise ValueError(f'{perf_gate.key_text(k)}: a rep is not a number')
            seen = found.setdefault(k, {'kind': c.get('kind'), 'reps': [], 'cpu_models': []})
            if seen['kind'] != c.get('kind'):
                raise ValueError(
                    f'{perf_gate.key_text(k)}: measured as kind {seen["kind"]} and {c.get("kind")}'
                )
            seen['reps'].append(list(c.get('reps') or []))
            seen['cpu_models'].append(c.get('cpu_model', ''))
    timed: dict[tuple, list] = {}
    profiled: dict[tuple, list] = {}
    for name, run in zip(sorted(groups), runs, strict=True):
        mine = set()
        for s in run['seconds']:
            k = perf_gate.unit_key(s)
            if not all(isinstance(x, str) and x for x in k):
                raise ValueError(
                    f'a unit timed in {name or "the run"} names no {", ".join(perf_gate.UNIT_FIELDS)}'
                )
            if k in mine:
                raise ValueError(f'{perf_gate.key_text(k)}: timed twice in {name or "the run"}')
            if not perf_gate.is_number(s.get('seconds')):
                raise ValueError(f'{perf_gate.key_text(k)}: its seconds are not a number')
            if 'profile_seconds' in s and not perf_gate.is_number(s['profile_seconds']):
                raise ValueError(f'{perf_gate.key_text(k)}: its profile_seconds are not a number')
            mine.add(k)
            timed.setdefault(k, []).append(s['seconds'])
            if 'profile_seconds' in s:
                profiled.setdefault(k, []).append(s['profile_seconds'])
    units = []
    for k, v in sorted(timed.items()):
        u = {**dict(zip(perf_gate.UNIT_FIELDS, k, strict=True)), 'seconds': statistics.median(v)}
        if k in profiled:
            u['profile_seconds'] = statistics.median(profiled[k])
        units.append(u)
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
        'units': units,
        'cells': cells,
    }


def identity_error(k: tuple) -> str | None:
    """Why a measured cell's six fields cannot reach an issue's title, table and marker,
    or None. The metric alone may hold a space, as a server route's does."""
    if not all(isinstance(x, str) and x for x in k):
        return 'a cell field is empty'
    if any(re.search(r'[\s<>]', x) for x in k[:-1]):
        return 'a cell field holds whitespace, < or >'
    if re.search(r'[\r\n<>]', k[-1]):
        return 'its metric holds a line break, < or >'
    return None


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
    or else the baseline the newest such point carried, and None while fewer than
    perf_gate.GUARD_POINTS points of runner measured it since it last took over. Under an
    accepted regression the points before its first bad point are no baseline, and those
    up to its acceptance count however they were judged."""
    good, carried, measured = [], None, 0
    for h in reversed(history):
        if h['runner'] != runner:
            break
        c = h['cells'].get(k)
        if c is None or c.get('value') is None:
            continue
        if accepted and h['date'] < accepted['from']:
            break
        measured += 1
        if carried is None and perf_gate.is_number(c.get('baseline')):
            carried = c['baseline']
        forgiven = bool(accepted) and h['date'] <= accepted['at']
        if c.get('bad') and not forgiven:
            continue
        good.append(c['value'])
        if len(good) == BASELINE_POINTS:
            break
    if measured < perf_gate.GUARD_POINTS:
        return None
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
    each measured scenario's lifecycle in state. A regression tracked on another runner
    than p's ends, as does, at a dev point, one of a scenario p does not measure: a
    suspect silently, an open or recovering one with a close. Returns the new state, p
    with each judged timing cell's `baseline` and `bad`, and the issue actions."""
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
    ended: dict[str, tuple[str, str]] = {}
    for s, v in sorted(tracked.items()):
        if v['status'] == 'ok':
            if series == DEV and s not in scenarios:
                new['scenarios'].pop(s)
            continue
        if v['runner'] != p['runner']:
            why = f'the runner changed to `{p["runner"]}`'
            comment = (
                f'The runner changed to `{p["runner"]}`, so this issue is closed and the '
                'scenario is judged again on that runner, against its own baselines.'
            )
        elif series == DEV and s not in scenarios:
            why = 'the full set no longer measures it'
            comment = f'The full set no longer measures {s}, so this issue is closed.'
        else:
            continue
        ended[s] = (v['status'], why)
        new['scenarios'].pop(s)
        if v['status'] != 'suspect':
            actions.append(
                {
                    'op': 'close',
                    'marker': marker(series, s),
                    'title': title(series, s),
                    'labels': [LABEL],
                    'comment': comment,
                }
            )
    for s in scenarios:
        before = new['scenarios'].get(s, {'status': 'ok'})
        # Stepping on a point that judged none of the scenario's cells would close an
        # issue against values no baseline was taken for.
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
    summary = render_summary(judged, new, bad, previous, actions, unjudged, ended)
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
    ended: dict[str, tuple[str, str]],
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
        'Regressions ended at this point',
        ('Scenario', 'Was', 'Why'),
        ((s, was, why) for s, (was, why) in sorted(ended.items())),
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


def open_issues(issues: list[dict], mark: str) -> list[dict]:
    """The open issues of the regression mark names, oldest first: each whose body opens
    with the marker, however old, since apply edits the oldest of them."""
    tag = f'<!-- {mark} -->'
    mine = (i for i in issues if i.get('state') == 'open' and first_line(i.get('body')) == tag)
    return sorted(mine, key=lambda i: i['number'])


def observe(state: dict, issues: list[dict], *, series: str) -> tuple[dict, list[dict]]:
    """Fold the repository's `performance` issues into state: a tracked regression with
    no open issue and one a person closed since it opened is accepted (status ok, its
    cells' baselines restart from its first bad point); one with neither is sent the
    body it was last sent again, which repairs a run that stored its point and lost its
    issue writes; and an open issue of this series whose scenario tracks no regression
    is closed."""
    new = {**state, 'scenarios': {k: dict(v) for k, v in state['scenarios'].items()}}
    actions = []
    for s, v in sorted(state['scenarios'].items()):
        if v['status'] not in ('open', 'recovering'):
            continue
        if open_issues(issues, marker(series, s)):
            continue
        closed = [
            i
            for i in issues
            if i.get('state') == 'closed'
            and marked(i.get('body')) == (series, s)
            and (i.get('closed_at') or '') >= v['opened']
        ]
        if closed:
            at = state['last']['date'] if state.get('last') else v['first_bad']['date']
            new['scenarios'][s] = {
                'status': 'ok',
                'accepted': {'from': v['first_bad']['date'], 'at': at},
            }
        else:
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


def skip(state: dict, commit: str, runner: str) -> bool:
    """Whether a point of commit on runner adds nothing: the series' last point measured
    it on runner, no scenario tracked on runner is suspect, waiting on a confirming
    point, or recovering, waiting on its second good one, and the guard's baseline holds
    that point (guard_behind). A skipped run still sends observe's issue writes."""
    last = state.get('last')
    pending = any(
        v['status'] in ('suspect', 'recovering') and v.get('runner') == runner
        for v in state['scenarios'].values()
    )
    same = bool(last) and last['commit'] == commit and last.get('runner') == runner
    return same and not pending and not last.get('guard_behind')


def guard_behind(base: dict, p: dict, pr_units: dict[tuple, set[str] | None]) -> str | None:
    """Why the guard's baseline (guard_baseline) does not hold dev's point p yet, or None:
    it holds fewer than perf_gate.GUARD_POINTS runs, or a timing cell of pr_units
    (planned_windows of the pr set) is past the guard against it, so every pull request
    fails on that cell until more points of p move the median."""
    runs = base['runs']
    if runs < perf_gate.GUARD_POINTS:
        return (
            f'it holds {runs} run{"" if runs == 1 else "s"} of `{base["runner"]}`, '
            f'fewer than {perf_gate.GUARD_POINTS}'
        )
    held = {perf_gate.key(c): c['baseline'] for c in base['cells'] if perf_gate.is_timing(c)}
    over = sum(
        1
        for c in p['cells']
        if perf_gate.is_timing(c)
        and perf_gate.planned(pr_units, c)
        and perf_gate.is_number(c.get('value'))
        and perf_gate.key(c) in held
        and perf_gate.over_guard(c['metric'], c['value'], held[perf_gate.key(c)])
    )
    if not over:
        return None
    return f'{over} timing cell{"" if over == 1 else "s"} {"is" if over == 1 else "are"} past the guard against it'


def point_runs(p: dict) -> int:
    """The runs most of a point's timing cells were measured in."""
    runs = [len(c['reps']) for c in p['cells'] if perf_gate.is_timing(c) and c.get('reps')]
    return statistics.median_low(runs) if runs else 0


def guard_baseline(points: list[dict]) -> dict:
    """The pull request guard's baselines from dev's points, oldest first: each timing
    cell's median over the newest point's runner's last perf_gate.GUARD_POINTS points
    since it last took over, when the points that measured it hold GUARD_POINTS runs
    between them; each count as the newest of them measured it; and `runs`, the runs
    those points hold."""
    runner = points[-1]['runner']
    stretch = list(itertools.takewhile(lambda p: p['runner'] == runner, reversed(points)))
    last = stretch[: perf_gate.GUARD_POINTS][::-1]
    values: dict[tuple, tuple[str, list, list]] = {}
    for p in last:
        p_runs = point_runs(p)
        for c in p['cells']:
            counted = c['kind'] == 'count' and c.get('value') is not None
            if counted or (perf_gate.is_timing(c) and perf_gate.is_number(c.get('value'))):
                _, v, runs = values.setdefault(perf_gate.key(c), (c['kind'], [], []))
                v.append(c['value'])
                runs.append(p_runs)
    cells = []
    for k, (kind, v, runs) in sorted(values.items()):
        if kind != 'count' and sum(runs) < perf_gate.GUARD_POINTS:
            continue
        value = v[-1] if kind == 'count' else statistics.median(v)
        cells.append({**perf_gate.key_dict(k), 'kind': kind, 'baseline': value})
    return {
        'schema': SCHEMA,
        'series': DEV,
        'runner': runner,
        'commits': [p['commit'] for p in last],
        'runs': sum(map(point_runs, last)),
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
    record job checks out to judge a point of series: its state, dev's state for another
    series, its last HISTORY points and, while it has fewer than BASELINE_POINTS, the
    last HISTORY dev points at or before its first one."""

    def points_of(s: str) -> list[str]:
        prefix = f'series/{s}/points/'
        return sorted(p for p in listing if p.startswith(prefix) and p.endswith('.json'))

    own = points_of(series)
    out = [f'series/{series}/state.json']
    if series != DEV:
        out.append(f'series/{DEV}/state.json')
    out += own[-HISTORY:]
    if series != DEV and len(own) < BASELINE_POINTS:
        first = pathlib.PurePosixPath(own[0]).name if own else None
        dev = [p for p in points_of(DEV) if first is None or pathlib.PurePosixPath(p).name <= first]
        out += dev[-HISTORY:]
    return out


def seedable(issues: list[dict], series: str) -> None:
    """Raise when issues holds one marked with series, whose history a new series would
    lose: observe would close its open issues and the guard restart from one point."""
    lost = [i for i in issues if (m := marked(i.get('body'))) and m[0] == series]
    if lost:
        raise ValueError(
            f'the {series} series has no state.json but issue #{lost[0]["number"]} is '
            'marked with it, so its history is lost: restore the perf-trend branch from '
            'a clone that holds it'
        )


def record(
    root: pathlib.Path,
    p: dict,
    *,
    repo: str,
    issues: list[dict] | None,
    pr_units: dict[tuple, set[str] | None],
) -> tuple[list[dict], str]:
    """Judge point p against the checkout at root and write it there: the judged point,
    the series' state and, for dev, the guard's baseline.json and whether it holds p
    for the pull requests' units (pr_units, as guard_behind reads them). Refuses a point older than
    the series' newest, a series with no state while issues holds one marked with it, and
    a series other than dev on a checkout with no dev series. Returns the issue actions
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
    if not state_file.is_file():
        if series != DEV and not (series_dir(root, DEV) / 'state.json').is_file():
            raise ValueError(
                f'the {series} series cannot start a perf-trend branch that holds no dev '
                'series: restore the branch from a clone that holds it, or seed it with dev'
            )
        seedable(issues or [], series)
    points = sorted(f.name for f in (d / 'points').glob('*.json'))
    if points and target.name < points[-1]:
        raise ValueError(
            f'{target.name} is older than {points[-1]}, the newest point of the {series} '
            'series, so it is not recorded: a later run judged the series past it'
        )
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
    state, summary = judged.state, judged.summary
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(judged.point))
    if series == DEV:
        base = guard_baseline([*history, judged.point])
        behind = guard_behind(base, judged.point, pr_units)
        state = {**state, 'last': {**state['last'], 'guard_behind': behind}}
        (d / 'baseline.json').write_text(dumps(base))
        if behind:
            summary += (
                f"The pull request guard's baseline does not hold this point yet: {behind}, "
                f'so the next run measures `{p["commit"][:12]}` again.\n'
            )
    state_file.write_text(dumps(state))
    return actions, summary


def pr_units(plan_doc: dict) -> dict[tuple, set[str] | None]:
    """planned_windows of plan_doc's PR_SET cells, passing over an entry plan() would
    refuse; empty when there is no such set."""
    spec = (plan_doc.get('sets') or {}).get(PR_SET) if isinstance(plan_doc, dict) else None
    cells = spec.get('cells') if isinstance(spec, dict) else None
    return perf_gate.planned_windows(
        c
        for c in (cells if isinstance(cells, list) else ())
        if isinstance(c, dict)
        and all(isinstance(c.get(f), str) for f in perf_gate.UNIT_FIELDS)
        and perf_gate.windows_error(c) is None
    )


def seconds_drift(plan_doc: dict, p: dict) -> str:
    """The summary table of each window-less unit of plan_doc's sets whose seconds point
    p measured more than SECONDS_DRIFT off its `seconds`, which plan() shards on: the
    unit's seconds, plus its profile_seconds for a cell marked `profile`, and a profiled
    cell is passed over when p ran no profiled rep of it. Information: an entry plan()
    would refuse is passed over, never failed."""
    measured = {perf_gate.unit_key(u): u for u in p['units']}
    sets = plan_doc.get('sets')
    rows = []
    for name, spec in sorted(sets.items()) if isinstance(sets, dict) else ():
        for u in spec.get('cells') or [] if isinstance(spec, dict) else ():
            if (
                not isinstance(u, dict)
                or 'windows' in u
                or not perf_gate.is_number(u.get('seconds'))
            ):
                continue
            m = took(measured.get(perf_gate.unit_key(u)), profiled=bool(u.get('profile')))
            if m is not None and abs(m - u['seconds']) > SECONDS_DRIFT * u['seconds']:
                rows.append(
                    (name, perf_gate.key_text(perf_gate.unit_key(u)), num(u['seconds']), num(m))
                )
    title = f'Units measured more than {SECONDS_DRIFT * 100:g} % off their plan.json seconds (information)'
    lines = perf_gate.table(title, ('Set', 'Unit', 'plan.json', 'Measured'), rows)
    return '\n'.join(lines) + '\n' if lines else ''


def took(unit: dict | None, *, profiled: bool) -> float | None:
    if unit is None or (profiled and 'profile_seconds' not in unit):
        return None
    return unit['seconds'] + (unit['profile_seconds'] if profiled else 0)


# --- issues ----------------------------------------------------------------


def fetch_issues(repo: str, client: ghrest.Client | None = None) -> list[dict]:
    """Every issue of repo labelled `performance`, open or closed, as observe reads them."""
    client = client or ghrest.DEFAULT
    path = f'repos/{repo}/issues?state=all&labels={quote(LABEL, safe="")}'
    return [
        {k: i.get(k) for k in ('number', 'state', 'closed_at', 'body')}
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
        mine = open_issues(found, a['marker'])
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
    sk.add_argument(
        '--runner', required=True, help='the runner label the point would be stamped with'
    )
    rp = sub.add_parser(
        'repair', help="write observe's issue writes of a skipped run and print whether any"
    )
    rp.add_argument('--state', required=True, help="the series' state.json")
    rp.add_argument('--issues', required=True, help="fetch-issues' file")
    rp.add_argument('--out', required=True, help='where the issue actions are written')
    nd = sub.add_parser('needs', help='print the perf-trend paths judging a point reads')
    nd.add_argument('--listing', required=True, help='`git ls-tree -r --name-only` of the branch')
    nd.add_argument('--series', required=True)
    sd = sub.add_parser('seedable', help='fail when an issue is marked with a series to start')
    sd.add_argument('--issues', required=True, help="fetch-issues' file")
    sd.add_argument('--series', required=True)
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
    jd.add_argument(
        '--plan', required=True, help="the measured commit's tests/perf/plan.json, read as data"
    )
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
        print('true' if skip(state, a.commit, a.runner) else 'false')
    elif a.command == 'repair':
        state = perf_gate.load(a.state)
        issues = json.loads(pathlib.Path(a.issues).read_text())
        actions = observe(state, issues, series=state['series'])[1]
        pathlib.Path(a.out).write_text(json.dumps(actions, indent=1) + '\n')
        print('true' if actions else 'false')
    elif a.command == 'needs':
        listing = pathlib.Path(a.listing).read_text().splitlines()
        print('\n'.join(needs(listing, a.series)))
    elif a.command == 'seedable':
        seedable(json.loads(pathlib.Path(a.issues).read_text()), a.series)
    elif a.command == 'judge':
        p = perf_gate.load(a.point)
        if p.get('series') != a.series:
            raise ValueError(
                f'{a.point} is a point of series {p.get("series")!r}, not {a.series!r}'
            )
        issues = json.loads(pathlib.Path(a.issues).read_text()) if a.issues else None
        plan_doc = perf_gate.load(a.plan)
        actions, summary = record(
            pathlib.Path(a.root), p, repo=a.repo, issues=issues, pr_units=pr_units(plan_doc)
        )
        summary += seconds_drift(plan_doc, p)
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
