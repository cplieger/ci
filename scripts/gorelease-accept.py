#!/usr/bin/env python3
"""Apply a repository's api-accept.txt to a gorelease report (release.yaml's API compatibility step).

`gorelease-accept.py --verdict ok|api --report R --accept A --base vX.Y.Z --version vX.Y.Z
[--existing VERSIONS] [--self VERSION] --summary S` reads A, whose non-comment lines each name one accepted incompatible change as
`<package import path>: <report line>`, matched exactly against the lines gorelease lists under
`## incompatible changes` for that package. Under `--verdict api` (gorelease rejected the
version) it exits 0 only when every incompatible line is accepted and nothing else in the report
rejects the version, else 1. Under `--verdict ok` it exits 0. Either way an entry that matches no
incompatible line gets a warning, and S gets a Markdown account of the result. Exits 2 when A is
malformed or an argument is invalid.

--existing lists the module's published versions (`go list -m -versions`), whitespace-separated.
--self is the version of the module that a module requiring it at --version selects, without the
module's own replace and exclude directives (gorelease's `go list -m` self-dependency lookup). An
omitted one is a lookup the caller could not make, and it blocks acceptance.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ACCEPT_FILE = 'api-accept.txt'
INCOMPATIBLE = '## incompatible changes'
PACKAGE_SECTIONS = {
    '## errors in base version:': 'base_errors',
    '## errors in release version:': 'release_errors',
    INCOMPATIBLE: 'incompatible',
    '## compatible changes': 'compatible',
}
STABLE = re.compile(r'v(\d+)\.(\d+)\.(\d+)')
SEMVER = re.compile(r'v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?')


class InputError(Exception):
    """An argument or api-accept.txt line this script cannot use (exit 2)."""


class ReportError(Exception):
    """A report whose shape the parser does not know, so nothing in it can be accepted."""


@dataclass
class Package:
    base_errors: list[str] = field(default_factory=list)
    release_errors: list[str] = field(default_factory=list)
    incompatible: list[str] = field(default_factory=list)
    compatible: list[str] = field(default_factory=list)


@dataclass
class Report:
    packages: dict[str, Package] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Entry:
    lineno: int
    package: str
    change: str


@dataclass
class Result:
    accepted: list[Entry] = field(default_factory=list)
    stale: list[Entry] = field(default_factory=list)
    duplicates: list[tuple[Entry, int]] = field(default_factory=list)
    unaccepted: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def parse_report(text: str) -> Report:
    """Parse gorelease's report.String() at the pinned x/exp (cmd/gorelease/report.go)."""
    report = Report()
    block: list[str] | None = None
    package: Package | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith('# '):
            name = line[2:]
            package = None
            if name == 'summary':
                block = report.summary
            elif name == 'diagnostics':
                block = report.diagnostics
            elif name in report.packages or not name or ' ' in name:
                raise ReportError(f'unexpected package header {line!r}')
            else:
                package = report.packages[name] = Package()
                block = None
        elif line.startswith('##'):
            if package is None or line not in PACKAGE_SECTIONS:
                raise ReportError(f'unexpected section {line!r}')
            block = getattr(package, PACKAGE_SECTIONS[line])
        elif block is None:
            raise ReportError(f'line outside any section: {line!r}')
        else:
            block.append(line)
    return report


def parse_accept(text: str) -> list[Entry]:
    entries = []
    for lineno, raw in enumerate(text.removeprefix('\ufeff').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        package, sep, change = line.partition(': ')
        package, change = package.strip(), change.strip()
        if not sep or not package or not change or any(c.isspace() or c == ':' for c in package):
            raise InputError(
                f'{ACCEPT_FILE} line {lineno} is not `<package import path>: <gorelease line>`: '
                f'{line!r}'
            )
        entries.append(Entry(lineno, package, change))
    return entries


def stable(version: str) -> tuple[int, int, int]:
    m = STABLE.fullmatch(version)
    if not m:
        raise InputError(f'{version!r} is not a stable vMAJOR.MINOR.PATCH version')
    return int(m[1]), int(m[2]), int(m[3])


def semver_key(version: str) -> tuple:
    """A sort key in semver precedence, where a pre-release sorts below its release."""
    m = SEMVER.fullmatch(version)
    if not m:
        raise InputError(f'{version!r} is not a semantic version')
    pre = m[4].split('.') if m[4] else []
    return (
        int(m[1]),
        int(m[2]),
        int(m[3]),
        not pre,
        [(0, int(i), '') if i.isdigit() else (1, 0, i) for i in pre],
    )


def versions(value: str | None) -> list[str] | None:
    if value is None:
        return None
    listed = value.split()
    for v in listed:
        semver_key(v)
    return listed


def apply(
    report: Report,
    entries: list[Entry],
    base: str,
    version: str,
    existing: list[str] | None,
    self_version: str | None,
) -> Result:
    """Match the entries, then recompute the verdict gorelease gave without the accepted lines."""
    result = Result()
    present = {(p, c) for p, pkg in report.packages.items() for c in pkg.incompatible}
    first: dict[tuple[str, str], int] = {}
    for e in entries:
        key = (e.package, e.change)
        if (line := first.setdefault(key, e.lineno)) != e.lineno:
            result.duplicates.append((e, line))
        else:
            (result.accepted if key in present else result.stale).append(e)
    wanted = first.keys()
    for path, pkg in report.packages.items():
        result.unaccepted += [f'{path}: {c}' for c in pkg.incompatible if (path, c) not in wanted]
        if pkg.base_errors or pkg.release_errors:
            result.reasons.append(f'{path} has package load errors')
    if report.diagnostics:
        result.reasons.append('gorelease reported diagnostics')
    # In validateReleaseVersion (cmd/gorelease/report.go) this summary overwrites an existing-version
    # one and returns before the minor-increment and self-dependency checks; those three follow.
    if report.summary != [
        f'{version} is not a valid semantic version for this release.',
        'There are incompatible changes.',
    ]:
        result.reasons.append('gorelease rejected the version for a reason other than its changes')
    if stable(version)[:2] <= stable(base)[:2]:
        result.reasons.append(f'{version} does not increment the minor version over {base}')
    if existing is None:
        result.reasons.append('the published versions could not be listed (go list -m -versions)')
    elif any(semver_key(v) == semver_key(version) for v in existing):
        result.reasons.append(f'version {version} already exists')
    if self_version is None:
        result.reasons.append(
            'the version a dependent module selects could not be listed (go list -m)'
        )
    elif semver_key(self_version) > semver_key(version):
        result.reasons.append(
            f'the module indirectly depends on a higher version of itself ({self_version})'
        )
    return result


def escape(value: str) -> str:
    """A workflow command's message escaping (%, CR, LF)."""
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def fenced(title: str, lines: list[str]) -> list[str]:
    return [title, '', '```text', *lines, '```', ''] if lines else []


def render(result: Result, rejected: bool) -> list[str]:
    matched = [f'{e.package}: {e.change}' for e in result.accepted]
    stale = [f'line {e.lineno}: {e.package}: {e.change}' for e in result.stale]
    out = [f'#### {ACCEPT_FILE}', '']
    if rejected:
        out += fenced('Accepted incompatible changes:', matched)
        out += fenced('Incompatible changes it does not accept:', result.unaccepted)
    else:
        out += fenced('Matching entries, not needed (gorelease accepted the version):', matched)
    out += fenced('Entries that match no incompatible change:', stale)
    if rejected:
        out += fenced('Why the accepted list cannot pass the release:', result.reasons)
    return out


def annotate(level: str, message: str, lineno: int | None = None) -> None:
    where = f' file={ACCEPT_FILE},line={lineno}' if lineno else ''
    print(f'::{level}{where}::{escape(message)}')


def run(
    verdict: str,
    report_text: str,
    accept_text: str,
    base: str,
    version: str,
    existing: str | None = None,
    self_version: str | None = None,
):
    """The exit code and the summary lines; annotations go to stdout."""
    entries = parse_accept(accept_text)
    stable(base)
    stable(version)
    published = versions(existing)
    if self_version is not None:
        semver_key(self_version)
    rejected = verdict == 'api'
    try:
        report = parse_report(report_text)
    except ReportError as exc:
        message = f'cannot read the gorelease report, so {ACCEPT_FILE} was not applied: {exc}'
        annotate('error' if rejected else 'warning', message)
        return (1 if rejected else 0), [f'#### {ACCEPT_FILE}', '', message]
    result = apply(report, entries, base, version, published, self_version)
    for e, line in result.duplicates:
        annotate('warning', f'duplicate of line {line}: {e.package}: {e.change}', e.lineno)
    for e in result.stale:
        annotate(
            'warning',
            f'{e.package}: {e.change} matches no incompatible change gorelease reported; '
            'delete the entry',
            e.lineno,
        )
    passed = not rejected or (not result.unaccepted and not result.reasons)
    return (0 if passed else 1), render(result, rejected)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--verdict', required=True, choices=('ok', 'api'))
    p.add_argument('--report', required=True, type=Path)
    p.add_argument('--accept', required=True, type=Path)
    p.add_argument('--base', required=True)
    p.add_argument('--version', required=True)
    p.add_argument('--existing')
    p.add_argument('--self', dest='self_version')
    p.add_argument('--summary', required=True, type=Path)
    args = p.parse_args()
    try:
        code, lines = run(
            args.verdict,
            args.report.read_text(),
            args.accept.read_text(),
            args.base,
            args.version,
            args.existing,
            args.self_version,
        )
    except InputError as exc:
        print(f'::error::{escape(str(exc))}')
        return 2
    text = '\n'.join(lines) + '\n'
    with args.summary.open('a') as f:
        f.write(text)
    sys.stdout.write(text)
    return code


if __name__ == '__main__':
    sys.exit(main())
