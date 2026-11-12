"""The weekly runs take public repositories only, and a job running one's code holds no secret.

This repository's run logs and artifacts are world-readable, so every fanout keeps
only repositories that are public, not archived and not forks, and a target is
cloned anonymously. A job that runs a target's code (its tests, mutants, fuzz
targets or benchmarks) then references no secret: GitHub hands every secret a job
references to its runner, where that code could read it. CI_SCHEDULE stays in the
jobs that list, publish or file issues and run no target code. Those jobs take each
repo from their own matrix and read only the artifacts named for it, because target
code writes the artifacts they read. The fanouts and the bench smoke, stryker stage,
badge and tracker steps are replayed with stub binaries.
"""

from __future__ import annotations

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
from contextlib import redirect_stderr

import yaml

SCRIPTS = pathlib.Path(__file__).resolve().parent
WORKFLOWS = SCRIPTS.parent / '.github' / 'workflows'
SECRET = re.compile(r'secrets\.|github\.token')

_spec = importlib.util.spec_from_file_location('workflow_replay', SCRIPTS / 'workflow_replay.py')
workflow_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workflow_replay)

# Scheduled jobs that run a target repository's code.
TARGET_CODE_JOBS = {
    ('mutation-stryker-repo.yaml', 'plan'),
    ('mutation-stryker-repo.yaml', 'run'),
    ('mutation-gremlins-repo.yaml', 'plan'),
    ('mutation-gremlins-repo.yaml', 'run'),
    ('weekly-fuzz.yaml', 'fuzz'),
    ('weekly-bench.yaml', 'smoke'),
    ('weekly-bench.yaml', 'run'),
}
# Scheduled jobs that hold CI_SCHEDULE or the run's token and run no target code; a
# `repo` job only passes CI_SCHEDULE to the per-repository workflow it calls.
NO_TARGET_CODE = {
    ('weekly-stryker.yaml', 'fanout'),
    ('weekly-stryker.yaml', 'repo'),
    ('mutation-stryker-repo.yaml', 'shard'),
    ('mutation-stryker-repo.yaml', 'badge'),
    ('mutation-stryker-repo.yaml', 'aggregate'),
    ('weekly-gremlins.yaml', 'fanout'),
    ('weekly-gremlins.yaml', 'repo'),
    ('mutation-gremlins-repo.yaml', 'shard'),
    ('mutation-gremlins-repo.yaml', 'badge'),
    ('mutation-gremlins-repo.yaml', 'aggregate'),
    ('weekly-fuzz.yaml', 'fanout'),
    ('weekly-fuzz.yaml', 'report'),
    ('weekly-bench.yaml', 'fanout'),
    ('weekly-bench.yaml', 'publish'),
    ('weekly-bench.yaml', 'aggregate'),
    ('weekly-links.yaml', 'fanout'),
    ('weekly-links.yaml', 'run'),
}
# The workflows that list repositories, and the per-repository ones they call.
LISTING_WORKFLOWS = [
    'weekly-bench.yaml',
    'weekly-fuzz.yaml',
    'weekly-gremlins.yaml',
    'weekly-stryker.yaml',
]
SCHEDULED = ('weekly-*.yaml', 'mutation-*-repo.yaml')
TOOLCHAIN = ('actions/setup-go@', 'actions/setup-node@')


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def references_secret(node) -> bool:
    return bool(SECRET.search(json.dumps(node)))


class TokenScope(unittest.TestCase):
    def test_a_target_code_job_references_no_secret(self):
        for name, job_name in sorted(TARGET_CODE_JOBS):
            with self.subTest(f'{name}:{job_name}'):
                workflow = load(name)
                self.assertFalse(references_secret(workflow.get('env', {})))
                self.assertFalse(references_secret(workflow['jobs'][job_name]))

    def test_every_scheduled_job_referencing_a_secret_runs_no_target_code(self):
        found = set()
        for path in sorted(p for g in SCHEDULED for p in WORKFLOWS.glob(g)):
            workflow = yaml.safe_load(path.read_text())
            self.assertFalse(references_secret(workflow.get('env', {})), path.name)
            for job_name, job in workflow['jobs'].items():
                if references_secret(job):
                    found.add((path.name, job_name))
        self.assertEqual(found, NO_TARGET_CODE)

    def test_a_job_classified_as_running_no_target_code_sets_up_no_toolchain(self):
        for name, job_name in sorted(NO_TARGET_CODE):
            for s in load(name)['jobs'][job_name].get('steps', []):
                with self.subTest(f'{name}:{job_name}', step=s.get('name')):
                    self.assertFalse(s.get('uses', '').startswith(TOOLCHAIN))

    def test_no_scheduled_clone_carries_an_auth_header(self):
        for path in sorted(p for g in SCHEDULED for p in WORKFLOWS.glob(g)):
            with self.subTest(path.name):
                self.assertNotIn('extraheader', path.read_text())
        self.assertNotIn('extraheader', (SCRIPTS / 'gremlins-prepare.sh').read_text())


# One page of `user/repos`. Every fanout keeps xmlx and zpub only; xmlx and
# jsonx are bench-enrolled, zpub and zpriv are not.
LISTING = [
    {'name': 'xmlx', 'visibility': 'public', 'private': False, 'archived': False, 'fork': False},
    {'name': 'jsonx', 'visibility': 'private', 'private': True, 'archived': False, 'fork': False},
    {'name': 'ssrf', 'private': False, 'archived': False, 'fork': False},
    {'name': 'httpx', 'visibility': None, 'private': False, 'archived': False, 'fork': False},
    {
        'name': 'jsoncap',
        'visibility': 'internal',
        'private': True,
        'archived': False,
        'fork': False,
    },
    {'name': 'runesafe', 'visibility': 'public', 'private': False, 'archived': True, 'fork': False},
    {'name': 'keyenc', 'visibility': 'public', 'private': False, 'archived': False, 'fork': True},
    {'name': 'zpub', 'visibility': 'public', 'private': False, 'archived': False, 'fork': False},
    {'name': 'zpriv', 'visibility': 'private', 'private': True, 'archived': False, 'fork': False},
]
PUBLIC = ['xmlx', 'zpub']
# A weekly workflow runs in cplieger/ci itself, so its ci-source checkout names
# github.repository at github.sha.
WEEKLY_GITHUB = {'repository': 'cplieger/ci', 'sha': '0' * 40}
SHA = '0123456789abcdef0123456789abcdef01234567'
FANOUT_GH_STUB = """#!/usr/bin/env bash
case "$*" in
  *user/repos*)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --jq ]; then jq -r "$2" "$LISTING"; exit; fi
      shift
    done
    exit 1 ;;
  */contents/go.mod*) exit 0 ;;
  */git/trees/*) echo 20000 ;;
  *) exit 1 ;;
esac
"""
# A clone holds a Go module with a fuzz target ($TREES/<repo>.fuzz of them when
# that file exists) and a benchmark, and a stryker config in each dir
# $TREES/<repo> lists (the root when it lists none). Each dir $TREES/<repo>.mods
# lists is a nested module with one fuzz target and one benchmark, and with
# $TREES/<repo>.noroot the root holds no module. With $TREES/<repo>.nohead the
# clone succeeds but leaves no resolvable HEAD, and with $TREES/<repo>.noclone it
# fails.
FANOUT_GIT_STUB = """#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
if [ "$1" = -C ]; then [ -f "$2/.nohead" ] && exit 128; echo "$SHA"; exit 0; fi
dest="${@: -1}"
r=$(basename "${@: -2:1}" .git)
[ -f "$TREES/$r.noclone" ] && exit 128
mkdir -p "$dest"
[ -f "$TREES/$r.nohead" ] && : >"$dest/.nohead"
if [ ! -f "$TREES/$r.noroot" ]; then
  printf 'module example.com/%s\n' "$r" >"$dest/go.mod"
  printf 'package x\nfunc FuzzX(f *testing.F) {}\nfunc BenchmarkX(b *testing.B) {}\n' >"$dest/x_test.go"
  [ -f "$TREES/$r.fuzz" ] && seq 2 "$(cat "$TREES/$r.fuzz")" | sed 's/.*/func FuzzX&(f *testing.F) {}/' >>"$dest/x_test.go"
fi
if [ -f "$TREES/$r.mods" ]; then
  while IFS= read -r m; do
    mkdir -p "$dest/$m"
    printf 'module example.com/%s/%s\n' "$r" "$m" >"$dest/$m/go.mod"
    printf 'package m\n' >"$dest/$m/m.go"
    printf 'package m\nfunc FuzzM(f *testing.F) {}\nfunc BenchmarkM(b *testing.B) {}\n' >"$dest/$m/m_test.go"
  done <"$TREES/$r.mods"
fi
dirs=.
[ -f "$TREES/$r" ] && dirs=$(cat "$TREES/$r")
while IFS= read -r d; do
  mkdir -p "$dest/$d"
  : >"$dest/$d/stryker.config.json"
done <<<"$dirs"
"""


def listing_programs(name: str) -> list[str]:
    programs = []
    for job in load(name)['jobs'].values():
        for s in job.get('steps', []):
            body = s.get('run', '')
            if 'user/repos' in body:
                found = re.search(r"--jq '(.*?)'", body, re.DOTALL)
                programs.append(found.group(1) if found else '')
    return programs


class FanoutReplay:
    def fanout(
        self,
        name: str,
        github: dict | None = None,
        fuzz: dict | None = None,
        nohead: tuple[str, ...] = (),
        mods: dict[str, list[str]] | None = None,
        noroot: tuple[str, ...] = (),
        extra: tuple[str, ...] = (),
        noclone: tuple[str, ...] = (),
    ) -> dict:
        """Replays a fanout; `extra` adds public live repositories to the listing."""
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        bindir, trees = tmp / 'bin', tmp / 'trees'
        bindir.mkdir()
        trees.mkdir()
        for stub, body in (('gh', FANOUT_GH_STUB), ('git', FANOUT_GIT_STUB)):
            (bindir / stub).write_text(body)
            (bindir / stub).chmod(0o755)
        listing = LISTING + [
            {'name': r, 'visibility': 'public', 'private': False, 'archived': False, 'fork': False}
            for r in extra
        ]
        (tmp / 'listing.json').write_text(json.dumps(listing))
        (trees / 'xmlx').write_text('.\nweb/app\nodd[1]\n')
        for repo, targets in (fuzz or {}).items():
            (trees / f'{repo}.fuzz').write_text(str(targets))
        for repo in nohead:
            (trees / f'{repo}.nohead').write_text('')
        for repo, dirs in (mods or {}).items():
            (trees / f'{repo}.mods').write_text(''.join(f'{d}\n' for d in dirs))
        for repo in noroot:
            (trees / f'{repo}.noroot').write_text('')
        for repo in noclone:
            (trees / f'{repo}.noclone').write_text('')
        (tmp / 'cwd').mkdir()
        (tmp / 'cwd' / 'scripts').symlink_to(SCRIPTS)
        rc, result, log = replay_job(
            tmp,
            name,
            'fanout',
            [],
            github={
                **WEEKLY_GITHUB,
                **(github or {'event_name': 'schedule', 'event': {'inputs': {'only': ''}}}),
            },
            PATH=f'{bindir}:{os.environ["PATH"]}',
            LISTING=str(tmp / 'listing.json'),
            TREES=str(trees),
            SHA=SHA,
        )
        self.assertEqual(rc, 0, result.get('failed'))
        for line in log:
            self.assertNotIn('extraheader', line)
        clones = [line.split()[-2] for line in log if line.startswith('clone ')]
        self.clones = clones
        names = '|'.join(('xmlx', 'zpub', *extra))
        self.assertTrue(all(re.search(rf'/({names})\.git$', url) for url in clones), clones)
        return {
            k: v for step in result['steps'].values() for k, v in step.get('outputs', {}).items()
        }


class PublicOnly(FanoutReplay, unittest.TestCase):
    def test_every_listing_keeps_only_public_live_repositories_it_owns(self):
        listing = json.dumps(LISTING)
        for name in LISTING_WORKFLOWS:
            programs = listing_programs(name)
            self.assertTrue(programs, name)
            for program in programs:
                with self.subTest(name, program=program):
                    out = subprocess.run(
                        ['jq', '-r', program], input=listing, capture_output=True, text=True
                    )
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertEqual(out.stdout.split(), PUBLIC)

    def test_no_target_workflow_lists_repositories_another_way(self):
        for name in sorted({*LISTING_WORKFLOWS, *(n for n, _ in TARGET_CODE_JOBS)}):
            for job in load(name)['jobs'].values():
                for s in job.get('steps', []):
                    code = '\n'.join(
                        line
                        for line in s.get('run', '').splitlines()
                        if not line.lstrip().startswith('#')
                    )
                    with self.subTest(name, step=s.get('name')):
                        self.assertNotRegex(code, r'gh repo list|users/[^/]+/repos|orgs/')

    def test_the_stryker_fanout_emits_public_repositories(self):
        outputs = self.fanout('weekly-stryker.yaml')
        self.assertEqual(json.loads(outputs['repos']), PUBLIC)

    def test_the_gremlins_fanout_emits_public_repositories(self):
        outputs = self.fanout('weekly-gremlins.yaml')
        self.assertEqual(json.loads(outputs['repos']), PUBLIC)

    def test_the_fuzz_fanout_emits_public_repositories(self):
        outputs = self.fanout('weekly-fuzz.yaml')
        self.assertEqual(
            json.loads(outputs['repo_matrix']),
            [
                {'repo': r, 'sha': SHA, 'artifacts': f'fuzz-findings-{r}=root-shard0'}
                for r in PUBLIC
            ],
        )
        self.assertEqual(
            json.loads(outputs['shard_matrix']),
            [
                {'repo': r, 'sha': SHA, 'dir': '.', 'slug': 'root', 'shard': 0, 'shards': 1}
                for r in PUBLIC
            ],
        )

    def test_the_fuzz_fanout_emits_each_go_module_as_its_own_unit(self):
        outputs = self.fanout(
            'weekly-fuzz.yaml', mods={'xmlx': ['a', 'b/c'], 'zpub': ['m']}, noroot=('xmlx',)
        )
        self.assertEqual(
            json.loads(outputs['repo_matrix']),
            [
                {
                    'repo': 'xmlx',
                    'sha': SHA,
                    'artifacts': '{fuzz-findings-xmlx=a-shard0,fuzz-findings-xmlx=b_c-shard0}',
                },
                {
                    'repo': 'zpub',
                    'sha': SHA,
                    'artifacts': '{fuzz-findings-zpub=root-shard0,fuzz-findings-zpub=m-shard0}',
                },
            ],
        )
        self.assertEqual(
            [(e['repo'], e['dir'], e['slug']) for e in json.loads(outputs['shard_matrix'])],
            [('xmlx', 'a', 'a'), ('xmlx', 'b/c', 'b_c'), ('zpub', '.', 'root'), ('zpub', 'm', 'm')],
        )

    def test_a_clone_with_no_head_drops_only_that_repository(self):
        fuzz = self.fanout('weekly-fuzz.yaml', nohead=('zpub',))
        self.assertEqual([e['repo'] for e in json.loads(fuzz['repo_matrix'])], ['xmlx'])
        self.assertEqual([e['repo'] for e in json.loads(fuzz['shard_matrix'])], ['xmlx'])

    def test_the_bench_fanout_emits_each_enrolled_module_once(self):
        outputs = self.fanout(
            'weekly-bench.yaml', extra=('go',), mods={'go': ['xmlx', 'jsonx']}, noroot=('go',)
        )
        self.assertEqual(
            json.loads(outputs['matrix']),
            [
                {'repo': 'go', 'sha': SHA, 'dir': 'xmlx', 'slug': 'xmlx', 'key': 'go/xmlx'},
                {'repo': 'go', 'sha': SHA, 'dir': 'jsonx', 'slug': 'jsonx', 'key': 'go/jsonx'},
            ],
        )
        self.assertEqual(outputs['live'], 'xmlx zpub go')
        self.assertIn('go/xmlx', outputs['enrolled'].split())

    def test_a_repo_whose_keys_enrol_apart_is_cloned_once_and_dropped_whole(self):
        # ENROLLED puts langtag between go/keyenc and go/urlform.
        outputs = self.fanout(
            'weekly-bench.yaml',
            extra=('go', 'langtag'),
            mods={'go': ['keyenc', 'urlform']},
            noroot=('go',),
            noclone=('go',),
        )
        self.assertEqual([e['key'] for e in json.loads(outputs['matrix'])], ['langtag'])
        self.assertEqual(sum(url.endswith('/go.git') for url in self.clones), 1)

    def test_a_repo_with_no_head_drops_every_key_and_is_read_once(self):
        outputs = self.fanout(
            'weekly-bench.yaml',
            extra=('go', 'langtag'),
            mods={'go': ['keyenc', 'urlform']},
            noroot=('go',),
            nohead=('go',),
        )
        self.assertEqual([e['key'] for e in json.loads(outputs['matrix'])], ['langtag'])
        self.assertEqual(sum(url.endswith('/go.git') for url in self.clones), 1)

    def test_the_bench_only_filter_takes_a_repo_or_one_key(self):
        for only, keys in (('go', ['go/xmlx', 'go/jsonx']), ('go/jsonx', ['go/jsonx'])):
            with self.subTest(only=only):
                outputs = self.fanout(
                    'weekly-bench.yaml',
                    github={'event_name': 'workflow_dispatch', 'event': {'inputs': {'only': only}}},
                    extra=('go',),
                    mods={'go': ['xmlx', 'jsonx']},
                    noroot=('go',),
                )
                self.assertEqual([e['key'] for e in json.loads(outputs['matrix'])], keys)


GIT_STUB = """#!/usr/bin/env bash
dest="${@: -1}"
r=$(basename "${@: -2:1}" .git)
h=no
case "$*" in *extraheader*) h=yes ;; esac
printf 'git %s header=%s\n' "$r" "$h" >>"$STUB_LOG"
[ "$r" = d ] && exit 128
mkdir -p "$dest"
printf 'module example.com/%s\n' "$r" >"$dest/go.mod"
[ "$r" = b ] || printf 'package x\nfunc BenchmarkX(b *testing.B) {}\n' >"$dest/x_test.go"
if [ "$r" = m ]; then
  mkdir -p "$dest/sub"
  printf 'module example.com/m/sub\n' >"$dest/sub/go.mod"
  printf 'package sub\n' >"$dest/sub/s.go"
  printf 'package sub\nfunc BenchmarkS(b *testing.B) {}\n' >"$dest/sub/s_test.go"
fi
"""
GO_STUB = """#!/usr/bin/env bash
printf 'go %s\n' "$3" >>"$STUB_LOG"
if [ "$3" = "scan/${FAIL_REPO:-}" ]; then echo 'panic: boom'; exit 1; fi
echo PASS
"""


class BenchSmoke(unittest.TestCase):
    def replay(
        self, live: str = 'a b d .github', enrolled: str = '', **env: str
    ) -> tuple[int, dict, list[str]]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        bindir = tmp / 'bin'
        bindir.mkdir()
        for name, body in (('git', GIT_STUB), ('go', GO_STUB)):
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        (tmp / 'cwd').mkdir()
        log = tmp / 'stub.log'
        context = {
            'github': WEEKLY_GITHUB,
            'secrets': {},
            'needs': {
                'fanout': {'result': 'success', 'outputs': {'live': live, 'enrolled': enrolled}}
            },
        }
        (tmp / 'ctx.json').write_text(json.dumps(context))
        argv = ['--workflow', str(WORKFLOWS / 'weekly-bench.yaml'), '--job', 'smoke']
        argv += ['--context', str(tmp / 'ctx.json'), '--cwd', str(tmp / 'cwd')]
        argv += ['--out', str(tmp / 'out.json'), '--skip', 'Setup Go']
        environ = {'PATH': f'{bindir}:{os.environ["PATH"]}', 'STUB_LOG': str(log), **env}
        with unittest.mock.patch.dict(os.environ, environ), redirect_stderr(io.StringIO()):
            rc = workflow_replay.main(argv)
        result = json.loads((tmp / 'out.json').read_text())
        summary = pathlib.Path(result['runner_temp'], 'summary.md')
        result['summary'] = summary.read_text() if summary.exists() else ''
        return rc, result, log.read_text().splitlines() if log.exists() else []

    def test_the_fanout_s_repositories_are_cloned_anonymously_and_run(self):
        rc, result, log = self.replay()
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertFalse(result['summary'].strip())
        self.assertEqual(
            log,
            [
                'git a header=no',
                'git b header=no',
                'git d header=no',
                'git .github header=no',
                'go scan/a',
                'go scan/.github',
            ],
        )

    def test_a_benchmark_that_does_not_run_fails_the_job(self):
        rc, result, log = self.replay(FAIL_REPO='a')
        self.assertEqual(rc, 1)
        self.assertIn('Execute every unenrolled benchmark once', result['failed'])
        self.assertIn('### a: benchmark does not run', result['summary'])
        self.assertIn('panic: boom', result['summary'])
        self.assertIn('go scan/.github', log)

    def test_an_empty_list_from_the_fanout_fails_the_job(self):
        rc, result, log = self.replay(live='')
        self.assertEqual(rc, 1)
        self.assertIn(
            'Clone every live repo and list its unenrolled modules with benchmarks',
            result['failed'],
        )
        self.assertEqual(log, [])

    def test_an_enrolled_key_is_skipped_and_a_nested_module_runs_on_its_own(self):
        rc, result, log = self.replay(live='a m', enrolled='a langtag m')
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(log, ['git a header=no', 'git m header=no', 'go scan/m/sub'])

    def test_a_run_that_checks_no_module_fails_the_job(self):
        rc, result, log = self.replay(live='a b', enrolled='a')
        self.assertEqual(rc, 1)
        self.assertIn('Execute every unenrolled benchmark once', result['failed'])
        self.assertEqual(log, ['git a header=no', 'git b header=no'])


PUBLISH_BADGE_STUB = """#!/usr/bin/env bash
t=no
[ "$2" = tok ] && t=yes
printf '%s %s %s token=%s\\n' "$1" "$3" "$4" "$t" >>"$STUB_LOG"
[ "$1" != "cplieger/${FAIL_REPO:-}" ]
"""


MATRIX = json.dumps([{'repo': 'a', 'sha': 'f00d'}])


def replay_job(
    tmp: pathlib.Path,
    workflow: str,
    job: str,
    args: list[str],
    matrix: dict | None = None,
    github: dict | None = None,
    needs: dict | None = None,
    **env: str,
) -> tuple[int, dict, list[str]]:
    context = {
        'github': github or {},
        'secrets': {'CI_SCHEDULE': 'tok'},
        'matrix': matrix or {'repo': 'a', 'sha': 'f00d'},
        'inputs': {'repository': 'a', 'dir': '', 'module': '', 'honour-opt-out': False},
        'needs': {
            'fanout': {'result': 'success', 'outputs': {'matrix': MATRIX}},
            'run': {'result': 'success', 'outputs': {}},
            **(needs or {}),
        },
    }
    (tmp / 'ctx.json').write_text(json.dumps(context))
    (tmp / 'cwd').mkdir(exist_ok=True)
    argv = ['--workflow', str(WORKFLOWS / workflow), '--job', job]
    argv += ['--context', str(tmp / 'ctx.json'), '--cwd', str(tmp / 'cwd')]
    argv += ['--out', str(tmp / 'out.json'), *args]
    log = tmp / 'stub.log'
    with (
        unittest.mock.patch.dict(os.environ, {'STUB_LOG': str(log), **env}),
        redirect_stderr(io.StringIO()),
    ):
        rc = workflow_replay.main(argv)
    result = json.loads((tmp / 'out.json').read_text())
    return rc, result, log.read_text().splitlines() if log.exists() else []


def stryker_step(job: str, name: str) -> dict:
    steps = load('mutation-stryker-repo.yaml')['jobs'][job]['steps']
    return next(s for s in steps if s.get('name') == name)


PUBLISHER_STUB_PATH = pathlib.Path('actions', 'publish-badge', 'publish-badge.sh')


BADGE_ENTRY = {'repo': 'a', 'sha': 'f00d', 'dir': 'web', 'key': 'a~web'}


class StrykerBadge(unittest.TestCase):
    def replay(self, meta: dict | None, **env: str) -> tuple[int, dict, list[str]]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub = tmp / 'cwd' / PUBLISHER_STUB_PATH
        stub.parent.mkdir(parents=True)
        stub.write_text(PUBLISH_BADGE_STUB)
        if meta is not None:
            (tmp / 'cwd' / 'artifact').mkdir()
            (tmp / 'cwd' / 'artifact' / 'meta.json').write_text(json.dumps(meta))
        args = ['--skip', "Download this entry's report"]
        needs = {'shard': {'result': 'success', 'outputs': {'merge': json.dumps([BADGE_ENTRY])}}}
        return replay_job(
            tmp, 'mutation-stryker-repo.yaml', 'badge', args, matrix=BADGE_ENTRY, needs=needs, **env
        )

    @staticmethod
    def badge(repo: str, score: str, color: str) -> str:
        body = json.dumps(
            {'schemaVersion': 1, 'label': 'mutation (TS)', 'message': f'{score}%', 'color': color},
            separators=(',', ':'),
        )
        return f'cplieger/{repo} mutation-ts.json {body} token=yes'

    def test_the_staged_score_is_published_with_its_colour_band(self):
        for score, color in (
            ('100.0', 'brightgreen'),
            ('85.0', 'brightgreen'),
            ('84.9', 'green'),
            ('75', 'green'),
            ('50.0', 'yellow'),
            ('30.0', 'orange'),
            ('29.9', 'red'),
            ('0.0', 'red'),
        ):
            with self.subTest(score=score):
                rc, result, log = self.replay({'repo': 'a', 'dir': 'web', 'score': score})
                self.assertEqual(rc, 0, result.get('failed'))
                self.assertEqual(log, [self.badge('a', score, color)])

    def test_the_repo_comes_from_the_matrix_never_from_the_artifact(self):
        rc, result, log = self.replay({'repo': 'victim', 'dir': '.', 'score': '91.0'})
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(log, [self.badge('a', '91.0', 'brightgreen')])

    def test_a_score_that_is_not_a_number_publishes_nothing_and_fails(self):
        for score in ('', '1000', '9.', 'abc', '50","color":"blue', '5 0', None):
            with self.subTest(score=score):
                meta = {'repo': 'a', 'dir': 'web'}
                if score is not None:
                    meta['score'] = score
                rc, result, log = self.replay(meta)
                self.assertEqual(rc, 1)
                self.assertIn("Publish this entry's mutation-ts badge", result['failed'])
                self.assertEqual(log, [])

    def test_an_entry_that_staged_no_report_publishes_nothing(self):
        rc, result, log = self.replay(None)
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(log, [])

    def test_a_failed_publish_fails_the_entry(self):
        rc, result, _ = self.replay({'repo': 'a', 'dir': 'web', 'score': '55.0'}, FAIL_REPO='a')
        self.assertEqual(rc, 1)
        self.assertIn("Publish this entry's mutation-ts badge", result['failed'])

    def test_each_entry_downloads_the_artifact_its_run_entry_uploaded(self):
        job = load('mutation-stryker-repo.yaml')['jobs']['badge']
        self.assertEqual(
            job['strategy']['matrix']['include'], '${{ fromJSON(needs.shard.outputs.merge) }}'
        )
        download = stryker_step('badge', "Download this entry's report")['with']
        upload = stryker_step('merge', 'Upload the report (artifact, 90-day retention)')['with']
        self.assertEqual(download['pattern'], 'stryker-${{ matrix.key }}')
        self.assertEqual(upload['name'], download['pattern'])
        self.assertNotIn('merge-multiple', download)

    def test_a_cancelled_run_publishes_nothing(self):
        cond = load('mutation-stryker-repo.yaml')['jobs']['badge']['if']
        self.assertIn('!cancelled()', cond)
        self.assertNotIn('always()', cond)


MUTANT = {
    'id': '7',
    'mutatorName': 'ConditionalExpression',
    'replacement': 'secretOperand && true',
    'location': {'start': {'line': 3, 'column': 2}, 'end': {'line': 3, 'column': 15}},
    'status': 'Killed',
    'killedBy': ['handles the secret path'],
    'coveredBy': ['handles the secret path'],
    'statusReason': 'expected secretOperand to be 2',
    'description': 'covers secretOperand',
    'testsCompleted': 1,
    'static': False,
}
REPORT = {
    'schemaVersion': '2',
    'thresholds': {'high': 80, 'low': 60, 'break': 50},
    'projectRoot': '/home/runner/work/ci/ci/target/web',
    'files': {
        'src/a.ts': {
            'language': 'typescript',
            'source': 'export const secretValue = secretOperand',
            'mutants': [MUTANT],
        }
    },
    'testFiles': {
        'src/a.test.ts': {
            'source': "it('handles the secret path')",
            'tests': [{'id': 'handles the secret path', 'name': 'handles the secret path'}],
        }
    },
}


SURVIVED = {
    **REPORT,
    'files': {
        'src/a.ts': {**REPORT['files']['src/a.ts'], 'mutants': [{**MUTANT, 'status': 'Survived'}]}
    },
}


class StrykerStage(unittest.TestCase):
    """The merge job stages one shard's report as the unsharded run job staged it."""

    def stage(self) -> tuple[dict, dict]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        plan = tmp / 'plan.json'
        plan.write_text(
            json.dumps(
                {'repo': 'r', 'dir': 'web', 'sha': SHA, 'shards': 1, 'files': None, 'break': None}
            )
        )
        shard = tmp / 'shards'
        shard.mkdir()
        (shard / 'result.json').write_text(json.dumps(REPORT))
        (shard / 'meta.json').write_text(
            json.dumps(
                {
                    'repo': 'r',
                    'dir': 'web',
                    'shard': 1,
                    'shards': 1,
                    'complete': True,
                    'skipped': False,
                }
            )
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / 'stryker-shard.py'),
                'merge',
                '--plan',
                str(plan),
                '--shards',
                str(shard),
                '--stage',
                str(tmp / 'stage'),
                '--costs',
                str(tmp / 'costs'),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return (
            json.loads((tmp / 'stage' / 'mutation.json').read_text()),
            json.loads((tmp / 'stage' / 'meta.json').read_text()),
        )

    def test_the_merge_step_reads_its_entry_s_plan_and_shards(self):
        step = stryker_step('merge', 'Merge the shards')
        self.assertEqual(step['env'], {'KEY': '${{ matrix.key }}'})
        self.assertIn('--plan "plan/${KEY}.json" --shards shards', step['run'])
        self.assertIn('--stage stage --costs costs', step['run'])

    def test_a_report_loses_only_the_sources_and_its_score_is_computed(self):
        want = json.loads(json.dumps(REPORT))
        del want['files']['src/a.ts']['source']
        del want['testFiles']['src/a.test.ts']['source']
        staged, meta = self.stage()
        self.assertEqual(staged, want)
        self.assertEqual(meta, {'repo': 'r', 'dir': 'web', 'score': '100.0'})

    def test_the_tracker_reads_a_staged_report(self):
        staged, _ = self.stage()
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'artifacts').mkdir()
        (tmp / 'artifacts' / 'mutation.json').write_text(json.dumps(staged))
        (tmp / 'artifacts' / 'meta.json').write_text(json.dumps({'repo': 'r', 'dir': 'web'}))
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / 'stryker-aggregate.py'),
                '--repo',
                'r',
                '--artifacts-dir',
                str(tmp / 'artifacts'),
                '--week',
                '2026-11-14 22:00',
                '--run-url',
                'https://example.invalid/run',
                '--entries-marker-file',
                str(tmp / 'entries'),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((tmp / 'entries').read_text().strip(), '1')


# Each reader of the run artifacts: (workflow, job, the shard job output naming
# its downloads, the job and step whose uploads they are).
ARTIFACT_READERS = (
    (
        'mutation-stryker-repo.yaml',
        'aggregate',
        'reports',
        ('merge', 'Upload the report (artifact, 90-day retention)'),
    ),
    (
        'mutation-gremlins-repo.yaml',
        'badge',
        'artifacts',
        ('run', 'Upload full mutants.json (artifact, 90-day retention)'),
    ),
    (
        'mutation-gremlins-repo.yaml',
        'aggregate',
        'artifacts',
        ('run', 'Upload full mutants.json (artifact, 90-day retention)'),
    ),
)
GLOB_CHARACTERS = re.compile(r'[*?\[\]{}()!+@,]')


def expand(pattern: str) -> set[str]:
    """The names a reader's download pattern matches: a brace list or one name."""
    names = pattern[1:-1].split(',') if pattern.startswith('{') else [pattern]
    for name in names:
        if GLOB_CHARACTERS.search(name):
            raise AssertionError(f'{name!r} in {pattern!r} is not a literal artifact name')
    return set(names)


def upload_names(
    workflow: str, upload: str, entries: list[dict], job: str = 'run', needs: dict | None = None
) -> dict[str, set[str]]:
    """The artifact name each run entry uploads, by repo, rendered as GitHub would."""
    expr = next(s for s in load(workflow)['jobs'][job]['steps'] if s.get('name') == upload)
    names: dict[str, set[str]] = {}
    for entry in entries:
        scope = workflow_replay.Scope({'matrix': entry, 'needs': needs or {}}, {}, [])
        names.setdefault(entry['repo'], set()).add(scope.render(expr['with']['name']))
    return names


# A sharded repo, a repo whose name extends another's, and a plain one.
GREMLINS_MATRIX = [
    *({'repo': 'a', 'attempt': n, 'shard': s, 'shards': 2} for n in (1, 2, 3) for s in (1, 2)),
    *({'repo': 'a-1', 'attempt': n} for n in (1, 2, 3)),
    {'repo': 'b', 'attempt': 1},
]
SHARD_STUB = """import json, os, sys
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write('shard ' + ' '.join(sys.argv[1:]) + '\\n')
print(json.dumps(json.loads(os.environ['STUB_MATRIX'])))
"""
TRACKER_STUB = """import os, pathlib, sys
a = sys.argv[1:]
repo, mode = a[a.index('--repo') + 1], a[a.index('--mode') + 1]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(f'tracker {repo} {mode}\\n')
if mode == 'fetch':
    pathlib.Path(a[a.index('--body-file') + 1]).write_text('')
"""


def gremlins_out(efficacy: float) -> dict:
    return {
        'go_module': 'example.com/x',
        'test_efficacy': efficacy,
        'mutations_coverage': 90.0,
        'mutants_total': 10,
        'mutants_killed': 8,
        'mutants_lived': 2,
        'mutants_not_viable': 0,
        'mutants_not_covered': 0,
        'files': [],
    }


class ArtifactBinding(FanoutReplay, unittest.TestCase):
    def test_each_reader_runs_on_the_artifacts_named_for_its_repo(self):
        for name, job_name, output, _ in ARTIFACT_READERS:
            with self.subTest(f'{name}:{job_name}'):
                job = load(name)['jobs'][job_name]
                self.assertIn('shard', job['needs'])
                self.assertIn("needs.shard.result == 'success'", job['if'])
                download = next(
                    s
                    for s in job['steps']
                    if s.get('uses', '').startswith('actions/download-artifact@')
                )['with']
                self.assertEqual(
                    download,
                    {'path': 'artifacts', 'pattern': f'${{{{ needs.shard.outputs.{output} }}}}'},
                )
                reads = [s for s in job['steps'] if 'artifacts' in s.get('run', '')]
                self.assertEqual(len(reads), 1)
                self.assertIn(
                    'discover-artifact-repos.sh --allow "$REPO" artifacts', reads[0]['run']
                )
                self.assertEqual(reads[0]['env']['REPO'], '${{ inputs.repository }}')
        record = load('mutation-gremlins-repo.yaml')['jobs']['record']
        costs = next(s for s in record['steps'] if 'gremlins-shard.py history' in s.get('run', ''))
        # gremlins-shard.py history binds each cost record by its meta.json.
        self.assertIn('--repo "$REPO" --artifacts-dir artifacts', costs['run'])
        self.assertEqual(costs['env']['REPO'], '${{ inputs.repository }}')

    def test_the_stryker_readers_download_exactly_each_entry_s_upload_names(self):
        body = stryker_step('shard', 'Build the run and merge matrices')['run']
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'scripts').symlink_to(SCRIPTS)
        entries = [
            {'repo': 'xmlx', 'sha': SHA, 'dir': d, 'key': k}
            for d, k in (('.', 'xmlx~'), ('web/app', 'xmlx~web=app'))
        ]
        out = tmp / 'out'
        proc = subprocess.run(
            ['bash', '-c', body],
            cwd=tmp,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                'ENTRIES': json.dumps(entries),
                'TARGET_MINUTES': '200',
                'GITHUB_OUTPUT': str(out),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outputs = dict(line.split('=', 1) for line in out.read_text().splitlines())
        self.assertEqual(outputs['reports'], '{stryker-xmlx~,stryker-xmlx~web=app}')
        want = upload_names(
            'mutation-stryker-repo.yaml',
            ARTIFACT_READERS[0][3][1],
            json.loads(outputs['merge']),
            'merge',
        )
        self.assertEqual({'xmlx': expand(outputs['reports'])}, want)

    def test_the_gremlins_readers_download_exactly_each_repo_s_upload_names(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'cwd' / 'scripts').mkdir(parents=True)
        (tmp / 'cwd' / 'scripts' / 'gremlins-shard.py').write_text(SHARD_STUB)
        args = [
            '--skip',
            'Checkout ci (for scripts/)',
            '--skip',
            'Download the shard plan',
            '--skip',
            'Fetch the cost history',
            '--skip',
            'Upload the run plan',
        ]
        needs = {
            'plan': {'result': 'success', 'outputs': {}},
            'setup': {'result': 'success', 'outputs': {'key': 'a', 'shard_target_minutes': '240'}},
        }
        matrix = [r for r in GREMLINS_MATRIX if r['repo'] == 'a']
        rc, result, log = replay_job(
            tmp,
            'mutation-gremlins-repo.yaml',
            'shard',
            args,
            needs=needs,
            STUB_MATRIX=json.dumps(matrix),
        )
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(
            log,
            [
                (
                    'shard matrix --repo a --plan sizing/plan.json --attempts 3 --target-minutes 240 '
                    '--history history/costs.json --out-plan run-plans/plan.json'
                )
            ],
        )
        self.assertEqual(
            load('mutation-gremlins-repo.yaml')['jobs']['shard']['permissions']['actions'], 'read'
        )
        got = expand(result['steps']['matrix']['outputs']['artifacts'])
        want = upload_names(
            'mutation-gremlins-repo.yaml', ARTIFACT_READERS[1][3][1], matrix, needs=needs
        )
        self.assertEqual({'a': got}, want)
        self.assertEqual(len(got), 6)

    def test_the_fuzz_report_downloads_exactly_each_repo_s_shard_names(self):
        report = load('weekly-fuzz.yaml')['jobs']['report']
        self.assertIn("needs.fanout.result == 'success'", report['if'])
        download = next(
            s for s in report['steps'] if s.get('uses', '').startswith('actions/download-artifact@')
        )['with']
        self.assertEqual(download, {'path': 'shard-findings', 'pattern': '${{ matrix.artifacts }}'})
        self.assertEqual(
            report['strategy']['matrix']['include'],
            '${{ fromJSON(needs.fanout.outputs.repo_matrix) }}',
        )
        # 121 targets at 120 s each overrun one 14400 s shard.
        outputs = self.fanout('weekly-fuzz.yaml', fuzz={'zpub': 121})
        repos = json.loads(outputs['repo_matrix'])
        self.assertEqual(
            [r['artifacts'] for r in repos],
            [
                'fuzz-findings-xmlx=root-shard0',
                '{fuzz-findings-zpub=root-shard0,fuzz-findings-zpub=root-shard1}',
            ],
        )
        want = upload_names(
            'weekly-fuzz.yaml', 'Upload shard findings', json.loads(outputs['shard_matrix']), 'fuzz'
        )
        self.assertEqual({r['repo']: expand(r['artifacts']) for r in repos}, want)

    def reader(
        self, workflow: str, job: str, artifacts: dict[str, tuple[dict, str, object]], **env: str
    ) -> tuple[int, dict, list[str], str]:
        """Replays one reader for matrix repo `a` on the artifacts its download extracted."""
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        scripts = tmp / 'cwd' / 'scripts'
        scripts.mkdir(parents=True)
        for f in SCRIPTS.iterdir():
            if f.is_file() and f.name != 'tracker_issue.py':
                (scripts / f.name).symlink_to(f)
        (scripts / 'tracker_issue.py').write_text(TRACKER_STUB)
        badge = tmp / 'cwd' / PUBLISHER_STUB_PATH
        badge.parent.mkdir(parents=True)
        badge.write_text(PUBLISH_BADGE_STUB)
        for name, (meta, file, content) in artifacts.items():
            (tmp / 'cwd' / 'artifacts' / name).mkdir(parents=True)
            (tmp / 'cwd' / 'artifacts' / name / 'meta.json').write_text(json.dumps(meta))
            (tmp / 'cwd' / 'artifacts' / name / file).write_text(json.dumps(content))
        steps = [s.get('name') for s in load(workflow)['jobs'][job]['steps']]
        args = [a for s in steps[:-1] for a in ('--skip', s)]
        needs = {
            n: {'result': 'success', 'outputs': {'merge': '[{"key": "a~"}]', 'artifacts': 'x'}}
            for n in ('discover', 'setup', 'plan', 'shard', 'run', 'merge')
        }
        rc, result, log = replay_job(
            tmp,
            workflow,
            job,
            args,
            needs=needs,
            GITHUB_SERVER_URL='https://github.com',
            GITHUB_REPOSITORY='cplieger/ci',
            GITHUB_RUN_ID='1',
            **env,
        )
        logs = sorted(pathlib.Path(result['runner_temp']).glob('log-*.txt'))
        return rc, result, log, ''.join(p.read_text() for p in logs)

    def test_a_victim_named_artifact_reaches_neither_the_victim_nor_the_reader_s_repo(self):
        stryker = {
            'stryker-a-root': ({'repo': 'a', 'dir': '.'}, 'mutation.json', REPORT),
            'stryker-a-web': ({'repo': 'victim', 'dir': 'web'}, 'mutation.json', SURVIVED),
        }
        gremlins = {
            'gremlins-a-1': (
                {'repo': 'a', 'attempt': '1'},
                'gremlins-out.json',
                gremlins_out(80.0),
            ),
            'gremlins-a-2': (
                {'repo': 'victim', 'attempt': '2'},
                'gremlins-out.json',
                gremlins_out(10.0),
            ),
        }
        cases = (
            (
                'mutation-stryker-repo.yaml',
                'aggregate',
                stryker,
                'entries.txt',
                '100.0% mutation score',
            ),
            (
                'mutation-gremlins-repo.yaml',
                'aggregate',
                gremlins,
                'attempts.txt',
                '80.0% efficacy',
            ),
        )
        for workflow, job, artifacts, marker, week in cases:
            with self.subTest(f'{workflow}:{job}'):
                rc, result, log, out = self.reader(workflow, job, artifacts)
                self.assertEqual(rc, 0, result.get('failed'))
                self.assertEqual(log, ['tracker cplieger/a fetch', 'tracker cplieger/a upsert'])
                self.assertEqual(
                    pathlib.Path(result['runner_temp'], marker).read_text().strip(), '1'
                )
                body = pathlib.Path(result['runner_temp'], 'body.md').read_text()
                self.assertIn(f'**This week**: {week}', body)
                self.assertIn("an artifact names 'victim'", out)
        rc, result, log, out = self.reader('mutation-gremlins-repo.yaml', 'badge', gremlins)
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(len(log), 1)
        target, file, rest = log[0].split(' ', 2)
        body, token = rest.rsplit(' ', 1)
        self.assertEqual((target, file, token), ('cplieger/a', 'mutation.json', 'token=yes'))
        self.assertEqual(json.loads(body)['message'], '80.0%')
        self.assertIn("an artifact names 'victim'", out)

    def test_a_reader_whose_download_names_only_another_repo_writes_nothing(self):
        cases = (
            ('mutation-stryker-repo.yaml', 'aggregate', 'mutation.json', REPORT),
            ('mutation-gremlins-repo.yaml', 'aggregate', 'gremlins-out.json', gremlins_out(80.0)),
            ('mutation-gremlins-repo.yaml', 'badge', 'gremlins-out.json', gremlins_out(80.0)),
        )
        for workflow, job, file, content in cases:
            for artifacts in ({'x': ({'repo': 'victim'}, file, content)}, {}):
                with self.subTest(f'{workflow}:{job}', downloaded=bool(artifacts)):
                    rc, result, log, _ = self.reader(workflow, job, artifacts)
                    self.assertEqual(rc, 0, result.get('failed'))
                    self.assertEqual(log, [])

    def test_a_download_with_no_readable_meta_fails_the_reader(self):
        for workflow, job, _, _ in ARTIFACT_READERS:
            with self.subTest(f'{workflow}:{job}'):
                rc, result, log, _ = self.reader(workflow, job, {'x': ({}, 'f.json', {})})
                self.assertEqual(rc, 1)
                self.assertIn('no repo could be read from any meta.json', result['failed'])
                self.assertEqual(log, [])


class BenchPublish(unittest.TestCase):
    ARGS = (
        '--skip',
        'Checkout ci (for the attribution fix)',
        '--skip',
        "Download this entry's medians",
        '--stop-before',
        'Ensure benchmarks branch exists',
    )

    def medians(self, content: str | None) -> tuple[int, dict]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'cwd' / 'bench').mkdir(parents=True)
        if content is not None:
            (tmp / 'cwd' / 'bench' / 'bench.json').write_text(content)
        rc, result, _ = replay_job(tmp, 'weekly-bench.yaml', 'publish', list(self.ARGS))
        return rc, result

    def test_only_a_non_empty_series_of_medians_is_published(self):
        series = json.dumps([{'name': 'BenchmarkX', 'unit': 'ns/op', 'value': 1}])
        for content, want in ((series, 'true'), ('[]', ''), ('', ''), (None, ''), ('{', '')):
            with self.subTest(content=content):
                rc, result = self.medians(content)
                self.assertEqual(rc, 0, result.get('failed'))
                self.assertEqual(result['steps']['medians']['outputs'].get('ok', ''), want)

    def test_every_step_after_the_check_waits_for_it(self):
        steps = load('weekly-bench.yaml')['jobs']['publish']['steps']
        names = [s.get('name') for s in steps]
        later = steps[names.index('Check the medians') + 1 :]
        self.assertEqual(len(later), 3)
        for s in later:
            with self.subTest(step=s.get('name')):
                self.assertEqual(s.get('if'), "${{ steps.medians.outputs.ok == 'true' }}")
        download = steps[names.index("Download this entry's medians")]['with']
        self.assertEqual(download['pattern'], 'bench-raw-${{ matrix.repo }}=${{ matrix.slug }}')
        upload = next(
            s
            for s in load('weekly-bench.yaml')['jobs']['run']['steps']
            if s.get('name') == 'Keep the raw samples'
        )
        self.assertEqual(upload['with']['name'], download['pattern'])

    def test_the_tracker_reads_the_branch_after_the_publish(self):
        self.assertIn('publish', load('weekly-bench.yaml')['jobs']['aggregate']['needs'])


# git for the per-module bench steps: a clone is a real repository holding the
# root's and jsonx's data.js, ls-remote finds the branch, and a push fails the
# first $FAIL_PUSHES times.
MODULE_GIT_STUB = """#!/usr/bin/env bash
printf 'git %s\\n' "$*" >>"$STUB_LOG"
case "$1" in
  ls-remote) exit 0 ;;
  clone)
    dest="${@: -1}"
    mkdir -p "$dest/jsonx"
    printf 'old\\n' >"$dest/data.js"
    printf 'old\\n' >"$dest/jsonx/data.js"
    /usr/bin/git -C "$dest" init -q -b benchmarks
    /usr/bin/git -C "$dest" add -A
    /usr/bin/git -C "$dest" -c user.name=t -c user.email=t@t commit -qm init
    exit 0 ;;
esac
case " $* " in
  *" push "*)
    n=$(($(cat "$PUSHES" 2>/dev/null || echo 0) + 1))
    echo "$n" >"$PUSHES"
    [ "$n" -gt "${FAIL_PUSHES:-0}" ]
    exit ;;
esac
exec /usr/bin/git "$@"
"""
FIX_STUB = """import os, pathlib, sys
a = sys.argv[1:]
data = pathlib.Path(a[a.index('--data-file') + 1])
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(f'fix {data.relative_to(data.parents[1]) if data.parent.name != "data" else data.name}\\n')
data.write_text('fixed\\n')
"""
LOGGING_TRACKER_STUB = """import os, pathlib, sys
a = sys.argv[1:]
mode, title = a[a.index('--mode') + 1], a[a.index('--title') + 1]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(f'tracker {mode} {title}\\n')
if mode == 'fetch':
    pathlib.Path(a[a.index('--body-file') + 1]).write_text('')
"""
AGGREGATE_STUB = """import os, pathlib, sys
a = sys.argv[1:]
data = a[a.index('--data-file') + 1]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(f"aggregate --module {a[a.index('--module') + 1]} {pathlib.Path(data).parent.name}/{pathlib.Path(data).name}\\n")
pathlib.Path(a[a.index('--entries-marker-file') + 1]).write_text('1')
pathlib.Path(a[a.index('--regression-marker-file') + 1]).write_text('false')
print('body')
"""


class BenchPerModule(unittest.TestCase):
    """Each enrolled module publishes and tracks its own series on the repo's branch."""

    def stubs(self, tmp: pathlib.Path) -> pathlib.Path:
        bindir = tmp / 'bin'
        bindir.mkdir()
        (bindir / 'git').write_text(MODULE_GIT_STUB)
        (bindir / 'gh').write_text('#!/usr/bin/env bash\necho "{}"\n')
        for f in bindir.iterdir():
            f.chmod(0o755)
        return bindir

    def repoint(self, dir_: str, fail_pushes: int = 0) -> tuple[int, dict, list[str]]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        bindir = self.stubs(tmp)
        (tmp / 'cwd' / 'bench').mkdir(parents=True)
        (tmp / 'cwd' / 'bench' / 'bench.json').write_text(
            '[{"name": "B", "unit": "ns/op", "value": 1}]'
        )
        (tmp / 'cwd' / '_ci' / 'scripts').mkdir(parents=True)
        (tmp / 'cwd' / '_ci' / 'scripts' / 'bench-fix-attribution.py').write_text(FIX_STUB)
        slug = 'root' if dir_ == '.' else dir_
        key = 'go' if dir_ == '.' else f'go/{dir_}'
        args = [
            '--skip',
            'Checkout ci (for the attribution fix)',
            '--skip',
            "Download this entry's medians",
        ]
        args += ['--skip', 'Publish + compare']
        return replay_job(
            tmp,
            'weekly-bench.yaml',
            'publish',
            args,
            matrix={'repo': 'go', 'sha': 'f00d', 'dir': dir_, 'slug': slug, 'key': key},
            github={'sha': 'c1c1'},
            PATH=f'{bindir}:{os.environ["PATH"]}',
            PUSHES=str(tmp / 'pushes'),
            FAIL_PUSHES=str(fail_pushes),
            GIT_CONFIG_GLOBAL='/dev/null',
            GITHUB_SHA='c1c1',
        )

    def test_each_module_publishes_under_its_own_dir(self):
        steps = load('weekly-bench.yaml')['jobs']['publish']['steps']
        publish = next(s for s in steps if s.get('name') == 'Publish + compare')
        self.assertEqual(publish['with']['benchmark-data-dir-path'], '${{ matrix.dir }}')

    def test_the_re_point_edits_the_entry_s_own_data_file(self):
        for dir_, want in (('jsonx', 'fix jsonx/data.js'), ('.', 'fix data.js')):
            with self.subTest(dir=dir_):
                rc, result, log = self.repoint(dir_)
                self.assertEqual(rc, 0, result.get('failed'))
                self.assertIn(want, log)
                self.assertEqual(sum(line.startswith('git clone') for line in log), 1)

    def test_a_refused_push_starts_over_from_a_fresh_clone(self):
        rc, result, log = self.repoint('jsonx', fail_pushes=1)
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(sum(line.startswith('git clone') for line in log), 2)
        self.assertEqual(log.count('fix jsonx/data.js'), 2)

    def test_a_push_refused_five_times_fails_the_entry(self):
        rc, result, log = self.repoint('jsonx', fail_pushes=9)
        self.assertEqual(rc, 1)
        self.assertIn('Re-point the data point at the benchmarked commit', result['failed'])
        self.assertEqual(sum(line.startswith('git clone') for line in log), 5)

    def test_the_tracker_of_a_nested_module_names_its_dir_and_reads_its_series(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        bindir = self.stubs(tmp)
        scripts = tmp / 'cwd' / 'scripts'
        scripts.mkdir(parents=True)
        (scripts / 'tracker_issue.py').write_text(LOGGING_TRACKER_STUB)
        (scripts / 'bench-aggregate.py').write_text(AGGREGATE_STUB)
        matrix = [
            {'repo': 'go', 'sha': 'f00d', 'dir': '.', 'slug': 'root', 'key': 'go'},
            {'repo': 'go', 'sha': 'f00d', 'dir': 'jsonx', 'slug': 'jsonx', 'key': 'go/jsonx'},
        ]
        rc, result, log = replay_job(
            tmp,
            'weekly-bench.yaml',
            'aggregate',
            ['--skip', 'Checkout ci (for the aggregator)'],
            needs={'fanout': {'result': 'success', 'outputs': {'matrix': json.dumps(matrix)}}},
            PATH=f'{bindir}:{os.environ["PATH"]}',
            GIT_CONFIG_GLOBAL='/dev/null',
            GITHUB_SERVER_URL='https://github.com',
            GITHUB_REPOSITORY='cplieger/ci',
            GITHUB_RUN_ID='1',
        )
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertEqual(
            [line for line in log if not line.startswith('git ')],
            [
                'tracker fetch Benchmark regression tracker',
                'aggregate --module . data/data.js',
                'tracker upsert Benchmark regression tracker',
                'tracker fetch Benchmark regression tracker: jsonx',
                'aggregate --module jsonx jsonx/data.js',
                'tracker upsert Benchmark regression tracker: jsonx',
            ],
        )


# go for the fuzz shard: one package whose one target writes a reproducer.
FUZZ_GO_STUB = """#!/usr/bin/env bash
printf 'go %s cwd=%s GOWORK=%s\\n' "$1" "${PWD##*/target}" "${GOWORK:-}" >>"$STUB_LOG"
case "$1 $2" in
  "list ./...") echo example.com/m/inner ;;
  "list -f") echo "$PWD/inner" ;;
  "test -list") echo FuzzM ;;
  "test -run=^\\$")
    echo 'Failing input written to testdata/fuzz/FuzzM/abc'
    exit 1 ;;
esac
"""


class FuzzPerModule(unittest.TestCase):
    """A nested module's finding carries its module from the shard to the issue."""

    def test_a_nested_module_s_finding_names_it_and_reproduces_from_its_dir(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        bindir = tmp / 'bin'
        bindir.mkdir()
        (bindir / 'go').write_text(FUZZ_GO_STUB)
        (bindir / 'go').chmod(0o755)
        (tmp / 'cwd' / 'target' / 'm' / 'inner' / 'testdata' / 'fuzz' / 'FuzzM').mkdir(parents=True)
        (tmp / 'cwd' / 'target' / 'm' / 'inner' / 'testdata' / 'fuzz' / 'FuzzM' / 'abc').write_text(
            'x'
        )
        matrix = {'repo': 'go', 'sha': 'f00d', 'dir': 'm', 'slug': 'm', 'shard': 0, 'shards': 1}
        skip = ['--skip', 'Clone target repo at pinned SHA', '--skip', 'Set up Go']
        skip += ['--skip', 'Upload shard findings']
        rc, result, log = replay_job(
            tmp,
            'weekly-fuzz.yaml',
            'fuzz',
            skip,
            matrix=matrix,
            PATH=f'{bindir}:{os.environ["PATH"]}',
        )
        self.assertEqual(rc, 0, result.get('failed'))
        self.assertIn('go list cwd=/m GOWORK=off', log)
        finding = (tmp / 'cwd' / 'findings.txt').read_text().strip().split('|')
        self.assertEqual(finding[0:3], ['FuzzM', 'abc', 'm/inner/testdata/fuzz/FuzzM/abc'])
        self.assertEqual(finding[5], 'm')

        report = self.report(tmp / 'report', '|'.join(finding))
        self.assertTrue(report.startswith('[fuzz] FuzzM (m) regression — abc\n'), report)
        self.assertIn('**Target**: `FuzzM`\n**Module**: `m`\n**Reproducer hash**', report)
        self.assertIn("cd m && go test -run='^FuzzM$/abc' ./...", report)

    def test_a_root_finding_renders_with_no_module_line(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        for line in ('R|abc|testdata/fuzz/R/abc|1|eA==|.', 'R|abc|testdata/fuzz/R/abc|1|eA=='):
            with self.subTest(line=line):
                report = self.report(tmp / str(len(line)), line)
                self.assertTrue(report.startswith('[fuzz] R regression — abc\n'), report)
                self.assertIn('**Target**: `R`\n**Reproducer hash**: `abc`', report)
                self.assertIn("\ngo test -run='^R$/abc' ./...\n", report)
                self.assertNotIn('Module', report)

    def report(
        self,
        tmp: pathlib.Path,
        line: str,
        files: dict[str, str] | None = None,
        open_titles: tuple[str, ...] = (),
    ) -> str:
        """Replays the report step; returns the filed body, or the closed titles."""
        tmp.mkdir(parents=True)
        target = tmp / 'cwd' / 'target'
        target.mkdir(parents=True)
        for rel, body in (files or {}).items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_text(body)
        (tmp / 'cwd' / 'shard-findings' / 'a').mkdir(parents=True)
        (tmp / 'cwd' / 'shard-findings' / 'a' / 'findings.txt').write_text(
            line + '\n' if line else ''
        )
        ci = tmp / 'cwd' / 'ci' / 'scripts'
        ci.mkdir(parents=True)
        (ci / 'go-units.sh').symlink_to(SCRIPTS / 'go-units.sh')
        (ci / 'open.tsv').write_text(''.join(f'1\t{t}\n' for t in open_titles))
        (ci / 'tracker_issue.py').write_text(
            'import pathlib, sys\n'
            'a = sys.argv[1:]\n'
            "mode, here = a[a.index('--mode') + 1], pathlib.Path(__file__).parent\n"
            "if mode == 'recur':\n"
            "    title = a[a.index('--title') + 1]\n"
            "    body = pathlib.Path(a[a.index('--body-file') + 1]).read_text()\n"
            "    (here / 'body.md').write_text(f'{title}\\n{body}')\n"
            "elif mode == 'list':\n"
            "    print((here / 'open.tsv').read_text(), end='')\n"
            "elif mode == 'close-when-clean':\n"
            "    with open(here / 'closed.txt', 'a') as f:\n"
            "        f.write(a[a.index('--title') + 1] + '\\n')\n"
        )
        skip = [
            '--skip',
            'Clone target repo at pinned SHA',
            '--skip',
            "Download this repo's shard findings",
        ]
        skip += ['--skip', 'Checkout ci (for the tracker transport)']
        rc, result, _ = replay_job(
            tmp,
            'weekly-fuzz.yaml',
            'report',
            skip,
            matrix={'repo': 'go', 'sha': 'f00d', 'artifacts': 'x'},
        )
        self.assertEqual(rc, 0, result.get('failed'))
        if not line:
            closed = ci / 'closed.txt'
            return closed.read_text() if closed.exists() else ''
        return (ci / 'body.md').read_text()

    def test_a_seed_closes_only_the_issue_of_the_module_that_holds_it(self):
        modules = {
            'go.mod': 'module r\n',
            'jsonx/go.mod': 'module j\n',
            'jsonx/j.go': '',
            'xmlx/go.mod': 'module x\n',
            'xmlx/x.go': '',
            'jsonx/testdata/fuzz/FuzzP/a1b2': 'seed',
            'jsonx/testdata/fuzz/FuzzR/c3d4': 'seed',
            # A module inside jsonx: its seed closes no jsonx issue.
            'jsonx/inner/go.mod': 'module i\n',
            'jsonx/inner/i.go': '',
            'jsonx/inner/testdata/fuzz/FuzzQ/e5f6': 'seed',
            # A dir named like a substring of a module, which a loose match would take.
            'json/testdata/fuzz/FuzzP/a1b2': 'seed',
        }
        titles = (
            '[fuzz] FuzzP (jsonx) regression — a1b2',
            '[fuzz] FuzzP (xmlx) regression — a1b2',
            '[fuzz] FuzzR regression — c3d4',
            '[fuzz] FuzzP (../jsonx) regression — a1b2',
            '[fuzz] FuzzQ (jsonx) regression — e5f6',
            '[fuzz] FuzzP (json) regression — a1b2',
        )
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        closed = self.report(tmp / 'nested', '', modules, titles)
        self.assertEqual(closed, '[fuzz] FuzzP (jsonx) regression — a1b2\n')
        rooted = {**modules, 'testdata/fuzz/FuzzR/c3d4': 'seed'}
        closed = self.report(tmp / 'root', '', rooted, titles[2:3])
        self.assertEqual(closed, '[fuzz] FuzzR regression — c3d4\n')


if __name__ == '__main__':
    unittest.main()
