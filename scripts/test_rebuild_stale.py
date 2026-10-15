"""rebuild-stale.sh and rebuild-stale.yaml, driven by stub `gh`, `curl` and `date`
binaries that answer from a fixture and log every call."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
SCRIPT = SCRIPTS / 'rebuild-stale.sh'
WORKFLOW = ROOT / '.github' / 'workflows' / 'rebuild-stale.yaml'

sys.path.insert(0, str(SCRIPTS))
import release_channels  # noqa: E402

_spec = importlib.util.spec_from_file_location('workflow_replay', SCRIPTS / 'workflow_replay.py')
workflow_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workflow_replay)

GH_STUB = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, urllib.parse
fx = json.load(open(os.environ['STUB_FIXTURE']))
args = sys.argv[1:]
stdin = sys.stdin.read() if '--input' in args else ''
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(json.dumps(['gh', *args, *([stdin] if stdin else [])]) + '\n')
def opt(name):
    return args[args.index(name) + 1] if name in args else ''
def emit(body, jq):
    text = body if isinstance(body, str) else json.dumps(body)
    if jq:
        out = subprocess.run(['jq', '-r', jq], input=text, capture_output=True, text=True)
        sys.stdout.write(out.stdout)
        sys.exit(out.returncode)
    sys.stdout.write(text + '\n')
if args[0] == 'api':
    rest = args[1:]
    include = '-i' in rest
    method, path, i = 'GET', None, 0
    while i < len(rest):
        if rest[i] in ('-X', '--jq', '-f', '-F', '-H', '--input'):
            if rest[i] == '-X':
                method = rest[i + 1]
            i += 2
        elif rest[i] in ('--paginate', '-i'):
            i += 1
        else:
            path = rest[i]
            i += 1
    entry = fx['api'].get(f'{method} {path}')
    if isinstance(entry, list):
        # A list answers each repeat of this exact call with its next entry.
        with open(os.environ['STUB_LOG']) as log:
            seen = sum(json.loads(line)[1:] == args for line in log) - 1
        entry = entry[min(seen, len(entry) - 1)]
    where, _, query = path.partition('?')
    q = dict(urllib.parse.parse_qsl(query))
    if (method, where) == ('GET', 'repos/cplieger/tb/pulls'):
        # `prs_later` is what the listing reads once this run has written anything.
        with open(os.environ['STUB_LOG']) as log:
            wrote = any(json.loads(line)[2:4] in (['-X', 'POST'], ['-X', 'PATCH']) for line in log)
        prs = fx.get('prs_later' if wrote and 'prs_later' in fx else 'prs', {}).get('cplieger/tb', [])
        entry = {'body': [p for p in prs if p['base']['ref'] == q.get('base', p['base']['ref'])]}
    elif (method, where) == ('POST', 'repos/cplieger/tb/pulls'):
        base = next(a.split('=', 1)[1] for a in args if a.startswith('base='))
        url = fx.get('created_urls', {}).get(base, fx.get('created_url', 'https://github.com/cplieger/x/pull/9'))
        refused = fx.get('pull_create_fails')
        entry = {'exit': 1, 'stderr': refused} if refused else {'body': {'html_url': url}}
    elif method == 'PUT' and where.endswith('/merge'):
        failed = fx.get('merge_fails', {}).get('direct')
        entry = {'exit': 1, 'stderr': 'gh: Pull Request is not mergeable (HTTP 405)'} if failed else {'body': {}}
    elif method == 'DELETE' and where.startswith('repos/cplieger/tb/git/refs/heads/'):
        entry = {'body': ''}
    if entry is None or entry.get('exit'):
        stderr = (entry or {}).get('stderr', 'gh: Not Found (HTTP 404)')
        if include:
            status = stderr.rsplit('HTTP ', 1)[-1].rstrip(')') if 'HTTP ' in stderr else '404'
            sys.stdout.write(f'HTTP/2.0 {status} Error\n\r\n' + json.dumps({'message': stderr}))
        sys.stderr.write(stderr + '\n')
        sys.exit(1)
    if include:
        sys.stdout.write('HTTP/2.0 200 OK\n\r\n')
    emit(entry['body'], opt('--jq'))
elif args[:2] == ['pr', 'merge']:
    sys.exit(1 if fx.get('merge_fails', {}).get('auto' if '--auto' in args else 'direct') else 0)
else:
    sys.stderr.write(f'gh stub: unexpected {args}\n')
    sys.exit(64)
"""

CURL_STUB = r"""#!/usr/bin/env python3
import json, os, sys
fx = json.load(open(os.environ['STUB_FIXTURE']))
args = sys.argv[1:]
VALUED = {'-o', '-w', '-H', '--connect-timeout', '--max-time', '--retry', '--retry-max-time'}
out = fmt = url = None
headers = []
i = 0
while i < len(args):
    a = args[i]
    if a in VALUED:
        if a == '-o':
            out = args[i + 1]
        elif a == '-w':
            fmt = args[i + 1]
        elif a == '-H':
            headers.append(args[i + 1])
        i += 2
        continue
    if not a.startswith('-'):
        url = a
    i += 1
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(json.dumps(['curl', url, sorted(h for h in headers)]) + '\n')
entry = fx.get('curl', {}).get(url)
if entry is None:
    sys.stderr.write(f'curl: (6) Could not resolve {url}\n')
    sys.exit(6)
if entry.get('exit'):
    sys.exit(entry['exit'])
body = entry.get('body', {'errors': [{'code': 'MANIFEST_UNKNOWN'}]})
open(out, 'w').write(body if isinstance(body, str) else json.dumps(body))
if fmt == '%{http_code}':
    sys.stdout.write(str(entry.get('status', 200)))
"""

DATE_STUB = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
real = os.environ['STUB_REAL_DATE']
if '-d' not in args:
    now = json.load(open(os.environ['STUB_FIXTURE']))['now']
    args = ['-d', f'@{now}', *args]
os.execv(real, [real, *args])
"""

NOW = 1791086400  # 2026-10-04T04:00:00Z
DAY = 86400


def iso(epoch: int, *, fraction: str = '') -> str:
    text = subprocess.run(
        ['date', '-u', '-d', f'@{epoch}', '+%Y-%m-%dT%H:%M:%S'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return f'{text}{fraction}Z'


def b64(text: str) -> str:
    return base64.encodebytes(text.encode()).decode()


APK = 'FROM alpine:3.24\nRUN apk add --no-cache tini\n'
# A two-branch rebuild needs an install after the stage's ARG PKG_REFRESH.
APK_REFRESH = (
    'FROM alpine:3.24\nARG PKG_REFRESH=static\n'
    'RUN echo "refresh ${PKG_REFRESH}" && apk add --no-cache tini\n'
)
SCRATCH = 'FROM scratch\nCOPY app /app\n'


def repo(name, default='main', **kw):
    return {
        'name': name,
        'default_branch': default,
        'visibility': kw.get('visibility', 'public'),
        'archived': kw.get('archived', False),
        'fork': kw.get('fork', False),
    }


INDEX = 'application/vnd.oci.image.index.v1+json'


def ghcr(curl, name, tag, created=None, *, index=True):
    """GHCR answers for <name>:<tag>; `created` None leaves the tag absent (404)."""
    base = f'https://ghcr.io/v2/cplieger/{name}'
    prefix = 'sha256:'
    curl[f'https://ghcr.io/token?scope=repository:cplieger/{name}:pull'] = {
        'body': {'token': 't0k'}
    }
    if created is None:
        curl[f'{base}/manifests/{tag}'] = {'status': 404}
        return
    config = {'config': {'digest': f'{prefix}cfg-{tag}'}}
    if index:
        attest = {
            'digest': f'{prefix}att-{tag}',
            'platform': {'os': 'unknown', 'architecture': 'unknown'},
        }
        image = {
            'digest': f'{prefix}img-{tag}',
            'platform': {'os': 'linux', 'architecture': 'amd64'},
        }
        curl[f'{base}/manifests/{tag}'] = {
            'body': {'mediaType': INDEX, 'manifests': [attest, image]}
        }
        curl[f'{base}/manifests/{prefix}img-{tag}'] = {'body': config}
    else:
        curl[f'{base}/manifests/{tag}'] = {'body': config}
    curl[f'{base}/blobs/{prefix}cfg-{tag}'] = {'body': {'created': created}}


REBUILD = 'fix(deps): rebuild against refreshed base packages'


def commits(fx, base, entries):
    """The commit listing rebuild_requested reads on <base>, and each listed commit's files.

    `entries` are (sha, subject, committed epoch, file count), newest first.
    """
    since = iso(NOW - 7 * DAY)
    fx['api'][f'GET repos/cplieger/tb/commits?sha={base}&since={since}&per_page=100'] = {
        'body': [
            {
                'sha': sha,
                'commit': {'message': f'{subject}\n\nbody', 'committer': {'date': iso(at)}},
            }
            for sha, subject, at, _ in entries
        ]
    }
    for sha, _, _, files in entries:
        fx['api'][f'GET repos/cplieger/tb/commits/{sha}'] = {
            'body': {'files': [{'filename': f'f{i}'} for i in range(files)]}
        }


def two_branch_fixture(**over) -> dict:
    """One dev-default repo `tb` with a refreshing apk Dockerfile on both bases."""
    api = {
        'GET user/repos?per_page=100&affiliation=owner': {'body': [repo('tb', 'dev')]},
        'GET repos/cplieger/tb/contents/Dockerfile?ref=main': {
            'body': {'content': b64(APK_REFRESH)}
        },
        'GET repos/cplieger/tb/contents/Dockerfile?ref=dev': {
            'body': {'content': b64(APK_REFRESH)}
        },
    }
    curl: dict = {}
    ghcr(curl, 'tb', 'latest', iso(NOW - 10 * DAY, fraction='.123456789'))
    ghcr(curl, 'tb', 'dev', iso(NOW - DAY), index=False)
    fx = {'now': NOW, 'api': api, 'curl': curl}
    commits(fx, 'main', [])
    commits(fx, 'dev', [])
    fx.update(over)
    return fx


class Harness:
    def __init__(self, test: unittest.TestCase):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='rebuild-stale-'))
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        for name, body in (('gh', GH_STUB), ('curl', CURL_STUB), ('date', DATE_STUB)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.fixture = self.tmp / 'fixture.json'
        self.log = self.tmp / 'calls.log'
        self.summary = self.tmp / 'summary.md'
        self.output = self.tmp / 'output'

    def run(
        self, argv: list[str], fx: dict, env: dict | None = None, cwd: pathlib.Path | None = None
    ) -> subprocess.CompletedProcess:
        self.fixture.write_text(json.dumps(fx))
        for f in (self.log, self.summary, self.output):
            f.write_text('')
        full = {
            'PATH': f'{self.bin}:{os.environ["PATH"]}',
            'HOME': str(self.tmp),
            'TMPDIR': str(self.tmp),
            'STUB_FIXTURE': str(self.fixture),
            'STUB_LOG': str(self.log),
            'STUB_REAL_DATE': shutil.which('date'),
            'INTERVAL_DAYS': '7',
            'GITHUB_STEP_SUMMARY': str(self.summary),
            'GITHUB_OUTPUT': str(self.output),
            **(env or {}),
        }
        return subprocess.run(argv, env=full, cwd=cwd, capture_output=True, text=True, check=False)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def matrix(self) -> list[dict]:
        line = self.output.read_text().strip()
        assert line.startswith('matrix='), line
        return json.loads(line.removeprefix('matrix='))

    def rows(self) -> list[dict]:
        """Each channel the matrix names, as one row carrying its repo."""
        return [{'repo': leg['repo'], **row} for leg in self.matrix() for row in leg['bases']]


class TwoBranchFanout(unittest.TestCase):
    def fanout(self, fx):
        h = Harness(self)
        res = h.run(['bash', str(SCRIPT), 'fanout'], fx)
        return h, res

    def test_stale_latest_and_fresh_dev_give_one_main_row(self):
        h, res = self.fanout(two_branch_fixture())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            h.rows(),
            [
                {
                    'repo': 'tb',
                    'base': 'main',
                    'channel': 'latest',
                    'last': iso(NOW - 10 * DAY),
                    'age_days': '10',
                    'since': iso(NOW - 7 * DAY),
                }
            ],
        )
        summary = h.summary.read_text()
        self.assertIn('| Repo | Image built (UTC) | Age (days) |', summary)
        self.assertIn(
            f'| tb (main, :latest) | {iso(NOW - 10 * DAY)} | 10 | 7 | **rebuild** |', summary
        )
        self.assertIn(f'| tb (dev, :dev) | {iso(NOW - DAY)} | 1 | 7 | fresh |', summary)
        self.assertIn('Rebuilding 1 image(s).', summary)
        self.assertIn('Stale images: 1', res.stdout)

    def test_only_a_two_branch_repo_is_read(self):
        self.assertIn('tool-catalog', release_channels.SINGLE_MAIN_REPOS)
        fx = two_branch_fixture()
        fx['api']['GET user/repos?per_page=100&affiliation=owner']['body'] += [
            repo('main-app'),
            repo('private-app', 'dev', visibility='private'),
            repo('tool-catalog', 'dev'),
            repo('archived-app', 'dev', archived=True),
            repo('fork-app', 'dev', fork=True),
        ]
        h, res = self.fanout(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        read = {
            c[2].split('/')[2]
            for c in h.calls()
            if c[:2] == ['gh', 'api'] and c[2].startswith('repos/cplieger/')
        }
        self.assertEqual(read, {'tb'})
        self.assertTrue(all('cplieger/tb' in c[1] for c in h.calls() if c[0] == 'curl'))
        self.assertEqual({r['repo'] for r in h.rows()}, {'tb'})

    def test_both_channels_stale_give_one_leg_whose_bases_run_main_first(self):
        fx = two_branch_fixture()
        ghcr(fx['curl'], 'tb', 'dev', iso(NOW - 8 * DAY))
        h, _ = self.fanout(fx)
        (leg,) = h.matrix()
        self.assertEqual((leg['repo'], leg['readable']), ('tb', True))
        self.assertEqual(
            [(r['base'], r['channel']) for r in leg['bases']], [('main', 'latest'), ('dev', 'dev')]
        )
        self.assertFalse([r for r in leg['bases'] if 'repo' in r])

    def test_each_repository_is_one_leg_in_discovery_order(self):
        fx = two_branch_fixture()
        fx['api']['GET user/repos?per_page=100&affiliation=owner']['body'].insert(
            0, repo('ta', 'dev')
        )
        fx['api']['GET repos/cplieger/ta/contents/Dockerfile?ref=main'] = {
            'body': {'content': b64(APK_REFRESH)}
        }
        ghcr(fx['curl'], 'ta', 'latest', iso(NOW - 9 * DAY))
        ghcr(fx['curl'], 'tb', 'dev', iso(NOW - 8 * DAY))
        h, res = self.fanout(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            [(leg['repo'], [r['base'] for r in leg['bases']]) for leg in h.matrix()],
            [('ta', ['main']), ('tb', ['main', 'dev'])],
        )
        self.assertIn('Rebuilding 3 image(s).', h.summary.read_text())

    def test_a_leg_is_readable_when_any_of_its_bases_read(self):
        key = 'GET repos/cplieger/tb/contents/Dockerfile?ref={}'
        broken = {'exit': 1, 'stderr': 'gh: Server Error (HTTP 502)'}
        for name, bases, readable in (
            ('main unreadable, dev stale', ['main'], True),
            ('both unreadable', ['main', 'dev'], False),
        ):
            with self.subTest(name):
                fx = two_branch_fixture()
                ghcr(fx['curl'], 'tb', 'dev', iso(NOW - 8 * DAY))
                for base in bases:
                    fx['api'][key.format(base)] = broken
                h, _ = self.fanout(fx)
                (leg,) = h.matrix()
                self.assertEqual(
                    [('unreadable' in r, r['base']) for r in leg['bases']],
                    [(True, 'main'), ('dev' in bases, 'dev')],
                )
                self.assertIs(leg['readable'], readable)

    def test_the_age_is_the_images_not_a_runs(self):
        h, _ = self.fanout(two_branch_fixture())
        self.assertFalse([c for c in h.calls() if c[0] == 'gh' and '/actions/' in ' '.join(c)])
        self.assertIn(
            ['gh', 'api', 'repos/cplieger/tb/contents/Dockerfile?ref=main', '--jq', '.content'],
            h.calls(),
        )

    def test_the_registry_is_read_anonymously_with_index_and_manifest_types(self):
        h, _ = self.fanout(two_branch_fixture())
        curls = [c for c in h.calls() if c[0] == 'curl']
        token = curls[0]
        self.assertEqual(token[1], 'https://ghcr.io/token?scope=repository:cplieger/tb:pull')
        self.assertFalse([x for x in token[2] if x.startswith('Authorization')])
        top = next(c for c in curls if c[1].endswith('/manifests/latest'))
        accept = next(x for x in top[2] if x.startswith('Accept:'))
        for media in (
            INDEX,
            'application/vnd.docker.distribution.manifest.list.v2+json',
            'application/vnd.oci.image.manifest.v1+json',
            'application/vnd.docker.distribution.manifest.v2+json',
        ):
            self.assertIn(media, accept)
        self.assertIn('Authorization: Bearer t0k', top[2])

    def test_the_attestation_manifest_is_skipped(self):
        h, _ = self.fanout(two_branch_fixture())
        read = [c[1] for c in h.calls() if c[0] == 'curl']
        self.assertIn('https://ghcr.io/v2/cplieger/tb/manifests/sha256:img-latest', read)
        self.assertNotIn('https://ghcr.io/v2/cplieger/tb/manifests/sha256:att-latest', read)

    def test_a_missing_dev_tag_is_stale(self):
        fx = two_branch_fixture()
        ghcr(fx['curl'], 'tb', 'dev', None)
        h, res = self.fanout(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        dev = [r for r in h.rows() if r['base'] == 'dev']
        self.assertEqual(dev[0]['last'], 'not published')
        self.assertEqual(dev[0]['age_days'], 'n/a')
        self.assertNotIn('unreadable', dev[0])

    def test_an_unreadable_registry_gives_unreadable_rows_and_rebuilds_nothing_there(self):
        cases = {
            'token refused': (
                {'https://ghcr.io/token?scope=repository:cplieger/tb:pull': {'status': 403}},
                'GHCR token endpoint answered HTTP 403',
            ),
            'server error': (
                {'https://ghcr.io/v2/cplieger/tb/manifests/latest': {'status': 500, 'body': {}}},
                'cplieger/tb:latest answered HTTP 500',
            ),
            'no answer': (
                {'https://ghcr.io/v2/cplieger/tb/manifests/latest': {'exit': 7}},
                'no answer for cplieger/tb:latest',
            ),
            'attestations only': (
                {
                    'https://ghcr.io/v2/cplieger/tb/manifests/latest': {
                        'body': {
                            'mediaType': INDEX,
                            'manifests': [{'digest': 'sha256:a', 'platform': {'os': 'unknown'}}],
                        }
                    }
                },
                'cplieger/tb:latest lists no platform image',
            ),
            'no created time': (
                {'https://ghcr.io/v2/cplieger/tb/blobs/sha256:cfg-latest': {'body': {}}},
                'the image config of cplieger/tb:latest carries no created time',
            ),
        }
        for label, (patch, reason) in cases.items():
            with self.subTest(label):
                fx = two_branch_fixture()
                fx['curl'].update(patch)
                h, res = self.fanout(fx)
                self.assertEqual(res.returncode, 0, res.stderr)
                main = next(r for r in h.rows() if r['base'] == 'main')
                self.assertEqual(main['unreadable'], reason)
                self.assertEqual(main['last'], 'unreadable')
                summary = h.summary.read_text()
                self.assertIn('**unreadable**', summary)
                self.assertIn(
                    "could not be read; each fails its repository's dispatch job.", summary
                )
                self.assertIn('Rebuilding 0 image(s).', summary)

    def test_a_dockerfile_on_one_base_ages_that_channel_only(self):
        fx = two_branch_fixture()
        del fx['api']['GET repos/cplieger/tb/contents/Dockerfile?ref=main']
        ghcr(fx['curl'], 'tb', 'dev', iso(NOW - 9 * DAY))
        h, _ = self.fanout(fx)
        self.assertEqual([r['base'] for r in h.rows()], ['dev'])
        self.assertFalse([c for c in h.calls() if c[0] == 'curl' and c[1].endswith(':latest')])
        self.assertFalse([c for c in h.calls() if c[0] == 'curl' and '/manifests/latest' in c[1]])

    def test_a_dockerfile_read_that_is_not_a_404_is_an_unreadable_row(self):
        key = 'GET repos/cplieger/tb/contents/Dockerfile?ref=main'
        cases = {
            'server error': (
                {'exit': 1, 'stderr': 'gh: Server Error (HTTP 502)'},
                'the Dockerfile on main could not be read: gh: Server Error (HTTP 502)',
            ),
            'not a file': (
                {'body': [{'name': 'Dockerfile'}]},
                'the Dockerfile on main could not be read: ',
            ),
            'bad base64': (
                {'body': {'content': '%%%not base64%%%'}},
                'the Dockerfile on main did not decode as base64 content',
            ),
            'empty': (
                {'body': {'content': ''}},
                'the Dockerfile on main did not decode as base64 content',
            ),
        }
        for label, (entry, reason) in cases.items():
            with self.subTest(label):
                fx = two_branch_fixture()
                fx['api'][key] = entry
                h, res = self.fanout(fx)
                self.assertEqual(res.returncode, 0, res.stderr)
                main = next(r for r in h.rows() if r['base'] == 'main')
                self.assertTrue(main['unreadable'].startswith(reason), main['unreadable'])
                self.assertEqual((main['channel'], main['last']), ('latest', 'unreadable'))
                self.assertFalse(
                    [c for c in h.calls() if c[0] == 'curl' and '/manifests/latest' in c[1]]
                )
                summary = h.summary.read_text()
                self.assertIn('| tb (main, :latest) | unreadable: the Dockerfile on main', summary)
                self.assertIn(
                    "1 image(s) could not be read; each fails its repository's dispatch job.",
                    summary,
                )

    def test_a_distroless_base_is_not_a_candidate(self):
        fx = two_branch_fixture()
        fx['api']['GET repos/cplieger/tb/contents/Dockerfile?ref=main'] = {
            'body': {'content': b64(SCRATCH)}
        }
        h, _ = self.fanout(fx)
        self.assertEqual(h.rows(), [])
        self.assertIn('Rebuilding 0 image(s).', h.summary.read_text())

    def test_an_install_counts_as_docker_runs_it(self):
        for text, candidate in (
            ('FROM alpine\nARG PKG_REFRESH\nRUN : $PKG_REFRESH && apk \\\n  add tini\n', True),
            (
                (
                    'FROM alpine\nARG PKG_REFRESH\n'
                    'RUN ["sh", "-c", "echo ${PKG_REFRESH}; apt-get install -y curl"]\n'
                ),
                True,
            ),
            ('FROM alpine\n# apk upgrade happens upstream\nCOPY app /app\n', False),
            ('FROM alpine\nARG PKG_REFRESH\nRUN sudo -u root apk add tini\n', True),
            ('FROM alpine\nARG PKG_REFRESH\nRUN apk add tini\n', True),
            ('FROM alpine\nARG PKG_REFRESH\nRUN command -v apk add\n', False),
        ):
            with self.subTest(text=text):
                fx = two_branch_fixture()
                fx['api']['GET repos/cplieger/tb/contents/Dockerfile?ref=main'] = {
                    'body': {'content': b64(text)}
                }
                h, res = self.fanout(fx)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual([r['base'] for r in h.rows()], ['main'] if candidate else [])

    def test_an_install_no_pkg_refresh_reaches_is_a_warning_row_and_no_rebuild(self):
        for name, text in (
            ('no key', APK),
            (
                'a global ARG the stage never declares',
                'ARG PKG_REFRESH\nFROM alpine\nRUN echo $PKG_REFRESH && apk add tini\n',
            ),
            (
                'declared only after the install',
                'FROM alpine\nRUN apk add tini\nARG PKG_REFRESH\nRUN echo $PKG_REFRESH\n',
            ),
            (
                'declared on a line the release build does not pass it for',
                'FROM alpine\nARG BUILD_VERSION PKG_REFRESH\nRUN apk add tini\n',
            ),
        ):
            with self.subTest(name):
                fx = two_branch_fixture()
                fx['api']['GET repos/cplieger/tb/contents/Dockerfile?ref=main'] = {
                    'body': {'content': b64(text)}
                }
                h, res = self.fanout(fx)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(h.rows(), [])
                self.assertIn(
                    '::warning::cplieger/tb: the Dockerfile on main installs packages in no '
                    'layer PKG_REFRESH reaches',
                    res.stdout,
                )
                self.assertIn(
                    '| tb (main, :latest) | not refreshed by a rebuild | n/a | 7 | '
                    '**no PKG_REFRESH** |',
                    h.summary.read_text(),
                )
                self.assertFalse(
                    [c for c in h.calls() if c[0] == 'curl' and '/manifests/latest' in c[1]]
                )

    def test_a_dockerfile_the_parser_refuses_is_an_unreadable_row(self):
        fx = two_branch_fixture()
        h = Harness(self)
        real = shutil.which('python3')
        stub = h.bin / 'python3'
        stub.write_text(
            '#!/usr/bin/env bash\n'
            'case " $* " in *" rebuild-refreshes "*) echo "Traceback: boom" >&2; exit 1 ;; esac\n'
            f'exec {real} "$@"\n'
        )
        stub.chmod(0o755)
        res = h.run(['bash', str(SCRIPT), 'fanout'], fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            [(r['base'], r.get('unreadable')) for r in h.rows()],
            [
                ('main', 'the Dockerfile on main could not be parsed: Traceback: boom'),
                ('dev', 'the Dockerfile on dev could not be parsed: Traceback: boom'),
            ],
        )

    def test_the_interval_threshold_is_exclusive(self):
        fx = two_branch_fixture()
        ghcr(fx['curl'], 'tb', 'latest', iso(NOW - 7 * DAY))
        h, _ = self.fanout(fx)
        self.assertEqual(h.rows(), [])
        ghcr(fx['curl'], 'tb', 'latest', iso(NOW - 7 * DAY - 1))
        h, _ = self.fanout(fx)
        self.assertEqual([r['base'] for r in h.rows()], ['main'])

    def test_a_rebuild_request_within_the_interval_holds_a_stale_image_back(self):
        fx = two_branch_fixture()
        commits(fx, 'main', [('r1', f'{REBUILD} (#40)', NOW - 2 * DAY, 0)])
        h, res = self.fanout(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(h.rows(), [])
        self.assertIn(
            f'| tb (main, :latest) | {iso(NOW - 10 * DAY)} | 10 | 7 | '
            f'fresh (rebuild requested {iso(NOW - 2 * DAY)}) |',
            h.summary.read_text(),
        )

    def test_a_rebuild_request_older_than_the_interval_does_not(self):
        fx = two_branch_fixture()
        commits(fx, 'main', [('r1', REBUILD, NOW - 8 * DAY, 0)])
        h, _ = self.fanout(fx)
        self.assertEqual([r['base'] for r in h.rows()], ['main'])

    def test_a_commit_borrowing_the_subject_is_not_a_request(self):
        fx = two_branch_fixture()
        commits(fx, 'main', [('r1', REBUILD, NOW - 2 * DAY, 1)])
        h, _ = self.fanout(fx)
        self.assertEqual([r['base'] for r in h.rows()], ['main'])

    def test_an_unreadable_request_listing_falls_back_to_the_image_age(self):
        fx = two_branch_fixture()
        del fx['api'][
            f'GET repos/cplieger/tb/commits?sha=main&since={iso(NOW - 7 * DAY)}&per_page=100'
        ]
        h, res = self.fanout(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([r['base'] for r in h.rows()], ['main'])
        self.assertIn(
            '::warning::cplieger/tb: the rebuild requests on main could not be read, '
            'so the image age alone decides\ngh: Not Found (HTTP 404)\n',
            res.stdout,
        )

    def test_the_dev_channel_reads_devs_commits(self):
        fx = two_branch_fixture()
        ghcr(fx['curl'], 'tb', 'dev', iso(NOW - 8 * DAY))
        commits(fx, 'dev', [('d1', REBUILD, NOW - DAY, 0)])
        h, _ = self.fanout(fx)
        self.assertEqual([(r['base'], r['channel']) for r in h.rows()], [('main', 'latest')])
        self.assertTrue(
            [c for c in h.calls() if c[0] == 'gh' and 'commits?sha=dev&' in ' '.join(c)]
        )

    def test_a_fresh_image_reads_no_commits(self):
        h, _ = self.fanout(two_branch_fixture())
        self.assertFalse(
            [c for c in h.calls() if c[0] == 'gh' and 'commits?sha=dev' in ' '.join(c)]
        )

    def test_an_unparsable_created_time_is_an_unreadable_row(self):
        fx = two_branch_fixture()
        ghcr(fx['curl'], 'tb', 'latest', 'yesterday-ish')
        h, res = self.fanout(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        main = next(r for r in h.rows() if r['base'] == 'main')
        self.assertEqual(
            (main['last'], main['age_days'], main['unreadable']),
            ('unreadable', 'n/a', 'the image creation time yesterday-ish does not parse'),
        )
        summary = h.summary.read_text()
        self.assertIn('| tb (main, :latest) | unreadable: the image creation time', summary)
        self.assertIn('Rebuilding 0 image(s).', summary)


class ImageCreated(unittest.TestCase):
    def test_exit_codes(self):
        h = Harness(self)
        fx = two_branch_fixture()
        res = h.run(['bash', str(SCRIPT), 'image-created', 'tb', 'latest'], fx)
        self.assertEqual((res.returncode, res.stdout), (0, f'{iso(NOW - 10 * DAY)}\n'))
        ghcr(fx['curl'], 'tb', 'dev', None)
        res = h.run(['bash', str(SCRIPT), 'image-created', 'tb', 'dev'], fx)
        self.assertEqual((res.returncode, res.stdout), (3, ''))
        fx['curl']['https://ghcr.io/v2/cplieger/tb/manifests/dev'] = {'status': 401}
        res = h.run(['bash', str(SCRIPT), 'image-created', 'tb', 'dev'], fx)
        self.assertEqual(res.returncode, 1)
        self.assertIn('answered HTTP 401', res.stderr)

    def test_a_single_manifest_resolves_its_config_directly(self):
        h = Harness(self)
        res = h.run(['bash', str(SCRIPT), 'image-created', 'tb', 'dev'], two_branch_fixture())
        self.assertEqual(res.stdout, f'{iso(NOW - DAY)}\n')
        reads = [c[1] for c in h.calls() if c[0] == 'curl']
        self.assertEqual(
            reads[1:],
            [
                'https://ghcr.io/v2/cplieger/tb/manifests/dev',
                'https://ghcr.io/v2/cplieger/tb/blobs/sha256:cfg-dev',
            ],
        )

    def test_usage(self):
        h = Harness(self)
        for argv in (
            [],
            ['bogus'],
            ['image-created', 'tb'],
            ['open-pr', 'tb', 'main'],
            ['dispatch', 'tb'],
        ):
            with self.subTest(argv):
                self.assertEqual(h.run(['bash', str(SCRIPT), *argv], {}).returncode, 2)


CHECKS = (
    'GET repos/cplieger/tb/commits/headsha/check-runs'
    '?check_name=ci%20%2F%20validate&filter=latest&per_page=100'
)


def pull(base, ref, *, now_base=None, repo='cplieger/tb'):
    """The pull request merge-checked reads back, at head `headsha`."""
    return {
        'body': {
            'state': 'open',
            'base': {'ref': now_base or base},
            'head': {'sha': 'headsha', 'ref': ref, 'repo': {'full_name': repo}},
        }
    }


def check_runs(runs):
    return {
        'body': {
            'check_runs': [
                {'app': {'id': app}, 'status': status, 'conclusion': conclusion}
                for app, status, conclusion in runs
            ]
        }
    }


BRANCH_READ = 'GET repos/cplieger/tb/git/ref/heads/rebuild/main-20261004'
NOT_FOUND = {'exit': 1, 'stderr': 'gh: Not Found (HTTP 404)'}
REF_REFUSED = {'exit': 1, 'stderr': 'gh: Reference already exists (HTTP 422)'}
REF_CREATE = [
    'gh', 'api', '-X', 'POST', 'repos/cplieger/tb/git/refs',
    '-f', 'ref=refs/heads/rebuild/main-20261004', '-f', 'sha=emptycommit',
]  # fmt: skip


def ref_creates(calls):
    return [c for c in calls if c[1:5] == ['api', '-X', 'POST', 'repos/cplieger/tb/git/refs']]


def pr_fixture(base='main', **over) -> dict:
    api = {
        'GET repos/cplieger/tb/git/ref/heads/main': {'body': {'object': {'sha': 'mainhead'}}},
        'GET repos/cplieger/tb/git/ref/heads/dev': {'body': {'object': {'sha': 'devhead'}}},
        'GET repos/cplieger/tb/git/commits/mainhead': {'body': {'tree': {'sha': 'maintree'}}},
        'GET repos/cplieger/tb/git/commits/devhead': {'body': {'tree': {'sha': 'devtree'}}},
        'POST repos/cplieger/tb/git/commits': {'body': {'sha': 'emptycommit'}},
        'POST repos/cplieger/tb/git/refs': {'body': {}},
        'PATCH repos/cplieger/tb/git/refs/heads/rebuild/main-20261004': {'body': {}},
        'GET repos/cplieger/tb/pulls/9': pull(base, f'rebuild/{base}-20261004'),
        'GET repos/cplieger/tb/pulls/5': pull(base, f'rebuild/{base}-20261001'),
        CHECKS: check_runs([(15368, 'completed', 'success')]),
    }
    fx = {'now': NOW, 'api': api, 'prs': {'cplieger/tb': []}}
    fx.update(over)
    return fx


def pr(head, base, *, fork=False, n=1):
    """One row of the REST open-pulls listing of cplieger/tb."""
    return {
        'number': n,
        'head': {'ref': head, 'repo': {'full_name': 'someone/tb' if fork else 'cplieger/tb'}},
        'base': {'ref': base},
        'html_url': f'https://github.com/cplieger/tb/pull/{n}',
    }


def pull_creates(calls):
    """The -f fields of each pull request create, in order; each must answer its URL."""
    creates = [c for c in calls if c[1:5] == ['api', '-X', 'POST', 'repos/cplieger/tb/pulls']]
    for call in creates:
        assert call[-2:] == ['--jq', '.html_url'], call
    return [dict(c[i + 1].split('=', 1) for i, a in enumerate(c) if a == '-f') for c in creates]


def created(calls):
    """The -f fields of the one pull request create."""
    (fields,) = pull_creates(calls)
    return fields


def merge_calls(calls):
    """Every merge: each `gh pr merge` and each REST merge."""
    return [
        c
        for c in calls
        if c[1:3] == ['pr', 'merge'] or (c[1] == 'api' and any(a.endswith('/merge') for a in c))
    ]


class OpenPr(unittest.TestCase):
    def open(self, fx, base='main', reason='Because.'):
        h = Harness(self)
        res = h.run(['bash', str(SCRIPT), 'open-pr', 'tb', base, reason], fx)
        return h, res

    def test_a_new_pr_carries_one_empty_commit_on_the_bases_head(self):
        h, res = self.open(pr_fixture())
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = h.calls()
        self.assertIn(
            [
                'gh', 'api', '-X', 'POST', 'repos/cplieger/tb/git/commits',
                '-f', 'message=fix(deps): rebuild against refreshed base packages',
                '-f', 'tree=maintree', '-f', 'parents[]=mainhead', '--jq', '.sha',
            ],
            calls,
        )  # fmt: skip
        self.assertIn(
            [
                'gh', 'api', '-X', 'POST', 'repos/cplieger/tb/git/refs',
                '-f', 'ref=refs/heads/rebuild/main-20261004', '-f', 'sha=emptycommit',
            ],
            calls,
        )  # fmt: skip
        create = created(calls)
        self.assertEqual(
            {k: create[k] for k in ('base', 'head', 'title')},
            {
                'base': 'main',
                'head': 'rebuild/main-20261004',
                'title': 'fix(deps): rebuild against refreshed base packages',
            },
        )
        self.assertEqual(
            create['body'],
            'Because. Merging this empty commit rebuilds it against the base packages '
            'published since then; nothing else changes.',
        )
        self.assertIn('Opened https://github.com/cplieger/x/pull/9', res.stdout)
        merges = [c for c in calls if c[1:3] == ['pr', 'merge']]
        self.assertEqual(
            merges,
            [
                [
                    'gh', 'pr', 'merge', '9', '-R', 'cplieger/tb', '--squash', '--delete-branch',
                    '--auto', '--match-head-commit', 'headsha',
                ]
            ],
        )  # fmt: skip
        read = calls.index(['gh', 'api', '-i', 'repos/cplieger/tb/pulls/9'])
        self.assertLess(read, calls.index(merges[0]), 'the base and head are read before arming')

    def test_the_main_branch_is_a_rebuild_intake_head(self):
        h, _ = self.open(pr_fixture())
        head = created(h.calls())['head']
        self.assertEqual(release_channels.main_intake_kind(head), 'rebuild')

    def test_dev_uses_devs_head_and_tree(self):
        h, res = self.open(pr_fixture('dev'), base='dev')
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = h.calls()
        commit = next(c for c in calls if 'repos/cplieger/tb/git/commits' in c and 'POST' in c)
        self.assertIn('tree=devtree', commit)
        self.assertIn('parents[]=devhead', commit)
        self.assertEqual(created(calls)['head'], 'rebuild/dev-20261004')
        self.assertIsNone(release_channels.main_intake_kind('rebuild/dev-20261004'))

    def test_an_open_rebuild_pr_on_the_base_is_asked_again(self):
        fx = pr_fixture(prs={'cplieger/tb': [pr('rebuild/main-20261001', 'main', n=5)]})
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = h.calls()
        self.assertFalse([c for c in calls if 'POST' in c])
        self.assertIn(
            ['gh', 'pr', 'merge', '5', '-R', 'cplieger/tb', '--squash', '--delete-branch',
             '--auto', '--match-head-commit', 'headsha'],
            calls,
        )  # fmt: skip
        (listing,) = [c for c in calls if any('/pulls?' in a for a in c)]
        self.assertEqual(
            listing[:4],
            [
                'gh',
                'api',
                '--paginate',
                (
                    'repos/cplieger/tb/pulls?state=open&base=main&sort=created&direction=desc'
                    '&per_page=100'
                ),
            ],
        )

    def test_the_newest_of_several_open_rebuild_prs_is_asked_again(self):
        fx = pr_fixture(
            prs={
                'cplieger/tb': [
                    pr('rebuild/main-20261001', 'main', n=5),
                    pr('rebuild/main-20260924', 'main', n=7),
                ]
            }
        )
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([c[3] for c in h.calls() if c[1:3] == ['pr', 'merge']], ['5'])
        self.assertIn('https://github.com/cplieger/tb/pull/5 is already open', res.stdout)

    def test_another_bases_or_a_forks_rebuild_pr_is_not_this_bases(self):
        fx = pr_fixture(
            prs={
                'cplieger/tb': [
                    pr('rebuild/dev-20261001', 'dev', n=2),
                    pr('rebuild/dev-20261002', 'main', n=3),
                    pr('rebuild/main-20261001', 'main', fork=True, n=4),
                    pr('renovate/main-x', 'main', n=6),
                ]
            }
        )
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        merged = [c[3] for c in h.calls() if c[1:3] == ['pr', 'merge']]
        self.assertEqual(merged, ['9'])

    def test_an_existing_branch_without_a_pr_is_moved(self):
        fx = pr_fixture()
        fx['api']['GET repos/cplieger/tb/git/ref/heads/rebuild/main-20261004'] = {'body': {}}
        h, res = self.open(fx)
        self.assertIn(
            '::notice::cplieger/tb: rebuild/main-20261004 already exists without an open pull'
            ' request, so it is moved to the new commit\n',
            res.stdout,
        )
        calls = h.calls()
        self.assertIn(
            [
                'gh', 'api', '-X', 'PATCH', 'repos/cplieger/tb/git/refs/heads/rebuild/main-20261004',
                '-f', 'sha=emptycommit', '-F', 'force=true',
            ],
            calls,
        )  # fmt: skip
        self.assertFalse([c for c in calls if 'repos/cplieger/tb/git/refs' in c and 'POST' in c])

    def concurrent(self, **over):
        """A second writer opened rebuild/main-20261004 as #12 after this run's first read."""
        fx = pr_fixture(
            prs_later={'cplieger/tb': [pr('rebuild/main-20261004', 'main', n=12)]}, **over
        )
        fx['api']['GET repos/cplieger/tb/pulls/12'] = pull('main', 'rebuild/main-20261004')
        return fx

    def test_a_branch_another_writer_created_with_its_pr_is_that_prs_merge_request(self):
        fx = self.concurrent()
        fx['api']['POST repos/cplieger/tb/git/refs'] = {
            'exit': 1,
            'stderr': 'gh: Reference already exists (HTTP 422)',
        }
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = h.calls()
        self.assertFalse(
            [c for c in calls if c[1:5] == ['api', '-X', 'POST', 'repos/cplieger/tb/pulls']]
        )
        self.assertFalse([c for c in calls if 'PATCH' in c])
        self.assertEqual([c[3] for c in merge_calls(calls)], ['12'])
        self.assertIn(
            '::notice::cplieger/tb: https://github.com/cplieger/tb/pull/12 opened '
            'rebuild/main-20261004 concurrently, so its merge is asked for',
            res.stdout,
        )

    def test_a_branch_another_writer_created_without_a_pr_is_moved_to_this_commit(self):
        fx = pr_fixture()
        fx['api']['POST repos/cplieger/tb/git/refs'] = REF_REFUSED
        fx['api'][BRANCH_READ] = [NOT_FOUND, {'body': {}}]
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = h.calls()
        patch = [
            'gh', 'api', '-X', 'PATCH', 'repos/cplieger/tb/git/refs/heads/rebuild/main-20261004',
            '-f', 'sha=emptycommit', '-F', 'force=true',
        ]  # fmt: skip
        self.assertIn(patch, calls)
        self.assertEqual(created(calls)['head'], 'rebuild/main-20261004')
        self.assertLess(
            calls.index(patch), calls.index(next(c for c in calls if 'title=' + REBUILD in c))
        )
        self.assertEqual([c[3] for c in merge_calls(calls)], ['9'])

    def test_a_refused_create_that_left_no_branch_is_sent_once_more(self):
        fx = pr_fixture()
        fx['api']['POST repos/cplieger/tb/git/refs'] = [REF_REFUSED, {'body': {}}]
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = h.calls()
        self.assertEqual(ref_creates(calls), [REF_CREATE, REF_CREATE])
        self.assertEqual(calls.count(['gh', 'api', BRANCH_READ.split(' ', 1)[1]]), 2)
        self.assertFalse([c for c in calls if 'PATCH' in c])
        self.assertNotIn('so it is moved to the new commit', res.stdout)
        self.assertEqual(created(calls)['head'], 'rebuild/main-20261004')
        self.assertEqual([c[3] for c in merge_calls(calls)], ['9'])

    def test_a_create_refused_twice_with_no_branch_fails_saying_so(self):
        fx = pr_fixture()
        fx['api']['POST repos/cplieger/tb/git/refs'] = REF_REFUSED
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 1)
        calls = h.calls()
        self.assertEqual(len(ref_creates(calls)), 2)
        self.assertEqual(calls.count(['gh', 'api', BRANCH_READ.split(' ', 1)[1]]), 3)
        self.assertFalse([c for c in calls if 'PATCH' in c])
        self.assertEqual(pull_creates(calls), [])
        self.assertEqual(merge_calls(calls), [])
        self.assertIn(
            '::error::cplieger/tb: rebuild/main-20261004 could not be created and does not exist,'
            ' so no rebuild pull request into main was opened',
            res.stdout,
        )

    def test_a_create_refused_twice_whose_branch_then_reads_present_is_moved_and_opened(self):
        fx = pr_fixture()
        fx['api']['POST repos/cplieger/tb/git/refs'] = REF_REFUSED
        fx['api'][BRANCH_READ] = [NOT_FOUND, NOT_FOUND, {'body': {}}]
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = h.calls()
        self.assertEqual(ref_creates(calls), [REF_CREATE, REF_CREATE])
        self.assertEqual(calls.count(['gh', 'api', BRANCH_READ.split(' ', 1)[1]]), 3)
        patch = [
            'gh', 'api', '-X', 'PATCH', 'repos/cplieger/tb/git/refs/heads/rebuild/main-20261004',
            '-f', 'sha=emptycommit', '-F', 'force=true',
        ]  # fmt: skip
        self.assertIn(patch, calls)
        self.assertEqual(created(calls)['head'], 'rebuild/main-20261004')
        self.assertEqual([c[3] for c in merge_calls(calls)], ['9'])
        self.assertNotIn('does not exist', res.stdout)

    def test_a_create_refused_twice_whose_branch_then_cannot_be_read_fails_without_a_write(self):
        fx = pr_fixture()
        fx['api']['POST repos/cplieger/tb/git/refs'] = REF_REFUSED
        fx['api'][BRANCH_READ] = [
            NOT_FOUND,
            NOT_FOUND,
            {'exit': 1, 'stderr': 'gh: Server Error (HTTP 502)'},
        ]
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 1)
        calls = h.calls()
        self.assertEqual(len(ref_creates(calls)), 2)
        self.assertFalse([c for c in calls if 'PATCH' in c])
        self.assertEqual(pull_creates(calls), [])
        self.assertIn('gh: Server Error (HTTP 502)', res.stdout)
        self.assertIn(
            '::error::cplieger/tb: rebuild/main-20261004 could not be created, and reading it again'
            ' failed',
            res.stdout,
        )
        self.assertNotIn('does not exist', res.stdout)

    def test_a_refused_create_whose_branch_cannot_be_read_again_fails_without_a_write(self):
        fx = pr_fixture()
        fx['api']['POST repos/cplieger/tb/git/refs'] = REF_REFUSED
        fx['api'][BRANCH_READ] = [NOT_FOUND, {'exit': 1, 'stderr': 'gh: Server Error (HTTP 502)'}]
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 1)
        calls = h.calls()
        self.assertEqual(len(ref_creates(calls)), 1)
        self.assertFalse([c for c in calls if 'PATCH' in c])
        self.assertEqual(pull_creates(calls), [])
        self.assertIn(
            '::error::cplieger/tb: rebuild/main-20261004 could not be created, and reading it again'
            ' failed',
            res.stdout,
        )

    def test_a_pr_another_writer_opened_first_is_that_prs_merge_request(self):
        fx = self.concurrent(
            pull_create_fails='gh: Validation Failed: A pull request already exists (HTTP 422)'
        )
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([c[3] for c in merge_calls(h.calls())], ['12'])
        self.assertNotIn('Opened ', res.stdout)

    def test_a_refused_pr_create_with_no_open_pr_fails_naming_why(self):
        fx = pr_fixture(pull_create_fails='gh: Validation Failed: No commits between (HTTP 422)')
        h, res = self.open(fx)
        self.assertEqual(res.returncode, 1)
        self.assertEqual(merge_calls(h.calls()), [])
        self.assertIn('No commits between (HTTP 422)', res.stdout)
        self.assertIn(
            '::error::cplieger/tb: no rebuild pull request into main was opened, and none is open',
            res.stdout,
        )

    def test_a_ref_create_failure_other_than_an_existing_ref_stops_the_run(self):
        fx = self.concurrent()
        fx['api']['POST repos/cplieger/tb/git/refs'] = {
            'exit': 1,
            'stderr': 'gh: Server Error (HTTP 502)',
        }
        h, res = self.open(fx)
        self.assertNotEqual(res.returncode, 0)
        calls = h.calls()
        self.assertFalse([c for c in calls if 'PATCH' in c])
        self.assertFalse(
            [c for c in calls if c[1:5] == ['api', '-X', 'POST', 'repos/cplieger/tb/pulls']]
        )
        self.assertEqual(merge_calls(calls), [])

    def test_the_merge_falls_back_once_and_fails_when_both_refuse(self):
        res, merges = self.fallback([(15368, 'completed', 'success')], base='dev')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(['--auto' in c for c in merges], [True, False])
        res, merges = self.fallback(
            [(15368, 'completed', 'success')],
            base='dev',
            merge_fails={'auto': True, 'direct': True},
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn(
            'could not enable auto-merge on https://github.com/cplieger/x/pull/9', res.stdout
        )

    def test_a_rebuild_pr_retargeted_after_it_was_opened_is_never_armed_or_merged(self):
        for base, now_base in (('dev', 'main'), ('main', 'dev')):
            for refused in (False, True):
                with self.subTest(base=base, now_base=now_base, auto_refused=refused):
                    res, merges = self.fallback(
                        [(15368, 'completed', 'success')],
                        base=base,
                        now_base=now_base,
                        merge_fails={'auto': refused},
                    )
                    self.assertEqual(res.returncode, 1)
                    self.assertEqual(merges, [])
                    self.assertIn(
                        f'not merged: the pull request is not rebuild/{base}- of cplieger/tb '
                        f'into {base}',
                        res.stdout,
                    )

    def fallback(self, runs, base='main', now_base=None, **over):
        fx = pr_fixture(base, **{'merge_fails': {'auto': True}, **over})
        fx['api']['GET repos/cplieger/tb/pulls/9'] = pull(
            base, f'rebuild/{base}-20261004', now_base=now_base
        )
        fx['api'][CHECKS] = check_runs(runs)
        h, res = self.open(fx, base=base)
        self.last_calls = h.calls()
        return res, merge_calls(self.last_calls)

    def main_fallback(self, runs, **over):
        return self.fallback(runs, **over)

    def test_mains_direct_merge_needs_a_green_validate_at_the_head_it_pins(self):
        res, merges = self.main_fallback([(15368, 'completed', 'success')])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            merges[1],
            [
                'gh', 'api', '-i', '-X', 'PUT', 'repos/cplieger/tb/pulls/9/merge', '--input', '-',
                '{"merge_method": "squash", "sha": "headsha"}',
            ],
        )  # fmt: skip
        self.assertIn(
            [
                'gh',
                'api',
                '-i',
                '-X',
                'DELETE',
                'repos/cplieger/tb/git/refs/heads/rebuild/main-20261004',
            ],
            self.last_calls,
        )

    def test_mains_pr_stays_open_and_fails_the_row_without_a_green_validate(self):
        cases = {
            'pending': [(15368, 'in_progress', None)],
            'missing': [],
            'red': [(15368, 'completed', 'failure')],
            'another app': [(99, 'completed', 'success')],
            'one of two red': [(15368, 'completed', 'success'), (15368, 'completed', 'failure')],
        }
        for name, runs in cases.items():
            with self.subTest(name):
                res, merges = self.main_fallback(runs)
                self.assertEqual(res.returncode, 1)
                self.assertEqual([('--auto' in c) for c in merges], [True])
                self.assertIn('`ci / validate` is not green at headsha', res.stdout)
                self.assertIn('could not enable auto-merge', res.stdout)

    def test_mains_direct_merge_refused_by_github_fails_the_row(self):
        res, merges = self.main_fallback(
            [(15368, 'completed', 'success')], merge_fails={'auto': True, 'direct': True}
        )
        self.assertEqual(res.returncode, 1)
        self.assertEqual(
            ['--auto' in c for c in merges], [True, False, False], 'a 405 is retried once'
        )
        self.assertIn('could not enable auto-merge', res.stdout)

    def test_a_base_other_than_dev_or_main_is_refused_before_any_call(self):
        h, res = self.open(pr_fixture(), base='master')
        self.assertEqual(res.returncode, 2)
        self.assertEqual(h.calls(), [])


class StalePr(unittest.TestCase):
    def body(self, *args):
        h = Harness(self)
        res = h.run(['bash', str(SCRIPT), 'stale-pr', 'tb', *args], pr_fixture(args[0]))
        self.assertEqual(res.returncode, 0, res.stderr)
        return created(h.calls())['body'].split(' Merging this empty commit')[0]

    def stale(self, fx, since=None):
        h = Harness(self)
        since = iso(NOW - 7 * DAY) if since is None else since
        argv = ['bash', str(SCRIPT), 'stale-pr', 'tb', 'main', 'latest', '10', 'L', since]
        return h, h.run(argv, fx)

    def test_reasons(self):
        self.assertEqual(
            self.body('main', 'latest', '10', '2026-09-24T04:00:00Z', ''),
            'The `:latest` image is 10 days old (built 2026-09-24T04:00:00Z).',
        )
        self.assertEqual(
            self.body('dev', 'dev', 'n/a', 'not published', ''), 'No `:dev` image is published.'
        )

    def test_a_request_merged_after_fanout_opens_no_second_pr(self):
        fx = pr_fixture()
        commits(fx, 'main', [('r1', f'{REBUILD} (#41)', NOW - 3600, 0)])
        h, res = self.stale(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            f'::notice::cplieger/tb: a rebuild request merged into main at {iso(NOW - 3600)}, '
            'so no other pull request is opened',
            res.stdout,
        )
        calls = h.calls()
        self.assertFalse([c for c in calls if 'POST' in c or 'PATCH' in c])
        self.assertEqual(merge_calls(calls), [])
        listing = next(i for i, c in enumerate(calls) if any('/pulls?' in a for a in c))
        read = next(i for i, c in enumerate(calls) if any('/commits?sha=main&' in a for a in c))
        self.assertLess(listing, read, 'the base is read again only once no PR is open')

    def test_no_request_since_fanout_opens_the_pr(self):
        fx = pr_fixture()
        commits(fx, 'main', [('r1', REBUILD, NOW - 8 * DAY, 0), ('r2', REBUILD, NOW - DAY, 2)])
        h, res = self.stale(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(created(h.calls())['base'], 'main')

    def test_an_unreadable_second_read_opens_the_pr_naming_why(self):
        h, res = self.stale(pr_fixture())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            '::warning::cplieger/tb: the rebuild requests on main could not be read again, '
            'so a rebuild pull request is opened\ngh: Not Found (HTTP 404)\n',
            res.stdout,
        )
        self.assertEqual(created(h.calls())['base'], 'main')

    def test_an_empty_since_reads_no_commits(self):
        h, res = self.stale(pr_fixture(), since='')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse([c for c in h.calls() if any('/commits?' in a for a in c)])
        self.assertEqual(created(h.calls())['base'], 'main')


MAIN_ROW = {'base': 'main', 'channel': 'latest', 'last': 'L', 'age_days': '9', 'since': ''}
DEV_ROW = {'base': 'dev', 'channel': 'dev', 'last': 'not published', 'age_days': 'n/a'}
BAD_ROW = {**MAIN_ROW, 'last': 'unreadable', 'age_days': 'n/a', 'unreadable': 'boom'}


def both_bases_fixture(**over) -> dict:
    """pr_fixture answering a rebuild pull request into each base: #9 into main, #10 into dev."""
    fx = pr_fixture(
        created_urls={
            'main': 'https://github.com/cplieger/tb/pull/9',
            'dev': 'https://github.com/cplieger/tb/pull/10',
        },
        **over,
    )
    fx['api']['GET repos/cplieger/tb/pulls/10'] = pull('dev', 'rebuild/dev-20261004')
    return fx


class Dispatch(unittest.TestCase):
    def dispatch(self, fx, bases):
        h = Harness(self)
        res = h.run(['bash', str(SCRIPT), 'dispatch', 'tb', json.dumps(bases)], fx)
        return h, res

    def opened(self, h):
        return [b['base'] for b in pull_creates(h.calls())]

    def test_each_base_is_handled_in_turn_main_first(self):
        h, res = self.dispatch(both_bases_fixture(), [MAIN_ROW, DEV_ROW])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.opened(h), ['main', 'dev'])
        calls = h.calls()
        main_done = calls.index(next(c for c in merge_calls(calls) if c[3] == '9'))
        dev_first = calls.index(
            next(c for c in calls if 'repos/cplieger/tb/git/ref/heads/dev' in c)
        )
        self.assertLess(main_done, dev_first, "main's pull request is armed before dev is read")
        self.assertEqual([c[3] for c in merge_calls(calls)], ['9', '10'])
        summary = h.summary.read_text()
        self.assertIn('| tb | main | :latest | done |', summary)
        self.assertIn('| tb | dev | :dev | done |', summary)
        dev = pull_creates(calls)[1]
        self.assertTrue(dev['body'].startswith('No `:dev` image is published.'))

    def test_a_failed_base_fails_the_leg_after_the_other_base_is_handled(self):
        fx = both_bases_fixture()
        fx['api']['GET repos/cplieger/tb/git/ref/heads/main'] = {
            'exit': 1,
            'stderr': 'gh: Server Error (HTTP 502)',
        }
        h, res = self.dispatch(fx, [MAIN_ROW, DEV_ROW])
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.opened(h), ['dev'])
        self.assertIn(
            '::error::cplieger/tb: the rebuild of main (:latest) failed. Its log is above.',
            res.stdout,
        )
        summary = h.summary.read_text()
        self.assertIn('| tb | main | :latest | failed |', summary)
        self.assertIn('| tb | dev | :dev | done |', summary)

    def test_an_unreadable_base_fails_the_leg_naming_the_image_and_rebuilds_the_other(self):
        h, res = self.dispatch(both_bases_fixture(), [BAD_ROW, DEV_ROW])
        self.assertEqual(res.returncode, 1)
        self.assertIn(
            '::error::ghcr.io/cplieger/tb:latest (main) did not read, so its age is unknown and '
            'nothing was rebuilt. boom',
            res.stdout,
        )
        self.assertEqual(self.opened(h), ['dev'])
        self.assertIn('| tb | main | :latest | unreadable |', h.summary.read_text())

    def test_only_unreadable_bases_call_nothing(self):
        h, res = self.dispatch({}, [BAD_ROW])
        self.assertEqual(res.returncode, 1)
        self.assertEqual(h.calls(), [])

    def test_a_since_bound_reaches_its_bases_rebuild_check(self):
        fx = both_bases_fixture()
        commits(fx, 'main', [('r1', REBUILD, NOW - 3600, 0)])
        h, res = self.dispatch(fx, [{**MAIN_ROW, 'since': iso(NOW - 7 * DAY)}, DEV_ROW])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.opened(h), ['dev'])

    def test_bases_that_are_not_a_json_array_of_rows_are_refused_before_any_call(self):
        for bases in ('not json', '{}', '[{"channel": "dev"}]'):
            with self.subTest(bases=bases):
                h = Harness(self)
                res = h.run(['bash', str(SCRIPT), 'dispatch', 'tb', bases], {})
                self.assertEqual(res.returncode, 2)
                self.assertEqual(h.calls(), [])


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def step(job: dict, prefix: str) -> dict:
    return next(s for s in job['steps'] if s.get('name', '').startswith(prefix))


def scope(matrix: dict) -> workflow_replay.Scope:
    return workflow_replay.Scope({'matrix': matrix, 'secrets': {}, 'steps': {}}, {}, [])


READABLE_LEG = {'repo': 'tb', 'readable': True, 'bases': [MAIN_ROW, DEV_ROW]}
UNREADABLE_LEG = {'repo': 'tb', 'readable': False, 'bases': [BAD_ROW]}


class Workflow(unittest.TestCase):
    def test_each_leg_kind_runs_exactly_its_steps(self):
        job = workflow()['jobs']['dispatch']
        names = [s['name'] for s in job['steps']]
        expect = {
            'readable': (
                READABLE_LEG,
                [
                    'Checkout',
                    'Mint the App token',
                    'Rebuild the stale images of cplieger/${{ matrix.repo }}',
                ],
            ),
            'unreadable': (
                UNREADABLE_LEG,
                ['Checkout', 'Rebuild the stale images of cplieger/${{ matrix.repo }}'],
            ),
        }
        for label, (leg, wanted) in expect.items():
            with self.subTest(label):
                ran = [
                    n
                    for n, s in zip(names, job['steps'], strict=True)
                    if scope(leg).condition(s.get('if'))
                ]
                self.assertEqual(ran, wanted)

    def test_each_leg_is_named_for_its_repository_alone(self):
        name = workflow()['jobs']['dispatch']['name']
        self.assertEqual(scope(READABLE_LEG).render(name), 'dispatch (tb)')

    def test_the_rebuild_step_hands_the_script_every_base_of_its_leg(self):
        st = step(workflow()['jobs']['dispatch'], 'Rebuild the stale images')
        self.assertEqual(
            json.loads(scope(READABLE_LEG).render(st['env']['BASES'])), [MAIN_ROW, DEV_ROW]
        )

    def test_the_fan_out_stays_inside_the_concurrent_job_margin(self):
        strategy = workflow()['jobs']['dispatch']['strategy']
        self.assertLessEqual(strategy['max-parallel'], 4)
        self.assertFalse(strategy['fail-fast'])

    def test_both_jobs_check_out_without_credentials_before_the_script(self):
        jobs = workflow()['jobs']
        for name, user in (('fanout', 'Discover stale'), ('dispatch', 'Rebuild the stale')):
            with self.subTest(name):
                steps = jobs[name]['steps']
                checkout = next(
                    i
                    for i, s in enumerate(steps)
                    if s.get('uses', '').startswith('actions/checkout@')
                )
                runner = next(i for i, s in enumerate(steps) if s['name'].startswith(user))
                self.assertLess(checkout, runner)
                self.assertEqual(steps[checkout]['with'], {'persist-credentials': False})
                self.assertIn('bash scripts/rebuild-stale.sh', steps[runner]['run'])

    def test_each_secret_reaches_only_its_steps(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('SYNC_PAT', text)
        self.assertEqual(text.count('secrets.SYNC_APP_PRIVATE_KEY'), 1)
        jobs = workflow()['jobs']
        mint = step(jobs['dispatch'], 'Mint the App token')
        self.assertEqual(mint['if'], '${{ matrix.readable }}')
        self.assertEqual(mint['id'], 'app-token')
        self.assertRegex(mint['uses'], r'^actions/create-github-app-token@[0-9a-f]{40}$')
        self.assertNotIn('continue-on-error', mint)
        self.assertEqual(
            mint['with'],
            {
                'client-id': '${{ secrets.SYNC_APP_ID }}',
                'private-key': '${{ secrets.SYNC_APP_PRIVATE_KEY }}',
                'owner': 'cplieger',
                'repositories': '${{ matrix.repo }}',
                'permission-checks': 'read',
                'permission-contents': 'write',
                'permission-metadata': 'read',
                'permission-pull-requests': 'write',
            },
        )
        pr_step = step(jobs['dispatch'], 'Rebuild the stale images')
        self.assertEqual(pr_step['env']['GH_TOKEN'], '${{ steps.app-token.outputs.token }}')
        steps = jobs['dispatch']['steps']
        self.assertLess(steps.index(mint), steps.index(pr_step))
        for name in ('fanout', 'dispatch'):
            self.assertNotIn('secrets.', json.dumps(jobs[name].get('env', {})))
        self.assertEqual(
            step(jobs['fanout'], 'Discover stale')['env'],
            {'GH_TOKEN': '${{ secrets.CI_SCHEDULE }}'},
        )

    def test_the_rebuild_step_runs_the_script_with_the_legs_bases(self):
        st = step(workflow()['jobs']['dispatch'], 'Rebuild the stale images')
        h = Harness(self)
        env = {'REPO': 'tb', 'BASES': json.dumps([MAIN_ROW, DEV_ROW])}
        res = h.run(['bash', '-c', st['run']], both_bases_fixture(), env, cwd=ROOT)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        bodies = pull_creates(h.calls())
        self.assertEqual([b['base'] for b in bodies], ['main', 'dev'])
        self.assertTrue(
            bodies[0]['body'].startswith('The `:latest` image is 9 days old (built L).')
        )


if __name__ == '__main__':
    unittest.main()
