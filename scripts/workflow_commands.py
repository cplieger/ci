"""Workflow command lines, and plain log lines, whose values cannot end the line or open a command.

The escaping is the Actions toolkit's:
https://github.com/actions/toolkit/blob/main/packages/core/src/command.ts
"""

from __future__ import annotations

import sys
import traceback
from string.templatelib import Template, convert
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

NAMES = frozenset({'error', 'warning', 'notice', 'group', 'endgroup'})
# Every plain log line say writes starts with this, so the runner never reads one as a command.
SAY_PREFIX = '- '


def escape_data(value: str) -> str:
    """VALUE as a command's message: %, CR and LF escaped."""
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def escape_property(value: str) -> str:
    """VALUE as a command's property value: the message escaping plus : and ,."""
    return escape_data(value).replace(':', '%3A').replace(',', '%2C')


def command(name: str, message: object = '', **properties: object) -> str:
    """The `::NAME key=value,...::MESSAGE` line; a property whose value is falsy is left out, as the toolkit does."""
    if name not in NAMES:
        raise ValueError(f'{name!r} is not a workflow command this module writes')
    props = ','.join(f'{k}={escape_property(str(v))}' for k, v in properties.items() if v)
    return f'::{name}{" " + props if props else ""}::{escape_data(str(message))}'


def render(msg: Template, quote: Callable[[str], str] = str) -> str:
    """MSG's text, each interpolated value passed through QUOTE and each literal part kept.

    A t-string's literal parts are what the script wrote and its values are
    data, so this is the one place data gets its escaping.
    """
    if not isinstance(msg, Template):
        raise TypeError(f'a log line is a t-string, not {type(msg).__name__}')
    out = []
    for part in msg:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part.value, Template):
            out.append(render(part.value, quote))
        else:
            out.append(quote(format(convert(part.value, part.conversion), part.format_spec)))
    return ''.join(out)


def say(msg: Template, *, file: TextIO | None = None) -> None:
    """Log MSG as one plain line behind SAY_PREFIX, each value escaped as a message is (gha-log.sh gha_say's twin).

    The runner also reads `##[command]` anywhere in a line that is not a `::` command
    (actions/runner ActionCommand.TryParse) and never unescapes a plain line, so every
    `##[` is broken, the script's own text included.
    """
    line = SAY_PREFIX + render(msg, escape_data)
    print(line.replace('##[', '#%23['), file=file)


def say_lines(text: str, *, file: TextIO | None = None) -> None:
    """Log each line of TEXT through say."""
    for line in text.splitlines():
        say(t'{line}', file=file)


def run_main(main: Callable[[], int | None]) -> int:
    """MAIN's exit code; a SystemExit message is written as an error command and an
    uncaught error's traceback through say, so neither starts a command of its own."""
    try:
        return main() or 0
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            return exc.code or 0
        print(command('error', exc.code), file=sys.stderr)
        return 1
    except Exception:
        for line in traceback.format_exc().splitlines():
            say(t'{line}', file=sys.stderr)
        return 1
