from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dashboard_check_fixtures import (
    CLASSIC,
    FIXTURE,
    ROOT,
    V2_STUB,
)


class PublishGate(unittest.TestCase):
    STUB_ORAS = (
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$@" > "$STUB_DIR/oras.args"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in --export-manifest) printf "{}" > "$2"; shift ;; esac\n'
        '  shift\n'
        'done\n'
    )

    def body(self) -> str:
        workflow = yaml.safe_load((ROOT / '.github/workflows/docker-release.yaml').read_text())
        for step in workflow['jobs']['finalize']['steps']:
            if step.get('name') == 'Publish dashboard OCI artifact':
                return step['run']
        self.fail('no "Publish dashboard OCI artifact" step in the finalize job')
        return ''

    def publish(self, content: str) -> tuple[subprocess.CompletedProcess, str]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = tmp / 'bin'
        bin_dir.mkdir()
        stubs = {
            'oras': self.STUB_ORAS,
            'cosign': '#!/usr/bin/env bash\nexit 0\n',
            'git': '#!/usr/bin/env bash\necho 2026-01-01T00:00:00Z\n',
        }
        for name, text in stubs.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        (tmp / 'retry.sh').write_text('retry() { "$@"; }\n')
        (tmp / 'grafana-dashboard.json').write_text(content)
        env = {
            'PATH': f'{bin_dir}:/usr/bin:/bin',
            'STUB_DIR': str(tmp),
            'RUNNER_TEMP': str(tmp),
            'CI_TOOLS': str(tmp),
            'GITHUB_OUTPUT': str(tmp / 'output'),
            'GITHUB_STEP_SUMMARY': str(tmp / 'summary'),
            'GITHUB_SHA': 'a' * 40,
            'GITHUB_REPOSITORY': 'owner/app',
            'CHANNEL': 'stable',
            'TAG': 'v1.2.3',
            'VERSION': 'v1.2.3',
            'MAJOR': 'v1',
            'DASHBOARD_REF': 'ghcr.io/owner/app/dashboard',
        }
        result = subprocess.run(
            ['bash', '-e', '-c', self.body()],
            cwd=tmp,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        args = (tmp / 'oras.args').read_text() if (tmp / 'oras.args').exists() else ''
        return result, args

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
