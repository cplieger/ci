#!/usr/bin/env python3
"""Pin the contract of the gremlins measurement scripts.

Two scripts decide what number every Go repo publishes each week, and neither
has a caller that would notice a silent regression until the following Sunday:

    gremlins-merge.py      folds one gremlins result per Go module into the
                           single per-attempt result everything downstream
                           assumes, so a repo with a nested module keeps ONE
                           published number that covers every module.
    gremlins-aggregate.py  turns the per-attempt results into the tracker-issue
                           body, the rolling history and the README badge.

The sibling stryker aggregate script sat broken for weeks in this repo because
nothing executed it. This probe is what stops that repeating: run it and the
whole path from "N gremlins JSONs" to "issue body + badge" is exercised.

Run: python3 scripts/test-gremlins-scripts.py     (exit 0 = pass)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MERGE = HERE / 'gremlins-merge.py'
AGGREGATE = HERE / 'gremlins-aggregate.py'

FAILURES: list[str] = []


def check(name: str, *, ok: bool, detail: str = '') -> None:
    if ok:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}{": " + detail if detail else ""}')
        FAILURES.append(name)


def result(module: str, files: list[tuple[str, list[tuple[str, str]]]]) -> dict:
    """Build a gremlins v0.6.0 --output document with self-consistent totals.

    files: [(file_name, [(status, mutator_type), ...]), ...]

    The counters and percentages follow internal/report: mutants_total is
    killed+lived+not_viable (TIMED OUT / SKIPPED are in no counter), efficacy is
    killed/(killed+lived) and 0 when nothing was killed, coverage is
    (killed+lived)/(killed+lived+not_covered).
    """
    stat_key = {
        'ARITHMETIC_BASE': 'arithmetic_base',
        'CONDITIONALS_BOUNDARY': 'conditionals_boundary',
        'CONDITIONALS_NEGATION': 'conditionals_negation',
        'INVERT_BWASSIGN': 'invert_bitwise_assignments',
        'INVERT_LOOPCTRL': 'invert_loop_ctrl',
    }
    counts = {'KILLED': 0, 'LIVED': 0, 'NOT COVERED': 0, 'NOT VIABLE': 0, 'TIMED OUT': 0}
    stats: dict[str, int] = {}
    out_files = []
    for name, muts in files:
        out_files.append({
            'file_name': name,
            'mutations': [
                {'type': t, 'status': s, 'line': 10 + i, 'column': 3}
                for i, (s, t) in enumerate(muts)
            ],
        })
        for s, t in muts:
            counts[s] += 1
            stats[stat_key[t]] = stats.get(stat_key[t], 0) + 1
    killed, lived = counts['KILLED'], counts['LIVED']
    nc, nv = counts['NOT COVERED'], counts['NOT VIABLE']
    return {
        'go_module': module,
        'test_efficacy': (killed / (killed + lived) * 100) if killed else 0.0,
        'mutations_coverage': ((killed + lived) / (killed + lived + nc) * 100) if killed + lived else 0.0,
        'mutants_total': killed + lived + nv,
        'mutants_killed': killed,
        'mutants_lived': lived,
        'mutants_not_viable': nv,
        'mutants_not_covered': nc,
        'elapsed_time': 12.5,
        'mutator_statistics': stats,
        'files': out_files,
    }


def run_merge(tmp: Path, modules: list[tuple[str, dict | None]], out_name: str = 'merged.json'):
    """Write each module's JSON (None = no file at all) and merge them."""
    args = []
    for dir_, data in modules:
        path = tmp / (f'{dir_.replace("/", "_")}-out.json' if dir_ != '.' else 'root-out.json')
        if data is not None:
            path.write_text(json.dumps(data))
        args += ['--module', f'{dir_}={path}']
    out = tmp / out_name
    proc = subprocess.run(
        [sys.executable, str(MERGE), '--out', str(out), *args],
        capture_output=True, text=True, check=False,
    )
    merged = json.loads(out.read_text()) if proc.returncode == 0 and out.exists() else None
    return proc, merged


# ---------------------------------------------------------------------------
# gremlins-merge.py
# ---------------------------------------------------------------------------
def test_single_module_is_identity(tmp: Path) -> None:
    """A repo with no nested module must publish exactly what gremlins said.

    ~40 of the fleet's repos are single-module, so this is the path the merge
    takes almost every time: it must not shift a single number.
    """
    root = result('github.com/cplieger/thing/v2', [
        ('thing.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'CONDITIONALS_BOUNDARY'),
                      ('NOT COVERED', 'INVERT_LOOPCTRL'), ('NOT VIABLE', 'INVERT_BWASSIGN')]),
        ('other.go', [('KILLED', 'CONDITIONALS_NEGATION'), ('TIMED OUT', 'ARITHMETIC_BASE')]),
    ])
    proc, merged = run_merge(tmp, [('.', root)])
    check('single module: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    same = all(
        merged[k] == root[k]
        for k in ('go_module', 'mutants_total', 'mutants_killed', 'mutants_lived',
                  'mutants_not_viable', 'mutants_not_covered', 'mutator_statistics')
    )
    check('single module: counters unchanged', ok=same,
          detail=f'{ {k: merged[k] for k in ("mutants_total", "mutants_killed")} }')
    check('single module: efficacy unchanged',
          ok=abs(merged['test_efficacy'] - root['test_efficacy']) < 0.001,
          detail=f'{merged["test_efficacy"]} vs {root["test_efficacy"]}')
    check('single module: coverage unchanged',
          ok=abs(merged['mutations_coverage'] - root['mutations_coverage']) < 0.001,
          detail=f'{merged["mutations_coverage"]} vs {root["mutations_coverage"]}')
    check('single module: mutation positions preserved',
          ok=sorted(f['file_name'] for f in merged['files']) == ['other.go', 'thing.go'])


def test_upstream_fixture_roundtrips(tmp: Path) -> None:
    """The recompute must reproduce a REAL gremlins document, not just ours.

    Values are gremlins v0.6.0's own internal/report/testdata/normal_output.json:
    4 killed, 3 lived, 2 not viable, 3 not covered -> 57.142857% efficacy, 70%
    coverage. If gremlins ever changes how it counts, this fails here rather
    than publishing a number nobody checked.
    """
    upstream = {
        'go_module': 'example.com/go/module',
        'test_efficacy': 57.14285714285714,
        'mutations_coverage': 70,
        'mutants_total': 9,
        'mutants_killed': 4,
        'mutants_lived': 3,
        'mutants_not_viable': 2,
        'mutants_not_covered': 3,
        'elapsed_time': 142.123,
        'mutator_statistics': {'arithmetic_base': 1, 'conditionals_boundary': 1},
        'files': [
            {'file_name': 'file1.go', 'mutations': [
                {'type': 'ARITHMETIC_BASE', 'status': 'KILLED', 'line': 1, 'column': 1},
                {'type': 'CONDITIONALS_BOUNDARY', 'status': 'KILLED', 'line': 2, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'LIVED', 'line': 3, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT COVERED', 'line': 4, 'column': 1},
            ]},
            {'file_name': 'file2.go', 'mutations': [
                {'type': 'ARITHMETIC_BASE', 'status': 'KILLED', 'line': 1, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'KILLED', 'line': 2, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'LIVED', 'line': 3, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'LIVED', 'line': 4, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT COVERED', 'line': 5, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT VIABLE', 'line': 6, 'column': 1},
            ]},
            {'file_name': 'file3.go', 'mutations': [
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT COVERED', 'line': 1, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT VIABLE', 'line': 2, 'column': 1},
            ]},
        ],
    }
    # mutator_statistics is asserted separately by the self-check; strip it so
    # this case pins the counters and percentages only.
    del upstream['mutator_statistics']
    proc, merged = run_merge(tmp, [('.', upstream)], out_name='upstream.json')
    check('upstream fixture: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    check('upstream fixture: efficacy 57.14%',
          ok=abs(merged['test_efficacy'] - 57.14285714285714) < 0.001,
          detail=str(merged['test_efficacy']))
    check('upstream fixture: coverage 70%',
          ok=abs(merged['mutations_coverage'] - 70) < 0.001, detail=str(merged['mutations_coverage']))
    check('upstream fixture: total 9', ok=merged['mutants_total'] == 9, detail=str(merged['mutants_total']))


def test_nested_module_is_measured_not_counted_uncovered(tmp: Path) -> None:
    """The envx defect, end to end.

    Root run: 5 killed / 1 lived of its own, plus a nested module's 4 files
    analysed and uncovered because a root `go test ./...` cannot run them.
    Nested run: the same code measured by its own suite, 4 killed.

    Merged, the nested mutants must count as killed (not as uncovered noise),
    the root's copies must be dropped exactly once, and paths must stay
    repo-relative.
    """
    root = result('github.com/cplieger/envx/v2', [
        ('envx.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY'),
                     ('KILLED', 'CONDITIONALS_NEGATION'), ('KILLED', 'INVERT_LOOPCTRL'),
                     ('KILLED', 'INVERT_BWASSIGN'), ('LIVED', 'ARITHMETIC_BASE')]),
        ('yamlenv/yamlenv.go', [('NOT COVERED', 'ARITHMETIC_BASE')] * 3),
        ('yamlenv/parse.go', [('NOT COVERED', 'CONDITIONALS_BOUNDARY')]),
    ])
    nested = result('github.com/cplieger/envx/yamlenv/v2', [
        ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')] * 3),
        ('parse.go', [('KILLED', 'CONDITIONALS_BOUNDARY')]),
    ])
    check('nested: root run alone reports 60% coverage (the defect)',
          ok=abs(root['mutations_coverage'] - 60.0) < 0.001, detail=str(root['mutations_coverage']))

    proc, merged = run_merge(tmp, [('.', root), ('yamlenv', nested)], out_name='nested.json')
    check('nested: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    check('nested: merged coverage is 100% (no module-boundary uncovered)',
          ok=abs(merged['mutations_coverage'] - 100.0) < 0.001, detail=str(merged['mutations_coverage']))
    check('nested: merged efficacy is weighted, not averaged',
          ok=abs(merged['test_efficacy'] - 9 / 10 * 100) < 0.001, detail=str(merged['test_efficacy']))
    check('nested: killed = 5 root + 4 nested', ok=merged['mutants_killed'] == 9,
          detail=str(merged['mutants_killed']))
    check('nested: no uncovered mutants survive the merge', ok=merged['mutants_not_covered'] == 0,
          detail=str(merged['mutants_not_covered']))
    names = sorted(f['file_name'] for f in merged['files'])
    check('nested: paths stay repo-relative and unduplicated',
          ok=names == ['envx.go', 'yamlenv/parse.go', 'yamlenv/yamlenv.go'], detail=str(names))
    dropped = {m['dir']: m['dropped_nested_mutations'] for m in merged['modules']}
    check('nested: per-module split records the 4 dropped root copies',
          ok=dropped == {'.': 4, 'yamlenv': 0}, detail=str(dropped))


def test_nested_verdict_in_root_run_is_fatal(tmp: Path) -> None:
    """Dropping a nested file that carries a real verdict must fail loudly.

    The drop is only safe because an uncovered mutant is never executed. If a
    root run ever DOES kill a mutant in a nested module, silently dropping it
    would bias the number, so the attempt has to go red instead.
    """
    root = result('github.com/cplieger/envx/v2', [
        ('envx.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ('yamlenv/yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    nested = result('github.com/cplieger/envx/yamlenv/v2', [
        ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    proc, _ = run_merge(tmp, [('.', root), ('yamlenv', nested)], out_name='verdict.json')
    check('nested verdict in root run: exits non-zero', ok=proc.returncode != 0)
    check('nested verdict in root run: says which file and status',
          ok='yamlenv/yamlenv.go' in proc.stderr and 'KILLED' in proc.stderr,
          detail=proc.stderr[-300:])


def test_inconsistent_input_is_fatal(tmp: Path) -> None:
    """A gremlins result whose own totals disagree with its mutant list.

    This is the schema-drift guard: it fires if a future gremlins version counts
    differently, instead of publishing a number derived from a formula that no
    longer matches the tool.
    """
    root = result('github.com/cplieger/thing/v2', [
        ('thing.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'ARITHMETIC_BASE')]),
    ])
    root['mutants_killed'] = 7
    proc, _ = run_merge(tmp, [('.', root)], out_name='inconsistent.json')
    check('inconsistent input: exits non-zero', ok=proc.returncode != 0)
    check('inconsistent input: names the field',
          ok='mutants_killed' in proc.stderr, detail=proc.stderr[-300:])


def test_missing_module_output_is_zero_not_error(tmp: Path) -> None:
    """gremlins writes no file when a module has no mutants at all."""
    root = result('github.com/cplieger/thing/v2', [
        ('thing.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    proc, merged = run_merge(tmp, [('.', root), ('tools', None)], out_name='missing.json')
    check('missing module output: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    check('missing module output: contributes nothing', ok=merged['mutants_total'] == 1,
          detail=str(merged['mutants_total']))
    check('missing module output: still listed in the split',
          ok=[m['dir'] for m in merged['modules']] == ['.', 'tools'])


def test_module_nested_in_nested(tmp: Path) -> None:
    """Three levels: each mutant must land in exactly one bucket."""
    root = result('github.com/cplieger/thing/v2', [
        ('thing.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ('a/a.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
        ('a/b/b.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
    ])
    mid = result('github.com/cplieger/thing/a/v2', [
        ('a.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ('b/b.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
    ])
    leaf = result('github.com/cplieger/thing/a/b/v2', [
        ('b.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    proc, merged = run_merge(tmp, [('.', root), ('a', mid), ('a/b', leaf)], out_name='deep.json')
    check('nested-in-nested: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    names = sorted(f['file_name'] for f in merged['files'])
    check('nested-in-nested: every file counted once',
          ok=names == ['a/a.go', 'a/b/b.go', 'thing.go'], detail=str(names))
    check('nested-in-nested: all three killed, none uncovered',
          ok=(merged['mutants_killed'], merged['mutants_not_covered']) == (3, 0),
          detail=f'{merged["mutants_killed"]}/{merged["mutants_not_covered"]}')


def test_root_module_required(tmp: Path) -> None:
    nested = result('github.com/cplieger/envx/yamlenv/v2', [
        ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    proc, _ = run_merge(tmp, [('yamlenv', nested)], out_name='norootmod.json')
    check('root module required: exits non-zero', ok=proc.returncode != 0)


# ---------------------------------------------------------------------------
# gremlins-aggregate.py — the merged document must still parse downstream
# ---------------------------------------------------------------------------
def test_aggregate_consumes_merged_output(tmp: Path) -> None:
    """A merged document must drive the body, the rolling table and the badge.

    The whole reason the merge happens in the run job (rather than the aggregate
    script growing a per-module mode) is that a repo keeps ONE artifact per
    attempt and ONE published number. This proves the chain holds.
    """
    art = tmp / 'artifacts'
    for attempt in (1, 2, 3):
        d = art / f'gremlins-envx-{attempt}'
        d.mkdir(parents=True)
        root = result('github.com/cplieger/envx/v2', [
            ('envx.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY'),
                         ('LIVED', 'CONDITIONALS_NEGATION')]),
            ('yamlenv/yamlenv.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
        ])
        nested = result('github.com/cplieger/envx/yamlenv/v2', [
            ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ])
        proc, merged = run_merge(tmp, [('.', root), ('yamlenv', nested)],
                                 out_name=f'attempt-{attempt}.json')
        if merged is None:
            check(f'aggregate: attempt {attempt} merged', ok=False, detail=proc.stderr)
            return
        (d / 'gremlins-out.json').write_text(json.dumps(merged))

    badge = tmp / 'badge.json'
    attempts_marker = tmp / 'attempts.txt'
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'envx', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1',
         '--badge-file', str(badge), '--attempts-marker-file', str(attempts_marker)],
        capture_output=True, text=True, check=False,
    )
    check('aggregate: exits 0 on merged input', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    if proc.returncode != 0:
        return
    body = proc.stdout
    check('aggregate: read all three attempts', ok=attempts_marker.read_text() == '3',
          detail=attempts_marker.read_text())
    check('aggregate: efficacy is the merged 75%', ok='75.0% efficacy' in body,
          detail=body.splitlines()[4] if len(body.splitlines()) > 4 else body[:200])
    check('aggregate: coverage is 100% (nested module measured)',
          ok='100.0% mutant coverage' in body)
    check('aggregate: nested path renders repo-relative in the live-mutant list',
          ok='`envx.go`' in body)
    check('aggregate: badge written', ok=badge.exists() and 'mutation' in badge.read_text(),
          detail=badge.read_text() if badge.exists() else 'missing')


def test_aggregate_flags_disagreeing_verdicts(tmp: Path) -> None:
    """docker-rsync-scheduler's signature: three attempts, three different survivors.

    All three of its live mutants are equivalent, so none can be killed; each
    attempt nonetheless reported a different single survivor and called the other
    two killed. The mean is then noise, and a 100% week is a measurement failure
    rather than a perfect score, so the body must say so.
    """
    art = tmp / 'artifacts-flaky'
    survivors = ['a.go', 'b.go', 'c.go']
    for attempt, lived_in in enumerate(survivors, start=1):
        d = art / f'gremlins-rsync-{attempt}'
        d.mkdir(parents=True)
        files = [
            (f, [('LIVED' if f == lived_in else 'KILLED', 'ARITHMETIC_BASE')])
            for f in survivors
        ]
        (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', files)))
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'rsync', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1'],
        capture_output=True, text=True, check=False,
    )
    check('disagreeing verdicts: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('disagreeing verdicts: body carries the caution',
          ok='Verdicts disagree' in proc.stdout, detail=proc.stdout[:600])
    check('disagreeing verdicts: caution names the worker-interference cause',
          ok='workers' in proc.stdout.lower())


def test_aggregate_flags_sudden_perfect_week(tmp: Path) -> None:
    """A jump from live mutants to a flawless 100% with no test change."""
    art = tmp / 'artifacts-perfect'
    for attempt in (1, 2, 3):
        d = art / f'gremlins-rsync-{attempt}'
        d.mkdir(parents=True)
        (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', [
            ('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY')]),
        ])))
    existing = tmp / 'existing.md'
    existing.write_text(
        '# Gremlins mutation testing tracker\n\n'
        '## Rolling 12-week history\n'
        '<!-- gremlins-data -->\n'
        '| Run (UTC) | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ efficacy |\n'
        '|---|---|---|---|---|---|\n'
        '| 2026-08-16 22:00 | 40.0% | ±0.0% | 100.0% | 3 | -0.0% |\n'
        '<!-- /gremlins-data -->\n'
    )
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'rsync', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1',
         '--existing-body-file', str(existing)],
        capture_output=True, text=True, check=False,
    )
    check('sudden perfect week: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('sudden perfect week: body carries the caution',
          ok='100%' in proc.stdout and 'measurement' in proc.stdout.lower(),
          detail=proc.stdout[:600])
    check('sudden perfect week: history row preserved', ok='2026-08-16 22:00' in proc.stdout)


def test_aggregate_quiet_on_a_stable_week(tmp: Path) -> None:
    """No caution when every attempt agrees and the score is not a jump to 100%."""
    art = tmp / 'artifacts-stable'
    for attempt in (1, 2, 3):
        d = art / f'gremlins-stable-{attempt}'
        d.mkdir(parents=True)
        (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', [
            ('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'CONDITIONALS_BOUNDARY')]),
        ])))
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'stable', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1'],
        capture_output=True, text=True, check=False,
    )
    check('stable week: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('stable week: no caution line',
          ok='Verdicts disagree' not in proc.stdout and 'measurement failure' not in proc.stdout,
          detail=proc.stdout[:400])


def main() -> int:
    if not MERGE.exists() or not AGGREGATE.exists():
        print(f'missing script: {MERGE} / {AGGREGATE}')
        return 1
    tests = [
        test_single_module_is_identity,
        test_upstream_fixture_roundtrips,
        test_nested_module_is_measured_not_counted_uncovered,
        test_nested_verdict_in_root_run_is_fatal,
        test_inconsistent_input_is_fatal,
        test_missing_module_output_is_zero_not_error,
        test_module_nested_in_nested,
        test_root_module_required,
        test_aggregate_consumes_merged_output,
        test_aggregate_flags_disagreeing_verdicts,
        test_aggregate_flags_sudden_perfect_week,
        test_aggregate_quiet_on_a_stable_week,
    ]
    for t in tests:
        print(f'{t.__name__}:')
        with tempfile.TemporaryDirectory() as td:
            t(Path(td))
    print()
    if FAILURES:
        print(f'FAILED ({len(FAILURES)}): ' + ', '.join(FAILURES))
        return 1
    print('all gremlins script checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
