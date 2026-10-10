"""Tests for promote.py: the promotion checks, the reconciliation commit and the job boundaries."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest import mock

import promote
import workflow_replay
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIGEST = 'sha256:' + 'a' * 64
GIT_ENV = {
    'GIT_AUTHOR_NAME': 't',
    'GIT_AUTHOR_EMAIL': 't@example.invalid',
    'GIT_COMMITTER_NAME': 't',
    'GIT_COMMITTER_EMAIL': 't@example.invalid',
    'GIT_CONFIG_GLOBAL': '/dev/null',
    'GIT_CONFIG_NOSYSTEM': '1',
}
GO_MOD = 'module github.com/cplieger/demo\n\ngo 1.27\n'


class Fixture:
    """A source repository with `main` and `dev`, published as a bare origin that
    promote.CLONE_URL points at."""

    def __init__(self, tmp: str, name: str = 'demo'):
        self.tmp = Path(tmp)
        self.name = name
        self.src = self.tmp / 'src'
        self.origin = self.tmp / 'origin' / f'{name}.git'
        self.work = self.tmp / 'work'
        self.work.mkdir()
        self.g('init', '-q', '-b', 'main', str(self.src), cwd=self.tmp)

    def g(self, *args, cwd=None) -> str:
        return subprocess.run(
            ['git', *args],
            cwd=cwd or self.src,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ENV},
        ).stdout.strip()

    def commit(self, message: str, files: dict[str, str] | None = None, *, empty=False) -> str:
        for path, text in (files or {}).items():
            target = self.src / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            self.g('add', path)
        self.g('commit', '-q', *(['--allow-empty'] if empty else []), '-m', message)
        return self.g('rev-parse', 'HEAD')

    def publish(self) -> None:
        shutil.rmtree(self.origin, ignore_errors=True)
        self.origin.parent.mkdir(parents=True, exist_ok=True)
        self.g('clone', '-q', '--bare', str(self.src), str(self.origin), cwd=self.tmp)

    def url(self) -> str:
        return str(self.tmp / 'origin' / '{repo}.git')


def go_repo(fx: Fixture) -> tuple[str, str]:
    """main at v1.0.0 and dev one feature past it: (M, T)."""
    m = fx.commit('feat: init', {'go.mod': GO_MOD, 'main.go': 'package main\n'})
    fx.g('tag', 'v1.0.0')
    fx.g('checkout', '-q', '-b', 'dev')
    t = fx.commit('feat: add greeting', {'main.go': 'package main\n\n// hi\n'})
    return m, t


def fake_classify(patterns=('.editorconfig',), sources=None):
    return types.SimpleNamespace(
        sync_owned_patterns=lambda: list(patterns),
        canonical_sources=lambda repo: dict(sources or {}),
        ClassifyError=RuntimeError,
    )


class Purity(unittest.TestCase):
    def test_go_mod_single_line_and_block(self):
        text = (
            'module github.com/cplieger/subflux\n\n'
            'require github.com/cplieger/httpx v5.0.4-dev.1\n'
            'require (\n'
            '\tgithub.com/cplieger/envx v2.1.0\n'
            '\tgithub.com/cplieger/health v1.4.0-dev.2 // indirect\n'
            '\tgolang.org/x/text v0.38.0-dev.9\n'
            ')\n'
        )
        got = promote.purity_violations({'go.mod': text})
        self.assertEqual(
            got,
            [
                'go.mod: github.com/cplieger/httpx v5.0.4-dev.1',
                'go.mod: github.com/cplieger/health v1.4.0-dev.2',
            ],
        )

    def test_go_mod_stable_pins_are_pure(self):
        text = 'module x\nrequire (\n\tgithub.com/cplieger/envx v2.1.0\n)\n'
        self.assertEqual(promote.purity_violations({'go.mod': text}), [])

    def test_go_mod_replace_to_a_dev_version_single_line_and_block(self):
        text = (
            'module x\n'
            'require github.com/cplieger/httpx v5.0.4\n'
            'replace github.com/cplieger/httpx => github.com/cplieger/httpx v5.0.5-dev.1\n'
            'replace (\n'
            '\tgithub.com/cplieger/envx v2.1.0 => github.com/cplieger/envx v2.2.0-dev.3\n'
            '\tgithub.com/cplieger/health => ../health\n'
            '\tgolang.org/x/text => golang.org/x/text v0.38.0-dev.9\n'
            ')\n'
        )
        got = promote.purity_violations({'go.mod': text})
        self.assertEqual(
            got,
            [
                'go.mod: replace github.com/cplieger/httpx => github.com/cplieger/httpx v5.0.5-dev.1',
                'go.mod: replace github.com/cplieger/envx => github.com/cplieger/envx v2.2.0-dev.3',
            ],
        )

    def test_go_mod_replace_to_a_stable_version_is_pure(self):
        text = 'module x\nreplace github.com/cplieger/httpx => github.com/cplieger/httpx v5.0.5\n'
        self.assertEqual(promote.purity_violations({'go.mod': text}), [])

    def test_package_json_alias_spec_naming_a_first_party_dev_version(self):
        text = (
            '{"dependencies": {"httpx-alias": "npm:@cplieger/httpx@5.0.5-dev.1",'
            ' "zod-alias": "npm:zod@3.0.0-dev.1"}}'
        )
        got = promote.purity_violations({'package.json': text})
        self.assertEqual(
            got, ['package.json: dependencies httpx-alias npm:@cplieger/httpx@5.0.5-dev.1']
        )

    def test_package_json_shipping_sections_only(self):
        text = (
            '{"dependencies": {"@cplieger/reactive": "1.2.0-dev.3"},'
            ' "peerDependencies": {"@cplieger/actions": "^2.0.0"},'
            ' "optionalDependencies": {"@cplieger/fetch": "3.0.0-dev.1"},'
            ' "devDependencies": {"@cplieger/ui-primitives": "1.0.0-dev.7"}}'
        )
        got = promote.purity_violations({'web/package.json': text})
        self.assertEqual(
            got,
            [
                'web/package.json: dependencies @cplieger/reactive 1.2.0-dev.3',
                'web/package.json: optionalDependencies @cplieger/fetch 3.0.0-dev.1',
            ],
        )

    def test_jsr_json_imports(self):
        text = '{"imports": {"@cplieger/reactive": "jsr:@cplieger/reactive@1.2.0-dev.3", "zod": "npm:zod@3.0.0-dev.1"}}'
        got = promote.purity_violations({'jsr.json': text})
        self.assertEqual(
            got, ['jsr.json: imports @cplieger/reactive jsr:@cplieger/reactive@1.2.0-dev.3']
        )

    def test_a_json_manifest_of_the_wrong_shape_is_a_named_input_error(self):
        cases = {
            'package.json': [
                ('[]', 'package.json is not a JSON object'),
                ('null', 'package.json is not a JSON object'),
                ('"x"', 'package.json is not a JSON object'),
                ('{"dependencies": []}', 'dependencies in package.json is not an object'),
                (
                    '{"peerDependencies": ["@cplieger/x"]}',
                    'peerDependencies in package.json is not an object',
                ),
                (
                    '{"optionalDependencies": "x"}',
                    'optionalDependencies in package.json is not an object',
                ),
                ('{', 'package.json is not valid JSON'),
            ],
            'jsr.json': [
                ('[]', 'jsr.json is not a JSON object'),
                ('7', 'jsr.json is not a JSON object'),
                ('{"imports": []}', 'imports in jsr.json is not an object'),
                ('{"imports": ["@cplieger/x"]}', 'imports in jsr.json is not an object'),
            ],
        }
        for path, shapes in cases.items():
            for text, want in shapes:
                with self.subTest(path=path, text=text):
                    with self.assertRaises(promote.PurityInputError) as caught:
                        promote.purity_violations({path: text})
                    self.assertTrue(str(caught.exception).startswith(want), str(caught.exception))

    def test_absent_and_null_sections_are_no_pins(self):
        for path in ('package.json', 'jsr.json'):
            for text in ('{}', '{"dependencies": null, "imports": null}'):
                with self.subTest(path=path, text=text):
                    self.assertEqual(promote.purity_violations({path: text}), [])

    def test_dockerfile_from_and_args(self):
        text = (
            'FROM ghcr.io/cplieger/knell:v2.0.17-dev.3 AS base\n'
            'FROM alpine:3.23.1-dev.1\n'
            '# renovate: datasource=npm depName=@cplieger/web-terminal-ui\n'
            'ARG WTUI_VERSION=5.6.1-dev.2\n'
            '# renovate: datasource=npm depName=@xterm/xterm\n'
            'ARG XTERM_VERSION=5.5.0-dev.1\n'
            'ARG ENGINE=github.com/cplieger/web-terminal-engine@v5.1.0-dev.4\n'
            'ARG PKG_REFRESH=static\n'
        )
        got = promote.purity_violations({'Dockerfile': text})
        self.assertEqual(
            got,
            [
                'Dockerfile: FROM ghcr.io/cplieger/knell:v2.0.17-dev.3',
                'Dockerfile: ARG WTUI_VERSION=5.6.1-dev.2',
                'Dockerfile: ARG ENGINE=github.com/cplieger/web-terminal-engine@v5.1.0-dev.4',
            ],
        )

    def test_renovate_hint_applies_to_the_next_arg_only(self):
        text = (
            '# renovate: datasource=npm depName=@cplieger/web-terminal-ui\n'
            'ARG WTUI_VERSION=5.6.1\n'
            'ARG OTHER=1.0.0-dev.1\n'
        )
        self.assertEqual(promote.purity_violations({'Dockerfile': text}), [])

    def test_renovate_hint_is_disowned_by_any_line_in_between(self):
        # Renovate reads the marker on the line right above the ARG, so a
        # marker followed by other instructions annotates nothing.
        text = (
            '# renovate: datasource=docker depName=ghcr.io/cplieger/knell\n'
            'FROM alpine:3.23 AS base\n'
            'RUN apk add --no-cache ca-certificates\n'
            'ARG OTHER=1.0.0-dev.1\n'
        )
        self.assertEqual(promote.purity_violations({'Dockerfile': text}), [])

    def test_purity_file_selection(self):
        self.assertTrue(promote.is_purity_file('go.mod'))
        self.assertTrue(promote.is_purity_file('web/jsr.json'))
        self.assertTrue(promote.is_purity_file('Dockerfile.arm64'))
        self.assertFalse(promote.is_purity_file('static-src/node_modules/x/package.json'))
        self.assertFalse(promote.is_purity_file('main.go'))


class PurityPerLane(unittest.TestCase):
    def test_each_violation_names_its_lane_and_shipped_pin_files_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.commit(
                'feat: init',
                {
                    'go.mod': GO_MOD + 'require github.com/cplieger/httpx v5.0.4-dev.1\n',
                    'main.go': 'package main\n',
                    'sub/go.mod': 'module github.com/cplieger/demo/sub\n\nrequire github.com/cplieger/envx v2.2.0-dev.3\n',
                    'sub/x.go': 'package sub\n',
                    'entrypoint.sh': '# renovate: datasource=github-releases depName=cplieger/toolbelt\nTOOLBELT_VERSION=v3.3.0-dev.2\n',
                    'package-lock.json': '{"lockfileVersion": 3, "packages": {"node_modules/@cplieger/x": {"version": "1.0.0-dev.1"}}}',
                },
            )
            repo = promote.inventory.Repo(str(fx.src))
            got = promote.check_purity(repo, fx.g('rev-parse', 'HEAD'))
        self.assertEqual(len(got), 3, got)
        self.assertIn('lane .:', got[0])
        self.assertIn('(go.mod: github.com/cplieger/httpx v5.0.4-dev.1)', got[0])
        self.assertIn('lane sub:', got[1])
        self.assertIn('(sub/go.mod: github.com/cplieger/envx v2.2.0-dev.3)', got[1])
        self.assertIn('lane .:', got[2])
        self.assertIn('(entrypoint.sh: cplieger/toolbelt v3.3.0-dev.2)', got[2])


class Reconciliation(unittest.TestCase):
    """The writer here and scripts/reconciliation.sh, the stable pipeline's reader, agree."""

    def sh(self, cwd, script, *args):
        return subprocess.run(
            ['bash', '-c', f'. "$0"; {script}', str(HERE / 'reconciliation.sh'), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_subject_is_the_one_the_shell_reader_matches(self):
        got = self.sh(HERE, 'printf %s "$RECONCILIATION_SUBJECT"').stdout
        self.assertEqual(got, promote.RECONCILIATION_SUBJECT)

    def test_r_has_t_tree_parents_m_then_t_and_one_digest_trailer(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            r = promote.build_reconciliation(fx.src, m, t, DIGEST)
            self.assertEqual(r['parents'], [m, t])
            self.assertEqual(r['tree'], fx.g('rev-parse', f'{t}^{{tree}}'))
            self.assertEqual(fx.g('rev-list', '--parents', '-n1', r['sha']).split()[1:], [m, t])
            self.assertEqual(fx.g('rev-parse', f'{r["sha"]}^{{tree}}'), r['tree'])
            self.assertEqual(self.sh(fx.src, 'is_reconciliation "$1"', r['sha']).returncode, 0)
            got = self.sh(fx.src, 'promoted_digest "$1"', r['sha'])
            self.assertEqual(got.stdout.strip(), DIGEST)
            self.assertEqual(
                r['message'], f'{promote.RECONCILIATION_SUBJECT}\n\nPromoted-Digest: {DIGEST}\n'
            )

    def test_a_promotion_without_an_image_carries_no_trailer(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            r = promote.build_reconciliation(fx.src, m, t, '')
            self.assertEqual(r['message'], promote.RECONCILIATION_SUBJECT + '\n')
            self.assertEqual(self.sh(fx.src, 'is_reconciliation "$1"', r['sha']).returncode, 0)
            self.assertEqual(self.sh(fx.src, 'promoted_digest "$1"', r['sha']).returncode, 1)

    def test_a_writer_the_reader_does_not_recognise_is_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            with (
                mock.patch.object(promote, 'RECONCILIATION_SUBJECT', 'release: promote'),
                self.assertRaisesRegex(promote.GhError, 'does not recognise'),
            ):
                promote.build_reconciliation(fx.src, m, t, '')


class Snapshot(unittest.TestCase):
    def run_snapshot(self, fx, repo='demo', target='', meta=None):
        out = fx.tmp / 'github_output'
        out.write_text('')
        meta = {
            'name': repo,
            'default_branch': 'dev',
            'visibility': 'public',
            'archived': False,
            'fork': False,
            **(meta or {}),
        }
        calls = []

        def gh_json(path):
            calls.append(path)
            return meta

        args = promote.parse_args(
            ['--work-dir', str(fx.work), 'snapshot', '--repo', repo, '--target', target]
        )
        with (
            mock.patch.object(promote, 'gh_json', side_effect=gh_json),
            mock.patch.object(promote, 'CLONE_URL', fx.url()),
            mock.patch.dict(
                os.environ, {'GITHUB_OUTPUT': str(out), 'GITHUB_STEP_SUMMARY': os.devnull}
            ),
        ):
            rc = promote.cmd_snapshot(args)
        values = dict(line.split('=', 1) for line in out.read_text().splitlines())
        return rc, values, calls

    def test_outputs_are_the_dev_head_and_main_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            fx.publish()
            rc, values, _ = self.run_snapshot(fx)
        self.assertEqual(rc, 0)
        self.assertEqual(values, {'repo': 'demo', 'target': t, 'main': m})

    def test_an_abbreviated_target_on_dev_resolves_to_its_full_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            fx.commit('feat: later', {'b.go': 'package main\n'})
            fx.publish()
            rc, values, _ = self.run_snapshot(fx, target=t[:10])
        self.assertEqual((rc, values['target'], values['main']), (0, t, m))

    def test_single_main_and_malformed_repos_are_refused_before_any_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            for repo in ('tool-catalog', 'ci', '../x', 'a b'):
                with self.subTest(repo=repo):
                    rc, values, calls = self.run_snapshot(fx, repo=repo)
                    self.assertEqual((rc, values, calls), (1, {}, []))

    def test_a_main_default_archived_forked_or_private_repo_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            go_repo(fx)
            fx.publish()
            self.assertEqual(self.run_snapshot(fx)[0], 0)
            for meta, why in (
                ({'default_branch': 'main'}, 'the default branch of demo is main, not dev'),
                ({'archived': True}, 'demo is archived or a fork'),
                ({'fork': True}, 'demo is archived or a fork'),
                ({'visibility': 'private'}, 'demo is not public'),
                ({'visibility': None}, 'demo is not public'),
                ({'archived': None}, 'demo is not a two-branch repository'),
            ):
                with self.subTest(meta=meta), redirect_stderr(io.StringIO()) as err:
                    rc, values, _ = self.run_snapshot(fx, meta=meta)
                self.assertEqual((rc, values), (1, {}))
                self.assertIn(f'::error::refused: {why}', err.getvalue())

    def test_a_target_off_dev_first_parent_history_or_already_on_main_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, _ = go_repo(fx)
            fx.g('checkout', '-q', '-b', 'side', m)
            side = fx.commit('feat: side', {'s.go': 'package main\n'})
            fx.g('checkout', '-q', 'dev')
            fx.g('merge', '-q', '--no-ff', '-m', 'merge side', 'side')
            fx.publish()
            for target in (side, m, 'not-a-sha'):
                with self.subTest(target=target):
                    rc, values, _ = self.run_snapshot(fx, target=target)
                    self.assertEqual((rc, values), (1, {}))


class Checks(unittest.TestCase):
    def run_check(self, fx, m, t, *, classify=None, extra=(), patches=()):
        plan = fx.work / 'plan.json'
        summary = fx.work / 'summary.md'
        args = promote.parse_args(
            [
                '--work-dir',
                str(fx.work),
                'check',
                '--repo',
                fx.name,
                '--main',
                m,
                '--target',
                t,
                '--plan-file',
                str(plan),
                *extra,
            ]
        )
        with (
            mock.patch.object(promote, 'CLONE_URL', fx.url()),
            mock.patch.object(promote, 'load_classify', return_value=classify or fake_classify()),
            mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}),
        ):
            for p in patches:
                p.start()
            try:
                rc = promote.cmd_check(args)
            finally:
                for p in patches:
                    p.stop()
        return rc, (json.loads(plan.read_text()) if plan.exists() else None), summary.read_text()

    def test_every_check_passing_plans_r_from_exactly_m_and_t(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            fx.publish()
            rc, plan, summary = self.run_check(fx, m, t, extra=['--dry-run'])
            tree = fx.g('rev-parse', f'{t}^{{tree}}')
        self.assertEqual(rc, 0, summary)
        self.assertEqual(plan['repo'], 'demo')
        self.assertEqual(plan['parents'], [m, t])
        self.assertEqual(plan['tree'], tree)
        self.assertEqual(plan['message'], promote.RECONCILIATION_SUBJECT + '\n')
        self.assertIn('Dry run: main is not moved.', summary)

    def test_main_moved_since_the_snapshot_is_refused_before_any_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            fx.g('checkout', '-q', 'main')
            fx.commit('fix(deps): bump', {'go.sum': 'x\n'})
            fx.publish()
            with mock.patch.object(promote, 'run_checks', side_effect=AssertionError('checked')):
                rc, plan, summary = self.run_check(fx, m, t)
        self.assertEqual((rc, plan), (1, None))
        self.assertIn('main moved since the snapshot', summary)

    def test_a_dev_holding_only_an_empty_rebuild_has_nothing_to_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m = fx.commit('feat: init', {'go.mod': GO_MOD, 'main.go': 'package main\n'})
            fx.g('checkout', '-q', '-b', 'dev')
            t = fx.commit('fix(deps): rebuild', empty=True)
            fx.publish()
            rc, plan, summary = self.run_check(fx, m, t)
        self.assertEqual((rc, plan), (1, None))
        self.assertIn('nothing to publish', summary)

    def test_a_docs_only_dev_has_nothing_to_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m = fx.commit('feat: init', {'go.mod': GO_MOD, 'main.go': 'package main\n'})
            fx.g('checkout', '-q', '-b', 'dev')
            t = fx.commit('docs: readme', {'README.md': '# x\n'})
            fx.publish()
            rc, _, summary = self.run_check(fx, m, t)
        self.assertEqual(rc, 1)
        self.assertIn('nothing to publish', summary)

    def test_dominance_refuses_a_non_machine_path_main_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            _, t = go_repo(fx)
            fx.g('checkout', '-q', 'main')
            m = fx.commit('fix: hand edit on main', {'util.go': 'package main\n'})
            fx.publish()
            rc, plan, summary = self.run_check(fx, m, t)
        self.assertEqual((rc, plan), (1, None))
        self.assertIn('util.go: neither an inventory surface nor sync-owned', summary)

    def test_dominance_accepts_a_sync_owned_file_equal_to_the_canonical_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            _, t0 = go_repo(fx)
            canonical = (ROOT / '.editorconfig').read_text()
            t = fx.commit('chore(sync): editorconfig', {'.editorconfig': canonical})
            fx.g('checkout', '-q', 'main')
            m = fx.commit('chore(sync): editorconfig', {'.editorconfig': 'root = true\n'})
            fx.publish()
            classify = fake_classify(sources={'.editorconfig': '.editorconfig'})
            rc, _, summary = self.run_check(fx, m, t, classify=classify)
            self.assertEqual(rc, 0, summary)
            fx.g('checkout', '-q', 'dev')
            t2 = fx.commit('chore: drift', {'.editorconfig': canonical + '# drift\n'})
            fx.publish()
            rc, _, summary = self.run_check(fx, m, t2, classify=classify)
        self.assertNotEqual(t0, t2)
        self.assertEqual(rc, 1)
        self.assertIn('sync-owned, target equals neither main nor the canonical copy', summary)

    def test_dominance_fails_closed_until_classify_repos_publishes_the_sync_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            fx.publish()
            rc, _, summary = self.run_check(fx, m, t, classify=types.SimpleNamespace())
        self.assertEqual(rc, 1)
        self.assertIn('publishes no sync_owned_patterns()/canonical_sources()', summary)

    def test_a_failed_classifier_tags_read_is_a_named_dominance_refusal(self):
        classify = promote.load_classify()
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            fx.publish()
            with mock.patch.object(classify, 'api_json', return_value=None):
                rc, plan, summary = self.run_check(fx, m, t, classify=classify)
        self.assertEqual((rc, plan), (1, None))
        self.assertIn(
            '- dominance: classify-repos.py: tags read failed for demo; aborting rather than '
            'guessing the cliff tier',
            summary,
        )

    def test_a_wrong_shape_classifier_tags_page_is_a_named_dominance_refusal(self):
        classify = promote.load_classify()
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            fx.publish()
            with mock.patch.object(classify, 'api_json', return_value={'message': 'x'}):
                rc, plan, summary = self.run_check(fx, m, t, classify=classify)
        self.assertEqual((rc, plan), (1, None))
        self.assertIn(
            '- dominance: classify-repos.py: tags read failed for demo; aborting', summary
        )

    def test_purity_refuses_a_first_party_dev_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, _ = go_repo(fx)
            t = fx.commit(
                'fix(deps): pre-release',
                {'go.mod': GO_MOD + 'require github.com/cplieger/httpx v5.0.4-dev.1\n'},
            )
            fx.publish()
            rc, _, summary = self.run_check(fx, m, t)
        self.assertEqual(rc, 1)
        self.assertIn('lane .: a first-party dependency is pinned at a dev version', summary)
        self.assertIn('github.com/cplieger/httpx v5.0.4-dev.1', summary)

    def test_purity_refuses_a_manifest_it_cannot_read_and_still_judges_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, _ = go_repo(fx)
            t = fx.commit(
                'fix(deps): pre-release',
                {
                    'go.mod': GO_MOD + 'require github.com/cplieger/httpx v5.0.4-dev.1\n',
                    'package.json': '{"dependencies": []}',
                },
            )
            fx.publish()
            rc, _, summary = self.run_check(fx, m, t)
        self.assertEqual(rc, 1)
        self.assertIn(
            'lane .: dependencies in package.json is not an object, so the first-party pins in '
            'package.json cannot be judged. Fix package.json on dev',
            summary,
        )
        self.assertIn('github.com/cplieger/httpx v5.0.4-dev.1', summary)

    def test_an_image_repo_resolves_and_scans_its_dev_digest_and_trails_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m = fx.commit(
                'feat: init', {'Dockerfile': 'FROM scratch\n', 'main.go': 'package main\n'}
            )
            fx.g('checkout', '-q', '-b', 'dev')
            t = fx.commit('feat: more', {'main.go': 'package main\n// more\n'})
            fx.publish()
            seen = {}

            def vulns(clone, repo, main, digest):
                seen['vulns'] = (repo, main, digest)
                return []

            patches = (
                mock.patch.object(promote, 'check_digest', return_value=(DIGEST, [])),
                mock.patch.object(promote, 'check_vulnerabilities', side_effect=vulns),
            )
            rc, plan, summary = self.run_check(fx, m, t, patches=patches)
        self.assertEqual(rc, 0, summary)
        self.assertEqual(seen['vulns'], ('demo', m, DIGEST))
        self.assertEqual(
            plan['message'], f'{promote.RECONCILIATION_SUBJECT}\n\nPromoted-Digest: {DIGEST}\n'
        )

    def test_a_tampered_preview_output_changes_nothing_check_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m, t = go_repo(fx)
            fx.publish()
            forged = {
                'repo': 'demo',
                'sha': '0' * 40,
                'tree': '1' * 40,
                'parents': ['2' * 40, '3' * 40],
                'message': 'x',
            }
            (fx.work / 'plan.json').write_text(json.dumps(forged))
            (fx.work / 'demo-notes.md').write_text('forged')
            (fx.work / 'demo.output').write_text('state={"forged": true}\n')
            rc, plan, _ = self.run_check(fx, m, t)
        self.assertEqual(rc, 0)
        self.assertEqual(plan['parents'], [m, t])
        self.assertNotEqual(plan['tree'], forged['tree'])


STUB_CURL = r"""#!/usr/bin/env bash
out="" url="" head=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift ;;
    -H | --connect-timeout | --max-time | --retry | --retry-max-time) shift ;;
    -*I*) head=true ;;
    -*) ;;
    *) url=$1 ;;
  esac
  shift
done
echo "$url" >>"$STUB_LOG"
case "$url" in
  */token*) echo '{"token":"t"}' >"$out" ;;
  */manifests/sha-*)
    sha=${url##*/sha-}
    if grep -qx "$sha" "$STUB_IMAGES"; then
      printf 'HTTP/2 200\r\ndocker-content-digest: sha256:%s\r\n\r\n' "$(printf '%064d' 7)"
    else
      exit 22
    fi
    ;;
  *) exit 22 ;;
esac
"""
STUB_COSIGN = r"""#!/usr/bin/env bash
echo "cosign $*" >>"$STUB_LOG"
if [ "${STUB_SIGNED:-yes}" = yes ]; then exit 0; fi
echo 'Error: no matching signatures' >&2
exit 1
"""


class RetainedDigest(unittest.TestCase):
    """The retained-digest check runs the real promote-digest.sh against stubbed registry and cosign answers."""

    def run_digest(self, built: list[str], signed: bool):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.commit('feat: init', {'Dockerfile': 'FROM scratch\n', 'main.go': 'package main\n'})
            fx.g('checkout', '-q', '-b', 'dev')
            b = fx.commit('feat: build', {'main.go': 'package main\n// b\n'})
            t = fx.commit('docs: readme', {'README.md': '# x\n'})
            bin_dir = Path(tmp) / 'bin'
            bin_dir.mkdir()
            for name, body in (('curl', STUB_CURL), ('cosign', STUB_COSIGN)):
                (bin_dir / name).write_text(body)
                (bin_dir / name).chmod(0o755)
            images = Path(tmp) / 'images'
            images.write_text(''.join({'b': b, 't': t}[x] + '\n' for x in built))
            log = Path(tmp) / 'log'
            env = {
                'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                'STUB_IMAGES': str(images),
                'STUB_LOG': str(log),
                'STUB_SIGNED': 'yes' if signed else 'no',
            }
            repo = promote.inventory.Repo(str(fx.src))
            layout = promote.layout_of(repo, t)
            with mock.patch.dict(os.environ, env):
                sig = promote.path_significance(fx.src, b, t, layout)
                got = promote.check_digest(fx.src, 'demo', t, layout, sig)
            return got, log.read_text() if log.exists() else '', b

    def test_a_docs_only_target_re_tags_its_nearest_built_ancestor(self):
        (digest, refusals), log, b = self.run_digest(['b'], signed=True)
        self.assertEqual((digest, refusals), ('sha256:' + '0' * 63 + '7', []))
        self.assertIn('--certificate-github-workflow-repository cplieger/demo', log)
        self.assertIn(f'--certificate-github-workflow-sha {b}', log)

    def test_no_retained_image_is_refused(self):
        (digest, refusals), _, _ = self.run_digest([], signed=True)
        self.assertEqual(digest, '')
        self.assertEqual(len(refusals), 1)
        self.assertIn('no complete dev image to re-tag', refusals[0])

    def test_an_image_whose_build_stopped_before_signing_is_refused(self):
        (digest, refusals), log, _ = self.run_digest(['b'], signed=False)
        self.assertIn('cosign verify', log)
        self.assertEqual(digest, '')
        self.assertIn('no complete dev image to re-tag', refusals[0])


class Vulnerabilities(unittest.TestCase):
    def run_vulns(self, ours, theirs, findings, *, tag='v1.0.0', waited=''):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m = fx.commit('feat: init', {'Dockerfile': 'FROM scratch\n'})
            if tag:
                fx.g('tag', tag)
            refs = {}

            def platforms(repo, digest, token):
                return ours if digest == DIGEST else theirs

            def tag_digest(repo, name, token):
                refs['tag'] = name
                return 'sha256:main'

            def scan(ref):
                refs.setdefault('scanned', []).append(ref)
                return findings.get(ref.rsplit('@', 1)[1], set())

            with (
                mock.patch.object(promote, 'wait_for_main_release', return_value=waited),
                mock.patch.object(promote, 'registry_token', return_value='t'),
                mock.patch.object(promote, 'tag_digest', side_effect=tag_digest),
                mock.patch.object(promote, 'platforms', side_effect=platforms),
                mock.patch.object(promote, 'trivy_findings', side_effect=scan),
            ):
                return promote.check_vulnerabilities(fx.src, 'demo', m, DIGEST), refs

    def test_a_fixable_finding_main_lacks_is_refused_per_platform(self):
        ours = {'linux/amd64': 't-amd', 'linux/arm64': 't-arm'}
        theirs = {'linux/amd64': 'm-amd', 'linux/arm64': 'm-arm'}
        findings = {
            't-amd': {('CVE-1', 'openssl'), ('CVE-2', 'zlib')},
            'm-amd': {('CVE-2', 'zlib')},
            't-arm': {('CVE-3', 'musl')},
            'm-arm': set(),
        }
        got, refs = self.run_vulns(ours, theirs, findings)
        self.assertEqual(refs['tag'], 'v1.0.0')
        self.assertEqual(len(refs['scanned']), 4)
        self.assertEqual(len(got), 2, got)
        self.assertIn('linux/amd64: CVE-1 in openssl', got[0])
        self.assertIn('linux/arm64: CVE-3 in musl', got[1])

    def test_findings_main_also_has_pass(self):
        got, _ = self.run_vulns(
            {'linux/amd64': 't'},
            {'linux/amd64': 'm'},
            {'t': {('CVE-1', 'x')}, 'm': {('CVE-1', 'x')}},
        )
        self.assertEqual(got, [])

    def test_a_platform_main_never_published_holds_every_finding_against_t(self):
        got, _ = self.run_vulns(
            {'linux/amd64': 't', 'linux/arm64': 'ta'},
            {'linux/amd64': 'm'},
            {'ta': {('CVE-9', 'y')}},
        )
        self.assertEqual(len(got), 1)
        self.assertIn('linux/arm64: CVE-9 in y', got[0])

    def test_a_platform_main_publishes_and_the_dev_image_lacks_is_refused(self):
        got, refs = self.run_vulns(
            {'linux/amd64': 't'},
            {'linux/amd64': 'm', 'linux/arm64': 'ma', 'linux/arm/v7': 'mv7'},
            {},
        )
        self.assertEqual(len(got), 2, got)
        self.assertIn("image linux/arm/v7: main's v1.0.0 publishes this platform", got[0])
        self.assertIn("image linux/arm64: main's v1.0.0 publishes this platform", got[1])
        self.assertEqual(sorted(r.rsplit('@', 1)[1] for r in refs['scanned']), ['m', 't'])

    def test_one_manifest_against_an_index_is_refused_as_a_shape_mismatch(self):
        index = {'linux/amd64': 'x', 'linux/arm64': 'xa'}
        for ours, theirs, main_shape, dev_shape in (
            ({'single': 't'}, index, 'a multi-platform index', 'one manifest'),
            (index, {'single': 'm'}, 'one manifest', 'a multi-platform index'),
        ):
            got, refs = self.run_vulns(ours, theirs, {})
            self.assertEqual(len(got), 1, got)
            self.assertIn(
                f"image: main's v1.0.0 is {main_shape} and the dev image is {dev_shape}", got[0]
            )
            self.assertNotIn('scanned', refs)

    def test_one_manifest_on_both_sides_is_scanned_as_one_platform(self):
        got, refs = self.run_vulns(
            {'single': 't'}, {'single': 'm'}, {'t': {('CVE-1', 'x')}, 'm': set()}
        )
        self.assertEqual(len(got), 1, got)
        self.assertIn('image single: CVE-1 in x', got[0])
        self.assertEqual(sorted(r.rsplit('@', 1)[1] for r in refs['scanned']), ['m', 't'])

    def test_no_stable_image_on_main_passes_without_a_scan(self):
        got, refs = self.run_vulns({'linux/amd64': 't'}, {}, {'t': {('CVE-1', 'x')}}, tag='')
        self.assertEqual((got, refs), ([], {}))

    def test_main_release_still_running_is_refused(self):
        got, refs = self.run_vulns({}, {}, {}, waited='image: still running')
        self.assertEqual((got, refs), (['image: still running'], {}))


class WaitForMainRelease(unittest.TestCase):
    def wait(self, answers, committed_ago):
        calls = iter(answers)
        clock = iter(range(0, 10**6, promote.MAIN_RUN_POLL_SECONDS))
        slept = []
        with mock.patch.object(promote, 'release_runs', side_effect=lambda repo, sha: next(calls)):
            got = promote.wait_for_main_release(
                'demo',
                'm' * 40,
                datetime.now(UTC) - timedelta(seconds=committed_ago),
                sleep=slept.append,
                clock=lambda: next(clock),
            )
        return got, len(slept)

    def test_waits_until_every_run_of_main_is_complete(self):
        busy, done = [{'status': 'in_progress'}], [{'status': 'completed'}]
        self.assertEqual(self.wait([busy, busy, done], 30), ('', 2))

    def test_a_young_main_with_no_run_listed_yet_waits_for_one(self):
        self.assertEqual(self.wait([[], [{'status': 'completed'}]], 30), ('', 1))

    def test_an_old_main_with_no_run_has_none_in_flight(self):
        self.assertEqual(self.wait([[]], promote.MAIN_RUN_GRACE_SECONDS + 1), ('', 0))

    def test_gives_up_with_a_refusal(self):
        polls = promote.MAIN_RUN_WAIT_SECONDS // promote.MAIN_RUN_POLL_SECONDS + 1
        got, _ = self.wait([[{'status': 'queued'}]] * polls, 30)
        self.assertIn('still running', got)


class Trivy(unittest.TestCase):
    def test_one_db_snapshot_fixable_high_and_critical_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = Path(tmp) / 'argv'
            stub = Path(tmp) / 'trivy'
            report = {
                'Results': [
                    {'Vulnerabilities': [{'VulnerabilityID': 'CVE-1', 'PkgName': 'openssl'}]},
                    {'Vulnerabilities': None},
                ]
            }
            stub.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$@\" >{argv}\necho '{json.dumps(report)}'\n"
            )
            stub.chmod(0o755)
            with mock.patch.dict(os.environ, {'TRIVY_BIN': str(stub), 'TRIVY_CACHE_DIR': '/db'}):
                got = promote.trivy_findings('ghcr.io/cplieger/demo@sha256:x')
            args = argv.read_text().split('\n')
        self.assertEqual(got, {('CVE-1', 'openssl')})
        for flag in (
            '--skip-db-update',
            '--skip-java-db-update',
            '--ignore-unfixed',
            'HIGH,CRITICAL',
        ):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--cache-dir') + 1], '/db')
        self.assertEqual(args[-2], 'ghcr.io/cplieger/demo@sha256:x')

    def test_a_failed_scan_is_a_read_failure(self):
        with (
            mock.patch.dict(os.environ, {'TRIVY_BIN': 'false'}),
            self.assertRaises(promote.GhError),
        ):
            promote.trivy_findings('x')


class Platforms(unittest.TestCase):
    def test_attestation_manifests_are_not_platforms(self):
        index = {
            'mediaType': 'application/vnd.oci.image.index.v1+json',
            'manifests': [
                {'digest': 'sha256:a', 'platform': {'os': 'linux', 'architecture': 'amd64'}},
                {
                    'digest': 'sha256:b',
                    'platform': {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'},
                },
                {'digest': 'sha256:c', 'platform': {'os': 'unknown', 'architecture': 'unknown'}},
            ],
        }
        with mock.patch.object(promote, 'registry_json', return_value=index):
            got = promote.platforms('demo', 'sha256:i', 't')
        self.assertEqual(got, {'linux/amd64': 'sha256:a', 'linux/arm64/v8': 'sha256:b'})


R_SHA = 'c' * 40
MAIN_SHA = '1' * 40
TARGET_SHA = '2' * 40
TREE_SHA = 'e' * 40
R_MESSAGE = f'{promote.RECONCILIATION_SUBJECT}\n\nPromoted-Digest: {DIGEST}\n'


def commit_doc(**over):
    doc = {
        'sha': R_SHA,
        'tree': {'sha': TREE_SHA},
        'parents': [{'sha': MAIN_SHA}, {'sha': TARGET_SHA}],
        'message': R_MESSAGE.rstrip('\n'),
    }
    doc.update(over)
    return doc


class Create(unittest.TestCase):
    PLAN: ClassVar[dict] = {
        'repo': 'demo',
        'digest': DIGEST,
        'sha': 'r' * 40,
        'tree': TREE_SHA,
        'parents': [MAIN_SHA, TARGET_SHA],
        'message': R_MESSAGE,
    }

    def run_create(self, answer):
        calls = []

        def gh_send(method, path, body):
            calls.append((method, path, body))
            return answer

        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / 'plan.json'
            plan.write_text(json.dumps(self.PLAN))
            out = Path(tmp) / 'out'
            out.write_text('')
            with (
                mock.patch.object(promote, 'gh_send', side_effect=gh_send),
                mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(out)}),
            ):
                rc = promote.cmd_create(promote.parse_args(['create', '--plan-file', str(plan)]))
            return rc, calls, out.read_text()

    def test_creates_r_and_outputs_it_with_the_digest_and_moves_no_ref(self):
        rc, calls, out = self.run_create(commit_doc())
        self.assertEqual(rc, 0)
        self.assertEqual([c[:2] for c in calls], [('POST', 'repos/cplieger/demo/git/commits')])
        self.assertEqual(calls[0][2]['parents'], [MAIN_SHA, TARGET_SHA])
        self.assertIn(f'r={R_SHA}', out.splitlines())
        self.assertIn(f'digest={DIGEST}', out.splitlines())

    def test_a_created_commit_unlike_the_checked_one_outputs_nothing(self):
        for over in (
            {'tree': {'sha': 'f' * 40}},
            {'parents': [{'sha': TARGET_SHA}, {'sha': MAIN_SHA}]},
            {'message': 'release: promote dev into main'},
        ):
            with self.subTest(over=over):
                rc, _, out = self.run_create(commit_doc(**over))
                self.assertEqual((rc, out), (1, ''))


class FakeRegistry:
    """urlopen for ghcr.io: a token endpoint, one index manifest, and the tags pushed."""

    def __init__(self, body=b'{"manifests": []}', put_error=None, answer_digest=None):
        self.body = body
        self.digest = 'sha256:' + __import__('hashlib').sha256(body).hexdigest()
        self.tags: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.put_error = put_error
        self.answer_digest = answer_digest

    def __call__(self, req, timeout=None):
        url, method = req.full_url, req.get_method()
        self.calls.append((method, url.rsplit('/', 1)[-1]))
        if '/token?' in url:
            assert req.get_header('Authorization').startswith('Basic ')
            return self.response({}, b'{"token": "push"}')
        name = url.rsplit('/', 1)[-1]
        if method == 'PUT':
            if self.put_error:
                raise self.put_error
            self.tags[name] = req.data
            self.put_type = req.get_header('Content-type')
            return self.response({}, b'')
        if name == self.digest:
            return self.response(
                {'Content-Type': 'application/vnd.oci.image.index.v1+json'}, self.body
            )
        if name in self.tags:
            digest = (
                self.answer_digest
                or 'sha256:' + __import__('hashlib').sha256(self.tags[name]).hexdigest()
            )
            return self.response({'Docker-Content-Digest': digest}, b'')
        raise promote.urllib.error.URLError(f'404 {name}')

    @staticmethod
    def response(headers, body):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.headers.items.return_value = list(headers.items())
        resp.read.return_value = body
        return resp


class Tag(unittest.TestCase):
    def run_tag(self, registry, digest=None, env=None):
        args = ['tag', '--repo', 'demo', '--digest', digest or registry.digest, '--r', R_SHA]
        with (
            mock.patch.object(promote.urllib.request, 'urlopen', side_effect=registry),
            mock.patch.dict(
                os.environ, {'GHCR_TOKEN': 'pat', 'GITHUB_STEP_SUMMARY': os.devnull, **(env or {})}
            ),
        ):
            return promote.cmd_tag(promote.parse_args(args))

    def test_pushes_the_digests_own_bytes_under_promoted_r_then_reads_it_back(self):
        registry = FakeRegistry()
        self.assertEqual(self.run_tag(registry), 0)
        tag = f'promoted-{R_SHA}'
        self.assertEqual(registry.tags, {tag: registry.body})
        self.assertEqual(registry.put_type, 'application/vnd.oci.image.index.v1+json')
        self.assertEqual(
            [m for m, _ in registry.calls], ['GET', 'GET', 'PUT', 'HEAD'], registry.calls
        )
        self.assertEqual(registry.calls[-1], ('HEAD', tag))

    def test_a_failed_write_or_a_different_read_back_fails(self):
        refused = promote.urllib.error.URLError('403 denied')
        for name, registry in (
            ('write refused', FakeRegistry(put_error=refused)),
            ('read back names another digest', FakeRegistry(answer_digest='sha256:' + 'b' * 64)),
        ):
            with self.subTest(name):
                self.assertEqual(self.run_tag(registry), 1)

    def test_bytes_that_are_not_the_digest_are_never_pushed(self):
        registry = FakeRegistry()
        registry.digest = DIGEST
        self.assertEqual(self.run_tag(registry, digest=DIGEST), 1)
        self.assertEqual(registry.tags, {})

    def test_refuses_without_a_credential_or_with_a_malformed_digest(self):
        self.assertEqual(self.run_tag(FakeRegistry(), env={'GHCR_TOKEN': ''}), 2)
        self.assertEqual(self.run_tag(FakeRegistry(), digest='sha256:short'), 2)


class Move(unittest.TestCase):
    def run_move(self, created=None, tagged=DIGEST, patch=None, digest=DIGEST):
        sends = []

        def gh_json(path):
            if path.endswith(f'/git/commits/{R_SHA}'):
                return created or commit_doc()
            if path.endswith(f'/git/commits/{TARGET_SHA}'):
                return {'sha': TARGET_SHA, 'tree': {'sha': TREE_SHA}}
            raise AssertionError(path)

        def gh_send(method, path, body):
            sends.append((method, path, body))
            if isinstance(patch, Exception):
                raise patch
            return {}

        def tag_digest(repo, tag, token):
            self.assertEqual((repo, tag), ('demo', f'promoted-{R_SHA}'))
            if isinstance(tagged, Exception):
                raise tagged
            return tagged

        args = ['move', '--repo', 'demo', '--main', MAIN_SHA, '--target', TARGET_SHA]
        args += ['--digest', digest, '--r', R_SHA]
        with (
            mock.patch.object(promote, 'gh_json', side_effect=gh_json),
            mock.patch.object(promote, 'gh_send', side_effect=gh_send),
            mock.patch.object(promote, 'registry_token', return_value='pull'),
            mock.patch.object(promote, 'tag_digest', side_effect=tag_digest),
            mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': os.devnull}),
        ):
            rc = promote.cmd_move(promote.parse_args(args))
        return rc, sends

    def test_moves_main_to_r_once_without_force_after_its_tag(self):
        rc, sends = self.run_move()
        self.assertEqual(rc, 0)
        self.assertEqual(
            sends,
            [('PATCH', 'repos/cplieger/demo/git/refs/heads/main', {'sha': R_SHA, 'force': False})],
        )

    def test_an_image_lane_without_its_promoted_tag_never_moves_main(self):
        for tagged in (promote.GhError('HEAD x: 404'), 'sha256:' + 'b' * 64):
            with self.subTest(tagged=tagged):
                rc, sends = self.run_move(tagged=tagged)
                self.assertEqual((rc, sends), (1, []))

    def test_no_image_lane_moves_main_without_a_tag(self):
        message = f'{promote.RECONCILIATION_SUBJECT}'
        rc, sends = self.run_move(
            created=commit_doc(message=message), digest='', tagged=AssertionError
        )
        self.assertEqual((rc, len(sends)), (0, 1))

    def test_an_r_unlike_the_checked_one_never_moves_main(self):
        for over in (
            {'tree': {'sha': 'f' * 40}},
            {'parents': [{'sha': TARGET_SHA}, {'sha': MAIN_SHA}]},
            {'message': promote.RECONCILIATION_SUBJECT},
        ):
            with self.subTest(over=over):
                rc, sends = self.run_move(created=commit_doc(**over))
                self.assertEqual((rc, sends), (1, []))

    def test_main_moved_since_the_snapshot_fails_with_main_untouched(self):
        refused = promote.GhError('gh api -X PATCH failed: Update is not a fast forward (HTTP 422)')
        rc, sends = self.run_move(patch=refused)
        self.assertEqual(rc, 1)
        self.assertEqual(json.dumps(sends[0][2]['force']), 'false')


class Workflow(unittest.TestCase):
    doc = yaml.safe_load((ROOT / '.github/workflows/promote.yaml').read_text())
    text = (ROOT / '.github/workflows/promote.yaml').read_text()

    def test_manual_only_with_three_inputs(self):
        on = self.doc[True]
        self.assertEqual(list(on), ['workflow_dispatch'])
        self.assertEqual(list(on['workflow_dispatch']['inputs']), ['repo', 'target', 'dry_run'])

    def test_apply_reads_only_snapshot_outputs_and_the_dry_run_input(self):
        apply = self.doc['jobs']['apply']
        body = json.dumps(apply)
        self.assertNotIn('needs.preview', body)
        self.assertNotRegex(body, r'download-artifact|actions/cache|upload-artifact')
        exprs = set(re.findall(r'\$\{\{\s*([^}]*?)\s*\}\}', body))
        allowed = {
            'github.token',
            'secrets.PROMOTE_PAT',
            'inputs.dry_run',
            '!inputs.dry_run',
            'env.COSIGN_VERSION',
            "!cancelled() && needs.snapshot.result == 'success'",
            'needs.snapshot.outputs.repo',
            'needs.snapshot.outputs.target',
            'needs.snapshot.outputs.main',
            'steps.create.outputs.r',
            'steps.create.outputs.digest',
        }
        self.assertLessEqual(exprs, allowed, exprs - allowed)
        self.assertIn('preview', apply['needs'])

    def test_tag_and_move_read_only_snapshot_and_apply_outputs(self):
        for job in ('tag', 'move'):
            body = json.dumps(self.doc['jobs'][job])
            refs = set(re.findall(r'\b(needs\.\w+\.\w+(?:\.\w+)?|inputs\.\w+|steps\.\w+)', body))
            outside = {
                r for r in refs if not re.fullmatch(r'needs\.(snapshot|apply|tag)\.\w+(\.\w+)?', r)
            }
            self.assertEqual(outside, set(), job)
            self.assertNotIn('git-cliff', body)

    def job_runs(self, job, digest, r, tag='skipped', apply='success', preview='success'):
        needs = {
            'snapshot': {'result': 'success', 'outputs': {}},
            'preview': {'result': preview, 'outputs': {}},
            'apply': {'result': apply, 'outputs': {'r': r, 'digest': digest}},
            'tag': {'result': tag, 'outputs': {}},
        }
        return workflow_replay.job_condition(self.doc, job, {'needs': needs}, [])

    def test_main_moves_only_after_an_image_lane_is_tagged(self):
        r = 'c' * 40
        self.assertEqual(self.doc['jobs']['tag']['needs'], ['snapshot', 'apply'])
        self.assertIn('tag', self.doc['jobs']['move']['needs'])
        self.assertTrue(self.job_runs('tag', DIGEST, r))
        self.assertFalse(self.job_runs('tag', '', r), 'no image lane, no tag')
        self.assertFalse(self.job_runs('tag', DIGEST, ''), 'a dry run creates no R')
        self.assertFalse(self.job_runs('tag', DIGEST, r, apply='failure'))
        for digest, r_sha, tag, apply, moves in (
            (DIGEST, r, 'success', 'success', True),
            (DIGEST, r, 'failure', 'success', False),
            (DIGEST, r, 'skipped', 'success', False),
            (DIGEST, r, 'cancelled', 'success', False),
            ('', r, 'skipped', 'success', True),
            ('', '', 'skipped', 'success', False),
            ('', r, 'skipped', 'failure', False),
        ):
            with self.subTest(digest=digest, r=r_sha, tag=tag, apply=apply):
                self.assertEqual(self.job_runs('move', digest, r_sha, tag, apply), moves)

    def test_a_failed_preview_blocks_no_lane_kind(self):
        r = 'c' * 40
        self.assertTrue(self.job_runs('tag', DIGEST, r, preview='failure'))
        for digest, tag in ((DIGEST, 'success'), ('', 'skipped')):
            with self.subTest(digest=digest):
                self.assertTrue(self.job_runs('move', digest, r, tag, preview='failure'))

    def test_every_job_behind_the_preview_names_a_status_function(self):
        jobs = self.doc['jobs']

        def ancestors(job):
            needs = jobs[job].get('needs') or []
            direct = [needs] if isinstance(needs, str) else needs
            return set(direct).union(*(ancestors(n) for n in direct))

        behind = {j for j in jobs if 'preview' in ancestors(j)}
        self.assertEqual(behind, {'apply', 'tag', 'move', 'notify'})
        for job in behind:
            self.assertRegex(str(jobs[job]['if']), workflow_replay.STATUS_CALL.pattern, job)

    def test_each_token_reaches_only_its_steps(self):
        def holders(secret):
            return [
                (job, step.get('name'))
                for job, spec in self.doc['jobs'].items()
                for step in spec.get('steps') or []
                if f'secrets.{secret}' in json.dumps(step)
            ]

        self.assertEqual(holders('PROMOTE_PAT'), [('apply', 'Create R'), ('move', 'Promote')])
        self.assertEqual(self.text.count('secrets.PROMOTE_PAT'), 2)
        self.assertEqual(holders('PACKAGES_PAT'), [('tag', 'Tag the promoted image')])
        self.assertEqual(self.text.count('secrets.PACKAGES_PAT'), 1)
        last = self.doc['jobs']['apply']['steps'][-1]
        self.assertEqual((last['name'], last['if']), ('Create R', '${{ !inputs.dry_run }}'))
        runs = {
            job: spec['steps'][-1]['run'].split('\n')[1].split()[2]
            for job, spec in self.doc['jobs'].items()
            if job in ('apply', 'tag', 'move')
        }
        self.assertEqual(runs, {'apply': 'create', 'tag': 'tag', 'move': 'move'})

    def test_snapshot_and_preview_hold_no_secret_and_preview_no_token(self):
        for job in ('snapshot', 'preview'):
            self.assertNotIn('secrets.', json.dumps(self.doc['jobs'][job]))
        self.assertNotIn('GH_TOKEN', json.dumps(self.doc['jobs']['preview']))
        for job in ('snapshot', 'preview', 'apply', 'tag', 'move'):
            self.assertEqual(self.doc['jobs'][job]['permissions'], {'contents': 'read'})

    def test_notify_waits_for_every_job(self):
        jobs = set(self.doc['jobs']) - {'notify'}
        self.assertEqual(set(self.doc['jobs']['notify']['needs']), jobs)

    def test_runs_queue_per_repository_and_notify_globally_and_carry_the_repo_name(self):
        self.assertEqual(
            self.doc['concurrency'],
            {'group': 'promote-${{ inputs.repo }}', 'cancel-in-progress': False, 'queue': 'max'},
        )
        self.assertEqual(
            self.doc['jobs']['notify']['concurrency'],
            {'group': 'promote:notify', 'cancel-in-progress': False, 'queue': 'max'},
        )
        self.assertEqual(
            self.doc['run-name'],
            "Promote ${{ inputs.repo }}${{ inputs.dry_run && ' (dry run)' || '' }}",
        )


def cliff_bin() -> str:
    found = os.environ.get('CLIFF_BIN') or shutil.which('git-cliff') or ''
    return found if found and Path(found).exists() else ''


@unittest.skipUnless(cliff_bin(), 'needs git-cliff (CLIFF_BIN or PATH)')
class Preview(unittest.TestCase):
    def test_the_stable_release_steps_number_and_render_the_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            m = fx.commit(
                'feat: init',
                {
                    'go.mod': GO_MOD,
                    'main.go': 'package main\n',
                    'cliff.toml': (ROOT / 'configs/cliff-stable.toml').read_text(),
                },
            )
            fx.g('tag', 'v1.0.0')
            fx.g('checkout', '-q', '-b', 'dev')
            t = fx.commit('feat: add greeting (#7)', {'main.go': 'package main\n\n// hi\n'})
            fx.g('tag', 'v1.1.0-dev.1')
            fx.publish()
            summary = fx.work / 'summary.md'
            args = promote.parse_args(
                [
                    '--work-dir',
                    str(fx.work),
                    'preview',
                    '--repo',
                    'demo',
                    '--target',
                    t,
                    '--main',
                    m,
                ]
            )
            env = {'GITHUB_STEP_SUMMARY': str(summary), 'CLIFF_BIN': cliff_bin(), 'GH_TOKEN': 'x'}
            with (
                mock.patch.object(promote, 'CLONE_URL', fx.url()),
                mock.patch.dict(os.environ, env),
            ):
                rc = promote.cmd_preview(args)
            text = summary.read_text() if summary.exists() else ''
        self.assertEqual(rc, 0, text)
        self.assertIn('### root v1.1.0', text)
        self.assertIn('Promoted from `v1.1.0-dev.1`', text)
        self.assertIn('- Add greeting (#7)', text)
        self.assertIn('compare/v1.0.0...v1.1.0', text)


class RestTransport(unittest.TestCase):
    """gh_json and gh_send read and write through scripts/ghrest.py."""

    def test_reads_and_writes_go_over_rest_and_failures_are_gh_errors(self):
        seen = []

        def run(args, stdin=None, timeout=None):
            seen.append((list(args), stdin))
            if args[-1] == 'repos/cplieger/x':
                out = b'HTTP/2.0 200 OK\n\r\n{"default_branch": "dev"}'
            else:
                out = b'HTTP/2.0 422 Unprocessable\n\r\n{"message": "Reference already exists"}'
            return subprocess.CompletedProcess(args, 0 if b' 200 ' in out else 1, out, b'')

        with mock.patch.object(promote.ghrest.DEFAULT, 'run', run):
            self.assertEqual(promote.gh_json('repos/cplieger/x'), {'default_branch': 'dev'})
            with self.assertRaises(promote.GhError) as caught:
                promote.gh_send('POST', 'repos/cplieger/x/git/refs', {'ref': 'refs/heads/r'})
        self.assertIn('HTTP 422 Reference already exists', str(caught.exception))
        self.assertEqual(seen[0], (['api', '-i', 'repos/cplieger/x'], None))
        self.assertEqual(
            seen[1][0], ['api', '-i', '-X', 'POST', 'repos/cplieger/x/git/refs', '--input', '-']
        )
        self.assertEqual(json.loads(seen[1][1]), {'ref': 'refs/heads/r'})


if __name__ == '__main__':
    unittest.main()
