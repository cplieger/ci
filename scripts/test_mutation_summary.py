"""Every outcome a repository call can have, through its real jobs: the call's record and the fleet summary.

Each case replays mutation-stryker-repo.yaml's or mutation-gremlins-repo.yaml's
`result` job (its `if:` and its record step, from the workflow file) on a
`needs` context, the Stryker ones on the outputs a replayed `discover` job and
`stryker-shard.py matrix` wrote, then runs the weekly `fleet` summary over the
record. A case asserts the record's outcome and per-entry states, the result
job's status and the fleet's row and status.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
WORKFLOWS = SCRIPTS.parent / '.github' / 'workflows'
SUMMARY = SCRIPTS / 'mutation-summary.py'
SHA = '0123456789abcdef0123456789abcdef01234567'

_spec = importlib.util.spec_from_file_location('workflow_replay', SCRIPTS / 'workflow_replay.py')
workflow_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workflow_replay)

WORKFLOW = {
    'stryker': WORKFLOWS / 'mutation-stryker-repo.yaml',
    'gremlins': WORKFLOWS / 'mutation-gremlins-repo.yaml',
}
STRYKER_JOBS = ('discover', 'plan', 'shard', 'run', 'merge', 'badge', 'aggregate')
GREMLINS_JOBS = ('setup', 'plan', 'shard', 'run', 'record', 'badge', 'aggregate')
GIT_STUB = (
    '#!/bin/bash\n'
    'if [ "$1" = clone ]; then mkdir -p scan; while IFS= read -r c; do [ -n "$c" ] || continue\n'
    '  mkdir -p "scan/$(dirname "$c")"; echo "{}" > "scan/$c"; done < tree\n'
    '  [ -d contents ] && cp -R contents/. scan/; exit 0; fi\n'
    f'echo {SHA}\n'
)


class Calls(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.n = 0

    def dir(self) -> Path:
        self.n += 1
        d = self.tmp / str(self.n)
        d.mkdir()
        (d / 'scripts').symlink_to(SCRIPTS)
        return d

    def replay(
        self, tool: str, job: str, context: dict, cwd: Path, **argv: str
    ) -> tuple[int, dict]:
        (cwd / 'context.json').write_text(json.dumps(context))
        args = [
            '--workflow', str(WORKFLOW[tool]), '--job', job, '--context', str(cwd / 'context.json'),
            '--cwd', str(cwd), '--out', str(cwd / 'replay.json'),
        ]  # fmt: skip
        for k, v in argv.items():
            args += [f'--{k.replace("_", "-")}', v]
        with contextlib.redirect_stderr(io.StringIO()):
            rc = workflow_replay.main(args)
        return rc, json.loads((cwd / 'replay.json').read_text())

    # --- Stryker -----------------------------------------------------------

    def discover(self, tree: list[str], files: dict | None = None, **inputs) -> dict:
        """The discover job's outputs on a stub clone holding TREE's configs and FILES."""
        cwd = self.dir()
        (cwd / 'bin').mkdir()
        (cwd / 'bin' / 'git').write_text(GIT_STUB)
        (cwd / 'bin' / 'git').chmod(0o755)
        (cwd / 'tree').write_text(''.join(f'{c}\n' for c in tree))
        for rel, text in (files or {}).items():
            (cwd / 'contents' / rel).parent.mkdir(parents=True, exist_ok=True)
            (cwd / 'contents' / rel).write_text(text)
        context = {'inputs': {'repository': 'a', 'dir': '', 'honour-opt-out': True, **inputs}}
        with unittest.mock.patch.dict(os.environ, {'PATH': f'{cwd / "bin"}:{os.environ["PATH"]}'}):
            rc, out = self.replay('stryker', 'discover', context, cwd)
        self.assertEqual(rc, 0, out.get('failed'))
        return out['outputs']

    def shard_outputs(self, found: dict, skipped: tuple[str, ...] = ()) -> dict:
        """The shard job's matrices from stryker-shard.py matrix, SKIPPED dirs' plans marked not enrolled."""
        d = self.dir()
        for dir_ in skipped:
            key = next(e['key'] for e in json.loads(found['entries']) if e['dir'] == dir_)
            (d / 'plans').mkdir(exist_ok=True)
            (d / 'plans' / f'{key}.json').write_text(
                json.dumps({'repo': 'a', 'dir': dir_, 'sha': SHA, 'skipped': True})
            )
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / 'stryker-shard.py'),
                'matrix',
                '--entries',
                found['entries'],
                '--plans',
                str(d / 'plans'),
                '--target-minutes',
                '200',
                '--history',
                str(d / 'history'),
                '--out-plans',
                str(d / 'out'),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        return {'run': json.dumps(out['run']), 'merge': json.dumps(out['merge'])}

    def stryker_needs(self, found: dict, matrices: dict | None, **results: str) -> dict:
        """NEEDS of the Stryker result job: every job succeeded unless RESULTS says otherwise."""
        some = found.get('entries') not in (None, '[]')
        needs = {
            j: {'result': 'success' if some else 'skipped', 'outputs': {}} for j in STRYKER_JOBS
        }
        needs['discover'] = {'result': 'success', 'outputs': found}
        needs['shard']['outputs'] = matrices or {}
        for j, r in results.items():
            needs[j]['result'] = r
        return needs

    # --- both --------------------------------------------------------------

    @staticmethod
    def merged(tool: str, needs: dict, cwd: Path, judged: dict | None) -> None:
        """The merged results the result job downloads: per dir or module, JUDGED's statuses
        (Stryker; an int N is N Killed) or judged count per attempt (gremlins; an int is every
        attempt's). None gives every planned entry one judged mutant."""
        out = needs['shard'].get('outputs') or {}
        try:
            merge = json.loads(out.get('merge') or '[]')
            legs = json.loads(out.get('matrix') or '[]')
        except ValueError:
            merge, legs = [], []
        if tool == 'stryker':
            if judged is None:
                judged = {m['dir']: 1 for m in merge if isinstance(m, dict)}
            for dir_, statuses in judged.items():
                if isinstance(statuses, int):
                    statuses = ['Killed'] * statuses
                d = cwd / 'reports' / f'stryker-{dir_.replace("/", "=")}'
                d.mkdir(parents=True)
                (d / 'meta.json').write_text(json.dumps({'repo': 'a', 'dir': dir_, 'score': '1.0'}))
                mutants = [{'id': str(i), 'status': st} for i, st in enumerate(statuses)]
                (d / 'mutation.json').write_text(
                    json.dumps({'files': {'src/a.ts': {'mutants': mutants}}})
                )
            return
        modules = (needs['plan'].get('outputs') or {}).get('modules') or ''
        if judged is None:
            judged = dict.fromkeys(modules.split(), 1) if isinstance(modules, str) else {}
        for leg in legs:
            a, shard = leg['attempt'], leg.get('shard')
            d = cwd / 'attempts' / f'gremlins-a-{a}{"" if shard is None else f"-shard{shard}"}'
            d.mkdir(parents=True)
            meta = {'repo': 'a', 'attempt': str(a)}
            if shard is not None:
                meta |= {'shard': str(shard), 'shards': str(leg['shards'])}
            (d / 'meta.json').write_text(json.dumps(meta))
            rows = []
            for m, n in judged.items() if shard in (None, 1) else ():
                n = n if isinstance(n, int) else n.get(a)
                if n is not None:
                    rows.append({'dir': m, 'mutants_killed': n, 'mutants_lived': 0,
                                 'mutants_not_covered': 0, 'mutants_timed_out': 0})  # fmt: skip
            (d / 'gremlins-out.json').write_text(json.dumps({'files': [], 'modules': rows}))

    def call(
        self, tool: str, needs: dict, judged: dict | None = None, **inputs: str
    ) -> tuple[int | None, dict | None]:
        """The result job's (status, record): None, None when the job is skipped; status 1 when it fails."""
        cwd = self.dir()
        target = 'dir' if tool == 'stryker' else 'module'
        context = {'needs': needs, 'inputs': {'repository': 'a', target: '', **inputs}}
        self.merged(tool, needs, cwd, judged)
        rc, out = self.replay(
            tool, 'result', context, cwd,
            stop_before="Upload this call's result", skip="Download this call's merged results",
        )  # fmt: skip
        if out.get('skipped'):
            return None, None
        files = list((cwd / 'result').glob('*.json')) if (cwd / 'result').is_dir() else []
        self.assertLessEqual(len(files), 1)
        self.assertIn(rc, (0, 1), out.get('failed'))
        return rc, json.loads(files[0].read_text()) if files else None

    def fleet(
        self, tool: str, records: dict[str, object], keys: list[str]
    ) -> tuple[int, dict[str, str], str]:
        """The fleet summary's status, each key's outcome line and the run summary, over RECORDS by key."""
        d = self.dir()
        (d / 'results').mkdir()
        for key, doc in records.items():
            text = doc if isinstance(doc, str) else json.dumps(doc)
            (d / 'results' / f'{key}.json').write_text(text)
        summary = d / 'summary.md'
        proc = subprocess.run(
            [
                sys.executable,
                str(SUMMARY),
                'fleet',
                '--tool',
                tool,
                '--keys',
                json.dumps(keys),
                '--results',
                str(d / 'results'),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, 'GITHUB_STEP_SUMMARY': str(summary)},
        )
        rows = dict(
            line[2:].split(': ', 1) for line in proc.stderr.splitlines() if line.startswith('- ')
        )
        return proc.returncode, rows, summary.read_text() if summary.exists() else ''

    def outcome(
        self,
        tool: str,
        needs: dict,
        want: str,
        states: dict[str, str],
        judged: dict | None = None,
        **inputs: str,
    ) -> dict:
        """Assert the record's WANT outcome and STATES, the result job's status, and the fleet's row."""
        rc, doc = self.call(tool, needs, judged, **inputs)
        self.assertIsNotNone(doc, 'the result job wrote no record')
        green = want in ('passed', 'opted out')
        self.assertEqual((doc['outcome'], rc), (want, 0 if green else 1), doc)
        self.assertEqual({n: e['state'] for n, e in doc['entries'].items()}, states, doc)
        self.assertEqual(doc['tested'], sorted(n for n, s in states.items() if s == 'tested'))
        self.assertEqual(doc['found'], sorted(states))
        frc, rows, _ = self.fleet(tool, {doc['key']: doc}, [doc['key']])
        self.assertEqual((rows, frc), ({doc['key']: want}, 0 if green else 1))
        return doc

    # --- the Stryker table -------------------------------------------------

    def test_stryker_success_passes(self):
        found = self.discover(['v/stryker.config.json', 'w/stryker.config.json'])
        doc = self.outcome(
            'stryker', self.stryker_needs(found, self.shard_outputs(found)), 'passed',
            {'v': 'tested', 'w': 'tested'},
        )  # fmt: skip
        self.assertEqual((doc['shards'], doc['reason'], doc['fault']), (1, '', ''))

    def test_stryker_shards_is_the_most_one_dir_runs(self):
        found = self.discover(['v/stryker.config.json', 'w/stryker.config.json'])
        run = [{'dir': 'v', 'shard': n, 'shards': 3} for n in (1, 2, 3)] + [
            {'dir': 'w', 'shard': 1, 'shards': 1}
        ]
        shard = {**self.shard_outputs(found), 'run': json.dumps(run)}
        doc = self.outcome(
            'stryker', self.stryker_needs(found, shard), 'passed', {'v': 'tested', 'w': 'tested'}
        )
        self.assertEqual(doc['shards'], 3)

    def test_stryker_discover_failed_after_its_key_is_red(self):
        needs = self.stryker_needs({'key': 'a'}, None, discover='failure')
        for j in STRYKER_JOBS[1:]:
            needs[j]['result'] = 'skipped'
        doc = self.outcome('stryker', needs, 'failed', {})
        self.assertIn('the discover job ended failure', doc['reason'])

    def test_stryker_discover_failed_before_its_key_leaves_no_record_and_the_fleet_red(self):
        needs = self.stryker_needs({}, None, discover='failure')
        self.assertEqual(self.call('stryker', needs), (None, None))
        rc, rows, summary = self.fleet('stryker', {}, ['a'])
        self.assertEqual((rc, rows), (1, {'a': 'not run'}))
        self.assertIn('no result', summary)

    def test_stryker_empty_discover_is_not_run_and_red(self):
        found = self.discover([])
        self.assertEqual(found['entries'], '[]')
        doc = self.outcome('stryker', self.stryker_needs(found, None), 'not run', {})
        self.assertEqual(doc['reason'], 'it found no entry to test')

    def test_stryker_a_refused_dir_alone_is_red(self):
        found = self.discover(['a b/stryker.config.json'])
        doc = self.outcome('stryker', self.stryker_needs(found, None), 'failed', {'a b': 'refused'})
        self.assertIn("'a b' is not a relative path", doc['entries']['a b']['reason'])

    def test_stryker_a_refused_dir_beside_siblings_is_red_and_they_are_tested(self):
        found = self.discover(['a b/stryker.config.json', 'w/stryker.config.json'])
        self.assertEqual([e['dir'] for e in json.loads(found['entries'])], ['w'])
        self.outcome(
            'stryker', self.stryker_needs(found, self.shard_outputs(found)), 'failed',
            {'a b': 'refused', 'w': 'tested'},
        )  # fmt: skip

    def test_stryker_every_dir_opted_out_is_green(self):
        found = self.discover(['stryker.config.json'], {'stryker.weekly.json': '{"weekly": false}'})
        self.outcome('stryker', self.stryker_needs(found, None), 'opted out', {'.': 'opted out'})

    def test_stryker_an_opted_out_dir_beside_a_tested_one_passes(self):
        found = self.discover(
            ['v/stryker.config.json', 'w/stryker.config.json'],
            {'v/stryker.weekly.json': '{"weekly": false}'},
        )
        self.outcome(
            'stryker', self.stryker_needs(found, self.shard_outputs(found)), 'passed',
            {'v': 'opted out', 'w': 'tested'},
        )  # fmt: skip

    def test_stryker_a_dispatch_tests_an_opted_out_dir(self):
        found = self.discover(
            ['stryker.config.json'],
            {'stryker.weekly.json': '{"weekly": false}'},
            **{'honour-opt-out': False},
        )
        self.outcome(
            'stryker',
            self.stryker_needs(found, self.shard_outputs(found)),
            'passed',
            {'.': 'tested'},
        )

    def test_stryker_a_dir_that_is_not_enrolled_is_not_run_and_red(self):
        found = self.discover(['v/stryker.config.json', 'w/stryker.config.json'])
        doc = self.outcome(
            'stryker', self.stryker_needs(found, self.shard_outputs(found, skipped=('v',))), 'not run',
            {'v': 'not run', 'w': 'tested'},
        )  # fmt: skip
        self.assertIn('not enrolled', doc['entries']['v']['reason'])

    def test_stryker_tested_is_the_count_of_judged_mutants_in_the_merged_report(self):
        found = self.discover(['v/stryker.config.json', 'w/stryker.config.json'])
        needs = self.stryker_needs(found, self.shard_outputs(found))
        doc = self.outcome('stryker', needs, 'passed', {'v': 'tested', 'w': 'tested'},
                           {'v': ['Killed', 'Survived', 'NoCoverage', 'Timeout', 'Ignored'], 'w': 1})  # fmt: skip
        self.assertEqual((doc['entries']['v']['mutants'], doc['entries']['w']['mutants']), (4, 1))

    def test_stryker_an_unenrolled_dir_whose_plan_was_lost_fails_at_its_merge(self):
        # The plan download failed, so the dir ran as one job, exited 3 and staged
        # nothing, and merge exits 1 (test_stryker_shard's every-shard-skipped test).
        found = self.discover(['w/stryker.config.json'])
        shard = self.shard_outputs(found)
        self.assertEqual([m['dir'] for m in json.loads(shard['merge'])], ['w'])
        doc = self.outcome(
            'stryker',
            self.stryker_needs(found, shard, merge='failure'),
            'failed',
            {'w': 'failed'},
            {},
        )
        self.assertIn('the merge job ended failure', doc['entries']['w']['reason'])

    def test_stryker_a_merged_report_with_no_judged_mutant_is_not_run(self):
        found = self.discover(['v/stryker.config.json', 'w/stryker.config.json'])
        needs = self.stryker_needs(found, self.shard_outputs(found))
        for v in ([], ['Ignored', 'CompileError', 'RuntimeError', 'Pending']):
            with self.subTest(v=v):
                doc = self.outcome('stryker', needs, 'not run', {'v': 'not run', 'w': 'tested'},
                                   {'v': v, 'w': 2})  # fmt: skip
                self.assertIn(
                    'v: not run (its merged result holds no mutant a test judged)', doc['reason']
                )

    def test_stryker_an_unusable_or_foreign_merged_report_is_not_run(self):
        found = self.discover(['w/stryker.config.json'])
        needs = self.stryker_needs(found, self.shard_outputs(found))
        for label, (meta, report, why) in {
            'torn report': ({'repo': 'a', 'dir': 'w'}, '{', 'unreadable'),
            'no files': ({'repo': 'a', 'dir': 'w'}, '[]', 'holds no files'),
            'another repo': ({'repo': 'b', 'dir': 'w'}, '{"files": {}}', 'no merged result'),
            'torn meta': ('{', '{"files": {}}', 'no merged result'),
        }.items():
            with self.subTest(label):
                cwd = self.dir()
                (cwd / 'reports').mkdir()
                (cwd / 'reports' / 'meta.json').write_text(
                    meta if isinstance(meta, str) else json.dumps(meta)
                )
                (cwd / 'reports' / 'mutation.json').write_text(report)
                context = {'needs': needs, 'inputs': {'repository': 'a', 'dir': ''}}
                rc, _ = self.replay('stryker', 'result', context, cwd,
                                    stop_before="Upload this call's result",
                                    skip="Download this call's merged results")  # fmt: skip
                doc = json.loads((cwd / 'result' / 'a.json').read_text())
                self.assertEqual(
                    (rc, doc['outcome'], doc['entries']['w']['state']), (1, 'not run', 'not run')
                )
                self.assertIn(why, doc['entries']['w']['reason'])

    def test_stryker_a_failed_plan_is_red_though_its_fallback_tested(self):
        found = self.discover(['w/stryker.config.json'])
        doc = self.outcome(
            'stryker', self.stryker_needs(found, self.shard_outputs(found), plan='failure'), 'failed',
            {'w': 'tested'},
        )  # fmt: skip
        self.assertIn('failed jobs: plan', doc['reason'])

    def test_stryker_one_failed_shard_fails_every_dir_of_its_matrix(self):
        found = self.discover(['v/stryker.config.json', 'w/stryker.config.json'])
        for job, result in (('run', 'failure'), ('run', 'cancelled'), ('merge', 'failure')):
            with self.subTest(job=job, result=result):
                doc = self.outcome(
                    'stryker', self.stryker_needs(found, self.shard_outputs(found), **{job: result}),
                    'failed', {'v': 'failed', 'w': 'failed'},
                )  # fmt: skip
                self.assertIn(f'the {job} job ended {result}', doc['entries']['v']['reason'])

    def test_stryker_a_shard_job_that_failed_or_wrote_no_matrix_is_red(self):
        found = self.discover(['w/stryker.config.json'])
        needs = self.stryker_needs(found, None, shard='failure', run='skipped', merge='skipped')
        self.outcome('stryker', needs, 'failed', {'w': 'failed'})
        needs = self.stryker_needs(found, {'run': '[]', 'merge': 'torn'})
        self.outcome('stryker', needs, 'failed', {'w': 'failed'})

    def test_stryker_a_failed_badge_or_tracker_is_red(self):
        found = self.discover(['w/stryker.config.json'])
        for job in ('badge', 'aggregate'):
            with self.subTest(job=job):
                doc = self.outcome(
                    'stryker', self.stryker_needs(found, self.shard_outputs(found), **{job: 'failure'}),
                    'failed', {'w': 'tested'},
                )  # fmt: skip
                self.assertIn(f'failed jobs: {job}', doc['reason'])

    def test_stryker_a_one_dir_call_passes_without_badge_or_tracker(self):
        found = self.discover(['v/stryker.config.json', 'w/stryker.config.json'], dir='w')
        needs = self.stryker_needs(
            found, self.shard_outputs(found), badge='skipped', aggregate='skipped'
        )
        doc = self.outcome('stryker', needs, 'passed', {'w': 'tested'}, dir='w')
        self.assertEqual((doc['key'], doc['target']), ('a~w', 'w'))

    # --- the gremlins table ------------------------------------------------

    @staticmethod
    def gremlins_needs(found: dict | None = None, legs: list | None = None, **results: str) -> dict:
        needs = {j: {'result': 'success', 'outputs': {}} for j in GREMLINS_JOBS}
        needs['setup']['outputs'] = {'key': 'a'}
        needs['plan']['outputs'] = (
            {'modules': '. mapread', 'refused': '[]'} if found is None else found
        )
        legs = (
            [{'attempt': a, 'shard': s, 'shards': 2} for a in (1, 2, 3) for s in (1, 2)]
            if legs is None
            else legs
        )
        needs['shard']['outputs'] = {'matrix': json.dumps(legs)}
        for j, r in results.items():
            needs[j]['result'] = r
        return needs

    def test_gremlins_success_passes(self):
        doc = self.outcome(
            'gremlins', self.gremlins_needs(), 'passed', {'.': 'tested', 'mapread': 'tested'}
        )
        self.assertEqual(doc['shards'], 2)
        doc = self.outcome('gremlins', self.gremlins_needs(legs=[{'attempt': a} for a in (1, 2, 3)]), 'passed',
                           {'.': 'tested', 'mapread': 'tested'})  # fmt: skip
        self.assertEqual(doc['shards'], 1)

    def test_gremlins_tested_is_the_fewest_judged_mutants_over_the_attempts(self):
        doc = self.outcome('gremlins', self.gremlins_needs(), 'passed', {'.': 'tested', 'mapread': 'tested'},
                           {'.': {1: 5, 2: 3, 3: 4}, 'mapread': 2})  # fmt: skip
        self.assertEqual(
            (doc['entries']['.']['mutants'], doc['entries']['mapread']['mutants']), (3, 2)
        )

    def test_gremlins_a_module_an_attempt_did_not_judge_is_not_run(self):
        for label, judged in {
            'no result at all': {},
            'one attempt lost the module': {'.': 1, 'mapread': {1: 1, 2: 1}},
            'judged nothing': {'.': 1, 'mapread': 0},
        }.items():
            with self.subTest(label):
                states = {'.': 'tested' if '.' in judged else 'not run', 'mapread': 'not run'}
                doc = self.outcome('gremlins', self.gremlins_needs(), 'not run', states, judged)
                self.assertIn('mapread: not run', doc['reason'])

    def test_gremlins_setup_failed_leaves_no_record_and_the_fleet_red(self):
        needs = self.gremlins_needs(setup='failure')
        self.assertEqual(self.call('gremlins', needs), (None, None))
        self.assertEqual(self.fleet('gremlins', {}, ['a'])[:2], (1, {'a': 'not run'}))

    def test_gremlins_a_plan_that_failed_before_its_outputs_is_red(self):
        doc = self.outcome('gremlins', self.gremlins_needs({}, plan='failure'), 'failed', {})
        self.assertIn('the plan job wrote no usable entry list', doc['reason'])

    def test_gremlins_a_failed_sizing_is_red_though_its_fallback_tested(self):
        doc = self.outcome(
            'gremlins',
            self.gremlins_needs(plan='failure'),
            'failed',
            {'.': 'tested', 'mapread': 'tested'},
        )
        self.assertIn('failed jobs: plan', doc['reason'])

    def test_gremlins_a_refused_module_beside_siblings_is_red_and_they_are_tested(self):
        refused = json.dumps(
            [{'dir': 'odd dir', 'reason': "directory 'odd dir' is not a relative path"}]
        )
        doc = self.outcome(
            'gremlins', self.gremlins_needs({'modules': '.', 'refused': refused}), 'failed',
            {'.': 'tested', 'odd dir': 'refused'},
        )  # fmt: skip
        self.assertIn('odd dir: refused', doc['reason'])

    def test_gremlins_one_failed_shard_fails_every_module(self):
        for job, result in (('run', 'failure'), ('shard', 'failure'), ('run', 'cancelled')):
            with self.subTest(job=job, result=result):
                self.outcome('gremlins', self.gremlins_needs(**{job: result}), 'failed',
                             {'.': 'failed', 'mapread': 'failed'})  # fmt: skip

    def test_gremlins_a_failed_record_badge_or_tracker_is_red(self):
        for job in ('record', 'badge', 'aggregate'):
            with self.subTest(job=job):
                self.outcome('gremlins', self.gremlins_needs(**{job: 'failure'}), 'failed',
                             {'.': 'tested', 'mapread': 'tested'})  # fmt: skip

    def test_gremlins_a_one_module_call_passes_without_badge_or_tracker(self):
        needs = self.gremlins_needs(
            {'modules': 'mapread', 'refused': '[]'}, badge='skipped', aggregate='skipped'
        )
        doc = self.outcome('gremlins', needs, 'passed', {'mapread': 'tested'}, module='mapread')
        self.assertEqual(doc['target'], 'mapread')

    def test_a_pipeline_job_that_did_not_run_leaves_its_entries_not_run_and_red(self):
        found = self.discover(['w/stryker.config.json'])
        for job in ('run', 'merge'):
            with self.subTest('stryker', job=job):
                needs = self.stryker_needs(found, self.shard_outputs(found), **{job: 'skipped'})
                doc = self.outcome('stryker', needs, 'not run', {'w': 'not run'})
                self.assertIn(f'the {job} job was skipped', doc['reason'])
        with self.subTest('gremlins'):
            self.outcome('gremlins', self.gremlins_needs(run='skipped'), 'not run',
                         {'.': 'not run', 'mapread': 'not run'})  # fmt: skip

    # --- the fleet over records it must not trust --------------------------

    def good(self) -> dict:
        _, doc = self.call('gremlins', self.gremlins_needs())
        return doc

    def test_the_fleet_reads_passed_only_from_a_valid_record(self):
        good, failed = self.good(), self.call('gremlins', self.gremlins_needs(run='failure'))[1]
        bad = {
            'torn': '{"tool": "gremlins", ',
            'not an object': '[]',
            'wrong tool': {**good, 'tool': 'stryker'},
            'wrong key': {**good, 'key': 'b'},
            'zero shards': {**good, 'shards': 0},
            'bool shards': {**good, 'shards': True},
            'forged outcome': {**failed, 'outcome': 'passed', 'reason': ''},
            'forged tested': {**failed, 'tested': ['.', 'mapread']},
            'unknown state': {**good, 'entries': {'.': {'state': 'fine', 'reason': ''}}},
            'no entries': {**good, 'entries': None},
            'bad job result': {**good, 'jobs': {'run': 'green'}},
            'fault not text': {**good, 'fault': None},
            'tested without a count': {
                **good,
                'entries': {n: {'state': 'tested', 'reason': ''} for n in good['entries']},
            },
            'tested with zero': {
                **good,
                'entries': {n: {**e, 'mutants': 0} for n, e in good['entries'].items()},
            },
        }
        for label, doc in bad.items():
            with self.subTest(label):
                rc, rows, summary = self.fleet('gremlins', {'a': doc}, ['a'])
                self.assertEqual((rc, rows), (1, {'a': 'failed'}))
                self.assertIn('unreadable result', summary)

    def test_one_bad_row_leaves_the_others_intact_and_the_run_red(self):
        good = self.good()
        records = {'a': good, 'b': {**good, 'key': 'b'}, 'd': '{'}
        rc, rows, summary = self.fleet('gremlins', records, ['a', 'b', 'c', 'd', '../x'])
        self.assertEqual(
            (rc, rows),
            (1, {'a': 'passed', 'b': 'passed', 'c': 'not run', 'd': 'failed', '../x': 'failed'}),
        )
        self.assertEqual(summary.count('\n'), 3 + 5)
        rc, rows, _ = self.fleet('gremlins', {'a': good, 'b': records['b']}, ['a', 'b'])
        self.assertEqual((rc, rows), (0, {'a': 'passed', 'b': 'passed'}))

    def test_a_fleet_of_no_calls_is_red(self):
        rc, rows, _ = self.fleet('gremlins', {}, [])
        self.assertEqual((rc, rows), (1, {}))

    def test_the_result_jobs_read_every_job_s_result(self):
        for tool, jobs in (('stryker', STRYKER_JOBS), ('gremlins', GREMLINS_JOBS)):
            with self.subTest(tool):
                wf = yaml.safe_load(WORKFLOW[tool].read_text())
                self.assertEqual(set(wf['jobs']) - {'result'}, set(jobs))
                self.assertEqual(sorted(wf['jobs']['result']['needs']), sorted(jobs))
                step = next(
                    s
                    for s in wf['jobs']['result']['steps']
                    if s.get('name') == "Write this call's result"
                )
                self.assertEqual(step['env']['NEEDS'], '${{ toJSON(needs) }}')
                names = [s.get('name') for s in wf['jobs']['result']['steps']]
                self.assertLess(
                    names.index("Download this call's merged results"),
                    names.index("Write this call's result"),
                )


if __name__ == '__main__':
    unittest.main()
