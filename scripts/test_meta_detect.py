"""The meta ci.yaml's detect routing for nested modules: which TS halves get
ts-ci, when the modules are tested together under a generated go.work, how the
jobs they feed are wired into validate, and which dead-code targets a root-less
repository gets."""

from __future__ import annotations

import json
import os
import re
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


GIT_ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}


def git(repo: Path, *args: str) -> str:
    argv = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args]
    return subprocess.run(
        argv, cwd=repo, check=True, env=GIT_ENV, capture_output=True, text=True
    ).stdout.strip()


class PullRequestBase(Harness):
    """A pull request's diff starts at its merge ref's first parent, not at the
    event's base SHA, which GitHub leaves at the base branch's tip when the
    pull request opened."""

    def setUp(self):
        super().setUp()
        repo = git_repo(
            self.tmp / 'pr',
            {**module('alpha', 'example.com/r/alpha'), **module('beta', 'example.com/r/beta')},
        )
        self.repo = repo
        git(repo, 'checkout', '-qb', 'dev')
        git(repo, 'commit', '-qm', 'opened on')
        self.opened = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'checkout', '-qb', 'topic')
        (repo / 'alpha' / 'm.go').write_text('package m\n\nvar A = 1\n')
        git(repo, 'commit', '-qam', 'the pull request changes alpha')
        git(repo, 'checkout', '-q', 'dev')
        (repo / 'beta' / 'm.go').write_text('package m\n\nvar B = 1\n')
        git(repo, 'commit', '-qam', 'dev changes beta after the pull request opened')
        self.built_on = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'checkout', '-q', '--detach')
        git(repo, 'merge', '-q', '--no-ff', '-m', 'merge ref', 'topic')
        self.merge_ref = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'update-ref', 'refs/remotes/origin/dev', self.built_on)

    def detect(self, env: dict[str, str]) -> dict[str, str]:
        body = workflow_step(META, 'Detect repo surfaces', 'detect')['run']
        body = body.replace('${{ github.event_name }}', 'pull_request')
        body = body.replace('${{ github.event.pull_request.base.sha }}', self.opened)
        body = re.sub(r'\$\{\{[^}]*\}\}', '', body)
        run = self.run_body(body, self.repo, env)
        self.assertEqual(run.rc, 0, run.out)
        return run.outputs

    def test_a_base_that_moved_after_the_pull_request_opened_adds_no_module(self):
        out = self.detect({'GITHUB_BASE_REF': 'dev'})
        self.assertEqual(json.loads(out['go_nested_dirs']), ['alpha'])
        self.assertEqual(out['pr_base'], self.built_on)

    def test_the_merge_ref_alone_names_the_base(self):
        git(self.repo, 'update-ref', '-d', 'refs/remotes/origin/dev')
        out = self.detect({})
        self.assertEqual(json.loads(out['go_nested_dirs']), ['alpha'])
        self.assertEqual(out['pr_base'], self.built_on)

    def test_a_base_that_took_in_head_commits_still_diffs_the_whole_head(self):
        # A stacked pull request merged into the base first: the base now holds
        # the head's commit, and a merge-base with it would drop alpha.
        git(
            self.repo, 'update-ref', 'refs/remotes/origin/dev', git(self.repo, 'rev-parse', 'topic')
        )
        out = self.detect({'GITHUB_BASE_REF': 'dev'})
        self.assertEqual(json.loads(out['go_nested_dirs']), ['alpha'])
        self.assertEqual(out['pr_base'], self.built_on)

    def test_a_base_that_moved_after_the_merge_ref_was_built_changes_nothing(self):
        git(self.repo, 'checkout', '-q', 'dev')
        (self.repo / 'beta' / 'm.go').write_text('package m\n\nvar B = 2\n')
        git(self.repo, 'commit', '-qam', 'dev moves again')
        git(self.repo, 'update-ref', 'refs/remotes/origin/dev', 'HEAD')
        git(self.repo, 'checkout', '-q', self.merge_ref)
        out = self.detect({'GITHUB_BASE_REF': 'dev'})
        self.assertEqual(json.loads(out['go_nested_dirs']), ['alpha'])
        self.assertEqual(out['pr_base'], self.built_on)

    def test_a_checkout_that_is_no_merge_falls_back_to_the_event_base(self):
        git(self.repo, 'checkout', '-q', 'topic')
        out = self.detect({'GITHUB_BASE_REF': 'dev'})
        self.assertEqual(json.loads(out['go_nested_dirs']), ['alpha'])
        self.assertEqual(out['pr_base'], self.opened)

    def test_the_main_intake_reads_pr_base(self):
        jobs = workflow()['jobs']
        self.assertEqual(
            jobs['detect']['outputs']['pr_base'], '${{ steps.detect.outputs.pr_base }}'
        )
        self.assertEqual(jobs['pr-policy']['needs'], 'detect')
        intake = next(s for s in jobs['pr-policy']['steps'] if s.get('id') == 'intake')
        self.assertEqual(intake['with']['base-sha'], '${{ needs.detect.outputs.pr_base }}')


# CI must run these, so a runner without Go fails them instead of skipping.
@unittest.skipUnless(shutil.which('go') or os.environ.get('CI'), 'go is not installed')
class GoWorkStep(Harness):
    def setUp(self):
        self.assertTrue(shutil.which('go'), 'go is not on PATH')
        super().setUp()

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
    """With no root go.mod, deadset-go analyzes every module from the root in one run."""

    def resolve_root(self, files: dict[str, str]):
        repo = git_repo(self.tmp / f'resolve-{len(list(self.tmp.glob("resolve-*")))}', files)
        return self.run_step('Resolve target', repo, {'TARGET': '.'})

    def test_go_modules_alone_have_one_target_and_keep_their_go_jobs(self):
        out = run_detect(self, GO_MODULES_ONLY)
        self.assertEqual(json.loads(out['deadset_targets']), ['.'])
        self.assertEqual(json.loads(out['go_nested_dirs']), ['alpha', 'beta'])

    def test_the_root_of_module_halves_is_the_one_target_and_names_no_language(self):
        out = run_detect(self, ROOTLESS)
        self.assertEqual(json.loads(out['deadset_targets']), ['.'])
        self.assertEqual(json.loads(out['go_nested_dirs']), ['alpha', 'beta', 'gamma'])
        run = self.resolve_root(ROOTLESS)
        self.assertEqual(run.rc, 0, run.out)
        central = json.loads(Path(run.outputs['work'], 'central.json').read_text())
        self.assertEqual(central, {'target': {'kind': 'library'}})
        self.assertEqual(run.outputs['go_mod'], 'false')

    def test_a_module_under_static_or_dist_adds_no_target(self):
        for d in ('dist/x', 'static/x', 'alpha/dist/x'):
            with self.subTest(d=d):
                files = {**GO_MODULES_ONLY, **module(d, f'example.com/r/{d}')}
                self.assertEqual(json.loads(run_detect(self, files)['deadset_targets']), ['.'])


if __name__ == '__main__':
    unittest.main()
