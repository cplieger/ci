#!/usr/bin/env python3
"""Pin the contract of patch-vitest-runner-separator.py.

That script is a stopgap whose whole value is what it does when its assumptions
break: it must patch a stock vitest-runner, refuse to touch a vitest 4 install,
and exit 3 the moment the anchor is gone so the weekly run goes red and someone
deletes it. None of that is exercised until Saturday 22:00 UTC, which is exactly
how the sibling stryker aggregate script sat broken here for weeks.

Run: python3 scripts/test-patch-vitest-runner-separator.py     (exit 0 = pass)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCHER = HERE / 'patch-vitest-runner-separator.py'

STOCK = "return nameParts.join(' ').trim();"
PATCHED = "return nameParts.join(' > ').trim();"
TARGETS = ('dist/src/test-helpers.js', 'dist/src/stryker-setup.js')

FAILURES: list[str] = []


def check(name: str, *, ok: bool, detail: str = '') -> None:
    if ok:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}{": " + detail if detail else ""}')
        FAILURES.append(name)


def collect_test_name(body: str) -> str:
    """A stand-in for the runner's collectTestName(), shaped like the real dist."""
    return f"""function collectTestName({{ name, suite }}) {{
    const nameParts = [name];
    let currentSuite = suite;
    while (currentSuite) {{
        nameParts.unshift(currentSuite.name);
        currentSuite = currentSuite.suite;
    }}
    {body}
}}
"""


def make_tree(
    root: Path,
    *,
    runner_version: str = '10.0.0',
    vitest_version: str | None = '5.0.0',
    runner: bool = True,
    body: str = STOCK,
) -> None:
    if vitest_version is not None:
        vitest = root / 'node_modules' / 'vitest'
        vitest.mkdir(parents=True)
        (vitest / 'package.json').write_text(
            f'{{"name": "vitest", "version": "{vitest_version}"}}', encoding='utf-8'
        )
    if not runner:
        return
    runner_dir = root / 'node_modules' / '@stryker-mutator' / 'vitest-runner'
    (runner_dir / 'dist' / 'src').mkdir(parents=True)
    (runner_dir / 'package.json').write_text(
        f'{{"name": "@stryker-mutator/vitest-runner", "version": "{runner_version}"}}',
        encoding='utf-8',
    )
    for rel in TARGETS:
        (runner_dir / rel).write_text(collect_test_name(body), encoding='utf-8')


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PATCHER), '--package-dir', str(root), *args],
        capture_output=True,
        text=True,
    )


def bodies(root: Path) -> list[str]:
    runner_dir = root / 'node_modules' / '@stryker-mutator' / 'vitest-runner'
    return [(runner_dir / rel).read_text(encoding='utf-8') for rel in TARGETS]


def case_patches_stock_vitest5() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root)
        first = run(root)
        check('stock + vitest 5 exits 0', ok=first.returncode == 0, detail=first.stdout)
        check(
            'both files carry the " > " join',
            ok=all(PATCHED in b and STOCK not in b for b in bodies(root)),
        )
        # Idempotent: a rerun (a workflow rerun, a second local invocation) is a no-op.
        second = run(root)
        check('rerun exits 0', ok=second.returncode == 0, detail=second.stdout)
        check('rerun reports already patched', ok='already patched' in second.stdout)
        check('rerun changed nothing', ok=all(b.count(PATCHED) == 1 for b in bodies(root)))


def case_check_mode_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root)
        before = bodies(root)
        proc = run(root, '--check')
        check('--check exits 0', ok=proc.returncode == 0, detail=proc.stdout)
        check('--check leaves both files untouched', ok=bodies(root) == before)


def case_vitest4_is_not_patched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root, vitest_version='4.1.11')
        before = bodies(root)
        proc = run(root)
        check('vitest 4 exits 0', ok=proc.returncode == 0, detail=proc.stdout)
        check('vitest 4 is left alone', ok=bodies(root) == before)
        check('vitest 4 says why it skipped', ok='not applicable' in proc.stdout)


def case_upstream_fix_fails_loudly() -> None:
    """The reason this script exists: upstream ships the fix, we go red, we delete it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(
            root,
            runner_version='10.1.0',
            body='return nameParts.join(separator).trim();',
        )
        proc = run(root)
        check('upstream fix exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}')
        check('upstream fix names the version', ok='10.1.0' in proc.stdout)
        check('upstream fix says delete the script', ok='DELETE this script' in proc.stdout)


def case_missing_anchor_fails_loudly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root, body='return nameParts.join(RUNTIME_SEP).trim();')
        proc = run(root)
        check('unknown shape exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}')
        check('unknown shape warns about the score', ok='mutation score' in proc.stdout)


def case_two_anchors_fails_loudly() -> None:
    """A file with two joins changed shape; a blind replace was never measured against it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root)
        runner_dir = root / 'node_modules' / '@stryker-mutator' / 'vitest-runner'
        doubled = runner_dir / TARGETS[0]
        doubled.write_text(collect_test_name(STOCK) + collect_test_name(STOCK), encoding='utf-8')
        proc = run(root)
        check('two anchors exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}')
        check('two anchors reports the count', ok='appears 2 times' in proc.stdout)


def case_no_runner_skips() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root, runner=False)
        proc = run(root)
        check('no runner exits 0', ok=proc.returncode == 0, detail=proc.stdout)
        check('no runner says so', ok='is not installed' in proc.stdout)


def case_no_vitest_skips() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root, vitest_version=None)
        before = bodies(root)
        proc = run(root)
        check('no vitest exits 0', ok=proc.returncode == 0, detail=proc.stdout)
        check('no vitest is left alone', ok=bodies(root) == before)


def main() -> int:
    if not PATCHER.is_file():
        print(f'FAIL  {PATCHER} is missing')
        return 1
    for case in (
        case_patches_stock_vitest5,
        case_check_mode_writes_nothing,
        case_vitest4_is_not_patched,
        case_upstream_fix_fails_loudly,
        case_missing_anchor_fails_loudly,
        case_two_anchors_fails_loudly,
        case_no_runner_skips,
        case_no_vitest_skips,
    ):
        print(f'{case.__name__}:')
        case()
    if FAILURES:
        print(f'\n{len(FAILURES)} failure(s): ' + ', '.join(FAILURES))
        return 1
    print('\nall checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
