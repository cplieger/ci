#!/usr/bin/env python3
"""Teach @stryker-mutator/vitest-runner the test-name separator vitest 5 matches on.

STOPGAP. It exists to be deleted, and it fails the run the moment it can be.

The defect it works around: vitest 5 changed `testNamePattern` to match a test's
full name with the suite chain joined by `" > "` (space, gt, space — the string
the reporter prints; vitest's migration guide renders it as `"> "`, but the
separator MEASURED against vitest 5.0.0 is `" > "`).
`@stryker-mutator/vitest-runner@10.0.0` still joins with a bare space, so the
per-mutant filter it assigns to `project.config.testNamePattern` matches NO test
nested inside a `describe`. Zero tests run per mutant, nothing can fail, every
mutant is reported Survived, and the published mutation score is 0.00% for a
suite that was killing 90%+ of its mutants a week earlier.

The dry run is unaffected (it passes no `testIds`, so `testNamePattern` stays
`undefined` and everything runs), which is why coverage looks healthy while
detection is dead and why the whole failure is invisible to a check that only
asks whether Stryker completed.

Measured on `reactive`'s `src/bus.ts`, same file and same command, 36 mutants:

    vitest 4.1.11                     33 killed / 3 survived / 91.67% / 1.00 tests per mutant
    vitest 5.0.0                       0 killed / 36 survived /  0.00% / 0.00 tests per mutant
    vitest 5.0.0 + this patch         33 killed / 3 survived / 91.67% / 1.00 tests per mutant

So the separator is the whole defect: patched, vitest 5 reproduces the vitest 4
baseline down to which three mutants survive.

BOTH copies of the join have to move. `test-helpers.js` feeds the reported test
ids and the per-mutant filter; `stryker-setup.js` carries its own copy and feeds
`currentTestId` for coverage. Moving one alone desynchronises the two.

HOW THIS REMOVES ITSELF. The replacement is anchored on the exact stock text, so
when upstream ships the real fix (stryker-js#6214 or #6220, both open and
unreviewed as of 2026-09-13) and Renovate bumps the runner, the anchor is gone,
this script exits 3, and the weekly run goes red naming the file. That red is the
signal to DELETE this script, its call in weekly-stryker.yaml, and its probe in
test-patch-vitest-runner-separator.py. Do not re-anchor it on the new shape.

The zero-kill guard in weekly-stryker.yaml stays the backstop underneath: a run
that reaches Stryker with an unpatched runner still refuses to publish a score,
so a silent skip here cannot put a wrong number on a badge.

Usage (from a TS package dir, or with --package-dir):

    python3 scripts/patch-vitest-runner-separator.py
    python3 scripts/patch-vitest-runner-separator.py --check   # report, write nothing

Exit codes:

    0  patched, already patched, or not applicable (vitest < 5, or no vitest runner)
    1  a file the runner ships could not be read or written
    2  bad usage
    3  an anchor is absent: upstream has shipped the fix. Delete this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RUNNER = '@stryker-mutator/vitest-runner'

# The two copies of collectTestName()'s join, and what each becomes.
TARGETS = ('dist/src/test-helpers.js', 'dist/src/stryker-setup.js')
STOCK = "return nameParts.join(' ').trim();"
PATCHED = "return nameParts.join(' > ').trim();"

# Shapes the upstream fix introduces. Recognised only to make the failure
# message say "upstream fixed it" instead of "the anchor moved".
UPSTREAM_MARKERS = ('testNameSeparator', 'nameParts.join(separator)')

EXIT_OK = 0
EXIT_IO = 1
EXIT_UPSTREAM_FIXED = 3


def read_version(package_json: Path) -> str | None:
    # Two single-type excepts, deliberately: a tuple form gets reformatted into
    # PEP 758 (`except A, B:`) against this repo's py314 target, and the runner
    # that executes this script is ubuntu-24.04's system python3 (3.12), which
    # rejects that at PARSE time. Same trap that left stryker-aggregate.py
    # unparseable for weeks. Nothing here needs 3.14 syntax, so it uses none.
    try:
        raw = package_json.read_text(encoding='utf-8')
    except OSError:
        return None
    try:
        version = json.loads(raw).get('version')
    except ValueError:
        return None
    return version if isinstance(version, str) else None


def major(version: str | None) -> int | None:
    if not version:
        return None
    try:
        return int(version.lstrip('v').split('.')[0])
    except ValueError:
        return None


def patch_file(path: Path, *, check_only: bool) -> tuple[str, str]:
    """Return (verdict, detail). Verdict is one of ok, patched, upstream, anchor, io."""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        return 'io', str(exc)

    if PATCHED in text:
        return 'ok', 'already patched'

    hits = text.count(STOCK)
    if hits == 0:
        found = [m for m in UPSTREAM_MARKERS if m in text]
        if found:
            return 'upstream', f'carries {found[0]}'
        return 'anchor', 'neither the stock join nor a known upstream shape is present'
    if hits > 1:
        # Two joins in one file means the file's shape changed and a blind
        # replace would patch something this was never measured against.
        return 'anchor', f'the stock join appears {hits} times, expected exactly 1'

    if check_only:
        return 'patched', 'would patch 1 occurrence'
    try:
        path.write_text(text.replace(STOCK, PATCHED, 1), encoding='utf-8')
    except OSError as exc:
        return 'io', str(exc)
    return 'patched', 'patched 1 occurrence'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--package-dir',
        default='.',
        help='directory holding the package.json whose node_modules to patch (default: cwd)',
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='report what would happen and write nothing',
    )
    args = parser.parse_args(argv)

    root = Path(args.package_dir).resolve()
    runner_dir = root / 'node_modules' / RUNNER
    if not runner_dir.is_dir():
        print(f'SKIP  {RUNNER} is not installed under {root} — nothing to patch')
        return EXIT_OK

    runner_version = read_version(runner_dir / 'package.json')
    vitest_version = read_version(root / 'node_modules' / 'vitest' / 'package.json')
    vitest_major = major(vitest_version)
    print(f'{RUNNER} {runner_version or "unknown"}, vitest {vitest_version or "not installed"}')

    if vitest_major is None:
        print(f'SKIP  cannot read the installed vitest version under {root} — nothing to patch')
        return EXIT_OK
    if vitest_major < 5:
        # The bare-space join is CORRECT for vitest 4, so patching would break it.
        print(f'SKIP  vitest {vitest_version} joins with a space already — patch not applicable')
        return EXIT_OK

    verdicts: dict[str, tuple[str, str]] = {}
    for rel in TARGETS:
        verdicts[rel] = patch_file(runner_dir / rel, check_only=args.check)
        verdict, detail = verdicts[rel]
        print(f'  {verdict.upper():9} {rel}: {detail}')

    if any(v == 'upstream' for v, _ in verdicts.values()):
        print(
            f'\nupstream appears to have fixed this in {RUNNER} {runner_version}. '
            'DELETE this script, its call in .github/workflows/weekly-stryker.yaml, '
            'and scripts/test-patch-vitest-runner-separator.py. Do not re-anchor it.'
        )
        return EXIT_UPSTREAM_FIXED
    if any(v == 'anchor' for v, _ in verdicts.values()):
        print(
            f'\nthe patch anchor is gone from {RUNNER} {runner_version} and no known '
            'upstream fix shape is present. Re-read the two files before trusting any '
            'mutation score from this package.'
        )
        return EXIT_UPSTREAM_FIXED
    if any(v == 'io' for v, _ in verdicts.values()):
        return EXIT_IO
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
