#!/usr/bin/env python3
"""The community data refresh: the pull request a listed repository's own
refresh run proposes, opened against its base branch.

Each repository in REPOS runs its refresh in its own community-data-refresh.yaml
on the base branch, with no secret, and uploads the proposal as the
community-data-refresh artifact. community-data-refresh.yaml here runs no code
of the repository; it reads that artifact as data, one subcommand per step:

    plan     the matrix of REPOS rows a run covers, from the dispatch input.
    locate   the repository's newest completed refresh run on its base branch,
             refusing one that failed: its run id and the commit it ran on.
    check    validates a downloaded proposal and outputs whether it changes
             anything.
    propose  commits a proposal onto the commit its run ran on through the
             REST API and opens or updates the pull request from BRANCH,
             refusing when the base branch was rewritten or changed a
             proposed path since that commit.

The proposal format is in docs/workflows.md. Stdlib only; runs on the
runner's system python3 (3.12).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

import ghrest

OWNER = 'cplieger'
BRANCH = 'community-data-refresh'
WORKFLOW = 'community-data-refresh.yaml'
# Each row's paths are every file its refresh may propose; a proposal naming
# any other path is refused, so a repository's run reaches only its listed
# generated files through this job's write token.
REPOS = {
    'langtag': {
        'branch': 'dev',
        'paths': (
            'internal/gen/cldr/cldr.go',
            'scripttable.go',
            'regiontable.go',
            'names/table.go',
            'names/testdata/names.golden',
        ),
    },
}
# The events of a scheduled or hand-started run on the base branch; a pull
# request's run of the same workflow uploads an artifact anyone can shape.
EVENTS = frozenset({'schedule', 'workflow_dispatch'})
# GitHub's caps on a pull request's title and body, in characters.
TITLE_MAX = 256
BODY_MAX = 65536
# The most one proposal uploads as blobs; langtag's tables total about 240 KB.
FILES_MAX = 8 * 2**20
SHA = re.compile(r'^[0-9a-f]{40}$')
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


def listed(repo: str) -> dict:
    if repo not in REPOS:
        raise RefusedError(f'repo {repo!r} is not refreshed')
    return REPOS[repo]


def plan(only: str) -> list[dict[str, str]]:
    """The matrix rows for a run limited to the space- or comma-separated
    repositories `only` ('' for every one)."""
    names = only.replace(',', ' ').split()
    unknown = [n for n in names if n not in REPOS]
    if unknown:
        raise RefusedError(
            f'{", ".join(unknown)} not refreshed. The repositories are {", ".join(REPOS)}'
        )
    rows = [{'repo': n} for n in dict.fromkeys(names or REPOS)]
    if not rows:
        raise RefusedError('REPOS lists no repository')
    return rows


def cmd_plan(a) -> int:
    write_outputs({'matrix': json.dumps(plan(a.only), separators=(',', ':'))})
    return 0


class Run(NamedTuple):
    id: int
    base: str
    url: str


def locate(gh: ghrest.Client, repo: str) -> Run:
    """The newest completed refresh run of `repo` on its base branch, refused
    unless it succeeded: an older proposal is never read in its place."""
    branch = listed(repo)['branch']
    runs = gh.get(
        f'repos/{OWNER}/{repo}/actions/workflows/{WORKFLOW}/runs'
        f'?branch={branch}&status=completed&exclude_pull_requests=true&per_page=100'
    )['workflow_runs']
    for r in runs:
        if (
            r.get('event') not in EVENTS
            or r.get('head_branch') != branch
            or (r.get('head_repository') or {}).get('full_name') != f'{OWNER}/{repo}'
        ):
            continue
        if r.get('conclusion') != 'success':
            raise RefusedError(
                f"{repo}'s newest refresh run {r.get('html_url')} concluded"
                f' {r.get("conclusion")}, so it proposes nothing'
            )
        if not isinstance(r.get('id'), int) or not SHA.match(str(r.get('head_sha'))):
            raise RefusedError(f'run {r.get("html_url")} names no run id and commit')
        return Run(r['id'], r['head_sha'], r['html_url'])
    raise RefusedError(f'{repo} has no completed {WORKFLOW} run on {branch}')


def cmd_locate(a) -> int:
    run = locate(ghrest.DEFAULT, a.repo)
    print(f'reading {run.url}')
    write_outputs({'run-id': str(run.id), 'base': run.base})
    return 0


class Proposal:
    """A validated proposal directory: `changes`, the paths it changes, and
    `title` and `body` when it changes anything."""

    def __init__(self, root: Path, allowed):
        self.root = root
        try:
            doc = json.loads((root / MANIFEST).read_text(encoding='utf-8'))
        except (OSError, ValueError) as err:
            raise RefusedError(f'{MANIFEST} is unreadable: {err}') from None
        changes = doc.get('changes') if isinstance(doc, dict) else None
        if not isinstance(changes, list) or not all(isinstance(c, str) for c in changes):
            raise RefusedError(f'{MANIFEST} holds no list of paths')
        if len(set(changes)) != len(changes):
            raise RefusedError(f'{MANIFEST} names a path twice')
        outside = sorted(set(changes) - set(allowed))
        if outside:
            raise RefusedError(f'{", ".join(outside)} is not a path this refresh may change')
        found = self._files()
        if found != set(changes):
            raise RefusedError(
                f'{FILES}/ holds {sorted(found)}, but {MANIFEST} lists {sorted(changes)}'
            )
        self.changes = changes
        self.title = self.body = ''
        if changes:
            self.title = self._read('title').removesuffix('\n')
            self.body = self._read('body.md')
            if not self.title.strip() or '\n' in self.title or '\r' in self.title:
                raise RefusedError('the title must be one non-empty line')
            if len(self.title) > TITLE_MAX:
                raise RefusedError(f'the title is {len(self.title)} characters, over {TITLE_MAX}')
            if len(self.body) > BODY_MAX:
                raise RefusedError(f'the body is {len(self.body)} characters, over {BODY_MAX}')

    def _files(self) -> set[str]:
        """The path of every file under FILES, refusing anything but regular
        files and folders, and a total over FILES_MAX."""
        top = self.root / FILES
        if top.is_symlink() or (top.exists() and not top.is_dir()):
            raise RefusedError(f'{FILES} is not a folder')
        found, size = set(), 0
        for folder, dirs, names in os.walk(top):
            for name in dirs + names:
                path = Path(folder) / name
                rel = path.relative_to(top).as_posix()
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise RefusedError(f'{FILES}/{rel} is not a regular file or folder')
                if name in names:
                    found.add(rel)
                    size += path.stat().st_size
        if size > FILES_MAX:
            raise RefusedError(f'the proposal holds {size} bytes, over {FILES_MAX}')
        return found

    def _read(self, name: str) -> str:
        try:
            return (self.root / name).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as err:
            raise RefusedError(f'{name} is unreadable: {err}') from None

    def file(self, path: str) -> Path:
        return self.root / FILES / path


def cmd_check(a) -> int:
    p = Proposal(Path(a.proposal), listed(a.repo)['paths'])
    write_outputs({'changed': 'true' if p.changes else 'false'})
    return 0


def tree_of(gh: ghrest.Client, repo: str, base: str, p: Proposal) -> str:
    """The SHA of the tree that is commit `base`'s with `p.changes` applied."""
    api = f'repos/{OWNER}/{repo}/git'
    base_tree = gh.get(f'{api}/commits/{base}')['tree']['sha']
    entries = []
    for path in p.changes:
        content = base64.b64encode(p.file(path).read_bytes()).decode()
        blob = gh.send('POST', f'{api}/blobs', {'content': content, 'encoding': 'base64'})
        entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    return gh.send('POST', f'{api}/trees', {'base_tree': base_tree, 'tree': entries})['sha']


def object_at(gh: ghrest.Client, repo: str, ref: str, path: str) -> str | None:
    """The git object id of `path` at `ref`, None when `ref` has no such path."""
    # The object media type answers one object, with no content past 1 MB, for a
    # file, a folder or a link: https://docs.github.com/en/rest/repos/contents
    found = gh.get_or_none(
        f'repos/{OWNER}/{repo}/contents/{path}?ref={ref}',
        ('Accept: application/vnd.github.object+json',),
    )
    return None if found is None else found['sha']


def edited_since(gh: ghrest.Client, repo: str, base: str, tip: str, p: Proposal) -> list[str]:
    """Each of `p.changes` whose object differs between commits `base` and
    `tip`, as '<path> (<id at base> to <id at tip>)'."""
    edited = []
    for path in p.changes:
        was, now = object_at(gh, repo, base, path), object_at(gh, repo, tip, path)
        if was != now:
            edited.append(f'{path} ({was or "absent"} to {now or "absent"})')
    return edited


class Outcome(NamedTuple):
    """What propose did; `warning` when it proposed nothing someone must know of."""

    text: str
    warning: bool = False


def propose(gh: ghrest.Client, repo: str, bot: str, base: str, p: Proposal) -> Outcome:
    """Open or update the pull request from BRANCH into `repo`'s base branch
    carrying `p` as one commit on `base`, the commit its refresh ran on. An
    open one whose commits are not all `bot`'s holds someone's work, so it is
    left as it is, with a warning; one already carrying the proposed tree gets
    no new commit. Refuses, writing nothing, when `base` is no longer in the
    base branch's history, or when the base branch changed a proposed path
    since `base`, which the proposal would overwrite."""
    base_branch = listed(repo)['branch']
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
    again = f"Run {repo}'s {WORKFLOW}, then this workflow"
    # One read of the tip, so the history and path checks judge the same commit.
    # git/ref/ answers one exact ref; git/refs/ also answers a list of the refs
    # it prefixes: https://docs.github.com/en/rest/git/refs#get-a-reference
    ref = gh.get_or_none(f'{api}/git/ref/heads/{base_branch}')
    if ref is None:
        raise RefusedError(f'{repo} has no {base_branch} branch to propose to')
    tip = ref['object']['sha']
    # status is not cut by the endpoint's 300-file cap
    # (https://docs.github.com/en/rest/commits/commits#compare-two-commits); a
    # base with no common ancestor, or one GitHub no longer holds, answers 404.
    try:
        status = gh.get(f'{api}/compare/{base}...{tip}?per_page=1')['status']
    except ghrest.NotFoundError:
        status = 'gone'
    if status not in ('ahead', 'identical'):
        raise RefusedError(
            f'{base} is not in the history of {base_branch} ({status}), so it was rewritten'
            f' after the refresh ran. {again}'
        )
    edited = edited_since(gh, repo, base, tip, p)
    if edited:
        raise RefusedError(
            f'{base_branch} changed {", ".join(edited)} since {base}, the commit the refresh'
            f' ran on, which the proposal would overwrite. {again}'
        )
    tree = tree_of(gh, repo, base, p)
    if pr and gh.get(f'{api}/git/commits/{pr["head"]["sha"]}')['tree']['sha'] == tree:
        if (pr['title'], pr.get('body') or '') == (p.title, p.body):
            return Outcome(f'{pr["html_url"]} already proposes this change')
        gh.send('PATCH', f'{api}/pulls/{pr["number"]}', {'title': p.title, 'body': p.body})
        return Outcome(f'updated the text of {pr["html_url"]}')
    commit = gh.send(
        'POST', f'{api}/git/commits', {'message': p.title, 'tree': tree, 'parents': [base]}
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
    p = Proposal(Path(a.proposal), listed(a.repo)['paths'])
    if not p.changes:
        print('no change')
        return 0
    done = propose(ghrest.DEFAULT, a.repo, a.bot, a.base, p)
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
    s.set_defaults(func=cmd_plan)
    s = sub.add_parser('locate', help="output the newest refresh run's id and commit")
    s.add_argument('--repo', required=True)
    s.set_defaults(func=cmd_locate)
    s = sub.add_parser('check', help='validate a proposal and output changed')
    s.add_argument('--repo', required=True)
    s.add_argument('--proposal', required=True)
    s.set_defaults(func=cmd_check)
    s = sub.add_parser('propose', help='open or update the pull request')
    s.add_argument('--repo', required=True)
    s.add_argument('--base', required=True, help='the commit the refresh run ran on')
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
