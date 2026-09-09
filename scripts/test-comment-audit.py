#!/usr/bin/env python3
"""Pin the contract of the comment-ratio gate shipped as actions/comment-audit.

The gate has no executor in this repository. `cplieger/ci` is on the script's own
EXCLUDED_REPOSITORIES denylist, so the one variant of the meta workflow's
"Comment ratio" step that runs here returns SKIP before reading a single file:
nothing exercises the scanner, the language line counters or the exclusion rules.
A defect in any of them merges green and then fails every consumer at once, one
tag and one Renovate hop later, which is backwards from the point of centralising
the check. That is the same failure the shell unit-test harness had before its
self-test was executed here.

So this probe is the gate at the source. It exercises the counters per language,
the three constructs whose state machines a line-at-a-time scanner gets wrong (a
Go block comment, a Go raw string literal, a shell heredoc), the exclusion
decisions, and the three verdicts end to end through a real git checkout.

Run: python3 scripts/test-comment-audit.py     (exit 0 = pass)
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent / 'actions' / 'comment-audit' / 'comment-audit.py'

FAILURES: list[str] = []


def check(name: str, *, ok: bool, detail: str = '') -> None:
    if ok:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}{": " + detail if detail else ""}')
        FAILURES.append(name)


def load_audit():
    """Import the action script, whose filename is not a Python identifier."""
    spec = importlib.util.spec_from_file_location('comment_audit', AUDIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules['comment_audit'] = module
    spec.loader.exec_module(module)
    return module


CA = load_audit()


def counts(name: str, body: str) -> tuple[int, int]:
    """Return (comment lines, code lines) for a file named `name`."""
    path = Path(name)
    kind = CA.source_kind(path)
    if kind is None:
        raise AssertionError(f'{name} is not recognised as source')
    counted = CA.scan(path, body, kind)
    return counted.comments, counted.code


# ---------------------------------------------------------------------------
# Line counters, per language
# ---------------------------------------------------------------------------


def test_go_line_and_block_comments() -> None:
    got = counts(
        'a.go',
        'package main\n'
        '\n'
        '// a line comment\n'
        '/* one line block */\n'
        '/* opening\n'
        '   continued\n'
        '*/\n'
        'func main() {}\n'
        '/* trailing */ x := 1\n',
    )
    # 1 line + 1 one-line block + 3 block = 6 comments; package, func, x := 1.
    check('go line, block and trailing-code comments', ok=got == (6, 3), detail=str(got))


def test_go_blank_lines_count_as_neither() -> None:
    check(
        'go blank lines are not code and not comments',
        ok=counts('a.go', 'package main\n\n\n\nvar x = 1\n') == (0, 2),
    )


def test_go_raw_string_holding_comment_markers_is_code() -> None:
    got = counts(
        'a.go',
        'package main\nvar q = `\n// not a comment\n/* also not */\n`\n',
    )
    check(
        'go raw string literal body counts as code',
        ok=got == (0, 5),
        detail=f'{got} (a backtick state machine regression reads the body as comments)',
    )


def test_go_url_in_code_is_not_a_comment() -> None:
    check(
        'a // inside a Go string is code',
        ok=counts('a.go', 'package main\nvar u = "https://example.com"\n') == (0, 2),
    )


def test_shell_comments_and_shebang() -> None:
    got = counts(
        'a.sh',
        '#!/usr/bin/env bash\n'
        '# a comment\n'
        'set -euo pipefail\n'
        'echo hi  # trailing, so the line is code\n',
    )
    check('shell shebang is code, leading # is a comment', ok=got == (1, 3), detail=str(got))


def test_shell_heredoc_body_is_code() -> None:
    got = counts(
        'a.sh',
        'cat <<EOF\n# inside a heredoc\nstill inside\nEOF\n# a real comment\n',
    )
    check(
        'shell heredoc body counts as code',
        ok=got == (1, 4),
        detail=f'{got} (a heredoc regression reads the body as comments)',
    )


def test_shell_quoted_heredoc_delimiter() -> None:
    got = counts('a.sh', "cat <<'PY'\n# inside\nPY\n")
    check('quoted heredoc delimiter is honoured', ok=got == (0, 3), detail=str(got))


def test_dockerfile_directives_are_code() -> None:
    got = counts(
        'Dockerfile',
        '# syntax=docker/dockerfile:1\n# a real comment\nFROM scratch\n',
    )
    check(
        'Dockerfile syntax directive is code, prose is a comment',
        ok=got == (1, 2),
        detail=str(got),
    )


def test_python_counters() -> None:
    counted = CA.scan_python(
        Path('a.py'),
        '#!/usr/bin/env python3\n'
        '"""A docstring, which is code."""\n'
        '# a comment\n'
        'x = 1  # trailing\n',
    )
    got = (counted.comments, counted.code)
    check(
        'python shebang is code, docstring is code, trailing # is not double counted',
        ok=got == (1, 3),
        detail=str(got),
    )


def test_python_unparsable_file_is_loud() -> None:
    try:
        CA.scan_python(Path('a.py'), 'def (:\n')
    except RuntimeError:
        check('an untokenizable python file raises rather than counting zero', ok=True)
        return
    check('an untokenizable python file raises rather than counting zero', ok=False)


def test_typescript_is_measured() -> None:
    check(
        'a .ts file is measured with slash comments',
        ok=counts('a.ts', '// c\nexport const x = 1;\n') == (1, 1),
    )


def test_unmeasured_suffixes() -> None:
    check(
        'markdown and yaml carry no comment budget',
        ok=CA.source_kind(Path('a.md')) is None and CA.source_kind(Path('a.yaml')) is None,
    )


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_tests_are_measured() -> None:
    for name in ('pkg/thing_test.go', 'src/a.test.ts', 'tests/helper.go'):
        check(
            f'test source is measured, not excluded ({name})',
            ok=CA.exclusion(Path(name), '') is None,
        )


def test_excluded_paths() -> None:
    cases = {
        'vendor/x/a.go': 'generated/vendor',
        'node_modules/x/a.js': 'generated/vendor',
        'a.gen.go': 'generated/vendor',
        'docs/example.go': 'docs/example',
        'vitest.config.ts': 'config/example',
        'eslint.config.base.mjs': 'config/example',
        'a.example.sh': 'config/example',
        'tests/image-smoke.sh': 'synced from cplieger/ci',
        'tests/shell/lib.sh': 'synced from cplieger/ci',
    }
    for name, reason in cases.items():
        got = CA.exclusion(Path(name), '')
        check(f'{name} excluded as {reason}', ok=got == reason, detail=str(got))


def test_generated_header_needs_both_markers() -> None:
    header = '// Code generated by tool. DO NOT EDIT.\npackage x\n'
    check(
        'a code-generated + do-not-edit header excludes the file',
        ok=CA.exclusion(Path('a.go'), header) == 'generated/vendor',
    )
    check(
        'a generated marker alone does not exclude the file',
        ok=CA.exclusion(Path('a.go'), '// Code generated by hand\npackage x\n') is None,
    )


def test_synced_path_beats_the_test_directory_rule() -> None:
    # Three of the four synced paths live under tests/, which the test-source
    # branch admits — so their check has to come first or they are measured.
    check(
        'a synced path under tests/ is still excluded',
        ok=CA.exclusion(Path('tests/shell/harness_test.sh'), '') == 'synced from cplieger/ci',
    )


# ---------------------------------------------------------------------------
# Verdicts, end to end through a real checkout
# ---------------------------------------------------------------------------


def make_repo(root: Path, files: dict[str, str], *, origin: str) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    env = {
        **os.environ,
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull,
    }
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


def test_lean_repo_passes(td: Path) -> None:
    body = 'package main\n' + ''.join(f'var x{i} = {i}\n' for i in range(50))
    make_repo(td, {'main.go': '// one comment\n' + body}, origin='cplieger/probe')
    got = run_audit(td)
    check(
        'a lean repo exits 0 and reports PASS',
        ok=got.returncode == 0 and 'Comment ratio: PASS' in got.stdout,
        detail=got.stdout.strip() + got.stderr.strip(),
    )


def test_comment_heavy_repo_fails(td: Path) -> None:
    body = 'package main\n' + ''.join(f'// c{i}\nvar x{i} = {i}\n' for i in range(50))
    make_repo(td, {'main.go': body}, origin='cplieger/probe')
    got = run_audit(td)
    check(
        'a repo over the ratio exits 1, reports FAIL and names the contributor',
        ok=(
            got.returncode == 1
            and 'Comment ratio: FAIL' in got.stdout
            and 'main.go' in got.stdout
            and '::error title=Excessive comments::' in got.stdout
        ),
        detail=got.stdout.strip(),
    )


def test_limit_boundary_is_inclusive(td: Path) -> None:
    # 11 comments over 20 code lines is 0.550, exactly the limit, and passes.
    body = 'package main\n' + ''.join(f'var x{i} = {i}\n' for i in range(19))
    make_repo(td, {'main.go': '// c\n' * 11 + body}, origin='cplieger/probe')
    got = run_audit(td)
    check(
        f'a ratio equal to the {CA.LIMIT:.2f} limit passes',
        ok=got.returncode == 0 and '= 0.550:1' in got.stdout,
        detail=got.stdout.strip(),
    )


def test_denylisted_repo_skips(td: Path) -> None:
    # Asserted as a literal set, not read from the module: iterating the module's
    # own value would make removing an entry invisible to this probe.
    expected = {'cplieger/ci', 'cplieger/.kiro', 'cplieger/homelab', 'cplieger/.github'}
    check(
        'the denylist is exactly the four non-app repositories',
        ok=expected == CA.EXCLUDED_REPOSITORIES,
        detail=str(sorted(CA.EXCLUDED_REPOSITORIES)),
    )
    make_repo(td, {'main.go': '// c\n' * 30 + 'package main\n'}, origin='cplieger/ci')
    for repository in sorted(expected):
        got = run_audit(td, repository=repository)
        check(
            f'{repository} skips with exit 0 despite being over the ratio',
            ok=got.returncode == 0 and 'Comment ratio: SKIP' in got.stdout,
            detail=got.stdout.strip(),
        )
    got = run_audit(td, repository='cplieger/some-app')
    check(
        'a repository off the denylist is measured',
        ok=got.returncode == 1 and 'Comment ratio: FAIL' in got.stdout,
        detail=got.stdout.strip(),
    )


def test_repository_comes_from_the_git_remote(td: Path) -> None:
    # The step passes no repository name, so the denylist depends on this.
    make_repo(td, {'main.go': '// c\n' * 30 + 'package main\n'}, origin='cplieger/ci')
    got = run_audit(td)
    check(
        'the repository name is read from origin when GITHUB_REPOSITORY is unset',
        ok=got.returncode == 0 and 'Comment ratio: SKIP' in got.stdout,
        detail=got.stdout.strip(),
    )


def test_repo_with_no_measurable_code_fails(td: Path) -> None:
    make_repo(td, {'README.md': 'no source here\n'}, origin='cplieger/probe')
    got = run_audit(td)
    check(
        'a repo with no measurable code fails rather than passing vacuously',
        ok=got.returncode == 1 and 'no executable code lines' in got.stdout,
        detail=got.stdout.strip(),
    )


def test_untracked_but_visible_file_is_measured(td: Path) -> None:
    # git ls-files --others --exclude-standard: new work is measured before it is
    # committed, and a gitignored file never is.
    make_repo(
        td,
        {
            '.gitignore': 'ignored.go\n',
            'ignored.go': '// c\n' * 40 + 'package main\n',
            'main.go': 'package main\n' + ''.join(f'var x{i} = {i}\n' for i in range(30)),
        },
        origin='cplieger/probe',
    )
    got = run_audit(td)
    check(
        'a gitignored file cannot move the ratio',
        ok=got.returncode == 0,
        detail=got.stdout.strip(),
    )
    check(
        'the uncommitted source file IS measured',
        ok='across 1 source files' in got.stdout,
        detail=got.stdout.strip(),
    )


def main() -> int:
    if not AUDIT.is_file():
        print(f'error: {AUDIT} not found', file=sys.stderr)
        return 2

    unit_tests = [
        test_go_line_and_block_comments,
        test_go_blank_lines_count_as_neither,
        test_go_raw_string_holding_comment_markers_is_code,
        test_go_url_in_code_is_not_a_comment,
        test_shell_comments_and_shebang,
        test_shell_heredoc_body_is_code,
        test_shell_quoted_heredoc_delimiter,
        test_dockerfile_directives_are_code,
        test_python_counters,
        test_python_unparsable_file_is_loud,
        test_typescript_is_measured,
        test_unmeasured_suffixes,
        test_tests_are_measured,
        test_excluded_paths,
        test_generated_header_needs_both_markers,
        test_synced_path_beats_the_test_directory_rule,
    ]
    repo_tests = [
        test_lean_repo_passes,
        test_comment_heavy_repo_fails,
        test_limit_boundary_is_inclusive,
        test_denylisted_repo_skips,
        test_repository_comes_from_the_git_remote,
        test_repo_with_no_measurable_code_fails,
        test_untracked_but_visible_file_is_measured,
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
    print('all comment-audit checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
