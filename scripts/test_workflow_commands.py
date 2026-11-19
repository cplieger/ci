"""Every workflow command a script writes goes through workflow_commands, escaped."""

from __future__ import annotations

import ast
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
# A shell command literal, then data on the same line: an expansion or a printf conversion.
SHELL_RAW = re.compile(r'::\$|::(?:error|warning|notice|group)\b[^\n]*?(?:\$|%[-+ #0-9.]*[a-zA-Z])')


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
    """Lines of TEXT, a shell script, that write a workflow command holding data outside gha-log.sh."""
    return [
        n
        for n, line in enumerate(text.splitlines(), 1)
        if not line.lstrip().startswith('#') and SHELL_RAW.search(line)
    ]


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

    def test_the_shell_check_sees_each_raw_form(self):
        for source, want in (
            ('echo "::error::${x} failed"', [1]),
            ('  echo "::warning::$1"  >&2', [1]),
            ('printf \'::notice::%s\\n\' "$m"', [1]),
            ('echo "::error file=$f::fixed"', [1]),
            ('echo "::group::$(name)"', [1]),
            ('echo "::$kind::m"', [1]),
            ('decide false "::warning::because ${why}."', [1]),
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
