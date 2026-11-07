"""classify-repos.py: the manifest per base, and the sync-owned set the main
intake and the promotion read, driven by a stub `gh` over fixture repos."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import yaml
from inventory import glob_pattern

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
TESTDATA = SCRIPTS / 'testdata' / 'classify-repos'

# Answers `gh api -i` with the fixture: the REST repo listing, tree and tag
# reads (a missing key is a 404, `tags_fail` a 403, `tags_raw` a body answered
# verbatim, a `slow` tree half a second late), and logs each argv.
GH_STUB = r"""#!/usr/bin/env python3
import json, os, re, sys, time
fx = json.load(open(os.environ['GH_FIXTURE']))
args = sys.argv[1:]
with open(os.environ['GH_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
def answer(status, doc):
    sys.stdout.write(f'HTTP/2.0 {status} Reason\nContent-Type: application/json\r\n\r\n')
    sys.stdout.write(json.dumps(doc))
    sys.exit(0 if status < 300 else 1)
if args[:2] != ['api', '-i'] or len(args) != 3:
    sys.exit(f'gh stub: unexpected call {args}')
path = args[2]
m = re.fullmatch(r'installation/repositories\?per_page=(\d+)&page=(\d+)', path)
if m:
    size, page = int(m[1]), int(m[2])
    rows = fx['repos'][(page - 1) * size : page * size]
    answer(200, {'total_count': len(fx['repos']), 'repositories': rows})
m = re.fullmatch(r'repos/cplieger/([^/]+)/git/trees/([^?]+)\?recursive=[01]', path)
if m:
    if f'{m[1]}@{m[2]}' in fx.get('slow', []):
        time.sleep(0.5)
    paths = fx['trees'].get(f'{m[1]}@{m[2]}')
    if paths is None:
        answer(404, {'message': 'Not Found'})
    answer(200, {'tree': [{'path': p} for p in paths]})
m = re.fullmatch(r'repos/cplieger/([^/]+)/tags\?per_page=100&page=(\d+)', path)
if m:
    if m[1] in fx.get('tags_fail', []):
        answer(403, {'message': 'Resource not accessible'})
    if m[1] in fx.get('tags_raw', {}):
        answer(200, fx['tags_raw'][m[1]])
    names = fx['tags'].get(m[1], [])
    page = int(m[2])
    answer(200, [{'name': n} for n in names[(page - 1) * 100 : page * 100]])
sys.exit(f'gh stub: unexpected call {args}')
"""


def repo(name, branch='main', *, archived=False, fork=False, visibility='public'):
    return {
        'name': name,
        'archived': archived,
        'fork': fork,
        'language': None,
        'default_branch': branch,
        'visibility': visibility,
    }


# Eight repos reaching every single-branch group, plus the ones discovery drops.
# No tags: a single-branch repo's are never read.
MAIN_FIXTURE = {
    'repos': [
        repo('ci'),
        repo('goapp'),
        repo('tslib'),
        repo('gohybrid'),
        repo('shellimg'),
        repo('infra'),
        repo('tool-catalog'),
        repo('pytool'),
        repo('glyphs'),
        repo('unreadable'),
        repo('old', archived=True),
        repo('upstream', fork=True),
    ],
    'trees': {
        'goapp@HEAD': ['go.mod', 'Dockerfile', 'static-src', 'tests/image-smoke.conf'],
        'tslib@HEAD': ['jsr.json', 'package.json', 'src/index.ts'],
        'gohybrid@HEAD': ['go.mod', 'internal/server/static-src/app.ts'],
        'shellimg@HEAD': ['Dockerfile', 'entrypoint.sh', 'tests/shell/run.sh'],
        'infra@HEAD': ['apps/x/compose.yaml', 'tests/shell/run.sh'],
        'tool-catalog@HEAD': ['catalog.json', '.github/workflows/publish.yaml'],
        'pytool@HEAD': ['pyproject.toml'],
        'glyphs@HEAD': ['pyproject.toml', '.github/workflows/publish.yaml'],
    },
    'tags': {},
}


def two_branch_fixture():
    """MAIN_FIXTURE plus two repos whose default branch is dev, and
    tool-catalog switched to a dev default (a single-main repo all the same)."""
    fx = copy.deepcopy(MAIN_FIXTURE)
    for entry in fx['repos']:
        if entry['name'] == 'tool-catalog':
            entry['default_branch'] = 'dev'
    fx['repos'] += [repo('edgeapp', 'dev'), repo('edgelib', 'dev')]
    fx['trees'].update(
        {
            'edgeapp@dev': ['go.mod', 'Dockerfile', 'main.go', 'tests/image-smoke.conf'],
            'edgeapp@main': ['go.mod', 'Dockerfile', 'main.go'],
            'edgelib@dev': ['jsr.json', 'package.json'],
        }
    )
    fx['tags'].update({'edgeapp': ['v1.4.0-dev.2', 'v1.3.0']})
    return fx


def load_classify():
    spec = importlib.util.spec_from_file_location('classify_repos', SCRIPTS / 'classify-repos.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StubGh:
    """A temp dir with the gh stub on PATH; `env()` for a subprocess,
    `patched()` for an in-process call."""

    def __init__(self, fixture):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        (self.dir / 'bin').mkdir()
        stub = self.dir / 'bin' / 'gh'
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.fixture = self.dir / 'fixture.json'
        self.fixture.write_text(json.dumps(fixture))
        self.log = self.dir / 'gh.log'

    def env(self):
        return {
            **os.environ,
            'PATH': f'{self.dir / "bin"}{os.pathsep}{os.environ["PATH"]}',
            'GH_FIXTURE': str(self.fixture),
            'GH_LOG': str(self.log),
        }

    def patched(self):
        return unittest.mock.patch.dict(os.environ, self.env())

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def close(self):
        self.tmp.cleanup()


def run_classify(script, fixture):
    """(returncode, stdout, stderr, gh argv list) of `script` over `fixture`."""
    gh = StubGh(fixture)
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            env=gh.env(),
            cwd=pathlib.Path(script).parent,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr, gh.calls()
    finally:
        gh.close()


def calls_by_repo(calls):
    """{repo name: its calls, in order} of a gh argv list."""
    out = {}
    for call in calls:
        out.setdefault(call[-1].split('/')[2], []).append(call)
    return out


def manifest_dests(text):
    dests = set()
    for group in yaml.safe_load(text)['group']:
        for entry in group['files']:
            dests.add(entry if isinstance(entry, str) else entry['dest'])
    return dests


def groups_by_base(text):
    """{(comment, base): [repo names]} of a rendered manifest."""
    doc = yaml.safe_load(text)
    comments = [line.strip()[2:] for line in text.splitlines() if line.startswith('  # ')]
    out = {}
    for comment, group in zip(comments, doc['group'], strict=True):
        names = [r.split('/')[-1] for r in group['repos'].split()]
        out[comment, group.get('base')] = names
    return out


class MainDefault(unittest.TestCase):
    def setUp(self):
        self.golden = (TESTDATA / 'main-default.yml').read_text()

    def test_the_manifest_of_main_default_repos_is_unchanged(self):
        rc, out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', MAIN_FIXTURE)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, self.golden)
        self.assertNotIn('base:', out)

    def test_a_single_branch_repo_gets_no_release_pipeline_and_no_tags_read(self):
        fx = copy.deepcopy(MAIN_FIXTURE)
        fx['tags_fail'] = [entry['name'] for entry in fx['repos']]
        rc, out, err, calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        self.assertEqual(rc, 0, err)
        self.assertEqual([c for c in calls if '/tags?' in c[-1]], [])
        dests = manifest_dests(out)
        self.assertNotIn('.github/workflows/release.yaml', dests)
        self.assertNotIn('cliff.toml', dests)
        self.assertIn('.github/workflows/ci.yaml', dests)
        self.assertNotIn('cliff=', err)
        self.assertNotIn('release=', err)

    def test_the_api_calls_differ_only_by_the_rest_transport(self):
        # The golden records the gh-repo-list argv; the REST transport differs
        # from it only in the listing call and the `-i` on every read. Repos are
        # read concurrently, so only each repo's own calls keep an order.
        _rc, _out, _err, calls = run_classify(SCRIPTS / 'classify-repos.py', MAIN_FIXTURE)
        golden = [
            json.loads(line) for line in (TESTDATA / 'main-default.calls').read_text().splitlines()
        ]
        listing = ['api', '-i', 'installation/repositories?per_page=100&page=1']
        want = [listing if c[:2] == ['repo', 'list'] else ['api', '-i', *c[1:]] for c in golden]
        self.assertEqual(calls[0], listing)
        self.assertEqual(calls_by_repo(calls[1:]), calls_by_repo(want[1:]))
        self.assertEqual(len(calls), len(want))

    def test_one_tree_read_per_repo_and_base(self):
        _rc, _out, _err, calls = run_classify(SCRIPTS / 'classify-repos.py', two_branch_fixture())
        trees = [c[-1] for c in calls if '/git/trees/' in c[-1]]
        self.assertEqual(len(trees), len(set(trees)), trees)
        self.assertTrue(all(t.endswith('?recursive=1') for t in trees), trees)

    def test_the_log_keeps_the_repo_order_however_the_reads_finish(self):
        fx = two_branch_fixture()
        fx['slow'] = ['edgeapp@dev']
        _rc, _out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        names = [line.split()[1] for line in err.splitlines() if 'classified:' in line]
        self.assertEqual(names, sorted(names))
        self.assertEqual(names.count('edgeapp'), 2)

    def test_a_failed_two_branch_tags_read_aborts_with_the_repo_named(self):
        fx = two_branch_fixture()
        fx['tags_fail'] = ['edgeapp']
        rc, out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('classify-repos: tags read failed for edgeapp; aborting', err)

    def test_a_truncated_two_branch_tags_listing_aborts(self):
        fx = two_branch_fixture()
        fx['tags']['edgeapp'] = [f'v1.0.0-dev.{n}' for n in range(1, 2001)]
        rc, out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('classify-repos: tags listing of edgeapp truncated', err)


class EslintBaseDest(unittest.TestCase):
    """The ESLint base lands beside the npm project its eslint.config.mjs sits in."""

    def manifest(self, trees):
        fx = {
            'repos': [repo(name.split('@')[0]) for name in trees],
            'trees': trees,
            'tags': {},
        }
        rc, out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        self.assertEqual(rc, 0, err)
        return yaml.safe_load(out)['group']

    def base_dests(self, groups):
        """{repo name: ESLint base dest} over the manifest's groups."""
        out = {}
        for group in groups:
            for entry in group['files']:
                if isinstance(entry, dict) and entry['source'] == 'configs/eslint.config.base.mjs':
                    for name in group['repos'].split():
                        out[name.split('/')[-1]] = entry['dest']
        return out

    def test_a_nested_npm_project_gets_the_base_in_its_folder_and_the_rest_at_the_root(self):
        groups = self.manifest(
            {
                'rootts@HEAD': ['package.json', 'src/a.ts'],
                'webgo@HEAD': ['go.mod', 'web', 'web/package.json', 'web/src/a.ts'],
                'srcgo@HEAD': ['go.mod', 'static-src', 'static-src/package.json'],
                'deepgo@HEAD': ['go.mod', 'internal/server/static-src/package.json'],
            }
        )
        self.assertEqual(
            self.base_dests(groups),
            {
                'rootts': 'eslint.config.base.mjs',
                'webgo': 'web/eslint.config.base.mjs',
                'srcgo': 'static-src/eslint.config.base.mjs',
                'deepgo': 'internal/server/static-src/eslint.config.base.mjs',
            },
        )
        webgo = next(g for g in groups if 'web/eslint.config.base.mjs' in str(g['files']))
        self.assertIn(
            {'source': 'configs/prettier.json', 'dest': '.prettierrc.json'}, webgo['files']
        )

    def half_dests(self, groups):
        """{repo name: [ESLint base dests]} over the manifest's groups."""
        out = {}
        for group in groups:
            for entry in group['files']:
                if isinstance(entry, dict) and entry['source'] == 'configs/eslint.config.base.mjs':
                    for name in group['repos'].split():
                        out.setdefault(name.split('/')[-1], []).append(entry['dest'])
        return out

    def test_each_module_half_gets_the_base_and_the_root_gets_none(self):
        halves = [
            *('alpha/go.mod', 'alpha/a.go', 'alpha/web/package.json', 'alpha/web/go.mod'),
            *('beta/go.mod', 'beta/b.go'),
            *('gamma/go.mod', 'gamma/g.go', 'gamma/web/jsr.json'),
        ]
        rc, out, err, _calls = run_classify(
            SCRIPTS / 'classify-repos.py', rootless_fixture(['README.md', *halves])
        )
        self.assertEqual(rc, 0, err)
        groups = [g for g in yaml.safe_load(out)['group'] if 'lanes' in g['repos']]
        want = ['alpha/web/eslint.config.base.mjs', 'gamma/web/eslint.config.base.mjs']
        self.assertEqual(self.half_dests(groups), {'lanes': want + want})
        lanes = next(g for g in groups if 'alpha/web/eslint.config.base.mjs' in str(g['files']))
        self.assertIn(
            {'source': 'configs/prettier.json', 'dest': '.prettierrc.json'}, lanes['files']
        )

    def test_a_root_project_and_module_halves_each_get_the_base(self):
        half = ['alpha/go.mod', 'alpha/a.go', 'alpha/web/package.json']
        groups = self.manifest(
            {
                'mixed@HEAD': ['go.mod', 'main.go', 'package.json', 'mod.ts', *half],
                'jsrmixed@HEAD': ['go.mod', 'main.go', 'jsr.json', 'mod.ts', *half],
            }
        )
        want = ['eslint.config.base.mjs', 'alpha/web/eslint.config.base.mjs']
        self.assertEqual(self.half_dests(groups), {'mixed': want, 'jsrmixed': want})

    def test_a_root_project_and_a_web_folder_each_get_the_base(self):
        # ci.yaml's web job probes WEB_DIRS whatever the root holds, so both are linted.
        tree = ['package.json', 'mod.ts', 'web/package.json', 'web/app.ts']
        groups = self.manifest({'both@HEAD': tree})
        self.assertEqual(
            self.half_dests(groups),
            {'both': ['eslint.config.base.mjs', 'web/eslint.config.base.mjs']},
        )

    def test_halves_follow_go_nested_at_any_depth(self):
        # go-nested validates a module at any depth whose .go files may sit in a subdirectory.
        tree = ['go.mod', 'main.go', 'pkg/mods/x/go.mod', 'pkg/mods/x/x.go']
        tree += ['pkg/mods/x/web/package.json', 'beta/go.mod', 'beta/internal/b.go']
        tree += ['beta/web/jsr.json']
        self.assertEqual(load_classify().nested_halves(tree), ['beta/web', 'pkg/mods/x/web'])

    def test_a_truncated_tree_reads_as_unreadable(self):
        classify = load_classify()
        listing = {'tree': [{'path': 'go.mod'}], 'truncated': True}
        with unittest.mock.patch.object(classify, 'api_json', return_value=listing):
            self.assertIsNone(classify.tree_paths('big'))
        with unittest.mock.patch.object(classify, 'api_json', return_value={'tree': []}):
            self.assertEqual(classify.tree_paths('empty'), [])

    def test_a_module_name_go_nested_refuses_is_no_half(self):
        # ci.yaml fails on a module dir outside [A-Za-z0-9._/-]; such a name must never
        # reach a sync.yml dest, where ` #` would cut it short.
        odd = ['go.mod', 'main.go', 'a #b/go.mod', 'a #b/x.go', 'a #b/web/package.json']
        self.assertEqual(load_classify().nested_halves(odd), [])
        spaced = ['go.mod', 'main.go', 'a b/go.mod', 'a b/x.go', 'a b/web/package.json']
        self.assertEqual(load_classify().nested_halves(spaced), [])
        groups = self.manifest({'oddname@HEAD': odd})
        self.assertNotIn('oddname', self.half_dests(groups))

    def test_the_typescript_groups_print_single_folders_then_sets_then_no_base(self):
        half = ['alpha/go.mod', 'alpha/a.go', 'alpha/web/package.json']
        groups = self.manifest(
            {
                'zroot@HEAD': ['package.json', 'mod.ts'],
                'ytwo@HEAD': [
                    'go.mod',
                    'main.go',
                    *half,
                    'beta/go.mod',
                    'beta/b.go',
                    'beta/web/jsr.json',
                ],
                'xone@HEAD': ['go.mod', 'main.go', *half],
                'wnone@HEAD': ['go.mod', 'main.go', 'tools/web/package.json'],
            }
        )
        ts = [g for g in groups if 'configs/prettier.json' in str(g['files'])]
        self.assertEqual(
            [g['repos'].split()[0].split('/')[-1] for g in ts], ['zroot', 'xone', 'ytwo', 'wnone']
        )

    def test_npm_projects_only_in_other_folders_get_no_base(self):
        # dist/x is pruned and the tools module holds no Go source, so go-nested skips both
        # and neither web folder is a half; no lint step reads a base beside them, and a
        # testdata project is no project.
        odd = ['go.mod', 'main.go', 'dist/x/go.mod', 'dist/x/x.go', 'dist/x/web/package.json']
        odd += ['tools/go.mod', 'tools/web/package.json']
        groups = self.manifest(
            {
                'odd@HEAD': odd,
                'fixtures@HEAD': ['go.mod', 'web', 'web/app.ts', 'testdata/x/package.json'],
            }
        )
        self.assertEqual(self.half_dests(groups), {'fixtures': ['eslint.config.base.mjs']})
        prettier = {'source': 'configs/prettier.json', 'dest': '.prettierrc.json'}
        self.assertTrue(any('odd' in g['repos'] and prettier in g['files'] for g in groups), groups)

    def test_a_web_folder_without_an_npm_project_keeps_the_base_at_the_root(self):
        groups = self.manifest({'gohybrid@HEAD': ['go.mod', 'internal/server/static-src/app.ts']})
        self.assertEqual(self.base_dests(groups), {'gohybrid': 'eslint.config.base.mjs'})

    def test_the_folders_are_the_ones_ci_yaml_probes_in_its_order(self):
        text = (ROOT / '.github' / 'workflows' / 'ci.yaml').read_text()
        loop = text.split('# Probe known nested web-frontend locations.', 1)[1].split('; do', 1)[0]
        self.assertEqual(tuple(re.findall(r'"([^"]+)" \\', loop)), load_classify().WEB_DIRS)


class Discovery(unittest.TestCase):
    """The REST repo listing: every page read, a failure or timeout aborts."""

    def test_a_listing_longer_than_a_page_is_read_whole(self):
        fx = copy.deepcopy(MAIN_FIXTURE)
        fx['repos'] += [repo(f'zz{n:03d}') for n in range(120)]
        classify = load_classify()
        gh = StubGh(fx)
        try:
            with gh.patched():
                names = [r['name'] for r in classify.discover_repos()]
            calls = gh.calls()
        finally:
            gh.close()
        self.assertEqual(len(names), 130)
        self.assertNotIn('old', names)
        self.assertNotIn('upstream', names)
        self.assertEqual([c[-1].rsplit('=', 1)[-1] for c in calls], ['1', '2'])

    def test_a_failed_listing_aborts_with_the_error(self):
        classify = load_classify()
        refused = subprocess.CompletedProcess(
            ['gh'], 1, b'HTTP/2.0 401 Unauthorized\n\r\n{"message": "Bad credentials"}', b''
        )
        classify.REST = classify.ghrest.Client(run=lambda *_: refused, sleep=lambda _s: None)
        with self.assertRaises(SystemExit) as caught:
            classify.discover_repos()
        self.assertIn('repo listing failed', str(caught.exception.code))
        self.assertIn('HTTP 401 Bad credentials', str(caught.exception.code))

    def test_the_client_holds_on_the_shared_pacers_rate_limit_pause(self):
        classify = load_classify()
        self.assertEqual(classify.REST.pause, classify.PACER.pause)

    def test_every_call_is_admitted_by_the_pacer_as_a_read(self):
        import ghrest

        ok = subprocess.CompletedProcess(['gh'], 0, b'HTTP/2.0 200 OK\n\r\n{}', b'')
        with unittest.mock.patch.object(ghrest, 'run_process', lambda *_: ok):
            classify = load_classify()
        with unittest.mock.patch.object(classify.PACER, 'wait') as wait:
            self.assertEqual(classify.REST.get('repos/cplieger/goapp'), {})
        self.assertEqual([c.args for c in wait.call_args_list], [(False,)])

    def test_the_repos_are_read_on_the_default_pool_of_several_workers(self):
        classify = load_classify()
        seen = []

        def recorder(_items, _work, *workers, **kw):
            seen.append((workers, kw))
            return iter(())

        with (
            unittest.mock.patch.object(classify, 'discover_repos', return_value=[]),
            unittest.mock.patch.object(classify.fanout, 'ordered', recorder),
            unittest.mock.patch('sys.stdout', io.StringIO()),
        ):
            classify.main()
        self.assertEqual(seen, [((), {})])
        self.assertGreater(classify.fanout.ordered.__defaults__[0], 1)

    def test_a_listing_that_times_out_exits_124(self):
        classify = load_classify()

        def expire(*_):
            raise subprocess.TimeoutExpired(['gh'], classify.TIMEOUT)

        slept = []
        classify.REST = classify.ghrest.Client(run=expire, sleep=slept.append, timeout=10)
        with self.assertRaises(SystemExit) as caught:
            classify.discover_repos()
        self.assertEqual(caught.exception.code, 124)
        self.assertEqual(slept, [2, 4, 8])


class TwoBranch(unittest.TestCase):
    def setUp(self):
        self.golden = (TESTDATA / 'main-default.yml').read_text()
        rc, self.out, err, self.calls = run_classify(
            SCRIPTS / 'classify-repos.py', two_branch_fixture()
        )
        self.assertEqual(rc, 0, err)
        self.groups = groups_by_base(self.out)

    def test_main_default_groups_come_first_and_are_unchanged(self):
        self.assertTrue(self.out.startswith(self.golden), self.out)
        for (_comment, base), names in self.groups.items():
            if base is None:
                self.assertNotIn('edgeapp', names)
                self.assertNotIn('edgelib', names)

    def test_a_single_main_repo_with_a_dev_default_is_classified_from_head(self):
        trees = [c[-1] for c in self.calls if 'tool-catalog' in c[-1]]
        self.assertTrue(trees)
        self.assertTrue(all('/git/trees/HEAD?' in t for t in trees if '/git/trees/' in t))
        for (_comment, base), names in self.groups.items():
            if base is not None:
                self.assertNotIn('tool-catalog', names)

    def test_each_base_is_classified_from_its_own_tree(self):
        smoke = 'Image-smoke harness (repos with a tests/image-smoke.conf opt-in)'
        self.assertEqual(self.groups[f'{smoke} (base: dev)', 'dev'], ['edgeapp'])
        self.assertNotIn((f'{smoke} (base: main)', 'main'), self.groups)
        ci = 'Unified CI (auto-detects go/ts/web/shell surfaces)'
        self.assertEqual(self.groups[f'{ci} (base: dev)', 'dev'], ['edgeapp', 'edgelib'])
        self.assertEqual(self.groups[f'{ci} (base: main)', 'main'], ['edgeapp'])
        refs = {
            c[-1].split('/git/trees/')[1].split('?')[0]
            for c in self.calls
            if '/edgeapp/git/trees/' in c[-1]
        }
        self.assertEqual(refs, {'dev', 'main'})

    def test_the_tags_are_read_once_and_set_both_bases_tier(self):
        reads = [c for c in self.calls if '/edgeapp/tags' in c[-1]]
        self.assertEqual(len(reads), 1)
        stable = 'Cliff config (stable — v1.x+)'
        for base in ('dev', 'main'):
            self.assertIn('edgeapp', self.groups[f'{stable} (base: {base})', base])

    def test_every_readable_base_gets_the_two_branch_renovate_file(self):
        renovate = 'Two-branch Renovate delta (repos whose default branch is dev)'
        self.assertEqual(self.groups[f'{renovate} (base: dev)', 'dev'], ['edgeapp', 'edgelib'])
        self.assertEqual(self.groups[f'{renovate} (base: main)', 'main'], ['edgeapp'])
        doc = yaml.safe_load(self.out)
        files = [g['files'] for g in doc['group'] if g.get('base') and 'edgelib' in g['repos']]
        self.assertIn(
            [{'source': 'configs/renovate-two-branch.json', 'dest': 'renovate.json'}], files
        )
        self.assertEqual(
            (ROOT / 'configs' / 'renovate-two-branch.json').read_text(),
            '{ "extends": ["github>cplieger/.github:two-branch"] }\n',
        )

    def test_the_sync_engine_reads_one_target_per_base(self):
        spec = importlib.util.spec_from_file_location('sync_files', SCRIPTS / 'sync-files.py')
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / 'sync.yml'
            manifest.write_text(self.out)
            mapping = sync.load_mapping(manifest)
        dev = mapping['cplieger/edgeapp', 'dev']
        main = mapping['cplieger/edgeapp', 'main']
        self.assertEqual(dev['tests/image-smoke.sh'], 'configs/image-smoke.sh')
        self.assertNotIn('tests/image-smoke.sh', main)
        self.assertEqual(main['renovate.json'], 'configs/renovate-two-branch.json')
        self.assertNotIn(('cplieger/edgeapp', None), mapping)
        self.assertNotIn(('cplieger/edgelib', 'main'), mapping)
        self.assertIn(('cplieger/goapp', None), mapping)


class PrivateOnDev(unittest.TestCase):
    def test_a_private_dev_default_repo_is_classified_from_head_like_any_other(self):
        fx = copy.deepcopy(MAIN_FIXTURE)
        fx['repos'].append(repo('secret', 'dev', visibility='private'))
        fx['trees']['secret@HEAD'] = ['go.mod', 'Dockerfile', 'main.go']
        rc, out, err, calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        self.assertEqual(rc, 0, err)
        self.assertNotIn('base:', out)
        trees = [c[-1] for c in calls if '/secret/git/trees/' in c[-1]]
        self.assertTrue(trees)
        self.assertTrue(all('/git/trees/HEAD?' in t for t in trees), trees)
        ci = 'Unified CI (auto-detects go/ts/web/shell surfaces)'
        self.assertIn('secret', groups_by_base(out)[ci, None])


ROOTLESS_LANES = [
    'README.md',
    'alpha',
    'alpha/go.mod',
    'alpha/alpha.go',
    'beta',
    'beta/go.mod',
    'beta/cmd/beta/main.go',
]
WEB_HALF = ['alpha/web', 'alpha/web/package.json', 'alpha/web/jsr.json', 'alpha/web/go.mod']


def rootless_fixture(trees, branch='dev', name='lanes'):
    """MAIN_FIXTURE plus one repo whose bases (or HEAD) hold `trees`."""
    fx = copy.deepcopy(MAIN_FIXTURE)
    fx['repos'].append(repo(name, branch))
    refs = ('dev', 'main') if branch == 'dev' else ('HEAD',)
    fx['trees'].update({f'{name}@{ref}': trees for ref in refs})
    fx['tags'][name] = ['alpha/v1.1.0', 'alpha/v1.2.0-dev.1']
    return fx


def groups_of(out, name):
    """{base: sorted group comments naming `name`}, base None for single-branch."""
    found = {}
    for (comment, base), names in groups_by_base(out).items():
        if name in names:
            found.setdefault(base, []).append(comment.split(' (base: ')[0])
    return {base: sorted(comments) for base, comments in found.items()}


class RootLessGo(unittest.TestCase):
    """A repo with no root go.mod whose product is its nested Go modules."""

    CI = 'Unified CI (auto-detects go/ts/web/shell surfaces)'
    GOLANGCI = 'Go-tooling configs (Go-having repos)'
    TS = 'TypeScript lint/format configs'
    RELEASE = 'Release (unified auto-detect)'
    CLIFF = 'Cliff config (stable — v1.x+)'
    RENOVATE = 'Two-branch Renovate delta (repos whose default branch is dev)'

    def classify(self, fx, name='lanes'):
        rc, out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        self.assertEqual(rc, 0, err)
        return groups_of(out, name), err

    def test_two_branch_nested_modules_with_a_web_half_release_as_go(self):
        groups, err = self.classify(rootless_fixture(ROOTLESS_LANES + WEB_HALF))
        ts = f'{self.TS} (npm project in alpha/web)'
        want = sorted([self.CI, self.GOLANGCI, ts, self.RELEASE, self.CLIFF, self.RENOVATE])
        self.assertEqual(groups, {'dev': want, 'main': want})
        for base in ('dev', 'main'):
            self.assertRegex(
                err,
                rf'classified: lanes +lang=go +web=true +cliff=stable +release=true base={base}',
            )

    def test_without_a_web_half_no_ts_configs_sync(self):
        groups, _err = self.classify(rootless_fixture(ROOTLESS_LANES))
        want = sorted([self.CI, self.GOLANGCI, self.RELEASE, self.CLIFF, self.RENOVATE])
        self.assertEqual(groups, {'dev': want, 'main': want})

    def test_vendored_testdata_and_sentinel_modules_do_not_count(self):
        trees = [
            'README.md',
            'vendor/x/go.mod',
            'vendor/x/x.go',
            'tools/testdata/m/go.mod',
            'tools/testdata/m/m.go',
            'site/node_modules/y/go.mod',
            'site/node_modules/y/y.go',
            *WEB_HALF,
        ]
        groups, err = self.classify(rootless_fixture(trees))
        self.assertEqual(groups, {'dev': [self.RENOVATE], 'main': [self.RENOVATE]})
        self.assertRegex(
            err, r'classified: lanes +lang=none +web=false +cliff=stable +release=false'
        )

    def test_a_single_branch_root_less_repo_keeps_its_own_ci(self):
        groups, err = self.classify(rootless_fixture(ROOTLESS_LANES, branch='main'))
        self.assertEqual(groups, {})
        self.assertRegex(err, r'classified: lanes +lang=none +web=false\n')

    def test_a_root_module_repo_is_classified_as_before(self):
        trees = ['go.mod', 'main.go', 'sub/go.mod', 'sub/sub.go']
        groups, _err = self.classify(rootless_fixture(trees))
        want = sorted([self.CI, self.GOLANGCI, self.RELEASE, self.CLIFF, self.RENOVATE])
        self.assertEqual(groups, {'dev': want, 'main': want})


class WrongShapeTags(unittest.TestCase):
    """A tags response that parses but is not a list of named tag objects."""

    SHAPES = (
        {'message': 'not a list'},
        ['v1.3.0'],
        [{'name': 'v1.3.0'}, {'commit': {}}],
        [{'name': 7}],
        [{'name': ''}],
    )

    def test_a_two_branch_sync_aborts_with_the_repo_named(self):
        for body in self.SHAPES:
            with self.subTest(body=body):
                fx = two_branch_fixture()
                fx['tags_raw'] = {'edgeapp': body}
                rc, out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
                self.assertEqual(rc, 1)
                self.assertEqual(out, '')
                self.assertIn('classify-repos: tags read failed for edgeapp; aborting', err)

    def test_a_main_default_repo_never_reads_it(self):
        fx = copy.deepcopy(MAIN_FIXTURE)
        fx['tags_raw'] = {'tslib': {'message': 'not a list'}}
        rc, _out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        self.assertEqual(rc, 0, err)
        self.assertRegex(err, r'classified: tslib +lang=ts +web=false\n')

    def test_the_promotion_sources_raise(self):
        classify = load_classify()
        for body in self.SHAPES:
            with self.subTest(body=body):
                gh = StubGh({'repos': [], 'trees': {}, 'tags': {}, 'tags_raw': {'a': body}})
                try:
                    with gh.patched(), self.assertRaises(classify.ClassifyError) as caught:
                        classify.canonical_sources('a')
                finally:
                    gh.close()
                self.assertIn('tags read failed for a', str(caught.exception))


class SyncOwnedSet(unittest.TestCase):
    def setUp(self):
        self.classify = load_classify()

    def test_it_is_the_static_set_of_every_dest(self):
        self.assertEqual(
            self.classify.sync_owned_patterns(),
            [
                '**/web/eslint.config.base.mjs',
                '.editorconfig',
                '.gitattributes',
                '.github/workflows/ci.yaml',
                '.github/workflows/codeql.yml',
                '.github/workflows/release.yaml',
                '.github/workflows/security.yml',
                '.golangci.yaml',
                '.gremlins.yaml',
                '.htmlvalidate.json',
                '.prettierrc.json',
                '.stylelintrc.json',
                'cliff.toml',
                'eslint.config.base.mjs',
                'internal/server/static-src/eslint.config.base.mjs',
                'internal/webui/static-src/eslint.config.base.mjs',
                'renovate.json',
                'ruff.toml',
                'scripts/collect-licenses.sh',
                'scripts/repin-sha.sh',
                'static-src/eslint.config.base.mjs',
                'tests/image-smoke.sh',
                'tests/shell/harness_test.sh',
                'tests/shell/lib.sh',
                'web/eslint.config.base.mjs',
            ],
        )

    def test_it_covers_every_dest_a_manifest_writes_and_reads_no_api(self):
        _rc, out, _err, _calls = run_classify(SCRIPTS / 'classify-repos.py', two_branch_fixture())
        with unittest.mock.patch.object(subprocess, 'run', side_effect=AssertionError('API read')):
            owned = [glob_pattern(g) for g in self.classify.sync_owned_patterns()]
        for dest in manifest_dests(out):
            self.assertTrue(any(p.fullmatch(dest) for p in owned), dest)

    def test_canonical_sources_resolve_cliff_by_tier_and_exist_here(self):
        trees = {f'{r}@HEAD': ['go.mod', 'main.go'] for r in 'abc'}
        fx = {'repos': [], 'trees': trees, 'tags': {'a': ['v0.4.1'], 'b': ['v2.0.0']}}
        gh = StubGh(fx)
        try:
            with gh.patched():
                alpha = self.classify.canonical_sources('a')
                stable = self.classify.canonical_sources('cplieger/b')
                untagged = self.classify.canonical_sources('c')
                with self.assertRaisesRegex(self.classify.ClassifyError, 'tree read failed for d'):
                    self.classify.canonical_sources('d')
        finally:
            gh.close()
        self.assertEqual(alpha['cliff.toml'], 'configs/cliff-alpha.toml')
        self.assertEqual(stable['cliff.toml'], 'configs/cliff-stable.toml')
        self.assertEqual(untagged['cliff.toml'], 'configs/cliff-stable.toml')
        static = [p for p in self.classify.sync_owned_patterns() if '*' not in p]
        self.assertEqual(sorted(alpha), static)
        for source in {**alpha, **stable}.values():
            self.assertTrue((ROOT / source).is_file(), source)
        self.assertEqual(alpha['renovate.json'], 'configs/renovate-two-branch.json')
        self.assertEqual(alpha['.github/workflows/ci.yaml'], '.github/workflow-templates/ci.yml')

    def test_canonical_sources_add_each_module_half(self):
        trees = ['alpha/go.mod', 'alpha/a.go', 'alpha/web/package.json', 'beta/go.mod']
        fx = {'repos': [], 'trees': {'lanes@HEAD': trees}, 'tags': {'lanes': ['v1.0.0']}}
        gh = StubGh(fx)
        try:
            with gh.patched():
                sources = self.classify.canonical_sources('cplieger/lanes')
        finally:
            gh.close()
        self.assertEqual(
            sources['alpha/web/eslint.config.base.mjs'], 'configs/eslint.config.base.mjs'
        )
        self.assertNotIn('beta/web/eslint.config.base.mjs', sources)
        owned = [glob_pattern(g) for g in self.classify.sync_owned_patterns()]
        self.assertTrue(all(any(p.fullmatch(d) for p in owned) for d in sources), sources)

    def test_canonical_sources_raise_on_a_failed_tags_read(self):
        gh = StubGh({'repos': [], 'trees': {}, 'tags': {}, 'tags_fail': ['a']})
        try:
            with gh.patched(), self.assertRaises(self.classify.ClassifyError) as caught:
                self.classify.canonical_sources('a')
        finally:
            gh.close()
        self.assertIn('tags read failed for a', str(caught.exception))

    def test_groups_that_share_a_dest_agree_on_its_source_except_cliff(self):
        seen = {}
        for _key, group in self.classify.GROUPS:
            if group in self.classify.CLIFF.values():
                continue
            for source, dest in self.classify.pairs(group):
                self.assertEqual(seen.setdefault(dest, source), source, dest)


class LocalTree(unittest.TestCase):
    """classify_tree and synced_dests answer from a tree listing alone, as a local clone
    gives one, and agree with the manifest the API-read classification prints."""

    @classmethod
    def setUpClass(cls):
        cls.classify = load_classify()

    def dests(self, tree, releasing=False):
        return self.classify.synced_dests(self.classify.classify_tree(tree, releasing=releasing))

    def test_the_tree_classifier_is_what_classify_reads(self):
        tree = ['go.mod', 'main.go', 'web', 'web/package.json', 'Dockerfile']
        with unittest.mock.patch.object(self.classify, 'tree_paths', return_value=tree):
            for releasing in (False, True):
                self.assertEqual(
                    self.classify.classify('x', releasing=releasing),
                    self.classify.classify_tree(tree, releasing=releasing),
                )
        with unittest.mock.patch.object(self.classify, 'api_json', side_effect=AssertionError):
            self.classify.classify_tree(tree)

    def test_a_files_only_listing_profiles_like_the_trees_listing(self):
        for files in (
            ['go.mod', 'main.go', 'web/package.json', 'web/a.ts'],
            ['go.mod', 'main.go', 'web/index.ts'],
            ['go.mod', 'main.go', 'static-src/app.ts'],
            ['go.mod', 'main.go', 'web/static/app.js'],
            ['a/go.mod', 'a/a.go', 'a/web/package.json'],
        ):
            listed = files + sorted(
                {f.rsplit('/', depth)[0] for f in files for depth in range(1, f.count('/') + 1)}
            )
            for releasing in (False, True):
                self.assertEqual(
                    self.classify.classify_tree(files, releasing=releasing),
                    self.classify.classify_tree(listed, releasing=releasing),
                    files,
                )
        self.assertIn('.prettierrc.json', self.dests(['go.mod', 'main.go', 'web/package.json']))
        self.assertIn('.prettierrc.json', self.dests(['go.mod', 'main.go', 'web/static/app.js']))

    def test_an_unreadable_tree_gets_nothing(self):
        for releasing in (False, True):
            self.assertEqual(self.dests([], releasing), [])

    def test_a_single_branch_go_library_gets_ci_and_lint_but_no_release(self):
        dests = self.dests(['go.mod', 'lib.go'])
        self.assertIn('.github/workflows/ci.yaml', dests)
        self.assertIn('.golangci.yaml', dests)
        for absent in ('.github/workflows/release.yaml', 'cliff.toml', 'renovate.json'):
            self.assertNotIn(absent, dests)

    def test_a_two_branch_module_repo_gets_release_renovate_and_the_half_bases(self):
        tree = ['README.md', 'a/go.mod', 'a/a.go', 'b/go.mod', 'b/b.go']
        tree += ['b/web/package.json', 'b/web/go.mod']
        dests = self.dests(tree, releasing=True)
        for present in (
            '.github/workflows/release.yaml',
            'cliff.toml',
            'renovate.json',
            'b/web/eslint.config.base.mjs',
            '.prettierrc.json',
        ):
            self.assertIn(present, dests)
        self.assertNotIn('eslint.config.base.mjs', dests)
        self.assertEqual(self.dests(tree), [])

    def test_a_publishing_repo_gets_the_artifact_ci(self):
        dests = self.dests(['data.json', '.github/workflows/publish.yaml'], releasing=True)
        artifact = sorted(dest for _s, dest in self.classify.pairs(self.classify.ARTIFACT_CI))
        self.assertTrue(set(artifact) <= set(dests), (artifact, dests))
        self.assertNotIn('.github/workflows/release.yaml', dests)

    def test_the_dests_match_the_manifest_for_every_base(self):
        fx = two_branch_fixture()
        rc, out, err, _calls = run_classify(SCRIPTS / 'classify-repos.py', fx)
        self.assertEqual(rc, 0, err)
        want = {}
        for group in yaml.safe_load(out)['group']:
            for name in (r.split('/')[-1] for r in group['repos'].split()):
                want.setdefault((name, group.get('base')), set()).update(
                    entry if isinstance(entry, str) else entry['dest'] for entry in group['files']
                )
        seen = set()
        for row in fx['repos']:
            if row['name'] == 'ci' or row['archived'] or row['fork']:
                continue
            releasing = self.classify.release_channels.is_two_branch(row)
            for base in self.classify.BASES if releasing else (None,):
                key = (row['name'], base)
                tree = fx['trees'].get(f'{row["name"]}@{base or "HEAD"}', [])
                got = self.dests(tree, releasing)
                self.assertEqual(got, sorted(want.get(key, ())), key)
                seen.add(key)
        self.assertEqual(set(want) - seen, set())
        self.assertIn(('edgeapp', 'main'), seen)
        self.assertIn(('tool-catalog', None), seen)


class Consumers(unittest.TestCase):
    """The main intake and the promotion read the set from this module."""

    def test_the_intake_reads_the_published_set(self):
        import intake
        import inventory

        patterns = intake.load_sync_owned(None)
        owned = [p for p in patterns if p.fullmatch('renovate.json')]
        self.assertTrue(owned)
        self.assertTrue(any(p.fullmatch('.github/workflows/release.yaml') for p in patterns))
        self.assertFalse(any(p.fullmatch('go.mod') for p in patterns))
        self.assertIsInstance(patterns[0], type(inventory.glob_pattern('x')))

    def test_the_promotion_writes_the_set_and_the_canonical_copies(self):
        import promote

        half = ['alpha/go.mod', 'alpha/a.go', 'alpha/web/package.json']
        trees = {'edgeapp@HEAD': ['go.mod', 'main.go'], 'halves@HEAD': ['go.mod', 'main.go', *half]}
        tags = {'edgeapp': ['v1.3.0'], 'halves': ['v1.0.0']}
        gh = StubGh({'repos': [], 'trees': trees, 'tags': tags})
        try:
            with tempfile.TemporaryDirectory() as tmp, gh.patched():
                work = pathlib.Path(tmp)
                owned, canonical = promote.sync_inputs('edgeapp', work)
                lines = owned.read_text().splitlines()
                renovate = (canonical / 'renovate.json').read_text()
                cliff = (canonical / 'cliff.toml').read_text()
                owned, canonical = promote.sync_inputs('halves', work)
                half_lines = owned.read_text().splitlines()
                half_base = (canonical / 'alpha/web/eslint.config.base.mjs').read_text()
        finally:
            gh.close()
        # A glob owns only the paths the sync writes in this repo.
        literal = [p for p in load_classify().sync_owned_patterns() if '*' not in p]
        self.assertEqual(lines, literal)
        self.assertEqual(half_lines, [*literal, 'alpha/web/eslint.config.base.mjs'])
        self.assertEqual(half_base, (ROOT / 'configs' / 'eslint.config.base.mjs').read_text())
        self.assertEqual(renovate, (ROOT / 'configs' / 'renovate-two-branch.json').read_text())
        self.assertEqual(cliff, (ROOT / 'configs' / 'cliff-stable.toml').read_text())


if __name__ == '__main__':
    unittest.main()
