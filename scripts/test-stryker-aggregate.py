#!/usr/bin/env python3
"""Pin stryker-aggregate.py's artifact-layout contract.

This script decides what every enrolled TS package publishes each week, and until
2026-09-13 it had no probe at all — which is the same gap that let it sit
unparseable for weeks (`ci.md`, "weekly-stryker: two defects that discarded every
run"). The specific contract pinned here is the one that broke:

    actions/download-artifact only creates the per-artifact directory when MORE
    THAN ONE artifact matched its pattern. A run with exactly one reporting
    package dir extracts FLAT, so `artifacts/stryker-*/meta.json` finds nothing,
    no tracker issue is updated, and the job reports success.

Measured live on 2026-09-13: a single-repo dispatch published a correct 95.5%
badge and left the tracker issue untouched with the aggregate job green.

Scoped to discovery on purpose. It is not a full suite for the score arithmetic or
the body rendering; those are a separate piece of work.

Run: python3 scripts/test-stryker-aggregate.py     (exit 0 = pass)
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGGREGATE = HERE / 'stryker-aggregate.py'

FAILURES: list[str] = []


def check(name: str, *, ok: bool, detail: str = '') -> None:
    if ok:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}{": " + detail if detail else ""}')
        FAILURES.append(name)


def report(killed: int = 3, survived: int = 1) -> dict:
    """A minimal mutation-testing-elements report: N killed, M survived, one file."""
    mutants = [
        {
            'id': f'k{i}',
            'mutatorName': 'BooleanLiteral',
            'replacement': 'true',
            'status': 'Killed',
            'location': {
                'start': {'line': i + 1, 'column': 1},
                'end': {'line': i + 1, 'column': 9},
            },
        }
        for i in range(killed)
    ]
    mutants += [
        {
            'id': f's{i}',
            'mutatorName': 'ArithmeticOperator',
            'replacement': '-',
            'status': 'Survived',
            'location': {
                'start': {'line': 100 + i, 'column': 1},
                'end': {'line': 100 + i, 'column': 9},
            },
        }
        for i in range(survived)
    ]
    return {
        'schemaVersion': '1.0',
        'thresholds': {'high': 80, 'low': 60},
        'files': {'src/bus.ts': {'language': 'typescript', 'source': 'x\n', 'mutants': mutants}},
    }


def stage(art: Path, *, repo: str, dirname: str, flat: bool) -> None:
    """Write one entry's artifact content, in either layout the transport produces."""
    dest = art if flat else art / f'stryker-{repo}-{dirname.replace("/", "_")}'
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'meta.json').write_text(json.dumps({'repo': repo, 'dir': dirname}))
    (dest / 'mutation.json').write_text(json.dumps(report()))


def aggregate(art: Path, repo: str, tmp: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    entries = tmp / f'entries-{repo}.txt'
    proc = subprocess.run(
        [
            sys.executable,
            str(AGGREGATE),
            '--repo',
            repo,
            '--artifacts-dir',
            str(art),
            '--week',
            '2026-09-13 22:00',
            '--run-url',
            'https://example.invalid/run/1',
            '--entries-marker-file',
            str(entries),
        ],
        capture_output=True,
        text=True,
    )
    return proc, entries


def case_flat_single_artifact(tmp: Path) -> None:
    """The defect: one reporting package dir, so no per-artifact directory exists."""
    art = tmp / 'artifacts'
    stage(art, repo='reactive', dirname='.', flat=True)
    proc, entries = aggregate(art, 'reactive', tmp)
    check('flat layout: exits 0', ok=proc.returncode == 0, detail=proc.stderr)
    check(
        'flat layout: the report is found', ok='found 1 report' in proc.stderr, detail=proc.stderr
    )
    check(
        'flat layout: the entries marker is 1, so the workflow updates the issue',
        ok=entries.is_file() and entries.read_text().strip() == '1',
        detail=entries.read_text() if entries.is_file() else 'marker missing',
    )
    check("flat layout: the body carries this week's row", ok='| 2026-09-13 22:00 |' in proc.stdout)


def case_nested_multi_artifact(tmp: Path) -> None:
    """The N>1 layout must keep working, and multi-dir repos aggregate into one row."""
    art = tmp / 'artifacts'
    stage(art, repo='vibekit', dirname='static-src', flat=False)
    stage(art, repo='vibekit', dirname='web', flat=False)
    proc, entries = aggregate(art, 'vibekit', tmp)
    check('nested layout: exits 0', ok=proc.returncode == 0, detail=proc.stderr)
    check(
        'nested layout: both reports are found',
        ok='found 2 report' in proc.stderr,
        detail=proc.stderr,
    )
    check(
        'nested layout: the entries marker is 2',
        ok=entries.is_file() and entries.read_text().strip() == '2',
    )


def case_other_repos_are_ignored(tmp: Path) -> None:
    """Every artifact of the run is downloaded, so the repo filter is load-bearing."""
    art = tmp / 'artifacts'
    stage(art, repo='mine', dirname='.', flat=False)
    stage(art, repo='theirs', dirname='.', flat=False)
    proc, entries = aggregate(art, 'mine', tmp)
    check('repo filter: exits 0', ok=proc.returncode == 0, detail=proc.stderr)
    check(
        'repo filter: only this repo is counted',
        ok='found 1 report' in proc.stderr,
        detail=proc.stderr,
    )
    check(
        'repo filter: the entries marker counts only this repo',
        ok=entries.is_file() and entries.read_text().strip() == '1',
    )


def case_no_artifacts_is_zero_entries(tmp: Path) -> None:
    """Nothing downloaded must report 0 entries, which the workflow reads as leave-alone."""
    art = tmp / 'artifacts'
    art.mkdir(parents=True)
    proc, entries = aggregate(art, 'reactive', tmp)
    check('empty: exits 0', ok=proc.returncode == 0, detail=proc.stderr)
    check(
        'empty: the entries marker is 0',
        ok=entries.is_file() and entries.read_text().strip() == '0',
    )


def main() -> int:
    if not AGGREGATE.is_file():
        print(f'missing script: {AGGREGATE}')
        return 1
    for case in (
        case_flat_single_artifact,
        case_nested_multi_artifact,
        case_other_repos_are_ignored,
        case_no_artifacts_is_zero_entries,
    ):
        print(f'{case.__name__}:')
        with tempfile.TemporaryDirectory() as td:
            case(Path(td))
    print()
    if FAILURES:
        print(f'FAILED ({len(FAILURES)}): ' + ', '.join(FAILURES))
        return 1
    print('all stryker-aggregate checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
