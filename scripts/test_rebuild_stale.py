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
    where, _, query = path.partition('?')
    q = dict(urllib.parse.parse_qsl(query))
    if (method, where) == ('GET', 'repos/cplieger/tb/pulls'):
        prs = fx.get('prs', {}).get('cplieger/tb', [])
        entry = {'body': [p for p in prs if p['base']['ref'] == q.get('base', p['base']['ref'])]}
    elif (method, where) == ('POST', 'repos/cplieger/tb/pulls'):
        entry = {'body': {'html_url': fx.get('created_url', 'https://github.com/cplieger/x/pull/9')}}
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


class TwoBranchFanout(unittest.TestCase):
    def fanout(self, fx):
        h = Harness(self)
        res = h.run(['bash', str(SCRIPT), 'fanout'], fx)
        return h, res

    def test_stale_latest_and_fresh_dev_give_one_main_row(self):
        h, res = self.fanout(two_branch_fixture())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            h.matrix(),
            [
                {
                    'repo': 'tb',
                    'base': 'main',
                    'channel': 'latest',
                    'last': iso(NOW - 10 * DAY),
                    'age_days': '10',
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
        self.assertEqual({r['repo'] for r in h.matrix()}, {'tb'})

    def test_both_channels_stale_give_two_rows_main_first(self):
        fx = two_branch_fixture()
        ghcr(fx['curl'], 'tb', 'dev', iso(NOW - 8 * DAY))
        h, _ = self.fanout(fx)
        self.assertEqual(
            [(r['base'], r['channel']) for r in h.matrix()], [('main', 'latest'), ('dev', 'dev')]
        )

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
        dev = [r for r in h.matrix() if r['base'] == 'dev']
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
                main = next(r for r in h.matrix() if r['base'] == 'main')
                self.assertEqual(main['unreadable'], reason)
                self.assertEqual(main['last'], 'unreadable')
                summary = h.summary.read_text()
                self.assertIn('**unreadable**', summary)
                self.assertIn('could not be read; each fails its dispatch row.', summary)
                self.assertIn('Rebuilding 0 image(s).', summary)

    def test_a_dockerfile_on_one_base_ages_that_channel_only(self):
        fx = two_branch_fixture()
        del fx['api']['GET repos/cplieger/tb/contents/Dockerfile?ref=main']
        ghcr(fx['curl'], 'tb', 'dev', iso(NOW - 9 * DAY))
        h, _ = self.fanout(fx)
        self.assertEqual([r['base'] for r in h.matrix()], ['dev'])
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
                main = next(r for r in h.matrix() if r['base'] == 'main')
                self.assertTrue(main['unreadable'].startswith(reason), main['unreadable'])
                self.assertEqual((main['channel'], main['last']), ('latest', 'unreadable'))
                self.assertFalse(
                    [c for c in h.calls() if c[0] == 'curl' and '/manifests/latest' in c[1]]
                )
                summary = h.summary.read_text()
                self.assertIn('| tb (main, :latest) | unreadable: the Dockerfile on main', summary)
                self.assertIn('1 image(s) could not be read; each fails its dispatch row.', summary)

    def test_a_distroless_base_is_not_a_candidate(self):
        fx = two_branch_fixture()
        fx['api']['GET repos/cplieger/tb/contents/Dockerfile?ref=main'] = {
            'body': {'content': b64(SCRATCH)}
        }
        h, _ = self.fanout(fx)
        self.assertEqual(h.matrix(), [])
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
                self.assertEqual([r['base'] for r in h.matrix()], ['main'] if candidate else [])

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
                self.assertEqual(h.matrix(), [])
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
            [(r['base'], r.get('unreadable')) for r in h.matrix()],
            [
                ('main', 'the Dockerfile on main could not be parsed: Traceback: boom'),
                ('dev', 'the Dockerfile on dev could not be parsed: Traceback: boom'),
            ],
        )

    def test_the_interval_threshold_is_exclusive(self):
        fx = two_branch_fixture()
        ghcr(fx['curl'], 'tb', 'latest', iso(NOW - 7 * DAY))
        h, _ = self.fanout(fx)
        self.assertEqual(h.matrix(), [])
        ghcr(fx['curl'], 'tb', 'latest', iso(NOW - 7 * DAY - 1))
        h, _ = self.fanout(fx)
        self.assertEqual([r['base'] for r in h.matrix()], ['main'])

    def test_an_unparsable_created_time_is_an_unreadable_row(self):
        fx = two_branch_fixture()
        ghcr(fx['curl'], 'tb', 'latest', 'yesterday-ish')
        h, res = self.fanout(fx)
        self.assertEqual(res.returncode, 0, res.stderr)
        main = next(r for r in h.matrix() if r['base'] == 'main')
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
        for argv in ([], ['bogus'], ['image-created', 'tb'], ['open-pr', 'tb', 'main']):
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


def created(calls):
    """The -f fields of the one pull request create, which must answer its URL."""
    (call,) = [c for c in calls if c[1:5] == ['api', '-X', 'POST', 'repos/cplieger/tb/pulls']]
    assert call[-2:] == ['--jq', '.html_url'], call
    return dict(call[i + 1].split('=', 1) for i, a in enumerate(call) if a == '-f')


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
            ' request; moving it to the new commit\n',
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
        self.assertEqual(len(merges), 2)
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

    def test_reasons(self):
        self.assertEqual(
            self.body('main', 'latest', '10', '2026-09-24T04:00:00Z'),
            'The `:latest` image is 10 days old (built 2026-09-24T04:00:00Z).',
        )
        self.assertEqual(
            self.body('dev', 'dev', 'n/a', 'not published'), 'No `:dev` image is published.'
        )


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def step(job: dict, prefix: str) -> dict:
    return next(s for s in job['steps'] if s.get('name', '').startswith(prefix))


def condition(st: dict, matrix: dict) -> bool:
    scope = workflow_replay.Scope({'matrix': matrix}, {}, [])
    return scope.condition(st.get('if'))


TB_ROW = {'repo': 'tb', 'base': 'main', 'channel': 'latest', 'last': 'x', 'age_days': '9'}
BAD_ROW = {**TB_ROW, 'last': 'unreadable', 'age_days': 'n/a', 'unreadable': 'boom'}


class Workflow(unittest.TestCase):
    def test_each_row_kind_runs_exactly_its_steps(self):
        job = workflow()['jobs']['dispatch']
        names = [s['name'] for s in job['steps']]
        expect = {
            'readable': (
                TB_ROW,
                [
                    'Checkout',
                    'Mint the App token',
                    'Open a rebuild pull request on cplieger/${{ matrix.repo }}',
                ],
            ),
            'unreadable': (BAD_ROW, ['Report an unreadable image on cplieger/${{ matrix.repo }}']),
        }
        for label, (row, wanted) in expect.items():
            with self.subTest(label):
                ran = [n for n, s in zip(names, job['steps'], strict=True) if condition(s, row)]
                self.assertEqual(ran, wanted)

    def test_the_pr_steps_base_is_the_rows(self):
        st = step(workflow()['jobs']['dispatch'], 'Open a rebuild pull request')
        for row, base in ((TB_ROW, 'main'), ({**TB_ROW, 'base': 'dev'}, 'dev')):
            scope = workflow_replay.Scope({'matrix': row, 'secrets': {}}, {}, [])
            self.assertEqual(scope.render(st['env']['BASE']), base)

    def test_both_jobs_check_out_without_credentials_before_the_script(self):
        jobs = workflow()['jobs']
        for name, user in (('fanout', 'Discover stale'), ('dispatch', 'Open a rebuild')):
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
        self.assertEqual(mint['if'], '${{ !matrix.unreadable }}')
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
        pr_step = step(jobs['dispatch'], 'Open a rebuild pull request')
        self.assertEqual(pr_step['env']['GH_TOKEN'], '${{ steps.app-token.outputs.token }}')
        steps = jobs['dispatch']['steps']
        self.assertLess(steps.index(mint), steps.index(pr_step))
        for name in ('fanout', 'dispatch'):
            self.assertNotIn('secrets.', json.dumps(jobs[name].get('env', {})))
        self.assertEqual(
            step(jobs['fanout'], 'Discover stale')['env'],
            {'GH_TOKEN': '${{ secrets.CI_SCHEDULE }}'},
        )

    def test_an_unreadable_row_fails_naming_the_image(self):
        st = step(workflow()['jobs']['dispatch'], 'Report an unreadable image')
        env = {
            **os.environ,
            'REPO': 'tb',
            'BASE': 'main',
            'CHANNEL': 'latest',
            'UNREADABLE': 'GHCR token endpoint answered HTTP 403',
        }
        res = subprocess.run(
            ['bash', '-c', st['run']], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn(
            '::error::ghcr.io/cplieger/tb:latest (main) did not read, so its age is unknown and nothing'
            ' was rebuilt. GHCR token endpoint answered HTTP 403',
            res.stdout,
        )

    def test_the_pr_step_runs_the_script_with_the_row(self):
        st = step(workflow()['jobs']['dispatch'], 'Open a rebuild pull request')
        h = Harness(self)
        res = h.run(
            ['bash', '-c', st['run']],
            pr_fixture(),
            {'REPO': 'tb', 'BASE': 'main', 'CHANNEL': 'latest', 'AGE_DAYS': '9', 'LAST': 'L'},
            cwd=ROOT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(
            created(h.calls())['body'].startswith('The `:latest` image is 9 days old (built L).')
        )


if __name__ == '__main__':
    unittest.main()
