#!/usr/bin/env python3
"""Which changed files a non-test //go:embed names, so they ship though the
release excludes call them docs (grelot's `grelot help` is an embedded .md).

`go-embeds.py <commit> < paths` prints the paths a directive at <commit> names
(path-significance.sh). `go-embeds.py --with-commits <range> [--include G]
[--exclude G] [--config cliff.toml] [--positions FILE]` prints, NUL-separated
and oldest first, the message of each commit git-cliff's path filter drops
though it changes an embedded file (compute.sh and render-notes.sh); FILE gets
each commit's index in the range, by sha and by printed message. Exits 2 when
git cannot read the tree.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import subprocess
import sys
import tomllib


def git(*args: str) -> str:
    proc = subprocess.run(['git', *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(f'go-embeds: git {" ".join(args)} failed: {proc.stderr.strip()}\n')
        sys.exit(2)
    return proc.stdout


def patterns(line: str) -> list[str]:
    """The patterns of one //go:embed line's argument text."""
    out, i, n = [], 0, len(line)
    while i < n:
        if line[i].isspace():
            i += 1
            continue
        if line[i] == '`':
            j = line.find('`', i + 1)
            if j < 0:
                return out
            out.append(line[i + 1 : j])
            i = j + 1
        elif line[i] == '"':
            j = i + 1
            while j < n and line[j] != '"':
                j += 2 if line[j] == '\\' else 1
            try:
                out.append(ast.literal_eval(line[i : j + 1]))
            except SyntaxError, ValueError:
                return out
            i = j + 1
        else:
            j = i
            while j < n and not line[j].isspace():
                j += 1
            out.append(line[i:j])
            i = j
    return out


NEVER = re.compile(r'(?!)')


def translate(pattern: str) -> str:
    """path.Match for one path element as a regex: * and ? never cross a slash,
    and a class is `[` then an optional `^`, ranges `lo-hi` or single characters
    (each backslash-escapable), then `]`. Raises ValueError on a pattern
    path.Match rejects."""
    out, i, n = [], 0, len(pattern)

    def esc(k: int) -> tuple[str, int]:
        if k >= n or pattern[k] in '-]':
            raise ValueError(pattern)
        if pattern[k] == '\\':
            k += 1
            if k >= n:
                raise ValueError(pattern)
        return pattern[k], k + 1

    while i < n:
        c = pattern[i]
        if c in '*?':
            out.append('[^/]*' if c == '*' else '[^/]')
            i += 1
        elif c == '\\':
            if i + 1 >= n:
                raise ValueError(pattern)
            out.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == '[':
            i += 1
            neg = i < n and pattern[i] == '^'
            i += neg
            ranges = []
            while not (i < n and pattern[i] == ']' and ranges):
                lo, i = esc(i)
                hi = lo
                if i < n and pattern[i] == '-':
                    hi, i = esc(i + 1)
                ranges.append(re.escape(lo) if lo == hi else f'{re.escape(lo)}-{re.escape(hi)}')
            i += 1
            out.append(f'[{"^/" if neg else ""}{"".join(ranges)}]')
        else:
            out.append(re.escape(c))
            i += 1
    return ''.join(out) + r'\Z'


def element_re(pattern: str) -> re.Pattern[str]:
    """translate's regex, compiled; a pattern path.Match rejects matches nothing,
    which Go's build refuses anyway."""
    try:
        return re.compile(translate(pattern))
    except ValueError, re.error:
        return NEVER


def embeds(pattern: str, rel: str) -> bool:
    """Whether the directive pattern names rel, a path under the package dir."""
    keep_hidden = pattern.startswith('all:')
    if keep_hidden:
        pattern = pattern[len('all:') :]
    ps, rs = pattern.split('/'), rel.split('/')
    if len(ps) > len(rs):
        return False
    if not all(element_re(p).match(r) for p, r in zip(ps, rs, strict=False)):
        return False
    below = rs[len(ps) :]
    return keep_hidden or not any(e.startswith(('.', '_')) for e in below)


DIRECTIVE = r'^[[:space:]]*//go:(embed|build)[[:space:]]'


def go_skips(name: str) -> bool:
    """Whether Go builds no package from this file's directory or name: a
    directory or file named testdata or vendor, or starting with `.` or `_`."""
    return any(e in ('testdata', 'vendor') or e.startswith(('.', '_')) for e in name.split('/'))


def glob_re(pattern: str) -> re.Pattern[str]:
    """git-cliff's glob (Rust `glob`, require_literal_separator off): a `**/`
    component matches zero or more directories, `**` and `*` otherwise cross
    slashes, `?` is one character, and a pattern ending in `/` means the
    directory's tree."""
    if pattern.endswith('/'):
        pattern += '**'
    pattern = pattern.removeprefix('./')
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith('**/', i) and (i == 0 or pattern[i - 1] == '/'):
            # A `**` component matches zero or more directories.
            out.append('(?:.*/)?')
            i += 2
        elif c == '*':
            out.append('.*')
            while i + 1 < len(pattern) and pattern[i + 1] == '*':
                i += 1
        elif c == '?':
            out.append('.')
        elif c == '[':
            j = pattern.find(']', i + 2)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j]
                neg = body.startswith('!')
                body = (body[1:] if neg else body).replace('\\', '\\\\')
                # Rust glob negates with `!` only; a leading `^` is a character.
                if body.startswith('^'):
                    body = '\\' + body
                out.append(f'[{"^" if neg else ""}{body}]')
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile(''.join(out) + r'\Z')


@functools.cache
def directives_at(commit: str) -> tuple[frozenset[str], dict[str, list[str]]]:
    """The module directories and each directory's //go:embed patterns at
    <commit>, read once: a file Go never builds is skipped (go_skips, or
    `//go:build ignore`); other constraints are not evaluated, and a directive
    line inside a raw string or a /* */ comment counts, either of which can
    release a commit that ships nothing."""
    modules = {
        n.rsplit('/', 1)[0] if '/' in n else ''
        for n in git('ls-tree', '-r', '--name-only', commit).splitlines()
        if n == 'go.mod' or n.endswith('/go.mod')
    }
    # One read of every directive: `git grep` exits 1 when nothing matches.
    proc = subprocess.run(
        [
            'git',
            'grep',
            '--no-color',
            '-I',
            '--full-name',
            '-n',
            '--null',
            '-E',
            DIRECTIVE,
            commit,
            '--',
            '*.go',
            ':(exclude)*_test.go',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode > 1:
        sys.stderr.write(f'go-embeds: git grep failed: {proc.stderr.strip()}\n')
        sys.exit(2)
    lines = []
    ignored = set()
    for line in proc.stdout.splitlines():
        name, _, rest = line.partition('\0')
        _, _, text = rest.partition('\0')
        name = name.removeprefix(f'{commit}:')
        if '//go:build' in text:
            # `//go:build ignore` keeps a file out of every build; other
            # constraints hold on some platform, so their files count.
            if text.split('//go:build', 1)[1].strip() == 'ignore':
                ignored.add(name)
        elif not go_skips(name):
            lines.append((name, text))
    directives: dict[str, list[str]] = {}
    for name, text in lines:
        if name in ignored:
            continue
        d = name.rsplit('/', 1)[0] if '/' in name else ''
        directives.setdefault(d, []).extend(patterns(text.split('//go:embed', 1)[1]))
    return frozenset(modules), directives


def embedded_at(commit: str, paths: list[str]) -> list[str]:
    """The paths a non-test //go:embed at <commit> names, by Go's rules
    (https://pkg.go.dev/embed): a directive in a file's directory or a parent up
    to the nearest go.mod."""
    modules, directives = directives_at(commit)
    found = []
    for path in paths:
        parts = path.split('/')
        for k in range(len(parts) - 1, -1, -1):
            d = '/'.join(parts[:k])
            rel = '/'.join(parts[k:])
            if any(embeds(p, rel) for p in directives.get(d, ())):
                found.append(path)
                break
            if d in modules:
                break
    return found


def with_commits(args: list[str]) -> int:
    """The --with-commits mode. Each message is printed whole, with no sha:
    git-cliff 2.14.2 parses a `<sha> <message>` --with-commit only to the first
    newline, which would lose a BREAKING CHANGE footer. Such a commit has an
    empty id in git-cliff's context, so --positions also keys each one by its
    message, for render-notes.sh to sort the release in the range's order."""
    # Scope globs (--include, --exclude) say which lane a file belongs to; the
    # config's exclude_paths say which files are docs. git-cliff drops a commit
    # unless a changed file is in scope and not a doc; this keeps a dropped
    # one when a file in scope is a doc a directive embeds.
    rng, includes, scope_out, docs, positions = args[0], [], [], [], None
    rest = iter(args[1:])
    for flag in rest:
        value = next(rest, None)
        if flag not in ('--include', '--exclude', '--config', '--positions') or value is None:
            sys.stderr.write(f'go-embeds: unexpected argument {flag!r}\n')
            return 2
        if flag == '--positions':
            positions = value
        elif flag == '--config':
            if os.path.exists(value):
                with open(value, 'rb') as f:
                    cfg = tomllib.load(f).get('git', {})
                docs += [glob_re(g) for g in cfg.get('exclude_paths', [])]
                includes += [glob_re(g) for g in cfg.get('include_paths', [])]
        elif flag == '--include':
            includes.append(glob_re(value))
        else:
            scope_out.append(glob_re(value))
    end = rng.split('..')[-1]
    at: dict[str, dict[str, int]] = {'commits': {}, 'messages': {}}
    for i, sha in enumerate(git('rev-list', '--reverse', '--no-merges', rng).split()):
        at['commits'][sha] = i
        files = git('diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-z', sha).split(
            '\0'
        )
        scoped = [
            f
            for f in files
            if f
            and (not includes or any(r.match(f) for r in includes))
            and not any(r.match(f) for r in scope_out)
        ]
        if not scoped or any(not any(r.match(f) for r in docs) for f in scoped):
            continue
        if embedded_at(end, scoped):
            message = git('log', '-1', '--format=%B', sha).strip()
            at['messages'].setdefault(message, i)
            sys.stdout.write(message + '\0')
    if positions:
        with open(positions, 'w') as f:
            json.dump(at, f)
    return 0


def main() -> int:
    if sys.argv[1:2] == ['--with-commits']:
        return with_commits(sys.argv[2:])
    commit = sys.argv[1]
    for path in embedded_at(commit, [p.strip() for p in sys.stdin if p.strip()]):
        print(path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
