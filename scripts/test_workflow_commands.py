"""Every workflow command a script writes goes through workflow_commands, escaped."""

from __future__ import annotations

import ast
import importlib.util
import io
import pathlib
import re
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
