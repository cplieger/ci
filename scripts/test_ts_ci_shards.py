"""ts-ci.yaml's package settings and vitest shards: the count read and validated
from the package's own `cplieger.vitestShards` and the floor from its
`engines.node`, the shard matrix and check names of `validate` and
`engines-floor`, which steps each shard runs and how each test step hands vitest
its shard."""

from __future__ import annotations

import json
import stat
import unittest

import workflow_replay as wr
import yaml
from test_deadset_steps import META
from test_package_script_steps import WORKFLOWS, Harness, workflow_step

TS_CI = WORKFLOWS / 'ts-ci.yaml'
READ_STEP = 'Read cplieger.vitestShards and engines.node'
# The jobs that run the vitest suite, each split by the read count, with its
# test step.
SHARDED = {'validate': 'Test (vitest)', 'engines-floor': 'Test (vitest) on the engines floor'}

# Logs the arguments vitest received, so a test sees what reached it.
VITEST_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG/vitest"
"""


def ts_ci() -> dict:
    return yaml.safe_load(TS_CI.read_text())


def evaluate(template: str, contexts: dict):
    """A `${{ }}` template as GitHub evaluates it, typed (a list stays a list)."""
    whole = wr.EXPR.fullmatch(template.strip())
    assert whole, template
    return wr.Scope(contexts, {}, []).evaluate(whole.group(1))


def counted(count: int | None, result: str = 'success', floor: str = '24') -> dict:
    """The needs context for the package job: outputs as the read step writes
    them, or none when it failed."""
    outputs = (
        {}
        if count is None
        else {'count': str(count), 'shards': json.dumps(list(range(1, count + 1))), 'floor': floor}
    )
    return {'package': {'result': result, 'outputs': outputs}}


class PackageSettings(Harness):
    STEP = workflow_step(TS_CI, 'package', READ_STEP)

    def read(self, manifest: object | None = None, *, raw: str | None = None):
        if raw is not None:
            (self.pkg / 'package.json').write_text(raw)
        elif manifest is not None:
            (self.pkg / 'package.json').write_text(json.dumps(manifest))
        return self.run_step({'READ_PACKAGE': self.STEP['env']['READ_PACKAGE'], 'PACKAGE': './package.json'})

    def assert_refused(self, run, message: str):
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn(message, run.out)
        self.assertEqual(run.outputs, {})
        self.assertEqual([line for line in run.out.splitlines() if line.startswith('::')], [run.out.strip()])

    def floor(self, node: object) -> str:
        run = self.read({'engines': {'node': node}})
        self.assertEqual(run.rc, 0, run.out)
        return run.outputs['floor']

    def test_the_step_reads_the_working_directory_package(self):
        self.assertEqual(self.STEP['env']['PACKAGE'], '${{ inputs.working-directory }}/package.json')
        self.assertNotIn('defaults', ts_ci()['jobs']['package'])

    def test_a_package_without_the_field_runs_one_shard(self):
        for manifest in ({'name': 'x'}, {'name': 'x', 'cplieger': {}}, {'cplieger': {'other': 3}}):
            with self.subTest(manifest=manifest):
                run = self.read(manifest)
                self.assertEqual(run.rc, 0, run.out)
                self.assertEqual(run.outputs, {'count': '1', 'shards': '[1]', 'floor': ''})

    def test_a_count_from_one_to_one_hundred_lists_every_shard_once(self):
        for count in (1, 2, 100):
            with self.subTest(count=count):
                run = self.read({'cplieger': {'vitestShards': count}})
                self.assertEqual(run.rc, 0, run.out)
                self.assertEqual(run.outputs['count'], str(count))
                self.assertEqual(json.loads(run.outputs['shards']), list(range(1, count + 1)))

    def test_a_count_that_is_not_an_integer_from_one_to_one_hundred_fails_the_job(self):
        for value in (0, 101, -1, 2.5, '2', True, None, [2], {'n': 2}, 1e300):
            with self.subTest(value=value):
                run = self.read({'cplieger': {'vitestShards': value}})
                self.assert_refused(run, 'it must be an integer from 1 to 100')

    def test_a_cplieger_field_that_is_not_an_object_fails_the_job(self):
        for value in (2, 'vitestShards', None, [{'vitestShards': 2}]):
            with self.subTest(value=value):
                self.assert_refused(self.read({'cplieger': value}), '"cplieger" must be an object')

    def test_an_unreadable_package_fails_the_job(self):
        self.assert_refused(self.read(), 'cannot read the package settings')
        self.assert_refused(self.read(raw='{"cplieger":'), 'cannot read the package settings')

    def test_a_value_cannot_start_a_workflow_command(self):
        run = self.read({'cplieger': {'vitestShards': '\n::add-mask::x\n'}})
        self.assert_refused(run, 'cplieger.vitestShards is "\\n::add-mask::x\\n"')
        run = self.read(raw='{"a":\n::add-mask::x\n}')
        self.assert_refused(run, 'cannot read the package settings')

    def test_the_floor_is_the_major_npm_reads_as_the_oldest_allowed(self):
        for node, want in (
            ('>=24', '24'),
            ('>=24.3', '24'),
            ('>= 24.3.1', '24'),
            ('^22', '22'),
            ('~20.1', '20'),
            ('~>20', '20'),
            ('=18.17.0', '18'),
            ('v18', '18'),
            ('v=20', '20'),
            ('24.x', '24'),
            ('20 - 22', '20'),
            (' >=26 <28', '26'),
            ('<26 >=22', '22'),
            ('>=18 >=20', '20'),
            ('>24', '25'),
            ('^22 || ^20', '20'),
            ('^10 || ^8', '8'),
            ('22 || >=20.3 || ^24', '20'),
            ('>=4', '4'),
        ):
            with self.subTest(node=node):
                self.assertEqual(self.floor(node), want)

    def test_no_engines_node_means_no_floor_with_a_notice(self):
        for manifest in (
            {},
            {'engines': {}},
            {'engines': {'node': None}},
            {'engines': {'node': ''}},
            {'engines': 'x'},
        ):
            with self.subTest(manifest=manifest):
                run = self.read(manifest)
                self.assertEqual(run.rc, 0, run.out)
                self.assertEqual(run.outputs['floor'], '')
                self.assertIn('::notice::package.json declares no engines.node floor', run.out)

    def test_a_floor_below_node_4_means_no_floor_with_its_own_notice(self):
        for node, major in (('*', 0), ('>=0.0.0', 0), ('>=0.12', 0), ('<26', 0), ('^22 || *', 0), ('>=1', 1), ('^3', 3)):
            with self.subTest(node=node):
                run = self.read({'engines': {'node': node}})
                self.assertEqual(run.rc, 0, run.out)
                self.assertEqual(run.outputs['floor'], '')
                self.assertIn(f'::notice::engines.node {json.dumps(node)} allows Node {major}, which this job cannot install', run.out)

    def test_an_invalid_range_fails_the_job(self):
        for node in ('lts', '08', '>=1e3', '>=2O', '>=24 ' + 'x' * 500, '>=24\n::add-mask::x'):
            with self.subTest(node=node):
                run = self.read({'engines': {'node': node}})
                self.assert_refused(run, 'it must be a valid semver range')
                self.assertLess(len(run.out), 200, run.out)

    def test_a_range_no_version_satisfies_fails_the_job(self):
        for node in ('>=22 <22', '22 - 20', '^18 >=20'):
            with self.subTest(node=node):
                self.assert_refused(self.read({'engines': {'node': node}}), 'no version satisfies it')

    def test_the_job_passes_on_every_output_the_step_writes(self):
        run = self.read({'cplieger': {'vitestShards': 2}, 'engines': {'node': '>=22'}})
        job = ts_ci()['jobs']['package']
        steps = {self.STEP['id']: {'outputs': run.outputs}}
        self.assertEqual({k: evaluate(v, {'steps': steps}) for k, v in job['outputs'].items()}, run.outputs)

    def test_a_floor_is_logged_without_a_notice(self):
        run = self.read({'engines': {'node': '>=24'}})
        self.assertIn('engines.node ">=24": testing on Node 24', run.out)
        self.assertNotIn('::', run.out)

    def test_an_engines_node_that_is_not_a_string_fails_the_job(self):
        for value in (24, 0, False, ['>=24'], {'min': 24}, True):
            with self.subTest(value=value):
                self.assert_refused(self.read({'engines': {'node': value}}), 'it must be a version range string')

    def test_a_long_value_is_cut_in_the_error(self):
        run = self.read({'cplieger': {'vitestShards': 'x' * 500}})
        self.assert_refused(run, 'it must be an integer from 1 to 100')
        self.assertLess(len(run.out), 200)


class ShardMatrix(unittest.TestCase):
    def test_each_sharded_job_waits_for_the_count_and_takes_its_list(self):
        for job in SHARDED:
            with self.subTest(job=job):
                spec = ts_ci()['jobs'][job]
                self.assertEqual(spec['needs'], 'package')
                matrix = spec['strategy']['matrix']['shard']
                for count in (1, 2, 100):
                    with self.subTest(count=count):
                        self.assertEqual(evaluate(matrix, {'needs': counted(count)}), list(range(1, count + 1)))

    def test_only_the_sharded_jobs_run_vitest(self):
        jobs = ts_ci()['jobs']
        running = {job for job, spec in jobs.items() for step in spec.get('steps', []) if 'npm test' in step.get('run', '')}
        self.assertEqual(running, set(SHARDED))

    def test_the_count_is_no_longer_an_input(self):
        self.assertNotIn('vitest-shards', ts_ci()[True]['workflow_call']['inputs'])
        self.assertNotIn('vitest-shards', TS_CI.read_text())

    def test_one_failed_shard_does_not_cancel_the_others(self):
        for job in SHARDED:
            with self.subTest(job=job):
                self.assertFalse(ts_ci()['jobs'][job]['strategy']['fail-fast'])

    def name(self, shard: int, count: int) -> str:
        template = ts_ci()['jobs']['validate']['name']
        return evaluate(template, {'needs': counted(count), 'matrix': {'shard': shard}})

    def test_unsharded_validate_keeps_its_plain_name(self):
        self.assertEqual(self.name(1, 1), 'validate')

    def test_each_validate_shard_is_named_with_its_index_and_count(self):
        self.assertEqual(self.name(1, 2), 'validate (shard 1/2)')
        self.assertEqual(self.name(2, 2), 'validate (shard 2/2)')

    def test_the_floor_job_takes_no_expression_name(self):
        """GitHub does not evaluate a skipped job's expression name, and most
        packages skip the floor job, so it takes no name: skipped it reads
        `engines-floor`, run `engines-floor (<i>)`."""
        self.assertNotIn('name', ts_ci()['jobs']['engines-floor'])

    def test_a_refused_count_skips_each_sharded_job(self):
        needs = counted(None, 'failure')
        for job in SHARDED:
            with self.subTest(job=job):
                self.assertFalse(wr.job_condition(ts_ci(), job, {'needs': needs}, []))

    def test_a_read_count_runs_each_sharded_job(self):
        for job in SHARDED:
            for count in (1, 3):
                with self.subTest(job=job, count=count):
                    self.assertTrue(wr.job_condition(ts_ci(), job, {'needs': counted(count)}, []))

    def test_no_floor_skips_every_floor_shard_and_not_validate(self):
        needs = counted(3, floor='')
        self.assertFalse(wr.job_condition(ts_ci(), 'engines-floor', {'needs': needs}, []))
        self.assertTrue(wr.job_condition(ts_ci(), 'validate', {'needs': needs}, []))


def running_steps(shard: int) -> list[str]:
    """The validate steps that run in a shard of a public pull request on a root
    web package, every earlier step having passed."""
    contexts = {
        'matrix': {'shard': shard},
        'needs': counted(2),
        'inputs': {'web-lint': True, 'working-directory': '.'},
        'github': {'event_name': 'pull_request', 'event': {'repository': {'private': False}}},
        'steps': {},
    }
    scope = wr.Scope(contexts, {}, [])
    return [s['name'] for s in ts_ci()['jobs']['validate']['steps'] if scope.condition(s.get('if'))]


class ShardSteps(unittest.TestCase):
    SETUP = ('Checkout', 'Setup Node.js', 'Install deps', 'Install browser (vitest browser mode)')

    def test_a_later_shard_runs_only_the_setup_its_tests_and_the_verdict(self):
        self.assertEqual(running_steps(2), [*self.SETUP, 'Test (vitest)', 'Check results'])

    def test_the_first_shard_runs_every_check_once(self):
        steps = running_steps(1)
        for name in (
            'Lint (eslint)',
            'Typecheck (tsc)',
            'Import cycles and undeclared dependencies (knip)',
            'Test (vitest)',
            'Fuzz (fast-check)',
            'Format (prettier)',
            'Version parity (package.json == jsr.json)',
            'Publish surface (registries ship exactly the public API)',
            'Import map coverage',
            'Lint web (CSS/HTML)',
            'Dependency review',
            'Check results',
        ):
            self.assertIn(name, steps)


class TestStep(Harness):
    """validate's test step, which records a failure for its soft gate."""

    JOB = 'validate'
    STEP = workflow_step(TS_CI, JOB, SHARDED[JOB])

    def setUp(self):
        super().setUp()
        bin_dir = self.pkg / 'node_modules' / '.bin'
        bin_dir.mkdir(parents=True)
        stub = bin_dir / 'vitest'
        stub.write_text(VITEST_STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    def run_test(self, scripts: dict[str, str], shard: int, shards: int):
        self.package(scripts)
        return self.run_step({'SHARD': str(shard), 'SHARDS': str(shards)})

    def test_the_step_takes_its_shard_and_the_read_count(self):
        self.assertEqual(
            self.STEP['env'],
            {'SHARD': '${{ matrix.shard }}', 'SHARDS': '${{ needs.package.outputs.count }}'},
        )

    def test_unsharded_runs_the_test_script_as_it_stands(self):
        guard = 'echo guard "$@" >> "$STUB_LOG/guard"'
        run = self.run_test({'test': f'vitest --run && {guard}'}, 1, 1)
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('npm'), 'test\n')
        self.assertEqual(run.logged('vitest'), '--run\n')
        self.assertEqual(run.logged('guard'), 'guard\n')

    def test_a_shard_hands_vitest_its_index_and_count(self):
        scripts = {'test': 'vitest --run', 'posttest': 'echo post "$@" >> "$STUB_LOG/post"'}
        run = self.run_test(scripts, 2, 3)
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('vitest'), '--run --shard=2/3\n')
        self.assertEqual(run.logged('post'), 'post\n')
        self.assertEqual(run.recorded, '')

    # What a refused shard leaves in the soft gate's failure file.
    REFUSED_RECORD = 'test\n'

    def test_a_compound_test_script_fails_the_shard_before_running(self):
        for script in (
            'vitest --run && node guard.ts',
            'vitest --run; true',
            'vitest | tee x',
            'vitest --run # x',
            'vitest --run\nnode guard.ts',
        ):
            with self.subTest(script=script):
                (self.tmp / 'log' / 'vitest').unlink(missing_ok=True)
                (self.tmp / 'ci_failures').unlink(missing_ok=True)
                run = self.run_test({'test': script}, 1, 2)
                self.assertEqual(run.rc, 1, run.out)
                self.assertEqual(run.recorded, self.REFUSED_RECORD)
                self.assertIn('sets cplieger.vitestShards to 2', run.out)
                self.assertIn('must be one vitest command', run.out)
                self.assertEqual(run.logged('vitest'), '')

    def test_a_test_script_without_vitest_fails_the_shard(self):
        run = self.run_test({'test': 'node --test'}, 1, 2)
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.recorded, self.REFUSED_RECORD)


class FloorTestStep(TestStep):
    """engines-floor's test step: the same shard hand-off, with no soft gate, so a
    refusal fails the step without recording it."""

    JOB = 'engines-floor'
    STEP = workflow_step(TS_CI, JOB, SHARDED[JOB])
    REFUSED_RECORD = ''


class FloorShardSteps(unittest.TestCase):
    def test_every_shard_runs_every_floor_step(self):
        """The job condition holds the floor gate, so each shard runs the setup,
        the install and its part of the suite, and no step tolerates a failure."""
        for step in ts_ci()['jobs']['engines-floor']['steps']:
            with self.subTest(step=step['name']):
                self.assertNotIn('if', step)
                self.assertNotIn('continue-on-error', step)

    def test_the_floor_job_runs_on_the_read_floor(self):
        setup = workflow_step(TS_CI, 'engines-floor', 'Setup Node.js')
        self.assertEqual(evaluate(setup['with']['node-version'], {'needs': counted(2, floor='22')}), '22')


class MetaPassesNoCount(unittest.TestCase):
    def test_no_ts_ci_call_names_a_count_or_a_repository(self):
        jobs = yaml.safe_load(META.read_text())['jobs']
        for name in ('ts', 'ts-nested', 'web'):
            with self.subTest(job=name):
                call = json.dumps(jobs[name])
                self.assertNotIn('vitest-shards', call)
                self.assertNotIn('repository.name', call)
        self.assertNotIn('vitest-shards', META.read_text())
        self.assertNotIn('repository.name', TS_CI.read_text())


if __name__ == '__main__':
    unittest.main()
