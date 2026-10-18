from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import community_data_refresh as cdr
import ghrest
import workflow_replay
import yaml

BASE = 'b' * 40
TIP = 'd' * 40
WORKFLOW = Path(__file__).resolve().parent.parent / '.github/workflows/community-data-refresh.yaml'
BOT = 'tribble-trouble[bot]'
API = 'repos/cplieger/langtag'
PATHS = cdr.REPOS['langtag']['paths']


class Plan(unittest.TestCase):
    def test_an_empty_only_covers_every_repository(self):
        self.assertEqual(cdr.plan(''), [{'repo': n} for n in cdr.REPOS])

    def test_only_selects_named_repositories_once(self):
        self.assertEqual(cdr.plan('langtag, langtag'), [{'repo': 'langtag'}])

    def test_an_unlisted_repository_is_refused(self):
        with self.assertRaisesRegex(
            cdr.RefusedError, 'nope not refreshed. The repositories are langtag'
        ):
            cdr.plan('nope')

    def test_an_empty_list_fails_rather_than_covering_nothing(self):
        with (
            unittest.mock.patch.dict(cdr.REPOS, clear=True),
            self.assertRaisesRegex(cdr.RefusedError, 'lists no repository'),
        ):
            cdr.plan('')

    def test_the_command_writes_the_matrix_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            with (
                unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(out)}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cdr.main(['plan', '--only', 'langtag']), 0)
            key, _, value = out.read_text().strip().partition('=')
            self.assertEqual((key, json.loads(value)), ('matrix', cdr.plan('langtag')))

    def test_a_refusal_exits_1_with_an_error_annotation(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cdr.main(['plan', '--only', 'nope']), 1)
        self.assertTrue(err.getvalue().startswith('::error::nope not refreshed'))


def write_proposal(root: Path, changes, files=None, title='feat: x\n', body='Body.\n'):
    (root / cdr.FILES).mkdir(parents=True, exist_ok=True)
    for path, data in (files or {}).items():
        (root / cdr.FILES / path).parent.mkdir(parents=True, exist_ok=True)
        (root / cdr.FILES / path).write_bytes(data)
    (root / cdr.MANIFEST).write_text(json.dumps({'changes': changes}))
    if title is not None:
        (root / 'title').write_text(title)
    if body is not None:
        (root / 'body.md').write_text(body)
    return root


class Check(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_valid_proposal_reads(self):
        write_proposal(
            self.root,
            ['names/table.go', 'scripttable.go'],
            {'names/table.go': b'n', 'scripttable.go': b's'},
        )
        p = cdr.Proposal(self.root, PATHS)
        self.assertEqual(
            (p.changes, p.title, p.body),
            (['names/table.go', 'scripttable.go'], 'feat: x', 'Body.\n'),
        )
        self.assertEqual(p.file('names/table.go').read_bytes(), b'n')

    def test_a_proposal_of_no_change_needs_no_text_or_files(self):
        (self.root / cdr.MANIFEST).write_text('{"changes": []}')
        p = cdr.Proposal(self.root, PATHS)
        self.assertEqual((p.changes, p.title, p.body), ([], '', ''))

    def test_refusals(self):
        one = {'changes': ['scripttable.go'], 'files': {'scripttable.go': b'x'}}
        for name, kw, want in (
            ('not a list', {'changes': 'scripttable.go'}, 'holds no list of paths'),
            ('not a path', {'changes': [7]}, 'holds no list of paths'),
            (
                'twice',
                {'changes': ['scripttable.go', 'scripttable.go']},
                'names a path twice',
            ),
            (
                'outside the allowed paths',
                {
                    'changes': ['.github/workflows/ci.yaml'],
                    'files': {'.github/workflows/ci.yaml': b'x'},
                },
                r'\.github/workflows/ci\.yaml is not a path this refresh may change',
            ),
            (
                'missing file',
                {'files': {}},
                r"holds \[\], but proposal.json lists \['scripttable.go'\]",
            ),
            (
                'extra file',
                {'files': {'scripttable.go': b'x', 'regiontable.go': b'y'}},
                r"holds \['regiontable.go', 'scripttable.go'\]",
            ),
            ('no title', {'title': None}, 'title is unreadable'),
            ('blank title', {'title': ' \n'}, 'one non-empty line'),
            ('two lines', {'title': 'a\nb\n'}, 'one non-empty line'),
            ('long title', {'title': 'x' * 257}, 'over 256'),
            ('long body', {'body': 'x' * 65537}, 'over 65536'),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                write_proposal(Path(tmp), **{**one, **kw})
                with self.assertRaisesRegex(cdr.RefusedError, want):
                    cdr.Proposal(Path(tmp), PATHS)

    def test_a_link_is_refused_wherever_it_sits(self):
        for name, plant in (
            ('file', lambda f: (f / 'scripttable.go').symlink_to('/etc/passwd')),
            ('folder', lambda f: (f / 'names').symlink_to('/etc')),
            ('files', None),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_proposal(root, ['scripttable.go'])
                if plant is None:
                    (root / cdr.FILES).rmdir()
                    (root / cdr.FILES).symlink_to('/etc')
                    want = 'files is not a folder'
                else:
                    plant(root / cdr.FILES)
                    want = f'files/{"names" if name == "folder" else "scripttable.go"} is not a regular'
                with self.assertRaisesRegex(cdr.RefusedError, want):
                    cdr.Proposal(root, PATHS)

    def test_a_proposal_over_the_size_cap_is_refused(self):
        write_proposal(self.root, ['scripttable.go'], {'scripttable.go': b'x' * 11})
        with (
            unittest.mock.patch.object(cdr, 'FILES_MAX', 10),
            self.assertRaisesRegex(cdr.RefusedError, 'holds 11 bytes, over 10'),
        ):
            cdr.Proposal(self.root, PATHS)
        with unittest.mock.patch.object(cdr, 'FILES_MAX', 11):
            cdr.Proposal(self.root, PATHS)

    def test_an_unreadable_manifest_is_refused(self):
        with self.assertRaisesRegex(cdr.RefusedError, 'proposal.json is unreadable'):
            cdr.Proposal(self.root, PATHS)
        (self.root / cdr.MANIFEST).write_text('[]')
        with self.assertRaisesRegex(cdr.RefusedError, 'holds no list of paths'):
            cdr.Proposal(self.root, PATHS)

    def test_the_command_outputs_whether_it_changes_anything(self):
        for changes, files, want in (
            ([], {}, 'changed=false'),
            (['scripttable.go'], {'scripttable.go': b'x'}, 'changed=true'),
        ):
            with self.subTest(want), tempfile.TemporaryDirectory() as tmp:
                write_proposal(Path(tmp) / 'p', changes, files)
                out = Path(tmp) / 'out'
                with (
                    unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(out)}),
                    redirect_stdout(io.StringIO()),
                ):
                    argv = ['check', '--repo', 'langtag', '--proposal', str(Path(tmp) / 'p')]
                    self.assertEqual(cdr.main(argv), 0)
                self.assertEqual(out.read_text(), want + '\n')

    def test_the_command_refuses_an_unlisted_repository(self):
        write_proposal(self.root, [])
        with redirect_stderr(io.StringIO()):
            argv = ['check', '--repo', 'nope', '--proposal', str(self.root)]
            self.assertEqual(cdr.main(argv), 1)


def reply(status: int, body=None) -> subprocess.CompletedProcess:
    data = b'' if body is None else json.dumps(body).encode()
    rc = 0 if 200 <= status < 300 else 1
    return subprocess.CompletedProcess(
        ['gh'], rc, f'HTTP/2.0 {status} R\n\r\n'.encode() + data, b''
    )


class FakeGitHub:
    """`gh api -i` over a route table of (method, path) to a JSON body, a
    missing route answering 404; every write is logged with its JSON body."""

    def __init__(self, routes):
        self.routes = routes
        self.writes = []

    def __call__(self, args, stdin=None, timeout=None):
        method = args[args.index('-X') + 1] if '-X' in args else 'GET'
        path = args[args.index('--input') - 1] if '--input' in args else args[-1]
        if method != 'GET':
            self.writes.append((method, path, json.loads(stdin)))
        if (method, path) not in self.routes:
            return reply(404, {'message': 'Not Found'})
        return reply(200, self.routes[method, path])


def base_routes(extra=None):
    routes = {
        ('GET', f'{API}/pulls?state=open&head=cplieger:{cdr.BRANCH}&base=dev'): [],
        ('GET', f'{API}/git/ref/heads/dev'): {'object': {'sha': TIP}},
        ('GET', f'{API}/compare/{BASE}...{TIP}?per_page=1'): {'status': 'ahead'},
        ('GET', f'{API}/git/commits/{BASE}'): {'tree': {'sha': 'basetree'}},
        ('POST', f'{API}/git/blobs'): {'sha': 'blob1'},
        ('POST', f'{API}/git/trees'): {'sha': 'newtree'},
        ('POST', f'{API}/git/commits'): {'sha': 'c' * 40},
        ('POST', f'{API}/git/refs'): {},
        ('PATCH', f'{API}/git/refs/heads/{cdr.BRANCH}'): {},
        ('POST', f'{API}/pulls'): {'html_url': 'https://github.com/cplieger/langtag/pull/9'},
        ('PATCH', f'{API}/pulls/7'): {},
    }
    for path in ('scripttable.go', 'names/table.go'):
        for ref in (BASE, TIP):
            routes['GET', f'{API}/contents/{path}?ref={ref}'] = {'sha': f'{path} blob'}
    routes.update(extra or {})
    return routes


def open_pr(head_sha='h' * 40, title='feat: x', body='Body.\n'):
    pr = {'number': 7, 'html_url': 'https://github.com/cplieger/langtag/pull/7', 'title': title}
    return {**pr, 'body': body, 'head': {'sha': head_sha}}


class Propose(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        write_proposal(
            Path(tmp.name),
            ['scripttable.go', 'names/table.go'],
            {'scripttable.go': b'new\n', 'names/table.go': b'names\n'},
        )
        self.proposal = cdr.Proposal(Path(tmp.name), PATHS)

    def run_propose(self, routes):
        fake = FakeGitHub(routes)
        gh = ghrest.Client(run=fake, sleep=lambda _: None)
        return cdr.propose(gh, 'langtag', BOT, BASE, self.proposal), fake.writes

    def with_pr(self, pr, commits, head_tree='oldtree'):
        return base_routes(
            {
                ('GET', f'{API}/pulls?state=open&head=cplieger:{cdr.BRANCH}&base=dev'): [pr],
                ('GET', f'{API}/pulls/7/commits?per_page=100&page=1'): commits,
                ('GET', f'{API}/git/commits/{pr["head"]["sha"]}'): {'tree': {'sha': head_tree}},
                ('GET', f'{API}/git/ref/heads/{cdr.BRANCH}'): {
                    'object': {'sha': pr['head']['sha']}
                },
            }
        )

    def test_a_first_proposal_opens_the_pull_request(self):
        said, writes = self.run_propose(base_routes())
        self.assertEqual(said, cdr.Outcome('opened https://github.com/cplieger/langtag/pull/9'))

        def blob(data):
            return (
                'POST',
                f'{API}/git/blobs',
                {'content': base64.b64encode(data).decode(), 'encoding': 'base64'},
            )

        def entry(path):
            return {'path': path, 'mode': '100644', 'type': 'blob', 'sha': 'blob1'}

        self.assertEqual(
            writes,
            [
                blob(b'new\n'),
                blob(b'names\n'),
                (
                    'POST',
                    f'{API}/git/trees',
                    {
                        'base_tree': 'basetree',
                        'tree': [entry('scripttable.go'), entry('names/table.go')],
                    },
                ),
                (
                    'POST',
                    f'{API}/git/commits',
                    {'message': 'feat: x', 'tree': 'newtree', 'parents': [BASE]},
                ),
                ('POST', f'{API}/git/refs', {'ref': f'refs/heads/{cdr.BRANCH}', 'sha': 'c' * 40}),
                (
                    'POST',
                    f'{API}/pulls',
                    {'title': 'feat: x', 'body': 'Body.\n', 'head': cdr.BRANCH, 'base': 'dev'},
                ),
            ],
        )

    def test_a_branch_left_by_a_closed_pull_request_is_moved(self):
        routes = base_routes({('GET', f'{API}/git/ref/heads/{cdr.BRANCH}'): {'object': {}}})
        said, writes = self.run_propose(routes)
        self.assertEqual(said.text, 'opened https://github.com/cplieger/langtag/pull/9')
        self.assertIn(
            ('PATCH', f'{API}/git/refs/heads/{cdr.BRANCH}', {'sha': 'c' * 40, 'force': True}),
            writes,
        )
        self.assertFalse([w for w in writes if w[:2] == ('POST', f'{API}/git/refs')])

    def test_an_open_bot_pull_request_is_updated(self):
        said, writes = self.run_propose(self.with_pr(open_pr(), [{'author': {'login': BOT}}]))
        self.assertEqual(said.text, 'updated https://github.com/cplieger/langtag/pull/7')
        self.assertEqual(
            [w[:2] for w in writes][-3:],
            [
                ('POST', f'{API}/git/commits'),
                ('PATCH', f'{API}/git/refs/heads/{cdr.BRANCH}'),
                ('PATCH', f'{API}/pulls/7'),
            ],
        )
        self.assertEqual(writes[-1][2], {'title': 'feat: x', 'body': 'Body.\n'})

    def test_an_open_pull_request_with_the_same_change_gets_no_commit(self):
        routes = self.with_pr(open_pr(), [{'author': {'login': BOT}}], head_tree='newtree')
        said, writes = self.run_propose(routes)
        self.assertEqual(
            said.text, 'https://github.com/cplieger/langtag/pull/7 already proposes this change'
        )
        self.assertEqual(
            [w[:2] for w in writes],
            [
                ('POST', f'{API}/git/blobs'),
                ('POST', f'{API}/git/blobs'),
                ('POST', f'{API}/git/trees'),
            ],
        )

    def test_an_open_pull_request_with_the_same_files_gets_the_new_text(self):
        routes = self.with_pr(
            open_pr(body='Old.\n'), [{'author': {'login': BOT}}], head_tree='newtree'
        )
        said, writes = self.run_propose(routes)
        self.assertEqual(
            said.text, 'updated the text of https://github.com/cplieger/langtag/pull/7'
        )
        self.assertEqual(
            writes[-1], ('PATCH', f'{API}/pulls/7', {'title': 'feat: x', 'body': 'Body.\n'})
        )
        self.assertFalse(
            [w for w in writes if w[1].endswith(('/git/commits', f'/heads/{cdr.BRANCH}'))]
        )

    def test_an_open_pull_request_carrying_someone_elses_commit_is_left_alone(self):
        commits = [{'author': {'login': BOT}}, {'author': {'login': 'cplieger'}}, {'author': None}]
        said, writes = self.run_propose(self.with_pr(open_pr(), commits))
        self.assertEqual(
            said,
            cdr.Outcome(
                'https://github.com/cplieger/langtag/pull/7 carries commits by (unknown), '
                'cplieger, so it is left as it is and this run proposes nothing',
                warning=True,
            ),
        )
        self.assertEqual(writes, [])

    def test_a_base_still_in_the_base_branch_history_is_proposed_on(self):
        for status in ('identical', 'ahead'):
            with self.subTest(status):
                routes = base_routes()
                routes['GET', f'{API}/compare/{BASE}...{TIP}?per_page=1'] = {'status': status}
                said, writes = self.run_propose(routes)
                self.assertEqual(said.text, 'opened https://github.com/cplieger/langtag/pull/9')
                self.assertIn(
                    (
                        'POST',
                        f'{API}/git/commits',
                        {'message': 'feat: x', 'tree': 'newtree', 'parents': [BASE]},
                    ),
                    writes,
                )

    def test_a_base_gone_from_the_base_branch_history_writes_nothing(self):
        for status in ('diverged', 'behind', 'gone'):
            with self.subTest(status):
                routes = self.with_pr(open_pr(), [{'author': {'login': BOT}}])
                compare = 'GET', f'{API}/compare/{BASE}...{TIP}?per_page=1'
                if status == 'gone':
                    del routes[compare]
                else:
                    routes[compare] = {'status': status}
                fake = FakeGitHub(routes)
                gh = ghrest.Client(run=fake, sleep=lambda _: None)
                with self.assertRaisesRegex(
                    cdr.RefusedError, f'{BASE} is not in the history of dev \\({status}\\)'
                ):
                    cdr.propose(gh, 'langtag', BOT, BASE, self.proposal)
                self.assertEqual(fake.writes, [])

    def test_a_missing_base_branch_writes_nothing(self):
        routes = base_routes()
        del routes['GET', f'{API}/git/ref/heads/dev']
        fake = FakeGitHub(routes)
        gh = ghrest.Client(run=fake, sleep=lambda _: None)
        with self.assertRaisesRegex(cdr.RefusedError, 'langtag has no dev branch to propose to'):
            cdr.propose(gh, 'langtag', BOT, BASE, self.proposal)
        self.assertEqual(fake.writes, [])

    def test_a_base_branch_edit_to_a_proposed_path_writes_nothing(self):
        routes = self.with_pr(open_pr(), [{'author': {'login': BOT}}])
        routes['GET', f'{API}/contents/scripttable.go?ref={TIP}'] = {'sha': 'by hand'}
        del routes['GET', f'{API}/contents/names/table.go?ref={TIP}']
        fake = FakeGitHub(routes)
        gh = ghrest.Client(run=fake, sleep=lambda _: None)
        with self.assertRaisesRegex(
            cdr.RefusedError,
            re.escape(
                'dev changed scripttable.go (scripttable.go blob to by hand), names/table.go'
                f' (names/table.go blob to absent) since {BASE}, the commit the refresh ran on,'
                " which the proposal would overwrite. Run langtag's community-data-refresh.yaml"
            ),
        ):
            cdr.propose(gh, 'langtag', BOT, BASE, self.proposal)
        self.assertEqual(fake.writes, [])

    def test_a_pull_request_left_alone_warns_on_the_run(self):
        left = cdr.Outcome('https://github.com/cplieger/langtag/pull/7 is left', warning=True)
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / 'summary'
            out = io.StringIO()
            with (
                unittest.mock.patch.object(cdr, 'propose', return_value=left),
                unittest.mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}),
                redirect_stdout(out),
            ):
                argv = ['propose', '--repo', 'langtag', '--base', BASE]
                argv += ['--proposal', str(self.proposal.root), '--bot', BOT]
                self.assertEqual(cdr.main(argv), 0)
            self.assertEqual(out.getvalue(), f'::warning::{left.text}\n')
            self.assertEqual(summary.read_text(), left.text + '\n')

    def test_the_command_writes_nothing_for_an_unchanged_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_proposal(Path(tmp), [], title=None, body=None)
            out = io.StringIO()
            with (
                unittest.mock.patch.object(cdr, 'propose', side_effect=AssertionError),
                redirect_stdout(out),
            ):
                argv = ['propose', '--repo', 'langtag', '--base', BASE, '--proposal', tmp]
                self.assertEqual(cdr.main([*argv, '--bot', BOT]), 0)
            self.assertEqual(out.getvalue(), 'no change\n')

    def test_the_command_refuses_an_unlisted_repository(self):
        err = io.StringIO()
        argv = ['propose', '--repo', 'nope', '--base', BASE]
        argv += ['--proposal', str(self.proposal.root), '--bot', BOT]
        with (
            unittest.mock.patch.object(cdr, 'propose', side_effect=AssertionError),
            redirect_stderr(err),
        ):
            self.assertEqual(cdr.main(argv), 1)
        self.assertIn("repo 'nope' is not refreshed", err.getvalue())


GIT_ENV = {
    'GIT_AUTHOR_NAME': 't',
    'GIT_AUTHOR_EMAIL': 't@example.invalid',
    'GIT_COMMITTER_NAME': 't',
    'GIT_COMMITTER_EMAIL': 't@example.invalid',
    'GIT_CONFIG_GLOBAL': '/dev/null',
    'GIT_CONFIG_NOSYSTEM': '1',
}


class GitGitHub:
    """`gh api -i` over a real git repository: the git data, refs, contents and
    compare endpoints propose calls act on it, so git, not a route table,
    decides what the proposal commit holds and whether its pull request merges."""

    def __init__(self, root: Path):
        self.root = root
        self.pulls = []
        self.writes = []
        self.git('init', '-q', '-b', 'dev')

    def git(self, *args, stdin=None, env=None, check=True) -> str:
        return (
            subprocess.run(
                ['git', *args],
                cwd=self.root,
                input=stdin,
                capture_output=True,
                check=check,
                env={**os.environ, **GIT_ENV, **(env or {})},
            )
            .stdout.decode()
            .strip()
        )

    def commit(self, files: dict[str, bytes | None]) -> str:
        """A commit writing `files` (None deletes one) on dev's tip, or a root
        commit when dev is gone; dev moves to it."""
        parent = self.git('rev-parse', '-q', '--verify', 'dev', check=False)
        index = {'GIT_INDEX_FILE': str(self.root / '.git' / 'test-index')}
        self.git('read-tree', *([parent] if parent else ['--empty']), env=index)
        for path, data in files.items():
            if data is None:
                self.git('update-index', '--force-remove', path, env=index)
                continue
            blob = self.git('hash-object', '-w', '--stdin', stdin=data)
            self.git('update-index', '--add', '--cacheinfo', f'100644,{blob},{path}', env=index)
        tree = self.git('write-tree', env=index)
        sha = self.git('commit-tree', tree, *(['-p', parent] if parent else []), '-m', 'c')
        self.git('update-ref', 'refs/heads/dev', sha)
        return sha

    def mergeable(self) -> bool:
        merge = ['merge-tree', '--write-tree', 'dev', cdr.BRANCH]
        return (
            subprocess.run(
                ['git', *merge], cwd=self.root, capture_output=True, check=False
            ).returncode
            == 0
        )

    def answer(self, method, path, body, headers):
        git = path.removeprefix(f'{API}/git/')
        if (method, path) == ('GET', f'{API}/pulls?state=open&head=cplieger:{cdr.BRANCH}&base=dev'):
            return self.pulls
        if method == 'GET' and (
            m := re.fullmatch(rf'{API}/compare/(\w+)\.\.\.(\w+)\?per_page=1', path)
        ):
            base, dev = m[1], m[2]
            ancestor = self.git('merge-base', base, dev, check=False)
            if not ancestor:
                return None
            if base == dev:
                return {'status': 'identical'}
            if ancestor in (base, dev):
                return {'status': 'ahead' if ancestor == base else 'behind'}
            return {'status': 'diverged'}
        if method == 'GET' and (m := re.fullmatch(rf'{API}/contents/(.+)\?ref=(\w+)', path)):
            sha = self.git('rev-parse', '-q', '--verify', f'{m[2]}:{m[1]}', check=False)
            if not sha:
                return None
            # The default media type answers a folder as the list of its entries:
            # https://docs.github.com/en/rest/repos/contents
            folder = self.git('cat-file', '-t', sha) == 'tree'
            if folder and 'Accept: application/vnd.github.object+json' not in headers:
                return [{'sha': e.split()[2]} for e in self.git('ls-tree', sha).splitlines()]
            return {'sha': sha}
        if method == 'GET' and git.startswith('commits/'):
            return {
                'tree': {'sha': self.git('rev-parse', git.removeprefix('commits/') + '^{tree}')}
            }
        if method == 'GET' and git.startswith('ref/heads/'):
            sha = self.git('rev-parse', '-q', '--verify', 'refs/' + git[4:], check=False)
            return {'object': {'sha': sha}} if sha else None
        if (method, git) == ('POST', 'blobs'):
            data = base64.b64decode(body['content'])
            return {'sha': self.git('hash-object', '-w', '--stdin', stdin=data)}
        if (method, git) == ('POST', 'trees'):
            index = {'GIT_INDEX_FILE': str(self.root / '.git' / 'api-index')}
            self.git('read-tree', body['base_tree'], env=index)
            for e in body['tree']:
                info = f'{e["mode"]},{e["sha"]},{e["path"]}'
                self.git('update-index', '--add', '--cacheinfo', info, env=index)
            return {'sha': self.git('write-tree', env=index)}
        if (method, git) == ('POST', 'commits'):
            parents = [a for p in body['parents'] for a in ('-p', p)]
            return {'sha': self.git('commit-tree', body['tree'], *parents, '-m', body['message'])}
        if (method, git) == ('POST', 'refs'):
            self.git('update-ref', body['ref'], body['sha'])
            return {}
        if method == 'POST' and path == f'{API}/pulls':
            self.pulls.append({**body, 'number': 9, 'html_url': 'https://example.invalid/9'})
            return self.pulls[-1]
        return None

    def __call__(self, args, stdin=None, timeout=None):
        method = args[args.index('-X') + 1] if '-X' in args else 'GET'
        path = args[args.index('--input') - 1] if '--input' in args else args[-1]
        body = json.loads(stdin) if stdin else None
        headers = [args[i + 1] for i, a in enumerate(args) if a == '-H']
        if method != 'GET':
            self.writes.append((method, path))
        out = self.answer(method, path, body, headers)
        return reply(404, {'message': 'Not Found'}) if out is None else reply(200, out)


class ProposeOnGit(unittest.TestCase):
    """The proposal commit sits on the refresh's commit and goes ahead only
    while dev still holds that commit and every proposed path as it was."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'repo').mkdir()
        self.gh = GitGitHub(root / 'repo')
        self.root = self.gh.commit({'README.md': b'readme\n'})
        self.base = self.gh.commit({'scripttable.go': b'one\ntwo\nthree\n'})
        write_proposal(root / 'p', ['scripttable.go'], {'scripttable.go': b'one\ntwo\n3\n'})
        self.proposal = cdr.Proposal(root / 'p', PATHS)

    def propose(self):
        gh = ghrest.Client(run=self.gh, sleep=lambda _: None)
        return cdr.propose(gh, 'langtag', BOT, self.base, self.proposal)

    def refused(self, want: str):
        with self.assertRaisesRegex(cdr.RefusedError, want):
            self.propose()
        self.assertEqual(self.gh.writes, [])

    def test_dev_moved_on_an_unlisted_path_proceeds_and_merges(self):
        self.gh.commit({'README.md': b'edited\n'})
        self.assertEqual(self.propose().text, 'opened https://example.invalid/9')
        self.assertEqual(self.gh.git('rev-parse', f'{cdr.BRANCH}^'), self.base)
        self.assertTrue(self.gh.mergeable())

    def test_dev_edited_a_listed_path_elsewhere_in_the_file_is_refused(self):
        was = self.gh.git('rev-parse', f'{self.base}:scripttable.go')
        self.gh.commit({'scripttable.go': b'1\ntwo\nthree\n'})
        now = self.gh.git('rev-parse', 'dev:scripttable.go')
        self.refused(re.escape(f'dev changed scripttable.go ({was} to {now}) since {self.base}'))

    def test_dev_deleted_a_listed_path_is_refused(self):
        was = self.gh.git('rev-parse', f'{self.base}:scripttable.go')
        self.gh.commit({'scripttable.go': None})
        self.refused(re.escape(f'dev changed scripttable.go ({was} to absent)'))

    def test_dev_replaced_a_listed_path_with_a_folder_is_refused(self):
        was = self.gh.git('rev-parse', f'{self.base}:scripttable.go')
        self.gh.commit({'scripttable.go': None, 'scripttable.go/x.go': b'x\n'})
        now = self.gh.git('rev-parse', 'dev:scripttable.go')
        self.refused(re.escape(f'dev changed scripttable.go ({was} to {now})'))

    def test_dev_rewritten_from_a_shared_commit_is_refused(self):
        self.gh.git('update-ref', 'refs/heads/dev', self.root)
        self.gh.commit({'scripttable.go': b'one\ntwo\nthree\n', 'README.md': b'again\n'})
        self.refused(r'is not in the history of dev \(diverged\)')

    def test_dev_rewritten_from_no_shared_commit_is_refused(self):
        self.gh.git('update-ref', '-d', 'refs/heads/dev')
        self.gh.commit({'scripttable.go': b'one\ntwo\nthree\n'})
        self.refused(r'is not in the history of dev \(gone\), so it was rewritten')

    def test_the_proposal_commit_changes_only_its_listed_paths(self):
        self.gh.commit({'README.md': b'edited\n', 'names/table.go': b'n\n'})
        self.propose()
        changed = self.gh.git('diff', '--name-only', self.base, cdr.BRANCH).split()
        self.assertEqual(changed, self.proposal.changes)


RUNS = (
    f'{API}/actions/workflows/{cdr.WORKFLOW}/runs'
    '?branch=dev&status=completed&exclude_pull_requests=true&per_page=100'
)


def a_run(run_id=41, **kw):
    run = {
        'id': run_id,
        'event': 'schedule',
        'head_branch': 'dev',
        'head_sha': BASE,
        'head_repository': {'full_name': 'cplieger/langtag'},
        'conclusion': 'success',
        'html_url': f'https://github.com/cplieger/langtag/actions/runs/{run_id}',
    }
    return {**run, **kw}


class Locate(unittest.TestCase):
    def locate(self, runs):
        fake = FakeGitHub({('GET', RUNS): {'workflow_runs': runs}})
        return cdr.locate(ghrest.Client(run=fake, sleep=lambda _: None), 'langtag'), fake.writes

    def test_the_newest_run_of_the_base_branch_is_read(self):
        runs = [
            a_run(1, event='pull_request', conclusion='failure'),
            a_run(2, event='push'),
            a_run(3, head_branch='other'),
            a_run(4, head_repository={'full_name': 'someone/langtag'}),
            a_run(5, event='workflow_dispatch', head_sha='e' * 40),
            a_run(6),
        ]
        run, writes = self.locate(runs)
        self.assertEqual(run, cdr.Run(5, 'e' * 40, a_run(5)['html_url']))
        self.assertEqual(writes, [])

    def test_a_failed_newest_run_is_refused_rather_than_an_older_one_read(self):
        with self.assertRaisesRegex(
            cdr.RefusedError, r'runs/7 concluded failure, so it proposes nothing'
        ):
            self.locate([a_run(7, conclusion='failure'), a_run(6)])

    def test_refusals(self):
        for name, runs, want in (
            (
                'no run',
                [a_run(1, event='pull_request')],
                'langtag has no completed community-data-refresh.yaml run on dev',
            ),
            ('no commit', [a_run(head_sha='HEAD')], 'names no run id and commit'),
            ('no id', [a_run(run_id='41')], 'names no run id and commit'),
        ):
            with self.subTest(name), self.assertRaisesRegex(cdr.RefusedError, want):
                self.locate(runs)

    def test_the_command_writes_the_run_id_and_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            with (
                unittest.mock.patch.object(cdr, 'locate', return_value=cdr.Run(41, BASE, 'u')),
                unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(out)}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cdr.main(['locate', '--repo', 'langtag']), 0)
            self.assertEqual(out.read_text(), f'run-id=41\nbase={BASE}\n')


class Workflow(unittest.TestCase):
    def test_plan_outputs_the_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'cwd').mkdir()
            (tmp / 'cwd' / 'scripts').symlink_to(Path(__file__).resolve().parent)
            (tmp / 'ctx.json').write_text(json.dumps({'inputs': {'only': 'langtag'}}))
            argv = [
                '--workflow',
                str(WORKFLOW),
                '--job',
                'plan',
                '--context',
                str(tmp / 'ctx.json'),
            ]
            argv += ['--cwd', str(tmp / 'cwd'), '--out', str(tmp / 'out.json')]
            with redirect_stderr(io.StringIO()):
                rc = workflow_replay.main(argv)
            result = json.loads((tmp / 'out.json').read_text())
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(json.loads(result['outputs']['matrix']), cdr.plan('langtag'))


APP_KEY = {
    'client-id': '${{ secrets.SYNC_APP_ID }}',
    'private-key': '${{ secrets.SYNC_APP_PRIVATE_KEY }}',
    'owner': 'cplieger',
    'repositories': '${{ matrix.repo }}',
}
CHECKOUT = {
    'name': 'Checkout',
    'uses': 'actions/checkout@SHA',
    'with': {'persist-credentials': False},
}
JOBS = {
    'plan': {
        'runs-on': 'ubuntu-26.04',
        'timeout-minutes': 5,
        'outputs': {'matrix': '${{ steps.plan.outputs.matrix }}'},
        'steps': [
            CHECKOUT,
            {
                'name': 'Plan the repositories',
                'id': 'plan',
                'env': {'ONLY': '${{ inputs.only }}'},
                'run': 'set -euo pipefail\n'
                'python3 scripts/community_data_refresh.py plan --only "$ONLY"\n',
            },
        ],
    },
    'propose': {
        'name': 'propose (${{ matrix.repo }})',
        'needs': 'plan',
        'strategy': {
            'fail-fast': False,
            'matrix': {'include': '${{ fromJSON(needs.plan.outputs.matrix) }}'},
        },
        'runs-on': 'ubuntu-26.04',
        'timeout-minutes': 10,
        'permissions': {'contents': 'read'},
        'concurrency': {
            'group': 'community-data-refresh-${{ matrix.repo }}',
            'cancel-in-progress': False,
        },
        'steps': [
            CHECKOUT,
            {
                'name': 'Mint the read token',
                'id': 'read-token',
                'uses': 'actions/create-github-app-token@SHA',
                'with': {**APP_KEY, 'permission-actions': 'read'},
            },
            {
                'name': 'Find the refresh run',
                'id': 'run',
                'env': {
                    'GH_TOKEN': '${{ steps.read-token.outputs.token }}',
                    'REPO': '${{ matrix.repo }}',
                },
                'run': 'set -euo pipefail\n'
                'python3 scripts/community_data_refresh.py locate --repo "$REPO"\n',
            },
            {
                'name': 'Download the proposal',
                'uses': 'actions/download-artifact@SHA',
                'with': {
                    'name': 'community-data-refresh',
                    'path': '${{ runner.temp }}/proposal',
                    'github-token': '${{ steps.read-token.outputs.token }}',
                    'repository': 'cplieger/${{ matrix.repo }}',
                    'run-id': '${{ steps.run.outputs.run-id }}',
                },
            },
            {
                'name': 'Check the proposal',
                'id': 'check',
                'env': {'REPO': '${{ matrix.repo }}', 'PROPOSAL': '${{ runner.temp }}/proposal'},
                'run': 'set -euo pipefail\n'
                'python3 scripts/community_data_refresh.py check --repo "$REPO"'
                ' --proposal "$PROPOSAL"\n',
            },
            {
                'name': 'Mint the App token',
                'if': "steps.check.outputs.changed == 'true'",
                'id': 'app-token',
                'uses': 'actions/create-github-app-token@SHA',
                'with': {
                    **APP_KEY,
                    'permission-contents': 'write',
                    'permission-pull-requests': 'write',
                },
            },
            {
                'name': "Propose cplieger/${{ matrix.repo }}'s refresh",
                'if': "steps.check.outputs.changed == 'true'",
                'env': {
                    'GH_TOKEN': '${{ steps.app-token.outputs.token }}',
                    'APP_SLUG': '${{ steps.app-token.outputs.app-slug }}',
                    'REPO': '${{ matrix.repo }}',
                    'BASE': '${{ steps.run.outputs.base }}',
                    'PROPOSAL': '${{ runner.temp }}/proposal',
                },
                'run': 'set -euo pipefail\n'
                'python3 scripts/community_data_refresh.py propose --repo "$REPO"'
                ' --base "$BASE" --proposal "$PROPOSAL" --bot "${APP_SLUG}[bot]"\n',
            },
        ],
    },
    'notify': {
        'needs': ['plan', 'propose'],
        'if': 'always()',
        'permissions': {'actions': 'read', 'contents': 'read', 'issues': 'write'},
        'uses': './.github/workflows/notify-failure.yaml',
        'with': {
            'workflow': '${{ github.workflow }}',
            'run-url': '${{ github.server_url }}/${{ github.repository }}'
            '/actions/runs/${{ github.run_id }}',
            'scope': '${{ inputs.only }}',
        },
    },
}


# YAML 1.1 resolves on as true, which PyYAML follows: https://yaml.org/type/bool.html
TOP = {
    'name': 'Community data refresh',
    True: {
        'schedule': [{'cron': '17 8 3 * *'}],
        'workflow_dispatch': {
            'inputs': {
                'only': {
                    'description': 'Space/comma-separated repo names to limit the run.'
                    ' Empty runs every listed repository.',
                    'type': 'string',
                    'required': False,
                    'default': '',
                }
            }
        },
    },
    'permissions': {'contents': 'read'},
}


class WorkflowShape(unittest.TestCase):
    """The trust boundary is the whole workflow, compared exactly: it runs only
    on schedule or by hand, no job checks out or runs a refreshed repository's
    code, the read token can only read its runs, and the write token is minted
    only for a checked change and reaches only the propose step. Changing any
    key or step changes TOP or JOBS."""

    def unpinned(self, job):
        """`job` with each action's commit SHA replaced by `SHA`, after checking
        it is one."""
        steps = []
        for step in job.get('steps', []):
            if 'uses' in step:
                action, _, sha = step['uses'].partition('@')
                self.assertRegex(sha, r'\A[0-9a-f]{40}\Z', step['uses'])
                step = {**step, 'uses': f'{action}@SHA'}
            steps.append(step)
        return {**job, 'steps': steps} if 'steps' in job else job

    def test_the_workflow_is_exactly_the_reviewed_one(self):
        doc = yaml.safe_load(WORKFLOW.read_text())
        self.assertEqual({k: v for k, v in doc.items() if k != 'jobs'}, TOP)
        self.assertEqual(list(doc['jobs']), list(JOBS))
        for name, want in JOBS.items():
            with self.subTest(name):
                self.assertEqual(self.unpinned(doc['jobs'][name]), want)


if __name__ == '__main__':
    unittest.main()
