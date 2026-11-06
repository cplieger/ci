#!/usr/bin/env python3
"""Prints which of the paths on stdin a non-test //go:embed names at a commit.

Usage: go-embeds.py <commit> < paths. path-significance.sh asks it about the
changed paths its excludes drop: a doc or config file a Go build embeds ships
in the binary (grelot's `grelot help` is an embedded manual.md). Each path is
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
import re
import subprocess
import sys


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


def main() -> int:
    commit = sys.argv[1]
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

    for path in (p.strip() for p in sys.stdin):
        if not path:
            continue
        parts = path.split('/')
        for k in range(len(parts) - 1, -1, -1):
            d = '/'.join(parts[:k])
            rel = '/'.join(parts[k:])
            if any(embeds(p, rel) for p in directives.get(d, ())):
                print(path)
                break
            if d in modules:
                break
    return 0


if __name__ == '__main__':
    sys.exit(main())
