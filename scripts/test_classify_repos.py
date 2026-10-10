"""classify-repos.py: the manifest per base, and the sync-owned set the main
intake and the promotion read, driven by a stub `gh` over fixture repos."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import yaml

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
                'renovate.json',
                'ruff.toml',
                'scripts/collect-licenses.sh',
                'scripts/repin-sha.sh',
                'tests/image-smoke.sh',
                'tests/shell/harness_test.sh',
                'tests/shell/lib.sh',
            ],
        )

    def test_it_covers_every_dest_a_manifest_writes_and_reads_no_api(self):
        _rc, out, _err, _calls = run_classify(SCRIPTS / 'classify-repos.py', two_branch_fixture())
        with unittest.mock.patch.object(subprocess, 'run', side_effect=AssertionError('API read')):
            owned = set(self.classify.sync_owned_patterns())
        self.assertLessEqual(manifest_dests(out), owned)

    def test_canonical_sources_resolve_cliff_by_tier_and_exist_here(self):
        fx = {'repos': [], 'trees': {}, 'tags': {'a': ['v0.4.1'], 'b': ['v2.0.0']}}
        gh = StubGh(fx)
        try:
            with gh.patched():
                alpha = self.classify.canonical_sources('a')
                stable = self.classify.canonical_sources('cplieger/b')
                untagged = self.classify.canonical_sources('c')
        finally:
            gh.close()
        self.assertEqual(alpha['cliff.toml'], 'configs/cliff-alpha.toml')
        self.assertEqual(stable['cliff.toml'], 'configs/cliff-stable.toml')
        self.assertEqual(untagged['cliff.toml'], 'configs/cliff-stable.toml')
        self.assertEqual(sorted(alpha), self.classify.sync_owned_patterns())
        for source in {**alpha, **stable}.values():
            self.assertTrue((ROOT / source).is_file(), source)
        self.assertEqual(alpha['renovate.json'], 'configs/renovate-two-branch.json')
        self.assertEqual(alpha['.github/workflows/ci.yaml'], '.github/workflow-templates/ci.yml')

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

        gh = StubGh({'repos': [], 'trees': {}, 'tags': {'edgeapp': ['v1.3.0']}})
        try:
            with tempfile.TemporaryDirectory() as tmp, gh.patched():
                work = pathlib.Path(tmp)
                owned, canonical = promote.sync_inputs('edgeapp', work)
                lines = owned.read_text().splitlines()
                renovate = (canonical / 'renovate.json').read_text()
                cliff = (canonical / 'cliff.toml').read_text()
        finally:
            gh.close()
        self.assertEqual(lines, load_classify().sync_owned_patterns())
        self.assertEqual(renovate, (ROOT / 'configs' / 'renovate-two-branch.json').read_text())
        self.assertEqual(cliff, (ROOT / 'configs' / 'cliff-stable.toml').read_text())


if __name__ == '__main__':
    unittest.main()
