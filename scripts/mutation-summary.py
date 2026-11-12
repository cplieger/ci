#!/usr/bin/env python3
"""A repository call's result record, and the fleet summary of one weekly mutation run.

`record`: the `result` job of mutation-gremlins-repo.yaml or
mutation-stryker-repo.yaml writes `<key>.json` from its `needs` (every job's
result and outputs) and the merged results it downloaded: the entries the call
found, and per entry tested, refused, failed, not run or opted out, each with
its reason, and the modules a one-module call leaves untested, which never
decide its outcome. An entry is tested only when its merged result holds a
mutant a test judged. It exits 1 unless the call passed or every entry opted out, so
the call goes red on its own.

`fleet`: the weekly summary job reads each call's record on its own. A row
reads `passed` only from a record that parses, names a tested entry and holds
no refused, failed or not-run entry and no failed job; a missing or unusable
record is named for its key alone. It exits 1 when any row is neither passed
nor opted out.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import mutation_cost

_spec = importlib.util.spec_from_file_location(
    'stryker_aggregate', Path(__file__).with_name('stryker-aggregate.py')
)
stryker_aggregate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stryker_aggregate)

TOOLS = ('gremlins', 'stryker')
RESULTS = ('success', 'failure', 'cancelled', 'skipped')
TESTED, REFUSED, FAILED, NOT_RUN, OPTED_OUT = 'tested', 'refused', 'failed', 'not run', 'opted out'
STATES = (TESTED, REFUSED, FAILED, NOT_RUN, OPTED_OUT)
GREEN = ('passed', OPTED_OUT)
# The job that finds the call's entries, and the jobs an entry must pass through to be tested.
FINDER = {'gremlins': 'plan', 'stryker': 'discover'}
PIPELINE = {'gremlins': ('shard', 'run'), 'stryker': ('shard', 'run', 'merge')}
# The statuses of a mutant a test judged, in each tool's merged result.
JUDGED = {
    'stryker': frozenset(stryker_aggregate.DETECTED | stryker_aggregate.UNDETECTED),
    'gremlins': frozenset(('killed', 'lived', 'not_covered', 'timed_out')),
}
# mutation_cost.entry_key's characters, its separators included.
KEY = re.compile(r'[A-Za-z0-9._~=-]+')


def _json(text: object, default: object) -> object:
    if not isinstance(text, str) or not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return None


def found(tool: str, needs: dict) -> tuple[list[str], list[dict], list[str], list[str], str]:
    """The testable, refused, opted-out and untested entries the finder job output, and why
    they are unknown, or ''."""
    job = FINDER[tool]
    if needs[job]['result'] != 'success' and tool == 'stryker':
        return [], [], [], [], f'the {job} job ended {needs[job]["result"]}'
    out = needs[job].get('outputs') or {}
    untested: object = []
    if tool == 'gremlins':
        testable = out.get('modules', '').split() if isinstance(out.get('modules'), str) else None
        opted: object = []
        untested = out.get('untested', '')
        untested = untested.split() if isinstance(untested, str) else None
    else:
        raw = _json(out.get('entries'), None)
        testable = (
            [e['dir'] for e in raw]
            if isinstance(raw, list)
            and all(isinstance(e, dict) and isinstance(e.get('dir'), str) for e in raw)
            else None
        )
        opted = _json(out.get('opted_out'), [])
    refused = _json(out.get('refused'), [])
    usable = isinstance(refused, list) and all(
        isinstance(r, dict) and isinstance(r.get('dir'), str) and isinstance(r.get('reason'), str)
        for r in refused
    )
    if (
        testable is None
        or not usable
        # A gremlins call always finds a module to test, or refused every one it found.
        or (tool == 'gremlins' and not testable and not refused)
        or not isinstance(opted, list)
        or not all(isinstance(d, str) for d in opted)
        or untested is None
    ):
        return (
            [],
            [],
            [],
            [],
            f'the {job} job wrote no usable entry list (it ended {needs[job]["result"]})',
        )
    return testable, refused, opted, untested, ''


def planned(needs: dict) -> set[str] | None:
    """The Stryker dirs the shard job planned to run, or None when it wrote no usable merge list."""
    merge = _json((needs['shard'].get('outputs') or {}).get('merge'), None)
    if not isinstance(merge, list) or not all(
        isinstance(m, dict) and isinstance(m.get('dir'), str) for m in merge
    ):
        return None
    return {m['dir'] for m in merge}


def stryker_judged(reports: Path, repo: str) -> dict[str, int | str]:
    """Per dir, the mutants a test judged in the merged report the merge job staged, or why it is unreadable.

    The merge job, which runs no target code, wrote each meta.json and mutation.json.
    """
    out: dict[str, int | str] = {}
    for meta_path in sorted(reports.rglob('meta.json')) if reports.is_dir() else []:
        try:
            meta = mutation_cost.load_json(meta_path)
        except Exception:
            continue
        if not isinstance(meta, dict) or meta.get('repo') != repo:
            continue
        dir_ = meta.get('dir')
        if not isinstance(dir_, str) or dir_ in out:
            continue
        try:
            doc = mutation_cost.load_json(meta_path.parent / 'mutation.json', None)
        except Exception as exc:
            out[dir_] = f'its merged report is unreadable ({mutation_cost.describe(exc)})'
            continue
        files = doc.get('files') if isinstance(doc, dict) else None
        if not isinstance(files, dict):
            out[dir_] = 'its merged report holds no files'
            continue
        out[dir_] = sum(
            1
            for f in files.values()
            if isinstance(f, dict) and isinstance(f.get('mutants'), list)
            for m in f['mutants']
            if isinstance(m, dict) and m.get('status') in JUDGED['stryker']
        )
    return out


def gremlins_judged(reports: Path, repo: str, legs: object) -> dict[str, int | str]:
    """Per module, the fewest mutants a test judged in any planned attempt (its shards summed).

    An attempt whose result did not arrive counts 0 for every module.
    """
    if not isinstance(legs, list) or not legs:
        return {}
    attempts = {str(r.get('attempt')) for r in legs if isinstance(r, dict)}
    per: dict[str, dict[str, int]] = {a: {} for a in attempts}
    for meta_path in sorted(reports.rglob('meta.json')) if reports.is_dir() else []:
        try:
            meta = mutation_cost.load_json(meta_path)
            if not isinstance(meta, dict) or meta.get('repo') != repo:
                continue
            got = per.get(str(meta.get('attempt')))
            doc = mutation_cost.load_json(meta_path.parent / 'gremlins-out.json', None)
            modules = doc.get('modules') if isinstance(doc, dict) else None
            if got is None or not isinstance(modules, list):
                continue
            for m in modules:
                if not isinstance(m, dict) or not isinstance(m.get('dir'), str):
                    continue
                n = [m.get(f'mutants_{s}') for s in sorted(JUDGED['gremlins'])]
                if all(mutation_cost.count(v) for v in n):
                    got[m['dir']] = got.get(m['dir'], 0) + sum(n)
        except Exception:
            continue
    dirs = {d for counts in per.values() for d in counts}
    return {d: min(counts.get(d, 0) for counts in per.values()) for d in dirs}


def state(
    tool: str, needs: dict, dir_: str, plan: set[str] | None, judged: dict[str, int | str]
) -> dict:
    """A testable entry's state: tested only when every job of its pipeline succeeded and
    its merged result holds a mutant a test judged."""
    for job in PIPELINE[tool]:
        result = needs[job]['result']
        if result in ('failure', 'cancelled'):
            return {'state': FAILED, 'reason': f'the {job} job ended {result}'}
        if job == 'shard' and result == 'success' and tool == 'stryker':
            if plan is None:
                return {'state': FAILED, 'reason': 'the shard job wrote no usable merge list'}
            if dir_ not in plan:
                return {
                    'state': NOT_RUN,
                    'reason': 'not enrolled: @stryker-mutator/core is not a devDependency',
                }
        if result != 'success':
            return {'state': NOT_RUN, 'reason': f'the {job} job was {result}'}
    n = judged.get(dir_)
    if isinstance(n, str):
        return {'state': NOT_RUN, 'reason': n}
    if n is None:
        return {'state': NOT_RUN, 'reason': 'no merged result reached the result job'}
    if n < 1:
        return {'state': NOT_RUN, 'reason': 'its merged result holds no mutant a test judged'}
    return {'state': TESTED, 'reason': '', 'mutants': n}


def max_shards(tool: str, needs: dict) -> int | None:
    """The most shards one of the call's run legs belongs to."""
    out = needs['shard'].get('outputs') or {}
    legs = _json(out.get('matrix' if tool == 'gremlins' else 'run'), [])
    if not isinstance(legs, list) or not legs or not all(isinstance(r, dict) for r in legs):
        return None
    n = [r.get('shards') or 1 for r in legs]
    return max(n) if all(mutation_cost.count(s) and s >= 1 for s in n) else None


def judge(doc: dict) -> tuple[str, str]:
    """The call's outcome and its reason, from its fault, its jobs and its entries alone."""
    problems = [doc['fault']] if doc['fault'] else []
    if bad := sorted(j for j, r in doc['jobs'].items() if r in ('failure', 'cancelled')):
        problems.append(f'failed jobs: {", ".join(bad)}')
    entries = doc['entries']
    for state_ in (REFUSED, FAILED, NOT_RUN):
        problems += [
            f'{n}: {state_} ({e["reason"]})'
            for n, e in sorted(entries.items())
            if e['state'] == state_
        ]
    if problems:
        red = doc['fault'] or bad or any(e['state'] in (REFUSED, FAILED) for e in entries.values())
        return (FAILED if red else NOT_RUN), '; '.join(problems)
    if not entries:
        return NOT_RUN, 'it found no entry to test'
    if any(e['state'] == TESTED for e in entries.values()):
        return 'passed', ''
    return OPTED_OUT, 'every entry opts out of the scheduled run'


def record(
    tool: str, repo: str, target: str, key: str, needs: object, reports: Path | None = None
) -> dict:
    """The call's result record from its NEEDS context (every job's result and outputs) and
    the merged results under REPORTS."""
    wanted = {FINDER[tool], *PIPELINE[tool]}
    if (
        not isinstance(needs, dict)
        or not wanted <= set(needs)
        or not all(isinstance(v, dict) and v.get('result') in RESULTS for v in needs.values())
    ):
        raise SystemExit(
            f'mutation-summary: the needs context lacks {sorted(wanted)} or a job result'
        )
    testable, refused, opted, untested, fault = found(tool, needs)
    plan = planned(needs) if tool == 'stryker' else None
    entries = {r['dir']: {'state': REFUSED, 'reason': r['reason']} for r in refused}
    entries |= {
        d: {'state': OPTED_OUT, 'reason': 'stryker.weekly.json sets weekly=false'} for d in opted
    }
    reports = reports or Path('/nonexistent')
    if tool == 'stryker':
        judged = stryker_judged(reports, repo)
    else:
        legs = _json((needs['shard'].get('outputs') or {}).get('matrix'), None)
        judged = gremlins_judged(reports, repo, legs)
    entries |= {d: state(tool, needs, d, plan, judged) for d in testable}
    doc = {
        'tool': tool,
        'repo': repo,
        'target': target,
        'key': key,
        'jobs': {j: v['result'] for j, v in sorted(needs.items())},
        'fault': fault,
        'found': sorted(entries),
        'tested': sorted(n for n, e in entries.items() if e['state'] == TESTED),
        'entries': dict(sorted(entries.items())),
        'untested': sorted(set(untested) - set(entries)),
        'shards': max_shards(tool, needs),
    }
    doc['outcome'], doc['reason'] = judge(doc)
    return doc


def result_fault(doc: object, tool: str, key: str) -> str:
    """Why DOC is not KEY's record of TOOL, or ''."""
    if not isinstance(doc, dict):
        return 'it is not a JSON object'
    if doc.get('tool') != tool or doc.get('key') != key:
        return f'it is the result of {doc.get("tool")!r:.40} {doc.get("key")!r:.200}'
    jobs, entries = doc.get('jobs'), doc.get('entries')
    if not isinstance(jobs, dict) or not all(
        isinstance(j, str) and r in RESULTS for j, r in jobs.items()
    ):
        return 'its job results are unusable'
    if not isinstance(entries, dict) or not all(
        isinstance(n, str)
        and isinstance(e, dict)
        and e.get('state') in STATES
        and isinstance(e.get('reason'), str)
        and (e['state'] != TESTED or (mutation_cost.count(e.get('mutants')) and e['mutants'] >= 1))
        for n, e in entries.items()
    ):
        return 'its entries are unusable'
    if doc.get('found') != sorted(entries) or doc.get('tested') != sorted(
        n for n, e in entries.items() if e['state'] == TESTED
    ):
        return 'its found or tested list disagrees with its entries'
    untested = doc.get('untested')
    if (
        not isinstance(untested, list)
        or not all(isinstance(d, str) for d in untested)
        or untested != sorted(set(untested))
        or set(untested) & set(entries)
    ):
        return 'its untested list is not a sorted list of names apart from its entries'
    if not isinstance(doc.get('fault'), str):
        return 'its fault is not a string'
    shards = doc.get('shards')
    if shards is not None and (not mutation_cost.count(shards) or shards < 1):
        return f'its shard count {shards!r:.40} is not a positive integer'
    if (doc.get('outcome'), doc.get('reason')) != judge(doc):
        return f'its outcome {doc.get("outcome")!r:.40} is not what its jobs and entries give'
    return ''


def row(tool: str, key: str, results: Path) -> tuple[str, str, str, str]:
    """KEY's (outcome, shards, untested, reason) cells; a fault in its record is that key's alone."""
    try:
        if not KEY.fullmatch(key) or key in ('.', '..'):
            return FAILED, '', '', f'unreadable result: its key {key!r:.200} is not an entry_key'
        path = results / f'{key}.json'
        if not path.is_file():
            return NOT_RUN, '', '', 'no result: the call ended before its result job'
        doc = mutation_cost.load_json(path)
        if why := result_fault(doc, tool, key):
            return FAILED, '', '', f'unreadable result: {why}'
    except Exception as exc:
        return FAILED, '', '', f'unreadable result: {mutation_cost.describe(exc)}'
    shards = '' if doc['shards'] is None else str(doc['shards'])
    return doc['outcome'], shards, ', '.join(doc['untested']), doc['reason']


def summary(tool: str, keys: list[str], results: Path) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for key in keys:
        outcome, shards, untested, why = row(tool, key, results)
        rows.append((key, outcome, shards, untested, why))
        if outcome not in GREEN:
            mutation_cost.annotate('error', t'{tool} {key}: {outcome} ({why:.2000})')
        elif outcome == OPTED_OUT:
            mutation_cost.annotate('notice', t'{tool} {key}: {outcome} ({why})')
    return rows


def fleet(tool: str, keys_json: str, results: Path) -> int:
    keys = json.loads(keys_json)
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise SystemExit(f'--keys must be a JSON list of names, got {keys_json!r:.200}')
    rows = summary(tool, keys, results)
    mutation_cost.summarize(t'{tool}: {len(rows)} call(s)')
    mutation_cost.summarize(t'| key | outcome | shards | untested | reason |', table=True)
    mutation_cost.summarize(t'|---|---|---|---|---|', table=True)
    for key, outcome, shards, untested, why in rows:
        mutation_cost.summarize(
            t'| {key} | {outcome} | {shards} | {untested:.2000} | {why:.2000} |', table=True
        )
    for key, outcome, *_ in rows:
        mutation_cost.say(t'{key}: {outcome}')
    if not rows:
        mutation_cost.annotate('error', t'{tool}: the run named no call, so it tested nothing')
    return 0 if rows and all(r[1] in GREEN for r in rows) else 1


def write_record(args: argparse.Namespace) -> int:
    doc = record(args.tool, args.repo, args.target, args.key, json.loads(args.needs), args.reports)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1) + '\n')
    where = f'{args.repo}:{args.target}' if args.target else args.repo
    if doc['untested']:
        mutation_cost.annotate(
            'notice',
            t'{where}: untested in this call: {", ".join(doc["untested"]):.2000}',
            summary=True,
        )
    if doc['outcome'] not in GREEN:
        mutation_cost.annotate(
            'error', t'{where}: {doc["outcome"]} ({doc["reason"]:.2000})', summary=True
        )
        return 1
    mutation_cost.annotate(
        'notice', t'{where}: {doc["outcome"]}, tested {", ".join(doc["tested"]):.2000}'
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest='command', required=True)
    rec = sub.add_parser('record')
    rec.add_argument('--tool', required=True, choices=TOOLS)
    for a in ('--repo', '--key', '--needs'):
        rec.add_argument(a, required=True)
    rec.add_argument('--target', default='', help="the call's module or dir input")
    rec.add_argument('--out', required=True, type=Path)
    rec.add_argument(
        '--reports', type=Path, help="the call's merged results (Stryker) or attempts (gremlins)"
    )
    fl = sub.add_parser('fleet')
    fl.add_argument('--tool', required=True, choices=TOOLS)
    fl.add_argument('--keys', required=True, help="JSON list of the run's call keys")
    fl.add_argument('--results', required=True, type=Path)
    args = p.parse_args()
    if args.command == 'record':
        return write_record(args)
    return fleet(args.tool, args.keys, args.results)


if __name__ == '__main__':
    sys.exit(mutation_cost.run_main(main))
