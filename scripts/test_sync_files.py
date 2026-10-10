"""sync-files.py and sync.yaml's auto-merge sweep: one target per base, driven
by stub `git` and `gh` binaries that log every call."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

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
if (method, where) == ('GET', 'installation/repositories'):
    rows = [{'name': n, 'fork': n in fx.get('forks', [])} for n in fx['repos']]
    reply(200, {'total_count': len(rows), 'repositories': rows if q.get('page') == '1' else []})
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
    `added` is each staged file as [repo, dest, text, executable]. One worker unless
    `extra` names --workers, so the call logs keep the serial order."""
    if '--workers' not in extra:
        extra = (*extra, '--workers', '1')
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

    def test_a_fork_pull_request_with_the_default_head_is_neither_reused_nor_closed(self):
        pulls = {
            **MAIN_FIXTURE['pulls'],
            'cplieger/a': [listed('a', 6, 'repo-sync/ci/default', head_repo='someone/a')],
            'cplieger/b': [listed('b', 5, 'repo-sync/ci/default', head_repo='someone/b')],
        }
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, {**MAIN_FIXTURE, 'pulls': pulls})
        self.assertEqual(got['rc'], 0, got['stdout'])
        writes = [c[:2] for c in api_calls(got['gh']) if c[0] != 'GET']
        self.assertEqual(
            writes,
            [('POST', 'repos/cplieger/a/pulls'), ('POST', 'repos/cplieger/a/issues/99/labels')],
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
        fixture = {**MAIN_FIXTURE, 'fail': {'GET installation/repositories': 403}}
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


class SweepLookups(unittest.TestCase):
    def test_every_open_sync_pull_request_through_the_engines_matching(self):
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
            'api': pulls({11: 'dev', 12: 'main'}),
        }
        proc, calls = run_sweep(TWO_BRANCH_MANIFEST, fixture)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            [line for line in proc.stdout.splitlines() if line.startswith('Enabling')],
            [
                'Enabling auto-merge on cplieger/a#8',
                'Enabling auto-merge on cplieger/edge#11',
                'Enabling auto-merge on cplieger/edge#12',
            ],
        )
        self.assertEqual(
            [api_call(c) for c in calls if '/pulls?' in c[-1]],
            [lookup('a'), lookup('edge', 'dev'), lookup('edge', 'main')],
        )

    def test_a_failed_lookup_is_a_warning_and_skips_only_that_target(self):
        fixture = {
            'repos': [],
            'pulls': {'cplieger/edge': [listed('edge', 12, 'repo-sync/ci/main', 'main')]},
            'api': pulls({12: 'main'}),
            'fail': {'GET repos/cplieger/a/pulls': 403},
        }
        proc, _calls = run_sweep(TWO_BRANCH_MANIFEST, fixture)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('::warning::cplieger/a: the open sync PR lookup failed: GET ', proc.stdout)
        enabling = [line for line in proc.stdout.splitlines() if line.startswith('Enabling')]
        self.assertEqual(enabling, ['Enabling auto-merge on cplieger/edge#12'])


class Concurrent(unittest.TestCase):
    """Several workers: the same output, calls, and exit as one."""

    def both(self, manifest, fixture):
        serial = run_sync(SCRIPTS / 'sync-files.py', manifest, fixture)
        parallel = run_sync(SCRIPTS / 'sync-files.py', manifest, fixture, '--workers', '4')
        return serial, parallel

    def assert_same(self, serial, parallel):
        self.assertEqual(parallel['rc'], serial['rc'])
        self.assertEqual(parallel['stdout'], serial['stdout'])
        for log in ('git', 'gh'):
            key = json.dumps
            self.assertEqual(sorted(parallel[log], key=key), sorted(serial[log], key=key), log)

    def test_the_output_is_in_manifest_order_and_the_calls_are_the_serial_ones(self):
        serial, parallel = self.both(TWO_BRANCH_MANIFEST, TWO_BRANCH_FIXTURE)
        self.assertEqual(serial['rc'], 0, serial['stdout'] + serial['stderr'])
        self.assert_same(serial, parallel)

    def test_a_failed_target_fails_the_run_and_spares_the_others(self):
        fixture = {**MAIN_FIXTURE, 'fail': {'GET repos/cplieger/a/pulls': 403}}
        serial, parallel = self.both(MAIN_MANIFEST, fixture)
        self.assertEqual(parallel['rc'], 1)
        self.assertIn('1 failed (cplieger/a)', parallel['stdout'])
        self.assertIn('  PR already open; force-push refreshed it', parallel['stdout'])
        self.assert_same(serial, parallel)

    def test_workers_outside_one_to_the_bound_are_refused_before_any_call(self):
        for workers in ('0', '17'):
            got = run_sync(
                SCRIPTS / 'sync-files.py', MAIN_MANIFEST, MAIN_FIXTURE, '--workers', workers
            )
            self.assertEqual(got['rc'], 2, workers)
            self.assertIn('--workers must be 1 to 16', got['stderr'])
            self.assertEqual((got['git'], got['gh']), ([], []))

    def test_every_client_holds_on_the_shared_pacers_rate_limit_pause(self):
        sync = load_sync()
        self.assertEqual(sync.REST.pause, sync.PACER.pause)
        import release_maintenance

        with unittest.mock.patch.object(release_maintenance, 'Api') as api:
            self.assertEqual(sync.arm_open_prs({}, 1), 0)
        self.assertEqual(api.call_args.kwargs, {'run': sync.RUN, 'pause': sync.PACER.pause})

    def test_the_bound_itself_is_accepted(self):
        got = run_sync(SCRIPTS / 'sync-files.py', MAIN_MANIFEST, MAIN_FIXTURE, '--workers', '16')
        self.assertEqual(got['rc'], 0, got['stderr'])

    def test_every_gh_call_is_admitted_by_the_pacer_a_write_as_a_write(self):
        sync, answered = load_with_stub_gh(load_sync)
        with unittest.mock.patch.object(sync.PACER, 'wait') as wait:
            sync.REST.get('repos/cplieger/a')
            sync.REST.send('POST', 'repos/cplieger/a/pulls', {})
            sync.RUN(['pr', 'merge', '--auto', '7'])
        self.assertEqual([c.args for c in wait.call_args_list], [(False,), (True,), (True,)])
        self.assertEqual(len(answered), 3)

    def test_writes_are_spaced_only_when_more_than_one_worker_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / 'sync.yml'
            manifest.write_text(yaml.safe_dump(MAIN_MANIFEST, sort_keys=False))
            for workers, gap in (('1', 0.0), ('2', 1.0), ('16', 1.0)):
                with self.subTest(workers=workers):
                    sync = load_sync()
                    argv = ['sync-files.py', '--manifest', str(manifest), '--only', 'none']
                    argv += ['--allow-forks', '--workers', workers]
                    out = io.StringIO()
                    with (
                        unittest.mock.patch.object(sys, 'argv', argv),
                        contextlib.redirect_stdout(out),
                    ):
                        sync.main()
                    self.assertIn('nothing to sync', out.getvalue())
                    self.assertEqual(sync.PACER.gap, gap)


class ArmDefault(unittest.TestCase):
    """A main-default arm in-process: only GitHub's refusal of --auto itself leads to the
    direct merge; a refusal that says nothing about the pull request leaves it open."""

    def arm(self, answer, now_merged=False):
        sync = load_sync()

        def run(_args, _stdin=None, _timeout=None):
            if isinstance(answer, BaseException):
                raise answer
            return answer

        out = io.StringIO()
        reread = {'merged_at': '2026-10-10T00:00:00Z' if now_merged is True else None}
        failed = now_merged if isinstance(now_merged, Exception) else None
        with (
            unittest.mock.patch.object(sync, 'RUN', run),
            unittest.mock.patch.object(sync.REST, 'send') as send,
            unittest.mock.patch.object(
                sync.REST, 'get', return_value=reread, side_effect=failed
            ) as get,
            contextlib.redirect_stdout(out),
        ):
            self.assertTrue(sync.arm(('cplieger/a', None, 7), None))
        self.reads = [c.args for c in get.call_args_list]
        return [c.args[:2] for c in send.call_args_list], out.getvalue()

    def test_a_rate_limited_or_unstarted_arm_leaves_the_pull_request_open(self):
        import ghrest

        for name, answer in (
            ('rate limited', gh_failed(b'GraphQL: API rate limit exceeded for user ID 1.')),
            ('cooldown past the cap', ghrest.ApiError(None, 'held', rate_limited=True)),
            ('gh did not start', FileNotFoundError('gh')),
        ):
            with self.subTest(name):
                sent, out = self.arm(answer)
                self.assertEqual(sent, [])
                self.assertIn('WARN: failed to merge cplieger/a#7', out)

    def test_a_rate_limit_refusal_after_the_merge_reports_it_merged(self):
        sent, out = self.arm(gh_failed(b'API rate limit exceeded'), now_merged=True)
        self.assertEqual(sent, [])
        self.assertEqual(self.reads, [('repos/cplieger/a/pulls/7',)])
        self.assertEqual(out, 'Enabling auto-merge on cplieger/a#7\ncplieger/a#7: merged\n')
        import ghrest

        sent, out = self.arm(gh_failed(b'API rate limit exceeded'), ghrest.ApiError(403, 'held'))
        self.assertEqual(sent, [])
        self.assertIn('WARN: failed to merge cplieger/a#7: auto-merge rate limited', out)

    def test_an_ordinary_refusal_still_falls_back_to_the_direct_merge(self):
        sent, out = self.arm(gh_failed(b'Pull request is in clean status'))
        self.assertEqual(
            sent,
            [
                ('PUT', 'repos/cplieger/a/pulls/7/merge'),
                ('DELETE', 'repos/cplieger/a/git/refs/heads/repo-sync/ci/default'),
            ],
        )
        self.assertIn('cplieger/a#7: merged', out)


def gh_failed(stderr):
    return subprocess.CompletedProcess(['gh'], 1, b'', stderr)


def load_with_stub_gh(load):
    """(module, answered calls) of `load()` with ghrest.run_process stubbed to a 200
    before the module captures it, so a call through its paced runner reaches no gh."""
    import ghrest

    answered = []

    def stub(args, stdin=None, timeout=None):
        answered.append(args)
        return subprocess.CompletedProcess(['gh'], 0, b'HTTP/2.0 200 OK\n\r\n{}', b'')

    with unittest.mock.patch.object(ghrest, 'run_process', stub):
        return load(), answered


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


class CheckoutSource(unittest.TestCase):
    def test_a_target_pinned_to_an_older_major_receives_the_checkout(self):
        src = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, src)
        (src / 'configs').mkdir()
        (src / 'templates').mkdir()
        (src / '.editorconfig').write_text('checkout\n')
        (src / 'configs' / 'tool.sh').write_text('#!/bin/sh\necho checkout\n')
        (src / 'configs' / 'tool.sh').chmod(0o755)
        (src / 'configs' / 'new.json').write_text('{}\n')
        for name in ('ci.yml', 'release.yml'):
            (src / 'templates' / name).write_text(pin('# v3', name.replace('.yml', '.yaml')))
        git('init', '-q', cwd=src)
        workflows = {'ci.yaml': pin('# v2'), 'release.yaml': pin('# v2', 'release.yaml')}
        fixture = {'repos': ['a'], 'workflows': {'cplieger/a': workflows}, 'diff': {}}
        manifest = {'group': [group(['a'], SOURCE_FILES)]}
        got = run_sync(SCRIPTS / 'sync-files.py', manifest, fixture, '--dry-run', source_dir=src)
        self.assertEqual(got['rc'], 0, got['stdout'] + got['stderr'])
        self.assertNotIn('source:', got['stdout'])
        added = {dest: (text, x) for name, dest, text, x in got['added'] if name == 'cplieger/a'}
        self.assertEqual(added['.editorconfig'], ('checkout\n', False))
        self.assertEqual(added['scripts/tool.sh'], ('#!/bin/sh\necho checkout\n', True))
        self.assertEqual(added['renovate.json'], ('{}\n', False))
        self.assertEqual(added['.github/workflows/release.yaml'][0], pin('# v3', 'release.yaml'))


class Manifest(unittest.TestCase):
    def test_a_missing_or_empty_manifest_is_refused_in_every_mode(self):
        cases = (('empty file', ''), ('null', None), ('no groups', {'group': []}), ('a list', [1]))
        for name, manifest in cases:
            for mode in ((), ('--arm-open-prs',)):
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


def run_sweep(manifest, fixture, workers=1, extra=()):
    """(process, gh calls) of sync.yaml's sweep step, run as written plus --workers so
    one worker keeps the serial call order, and plus `extra`."""
    with tempfile.TemporaryDirectory() as name:
        tmp = pathlib.Path(name)
        env = install_stubs(tmp, fixture)
        (tmp / '.github').mkdir()
        text = manifest if isinstance(manifest, str) else yaml.safe_dump(manifest, sort_keys=False)
        (tmp / '.github' / 'sync.yml').write_text(text)
        (tmp / 'scripts').symlink_to(SCRIPTS)
        flags = ' '.join(['--workers', str(workers), *extra])
        proc = subprocess.run(
            ['bash', '-e', '-c', f'{sweep_body().rstrip()} {flags}'],
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
FORK_LIST = ('GET', 'installation/repositories?per_page=100&page=1', None)


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
                FORK_LIST,
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
        direct = [
            'api', '-i', '-X', 'PUT', 'repos/cplieger/a/pulls/7/merge', '--input', '-',
            '{"merge_method": "squash"}',
        ]  # fmt: skip
        delete = [
            'api',
            '-i',
            '-X',
            'DELETE',
            'repos/cplieger/a/git/refs/heads/repo-sync/ci/default',
        ]
        cases = (
            ('merged', {'auto': True}, [auto, direct, delete], False),
            ('refused', {'auto': True, 'direct': True}, [auto, direct], True),
        )
        for name, fails, want, warned in cases:
            with self.subTest(name):
                proc, calls = run_sweep(
                    manifest,
                    {
                        'repos': [],
                        'pulls': {'cplieger/a': [listed('a', 7, 'repo-sync/ci/default')]},
                        'merge_fails': fails,
                    },
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual([c for c in calls if '?' not in c[-1]], want)
                self.assertEqual(
                    'WARN: failed to merge cplieger/a#7' in proc.stdout, warned, proc.stdout
                )
                self.assertNotIn('could not delete', proc.stdout)

    def test_a_main_default_fork_pull_request_with_the_sync_head_is_never_merged(self):
        manifest = {'group': [group(['a'], FILES)]}
        fork = listed('a', 6, 'repo-sync/ci/default', head_repo='someone/a')
        for fails in ({}, {'auto': True}):
            with self.subTest(fails=fails):
                proc, calls = run_sweep(
                    manifest, {'repos': [], 'pulls': {'cplieger/a': [fork]}, 'merge_fails': fails}
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(merges(calls), [])
                self.assertNotIn('Enabling auto-merge', proc.stdout)

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

    def scoped(self, extra=(), fail=None):
        """(process, {repo: its gh calls}, other calls) of a sweep over a, b and the fork
        e, each with an own open sync PR."""
        fixture = {
            'repos': ['a', 'b', 'e'],
            'forks': ['e'],
            'pulls': {f'cplieger/{r}': [listed(r, 7, 'repo-sync/ci/default')] for r in 'abe'},
            'fail': fail or {},
        }
        manifest = {'group': [group(['a', 'b', 'e'], FILES)]}
        proc, calls = run_sweep(manifest, fixture, extra=extra)
        by_repo, other = {}, []
        for call in calls:
            repo = next((r for r in 'abe' if any(f'cplieger/{r}' in a for a in call)), None)
            (by_repo.setdefault(repo, []) if repo else other).append(call)
        return proc, by_repo, other

    def test_the_sweep_writes_only_the_targets_a_sync_would(self):
        for name, extra, swept in (
            ('forks skipped', (), 'ab'),
            ('only', ('--only', 'a'), 'a'),
            ('forks allowed', ('--allow-forks',), 'abe'),
        ):
            with self.subTest(name):
                proc, by_repo, other = self.scoped(extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(''.join(sorted(by_repo)), swept)
                for repo in swept:
                    self.assertIn(f'cplieger/{repo}#7: armed', proc.stdout)
                fork_list = [] if '--allow-forks' in extra else [FORK_LIST]
                self.assertEqual([api_call(c) for c in other], fork_list)
                notice = '::notice::cplieger/e: skipped, repo is a fork'
                self.assertEqual(notice in proc.stdout, 'e' not in swept and not extra)

    def test_an_unreadable_repo_list_refuses_the_sweep_before_any_lookup(self):
        proc, by_repo, other = self.scoped(fail={'GET installation/repositories': 403})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('forks cannot be excluded', proc.stderr)
        self.assertEqual(by_repo, {})
        self.assertEqual([api_call(c) for c in other], [FORK_LIST])

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

    def test_several_workers_arm_the_same_pull_requests_in_the_same_order(self):
        fixture = {
            'repos': [],
            'pulls': {
                'cplieger/a': [listed('a', 7, 'repo-sync/ci/default')],
                'cplieger/edge': [
                    listed('edge', 11, 'repo-sync/ci/dev', 'dev'),
                    listed('edge', 12, 'repo-sync/ci/main', 'main'),
                ],
            },
            'api': {
                **pulls({11: 'dev', 12: 'main'}),
                'repos/cplieger/edge/pulls/12': {
                    **pulls({12: 'main'})['repos/cplieger/edge/pulls/12'],
                    'base': {'ref': 'dev'},
                },
            },
        }
        serial, serial_calls = run_sweep(TWO_BRANCH_MANIFEST, fixture)
        parallel, parallel_calls = run_sweep(TWO_BRANCH_MANIFEST, fixture, workers=4)
        self.assertEqual(serial.returncode, 1, serial.stdout + serial.stderr)
        self.assertEqual(parallel.returncode, 1)
        self.assertEqual(parallel.stdout, serial.stdout)
        self.assertIn('cplieger/edge#11: armed', parallel.stdout)
        self.assertIn('a sync pull request into main was left open', parallel.stdout)
        key = json.dumps
        self.assertEqual(sorted(parallel_calls, key=key), sorted(serial_calls, key=key))


class Workflow(unittest.TestCase):
    def test_an_engine_change_reruns_the_sync(self):
        paths = yaml.safe_load((ROOT / '.github' / 'workflows' / 'sync.yaml').read_text())[True][
            'push'
        ]['paths']
        for path in (
            'scripts/classify-repos.py',
            'scripts/sync-files.py',
            'scripts/release_channels.py',
            'scripts/release_maintenance.py',
            'scripts/ghrest.py',
            'scripts/fanout.py',
        ):
            self.assertIn(path, paths)

    def test_the_sweep_is_the_engines(self):
        body = sweep_body()
        self.assertEqual(
            body.strip(), 'python3 scripts/sync-files.py --manifest .github/sync.yml --arm-open-prs'
        )
        doc = yaml.safe_load((ROOT / '.github' / 'workflows' / 'sync.yaml').read_text())
        (step,) = [
            s
            for s in doc['jobs']['sync']['steps']
            if s.get('name') == 'Enable auto-merge on sync PRs'
        ]
        self.assertEqual(step['if'], 'always()')

    def test_every_step_authenticates_with_the_app_token_minted_after_checkout(self):
        text = (ROOT / '.github' / 'workflows' / 'sync.yaml').read_text()
        self.assertNotIn('SYNC_PAT', text)
        steps = yaml.safe_load(text)['jobs']['sync']['steps']
        # Right after Checkout, so a failed mint leaves the always() sweep a
        # checked-out tree to fail in on the missing manifest.
        self.assertEqual([s['name'] for s in steps[:2]], ['Checkout', 'Mint the App token'])
        mint = steps[1]
        self.assertEqual(mint['id'], 'app-token')
        self.assertRegex(mint['uses'], r'^actions/create-github-app-token@[0-9a-f]{40}$')
        self.assertNotIn('continue-on-error', mint)
        self.assertNotIn('if', mint)
        self.assertEqual(
            mint['with'],
            {
                'client-id': '${{ secrets.SYNC_APP_ID }}',
                'private-key': '${{ secrets.SYNC_APP_PRIVATE_KEY }}',
                'owner': 'cplieger',
                'permission-checks': 'read',
                'permission-contents': 'write',
                'permission-metadata': 'read',
                'permission-pull-requests': 'write',
                'permission-workflows': 'write',
            },
        )
        tokens = {s['name']: s['env']['GH_TOKEN'] for s in steps if 'GH_TOKEN' in s.get('env', {})}
        self.assertEqual(
            tokens,
            dict.fromkeys(
                (
                    'Auto-classify repos',
                    'Sync files to consumer repos',
                    'Enable auto-merge on sync PRs',
                ),
                '${{ steps.app-token.outputs.token }}',
            ),
        )

    def test_the_sync_and_sweep_steps_run_on_the_default_pool_of_several_workers(self):
        import fanout
        import release_maintenance

        self.assertTrue(2 <= fanout.WORKERS <= fanout.MAX_WORKERS, fanout.WORKERS)
        self.assertEqual(fanout.ordered.__defaults__, (fanout.WORKERS,))
        doc = yaml.safe_load((ROOT / '.github' / 'workflows' / 'sync.yaml').read_text())
        runs = {s.get('name'): s.get('run', '') for s in doc['jobs']['sync']['steps']}
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / '.github').mkdir()
        (tmp / '.github' / 'sync.yml').write_text(yaml.safe_dump(MAIN_MANIFEST, sort_keys=False))
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp)
        for name, fan_outs in (
            ('Sync files to consumer repos', 1),
            ('Enable auto-merge on sync PRs', 2),
        ):
            with self.subTest(name):
                argv = shlex.split(runs[name])
                self.assertEqual(argv[:2], ['python3', 'scripts/sync-files.py'])
                self.assertNotIn('--workers', argv)
                sync = load_sync()
                seen = []

                def recorder(_items, _work, workers, seen=seen):
                    seen.append(workers)
                    return iter(())

                with (
                    unittest.mock.patch.object(sys, 'argv', ['sync-files.py', *argv[2:]]),
                    unittest.mock.patch.object(sync.fanout, 'ordered', recorder),
                    unittest.mock.patch.object(sync, 'fork_names', return_value=set()),
                    unittest.mock.patch.object(release_maintenance, 'Api'),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    try:
                        sync.main()
                    except SystemExit as stop:
                        self.assertEqual(stop.code, 0)
                self.assertEqual(seen, [fanout.WORKERS] * fan_outs)
                self.assertEqual(sync.PACER.gap, sync.WRITE_GAP)


if __name__ == '__main__':
    unittest.main()
