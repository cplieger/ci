#!/usr/bin/env python3
"""Fail when docs/monitoring.md's alert table and the alerts/*.yaml rule files name different alerts.

Run from the repository root. The alert table is every Markdown table whose
first header cell is `Alert`, outside fenced code; a row's alert names are the
backticked names in its first cell. A repository without both the page and a
rule file skips the check.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

PAGE = pathlib.Path('docs/monitoring.md')
HEADER = re.compile(r'^\|\s*Alert\s*\|')
SEPARATOR = re.compile(r'^\|[\s:|-]+$')
FENCE = re.compile(r'^\s*(```|~~~)')
NAME = re.compile(r'`([^`]+)`')


def error(path: str, message: str) -> None:
    print(f'::error file={path}::{message}')


def rule_alerts(paths: list[pathlib.Path]) -> tuple[dict[str, list[str]], bool]:
    """Every alert name the rule files define, with the files defining it."""
    found: dict[str, list[str]] = {}
    ok = True
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            error(path.as_posix(), f'{path.as_posix()} is not valid YAML: {exc}')
            ok = False
            continue
        groups = doc.get('groups') if isinstance(doc, dict) else None
        for group in groups or []:
            for rule in (group or {}).get('rules') or []:
                if isinstance(rule, dict) and 'alert' in rule:
                    found.setdefault(str(rule['alert']), []).append(path.as_posix())
    return found, ok


def table_alerts(text: str) -> tuple[set[str], bool]:
    """The alert names the page's alert tables list, and whether it has one."""
    names: set[str] = set()
    has_table = in_table = in_fence = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            in_table = False
            continue
        if in_fence:
            continue
        if HEADER.match(line):
            has_table = in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith('|'):
            in_table = False
            continue
        if SEPARATOR.match(line.strip()):
            continue
        first = line.strip().strip('|').split('|')[0]
        names.update(n for n in NAME.findall(first) or [first.strip()] if n)
    return names, has_table


def main() -> int:
    rules = sorted(pathlib.Path('alerts').glob('*.yaml'))
    if not PAGE.is_file() or not rules:
        print('alert table: needs both docs/monitoring.md and alerts/*.yaml - skipping')
        return 0
    defined, ok = rule_alerts(rules)
    listed, has_table = table_alerts(PAGE.read_text())
    page = PAGE.as_posix()
    if not has_table:
        error(page, f'{page} has no alert table, a table whose first column is headed Alert')
        return 1
    for name in sorted(listed - defined.keys()):
        error(
            page, f'{page} lists `{name}` in its alert table, but no alerts/*.yaml rule defines it'
        )
        ok = False
    for name in sorted(defined.keys() - listed):
        error(
            page,
            f'{", ".join(defined[name])} defines `{name}`, but the alert table of {page} does not list it',
        )
        ok = False
    if ok:
        print(f'alert table: {page} lists the {len(defined)} alerts of {len(rules)} rule file(s)')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
