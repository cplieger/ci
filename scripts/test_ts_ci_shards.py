"""ts-ci.yaml's vitest-shards input: the shard matrix and check names, which steps
each shard runs, how the test step hands vitest its shard, the one validate
result over every shard, and the meta ci.yaml's per-repository shard count."""

from __future__ import annotations

import json
import stat
import unittest

import workflow_replay as wr
import yaml
from test_deadset_steps import META
from test_deadset_steps import Harness as DetectHarness
from test_package_script_steps import WORKFLOWS, Harness, workflow_step

TS_CI = WORKFLOWS / 'ts-ci.yaml'

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


def shard_list(count) -> list | None:
    matrix = ts_ci()['jobs']['validate']['strategy']['matrix']['shard']
    return evaluate(matrix, {'inputs': {'vitest-shards': count}})


def validate_name(shard: int, count: int) -> str:
    name = ts_ci()['jobs']['validate']['name']
    return evaluate(name, {'inputs': {'vitest-shards': count}, 'matrix': {'shard': shard}})


class ShardMatrix(unittest.TestCase):
    def test_the_input_defaults_to_one_shard(self):
        # PyYAML reads the bare `on` key as YAML 1.1's boolean true.
        spec = ts_ci()[True]['workflow_call']['inputs']['vitest-shards']
        self.assertEqual((spec['type'], spec['default']), ('number', 1))

    def test_a_count_from_one_to_eight_lists_every_shard_once(self):
        for count in range(1, 9):
            with self.subTest(count=count):
                self.assertEqual(shard_list(count), list(range(1, count + 1)))

    def test_a_count_outside_one_to_eight_lists_no_shard(self):
        for count in (0, 9):
            with self.subTest(count=count):
                self.assertFalse(shard_list(count))

    def test_one_failed_shard_does_not_cancel_the_others(self):
        self.assertFalse(ts_ci()['jobs']['validate']['strategy']['fail-fast'])

    def test_unsharded_the_job_keeps_the_plain_validate_name(self):
        self.assertEqual(validate_name(1, 1), 'validate')

    def test_each_shard_is_named_with_its_index_and_count(self):
        self.assertEqual(validate_name(1, 2), 'validate (shard 1/2)')
        self.assertEqual(validate_name(2, 2), 'validate (shard 2/2)')


def running_steps(shard: int) -> list[str]:
    """The validate steps that run in a shard of a public pull request on a root
    web package, every earlier step having passed."""
    contexts = {
        'matrix': {'shard': shard},
        'inputs': {'vitest-shards': 2, 'web-lint': True, 'working-directory': '.'},
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
    STEP = workflow_step(TS_CI, 'validate', 'Test (vitest)')

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
                self.assertEqual(run.recorded, 'test\n')
                self.assertIn('must be one vitest command', run.out)
                self.assertEqual(run.logged('vitest'), '')

    def test_a_test_script_without_vitest_fails_the_shard(self):
        run = self.run_test({'test': 'node --test'}, 1, 2)
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.recorded, 'test\n')


def shards_job() -> dict:
    return ts_ci()['jobs']['shards']


def shards_runs(count: int, validate: str) -> bool:
    contexts = {'inputs': {'vitest-shards': count}, 'needs': {'validate': {'result': validate}}}
    return wr.job_condition(ts_ci(), 'shards', contexts, [])


class ShardsVerdict(DetectHarness):
    def test_sharded_the_verdict_job_carries_the_validate_name(self):
        name = shards_job()['name']
        self.assertEqual(evaluate(name, {'inputs': {'vitest-shards': 2}}), 'validate')
        self.assertNotEqual(evaluate(name, {'inputs': {'vitest-shards': 1}}), 'validate')

    def test_it_runs_after_every_shard_whatever_their_result_only_when_sharded(self):
        self.assertEqual(shards_job()['needs'], 'validate')
        self.assertTrue(shards_runs(2, 'failure'))
        self.assertTrue(shards_runs(2, 'success'))
        self.assertFalse(shards_runs(1, 'success'))

    def verdict(self, result: str):
        step = shards_job()['steps'][0]
        self.assertEqual(step['env']['RESULT'], '${{ needs.validate.result }}')
        return self.run_body(step['run'], self.tmp, {'RESULT': result})

    def test_it_passes_only_when_every_shard_passed(self):
        self.assertEqual(self.verdict('success').rc, 0)
        for result in ('failure', 'cancelled'):
            with self.subTest(result=result):
                run = self.verdict(result)
                self.assertEqual(run.rc, 1, run.out)
                self.assertIn(f'a validate shard ended {result}', run.out)


def meta_jobs() -> dict:
    return yaml.safe_load(META.read_text())['jobs']


def web_shard_count(github: dict):
    return evaluate(meta_jobs()['web']['with']['vitest-shards'], {'github': github})


class MetaShardCount(unittest.TestCase):
    def test_a_listed_repository_runs_its_web_suite_in_shards(self):
        self.assertEqual(web_shard_count({'event': {'repository': {'name': 'marotte'}}}), 2)

    def test_any_other_repository_runs_one_shard(self):
        self.assertEqual(web_shard_count({'event': {'repository': {'name': 'subflux'}}}), 1)

    def test_an_event_without_a_repository_runs_one_shard(self):
        self.assertEqual(web_shard_count({'event': {}}), 1)

    def test_the_count_reaches_the_web_job_only(self):
        jobs = meta_jobs()
        self.assertNotIn('vitest-shards', json.dumps(jobs['ts']) + json.dumps(jobs['ts-nested']))


if __name__ == '__main__':
    unittest.main()
