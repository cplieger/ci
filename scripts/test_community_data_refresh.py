"""community_data_refresh.py: the plan, the proposal a refresh packages in a real
git checkout, its validation, and the pull request a proposal makes through a
fake `gh api -i`."""

from __future__ import annotations

import base64
import io
import json
import os
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

GIT_ENV = {
    'GIT_AUTHOR_NAME': 't',
    'GIT_AUTHOR_EMAIL': 't@example.invalid',
    'GIT_COMMITTER_NAME': 't',
    'GIT_COMMITTER_EMAIL': 't@example.invalid',
    'GIT_CONFIG_GLOBAL': '/dev/null',
    'GIT_CONFIG_NOSYSTEM': '1',
}
BASE = 'b' * 40
WORKFLOW = Path(__file__).resolve().parent.parent / '.github/workflows/community-data-refresh.yaml'
BOT = 'tribble-trouble[bot]'
API = 'repos/cplieger/langtag'


class Plan(unittest.TestCase):
    def test_an_empty_only_covers_every_repository(self):
        self.assertEqual(cdr.plan('', ''), [{'repo': n, **row} for n, row in cdr.REPOS.items()])

    def test_only_selects_named_repositories_once(self):
        self.assertEqual([r['repo'] for r in cdr.plan('langtag, langtag', '')], ['langtag'])

    def test_args_go_with_exactly_one_repository(self):
        self.assertEqual(cdr.plan('langtag', '-release release-48')[0]['repo'], 'langtag')
        with self.assertRaisesRegex(cdr.RefusedError, "args are one repository's"):
            cdr.plan('', '-release release-48')
        with (
            unittest.mock.patch.dict(cdr.REPOS, {'other': {'branch': 'dev', 'command': 'x'}}),
            self.assertRaisesRegex(cdr.RefusedError, "args are one repository's"),
        ):
            cdr.plan('langtag other', '-release release-48')

    def test_refusals(self):
        for only, args, want in (
            ('nope', '', 'nope not refreshed. The repositories are langtag'),
            ('langtag', '-a\n-b', 'one line'),
            ('langtag', '-a\r', 'one line'),
        ):
            with self.subTest(only=only, args=args), self.assertRaisesRegex(cdr.RefusedError, want):
                cdr.plan(only, args)

    def test_an_empty_list_fails_rather_than_covering_nothing(self):
        with (
            unittest.mock.patch.dict(cdr.REPOS, clear=True),
            self.assertRaisesRegex(cdr.RefusedError, 'lists no repository'),
        ):
            cdr.plan('', '')

    def test_the_command_writes_the_matrix_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            with (
                unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(out)}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cdr.main(['plan', '--only', 'langtag']), 0)
            key, _, value = out.read_text().strip().partition('=')
            self.assertEqual((key, json.loads(value)), ('matrix', cdr.plan('langtag', '')))

    def test_a_refusal_exits_1_with_an_error_annotation(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cdr.main(['plan', '--only', 'nope']), 1)
        self.assertTrue(err.getvalue().startswith('::error::nope not refreshed'))


class Package(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.tree, self.text = self.tmp / 'tree', self.tmp / 'text'
        self.tree.mkdir()
        self.text.mkdir()
        self.git('init', '-q')
        (self.tree / 'kept.txt').write_text('kept\n')
        (self.tree / 'table.go').write_text('old\n')
        (self.tree / 'gone.txt').write_text('gone\n')
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.run(
            ['git', '-C', str(self.tree), *args],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **GIT_ENV},
        ).stdout

    def write_text(self):
        (self.text / 'title').write_text('feat: update the data\n')
        (self.text / 'body.md').write_text('Body.\n')

    def test_every_change_is_packaged_with_the_text(self):
        (self.tree / 'table.go').write_text('new\n')
        (self.tree / 'gone.txt').unlink()
        (self.tree / 'sub').mkdir()
        (self.tree / 'sub' / 'run.sh').write_text('#!/bin/sh\n')
        (self.tree / 'sub' / 'run.sh').chmod(0o755)
        self.write_text()
        out = self.tmp / 'proposal'
        manifest = cdr.package(self.tree, self.text, out)
        self.assertEqual(manifest['base'], self.base)
        self.assertEqual(
            manifest['changes'],
            [
                {'path': 'gone.txt', 'deleted': True},
                {'path': 'sub/run.sh', 'mode': '100755'},
                {'path': 'table.go', 'mode': '100644'},
            ],
        )
        self.assertEqual((out / 'files' / 'table.go').read_text(), 'new\n')
        self.assertEqual((out / 'files' / 'sub' / 'run.sh').read_text(), '#!/bin/sh\n')
        self.assertFalse((out / 'files' / 'kept.txt').exists())
        self.assertEqual(json.loads((out / cdr.MANIFEST).read_text()), manifest)
        p = cdr.Proposal(out)
        self.assertEqual((p.title, p.body), ('feat: update the data', 'Body.\n'))

    def test_an_unchanged_tree_needs_no_text(self):
        out = self.tmp / 'proposal'
        self.assertEqual(cdr.package(self.tree, self.text, out), {'base': self.base, 'changes': []})
        self.assertEqual(cdr.Proposal(out).changes, [])

    def test_a_change_without_its_text_is_refused(self):
        (self.tree / 'table.go').write_text('new\n')
        with self.assertRaisesRegex(cdr.RefusedError, 'wrote no title'):
            cdr.package(self.tree, self.text, self.tmp / 'proposal')

    def test_a_symlink_is_refused(self):
        (self.tree / 'link').symlink_to('kept.txt')
        self.write_text()
        with self.assertRaisesRegex(cdr.RefusedError, 'link has mode 120000'):
            cdr.package(self.tree, self.text, self.tmp / 'proposal')


def write_proposal(root: Path, changes, files=None, title='feat: x\n', body='Body.\n', base=BASE):
    (root / cdr.FILES).mkdir(parents=True, exist_ok=True)
    for path, data in (files or {}).items():
        (root / cdr.FILES / path).parent.mkdir(parents=True, exist_ok=True)
        (root / cdr.FILES / path).write_bytes(data)
    (root / cdr.MANIFEST).write_text(json.dumps({'base': base, 'changes': changes}))
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
            [{'path': 'a/b.go', 'mode': '100644'}, {'path': 'c', 'deleted': True}],
            {'a/b.go': b'x'},
        )
        p = cdr.Proposal(self.root)
        self.assertEqual((p.base, p.title, p.body, len(p.changes)), (BASE, 'feat: x', 'Body.\n', 2))

    def test_unsafe_paths_are_refused(self):
        for path in (
            '',
            '../x',
            '/etc/passwd',
            '.git/config',
            'a/../b',
            'a/./b',
            'a//b',
            'a/',
            'a\\b',
            7,
        ):
            with self.subTest(path=path):
                self.assertFalse(cdr.safe_path(path))
        self.assertTrue(cdr.safe_path('names/testdata/names.golden'))
        self.assertTrue(cdr.safe_path('.github/x'))

    def test_refusals(self):
        for name, kw, want in (
            ('base', {'base': 'HEAD'}, "base 'HEAD' is not a commit SHA"),
            ('path', {'changes': [{'path': '../x', 'mode': '100644'}]}, 'names no path'),
            (
                'twice',
                {'changes': [{'path': 'a', 'mode': '100644'}, {'path': 'a', 'deleted': True}]},
                'a is changed twice',
            ),
            ('mode', {'changes': [{'path': 'a', 'mode': '120000'}]}, 'want a path and a mode'),
            (
                'extra',
                {'changes': [{'path': 'a', 'deleted': True, 'mode': '100644'}]},
                'want a path',
            ),
            ('missing', {'changes': [{'path': 'b', 'mode': '100644'}]}, 'b is changed but not in'),
            ('no title', {'title': None}, 'title is unreadable'),
            ('blank title', {'title': ' \n'}, 'one non-empty line'),
            ('two lines', {'title': 'a\nb\n'}, 'one non-empty line'),
            ('long title', {'title': 'x' * 257}, 'over 256'),
            ('long body', {'body': 'x' * 65537}, 'over 65536'),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                args = {'changes': [{'path': 'a', 'mode': '100644'}], 'files': {'a': b'x'}, **kw}
                write_proposal(Path(tmp), **args)
                with self.assertRaisesRegex(cdr.RefusedError, want):
                    cdr.Proposal(Path(tmp))

    def test_an_unreadable_manifest_is_refused(self):
        with self.assertRaisesRegex(cdr.RefusedError, 'proposal.json is unreadable'):
            cdr.Proposal(self.root)
        (self.root / cdr.MANIFEST).write_text('[]')
        with self.assertRaisesRegex(cdr.RefusedError, 'holds no change list'):
            cdr.Proposal(self.root)

    def test_the_command_outputs_whether_it_changes_anything(self):
        for changes, files, want in (
            ([], {}, 'changed=false'),
            ([{'path': 'a', 'mode': '100644'}], {'a': b'x'}, 'changed=true'),
        ):
            with self.subTest(want), tempfile.TemporaryDirectory() as tmp:
                write_proposal(Path(tmp) / 'p', changes, files)
                out = Path(tmp) / 'out'
                with (
                    unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(out)}),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(cdr.main(['check', '--proposal', str(Path(tmp) / 'p')]), 0)
                self.assertEqual(out.read_text(), want + '\n')


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
        ('GET', f'{API}/git/ref/heads/dev'): {'object': {'sha': BASE}},
        ('GET', f'{API}/git/commits/{BASE}'): {'tree': {'sha': 'basetree'}},
        ('POST', f'{API}/git/blobs'): {'sha': 'blob1'},
        ('POST', f'{API}/git/trees'): {'sha': 'newtree'},
        ('POST', f'{API}/git/commits'): {'sha': 'c' * 40},
        ('POST', f'{API}/git/refs'): {},
        ('PATCH', f'{API}/git/refs/heads/{cdr.BRANCH}'): {},
        ('POST', f'{API}/pulls'): {'html_url': 'https://github.com/cplieger/langtag/pull/9'},
        ('PATCH', f'{API}/pulls/7'): {},
    }
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
            [{'path': 'table.go', 'mode': '100644'}, {'path': 'gone.txt', 'deleted': True}],
            {'table.go': b'new\n'},
        )
        self.proposal = cdr.Proposal(Path(tmp.name))

    def run_propose(self, routes):
        fake = FakeGitHub(routes)
        gh = ghrest.Client(run=fake, sleep=lambda _: None)
        return cdr.propose(gh, 'langtag', 'dev', BOT, self.proposal), fake.writes

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
        content = base64.b64encode(b'new\n').decode()
        self.assertEqual(
            writes,
            [
                ('POST', f'{API}/git/blobs', {'content': content, 'encoding': 'base64'}),
                (
                    'POST',
                    f'{API}/git/trees',
                    {
                        'base_tree': 'basetree',
                        'tree': [
                            {'path': 'table.go', 'mode': '100644', 'type': 'blob', 'sha': 'blob1'},
                            {'path': 'gone.txt', 'mode': '100644', 'type': 'blob', 'sha': None},
                        ],
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
            [w[:2] for w in writes], [('POST', f'{API}/git/blobs'), ('POST', f'{API}/git/trees')]
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

    def test_a_proposal_from_a_base_that_moved_writes_nothing(self):
        routes = self.with_pr(open_pr(head_sha='d' * 40), [{'author': {'login': BOT}}])
        routes['GET', f'{API}/git/ref/heads/dev'] = {'object': {'sha': 'e' * 40}}
        fake = FakeGitHub(routes)
        gh = ghrest.Client(run=fake, sleep=lambda _: None)
        with self.assertRaisesRegex(cdr.RefusedError, f'dev moved from {BASE} to {"e" * 40}'):
            cdr.propose(gh, 'langtag', 'dev', BOT, self.proposal)
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
                argv = ['propose', '--repo', 'langtag', '--proposal', str(self.proposal.root)]
                self.assertEqual(cdr.main([*argv, '--bot', BOT]), 0)
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
                argv = ['propose', '--repo', 'langtag', '--proposal', tmp, '--bot', BOT]
                self.assertEqual(cdr.main(argv), 0)
            self.assertEqual(out.getvalue(), 'no change\n')

    def test_the_command_refuses_an_unlisted_repository(self):
        with redirect_stderr(io.StringIO()):
            argv = ['propose', '--repo', 'nope', '--proposal', '/nonexistent', '--bot', BOT]
            self.assertEqual(cdr.main(argv), 1)


class Workflow(unittest.TestCase):
    """The plan and refresh jobs replayed, the refresh with a stand-in command
    in place of the repository's."""

    # The shape of a REPOS command: one program, whose arguments the job extends.
    COMMAND = 'bash ../stand-in.sh -out "$REFRESH_OUT"'
    STAND_IN = (
        'printf "%s\\n" "$@" > ../args\n'
        'echo new > table.go\n'
        'echo "feat: t" > "$2/title"\n'
        'echo b > "$2/body.md"\n'
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cwd = self.tmp / 'cwd'
        target = self.cwd / 'target'
        target.mkdir(parents=True)
        (self.cwd / 'scripts').symlink_to(Path(__file__).resolve().parent)
        (self.cwd / 'stand-in.sh').write_text(self.STAND_IN)
        env = {**os.environ, **GIT_ENV}
        (target / 'table.go').write_text('old\n')
        for args in (['init', '-q'], ['add', '.'], ['commit', '-q', '-m', 'base']):
            subprocess.run(['git', '-C', str(target), *args], check=True, env=env)

    def replay(self, job, context, *extra):
        ctx, out = self.tmp / 'ctx.json', self.tmp / 'out.json'
        ctx.write_text(json.dumps(context))
        argv = ['--workflow', str(WORKFLOW), '--job', job, '--context', str(ctx)]
        argv += ['--cwd', str(self.cwd), '--out', str(out), *extra]
        with redirect_stderr(io.StringIO()):
            rc = workflow_replay.main(argv)
        return rc, json.loads(out.read_text())

    def refresh(self, args, command=COMMAND):
        context = {
            'inputs': {'args': args},
            'matrix': {'repo': 'langtag', 'branch': 'dev', 'command': command},
            'github': {'token': 'test-token'},
            'needs': {'plan': {'result': 'success', 'outputs': {}}},
        }
        skips = ['Checkout cplieger/${{ matrix.repo }}', 'Setup Go', 'Upload the proposal']
        return self.replay('refresh', context, *(f'--skip={s}' for s in skips))

    def test_plan_outputs_the_matrix(self):
        rc, result = self.replay(
            'plan', {'inputs': {'only': 'langtag', 'args': '-release release-48'}}
        )
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(json.loads(result['outputs']['matrix']), cdr.plan('langtag', ''))

    def test_the_command_gets_its_directory_and_the_arguments(self):
        rc, result = self.refresh('-release release-48  -x')
        self.assertEqual(rc, 0, result.get('failed'))
        temp = Path(result['runner_temp'])
        self.assertEqual(
            (self.cwd / 'args').read_text().splitlines(),
            ['-out', str(temp / 'refresh-out'), '-release', 'release-48', '-x'],
        )
        p = cdr.Proposal(temp / 'proposal')
        self.assertEqual(
            (p.changes, p.title), ([{'path': 'table.go', 'mode': '100644'}], 'feat: t')
        )

    def test_no_arguments_reach_the_command_as_none(self):
        rc, result = self.refresh('')
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(
            (self.cwd / 'args').read_text().splitlines(),
            ['-out', str(Path(result['runner_temp']) / 'refresh-out')],
        )

    def test_a_failed_command_fails_the_job_before_packaging(self):
        rc, result = self.refresh('', command='exit 3')
        self.assertEqual(rc, 1)
        self.assertIn("step 'Run the refresh command' exited 3", result['failed'])
        self.assertFalse((Path(result['runner_temp']) / 'proposal').exists())


class WorkflowShape(unittest.TestCase):
    """The token boundary: the repository's code runs in refresh with only the
    job token, and the App token exists only in propose, for that repository."""

    APP_TOKEN = 'actions/create-github-app-token@'

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        cls.jobs = yaml.safe_load(cls.text)['jobs']

    def step(self, job, name):
        return next(s for s in self.jobs[job]['steps'] if s.get('name') == name)

    def test_refresh_runs_the_command_with_only_the_job_token(self):
        refresh = self.jobs['refresh']
        self.assertEqual(refresh['permissions'], {'contents': 'read'})
        self.assertNotIn('secrets.', yaml.safe_dump(refresh))
        self.assertFalse([s for s in refresh['steps'] if self.APP_TOKEN in s.get('uses', '')])
        env = self.step('refresh', 'Run the refresh command')['env']
        self.assertEqual(env['GITHUB_TOKEN'], '${{ github.token }}')

    def test_only_propose_mints_the_app_token_for_the_one_repository(self):
        minting = {
            job: [s for s in spec.get('steps', []) if self.APP_TOKEN in s.get('uses', '')]
            for job, spec in self.jobs.items()
        }
        self.assertEqual({job for job, steps in minting.items() if steps}, {'propose'})
        (mint,) = minting['propose']
        self.assertEqual(
            {k: v for k, v in mint['with'].items() if k not in ('client-id', 'private-key')},
            {
                'owner': 'cplieger',
                'repositories': '${{ matrix.repo }}',
                'permission-contents': 'write',
                'permission-pull-requests': 'write',
            },
        )
        self.assertEqual(self.jobs['propose']['permissions'], {'contents': 'read'})

    def test_propose_runs_no_target_code_and_passes_the_token_once(self):
        steps = self.jobs['propose']['steps']
        self.assertFalse([s for s in steps if 'repository' in s.get('with', {})])
        self.assertNotIn('matrix.command', yaml.safe_dump(self.jobs['propose']))
        for s in steps:
            if 'run' in s:
                self.assertIn('python3 scripts/community_data_refresh.py', s['run'], s['name'])
        holders = [s['name'] for s in steps if 'steps.app-token.outputs.token' in yaml.safe_dump(s)]
        self.assertEqual(holders, ["Propose cplieger/${{ matrix.repo }}'s refresh"])
        self.assertEqual(self.text.count('steps.app-token.outputs.token'), 1)

    def test_the_proposal_artifact_keeps_hidden_files(self):
        upload = self.step('refresh', 'Upload the proposal')
        self.assertTrue(upload['with']['include-hidden-files'] is True)


if __name__ == '__main__':
    unittest.main()
