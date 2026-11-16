from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dashboard_check_fixtures import (
    ACTION,
    CLASSIC,
    V2_STUB,
    run_cli,
    valid,
)

CUE = os.environ.get('DASHBOARD_CHECK_CUE', '')
SCHEMAS = os.environ.get('DASHBOARD_CHECK_SCHEMAS', '')
PROMQL = os.environ.get('DASHBOARD_CHECK_PROMQL', '')


class CommandLine(unittest.TestCase):
    def write(self, tmp: str, name: str, doc: dict) -> str:
        path = pathlib.Path(tmp) / name
        path.write_text(json.dumps(doc))
        return str(path)

    def test_classic_file_exits_zero_with_a_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'grafana-dashboard.json', CLASSIC)
            code, out = run_cli(
                '--github',
                '--cue',
                '/nonexistent/cue',
                '--promql',
                '/nonexistent/promql-check',
                '--schema',
                'v13.2.3=/x',
                path,
            )
        self.assertEqual(code, 0, out)
        self.assertIn('::notice file=', out)
        self.assertIn('convert it to schema v2', out)

    def test_classic_file_after_a_v2_base_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'grafana-dashboard.json', CLASSIC)
            base = self.write(tmp, 'base.json', valid())
            code, out = run_cli(
                '--cue',
                '/nonexistent/cue',
                '--promql',
                '/nonexistent/promql-check',
                '--schema',
                'v13.2.3=/x',
                '--base-file',
                base,
                path,
            )
        self.assertEqual(code, 1, out)
        self.assertIn('regresses to the classic schema', out)

    def test_github_mode_prints_error_annotations(self):
        doc = valid()
        doc['metadata']['name'] = ''
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'grafana-dashboard.json', doc)
            code, out = run_cli(
                '--github',
                '--cue',
                '/nonexistent/cue',
                '--promql',
                '/nonexistent/promql-check',
                '--schema',
                'v13.2.3=/x',
                path,
            )
        self.assertEqual(code, 1, out)
        self.assertIn('::error file=', out)

    def test_a_cue_that_cannot_run_fails_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, 'grafana-dashboard.json', valid())
            schema = self.write(tmp, 'schema.cue', {})
            code, out = run_cli(
                '--cue',
                '/nonexistent/cue',
                '--promql',
                '/nonexistent/promql-check',
                '--schema',
                f'v13.2.3={schema}',
                path,
            )
        self.assertEqual(code, 1, out)
        self.assertIn('cue vet against v13.2.3 did not run', out)

    def test_unparseable_file_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'grafana-dashboard.json'
            path.write_text('{"uid": ')
            code, out = run_cli(
                '--cue',
                '/nonexistent/cue',
                '--promql',
                '/nonexistent/promql-check',
                '--schema',
                'v13.2.3=/x',
                str(path),
            )
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
                    '--promql',
                    '/nonexistent/promql-check',
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
                    '--promql',
                    '/nonexistent/promql-check',
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
                run_cli(
                    '--cue',
                    'cue',
                    '--promql',
                    'promql-check',
                    '--schema',
                    'v13.2.3=/x',
                    '--base-file',
                    a,
                    a,
                    b,
                )
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
        self,
        ref: str,
        code: str = '200',
        extra: dict[str, str] | None = None,
        cwd: pathlib.Path | None = None,
        stale: bool = False,
    ) -> tuple[subprocess.CompletedProcess, str, pathlib.Path]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub = tmp / 'bin' / 'curl'
        stub.parent.mkdir()
        stub.write_text(self.STUB)
        stub.chmod(0o755)
        log = tmp / 'curl.log'
        log.touch()
        out = tmp / 'base.json'
        if stale:
            out.write_text('stale')
        env = {
            'PATH': f'{stub.parent}:/usr/bin:/bin',
            'STUB_LOG': str(log),
            'STUB_BODY': '{"uid": "x"}',
            'STUB_CODE': code,
            'TOKEN': 'token-value',
            'GITHUB_API_URL': 'https://api.github.com',
            'GITHUB_REPOSITORY': 'owner/repo',
            'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_CONFIG_NOSYSTEM': '1',
            **(extra or {}),
        }
        result = subprocess.run(
            ['bash', str(ACTION / 'fetch-base.sh'), 'grafana-dashboard.json', ref, str(out)],
            env=env,
            cwd=cwd,
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

    def merge_checkout(self) -> tuple[pathlib.Path, dict[str, str]]:
        """A checkout of a pull request's merge commit: the base moved to `built`
        after the pull request opened on `opened`."""
        repo = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}

        def git(*args: str) -> str:
            argv = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args]
            return subprocess.run(
                argv, cwd=repo, env=env, check=True, capture_output=True, text=True
            ).stdout.strip()

        git('init', '-q', '-b', 'dev')
        git('commit', '-q', '--allow-empty', '-m', 'opened on')
        opened = git('rev-parse', 'HEAD')
        git('checkout', '-qb', 'topic')
        git('commit', '-q', '--allow-empty', '-m', 'the pull request')
        git('checkout', '-q', 'dev')
        git('commit', '-q', '--allow-empty', '-m', 'the base moves')
        built = git('rev-parse', 'HEAD')
        git('checkout', '-q', '--detach')
        git(
            'merge',
            '-q',
            '--no-ff',
            '-m',
            'merge ref\n\nparent 0000000000000000000000000000000000000001',
            'topic',
        )
        merge = git('rev-parse', 'HEAD')
        # A single-parent commit whose parent is not the event base, so a
        # check that took any first parent would read it.
        git('commit', '-q', '--allow-empty', '-m', 'after the merge')
        after = git('rev-parse', 'HEAD')
        # HEAD stays on the merge, as a runner checks it out, so a fallback to
        # HEAD would read its first parent.
        git('checkout', '-q', merge)
        return repo, {'opened': opened, 'built': built, 'merge': merge, 'after': after}

    def test_a_pull_request_reads_the_base_its_merge_was_built_on(self):
        repo, sha = self.merge_checkout()
        event = {'GITHUB_EVENT_NAME': 'pull_request', 'EVENT_BASE_SHA': sha['opened']}
        result, log, _ = self.fetch(
            sha['opened'], extra={**event, 'GITHUB_SHA': sha['merge']}, cwd=repo
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'ref={sha["built"]}', log)

    def test_any_other_base_is_read_as_given(self):
        repo, sha = self.merge_checkout()
        # Each case names the merge and the event base, so only the event or
        # the equality guard keeps the ref as given.
        merge = {'GITHUB_SHA': sha['merge'], 'EVENT_BASE_SHA': sha['opened']}
        for name, ref, extra in (
            ('a push', sha['opened'], {**merge, 'GITHUB_EVENT_NAME': 'push'}),
            (
                'a pull_request_target',
                sha['opened'],
                {**merge, 'GITHUB_EVENT_NAME': 'pull_request_target'},
            ),
            ('a caller-chosen base', 'abc123', {**merge, 'GITHUB_EVENT_NAME': 'pull_request'}),
        ):
            with self.subTest(name):
                result, log, _ = self.fetch(ref, extra=extra, cwd=repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'ref={ref}', log)

    def test_a_pull_request_with_no_merge_commit_at_hand_keeps_the_event_base(self):
        repo, sha = self.merge_checkout()
        event = {'GITHUB_EVENT_NAME': 'pull_request', 'EVENT_BASE_SHA': sha['opened']}
        outside = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, extra, cwd in (
            ('a single-parent head', {**event, 'GITHUB_SHA': sha['after']}, repo),
            ('no GITHUB_SHA', event, repo),
            ('no repository', {**event, 'GITHUB_SHA': sha['merge']}, outside),
        ):
            with self.subTest(name):
                result, log, _ = self.fetch(sha['opened'], extra=extra, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'ref={sha["opened"]}', log)

    def test_the_action_hands_the_script_the_event_base_its_default_names(self):
        action = yaml.safe_load((ACTION / 'action.yml').read_text())
        step = next(s for s in action['runs']['steps'] if s.get('name') == 'Check the dashboard')
        self.assertEqual(step['env']['EVENT_BASE_SHA'], '${{ github.event.pull_request.base.sha }}')
        self.assertTrue(
            action['inputs']['base-ref']['default'].startswith(
                '${{ github.event.pull_request.base.sha ||'
            )
        )

    def test_other_answer_fails_closed(self):
        result, _, out = self.fetch('abc123', '500')
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error', result.stdout + result.stderr)
        self.assertFalse(out.exists())

    def test_an_answer_with_no_base_removes_an_earlier_files_copy(self):
        # check.sh reuses one out-file for every dashboard it checks.
        for ref, code, rc in (
            ('', '200', 0),
            ('0' * 40, '200', 0),
            ('abc123', '404', 0),
            ('abc123', '500', 1),
        ):
            with self.subTest(ref=ref, code=code):
                result, _, out = self.fetch(ref, code, stale=True)
                self.assertEqual(result.returncode, rc, result.stderr)
                self.assertFalse(out.exists())


@unittest.skipUnless(
    CUE and SCHEMAS and PROMQL,
    'DASHBOARD_CHECK_CUE, DASHBOARD_CHECK_SCHEMAS and DASHBOARD_CHECK_PROMQL not set',
)
class QueryCheckCLI(unittest.TestCase):
    """The command line runs promql-check on every file, judging the app rules by its path."""

    def check(self, relative: str, doc: dict) -> tuple[int, str]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))
        schemas = []
        for schema in sorted(pathlib.Path(SCHEMAS).glob('schema-*.cue')):
            schemas += ['--schema', f'{schema.stem.removeprefix("schema-")}={schema}']
        with contextlib.chdir(tmp):
            return run_cli('--cue', CUE, '--promql', PROMQL, *schemas, relative)

    def with_query(self, expr: str) -> dict:
        doc = valid()
        query = doc['spec']['elements']['panel-2']['spec']['data']['spec']['queries'][0]
        query['spec']['query']['spec']['expr'] = expr
        return doc

    def test_cli_runs_the_query_check(self):
        code, out = self.check(
            'grafana-dashboard.json', self.with_query('sum(rate(x{job="y"}[$__interval]))')
        )
        self.assertEqual(code, 1, out)
        prefix = 'grafana-dashboard.json: element panel-2 "Jobs finished" query 0: '
        self.assertIn(prefix + '[Q1] a window that follows $__interval', out)
        self.assertIn(prefix + '[Q3] a matcher on a label', out)
        self.assertIn('Fix: use [$__rate_interval]', out)

    def test_parse_error_names_file_and_panel(self):
        code, out = self.check('grafana-dashboard.json', self.with_query('sum(rate(x[5m])'))
        self.assertEqual(code, 1, out)
        self.assertIn(
            'grafana-dashboard.json: element panel-2 "Jobs finished" query 0: [promql] '
            "Prometheus's parser cannot read the query: ",
            out,
        )

    def test_ok_line_names_the_rules_that_ran(self):
        cases = {
            'grafana-dashboard.json': 'app dashboard: Q1, Q2, Q3',
            'grafana-dashboard-messages.json': 'app dashboard: Q1, Q2, Q3',
            'grafana-dashboards/live-operations.json': 'not an app dashboard: Q1 only',
        }
        for relative, rules in cases.items():
            with self.subTest(path=relative):
                code, out = self.check(relative, valid())
                self.assertEqual(code, 0, out)
                self.assertIn(f'6 Prometheus queries parsed, {rules})', out)

    def test_a_dashboard_off_the_root_gets_q1_only(self):
        doc = self.with_query('sum(rate(x{hostname="h"}[5m]))')
        code, out = self.check('grafana-dashboards/live-operations.json', doc)
        self.assertEqual(code, 0, out)
        code, out = self.check('grafana-dashboard.json', doc)
        self.assertEqual(code, 1, out)
        self.assertIn('[Q3]', out)


class Install(unittest.TestCase):
    """install.sh pins, keys and keeps the tools it installs."""

    def copy_action(self) -> pathlib.Path:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        action = tmp / 'dashboard-check'
        shutil.copytree(ACTION, action, ignore=shutil.ignore_patterns('__pycache__'))
        return action

    def key(self, action: pathlib.Path) -> str:
        result = subprocess.run(
            ['bash', str(action / 'install.sh'), '--key'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def test_go_directive_is_not_newer_than_the_pinned_toolchain(self):
        # GOTOOLCHAIN=local refuses to build a module asking for a newer Go.
        install = (ACTION / 'install.sh').read_text()
        go_mod = (ACTION / 'promql-check' / 'go.mod').read_text()
        pinned = re.search(r'^GO_VERSION=go(\S+)$', install, re.MULTILINE)
        wanted = re.search(r'^go (\S+)$', go_mod, re.MULTILINE)
        self.assertTrue(pinned and wanted)
        self.assertLessEqual(
            tuple(map(int, wanted[1].split('.'))), tuple(map(int, pinned[1].split('.')))
        )

    def test_cache_key_follows_what_is_built(self):
        action = self.copy_action()
        start = self.key(action)
        self.assertRegex(start, r'^[0-9a-f]{64}$')
        (action / 'promql-check' / 'check_test.go').write_text('package main\n')
        self.assertEqual(self.key(action), start, 'a test file is not built')
        for changed in ('promql-check/main.go', 'promql-check/go.sum', 'install.sh'):
            with self.subTest(file=changed):
                action = self.copy_action()
                with (action / changed).open('a') as f:
                    f.write('\n')
                self.assertNotEqual(self.key(action), start)

    def test_the_action_keys_its_cache_with_install_sh(self):
        action = yaml.safe_load((ACTION / 'action.yml').read_text())
        steps = {s.get('name'): s for s in action['runs']['steps']}
        self.assertIn('install.sh" --key', steps['Key the tools cache']['run'])
        key = '${{ steps.tools-key.outputs.key }}'
        for name in ('Restore the tools', 'Save the tools'):
            with self.subTest(step=name):
                self.assertIn(key, steps[name]['with']['key'])

    @unittest.skipUnless(
        CUE and SCHEMAS and PROMQL,
        'DASHBOARD_CHECK_CUE, DASHBOARD_CHECK_SCHEMAS and DASHBOARD_CHECK_PROMQL not set',
    )
    def test_a_rerun_with_every_pin_matched_downloads_nothing(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        tools = tmp / 'tools'
        shutil.copytree(SCHEMAS, tools)
        bin_dir = tmp / 'bin'
        bin_dir.mkdir()
        curl = bin_dir / 'curl'
        curl.write_text('#!/bin/sh\necho "curl $*" >&2\nexit 97\n')
        curl.chmod(0o755)
        env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}

        def install() -> subprocess.CompletedProcess:
            return subprocess.run(
                ['bash', str(ACTION / 'install.sh'), str(tools)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        kept = install()
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertNotIn('curl ', kept.stderr)
        (tools / 'promql-check.stamp').write_text('stale\n')
        rebuilt = install()
        self.assertNotEqual(rebuilt.returncode, 0)
        self.assertIn('https://go.dev/dl/', rebuilt.stderr)

    @unittest.skipUnless(
        CUE and SCHEMAS and PROMQL,
        'DASHBOARD_CHECK_CUE, DASHBOARD_CHECK_SCHEMAS and DASHBOARD_CHECK_PROMQL not set',
    )
    def test_a_cue_that_writes_past_its_version_line_is_kept(self):
        # cue version prints more lines after the one install.sh matches.
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        tools = tmp / 'tools'
        shutil.copytree(SCHEMAS, tools)
        cue = tools / 'cue'
        cue.write_text('#!/bin/sh\necho "cue version v0.17.1"\nsleep 0.2\necho "go version"\n')
        cue.chmod(0o755)
        bin_dir = tmp / 'bin'
        bin_dir.mkdir()
        curl = bin_dir / 'curl'
        curl.write_text('#!/bin/sh\necho "curl $*" >&2\nexit 97\n')
        curl.chmod(0o755)
        result = subprocess.run(
            ['bash', str(ACTION / 'install.sh'), str(tools)],
            env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('curl ', result.stderr)

    @unittest.skipUnless(
        CUE and SCHEMAS and PROMQL,
        'DASHBOARD_CHECK_CUE, DASHBOARD_CHECK_SCHEMAS and DASHBOARD_CHECK_PROMQL not set',
    )
    def test_a_go_archive_that_misses_its_sha256_is_never_run(self):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        tools = tmp / 'tools'
        shutil.copytree(SCHEMAS, tools)
        built = (tools / 'promql-check').read_bytes()
        (tools / 'promql-check.stamp').write_text('stale\n')
        # A Go archive whose go would install its own promql-check if it ran.
        go = tmp / 'archive' / 'go' / 'bin' / 'go'
        go.parent.mkdir(parents=True)
        go.write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            'echo hostile >"$out"; chmod +x "$out"\n'
        )
        go.chmod(0o755)
        archive = tmp / 'go.tgz'
        subprocess.run(['tar', '-czf', archive, '-C', tmp / 'archive', 'go'], check=True)
        bin_dir = tmp / 'bin'
        bin_dir.mkdir()
        curl = bin_dir / 'curl'
        curl.write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            f'cp {archive} "$out"\n'
        )
        curl.chmod(0o755)
        result = subprocess.run(
            ['bash', str(ACTION / 'install.sh'), str(tools)],
            env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did NOT match', result.stderr)
        self.assertEqual((tools / 'promql-check').read_bytes(), built)
        self.assertEqual((tools / 'promql-check.stamp').read_text(), 'stale\n')

    @unittest.skipUnless(
        CUE and SCHEMAS and PROMQL,
        'DASHBOARD_CHECK_CUE, DASHBOARD_CHECK_SCHEMAS and DASHBOARD_CHECK_PROMQL not set',
    )
    def test_the_build_ignores_the_callers_go_settings(self):
        action = self.copy_action()
        tmp = action.parent
        tools = tmp / 'tools'
        shutil.copytree(SCHEMAS, tools)
        (tools / 'promql-check.stamp').write_text('stale\n')
        record = tmp / 'go-env'
        go = tmp / 'archive' / 'go' / 'bin' / 'go'
        go.parent.mkdir(parents=True)
        go.write_text(
            f'#!/bin/sh\nenv >{record}\n'
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            'printf "#!/bin/sh\\n" >"$out"\n'
        )
        go.chmod(0o755)
        archive = tmp / 'go.tgz'
        subprocess.run(['tar', '-czf', archive, '-C', tmp / 'archive', 'go'], check=True)
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        install = action / 'install.sh'
        install.write_text(
            re.sub(
                r'^(GO_SHA256_(?:AMD|ARM)64)=\S+$',
                rf'\1={sha}',
                install.read_text(),
                flags=re.MULTILINE,
            )
        )
        bin_dir = tmp / 'bin'
        bin_dir.mkdir()
        curl = bin_dir / 'curl'
        curl.write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            f'cp {archive} "$out"\n'
        )
        curl.chmod(0o755)
        hostile = {
            'GOFLAGS': '-mod=vendor',
            'GOWORK': '/nonexistent/go.work',
            'GOTOOLCHAIN': 'go1.1',
            'GOROOT': '/nonexistent',
            'GOPROXY': 'off',
            'GONOSUMDB': '*',
            'GOENV': '/nonexistent/env',
            'CGO_ENABLED': '1',
        }
        result = subprocess.run(
            ['bash', str(install), str(tools)],
            env={**os.environ, **hostile, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = dict(line.split('=', 1) for line in record.read_text().splitlines() if '=' in line)
        self.assertNotIn('GOROOT', seen)
        # A module cache Go leaves read-only cannot be deleted by a non-root
        # runner user, and the EXIT trap's failed rm then fails install.sh.
        self.assertEqual(seen['GOFLAGS'].split(), ['-mod=readonly', '-modcacherw'])
        wanted = {
            'GOWORK': 'off',
            'GOTOOLCHAIN': 'local',
            'GOENV': 'off',
            'CGO_ENABLED': '0',
            'GOPROXY': 'https://proxy.golang.org',
            'GOSUMDB': 'sum.golang.org',
            'GONOSUMDB': '',
            'GONOPROXY': '',
            'GOPRIVATE': '',
            'GOINSECURE': '',
        }
        for name, value in wanted.items():
            with self.subTest(setting=name):
                self.assertEqual(seen.get(name), value)


if __name__ == '__main__':
    unittest.main()
