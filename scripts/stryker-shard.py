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
import collections
import importlib.util
import json
import math
import re
import subprocess
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
# freeCheckers() hands those back once checking ends. The prior still ran 2.69x
# low on marotte's store.ts, so plans scale it by mutation_cost.PRIOR_ERROR.
PRIOR_OVERHEAD_SECS = 0.96
# The run job's steps outside the Stryker process (checkout, clone, npm ci,
# upload) took 40 s to 1 min in run 42418943692.
JOB_OVERHEAD_SECS = 90
# A conservative allowlist: minimatch's specials and the --mutate list's comma
# are outside it, so a file outside it keeps its entry in one job.
SAFE_PATH = re.compile(r'[A-Za-z0-9._/-]+')
# A line range of one file, as --mutate takes it: Stryker mutates a node only
# when one range wholly includes its lines (stryker-js instrumenter
# babel-transformer.ts shouldMutate), so ranges cut where no mutant spans the cut.
RANGE = re.compile(r'(?P<file>[A-Za-z0-9._/-]+):(?P<first>[1-9][0-9]*)-(?P<last>[1-9][0-9]*)')
SHA = re.compile(r'[0-9a-f]{40}')
BLOB = mutation_cost.GIT_BLOB
# A comment Stryker reads as a directive (stryker-js 10.1.1 instrumenter
# directive-bookkeeper.js strykerCommentDirectiveRegex, matched on the comment's
# value). It reads one only from a node it enters, and a line range skips every
# subtree outside it (babel-transformer.js shouldSkip), so a ranged shard also
# passes each file-scope directive's opener as a range: that enters the node the
# comment leads, and two columns of comment wholly include no node to mutate.
DIRECTIVE = re.compile(r'(//|/\*)\s?Stryker (disable|restore)(?: (next-line))? [a-zA-Z, ]+')
OPENER = re.compile(
    r'(?P<file>[A-Za-z0-9._/-]+):(?P<line>[1-9][0-9]*):(?P<col>[0-9]+)-(?P=line):[0-9]+'
)
# Past this many directives a file's openers could outgrow one --mutate argument, so it runs whole.
MAX_DIRECTIVES = 100
LINE_BREAK = re.compile('\r\n|[\n\r\u2028\u2029]')
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


def spans_of(mutants: object, spread: float) -> list[list] | None:
    """A plan pass file's mutants as [first line, last line, prior worker-seconds, None for early], or None."""
    if not isinstance(mutants, list):
        return None
    out = []
    for m in mutants:
        if not (isinstance(m, list) and len(m) == 3 and all(mutation_cost.count(v) for v in m[:2])):
            return None
        first, last, net = m
        if not 1 <= first <= last or not (net is None or mutation_cost.nonnegative(net)):
            return None
        prior = None if net is None else round(float(net) / 1000 * spread + PRIOR_OVERHEAD_SECS, 3)
        out.append([first, last, prior])
    return out


def blobs(root: Path, paths: list[str]) -> dict[str, str]:
    """Each of PATHS' git blob at ROOT's HEAD, the version a range's measured cost belongs to."""
    if not paths:
        return {}
    proc = subprocess.run(
        ['git', '-C', str(root), 'ls-tree', '-z', 'HEAD', '--', *paths],
        capture_output=True,
        check=False,
    )
    out = {}
    for row in proc.stdout.split(b'\0') if proc.returncode == 0 else []:
        meta, _, name = row.partition(b'\t')
        fields = meta.split()
        if len(fields) == 3 and fields[1] == b'blob':
            out[name.decode(errors='replace')] = fields[2].decode()
    return {p: b for p, b in out.items() if p in paths and BLOB.fullmatch(b)}


def valid_ids(ids: object, spans: list[list]) -> bool:
    """IDS is one [start line, column, end line, column, mutator, early status, reason] per span,
    early exactly where the span has no prior."""
    return (
        isinstance(ids, list)
        and len(ids) == len(spans)
        and all(
            isinstance(i, list)
            and len(i) == 7
            and all(mutation_cost.count(v) and v >= 1 for v in i[:4])
            and isinstance(i[4], str)
            and (i[5] is None) == (sp[2] is not None)
            and (i[5] is None or isinstance(i[5], str))
            and (i[6] is None or isinstance(i[6], str))
            for i, sp in zip(ids, spans, strict=True)
        )
    )


def openers(text: str) -> list[list[int]]:
    """Each file-scope directive comment's [line, column] in TEXT: 1-based lines, 0-based UTF-16
    columns as Babel counts them. A match inside a string is kept; its range wholly includes no node."""
    starts = [0, *(m.end() for m in LINE_BREAK.finditer(text))]
    out, line = [], 0
    for m in DIRECTIVE.finditer(text):
        if m[3]:
            continue
        while line + 1 < len(starts) and starts[line + 1] <= m.start():
            line += 1
        out.append([line + 1, len(text[starts[line] : m.start()].encode('utf-16-le')) // 2])
    return out


def directives(root: Path, versions: dict[str, str]) -> dict[str, list[list[int]]]:
    """Each file's directive openers in its blob from VERSIONS, read from ROOT's object store."""
    if not versions:
        return {}
    proc = subprocess.run(
        ['git', '-C', str(root), 'cat-file', '--batch'],
        input=''.join(f'{b}\n' for b in versions.values()).encode(),
        capture_output=True,
        check=False,
    )
    out, data, pos = {}, proc.stdout if proc.returncode == 0 else b'', 0
    for path in versions:
        head, _, _ = data[pos:].partition(b'\n')
        fields = head.split()
        if len(fields) != 3 or fields[1] != b'blob':
            break
        size = int(fields[2])
        start = pos + len(head) + 1
        out[path] = openers(data[start : start + size].decode(errors='replace'))
        pos = start + size + 1
    return out


def sizing(
    repo: str,
    dir_: str,
    sha: str,
    config: object,
    plan_pass: object,
    versions: dict | None = None,
    marks: dict | None = None,
) -> dict:
    """The entry's sizing from the plan pass; untrusted, because the target's code wrote the pass.

    Each file carries its mutants' spans, which ranges cut between, their ids,
    which the merge checks a range's results against, its git blob from
    VERSIONS, which a range's measured cost is valid for, and its directive
    openers from MARKS, which a ranged shard also passes to --mutate.
    """
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
        spans = spans_of(f.get('mutants') if isinstance(f, dict) else None, spread)
        if (
            spans is not None
            and len(spans) == run + early
            and sum(s[2] is not None for s in spans) == run
        ):
            doc['files'][path]['spans'] = spans
            ids = f.get('ids')
            marked = (marks or {}).get(path)
            if valid_ids(ids, spans) and isinstance(marked, list) and len(marked) <= MAX_DIRECTIVES:
                doc['files'][path]['ids'] = ids
                doc['files'][path]['directives'] = marked
        if isinstance(blob := (versions or {}).get(path), str) and BLOB.fullmatch(blob):
            doc['files'][path]['blob'] = blob
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
    for path, f in files.items():
        if 'blob' in f and not (isinstance(f['blob'], str) and BLOB.fullmatch(f['blob'])):
            return f'file {path!r:.200} has blob {f["blob"]!r:.200}'
        if 'spans' in f and not valid_spans(f['spans'], f['run'], f['early']):
            return f'file {path!r:.200} has spans that are not its run and early mutants'
        if ('ids' in f or 'directives' in f) and not (
            'spans' in f
            and valid_ids(f.get('ids'), f['spans'])
            and valid_directives(f.get('directives'))
        ):
            return f'file {path!r:.200} has ids or directives that are not its mutants and comments'
    if doc.get('shardable') is True and (
        bad := next((p for p in files if not SAFE_PATH.fullmatch(p)), None)
    ):
        return f'it is shardable with file {bad!r:.200}, which --mutate cannot take'
    return ''


def valid_spans(spans: object, run: int, early: int) -> bool:
    """SPANS is one [first, last, prior or None] per mutant, RUN of them priced and EARLY not."""
    return (
        isinstance(spans, list)
        and len(spans) == run + early
        and all(
            isinstance(m, list)
            and len(m) == 3
            and mutation_cost.count(m[0])
            and mutation_cost.count(m[1])
            and 1 <= m[0] <= m[1]
            and (m[2] is None or mutation_cost.nonnegative(m[2]))
            for m in spans
        )
        and sum(m[2] is not None for m in spans) == run
    )


def valid_directives(marks: object) -> bool:
    """MARKS is at most MAX_DIRECTIVES [line, column] openers."""
    return (
        isinstance(marks, list)
        and len(marks) <= MAX_DIRECTIVES
        and all(
            isinstance(m, list)
            and len(m) == 2
            and mutation_cost.count(m[0])
            and m[0] >= 1
            and mutation_cost.count(m[1])
            for m in marks
        )
    )


def read_history(path: Path) -> dict | None:
    """The entry's last cost history from the shard job's own fetch, or None when it has none.

    Raises what mutation_cost.load_json raises, and ValueError for a value that is not a cost record.
    """
    if not path.is_file():
        return None
    doc = mutation_cost.load_json(path)
    if why := mutation_cost.record_fault(doc):
        raise ValueError(why)
    return {**mutation_cost.carry_forward({}, doc), 'blobs': dict(doc.get('blobs') or {})}


def file_of(unit: str) -> str:
    """The file a unit (a file, or one of its line ranges) belongs to."""
    m = RANGE.fullmatch(unit)
    return m['file'] if m else unit


def lines_of(unit: str) -> tuple[float, float]:
    """A unit's first and last line; a whole file spans every line."""
    m = RANGE.fullmatch(unit)
    return (int(m['first']), int(m['last'])) if m else (1, float('inf'))


def overlaps(u: str, v: str) -> bool:
    """Whether units U and V share a line of one file."""
    (a, b), (c, d) = lines_of(u), lines_of(v)
    return file_of(u) == file_of(v) and a <= d and c <= b


def by_file(history: dict | None) -> dict | None:
    """HISTORY with each split file's range rows summed into one row per file, which project() prices."""
    if not isinstance(history, dict):
        return history
    files: dict[str, dict] = {}
    for unit, e in mutation_cost.clean_files(history.get('files')).items():
        f = files.setdefault(file_of(unit), {'secs': 0.0, 'mutants': 0})
        f['secs'] += e['secs']
        f['mutants'] += e['mutants']
    return {**history, 'files': files}


def weights(path: str, f: dict, history: dict | None) -> list[float]:
    """Each of PATH's mutants' share of its price: its prior, or, for an unchanged file with
    measured ranges, its old range's measured seconds spread over that range by prior."""
    spans = f['spans']
    prior = [s[2] or 0.0 for s in spans]
    hist = history or {}
    if not f.get('blob') or (hist.get('blobs') or {}).get(path) != f['blob']:
        return prior
    rows = [
        (*lines_of(u), e['secs'])
        for u, e in mutation_cost.clean_files(hist.get('files')).items()
        if file_of(u) == path and RANGE.fullmatch(u)
    ]
    inside: list[int | None] = []
    for first, last, _ in spans:
        inside.append(next((i for i, r in enumerate(rows) if r[0] <= first and last <= r[1]), None))
    held = [sum(p for p, i in zip(prior, inside, strict=True) if i == n) for n in range(len(rows))]
    measured = [r[2] / held[n] if held[n] > 0 else None for n, r in enumerate(rows)]
    rates = [m for m in measured if m is not None]
    mean = sum(rates) / len(rates) if rates else 1.0
    return [
        p * (measured[i] if i is not None and measured[i] is not None else mean)
        for p, i in zip(prior, inside, strict=True)
    ]


def atoms(spans: list[list], costs: list[float]) -> list[list]:
    """The spans joined wherever one mutant's lines meet another's, as [first, last, run, early, cost].

    No legal cut falls inside an atom: a cut between two lines one mutant spans
    would leave that mutant in no range.
    """
    out: list[list] = []
    for (first, last, prior), cost in sorted(
        zip(spans, costs, strict=True), key=lambda x: x[0][:2]
    ):
        run, early = (1, 0) if prior is not None else (0, 1)
        if out and first <= out[-1][1]:
            a = out[-1]
            a[1], a[2], a[3], a[4] = max(a[1], last), a[2] + run, a[3] + early, a[4] + cost
        else:
            out.append([first, last, run, early, cost])
    return out


def cut(groups: list[list], pieces: int) -> list[list]:
    """GROUPS (atoms in line order) as at most PIECES contiguous ranges, the largest as small as one can be."""

    def fill(bound: float) -> list[list]:
        out: list[list] = []
        for g in groups:
            if out and out[-1][4] + g[4] <= bound:
                o = out[-1]
                o[1], o[2], o[3], o[4] = g[1], o[2] + g[2], o[3] + g[3], o[4] + g[4]
            else:
                out.append(list(g))
        return out

    low, high = max(g[4] for g in groups), sum(g[4] for g in groups)
    for _ in range(60):
        mid = (low + high) / 2
        if len(fill(mid)) <= pieces:
            high = mid
        else:
            low = mid
    return fill(high)


def split(
    path: str, f: dict, work: float, room: float, history: dict | None
) -> tuple[list[tuple], float]:
    """PATH as (unit, run, early, work) pieces of at most ROOM seconds where its atoms allow, and
    its largest atom's seconds; one whole-file piece when it fits ROOM, or has no spans to cut or
    no ids to check its ranges' results against."""
    if work <= room or room <= 0 or not f.get('spans') or 'ids' not in f:
        return [(path, f['run'], f['early'], work)], 0.0
    w = weights(path, f, history)
    total = sum(w)
    costs = [work * x / total if total > 0 else work / len(w) for x in w]
    groups = atoms(f['spans'], costs)
    # cut() minimises the largest piece for a given count only: uneven atoms can need
    # more pieces than ceil(work / room) before every piece fits.
    best: list[list] = []
    for pieces in range(
        min(mutation_cost.MAX_SHARDS, len(groups), max(1, -int(-work // room))),
        min(mutation_cost.MAX_SHARDS, len(groups)) + 1,
    ):
        tried = cut(groups, pieces)
        if not best or max(g[4] for g in tried) < max(g[4] for g in best):
            best = tried
        if max(g[4] for g in best) <= room:
            break
    ranges = [(f'{path}:{a}-{b}', run, early, c) for a, b, run, early, c in best]
    return ranges, max(g[4] for g in groups)


def assign(
    doc: dict, history: dict | None, limit: int = mutation_cost.MAX_SHARDS
) -> tuple[dict, list[Template]]:
    """The entry's run plan, its files priced from HISTORY, any file past one shard split into
    line ranges, packed into at most LIMIT shards, and its warnings."""
    workers = doc['workers']
    where = f'{doc["repo"]}:{doc["dir"]}'
    setup = mutation_cost.setup_price((history or {}).get('setup_secs'), doc['setup_secs'])
    top = mutation_cost.TOP_SECS
    priced = mutation_cost.project(
        {p: (f['run'], f['prior_secs']) for p, f in doc['files'].items()}, by_file(history)
    )
    warnings: list[Template] = []
    if refused := mutation_cost.refusals(priced):
        warnings.append(
            t'{where}: {refused} measured price(s) more than {mutation_cost.PRICE_FACTOR}x from '
            t"the plan pass's were refused, so those files are priced from it"
        )
    shardable = doc.get('shardable') is True and bool(doc['files'])
    units: dict[str, tuple[str, int, int]] = {}
    priors: dict[str, float] = {}
    items = []
    for path, f in doc['files'].items():
        work = priced[path][0] / workers
        pieces, atom = (
            split(path, f, work, top - setup, history)
            if shardable
            else ([(path, f['run'], f['early'], work)], 0.0)
        )
        if atom > top - setup:
            warnings.append(
                t"{where}: {path:.200} holds one mutant's span of {atom / 60:.0f} min, past a "
                t"shard's {(top - setup) / 60:.0f} min of work, and no range cut shortens it"
            )
        elif shardable and work > top - setup and f.get('spans') and 'ids' not in f:
            warnings.append(
                t"{where}: {path:.200} projects {work / 60:.0f} min, past a shard's "
                t'{(top - setup) / 60:.0f} min of work, but its plan pass gave no mutant ids or '
                t'more than {MAX_DIRECTIVES} Stryker directives, so it runs whole'
            )
        for unit, run, early, w in pieces:
            units[unit] = (path, run, early)
            items.append(mutation_cost.Item('.', unit, w))
            if priced[path][1] in ('prior', 'refused'):
                # A prior-priced file's pieces are cut by prior weight, so each holds its share of work.
                priors[unit] = f['prior_secs'] * (w / work if work > 0 else 1.0)
    one_job = setup + sum(it.work for it in items)
    k, bins = (
        mutation_cost.choose(items, {'.': setup}, top, limit)
        if shardable
        else (1, mutation_cost.pack(items, 1, {'.': setup}))
    )
    files = {}
    for n, b in enumerate(bins, start=1):
        for it in b.items:
            path, run, early = units[it.unit]
            files[it.unit] = {
                'shard': n,
                'run': run,
                'early': early,
                'work_secs': round(it.work, 1),
                'cost': priced[path][1],
            }
            if it.unit in priors:
                files[it.unit]['prior_secs'] = round(priors[it.unit], 3)
    plan = {k_: doc[k_] for k_ in ('repo', 'dir', 'sha', 'break', 'workers')}
    split_files = sorted({file_of(u) for u in files if RANGE.fullmatch(u)}) if k > 1 else []
    plan.update(
        shards=k,
        files=files if k > 1 else None,
        mutants={u: run for u, (_, run, _) in units.items()},
        ids={p: doc['files'][p]['ids'] for p in split_files},
        directives={p: doc['files'][p]['directives'] for p in split_files},
        blobs={p: f['blob'] for p, f in doc['files'].items() if f.get('blob')},
        one_job_minutes=round(one_job / 60, 1),
        projected_minutes=[round(b.load / 60, 1) for b in bins],
        top_minutes=round(top / 60, 1),
        history=history,
    )
    if shardable and mutation_cost.capped(k, bins, items, {'.': setup}, top, limit):
        plan['capped'] = mutation_cost.cap_reason(limit)
        return plan, [*warnings, mutation_cost.cap_note(where, plan)]
    if max(plan['projected_minutes']) > plan['top_minutes']:
        slowest = max(items, key=lambda it: it.work, default=None)
        note = (
            t'{where}: projects {max(plan["projected_minutes"])} min, '
            t'past {plan["top_minutes"]:.1f} min, the top of its band'
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


def plan_entry(e: dict, plans_dir: Path, history_dir: Path) -> tuple[dict, list[Template]] | None:
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
    return assign(doc, history)


def matrix(
    entries: list[dict], plans_dir: Path, out_plans: Path, history_dir: Path
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
        if (planned := plan_entry(e, plans_dir, history_dir)) is None:
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
    marks = plan.get('directives') if isinstance(plan.get('directives'), dict) else {}
    split_here = sorted({file_of(p) for p in mine if RANGE.fullmatch(p)})
    mine += [
        f'{path}:{m[0]}:{m[1]}-{m[0]}:{m[1] + 2}'
        for path in split_here
        if valid_directives(marks.get(path, []))
        for m in marks.get(path, [])
    ]
    if not mine or not all(
        SAFE_PATH.fullmatch(p) or RANGE.fullmatch(p) or OPENER.fullmatch(p) for p in mine
    ):
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
    """One report from each shard's, every file from the shards that own it or its line ranges.

    The target's code wrote each shard's report, so a file another shard owns
    is not read from it; a split file joins the mutants of each shard owning
    one of its ranges, which range_faults has checked. A lone shard's report is
    returned as Stryker wrote it; otherwise mutant and test ids are prefixed
    with their shard and a test the shards share keeps one id.
    """
    if len(reports) == 1 and owner is None:
        return next(iter(reports.values()))
    holders: dict[str, set[int]] = {}
    for unit, n in (owner or {}).items():
        holders.setdefault(file_of(unit), set()).add(n)
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
            if owner is not None and n not in holders.get(name, ()):
                continue
            joined = merged['files'].setdefault(name, {**f, 'mutants': []})
            joined['mutants'] += [
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
            ]
    return merged


def mutant_id(m: object) -> tuple | None:
    """A reported mutant's id as the plan pass writes it, without its status, or None."""
    loc = m.get('location') if isinstance(m, dict) else None
    start, end = (loc or {}).get('start') or {}, (loc or {}).get('end') or {}
    key = (start.get('line'), start.get('column'), end.get('line'), end.get('column'))
    if not all(isinstance(v, int) for v in key) or not isinstance(m.get('mutatorName'), str):
        return None
    return (*key, m['mutatorName'])


def range_faults(reports: dict[int, dict], files: dict, ids: dict | None = None) -> list[str]:
    """Why the shards' reports of each split file are not exactly its mutants, each once, or [].

    A shard's mutants of a file must lie in its own ranges of it and number
    what the plan pass counted there, no two shards may report one mutant, and
    together they must be the plan pass's mutants of the file (IDS), none
    ignored that the plan pass runs: a range never decides a mutant's status.
    """
    faults, seen = [], {}
    ranges: dict[tuple[str, int], list[tuple[int, int, int]]] = {}
    for unit, f in files.items():
        if m := RANGE.fullmatch(unit):
            ranges.setdefault((m['file'], f['shard']), []).append(
                (int(m['first']), int(m['last']), f['run'] + f['early'])
            )
    found: dict[str, list[tuple[int, dict]]] = {}
    for (path, n), mine in sorted(ranges.items()):
        got = ((reports.get(n) or {}).get('files') or {}).get(path)
        mutants = got.get('mutants') if isinstance(got, dict) else None
        if not isinstance(mutants, list):
            faults.append(f'shard {n} has no result for {path!r:.200}')
            continue
        if len(mutants) != sum(c for *_, c in mine):
            faults.append(
                f'shard {n} reports {len(mutants)} mutants in its ranges of {path!r:.200}, '
                f'not the {sum(c for *_, c in mine)} its plan counted'
            )
        for m in mutants:
            loc = m.get('location') if isinstance(m, dict) else None
            start, end = (loc or {}).get('start') or {}, (loc or {}).get('end') or {}
            first, last = start.get('line'), end.get('line')
            if not (
                isinstance(first, int)
                and isinstance(last, int)
                and any(a <= first and last <= b for a, b, _ in mine)
            ):
                faults.append(f'shard {n} reports a mutant of {path!r:.200} outside its ranges')
                break
            key = (
                path,
                first,
                start.get('column'),
                last,
                end.get('column'),
                m.get('mutatorName'),
                m.get('replacement'),
            )
            if seen.setdefault(key, n) != n:
                faults.append(f'shards {seen[key]} and {n} both report one mutant of {path!r:.200}')
                break
            found.setdefault(path, []).append((n, m))
    if faults:
        return faults
    for path in sorted({p for p, _ in ranges}):
        faults += plan_faults(path, (ids or {}).get(path), found.get(path, []))
    return faults


def plan_faults(path: str, rows: object, found: list[tuple[int, dict]]) -> list[str]:
    """Why FOUND, the (shard, mutant) results of PATH's ranges, are not the plan pass's ROWS, or []."""
    if not isinstance(rows, list) or not all(
        isinstance(r, list) and len(r) == 7 and r[5] in (None, 'Ignored') for r in rows
    ):
        return [f'the plan holds no mutant ids for {path!r:.200}']
    want = collections.Counter(tuple(r[:5]) for r in rows)
    runs = {tuple(r[:5]) for r in rows if r[5] is None}
    got: collections.Counter = collections.Counter()
    where: dict[tuple, int] = {}
    faults = []
    for n, m in found:
        key = mutant_id(m)
        got[key] += 1
        where.setdefault(key, n)
        if key in runs and m.get('status') == 'Ignored':
            faults.append(
                f'shard {n} ignored the mutant of {path!r:.200} at {key[0]}:{key[1]} '
                f'({key[4]!r:.100}) that its plan pass runs'
            )
    for key in got - want:
        at = '?' if key is None else f'{key[0]}:{key[1]} ({key[4]!r:.100})'
        faults.append(
            f'shard {where[key]} reports a mutant of {path!r:.200} at {at} its plan pass did not find'
        )
    for key in want - got:
        faults.append(
            f'no shard reports the mutant of {path!r:.200} its plan pass found at '
            f'{key[0]}:{key[1]} ({key[4]!r:.100})'
        )
    return faults[:1] + ([f'and {len(faults) - 1} more'] if len(faults) > 1 else [])


def plan_statuses(merged: dict, ids: dict | None) -> None:
    """Give each split file's mutants the plan pass's early status, which a range cannot decide."""
    for path, rows in (ids or {}).items():
        early = {tuple(r[:5]): (r[5], r[6]) for r in rows if r[5] is not None}
        for m in (merged['files'].get(path) or {}).get('mutants') or []:
            if (status := early.get(mutant_id(m))) is None:
                continue
            for k in ('killedBy', 'testsCompleted', 'statusReason'):
                m.pop(k, None)
            m['status'] = status[0]
            if status[1] is not None:
                m['statusReason'] = status[1]


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
    out: Path,
    setups: list[dict],
    plan: dict,
    measured: dict,
    keep: set[str] | None,
    prior_error: float | None = None,
) -> None:
    """OUT/costs.json: this run's measure of each unit, else its last one, for the files of KEEP when given.

    Every row of one file comes from the version its blob names: an old row of
    a file this run measured stays only when the file is unchanged and the row
    overlaps no unit measured now. A new sample of under half a unit's planned
    mutants never replaces a last measure of the same version; with none it is
    kept, and the next plan prices only the mutants it measured.
    """
    out.mkdir(parents=True, exist_ok=True)
    planned = plan.get('mutants') if isinstance(plan.get('mutants'), dict) else {}
    old = plan.get('history') if isinstance(plan.get('history'), dict) else {}
    old_blobs, new_blobs = old.get('blobs') or {}, plan.get('blobs') or {}
    last = [
        u
        for u in mutation_cost.clean_files(old.get('files'))
        if old_blobs.get(file_of(u)) == new_blobs.get(file_of(u))
    ]
    fresh = {
        u: e
        for u, e in mutation_cost.clean_files(measured).items()
        if e['mutants'] * 2 >= planned.get(u, 0) or not any(overlaps(u, v) for v in last)
    }
    touched = {file_of(u) for u in fresh}
    blobs = {f: new_blobs[f] for f in touched if f in new_blobs}
    rows = dict(fresh)
    for u, e in mutation_cost.clean_files(old.get('files')).items():
        f = file_of(u)
        if u in rows:
            continue
        if f in touched:
            if f not in blobs or old_blobs.get(f) != blobs[f]:
                continue
            if any(overlaps(u, v) for v in fresh):
                continue
        elif f in old_blobs:
            blobs[f] = old_blobs[f]
        rows[u] = e
    files = None if keep is None else {file_of(u) for u in keep}
    rows = {u: e for u, e in sorted(rows.items()) if files is None or file_of(u) in files}
    setup = mutation_cost.combine(setups)['setup_secs']
    if setup is None and mutation_cost.nonnegative(old.get('setup_secs')):
        setup = float(old['setup_secs'])
    history = {
        'files': rows,
        'setup_secs': setup,
        'blobs': {f: b for f, b in sorted(blobs.items()) if any(file_of(u) == f for u in rows)},
        'prior_error': prior_error,
    }
    (out / 'costs.json').write_text(json.dumps(history, indent=1) + '\n')


# Two runs of one file version differ in a few mutants that hang or race under load
# (diff.ts: 2 of 693 between runs 41323038268 and 43328238671), so each status may move
# by this share of the file's mutants, and at least COUNT_SLACK.
COUNT_VARIANCE = 0.01
COUNT_SLACK = 2


def file_counts(report: object) -> dict[str, dict[str, int]]:
    """Each file of a Stryker REPORT with its mutants' count per status."""
    files = report.get('files') if isinstance(report, dict) else None
    out: dict[str, dict[str, int]] = {}
    for path, f in (files or {}).items():
        counts: dict[str, int] = {}
        for m in f.get('mutants') or [] if isinstance(f, dict) else []:
            status = m.get('status') if isinstance(m, dict) else None
            counts[str(status)] = counts.get(str(status), 0) + 1
        out[path] = counts
    return out


def count_rows(baseline: object, proof: object, before: dict, after: dict) -> list[dict]:
    """Per file, whether PROOF's statuses match BASELINE's within run-to-run variance.

    Only a file whose git blob is the same in both runs (BEFORE and AFTER map
    file to blob) is judged: 'match' or 'differs', and a different mutant total
    differs. Any other file, a changed or unknown blob or a file one run lacks,
    is 'skipped', since its mutants are not the same ones.
    """
    old, new = file_counts(baseline), file_counts(proof)
    rows = []
    for path in sorted(set(old) | set(new)):
        row = {'file': path, 'baseline': old.get(path), 'proof': new.get(path)}
        blob = before.get(path)
        if path not in old or path not in new or not blob or after.get(path) != blob:
            rows.append({**row, 'verdict': 'skipped'})
            continue
        a, b = old[path], new[path]
        total = sum(a.values())
        slack = max(COUNT_SLACK, math.ceil(total * COUNT_VARIANCE))
        same = total == sum(b.values()) and all(
            abs(a.get(k, 0) - b.get(k, 0)) <= slack for k in set(a) | set(b)
        )
        rows.append({**row, 'verdict': 'match' if same else 'differs'})
    return rows


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
                if f['shard'] == n and f['run'] + f['early'] and file_of(p) not in report['files']
            ]
            if missing:
                problems.append(
                    f'shard {n} has no result for {missing[0]!r:.200} and {len(missing) - 1} more'
                )
    if not problems and files:
        problems += range_faults(reports, files, plan.get('ids'))
    error = mutation_cost.prior_error(
        (f['shard'], measured[u]['secs'], f['prior_secs'])
        for u, f in (files or {}).items()
        if u in measured and mutation_cost.nonnegative(f.get('prior_secs'))
    )
    mutation_cost.note_prior_error(where, error)
    if problems:
        mutation_cost.annotate(
            'error', t'{where}: {", ".join(problems)}, so this week publishes no score for it'
        )
        mutation_cost.summarize(t'{where}: no score this week ({", ".join(problems)})')
        reported = {p for rep in reports.values() for p in rep['files']}
        write_history(costs_out, setups, plan, measured, set(files or reported) or None, error)
        return 1
    merged = merge_reports(reports, owner)
    plan_statuses(merged, plan.get('ids'))
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
    write_history(costs_out, setups, plan, measured, set(files or merged['files']), error)
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
    cc = sub.add_parser('counts', help="compare two runs' statuses per file of an unchanged blob")
    cc.add_argument('--baseline', required=True, type=Path, help="the earlier run's mutation.json")
    cc.add_argument('--proof', required=True, type=Path, help="the later run's mutation.json")
    cc.add_argument('--baseline-blobs', required=True, type=Path, help='{file: blob} at its sha')
    cc.add_argument('--proof-blobs', required=True, type=Path, help='{file: blob} at its sha')
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
            plan_pass = read_json(args.plan_pass) if args.plan_pass else None
            listed = plan_pass.get('files') if isinstance(plan_pass, dict) else None
            names = [p for p in listed if isinstance(p, str)] if isinstance(listed, dict) else []
            versions = blobs(Path(), names)
            doc = sizing(
                args.repo,
                args.dir,
                args.sha,
                read_json(args.config),
                plan_pass,
                versions,
                directives(Path(), versions),
            )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=1) + '\n')
    elif args.command == 'matrix':
        entries = json.loads(args.entries)
        if not isinstance(entries, list):
            raise SystemExit(
                f"--entries must be the discover job's entry list, got {args.entries!r:.200}"
            )
        run, merge_ = matrix(entries, args.plans, args.out_plans, args.history)
        print(json.dumps({'run': run, 'merge': merge_}, separators=(',', ':')))
    elif args.command == 'counts':
        rows = count_rows(
            read_json(args.baseline, None),
            read_json(args.proof, None),
            read_json(args.baseline_blobs),
            read_json(args.proof_blobs),
        )
        print(json.dumps(rows, indent=1))
        return 1 if any(r['verdict'] == 'differs' for r in rows) else 0
    elif args.command == 'mutate':
        print(mutate_list(json.loads(args.plan.read_text()), args.shard))
    elif args.command == 'costs':
        args.out.write_text(json.dumps(costs(args.times)) + '\n')
    else:
        return merge(json.loads(args.plan.read_text()), args.shards, args.stage, args.costs)
    return 0


if __name__ == '__main__':
    sys.exit(mutation_cost.run_main(main))
