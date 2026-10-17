#!/usr/bin/env python3
"""The community data refresh: each listed repository's own refresh command,
proposed as one pull request against its base branch.

community-data-refresh.yaml runs one subcommand per step, so the repository's
code and the App token never share a job:

    plan     the matrix of REPOS rows a run covers, from the dispatch inputs.
    package  after the row's command ran in a checkout of its base branch, the
             proposal: the base commit, each changed file, and the title and
             body the command wrote. Holds no token.
    check    validates a proposal and outputs whether it changes anything.
    propose  commits a proposal onto its base through the REST API and opens
             or updates the pull request from BRANCH (the App token).

The command contract is in docs/workflows.md. Stdlib only; runs on the
runner's system python3 (3.12).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import ghrest

OWNER = 'cplieger'
BRANCH = 'community-data-refresh'
# Each row's command runs through bash from the repository root, with
# REFRESH_OUT naming an empty directory for its title and body.md.
REPOS = {
    'langtag': {'branch': 'dev', 'command': 'go run ./internal/gen/refresh -out "$REFRESH_OUT"'},
}
# GitHub's caps on a pull request's title and body, in characters.
TITLE_MAX = 256
BODY_MAX = 65536
SHA = re.compile(r'^[0-9a-f]{40}$')
MODES = frozenset({'100644', '100755'})
MANIFEST = 'proposal.json'
FILES = 'files'


class RefusedError(Exception):
    """An input or a proposal this job must not act on."""


def write_outputs(values: dict[str, str]) -> None:
    lines = ''.join(f'{k}={v}\n' for k, v in values.items())
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(lines)
    print(lines, end='')


# ── plan ──────────────────────────────────────────────────────────────────────


def plan(only: str, args: str) -> list[dict[str, str]]:
    """The matrix rows for a run limited to the space- or comma-separated
    repositories `only` ('' for every one), refusing `args`, the arguments for
    one repository's command, unless `only` names exactly one."""
    names = only.replace(',', ' ').split()
    if '\n' in args or '\r' in args:
        raise RefusedError('args must be one line')
    if args and len(names) != 1:
        raise RefusedError("args are one repository's: give that one repository in only")
    unknown = [n for n in names if n not in REPOS]
    if unknown:
        raise RefusedError(
            f'{", ".join(unknown)} not refreshed. The repositories are {", ".join(REPOS)}'
        )
    rows = [{'repo': n, **REPOS[n]} for n in dict.fromkeys(names or REPOS)]
    if not rows:
        raise RefusedError('REPOS lists no repository')
    return rows


def cmd_plan(a) -> int:
    write_outputs({'matrix': json.dumps(plan(a.only, a.args), separators=(',', ':'))})
    return 0


# ── package ───────────────────────────────────────────────────────────────────


def git(tree: Path, *args: str) -> str:
    return subprocess.run(
        ['git', '-C', str(tree), *args], capture_output=True, text=True, check=True
    ).stdout


def package(tree: Path, text: Path, out: Path) -> dict:
    """Write `out`, the proposal for every change the command made in `tree`
    (staging them there), and return its manifest."""
    out.mkdir()
    base = git(tree, 'rev-parse', 'HEAD').strip()
    git(tree, 'add', '--all')
    changes = []
    # -z prints each status and its path as two NUL-terminated fields.
    fields = git(tree, 'diff', '--cached', '--no-renames', '--name-status', '-z').split('\0')[:-1]
    for status, path in zip(fields[::2], fields[1::2], strict=True):
        if status == 'D':
            changes.append({'path': path, 'deleted': True})
            continue
        mode = git(tree, 'ls-files', '--stage', '--', path).split()[0]
        if mode not in MODES:
            raise RefusedError(f'{path} has mode {mode}, and a refresh changes only regular files')
        dest = out / FILES / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree / path, dest)
        changes.append({'path': path, 'mode': mode})
    manifest = {'base': base, 'changes': changes}
    if changes:
        for name in ('title', 'body.md'):
            if not (text / name).is_file():
                raise RefusedError(f'the command changed files but wrote no {name}')
            shutil.copyfile(text / name, out / name)
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8')
    return manifest


def cmd_package(a) -> int:
    manifest = package(Path(a.tree), Path(a.text), Path(a.out))
    for c in manifest['changes']:
        print(f'{"deleted" if c.get("deleted") else "changed"} {c["path"]}')
    if not manifest['changes']:
        print('no change')
    return 0


# ── check ─────────────────────────────────────────────────────────────────────


def safe_path(path) -> bool:
    """Whether `path` names a file inside a repository tree, outside .git."""
    if not isinstance(path, str) or not path or '\\' in path or '\0' in path:
        return False
    p = PurePosixPath(path)
    return (
        not p.is_absolute()
        and str(p) == path
        and all(part not in ('', '.', '..') for part in p.parts)
        and p.parts[0] != '.git'
    )


class Proposal:
    """A validated proposal directory: `base`, `changes`, and `title` and `body`
    when it changes anything."""

    def __init__(self, root: Path):
        self.root = root
        try:
            doc = json.loads((root / MANIFEST).read_text(encoding='utf-8'))
        except (OSError, ValueError) as err:
            raise RefusedError(f'{MANIFEST} is unreadable: {err}') from None
        if not isinstance(doc, dict) or not isinstance(doc.get('changes'), list):
            raise RefusedError(f'{MANIFEST} holds no change list')
        self.base = doc.get('base')
        if not isinstance(self.base, str) or not SHA.match(self.base):
            raise RefusedError(f'base {self.base!r} is not a commit SHA')
        self.changes = doc['changes']
        seen = set()
        for c in self.changes:
            self._check_change(c, seen)
        self.title = self.body = ''
        if self.changes:
            self.title = self._read('title').removesuffix('\n')
            self.body = self._read('body.md')
            if not self.title.strip() or '\n' in self.title or '\r' in self.title:
                raise RefusedError('the title must be one non-empty line')
            if len(self.title) > TITLE_MAX:
                raise RefusedError(f'the title is {len(self.title)} characters, over {TITLE_MAX}')
            if len(self.body) > BODY_MAX:
                raise RefusedError(f'the body is {len(self.body)} characters, over {BODY_MAX}')

    def _check_change(self, c, seen: set) -> None:
        if not isinstance(c, dict) or not safe_path(c.get('path')):
            raise RefusedError(f'change {c!r} names no path inside the tree')
        path = c['path']
        if path in seen:
            raise RefusedError(f'{path} is changed twice')
        seen.add(path)
        if c.get('deleted') is True and set(c) == {'path', 'deleted'}:
            return
        if set(c) != {'path', 'mode'} or c['mode'] not in MODES:
            raise RefusedError(
                f'change {c!r}: want a path and a mode of {sorted(MODES)}, or deleted'
            )
        if not self.file(path).is_file() or self.file(path).is_symlink():
            raise RefusedError(f'{path} is changed but not in the proposal')

    def _read(self, name: str) -> str:
        try:
            return (self.root / name).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as err:
            raise RefusedError(f'{name} is unreadable: {err}') from None

    def file(self, path: str) -> Path:
        return self.root / FILES / path


def cmd_check(a) -> int:
    p = Proposal(Path(a.proposal))
    write_outputs({'changed': 'true' if p.changes else 'false'})
    return 0


# ── propose ───────────────────────────────────────────────────────────────────


def tree_of(gh: ghrest.Client, repo: str, p: Proposal) -> str:
    """The SHA of the tree that is `p.base`'s with `p.changes` applied."""
    api = f'repos/{OWNER}/{repo}/git'
    base_tree = gh.get(f'{api}/commits/{p.base}')['tree']['sha']
    entries = []
    for c in p.changes:
        if c.get('deleted'):
            entries.append({'path': c['path'], 'mode': '100644', 'type': 'blob', 'sha': None})
            continue
        content = base64.b64encode(p.file(c['path']).read_bytes()).decode()
        blob = gh.send('POST', f'{api}/blobs', {'content': content, 'encoding': 'base64'})
        entries.append({'path': c['path'], 'mode': c['mode'], 'type': 'blob', 'sha': blob['sha']})
    return gh.send('POST', f'{api}/trees', {'base_tree': base_tree, 'tree': entries})['sha']


class Outcome(NamedTuple):
    """What propose did; `warning` when it proposed nothing someone must know of."""

    text: str
    warning: bool = False


def propose(gh: ghrest.Client, repo: str, base_branch: str, bot: str, p: Proposal) -> Outcome:
    """Open or update the pull request from BRANCH into `base_branch` carrying
    `p`. An open one whose commits are not all `bot`'s holds someone's work, so
    it is left as it is, with a warning; one already carrying the proposed tree
    gets no new commit. Refuses, writing nothing, when `base_branch` no longer
    points at `p.base`, so a stale run never moves BRANCH back."""
    api = f'repos/{OWNER}/{repo}'
    open_prs = gh.get(f'{api}/pulls?state=open&head={OWNER}:{BRANCH}&base={base_branch}')
    pr = open_prs[0] if open_prs else None
    if pr:
        commits = gh.pages(f'{api}/pulls/{pr["number"]}/commits')
        others = sorted(
            {(c.get('author') or {}).get('login') or '(unknown)' for c in commits} - {bot}
        )
        if others:
            return Outcome(
                f'{pr["html_url"]} carries commits by {", ".join(others)}, so it is left as it is'
                ' and this run proposes nothing',
                warning=True,
            )
    # git/ref/ answers one exact ref; git/refs/ also answers a list of the refs
    # it prefixes: https://docs.github.com/en/rest/git/refs#get-a-reference
    head = gh.get(f'{api}/git/ref/heads/{base_branch}')['object']['sha']
    if head != p.base:
        raise RefusedError(
            f'{base_branch} moved from {p.base} to {head} after the refresh ran. '
            'Run the workflow again to refresh from it'
        )
    tree = tree_of(gh, repo, p)
    if pr and gh.get(f'{api}/git/commits/{pr["head"]["sha"]}')['tree']['sha'] == tree:
        if (pr['title'], pr.get('body') or '') == (p.title, p.body):
            return Outcome(f'{pr["html_url"]} already proposes this change')
        gh.send('PATCH', f'{api}/pulls/{pr["number"]}', {'title': p.title, 'body': p.body})
        return Outcome(f'updated the text of {pr["html_url"]}')
    commit = gh.send(
        'POST', f'{api}/git/commits', {'message': p.title, 'tree': tree, 'parents': [p.base]}
    )['sha']
    if gh.get_or_none(f'{api}/git/ref/heads/{BRANCH}') is None:
        gh.send('POST', f'{api}/git/refs', {'ref': f'refs/heads/{BRANCH}', 'sha': commit})
    else:
        gh.send('PATCH', f'{api}/git/refs/heads/{BRANCH}', {'sha': commit, 'force': True})
    if pr:
        gh.send('PATCH', f'{api}/pulls/{pr["number"]}', {'title': p.title, 'body': p.body})
        return Outcome(f'updated {pr["html_url"]}')
    made = gh.send(
        'POST',
        f'{api}/pulls',
        {'title': p.title, 'body': p.body, 'head': BRANCH, 'base': base_branch},
    )
    return Outcome(f'opened {made["html_url"]}')


def cmd_propose(a) -> int:
    if a.repo not in REPOS:
        raise RefusedError(f'repo {a.repo!r} is not refreshed')
    p = Proposal(Path(a.proposal))
    if not p.changes:
        print('no change')
        return 0
    done = propose(ghrest.DEFAULT, a.repo, REPOS[a.repo]['branch'], a.bot, p)
    if not done.warning:
        print(done.text)
        return 0
    print(f'::warning::{done.text}')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as fh:
            fh.write(done.text + '\n')
    return 0


def parse_args(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest='command', required=True)
    s = sub.add_parser('plan', help='output the matrix a run covers')
    s.add_argument('--only', default='', help='space/comma-separated repositories (default: all)')
    s.add_argument('--args', default='', help="arguments for the one repository's command")
    s.set_defaults(func=cmd_plan)
    s = sub.add_parser('package', help="write the proposal of the command's changes")
    s.add_argument('--tree', required=True, help='the checkout the command ran in')
    s.add_argument('--text', required=True, help='the REFRESH_OUT directory')
    s.add_argument('--out', required=True, help='the proposal directory to create')
    s.set_defaults(func=cmd_package)
    s = sub.add_parser('check', help='validate a proposal and output changed')
    s.add_argument('--proposal', required=True)
    s.set_defaults(func=cmd_check)
    s = sub.add_parser('propose', help='open or update the pull request')
    s.add_argument('--repo', required=True)
    s.add_argument('--proposal', required=True)
    s.add_argument('--bot', required=True, help="the App's bot login, <slug>[bot]")
    s.set_defaults(func=cmd_propose)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        return a.func(a)
    except RefusedError as err:
        print(f'::error::{err}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
