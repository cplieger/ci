from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dashboard_check_fixtures import (
    ACTION,
    CLASSIC,
    V2_STUB,
    run_cli,
    valid,
)


class CommandLine(unittest.TestCase):
    def write(self, tmp: str, name: str, doc: dict) -> str:
        path = pathlib.Path(tmp) / name
        path.write_text(json.dumps(doc))
        return str(path)

    def test_classic_file_exits_zero_with_a_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'grafana-dashboard.json', CLASSIC)
            code, out = run_cli(
                '--github', '--cue', '/nonexistent/cue', '--schema', 'v13.2.3=/x', path
            )
        self.assertEqual(code, 0, out)
        self.assertIn('::notice file=', out)
        self.assertIn('convert it to schema v2', out)

    def test_classic_file_after_a_v2_base_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'grafana-dashboard.json', CLASSIC)
            base = self.write(tmp, 'base.json', valid())
            code, out = run_cli(
                '--cue', '/nonexistent/cue', '--schema', 'v13.2.3=/x', '--base-file', base, path
            )
        self.assertEqual(code, 1, out)
        self.assertIn('regresses to the classic schema', out)

    def test_github_mode_prints_error_annotations(self):
        doc = valid()
        doc['metadata']['name'] = ''
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'grafana-dashboard.json', doc)
            code, out = run_cli(
                '--github', '--cue', '/nonexistent/cue', '--schema', 'v13.2.3=/x', path
            )
        self.assertEqual(code, 1, out)
        self.assertIn('::error file=', out)

    def test_a_cue_that_cannot_run_fails_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'grafana-dashboard.json', valid())
            schema = self.write(tmp, 'schema.cue', {})
            code, out = run_cli('--cue', '/nonexistent/cue', '--schema', f'v13.2.3={schema}', path)
        self.assertEqual(code, 1, out)
        self.assertIn('cue vet against v13.2.3 did not run', out)

    def test_unparseable_file_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'grafana-dashboard.json'
            path.write_text('{"uid": ')
            code, out = run_cli('--cue', '/nonexistent/cue', '--schema', 'v13.2.3=/x', str(path))
        self.assertEqual(code, 1, out)
        self.assertIn('not valid JSON', out)

    def test_unusable_base_copy_fails_closed(self):
        renamed = valid()
        renamed['metadata']['name'] = 'changed-name'
        for label, base_text in (
            ('invalid JSON', '{"metadata": '),
            ('neither shape', '{"title": "Job queue"}'),
            ('missing', None),
        ):
            with self.subTest(base=label), tempfile.TemporaryDirectory() as tmp:
                path = self.write(tmp, 'grafana-dashboard.json', renamed)
                base = pathlib.Path(tmp) / 'base.json'
                if base_text is not None:
                    base.write_text(base_text)
                code, out = run_cli(
                    '--cue',
                    '/nonexistent/cue',
                    '--schema',
                    'v13.2.3=/x',
                    '--base-file',
                    str(base),
                    path,
                )
                self.assertEqual(code, 1, out)
                self.assertIn('[identity] the base copy cannot be used', out)

    def test_incomplete_v2_base_copy_fails_closed(self):
        renamed = valid()
        renamed['metadata']['name'] = 'changed-name'
        malformed = {
            'wrong kind': dict(V2_STUB, kind='DashboardList', metadata={'name': 'changed-name'}),
            'no kind or spec': {
                'apiVersion': 'dashboard.grafana.app/v2',
                'metadata': {'name': 'changed-name'},
            },
            'spec not an object': dict(V2_STUB, metadata={'name': 'changed-name'}, spec=[]),
            'extra metadata': dict(
                V2_STUB, metadata={'name': 'changed-name', 'namespace': 'default'}
            ),
            'extra top-level key': dict(V2_STUB, metadata={'name': 'changed-name'}, status={}),
        }
        for label, base_doc in malformed.items():
            with self.subTest(base=label), tempfile.TemporaryDirectory() as tmp:
                path = self.write(tmp, 'grafana-dashboard.json', renamed)
                base = self.write(tmp, 'base.json', base_doc)
                code, out = run_cli(
                    '--cue',
                    '/nonexistent/cue',
                    '--schema',
                    'v13.2.3=/x',
                    '--base-file',
                    base,
                    path,
                )
                self.assertEqual(code, 1, out)
                self.assertIn('[identity] the base copy cannot be used', out)
                self.assertNotIn('Traceback', out)

    def test_refuses_one_base_for_several_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.write(tmp, 'a.json', CLASSIC)
            b = self.write(tmp, 'b.json', CLASSIC)
            with self.assertRaises(SystemExit) as raised:
                run_cli('--cue', 'cue', '--schema', 'v13.2.3=/x', '--base-file', a, a, b)
        self.assertEqual(raised.exception.code, 2)


class BaseFetch(unittest.TestCase):
    STUB = (
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$@" >> "$STUB_LOG"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in -o) out=$2; shift ;; esac\n'
        '  shift\n'
        'done\n'
        'printf "%s" "$STUB_BODY" > "$out"\n'
        'printf "%s" "$STUB_CODE"\n'
    )

    def fetch(
        self, ref: str, code: str = '200'
    ) -> tuple[subprocess.CompletedProcess, str, pathlib.Path]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub = tmp / 'bin' / 'curl'
        stub.parent.mkdir()
        stub.write_text(self.STUB)
        stub.chmod(0o755)
        log = tmp / 'curl.log'
        log.touch()
        out = tmp / 'base.json'
        env = {
            'PATH': f'{stub.parent}:/usr/bin:/bin',
            'STUB_LOG': str(log),
            'STUB_BODY': '{"uid": "x"}',
            'STUB_CODE': code,
            'TOKEN': 'token-value',
            'GITHUB_API_URL': 'https://api.github.com',
            'GITHUB_REPOSITORY': 'owner/repo',
        }
        result = subprocess.run(
            ['bash', str(ACTION / 'fetch-base.sh'), 'grafana-dashboard.json', ref, str(out)],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, log.read_text(), out

    def test_no_base_ref_fetches_nothing(self):
        result, log, out = self.fetch('')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log, '')
        self.assertFalse(out.exists())

    def test_all_zero_ref_fetches_nothing(self):
        result, log, out = self.fetch('0' * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log, '')
        self.assertFalse(out.exists())

    def test_found_base_is_written(self):
        result, log, out = self.fetch('abc123')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out.read_text(), '{"uid": "x"}')
        self.assertIn(
            'https://api.github.com/repos/owner/repo/contents/grafana-dashboard.json', log
        )
        self.assertIn('ref=abc123', log)
        self.assertIn('Authorization: Bearer token-value', log)
        self.assertIn('Accept: application/vnd.github.raw+json', log)

    def test_missing_base_is_no_base(self):
        result, _, out = self.fetch('abc123', '404')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(out.exists())

    def test_other_answer_fails_closed(self):
        result, _, out = self.fetch('abc123', '500')
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error', result.stdout + result.stderr)
        self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
