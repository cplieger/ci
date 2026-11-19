#!/usr/bin/env python3
"""The community data refresh: the pull request a listed repository's own
refresh run proposes, and the issues its notices keep in step. Each subcommand
is one step of community-data-refresh.yaml; the proposal format and the issue
rules are in docs/workflows.md. Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import ghrest
import workflow_commands

OWNER = 'cplieger'
BRANCH = 'community-data-refresh'
WORKFLOW = 'community-data-refresh.yaml'
# Each row's paths are every file its refresh may propose; a proposal naming
# any other path is refused, so a repository's run reaches only its listed
# generated files through this job's write token.
REPOS = {
    'access': {
        'branch': 'dev',
        'paths': (
            'internal/blocklist/common.txt',
            'webauthn/aaguids_gen.go',
            'webauthn/testdata/aaguid.json',
        ),
    },
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
    'subflux': {
        'branch': 'dev',
        'paths': (
            'internal/release/patterns_gen.go',
            'internal/release/testdata/upstream.json',
            'internal/release/testdata/golden/fixtures.json',
            'internal/release/testdata/golden/labels.json',
        ),
        'upstreams': {
            'trash-guides': 'https://github.com/TRaSH-Guides/Guides',
            'sonarr': 'https://github.com/Sonarr/Sonarr',
            'radarr': 'https://github.com/Radarr/Radarr',
        },
        'labels': ('candidate', 'out-of-model'),
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
NOTICES = 'notices.json'
NOTICES_MAX = 50
NOTICE_ID = re.compile(r'^[a-z0-9][a-z0-9-]*$')
NOTICE_ID_MAX = 64
NOTICE_KEYS = frozenset({'id', 'title', 'body', 'labels', 'upstream', 'from', 'to'})
NOTICE_NEEDS = ('id', 'title', 'body', 'labels')
# The attributes whose value is a URL, from HTML's attribute index
# (https://html.spec.whatwg.org/multipage/indices.html#attributes-3) plus the
# obsolete longdesc, which GitHub's sanitizer keeps on an image.
URL_ATTRS = frozenset(
    {
        'action', 'cite', 'data', 'formaction', 'href', 'itemid', 'itemtype',
        'longdesc', 'manifest', 'ping', 'poster', 'src', 'srcset', 'xlink:href',
    }
)  # fmt: skip
# The label on every issue this job keeps; a notice adds its own from its row.
ISSUE_LABEL = 'community-data'
OPENS_MAX = 10
# The most issues check lists, in pages of ISSUES_PAGE (read_issues).
ISSUES_MAX = 200
ISSUES_PAGE = 100
# An issue is this job's by its body's last line, so text a person quotes
# above it cannot claim an id.
MARKER = re.compile(rf'<!-- community-data id=([a-z0-9][a-z0-9-]{{0,{NOTICE_ID_MAX - 1}}}) -->')
BULK_MARKER = '<!-- community-data bulk -->'
# issue_plan's key for the bulk issue; no notice id is empty.
BULK = ''
BULK_TITLE = 'Community data notices waiting for a later run'
LABEL_COLOR = 'c5def5'


class RefusedError(Exception):
    """An input or a proposal this job must not act on."""


class Notice(NamedTuple):
    """One validated notice; `body` is the issue body this job writes."""

    id: str
    title: str
    body: str
    labels: tuple[str, ...]
    compare: str = ''


def title_fault(title: str) -> str | None:
    """Why `title` cannot title a pull request or an issue, None when it can."""
    if not title.strip() or '\n' in title or '\r' in title:
        return 'the title must be one non-empty line'
    if len(title) > TITLE_MAX:
        return f'the title is {len(title)} characters, over {TITLE_MAX}'
    return None


def marker(nid: str) -> str:
    return f'<!-- community-data id={nid} -->'


def read_notice(i: int, entry, row: dict) -> Notice:
    """Entry `i` of a NOTICES file, validated against `row`, with the issue
    body this job writes: the compare link from the row's own upstream when it
    names one with `from` and `to`, the notice's body, then the marker."""
    if not isinstance(entry, dict):
        raise RefusedError(f'notice {i} is not an object')
    unknown = sorted(set(entry) - NOTICE_KEYS)
    if unknown:
        raise RefusedError(f'notice {i} holds unknown keys: {", ".join(unknown)}')
    missing = [k for k in NOTICE_NEEDS if k not in entry]
    if missing:
        raise RefusedError(f'notice {i} lacks {", ".join(missing)}')
    nid = entry['id']
    if not isinstance(nid, str) or len(nid) > NOTICE_ID_MAX or not NOTICE_ID.fullmatch(nid):
        raise RefusedError(f'notice {i}: {nid!r} is not a notice id')
    where = f'notice {nid!r}'
    labels = entry['labels']
    if not isinstance(labels, list) or not all(isinstance(n, str) for n in labels):
        raise RefusedError(f'{where}: labels is not a list of labels')
    allowed = row['labels']
    for label in labels:
        if label not in allowed:
            raise RefusedError(f'{where}: label {label!r} is not one of {", ".join(allowed)}')
    if len(set(labels)) != len(labels):
        raise RefusedError(f'{where}: names a label twice')
    upstreams = row.get('upstreams', {})
    upstream = entry.get('upstream')
    if 'upstream' in entry and (not isinstance(upstream, str) or upstream not in upstreams):
        raise RefusedError(f'{where}: upstream {upstream!r} is not one of {", ".join(upstreams)}')
    compare = link = ''
    commits = [entry[k] for k in ('from', 'to') if k in entry]
    if commits:
        if upstream is None:
            raise RefusedError(f'{where}: from and to need an upstream')
        if len(commits) != 2 or not all(isinstance(c, str) and SHA.fullmatch(c) for c in commits):
            raise RefusedError(f'{where}: from and to are two 40-hex commits')
        compare = f'{upstreams[upstream]}/compare/{commits[0]}...{commits[1]}'
        link = f'Upstream change: {compare}'
    title, body = entry['title'], entry['body']
    for field, text in (('title', title), ('body', body)):
        if not isinstance(text, str):
            raise RefusedError(f'{where}: the {field} is not text')
    fault = title_fault(title)
    if fault:
        raise RefusedError(f'{where}: {fault}')
    # The link goes first, so no block the body leaves open can hide it.
    written = '\n\n'.join(b for b in (link, body.rstrip(), marker(nid)) if b) + '\n'
    if len(written) > BODY_MAX:
        raise RefusedError(f'{where}: the body is {len(written)} characters, over {BODY_MAX}')
    return Notice(nid, title, written, tuple(labels), compare)


def read_notices(root: Path, row: dict) -> list[Notice] | None:
    """The notices of the proposal at `root`, None for a row that declares no
    labels. A row that declares labels carries NOTICES in every proposal (an
    empty list reports nothing); a row that declares none carries none."""
    path = root / NOTICES
    present = os.path.lexists(path)
    if 'labels' not in row:
        if present:
            raise RefusedError(f'{NOTICES} is present, but this row declares no labels')
        return None
    if not present:
        raise RefusedError(f'{NOTICES} is missing, and this row declares labels')
    if path.is_symlink() or not path.is_file():
        raise RefusedError(f'{NOTICES} is not a regular file')
    size = path.stat().st_size
    if size > FILES_MAX:
        raise RefusedError(f'{NOTICES} is {size} bytes, over {FILES_MAX}')
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        raise RefusedError(f'{NOTICES} is unreadable: {err}') from None
    if not isinstance(doc, list):
        raise RefusedError(f'{NOTICES} holds no list of notices')
    if len(doc) > NOTICES_MAX:
        raise RefusedError(f'{NOTICES} holds {len(doc)} notices, over {NOTICES_MAX}')
    notices, seen = [], set()
    for i, entry in enumerate(doc):
        n = read_notice(i, entry, row)
        if n.id in seen:
            raise RefusedError(f'notice {n.id!r} is listed twice')
        seen.add(n.id)
        notices.append(n)
    return notices


def issue_pages(gh: ghrest.Client, path: str, pages: int) -> tuple[list[dict], int, bool]:
    """Up to `pages` pages of the issue listing at `path`, pull requests
    dropped, with the pages that listed an issue and whether the listing ended
    within them."""
    issues = []
    for page in range(1, pages + 1):
        batch = gh.get(f'{path}&page={page}')
        if not isinstance(batch, list) or not all(
            isinstance(i, dict) and type(i.get('number')) is int for i in batch
        ):
            raise RefusedError(f'GET {path}&page={page} answered no list of issues')
        issues += [i for i in batch if 'pull_request' not in i]
        if len(batch) < ISSUES_PAGE:
            return issues, page if batch else page - 1, True
    return issues, pages, False


def read_issues(gh: ghrest.Client, repo: str) -> list[dict]:
    """`repo`'s ISSUE_LABEL issues in at most ISSUES_MAX listed: every open
    one, then the closed ones newest update first in the pages left, which
    only a reopen reads. Refused when the open ones fill ISSUES_MAX, since a
    partial open set would plan a duplicate or miss a close."""
    path = (
        f'repos/{OWNER}/{repo}/issues?labels={ISSUE_LABEL}&state={{}}'
        f'&sort=updated&direction=desc&per_page={ISSUES_PAGE}'
    )
    pages = ISSUES_MAX // ISSUES_PAGE
    opened, used, ended = issue_pages(gh, path.format('open'), pages)
    if not ended:
        raise RefusedError(
            f'{OWNER}/{repo} lists {ISSUES_MAX} or more open {ISSUE_LABEL} issues,'
            f' the most this job reads'
        )
    closed, _, _ = issue_pages(gh, path.format('closed'), pages - used)
    return opened + closed


def last_line(text: str) -> str:
    return text.rstrip().rpartition('\n')[2]


def issue_key(issue: dict) -> str | None:
    """The notice id on the last line of `issue`'s body, BULK for the bulk
    issue, None for an issue this job did not write."""
    body = issue.get('body')
    if not isinstance(body, str):
        return None
    last = last_line(body)
    if last == BULK_MARKER:
        return BULK
    found = MARKER.fullmatch(last)
    return found[1] if found else None


def is_open(issue: dict) -> bool:
    return issue.get('state') == 'open'


def written(kind: str, title: str, body: str, labels, issue: dict | None, managed) -> dict | None:
    """The `kind` action writing `title`, `body` and `labels` to `issue` (None
    to create one), or None for an edit of an issue already in step. The
    labels a person added outside `managed` are kept."""
    theirs = [n['name'] for n in issue.get('labels', ()) if isinstance(n, dict)] if issue else []
    want = [ISSUE_LABEL, *labels, *(n for n in theirs if n not in managed)]
    if kind == 'edit':
        now = (issue.get('title'), issue.get('body'), set(theirs))
        if now == (title, body, set(want)):
            return None
    action = {'kind': kind, 'title': title, 'body': body, 'labels': want}
    if issue:
        action['number'] = issue['number']
    return action


def code_span(text: str) -> str:
    """`text` as one inline code span, which GitHub shows as text whatever it holds."""
    tick = '`' * (max(map(len, re.findall('`+', text)), default=0) + 1)
    # CommonMark strips one space each side, so `text` may start or end with a
    # backtick: https://spec.commonmark.org/0.31.2/#code-spans
    return f'{tick} {text} {tick}'


def bulk_body(titles: list[str]) -> str:
    listing = ''.join(f'- {code_span(t)}\n' for t in titles)
    return (
        f'This run opened or reopened {OPENS_MAX} issues, the most one run opens. These notices'
        f' wait for a later run:\n\n{listing}\n{BULK_MARKER}\n'
    )


def issue_plan(notices: list[Notice], issues: list[dict], managed) -> list[dict]:
    """The writes that bring a repository's `issues` in step with `notices`:
    one issue per notice id, created, reopened or edited in notice order, at
    most OPENS_MAX created or reopened with the rest listed in one bulk issue,
    then every open issue no notice lists closed. Of several issues with one
    id, an open one outranks a closed one, then the higher number, and the
    other open ones are closed. `managed` is the row's labels, which an edit
    replaces."""
    managed = {ISSUE_LABEL, *managed}
    kept, closes = {}, []
    for issue in sorted(issues, key=lambda i: (is_open(i), i['number']), reverse=True):
        key = issue_key(issue)
        if key is None:
            continue
        if key in kept:
            if is_open(issue):
                closes.append(issue['number'])
            continue
        kept[key] = issue
    actions, deferred, opens = [], [], 0
    for n in notices:
        issue = kept.get(n.id)
        if issue and is_open(issue):
            action = written('edit', n.title, n.body, n.labels, issue, managed)
        elif opens == OPENS_MAX:
            deferred.append(n.title)
            continue
        else:
            opens += 1
            action = written(
                'reopen' if issue else 'create', n.title, n.body, n.labels, issue, managed
            )
        if action:
            actions.append(action)
    bulk = kept.pop(BULK, None)
    if deferred:
        kind = 'create' if bulk is None else 'edit' if is_open(bulk) else 'reopen'
        action = written(kind, BULK_TITLE, bulk_body(deferred), (), bulk, managed)
        if action:
            actions.append(action)
    elif bulk and is_open(bulk):
        closes.append(bulk['number'])
    listed = {n.id for n in notices}
    closes += [i['number'] for k, i in kept.items() if k not in listed and is_open(i)]
    return actions + [{'kind': 'close', 'number': n} for n in sorted(closes)]


class Rendering(HTMLParser):
    """The URL attribute values, the text and whether the page builds part of
    one HTML rendering in the browser."""

    def __init__(self, html: str):
        super().__init__()
        self.urls: list[str] = []
        self.text: list[str] = []
        self.built = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.urls += [value or '' for name, value in attrs if name in URL_ATTRS]
        # GitHub's math (MathJax, whose \href is a link) and its diagram and map
        # fences are built client-side, so their links never reach this HTML:
        # https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/writing-mathematical-expressions
        # https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams
        classes = next((v or '' for n, v in attrs if n == 'class'), '').split()
        self.built |= tag == 'math-renderer' or 'render-needs-enrichment' in classes

    def handle_data(self, data):
        self.text.append(data)


def check_renders(gh: ghrest.Client, repo: str, notices: list[Notice], actions: list[dict]) -> None:
    """Refuses unless GitHub renders each of `notices`' issue bodies, and the
    bulk issue's body when `actions` write it, as `repo`'s issue page does, with
    no URL but the notice's compare link, nothing the browser builds and its
    marker hidden. A failed render refuses too: the check is never skipped."""
    bodies = [(f'notice {n.id!r}', n.body, n.compare) for n in notices]
    bodies += [('the bulk issue', a['body'], None) for a in actions if issue_key(a) == BULK]
    for where, body, compare in bodies:
        # gfm in the repository's context is the issue page's rendering, where a
        # reference or @name becomes a link:
        # https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/autolinked-references-and-urls
        sent = {'text': body, 'mode': 'gfm', 'context': f'{OWNER}/{repo}'}
        try:
            resp = gh.request('POST', 'markdown', sent)
        except ghrest.ApiError as err:
            raise RefusedError(f'{where}: GitHub did not render its body: {err}') from None
        seen = Rendering(resp.body.decode(errors='replace'))
        # An empty href is refused too: '' is no notice's compare link.
        if any(not compare or url != compare for url in seen.urls):
            other = ' other than the compare link' if compare else ''
            raise RefusedError(f'{where}: GitHub renders its body with a link or an image{other}')
        if seen.built:
            raise RefusedError(
                f'{where}: GitHub renders its body with math or a diagram, which the page'
                ' builds in the browser'
            )
        if last_line(body) in ''.join(seen.text):
            raise RefusedError(f'{where}: GitHub renders its marker as text')


def apply_issues(gh: ghrest.Client, repo: str, actions: list[dict]) -> list[str]:
    """Write `actions`, an issue plan, to `repo`, creating each label it names
    that `repo` lacks first; one line per action, '<verb> <issue url>'."""
    api = f'repos/{OWNER}/{repo}'
    for name in dict.fromkeys(n for a in actions for n in a.get('labels', ())):
        if gh.get_or_none(f'{api}/labels/{quote(name, safe="")}') is None:
            gh.send('POST', f'{api}/labels', {'name': name, 'color': LABEL_COLOR})
    said = []
    for a in actions:
        text = {k: a[k] for k in ('title', 'body', 'labels') if k in a}
        match a['kind']:
            case 'create':
                verb, done = 'opened', gh.send('POST', f'{api}/issues', text)
            case 'edit':
                verb, done = 'edited', gh.send('PATCH', f'{api}/issues/{a["number"]}', text)
            case 'reopen':
                body = {'state': 'open', **text}
                verb, done = 'reopened', gh.send('PATCH', f'{api}/issues/{a["number"]}', body)
            case _:
                body = {'state': 'closed', 'state_reason': 'completed'}
                verb, done = 'closed', gh.send('PATCH', f'{api}/issues/{a["number"]}', body)
        said.append(f'{verb} {done["html_url"]}')
    return said


def write_outputs(values: dict[str, str]) -> None:
    lines = ''.join(f'{k}={v}\n' for k, v in values.items())
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a') as fh:
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
        if not isinstance(r.get('id'), int) or not SHA.fullmatch(str(r.get('head_sha'))):
            raise RefusedError(f'run {r.get("html_url")} names no run id and commit')
        return Run(r['id'], r['head_sha'], r['html_url'])
    raise RefusedError(f'{repo} has no completed {WORKFLOW} run on {branch}')


def cmd_locate(a) -> int:
    run = locate(ghrest.DEFAULT, a.repo)
    print(f'reading {run.url}')
    write_outputs({'run-id': str(run.id), 'base': run.base})
    return 0


class Proposal:
    """A validated proposal directory for one REPOS row: `changes`, the paths
    it changes, `title` and `body` when it changes anything, and `notices`, None
    for a row that declares no labels."""

    def __init__(self, root: Path, row: dict):
        self.root = root
        allowed = row['paths']
        try:
            doc = json.loads((root / MANIFEST).read_text())
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
            fault = title_fault(self.title)
            if fault:
                raise RefusedError(fault)
            if len(self.body) > BODY_MAX:
                raise RefusedError(f'the body is {len(self.body)} characters, over {BODY_MAX}')
        self.notices = read_notices(root, row)

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
            return (self.root / name).read_text()
        except (OSError, UnicodeDecodeError) as err:
            raise RefusedError(f'{name} is unreadable: {err}') from None

    def file(self, path: str) -> Path:
        return self.root / FILES / path


def cmd_check(a) -> int:
    row = listed(a.repo)
    p = Proposal(Path(a.proposal), row)
    actions = []
    if p.notices is not None:
        actions = issue_plan(p.notices, read_issues(ghrest.DEFAULT, a.repo), row['labels'])
        check_renders(ghrest.DEFAULT, a.repo, p.notices, actions)
    Path(a.plan).write_text(json.dumps(actions, indent=2) + '\n')
    write_outputs(
        {
            'changed': 'true' if p.changes else 'false',
            'notices_changed': 'true' if actions else 'false',
        }
    )
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
    p = Proposal(Path(a.proposal), listed(a.repo))
    if not p.changes:
        print('no change')
        return 0
    done = propose(ghrest.DEFAULT, a.repo, a.bot, a.base, p)
    if not done.warning:
        print(done.text)
        return 0
    print(workflow_commands.command('warning', done.text))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as fh:
            fh.write(done.text + '\n')
    return 0


def cmd_issues(a) -> int:
    listed(a.repo)
    try:
        actions = json.loads(Path(a.plan).read_text())
    except (OSError, ValueError) as err:
        raise RefusedError(f'the issue plan {a.plan} is unreadable: {err}') from None
    for line in apply_issues(ghrest.DEFAULT, a.repo, actions):
        print(line)
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
    s = sub.add_parser('check', help='validate a proposal, plan its issues and output what moves')
    s.add_argument('--repo', required=True)
    s.add_argument('--proposal', required=True)
    s.add_argument('--plan', required=True, help='the file the issue plan is written to')
    s.set_defaults(func=cmd_check)
    s = sub.add_parser('propose', help='open or update the pull request')
    s.add_argument('--repo', required=True)
    s.add_argument('--base', required=True, help='the commit the refresh run ran on')
    s.add_argument('--proposal', required=True)
    s.add_argument('--bot', required=True, help="the App's bot login, <slug>[bot]")
    s.set_defaults(func=cmd_propose)
    s = sub.add_parser('issues', help="apply check's issue plan")
    s.add_argument('--repo', required=True)
    s.add_argument('--plan', required=True)
    s.set_defaults(func=cmd_issues)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        return a.func(a)
    except RefusedError as err:
        print(workflow_commands.command('error', err), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
