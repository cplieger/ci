#!/usr/bin/env python3
"""Merge one gremlins result per Go module into a single per-attempt result.

Why this exists
---------------
`gremlins unleash .` measures exactly ONE Go module: it gathers coverage with
`go test -cover ./...` at the module root and then strips the module path off
every coverage-profile line to key blocks by repo-relative filename
(`internal/coverage/coverage.go:removeModuleFromPath`). A NESTED module's
packages carry a different module path, and a root `go test ./...` cannot run
their tests at all. When the root tests do not compile those files either, their
mutants come back `NOT COVERED` no matter how good that module's own suite is;
when an in-tree `replace` the root tests import compiles them and the run is in
integration mode (`--coverpkg ./...`), the root suite gives them a real verdict
instead. Measured on cplieger/envx: its `yamlenv` subdirectory (own `go.mod`,
own suite at 98.6% coverage) contributed 43 analysed / 43 uncovered / 0 killed /
0 lived, which dragged the repo's published mutant coverage to 41.4%, a number
that described a module boundary, not a test suite.

So the weekly runner unleashes gremlins once per module. That yields several
JSON results per attempt, while everything downstream — `gremlins-aggregate.py`,
the README badge, the tracker issue — is built on ONE result per attempt. This
script closes that gap: it folds the per-module results into a single document
in gremlins' own output schema, so the repo keeps exactly one published number
and that number covers every module instead of one of several.

What it does
------------
1. Reads one gremlins `--output` JSON per module dir (`.` for the root module).
   A MISSING file means "this module had no mutants at all" — gremlins writes no
   report when it finds none — and folds in as zeros.
2. Verifies each input against itself: recomputes gremlins' own counters and
   percentages from that file's `files[]` entries and compares them with the
   file's top-level fields. A mismatch means gremlins changed how it counts, and
   this script would then publish a number nobody has checked, so it exits 1 and
   the attempt goes red instead.
3. Drops, from each module's result, mutations that live inside a DEEPER module's
   directory. gremlins-scope.py excludes those directories from every outer
   module's run, so a healthy run has none. One that still appears is dropped
   when every mutation is NOT COVERED or SKIPPED (never executed, so no verdict;
   its module's own run counts it); anything else means the exclusion failed and
   the outer suite ran nested code, so it exits 1.
4. Prefixes each non-root module's filenames with its directory, so positions in
   the tracker issue stay repo-relative and unambiguous.
5. Recomputes the merged counters, `test_efficacy`, `mutations_coverage` and
   `mutator_statistics` with gremlins' own formulas (see `internal/report`):

       mutants_total      = killed + lived + not_viable   (TIMED OUT and SKIPPED
                                                           are in neither)
       test_efficacy      = killed / (killed + lived)          [0 if killed == 0]
       mutations_coverage = (killed + lived) / (killed + lived + not_covered)
                                                    [0 if killed + lived == 0]

   Summing the counters and re-deriving the percentages is the whole point: a
   plain mean of per-module percentages would weight a 3-mutant module the same
   as a 300-mutant one.

Usage
-----
    gremlins-merge.py --out merged.json \
        --module .=target/gremlins-out.json \
        --module yamlenv=target/yamlenv/gremlins-out.json

Stdout stays clean; per-module diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# gremlins' mutator.Type.String() values are NOT the JSON keys of
# mutator_statistics for every type: INVERT_BWASSIGN -> invert_bitwise_assignments
# and INVERT_LOOPCTRL -> invert_loop_ctrl. The rest are the lowercased string.
# Pinned against gremlins v0.6.0 internal/report/testdata/normal_output.json,
# which exercises all eleven.
TYPE_TO_STAT_KEY = {
    'ARITHMETIC_BASE': 'arithmetic_base',
    'CONDITIONALS_NEGATION': 'conditionals_negation',
    'CONDITIONALS_BOUNDARY': 'conditionals_boundary',
    'INCREMENT_DECREMENT': 'increment_decrement',
    'INVERT_ASSIGNMENTS': 'invert_assignments',
    'INVERT_BITWISE': 'invert_bitwise',
    'INVERT_BWASSIGN': 'invert_bitwise_assignments',
    'INVERT_LOGICAL': 'invert_logical',
    'INVERT_LOOPCTRL': 'invert_loop_ctrl',
    'INVERT_NEGATIVES': 'invert_negatives',
    'REMOVE_SELF_ASSIGNMENTS': 'remove_self_assignments',
}

# Statuses a mutant inside a nested module may legitimately carry in an OUTER
# module's run that missed the scope exclusion. An uncovered mutant is never
# executed, so it cannot come back killed, lived, timed out or not viable.
DROPPABLE_STATUSES = {'NOT_COVERED', 'SKIPPED'}

# Percentage comparison tolerance for the per-input self-check. gremlins writes
# full float64 precision; we recompute the same expression, so any real
# divergence is far larger than this.
PCT_TOLERANCE = 0.01

MODES = {'integration', 'per-package'}


def norm_status(status: str | None) -> str:
    """gremlins prints statuses with spaces ("NOT COVERED"); normalise to _."""
    return (status or '').upper().replace(' ', '_')


def counts_from_files(files: list[dict]) -> dict:
    """Tally gremlins' counters from per-mutation statuses.

    Mirrors internal/report.reportMutationStatus + fileReport: `mutants_total`
    is killed+lived+not_viable only, so TIMED OUT and SKIPPED mutants appear in
    `files[]` but in no counter.
    """
    c = {
        'killed': 0,
        'lived': 0,
        'not_covered': 0,
        'not_viable': 0,
        'timed_out': 0,
        'skipped': 0,
        'runnable': 0,
    }
    stats: dict[str, int] = {}
    for fe in files:
        for m in fe.get('mutations') or []:
            status = norm_status(m.get('status'))
            key = {
                'KILLED': 'killed',
                'LIVED': 'lived',
                'NOT_COVERED': 'not_covered',
                'NOT_VIABLE': 'not_viable',
                'TIMED_OUT': 'timed_out',
                'SKIPPED': 'skipped',
                'RUNNABLE': 'runnable',
            }.get(status)
            if key is None:
                raise SystemExit(
                    f'unknown gremlins mutant status {status!r} in {fe.get("file_name")!r}'
                )
            c[key] += 1
            stat_key = TYPE_TO_STAT_KEY.get((m.get('type') or '').upper())
            if stat_key:
                stats[stat_key] = stats.get(stat_key, 0) + 1
    c['total'] = c['killed'] + c['lived'] + c['not_viable']
    return {'counts': c, 'stats': stats}


def efficacy(c: dict) -> float:
    """gremlins: killed / (killed + lived), and 0 when nothing was killed."""
    if c['killed'] <= 0:
        return 0.0
    return c['killed'] / (c['killed'] + c['lived']) * 100


def mutant_coverage(c: dict) -> float:
    """gremlins: (killed + lived) / (killed + lived + not_covered)."""
    runnable = c['killed'] + c['lived']
    if runnable <= 0:
        return 0.0
    return runnable / (runnable + c['not_covered']) * 100


def self_check(dir_: str, data: dict, tallied: dict) -> list[str]:
    """Compare a recomputation of an input against its own top-level fields."""
    c = tallied['counts']
    problems = []
    for field, got in (
        ('mutants_killed', c['killed']),
        ('mutants_lived', c['lived']),
        ('mutants_not_covered', c['not_covered']),
        ('mutants_not_viable', c['not_viable']),
        ('mutants_total', c['total']),
    ):
        want = int(data.get(field) or 0)
        if want != got:
            problems.append(f'{field}: file says {want}, files[] tally says {got}')
    for field, got in (
        ('test_efficacy', efficacy(c)),
        ('mutations_coverage', mutant_coverage(c)),
    ):
        want = float(data.get(field) or 0.0)
        if abs(want - got) > PCT_TOLERANCE:
            problems.append(f'{field}: file says {want:.4f}, recomputed {got:.4f}')
    if data.get('mutator_statistics') is not None:
        want_stats = {k: v for k, v in (data['mutator_statistics'] or {}).items() if v}
        if want_stats != tallied['stats']:
            problems.append(
                f'mutator_statistics: file says {want_stats}, recomputed {tallied["stats"]}'
            )
    return [f'[{dir_}] {p}' for p in problems]


def load_module(dir_: str, path: Path) -> tuple[dict, bool]:
    """Load one module's gremlins result. Returns (data, present)."""
    if not path.exists() or path.stat().st_size == 0:
        # gremlins writes no report at all when a module yields no mutants
        # (internal/report.newReport bails on an empty mutant list), so a
        # missing file is a zero-mutant module, not a failure.
        print(f'[{dir_}] no gremlins output at {path} — treating as 0 mutants', file=sys.stderr)
        return {'files': []}, False
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f'[{dir_}] {path} is not valid JSON: {exc}') from exc
    if not isinstance(data, dict) or 'files' not in data:
        raise SystemExit(f"[{dir_}] {path} is not a gremlins result (no 'files' key)")
    return data, True


def load_mode(path: Path) -> str:
    """The mode scripts/gremlins-run.sh wrote beside a module's result; absent reads as per-package."""
    mode_path = Path(f'{path}.mode')
    if not mode_path.exists():
        return 'per-package'
    mode = mode_path.read_text().strip()
    if mode not in MODES:
        raise SystemExit(f'{mode_path} names unknown mode {mode!r}')
    return mode


def deeper_dirs(dir_: str, all_dirs: list[str]) -> list[str]:
    """Module dirs strictly inside `dir_`, as module-relative prefixes.

    A deeper module's files are that module's business, counted by its own run:
    gremlins-scope.py excludes these prefixes from `dir_`'s run and merge() drops
    any file under them that still appears. Doing this by containment rather
    than by rejecting overlapping modules means a module nested inside a nested
    module still lands in exactly one bucket.
    """
    out = []
    for other in all_dirs:
        if other == dir_:
            continue
        if dir_ == '.':
            out.append(other)
        elif other.startswith(dir_ + '/'):
            out.append(other[len(dir_) + 1 :])
    return out


def merge(modules: list[tuple[str, Path]], skipped: list[str] = ()) -> dict:
    all_dirs = [d for d, _ in modules] + list(skipped)
    merged_files: list[dict] = []
    per_module = []
    problems: list[str] = []
    root_module_name = ''

    for dir_, path in modules:
        data, present = load_module(dir_, path)
        if present:
            problems += self_check(dir_, data, counts_from_files(data.get('files') or []))
        if dir_ == '.':
            root_module_name = data.get('go_module') or ''

        inner = deeper_dirs(dir_, all_dirs)
        kept: list[dict] = []
        dropped = 0
        for fe in data.get('files') or []:
            name = fe.get('file_name') or ''
            if any(name == d or name.startswith(d + '/') for d in inner):
                statuses = {norm_status(m.get('status')) for m in fe.get('mutations') or []}
                bad = statuses - DROPPABLE_STATUSES
                if bad:
                    problems.append(
                        f'[{dir_}] {name} belongs to a nested module but carries {sorted(bad)} '
                        'in this run — its directory escaped the nested-module exclusion '
                        '(scripts/gremlins-scope.py), and dropping it would discard a real verdict'
                    )
                dropped += len(fe.get('mutations') or [])
                continue
            kept.append(
                {
                    'file_name': name if dir_ == '.' else f'{dir_}/{name}',
                    'mutations': list(fe.get('mutations') or []),
                }
            )

        tallied = counts_from_files(kept)
        c = tallied['counts']
        per_module.append(
            {
                'dir': dir_,
                'go_module': data.get('go_module') or '',
                'test_efficacy': round(efficacy(c), 4),
                'mutations_coverage': round(mutant_coverage(c), 4),
                'mutants_total': c['total'],
                'mutants_killed': c['killed'],
                'mutants_lived': c['lived'],
                'mutants_not_covered': c['not_covered'],
                'mutants_not_viable': c['not_viable'],
                'mutants_timed_out': c['timed_out'],
                'dropped_nested_mutations': dropped,
                'mode': load_mode(path),
            }
        )
        merged_files += kept
        print(
            f'[{dir_}] module={data.get("go_module") or "?"} killed={c["killed"]} lived={c["lived"]} '
            f'not_covered={c["not_covered"]} not_viable={c["not_viable"]} timed_out={c["timed_out"]} '
            f'efficacy={efficacy(c):.1f}% coverage={mutant_coverage(c):.1f}% '
            f'dropped_nested={dropped} mode={per_module[-1]["mode"]}',
            file=sys.stderr,
        )

    if problems:
        for p in problems:
            print(f'::error::gremlins-merge: {p}', file=sys.stderr)
        raise SystemExit(
            'gremlins-merge: refusing to publish a number derived from results that '
            'do not add up (see errors above)'
        )
    return result_doc(root_module_name, merged_files, per_module)


def result_doc(go_module: str, files: list[dict], per_module: list[dict]) -> dict:
    tallied = counts_from_files(files)
    c = tallied['counts']
    module_modes = {m['mode'] for m in per_module}
    return {
        'go_module': go_module,
        'test_efficacy': efficacy(c),
        'mutations_coverage': mutant_coverage(c),
        'mutants_total': c['total'],
        'mutants_killed': c['killed'],
        'mutants_lived': c['lived'],
        'mutants_not_viable': c['not_viable'],
        'mutants_not_covered': c['not_covered'],
        'elapsed_time': 0.0,
        'mutator_statistics': tallied['stats'],
        'files': files,
        # Not part of gremlins' schema (readers ignore unknown keys).
        'modules': per_module,
        'mode': module_modes.pop() if len(module_modes) == 1 else 'mixed',
    }


MODULE_COUNTERS = (
    'mutants_total',
    'mutants_killed',
    'mutants_lived',
    'mutants_not_covered',
    'mutants_not_viable',
    'mutants_timed_out',
    'dropped_nested_mutations',
)


def merge_shards(docs: list[dict]) -> dict:
    """Fold one attempt's shard results (each this script's output) into one result.

    Shards mutate disjoint files, so their files are concatenated and the
    counters re-derived; a file in two shards would count its verdicts twice,
    so it is refused, as is a shard whose own totals do not add up.
    """
    problems: list[str] = []
    files: list[dict] = []
    seen: dict[str, int] = {}
    modules: dict[str, dict] = {}
    go_module = ''
    for n, doc in enumerate(docs, start=1):
        problems += self_check(f'shard {n}', doc, counts_from_files(doc.get('files') or []))
        go_module = go_module or doc.get('go_module') or ''
        for fe in doc.get('files') or []:
            name = fe.get('file_name') or ''
            if name in seen:
                problems.append(f'[shard {n}] {name} was measured by shard {seen[name]} too')
                continue
            seen[name] = n
            files.append(fe)
        for m in doc.get('modules') or []:
            acc = modules.get(m['dir'])
            if acc is None:
                modules[m['dir']] = dict(m)
                continue
            for key in MODULE_COUNTERS:
                acc[key] += m[key]
            if acc['mode'] != m['mode']:
                acc['mode'] = 'mixed'
    if problems:
        raise SystemExit('gremlins-merge: shards do not add up: ' + '; '.join(problems))
    for m in modules.values():
        c = {
            'killed': m['mutants_killed'],
            'lived': m['mutants_lived'],
            'not_covered': m['mutants_not_covered'],
        }
        m['test_efficacy'] = round(efficacy(c), 4)
        m['mutations_coverage'] = round(mutant_coverage(c), 4)
    return result_doc(go_module, files, list(modules.values()))


def parse_module_arg(raw: str) -> tuple[str, Path]:
    if '=' not in raw:
        raise SystemExit(f'--module expects DIR=PATH, got {raw!r}')
    dir_, path = raw.split('=', 1)
    dir_ = dir_.rstrip('/') or '.'
    if not path:
        raise SystemExit(f'--module {raw!r} has an empty path')
    return dir_, Path(path)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        '--module',
        action='append',
        required=True,
        metavar='DIR=PATH',
        help="module directory ('.' for the root module) and its gremlins JSON; repeatable",
    )
    p.add_argument(
        '--skip-module',
        action='append',
        default=[],
        metavar='DIR',
        help='module directory this shard did not run (repeatable)',
    )
    p.add_argument('--out', required=True, type=Path, help='merged JSON destination')
    args = p.parse_args()

    modules = [parse_module_arg(m) for m in args.module]
    skipped = [d.rstrip('/') or '.' for d in args.skip_module]
    dirs = [d for d, _ in modules] + skipped
    if len(set(dirs)) != len(dirs):
        raise SystemExit(f'--module and --skip-module directories must be unique, got {dirs}')

    merged = merge(modules, skipped)
    args.out.write_text(json.dumps(merged))
    c = merged
    print(
        f'[merged] modules={len(modules)} killed={c["mutants_killed"]} lived={c["mutants_lived"]} '
        f'not_covered={c["mutants_not_covered"]} efficacy={c["test_efficacy"]:.1f}% '
        f'coverage={c["mutations_coverage"]:.1f}% -> {args.out}',
        file=sys.stderr,
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
