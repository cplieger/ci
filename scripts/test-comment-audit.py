#!/usr/bin/env python3
"""Pin the contract of the comment-ratio and private-pointer gates shipped as
actions/comment-audit.

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
import shutil
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


def test_go_raw_string_holding_a_double_quote_closes() -> None:
    got = counts(
        'a.go',
        'package main\nvar r = `https://example.com"x`\n// a comment after it\nvar y = 1\n',
    )
    check(
        'a Go raw string holding a double quote closes on its line',
        ok=got == (1, 3),
        detail=f'{got} (a quote inside the raw string read as opening one turns the rest into code)',
    )


def test_ts_template_with_escaped_backtick_closes() -> None:
    got = counts('a.ts', 'const t = `a \\` b`;\n// c\n')
    check(
        'an escaped backtick in a template literal does not close it',
        ok=got == (1, 1),
        detail=str(got),
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
        'scripts/repin-sha.sh': 'synced from cplieger/ci',
        'scripts/collect-licenses.sh': 'synced from cplieger/ci',
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
    # Three of the synced paths live under tests/, which the test-source
    # branch admits — so their check has to come first or they are measured.
    check(
        'a synced path under tests/ is still excluded',
        ok=CA.exclusion(Path('tests/shell/harness_test.sh'), '') == 'synced from cplieger/ci',
    )


# ---------------------------------------------------------------------------
# Private pointers
# ---------------------------------------------------------------------------

# Each names what a public reader cannot open, with the rule that must catch it.
POINTER_TRUE = {
    'AP1-23 keeps the bound': 'plan id',
    'the SE2-16 arm': 'plan id',
    'see CP12-3': 'plan id',
    'none usable (G4-23).': 'plan id',
    "A5-2's reproduction reversed": 'plan id',
    'reads every export (P-J14-3 point 2)': 'plan id',
    'The attempt-row spellings (CR-SE-10).': 'finding id',
    "CR-UX-1's recount": 'finding id',
    'refuses redirects (PLEX-LS-SEC-01)': 'finding id',
    'sends (DECISIONS 41, D-UA).': 'decision',
    'keeps it (D-UA) for this exception': 'letter id',
    'TestNoStoreEverywhere is BRIEF-8 over every route': 'document id',
    'opened inside its repository (ADR-0104).': 'document id',
    'The queue bounds (NFR4).': 'requirement id',
    'must reserve no revision (AC1).': 'requirement id',
    "checkCut asserts AC19's bar": 'requirement id',
    "decision 28's change cost": 'decision',
    'design 5.4 removes the source': 'design section',
    'the tick (design section 6).': 'design section',
    "the case grammar of the design's section 7.2.": 'design section',
    'DESIGN §7.1: a lock write': 'design section',
    "one script per row of DESIGN section 3's table": 'design section',
    'the corpus (its REPORT section 1)': 'design section',
    'the fit (reference-sync-research.md section 6)': 'private file',
    'recorded in FOLLOWUPS': 'private file',
    'as gate.json records': 'private file',
    'see PLAN.md': 'private file',
    'fixed in review round 3': 'review round',
    "refused, round 8's IgnoredFiles range": 'review round',
    'the review seat of the run': 'review round',
    'replays wf_21c4c7533f79f52d pf-build': 'run id',
    'written to _scratch/x/notes.md': 'scratch path',
}
# Each is public text one of the rules would reach for if drawn looser.
POINTER_FALSE = [
    'decodes UTF-8 and UTF-16LE',
    'the SHA-256 of the username, never SHA-1',
    'SHA3-256 instead of SHA-256',
    'an ISO-8601 time, ISO-8859-1 text',
    'RFC 9110 section 15 lists the codes',
    'RFC 9111 section 4.2.1 freshness',
    'Discovery 1.0 §3 requires the list',
    'PyJWT CVE-2026-102267; CWE-532',
    'x/crypto for GO-2026-5932',
    'HTTP/2 and HTTP-200 GraphQL failures',
    'https://github.com/owner/repo/issues/123',
    'access offers ML-DSA-44 and ML-KEM-768',
    'RFC 5737 TEST-NET-1, HMAC-SHA-1, AVD-DS-0002, DS-0026',
    'the X-UA-Compatible header',
    'ids are [a-zA-Z0-9]+',
    'a name like "JDK-21" or "Z3-9" is ordinary',
    'Show S01E01 E05-05 and S2 EP01-12',
    'Audio decoders: AAC, AC3, EAC3',
    'a Material Design 3 surface',
    'an orchestrator restarts the container',
    'a slot may round 1/64 px wider; round 240 KB',
    'Seat a repaired turn at its seq',
    'tests/perf/plan.json names the cells',
    'docs/design.md explains the cost model',
    'the plan is REPORT-ONLY BY DESIGN',
    'the P-256 key, AES-256-CTR, BSD-3-Clause, MPEG-2',
    'UTC-4 to UTC-3, a DPR-3 phone',
]


def pointer_rules_of(text: str) -> list[str]:
    counted = CA.FileCount('a.go', 1, 0, ((1, text),))
    return [item.rule for item in CA.pointers(counted)]


def test_pointer_rules_catch_each_private_shape() -> None:
    for text, rule in POINTER_TRUE.items():
        got = pointer_rules_of(text)
        check(f'pointer {rule}: {text!r}', ok=rule in got, detail=str(got))


def test_pointer_rules_pass_public_text() -> None:
    for text in POINTER_FALSE:
        got = pointer_rules_of(text)
        check(f'no pointer in {text!r}', ok=not got, detail=str(got))


def test_pointer_notes_per_language() -> None:
    cases = {
        'a.go': 'package a\n// see AP1-23\nvar x = 1 // and SE2-16\nvar u = "https://x//AP1-24"\n',
        'a.ts': '/* AP1-23\n * SE2-16 */\nconst x = 1; // CP12-3\n',
        'a.py': '"""AP1-23 docstring."""\n# SE2-16\nx = "CP12-3"  # G4-1\n',
        'a.sh': '#!/bin/sh\n# AP1-23\necho "#SE2-16" # CP12-3\ncat <<EOF\n# G4-1\nEOF\n',
        'Dockerfile': '# syntax=docker/dockerfile:1\n# AP1-23\nRUN true # SE2-16\n',
        'a.yaml': '# AP1-23\nkey: "#SE2-16" # CP12-3\n',
    }
    want = {
        'a.go': [2, 3],
        'a.ts': [1, 2, 3],
        'a.py': [1, 2, 3],
        'a.sh': [2, 3],
        'Dockerfile': [2, 3],
        'a.yaml': [1, 2],
    }
    for name, body in cases.items():
        path = Path(name)
        counted = CA.scan(path, body, CA.pointer_kind(path))
        got = sorted({item.line for item in CA.pointers(counted)})
        check(
            f'{name}: comments, trailing comments and docstrings are read, strings and heredocs are not',
            ok=got == want[name],
            detail=str(got),
        )


def test_go_test_literal_naming_a_plan_id() -> None:
    body = 'package a\nvar rows = []string{"MS2-15", `AP1-3`, "switched to X9-2", "E05-05"}\n'
    test_hits = CA.pointers(CA.scan(Path('a_test.go'), body, 'slash'))
    check(
        'a Go test literal holding only a plan id is refused',
        ok=[(item.rule, item.token) for item in test_hits]
        == [('plan id literal', 'MS2-15'), ('plan id literal', 'AP1-3')],
        detail=str(test_hits),
    )
    check(
        'a production Go literal is not test data',
        ok=not CA.pointers(CA.scan(Path('a.go'), body, 'slash')),
    )


def test_pointer_kind_covers_yaml_without_a_ratio_budget() -> None:
    check(
        'YAML is read for pointers and still carries no ratio budget',
        ok=CA.pointer_kind(Path('a.yml')) == 'hash' and CA.source_kind(Path('a.yml')) is None,
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


def run_audit(
    root: Path,
    *,
    repository: str | None = None,
    check: str = 'ratio',
    private: str | None = None,
    extra_env: dict[str, str] | None = None,
):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {'GITHUB_REPOSITORY', 'REPOSITORY_PRIVATE', 'GITHUB_EVENT_NAME'}
    }
    if repository is not None:
        env['GITHUB_REPOSITORY'] = repository
    if private is not None:
        env['REPOSITORY_PRIVATE'] = private
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(AUDIT), '--check', check, '--github'],
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
    expected = {'cplieger/ci', 'cplieger/.github'}
    check(
        'the denylist is exactly the two infrastructure repositories',
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


def test_pointer_verdicts(td: Path) -> None:
    make_repo(
        td,
        {
            'main.go': 'package main\n// The bound is AP1-23 (design 5.4).\nfunc main() {}\n',
            'docs/example.go': 'package docs\n// DECISIONS 4\n',
            'vendor/x/a.go': 'package x\n// AP1-23\n',
            'tests/shell/lib.sh': '# AP1-23\n',
            'clean.go': 'package main\n// UTF-8, RFC 9110 section 15.\n',
        },
        origin='cplieger/probe',
    )
    got = run_audit(td, check='pointers', private='false')
    check(
        'a pointer fails the pointer check with a file and line annotation',
        ok=(
            got.returncode == 1
            and 'Private pointers: FAIL' in got.stdout
            and '::error file=main.go,line=2,title=Private pointer::plan id AP1-23' in got.stdout
            and '"docs/example.go":2: decision' in got.stdout
        ),
        detail=got.stdout.strip() + got.stderr.strip(),
    )
    check(
        'vendored and synced files are not read for pointers',
        ok='vendor/' not in got.stdout and 'tests/shell/lib.sh' not in got.stdout,
        detail=got.stdout.strip(),
    )
    got = run_audit(td, check='pointers', private='true')
    check(
        'a private repository skips the pointer check',
        ok=got.returncode == 0 and 'Private pointers: SKIP' in got.stdout,
        detail=got.stdout.strip(),
    )
    got = run_audit(td, check='pointers', repository='cplieger/ci', private='false')
    check(
        'the ratio denylist does not exempt a repository from the pointer check',
        ok=got.returncode == 1,
        detail=got.stdout.strip(),
    )


def write_fake_gh(bin_dir: Path, *, answer: str, code: int) -> Path:
    """A gh stub that logs its arguments and prints answer; returns the log path."""
    log = bin_dir / 'gh-args.log'
    gh = bin_dir / 'gh'
    gh.write_text(f'#!/bin/sh\necho "$*" >> {log}\nprintf "%s\\n" "{answer}"\nexit {code}\n')
    gh.chmod(0o755)
    return log


def test_private_flag_falls_back_to_the_api(td: Path) -> None:
    make_repo(td, {'main.go': 'package main\n// AP1-23\nfunc main() {}\n'}, origin='cplieger/probe')
    git = shutil.which('git')
    assert git, 'git is required'
    for answer, code, want_rc, want, label in (
        ('true', 0, 0, 'Private pointers: SKIP', 'an API answer of true skips'),
        ('false', 0, 1, 'Private pointers: FAIL — 1 line', 'an API answer of false checks'),
        ('', 1, 2, 'refusing to treat the repository as public', 'a failed API call fails'),
        ('null', 0, 2, 'refusing to treat the repository as public', 'a non-boolean fails'),
    ):
        with tempfile.TemporaryDirectory() as bin_name:
            bin_dir = Path(bin_name)
            log = write_fake_gh(bin_dir, answer=answer, code=code)
            got = run_audit(
                td,
                check='pointers',
                private='',
                extra_env={
                    'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                    'GITHUB_EVENT_NAME': 'schedule',
                },
            )
            asked = log.read_text().strip() if log.exists() else ''
        check(
            f'an empty payload flag asks gh api: {label}',
            ok=(
                got.returncode == want_rc
                and want in got.stdout
                and asked == 'api repos/cplieger/probe --jq .private'
                and (want_rc != 2 or 'the schedule event payload' in got.stdout)
            ),
            detail=f'rc={got.returncode} asked={asked!r} {got.stdout.strip()}',
        )
    with tempfile.TemporaryDirectory() as bin_name:
        bin_dir = Path(bin_name)
        (bin_dir / 'git').symlink_to(git)
        got = run_audit(
            td,
            check='pointers',
            extra_env={'PATH': str(bin_dir), 'GITHUB_EVENT_NAME': 'schedule'},
        )
    check(
        'an unset payload flag with no gh installed fails closed and names the event',
        ok=(
            got.returncode == 2
            and 'the schedule event payload' in got.stdout
            and 'gh is not installed' in got.stdout
        ),
        detail=f'rc={got.returncode} {got.stdout.strip()}',
    )
    with tempfile.TemporaryDirectory() as bin_name:
        bin_dir = Path(bin_name)
        log = write_fake_gh(bin_dir, answer='true', code=0)
        got = run_audit(
            td,
            check='pointers',
            private='false',
            extra_env={'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'},
        )
        asked = log.exists()
    check(
        'a payload flag is trusted without asking the API',
        ok=got.returncode == 1 and not asked,
        detail=f'rc={got.returncode} asked={asked}',
    )


def test_clean_repo_passes_the_pointer_check(td: Path) -> None:
    make_repo(td, {'main.go': 'package main\n// UTF-8 per RFC 3629.\n'}, origin='cplieger/probe')
    got = run_audit(td, check='pointers', private='false')
    check(
        'a repository with no pointer passes',
        ok=got.returncode == 0 and 'Private pointers: PASS' in got.stdout,
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
        test_go_raw_string_holding_a_double_quote_closes,
        test_ts_template_with_escaped_backtick_closes,
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
        test_pointer_rules_catch_each_private_shape,
        test_pointer_rules_pass_public_text,
        test_pointer_notes_per_language,
        test_go_test_literal_naming_a_plan_id,
        test_pointer_kind_covers_yaml_without_a_ratio_budget,
    ]
    repo_tests = [
        test_lean_repo_passes,
        test_comment_heavy_repo_fails,
        test_limit_boundary_is_inclusive,
        test_denylisted_repo_skips,
        test_repository_comes_from_the_git_remote,
        test_repo_with_no_measurable_code_fails,
        test_untracked_but_visible_file_is_measured,
        test_pointer_verdicts,
        test_private_flag_falls_back_to_the_api,
        test_clean_repo_passes_the_pointer_check,
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
