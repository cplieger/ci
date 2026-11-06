#!/usr/bin/env python3
"""Which changed files a non-test //go:embed names, so they ship.

Usage: go-embeds.py <commit> < paths prints the paths on stdin a directive at
<commit> names; path-significance.sh asks it about the changed paths its
excludes drop. go-embeds.py --with-commits <range> [--include G]...
[--exclude G]... [--config cliff.toml] prints, each followed by a NUL, the full message of every
commit in <range> that git-cliff's own path filter would drop (every changed
file matches an --exclude, or none matches an --include) though one of those
files is embedded at the range's end. render-notes.sh and the version compute
pass each back with --with-commit, with no sha: git-cliff 2.14.2 parses a
`<sha> <message>` argument's message to the first newline only, which loses a
BREAKING CHANGE footer. A doc or config file a Go
build embeds ships in the binary (grelot's `grelot help` is an embedded
manual.md). Each path is
checked against the directives of the non-test .go files in its directory and
in each parent up to the nearest go.mod, read from the commit's tree in two git
calls (a dispatch run asks about every tracked file), with Go's
rules (https://pkg.go.dev/embed): space-separated patterns, each optionally a
quoted string and prefixed `all:`, matched element by element as path.Match
does; a pattern naming a directory embeds its tree except names starting with
`.` or `_`, unless `all:`. Build constraints are not evaluated, so a file any
platform builds counts. Exits 2 when git cannot read the tree.
"""

from __future__ import annotations

import ast
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


def element_re(pattern: str) -> re.Pattern[str]:
    """path.Match for one path element: * and ? never cross a slash."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == '*':
            out.append('[^/]*')
        elif c == '?':
            out.append('[^/]')
        elif c == '\\' and i + 1 < n:
            i += 1
            out.append(re.escape(pattern[i]))
        elif c == '[':
            j = pattern.find(']', i + 2)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j]
                neg = body.startswith('^')
                body = body[1:] if neg else body
                body = body.replace('\\', '\\\\')
                out.append(f'[{"^" if neg else ""}{body}]')
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile(''.join(out) + r'\Z')


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


DIRECTIVE = r'^[[:space:]]*//go:embed[[:space:]]'


def glob_re(pattern: str) -> re.Pattern[str]:
    """git-cliff's glob (Rust `glob`, require_literal_separator off): `**` and
    `*` cross slashes, `?` is one character, and a pattern ending in `/` means
    the directory's tree."""
    if pattern.endswith('/'):
        pattern += '**'
    pattern = pattern.removeprefix('./')
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == '*':
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
                out.append(
                    f'[{"^" if neg else ""}{(body[1:] if neg else body).replace(chr(92), chr(92) * 2)}]'
                )
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile(''.join(out) + r'\Z')


def embedded_at(commit: str, paths: list[str]) -> list[str]:
    """The paths a non-test //go:embed at <commit> names."""
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
    directives: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        name, _, rest = line.partition('\0')
        _, _, text = rest.partition('\0')
        name = name.removeprefix(f'{commit}:')
        d = name.rsplit('/', 1)[0] if '/' in name else ''
        directives.setdefault(d, []).extend(patterns(text.split('//go:embed', 1)[1]))

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
    """The --with-commits mode; see the module docstring."""
    # Scope globs (--include, --exclude) say which lane a file belongs to; the
    # config's exclude_paths say which files are docs. git-cliff drops a commit
    # unless a changed file is in scope and not a doc; this keeps a dropped
    # one when a file in scope is a doc a directive embeds.
    rng, includes, scope_out, docs = args[0], [], [], []
    rest = iter(args[1:])
    for flag in rest:
        value = next(rest, None)
        if flag not in ('--include', '--exclude', '--config') or value is None:
            sys.stderr.write(f'go-embeds: unexpected argument {flag!r}\n')
            return 2
        if flag == '--config':
            if os.path.exists(value):
                with open(value, 'rb') as f:
                    docs += [
                        glob_re(g) for g in tomllib.load(f).get('git', {}).get('exclude_paths', [])
                    ]
        elif flag == '--include':
            includes.append(glob_re(value))
        else:
            scope_out.append(glob_re(value))
    end = rng.split('..')[-1]
    for sha in git('rev-list', '--no-merges', rng).split():
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
            sys.stdout.write(git('log', '-1', '--format=%B', sha).strip() + '\0')
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
