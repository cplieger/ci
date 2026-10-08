"""backfill-release-sbom-sigs.yaml's signing step, run against stub gh and cosign."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'backfill-release-sbom-sigs.yaml'

# REST answers from STUB_RELEASES ({repo: [release]}); an asset is its JSON metadata
# unless the read asks for application/octet-stream, as GitHub answers.
GH_STUB = r"""#!/usr/bin/env python3
import json, os, sys, urllib.parse
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
releases = json.load(open(os.environ['STUB_RELEASES']))
if args[:2] == ['release', 'upload']:
    sys.exit(0)
if args[0] != 'api':
    sys.exit(f'gh stub: unexpected {args}')
path = [a for a in args[1:] if a.startswith('repos/')][0]
where, _, query = path.partition('?')
repo = where.split('/')[2]
if where.endswith('/releases'):
    rows = releases.get(repo)
    if rows is None:
        sys.exit('gh: Not Found (HTTP 404)')
    q = dict(urllib.parse.parse_qsl(query))
    per = int(q['per_page'])
    pages = [rows[i : i + per] for i in range(0, len(rows), per)] or [[]]
    out = []
    for page in pages if '--paginate' in args else pages[:1]:
        out += page
    jq = args[args.index('--jq') + 1]
    sys.stdout.write(__import__('subprocess').run(['jq', '-r', jq], input=json.dumps(out),
                     capture_output=True, text=True, check=True).stdout)
elif '/releases/assets/' in where:
    if 'Accept: application/octet-stream' in args:
        print('sbom bytes of ' + where.rsplit('/', 1)[1])
    else:
        print(json.dumps({'id': where.rsplit('/', 1)[1]}))
"""
COSIGN_STUB = """#!/bin/sh
echo "cosign $*" >>"$STUB_LOG"
echo "signed $(cat "$3")" >>"$STUB_LOG"
"""


def release(tag, *assets, immutable=False):
    return {
        'tag_name': tag,
        'immutable': immutable,
        'assets': [{'id': f'{tag}-{i}', 'name': n, 'size': 1} for i, n in enumerate(assets)],
    }


class SignStep(unittest.TestCase):
    def run_step(self, releases, repos='app', limit='10', dry_run='true'):
        doc = yaml.safe_load(WORKFLOW.read_text())
        (step,) = [
            s for s in doc['jobs']['backfill']['steps'] if s.get('name', '').startswith('Sign')
        ]
        with tempfile.TemporaryDirectory() as name:
            tmp = pathlib.Path(name)
            for tool, body in (('gh', GH_STUB), ('cosign', COSIGN_STUB)):
                (tmp / tool).write_text(body)
                (tmp / tool).chmod(0o755)
            (tmp / 'releases.json').write_text(json.dumps(releases))
            env = {
                **os.environ,
                'PATH': f'{tmp}{os.pathsep}{os.environ["PATH"]}',
                'STUB_LOG': str(tmp / 'log'),
                'STUB_RELEASES': str(tmp / 'releases.json'),
                'FLEET': 'app other',
                'INPUT_REPOS': repos,
                'INPUT_LIMIT': limit,
                'INPUT_DRY_RUN': dry_run,
            }
            proc = subprocess.run(
                ['bash', '-e', '-c', step['run']],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            log = (tmp / 'log').read_text().splitlines() if (tmp / 'log').exists() else []
            return proc, log

    def test_unsigned_sboms_are_signed_and_every_other_release_is_skipped(self):
        releases = {
            'app': [
                release('v1.2.0', 'sbom.spdx.json'),
                release('v1.1.0', 'sbom.spdx.json', 'sbom.spdx.json.sigstore.json'),
                release('v1.0.0', 'notes.txt'),
            ]
        }
        proc, log = self.run_step(releases, dry_run='false')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('v1.2.0: signed + uploaded sbom.spdx.json.sigstore.json', proc.stdout)
        self.assertIn('v1.1.0: already has a signature asset, skip', proc.stdout)
        self.assertIn('v1.0.0: no sbom.spdx.json asset, skip', proc.stdout)
        self.assertIn('Done. signed=1 skipped=2 dry_run=false', proc.stdout)
        calls = [json.loads(c) for c in log if c.startswith('[')]
        self.assertEqual(
            calls[0],
            ['api', 'repos/cplieger/app/releases?per_page=10', '--jq', calls[0][-1]],
        )
        self.assertIn(
            [
                'api',
                '-H',
                'Accept: application/octet-stream',
                'repos/cplieger/app/releases/assets/v1.2.0-0',
            ],
            calls,
        )
        self.assertEqual(
            [c[:3] for c in calls if c[0] == 'release'], [['release', 'upload', 'v1.2.0']]
        )
        signed = [c for c in log if c.startswith('cosign ')]
        self.assertEqual(len(signed), 1)
        self.assertFalse([c for c in calls if c[:2] in (['release', 'list'], ['release', 'view'])])

    def test_the_downloaded_bytes_are_the_asset_not_its_metadata(self):
        proc, log = self.run_step({'app': [release('v1.2.0', 'sbom.spdx.json')]}, dry_run='false')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (sign,) = [c for c in log if c.startswith('cosign ')]
        self.assertTrue(sign.split()[3].endswith('/app/v1.2.0/sbom.spdx.json'), sign)
        self.assertIn('signed sbom bytes of v1.2.0-0', log)

    def test_a_dry_run_signs_and_uploads_nothing(self):
        proc, log = self.run_step({'app': [release('v1.2.0', 'sbom.spdx.json')]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('v1.2.0: WOULD sign + upload', proc.stdout)
        self.assertFalse([c for c in log if c.startswith('cosign ') or '"upload"' in c])

    def test_the_limit_bounds_the_newest_releases_read(self):
        rows = [release(f'v1.0.{n}', 'notes.txt') for n in range(250, 0, -1)]
        for limit, pages, scanned in (('3', False, 3), ('150', True, 150)):
            with self.subTest(limit=limit):
                proc, log = self.run_step({'app': rows}, limit=limit)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f'skipped={scanned} ', proc.stdout)
                listing = json.loads(log[0])
                self.assertEqual('--paginate' in listing, pages)
                per_page = min(int(limit), 100)
                self.assertIn(f'releases?per_page={per_page}', ' '.join(listing))

    def test_a_limit_that_is_not_a_positive_integer_reads_nothing(self):
        for limit in ('', '0', 'ten', '-1'):
            with self.subTest(limit=limit):
                proc, log = self.run_step({'app': []}, limit=limit)
                self.assertEqual(proc.returncode, 1)
                self.assertIn('limit must be a positive integer', proc.stdout)
                self.assertEqual(log, [])

    def test_an_immutable_release_is_skipped_before_anything_is_read_or_signed(self):
        releases = {
            'app': [
                release('v1.3.0', 'sbom.spdx.json', immutable=True),
                release('v1.2.0', 'sbom.spdx.json'),
            ]
        }
        for dry_run in ('true', 'false'):
            with self.subTest(dry_run=dry_run):
                proc, log = self.run_step(releases, dry_run=dry_run)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn('v1.3.0: immutable release, assets cannot be added', proc.stdout)
                self.assertIn(f'Done. signed=1 skipped=1 dry_run={dry_run}', proc.stdout)
                touched = [c for c in log if 'v1.3.0' in c]
                self.assertEqual(touched, [], 'no download, signature or upload for it')

    def test_a_release_without_the_field_is_signed_as_a_mutable_one(self):
        rel = release('v1.2.0', 'sbom.spdx.json')
        del rel['immutable']
        proc, _ = self.run_step({'app': [rel]}, dry_run='false')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('v1.2.0: signed + uploaded', proc.stdout)

    def test_a_repo_without_releases_is_reported_and_the_run_goes_on(self):
        proc, _ = self.run_step({'other': [release('v2.0.0', 'notes.txt')]}, repos='all')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('no releases found', proc.stdout)
        self.assertIn('v2.0.0: no sbom.spdx.json asset, skip', proc.stdout)


if __name__ == '__main__':
    unittest.main()
