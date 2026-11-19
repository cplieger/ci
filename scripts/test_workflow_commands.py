"""Every workflow command a script writes goes through workflow_commands, escaped."""

from __future__ import annotations

import ast
import importlib.util
import io
import pathlib
import re
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

import intake
import mutation_cost
import runner_images
import workflow_commands as wc

SCRIPTS = pathlib.Path(__file__).resolve().parent
HOSTILE = '100%\r\n::add-mask::x'
ESCAPED = '100%25%0D%0A::add-mask::x'
OPENS_COMMAND = re.compile(r'::[a-z-]*(::| |$)')
# A shell command literal, then data on the same line: an expansion or a printf conversion.
SHELL_RAW = re.compile(r'::\$|::(?:error|warning|notice|group)\b[^\n]*?(?:\$|%[-+ #0-9.]*[a-zA-Z])')


def load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / f'{name}.py')
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
        ga = load('gorelease-accept')

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


class NoRawCommands(unittest.TestCase):
    def test_no_script_writes_a_command_outside_the_helper(self):
        scripts = sorted(
            p
            for p in SCRIPTS.glob('*.py')
            if not p.name.startswith('test') and p.name != 'workflow_commands.py'
        )
        found = {p.name: lines for p in scripts if (lines := command_literals(p))}
        self.assertEqual(found, {}, 'write these through workflow_commands.command')
        self.assertGreater(len(scripts), 20)

    def test_no_shell_script_writes_a_command_with_data_outside_the_helper(self):
        scripts = sorted(
            p
            for p in SCRIPTS.glob('*.sh')
            if not p.name.startswith('test') and p.name != 'gha-log.sh'
        )
        found = {p.name: lines for p in scripts if (lines := shell_command_lines(p.read_text()))}
        self.assertEqual(found, {}, 'write these through gha-log.sh gha_annotate')
        self.assertGreater(len(scripts), 20)

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
