"""ts-ci.yaml's opt-in Go test server: the package job reads and validates the
package's `sseFixture` and `sseFixtureTags`, and each job that runs the tests
sets up Go, builds the server and hands its path to them as SSE_FIXTURE."""

from __future__ import annotations

import json
import unittest

import workflow_replay as wr
from test_package_script_steps import WORKFLOWS, Harness, workflow_step, write_exec
from test_ts_ci_shards import INSTALL_STEP, SHARDED, TS_CI, counted, evaluate, ts_ci

READ_STEP = 'Read sseFixture and sseFixtureTags'
SETUP_STEP = 'Setup Go (sseFixture)'
BUILD_STEP = 'Build the sseFixture test server'

# Logs where it ran and its arguments, and writes the -o file unless
# $STUB_GO=fail, when it fails as a compile error would.
GO_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$PWD" "$*" >> "$STUB_LOG/go"
if [ "${STUB_GO:-ok}" != ok ]; then
  echo './main.go:1:1: expected package, found x'
  exit 1
fi
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then : > "$2"; fi
  shift
done
"""


def needs(fixture: str = '', tags: str = '') -> dict:
    """The package job's needs context with the fixture outputs the read wrote."""
    context = counted(2)
    context['package']['outputs'] |= {'sse-fixture': fixture, 'sse-fixture-tags': tags}
    return context


class ReadFixture(Harness):
    """The read, run with the package at <checkout>/web as `ts` or `web` sees it."""

    def setUp(self):
        super().setUp()
        self.STEP = workflow_step(TS_CI, 'package', READ_STEP)
        self.web = self.workspace / 'web'
        self.web.mkdir()
        (self.workspace / 'go.mod').write_text('module example.com/root\n')
        self.outside = self.tmp / 'outside'
        self.outside.mkdir()
        (self.outside / 'go.mod').write_text('module example.com/outside\n')

    def read(self, manifest: object = None, *, raw: str | None = None):
        text = raw if raw is not None else json.dumps(manifest)
        (self.web / 'package.json').write_text(text)
        return self.run_step(
            {'READ_FIXTURE': self.STEP['env']['READ_FIXTURE'], 'PACKAGE': 'web/package.json'}
        )

    def fixture(self, directory: object, tags: object = 'marotte_test'):
        return self.read({'name': 'x', 'sseFixture': directory, 'sseFixtureTags': tags})

    def assert_built(self, run, directory: str, tags: str = 'marotte_test'):
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.outputs, {'dir': directory, 'tags': tags})
        self.assertNotIn('::', run.out)

    def assert_refused(self, run, message: str):
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn(message, run.out)
        self.assertEqual(run.outputs, {})
        lines = [line for line in run.out.splitlines() if line.startswith('::')]
        self.assertEqual(lines, [run.out.strip()])
        self.assertTrue(lines[0].startswith('::error::web/package.json: '), lines[0])

    def test_the_step_reads_the_working_directory_package(self):
        self.assertEqual(
            self.STEP['env']['PACKAGE'], '${{ inputs.working-directory }}/package.json'
        )
        self.assertEqual(self.STEP['run'], 'node -e "$READ_FIXTURE"')

    def test_a_package_without_the_field_builds_nothing(self):
        for manifest in ({'name': 'x'}, {'cplieger': {'vitestShards': 2}}, [], 'x', None):
            with self.subTest(manifest=manifest):
                run = self.read(manifest)
                self.assertEqual(run.rc, 0, run.out)
                self.assertEqual(run.outputs, {'dir': '', 'tags': ''})
                self.assertIn('no sseFixture', run.out)

    def test_the_module_directory_is_read_against_the_package_directory(self):
        (self.workspace / 'server').mkdir()
        (self.workspace / 'server' / 'go.mod').write_text('module example.com/server\n')
        (self.web / 'cmd').mkdir()
        (self.web / 'cmd' / 'go.mod').write_text('module example.com/cmd\n')
        for directory, want in (
            ('..', '.'),
            ('../', '.'),
            ('../server', 'server'),
            ('./cmd', 'web/cmd'),
            ('cmd', 'web/cmd'),
            ('../web/../server', 'server'),
        ):
            with self.subTest(directory=directory):
                self.assert_built(self.fixture(directory), want)

    def test_a_symlink_that_stays_in_the_checkout_reads_as_its_target(self):
        (self.workspace / 'server').mkdir()
        (self.workspace / 'server' / 'go.mod').write_text('module example.com/server\n')
        (self.web / 'link').symlink_to('../server')
        self.assert_built(self.fixture('link'), 'server')

    def test_tags_are_optional_and_any_comma_list_of_tag_names_is_passed_as_written(self):
        self.assert_built(self.read({'sseFixture': '..'}), '.', '')
        for tags in ('marotte_test', 'a,b', 'A1_b,c2', '_'):
            with self.subTest(tags=tags):
                self.assert_built(self.fixture('..', tags), '.', tags)

    def test_a_directory_outside_the_checkout_fails_the_job(self):
        for directory in ('../..', '../../outside', '../../workspace-other'):
            with self.subTest(directory=directory):
                self.assert_refused(self.fixture(directory), 'outside the checkout')

    def test_an_absolute_directory_fails_the_job(self):
        for directory in ('/', str(self.workspace), str(self.outside)):
            with self.subTest(directory=directory):
                self.assert_refused(self.fixture(directory), 'a path relative to package.json')

    def test_a_symlink_out_of_the_checkout_fails_the_job(self):
        (self.web / 'link').symlink_to(self.outside)
        (self.workspace / 'up').symlink_to('..')
        (self.workspace / 'mod').mkdir()
        (self.workspace / 'mod' / 'go.mod').symlink_to(self.outside / 'go.mod')
        for directory in ('link', '../up/outside', 'link/'):
            with self.subTest(directory=directory):
                self.assert_refused(self.fixture(directory), 'a symlink takes it outside')
        self.assert_refused(self.fixture('../mod'), 'its go.mod is a symlink outside')

    def test_a_directory_without_a_go_mod_fails_the_job(self):
        (self.workspace / 'empty').mkdir()
        (self.workspace / 'dirmod' / 'go.mod').mkdir(parents=True)
        for directory in ('.', '../empty', '../dirmod'):
            with self.subTest(directory=directory):
                self.assert_refused(self.fixture(directory), 'holds no go.mod')

    def test_a_directory_that_is_missing_or_not_a_directory_fails_the_job(self):
        (self.web / 'loop').symlink_to('loop')
        for directory, message in (
            ('../missing', 'no such directory'),
            ('package.json', 'not a directory'),
            ('loop', 'cannot resolve it'),
        ):
            with self.subTest(directory=directory):
                self.assert_refused(self.fixture(directory), message)

    def test_a_directory_that_is_not_a_non_empty_string_fails_the_job(self):
        for directory in ('', 1, None, True, ['..'], {'dir': '..'}):
            with self.subTest(directory=directory):
                self.assert_refused(self.fixture(directory), 'a path relative to package.json')

    def test_bad_tags_fail_the_job(self):
        for tags in (
            '',
            'a b',
            'a;b',
            '-x',
            'a.b',
            '$(id)',
            'a\n::add-mask::x',
            7,
            None,
            ['marotte_test'],
            'x' * 500 + ';',
        ):
            with self.subTest(tags=tags):
                run = self.fixture('..', tags)
                self.assert_refused(run, 'it must be Go build tags')
                self.assertLess(len(run.out), 250, run.out)

    def test_tags_without_a_directory_fail_the_job(self):
        self.assert_refused(
            self.read({'sseFixtureTags': 'marotte_test'}),
            'sseFixtureTags is set without sseFixture',
        )

    def test_a_directory_with_a_control_character_fails_on_one_line(self):
        weird = self.workspace / 'a\nb'
        weird.mkdir()
        (weird / 'go.mod').write_text('module example.com/weird\n')
        (self.web / 'link').symlink_to(weird)
        for directory in ('../a\nb', '../a\n::add-mask::x', 'link'):
            with self.subTest(directory=directory):
                self.assert_refused(self.fixture(directory), 'control character')

    def test_an_unreadable_package_fails_the_job(self):
        self.assert_refused(self.read(raw='{"sseFixture":'), 'cannot read the package settings')


class BuildFixture(Harness):
    def setUp(self):
        super().setUp()
        self.STEP = workflow_step(TS_CI, 'validate', BUILD_STEP)
        write_exec(self.bin / 'go', GO_STUB)
        (self.workspace / 'server').mkdir()

    def build(self, tags: str = 'marotte_test', **stub: str):
        env = {
            'FIXTURE_DIR': 'server',
            'FIXTURE_TAGS': tags,
            'GITHUB_ENV': str(self.tmp / 'github_env'),
            **stub,
        }
        return self.run_step(env)

    def exported(self) -> str:
        path = self.tmp / 'github_env'
        return path.read_text() if path.exists() else ''

    def test_the_step_takes_both_values_from_the_package_job_through_env(self):
        self.assertEqual(
            self.STEP['env'],
            {
                'FIXTURE_DIR': '${{ needs.package.outputs.sse-fixture }}',
                'FIXTURE_TAGS': '${{ needs.package.outputs.sse-fixture-tags }}',
            },
        )
        self.assertNotIn('${{', self.STEP['run'])

    def test_the_server_builds_in_the_module_directory_and_its_path_reaches_the_tests(self):
        run = self.build()
        self.assertEqual(run.rc, 0, run.out)
        binary = self.tmp / 'runner' / 'sse-fixture'
        self.assertEqual(
            run.logged('go'),
            f'{self.workspace / "server"}\nbuild -tags marotte_test -o {binary} .\n',
        )
        self.assertTrue(binary.exists())
        self.assertEqual(self.exported(), f'SSE_FIXTURE={binary}\n')

    def test_no_tags_builds_with_an_empty_tag_list(self):
        run = self.build(tags='')
        self.assertEqual(run.rc, 0, run.out)
        self.assertIn('build -tags  -o ', run.logged('go'))

    def test_a_failed_build_fails_the_step_naming_the_field_and_exports_nothing(self):
        run = self.build(STUB_GO='fail')
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('expected package', run.out)
        self.assertIn('::error::the sseFixture test server in server failed to build', run.out)
        self.assertEqual(self.exported(), '')


class FixtureSteps(unittest.TestCase):
    def test_the_package_job_reads_after_the_checkout_and_passes_on_both_values(self):
        job = ts_ci()['jobs']['package']
        names = [s.get('name') for s in job['steps']]
        self.assertLess(names.index('Checkout'), names.index(READ_STEP))
        step = workflow_step(TS_CI, 'package', READ_STEP)
        steps = {step['id']: {'outputs': {'dir': 'server', 'tags': 'a,b'}}}
        self.assertEqual(evaluate(job['outputs']['sse-fixture'], {'steps': steps}), 'server')
        self.assertEqual(evaluate(job['outputs']['sse-fixture-tags'], {'steps': steps}), 'a,b')

    def test_each_test_job_builds_the_same_way_after_its_setup_and_before_its_checks(self):
        for job in SHARDED:
            with self.subTest(job=job):
                steps = ts_ci()['jobs'][job]['steps']
                names = [s.get('name') for s in steps]
                at = names.index(INSTALL_STEP)
                self.assertEqual(names[at + 1 : at + 3], [SETUP_STEP, BUILD_STEP])
                self.assertFalse(
                    [s for s in steps[: at + 3] if s.get('continue-on-error')], 'a setup step'
                )
                self.assertLess(at + 2, names.index(SHARDED[job]))
                for name in (SETUP_STEP, BUILD_STEP):
                    self.assertEqual(
                        workflow_step(TS_CI, job, name), workflow_step(TS_CI, 'validate', name)
                    )

    def test_only_a_package_that_names_a_directory_sets_up_go_and_builds(self):
        for name in (SETUP_STEP, BUILD_STEP):
            step = workflow_step(TS_CI, 'validate', name)
            for fixture, want in (('', False), ('.', True), ('0', True), ('server', True)):
                with self.subTest(step=name, fixture=fixture):
                    scope = wr.Scope({'needs': needs(fixture)}, {}, [])
                    self.assertIs(scope.condition(step['if']), want)
            with self.subTest(step=name, fixture='unset'):
                self.assertFalse(wr.Scope({'needs': counted(2)}, {}, []).condition(step['if']))

    def test_go_is_set_up_from_the_module_with_its_own_module_cache(self):
        step = workflow_step(TS_CI, 'validate', SETUP_STEP)
        go_ci = workflow_step(WORKFLOWS / 'go-ci.yaml', 'validate', 'Setup Go')
        self.assertEqual(step['uses'], go_ci['uses'])
        scope = wr.Scope({'needs': needs('server')}, {}, [])
        self.assertEqual(
            {k: scope.render(v) if isinstance(v, str) else v for k, v in step['with'].items()},
            {
                'go-version-file': 'server/go.mod',
                'cache': True,
                'cache-dependency-path': 'server/go.sum',
            },
        )

    def test_an_enrolled_package_builds_in_every_shard(self):
        contexts = {
            'matrix': {'shard': 2},
            'needs': needs('.', 'marotte_test'),
            'inputs': {'web-lint': True, 'working-directory': 'static-src'},
            'github': {'event_name': 'pull_request', 'event': {'repository': {'private': False}}},
            'steps': {},
        }
        scope = wr.Scope(contexts, {}, [])
        running = [
            s['name'] for s in ts_ci()['jobs']['validate']['steps'] if scope.condition(s.get('if'))
        ]
        self.assertEqual(
            running,
            [
                'Checkout',
                'Setup Node.js',
                'Install deps',
                INSTALL_STEP,
                SETUP_STEP,
                BUILD_STEP,
                'Test (vitest)',
                'Check results',
            ],
        )


if __name__ == '__main__':
    unittest.main()
