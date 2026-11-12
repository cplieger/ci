#!/usr/bin/env python3
"""Plan, shard and merge one repository's weekly Stryker entries (mutation-stryker-repo.yaml).

discover (discover job): one repository's entries, each named by mutation_cost.entry_key.
plan    (plan job): the entry's sizing from scripts/stryker-ci-reporter.mjs's plan pass.
matrix  (shard job): each of the repository's run plans, files packed by measured cost
        (scripts/mutation_cost.py) from its cost history, and the run and merge matrices.
mutate  (run job): one shard's --mutate list.
costs   (run job): one shard's measured cost per file.
merge   (merge job): the shards' results as one run's report, its score, and the next cost history.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import mutation_cost

if TYPE_CHECKING:
    from string.templatelib import Template

_spec = importlib.util.spec_from_file_location(
    'stryker_aggregate', Path(__file__).with_name('stryker-aggregate.py')
)
stryker_aggregate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stryker_aggregate)

# Worker-seconds a run mutant costs beyond its tests' netTime on the test runners
# (TypeScript checker, runner IPC): 0.96 in ui's run of 42418943692 (55 min x 4
# workers, less 45.7 min of summed duration x 4/2, over 8059 mutants), fitted
# with the 2 runners its checker leaves of 4 workers while checking. Stryker's
# freeCheckers() hands those back once checking ends, so the half split is an
# upper bound that over-prices a never-run entry. sse/web bounds nothing: its
# summed durations exceed what 2 runners could hold.
PRIOR_OVERHEAD_SECS = 0.96
# The run job's steps outside the Stryker process (checkout, clone, npm ci,
# upload) took 40 s to 1 min in run 42418943692.
JOB_OVERHEAD_SECS = 90
# A conservative allowlist: minimatch's specials and the --mutate list's comma
# are outside it, so a file outside it keeps its entry in one job.
SAFE_PATH = re.compile(r'[A-Za-z0-9._/-]+')
SHA = re.compile(r'[0-9a-f]{40}')
ENTRY_FIELDS = ('repo', 'sha', 'dir', 'key')


def read_json(path: Path, limit: int | None = mutation_cost.MAX_INPUT_BYTES) -> object:
    """PATH's JSON value, or None when it is missing or unusable in any way (mutation_cost.load_json)."""
    try:
        return mutation_cost.load_json(path, limit)
    except Exception:
        return None


# A stryker config's file name, and the opt-out sidecar beside one.
CONFIGS = re.compile(r'stryker\.config\..+')
OPT_OUT = 'stryker.weekly.json'
PRUNED = frozenset({'node_modules', '.git'})
SCAN_DEPTH = 4


# --- discover --------------------------------------------------------------


def config_dirs(root: Path) -> list[str]:
    """Each dir under ROOT, '.' for itself, holding a stryker config, at most SCAN_DEPTH - 1 levels down."""
    found: set[str] = set()
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        for child in d.iterdir():
            if child.is_symlink():
                continue
            if child.is_dir() and child.name not in PRUNED and depth + 1 < SCAN_DEPTH:
                stack.append((child, depth + 1))
            elif child.is_file() and CONFIGS.fullmatch(child.name):
                found.add(d.relative_to(root).as_posix())
    return sorted(found)


def opted_out(d: Path) -> bool | None:
    """True for an exact {"weekly": false} sidecar, False for none, None for any other sidecar."""
    path = d / OPT_OUT
    if not path.is_file():
        return False
    try:
        doc = mutation_cost.load_json(path, 4096)
    except Exception:
        return None
    # 0 == False in Python, and the sidecar's contract is the JSON literal false.
    return True if doc == {'weekly': False} and doc['weekly'] is False else None


def discover(repo: str, sha: str, root: Path, only_dir: str, *, honour_opt_out: bool) -> dict:
    """REPO's entries at SHA from its checkout ROOT, the one ONLY_DIR when given.

    Returns the testable `entries`, the `refused` dirs (a name entry_key
    refuses, with the reason; mutation-summary.py makes the call red) and the
    `opted_out` ones. The checkout is target-written, so a dir name reaches the
    log only through mutation_cost's output functions. More than MAX_SHARDS
    entries fail the call, which keeps its run matrix within the 256 jobs one
    matrix runs.
    """
    if not SHA.fullmatch(sha):
        raise SystemExit(f'stryker-shard: sha {sha!r:.200} is not a 40-hex commit')
    entries, refused, opted = [], [], []
    for dir_ in config_dirs(root):
        if only_dir and dir_ != only_dir:
            continue
        try:
            key = mutation_cost.entry_key(repo, dir_)
        except ValueError as exc:
            mutation_cost.annotate('error', t'{repo}: {str(exc)}, so it is refused', summary=True)
            refused.append({'dir': dir_, 'reason': str(exc)})
            continue
        if honour_opt_out:
            out = opted_out(root / dir_)
            if out:
                mutation_cost.annotate(
                    'notice',
                    t'{repo}:{dir_}: {OPT_OUT} sets weekly=false, so the scheduled run skips it. '
                    t'Dispatch with only={repo} to mutation-test it.',
                )
                opted.append(dir_)
                continue
            if out is None:
                mutation_cost.annotate(
                    'warning',
                    t'{repo}:{dir_}: {OPT_OUT} is not exactly {{"weekly": false}}; mutation-testing the dir anyway',
                )
        mutation_cost.say(t'found {repo}:{dir_} @ {sha[:12]}')
        entries.append({'repo': repo, 'sha': sha, 'dir': dir_, 'key': key})
    if only_dir and not (entries or refused or opted):
        raise SystemExit(
            f'stryker-shard: {repo} has no stryker config to test in {only_dir!r:.200}'
        )
    if len(entries) > mutation_cost.MAX_SHARDS:
        raise SystemExit(
            f'stryker-shard: {repo} has {len(entries)} stryker configs, more than the '
            f'{mutation_cost.MAX_SHARDS} one call shards within its 256-job matrix'
        )
    return {'entries': entries, 'refused': refused, 'opted_out': opted}


# --- plan and matrix -------------------------------------------------------


def positive_int(v: object) -> bool:
    return mutation_cost.count(v) and v >= 1


def sizing(repo: str, dir_: str, sha: str, config: object, plan_pass: object) -> dict:
    """The entry's sizing from the plan pass; untrusted, because the target's code wrote the pass."""
    doc = {'repo': repo, 'dir': dir_, 'sha': sha, 'files': {}, 'shardable': True, 'break': None}
    thresholds = config.get('thresholds') if isinstance(config, dict) else None
    if isinstance(thresholds, dict) and mutation_cost.nonnegative(thresholds.get('break')):
        doc['break'] = thresholds['break']
    if not isinstance(plan_pass, dict) or not isinstance(plan_pass.get('files'), dict):
        raise SystemExit(f'{repo}:{dir_}: the plan pass wrote no file list')
    doc['workers'] = workers = plan_pass.get('workers')
    doc['runners'] = runners = plan_pass.get('runners', workers)
    # A worker that runs no tests (a checker) stretches each runner's share of the net time.
    spread = workers / runners if positive_int(workers) and positive_int(runners) else 1
    doc['setup_secs'] = round(float(plan_pass.get('setup_ms') or 0) / 1000 + JOB_OVERHEAD_SECS, 1)
    # A shard passes bare paths to --mutate, which would widen a ranged entry to whole files.
    if plan_pass.get('ranged') is not False:
        doc['shardable'] = False
    for path, f in sorted(plan_pass['files'].items()):
        run, early, net = (
            f.get(k, 0) if isinstance(f, dict) else 0 for k in ('run', 'early', 'net_ms')
        )
        doc['files'][path] = {
            'run': run,
            'early': early,
            'prior_secs': round(float(net) / 1000 * spread + run * PRIOR_OVERHEAD_SECS, 3),
        }
        if not SAFE_PATH.fullmatch(path):
            doc['shardable'] = False
    return doc


def valid_sizing(doc: object, entry: dict) -> str:
    if not isinstance(doc, dict):
        return 'not a JSON object'
    for k in ('repo', 'dir', 'sha'):
        if doc.get(k) != entry[k]:
            return f"its {k} {doc.get(k)!r:.200} is not the entry's {entry[k]!r:.200}"
    w, r = doc.get('workers'), doc.get('runners')
    if not positive_int(w):
        return f'workers {w!r:.200} is not a positive integer'
    if not positive_int(r) or r > w:
        return f'runners {r!r:.200} is not a positive integer up to the {w} workers'
    if not mutation_cost.nonnegative(doc.get('setup_secs')):
        return f'setup_secs {doc.get("setup_secs")!r:.200}'
    if not isinstance(doc.get('shardable'), bool):
        return f'shardable {doc.get("shardable")!r:.200} is not a boolean'
    if doc.get('break') is not None and not mutation_cost.nonnegative(doc['break']):
        return f'its break threshold {doc["break"]!r:.200} is not a number'
    files = doc.get('files')
    if not isinstance(files, dict) or not all(
        isinstance(f, dict)
        and all(mutation_cost.count(f.get(k)) for k in ('run', 'early'))
        and mutation_cost.nonnegative(f.get('prior_secs'))
        for f in files.values()
    ):
        return 'its file list is unusable'
    if doc.get('shardable') is True and (
        bad := next((p for p in files if not SAFE_PATH.fullmatch(p)), None)
    ):
        return f'it is shardable with file {bad!r:.200}, which --mutate cannot take'
    return ''


def read_history(path: Path) -> dict | None:
    """The entry's last cost history from the shard job's own fetch, cleaned, or None when it has none.

    Raises what mutation_cost.load_json raises, and ValueError for a value that is not a cost record.
    """
    if not path.is_file():
        return None
    doc = mutation_cost.load_json(path)
    if why := mutation_cost.record_fault(doc):
        raise ValueError(why)
    return mutation_cost.carry_forward({}, doc)


def assign(
    doc: dict, target_minutes: float, history: dict | None, limit: int = mutation_cost.MAX_SHARDS
) -> tuple[dict, list[Template]]:
    """The entry's run plan, its files priced from HISTORY and packed into at most LIMIT shards, and its warnings."""
    workers = doc['workers']
    where = f'{doc["repo"]}:{doc["dir"]}'
    setup = mutation_cost.setup_price((history or {}).get('setup_secs'), doc['setup_secs'])
    priced = mutation_cost.project(
        {p: (f['run'], f['prior_secs']) for p, f in doc['files'].items()}, history
    )
    warnings: list[Template] = []
    if refused := mutation_cost.refusals(priced):
        warnings.append(
            t'{where}: {refused} measured price(s) more than {mutation_cost.PRICE_FACTOR}x from '
            t"the plan pass's were refused, so those files are priced from it"
        )
    items = [mutation_cost.Item('.', p, secs / workers) for p, (secs, _) in priced.items()]
    one_job = setup + sum(it.work for it in items)
    shardable = doc.get('shardable') is True and items
    k, bins = (
        mutation_cost.choose(items, {'.': setup}, target_minutes * 60, limit)
        if shardable
        else (1, mutation_cost.pack(items, 1, {'.': setup}))
    )
    files = {}
    for n, b in enumerate(bins, start=1):
        for it in b.items:
            _, source = priced[it.unit]
            files[it.unit] = {
                'shard': n,
                'run': doc['files'][it.unit]['run'],
                'early': doc['files'][it.unit]['early'],
                'work_secs': round(it.work, 1),
                'cost': source,
            }
    plan = {k_: doc[k_] for k_ in ('repo', 'dir', 'sha', 'break', 'workers')}
    plan.update(
        shards=k,
        files=files if k > 1 else None,
        mutants={p: f['run'] for p, f in doc['files'].items()},
        one_job_minutes=round(one_job / 60, 1),
        projected_minutes=[round(b.load / 60, 1) for b in bins],
        target_minutes=target_minutes,
        history=history,
    )
    if shardable and mutation_cost.capped(k, bins, items, {'.': setup}, target_minutes * 60, limit):
        plan['capped'] = mutation_cost.cap_reason(limit)
        return plan, [*warnings, mutation_cost.cap_note(where, plan)]
    top = mutation_cost.top_secs(target_minutes * 60) / 60
    if max(plan['projected_minutes']) > top:
        slowest = max(items, key=lambda it: it.work, default=None)
        note = (
            t'{where}: projects {max(plan["projected_minutes"])} min, '
            t'past {top:.1f} min, the top of the band around the {target_minutes:g}-min target'
        )
        if slowest:
            note += t', {slowest.unit:.200} alone {slowest.work / 60:.0f} min'
        if not shardable:
            note += t', and it cannot be sharded'
        warnings.append(note)
    return plan, warnings


def one_job(e: dict, history: dict | None) -> dict:
    """The run plan of an entry with no usable sizing: one job on its config's own patterns."""
    return {
        **{k: e[k] for k in ('repo', 'dir', 'sha')},
        'break': None,
        'workers': None,
        'shards': 1,
        'files': None,
        'mutants': None,
        'projected_minutes': [None],
        'history': history,
    }


def plan_entry(
    e: dict, plans_dir: Path, target_minutes: float, history_dir: Path
) -> tuple[dict, list[Template]] | None:
    """E's run plan and its warnings, or None when E is not enrolled.

    A refused history falls back to the plan pass's prices, and a missing or
    refused plan to one job on the config's own patterns, which tests the
    entry all the same.
    """
    key, where = e['key'], f'{e["repo"]}:{e["dir"]}'
    try:
        history = read_history(history_dir / f'{key}.json')
    except Exception as exc:
        mutation_cost.annotate(
            'warning',
            t'{where}: its cost history failed to load ({mutation_cost.describe(exc)}), so it is '
            t'priced from its plan pass',
        )
        history = None
    path = plans_dir / f'{key}.json'
    try:
        doc = mutation_cost.load_json(path) if path.is_file() else None
    except Exception as exc:
        return one_job(e, history), [
            t'{where}: its plan failed to load ({mutation_cost.describe(exc)}), so it runs in one job'
        ]
    if (
        isinstance(doc, dict)
        and doc.get('skipped') is True
        and doc.get('repo') == e['repo']
        and doc.get('dir') == e['dir']
    ):
        mutation_cost.say(t'{where}: not enrolled, so it does not run')
        return None
    if why := 'no plan' if doc is None else valid_sizing(doc, e):
        return one_job(e, history), [t'{where}: {why}, so it runs in one job']
    return assign(doc, target_minutes, history)


def matrix(
    entries: list[dict], plans_dir: Path, target_minutes: float, out_plans: Path, history_dir: Path
) -> tuple[list[dict], list[dict]]:
    """One repository's run and merge matrices; each planned entry's run plan lands in OUT_PLANS/<key>.json.

    ENTRIES is the discover job's list; each entry holds at most MAX_SHARDS
    shards and discover at most MAX_SHARDS entries, so the run matrix fits 256 jobs.
    """
    out_plans.mkdir(parents=True, exist_ok=True)
    run, merge = [], []
    for e in entries:
        if not isinstance(e, dict) or not all(isinstance(e.get(f), str) for f in ENTRY_FIELDS):
            raise SystemExit(f'stryker-shard: entry {e!r:.200} is not one discover wrote')
        key, where = mutation_cost.entry_key(e['repo'], e['dir']), f'{e["repo"]}:{e["dir"]}'
        if key != e['key']:
            raise SystemExit(f'stryker-shard: entry {where!r:.200} has key {e["key"]!r:.200}')
        if (planned := plan_entry(e, plans_dir, target_minutes, history_dir)) is None:
            continue
        plan, warnings = planned
        (out_plans / f'{key}.json').write_text(json.dumps(plan, indent=1) + '\n')
        for w in warnings:
            mutation_cost.annotate('warning', w)
        if plan['workers'] is not None:
            mutation_cost.say(
                t'{where}: {plan["shards"]} shard(s), projected {plan["projected_minutes"]} min'
            )
        k = plan['shards']
        row = {f: e[f] for f in ENTRY_FIELDS}
        run += [{**row, 'shard': n, 'shards': k} for n in range(1, k + 1)]
        names = [f'strykershard-{key}-{n}' for n in range(1, k + 1)]
        merge.append(
            {**row, 'shards': k, 'artifacts': names[0] if k == 1 else '{' + ','.join(names) + '}'}
        )
    return run, merge


def mutate_list(plan: dict, shard: int) -> str:
    """Shard SHARD's --mutate value; empty for an entry that runs whole on its config's patterns."""
    if not isinstance(plan.get('shards'), int) or not 1 <= shard <= plan['shards']:
        raise SystemExit(
            f"stryker-shard: shard {shard} is outside the plan's 1..{plan.get('shards')}"
        )
    files = plan.get('files')
    if plan['shards'] == 1 or not isinstance(files, dict):
        return ''
    mine = sorted(p for p, f in files.items() if f.get('shard') == shard)
    if not mine or not all(SAFE_PATH.fullmatch(p) for p in mine):
        raise SystemExit(f'stryker-shard: shard {shard} has no file list it can pass to --mutate')
    return ','.join(mine)


# --- costs -----------------------------------------------------------------


def costs(times: Path) -> dict:
    """One shard's measured cost per file from the reporter's CI_STRYKER_TIMES record."""
    header: dict[str, float] = {}
    events = []
    for row in times.read_text(errors='replace').splitlines():
        stamp, _, value = row.partition('\t')
        try:
            number = float(value if stamp.startswith('#') else stamp)
        except ValueError:
            continue
        if stamp.startswith('#'):
            header.setdefault(stamp, number)
        elif value:
            events.append((number / 1000, value))
    workers = int(header.get('#workers', 0))
    if workers < 1 or '#launch' not in header or '#start' not in header:
        raise ValueError(f'{times} has no worker count, launch or start row')
    start = header['#start'] / 1000
    return {
        'setup_secs': round(max(0.0, start - header['#launch'] / 1000) + JOB_OVERHEAD_SECS, 1),
        'files': mutation_cost.attribute(events, start, workers),
    }


# --- merge -----------------------------------------------------------------


def shard_results(shards_dir: Path, repo: str, dir_: str) -> dict[int, tuple[dict, Path]]:
    """Each shard's meta.json and directory, bound by the meta's repo and dir."""
    out = {}
    for meta_path in sorted(shards_dir.rglob('meta.json')) if shards_dir.is_dir() else []:
        meta = read_json(meta_path)
        if not isinstance(meta, dict) or (meta.get('repo'), meta.get('dir')) != (repo, dir_):
            continue
        n = meta.get('shard')
        if isinstance(n, int) and not isinstance(n, bool) and n not in out:
            out[n] = (meta, meta_path.parent)
    return out


def merge_reports(reports: dict[int, dict], owner: dict[str, int] | None) -> dict:
    """One report from each shard's, every file from the one shard that owns it.

    The target's code wrote each shard's report, so a file another shard owns
    is not read from it. A lone shard's report is returned as Stryker wrote it;
    otherwise mutant and test ids are prefixed with their shard and a test the
    shards share keeps one id.
    """
    if len(reports) == 1 and owner is None:
        return next(iter(reports.values()))
    first = reports[min(reports)]
    merged = {k: v for k, v in first.items() if k not in ('files', 'testFiles')}
    merged['files'], merged['testFiles'] = {}, {}
    test_id: dict[tuple, str] = {}
    for n in sorted(reports):
        rep = reports[n]
        local: dict[str, str] = {}
        for tf_name, tf in (rep.get('testFiles') or {}).items():
            out_tf = merged['testFiles'].setdefault(
                tf_name, {**{k: v for k, v in tf.items() if k != 'tests'}, 'tests': []}
            )
            for t in tf.get('tests') or []:
                loc = (t.get('location') or {}).get('start') or {}
                k = (tf_name, t.get('name'), loc.get('line'), loc.get('column'))
                if k not in test_id:
                    test_id[k] = f'{n}-{t.get("id")}'
                    out_tf['tests'].append({**t, 'id': test_id[k]})
                local[str(t.get('id'))] = test_id[k]
        for name, f in (rep.get('files') or {}).items():
            if owner is not None and owner.get(name) != n:
                continue
            merged['files'][name] = {
                **f,
                'mutants': [
                    {
                        **m,
                        'id': f'{n}-{m.get("id")}',
                        **{
                            k: [local.get(str(i), f'{n}-{i}') for i in m[k]]
                            for k in ('coveredBy', 'killedBy')
                            if isinstance(m.get(k), list)
                        },
                    }
                    for m in f.get('mutants') or []
                ],
            }
    return merged


def resolved_break(report: dict, planned: object) -> object:
    """The report's thresholds.break, which Stryker writes from its resolved options
    whatever the config's format (null for none), else the PLANNED one from stryker.config.json."""
    thresholds = report.get('thresholds')
    if isinstance(thresholds, dict) and 'break' in thresholds:
        value = thresholds['break']
        if value is None or mutation_cost.nonnegative(value):
            return value
    return planned


def write_history(
    out: Path, setups: list[dict], plan: dict, measured: dict, keep: set[str] | None
) -> None:
    """OUT/costs.json: this run's measure of each file, else its last one, limited to KEEP when given."""
    out.mkdir(parents=True, exist_ok=True)
    planned = plan.get('mutants') if isinstance(plan.get('mutants'), dict) else None
    history = mutation_cost.carry_forward(
        {**mutation_cost.combine(setups), 'files': measured},
        plan.get('history'),
        keep=keep,
        planned=planned,
    )
    (out / 'costs.json').write_text(json.dumps(history, indent=1) + '\n')


def merge(plan: dict, shards_dir: Path, stage: Path, costs_out: Path) -> int:
    """Stage the merged report and score, and write the next cost history; 1 when the entry publishes nothing."""
    repo, dir_, k = plan['repo'], plan['dir'], plan['shards']
    where = f'{repo}:{dir_}'
    if note := mutation_cost.cap_note(where, plan):
        mutation_cost.annotate('warning', note, summary=True)
    found = shard_results(shards_dir, repo, dir_)
    if found and all(meta.get('skipped') is True for meta, _ in found.values()):
        mutation_cost.annotate(
            'error',
            t'{where}: not enrolled, but its plan did not reach the shard job, so nothing was tested',
        )
        return 1
    files = plan.get('files') if isinstance(plan.get('files'), dict) else None
    owner = {p: f['shard'] for p, f in files.items()} if files else None
    reports, measured, setups, problems = {}, {}, [], []
    for n in range(1, k + 1):
        meta, d = found.get(n, (None, None))
        # A report carries every mutated file's source, so its size is the entry's, and only it reads it.
        report = read_json(d / 'result.json', None) if d else None
        if isinstance(report, dict) and isinstance(report.get('files'), dict):
            reports[n] = report
        rec = read_json(d / 'costs.json') if d else None
        if isinstance(rec, dict):
            mine = mutation_cost.clean_files(rec.get('files'))
            measured.update({p: e for p, e in mine.items() if owner is None or owner.get(p) == n})
            setups.append({'setup_secs': rec.get('setup_secs')})
        if meta is None or not isinstance(report, dict):
            problems.append(f'shard {n} has no result')
        elif meta.get('complete') is not True:
            problems.append(f'shard {n} did not finish')
        elif owner is not None:
            missing = [
                p
                for p, f in files.items()
                if f['shard'] == n and f['run'] + f['early'] and p not in report['files']
            ]
            if missing:
                problems.append(
                    f'shard {n} has no result for {missing[0]!r:.200} and {len(missing) - 1} more'
                )
    if problems:
        mutation_cost.annotate(
            'error', t'{where}: {", ".join(problems)}, so this week publishes no score for it'
        )
        mutation_cost.summarize(t'{where}: no score this week ({", ".join(problems)})')
        reported = {p for rep in reports.values() for p in rep['files']}
        write_history(costs_out, setups, plan, measured, set(files or reported) or None)
        return 1
    merged = merge_reports(reports, owner)
    stage.mkdir(parents=True, exist_ok=True)
    (stage / 'merged.json').write_text(json.dumps(merged))
    _, counts = stryker_aggregate.load_report(stage / 'merged.json')
    (stage / 'merged.json').unlink()
    detected = sum(counts.get(s, 0) for s in stryker_aggregate.DETECTED)
    valid = detected + sum(counts.get(s, 0) for s in stryker_aggregate.UNDETECTED)
    if detected == 0 and valid > 0:
        mutation_cost.annotate(
            'error',
            t'{where}: killed 0 of {valid} mutants, so the harness never activated one: '
            t'nothing is published or recorded',
        )
        mutation_cost.summarize(t'{where}: no score this week (killed 0 of {valid} mutants)')
        return 1
    write_history(costs_out, setups, plan, measured, set(files or merged['files']))
    score = f'{detected / valid * 100:.1f}' if valid else '0.0'
    staged = json.loads(json.dumps(merged))
    for f in staged['files'].values():
        f.pop('source', None)
    for f in (staged.get('testFiles') or {}).values():
        f.pop('source', None)
    (stage / 'mutation.json').write_text(json.dumps(staged))
    (stage / 'meta.json').write_text(json.dumps({'repo': repo, 'dir': dir_, 'score': score}))
    breaking = resolved_break(merged, plan.get('break'))
    below = breaking is not None and valid > 0 and detected / valid * 100 < float(breaking)
    (stage / 'below.txt').write_text('true' if below else 'false')
    line = t'{where}: mutation score {score}% over {k} shard(s), {detected} of {valid} detected'
    mutation_cost.say(line)
    mutation_cost.summarize(line)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest='command', required=True)
    di = sub.add_parser('discover')
    di.add_argument('--repo', required=True)
    di.add_argument('--sha', required=True)
    di.add_argument('--root', required=True, type=Path, help="the repository's checkout")
    di.add_argument('--dir', default='', help='test this one package dir alone')
    di.add_argument(
        '--honour-opt-out', action='store_true', help='skip a dir whose sidecar opts out'
    )
    pl = sub.add_parser('plan')
    for a in ('--repo', '--dir', '--sha'):
        pl.add_argument(a, required=True)
    pl.add_argument('--config', required=True, type=Path)
    pl.add_argument(
        '--pass', dest='plan_pass', type=Path, help="the reporter's CI_STRYKER_PLAN file"
    )
    pl.add_argument('--skipped', action='store_true', help='the entry is not enrolled')
    pl.add_argument('--out', required=True, type=Path)
    mx = sub.add_parser('matrix')
    mx.add_argument('--entries', required=True, help="the discover job's JSON entry list")
    mx.add_argument('--plans', required=True, type=Path, help="each entry's sizing as <key>.json")
    mx.add_argument('--target-minutes', required=True, type=float)
    mx.add_argument(
        '--history', required=True, type=Path, help="each entry's last cost history as <key>.json"
    )
    mx.add_argument('--out-plans', required=True, type=Path)
    mu = sub.add_parser('mutate')
    mu.add_argument('--plan', required=True, type=Path)
    mu.add_argument('--shard', required=True, type=int)
    co = sub.add_parser('costs')
    co.add_argument('--times', required=True, type=Path)
    co.add_argument('--out', required=True, type=Path)
    me = sub.add_parser('merge')
    me.add_argument('--plan', required=True, type=Path)
    me.add_argument('--shards', required=True, type=Path)
    me.add_argument('--stage', required=True, type=Path)
    me.add_argument('--costs', required=True, type=Path)
    args = p.parse_args()

    if args.command == 'discover':
        found = discover(
            args.repo, args.sha, args.root, args.dir, honour_opt_out=args.honour_opt_out
        )
        print(json.dumps(found, separators=(',', ':')))
        if not found['entries']:
            mutation_cost.annotate(
                'notice', t'{args.repo}: no stryker entry to test this week', summary=True
            )
    elif args.command == 'plan':
        if args.skipped:
            doc = {'repo': args.repo, 'dir': args.dir, 'sha': args.sha, 'skipped': True}
        else:
            doc = sizing(
                args.repo,
                args.dir,
                args.sha,
                read_json(args.config),
                read_json(args.plan_pass) if args.plan_pass else None,
            )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=1) + '\n')
    elif args.command == 'matrix':
        entries = json.loads(args.entries)
        if not isinstance(entries, list):
            raise SystemExit(
                f"--entries must be the discover job's entry list, got {args.entries!r:.200}"
            )
        run, merge_ = matrix(entries, args.plans, args.target_minutes, args.out_plans, args.history)
        print(json.dumps({'run': run, 'merge': merge_}, separators=(',', ':')))
    elif args.command == 'mutate':
        print(mutate_list(json.loads(args.plan.read_text()), args.shard))
    elif args.command == 'costs':
        args.out.write_text(json.dumps(costs(args.times)) + '\n')
    else:
        return merge(json.loads(args.plan.read_text()), args.shards, args.stage, args.costs)
    return 0


if __name__ == '__main__':
    sys.exit(mutation_cost.run_main(main))
