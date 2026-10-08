"""security-scan.yaml and daily-security.yaml: a repository whose default branch is
main runs HEAD's steps and calls (testdata/security-scan/head.json, generated from
HEAD before the published mode existed), and a two-branch repository's main scans
its published image."""

from __future__ import annotations

import fnmatch
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
WORKFLOWS = ROOT / '.github' / 'workflows'
TESTDATA = SCRIPTS / 'testdata'
HEAD = json.loads((TESTDATA / 'security-scan' / 'head.json').read_text())

_spec = importlib.util.spec_from_file_location('workflow_replay', SCRIPTS / 'workflow_replay.py')
workflow_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workflow_replay)

PUBLISHED_JOBS = ('published-image', 'trivy-published', 'govulncheck', 'summary')
BUILD_STEPS = (
    'Set up Docker Buildx',
    'Resolve OS package refresh key',
    'Build image (no push)',
    'Trivy image scan (advisory)',
)
PUBLISHED_STEPS = ('Trivy filesystem scan for the main summary', 'Upload the filesystem report')
# (default branch, event, ref) of every run a repository can start today, then
# the one the published mode exists for.
TODAY = [
    ('main', 'pull_request', 'refs/pull/7/merge'),
    ('main', 'workflow_dispatch', 'refs/heads/main'),
    ('dev', 'pull_request', 'refs/pull/7/merge'),
    ('dev', 'workflow_dispatch', 'refs/heads/dev'),
]
PUBLISHED = ('dev', 'workflow_dispatch', 'refs/heads/main')


class Scope(workflow_replay.Scope):
    def functions(self):
        return {**super().functions(), 'hashFiles': lambda *_: 'h'}


def scope(**contexts) -> Scope:
    return Scope(contexts, {}, [])


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def unpinned(node):
    """The node with every `uses:` digest dropped: an action bump changes no step."""
    if isinstance(node, dict):
        return {
            k: v.split('@', 1)[0] if k == 'uses' and isinstance(v, str) else unpinned(v)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [unpinned(v) for v in node]
    return node


def github(default: str, event: str, ref: str, *, private=False, fork=False) -> dict:
    return {
        'event_name': event,
        'ref': ref,
        'event': {'repository': {'default_branch': default, 'private': private, 'fork': fork}},
    }


def bash(body: str, env: dict, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['bash', '-e', '-c', body],
        cwd=cwd,
        env={**os.environ, **env},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def profile(
    job: dict, ctx: tuple[str, str, str], *, dockerfile: bool, **repo: bool
) -> dict[str, str]:
    """The Resolve profile step's outputs, run in a tree with or without a Dockerfile."""
    step = next(s for s in job['steps'] if s.get('id') == 'profile')
    ref = ctx[2]
    sc = scope(github=github(*ctx, **repo))
    env = {k: sc.render(v) for k, v in (step.get('env') or {}).items()}
    with tempfile.TemporaryDirectory() as tmp:
        tree = pathlib.Path(tmp)
        if dockerfile:
            (tree / 'Dockerfile').write_text('FROM scratch\n')
        out = tree / 'out'
        out.touch()
        proc = bash(step['run'], {**env, 'GITHUB_OUTPUT': str(out), 'GITHUB_REF': ref}, tree)
        assert proc.returncode == 0, proc.stderr
        return workflow_replay.read_kv_file(out)


def running(job: dict, ctx: tuple[str, str, str], outputs: dict[str, str]) -> list[str]:
    sc = scope(
        github=github(*ctx),
        steps={'profile': {'outputs': outputs}},
        inputs={'severity': 'HIGH,CRITICAL', 'dockerfile': './Dockerfile'},
        env={},
    )
    return [s['name'] for s in job['steps'] if sc.condition(s.get('if'))]


def needs(published: str, image: str = 'true', platforms: str = '') -> dict:
    return {
        'trivy': {'outputs': {'published': published, 'image': image}, 'result': 'success'},
        'published-image': {'outputs': {'platforms': platforms}, 'result': 'success'},
    }


class SecurityScanForMainDefaultRepos(unittest.TestCase):
    def setUp(self):
        self.wf = unpinned(workflow('security-scan.yaml'))
        self.head = unpinned(HEAD['security-scan.yaml'])

    def test_every_run_today_resolves_head_s_profile_and_runs_head_s_steps(self):
        new, old = self.wf['jobs']['trivy'], self.head['trivy']
        for ctx in TODAY:
            for dockerfile in (True, False):
                with self.subTest(ctx=ctx, dockerfile=dockerfile):
                    got = profile(new, ctx, dockerfile=dockerfile)
                    want = profile(old, ctx, dockerfile=dockerfile)
                    self.assertEqual(got, {**want, 'published': 'false'})
                    self.assertEqual(running(new, ctx, got), running(old, ctx, want))

    def test_every_head_step_is_unchanged_but_for_its_condition(self):
        new = {s['name']: s for s in self.wf['jobs']['trivy']['steps']}
        for step in self.head['trivy']['steps']:
            with self.subTest(step=step['name']):
                got = dict(new[step['name']])
                if step.get('id') == 'profile':
                    self.assertTrue(got.pop('run').startswith(step['run']))
                    got.pop('env')
                    step = {k: v for k, v in step.items() if k != 'run'}
                self.assertEqual(
                    {k: v for k, v in got.items() if k != 'if'},
                    {k: v for k, v in step.items() if k != 'if'},
                )
        names = [s['name'] for s in self.wf['jobs']['trivy']['steps']]
        self.assertEqual(
            [n for n in names if n not in PUBLISHED_STEPS],
            [s['name'] for s in self.head['trivy']['steps']],
        )

    def test_the_trivy_job_only_gains_outputs_and_the_history_scan_is_untouched(self):
        job = {k: v for k, v in self.wf['jobs']['trivy'].items() if k not in ('outputs', 'steps')}
        self.assertEqual(job, {k: v for k, v in self.head['trivy'].items() if k != 'steps'})
        self.assertEqual(self.wf['jobs']['gitleaks-history'], self.head['gitleaks-history'])
        self.assertEqual(sorted(self.wf['jobs']), sorted([*self.head['jobs'], *PUBLISHED_JOBS]))

    def test_no_published_job_runs_outside_the_published_mode(self):
        for ctx in TODAY:
            sc = scope(github=github(*ctx), needs=needs('false', platforms='[]'))
            for name in PUBLISHED_JOBS:
                with self.subTest(ctx=ctx, job=name):
                    self.assertFalse(sc.condition(self.wf['jobs'][name]['if']))


class SecurityScanPublishedMode(unittest.TestCase):
    def setUp(self):
        self.wf = workflow('security-scan.yaml')
        self.jobs = self.wf['jobs']

    def test_main_of_a_two_branch_repo_scans_instead_of_building(self):
        out = profile(self.jobs['trivy'], PUBLISHED, dockerfile=True)
        self.assertEqual(out, {'image': 'true', 'published': 'true'})
        steps = running(self.jobs['trivy'], PUBLISHED, out)
        self.assertTrue(set(PUBLISHED_STEPS) <= set(steps))
        self.assertFalse(set(BUILD_STEPS) & set(steps))
        self.assertIn('Trivy filesystem scan (advisory)', steps)

    def test_a_pull_request_into_main_or_a_main_default_main_is_not_published(self):
        for ctx in (
            ('dev', 'pull_request', 'refs/heads/main'),
            ('main', 'workflow_dispatch', 'refs/heads/main'),
            ('dev', 'push', 'refs/heads/dev'),
        ):
            with self.subTest(ctx=ctx):
                out = profile(self.jobs['trivy'], ctx, dockerfile=True)
                self.assertEqual(out['published'], 'false')

    def test_main_of_a_private_or_forked_dev_default_repo_is_not_published(self):
        for repo in ({'private': True}, {'fork': True}):
            with self.subTest(**repo):
                out = profile(self.jobs['trivy'], PUBLISHED, dockerfile=True, **repo)
                self.assertEqual(out, {'image': 'true', 'published': 'false'})

    def test_the_jobs_chain_on_the_profile_and_the_resolved_platforms(self):
        rows = json.dumps([{'platform': 'linux/amd64', 'slug': 'linux-amd64', 'ref': 'r'}])
        sc = scope(github=github(*PUBLISHED), needs=needs('true', platforms=rows))
        for name in PUBLISHED_JOBS:
            with self.subTest(job=name):
                self.assertTrue(sc.condition(self.jobs[name]['if']))
        no_image = scope(github=github(*PUBLISHED), needs=needs('true', image='false'))
        self.assertFalse(no_image.condition(self.jobs['published-image']['if']))
        self.assertTrue(no_image.condition(self.jobs['summary']['if']))
        for platforms in ('', '[]'):
            sc = scope(github=github(*PUBLISHED), needs=needs('true', platforms=platforms))
            self.assertFalse(sc.condition(self.jobs['trivy-published']['if']))
        self.assertEqual(self.jobs['summary']['needs'], ['trivy', *PUBLISHED_JOBS[:-1]])

    def test_each_platform_uploads_its_own_sarif_category_and_report(self):
        steps = {s['name']: s for s in self.jobs['trivy-published']['steps']}
        sarif = steps['Upload SARIF — published image']['with']['category']
        self.assertEqual(sarif, 'trivy-image-published-${{ matrix.slug }}')
        json_scan = steps['Trivy published image scan for the main summary']['with']
        self.assertEqual(json_scan['output'], 'trivy-image-${{ matrix.slug }}.json')
        self.assertEqual(json_scan['image-ref'], '${{ matrix.ref }}')
        upload = steps['Upload the image report']['with']
        self.assertEqual(upload['name'], 'security-scan-image-${{ matrix.slug }}')

    def test_the_resolve_step_names_the_repository_without_its_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = pathlib.Path(tmp)
            (tools / 'scan_coverage.py').write_text(
                'import sys\nprint("argv=" + " ".join(sys.argv[1:]))\n'
            )
            out = tools / 'out'
            out.touch()
            step = self.jobs['published-image']['steps'][1]
            env = {
                'CI_TOOLS': str(tools),
                'GITHUB_OUTPUT': str(out),
                'GITHUB_REPOSITORY': 'cplieger/demo',
            }
            proc = bash(step['run'], env, tools)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(out.read_text(), 'argv=platforms demo\n')

    def test_the_signed_sbom_of_the_resolved_index_reaches_the_summary(self):
        steps = {s['name']: s for s in self.jobs['published-image']['steps']}
        read = steps['Read the signed SBOM of the published image']
        for index, want in (('sha256:i', True), ('', False)):
            sc = scope(steps={'resolve': {'outputs': {'index': index}}})
            for name in ('Install cosign', read['name']):
                with self.subTest(index=index, step=name):
                    self.assertEqual(sc.condition(steps[name]['if']), want)
        with tempfile.TemporaryDirectory() as tmp:
            tools = pathlib.Path(tmp)
            (tools / 'scan_coverage.py').write_text(
                'import sys\nprint("argv=" + " ".join(sys.argv[1:]))\n'
            )
            env = {
                'CI_TOOLS': str(tools),
                'INDEX': 'sha256:i',
                'GITHUB_REPOSITORY': 'cplieger/demo',
            }
            proc = bash(read['run'], env, tools)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, 'argv=sbom demo sha256:i --out sbom.json\n')
        self.assertEqual(
            read['env']['INDEX'],
            '${{ steps.resolve.outputs.index }}',
        )
        upload = steps['Upload the SBOM report']['with']
        self.assertEqual((upload['name'], upload['path']), ('security-scan-sbom', 'sbom.json'))
        download = next(
            s for s in self.jobs['summary']['steps'] if s['name'] == 'Download the scan reports'
        )['with']
        self.assertTrue(fnmatch.fnmatchcase(upload['name'], download['pattern']))
        self.assertEqual(download['merge-multiple'], 1)

    def test_govulncheck_then_summary_produce_one_complete_record(self):
        """The govulncheck and summary run bodies, end to end, with stub go and
        govulncheck binaries and the real scan_coverage.py."""
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            tree, temp, stubs = base / 'checkout', base / 'runner', base / 'bin'
            for d in (tree, temp, stubs):
                d.mkdir()
            git_env = {'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
            for name, body in {
                'go.mod': 'module x\n',
                'main.go': 'package main\n',
                'lane/go.mod': 'module x/lane\n',
                'lane/l.go': 'package lane\n',
                'Dockerfile': 'FROM scratch\n',
            }.items():
                (tree / name).parent.mkdir(exist_ok=True)
                (tree / name).write_text(body)
            subprocess.run(
                ['git', 'init', '-q'], cwd=tree, check=True, env={**os.environ, **git_env}
            )
            subprocess.run(
                ['git', 'add', '-A'], cwd=tree, check=True, env={**os.environ, **git_env}
            )
            report = (TESTDATA / 'scan-coverage' / 'govulncheck-root.json').read_text()
            (base / 'report.json').write_text(report)
            (stubs / 'go').write_text('#!/bin/sh\nexit 0\n')
            (stubs / 'govulncheck').write_text(
                '#!/bin/sh\n'
                'echo "$@" >> "$STUB_LOG"\n'
                'if [ "$2" = lane ]; then echo "lane: no packages" >&2; exit 1; fi\n'
                f'cat {base / "report.json"}\n'
            )
            for stub in ('go', 'govulncheck'):
                (stubs / stub).chmod(0o755)
            env = {
                **git_env,
                'PATH': f'{stubs}:{os.environ["PATH"]}',
                'RUNNER_TEMP': str(temp),
                'STUB_LOG': str(base / 'calls'),
                'CI_TOOLS': str(SCRIPTS),
                'GITHUB_OUTPUT': str(base / 'out'),
            }
            steps = {s['name']: s for s in self.jobs['govulncheck']['steps']}
            listed = bash(steps['List the Go modules']['run'], env, tree)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual((base / 'out').read_text(), 'any=true\n')
            ran = bash(
                steps['Run govulncheck per module (advisory)']['run'],
                {**env, 'GOTOOLCHAIN': 'auto'},
                tree,
            )
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertEqual(
                (base / 'calls').read_text(),
                '-C . -format json ./...\n-C lane -format json ./...\n',
            )
            reports = temp / 'scan-reports'
            shutil.copytree(tree / 'govulncheck-reports', reports)
            shutil.copy(TESTDATA / 'scan-coverage' / 'trivy-fs.json', reports / 'trivy-fs.json')
            summarize = next(
                s
                for s in self.jobs['summary']['steps']
                if s['name'] == 'Summarize the scan of main'
            )
            proc = bash(
                summarize['run'],
                {
                    **env,
                    'GITHUB_REPOSITORY': 'cplieger/demo',
                    'GITHUB_SHA': 'f' * 40,
                    'GITHUB_STEP_SUMMARY': str(base / 'summary.md'),
                    'DOCKERFILE': './Dockerfile',
                    'IMAGE': 'false',
                    'PLATFORMS': '',
                    'INDEX': '',
                    'IMAGE_ERROR': '',
                    'IMAGE_JOB': 'skipped',
                },
                tree,
            )
            self.assertEqual(proc.returncode, 1)
            doc = json.loads((tree / 'security-main.json').read_text())
            self.assertEqual(doc['repo'], 'cplieger/demo')
            self.assertEqual(doc['commit'], 'f' * 40)
            self.assertEqual(doc['errors'], ['govulncheck of lane: failed: lane: no packages'])
            self.assertIn('govulncheck', {s for f in doc['findings'] for s in f['sources']})


GH_STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1 $2" in
  "api --paginate") exec jq -r "$5" "$STUB_REPOS" ;;
  "api repos/"*)
    [ -z "${STUB_READ_FAILS:-}" ] || { echo "gh: Bad credentials (HTTP 401)" >&2; exit 1; }
    exec jq -r --arg n "${2#repos/cplieger/}" '.[] | select(.name == $n) | .default_branch // ""' "$STUB_REPOS" ;;
  "workflow view")
    [ -z "${STUB_DRAIN:-}" ] || cat >/dev/null
    case " $STUB_SECURITY " in *" ${5#cplieger/} "*) exit 0 ;; esac; exit 1 ;;
  "workflow run") exit 0 ;;
esac
exit 9
"""
REPOS = [
    {
        'name': 'docker-age',
        'default_branch': 'main',
        'archived': False,
        'fork': False,
        'visibility': 'public',
    },
    {
        'name': 'subflux',
        'default_branch': 'dev',
        'archived': False,
        'fork': False,
        'visibility': 'public',
    },
    {
        'name': 'tool-catalog',
        'default_branch': 'dev',
        'archived': False,
        'fork': False,
        'visibility': 'public',
    },
    {
        'name': 'quiet',
        'default_branch': 'main',
        'archived': False,
        'fork': False,
        'visibility': 'public',
    },
    {
        'name': 'private-repo',
        'default_branch': 'dev',
        'archived': False,
        'fork': False,
        'visibility': 'private',
    },
    {
        'name': 'loki',
        'default_branch': 'dev',
        'archived': False,
        'fork': True,
        'visibility': 'public',
    },
    {
        'name': 'old',
        'default_branch': 'dev',
        'archived': True,
        'fork': False,
        'visibility': 'public',
    },
]


class DailySecurity(unittest.TestCase):
    def setUp(self):
        self.wf = workflow('daily-security.yaml')
        self.head = HEAD['daily-security.yaml']
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        (self.base / 'gh').write_text(GH_STUB)
        (self.base / 'gh').chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def fanout(
        self, body: str, repos: list[dict], security: str, drain: bool = False
    ) -> tuple[list, list[str]]:
        (self.base / 'repos.json').write_text(json.dumps(repos))
        log, out = self.base / 'calls', self.base / 'out'
        for f in (log, out):
            f.write_text('')
        env = {
            'PATH': f'{self.base}:{os.environ["PATH"]}',
            'STUB_LOG': str(log),
            'STUB_REPOS': str(self.base / 'repos.json'),
            'STUB_SECURITY': security,
            'STUB_DRAIN': '1' if drain else '',
            'GITHUB_OUTPUT': str(out),
        }
        proc = bash(body, env, ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        matrix = json.loads(workflow_replay.read_kv_file(out)['matrix'])
        return matrix, log.read_text().splitlines()

    def new_fanout(self) -> str:
        steps = self.wf['jobs']['fanout']['steps']
        self.assertEqual(steps[0]['with'], {'persist-credentials': False})
        return steps[1]['run']

    def test_main_default_rows_and_calls_are_head_s_and_dev_default_repos_add_main(self):
        security = 'docker-age subflux tool-catalog private-repo loki old'
        new, new_calls = self.fanout(self.new_fanout(), REPOS, security)
        old, old_calls = self.fanout(self.head['fanout-run'], REPOS, security)
        self.assertEqual(
            old, [{'repo': 'docker-age'}, {'repo': 'subflux'}, {'repo': 'tool-catalog'}]
        )
        self.assertEqual([r for r in new if 'ref' not in r], old)
        self.assertEqual([r for r in new if 'ref' in r], [{'repo': 'subflux', 'ref': 'main'}])
        self.assertEqual(new.index({'repo': 'subflux', 'ref': 'main'}), 2)

        def views(calls):
            return [c for c in calls if c.startswith('workflow ')]

        self.assertEqual(views(new_calls), views(old_calls))
        self.assertEqual(len(views(old_calls)), 4)

    def test_a_gh_that_reads_stdin_does_not_swallow_the_rest_of_the_list(self):
        security = 'docker-age subflux tool-catalog private-repo loki old'
        drained, _ = self.fanout(self.new_fanout(), REPOS, security, drain=True)
        plain, _ = self.fanout(self.new_fanout(), REPOS, security)
        self.assertEqual(drained, plain)
        self.assertEqual(len(plain), 4)

    def test_no_repo_with_security_yml_is_an_empty_matrix(self):
        new, _ = self.fanout(self.new_fanout(), REPOS, '')
        self.assertEqual(new, [])
        # HEAD emitted one row with an empty name here and dispatched to `cplieger/`.
        old, _ = self.fanout(self.head['fanout-run'], REPOS, '')
        self.assertEqual(old, [{'repo': ''}])

    def dispatch(self, step: dict, row: dict, rc: int = 0, **extra: str) -> str:
        sc = scope(matrix=row)
        env = {k: sc.render(v) for k, v in (step.get('env') or {}).items()}
        log = self.base / 'calls'
        log.write_text('')
        (self.base / 'repos.json').write_text(json.dumps(REPOS))
        env.update(
            PATH=f'{self.base}:{os.environ["PATH"]}',
            STUB_LOG=str(log),
            STUB_REPOS=str(self.base / 'repos.json'),
            **extra,
        )
        proc = bash(sc.render(step['run']), env, ROOT)
        self.assertEqual(proc.returncode, rc, proc.stderr)
        return log.read_text()

    def test_each_row_runs_exactly_one_dispatch_and_a_default_row_runs_head_s_call(self):
        steps = self.wf['jobs']['dispatch']['steps']
        (old,) = self.head['dispatch-steps']
        self.assertEqual(steps[0]['name'], old['name'])
        for row, want in (
            ({'repo': 'docker-age'}, [steps[0]['name']]),
            ({'repo': 'subflux', 'ref': 'main'}, [steps[1]['name']]),
        ):
            with self.subTest(row=row):
                sc = scope(matrix=row)
                self.assertEqual([s['name'] for s in steps if sc.condition(s.get('if'))], want)
        row = {'repo': 'docker-age'}
        head = self.dispatch(old, row)
        self.assertEqual(head, 'workflow run security.yml -R cplieger/docker-age\n')
        # HEAD's dispatch, with the default branch read over REST and passed as --ref.
        self.assertEqual(
            self.dispatch(steps[0], row).splitlines(),
            ['api repos/cplieger/docker-age --jq .default_branch', f'{head.strip()} --ref main'],
        )

    def test_a_default_branch_that_cannot_be_read_dispatches_nothing(self):
        step = self.wf['jobs']['dispatch']['steps'][0]
        calls = self.dispatch(step, {'repo': 'docker-age'}, rc=1, STUB_READ_FAILS='1')
        self.assertNotIn('workflow run', calls)
        calls = self.dispatch(step, {'repo': 'not-listed'}, rc=1)
        self.assertNotIn('workflow run', calls)

    def test_the_main_dispatch_passes_the_ref(self):
        step = self.wf['jobs']['dispatch']['steps'][1]
        self.assertEqual(
            self.dispatch(step, {'repo': 'subflux', 'ref': 'main'}),
            'workflow run security.yml -R cplieger/subflux --ref main\n',
        )


if __name__ == '__main__':
    sys.exit(unittest.main())
