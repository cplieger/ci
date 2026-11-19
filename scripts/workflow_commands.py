"""Workflow command lines whose values cannot end the line or open another command.

The escaping is the Actions toolkit's:
https://github.com/actions/toolkit/blob/main/packages/core/src/command.ts
"""

from __future__ import annotations

NAMES = frozenset({'error', 'warning', 'notice', 'group', 'endgroup'})


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
