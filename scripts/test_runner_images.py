"""Tests for runner_images.py: the plan, and which dev builds a promotion reuses."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import runner_images as ri

PKG = 'ghcr.io/owner/app-runner'
SHA = 'c' * 40
# The four images the pool ships, each built on the one before it.
POOL = {
    'base': 'FROM ghcr.io/actions/actions-runner:2.330.0\n',
    'build': f'FROM {PKG}:base-dev\nRUN apt-get install -y build-essential\n',
    'browser': f'FROM {PKG}:build-dev\n',
    'docker': f'FROM {PKG}:build-dev\nCOPY images/docker/run /usr/local/bin/run\n',
}


class Plan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, images: dict[str, str]) -> None:
        for name, text in images.items():
            (self.root / 'images' / name).mkdir(parents=True, exist_ok=True)
            (self.root / 'images' / name / 'Dockerfile').write_text(text)

    def run_plan(self, channel='dev', tag='v1.3.0-dev.4', licenses='{}', default=''):
        argv = [
            'plan',
            '--package',
            PKG,
            '--channel',
            channel,
            '--tag',
            tag,
            '--sha',
            SHA,
            '--repo',
            'owner/app',
            '--date',
            '2026-11-10',
            '--created',
            '2026-11-10T09:00:00Z',
            '--licenses',
            licenses,
            '--default-licenses',
            default,
        ]
        out, err = io.StringIO(), io.StringIO()
        cwd = Path.cwd()
        os.chdir(self.root)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ri.main(argv)
        finally:
            os.chdir(cwd)
        return rc, (json.loads(out.getvalue()) if rc == 0 else None), err.getvalue()

    def refused(self, images: dict[str, str]) -> str:
        self.write(images)
        rc, _, err = self.run_plan()
        self.assertEqual(rc, 1, err)
        self.assertTrue(err.startswith('::error::'), err)
        return err

    def test_a_repository_without_images_plans_nothing(self):
        self.assertEqual(self.run_plan()[:2], (0, []))
        (self.root / 'images' / 'notes').mkdir(parents=True)
        self.assertEqual(self.run_plan()[:2], (0, []))

    def test_each_image_builds_after_the_image_it_names(self):
        self.write(POOL)
        _, plan, _ = self.run_plan()
        self.assertEqual([i['name'] for i in plan], ['base', 'build', 'browser', 'docker'])
        self.assertEqual(
            {i['name']: i['parents'] for i in plan},
            {
                'base': {},
                'build': {f'{PKG}:base-dev': 'base'},
                'browser': {f'{PKG}:build-dev': 'build'},
                'docker': {f'{PKG}:build-dev': 'build'},
            },
        )
        self.assertEqual(plan[1]['dockerfile'], 'images/build/Dockerfile')

    def test_dev_tags_the_dev_version_and_moves_the_dev_tag(self):
        self.write(POOL)
        _, plan, _ = self.run_plan('dev', 'v1.3.0-dev.4')
        self.assertEqual(
            (plan[0]['version_tag'], plan[0]['moving_tag']), ('base-v1.3.0-dev.4', 'base-dev')
        )
        self.assertEqual(plan[0]['index_tag'], f'base-sha-{SHA}')

    def test_stable_tags_the_version_and_moves_the_bare_name(self):
        self.write(POOL)
        _, plan, _ = self.run_plan('stable', 'v1.3.0')
        self.assertEqual(
            (plan[2]['version_tag'], plan[2]['moving_tag']), ('browser-v1.3.0', 'browser')
        )
        self.assertEqual(plan[2]['index_tag'], f'browser-sha-{SHA}')

    def test_a_stable_moving_tag_names_a_sibling_too(self):
        self.write({'base': 'FROM scratch\n', 'build': f'FROM {PKG}:base\n'})
        _, plan, _ = self.run_plan()
        self.assertEqual(plan[1]['parents'], {f'{PKG}:base': 'base'})

    def test_build_args_follow_the_dockerfiles_own_args(self):
        self.write(
            {
                'base': 'FROM scratch\nARG PKG_REFRESH=static\nARG BUILD_VERSION\n',
                'build': f'FROM {PKG}:base-dev\nARG PKG_REFRESH_X=1\n',
            }
        )
        _, plan, _ = self.run_plan(tag='v1.3.0-dev.4')
        self.assertEqual(
            plan[0]['build_args'], ['PKG_REFRESH=2026-11-10', 'BUILD_VERSION=v1.3.0-dev.4']
        )
        self.assertEqual(plan[1]['build_args'], [])

    def test_labels_name_the_image_its_version_and_its_licences(self):
        self.write(POOL)
        _, plan, _ = self.run_plan(
            licenses='{"docker": "GPL-3.0-or-later AND Apache-2.0"}', default='GPL-3.0-or-later'
        )
        labels = {i['name']: dict(label.split('=', 1) for label in i['labels']) for i in plan}
        self.assertEqual(
            labels['docker'],
            {
                'org.opencontainers.image.created': '2026-11-10T09:00:00Z',
                'org.opencontainers.image.revision': SHA,
                'org.opencontainers.image.source': 'https://github.com/owner/app',
                'org.opencontainers.image.title': 'app-runner:docker',
                'org.opencontainers.image.version': 'docker-v1.3.0-dev.4',
                'org.opencontainers.image.licenses': 'GPL-3.0-or-later AND Apache-2.0',
            },
        )
        self.assertEqual(labels['base']['org.opencontainers.image.licenses'], 'GPL-3.0-or-later')

    def test_no_licence_sets_no_licence_label(self):
        self.write({'base': 'FROM scratch\n'})
        _, plan, _ = self.run_plan()
        self.assertFalse([label for label in plan[0]['labels'] if 'licenses' in label])

    def test_a_policy_entry_for_a_missing_image_warns(self):
        self.write({'base': 'FROM scratch\n'})
        rc, _, err = self.run_plan(licenses='{"gone": "MIT"}')
        self.assertEqual(rc, 0)
        self.assertIn('::warning::', err)
        self.assertIn('gone', err)

    def test_a_reference_is_found_in_copy_and_continued_lines_but_not_comments(self):
        self.write(
            {
                'base': 'FROM scratch\n',
                'tools': 'FROM scratch\n',
                'build': (
                    f'# FROM {PKG}:nope\n'
                    f'FROM {PKG}:base-dev\n'
                    'COPY \\\n'
                    f'  # was {PKG}:base-v1.0.0\n'
                    '\n'
                    f'  --from={PKG}:tools-dev /x /x\n'
                ),
            }
        )
        _, plan, _ = self.run_plan()
        self.assertEqual([i['name'] for i in plan], ['base', 'tools', 'build'])
        self.assertEqual(
            plan[2]['parents'], {f'{PKG}:base-dev': 'base', f'{PKG}:tools-dev': 'tools'}
        )

    def test_each_form_a_build_context_replaces_names_a_parent(self):
        forms = (
            f'FROM --platform=$BUILDPLATFORM {PKG}:base-dev AS tools\n',
            f'FROM scratch\nCOPY --link --from={PKG}:base-dev /x /x\n',
            f'FROM scratch\nRUN --mount=type=bind,from={PKG}:base-dev,target=/b true\n',
        )
        for i, text in enumerate(forms):
            with self.subTest(text=text):
                self.root = Path(self.tmp.name) / str(i)
                self.write({'base': 'FROM scratch\n', 'build': text})
                _, plan, err = self.run_plan()
                self.assertIsNotNone(plan, err)
                self.assertEqual(plan[1]['parents'], {f'{PKG}:base-dev': 'base'})

    def test_a_mention_no_build_context_replaces_is_refused(self):
        cases = (
            ('RUN', f'RUN docker pull {PKG}:base-dev'),
            ('ENV', f'ENV PARENT={PKG}:base-dev'),
            ('LABEL', f'LABEL parent={PKG}:base-dev'),
            ('RUN', f'RUN --mount=from={PKG}:base-dev,target=/b ls {PKG}:base-dev'),
        )
        for i, (keyword, line) in enumerate(cases):
            with self.subTest(line=line):
                self.root = Path(self.tmp.name) / str(i)
                err = self.refused({'base': 'FROM scratch\n', 'build': f'FROM scratch\n{line}\n'})
                self.assertIn(f'names {PKG}:base-dev in a {keyword} instruction', err)
                self.assertIn('does not rewrite', err)

    def test_a_longer_package_name_is_another_package(self):
        self.write({'base': f'FROM {PKG}2:base-dev\nFROM {PKG}/sub:base-dev\n'})
        _, plan, _ = self.run_plan()
        self.assertEqual(plan[0]['parents'], {})

    def test_names_must_be_lowercase_letters_and_digits(self):
        for i, bad in enumerate(('Base', 'my-image', 'a_b', 'x' * 33)):
            with self.subTest(bad=bad):
                self.root = Path(self.tmp.name) / str(i)
                self.assertIn(f'images/{bad}/Dockerfile', self.refused({bad: 'FROM scratch\n'}))

    def test_a_sibling_is_named_only_by_its_moving_tag(self):
        refs = (f'{PKG}:base-v1.2.3', PKG, f'{PKG}:base-dev@sha256:{"0" * 64}', f'{PKG}:nope')
        for i, ref in enumerate(refs):
            with self.subTest(ref=ref):
                self.root = Path(self.tmp.name) / str(i)
                err = self.refused({'base': 'FROM scratch\n', 'build': f'FROM {ref}\n'})
                self.assertIn(f'names {ref}', err)

    def test_an_image_cannot_name_itself(self):
        self.assertIn('the image itself', self.refused({'base': f'FROM {PKG}:base-dev\n'}))

    def test_a_cycle_is_refused(self):
        err = self.refused(
            {'a': f'FROM {PKG}:b-dev\n', 'b': f'FROM {PKG}:a\n', 'c': 'FROM scratch\n'}
        )
        self.assertIn('cycle: a on b, b on a', err)


# The token endpoint answers STUB_TOKEN_STATUS (curl exits STUB_TOKEN_EXIT). A
# manifest HEAD serves the sha256 of the tag, after a 503 attempt for STUB_RETRIED;
# STUB_MISSING answers 404, STUB_STATUS_FOR=<ref>=<code> that code, STUB_DOWN no answer.
CURL_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$STUB_LOG"
url=${*: -1}
case "$url" in
  https://ghcr.io/token*)
    [ -z "${STUB_TOKEN_EXIT:-}" ] || exit "$STUB_TOKEN_EXIT"
    while [ "$1" != -o ]; do shift; done
    printf '{"token":"tkn"}' >"$2"
    printf '%s' "${STUB_TOKEN_STATUS:-200}"
    ;;
  https://ghcr.io/v2/*/manifests/*)
    ref=${url##*/}
    [ "$ref" != "${STUB_DOWN:-}" ] || exit 7
    case ",${STUB_MISSING:-}," in *",$ref,"*) printf 'HTTP/2 404\r\n\r\n'; exit 0 ;; esac
    case "${STUB_STATUS_FOR:-}" in "$ref="*) printf 'HTTP/2 %s\r\n\r\n' "${STUB_STATUS_FOR#*=}"; exit 0 ;; esac
    [ "$ref" != "${STUB_RETRIED:-}" ] || printf 'HTTP/2 503\r\ndocker-content-digest: sha256:%s\r\n\r\n' "$(printf 0 | sha256sum | cut -d' ' -f1)"
    printf 'HTTP/2 200\r\ndocker-content-digest: sha256:%s\r\n\r\n' "$(printf %s "$ref" | sha256sum | cut -d' ' -f1)"
    ;;
  *) exit 64 ;;
esac
"""
# release-state.sh complete exits STUB_RC for the image STUB_RC_FOR names.
STATE_STUB = r"""printf '%s\n' "$*" >>"$STUB_LOG"
[ "$2" != "${STUB_RC_FOR:-}" ] || exit "$STUB_RC"
"""
SOURCE = 'a' * 40


def served(tag: str) -> str:
    return 'sha256:' + hashlib.sha256(tag.encode()).hexdigest()


class Promoted(unittest.TestCase):
    PACKAGE = 'owner/app-runner'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / 'bin').mkdir()
        (self.tmp / 'bin' / 'curl').write_text(CURL_STUB)
        (self.tmp / 'bin' / 'curl').chmod(0o755)
        # runner_images.py runs release-state.sh from its own directory.
        shutil.copy(Path(ri.__file__), self.tmp / 'runner_images.py')
        (self.tmp / 'release-state.sh').write_text(STATE_STUB)
        self.log = self.tmp / 'calls.log'

    def promoted(self, names=('base', 'build'), **env):
        r = subprocess.run(
            [
                sys.executable,
                str(self.tmp / 'runner_images.py'),
                'promoted',
                '--package',
                self.PACKAGE,
                '--source',
                SOURCE,
                '--names',
                json.dumps(names),
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                'PATH': f'{self.tmp / "bin"}:{os.environ["PATH"]}',
                'STUB_LOG': str(self.log),
                **env,
            },
            check=False,
        )
        return r.returncode, (json.loads(r.stdout) if r.returncode == 0 else None), r.stderr

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_every_complete_dev_build_of_the_commit_is_reused(self):
        rc, found, err = self.promoted()
        self.assertEqual(rc, 0, err)
        self.assertEqual(found, {n: served(f'{n}-sha-{SOURCE}') for n in ('base', 'build')})
        token, head, state, *_ = self.calls()
        self.assertTrue(
            token.endswith(f'https://ghcr.io/token?scope=repository:{self.PACKAGE}:pull')
        )
        self.assertIn(
            '-sSI --retry 3 --retry-all-errors -H Authorization: Bearer tkn '
            '-H Accept: application/vnd.oci.image.index.v1+json',
            head,
        )
        self.assertTrue(
            head.endswith(f'https://ghcr.io/v2/{self.PACKAGE}/manifests/base-sha-{SOURCE}'), head
        )
        self.assertEqual(
            state, f'complete ghcr.io/{self.PACKAGE}@{served(f"base-sha-{SOURCE}")} {SOURCE}'
        )

    def test_one_image_without_a_dev_build_sends_them_all_to_a_source_build(self):
        rc, found, err = self.promoted(STUB_MISSING=f'build-sha-{SOURCE}')
        self.assertEqual((rc, found), (0, {}))
        self.assertIn(f'::notice::ghcr.io/{self.PACKAGE} has no build-sha-{SOURCE}', err)

    def test_a_retried_read_takes_its_last_answer(self):
        rc, found, err = self.promoted(STUB_RETRIED=f'base-sha-{SOURCE}')
        self.assertEqual(rc, 0, err)
        self.assertEqual(found['base'], served(f'base-sha-{SOURCE}'))

    def test_a_dev_build_ghcr_cannot_answer_for_fails_instead_of_building(self):
        ref = f'build-sha-{SOURCE}'
        for env, want in (
            (
                {'STUB_STATUS_FOR': f'{ref}=503'},
                f'ghcr.io answered HTTP 503 for {self.PACKAGE}:{ref}',
            ),
            (
                {'STUB_STATUS_FOR': f'{ref}=401'},
                f'ghcr.io answered HTTP 401 for {self.PACKAGE}:{ref}',
            ),
            ({'STUB_DOWN': ref}, f'no answer from ghcr.io for {self.PACKAGE}:{ref}'),
        ):
            with self.subTest(env=env):
                rc, _, err = self.promoted(**env)
                self.assertEqual(rc, 1)
                self.assertIn(f'::error::{want}', err)

    def test_one_incomplete_dev_build_sends_them_all_to_a_source_build(self):
        ref = f'ghcr.io/{self.PACKAGE}@{served(f"build-sha-{SOURCE}")}'
        self.assertEqual(self.promoted(STUB_RC_FOR=ref, STUB_RC='3')[:2], (0, {}))

    def test_a_completeness_check_that_cannot_answer_fails(self):
        ref = f'ghcr.io/{self.PACKAGE}@{served(f"base-sha-{SOURCE}")}'
        rc, _, err = self.promoted(STUB_RC_FOR=ref, STUB_RC='1')
        self.assertEqual(rc, 1)
        self.assertIn(f'::error::cannot tell whether {ref} was signed and attested', err)

    def test_a_package_missing_or_private_builds_from_source_naming_both_causes(self):
        rc, found, err = self.promoted(STUB_TOKEN_STATUS='403')
        self.assertEqual((rc, found), (0, {}))
        self.assertIn('does not exist yet or is private', err)
        self.assertIn('makes a private package public in the package settings', err)
        self.assertEqual(len(self.calls()), 1)

    def test_a_token_endpoint_that_fails_fails_the_run(self):
        for env, want in (
            ({'STUB_TOKEN_STATUS': '500'}, 'answered HTTP 500'),
            ({'STUB_TOKEN_EXIT': '7'}, 'no answer from the ghcr.io token endpoint'),
        ):
            with self.subTest(env=env):
                rc, _, err = self.promoted(**env)
                self.assertEqual(rc, 1)
                self.assertIn(want, err)


if __name__ == '__main__':
    unittest.main()
