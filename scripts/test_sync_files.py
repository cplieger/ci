"""sync-files.py and sync.yaml's auto-merge sweep: one target per base, driven
by stub `git` and `gh` binaries that log every call."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
TESTDATA = SCRIPTS / 'testdata' / 'sync-files'

GIT_STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, re, sys
fx = json.load(open(os.environ['STUB_FIXTURE']))
args = sys.argv[1:]
while args[:1] == ['-c']:
    args = args[2:]
def norm(text):
    return re.sub(r'.*/sync-[^/]+', '<tmp>', text).replace(os.environ['TMPDIR'], '<root>')
with open(os.environ['GIT_LOG'], 'a') as log:
    log.write(json.dumps([norm(os.getcwd()), [norm(a) for a in sys.argv[1:]]]) + '\n')
if args[:1] == ['-C']:
    os.execv(os.environ['REAL_GIT'], ['git', *sys.argv[1:]])
if args[0] == 'clone':
    rest = args[1:]
    base = rest[rest.index('--branch') + 1] if '--branch' in rest else 'default'
    url, dest = rest[-2], pathlib.Path(rest[-1])
    dest.mkdir(parents=True)
    repo = re.fullmatch(r'https://github.com/(.+)\.git', url)[1]
    (dest / '.stub.json').write_text(json.dumps({'repo': repo, 'base': base}))
    for name, text in fx.get('workflows', {}).get(repo, {}).items():
        (dest / '.github' / 'workflows').mkdir(parents=True, exist_ok=True)
        (dest / '.github' / 'workflows' / name).write_text(text)
elif args[:2] == ['add', '--']:
    meta = json.loads(pathlib.Path('.stub.json').read_text())
    with open(os.environ['ADD_LOG'], 'a') as log:
        for path in args[2:]:
            entry = [meta['repo'], path, pathlib.Path(path).read_text(), os.access(path, os.X_OK)]
            log.write(json.dumps(entry) + '\n')
elif args[:3] == ['diff', '--cached', '--name-only']:
    meta = json.loads(pathlib.Path('.stub.json').read_text())
    print('\n'.join(fx['diff'].get(f"{meta['repo']}@{meta['base']}", [])))
elif args == ['rev-parse', '--abbrev-ref', 'HEAD']:
    meta = json.loads(pathlib.Path('.stub.json').read_text())
    print(fx.get('default_branch', {}).get(meta['repo'], 'main'))
"""

GH_STUB = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, urllib.parse
fx = json.load(open(os.environ['STUB_FIXTURE']))
args = sys.argv[1:]
stdin = sys.stdin.read() if '--input' in args else ''
with open(os.environ['GH_LOG'], 'a') as log:
    log.write(json.dumps(args + ([stdin] if stdin else [])) + '\n')
def reply(status, body=None):
    text = '' if body is None else json.dumps(body)
    if include:
        sys.stdout.write(f'HTTP/2.0 {status} X\nContent-Type: application/json\r\n\r\n{text}')
    elif status < 300 and '--jq' in args:
        jq = args[args.index('--jq') + 1]
        sys.stdout.write(subprocess.run(['jq', '-r', jq], input=text, capture_output=True,
                                        text=True, check=True).stdout)
    elif status < 300:
        sys.stdout.write(text + '\n')
    else:
        sys.stderr.write(f"gh: {(body or {}).get('message', '')} (HTTP {status})\n")
    sys.exit(0 if status < 300 else 1)
if args[:2] == ['pr', 'merge']:
    sys.exit(1 if fx.get('merge_fails', {}).get('auto' if '--auto' in args else 'direct') else 0)
if args[0] != 'api':
    sys.exit(f'gh stub: unexpected {args}')
method, path, include, i = 'GET', None, False, 1
while i < len(args):
    if args[i] in ('-X', '-f', '-F', '-H', '--input', '--jq', '-q'):
        method = args[i + 1] if args[i] == '-X' else method
        i += 2
    elif args[i] == '-i':
        include, i = True, i + 1
    else:
        path, i = args[i], i + 1
where, _, query = path.partition('?')
q = dict(urllib.parse.parse_qsl(query))
parts = where.split('/')
repo = '/'.join(parts[1:3])
if f'{method} {where}' in fx.get('fail', {}):
    reply(fx['fail'][f'{method} {where}'], {'message': 'Resource not accessible by integration'})
if (method, where) == ('GET', 'user/repos'):
    rows = [{'name': n, 'fork': n in fx.get('forks', [])} for n in fx['repos']]
    reply(200, rows if q.get('page') == '1' else [])
if method == 'GET' and parts[3:] == ['pulls'] and query:
    rows = [r for r in fx.get('pulls', {}).get(repo, []) if q.get('base', r['base']['ref']) == r['base']['ref']]
    per, page = int(q['per_page']), int(q['page'])
    reply(200, rows[(page - 1) * per : page * per])
if method == 'GET' and path in fx.get('api', {}):
    reply(200, fx['api'][path])
if (method, parts[3:]) == ('GET', ['labels', 'dependencies']):
    reply(404, {'message': 'Not Found'}) if repo in fx.get('unlabelled', []) else reply(200, {})
if (method, parts[3:]) == ('POST', ['pulls']):
    reply(201, {'number': 99, 'html_url': f'https://github.com/{repo}/pull/99'})
if method == 'POST' and parts[3] == 'issues' and parts[5:] in (['labels'], ['comments']):
    reply(200, {})
if method == 'PATCH' and parts[3] == 'pulls' and len(parts) == 5:
    reply(200, {})
if method == 'PUT' and parts[3] == 'pulls' and parts[5:] == ['merge']:
    if fx.get('merge_fails', {}).get('direct'):
        reply(405, {'message': 'Pull Request is not mergeable'})
    reply(200, {'merged': True})
if method == 'DELETE' and parts[3:6] == ['git', 'refs', 'heads']:
    reply(204)
reply(404, {'message': 'Not Found'})
"""


def install_stubs(tmp, fixture):
    real_git = shutil.which('git')
    bin_dir = tmp / 'bin'
    bin_dir.mkdir()
    for name, body in (('git', GIT_STUB), ('gh', GH_STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (tmp / 'fixture.json').write_text(json.dumps(fixture))
    return {
        **os.environ,
        'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        'STUB_FIXTURE': str(tmp / 'fixture.json'),
        'GIT_LOG': str(tmp / 'git.log'),
        'GH_LOG': str(tmp / 'gh.log'),
        'ADD_LOG': str(tmp / 'add.log'),
        'REAL_GIT': real_git,
        'TMPDIR': str(tmp),
    }


def read_log(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_sync(script, manifest, fixture, *extra, source_dir=ROOT):
    """{rc, stdout, stderr, git, gh, added} of one `script` run over a manifest dict;
    `added` is each staged file as [repo, dest, text, executable]."""
    with tempfile.TemporaryDirectory() as name:
        tmp = pathlib.Path(name)
        env = install_stubs(tmp, fixture)
        text = manifest if isinstance(manifest, str) else yaml.safe_dump(manifest, sort_keys=False)
        (tmp / 'sync.yml').write_text(text)
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                '--manifest',
                str(tmp / 'sync.yml'),
                '--source-dir',
                str(source_dir),
                *extra,
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp,
            check=False,
        )
        return {
            'rc': proc.returncode,
            'stdout': proc.stdout,
            'stderr': proc.stderr,
            'git': read_log(tmp / 'git.log'),
            'gh': read_log(tmp / 'gh.log'),
            'added': read_log(tmp / 'add.log'),
        }


def group(repos, files, base=None):
    out = {'repos': ''.join(f'cplieger/{r}\n' for r in repos)}
    if base:
        out['base'] = base
    out['files'] = files
    return out


def listed(repo, number, head, base='main', head_repo=None):
    """One row of the open-pulls listing of cplieger/`repo`."""
    return {
        'number': number,
        'head': {'ref': head, 'repo': {'full_name': head_repo or f'cplieger/{repo}'}},
        'base': {'ref': base},
    }


def api_call(args):
    """(METHOD, path, JSON body or None) of a logged `gh api` argv."""
    method, path, i = 'GET', None, 1
    while i < len(args):
        if args[i] in ('-X', '-f', '-F', '-H', '--input', '--jq', '-q'):
            method = args[i + 1] if args[i] == '-X' else method
            i += 2
        elif args[i] == '-i':
            i += 1
        else:
            body = json.loads(args[-1]) if '--input' in args else None
            return method, args[i], body
    return method, path, None


def api_calls(calls, repo=None):
    """The logged `gh api` calls, those on cplieger/`repo` when it is given."""
    out = [api_call(c) for c in calls if c[:1] == ['api']]
    return [c for c in out if repo is None or c[1].startswith(f'repos/cplieger/{repo}/')]


def lookup(repo, base=None):
    scope = '' if base is None else f'&base={base}'
    return (
        'GET',
        f'repos/cplieger/{repo}/pulls?state=open&sort=created&direction=desc{scope}&per_page=100&page=1',
        None,
    )


FILES = ['.editorconfig', {'source': 'configs/prettier.json', 'dest': '.prettierrc.json'}]
# a: a diff and no PR; b: clean with a stale PR; c: clean; d: a diff and an
# open PR; e: a fork.
MAIN_MANIFEST = {'group': [group(['a', 'b', 'c', 'd', 'e'], FILES)]}
MAIN_FIXTURE = {
    'repos': ['a', 'b', 'c', 'd', 'e'],
    'forks': ['e'],
    'diff': {'cplieger/a@default': ['.editorconfig'], 'cplieger/d@default': ['.prettierrc.json']},
    'pulls': {
        'cplieger/b': [listed('b', 5, 'repo-sync/ci/default')],
        'cplieger/d': [listed('d', 9, 'repo-sync/ci/default')],
    },
}
# HEAD's sync pull request body, byte for byte: a main-default repo sees no change.
HEAD_PR_BODY = (
    'Synced from [cplieger/ci](https://github.com/cplieger/ci) by\n'
    '`scripts/sync-files.py`. Files carrying a `Synced from cplieger/ci` header are\n'
    'overwritten on every sync — change the canonical copy in cplieger/ci\n'
    "instead. Auto-merges once this repo's required checks pass.\n"
    '\n'
    'Files updated in this run:\n'
    '- `.editorconfig`'
)
RENOVATE = {'source': 'configs/renovate-two-branch.json', 'dest': 'renovate.json'}
TWO_BRANCH_MANIFEST = {
    'group': [
        group(['a'], FILES),
        group(['edge'], ['.editorconfig', RENOVATE], base='dev'),
        group(['edge'], ['.editorconfig', RENOVATE], base='main'),
    ]
}
TWO_BRANCH_FIXTURE = {
    'repos': ['a', 'edge'],
    'diff': {'cplieger/a@default': ['.editorconfig'], 'cplieger/edge@dev': ['renovate.json']},
    # A stale PR on main, and a default-head PR into dev the base lookups must not see.
    'pulls': {
        'cplieger/edge': [
            listed('edge', 4, 'repo-sync/ci/main', 'main'),
            listed('edge', 3, 'repo-sync/ci/default', 'dev'),
        ]
    },
}


def load_sync():
    spec = importlib.util.spec_from_file_location('sync_files', SCRIPTS / 'sync-files.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MainDefault(unittest.TestCase):
    def test_every_call_and_the_output_are_unchanged(self):
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, MAIN_FIXTURE)
        golden = json.loads((TESTDATA / 'main-default.json').read_text())
        self.assertEqual(got['rc'], 0, got['stderr'])
        self.assertEqual(
            {k: got[k] for k in ('rc', 'stdout', 'git', 'gh')},
            golden,
        )

    def test_the_pull_request_carries_today_s_branch_title_body_and_label(self):
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, MAIN_FIXTURE)
        writes = [c for c in api_calls(got['gh']) if c[0] != 'GET']
        self.assertEqual(
            writes,
            [
                (
                    'POST',
                    'repos/cplieger/a/pulls',
                    {
                        'title': 'chore(sync): synced file(s) with cplieger/ci',
                        'head': 'repo-sync/ci/default',
                        'base': 'main',
                        'body': HEAD_PR_BODY,
                    },
                ),
                ('POST', 'repos/cplieger/a/issues/99/labels', {'labels': ['dependencies']}),
                (
                    'POST',
                    'repos/cplieger/b/issues/5/comments',
                    {'body': "Closing: the target branch already contains this sync's content."},
                ),
                ('PATCH', 'repos/cplieger/b/pulls/5', {'state': 'closed'}),
                ('DELETE', 'repos/cplieger/b/git/refs/heads/repo-sync/ci/default', None),
            ],
        )
        self.assertIn('  opened PR: https://github.com/cplieger/a/pull/99\n', got['stdout'])

    def test_the_lookups_are_unscoped_and_the_base_is_the_clones_default_branch(self):
        fixture = {**MAIN_FIXTURE, 'default_branch': {'cplieger/a': 'trunk'}}
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        for call in [c[1] for c in got['git']]:
            self.assertNotIn('--branch', call)
        reads = [c for c in api_calls(got['gh']) if '/pulls?' in c[1]]
        self.assertEqual(reads, [lookup(r) for r in 'abcd'])
        (create,) = [c for c in api_calls(got['gh']) if c[:2] == ('POST', 'repos/cplieger/a/pulls')]
        self.assertEqual(create[2]['base'], 'trunk')

    def test_a_failed_lookup_on_a_clean_target_is_a_warning_and_the_target_stays_clean(self):
        fixture = {**MAIN_FIXTURE, 'fail': {'GET repos/cplieger/b/pulls': 403}}
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        self.assertEqual(got['rc'], 0, got['stdout'])
        self.assertIn('::warning::cplieger/b: the open sync PR lookup failed: GET ', got['stdout'])
        self.assertIn('2 already in sync', got['stdout'])
        self.assertIn('· 0 failed\n', got['stdout'])
        self.assertFalse([c for c in api_calls(got['gh'], 'b') if c[0] != 'GET'])

    def test_a_failed_lookup_before_a_pull_request_is_opened_fails_the_target(self):
        fixture = {**MAIN_FIXTURE, 'fail': {'GET repos/cplieger/a/pulls': 403}}
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        self.assertEqual(got['rc'], 1)
        self.assertIn('::warning::cplieger/a: sync failed — GET ', got['stdout'])
        self.assertIn('1 failed (cplieger/a)', got['stdout'])
        self.assertFalse([c for c in api_calls(got['gh'], 'a') if c[0] == 'POST'])

    def test_a_repo_without_the_label_gets_an_unlabelled_pull_request(self):
        fixture = {**MAIN_FIXTURE, 'unlabelled': ['cplieger/a']}
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        self.assertEqual(got['rc'], 0, got['stdout'])
        calls = api_calls(got['gh'], 'a')
        self.assertIn(('POST', 'repos/cplieger/a/pulls'), [c[:2] for c in calls])
        self.assertIn(('GET', 'repos/cplieger/a/labels/dependencies', None), calls)
        self.assertFalse([c for c in calls if 'labels' in c[1] and c[0] != 'GET'])
        self.assertNotIn('::warning::', got['stdout'])

    def test_a_fork_pull_request_with_the_default_head_is_still_matched(self):
        fork = listed('b', 5, 'repo-sync/ci/default', head_repo='someone/b')
        fixture = {**MAIN_FIXTURE, 'pulls': {**MAIN_FIXTURE['pulls'], 'cplieger/b': [fork]}}
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        self.assertEqual(got['rc'], 0, got['stdout'])
        writes = [c[:2] for c in api_calls(got['gh'], 'b') if c[0] != 'GET']
        self.assertEqual(
            writes,
            [('POST', 'repos/cplieger/b/issues/5/comments'), ('PATCH', 'repos/cplieger/b/pulls/5')],
            "a fork's head branch is not this repository's to delete",
        )

    def test_a_failed_close_step_is_a_warning(self):
        fixture = {**MAIN_FIXTURE, 'fail': {'PATCH repos/cplieger/b/pulls/5': 403}}
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        self.assertEqual(got['rc'], 0, got['stdout'])
        self.assertIn('::warning::cplieger/b: stale sync PR #5: close failed: PATCH', got['stdout'])
        self.assertIn(
            ('DELETE', 'repos/cplieger/b/git/refs/heads/repo-sync/ci/default', None),
            api_calls(got['gh'], 'b'),
        )

    def test_an_already_deleted_head_is_no_warning(self):
        fixture = {
            **MAIN_FIXTURE,
            'fail': {'DELETE repos/cplieger/b/git/refs/heads/repo-sync/ci/default': 422},
        }
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        self.assertNotIn('::warning::', got['stdout'])
        fixture['fail'] = {'DELETE repos/cplieger/b/git/refs/heads/repo-sync/ci/default': 403}
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        self.assertIn('branch delete failed', got['stdout'])

    def test_an_unreadable_repo_list_refuses_to_sync(self):
        fixture = {**MAIN_FIXTURE, 'fail': {'GET user/repos': 403}}
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, fixture)
        self.assertEqual(got['rc'], 1)
        self.assertIn('forks cannot be excluded — refusing to sync', got['stderr'])
        self.assertEqual(got['git'], [])


class TwoBranch(unittest.TestCase):
    def setUp(self):
        self.got = run_sync(SCRIPTS / 'sync-files.py', TWO_BRANCH_MANIFEST, TWO_BRANCH_FIXTURE)
        self.assertEqual(self.got['rc'], 0, self.got['stdout'] + self.got['stderr'])

    def git_calls(self, verb):
        return [c[1] for c in self.got['git'] if verb in c[1]]

    def test_each_base_is_cloned_and_branched_on_its_own(self):
        clones = [c for c in self.git_calls('clone') if 'cplieger/edge' in c[-2]]
        self.assertEqual([c[c.index('--branch') + 1] for c in clones], ['dev', 'main'])
        self.assertEqual(
            [c[-1] for c in self.git_calls('checkout')],
            ['repo-sync/ci/default', 'repo-sync/ci/dev', 'repo-sync/ci/main'],
        )
        self.assertEqual(
            [c[-1] for c in self.git_calls('push')],
            ['HEAD:refs/heads/repo-sync/ci/default', 'HEAD:refs/heads/repo-sync/ci/dev'],
        )
        self.assertFalse(
            [c for c in self.got['git'] if 'rev-parse' in c[1] and 'edge' in c[0]],
            'a base target names its base; it reads no default branch',
        )

    def test_every_pr_call_for_a_base_target_carries_its_base(self):
        edge = api_calls(self.got['gh'], 'edge')
        self.assertEqual(
            [c[:2] for c in edge],
            [
                lookup('edge', 'dev')[:2],
                ('POST', 'repos/cplieger/edge/pulls'),
                ('GET', 'repos/cplieger/edge/labels/dependencies'),
                ('POST', 'repos/cplieger/edge/issues/99/labels'),
                lookup('edge', 'main')[:2],
                ('POST', 'repos/cplieger/edge/issues/4/comments'),
                ('PATCH', 'repos/cplieger/edge/pulls/4'),
                ('DELETE', 'repos/cplieger/edge/git/refs/heads/repo-sync/ci/main'),
            ],
        )
        self.assertEqual((edge[1][2]['head'], edge[1][2]['base']), ('repo-sync/ci/dev', 'dev'))

    def test_a_fork_pull_request_with_the_sync_head_is_neither_reused_nor_closed(self):
        fixture = {
            **TWO_BRANCH_FIXTURE,
            'pulls': {
                'cplieger/edge': [
                    listed('edge', 21, 'repo-sync/ci/dev', 'dev', 'someone/edge'),
                    listed('edge', 22, 'repo-sync/ci/main', 'main', 'someone/edge'),
                ]
            },
        }
        got = run_sync(SCRIPTS / 'sync-files.py', TWO_BRANCH_MANIFEST, fixture)
        self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
        edge = [c[:2] for c in api_calls(got['gh'], 'edge') if '/labels' not in c[1]]
        self.assertEqual(
            edge,
            [
                lookup('edge', 'dev')[:2],
                ('POST', 'repos/cplieger/edge/pulls'),
                lookup('edge', 'main')[:2],
            ],
        )

    def test_the_summary_counts_targets(self):
        self.assertIn('::group::cplieger/edge (base: dev)', self.got['stdout'])
        self.assertIn('\n3 target(s): 2 synced · 1 already in sync', self.got['stdout'])

    def test_the_main_head_is_the_one_the_intake_accepts(self):
        import release_channels

        self.assertEqual(load_sync().branch_for('main'), release_channels.MAIN_SYNC_HEAD)
        self.assertEqual(release_channels.main_intake_kind(load_sync().branch_for('main')), 'sync')

    def test_an_unknown_base_writes_nothing(self):
        manifest = {'group': [group(['edge'], FILES, base='feature')]}
        got = run_sync(SCRIPTS / 'sync-files.py', manifest, TWO_BRANCH_FIXTURE)
        self.assertEqual(got['rc'], 1)
        self.assertIn("unknown base 'feature'", got['stderr'])
        self.assertEqual((got['git'], got['gh']), ([], []))

    def test_only_keeps_both_bases_of_a_repo(self):
        got = run_sync(
            SCRIPTS / 'sync-files.py',
            TWO_BRANCH_MANIFEST,
            TWO_BRANCH_FIXTURE,
            '--only',
            'edge',
            '--dry-run',
        )
        self.assertEqual(len([c for c in got['git'] if 'clone' in c[1]]), 2)
        self.assertIn('2 target(s)', got['stdout'])


class Mapping(unittest.TestCase):
    def setUp(self):
        self.sync = load_sync()

    def load(self, manifest):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'sync.yml'
            path.write_text(yaml.safe_dump(manifest, sort_keys=False))
            return self.sync.load_mapping(path)

    def test_a_duplicate_dest_keeps_the_last_group_of_its_base(self):
        mapping = self.load(
            {
                'group': [
                    group(
                        ['edge'],
                        [{'source': 'configs/cliff-stable.toml', 'dest': 'cliff.toml'}],
                        'dev',
                    ),
                    group(
                        ['edge'],
                        [{'source': 'configs/cliff-alpha.toml', 'dest': 'cliff.toml'}],
                        'dev',
                    ),
                    group(
                        ['edge'],
                        [{'source': 'configs/cliff-stable.toml', 'dest': 'cliff.toml'}],
                        'main',
                    ),
                ]
            }
        )
        self.assertEqual(
            mapping['cplieger/edge', 'dev'], {'cliff.toml': 'configs/cliff-alpha.toml'}
        )
        self.assertEqual(
            mapping['cplieger/edge', 'main'], {'cliff.toml': 'configs/cliff-stable.toml'}
        )


class PrintOpenPrs(unittest.TestCase):
    def run_print(self, fixture, manifest=TWO_BRANCH_MANIFEST):
        return run_sync(SCRIPTS / 'sync-files.py', manifest, fixture, '--print-open-prs')

    def test_one_line_per_open_sync_pull_request_through_the_engines_matching(self):
        fixture = {
            'repos': [],
            'pulls': {
                'cplieger/a': [
                    listed('a', 8, 'repo-sync/ci/default'),
                    listed('a', 6, 'repo-sync/ci/default', head_repo='someone/a'),
                    listed('a', 5, 'renovate/x'),
                ],
                'cplieger/edge': [
                    listed('edge', 12, 'repo-sync/ci/main', 'main'),
                    listed('edge', 11, 'repo-sync/ci/dev', 'dev'),
                    listed('edge', 10, 'repo-sync/ci/dev', 'dev', 'someone/edge'),
                    listed('edge', 3, 'repo-sync/ci/default', 'dev'),
                ],
            },
        }
        got = self.run_print(fixture)
        self.assertEqual(got['rc'], 0, got['stderr'])
        self.assertEqual(
            got['stdout'],
            'a 8 repo-sync/ci/default\na 6 repo-sync/ci/default\n'
            'edge 11 repo-sync/ci/dev dev\nedge 12 repo-sync/ci/main main\n',
        )
        self.assertEqual(
            api_calls(got['gh']),
            [lookup('a'), lookup('edge', 'dev'), lookup('edge', 'main')],
        )
        self.assertEqual(got['git'], [])

    def test_a_failed_lookup_is_a_warning_and_skips_only_that_target(self):
        fixture = {
            'repos': [],
            'pulls': {'cplieger/edge': [listed('edge', 12, 'repo-sync/ci/main', 'main')]},
            'fail': {'GET repos/cplieger/a/pulls': 403},
        }
        got = self.run_print(fixture)
        self.assertEqual(got['rc'], 0, got['stderr'])
        self.assertEqual(got['stdout'], 'edge 12 repo-sync/ci/main main\n')
        self.assertIn('::warning::cplieger/a: the open sync PR lookup failed: GET ', got['stderr'])


def git(*args, cwd):
    """The real git, with no user or system config, for building a source repo."""
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
    ident = ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
    out = subprocess.run(['git', *ident, *args], cwd=cwd, env=env, check=True, capture_output=True)
    return out.stdout.decode().strip()


def pin(major, workflow='ci.yaml'):
    return f'jobs:\n  ci:\n    uses: cplieger/ci/.github/workflows/{workflow}@{"a" * 40} {major}\n'


SOURCE_FILES = [
    '.editorconfig',
    {'source': 'templates/ci.yml', 'dest': '.github/workflows/ci.yaml'},
    {'source': 'templates/release.yml', 'dest': '.github/workflows/release.yaml'},
    {'source': 'configs/tool.sh', 'dest': 'scripts/tool.sh'},
    {'source': 'configs/new.json', 'dest': 'renovate.json'},
]


class SyncSource(unittest.TestCase):
    """A ci source repo whose commit holds baseline files and whose working tree
    differs from it, served through a copy of the engine naming that commit."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        src = self.tmp / 'src'
        (src / 'configs').mkdir(parents=True)
        (src / 'templates').mkdir()
        (src / '.editorconfig').write_text('baseline\n')
        for name in ('ci.yml', 'release.yml'):
            (src / 'templates' / name).write_text(f'baseline {name}\n')
        (src / 'configs' / 'tool.sh').write_text('#!/bin/sh\necho baseline\n')
        (src / 'configs' / 'tool.sh').chmod(0o755)
        git('init', '-q', cwd=src)
        git('add', '.', cwd=src)
        git('commit', '-q', '-m', 'baseline', cwd=src)
        self.sha = git('rev-parse', 'HEAD', cwd=src)
        (src / '.editorconfig').write_text('checkout\n')
        (src / 'configs' / 'tool.sh').write_text('#!/bin/sh\necho checkout\n')
        (src / 'configs' / 'new.json').write_text('{}\n')
        self.src = src
        self.engine = self.engine_with(f'{{2: {self.sha!r}}}')

    def engine_with(self, sources):
        engine = self.tmp / f'engine-{len(list(self.tmp.glob("engine-*")))}'
        engine.mkdir()
        text = (SCRIPTS / 'sync-files.py').read_text()
        text, count = re.subn(
            r'^SYNC_SOURCES = .*$', f'SYNC_SOURCES = {sources}', text, flags=re.MULTILINE
        )
        self.assertEqual(count, 1)
        (engine / 'sync-files.py').write_text(text)
        (engine / 'ghrest.py').symlink_to(SCRIPTS / 'ghrest.py')
        return engine / 'sync-files.py'

    def sync(self, workflows, engine=None, repos=('a',), files=SOURCE_FILES):
        fixture = {'repos': list(repos), 'workflows': workflows, 'diff': {}}
        manifest = {'group': [group(list(repos), files)]}
        return run_sync(engine or self.engine, manifest, fixture, '--dry-run', source_dir=self.src)

    def added(self, got, repo='cplieger/a', workflows=False):
        return {
            dest: (text, x)
            for name, dest, text, x in got['added']
            if name == repo and workflows == dest.startswith('.github/workflows/')
        }

    def test_a_v2_pinned_target_receives_the_v2_commit_while_the_checkout_differs(self):
        workflows = {'ci.yaml': pin('# v2'), 'release.yaml': pin('# v2.50.1', 'release.yaml')}
        got = self.sync({'cplieger/a': workflows})
        self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
        self.assertIn(f'  source: v2 at {self.sha[:12]}\n', got['stdout'])
        self.assertEqual(
            self.added(got),
            {
                '.editorconfig': ('baseline\n', False),
                'scripts/tool.sh': ('#!/bin/sh\necho baseline\n', True),
            },
        )
        self.assertEqual(
            self.added(got, workflows=True),
            {
                '.github/workflows/ci.yaml': ('baseline ci.yml\n', False),
                '.github/workflows/release.yaml': ('baseline release.yml\n', False),
            },
        )

    def test_a_pin_in_a_workflow_the_sync_does_not_write_is_ignored(self):
        workflows = {'ci.yaml': pin('# v2'), 'weekly.yaml': pin('# v3.2.1', 'notify-failure.yaml')}
        got = self.sync({'cplieger/a': workflows})
        self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
        self.assertIn(f'  source: v2 at {self.sha[:12]}\n', got['stdout'])
        got = self.sync({'cplieger/a': {'weekly.yaml': pin('# v3.2.1', 'notify-failure.yaml')}})
        self.assertNotIn('source:', got['stdout'])
        self.assertEqual(self.added(got)['.editorconfig'], ('checkout\n', False))

    def test_a_file_absent_at_the_v2_commit_is_held_back_with_a_notice(self):
        got = self.sync({'cplieger/a': {'ci.yaml': pin('# v2')}})
        self.assertIn(
            '::notice::cplieger/a: renovate.json held back (absent at the v2 sync source)\n',
            got['stdout'],
        )
        self.assertNotIn('renovate.json', self.added(got))

    def test_a_new_repository_takes_the_major_the_incoming_workflows_pin(self):
        (self.src / 'templates' / 'release.yml').write_text(pin('# v2', 'release.yaml'))
        baseline = {
            '.editorconfig': ('baseline\n', False),
            'scripts/tool.sh': ('#!/bin/sh\necho baseline\n', True),
        }
        for name, workflows in (('empty', {}), ('unpinned', {'ci.yaml': 'name: ci\n'})):
            with self.subTest(name):
                got = self.sync({'cplieger/a': workflows})
                self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
                self.assertIn(f'  source: v2 at {self.sha[:12]}\n', got['stdout'])
                self.assertEqual(self.added(got), baseline)
                self.assertEqual(
                    self.added(got, workflows=True)['.github/workflows/release.yaml'],
                    ('baseline release.yml\n', False),
                )
        (self.src / 'templates' / 'release.yml').write_text(pin('# v3', 'release.yaml'))
        got = self.sync({})
        self.assertIn('  source: checkout (pinned to v3)\n', got['stdout'])
        self.assertEqual(self.added(got)['.editorconfig'], ('checkout\n', False))

    def test_the_clones_own_pins_outrank_the_incoming_workflows(self):
        (self.src / 'templates' / 'release.yml').write_text(pin('# v3', 'release.yaml'))
        got = self.sync({'cplieger/a': {'ci.yaml': pin('# v2')}})
        self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
        self.assertIn(f'  source: v2 at {self.sha[:12]}\n', got['stdout'])
        self.assertEqual(self.added(got)['.editorconfig'], ('baseline\n', False))

    def test_a_target_receiving_no_workflow_keeps_the_checkout(self):
        (self.src / 'templates' / 'release.yml').write_text(pin('# v2', 'release.yaml'))
        files = ['.editorconfig', {'source': 'configs/tool.sh', 'dest': 'scripts/tool.sh'}]
        got = self.sync({}, files=files)
        self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
        self.assertNotIn('source:', got['stdout'])
        self.assertEqual(
            self.added(got),
            {
                '.editorconfig': ('checkout\n', False),
                'scripts/tool.sh': ('#!/bin/sh\necho checkout\n', True),
            },
        )

    def test_a_current_major_pin_and_no_pin_receive_the_checkout(self):
        want = {
            '.editorconfig': ('checkout\n', False),
            'scripts/tool.sh': ('#!/bin/sh\necho checkout\n', True),
            'renovate.json': ('{}\n', False),
        }
        got = self.sync({'cplieger/a': {'ci.yaml': pin('# v3')}})
        self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
        self.assertIn('  source: checkout (pinned to v3)\n', got['stdout'])
        self.assertEqual(self.added(got), want)
        got = self.sync({})
        self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
        self.assertNotIn('source:', got['stdout'])
        self.assertEqual(self.added(got), want)

    def test_mixed_unknown_or_unnamed_majors_fail_only_that_target(self):
        cases = {
            'mixed': (
                {'ci.yaml': pin('# v2'), 'release.yaml': pin('# v3', 'release.yaml')},
                'pinned to several cplieger/ci majors: v2, v3',
            ),
            'older': ({'ci.yaml': pin('# v1')}, 'pinned to v1, which has no sync source'),
            'newer': ({'ci.yaml': pin('# v4')}, 'pinned to v4, which has no sync source'),
            'unnamed': ({'ci.yaml': pin('')}, 'a cplieger/ci pin names no major'),
        }
        for name, (workflows, message) in cases.items():
            with self.subTest(name):
                got = self.sync({'cplieger/a': workflows}, repos=('a', 'b'))
                self.assertEqual(got['rc'], 1)
                self.assertIn(f'::warning::cplieger/a: sync failed — {message}', got['stdout'])
                self.assertIn('1 failed (cplieger/a)', got['stdout'])
                self.assertEqual(self.added(got), {})
                self.assertEqual(len(self.added(got, 'cplieger/b')), 3)

    def test_a_tag_ref_names_its_major(self):
        workflows = {
            'ci.yaml': 'jobs:\n  ci:\n    uses: "cplieger/ci/.github/workflows/ci.yaml@v2"\n'
        }
        got = self.sync({'cplieger/a': workflows})
        self.assertIn(f'  source: v2 at {self.sha[:12]}\n', got['stdout'])

    def test_a_source_commit_missing_from_the_checkout_fails_the_target(self):
        engine = self.engine_with(f'{{2: {"0" * 40!r}}}')
        got = self.sync({'cplieger/a': {'ci.yaml': pin('# v2')}}, engine=engine)
        self.assertEqual(got['rc'], 1)
        self.assertIn('::warning::cplieger/a: sync failed — ', got['stdout'])
        self.assertEqual(self.added(got), {})

    def test_the_engine_names_a_full_commit_for_every_older_major(self):
        sync = load_sync()
        self.assertTrue(sync.SYNC_SOURCES)
        for major, sha in sync.SYNC_SOURCES.items():
            self.assertLess(major, sync.CURRENT_MAJOR)
            self.assertRegex(sha, r'^[0-9a-f]{40}$')


class Manifest(unittest.TestCase):
    def test_a_missing_or_empty_manifest_is_refused_in_every_mode(self):
        cases = (('empty file', ''), ('null', None), ('no groups', {'group': []}), ('a list', [1]))
        for name, manifest in cases:
            for mode in ((), ('--print-open-prs',)):
                with self.subTest(name, mode=mode):
                    got = run_sync(SCRIPTS / 'sync-files.py', manifest, {'repos': []}, *mode)
                    self.assertEqual(got['rc'], 1)
                    self.assertIn('lists no sync group', got['stderr'])
                    self.assertTrue(got['stderr'].startswith('::error::sync-files: '))
                    self.assertEqual((got['git'], got['gh']), ([], []))
        got = run_sync(SCRIPTS / 'sync-files.py', {'group': []}, {'repos': []}, '--manifest', 'x')
        self.assertEqual(got['rc'], 1)
        self.assertIn('::error::sync-files: cannot read the manifest x: ', got['stderr'])


def sweep_body():
    doc = yaml.safe_load((ROOT / '.github' / 'workflows' / 'sync.yaml').read_text())
    (step,) = [
        s for s in doc['jobs']['sync']['steps'] if s.get('name') == 'Enable auto-merge on sync PRs'
    ]
    return step['run']


def run_sweep(manifest, fixture):
    with tempfile.TemporaryDirectory() as name:
        tmp = pathlib.Path(name)
        env = install_stubs(tmp, fixture)
        (tmp / '.github').mkdir()
        text = manifest if isinstance(manifest, str) else yaml.safe_dump(manifest, sort_keys=False)
        (tmp / '.github' / 'sync.yml').write_text(text)
        (tmp / 'scripts').symlink_to(SCRIPTS)
        proc = subprocess.run(
            ['bash', '-e', '-c', sweep_body()],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp,
            check=False,
        )
        return proc, read_log(tmp / 'gh.log')


def pulls(heads):
    """`gh api` answers for each own sync pull request, {number: base}, at `headsha`."""
    return {
        f'repos/cplieger/edge/pulls/{n}': {
            'state': 'open',
            'base': {'ref': base},
            'head': {
                'sha': 'headsha',
                'ref': f'repo-sync/ci/{base}',
                'repo': {'full_name': 'cplieger/edge'},
            },
        }
        for n, base in heads.items()
    }


def merges(calls):
    """Every merge call: `gh pr merge`, and each REST merge with its body."""
    return [
        c if c[:2] == ['pr', 'merge'] else api_call(c)
        for c in calls
        if c[:2] == ['pr', 'merge'] or (c[:1] == ['api'] and api_call(c)[1].endswith('/merge'))
    ]


DIRECT = ('PUT', 'repos/cplieger/edge/pulls/12/merge', {'merge_method': 'squash', 'sha': 'headsha'})


class Sweep(unittest.TestCase):
    def test_main_default_repos_get_the_unscoped_lookup_and_merge(self):
        manifest = {'group': [group(['a', 'b'], FILES), group(['a'], ['.gitattributes'])]}
        proc, calls = run_sweep(
            manifest,
            {'repos': [], 'pulls': {'cplieger/a': [listed('a', 7, 'repo-sync/ci/default')]}},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            [api_call(c) if c[0] == 'api' else c for c in calls],
            [
                lookup('a'),
                lookup('b'),
                [
                    'pr',
                    'merge',
                    '--auto',
                    '--squash',
                    '--delete-branch',
                    '--repo',
                    'cplieger/a',
                    '7',
                ],
            ],
        )

    def test_a_two_branch_repo_is_swept_on_both_heads_scoped_to_their_base(self):
        fixture = {
            'repos': [],
            'pulls': {
                'cplieger/edge': [
                    listed('edge', 11, 'repo-sync/ci/dev', 'dev'),
                    listed('edge', 12, 'repo-sync/ci/main', 'main'),
                    listed('edge', 3, 'repo-sync/ci/default', 'dev'),
                ]
            },
            'api': pulls({11: 'dev', 12: 'main'}),
        }
        proc, calls = run_sweep(TWO_BRANCH_MANIFEST, fixture)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        edge = [c for c in calls if any('cplieger/edge' in a for a in c)]
        self.assertEqual(
            [api_call(c) for c in edge if '/pulls?' in c[-1]],
            [lookup('edge', 'dev'), lookup('edge', 'main')],
        )
        self.assertEqual([c[2] for c in edge if c[:2] == ['pr', 'merge']], ['11', '12'])

    def test_a_base_target_is_armed_only_after_a_fresh_read_pinned_to_that_head(self):
        for base in ('dev', 'main'):
            with self.subTest(base=base):
                fixture = {
                    'repos': [],
                    'pulls': {'cplieger/edge': [listed('edge', 12, f'repo-sync/ci/{base}', base)]},
                    'api': pulls({12: base}),
                }
                manifest = {'group': [group(['edge'], FILES, base=base)]}
                proc, calls = run_sweep(manifest, fixture)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                armed = [
                    'pr', 'merge', '12', '-R', 'cplieger/edge', '--squash', '--delete-branch',
                    '--auto', '--match-head-commit', 'headsha',
                ]  # fmt: skip
                self.assertEqual(merges(calls), [armed])
                read = calls.index(['api', '-i', 'repos/cplieger/edge/pulls/12'])
                self.assertLess(read, calls.index(armed))

    def test_a_main_default_refusal_falls_back_to_the_rest_direct_merge(self):
        manifest = {'group': [group(['a'], FILES)]}
        auto = ['pr', 'merge', '--auto', '--squash', '--delete-branch', '--repo', 'cplieger/a', '7']
        direct = ['api', '-X', 'PUT', 'repos/cplieger/a/pulls/7/merge', '-f', 'merge_method=squash']
        read = ['api', 'repos/cplieger/a/pulls/7', '--jq', '.head.repo.full_name // ""']
        delete = ['api', '-X', 'DELETE', 'repos/cplieger/a/git/refs/heads/repo-sync/ci/default']
        cases = (
            ('merged', {'auto': True}, 'cplieger/a', [auto, direct, read, delete], False),
            ('refused', {'auto': True, 'direct': True}, 'cplieger/a', [auto, direct], True),
            ('a fork head', {'auto': True}, 'someone/a', [auto, direct, read], False),
        )
        for name, fails, owner, want, warned in cases:
            with self.subTest(name):
                row = listed('a', 7, 'repo-sync/ci/default', head_repo=owner)
                proc, calls = run_sweep(
                    manifest,
                    {
                        'repos': [],
                        'pulls': {'cplieger/a': [row]},
                        'api': {'repos/cplieger/a/pulls/7': row},
                        'merge_fails': fails,
                    },
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual([c for c in calls if '/pulls?' not in c[-1]], want)
                self.assertEqual(
                    'WARN: failed to merge cplieger/a#7' in proc.stdout, warned, proc.stdout
                )
                self.assertNotIn('could not delete', proc.stdout)

    def test_a_main_default_branch_delete_tolerates_only_an_already_deleted_head(self):
        manifest = {'group': [group(['a'], FILES)]}
        row = listed('a', 7, 'repo-sync/ci/default')
        for status, warned in ((422, False), (403, True)):
            with self.subTest(status=status):
                proc, _ = run_sweep(
                    manifest,
                    {
                        'repos': [],
                        'pulls': {'cplieger/a': [row]},
                        'api': {'repos/cplieger/a/pulls/7': row},
                        'merge_fails': {'auto': True},
                        'fail': {
                            'DELETE repos/cplieger/a/git/refs/heads/repo-sync/ci/default': status
                        },
                    },
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    'WARN: merged cplieger/a#7 but could not delete repo-sync/ci/default'
                    in proc.stdout,
                    warned,
                    proc.stdout,
                )

    def fallback(
        self, runs, fails=None, head=None, head_repo='cplieger/edge', base='main', now_base=None
    ):
        fixture = {
            'repos': [],
            'pulls': {'cplieger/edge': [listed('edge', 12, f'repo-sync/ci/{base}', base)]},
            'merge_fails': {'auto': True} if fails is None else fails,
            'api': {
                'repos/cplieger/edge/pulls/12': {
                    'state': 'open',
                    'base': {'ref': now_base or base},
                    'head': {
                        'sha': 'headsha',
                        'ref': head or f'repo-sync/ci/{base}',
                        'repo': {'full_name': head_repo},
                    },
                },
                'repos/cplieger/edge/commits/headsha/check-runs'
                '?check_name=ci%20%2F%20validate&filter=latest&per_page=100': {
                    'check_runs': [
                        {'app': {'id': app}, 'status': status, 'conclusion': conclusion}
                        for app, status, conclusion in runs
                    ]
                },
            },
        }
        manifest = {'group': [group(['edge'], FILES, base=base)]}
        proc, calls = run_sweep(manifest, fixture)
        return proc, merges(calls)

    def main_fallback(self, runs, fails=None, head='repo-sync/ci/main', head_repo='cplieger/edge'):
        return self.fallback(runs, fails, head, head_repo)

    def test_devs_direct_merge_rereads_the_base_and_only_warns_when_it_is_refused(self):
        dev = ('PUT', 'repos/cplieger/edge/pulls/12/merge', DIRECT[2])
        proc, got = self.fallback([(15368, 'completed', 'success')], base='dev')
        self.assertEqual((proc.returncode, got[1:]), (0, [dev]), proc.stderr)
        self.assertNotIn('WARN', proc.stdout)
        proc, got = self.fallback([(15368, 'completed', 'failure')], base='dev')
        self.assertEqual((proc.returncode, len(got)), (0, 1))
        self.assertIn('`ci / validate` is not green at headsha', proc.stdout)
        self.assertIn('WARN: failed to merge cplieger/edge#12', proc.stdout)

    def test_a_sync_pull_request_retargeted_after_the_listing_is_never_armed_or_merged(self):
        for base, now_base in (('dev', 'main'), ('main', 'dev')):
            for fails in ({}, {'auto': True}):
                with self.subTest(base=base, now_base=now_base, fails=fails):
                    proc, got = self.fallback(
                        [(15368, 'completed', 'success')], fails, base=base, now_base=now_base
                    )
                    self.assertEqual(got, [])
                    self.assertIn(
                        f'not merged: the pull request is not repo-sync/ci/{base} of '
                        f'cplieger/edge into {base}',
                        proc.stdout,
                    )
                    self.assertEqual(proc.returncode, 1 if base == 'main' else 0)

    def test_mains_direct_merge_needs_a_green_validate_at_the_head_it_pins(self):
        proc, got = self.main_fallback([(15368, 'completed', 'success')])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(got[1], DIRECT)

    def test_mains_pr_stays_open_and_fails_the_step_without_a_green_validate(self):
        cases = {
            'pending': [(15368, 'in_progress', None)],
            'missing': [],
            'red': [(15368, 'completed', 'failure')],
            'another app': [(99, 'completed', 'success')],
        }
        for name, runs in cases.items():
            with self.subTest(name):
                proc, got = self.main_fallback(runs)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(len(got), 1)
                self.assertIn('`ci / validate` is not green at headsha', proc.stdout)
                self.assertIn('a sync pull request into main was left open', proc.stdout)
        proc, got = self.main_fallback(
            [(15368, 'completed', 'success')], {'auto': True, 'direct': True}
        )
        self.assertEqual((proc.returncode, len(got)), (1, 2))

    def test_mains_direct_merge_refuses_a_pull_request_that_is_not_the_sync_head(self):
        for head, head_repo in (
            ('repo-sync/ci/dev', 'cplieger/edge'),
            ('repo-sync/ci/main', 'someone/edge'),
        ):
            with self.subTest(head=head, repo=head_repo):
                proc, got = self.main_fallback(
                    [(15368, 'completed', 'success')], head=head, head_repo=head_repo
                )
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(got, [])
                self.assertIn('not merged: the pull request is not repo-sync/ci/main', proc.stdout)

    def test_a_fork_pull_request_with_the_sync_head_is_never_armed(self):
        fork = listed('edge', 21, 'repo-sync/ci/dev', 'dev', 'someone/edge')
        own = listed('edge', 11, 'repo-sync/ci/dev', 'dev')
        for name, rows, armed in (('fork only', [fork], []), ('both', [fork, own], ['11'])):
            with self.subTest(name):
                proc, calls = run_sweep(
                    TWO_BRANCH_MANIFEST,
                    {
                        'repos': [],
                        'pulls': {'cplieger/edge': rows},
                        'api': pulls({11: 'dev'}),
                    },
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                got = [c[2] for c in calls if c[:2] == ['pr', 'merge'] and 'cplieger/edge' in c]
                self.assertEqual(got, armed)

    def test_an_unreadable_manifest_fails_the_step(self):
        proc, calls = run_sweep(
            {'group': [group(['edge'], FILES, base='other')]}, {'repos': [], 'pulls': {}}
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(calls, [])

    def test_an_empty_manifest_fails_the_step_with_an_error_and_merges_nothing(self):
        # An empty file is what a failed classify step leaves behind.
        for manifest in ('', {'group': []}):
            with self.subTest(manifest=manifest):
                proc, calls = run_sweep(manifest, {'repos': [], 'pulls': {}})
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn('::error::sync-files: ', proc.stderr)
                self.assertNotIn('Traceback', proc.stderr)
                self.assertEqual(calls, [])


class Workflow(unittest.TestCase):
    def test_an_engine_change_reruns_the_sync(self):
        paths = yaml.safe_load((ROOT / '.github' / 'workflows' / 'sync.yaml').read_text())[True][
            'push'
        ]['paths']
        for path in (
            'scripts/classify-repos.py',
            'scripts/sync-files.py',
            'scripts/release_channels.py',
            'scripts/ghrest.py',
        ):
            self.assertIn(path, paths)

    def test_the_sweep_reads_its_pull_requests_from_the_engine(self):
        body = sweep_body()
        self.assertIn('scripts/sync-files.py --manifest .github/sync.yml --print-open-prs', body)
        self.assertNotIn('repo-sync/ci/default', body)
        self.assertNotIn('gh pr list', body)

    def test_the_checkout_holds_every_older_major_sync_source(self):
        doc = yaml.safe_load((ROOT / '.github' / 'workflows' / 'sync.yaml').read_text())
        (checkout,) = [s for s in doc['jobs']['sync']['steps'] if s.get('name') == 'Checkout']
        self.assertEqual(checkout['with']['fetch-depth'], 0)


if __name__ == '__main__':
    unittest.main()
