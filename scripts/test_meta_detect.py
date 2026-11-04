"""The meta ci.yaml's detect routing for nested modules: which TS halves get
ts-ci, when the modules are tested together under a generated go.work, how the
jobs they feed are wired into validate, and which dead-code targets a root-less
repository gets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import workflow_replay as wr
import yaml
from test_deadset_steps import GO_CI, META, Harness, git_repo, run_detect, workflow_step

GO_FILE = 'package m\n'
SENTINEL = 'module web-ignore\n'
HALF = {'package.json': '{}', 'package-lock.json': '{}', 'jsr.json': '{}', 'mod.ts': ''}


def module(d: str, path: str, extra: str = '') -> dict[str, str]:
    return {f'{d}/go.mod': f'module {path}\n\ngo 1.26\n{extra}', f'{d}/m.go': GO_FILE}


def half(d: str) -> dict[str, str]:
    return {f'{d}/web/go.mod': SENTINEL, **{f'{d}/web/{k}': v for k, v in HALF.items()}}


ROOTLESS = {
    'README.md': 'x\n',
    **module('alpha', 'example.com/r/alpha'),
    **half('alpha'),
    **module('beta', 'example.com/r/beta'),
    **module('gamma', 'example.com/r/gamma'),
    **half('gamma'),
}


def workflow() -> dict:
    return yaml.safe_load(META.read_text())


def job_runs(name: str, outputs: dict[str, str]) -> bool:
    contexts = {'needs': {'detect': {'result': 'success', 'outputs': outputs}}}
    return wr.job_condition(workflow(), name, contexts, [])


class TsNested(Harness):
    def test_every_module_half_is_listed_and_none_is_a_go_module(self):
        out = run_detect(self, ROOTLESS)
        self.assertEqual(json.loads(out['ts_nested_dirs']), ['alpha/web', 'gamma/web'])
        self.assertEqual(out['run_ts_nested'], 'true')
        self.assertEqual(json.loads(out['go_nested_dirs']), ['alpha', 'beta', 'gamma'])
        self.assertEqual(out['run_web'], 'false')
        self.assertTrue(job_runs('ts-nested', out))

    def test_a_root_web_package_stays_the_web_job(self):
        files = {
            'go.mod': 'module example.com/r\n',
            'main.go': 'package main\n',
            'web/go.mod': SENTINEL,
            **{f'web/{k}': v for k, v in HALF.items()},
        }
        out = run_detect(self, files)
        self.assertEqual(out['ts_nested_dirs'], '[]')
        self.assertEqual(out['run_ts_nested'], 'false')
        self.assertEqual((out['run_web'], out['web_dir']), ('true', 'web'))
        self.assertFalse(job_runs('ts-nested', out))

    def test_a_web_package_beside_no_go_module_is_not_a_half(self):
        files = {
            'go.mod': 'module example.com/r\n',
            'main.go': 'package main\n',
            **{f'tools/web/{k}': v for k, v in HALF.items()},
            'docs/go.mod': 'module example.com/r/docs\n',
            **{f'docs/web/{k}': v for k, v in HALF.items()},
        }
        out = run_detect(self, files)
        self.assertEqual(out['ts_nested_dirs'], '[]')

    def test_a_half_runs_ts_ci_at_its_own_directory_as_a_library(self):
        job = workflow()['jobs']['ts-nested']
        self.assertEqual(job['uses'], './.github/workflows/ts-ci.yaml')
        self.assertEqual(job['needs'], 'detect')
        self.assertEqual(
            job['strategy']['matrix']['dir'], '${{ fromJSON(needs.detect.outputs.ts_nested_dirs) }}'
        )
        self.assertFalse(job['strategy']['fail-fast'])
        self.assertEqual(job['with'], {'working-directory': '${{ matrix.dir }}'})

    def test_validate_fails_on_a_failed_half(self):
        validate = workflow()['jobs']['validate']
        self.assertIn('ts-nested', validate['needs'])
        step = validate['steps'][0]
        self.assertEqual(step['env']['TS_NESTED'], '${{ needs.ts-nested.result }}')
        self.assertIn('"ts-nested=$TS_NESTED"', step['run'])


ALPHA = 'example.com/r/alpha'
REQUIRES_ALPHA = f'\nrequire {ALPHA} v1.9.9\n'


class GoWorkDetect(Harness):
    def test_a_sibling_required_by_version_tests_every_module_together(self):
        files = {**ROOTLESS, **module('beta', 'example.com/r/beta', REQUIRES_ALPHA)}
        out = run_detect(self, files)
        self.assertEqual(out['run_go_work'], 'true')
        self.assertEqual(json.loads(out['go_work_dirs']), ['alpha', 'beta', 'gamma'])
        self.assertTrue(job_runs('go-work', out))

    def test_a_require_block_and_a_root_module_count(self):
        block = '\nrequire (\n\t"example.com/x/other" v1.0.0 // indirect\n\texample.com/r/sub v0.1.0\n)\n'
        files = {
            'go.mod': f'module example.com/r\n\ngo 1.26\n{block}',
            'main.go': 'package main\n',
            **module('sub', 'example.com/r/sub'),
        }
        out = run_detect(self, files)
        self.assertEqual(out['run_go_work'], 'true')
        self.assertEqual(json.loads(out['go_work_dirs']), ['.', 'sub'])

    def test_no_sibling_require_or_a_replaced_one_runs_no_workspace_job(self):
        replaced = 'require example.com/r/sub v0.0.0\n\nreplace example.com/r/sub => ./sub\n'
        cases = {
            'no sibling require': ROOTLESS,
            'a replaced sibling': {
                'go.mod': f'module example.com/r\n\ngo 1.26\n\n{replaced}',
                'main.go': 'package main\n',
                **module('sub', 'example.com/r/sub'),
            },
            'a replace block': {
                **ROOTLESS,
                **module(
                    'beta',
                    'example.com/r/beta',
                    f'{REQUIRES_ALPHA}\nreplace (\n\t{ALPHA} => ../alpha\n)\n',
                ),
            },
            'a require of a module outside the repo': {
                **ROOTLESS,
                **module('beta', 'example.com/r/beta', '\nrequire example.com/x/alpha v1.0.0\n'),
            },
            'a sentinel go.mod requiring a sibling': {
                **ROOTLESS,
                'alpha/web/go.mod': f'module web-ignore\n{REQUIRES_ALPHA}',
            },
        }
        for name, files in cases.items():
            with self.subTest(case=name):
                out = run_detect(self, files)
                self.assertEqual(out['run_go_work'], 'false')
                self.assertEqual(out['go_work_dirs'], '[]')
                self.assertFalse(job_runs('go-work', out))


def module_tree(alpha_api: str) -> dict[str, str]:
    alpha = f'module {ALPHA}\n\ngo 1.22\n'
    beta = f'module example.com/r/beta\n\ngo 1.22\n{REQUIRES_ALPHA}'
    call = 'package beta\n\nimport "example.com/r/alpha"\n\nfunc B() string { return alpha.Hi() }\n'
    test = 'package beta\n\nimport "testing"\n\nfunc TestB(t *testing.T) {\n\tif B() != "hi" {\n\t\tt.Fatal(B())\n\t}\n}\n'
    return {
        'alpha/go.mod': alpha,
        'alpha/a.go': f'package alpha\n\nfunc {alpha_api}() string {{ return "hi" }}\n',
        'beta/go.mod': beta,
        'beta/b.go': call,
        'beta/b_test.go': test,
    }


@unittest.skipUnless(shutil.which('go'), 'go is not installed')
class GoWorkStep(Harness):
    def run_go_work(self, files: dict[str, str]) -> subprocess.CompletedProcess:
        repo = git_repo(self.tmp / f'work-{len(list(self.tmp.glob("work-*")))}', files)
        step = workflow_step(META, 'Build and test every module under one go.work', 'go-work')
        env = {k: v for k, v in os.environ.items() if k not in ('GOWORK', 'GOFLAGS')}
        env |= {
            'DIRS': '["alpha","beta"]',
            'GOTOOLCHAIN': 'local',
            'GOPROXY': 'off',
            'GIT_CONFIG_GLOBAL': '/dev/null',
        }
        return subprocess.run(
            ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', step['run']],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )

    def test_a_require_of_an_untagged_version_builds_against_the_sibling_head(self):
        proc = self.run_go_work(module_tree('Hi'))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn('::error::', proc.stdout)

    def test_a_sibling_change_that_breaks_a_consumer_fails_naming_the_consumer(self):
        proc = self.run_go_work(module_tree('Hello'))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('::error::beta ', proc.stdout)
        self.assertNotIn('::error::alpha ', proc.stdout)


class GoWorkWiring(unittest.TestCase):
    def test_each_module_job_builds_as_its_consumers_do(self):
        self.assertEqual(
            yaml.safe_load(GO_CI.read_text())['jobs']['validate']['env'], {'GOWORK': 'off'}
        )

    def test_the_workspace_job_lets_the_toolchain_follow_go_work(self):
        job = workflow()['jobs']['go-work']
        self.assertEqual(job['needs'], 'detect')
        setup = next(s for s in job['steps'] if s.get('name') == 'Setup Go')
        self.assertEqual(
            setup['with']['go-version-file'],
            '${{ fromJSON(needs.detect.outputs.go_work_dirs)[0] }}/go.mod',
        )
        step = workflow_step(META, 'Build and test every module under one go.work', 'go-work')
        self.assertEqual(step['env']['GOTOOLCHAIN'], 'auto')
        self.assertEqual(step['env']['DIRS'], '${{ needs.detect.outputs.go_work_dirs }}')
        self.assertNotIn('GOWORK', json.dumps(job))

    def test_validate_fails_on_a_failed_workspace_job(self):
        validate = workflow()['jobs']['validate']
        self.assertIn('go-work', validate['needs'])
        step = validate['steps'][0]
        self.assertEqual(step['env']['GO_WORK'], '${{ needs.go-work.result }}')
        self.assertIn('"go-work=$GO_WORK"', step['run'])


GO_MODULES_ONLY = {
    'README.md': 'x\n',
    **module('alpha', 'example.com/r/alpha'),
    **module('beta', 'example.com/r/beta'),
}


class RootLessDeadset(Harness):
    def resolve_root(self, files: dict[str, str]):
        repo = git_repo(self.tmp / f'resolve-{len(list(self.tmp.glob("resolve-*")))}', files)
        return self.run_step('Resolve target', repo, {'TARGET': '.'})

    def test_go_modules_alone_are_each_a_target_and_the_root_is_none(self):
        out = run_detect(self, GO_MODULES_ONLY)
        self.assertEqual(json.loads(out['deadset_targets']), ['alpha', 'beta'])

    def test_the_root_of_module_halves_analyzes_only_their_typescript(self):
        out = run_detect(self, ROOTLESS)
        self.assertEqual(json.loads(out['deadset_targets']), ['.', 'alpha', 'beta', 'gamma'])
        run = self.resolve_root(ROOTLESS)
        self.assertEqual(run.rc, 0, run.out)
        got = run.outputs
        self.assertEqual(
            (got['languages'], got['languages_from'], got['go'], got['ts']),
            ('ts', 'central: Go only in nested modules', 'false', 'true'),
        )
        central = json.loads(Path(got['work'], 'central.json').read_text())
        self.assertEqual(central['analysis'], {'languages': ['ts']})
        self.assertEqual(got['npm_dirs'], 'alpha/web\ngamma/web')

    def test_go_outside_every_module_still_fails_closed(self):
        files = {**ROOTLESS, 'tools/x.go': 'package main\n'}
        self.assertIn('.', json.loads(run_detect(self, files)['deadset_targets']))
        run = self.resolve_root(files)
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error::Go is in scope at . (detected), but . has no go.mod', run.out)

    def test_a_repository_file_still_decides(self):
        files = {**GO_MODULES_ONLY, 'deadset.json': '{"analysis":{"languages":["go"]}}'}
        self.assertIn('.', json.loads(run_detect(self, files)['deadset_targets']))
        run = self.resolve_root(files)
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('Go is in scope at . (repository: deadset.json)', run.out)


if __name__ == '__main__':
    unittest.main()
