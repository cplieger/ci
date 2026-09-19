#!/usr/bin/env python3
"""Pin the contract of the NOTICE gate shipped as actions/notice-audit.

The gate runs in every repo's `repo` job, this one included, so its own run here
proves only that cplieger/ci's NOTICE passes. This probe exercises the template
rules and the published-package-root discovery against fixture checkouts, in the
shape of scripts/test-comment-audit.py.

Run: python3 scripts/test-notice-audit.py     (exit 0 = pass)
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent / 'actions' / 'notice-audit' / 'notice-audit.py'

FAILURES: list[str] = []

TEMPLATE = 'probe\nCopyright 2026 cplieger\nhttps://github.com/cplieger/probe\n'


def check(name: str, *, ok: bool, detail: str = '') -> None:
    if ok:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}{": " + detail if detail else ""}')
        FAILURES.append(name)


def load_audit():
    spec = importlib.util.spec_from_file_location('notice_audit', AUDIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules['notice_audit'] = module
    spec.loader.exec_module(module)
    return module


NA = load_audit()


# ---------------------------------------------------------------------------
# Template rules
# ---------------------------------------------------------------------------


def test_template_passes() -> None:
    check(
        'the three-line template has no findings', ok=NA.template_findings(TEMPLATE, 'probe') == []
    )


def test_wrong_repo_name() -> None:
    got = NA.template_findings(TEMPLATE, 'other')
    check(
        'a repo name mismatch is reported on line 1 and line 3',
        ok=len(got) == 2 and 'line 1' in got[0] and 'line 3' in got[1],
        detail=str(got),
    )


def test_year_format() -> None:
    cases = {
        'probe\nCopyright 2026-2027 cplieger\nhttps://github.com/cplieger/probe\n': 'a year range',
        'probe\nCopyright (c) 2026 cplieger\nhttps://github.com/cplieger/probe\n': 'a (c) marker',
        'probe\nCopyright 2026 someone\nhttps://github.com/cplieger/probe\n': 'another owner',
        'probe\ncopyright 2026 cplieger\nhttps://github.com/cplieger/probe\n': 'lowercase copyright',
    }
    for body, label in cases.items():
        got = NA.template_findings(body, 'probe')
        check(
            f'{label} on line 2 is a finding',
            ok=len(got) == 1 and 'line 2' in got[0],
            detail=str(got),
        )


def test_line_endings_and_length() -> None:
    check(
        'a missing final newline is a finding',
        ok=NA.template_findings(TEMPLATE[:-1], 'probe') == ['NOTICE does not end with a newline'],
    )
    check(
        'CRLF endings are a finding',
        ok=len(NA.template_findings(TEMPLATE.replace('\n', '\r\n'), 'probe')) == 1,
    )
    check(
        'two lines are a finding',
        ok=NA.template_findings('probe\nCopyright 2026 cplieger\n', 'probe')
        == ['NOTICE has 2 line(s); the template has three'],
    )
    check(
        'an empty file is a finding',
        ok=NA.template_findings('', 'probe') == ['NOTICE does not end with a newline'],
    )


def test_extra_block() -> None:
    block = 'This font contains no glyph outlines from any other font.\nSecond line of the block.\n'
    check(
        'one extra block after one blank line is allowed',
        ok=NA.template_findings(TEMPLATE + '\n' + block, 'probe') == [],
    )
    got = NA.template_findings(TEMPLATE + block, 'probe')
    check(
        'an extra block with no blank line is a finding',
        ok=len(got) == 1 and 'line 4' in got[0],
        detail=str(got),
    )
    got = NA.template_findings(TEMPLATE + '\n', 'probe')
    check('a trailing blank line with no block is a finding', ok=len(got) == 1, detail=str(got))
    got = NA.template_findings(TEMPLATE + '\n' + block + '\nA second block.\n', 'probe')
    check('a second block is a finding', ok=len(got) == 1, detail=str(got))


def test_go_mod_roots_need_go_files() -> None:
    files = [
        Path(p)
        for p in (
            'go.mod',
            'main.go',
            'yamlenv/go.mod',
            'yamlenv/x.go',
            'probe/go.mod',
            'probe/internal/deep/y.go',
            'web/go.mod',
            'web/index.ts',
            'webby/z.go',
        )
    ]
    got = NA.package_roots(Path('/nonexistent'), files)
    check(
        'a nested go.mod counts with a .go file beside it or anywhere below it, '
        'never on a .go file in a sibling directory',
        ok=got == [Path('probe'), Path('yamlenv')],
        detail=str(got),
    )


# ---------------------------------------------------------------------------
# Verdicts, end to end through a real checkout
# ---------------------------------------------------------------------------


def make_repo(root: Path, files: dict[str, str], *, origin: str = 'cplieger/probe') -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}
    subprocess.run(['git', 'init', '-q'], cwd=root, check=True, env=env)
    subprocess.run(
        ['git', 'remote', 'add', 'origin', f'https://github.com/{origin}.git'],
        cwd=root,
        check=True,
        env=env,
    )


def run_audit(root: Path, *, repository: str | None = None):
    env = {key: value for key, value in os.environ.items() if key != 'GITHUB_REPOSITORY'}
    if repository is not None:
        env['GITHUB_REPOSITORY'] = repository
    return subprocess.run(
        [sys.executable, str(AUDIT), '--github'],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


LICENSE = 'the license text\n'


def test_template_repo_passes(td: Path) -> None:
    make_repo(td, {'NOTICE': TEMPLATE, 'LICENSE': LICENSE})
    got = run_audit(td)
    check(
        'a repo with the template NOTICE and no package roots passes',
        ok=got.returncode == 0
        and 'NOTICE audit: PASS' in got.stdout
        and '0 published' in got.stdout,
        detail=got.stdout.strip() + got.stderr.strip(),
    )


def test_missing_notice_fails(td: Path) -> None:
    make_repo(td, {'LICENSE': LICENSE})
    got = run_audit(td)
    check(
        'a repo with no NOTICE fails and annotates',
        ok=got.returncode == 1
        and 'NOTICE is missing' in got.stdout
        and '::error title=NOTICE audit::' in got.stdout,
        detail=got.stdout.strip(),
    )


def test_repository_name_sources(td: Path) -> None:
    make_repo(td, {'NOTICE': TEMPLATE, 'LICENSE': LICENSE}, origin='cplieger/probe')
    got = run_audit(td)
    check(
        'the repo name comes from origin when GITHUB_REPOSITORY is unset',
        ok=got.returncode == 0,
        detail=got.stdout,
    )
    got = run_audit(td, repository='cplieger/other')
    check(
        'GITHUB_REPOSITORY wins over origin',
        ok=got.returncode == 1 and "line 1 is 'probe'" in got.stdout,
        detail=got.stdout.strip(),
    )


def test_dotted_repo_name(td: Path) -> None:
    body = '.kiro\nCopyright 2026 cplieger\nhttps://github.com/cplieger/.kiro\n'
    make_repo(td, {'NOTICE': body, 'LICENSE': LICENSE}, origin='cplieger/.kiro')
    got = run_audit(td)
    check(
        'a dotted repo name such as .kiro is handled',
        ok=got.returncode == 0,
        detail=got.stdout.strip(),
    )


def test_package_roots_need_copies(td: Path) -> None:
    make_repo(
        td,
        {
            'NOTICE': TEMPLATE,
            'LICENSE': LICENSE,
            'web/jsr.json': '{"name": "@cplieger/probe"}',
            'web/LICENSE': LICENSE,
            'web/NOTICE': TEMPLATE,
            'lib/package.json': '{"name": "@cplieger/lib"}',
            'lib/LICENSE': LICENSE,
            'nested/go.mod': 'module github.com/cplieger/probe/nested\n',
            'nested/internal/x.go': 'package internal\n',
            'nested/LICENSE': 'a different license\n',
            'nested/NOTICE': TEMPLATE,
        },
    )
    got = run_audit(td)
    check(
        'a package root with both copies is clean', ok='web/' not in got.stdout, detail=got.stdout
    )
    check(
        'a named package.json root missing NOTICE is a finding',
        ok='lib/NOTICE is missing' in got.stdout,
        detail=got.stdout,
    )
    check(
        'a nested go.mod root (with .go files below it) with a differing LICENSE is a finding',
        ok='nested/LICENSE differs' in got.stdout,
        detail=got.stdout,
    )
    check(
        'the verdict is FAIL with exactly two findings',
        ok=got.returncode == 1 and '2 finding(s)' in got.stdout,
        detail=got.stdout,
    )


def test_non_publishing_manifests_are_not_roots(td: Path) -> None:
    make_repo(
        td,
        {
            'NOTICE': TEMPLATE,
            'LICENSE': LICENSE,
            'static-src/package.json': '{"devDependencies": {"typescript": "5"}}',
            'app/package.json': '{"name": "app", "private": true}',
            'internal/testdata/mod/go.mod': 'module fixture\n',
            'internal/testdata/mod/x.go': 'package fixture\n',
            'vendor/dep/go.mod': 'module dep\n',
            'vendor/dep/x.go': 'package dep\n',
            'web/go.mod': 'module web-ignore\n',
            'web/index.ts': 'export {};\n',
            'sibling/x.go': 'package sibling\n',
        },
    )
    got = run_audit(td)
    check(
        'nameless/private package.json, testdata/vendor go.mods and a .go-less go.mod '
        'are not package roots',
        ok=got.returncode == 0 and '0 published' in got.stdout,
        detail=got.stdout.strip(),
    )


def test_untracked_package_root_is_seen(td: Path) -> None:
    make_repo(td, {'NOTICE': TEMPLATE, 'LICENSE': LICENSE})
    (td / 'web').mkdir()
    (td / 'web' / 'jsr.json').write_text('{"name": "@cplieger/probe"}')
    got = run_audit(td)
    check(
        'an uncommitted package root is audited before it is committed',
        ok=got.returncode == 1 and 'web/LICENSE is missing' in got.stdout,
        detail=got.stdout.strip(),
    )


def main() -> int:
    if not AUDIT.is_file():
        print(f'error: {AUDIT} not found', file=sys.stderr)
        return 2
    unit_tests = [
        test_template_passes,
        test_wrong_repo_name,
        test_year_format,
        test_line_endings_and_length,
        test_extra_block,
        test_go_mod_roots_need_go_files,
    ]
    repo_tests = [
        test_template_repo_passes,
        test_missing_notice_fails,
        test_repository_name_sources,
        test_dotted_repo_name,
        test_package_roots_need_copies,
        test_non_publishing_manifests_are_not_roots,
        test_untracked_package_root_is_seen,
    ]
    for t in unit_tests:
        print(f'{t.__name__}:')
        t()
    for t in repo_tests:
        print(f'{t.__name__}:')
        with tempfile.TemporaryDirectory() as td:
            t(Path(td))
    print()
    if FAILURES:
        print(f'FAILED ({len(FAILURES)}): ' + ', '.join(FAILURES))
        return 1
    print('all notice-audit checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
