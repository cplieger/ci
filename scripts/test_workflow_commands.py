"""Every workflow command, and every plain log line holding data, that a script writes goes
through workflow_commands or gha-log.sh, escaped."""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.util
import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

import intake
import mutation_cost
import runner_images
import workflow_commands as wc

SCRIPTS = pathlib.Path(__file__).resolve().parent
ACTIONS = SCRIPTS.parent / 'actions'
HOSTILE = '100%\r\n::add-mask::x'
ESCAPED = '100%25%0D%0A::add-mask::x'
OPENS_COMMAND = re.compile(r'::[a-z-]*(::| |$)')
# A command literal, then data on the same line: an expansion (shell or a JavaScript template), a
# printf conversion, a Python f-string field or a string concatenation.
SHELL_RAW = re.compile(
    r'::\$|::(?:error|warning|notice|group)\b[^\n]*?'
    r'(?:\$|%[-+ #0-9.]*[a-zA-Z]|\{[A-Za-z_]|["\'`]\s*\+)'
)


def load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command_literals(path: pathlib.Path) -> list[int]:
    """Lines of PATH where a string literal, or an f-string's leading text, opens a workflow command."""
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            text = head.value if isinstance(head, ast.Constant) else ''
            if isinstance(head, ast.Constant) and len(node.values) > 1 and text == '::':
                found.append(node.lineno)
                continue
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        else:
            continue
        if OPENS_COMMAND.match(text):
            found.append(node.lineno)
    return sorted(set(found))


def shell_command_lines(text: str) -> list[int]:
    """Lines of TEXT, a shell script or a workflow, that write a workflow command holding data
    outside gha-log.sh."""
    return [
        n
        for n, line in enumerate(text.splitlines(), 1)
        if not line.lstrip().startswith('#') and SHELL_RAW.search(line)
    ]


def bash(snippet: str, *args: str) -> subprocess.CompletedProcess:
    """SNIPPET under bash with gha-log.sh sourced, ARGS as its positional parameters."""
    return subprocess.run(
        ['bash', '-c', f'. "$0"; {snippet}', str(SCRIPTS / 'gha-log.sh'), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def gha(*args: str) -> subprocess.CompletedProcess:
    """gha_annotate ARGS run under bash with gha-log.sh sourced."""
    return subprocess.run(
        ['bash', '-c', '. "$0"; gha_annotate "$@"', str(SCRIPTS / 'gha-log.sh'), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class Command(unittest.TestCase):
    def test_a_message_cannot_end_its_line(self):
        self.assertEqual(wc.command('error', HOSTILE), f'::error::{ESCAPED}')

    def test_a_property_cannot_end_its_line_or_its_list(self):
        self.assertEqual(
            wc.command('warning', 'm', file='a,b:c%\n', line=3),
            '::warning file=a%2Cb%3Ac%25%0A,line=3::m',
        )

    def test_a_falsy_property_is_left_out(self):
        self.assertEqual(wc.command('notice', 'm', file='', line=None), '::notice::m')
        self.assertEqual(wc.command('endgroup'), '::endgroup::')

    def test_a_command_it_does_not_write_is_refused(self):
        with self.assertRaises(ValueError):
            wc.command('add-mask', 'x')


class ShellCommand(unittest.TestCase):
    """gha-log.sh's gha_annotate writes the line workflow_commands.command writes."""

    def test_a_message_cannot_end_its_line(self):
        out = gha('error', 'at %s: %s', HOSTILE, 'a,b:c')
        self.assertEqual(out.stdout, f'::error::at {ESCAPED}: a,b:c\n')
        self.assertEqual(out.stdout, wc.command('error', f'at {HOSTILE}: a,b:c') + '\n')

    def test_a_property_cannot_end_its_line_or_its_list(self):
        out = gha('warning', '-p', 'file=a,b:c%\n', '-p', 'line=3', '%s', 'm')
        self.assertEqual(out.stdout, '::warning file=a%2Cb%3Ac%25%0A,line=3::m\n')
        self.assertEqual(out.stdout, wc.command('warning', 'm', file='a,b:c%\n', line=3) + '\n')

    def test_an_empty_property_is_left_out_and_a_group_is_written(self):
        self.assertEqual(gha('notice', '-p', 'file=', 'm').stdout, '::notice::m\n')
        self.assertEqual(gha('group', '%s (%s)', 'n', 'p').stdout, '::group::n (p)\n')

    def test_a_command_or_key_it_does_not_write_is_refused(self):
        for args in (
            ('add-mask', 'x'),
            ('endgroup', 'x'),
            ('error', '-p', 'fi le=a', 'm'),
            ('error', '-p', 'file', 'm'),
            ('error', '-p', '=a', 'm'),
            ('error', '-p'),
        ):
            with self.subTest(args):
                out = gha(*args)
                self.assertEqual((out.returncode, out.stdout), (2, ''))


class Writers(unittest.TestCase):
    """Each script's own writer passes a hostile value through as one escaped line."""

    def test_each_writer_escapes_its_value(self):
        ga = load(SCRIPTS / 'gorelease-accept.py')

        def title():
            with unittest.mock.patch.object(intake, 'check_title', return_value=[HOSTILE]):
                intake.main(['title'])

        for name, write, stream, want in (
            ('intake', title, 'out', f'::error::{ESCAPED}'),
            (
                'gorelease-accept',
                lambda: ga.annotate('warning', HOSTILE, 2),
                'out',
                f'::warning file=api-accept.txt,line=2::{ESCAPED}',
            ),
            (
                'mutation_cost',
                lambda: mutation_cost.annotate('error', t'{HOSTILE}'),
                'err',
                f'::error::{ESCAPED}',
            ),
            ('runner_images', lambda: runner_images.note(HOSTILE), 'err', f'::notice::{ESCAPED}'),
        ):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(name), redirect_stdout(out), redirect_stderr(err):
                write()
                text = (out if stream == 'out' else err).getvalue()
                self.assertIn(f'{want}\n', text)
                self.assertNotIn('\n::add-mask::', text)


class ActionWriters(unittest.TestCase):
    """Each action's writer passes a hostile value through as one escaped line."""

    PROPERTY = '100%25%0D%0A%3A%3Aadd-mask%3A%3Ax'

    def setUp(self):
        self.tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

    @staticmethod
    def capture(write) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            write()
        return out.getvalue(), err.getvalue()

    def sh(
        self, script: str, *args: str, stub: str = '', **env: str
    ) -> subprocess.CompletedProcess:
        """actions/SCRIPT under bash, with uname and curl stubbed to print STUB."""
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir(exist_ok=True)
        for tool in ('uname', 'curl'):
            (bin_dir / tool).write_text('#!/bin/sh\nprintf "%s" "$STUB"\n')
            (bin_dir / tool).chmod(0o755)
        return subprocess.run(
            ['bash', str(ACTIONS / script), *args],
            cwd=self.tmp,
            env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'STUB': stub, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_alert_table(self):
        at = load(ACTIONS / 'alert-rules' / 'alert-table.py')
        out, _ = self.capture(lambda: at.error(HOSTILE, HOSTILE))
        self.assertEqual(out, f'::error file={self.PROPERTY}::{ESCAPED}\n')

    def test_render_hub_overview(self):
        rh = load(ACTIONS / 'render-hub-overview' / 'render-hub-overview.py')

        def fail():
            with self.assertRaises(SystemExit):
                rh.fail(HOSTILE)

        self.assertEqual(self.capture(fail)[1], f'::error::{ESCAPED}\n')

    def test_publish_surface(self):
        ps = load(ACTIONS / 'publish-surface' / 'publish-surface.py')
        argv = ['publish-surface.py', '--github', '--package-dir', str(self.tmp)]
        refused = ps.SurfaceError(f'a,b:c {HOSTILE}')
        with (
            unittest.mock.patch.object(sys, 'argv', argv),
            unittest.mock.patch.object(ps, 'read_json', side_effect=refused),
        ):
            out, _ = self.capture(lambda: self.assertEqual(ps.main(), 1))
        self.assertEqual(out, '::error::publish-surface check could not complete: a,b:c 100%25\n')

    def test_comment_audit(self):
        ca = load(ACTIONS / 'comment-audit' / 'comment-audit.py')
        with (
            unittest.mock.patch.object(ca, 'EXCLUDED_REPOSITORIES', {HOSTILE}),
            unittest.mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': HOSTILE}),
        ):
            out, _ = self.capture(lambda: self.assertEqual(ca.main(['--github', str(self.tmp)]), 0))
        want = f'::notice title=Comment ratio::{ESCAPED} is excluded because it is not an app repository'
        self.assertIn(f'{want}\n', out)

    def test_notice_audit(self):
        na = load(ACTIONS / 'notice-audit' / 'notice-audit.py')
        with (
            unittest.mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': 'cplieger/probe'}),
            unittest.mock.patch.object(na, 'audit', return_value=([HOSTILE], 0)),
        ):
            out, _ = self.capture(lambda: self.assertEqual(na.main(['--github', str(self.tmp)]), 1))
        self.assertIn(f'::error title=NOTICE audit::{ESCAPED}\n', out)

    def test_dashboard_check(self):
        dc = load(ACTIONS / 'dashboard-check' / 'dashboard-check.py')
        path = self.tmp / f'{HOSTILE}.json'
        argv = ['--github', '--cue', 'cue', '--promql', 'promql', '--schema', 't=f', str(path)]
        out, _ = self.capture(lambda: self.assertEqual(dc.main(argv), 1))
        self.assertEqual(
            out,
            f'::error file={wc.escape_property(str(path))},title=dashboard-check envelope::'
            'file: [envelope] the file does not exist. Fix: commit a valid dashboard JSON file.\n',
        )

    def test_git_cliff_version_compute(self):
        out = self.sh('git-cliff-version/compute.sh', HOSTILE)
        self.assertEqual(
            (out.returncode, out.stdout), (1, f"::error::unknown compute.sh command '{ESCAPED}'\n")
        )

    def test_dashboard_check_fetch_base(self):
        out = self.sh(
            'dashboard-check/fetch-base.sh',
            HOSTILE,
            'a,b',
            str(self.tmp / 'base.json'),
            stub='500',
            TOKEN='t',
            GITHUB_API_URL='https://api.invalid',
            GITHUB_REPOSITORY='cplieger/probe',
            GITHUB_EVENT_NAME='push',
        )
        want = f'::error::dashboard-check: reading {ESCAPED} at a,b answered HTTP 500\n'
        self.assertEqual((out.returncode, out.stdout), (1, want))

    def test_dashboard_check_install(self):
        out = self.sh('dashboard-check/install.sh', str(self.tmp / 'tools'), stub=HOSTILE)
        want = f'::error::dashboard-check: no cue or Go build pinned for {ESCAPED}\n'
        self.assertEqual((out.returncode, out.stderr), (1, want))

    def test_alert_rules_install(self):
        out = self.sh('alert-rules/install.sh', HOSTILE, '1', str(self.tmp / 'tools'), stub=HOSTILE)
        want = f'::error::alert-rules: no {ESCAPED} build for {ESCAPED}\n'
        self.assertEqual((out.returncode, out.stderr), (1, want))


class Say(unittest.TestCase):
    """say and gha-log.sh's gha_say write one plain line no value can turn into a command."""

    def said(self, msg) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            wc.say(msg)
        return out.getvalue()

    def test_a_value_cannot_end_its_line_or_start_a_command(self):
        self.assertEqual(self.said(t'at {HOSTILE}: {7}'), f'- at {ESCAPED}: 7\n')

    def test_every_hash_bracket_pair_is_broken(self):
        self.assertEqual(self.said(t'## {"#"}#[x] {"###[y"}'), '- ## #%23[x] ##%23[y\n')

    def test_a_literal_only_line_and_its_format_spec_are_kept(self):
        self.assertEqual(self.said(t'done {1.5:.2f}%'), '- done 1.50%\n')

    def test_it_refuses_a_plain_string(self):
        with self.assertRaises(TypeError):
            wc.say('x')

    def test_say_lines_writes_each_line_through_say(self):
        out = io.StringIO()
        with redirect_stdout(out):
            wc.say_lines(f'## a\n{HOSTILE}')
        self.assertEqual(out.getvalue(), '- ## a\n- 100%25\n- ::add-mask::x\n')

    def test_gha_say_writes_the_line_say_writes(self):
        for fmt, args, msg in (
            ('at %s: %s', (HOSTILE, '7'), t'at {HOSTILE}: {"7"}'),
            ('## %s#[x] %s', ('#', '###[y'), t'## {"#"}#[x] {"###[y"}'),
            ('done', (), t'done'),
        ):
            with self.subTest(fmt):
                out = bash('gha_say "$@"', fmt, *args)
                self.assertEqual((out.returncode, out.stdout), (0, self.said(msg)))

    def test_run_main_writes_a_refusal_as_one_command_and_a_crash_through_say(self):
        def refuse():
            raise SystemExit(HOSTILE)

        def crash():
            raise ValueError(HOSTILE)

        for main, code, want in ((refuse, 1, f'::error::{ESCAPED}\n'), (lambda: None, 0, '')):
            err = io.StringIO()
            with self.subTest(code), redirect_stderr(err):
                self.assertEqual(wc.run_main(main), code)
                self.assertEqual(err.getvalue(), want)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(wc.run_main(crash), 1)
        lines = err.getvalue().splitlines()
        self.assertTrue(all(line.startswith('- ') for line in lines), lines)
        self.assertEqual(lines[-2:], ['- ValueError: 100%25', '- ::add-mask::x'])


INJECT = 'v\n::error::x ##[error]y'


class PlainWriters(unittest.TestCase):
    """Each script that logs data writes a value holding a command as inert plain text."""

    def setUp(self):
        self.tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()

    def assert_inert(self, out: str) -> None:
        self.assertIn('::error::x', out)
        self.assertNotRegex(out, r'(?m)^\s*::error')
        self.assertNotIn('##[', out)

    @staticmethod
    def call(write) -> str:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), contextlib.suppress(SystemExit):
            write()
        return out.getvalue() + err.getvalue()

    def stub(self, name: str, body: str) -> None:
        (self.bin / name).write_text(f'#!/usr/bin/env bash\n{body}\n')
        (self.bin / name).chmod(0o755)

    def run_(self, *argv: str, **env: str) -> str:
        proc = subprocess.run(
            list(argv),
            cwd=self.tmp,
            env={**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}', **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout + proc.stderr

    def py(self, script: str, *args: str, **env: str) -> str:
        return self.run_(sys.executable, str(SCRIPTS / script), *args, **env)

    def sh(self, script: pathlib.Path, *args: str, **env: str) -> str:
        return self.run_('bash', str(script), *args, **env)

    def test_python_writers(self):
        nothing = str(self.tmp / INJECT)
        no_bench = self.tmp / f'{INJECT}.txt'
        no_bench.write_text('PASS\n')
        rows = {
            'audit': lambda: self.call(lambda: load(SCRIPTS / 'audit.py').load_findings(nothing)),
            'bench-aggregate': lambda: self.py(
                'bench-aggregate.py',
                *('--repo', INJECT, '--data-file', nothing, '--week', 'w', '--run-url', 'u'),
            ),
            'bench-fix-attribution': lambda: self.py(
                'bench-fix-attribution.py',
                *('--data-file', nothing, '--expect-commit', 'c', '--commit-json', nothing),
                '--repo-url',
                'u',
            ),
            'bench-stability': lambda: self.py('bench-stability.py', str(no_bench)),
            'classify-repos': lambda: self.call(
                lambda: load(SCRIPTS / 'classify-repos.py').log_profile(
                    INJECT, {'lang': 'go', 'is_web': False}
                )
            ),
            'community_data_refresh': self.community_data_refresh,
            'ghcr_retention': self.ghcr_retention,
            'go-embeds': lambda: self.py('go-embeds.py', '--with-commits', 'a..b', INJECT, 'x'),
            'gorelease-accept': self.gorelease_accept,
            'gremlins-aggregate': lambda: self.py(
                'gremlins-aggregate.py',
                *('--repo', INJECT, '--artifacts-dir', str(self.tmp), '--week', 'w'),
                *('--run-url', 'u'),
            ),
            'gremlins-merge': lambda: self.call(
                lambda: load(SCRIPTS / 'gremlins-merge.py').load_module(
                    INJECT, pathlib.Path(nothing)
                )
            ),
            'gremlins-scope': self.gremlins_scope,
            'mutation_cost': lambda: self.call(lambda: mutation_cost.note_prior_error(INJECT, 1.0)),
            'perf_gate': lambda: self.call(
                lambda: load(SCRIPTS / 'perf_gate.py').report(
                    unittest.mock.Mock(summary=''), f'## report\n{INJECT}'
                )
            ),
            'perf_trend': self.perf_trend,
            'promote': lambda: self.call(
                lambda: load(SCRIPTS / 'promote.py').write_summary(INJECT)
            ),
            'release_maintenance': self.release_maintenance,
            'stryker-aggregate': lambda: self.py(
                'stryker-aggregate.py',
                *('--repo', INJECT, '--artifacts-dir', str(self.tmp), '--week', 'w'),
                *('--run-url', 'u'),
            ),
            'sync-files': self.sync_files,
            'sync-hub-overviews': self.sync_hub_overviews,
            'tracker_issue': self.tracker_issue,
        }
        for name, write in rows.items():
            with self.subTest(name):
                self.assert_inert(write())

    def test_action_writers(self):
        rows = {
            'comment-audit': lambda: self.call(self.comment_audit),
            'dashboard-check': lambda: self.call(
                lambda: load(ACTIONS / 'dashboard-check' / 'dashboard-check.py').main(
                    [
                        '--cue',
                        'cue',
                        '--promql',
                        'promql',
                        '--schema',
                        't=f',
                        str(self.tmp / INJECT),
                    ]
                )
            ),
            'notice-audit': self.notice_audit,
            'publish-surface': self.publish_surface,
            'render-hub-overview': self.render_hub_overview,
        }
        for name, write in rows.items():
            with self.subTest(name):
                self.assert_inert(write())

    def test_shell_writers(self):
        rows = {
            'go-units': lambda: self.sh(SCRIPTS / 'go-units.sh', INJECT),
            'playwright-deps': lambda: self.sh(
                SCRIPTS / 'playwright-deps.sh', INJECT, RUNNER_ENVIRONMENT='self-hosted'
            ),
            'reconciliation': lambda: self.in_repo(
                '. "$0"; known_commit "$1"', SCRIPTS / 'reconciliation.sh', INJECT
            ),
            'render-notes': lambda: self.sh(SCRIPTS / 'render-notes.sh', INJECT),
            'retry': lambda: self.in_repo(
                '. "$0"; REGISTRY_WAIT_SECONDS=0 await_registry "$1" false',
                SCRIPTS / 'retry.sh',
                INJECT,
            ),
            'mutation-history': self.mutation_history,
            'release-state': self.release_state,
            'rebuild-gate': self.rebuild_gate,
            'gremlins-run': self.gremlins_run,
            'publish-badge': self.publish_badge,
            'dashboard-check fetch-base': self.fetch_base,
            'dashboard-check install': self.dashboard_check_install,
        }
        for name, write in rows.items():
            with self.subTest(name):
                self.assert_inert(write())

    def in_repo(self, snippet: str, *args: object) -> str:
        """SNIPPET under bash in a one-commit repository, ARGS as $0 and on."""
        self.git_repo()
        return self.run_('bash', '-c', snippet, *map(str, args))

    def git(self, *args: str) -> None:
        identity = ('-c', 'user.name=t', '-c', 'user.email=t@t')
        signing = ('-c', 'commit.gpgSign=false', '-c', 'tag.gpgSign=false')
        subprocess.run(['git', '-C', str(self.tmp), *identity, *signing, *args], check=True)

    def git_repo(self) -> None:
        self.git('init', '-q')
        self.git('commit', '-q', '--allow-empty', '-m', 'c')

    def mutation_history(self) -> str:
        self.stub('gh', 'exit 0')
        return self.sh(
            SCRIPTS / 'mutation-history.sh', INJECT, 'dev', 'out', 'f', GITHUB_REPOSITORY='o/r'
        )

    def release_state(self) -> str:
        self.stub('gh', 'exit 0')
        return self.sh(
            SCRIPTS / 'release-state.sh', 'receipt', INJECT, 'c', GITHUB_REPOSITORY='o/r'
        )

    def rebuild_gate(self) -> str:
        self.git_repo()
        self.git('tag', 'v1.0.0-dev.1')
        tools = self.tmp / 'tools'
        tools.mkdir()
        for name in ('gha-log.sh', 'reconciliation.sh'):
            (tools / name).write_text((SCRIPTS / name).read_text())
        (tools / 'path-significance.sh').write_text('printf "%s\\n" "$REASON" >&2\nexit 1\n')
        return self.sh(
            SCRIPTS / 'rebuild-gate.sh',
            'scope',
            CI_TOOLS=str(tools),
            CHANNEL='dev',
            GITHUB_EVENT_NAME='push',
            REASON=INJECT.replace('\n', ' '),
        )

    def gremlins_run(self) -> str:
        src = (SCRIPTS / 'gremlins-run.sh').read_text()
        body = re.search(r'^choose_workers\(\) \{\n.*?^\}\n', src, re.MULTILINE | re.DOTALL)[0]
        snippet = f'set -euo pipefail\n. "$0"\n{body}choose_workers'
        return self.run_(
            'bash',
            '-c',
            snippet,
            str(SCRIPTS / 'gha-log.sh'),
            GREMLINS_WORKERS=INJECT,
            WORKERS_MAX='2',
            GOMEM_MAX_MB='1',
        )

    def publish_badge(self) -> str:
        self.stub(
            'git', 'case "$*" in *fetch* | *ls-remote*) exit 1 ;; esac\nexec /usr/bin/git "$@"'
        )
        self.stub('sleep', 'exit 0')
        return self.sh(ACTIONS / 'publish-badge' / 'publish-badge.sh', INJECT, 't', 'b.json', '{}')

    def fetch_base(self) -> str:
        self.stub('curl', 'printf 200')
        env = {'TOKEN': 't', 'GITHUB_API_URL': 'https://api.invalid'}
        env |= {'GITHUB_REPOSITORY': 'o/r', 'GITHUB_EVENT_NAME': 'push'}
        script = ACTIONS / 'dashboard-check' / 'fetch-base.sh'
        return self.sh(script, INJECT, 'abc', str(self.tmp / 'base.json'), **env)

    def dashboard_check_install(self) -> str:
        """install.sh over a DEST that already holds every tool, so it fetches nothing."""
        dest = self.tmp / INJECT
        dest.mkdir()
        self.stub(
            'sha256sum',
            'case "$*" in *-c*) cat >/dev/null; exit 0 ;; esac\n'
            '[ "$#" -gt 0 ] || cat >/dev/null\necho "0  -"',
        )
        script = ACTIONS / 'dashboard-check' / 'install.sh'
        version = re.search(r'^CUE_VERSION=(\S+)$', script.read_text(), re.MULTILINE)[1]
        (dest / 'cue').write_text(f'#!/bin/sh\necho "cue version {version}"\n')
        (dest / 'promql-check').write_text('#!/bin/sh\n')
        for tool in ('cue', 'promql-check'):
            (dest / tool).chmod(0o755)
        (dest / 'promql-check.stamp').write_text('0\n')
        for tag in re.findall(r'^  "(\S+) ', script.read_text(), re.MULTILINE):
            (dest / f'schema-{tag}.cue').write_text('')
        return self.sh(script, str(dest))

    def community_data_refresh(self) -> str:
        cdr = load(SCRIPTS / 'community_data_refresh.py')
        plan = self.tmp / 'plan.json'
        plan.write_text('[]')
        a = argparse.Namespace(repo='subflux', plan=str(plan))
        with unittest.mock.patch.object(cdr, 'apply_issues', return_value=[INJECT]):
            return self.call(lambda: cdr.cmd_issues(a))

    def ghcr_retention(self) -> str:
        gr = load(SCRIPTS / 'ghcr_retention.py')
        with (
            unittest.mock.patch.object(gr, 'list_packages', return_value=[]),
            unittest.mock.patch.object(gr, 'render_table', return_value=f'{INJECT}\n'),
            unittest.mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}),
        ):
            return self.call(lambda: gr.main(['--dry-run']))

    def gorelease_accept(self) -> str:
        ga = load(SCRIPTS / 'gorelease-accept.py')
        for name in ('report', 'accept'):
            (self.tmp / name).write_text('')
        argv = ['gorelease-accept.py', '--verdict', 'ok', '--base', 'v1.0.0', '--version', 'v1.1.0']
        argv += ['--report', str(self.tmp / 'report'), '--accept', str(self.tmp / 'accept')]
        argv += ['--summary', str(self.tmp / 'summary')]
        with (
            unittest.mock.patch.object(sys, 'argv', argv),
            unittest.mock.patch.object(ga, 'run', return_value=(0, [INJECT])),
        ):
            return self.call(ga.main)

    def gremlins_scope(self) -> str:
        (self.tmp / 'a').mkdir()
        (self.tmp / 'central.yaml').write_text('unleash:\n  exclude-files:\n')
        return self.py(
            'gremlins-scope.py',
            *('--config', str(self.tmp / 'central.yaml'), '--target', str(self.tmp)),
            *('--exclude-dir', f'a/{INJECT}', 'a'),
        )

    def perf_trend(self) -> str:
        pt = load(SCRIPTS / 'perf_trend.py')
        a = argparse.Namespace(command='fetch-issues', repo='r', out=str(self.tmp / INJECT))
        with unittest.mock.patch.object(pt, 'fetch_issues', return_value=[]):
            return self.call(lambda: pt.run(a))

    def release_maintenance(self) -> str:
        rm = load(SCRIPTS / 'release_maintenance.py')
        api = unittest.mock.Mock()
        with unittest.mock.patch.object(rm, 'merge_checked', return_value=INJECT):
            return self.call(lambda: rm.report_merge_checked(api, 'r', 5, base='dev'))

    def sync_files(self) -> str:
        sf = load(SCRIPTS / 'sync-files.py')
        with unittest.mock.patch.object(sf, 'RUN', side_effect=OSError(INJECT)):
            return self.call(lambda: sf.arm_default('cplieger/r', 7))

    def sync_hub_overviews(self) -> str:
        sh = load(SCRIPTS / 'sync-hub-overviews.py')
        failed = subprocess.CompletedProcess([], 1, '', f'gh: {INJECT}')
        with unittest.mock.patch.object(sh.subprocess, 'run', return_value=failed):
            return self.call(lambda: sh.github_about('cplieger', 'r'))

    def tracker_issue(self) -> str:
        ti = load(SCRIPTS / 'tracker_issue.py')
        refused = ti.ghrest.ApiError(None, INJECT)
        with unittest.mock.patch.object(ti.ghrest, 'get', side_effect=refused):
            return self.call(lambda: ti.issues_enabled('cplieger/r'))

    def comment_audit(self) -> None:
        ca = load(ACTIONS / 'comment-audit' / 'comment-audit.py')
        with (
            unittest.mock.patch.object(ca, 'EXCLUDED_REPOSITORIES', {INJECT}),
            unittest.mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': INJECT}),
        ):
            ca.main([str(self.tmp)])

    def notice_audit(self) -> str:
        na = load(ACTIONS / 'notice-audit' / 'notice-audit.py')
        with (
            unittest.mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': 'cplieger/probe'}),
            unittest.mock.patch.object(na, 'audit', return_value=([INJECT], 0)),
        ):
            return self.call(lambda: na.main([str(self.tmp)]))

    def publish_surface(self) -> str:
        ps = load(ACTIONS / 'publish-surface' / 'publish-surface.py')
        argv = ['publish-surface.py', '--package-dir', str(self.tmp / INJECT)]
        with unittest.mock.patch.object(sys, 'argv', argv):
            return self.call(ps.main)

    def render_hub_overview(self) -> str:
        rh = load(ACTIONS / 'render-hub-overview' / 'render-hub-overview.py')
        argv = ['render-hub-overview.py', '--repo', 'r', '--output', str(self.tmp / INJECT)]
        with (
            unittest.mock.patch.object(sys, 'argv', argv),
            unittest.mock.patch.object(rh, 'render', return_value='page'),
        ):
            return self.call(rh.main)


class NoRawCommands(unittest.TestCase):
    def test_no_script_writes_a_command_outside_the_helper(self):
        scripts = sorted(
            p
            for p in (*SCRIPTS.glob('*.py'), *ACTIONS.glob('*/*.py'))
            if not p.name.startswith('test') and p.name != 'workflow_commands.py'
        )
        found = {
            str(p.relative_to(SCRIPTS.parent)): lines
            for p in scripts
            if (lines := command_literals(p))
        }
        self.assertEqual(found, {}, 'write these through workflow_commands.command')
        self.assertGreater(len(scripts), 20)
        self.assertGreater(len([p for p in scripts if p.is_relative_to(ACTIONS)]), 5)

    def test_no_shell_script_writes_a_command_with_data_outside_the_helper(self):
        scripts = sorted(
            p
            for p in (*SCRIPTS.glob('*.sh'), *ACTIONS.glob('*/*.sh'))
            if not p.name.startswith('test') and p.name != 'gha-log.sh'
        )
        found = {
            str(p.relative_to(SCRIPTS.parent)): lines
            for p in scripts
            if (lines := shell_command_lines(p.read_text()))
        }
        self.assertEqual(found, {}, 'write these through gha-log.sh gha_annotate')
        self.assertGreater(len(scripts), 20)
        self.assertGreater(len([p for p in scripts if p.is_relative_to(ACTIONS)]), 5)

    def test_no_run_step_writes_a_command_with_data_outside_the_helper(self):
        root = SCRIPTS.parent
        files = sorted(
            [*root.glob('.github/workflows/*.y*ml'), *root.glob('actions/*/action.y*ml')]
        )
        found = {
            str(p.relative_to(root)): lines
            for p in files
            if (lines := shell_command_lines(p.read_text()))
        }
        self.assertEqual(found, {}, 'write these through gha-log.sh gha_annotate')
        self.assertGreater(len(files), 40)

    def test_the_shell_check_sees_each_raw_form(self):
        for source, want in (
            ('echo "::error::${x} failed"', [1]),
            ('  echo "::warning::$1"  >&2', [1]),
            ('printf \'::notice::%s\\n\' "$m"', [1]),
            ('echo "::error file=$f::fixed"', [1]),
            ('echo "::group::$(name)"', [1]),
            ('echo "::$kind::m"', [1]),
            ('decide false "::warning::because ${why}."', [1]),
            ('console.error(`::error::${path}: ${why}`);', [1]),
            ('console.error("::error file=" + f + "::bad");', [1]),
            ("print(f'::notice::{name} skipped')", [1]),
            ('gha_annotate error "series must match ^[a-z]{0,30}$"', []),
            ('echo "::error::fixed text"', []),
            ('echo "::endgroup::"', []),
            ('# echo "::error::$x" in a comment', []),
            ('gha_annotate error \'%s failed\' "$x"', []),
        ):
            with self.subTest(source):
                self.assertEqual(shell_command_lines(source), want)

    def test_the_check_sees_each_raw_form(self):
        for source in (
            "print(f'::error::{x}')",
            "print('::warning::fixed')",
            "sys.exit(f'::notice file={f}::{m}')",
            "print(f'::{kind}::{m}')",
            "print('::endgroup::')",
        ):
            with (
                self.subTest(source),
                unittest.mock.patch.object(pathlib.Path, 'read_text', return_value=source),
            ):
                self.assertEqual(command_literals(pathlib.Path('x.py')), [1])


if __name__ == '__main__':
    unittest.main()
