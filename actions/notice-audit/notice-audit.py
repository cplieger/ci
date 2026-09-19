#!/usr/bin/env python3
"""Fail when NOTICE departs from the template or a published package root lacks its copies.

The root NOTICE is exactly three lines: the repository name, `Copyright <year>
cplieger`, and the repository URL. One extra block may follow after one blank
line. Every published package root below the repository root (a directory with a
jsr.json, a package.json that has a name and is not private, or a nested go.mod
with at least one .go file under it) carries LICENSE and NOTICE byte-identical to the root copies, because a nested
module zip and an npm tarball contain only their own subtree.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

OWNER = 'cplieger'
COPYRIGHT_LINE = re.compile(rf'^Copyright [0-9]{{4}} {OWNER}$')
SKIPPED_SEGMENTS = {'node_modules', 'vendor', 'testdata', '.git'}
PACKAGE_MARKERS = {'jsr.json', 'package.json', 'go.mod'}


def repository_name(root: Path) -> str:
    configured = os.environ.get('GITHUB_REPOSITORY', '').strip()
    if configured:
        return configured
    result = subprocess.run(
        ['git', 'remote', 'get-url', 'origin'],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        match = re.search(r'github\.com(?::|/)([^/]+/[^/]+?)(?:\.git)?$', result.stdout.strip())
        if match:
            return match.group(1)
    return ''


def visible_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors='replace').strip() or 'git ls-files failed')
    return [
        Path(item.decode(errors='surrogateescape')) for item in result.stdout.split(b'\0') if item
    ]


def template_findings(text: str, repo: str) -> list[str]:
    """Compare a NOTICE body against the template; the returned list is empty on a match."""
    if '\r' in text:
        return ['NOTICE has CRLF line endings; the template is LF-terminated']
    if not text.endswith('\n'):
        return ['NOTICE does not end with a newline']
    lines = text[:-1].split('\n')
    if len(lines) < 3:
        return [f'NOTICE has {len(lines)} line(s); the template has three']
    findings = []
    if lines[0] != repo:
        findings.append(f'NOTICE line 1 is {lines[0]!r}; the repository name is {repo!r}')
    if not COPYRIGHT_LINE.match(lines[1]):
        findings.append(f'NOTICE line 2 is {lines[1]!r}; expected `Copyright <year> {OWNER}`')
    url = f'https://github.com/{OWNER}/{repo}'
    if lines[2] != url:
        findings.append(f'NOTICE line 3 is {lines[2]!r}; expected {url!r}')
    rest = lines[3:]
    if not rest:
        return findings
    if rest[0] != '':
        findings.append(
            'NOTICE line 4 must be blank: an extra block is separated by one blank line'
        )
    elif len(rest) == 1 or '' in rest[1:]:
        findings.append(
            'NOTICE allows one extra block after one blank line; found a trailing blank line '
            'or a second block'
        )
    return findings


def package_roots(root: Path, files: list[Path]) -> list[Path]:
    """Directories below the root that publish a package, so must carry their own copies."""
    go_dirs = {path.parent for path in files if path.suffix == '.go'}
    roots: set[Path] = set()
    for path in files:
        if path.name not in PACKAGE_MARKERS or path.parent == Path('.'):
            continue
        if SKIPPED_SEGMENTS & set(path.parts[:-1]):
            continue
        if path.name == 'go.mod' and not any(
            d == path.parent or path.parent in d.parents for d in go_dirs
        ):
            # A .go-less go.mod is a toolchain fence, not a module anyone fetches.
            continue
        if path.name == 'package.json':
            try:
                manifest = json.loads((root / path).read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f'cannot read {path}: {exc}') from exc
            if (
                not isinstance(manifest, dict)
                or not manifest.get('name')
                or manifest.get('private')
            ):
                continue
        roots.add(path.parent)
    return sorted(roots)


def copy_findings(root: Path, package_dir: Path) -> list[str]:
    findings = []
    for name in ('LICENSE', 'NOTICE'):
        canonical = root / name
        copy = root / package_dir / name
        if not canonical.is_file():
            findings.append(f'{package_dir}/{name}: no root {name} to compare against')
        elif not copy.is_file():
            findings.append(f'{package_dir}/{name} is missing; copy the root {name} there')
        elif copy.read_bytes() != canonical.read_bytes():
            findings.append(f'{package_dir}/{name} differs from the root {name}; copy it verbatim')
    return findings


def audit(root: Path, repo: str) -> tuple[list[str], int]:
    findings = []
    notice = root / 'NOTICE'
    if not notice.is_file():
        findings.append('NOTICE is missing at the repository root')
    else:
        findings.extend(template_findings(notice.read_text(encoding='utf-8'), repo))
    roots = package_roots(root, visible_files(root))
    for package_dir in roots:
        findings.extend(copy_findings(root, package_dir))
    return findings, len(roots)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?', default='.')
    parser.add_argument('--github', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.path).resolve()
    full_name = repository_name(root)
    if '/' not in full_name:
        print('notice audit: cannot determine the repository name', file=sys.stderr)
        return 2
    repo = full_name.split('/', 1)[1]
    try:
        findings, packages = audit(root, repo)
    except (RuntimeError, OSError, UnicodeDecodeError) as exc:
        print(f'notice audit: {exc}', file=sys.stderr)
        return 2
    if not findings:
        summary = f'root NOTICE matches the template; {packages} published package root(s) checked'
        print(f'NOTICE audit: PASS — {summary}')
        if args.github:
            print(f'::notice title=NOTICE audit::{summary}')
        return 0
    print(f'NOTICE audit: FAIL — {len(findings)} finding(s):')
    for finding in findings:
        print(f'  {finding}')
        if args.github:
            print(f'::error title=NOTICE audit::{finding}')
    return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
