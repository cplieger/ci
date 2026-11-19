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
ROW = cdr.REPOS['langtag']
PATHS = ROW['paths']


class Plan(unittest.TestCase):
    def test_an_empty_only_covers_every_repository(self):
        self.assertEqual(cdr.plan(''), [{'repo': n} for n in cdr.REPOS])

    def test_only_selects_named_repositories_once(self):
        self.assertEqual(cdr.plan('langtag, langtag'), [{'repo': 'langtag'}])

    def test_an_unlisted_repository_is_refused(self):
        with self.assertRaisesRegex(
            cdr.RefusedError, 'nope not refreshed. The repositories are access, langtag, subflux'
        ):
            cdr.plan('nope')

    def test_every_row_is_well_formed(self):
        name = re.compile(r'[a-z0-9][a-z0-9-]*')
        for repo, row in cdr.REPOS.items():
            with self.subTest(repo):
                self.assertLessEqual(set(row), {'branch', 'paths', 'upstreams', 'labels'})
                self.assertIsInstance(row['branch'], str)
                self.assertIsInstance(row['paths'], tuple)
                for key, url in row.get('upstreams', {}).items():
                    self.assertRegex(key, name.pattern)
                    self.assertRegex(url, r'\Ahttps://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+\Z')
                labels = row.get('labels', ())
                self.assertIsInstance(labels, tuple)
                for label in labels:
                    self.assertTrue(name.fullmatch(label), label)
                self.assertNotIn(cdr.ISSUE_LABEL, labels)
                self.assertEqual(len(set(labels)), len(labels))

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


def write_proposal(
    root: Path, changes, files=None, title='feat: x\n', body='Body.\n', notices=None
):
    (root / cdr.FILES).mkdir(parents=True, exist_ok=True)
    if notices is not None:
        raw = notices if isinstance(notices, str) else json.dumps(notices)
        (root / cdr.NOTICES).write_text(raw)
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
        p = cdr.Proposal(self.root, ROW)
        self.assertEqual(
            (p.changes, p.title, p.body),
            (['names/table.go', 'scripttable.go'], 'feat: x', 'Body.\n'),
        )
        self.assertEqual(p.file('names/table.go').read_bytes(), b'n')

    def test_a_proposal_of_no_change_needs_no_text_or_files(self):
        (self.root / cdr.MANIFEST).write_text('{"changes": []}')
        p = cdr.Proposal(self.root, ROW)
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
                    cdr.Proposal(Path(tmp), ROW)

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
                    cdr.Proposal(root, ROW)

    def test_a_proposal_over_the_size_cap_is_refused(self):
        write_proposal(self.root, ['scripttable.go'], {'scripttable.go': b'x' * 11})
        with (
            unittest.mock.patch.object(cdr, 'FILES_MAX', 10),
            self.assertRaisesRegex(cdr.RefusedError, 'holds 11 bytes, over 10'),
        ):
            cdr.Proposal(self.root, ROW)
        with unittest.mock.patch.object(cdr, 'FILES_MAX', 11):
            cdr.Proposal(self.root, ROW)

    def test_an_unreadable_manifest_is_refused(self):
        with self.assertRaisesRegex(cdr.RefusedError, 'proposal.json is unreadable'):
            cdr.Proposal(self.root, ROW)
        (self.root / cdr.MANIFEST).write_text('[]')
        with self.assertRaisesRegex(cdr.RefusedError, 'holds no list of paths'):
            cdr.Proposal(self.root, ROW)

    def test_the_command_outputs_whether_it_changes_anything(self):
        for changes, files, want in (
            ([], {}, 'changed=false'),
            (['scripttable.go'], {'scripttable.go': b'x'}, 'changed=true'),
        ):
            with self.subTest(want), tempfile.TemporaryDirectory() as tmp:
                write_proposal(Path(tmp) / 'p', changes, files)
                out, plan = Path(tmp) / 'out', Path(tmp) / 'plan.json'
                with (
                    unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(out)}),
                    unittest.mock.patch.object(cdr, 'read_issues', side_effect=AssertionError),
                    redirect_stdout(io.StringIO()),
                ):
                    argv = ['check', '--repo', 'langtag', '--proposal', str(Path(tmp) / 'p')]
                    self.assertEqual(cdr.main([*argv, '--plan', str(plan)]), 0)
                self.assertEqual(out.read_text(), f'{want}\nnotices_changed=false\n')
                self.assertEqual(json.loads(plan.read_text()), [])

    def test_the_command_refuses_an_unlisted_repository(self):
        write_proposal(self.root, [])
        with redirect_stderr(io.StringIO()):
            argv = ['check', '--repo', 'nope', '--proposal', str(self.root)]
            self.assertEqual(cdr.main([*argv, '--plan', str(self.root / 'plan.json')]), 1)


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
        self.proposal = cdr.Proposal(Path(tmp.name), ROW)

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
        self.proposal = cdr.Proposal(root / 'p', ROW)

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
            ('a commit and a newline', [a_run(head_sha=f'{BASE}\n')], 'names no run id and commit'),
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


SUBFLUX = cdr.REPOS['subflux']
SAPI = 'repos/cplieger/subflux'
FROM, TO = 'a' * 40, 'c' * 40
SONARR_COMPARE = f'https://github.com/Sonarr/Sonarr/compare/{FROM}...{TO}'


def a_notice(nid='sonarr-parser', **kw):
    notice = {
        'id': nid,
        'title': f'Upstream change {nid}',
        'body': f'{nid} changed.\n',
        'labels': ['candidate'],
    }
    return {**notice, **kw}


def notices_of(entries, row=None):
    with tempfile.TemporaryDirectory() as tmp:
        write_proposal(Path(tmp), [], notices=entries)
        return cdr.Proposal(Path(tmp), row or SUBFLUX).notices


def marker_line(nid):
    return f'<!-- community-data id={nid} -->'


class Notices(unittest.TestCase):
    def refused(self, entries, want, row=None):
        with tempfile.TemporaryDirectory() as tmp:
            write_proposal(Path(tmp), [], notices=entries)
            with self.assertRaisesRegex(cdr.RefusedError, want):
                cdr.Proposal(Path(tmp), row or SUBFLUX)

    def test_a_notice_reads_with_the_link_and_marker_the_job_writes(self):
        got = notices_of(
            [
                a_notice(upstream='sonarr', **{'from': FROM, 'to': TO}),
                a_notice('plain', labels=[], body=''),
            ]
        )
        self.assertEqual(
            got,
            [
                cdr.Notice(
                    'sonarr-parser',
                    'Upstream change sonarr-parser',
                    f'sonarr-parser changed.\n\nUpstream change: {SONARR_COMPARE}\n\n'
                    f'{marker_line("sonarr-parser")}\n',
                    ('candidate',),
                ),
                cdr.Notice('plain', 'Upstream change plain', f'{marker_line("plain")}\n', ()),
            ],
        )

    def test_a_notice_naming_an_upstream_alone_gets_no_link(self):
        got = notices_of([a_notice(upstream='radarr')])
        self.assertEqual(got[0].body, f'sonarr-parser changed.\n\n{marker_line("sonarr-parser")}\n')

    def test_notices_over_50_refused(self):
        many = [a_notice(f'n{i}') for i in range(51)]
        self.refused(many, 'notices.json holds 51 notices, over 50')
        self.assertEqual(len(notices_of(many[:50])), 50)

    def test_notice_id_pattern(self):
        for nid in ('a', '0', 'a-1', 'x' * 64):
            with self.subTest(nid):
                self.assertEqual(notices_of([a_notice(nid)])[0].id, nid)
        for nid in ('', 'A', '-a', 'a_b', 'a b', 'a\n', 'é', 'x' * 65, 7, None):
            with self.subTest(repr(nid)):
                self.refused([a_notice(nid)], 'is not a notice id')
        self.refused([a_notice('a'), a_notice('a')], "notice 'a' is listed twice")

    def test_label_outside_row_refused(self):
        for labels, want in (
            (['bug'], "notice 'sonarr-parser': label 'bug' is not one of candidate, out-of-model"),
            (['community-data'], "label 'community-data' is not one of"),
            (['candidate', 'candidate'], 'names a label twice'),
            ('candidate', 'labels is not a list of labels'),
            ([7], 'labels is not a list of labels'),
        ):
            with self.subTest(repr(labels)):
                self.refused([a_notice(labels=labels)], want)
        got = notices_of([a_notice(labels=['out-of-model', 'candidate'])])
        self.assertEqual(got[0].labels, ('out-of-model', 'candidate'))

    def test_undeclared_upstream_refused(self):
        for upstream in ('lidarr', ['sonarr'], ''):
            with self.subTest(repr(upstream)):
                self.refused(
                    [a_notice(upstream=upstream)],
                    re.escape(f'upstream {upstream!r} is not one of trash-guides, sonarr, radarr'),
                )
        self.refused([a_notice(**{'from': FROM, 'to': TO})], 'from and to need an upstream')

    def test_from_and_to_are_40_hex_given_together(self):
        for kw in (
            {'from': FROM},
            {'to': TO},
            {'from': 'A' * 40, 'to': TO},
            {'from': FROM, 'to': 'c' * 39},
            {'from': FROM, 'to': 'main'},
            {'from': FROM, 'to': f'{TO}\n'},
            {'from': FROM, 'to': None},
        ):
            with self.subTest(repr(kw)):
                self.refused(
                    [a_notice(upstream='sonarr', **kw)], 'from and to are two 40-hex commits'
                )

    def test_body_with_link_refused(self):
        for text in (
            'see https://example.com',
            'ftp://x',
            'www.example.com',
            '[docs](guide.md)',
            '![x](y.png)',
            '[docs]: guide.md',
            '<a name="x">',
            '<A HREF=x>',
            '<img src=x>',
            'href=x',
            'MAILTO:x',
            'write to me@example.com',
        ):
            for field in ('title', 'body'):
                with self.subTest(f'{field}: {text}'):
                    self.refused(
                        [a_notice(**{field: text})],
                        f"notice 'sonarr-parser': the {field} holds a link",
                    )
        for text in ('Sonarr parser 4.0.1.2 changed', 'see [candidate] below', 'f(x) = y', 'a: b'):
            for field in ('title', 'body'):
                with self.subTest(f'{field}: {text}'):
                    notices_of([a_notice(**{field: text})])

    def test_notice_text_caps(self):
        for kw, want in (
            ({'title': ''}, 'the title must be one non-empty line'),
            ({'title': ' '}, 'the title must be one non-empty line'),
            ({'title': 'a\nb'}, 'the title must be one non-empty line'),
            ({'title': 'a\rb'}, 'the title must be one non-empty line'),
            ({'title': 'x' * 257}, 'the title is 257 characters, over 256'),
            ({'title': 7}, 'the title is not text'),
            ({'body': None}, 'the body is not text'),
        ):
            with self.subTest(repr(kw)):
                self.refused([a_notice(**kw)], want)
        self.assertEqual(notices_of([a_notice(title='x' * 256)])[0].title, 'x' * 256)
        tail = len(f'\n\n{marker_line("sonarr-parser")}\n')
        self.refused(
            [a_notice(body='x' * (cdr.BODY_MAX - tail + 1))], 'the body is 65537 characters'
        )
        got = notices_of([a_notice(body='x' * (cdr.BODY_MAX - tail))])
        self.assertEqual(len(got[0].body), cdr.BODY_MAX)

    def test_row_with_labels_requires_notices(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_proposal(Path(tmp), [])
            with self.assertRaisesRegex(
                cdr.RefusedError, 'notices.json is missing, and this row declares labels'
            ):
                cdr.Proposal(Path(tmp), SUBFLUX)
            self.assertIsNone(cdr.Proposal(Path(tmp), ROW).notices)
        self.assertEqual(notices_of([]), [])
        self.refused([], 'notices.json is present, but this row declares no labels', row=ROW)

    def test_an_unreadable_notices_file_is_refused(self):
        for raw, want in (
            ('{', 'notices.json is unreadable'),
            ('{}', 'notices.json holds no list of notices'),
            ('[7]', 'notice 0 is not an object'),
            ([{**a_notice(), 'url': 'x'}], 'notice 0 holds unknown keys: url'),
            ([{'id': 'a', 'title': 't', 'labels': []}], 'notice 0 lacks body'),
        ):
            with self.subTest(repr(raw)):
                self.refused(raw, want)
        with tempfile.TemporaryDirectory() as tmp:
            write_proposal(Path(tmp), [], notices=[a_notice()])
            with (
                unittest.mock.patch.object(cdr, 'FILES_MAX', 10),
                self.assertRaisesRegex(cdr.RefusedError, r'notices.json is \d+ bytes, over 10'),
            ):
                cdr.Proposal(Path(tmp), SUBFLUX)
            (Path(tmp) / cdr.NOTICES).unlink()
            (Path(tmp) / cdr.NOTICES).symlink_to('/etc/passwd')
            with self.assertRaisesRegex(cdr.RefusedError, 'notices.json is not a regular file'):
                cdr.Proposal(Path(tmp), SUBFLUX)


def an_issue(number, notice, state='open', **kw):
    issue = {
        'number': number,
        'state': state,
        'title': notice.title,
        'body': notice.body,
        'labels': [{'name': n} for n in (cdr.ISSUE_LABEL, *notice.labels)],
    }
    return {**issue, **kw}


def wants(kind, notice, number=None, labels=None):
    action = {'kind': kind, 'title': notice.title, 'body': notice.body}
    if number is not None:
        action['number'] = number
    return {**action, 'labels': labels or [cdr.ISSUE_LABEL, *notice.labels]}


BULK_MARKER = '<!-- community-data bulk -->'


def a_bulk(number, state='open', body=f'Old.\n\n{BULK_MARKER}\n'):
    labels = [{'name': cdr.ISSUE_LABEL}]
    return {'number': number, 'state': state, 'title': 'x', 'body': body, 'labels': labels}


class IssuePlan(unittest.TestCase):
    def setUp(self):
        self.a, self.b = notices_of([a_notice('a'), a_notice('b', labels=['out-of-model'])])

    def plan(self, notices, issues):
        return cdr.issue_plan(notices, issues, SUBFLUX['labels'])

    def test_a_new_notice_opens_an_issue(self):
        self.assertEqual(self.plan([self.a], []), [wants('create', self.a)])

    def test_an_open_issue_with_the_same_text_is_left(self):
        swapped = [{'name': 'candidate'}, {'name': cdr.ISSUE_LABEL}]
        theirs = [*swapped, {'name': 'priority'}]
        for name, issue in (
            ('same', an_issue(3, self.a)),
            ('label order', an_issue(3, self.a, labels=swapped)),
            ("a person's label", an_issue(3, self.a, labels=theirs)),
        ):
            with self.subTest(name):
                self.assertEqual(self.plan([self.a], [issue]), [])

    def test_an_open_issue_whose_text_changed_is_edited(self):
        moved = [{'name': cdr.ISSUE_LABEL}, {'name': 'out-of-model'}, {'name': 'priority'}]
        for name, kw, labels in (
            ('title', {'title': 'old'}, None),
            ('body', {'body': f'Old.\n\n{marker_line("a")}\n'}, None),
            ('labels', {'labels': moved}, [cdr.ISSUE_LABEL, 'candidate', 'priority']),
        ):
            with self.subTest(name):
                self.assertEqual(
                    self.plan([self.a], [an_issue(3, self.a, **kw)]),
                    [wants('edit', self.a, 3, labels)],
                )

    def test_a_closed_issue_listed_again_is_reopened(self):
        for kw in ({}, {'title': 'old'}):
            with self.subTest(repr(kw)):
                issue = an_issue(3, self.a, state='closed', **kw)
                self.assertEqual(self.plan([self.a], [issue]), [wants('reopen', self.a, 3)])

    def test_an_open_issue_no_longer_listed_is_closed(self):
        issues = [an_issue(3, self.a), an_issue(4, self.b), an_issue(5, self.b, state='closed')]
        self.assertEqual(self.plan([self.a], issues), [{'kind': 'close', 'number': 4}])
        self.assertEqual(
            self.plan([], issues), [{'kind': 'close', 'number': 3}, {'kind': 'close', 'number': 4}]
        )

    def test_only_the_last_line_is_the_marker(self):
        quoted = an_issue(3, self.a, body=f'{self.a.body}Thanks.\n')
        self.assertEqual(self.plan([self.a], [quoted]), [wants('create', self.a)])
        self.assertEqual(self.plan([], [quoted]), [])
        other = an_issue(3, self.a, body='<!-- community-data id=A -->')
        self.assertEqual(self.plan([], [other]), [])
        trailing = an_issue(3, self.a, body=f'{self.a.body}\n \n')
        self.assertEqual(self.plan([], [trailing]), [{'kind': 'close', 'number': 3}])
        self.assertEqual(self.plan([], [an_issue(3, self.a, body=None)]), [])

    def test_an_open_issue_outranks_a_closed_one_then_the_higher_number(self):
        a = self.a
        for name, issues, want in (
            ('open over closed', [an_issue(9, a, state='closed'), an_issue(3, a)], []),
            ('the higher open', [an_issue(3, a), an_issue(5, a)], [{'kind': 'close', 'number': 3}]),
            (
                'the higher closed',
                [an_issue(5, a, state='closed'), an_issue(3, a, state='closed', title='x')],
                [wants('reopen', a, 5)],
            ),
        ):
            with self.subTest(name):
                self.assertEqual(self.plan([a], issues), want)

    def test_openings_are_capped_at_10_with_the_rest_in_one_bulk_issue(self):
        ns = notices_of([a_notice(f'n{i:02}') for i in range(14)])
        issues = [
            an_issue(1, ns[0], title='old'),
            an_issue(2, ns[1], state='closed'),
            an_issue(3, ns[13], state='closed'),
        ]
        got = self.plan(ns, issues)
        self.assertEqual(
            got[:-1],
            [
                wants('edit', ns[0], 1),
                wants('reopen', ns[1], 2),
                *(wants('create', n) for n in ns[2:11]),
            ],
        )
        bulk = got[-1]
        self.assertEqual(
            {k: v for k, v in bulk.items() if k != 'body'},
            {'kind': 'create', 'title': cdr.BULK_TITLE, 'labels': [cdr.ISSUE_LABEL]},
        )
        self.assertTrue(bulk['body'].endswith(f'\n{BULK_MARKER}\n'))
        listed = [n.title for n in ns if n.title in bulk['body']]
        self.assertEqual(listed, [n.title for n in ns[11:]])
        positions = [bulk['body'].index(t) for t in listed]
        self.assertEqual(positions, sorted(positions))

    def test_the_bulk_issue_is_kept_in_step(self):
        ns = notices_of([a_notice(f'n{i:02}') for i in range(11)])
        body = self.plan(ns, [])[-1]['body']
        for name, bulk, want in (
            ('edited', a_bulk(50), [{'kind': 'edit', 'number': 50}]),
            ('reopened', a_bulk(50, state='closed'), [{'kind': 'reopen', 'number': 50}]),
            ('left', a_bulk(50, body=body) | {'title': cdr.BULK_TITLE}, []),
        ):
            with self.subTest(name):
                got = self.plan(ns, [bulk])[10:]
                for g in got:
                    self.assertEqual((g['title'], g['body']), (cdr.BULK_TITLE, body))
                self.assertEqual([{'kind': g['kind'], 'number': g['number']} for g in got], want)
        self.assertEqual(self.plan(ns[:10], [a_bulk(50)])[10:], [{'kind': 'close', 'number': 50}])
        self.assertEqual(self.plan(ns[:10], [a_bulk(50, state='closed')])[10:], [])


ISSUES = f'{SAPI}/issues?labels=community-data&state=all&sort=updated&direction=desc&per_page=100'


def issue_page(start, count=100):
    return [
        {'number': n, 'state': 'open', 'title': 't', 'body': '', 'labels': []}
        for n in range(start, start + count)
    ]


class ReadIssues(unittest.TestCase):
    def read(self, routes):
        fake = FakeGitHub(routes)
        return cdr.read_issues(ghrest.Client(run=fake, sleep=lambda _: None), 'subflux')

    def test_at_most_200_are_read_and_pull_requests_dropped(self):
        first = issue_page(1)
        first[0] = {**first[0], 'pull_request': {'url': 'x'}}
        routes = {
            ('GET', f'{ISSUES}&page=1'): first,
            ('GET', f'{ISSUES}&page=2'): issue_page(101),
            ('GET', f'{ISSUES}&page=3'): issue_page(201),
        }
        self.assertEqual([i['number'] for i in self.read(routes)], list(range(2, 201)))

    def test_a_short_page_ends_the_read(self):
        got = self.read({('GET', f'{ISSUES}&page=1'): issue_page(1, 5)})
        self.assertEqual([i['number'] for i in got], [1, 2, 3, 4, 5])


def issue_url(n):
    return f'https://github.com/cplieger/subflux/issues/{n}'


class ApplyIssues(unittest.TestCase):
    def setUp(self):
        self.a, self.b = notices_of([a_notice('a'), a_notice('b', labels=['out-of-model'])])
        self.fake = FakeGitHub(
            {
                ('GET', f'{SAPI}/labels/{cdr.ISSUE_LABEL}'): {'name': cdr.ISSUE_LABEL},
                ('POST', f'{SAPI}/labels'): {},
                ('POST', f'{SAPI}/issues'): {'html_url': issue_url(12)},
                **{('PATCH', f'{SAPI}/issues/{n}'): {'html_url': issue_url(n)} for n in (3, 4, 5)},
            }
        )
        self.gh = ghrest.Client(run=self.fake, sleep=lambda _: None)

    def test_each_kind_is_written_and_missing_labels_are_created_first(self):
        actions = [
            wants('create', self.a),
            wants('edit', self.b, 3),
            wants('reopen', self.a, 4),
            {'kind': 'close', 'number': 5},
        ]
        said = cdr.apply_issues(self.gh, 'subflux', actions)

        def text(n):
            return {'title': n.title, 'body': n.body, 'labels': [cdr.ISSUE_LABEL, *n.labels]}

        self.assertEqual(
            self.fake.writes,
            [
                ('POST', f'{SAPI}/labels', {'name': 'candidate', 'color': cdr.LABEL_COLOR}),
                ('POST', f'{SAPI}/labels', {'name': 'out-of-model', 'color': cdr.LABEL_COLOR}),
                ('POST', f'{SAPI}/issues', text(self.a)),
                ('PATCH', f'{SAPI}/issues/3', text(self.b)),
                ('PATCH', f'{SAPI}/issues/4', {'state': 'open', **text(self.a)}),
                ('PATCH', f'{SAPI}/issues/5', {'state': 'closed', 'state_reason': 'completed'}),
            ],
        )
        self.assertRegex(cdr.LABEL_COLOR, r'\A[0-9a-f]{6}\Z')
        self.assertEqual(
            said,
            [
                f'opened {issue_url(12)}',
                f'edited {issue_url(3)}',
                f'reopened {issue_url(4)}',
                f'closed {issue_url(5)}',
            ],
        )

    def test_a_malformed_plan_writes_nothing(self):
        for name, bad in (
            ('kind', {'kind': 'delete', 'number': 3}),
            ('number', {'kind': 'close', 'number': '3'}),
            ('create with a number', {**wants('create', self.a), 'number': 3}),
            ('edit without text', {'kind': 'edit', 'number': 3}),
            ('not an object', 'close 3'),
        ):
            with self.subTest(name):
                self.fake.writes.clear()
                with self.assertRaisesRegex(cdr.RefusedError, 'is not an issue action'):
                    cdr.apply_issues(self.gh, 'subflux', [wants('create', self.a), bad])
                self.assertEqual(self.fake.writes, [])


class CheckNotices(unittest.TestCase):
    def check(self, root: Path, issues=()):
        out, plan = root / 'out', root / 'plan.json'
        with (
            unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(out)}),
            unittest.mock.patch.object(cdr, 'read_issues', return_value=list(issues)) as read,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            argv = ['check', '--repo', 'subflux', '--proposal', str(root / 'p')]
            rc = cdr.main([*argv, '--plan', str(plan)])
        written = json.loads(plan.read_text()) if plan.exists() else None
        return rc, out.read_text() if out.exists() else '', written, read

    def test_the_command_writes_the_plan_and_whether_it_moves_an_issue(self):
        (a,) = notices_of([a_notice('a')])
        for name, issues, want, plan in (
            ('a new notice', [], 'true', [wants('create', a)]),
            ('in step', [an_issue(3, a)], 'false', []),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                write_proposal(Path(tmp) / 'p', [], notices=[a_notice('a')])
                rc, out, written, read = self.check(Path(tmp), issues)
                self.assertEqual(rc, 0)
                self.assertEqual(out, f'changed=false\nnotices_changed={want}\n')
                self.assertEqual(written, plan)
                read.assert_called_once()
                self.assertEqual(read.call_args.args[1:], ('subflux',))

    def test_a_refused_proposal_reads_and_plans_no_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_proposal(Path(tmp) / 'p', [])
            rc, out, written, read = self.check(Path(tmp))
        self.assertEqual((rc, out, written), (1, '', None))
        read.assert_not_called()


class IssuesCommand(unittest.TestCase):
    def test_the_command_applies_the_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / 'plan.json'
            plan.write_text(json.dumps([{'kind': 'close', 'number': 5}]))
            out = io.StringIO()
            with (
                unittest.mock.patch.object(cdr, 'apply_issues', return_value=['closed u']) as ap,
                redirect_stdout(out),
            ):
                self.assertEqual(cdr.main(['issues', '--repo', 'subflux', '--plan', str(plan)]), 0)
        self.assertEqual(ap.call_args.args[1:], ('subflux', [{'kind': 'close', 'number': 5}]))
        self.assertEqual(out.getvalue(), 'closed u\n')

    def test_an_unreadable_plan_is_refused(self):
        for raw in (None, '{', '{}'):
            with self.subTest(repr(raw)), tempfile.TemporaryDirectory() as tmp:
                plan = Path(tmp) / 'plan.json'
                if raw is not None:
                    plan.write_text(raw)
                err = io.StringIO()
                with (
                    unittest.mock.patch.object(cdr, 'apply_issues', side_effect=AssertionError),
                    redirect_stderr(err),
                ):
                    argv = ['issues', '--repo', 'subflux', '--plan', str(plan)]
                    self.assertEqual(cdr.main(argv), 1)
                self.assertIn('the issue plan', err.getvalue())


FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
method = args[args.index('-X') + 1] if '-X' in args else 'GET'
path = args[args.index('--input') - 1] if '--input' in args else args[-1]
body = json.loads(sys.stdin.read()) if '--input' in args else None
with open(os.environ['FAKE_GH_LOG'], 'a') as fh:
    fh.write(json.dumps([method, path, body]) + '\n')
routes = json.loads(open(os.environ['FAKE_GH_ROUTES']).read())
found = f'{method} {path}' in routes
out = routes.get(f'{method} {path}', {'message': 'Not Found'})
sys.stdout.write(f'HTTP/2.0 {200 if found else 404} R\n\r\n' + json.dumps(out))
sys.exit(0 if found else 1)
"""
PERMISSIONS = ('actions', 'contents', 'issues', 'pull-requests')
FAKE_TOKEN = {
    'inputs': {
        k: {}
        for k in (
            'client-id',
            'private-key',
            'owner',
            'repositories',
            *(f'permission-{p}' for p in PERMISSIONS),
        )
    },
    'outputs': {'token': {'value': 't'}, 'app-slug': {'value': 'tribble-trouble'}},
    'runs': {
        'using': 'composite',
        'steps': [
            {
                'shell': 'bash',
                'run': 'echo "mint repositories=${{ inputs.repositories }}'
                + ''.join(f' {p}=${{{{ inputs.permission-{p} }}}}' for p in PERMISSIONS)
                + '" >> "$FAKE_GH_LOG"\n',
            }
        ],
    },
}
FAKE_DOWNLOAD = {
    'inputs': {k: {} for k in ('name', 'path', 'github-token', 'repository', 'run-id')},
    'runs': {
        'using': 'composite',
        'steps': [
            {
                'shell': 'bash',
                'run': 'mkdir -p "${{ inputs.path }}"\n'
                'cp -R "$FAKE_PROPOSAL/." "${{ inputs.path }}/"\n',
            }
        ],
    },
}
APP_MINT = {
    'contents': 'write',
    'issues': 'write',
    'pull-requests': 'write',
    'actions': '',
}
READ_MINT = {'actions': 'read', 'issues': 'read', 'contents': '', 'pull-requests': ''}


class WorkflowRun(unittest.TestCase):
    """The propose job replayed end to end on this workflow: gh answers from a
    route table, and the token and download actions are local stand-ins that
    record what they were asked for."""

    def replay(self, repo, routes, proposal):
        """(exit code, replay result, gh calls, token mints) of one run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cwd, bin_ = tmp / 'cwd', tmp / 'bin'
            cwd.mkdir()
            bin_.mkdir()
            (cwd / 'scripts').symlink_to(Path(__file__).resolve().parent)
            (bin_ / 'gh').write_text(FAKE_GH)
            (bin_ / 'gh').chmod(0o755)
            for name, spec in (('token', FAKE_TOKEN), ('download', FAKE_DOWNLOAD)):
                (cwd / 'fake' / name).mkdir(parents=True)
                (cwd / 'fake' / name / 'action.yml').write_text(yaml.safe_dump(spec))
            doc = yaml.safe_load(WORKFLOW.read_text())
            for step in doc['jobs']['propose']['steps']:
                uses = step.get('uses', '')
                if uses.startswith('actions/create-github-app-token@'):
                    step['uses'] = './fake/token'
                elif uses.startswith('actions/download-artifact@'):
                    step['uses'] = './fake/download'
            (tmp / 'wf.yaml').write_text(yaml.safe_dump(doc))
            proposal(tmp / 'proposal')
            (tmp / 'routes.json').write_text(json.dumps(routes))
            (tmp / 'runner').mkdir()
            context = {
                'matrix': {'repo': repo},
                'secrets': {'SYNC_APP_ID': 'id', 'SYNC_APP_PRIVATE_KEY': 'key'},
                'needs': {'plan': {'result': 'success', 'outputs': {}}},
                'runner': {'temp': str(tmp / 'runner')},
            }
            (tmp / 'ctx.json').write_text(json.dumps(context))
            log = tmp / 'log'
            log.touch()
            env = {
                'PATH': f'{bin_}:{os.environ["PATH"]}',
                'FAKE_GH_LOG': str(log),
                'FAKE_GH_ROUTES': str(tmp / 'routes.json'),
                'FAKE_PROPOSAL': str(tmp / 'proposal'),
            }
            argv = ['--workflow', str(tmp / 'wf.yaml'), '--job', 'propose']
            argv += ['--context', str(tmp / 'ctx.json'), '--cwd', str(cwd)]
            argv += ['--out', str(tmp / 'out.json')]
            with unittest.mock.patch.dict(os.environ, env), redirect_stderr(io.StringIO()):
                rc = workflow_replay.main(argv)
            result = json.loads((tmp / 'out.json').read_text())
            calls, mints = [], []
            for line in log.read_text().splitlines():
                if line.startswith('mint '):
                    mints.append(dict(kv.split('=', 1) for kv in line[5:].split(' ')))
                else:
                    calls.append(tuple(json.loads(line)))
        return rc, result, calls, mints

    def subflux_routes(self, issues=(), runs=None):
        runs_path = RUNS.replace(API, SAPI)
        run = a_run(41, head_repository={'full_name': 'cplieger/subflux'})
        routes = {
            f'GET {runs_path}': {'workflow_runs': runs or [run]},
            f'GET {ISSUES}&page=1': list(issues),
            f'POST {SAPI}/issues': {'html_url': issue_url(20)},
            f'POST {SAPI}/labels': {},
        }
        for label in (cdr.ISSUE_LABEL, *SUBFLUX['labels']):
            routes[f'GET {SAPI}/labels/{label}'] = {'name': label}
        for n in range(1, 60):
            routes[f'PATCH {SAPI}/issues/{n}'] = {'html_url': issue_url(n)}
        return routes

    @staticmethod
    def notices_proposal(entries):
        return lambda root: write_proposal(root, [], notices=entries)

    @staticmethod
    def writes(calls):
        return [c for c in calls if c[0] != 'GET']

    def mint(self, permissions, repo='subflux'):
        return {'repositories': repo, **permissions}

    def test_notices_only_mints_issue_token(self):
        gone = an_issue(3, notices_of([a_notice('gone')])[0])
        entries = [a_notice('a'), a_notice('b')]
        a, b = notices_of(entries)
        rc, result, calls, mints = self.replay(
            'subflux', self.subflux_routes([gone]), self.notices_proposal(entries)
        )
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(mints, [self.mint(READ_MINT), self.mint(APP_MINT)])

        def text(n):
            return {'title': n.title, 'body': n.body, 'labels': [cdr.ISSUE_LABEL, *n.labels]}

        self.assertEqual(
            self.writes(calls),
            [
                ('POST', f'{SAPI}/issues', text(a)),
                ('POST', f'{SAPI}/issues', text(b)),
                ('PATCH', f'{SAPI}/issues/3', {'state': 'closed', 'state_reason': 'completed'}),
            ],
        )
        self.assertFalse([c for c in calls if '/pulls' in c[1] or '/git/' in c[1]])
        self.assertIn("Propose cplieger/${{ matrix.repo }}'s refresh (if false)", result['ran'])

    def test_no_change_mints_no_token(self):
        (a,) = notices_of([a_notice('a')])
        rc, result, calls, mints = self.replay(
            'subflux', self.subflux_routes([an_issue(3, a)]), self.notices_proposal([a_notice('a')])
        )
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(mints, [self.mint(READ_MINT)])
        self.assertEqual(self.writes(calls), [])
        for step in ('Mint the App token', "Write the notices' issues"):
            self.assertIn(f'{step} (if false)', result['ran'])

    def test_failed_refresh_closes_nothing(self):
        (a,) = notices_of([a_notice('a')])
        open_issues = [an_issue(3, a), an_issue(4, notices_of([a_notice('b')])[0])]
        failed = a_run(42, conclusion='failure', head_repository={'full_name': 'cplieger/subflux'})
        older = a_run(41, head_repository={'full_name': 'cplieger/subflux'})
        for name, routes, proposal, step in (
            (
                'a failed newest run',
                self.subflux_routes(open_issues, runs=[failed, older]),
                self.notices_proposal([]),
                'Find the refresh run',
            ),
            (
                'a proposal without notices.json',
                self.subflux_routes(open_issues),
                lambda root: write_proposal(root, []),
                'Check the proposal',
            ),
        ):
            with self.subTest(name):
                rc, result, calls, mints = self.replay('subflux', routes, proposal)
                self.assertEqual(rc, 1)
                self.assertIn(f"step '{step}' exited 1", result['failed'])
                self.assertEqual(mints, [self.mint(READ_MINT)])
                self.assertEqual(self.writes(calls), [])
                self.assertFalse([c for c in calls if '/issues' in c[1]])

    def test_a_fixture_run_opens_edits_reopens_and_closes_and_caps_at_10(self):
        entries = [a_notice(f'n{i:02}') for i in range(13)]
        entries[2] = a_notice('n02', upstream='sonarr', **{'from': FROM, 'to': TO})
        ns = notices_of(entries)
        issues = [
            an_issue(1, ns[0], title='old'),
            an_issue(2, ns[1], state='closed'),
            an_issue(3, notices_of([a_notice('gone')])[0]),
        ]
        rc, result, calls, _ = self.replay(
            'subflux', self.subflux_routes(issues), self.notices_proposal(entries)
        )
        self.assertEqual(rc, 0, result.get('failed'))
        writes = self.writes(calls)
        self.assertEqual(
            [(m, p) for m, p, _ in writes],
            [
                ('PATCH', f'{SAPI}/issues/1'),
                ('PATCH', f'{SAPI}/issues/2'),
                *[('POST', f'{SAPI}/issues')] * 10,
                ('PATCH', f'{SAPI}/issues/3'),
            ],
        )
        self.assertEqual(writes[0][2]['title'], ns[0].title)
        self.assertEqual(writes[1][2]['state'], 'open')
        self.assertEqual([w[2]['title'] for w in writes[2:11]], [n.title for n in ns[2:11]])
        linked = [w[2]['body'] for w in writes if w[2] and '://' in w[2].get('body', '')]
        self.assertEqual(linked, [ns[2].body])
        self.assertEqual(re.findall(r'\S+://\S+', ns[2].body), [SONARR_COMPARE])
        bulk = writes[11][2]
        self.assertEqual(bulk['title'], cdr.BULK_TITLE)
        self.assertIn(ns[11].title, bulk['body'])
        self.assertIn(ns[12].title, bulk['body'])
        self.assertEqual(writes[12][2], {'state': 'closed', 'state_reason': 'completed'})

    def test_files_only_opens_the_pull_request_and_writes_no_issue(self):
        routes = {f'{m} {p}': v for (m, p), v in base_routes().items()}
        routes[f'GET {RUNS}'] = {'workflow_runs': [a_run(41)]}

        def proposal(root):
            write_proposal(root, ['scripttable.go'], {'scripttable.go': b'new\n'})

        rc, result, calls, mints = self.replay('langtag', routes, proposal)
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(mints, [self.mint(READ_MINT, 'langtag'), self.mint(APP_MINT, 'langtag')])
        self.assertEqual(self.writes(calls)[-1][:2], ('POST', f'{API}/pulls'))
        self.assertFalse([c for c in calls if '/issues' in c[1] or '/labels' in c[1]])
        self.assertIn("Write the notices' issues (if false)", result['ran'])


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
SETUP_PYTHON = {
    'name': 'Setup Python',
    'uses': 'actions/setup-python@SHA',
    'with': {'python-version': '3.15'},
}
JOBS = {
    'plan': {
        'runs-on': 'ubuntu-26.04',
        'timeout-minutes': 5,
        'outputs': {'matrix': '${{ steps.plan.outputs.matrix }}'},
        'steps': [
            CHECKOUT,
            SETUP_PYTHON,
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
            SETUP_PYTHON,
            {
                'name': 'Mint the read token',
                'id': 'read-token',
                'uses': 'actions/create-github-app-token@SHA',
                'with': {**APP_KEY, 'permission-actions': 'read', 'permission-issues': 'read'},
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
                'env': {
                    'GH_TOKEN': '${{ steps.read-token.outputs.token }}',
                    'REPO': '${{ matrix.repo }}',
                    'PROPOSAL': '${{ runner.temp }}/proposal',
                    'PLAN': '${{ runner.temp }}/issues.json',
                },
                'run': 'set -euo pipefail\n'
                'python3 scripts/community_data_refresh.py check --repo "$REPO"'
                ' --proposal "$PROPOSAL" --plan "$PLAN"\n',
            },
            {
                'name': 'Mint the App token',
                'if': "steps.check.outputs.changed == 'true'"
                " || steps.check.outputs.notices_changed == 'true'",
                'id': 'app-token',
                'uses': 'actions/create-github-app-token@SHA',
                'with': {
                    **APP_KEY,
                    'permission-contents': 'write',
                    'permission-issues': 'write',
                    'permission-pull-requests': 'write',
                },
            },
            {
                'name': "Write the notices' issues",
                'if': "steps.check.outputs.notices_changed == 'true'",
                'env': {
                    'GH_TOKEN': '${{ steps.app-token.outputs.token }}',
                    'REPO': '${{ matrix.repo }}',
                    'PLAN': '${{ runner.temp }}/issues.json',
                },
                'run': 'set -euo pipefail\n'
                'python3 scripts/community_data_refresh.py issues --repo "$REPO" --plan "$PLAN"\n',
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
    code, the read token can only read its runs and issues, and the write token
    is minted only for a checked change and reaches only the issue and propose
    steps. Changing any key or step changes TOP or JOBS."""

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
