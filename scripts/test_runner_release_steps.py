"""Tests for the runner images' release steps: the per-platform build
(runner-images-build.sh), the index and the channel tags with their readback
(runner-images-publish.sh), the package summary (sbom-summary.sh), the Trivy
install (install-trivy.sh), the runner-publish job's and finalize's runner steps,
and when docker-release.yaml's jobs run with and without runner images."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import itertools
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import runner_images
import yaml

SCRIPTS = Path(__file__).resolve().parent
WORKFLOW = SCRIPTS.parent / '.github' / 'workflows' / 'docker-release.yaml'
PKG = 'ghcr.io/owner/app-runner'
BODY = '{"manifests":[]}'
DIGEST = 'sha256:' + hashlib.sha256(BODY.encode()).hexdigest()

# imagetools create writes DIGEST to the metadata file and logs its argv, or
# fails while the file STUB_FAILS counts down, for a create naming STUB_FAIL_TAG;
# imagetools inspect --raw serves BODY, or a stale body for the tag STALE_TAG and
# while the ref's file in STUB_LAGS (its / and : as _) counts down.
DOCKER_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$STUB_LOG"
[ "$1 $2" = "buildx imagetools" ] || exit 64
case "$3" in
  create)
    [ "$4" = --metadata-file ] || exit 64
    if [ -n "${STUB_FAIL_TAG:-}" ] && [[ " $* " == *" -t ${STUB_FAIL_TAG} "* ]]; then
      left=$(cat "$STUB_FAILS")
      if [ "$left" -gt 0 ]; then
        printf '%s\n' "$((left - 1))" >"$STUB_FAILS"
        exit 1
      fi
    fi
    printf '{"containerimage.descriptor":{"digest":"%s"}}' "$STUB_DIGEST" >"$5"
    ;;
  inspect)
    [ "$4" = --raw ] || exit 64
    lag=${STUB_LAGS:-/nonexistent}/${5//[\/:]/_}
    if [ -s "$lag" ] && [ "$(cat "$lag")" -gt 0 ]; then
      printf '%s\n' "$(($(cat "$lag") - 1))" >"$lag"
      printf '%s' stale
    elif [ "$5" = "${STALE_TAG:-}" ]; then printf '%s' stale; else printf '%s' "$STUB_BODY"; fi
    ;;
  *) exit 64 ;;
esac
"""


# buildx build logs its argv and writes the digest of "<--file> <--platform>"
# to the metadata file, or no digest for the Dockerfile STUB_NO_DIGEST names.
BUILD_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$STUB_LOG"
[ "$1 $2" = "buildx build" ] || exit 64
shift 2
meta='' file='' platform=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --metadata-file) meta=$2; shift 2 ;;
    --file) file=$2; shift 2 ;;
    --platform) platform=$2; shift 2 ;;
    *) shift ;;
  esac
done
[ "$file" != "${STUB_NO_DIGEST:-}" ] || { printf '{}' >"$meta"; exit 0; }
printf '{"containerimage.digest":"sha256:%s"}' \
  "$(printf '%s %s' "$file" "$platform" | sha256sum | cut -d' ' -f1)" >"$meta"
"""


def built(dockerfile: str, platform: str) -> str:
    return 'sha256:' + hashlib.sha256(f'{dockerfile} {platform}'.encode()).hexdigest()


def plan_entry(name: str) -> dict:
    return {
        'name': name,
        'index_tag': f'{name}-sha-{"c" * 40}',
        'version_tag': f'{name}-v1.3.0-dev.4',
        'moving_tag': f'{name}-dev',
    }


class Publish(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / 'bin').mkdir()
        stub = self.tmp / 'bin' / 'docker'
        stub.write_text(DOCKER_STUB)
        stub.chmod(0o755)
        self.digests = self.tmp / 'digests'
        self.digests.mkdir()
        self.log = self.tmp / 'docker.log'

    def run_publish(self, *argv, count='2', **env):
        e = {
            **os.environ,
            'PATH': f'{self.tmp / "bin"}:{os.environ["PATH"]}',
            'STUB_LOG': str(self.log),
            'STUB_DIGEST': DIGEST,
            'STUB_BODY': BODY,
            'PLAN': json.dumps([plan_entry('base'), plan_entry('build')]),
            'PACKAGE': PKG,
            'PLATFORM_COUNT': count,
            'REGISTRY_WAIT_SECONDS': '0',
            'READBACK_SLEEP': '0',
            **env,
        }
        return subprocess.run(
            ['bash', str(SCRIPTS / 'runner-images-publish.sh'), *argv],
            capture_output=True,
            text=True,
            env=e,
            check=False,
        )

    def index(self, image='build', **env):
        return self.run_publish('index', image, str(self.digests), **env)

    def tags(self, **env):
        return self.run_publish('tags', str(self.digests), **env)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_the_index_of_each_platform_digest_takes_the_commit_tag_alone(self):
        (self.digests / 'build.linux-amd64').write_text('sha256:' + 'a' * 64 + '\n')
        (self.digests / 'build.linux-arm64').write_text('sha256:' + 'b' * 64 + '\n')
        (self.digests / 'base.linux-amd64').write_text('sha256:' + 'f' * 64 + '\n')
        r = self.index()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f'{DIGEST}\n')
        create, *reads = self.calls()
        tag = plan_entry('build')['index_tag']
        self.assertEqual(
            create.split(' ', 5)[5],
            f'-t {PKG}:{tag} {PKG}@sha256:{"a" * 64} {PKG}@sha256:{"b" * 64}',
        )
        self.assertEqual(reads, [f'buildx imagetools inspect --raw {PKG}:{tag}'])

    def test_an_index_tag_serving_another_digest_fails(self):
        (self.digests / 'build.linux-amd64').write_text('sha256:' + 'a' * 64 + '\n')
        (self.digests / 'build.linux-arm64').write_text('sha256:' + 'b' * 64 + '\n')
        r = self.index(STALE_TAG=f'{PKG}:build-sha-{"c" * 40}')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, '')
        self.assertIn(f'::error::{PKG}:build-sha-{"c" * 40} does not serve {DIGEST}', r.stderr)

    def test_a_missing_platform_digest_is_refused_before_any_write(self):
        (self.digests / 'build.linux-amd64').write_text('sha256:' + 'a' * 64 + '\n')
        r = self.index()
        self.assertEqual(r.returncode, 1)
        self.assertIn('has 1 platform digests', r.stderr)
        self.assertEqual(self.calls(), [])

    def creates(self) -> list[str]:
        """Each create's tag and source, in call order."""
        return [
            c.split(' ', 5)[5] for c in self.calls() if c.startswith('buildx imagetools create')
        ]

    def failing(self, tag: str, times: int) -> dict:
        (self.tmp / 'fails').write_text(f'{times}\n')
        return {'STUB_FAIL_TAG': f'{PKG}:{tag}', 'STUB_FAILS': str(self.tmp / 'fails')}

    def test_every_version_tag_is_written_and_read_back_before_any_moving_tag(self):
        for name in ('base', 'build'):
            (self.digests / name).write_text(f'{DIGEST}\n')
        r = self.tags()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = [
            'create ' + c.split(' ', 5)[5] if c.startswith('buildx imagetools create') else c
            for c in self.calls()
        ]
        want = []
        for tags in (('base-v1.3.0-dev.4', 'build-v1.3.0-dev.4'), ('base-dev', 'build-dev')):
            want += [f'create -t {PKG}:{tag} {PKG}@{DIGEST}' for tag in tags]
            want += [f'buildx imagetools inspect --raw {PKG}:{tag}' for tag in tags]
        self.assertEqual(calls, want)

    def test_a_first_tag_that_spends_the_wait_leaves_the_moving_pass_its_own(self):
        for name in ('base', 'build'):
            (self.digests / name).write_text(f'{DIGEST}\n')
        lags = self.tmp / 'lags'
        lags.mkdir()
        # 13 waits (5, 10, 20, 40, eight of 60, then 45) spend the 600 s budget.
        (lags / f'{PKG}:base-v1.3.0-dev.4'.replace('/', '_').replace(':', '_')).write_text('13\n')
        for tag in ('base-dev', 'build-dev'):
            (lags / f'{PKG}:{tag}'.replace('/', '_').replace(':', '_')).write_text('1\n')
        r = self.tags(STUB_LAGS=str(lags), REGISTRY_WAIT_SECONDS='600')
        self.assertEqual(r.returncode, 0, r.stderr)
        reads = [
            c.rsplit(' ', 1)[1] for c in self.calls() if c.startswith('buildx imagetools inspect')
        ]
        self.assertEqual(reads.count(f'{PKG}:base-v1.3.0-dev.4'), 14)
        for tag in ('base-dev', 'build-dev'):
            self.assertEqual(reads.count(f'{PKG}:{tag}'), 2)
            self.assertIn(f'{PKG}:{tag} is {DIGEST}', r.stdout)

    def test_a_failed_version_write_moves_no_moving_tag(self):
        for name in ('base', 'build'):
            (self.digests / name).write_text(f'{DIGEST}\n')
        r = self.tags(**self.failing('build-v1.3.0-dev.4', 99))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'::error::imagetools could not write {PKG}:build-v1.3.0-dev.4', r.stderr)
        self.assertEqual(
            self.creates(),
            [f'-t {PKG}:base-v1.3.0-dev.4 {PKG}@{DIGEST}']
            + [f'-t {PKG}:build-v1.3.0-dev.4 {PKG}@{DIGEST}'] * 5,
        )

    def test_a_write_that_fails_once_is_retried(self):
        for name in ('base', 'build'):
            (self.digests / name).write_text(f'{DIGEST}\n')
        r = self.tags(**self.failing('base-dev', 1))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.creates(),
            [
                f'-t {PKG}:{tag} {PKG}@{DIGEST}'
                for tag in (
                    'base-v1.3.0-dev.4',
                    'build-v1.3.0-dev.4',
                    'base-dev',
                    'base-dev',
                    'build-dev',
                )
            ],
        )
        self.assertIn('::warning::imagetools_create attempt 1/5 failed', r.stderr)

    def test_a_missing_signed_digest_moves_no_tag(self):
        (self.digests / 'base').write_text(f'{DIGEST}\n')
        r = self.tags()
        self.assertEqual(r.returncode, 1)
        self.assertIn('no signed build of build', r.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_tag_that_would_change_the_digest_fails(self):
        for name in ('base', 'build'):
            (self.digests / name).write_text('sha256:' + 'd' * 64 + '\n')
        r = self.tags()
        self.assertEqual(r.returncode, 1)
        self.assertIn("tagging changed base's digest", r.stderr)

    def test_a_channel_tag_serving_another_digest_fails(self):
        for name in ('base', 'build'):
            (self.digests / name).write_text(f'{DIGEST}\n')
        r = self.tags(STALE_TAG=f'{PKG}:base-dev')
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'::error::{PKG}:base-dev does not serve {DIGEST}', r.stderr)


class Build(unittest.TestCase):
    PLATFORM = 'linux/arm64'
    ARCH = 'linux-arm64'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / 'bin').mkdir()
        stub = self.tmp / 'bin' / 'docker'
        stub.write_text(BUILD_STUB)
        stub.chmod(0o755)
        self.out = self.tmp / 'digests'
        self.log = self.tmp / 'docker.log'

    def plan(self, images: dict[str, str]) -> str:
        root = self.tmp / 'repo'
        for name, text in images.items():
            (root / 'images' / name).mkdir(parents=True)
            (root / 'images' / name / 'Dockerfile').write_text(text)
        argv = ['plan', '--package', PKG, '--channel', 'dev', '--tag', 'v1.3.0-dev.4']
        argv += ['--sha', 'c' * 40, '--repo', 'owner/app', '--date', '2026-11-10']
        argv += ['--created', '2026-11-10T09:00:00Z']
        out, cwd = io.StringIO(), Path.cwd()
        os.chdir(root)
        try:
            with contextlib.redirect_stdout(out):
                self.assertEqual(runner_images.main(argv), 0)
        finally:
            os.chdir(cwd)
        return out.getvalue()

    def run_build(self, plan: str, **env):
        e = {
            **os.environ,
            'PATH': f'{self.tmp / "bin"}:{os.environ["PATH"]}',
            'STUB_LOG': str(self.log),
            'PLAN': plan,
            'PACKAGE': PKG,
            **env,
        }
        return subprocess.run(
            [
                'bash',
                str(SCRIPTS / 'runner-images-build.sh'),
                self.PLATFORM,
                self.ARCH,
                str(self.out),
            ],
            capture_output=True,
            text=True,
            env=e,
            check=False,
        )

    def builds(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_each_image_builds_on_this_runs_digest_of_the_sibling_it_names(self):
        plan = self.plan(
            {
                'base': 'FROM scratch\nARG PKG_REFRESH=static\n',
                'build': f'FROM {PKG}:base-dev\n',
            }
        )
        r = self.run_build(plan)
        self.assertEqual(r.returncode, 0, r.stderr)
        base, build = self.builds()
        base_digest = built('images/base/Dockerfile', self.PLATFORM)
        self.assertEqual((self.out / f'base.{self.ARCH}').read_text(), f'{base_digest}\n')
        self.assertEqual(
            (self.out / f'build.{self.ARCH}').read_text(),
            built('images/build/Dockerfile', self.PLATFORM) + '\n',
        )
        self.assertIn('--file images/base/Dockerfile --platform linux/arm64', base)
        self.assertIn(
            f'--output type=image,name={PKG},push-by-digest=true,name-canonical=true,push=true',
            base,
        )
        self.assertIn('--build-arg PKG_REFRESH=2026-11-10', base)
        self.assertIn('--label org.opencontainers.image.version=base-v1.3.0-dev.4', base)
        self.assertNotIn('--build-context', base)
        self.assertIn(f'--build-context {PKG}:base-dev=docker-image://{PKG}@{base_digest}', build)
        self.assertTrue(base.endswith(' .') and build.endswith(' .'))

    def test_an_image_whose_sibling_has_no_build_here_is_refused(self):
        plan = json.loads(self.plan({'base': 'FROM scratch\n', 'build': f'FROM {PKG}:base-dev\n'}))
        r = self.run_build(json.dumps(plan[::-1]))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'build names {PKG}:base-dev, but base has no {self.ARCH} build', r.stdout)
        self.assertEqual(self.builds(), [])

    def test_a_build_reporting_no_digest_fails_and_writes_no_digest_file(self):
        plan = self.plan({'base': 'FROM scratch\n', 'build': f'FROM {PKG}:base-dev\n'})
        r = self.run_build(plan, STUB_NO_DIGEST='images/build/Dockerfile')
        self.assertEqual(r.returncode, 1)
        self.assertIn('buildx reported no digest for build (linux/arm64)', r.stdout)
        self.assertTrue((self.out / f'base.{self.ARCH}').is_file())
        self.assertFalse((self.out / f'build.{self.ARCH}').exists())


# gh api answers the tag's commit (STUB_TAG_SHA, none when empty) and the
# compare status of this run's commit against it (STUB_STATUS, fails when empty).
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$STUB_LOG"
case "$2" in
  repos/owner/app/git/ref/tags/v1.3.0) [ -n "$STUB_TAG_SHA" ] && printf '%s\n' "$STUB_TAG_SHA" ;;
  "repos/owner/app/compare/${GITHUB_SHA}...${STUB_TAG_SHA}") [ -n "$STUB_STATUS" ] && printf '%s\n' "$STUB_STATUS" ;;
  *) exit 64 ;;
esac
"""


class VersionOwner(unittest.TestCase):
    def owner(self, tag_sha: str, status: str = ''):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / 'gh').write_text(GH_STUB)
            (t / 'gh').chmod(0o755)
            return subprocess.run(
                ['bash', str(SCRIPTS / 'version-owner.sh'), 'v1.3.0'],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    'PATH': f'{t}:{os.environ["PATH"]}',
                    'STUB_LOG': str(t / 'log'),
                    'STUB_TAG_SHA': tag_sha,
                    'STUB_STATUS': status,
                    'GITHUB_REPOSITORY': 'owner/app',
                    'GITHUB_SHA': 'c' * 40,
                },
                check=False,
            )

    def test_an_absent_equal_or_ancestor_tag_publishes(self):
        for tag_sha, status in (('', ''), ('c' * 40, ''), ('a' * 40, 'behind')):
            with self.subTest(tag_sha=tag_sha, status=status):
                r = self.owner(tag_sha, status)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn('::error::', r.stdout)

    def test_a_tag_this_commit_does_not_descend_from_is_refused(self):
        for status in ('ahead', 'diverged', ''):
            with self.subTest(status=status):
                r = self.owner('a' * 40, status)
                self.assertEqual(r.returncode, 1)
                self.assertIn(
                    f'::error::refusing to publish v1.3.0: its tag names {"a" * 40} and this run built {"c" * 40}',
                    r.stdout,
                )
                self.assertIn(f'compare status: {status or "unknown"}', r.stdout)


class Summary(unittest.TestCase):
    def summary(self, doc: dict, *title: str) -> list[str]:
        with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
            json.dump(doc, f)
            f.flush()
            r = subprocess.run(
                ['bash', str(SCRIPTS / 'sbom-summary.sh'), f.name, *title],
                capture_output=True,
                text=True,
                check=True,
            )
        return r.stdout.splitlines()

    def test_the_table_leaves_out_the_described_image_and_repeats(self):
        doc = {
            'documentDescribes': ['SPDXRef-a'],
            'relationships': [
                {
                    'relationshipType': 'DESCRIBES',
                    'spdxElementId': 'SPDXRef-DOCUMENT',
                    'relatedSpdxElement': 'SPDXRef-b',
                }
            ],
            'packages': [
                {'SPDXID': 'SPDXRef-a', 'name': 'image', 'versionInfo': '1'},
                {'SPDXID': 'SPDXRef-b', 'name': 'other-root', 'versionInfo': '1'},
                {'SPDXID': 'SPDXRef-z', 'name': 'zlib', 'versionInfo': '1.3'},
                {'SPDXID': 'SPDXRef-g', 'name': 'git'},
                {'SPDXID': 'SPDXRef-z2', 'name': 'zlib', 'versionInfo': '1.3'},
            ],
        }
        self.assertEqual(
            self.summary(doc, 'Shipped package versions: base'),
            [
                '## Shipped package versions: base',
                '',
                '| Package | Version |',
                '|---|---|',
                '| git | (none) |',
                '| zlib | 1.3 |',
            ],
        )

    def test_the_table_stops_at_400_rows_and_counts_the_rest(self):
        doc = {
            'packages': [
                {'SPDXID': f'SPDXRef-{i}', 'name': f'p{i:04d}', 'versionInfo': '1'}
                for i in range(403)
            ]
        }
        lines = self.summary(doc)
        self.assertEqual(lines[0], '## Shipped package versions')
        self.assertEqual(len(lines), 4 + 400 + 2)
        self.assertEqual(lines[-1], '(+ 3 more in the SBOM attested to the image)')


class InstallTrivy(unittest.TestCase):
    def test_the_tarball_is_verified_before_it_is_unpacked_onto_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / 'bin').mkdir()
            (t / 'pkg').mkdir()
            (t / 'pkg' / 'trivy').write_text('#!/bin/sh\n')
            tarball = t / 'trivy.tgz'
            subprocess.run(['tar', 'czf', str(tarball), '-C', str(t / 'pkg'), 'trivy'], check=True)
            (t / 'bin' / 'curl').write_text(
                '#!/usr/bin/env bash\nset -euo pipefail\n'
                'printf "curl %s\\n" "${@: -1}" >>"$STUB_LOG"\n'
                'while [ "$1" != -o ]; do shift; done\n'
                f'cp "{tarball}" "$2"\n'
            )
            (t / 'bin' / 'cosign').write_text(
                '#!/usr/bin/env bash\nprintf "cosign %s\\n" "$*" >>"$STUB_LOG"\n'
                '[ -z "${COSIGN_FAILS:-}" ]\n'
            )
            for stub in ('curl', 'cosign'):
                (t / 'bin' / stub).chmod(0o755)

            def run(**env):
                rt = t / 'rt'
                subprocess.run(['rm', '-rf', str(rt)], check=True)
                rt.mkdir()
                (t / 'log').write_text('')
                (t / 'path').write_text('')
                e = {
                    **os.environ,
                    'PATH': f'{t / "bin"}:{os.environ["PATH"]}',
                    'STUB_LOG': str(t / 'log'),
                    'TRIVY_VERSION': 'v0.76.2',
                    'RUNNER_TEMP': str(rt),
                    'GITHUB_PATH': str(t / 'path'),
                    **env,
                }
                r = subprocess.run(
                    ['bash', str(SCRIPTS / 'install-trivy.sh')],
                    capture_output=True,
                    text=True,
                    env=e,
                    check=False,
                )
                return r, rt

            r, rt = run()
            self.assertEqual(r.returncode, 0, r.stderr)
            base = 'https://github.com/aquasecurity/trivy/releases/download/v0.76.2'
            log = (t / 'log').read_text().splitlines()
            self.assertEqual(
                log[:2],
                [
                    f'curl {base}/trivy_0.76.2_Linux-64bit.tar.gz',
                    f'curl {base}/trivy_0.76.2_Linux-64bit.tar.gz.sigstore.json',
                ],
            )
            self.assertIn(
                '--certificate-identity https://github.com/aquasecurity/trivy/.github/workflows/'
                'reusable-release.yaml@refs/tags/v0.76.2',
                log[2],
            )
            self.assertTrue((rt / 'trivy-bin' / 'trivy').is_file())
            self.assertEqual((t / 'path').read_text(), f'{rt / "trivy-bin"}\n')

            r, rt = run(COSIGN_FAILS='1')
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse((rt / 'trivy-bin').exists())
            self.assertEqual((t / 'path').read_text(), '')


def steps(job: str) -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())['jobs'][job]['steps']


def step(job: str, name: str) -> dict:
    return next(s for s in steps(job) if s.get('name') == name)


class RunnerPublishJob(unittest.TestCase):
    def setUp(self):
        self.job = yaml.safe_load(WORKFLOW.read_text())['jobs']['runner-publish']
        self.names = [s.get('name') for s in self.job['steps']]

    def test_it_can_sign_and_upload_a_scan(self):
        self.assertEqual(
            self.job['permissions'],
            {
                'contents': 'read',
                'packages': 'write',
                'id-token': 'write',
                'security-events': 'write',
            },
        )

    def test_only_a_build_generates_signs_and_attests(self):
        built = "${{ env.PROMOTED_DIGEST == '' }}"
        for name in (
            'Generate SBOM (from image)',
            'Sign the image (recursive, covering each platform)',
            'Attest SBOM to GHCR',
        ):
            self.assertEqual(step('runner-publish', name).get('if'), built, name)
        self.assertEqual(
            step('runner-publish', "Read the promoted build's SBOM").get('if'),
            "${{ env.PROMOTED_DIGEST != '' }}",
        )

    def test_the_steps_run_in_finalize_order_and_hand_on_only_a_signed_digest(self):
        order = [
            "Verify the promoted build's signature and SBOM",
            'Publish the build on GHCR',
            'Install Trivy',
            'Trivy scan published image',
            'Upload Trivy SARIF',
            'Generate SBOM (from image)',
            "Read the promoted build's SBOM",
            'Shipped package versions summary',
            'Sign the image (recursive, covering each platform)',
            'Attest SBOM to GHCR',
            'Hand the signed digest on',
            'Upload the signed digest',
        ]
        self.assertEqual([n for n in self.names if n in order], order)
        self.assertEqual(self.names[-2:], order[-2:])
        upload = step('runner-publish', 'Upload the signed digest')['with']
        self.assertEqual(upload['name'], 'runner-index-${{ matrix.image }}')

    def test_no_channel_tag_and_no_version_gate_in_a_leg(self):
        runs = ' '.join(s.get('run', '') for s in self.job['steps'])
        self.assertNotIn('version-owner.sh', runs)
        self.assertNotIn('runner-images-publish.sh" tags', runs)
        self.assertIn('runner-images-publish.sh" index', runs)

    def test_the_scan_matches_the_app_images_and_files_under_its_own_category(self):
        app = step('finalize', 'Trivy scan published image')['with']
        ours = step('runner-publish', 'Trivy scan published image')['with']
        self.assertEqual(
            {k: v for k, v in ours.items() if k != 'image-ref'},
            {k: v for k, v in app.items() if k != 'image-ref'},
        )
        self.assertEqual(
            ours['image-ref'],
            '${{ env.REGISTRY }}/${{ env.RUNNER_IMAGE_NAME }}@${{ steps.publish.outputs.digest }}',
        )
        self.assertEqual(
            step('runner-publish', 'Upload Trivy SARIF')['with']['category'],
            'trivy-image-runner-${{ matrix.image }}',
        )
        self.assertEqual(
            step('runner-publish', 'Trivy scan published image')['uses'],
            step('finalize', 'Trivy scan published image')['uses'],
        )

    def test_the_sbom_is_the_app_images_kind_without_a_shared_artifact_name(self):
        app = step('finalize', 'Generate SBOM (from image)')
        ours = step('runner-publish', 'Generate SBOM (from image)')
        self.assertEqual(ours['uses'], app['uses'])
        self.assertEqual(ours['with']['upload-artifact'], False)
        for key in ('format', 'output-file', 'upload-release-assets', 'config'):
            self.assertEqual(ours['with'][key], app['with'][key], key)

    def test_signing_uses_the_app_images_retried_commands(self):
        sign = step('runner-publish', 'Sign the image (recursive, covering each platform)')['run']
        attest = step('runner-publish', 'Attest SBOM to GHCR')['run']
        self.assertIn('retry cosign sign --yes --recursive "$IMAGE_REF"', sign)
        self.assertIn(
            'retry cosign attest --yes --predicate sbom.spdx.json --type spdxjson "$IMAGE_REF"',
            attest,
        )

    def test_both_jobs_install_trivy_and_summarise_through_the_shared_scripts(self):
        for job in ('finalize', 'runner-publish'):
            self.assertIn('install-trivy.sh', step(job, 'Install Trivy')['run'])
            self.assertIn('sbom-summary.sh', step(job, 'Shipped package versions summary')['run'])


class FinalizeRunnerTags(unittest.TestCase):
    GUARD = "${{ env.PUBLISH == 'true' && needs.prepare.outputs.runner-images != '' }}"
    WRITE = "Publish the runner images' channel tags on GHCR"

    def setUp(self):
        self.names = [s.get('name') for s in steps('finalize')]

    def test_the_runner_tags_move_right_after_the_app_images_and_after_every_refusal(self):
        at = self.names.index(self.WRITE)
        self.assertEqual(at, self.names.index('Publish channel tags on GHCR') + 1)
        for refusal in (
            'Version ownership gate',
            "Verify the promoted build's signature and SBOM",
            "Read the promoted build's SBOM",
            'Download the signed runner digests',
        ):
            self.assertLess(self.names.index(refusal), at, refusal)
        self.assertEqual(
            sum('runner-images-publish.sh" tags' in s.get('run', '') for s in steps('finalize')), 1
        )

    def test_only_a_publishing_run_with_runner_images_downloads_and_tags_them(self):
        download = step('finalize', 'Download the signed runner digests')
        self.assertEqual(download['if'], self.GUARD)
        self.assertEqual(download['with']['pattern'], 'runner-index-*')
        self.assertEqual(download['with']['path'], '${{ runner.temp }}/runner-index')
        write = step('finalize', self.WRITE)
        self.assertEqual(write['if'], self.GUARD)
        self.assertIn('runner-images-publish.sh" tags "$RUNNER_TEMP/runner-index"', write['run'])
        self.assertEqual(write['env']['PLAN'], '${{ needs.prepare.outputs.runner-images }}')

    def test_no_other_job_writes_a_runner_channel_tag(self):
        jobs = yaml.safe_load(WORKFLOW.read_text())['jobs']
        writers = [
            name
            for name, job in jobs.items()
            for s in job.get('steps') or []
            if 'runner-images-publish.sh" tags' in s.get('run', '')
        ]
        self.assertEqual(writers, ['finalize'])

    def test_finalize_takes_longer_only_with_runner_images(self):
        e = yaml.safe_load(WORKFLOW.read_text())['jobs']['finalize']['timeout-minutes']
        m = re.fullmatch(
            r"\$\{\{ needs\.prepare\.outputs\.runner-images != '' && (\d+) \|\| 15 \}\}", e
        )
        self.assertIsNotNone(m, e)
        # The version and moving passes each wait up to retry.sh's 600 s, beside the 15 minutes.
        self.assertGreaterEqual(int(m.group(1)), 15 + 2 * 10)


@functools.cache
def condition(job: str) -> str:
    e = ' '.join(str(yaml.safe_load(WORKFLOW.read_text())['jobs'][job]['if']).split())
    return e.removeprefix('${{').removesuffix('}}')


def runs(job: str, results=None, outputs=None, cancelled=False) -> bool:
    """A job's `if`, evaluated: a need missing from results succeeded, an output
    missing from outputs ({job: {name: value}}) is empty."""
    e = condition(job)
    e = re.sub(
        r'needs\.([\w-]+)\.outputs\.([\w-]+)',
        lambda m: repr((outputs or {}).get(m.group(1), {}).get(m.group(2), '')),
        e,
    )
    e = re.sub(
        r'needs\.([\w-]+)\.result', lambda m: repr((results or {}).get(m.group(1), 'success')), e
    )
    e = e.replace('!cancelled()', repr(not cancelled)).replace('always()', 'True')
    return eval(e.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}}, {})


RESULTS = ('success', 'failure', 'cancelled', 'skipped')
IMAGES = '[{"name":"base"}]'


class JobGraph(unittest.TestCase):
    def prepare(self, images='', promoted='', publish='true', rebuild_only='') -> dict:
        return {
            'prepare': {
                'publish': publish,
                'runner-images': images,
                'runner-names': '["base"]' if images else '',
                'runner-promoted': promoted,
                'rebuild_only': rebuild_only,
            }
        }

    def test_without_runner_images_no_runner_job_runs(self):
        for publish, promoted, cancelled in itertools.product(
            ('true', 'false'), ('', '{}'), (False, True)
        ):
            outputs = self.prepare(promoted=promoted, publish=publish)
            for job in ('runner-build', 'runner-publish'):
                with self.subTest(job=job, publish=publish, promoted=promoted, cancelled=cancelled):
                    self.assertFalse(runs(job, {'runner-build': 'skipped'}, outputs, cancelled))

    def test_without_runner_images_finalize_never_depends_on_the_runner_jobs(self):
        outputs = self.prepare()
        for gate, build, test, cancelled in itertools.product(
            RESULTS, RESULTS, RESULTS, (False, True)
        ):
            base = {'rebuild-gate': gate, 'build': build, 'image-test': test}
            alone = runs('finalize', {**base, 'runner-publish': 'skipped'}, outputs, cancelled)
            for rp in RESULTS:
                with self.subTest(**base, cancelled=cancelled, runner_publish=rp):
                    self.assertEqual(
                        runs('finalize', {**base, 'runner-publish': rp}, outputs, cancelled), alone
                    )

    def test_the_rebuild_gate_runs_only_for_a_rebuild_only_range_without_runner_images(self):
        self.assertTrue(runs('rebuild-gate', outputs=self.prepare(rebuild_only='true')))
        self.assertFalse(runs('rebuild-gate', outputs=self.prepare()))
        self.assertFalse(runs('rebuild-gate', outputs=self.prepare(IMAGES, rebuild_only='true')))

    def test_the_runner_images_build_unless_a_promotion_reuses_their_dev_builds(self):
        skipped_gate = {'rebuild-gate': 'skipped'}
        self.assertTrue(runs('runner-build', skipped_gate, self.prepare(IMAGES)))
        self.assertFalse(
            runs('runner-build', skipped_gate, self.prepare(IMAGES, '{"base":"sha256:x"}'))
        )
        self.assertFalse(runs('runner-build', skipped_gate, self.prepare(IMAGES, publish='false')))
        self.assertFalse(runs('runner-build', skipped_gate, self.prepare(IMAGES), cancelled=True))
        self.assertFalse(
            runs('runner-build', {**skipped_gate, 'prepare': 'failure'}, self.prepare(IMAGES))
        )

    def test_the_runner_images_publish_this_runs_build_or_the_promoted_dev_builds(self):
        promoted = self.prepare(IMAGES, '{"base":"sha256:x"}')
        for rb, outputs, want in (
            ('success', self.prepare(IMAGES), True),
            ('skipped', promoted, True),
            ('skipped', self.prepare(IMAGES), False),
            ('failure', self.prepare(IMAGES), False),
            ('cancelled', self.prepare(IMAGES), False),
        ):
            with self.subTest(runner_build=rb, promoted=outputs['prepare']['runner-promoted']):
                self.assertEqual(runs('runner-publish', {'runner-build': rb}, outputs), want)
        self.assertFalse(
            runs('runner-publish', {'runner-build': 'skipped'}, promoted, cancelled=True)
        )

    def test_with_runner_images_finalize_waits_for_every_signed_runner_image(self):
        outputs = self.prepare(IMAGES)
        for rp in RESULTS:
            with self.subTest(runner_publish=rp):
                self.assertEqual(runs('finalize', {'runner-publish': rp}, outputs), rp == 'success')

    def test_the_channel_tags_wait_for_every_signed_image_and_the_app_images_tests(self):
        outputs = self.prepare(IMAGES)
        self.assertTrue(runs('finalize', {}, outputs))
        promotion = {'build': 'skipped', 'image-test': 'skipped', 'runner-build': 'skipped'}
        self.assertTrue(runs('finalize', promotion, outputs))
        for results, cancelled in (
            ({'image-test': 'failure'}, False),
            ({'image-test': 'cancelled'}, True),
            ({'runner-publish': 'failure'}, False),
            ({'runner-publish': 'cancelled'}, True),
            ({'runner-publish': 'skipped'}, False),
            ({'build': 'failure', 'image-test': 'skipped'}, False),
            ({'build': 'cancelled', 'image-test': 'skipped'}, True),
            ({'prepare': 'failure'}, False),
        ):
            with self.subTest(results=results, cancelled=cancelled):
                self.assertFalse(runs('finalize', results, outputs, cancelled))


if __name__ == '__main__':
    unittest.main()
