"""Messages a person reads in a run log or a terminal (annotations, die/note/fail/warn calls,
raised errors, error and warning prints, stderr writes, argparse help, and audit.py's graded
findings) join clauses with neither a semicolon nor a dash. A message built in a list or a
variable before it is printed is not read. Older messages are listed in
testdata/message-text/legacy.txt."""

import ast
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / 'scripts' / 'testdata' / 'message-text' / 'legacy.txt'
EMITTER = re.compile(
    r'::(?:error|warning|notice)[^:\n]*::'
    r"|\b(?:die|note|fail|warn)\s+[\"']"
    r"|\b\w*Error\((?:[\w.]+,\s*)?f?[\"']"
    r"|\.error\(\s*f?[\"']"
    r"|print\(\s*f?[\"'](?:error|warning):"
    r"|print\(\s*f?[\"'](?=.*\bfile=sys\.stderr\b)"
    r"|\bstderr\.write\(\s*f?[\"']"
    r"|\bhelp=\s*f?[\"']"
    r"|\b(?:hard|warn)\.append\(\s*f?[\"']"
    r"|[\"']errors[\"'](?:\]|, \[\]\))\.append\(\s*f?[\"']"
)
JOINER = re.compile(r'(?<!\s);(?= \S)|[\u2013\u2014]')
# ruff format puts a long call's literals on the lines after its `(`, often split in two.
OPEN_PAREN = re.compile(r'\(\s*\n\s*')
SPLIT_LITERAL = re.compile(r'(["\'])[ \t]*\n\s*[rRbBuUfF]{0,2}(["\'])')


def production(name: str) -> bool:
    if not name.endswith(('.yaml', '.yml', '.sh', '.py')):
        return False
    if not name.startswith(('.github/workflows/', 'actions/', 'scripts/')):
        return False
    return not re.search(r'(^|/)test[-_]|testdata/', name)


def literal(line: str, m: re.Match) -> str:
    """The message text after the emitter, up to the quote that closes its literal; a shell
    message holding a command substitution runs to the end of the line."""
    quote = next((c for c in reversed(line[: m.end()]) if c in '"\''), '')
    rest = line[m.end() :]
    close = re.search(rf'(?<!\\){re.escape(quote)}', rest) if quote else None
    text = rest[: close.start()] if close else rest
    return rest if '$(' in text else text


def wrapped_calls(text: str):
    """(line number, call on one line) for each Python call spanning several lines."""
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call) and node.end_lineno > node.lineno:
            source = ast.get_source_segment(text, node)
            yield node.lineno, re.sub(r'\s*\n\s*', ' ', joined(OPEN_PAREN.sub('(', source)))


def joined(source: str) -> str:
    """Two literals split across lines as one. When their quotes differ, the second's quotes of
    the first's kind are escaped and its closing quote becomes the first's."""
    while m := SPLIT_LITERAL.search(source):
        rest = source[m.end() :]
        close = re.search(rf'(?<!\\){re.escape(m[2])}', rest)
        if m[1] != m[2] and close:
            body = re.sub(
                rf'(?<!\\){re.escape(m[1])}', lambda _: '\\' + m[1], rest[: close.start()]
            )
            rest = body + m[1] + rest[close.end() :]
        source = source[: m.start()] + rest
    return source


def keys(text: str, python: bool = False):
    """(line number, key) per joined clause; the key is the text three words either side."""
    lines = [(n, line) for n, line in enumerate(text.splitlines(), 1)]
    if python:
        lines += sorted(wrapped_calls(text))
    seen = set()
    for n, line in lines:
        if line.lstrip().startswith('#'):
            continue
        m = EMITTER.search(line)
        if not m:
            continue
        rest = literal(line, m)
        for j in JOINER.finditer(rest):
            before = re.search(r'(?:\S+\s+){0,2}\S*$', rest[: j.start()]).group()
            after = re.match(r'\s*(?:\S+\s+){0,2}\S*', rest[j.end() :]).group()
            key = (before + j.group() + after).strip()
            if key not in seen:
                seen.add(key)
                yield n, key


def tree_files() -> list[str]:
    out = subprocess.run(
        ['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted({n for n in out.split('\0') if n and production(n) and (ROOT / n).is_file()})


def legacy() -> set[str]:
    lines = LEGACY.read_text(encoding='utf-8').splitlines()
    return {line for line in lines if line and not line.startswith('#')}


def found() -> list[tuple[str, int, str]]:
    return [
        (name, n, key)
        for name in tree_files()
        for n, key in keys(
            (ROOT / name).read_text(encoding='utf-8', errors='replace'), name.endswith('.py')
        )
    ]


class MessageText(unittest.TestCase):
    def test_no_new_message_joins_clauses_with_a_semicolon_or_a_dash(self):
        allowed = legacy()
        new = [f'{name}:{n}: {key}' for name, n, key in found() if key not in allowed]
        self.assertEqual(new, [], 'write two sentences, or join them with a comma and a word')

    def test_every_legacy_entry_is_still_in_the_tree(self):
        gone = sorted(legacy() - {key for _, _, key in found()})
        self.assertEqual(gone, [], 'delete these lines from legacy.txt')

    def test_the_reader_finds_each_shape_and_skips_shell_punctuation(self):
        text = r"""echo "::error::the read failed; nothing was written"; exit 1
  *) die "renumber run ${id} concluded '${c}'; the publish stays held" ;;
raise GhError(f'{tag} names {x}; main not moved')
print(f'::warning::{repo}: sync failed — {err}')
print(f'warning: {tag} carries no bundle; its SBOM is not used')
note "one sentence, then another" ;;
echo "::notice::a label: a value"; exit 0
# die "a comment; not a message"
echo "plain log line; not read as a message"
    die "no build on npm$([ "$st" = a ] || echo " above"); the run stays held"
echo "::warning::${r}: is not {\"a\": 1}; read anyway"
"""
        self.assertEqual(
            list(keys(text)),
            [
                (1, 'the read failed; nothing was written'),
                (2, "${id} concluded '${c}'; the publish stays"),
                (3, '{tag} names {x}; main not moved'),
                (4, 'sync failed \u2014 {err}'),
                (5, 'carries no bundle; its SBOM is'),
                (10, 'echo " above"); the run stays'),
                (11, 'not {\\"a\\": 1}; read anyway'),
            ],
        )

    def test_the_reader_joins_a_wrapped_python_call(self):
        text = """def f(err, x, ap, hard, s):
    raise GhError(
        'classify-repos.py publishes no hook/'
        'ClassifyError; dominance cannot judge it'
    )
    print(
        f'error: {err}; refusing to proceed',
        file=sys.stderr,
    )
    raise ApiError(
        err.status,
        f'{err}; that is past the cap',
    ) from None
    print(f'could not read {x}; trying anyway', file=sys.stderr)
    sys.stderr.write(f"{x} is unreadable; nothing filed")
    ap.add_argument('--tag', help='one tag (repeatable; unknown tags fail)')
    hard.append(f"default_branch={x} (want main; dev is public only)")
    s.setdefault("errors", []).append("listing unreadable; nothing filed")
    log(
        'a plain log line; not a message',
        GhError('one sentence; then another'),
    )
    print('a plain stdout line; not a message')
    raise GhError(
        'one clause '
        "then; another"
    )
    raise GhError(
        'one clause '
        "it's then; another"
    )
"""
        self.assertEqual(
            sorted(keys(text, python=True)),
            [
                (2, 'publishes no hook/ClassifyError; dominance cannot judge'),
                (6, '{err}; refusing to proceed'),
                (10, '{err}; that is past'),
                (14, 'not read {x}; trying anyway'),
                (15, '{x} is unreadable; nothing filed'),
                (16, 'one tag (repeatable; unknown tags fail)'),
                (17, 'default_branch={x} (want main; dev is public'),
                (18, 'listing unreadable; nothing filed'),
                (21, 'one sentence; then another'),
                (24, 'one clause then; another'),
                (28, "clause it\\'s then; another"),
            ],
        )


if __name__ == '__main__':
    unittest.main()
