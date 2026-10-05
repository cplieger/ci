#!/usr/bin/env python3
"""Re-register vitest's browser-pool cancel listener on every run (stopgap).

runFiles() clears cancel listeners each run but createBrowserPool registers
pool.cancel() once, so a reused Vitest's bail stops emptying the browser queue
(https://github.com/vitest-dev/vitest/issues/11502).
Exit 0 patched, already patched or not applicable; 1 unreadable install; 2 usage;
3 anchor gone or upstream fixed it: delete this script, its weekly-stryker.yaml
call and its self-ci.yaml probe, never re-anchor; 4 ambiguous, nothing written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

VITEST = Path('node_modules') / 'vitest'
# Every dist file, because the chunk that defines the pool is renamed across
# releases (cli-api.*.js in vitest 4.1, index.*.js in 5.0).
DIST_GLOB = 'dist/**/*.js'
MARKER = 'function createBrowserPool'
DECLARATION = re.compile(r'^function createBrowserPool\(', re.MULTILINE)
# Present in any vitest that still ships a browser pool, whatever builds it.
BROWSER_POOL = 'BrowserPool'

ENSURE = '\t\t\tconst pool = ensurePool(project);\n'
CLEAR = '\t\t\tvitest.state.clearFiles(project, files.map((f) => f.filepath));'
CALL = '\t\t\tvitest.onCancel(() => pool.cancel());\n'
STOCK = ENSURE + CLEAR
FIXED = ENSURE + CALL + CLEAR

# Records the sha256 of the chunk this script wrote, so FIXED bytes without a
# matching stamp are upstream's fix rather than our own earlier write.
STAMP = '.patch-vitest-browser-bail'

# A cancel listener registered inside runWorkspaceTests, i.e. once per run.
# Recognised only to make the failure message say "upstream fixed it" instead
# of "the anchor moved".
PER_RUN_CANCEL = re.compile(r'\.onCancel\(\s*\(\)\s*=>\s*\{?\s*pool\.cancel\(\)')

EXIT_OK = 0
EXIT_IO = 1
EXIT_UPSTREAM_FIXED = 3
EXIT_REFUSED = 4

SELF = 'patch-vitest-browser-bail.py'


class InstallError(Exception):
    """vitest is installed but a file this script must trust cannot be read."""


def read_utf8(path: Path) -> str:
    # Two single-type excepts, deliberately: ruff's py314 target rewrites a
    # tuple form into PEP 758 (`except A, B:`), which the runner's system
    # python3 (3.12) rejects at PARSE time. Bytes, not Path.read_text: newline
    # translation would rewrite the whole chunk on write.
    try:
        return path.read_bytes().decode('utf-8')
    except OSError as exc:
        raise InstallError(f'{path}: {exc}') from exc
    except UnicodeDecodeError as exc:
        raise InstallError(f'{path}: {exc}') from exc


def read_version(package_json: Path) -> str:
    raw = read_utf8(package_json)
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise InstallError(f'{package_json}: not valid JSON: {exc}') from exc
    version = manifest.get('version') if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not version:
        raise InstallError(f'{package_json}: no string "version" field')
    return version


def stamp_line(rel: Path, text: str) -> str:
    return f'{hashlib.sha256(text.encode("utf-8")).hexdigest()}  {rel.as_posix()}\n'


def read_stamp(path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    return read_utf8(path)


def browser_pool_span(text: str, start: int) -> tuple[int, int] | None:
    end = text.find('\n}\n', start)
    return None if end == -1 else (start, end)


def classify(body: str, stamped: bool) -> tuple[str, str]:
    """Classify the createBrowserPool body: ok, patch, upstream or anchor."""
    if FIXED in body:
        if stamped:
            return 'ok', 'already patched'
        return 'upstream', 'createBrowserPool already registers pool.cancel() after ensurePool'
    if STOCK in body:
        return 'patch', 'stock anchor found once'
    run_body = body.partition('const runWorkspaceTests')[2]
    if PER_RUN_CANCEL.search(run_body):
        return 'upstream', 'createBrowserPool registers pool.cancel() inside each run'
    return 'anchor', 'the stock anchor is not inside createBrowserPool'


def upstream_changed(cause: str, vitest_version: str) -> int:
    print(
        f'\n{SELF}: upstream appears to have {cause} the browser pool cancel '
        f'listener in vitest {vitest_version}. DELETE {SELF}, its call in '
        '.github/workflows/weekly-stryker.yaml, and its probe in '
        '.github/workflows/self-ci.yaml (scripts/test-patch-vitest-browser-bail.py). '
        'Do not re-anchor it.'
    )
    return EXIT_UPSTREAM_FIXED


def refuse(detail: str, vitest_version: str) -> int:
    print(f'  REFUSE    {detail}')
    print(f'\n{SELF} refused to patch vitest {vitest_version}; nothing was written.')
    return EXIT_REFUSED


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
    vitest_dir = root / VITEST
    # lexists: a dangling node_modules/vitest symlink is a broken install, not an absent one.
    if not os.path.lexists(vitest_dir):
        print(f'SKIP  vitest is not installed under {root} — nothing to patch')
        return EXIT_OK
    try:
        vitest_version = read_version(vitest_dir / 'package.json')
        chunks = [
            (chunk.relative_to(vitest_dir), read_utf8(chunk))
            for chunk in sorted(vitest_dir.glob(DIST_GLOB))
        ]
        stamp = read_stamp(vitest_dir / STAMP)
    except InstallError as exc:
        print(f'  IO        {exc}')
        print(f'\n{SELF}: vitest is installed under {root} but could not be read.')
        return EXIT_IO
    print(f'vitest {vitest_version}')

    mentions = sum(text.count(MARKER) for _, text in chunks)
    declarations = [
        (rel, text, match.start()) for rel, text in chunks for match in DECLARATION.finditer(text)
    ]
    stock = sum(text.count(STOCK) for _, text in chunks)
    fixed = sum(text.count(FIXED) for _, text in chunks)

    if not mentions and not any(BROWSER_POOL in text for _, text in chunks):
        print(f'SKIP  vitest {vitest_version} ships no browser pool — {SELF} not applicable')
        return EXIT_OK
    if mentions != len(declarations):
        return refuse(
            f'`{MARKER}` occurs x{mentions} but only x{len(declarations)} as a declaration',
            vitest_version,
        )
    if not declarations:
        print('  ANCHOR    a browser pool ships, but no dist file defines createBrowserPool')
        return upstream_changed('changed', vitest_version)
    files = sorted({rel for rel, _, _ in declarations})
    if len(files) > 1:
        names = ', '.join(str(rel) for rel in files)
        return refuse(
            f'createBrowserPool is defined in {len(files)} files ({names})', vitest_version
        )
    if len(declarations) > 1:
        return refuse(
            f'createBrowserPool is defined x{len(declarations)} in {files[0]}', vitest_version
        )
    # Counted across every chunk, so a second copy anywhere in dist refuses.
    if stock + fixed > 1:
        return refuse(
            f'stock anchor x{stock}, fixed block x{fixed} across dist; expected exactly one',
            vitest_version,
        )

    rel, text, start = declarations[0]
    span = browser_pool_span(text, start)
    if span is None:
        verdict, detail = 'anchor', 'createBrowserPool has no closing brace at column 0'
    else:
        verdict, detail = classify(text[span[0] : span[1]], stamp == stamp_line(rel, text))
    print(f'  {verdict.upper():9} {rel}: {detail}')

    if verdict == 'ok':
        return EXIT_OK
    if verdict in {'upstream', 'anchor'}:
        return upstream_changed('fixed' if verdict == 'upstream' else 'changed', vitest_version)

    if args.check:
        print('  would insert the per-run cancel listener')
        return EXIT_OK
    start, end = span
    patched = text[:start] + text[start:end].replace(STOCK, FIXED, 1) + text[end:]
    # Stamp first: a failed chunk write then leaves the stock anchor and a stale
    # stamp, which the next run simply patches again.
    try:
        (vitest_dir / STAMP).write_bytes(stamp_line(rel, patched).encode('utf-8'))
        (vitest_dir / rel).write_bytes(patched.encode('utf-8'))
    except OSError as exc:
        print(f'  IO        {exc}')
        return EXIT_IO
    print('  patched 1 occurrence')
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
