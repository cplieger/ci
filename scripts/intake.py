#!/usr/bin/env python3
"""Pull-request policy of a two-branch repository.

  intake  whether a pull request into `main` holds only what its machine head
          may carry: a Renovate head only inventory surfaces with no breaking
          record, plus regular files of a tree its moved pin regenerates; the
          sync head only sync-owned paths; a rebuild head exactly one commit
          that changes no file
  title   whether the title (env PR_TITLE) is a conventional-commit header

Reads git objects and files as data; nothing in the repository is executed.
Exit 0 allowed, 1 refused (each reason as an error annotation), 2 an input error.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

import inventory
import release_channels

SCRIPTS = Path(__file__).resolve().parent

# The types the synced cliff configs route by name; test_intake.py pins the list
# against them. `release:` is left out: it is the promotion commit's subject.
TITLE_TYPES = (
    'feat',
    'fix',
    'sec',
    'refactor',
    'perf',
    'chore',
    'ci',
    'docs',
    'style',
    'test',
    'fuzz',
    'lint',
    'debug',
)
TITLE_RE = re.compile(rf'(?:{"|".join(TITLE_TYPES)})(?:\([A-Za-z0-9._/-]+\))?!?: \S.*')


def annotate(message: str) -> str:
    """An error workflow command whose text cannot open another command:
    https://github.com/actions/toolkit/blob/main/packages/core/src/command.ts"""
    data = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    return f'::error::{data}'


def check_title(title: str) -> list[str]:
    if TITLE_RE.fullmatch(title):
        return []
    return [
        (
            f'pull request title {title!r} is not `<type>[(scope)][!]: <description>` '
            f'with a type of {", ".join(TITLE_TYPES)}'
        )
    ]


# ── Breaking records ──────────────────────────────────────────────────────────

_MODULE_MAJOR = re.compile(r'(?P<path>.+?)(?:/v(?P<v>[2-9]|[1-9]\d+)|\.v(?P<gopkg>\d+))')


def _with_effective_toolchain(records: list[inventory.Record]) -> list[inventory.Record]:
    """A go.mod without a toolchain line runs the toolchain its `go` line names."""
    by_identity = {r.identity: r for r in records if r.kind == 'directive'}
    if 'go' in by_identity and 'toolchain' not in by_identity:
        records = [*records, replace(by_identity['go'], identity='toolchain')]
    return records


def _by_slot(records: list[inventory.Record]) -> dict[tuple, list[inventory.Record]]:
    out: dict[tuple, list[inventory.Record]] = {}
    for r in records:
        if r.kind != 'checksum':
            out.setdefault(r.slot, []).append(r)
    return out


def _module_line(identity: str) -> tuple[str, int]:
    m = _MODULE_MAJOR.fullmatch(identity)
    if not m:
        return identity, 1
    return m['path'], int(m['v'] or m['gopkg'])


def _values(records: list[inventory.Record]) -> str:
    return ', '.join(sorted({r.value for r in records}))


def breaking(path: str, before: list[inventory.Record], after: list[inventory.Record]) -> list[str]:
    """What in `after` a non-breaking update could not have produced from `before`."""
    by_b = _by_slot(_with_effective_toolchain(before))
    by_a = _by_slot(_with_effective_toolchain(after))
    out = []
    for slot in sorted(set(by_b) & set(by_a)):
        old = {r.value for r in by_b[slot]}
        moved = [r for r in by_a[slot] if r.value not in old]
        if not moved or not moved[0].versioning:
            continue
        what = 'minor' if moved[0].versioning == 'go-version' else 'major'
        change = f'{path}: {slot[1]} {_values(by_b[slot])} -> {_values(moved)}'
        lines_b = [inventory.release_lines(r.versioning, r.value) for r in by_b[slot]]
        lines_a = [inventory.release_lines(r.versioning, r.value) for r in moved]
        if None in lines_b or None in lines_a:
            out.append(f'{change}: no version order to rule out a {what} update')
        elif frozenset().union(*lines_a) - frozenset().union(*lines_b):
            out.append(f'{change} is a {what} update')
    modules = {
        s for s in set(by_b) ^ set(by_a) if s[0] == 'go' and s[2] in ('dep', 'pin', 'replace')
    }
    gone = {_module_line(s[1]) for s in modules if s in by_b}
    for slot in sorted(s for s in modules if s in by_a):
        line, major = _module_line(slot[1])
        for old_line, old_major in sorted(gone):
            if old_line == line and old_major != major:
                out.append(f'{path}: {slot[1]} replaces major {old_major} of {line}')
    return out


# ── Intake rules ──────────────────────────────────────────────────────────────


def changed_paths(repo: inventory.Repo, base: str, head: str) -> list[str]:
    return [
        p
        for p in repo.git('diff', '--no-renames', '--name-only', '-z', base, head).split('\0')
        if p
    ]


def check_regenerated(repo: inventory.Repo, head: str, path: str, dep: str, pins) -> str | None:
    if dep not in pins:
        return f'{path}: regenerated by {dep}, which this pull request does not move'
    entry = repo.git('ls-tree', '-z', head, '--', path).split('\0')[0]
    mode = entry.split(' ', 1)[0]
    if entry and mode != '100644':
        return f'{path}: mode {mode}, where a regenerated tree holds only regular files'
    return None


def check_renovate(repo: inventory.Repo, base: str, head: str) -> list[str]:
    out = []
    paths = changed_paths(repo, base, head)
    pins = None
    for path in paths:
        dep = inventory.regenerated_by(path)
        if dep:
            if pins is None:
                pins = inventory.moved_pins(repo, base, head, paths)
            if problem := check_regenerated(repo, head, path, dep, pins):
                out.append(problem)
            continue
        kind = inventory.surface_kind(path)
        if kind is None:
            out.append(f'{path}: not an inventory surface')
            continue
        if kind == 'gosum':
            continue
        try:
            at_b, at_h = repo.parsed(base, path), repo.parsed(head, path)
        except inventory.GitReadError:
            raise
        except inventory.InventoryError as exc:
            out.append(f'cannot read {exc}')
            continue
        if at_b is None or at_h is None:
            out.append(f'{path}: {"added" if at_b is None else "deleted"}, not updated')
            continue
        if at_b[1] != at_h[1]:
            out.append(f'{path}: changed outside its dependency records')
            continue
        out += breaking(path, at_b[0], at_h[0])
    return out


def load_sync_owned(path: str | None) -> list[re.Pattern]:
    """The given file, else the set classify-repos.py publishes beside this script."""
    if path:
        return inventory.load_sync_owned(path)
    source = SCRIPTS / 'classify-repos.py'
    patterns = None
    if source.is_file():
        spec = importlib.util.spec_from_file_location('classify_repos', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        patterns = getattr(module, 'sync_owned_patterns', None)
    if not callable(patterns):
        raise inventory.InventoryError(
            'no sync-owned set: pass --sync-owned or publish sync_owned_patterns() in classify-repos.py'
        )
    globs = list(patterns())
    if not globs:
        raise inventory.InventoryError('classify-repos.py publishes an empty sync-owned set')
    return [inventory.glob_pattern(g) for g in globs]


def check_sync(repo: inventory.Repo, base: str, head: str, owned: list[re.Pattern]) -> list[str]:
    return [
        f'{path}: not a sync-owned path'
        for path in changed_paths(repo, base, head)
        if not any(p.fullmatch(path) for p in owned)
    ]


def check_rebuild(repo: inventory.Repo, base: str, head: str) -> list[str]:
    commits = repo.git('rev-list', '--parents', f'{base}..{head}').splitlines()
    if len(commits) != 1:
        return [f'a rebuild carries exactly one commit, and this one carries {len(commits)}']
    sha, *parents = commits[0].split()
    if len(parents) != 1:
        return [f'{sha[:12]}: a rebuild commit has one parent, not {len(parents)}']
    if repo.git('rev-parse', f'{sha}^{{tree}}') != repo.git('rev-parse', f'{parents[0]}^{{tree}}'):
        return [f'{sha[:12]}: changes files, where a rebuild commit changes none']
    return []


def check_intake(args) -> list[str]:
    kind = release_channels.main_intake_kind(args.head_ref)
    if kind is None:
        return [
            f'{args.head_ref!r} is not a branch main takes: only Renovate, sync and rebuild heads'
        ]
    if args.head_repo != args.repository:
        return [f'{args.head_ref!r} comes from {args.head_repo!r}, not from {args.repository!r}']
    repo = inventory.Repo(args.git_dir)
    base_tip, head = repo.commit(args.base_sha), repo.commit(args.head_sha)
    base = repo.git('merge-base', base_tip, head).strip()
    if kind == 'renovate':
        return check_renovate(repo, base, head)
    if kind == 'sync':
        return check_sync(repo, base, head, load_sync_owned(args.sync_owned))
    return check_rebuild(repo, base, head)


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('intake')
    p.add_argument('--head-ref', required=True)
    p.add_argument('--head-repo', required=True, help='owner/name the head branch lives in')
    p.add_argument('--repository', required=True, help='owner/name of the base repository')
    p.add_argument('--base-sha', required=True)
    p.add_argument('--head-sha', required=True)
    p.add_argument('--git-dir', default='.')
    p.add_argument('--sync-owned', help='file of sync-owned paths and globs')
    sub.add_parser('title')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.cmd == 'title':
            refusals = check_title(os.environ.get('PR_TITLE', ''))
        else:
            refusals = check_intake(args)
    except inventory.InventoryError as exc:
        print(annotate(f'intake: {exc}'))
        return 2
    for reason in refusals:
        print(annotate(reason))
    if refusals:
        return 1
    print(f'{args.cmd}: allowed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
