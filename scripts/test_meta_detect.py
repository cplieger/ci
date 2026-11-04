"""The meta ci.yaml's detect routing for nested modules: which TS halves get
ts-ci, and how the jobs they feed are wired into validate."""

from __future__ import annotations

import json
import unittest

import workflow_replay as wr
import yaml
from test_deadset_steps import META, Harness, run_detect

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


if __name__ == '__main__':
    unittest.main()
