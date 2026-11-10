from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dashboard_check_fixtures import (
    CLASSIC,
    FIXTURE,
    ONE_DASHBOARD,
    ROOT,
    TWO_DASHBOARDS,
    V2_STUB,
)


class PublishGate(unittest.TestCase):
    # Logs each call's argv, then writes that argv as the exported manifest, so each
    # push has its own digest.
    STUB_ORAS = (
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$@" --- >> "$STUB_DIR/oras.args"\n'
        'all="$*"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in --export-manifest) out=$2; shift ;; esac\n'
        '  shift\n'
        'done\n'
        'printf "%s" "$all" > "$out"\n'
    )

    def body(self) -> str:
        workflow = yaml.safe_load((ROOT / '.github/workflows/docker-release.yaml').read_text())
        for step in workflow['jobs']['finalize']['steps']:
            if step.get('name') == 'Publish dashboard OCI artifact':
                return step['run']
        self.fail('no "Publish dashboard OCI artifact" step in the finalize job')
        return ''

    def run_step(
        self, files: dict[str, str], channel: str = 'stable'
    ) -> subprocess.CompletedProcess:
        self.tmp = tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = tmp / 'bin'
        bin_dir.mkdir()
        stubs = {
            'oras': self.STUB_ORAS,
            'cosign': '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$STUB_DIR/cosign.log"\n',
            'git': '#!/usr/bin/env bash\necho 2026-01-01T00:00:00Z\n',
        }
        for name, text in stubs.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        tools = tmp / 'tools'
        tools.mkdir()
        (tools / 'retry.sh').write_text('retry() { "$@"; }\n')
        shutil.copy(ROOT / 'scripts' / 'dashboards.sh', tools / 'dashboards.sh')
        work = tmp / 'work'
        work.mkdir()
        for name, content in files.items():
            (work / name).write_text(content)
        env = {
            'PATH': f'{bin_dir}:/usr/bin:/bin',
            'STUB_DIR': str(tmp),
            'RUNNER_TEMP': str(tmp),
            'CI_TOOLS': str(tools),
            'GITHUB_OUTPUT': str(tmp / 'output'),
            'GITHUB_STEP_SUMMARY': str(tmp / 'summary'),
            'GITHUB_SHA': 'a' * 40,
            'GITHUB_REPOSITORY': 'owner/app',
            'REGISTRY': 'ghcr.io',
            'IMAGE_NAME': 'owner/app',
            'CHANNEL': channel,
            'TAG': 'v1.2.3' if channel == 'stable' else 'v1.3.0-dev.4',
            'VERSION': 'v1.2.3',
            'MAJOR': 'v1',
        }
        return subprocess.run(
            ['bash', '-e', '-c', self.body()],
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

    def read(self, name: str) -> str:
        path = self.tmp / name
        return path.read_text() if path.exists() else ''

    def pushes(self) -> list[list[str]]:
        calls = self.read('oras.args').split('---\n')
        return [c.splitlines() for c in calls if c]

    def digest(self, package: str) -> str:
        return (
            'sha256:'
            + hashlib.sha256((self.tmp / f'{package}-manifest.json').read_bytes()).hexdigest()
        )

    def publish(self, content: str) -> tuple[subprocess.CompletedProcess, str]:
        result = self.run_step({'grafana-dashboard.json': content})
        return result, self.read('oras.args')

    def test_finalize_hands_every_published_dashboard_to_verify_publish(self):
        jobs = yaml.safe_load((ROOT / '.github/workflows/docker-release.yaml').read_text())['jobs']
        step = next(s for s in jobs['finalize']['steps'] if s.get('id') == 'dashboard-oci')
        self.assertEqual(step['name'], 'Publish dashboard OCI artifact')
        self.assertEqual(
            jobs['finalize']['outputs']['dashboards'],
            '${{ steps.dashboard-oci.outputs.published }}',
        )
        self.assertNotIn('dashboard-digest', jobs['finalize']['outputs'])
        self.assertEqual(
            jobs['verify-publish']['env']['DASHBOARDS'], '${{ needs.finalize.outputs.dashboards }}'
        )

    def test_one_file_publishes_as_it_did_before_the_list(self):
        result = self.run_step(
            {'grafana-dashboard.json': (ONE_DASHBOARD / 'grafana-dashboard.json').read_text()}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        [push] = self.pushes()
        digest = self.digest('dashboard')
        self.assertEqual(
            push[push.index('--export-manifest') + 1], f'{self.tmp}/dashboard-manifest.json'
        )
        self.assertEqual(
            push[-2:],
            ['ghcr.io/owner/app/dashboard:v1.2.3,v1', 'grafana-dashboard.json:application/json'],
        )
        self.assertEqual(
            self.read('cosign.log'), f'sign --yes ghcr.io/owner/app/dashboard@{digest}\n'
        )
        self.assertEqual(
            self.read('output'), f'published=[{{"package":"dashboard","digest":"{digest}"}}]\n'
        )
        self.assertEqual(
            self.read('summary'),
            '## Grafana dashboard\n\n'
            f'OCI artifact `ghcr.io/owner/app/dashboard:v1.2.3` (also `:v1`), digest `{digest}`, cosign-signed (keyless).\n\n'
            'Artifact type `application/vnd.grafana.dashboard.v2+json`.\n',
        )

    def test_a_second_file_publishes_as_its_own_package(self):
        files = {p.name: p.read_text() for p in TWO_DASHBOARDS.glob('grafana-dashboard*.json')}
        self.assertEqual(
            sorted(files), ['grafana-dashboard-messages.json', 'grafana-dashboard.json']
        )
        result = self.run_step(files)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [push[-2:] for push in self.pushes()],
            [
                [
                    'ghcr.io/owner/app/dashboard:v1.2.3,v1',
                    'grafana-dashboard.json:application/json',
                ],
                [
                    'ghcr.io/owner/app/dashboard-messages:v1.2.3,v1',
                    'grafana-dashboard-messages.json:application/json',
                ],
            ],
        )
        main, messages = self.digest('dashboard'), self.digest('dashboard-messages')
        self.assertNotEqual(main, messages)
        self.assertEqual(
            self.read('cosign.log').splitlines(),
            [
                f'sign --yes ghcr.io/owner/app/dashboard@{main}',
                f'sign --yes ghcr.io/owner/app/dashboard-messages@{messages}',
            ],
        )
        output = self.read('output')
        self.assertTrue(output.startswith('published='), output)
        self.assertEqual(
            json.loads(output.removeprefix('published=')),
            [
                {'package': 'dashboard', 'digest': main},
                {'package': 'dashboard-messages', 'digest': messages},
            ],
        )
        summary = self.read('summary')
        self.assertIn('## Grafana dashboard\n', summary)
        self.assertIn(
            '## Grafana dashboard `grafana-dashboard-messages.json`\n\n'
            f'OCI artifact `ghcr.io/owner/app/dashboard-messages:v1.2.3` (also `:v1`), digest `{messages}`',
            summary,
        )

    def test_the_dev_channel_tags_each_file_with_the_dev_version_only(self):
        result = self.run_step(
            {
                'grafana-dashboard.json': json.dumps(CLASSIC),
                'grafana-dashboard-logs.json': json.dumps(CLASSIC),
            },
            channel='dev',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [push[-2] for push in self.pushes()],
            [
                'ghcr.io/owner/app/dashboard:v1.3.0-dev.4',
                'ghcr.io/owner/app/dashboard-logs:v1.3.0-dev.4',
            ],
        )

    def test_a_bad_second_file_publishes_neither(self):
        result = self.run_step(
            {
                'grafana-dashboard.json': json.dumps(CLASSIC),
                'grafana-dashboard-messages.json': '{"title": "x"}',
            }
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(
            '::error file=grafana-dashboard-messages.json::grafana-dashboard-messages.json is neither',
            result.stdout,
        )
        self.assertNotIn('file=grafana-dashboard.json:', result.stdout)
        self.assertEqual(
            (self.read('oras.args'), self.read('cosign.log'), self.read('output')), ('', '', '')
        )

    def test_a_refused_suffix_publishes_nothing(self):
        result = self.run_step(
            {
                'grafana-dashboard.json': json.dumps(CLASSIC),
                'grafana-dashboard-Logs.json': json.dumps(CLASSIC),
            }
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error::grafana-dashboard-Logs.json cannot be published', result.stderr)
        self.assertEqual((self.read('oras.args'), self.read('output')), ('', ''))

    def test_classic_file_publishes_as_v1(self):
        result, args = self.publish(json.dumps(CLASSIC))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--artifact-type\napplication/vnd.grafana.dashboard.v1+json\n', args)

    def test_v2_resource_publishes_as_v2(self):
        result, args = self.publish(FIXTURE.read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--artifact-type\napplication/vnd.grafana.dashboard.v2+json\n', args)
        self.assertIn('grafana-dashboard.json:application/json', args)

    def test_neither_shape_is_refused(self):
        for content in (
            '{"title": "x"}',
            '{"apiVersion": "dashboard.grafana.app/v2beta1"}',
            '{"uid": ""}',
            '{"uid": 7}',
            '{"apiVersion": "v1", "uid": "app"}',
            json.dumps({**V2_STUB, 'kind': 'DashboardList'}),
            json.dumps({**V2_STUB, 'metadata': {'name': ''}}),
            json.dumps({**V2_STUB, 'metadata': {'name': 7}}),
            json.dumps({**V2_STUB, 'metadata': 'app'}),
            json.dumps({k: v for k, v in V2_STUB.items() if k != 'spec'}),
            json.dumps({**V2_STUB, 'spec': 'x'}),
            json.dumps({**V2_STUB, 'spec': []}),
            json.dumps({**V2_STUB, 'metadata': {'name': 'app', 'uid': 'app'}}),
            json.dumps({**V2_STUB, 'error': 'Dashboard not found'}),
            '[]',
            '{',
        ):
            with self.subTest(content=content):
                result, args = self.publish(content)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('::error', result.stdout + result.stderr)
                self.assertEqual(args, '')


if __name__ == '__main__':
    unittest.main()
