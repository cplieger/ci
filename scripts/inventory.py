#!/usr/bin/env python3
"""Resolved-dependency inventory of a commit, read from git objects as data.

  records    the records of one revision (JSON)
  surfaces   the package files the engine reads at one revision (JSON); exit 2
             names each one that does not parse or holds an unordered value
  diff       what changed between two revisions (JSON, or the release-notes block)
  dominance  whether a dev commit carries everything main shipped since their
             merge base; exit 1 names the failing paths and records, exit 2 an
             uncomparable record, an unreadable revision or a malformed file

Nothing in the repository is executed. Exit 2 on every engine or input error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path


class InventoryError(Exception):
    """An input the engine cannot read; the CLI maps it to exit 2."""


class GitReadError(InventoryError):
    """git could not read the repository, as opposed to a file that does not parse."""


@dataclass(frozen=True, order=True)
class Record:
    lane: str
    ecosystem: str
    file: str
    identity: str
    value: str
    digest: str = ''
    kind: str = 'direct'
    versioning: str = ''

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.identity, self.value, self.digest)

    @property
    def slot(self) -> tuple[str, str, str]:
        """What dominance matches across revisions: a dependency keeps its slot
        when it moves between direct, indirect and dev."""
        role = 'dep' if self.kind in ('direct', 'indirect', 'dev') else self.kind
        return (self.ecosystem, self.identity, role)

    @property
    def state(self) -> tuple[str, str, str]:
        return (self.value, self.digest, self.kind)


def _sign(a, b) -> int:
    return (a > b) - (a < b)


_NUM = re.compile(r'0|[1-9]\d*')
_IDENT = re.compile(r'[0-9A-Za-z-]+')
# The same arity as a prerelease key's (0, identifiers), and above every one of them.
_NO_PRERELEASE = (1, ())


def _prerelease(text: str | None):
    """Semver 2.0.0 precedence key (https://semver.org/#spec-item-11); None means no prerelease (ranks highest)."""
    if text is None:
        return _NO_PRERELEASE
    key = []
    for ident in text.split('.'):
        if not _IDENT.fullmatch(ident):
            raise ValueError(ident)
        if ident.isdigit():
            if not _NUM.fullmatch(ident):
                raise ValueError(ident)
            key.append((0, int(ident), ''))
        else:
            key.append((1, 0, ident))
    return (0, tuple(key))


_SEMVER = re.compile(
    r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
    r'(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
)


def _semver_key(text: str):
    m = _SEMVER.fullmatch(text)
    if not m:
        return None
    try:
        return (int(m[1]), int(m[2]), int(m[3]), _prerelease(m[4]))
    except ValueError:
        return None


def cmp_semver(a: str, b: str) -> int | None:
    """npm resolved versions: strict semver 2.0.0, build metadata ignored."""
    ka, kb = _semver_key(a), _semver_key(b)
    return None if ka is None or kb is None else _sign(ka, kb)


_GO_SHORT = re.compile(r'v(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?')


def _go_semver_key(text: str):
    # golang.org/x/mod/semver: `v` prefix, `v1` and `v1.2` shorthands without
    # prerelease, `+incompatible` is build metadata and does not order.
    m = _GO_SHORT.fullmatch(text)
    if m:
        return (int(m[1]), int(m[2] or 0), 0, _NO_PRERELEASE)
    if not text.startswith('v'):
        return None
    return _semver_key(text[1:])


def cmp_go_semver(a: str, b: str) -> int | None:
    ka, kb = _go_semver_key(a), _go_semver_key(b)
    return None if ka is None or kb is None else _sign(ka, kb)


_GO_VERSION = re.compile(r'(?:go)?(\d+)(?:\.(\d+))?(?:\.(\d+)|(alpha|beta|rc)(\d+))?')
_GO_PRE_RANK = {'alpha': 1, 'beta': 2, 'rc': 3}


def _go_version_key(text: str):
    # go/version ordering: a language version `1.21` sorts below `1.21rc1`,
    # which sorts below the release `1.21.0`.
    m = _GO_VERSION.fullmatch(text)
    if not m:
        return None
    major, minor = int(m[1]), int(m[2] or 0)
    if m[3] is not None:
        return (major, minor, 4, int(m[3]))
    if m[4] is not None:
        return (major, minor, _GO_PRE_RANK[m[4]], int(m[5]))
    return (major, minor, 0, 0)


def cmp_go_version(a: str, b: str) -> int | None:
    ka, kb = _go_version_key(a), _go_version_key(b)
    return None if ka is None or kb is None else _sign(ka, kb)


_PEP440 = re.compile(
    r"""v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>\d+)?)?
    (?:-(?P<post_n1>\d+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d+)?)?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>\d+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?""",
    re.VERBOSE | re.IGNORECASE,
)
_PEP440_PRE = {'a': 0, 'alpha': 0, 'b': 1, 'beta': 1, 'c': 2, 'pre': 2, 'preview': 2, 'rc': 2}


def _pep440_key(text: str):
    """PEP 440 ordering, the key `packaging.version` builds, stdlib only."""
    m = _PEP440.fullmatch(text.strip())
    if not m:
        return None
    release = [int(p) for p in m['release'].split('.')]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    has_post = m['post_n1'] is not None or m['post_l'] is not None
    has_dev = m['dev'] is not None
    if m['pre_l'] is not None:
        pre = (1, _PEP440_PRE[m['pre_l'].lower()], int(m['pre_n'] or 0))
    elif has_dev and not has_post:
        pre = (0,)
    else:
        pre = (2,)
    post = (1, int(m['post_n1'] or m['post_n2'] or 0)) if has_post else (0,)
    dev = (0, int(m['dev_n'] or 0)) if has_dev else (1,)
    if m['local'] is None:
        local = (0,)
    else:
        parts = re.split(r'[-_.]', m['local'].lower())
        local = (1, tuple((1, int(p), '') if p.isdigit() else (0, 0, p) for p in parts))
    return (int(m['epoch'] or 0), tuple(release), pre, post, dev, local)


def cmp_pep440(a: str, b: str) -> int | None:
    ka, kb = _pep440_key(a), _pep440_key(b)
    return None if ka is None or kb is None else _sign(ka, kb)


_DOCKER_TAG = re.compile(r'v?(\d+(?:\.\d+)*)(.*)')


def cmp_docker(a: str, b: str) -> int | None:
    """Numeric tags with the same variant suffix (`3.24.2`, `1.27-alpine`)."""
    ma, mb = _DOCKER_TAG.fullmatch(a), _DOCKER_TAG.fullmatch(b)
    if not ma or not mb or ma[2] != mb[2]:
        return None
    na = [int(p) for p in ma[1].split('.')]
    nb = [int(p) for p in mb[1].split('.')]
    if len(na) != len(nb):
        # `3.24` and `3.24.2` are different moving tags, not two orders of one.
        return None
    return _sign(na, nb)


_LOOSE = re.compile(
    r'(?P<prefix>[A-Za-z_]*?)(?P<nums>\d+(?:\.\d+)*)(?:[-.]?(?P<pre>[A-Za-z][0-9A-Za-z.-]*))?(?:\+.*)?'
)


def _loose_key(text: str):
    m = _LOOSE.fullmatch(text)
    if not m:
        return None
    prefix = m['prefix'].lower()
    nums = [int(p) for p in m['nums'].split('.')]
    try:
        pre = _prerelease(m['pre'].replace('-', '.') if m['pre'] else None)
    except ValueError:
        return None
    return ('' if prefix == 'v' else prefix), nums, pre


def cmp_loose(a: str, b: str) -> int | None:
    """Pinned versions of any datasource: dotted numbers, optional prerelease."""
    ka, kb = _loose_key(a), _loose_key(b)
    if ka is None or kb is None or ka[0] != kb[0]:
        return None
    width = max(len(ka[1]), len(kb[1]))
    na = ka[1] + [0] * (width - len(ka[1]))
    nb = kb[1] + [0] * (width - len(kb[1]))
    return _sign((na, ka[2]), (nb, kb[2]))


# Each comparator returns -1, 0 or 1, or None when either side does not parse.
COMPARATORS = {
    'semver': cmp_semver,
    'go-semver': cmp_go_semver,
    'go-version': cmp_go_version,
    'pep440': cmp_pep440,
    'docker': cmp_docker,
    'loose': cmp_loose,
}
# A value of these ecosystems always has an order; one that does not parse is
# an engine gap (exit 2), never something history may excuse.
STRICT_VERSIONING = frozenset({'semver', 'go-semver', 'go-version', 'pep440'})

_RANGE_VERSION = re.compile(r'(?<![\w.])v?(\d+)(?:\.(?:\d+|[xX*]))*')
_RANGE_UNORDERED = re.compile(r'[a-z+]+:|^\s*$|^\s*[*xX]\s*$|^[A-Za-z]')


def _range_lines(value: str) -> frozenset | None:
    if _RANGE_UNORDERED.search(value):
        return None
    return frozenset(int(m[1]) for m in _RANGE_VERSION.finditer(value)) or None


def _line_of(key, pick) -> frozenset | None:
    return None if key is None else frozenset({pick(key)})


def release_lines(versioning: str, value: str) -> frozenset | None:
    """The release lines a value belongs to; leaving them is a breaking update.
    A line is the major, except for a Go version, where it is the minor, and a
    manifest range can admit several. None when the value has no version order."""
    lines = {
        'semver': lambda: _line_of(_semver_key(value), lambda k: k[0]),
        'go-semver': lambda: _line_of(_go_semver_key(value), lambda k: k[0]),
        'go-version': lambda: _line_of(_go_version_key(value), lambda k: k[:2]),
        'pep440': lambda: _line_of(_pep440_key(value), lambda k: (k[0], k[1][0])),
        'loose': lambda: _line_of(_loose_key(value), lambda k: (k[0], k[1][0])),
        'docker': lambda: _line_of(
            _DOCKER_TAG.fullmatch(value), lambda m: (int(m[1].split('.')[0]), m[2])
        ),
        'range': lambda: _range_lines(value),
    }.get(versioning)
    return lines() if lines else None


# Renovate's :ignoreModulesAndTests (config:recommended), which the two-branch
# preset also applies on main, so the engine reads exactly the package files
# Renovate updates on main:
# https://docs.renovatebot.com/presets-default/#ignoremodulesandtests
IGNORED_DIRS = frozenset(
    {
        'node_modules',
        'bower_components',
        'vendor',
        'examples',
        '__tests__',
        'test',
        'tests',
        '__fixtures__',
    }
)
# Renovate's dockerfile manager file patterns:
# https://docs.renovatebot.com/modules/manager/dockerfile/
_DOCKERFILE = re.compile(
    r'(?:^|/|\.)(?:[Dd]ocker|[Cc]ontainer)file$|(?:^|/)(?:[Dd]ocker|[Cc]ontainer)file[^/]*$'
)
_SURFACE_NAMES = {
    'go.mod': 'gomod',
    'go.sum': 'gosum',
    'package.json': 'package-json',
    'package-lock.json': 'npm-lock',
    'npm-shrinkwrap.json': 'npm-lock',
    'uv.lock': 'uv-lock',
    'bundled-tools.json': 'bundled-tools',
    'entrypoint.sh': 'pins',
    'registries.env': 'pins',
}


# Trees a Renovate postUpgradeTask regenerates in the commit that moves one pin,
# by path prefix: no inventory surface, but a function of that pin's version. The
# cplieger/.github two-branch preset's rule for the pin lists the tree in fileFilters.
REGENERATED_TREES = {'licenses/crates/': 'pkolaczk/fclones'}


def regenerated_by(path: str) -> str | None:
    """The pin whose update regenerates `path`, or None."""
    return next((dep for tree, dep in REGENERATED_TREES.items() if path.startswith(tree)), None)


def surface_kind(path: str) -> str | None:
    """The parser for a path, or None when the path is no inventory surface.

    Workflow and composite-action pins are not surfaces: they ship nothing."""
    parts = path.split('/')
    if parts[0] == '.github' or IGNORED_DIRS.intersection(parts[:-1]):
        return None
    kind = _SURFACE_NAMES.get(parts[-1])
    if kind:
        return kind
    return 'dockerfile' if _DOCKERFILE.search(path) else None


def discover_lanes(paths: list[str]) -> list[str]:
    """Nested Go module lanes, by release.yaml's `Detect nested Go modules` rules."""
    lanes = []
    pruned = re.compile(r'(^|/)(node_modules|vendor|testdata|static|dist)/')
    for path in paths:
        if not path.endswith('/go.mod') or pruned.search(path):
            continue
        d = posixpath.dirname(path)
        if re.search(r'[^A-Za-z0-9._/-]', d) or '/internal/' in f'/{d}/':
            continue
        lanes.append(d)
    return sorted(lanes, key=len, reverse=True)


def lane_of(path: str, lanes: list[str]) -> str:
    for lane in lanes:
        if path.startswith(lane + '/'):
            return lane
    return '.'


# Every parser returns (records without lane, skeleton). The skeleton is the
# file with every recorded value masked or every recorded entry removed: two
# revisions whose skeletons differ changed something that is not a record.
_MASK = '\x00'
_HEX = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    out, pos = [], 0
    for start, end in sorted(spans):
        out.append(text[pos:start])
        out.append(_MASK)
        pos = end
    out.append(text[pos:])
    return ''.join(out)


def _go_mod_module(text: str) -> str:
    m = re.search(r'^\s*module\s+"?([^\s"]+)', text, re.MULTILINE)
    return m[1] if m else ''


def parse_go_mod(path: str, text: str) -> tuple[list[Record], str]:
    records: list[Record] = []
    kept: list[str] = []
    block = ''
    for raw in text.splitlines():
        code, _, comment = raw.partition('//')
        words = code.split()
        if block:
            if words == [')']:
                block = ''
            elif words:
                records += _go_directive(path, block, words, comment)
            continue
        if not words:
            if comment.strip():
                kept.append(raw.rstrip())
            continue
        directive = words[0]
        if directive in ('require', 'replace', 'exclude'):
            if words[1:] == ['(']:
                block = directive
            else:
                records += _go_directive(path, directive, words[1:], comment)
        elif directive in ('go', 'toolchain') and len(words) == 2:
            value = words[1].removeprefix('go') if directive == 'toolchain' else words[1]
            records.append(
                Record('', 'go', path, directive, value, kind='directive', versioning='go-version')
            )
        else:
            kept.append(raw.rstrip())
    if block:
        raise InventoryError(f'{path}: {block} block is not closed')
    return records, '\n'.join(kept)


def _go_directive(path: str, directive: str, words: list[str], comment: str) -> list[Record]:
    if directive == 'require':
        if len(words) != 2:
            raise InventoryError(f'{path}: malformed require: {" ".join(words)}')
        kind = 'indirect' if comment.strip().startswith('indirect') else 'direct'
        return [Record('', 'go', path, words[0], words[1], kind=kind, versioning='go-semver')]
    if directive == 'exclude':
        if len(words) != 2:
            raise InventoryError(f'{path}: malformed exclude: {" ".join(words)}')
        return [Record('', 'go', path, f'exclude {words[0]} {words[1]}', '', kind='exclude')]
    if '=>' not in words:
        raise InventoryError(f'{path}: malformed replace: {" ".join(words)}')
    at = words.index('=>')
    old, new = words[:at], words[at + 1 :]
    if not 1 <= len(old) <= 2 or not 1 <= len(new) <= 2:
        raise InventoryError(f'{path}: malformed replace: {" ".join(words)}')
    # The target path is part of the identity: two targets have no order.
    identity = f'replace {"@".join(old)} => {new[0]}'
    if len(new) == 1:
        return [Record('', 'go', path, identity, '', kind='replace')]
    return [Record('', 'go', path, identity, new[1], kind='replace', versioning='go-semver')]


def _json(path: str, text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InventoryError(f'{path}: not valid JSON: {exc}') from None


def _canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(',', ':'), default=str)


def _section(path: str, value, what: str) -> dict:
    """A structured section: `{}` when absent, InventoryError when not a mapping."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InventoryError(f'{path}: {what} is not a mapping')
    return value


def _array(path: str, value, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InventoryError(f'{path}: {what} is not an array')
    return value


def _string(path: str, value, what: str) -> str:
    if not isinstance(value, str):
        raise InventoryError(f'{path}: {what} is not a string')
    return value


def _flag(path: str, entry: dict, field: str, key: str) -> bool:
    value = entry.get(field, False)
    if not isinstance(value, bool):
        raise InventoryError(f'{path}: {key} {field} is not a boolean')
    return value


_PKG_SECTIONS = {
    'dependencies': 'direct',
    'optionalDependencies': 'direct',
    'peerDependencies': 'direct',
    'devDependencies': 'dev',
}


def _overrides(path: str, node, prefix: str):
    if isinstance(node, str):
        yield prefix, node
        return
    for name, value in _section(path, node, f'overrides {prefix}'.rstrip()).items():
        child = f'{prefix}>{name}' if prefix else name
        yield from _overrides(path, value, child)


def _npm_identity(install: str, package: str) -> str:
    """An alias keeps its install name beside the package it resolves to, so
    retargeting it is another dependency rather than a version change."""
    return install if package == install else f'{install}@npm:{package}'


def _npm_alias(path: str, name: str, spec: str, what: str) -> tuple[str, str]:
    """(identity, range) of a manifest dependency; `npm:<package>[@<range>]` is an alias."""
    if not spec.startswith('npm:'):
        return name, spec
    package, at, version = spec[4:], '', ''
    split = package.find('@', 1)
    if split != -1:
        package, at, version = package[:split], '@', package[split + 1 :]
    if not package or (at and not version):
        raise InventoryError(f'{path}: {what} is not a valid npm alias')
    return _npm_identity(name, package), version


def parse_package_json(path: str, text: str) -> tuple[list[Record], str]:
    """Ranges and overrides are recorded for notes; dominance does not judge them."""
    data = _json(path, text)
    if not isinstance(data, dict):
        raise InventoryError(f'{path}: not a JSON object')
    records = []
    for section, kind in _PKG_SECTIONS.items():
        for name, spec in _section(path, data.pop(section, None), section).items():
            what = f'{section} {name}'
            identity, spec = _npm_alias(path, name, _string(path, spec, what), what)
            records.append(
                Record('', 'npm-range', path, identity, spec, kind=kind, versioning='range')
            )
    for name, spec in _overrides(
        path, _section(path, data.pop('overrides', None), 'overrides'), ''
    ):
        records.append(
            Record(
                '', 'npm-range', path, f'overrides {name}', spec, kind='direct', versioning='range'
            )
        )
    manager = data.pop('packageManager', None)
    if manager is not None:
        name, _, version = _string(path, manager, 'packageManager').rpartition('@')
        records.append(
            Record(
                '', 'npm', path, f'packageManager {name}', version, kind='pin', versioning='loose'
            )
        )
    return records, _canonical(data)


def parse_npm_lock(path: str, text: str) -> tuple[list[Record], str]:
    data = _json(path, text)
    packages = data.pop('packages', None) if isinstance(data, dict) else None
    if not isinstance(packages, dict):
        raise InventoryError(f'{path}: no `packages` map (lockfileVersion 2 or later required)')
    _section(path, data.pop('dependencies', None), 'dependencies')
    root = dict(_section(path, packages.get(''), 'packages[""]'))
    direct = set()
    for section, kind in _PKG_SECTIONS.items():
        what = f'packages[""].{section}'
        for name, spec in _section(path, root.pop(section, None), what).items():
            _string(path, spec, f'{what} {name}')
            if kind == 'direct':
                direct.add(name)
    records = []
    for key, entry in packages.items():
        if key == '':
            continue
        entry = _section(path, entry, f'packages[{key!r}]')
        install = key.rpartition('node_modules/')[2]
        name = _string(path, entry['name'], f'{key} name') if 'name' in entry else install
        # A key outside node_modules is a workspace directory, never an alias.
        if 'node_modules/' in key:
            name = _npm_identity(install, name)
        resolved = _string(path, entry.get('resolved', ''), f'{key} resolved')
        if _flag(path, entry, 'link', key):
            records.append(Record('', 'npm', path, name, f'link:{resolved}', kind='indirect'))
            continue
        if _flag(path, entry, 'dev', key):
            kind = 'dev'
        elif key == f'node_modules/{install}' and install in direct:
            kind = 'direct'
        else:
            kind = 'indirect'
        version = entry.get('version')
        if not isinstance(version, str):
            raise InventoryError(f'{path}: {key} has no version')
        integrity = _string(path, entry.get('integrity', ''), f'{key} integrity')
        # A git dependency carries no integrity; its locator names the locked commit.
        records.append(
            Record(
                '',
                'npm',
                path,
                name,
                version,
                integrity or resolved,
                kind=kind,
                versioning='semver',
            )
        )
    data['packages'] = {'': root}
    return records, _canonical(data)


_UV_LOCAL_SOURCES = ('editable', 'virtual', 'directory', 'path')


def parse_uv_lock(path: str, text: str) -> tuple[list[Record], str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InventoryError(f'{path}: not valid TOML: {exc}') from None
    packages = _array(path, data.pop('package', None), 'package')
    members, entries = [], []
    for pkg in packages:
        pkg = _section(path, pkg, 'a package entry')
        source = _section(path, pkg.get('source'), f'{pkg.get("name")} source')
        if any(k in source for k in _UV_LOCAL_SOURCES):
            _string(path, pkg.get('name'), 'a workspace member name')
            members.append(pkg)
        else:
            entries.append(pkg)

    def names(member: dict, field: str, grouped: bool) -> list[str]:
        value = member.get(field)
        groups = _section(path, value, field).values() if grouped else [value]
        return [
            _string(path, _section(path, d, f'{field} entry').get('name'), f'{field} name')
            for group in groups
            for d in _array(path, group, field)
        ]

    direct, dev = set(), set()
    for member in members:
        direct.update(names(member, 'dependencies', grouped=False))
        direct.update(names(member, 'optional-dependencies', grouped=True))
        dev.update(names(member, 'dev-dependencies', grouped=True))
    records = []
    for pkg in entries:
        name, version = pkg.get('name'), pkg.get('version')
        if not isinstance(name, str) or not isinstance(version, str):
            raise InventoryError(f'{path}: package entry without name or version')
        # Every wheel, source, marker and edge of the entry is resolved state.
        rest = {k: v for k, v in pkg.items() if k not in ('name', 'version')}
        digest = 'lock-sha256:' + hashlib.sha256(_canonical(rest).encode()).hexdigest()
        kind = 'direct' if name in direct else 'dev' if name in dev else 'indirect'
        records.append(
            Record('', 'pypi', path, name, version, digest, kind=kind, versioning='pep440')
        )
    # A member's own fields restate pyproject.toml, which dominance judges as its own path.
    data['package'] = sorted(_canonical([m['name'], m.get('source', {})]) for m in members)
    return records, _canonical(data)


def parse_bundled_tools(path: str, text: str) -> tuple[list[Record], str]:
    """An entry is a pin when it carries `upstream`, the shape the Renovate manager reads."""
    data = _json(path, text)
    if not isinstance(data, dict):
        raise InventoryError(f'{path}: not a JSON object')
    records = []
    for name, entry in _section(path, data.get('entries'), 'entries').items():
        entry = _section(path, entry, f'entries {name}')
        if entry.get('upstream') is None:
            continue
        upstream = _section(path, entry['upstream'], f'{name} upstream')
        version = _string(path, entry.get('version'), f'{name} version')
        datasource = _string(path, upstream.get('datasource', 'pin'), f'{name} datasource')
        versioning = _string(path, upstream.get('versioning', ''), f'{name} versioning')
        records.append(
            Record(
                '',
                datasource,
                path,
                name,
                version,
                kind='pin',
                versioning='docker' if versioning == 'docker' else 'loose',
            )
        )
        entry['version'] = _MASK
    return records, _canonical(data)


_MARKER = re.compile(r'^\s*#\s*renovate:\s*(?P<attrs>.*?)\s*$')
_REPIN = re.compile(r'^\s*#\s*repin:\s*(?P<attrs>.*?)\s*$')
_ASSIGN = re.compile(
    r'^\s*(?:ARG\s+|export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<q>["\']?)(?P<val>[^"\'\s#]+)(?P=q)'
    r'(?P<trail>\s+#.*)?$'
)
# `# go1.27.1`, `# kiro-cli 2.27.1`: the version a digest line was resolved for.
_TRAILER_VERSION = re.compile(
    r'#\s*(?:[A-Za-z][A-Za-z-]*\s+)?[A-Za-z]*(?P<tver>\d[0-9A-Za-z.+-]*)\s*$'
)
_FROM = re.compile(
    r'^\s*FROM\s+(?:--platform=\S+\s+)?(?P<ref>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$', re.IGNORECASE
)
_FFMPEG = re.compile(r'^\s*ARG\s+FFMPEG_VERSION=(?P<val>\S+)\s*$')
_XCADDY = re.compile(r'--with\s+(?P<mod>github\.com/[^@\s]+)@(?P<ver>v\d+\.\d+\.\d+)')
_DATASOURCE_VERSIONING = {'docker': 'docker', 'go': 'go-semver', 'golang-version': 'go-version'}


def _attrs(text: str) -> dict[str, str]:
    return dict(tok.split('=', 1) for tok in text.split() if '=' in tok)


def _lines(text: str):
    pos = 0
    for line in text.splitlines(keepends=True):
        yield pos, line.rstrip('\r\n')
        pos += len(line)


def parse_pins(path: str, text: str, *, dockerfile: bool = False) -> tuple[list[Record], str]:
    """`# renovate:` pins with their digest lines, `# repin:` checksums, and
    for a Dockerfile its FROM references and the two marker-less managers."""
    lines = list(_lines(text))
    records: list[Record] = []
    checksums: list[tuple[str, Record]] = []
    spans: list[tuple[int, int]] = []
    stages: set[str] = set()
    owned: set[int] = set()
    i = 0
    while i < len(lines):
        offset, line = lines[i]
        marker = _MARKER.match(line)
        repin = _REPIN.match(line)
        if marker or repin:
            attrs = _attrs((marker or repin)['attrs'])
            group = []
            j = i + 1
            while j < len(lines) and (m := _ASSIGN.match(lines[j][1])):
                if group and not _HEX.fullmatch(m['val']):
                    break
                group.append((lines[j][0], m))
                j += 1
                if repin:
                    break
            if group and marker and 'depName' in attrs:
                records += _pin_records(path, attrs, group, spans)
                owned.update(range(i + 1, j))
            elif group and repin and 'dep' in attrs and _HEX.fullmatch(group[0][1]['val']):
                start, m = group[0]
                spans.append((start + m.start('val'), start + m.end('val')))
                checksums.append(
                    (
                        attrs['dep'],
                        Record('', 'pin', path, m['name'], '', m['val'], kind='checksum'),
                    )
                )
                owned.add(i + 1)
            i = j if group else i + 1
            continue
        if dockerfile and i not in owned:
            records += _docker_line(path, offset, line, stages, spans)
        i += 1
    by_dep = {r.identity: r for r in records if r.kind == 'pin'}
    for dep, record in checksums:
        paired = by_dep.get(dep)
        if paired:
            record = replace(
                record, ecosystem=paired.ecosystem, value=paired.value, versioning=paired.versioning
            )
        records.append(record)
    return records, _mask(text, spans)


def _pin_records(path: str, attrs: dict, group: list, spans: list) -> list[Record]:
    start, first = group[0]
    datasource = attrs.get('datasource', 'pin')
    versioning = (
        'docker'
        if attrs.get('versioning') == 'docker'
        else _DATASOURCE_VERSIONING.get(datasource, 'loose')
    )
    value, digest = first['val'], ''
    spans.append((start + first.start('val'), start + first.end('val')))
    trailer = _TRAILER_VERSION.search(first['trail'] or '') if _HEX.fullmatch(value) else None
    if trailer:
        at = start + first.start('trail') + trailer.start('tver')
        spans.append((at, at + len(trailer['tver'])))
        value, digest = trailer['tver'], first['val']
    elif _HEX.fullmatch(value):
        value, digest = attrs.get('branch', ''), value
        versioning = '' if 'branch' in attrs else versioning
    followers = []
    for line_start, m in group[1:]:
        spans.append((line_start + m.start('val'), line_start + m.end('val')))
        followers.append(m)
    if followers and not digest:
        digest = followers.pop(0)['val']
    pin = Record(
        '', datasource, path, attrs['depName'], value, digest, kind='pin', versioning=versioning
    )
    # Renovate captures one digest per marker; each further line is a checksum of that version.
    return [pin] + [
        Record(
            '', datasource, path, m['name'], value, m['val'], kind='checksum', versioning=versioning
        )
        for m in followers
    ]


def _docker_line(path: str, offset: int, line: str, stages: set, spans: list) -> list[Record]:
    m = _FROM.match(line)
    if m:
        ref = m['ref']
        known_stage = ref.lower() in stages
        if m['stage']:
            stages.add(m['stage'].lower())
        if known_stage or ref == 'scratch' or '$' in ref:
            return []
        spans.append((offset + m.start('ref'), offset + m.end('ref')))
        name, _, digest = ref.partition('@')
        image, tag = name, ''
        if ':' in name.rsplit('/', 1)[-1]:
            image, _, tag = name.rpartition(':')
        return [Record('', 'docker', path, image, tag, digest, kind='image', versioning='docker')]
    m = _FFMPEG.match(line)
    if m:
        spans.append((offset + m.start('val'), offset + m.end('val')))
        return [
            Record(
                '', 'github-tags', path, 'FFmpeg/FFmpeg', m['val'], kind='pin', versioning='loose'
            )
        ]
    found = []
    for m in _XCADDY.finditer(line):
        spans.append((offset + m.start('ver'), offset + m.end('ver')))
        found.append(Record('', 'go', path, m['mod'], m['ver'], kind='pin', versioning='go-semver'))
    return found


PARSERS = {
    'gomod': parse_go_mod,
    'package-json': parse_package_json,
    'npm-lock': parse_npm_lock,
    'uv-lock': parse_uv_lock,
    'bundled-tools': parse_bundled_tools,
    'pins': parse_pins,
    'dockerfile': lambda path, text: parse_pins(path, text, dockerfile=True),
}


class Repo:
    """Read-only view of one repository's objects, with parse results cached."""

    def __init__(self, git_dir: str):
        self.git_dir = git_dir
        self._parsed: dict[tuple[str, str], tuple[list[Record], str] | None] = {}
        self._trees: dict[str, list[str]] = {}
        self._present: dict[str, set[str]] = {}

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ['git', '-C', self.git_dir, *args], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise GitReadError(f'git {" ".join(args)}: {proc.stderr.strip()}')
        return proc.stdout

    def commit(self, rev: str) -> str:
        try:
            return self.git('rev-parse', '--verify', '--quiet', f'{rev}^{{commit}}').strip()
        except InventoryError:
            raise InventoryError(f'{rev}: not a commit in {self.git_dir}') from None

    def first_parent(self, sha: str) -> str | None:
        try:
            return self.git('rev-parse', '--verify', '--quiet', f'{sha}^1').strip()
        except InventoryError:
            return None

    def paths(self, sha: str) -> list[str]:
        if sha not in self._trees:
            self._trees[sha] = [
                p for p in self.git('ls-tree', '-r', '-z', '--name-only', sha).split('\0') if p
            ]
            self._present[sha] = set(self._trees[sha])
        return self._trees[sha]

    def blob(self, sha: str, path: str) -> str | None:
        """The file's text at `sha`; None only when the tree has no such path."""
        self.paths(sha)
        if path not in self._present[sha]:
            return None
        proc = subprocess.run(
            ['git', '-C', self.git_dir, 'cat-file', 'blob', f'{sha}:{path}'],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode('utf-8', errors='replace').strip()
            raise GitReadError(f'{path}: git cat-file blob {sha}:{path}: {stderr}')
        return proc.stdout.decode('utf-8', errors='surrogateescape')

    def parsed(self, sha: str, path: str) -> tuple[list[Record], str] | None:
        """(records, skeleton) of a surface file, None when the file is absent."""
        key = (sha, path)
        if key not in self._parsed:
            text = self.blob(sha, path)
            kind = surface_kind(path)
            if text is None or kind is None or kind == 'gosum':
                self._parsed[key] = None if text is None else ([], '')
            else:
                self._parsed[key] = PARSERS[kind](path, text)
        return self._parsed[key]

    def surfaces(self, sha: str) -> list[str]:
        return [p for p in self.paths(sha) if surface_kind(p) not in (None, 'gosum')]

    def lanes(self, sha: str) -> list[str]:
        return [d for d in discover_lanes(self.paths(sha)) if self._lane_has_go(sha, d)]

    def records(self, sha: str, lane: str | None = None) -> list[Record]:
        lanes = self.lanes(sha)
        out = []
        for path in self.surfaces(sha):
            file_lane = lane_of(path, lanes)
            if lane is not None and file_lane != lane:
                continue
            records, _ = self.parsed(sha, path) or ([], '')
            out += [replace(r, lane=file_lane) for r in records]
        return sorted(out)

    def _lane_has_go(self, sha: str, lane: str) -> bool:
        text = self.blob(sha, f'{lane}/go.mod') or ''
        first = _go_mod_module(text).split('/', 1)[0]
        if '.' not in first:
            return False
        return any(p.startswith(lane + '/') and p.endswith('.go') for p in self.paths(sha))

    def readable_surfaces(self, sha: str) -> tuple[list[str], list[str]]:
        """(surfaces that parse with every strict value ordered, one reason per
        surface that does not): what `records`, `diff` and `dominance` can use."""
        readable, refused = [], []
        for path in self.surfaces(sha):
            try:
                records, _ = self.parsed(sha, path) or ([], '')
            except InventoryError as exc:
                refused.append(str(exc))
                continue
            bad = [r for r in records if _unordered(r)]
            if bad:
                refused += [f'{path}: {r.identity} {r.value!r} is not {r.versioning}' for r in bad]
            else:
                readable.append(path)
        return readable, refused


_NOT_NOTED = frozenset({'dev'})
_LABELS = {'go': 'Go', 'npm': 'npm', 'npm-range': 'npm', 'pypi': 'Python', 'docker': 'image'}
_TRANSITIVE_NOUN = {
    'Go': ('Go module', 'Go modules'),
    'npm': ('npm package', 'npm packages'),
    'Python': ('Python package', 'Python packages'),
}


def _label(ecosystem: str) -> str:
    return _LABELS.get(ecosystem, ecosystem)


def _group(records: list[Record]) -> dict[tuple[str, str, str, str], dict[str, set]]:
    """Noted values per identity, split into the copies the notes name and the
    transitive copies they only count: one lockfile can hold both of one package."""
    groups: dict[tuple[str, str, str, str], dict[str, set]] = {}
    for r in records:
        if r.kind in _NOT_NOTED:
            continue
        roles = groups.setdefault((r.lane, r.ecosystem, r.file, r.identity), {})
        role = 'indirect' if r.kind == 'indirect' else 'named'
        roles.setdefault(role, set()).add((r.value, r.digest))
    return groups


def _render_values(ecosystem: str, values: set[tuple[str, str]], kind: str) -> str:
    def one(value: str, digest: str) -> str:
        if digest and kind in ('image', 'pin', 'checksum'):
            short = digest.removeprefix('sha256:')[:12]
            return f'{value} ({short})' if value else short
        return value

    return ', '.join(sorted(one(v, d) for v, d in values))


def _named_change(named_a: set, named_b: set, indirect_a: set, indirect_b: set) -> str:
    if named_a == named_b:
        return ''
    if named_a and named_b:
        return 'changed'
    if named_b:
        return 'now direct' if indirect_a else 'added'
    return 'no longer direct' if indirect_b else 'removed'


def diff(repo: Repo, a: str, b: str, lane: str | None, security: set[str]) -> dict:
    ra, rb = repo.records(a, lane), repo.records(b, lane)
    kinds = {
        (r.lane, r.ecosystem, r.file, r.identity): r.kind for r in ra + rb if r.kind != 'indirect'
    }
    ga, gb = _group(ra), _group(rb)
    changed_lock = {
        (posixpath.dirname(k[2]), k[3])
        for k in set(ga) | set(gb)
        if k[1] == 'npm' and ga.get(k) != gb.get(k)
    }
    direct, transitive = [], {}
    for key in sorted(set(ga) | set(gb)):
        before, after = ga.get(key, {}), gb.get(key, {})
        if before == after:
            continue
        lane_name, ecosystem, path, identity = key
        if ecosystem == 'npm-range' and (posixpath.dirname(path), identity) in changed_lock:
            continue
        named_a, named_b = before.get('named', set()), after.get('named', set())
        if kinds.get(key) == 'checksum' and {v for v, _ in named_a} != {v for v, _ in named_b}:
            # A checksum is news only when re-pinned at one version: one that moved is told by
            # its pin's line, and one added or dropped beside its pin (a license file) is none.
            continue
        via_security = _changed_by(repo, a, b, path, identity, security) if security else False
        indirect_a, indirect_b = before.get('indirect', set()), after.get('indirect', set())
        change = _named_change(named_a, named_b, indirect_a, indirect_b)
        # A value that crossed between named and transitive is told by its line, not counted.
        crossed_in = (named_a - named_b) & (indirect_b - indirect_a)
        crossed_out = (named_b - named_a) & (indirect_a - indirect_b)
        shown_a, shown_b = named_a, named_b
        # One copy that changed role and value at once (a Go require gaining `// indirect`).
        single = len(named_a | named_b) == 1 == len(indirect_a | indirect_b)
        if single and change == 'no longer direct' and not crossed_in and not indirect_a:
            crossed_in, shown_b = indirect_b, indirect_b
        if single and change == 'now direct' and not crossed_out and not indirect_b:
            crossed_out, shown_a = indirect_a, indirect_a
        if indirect_a - crossed_out != indirect_b - crossed_in:
            bucket = transitive.setdefault(_label(ecosystem), {'count': 0, 'security': 0})
            bucket['count'] += 1
            bucket['security'] += int(via_security)
        if not change:
            continue
        kind = kinds[key]
        direct.append(
            {
                'lane': lane_name,
                'ecosystem': ecosystem,
                'file': path,
                'identity': identity,
                'kind': kind,
                'change': change,
                'from': _render_values(ecosystem, shown_a, kind),
                'to': _render_values(ecosystem, shown_b, kind),
                'security': via_security,
            }
        )
    return {'direct': direct, 'transitive': transitive}


def _changed_by(repo: Repo, a: str, b: str, path: str, identity: str, security: set[str]) -> bool:
    """Whether a commit of `a..b` from the security set moved this identity."""
    for sha in repo.git('log', '--format=%H', f'{a}..{b}', '--', path).split():
        if not any(sha.startswith(s) for s in security):
            continue
        parent = repo.first_parent(sha)
        now = {r.key for r in (repo.parsed(sha, path) or ([], ''))[0] if r.identity == identity}
        then = (
            {r.key for r in (repo.parsed(parent, path) or ([], ''))[0] if r.identity == identity}
            if parent
            else set()
        )
        if now != then:
            return True
    return False


def _line(*words: str) -> str:
    return ' '.join(w for w in words if w)


def render_markdown(result: dict) -> str:
    """One `<details>` block; empty when nothing changed. No advisory IDs."""
    lines, names = [], []
    for item in result['direct']:
        tag = (
            f'{_label(item["ecosystem"])}, security update'
            if item['security']
            else _label(item['ecosystem'])
        )
        ident = f'`{item["identity"]}`'
        if item['change'] == 'added':
            lines.append(_line('- Added', ident, item['to'], f'({tag})'))
        elif item['change'] == 'removed':
            lines.append(_line('- Removed', ident, item['from'], f'({tag})'))
        elif item['change'] == 'now direct':
            moved = _line(item['from'], 'to') if item['from'] not in ('', item['to']) else ''
            lines.append(_line('-', ident, moved, item['to'], f'now a direct dependency ({tag})'))
        elif item['change'] == 'no longer direct':
            moved = _line('to', item['to']) if item['to'] not in ('', item['from']) else ''
            lines.append(
                _line('-', ident, item['from'], moved, f'no longer a direct dependency ({tag})')
            )
        elif item['from'] == item['to']:
            lines.append(_line('-', ident, item['to'], f'resolution changed ({tag})'))
        else:
            lines.append(_line('-', ident, item['from'], 'to', item['to'], f'({tag})'))
        names.append((not item['security'], item['identity']))
    total = len(result['direct'])
    parts = []
    for label in sorted(result['transitive']):
        bucket = result['transitive'][label]
        total += bucket['count']
        singular, plural = _TRANSITIVE_NOUN.get(label, (f'{label} entry', f'{label} entries'))
        part = f'{bucket["count"]} {singular if bucket["count"] == 1 else plural}'
        if bucket['security']:
            part += f' ({bucket["security"]} through security updates)'
        parts.append(part)
    if parts:
        lines.append(f'- Transitive changes: {", ".join(parts)}')
    if not total:
        return ''
    notable = [n for _, n in sorted(names)[:3]]
    summary = f'Dependencies: {total} change{"s" if total != 1 else ""}'
    if notable:
        summary += f' ({", ".join(notable)})'
    return '\n'.join(
        ['<details>', f'<summary>{summary}</summary>', '', *lines, '', '</details>', '']
    )


def glob_pattern(pattern: str) -> re.Pattern:
    """Match it with `fullmatch`. `*` stays within one segment, `**/` at the start
    or after `/` spans zero or more directories, any other `**` any run of
    characters, slashes included."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith('**/', i) and (i == 0 or pattern[i - 1] == '/'):
            out.append('(?:.*/)?')
            i += 3
        elif pattern.startswith('**', i):
            out.append('.*')
            i += 2
        elif pattern[i] == '*':
            out.append('[^/]*')
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile(''.join(out))


def load_sync_owned(path: str) -> list[re.Pattern]:
    """One path or glob (`glob_pattern`) per line; `#` comments."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError as exc:
        raise InventoryError(f'sync-owned set {path}: {exc}') from None
    patterns = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]
    if not patterns:
        raise InventoryError(f'sync-owned set {path} is empty')
    return [glob_pattern(p) for p in patterns]


def _dev_revisions(repo: Repo, base: str, target: str, path: str):
    """The parsed file at each revision of dev's first-parent history `base..target`."""
    for sha in repo.git(
        'log', '--first-parent', '--format=%H', f'{base}..{target}', '--', path
    ).split():
        try:
            parsed = repo.parsed(sha, path)
        except GitReadError:
            raise
        except InventoryError:
            # A revision dev could not parse held no value worth crediting.
            continue
        if parsed:
            yield parsed


def _history(repo: Repo, base: str, target: str, path: str) -> set[tuple]:
    """Every (slot, state) the file held on dev's first-parent history `base..target`."""
    return {
        (r.slot, r.state)
        for records, _ in _dev_revisions(repo, base, target, path)
        for r in records
    }


def _show(records: list[Record]) -> str:
    return ', '.join(
        sorted({_render_values(r.ecosystem, {(r.value, r.digest)}, r.kind) for r in records})
    )


def _unordered(r: Record) -> bool:
    """A value its strict comparator cannot parse: an engine gap, never a verdict."""
    return r.versioning in STRICT_VERSIONING and COMPARATORS[r.versioning](r.value, r.value) is None


def _beats(t: Record, m: Record, history: set) -> tuple[bool | None, str]:
    """Whether target record `t` carries at least what main record `m` does,
    with the reason; None when a strict comparator cannot order the two."""
    held = (m.slot, m.state) in history
    cmp = COMPARATORS.get(m.versioning)
    if m.versioning in STRICT_VERSIONING:
        # Equal text is no proof of a valid version, so the comparator parses both.
        order = cmp(t.value, m.value)
        if order is None:
            return None, f'{m.versioning} cannot order {t.value!r} against {m.value!r}'
    else:
        order = 0 if t.value == m.value else (cmp(t.value, m.value) if cmp else None)
    # Role before value: a newer copy in another role must not carry what an equal one cannot.
    if t.kind != m.kind:
        return False, f'{_show([t])} is {t.kind} at target, not {m.kind}'
    if order is None:
        return held, 'held on dev, then moved on'
    if order != 0:
        return order > 0, f'target has {t.value}'
    if t.digest == m.digest:
        return True, 'target equals main'
    return held, 'same version, digest held on dev'


def _held_on_dev(m: Record, history: set) -> bool:
    """Whether some state of m's slot on dev's history carries m; an unordered one proves nothing."""
    return any(
        _beats(replace(m, value=v, digest=d, kind=k), m, history)[0] is True
        for slot, (v, d, k) in history
        if slot == m.slot
    )


def _matched(edges: list[list[int]], size: int) -> int:
    """Maximum bipartite matching (Kuhn); `edges[g]` lists the pool copies carrying gained copy g."""
    owner = [-1] * size

    def assign(g: int, seen: set[int]) -> bool:
        for p in edges[g]:
            if p not in seen:
                seen.add(p)
                if owner[p] < 0 or assign(owner[p], seen):
                    owner[p] = g
                    return True
        return False

    return sum(assign(g, set()) for g in range(len(edges)))


def judge(
    main: list[Record],
    base_records: list[Record],
    candidates: list[Record],
    history: set,
    *,
    every: bool = False,
) -> tuple[str, str]:
    """('ok'|'fail'|'uncomparable', reason) for one slot whose states main changed.

    Copies in a state base and main share are main's untouched ones and carry nothing. Each copy
    main gained needs its own carrier among the rest, except as many as dev deduplicated, which any
    copy may carry (with `every`, each remaining copy carries every gained value). A remaining
    copy of a state main retired must carry every gained value; with nothing gained, one main
    kept fewer of passes, one it dropped fails, and any other carries every state main reduced.
    Every strict value on main and at base must parse, whatever the target holds."""
    for where, records in (('on main', main), ('at base', base_records)):
        for r in records:
            if _unordered(r):
                return 'uncomparable', f'{r.value!r} {where} is not {r.versioning}'
    base = Counter(r.state for r in base_records)
    held = Counter(r.state for r in main)
    left, common = held - base, held & base
    retired = {s: held[s] for s in base - held}
    gained = []
    for m in main:
        if left[m.state]:
            left[m.state] -= 1
            gained.append(m)
    if not candidates:
        if not gained:
            return 'ok', 'removed on main and on dev'
        if base:
            return 'ok', 'removed on dev'
        # Main added the slot, so its absence at T is dev's removal only once dev held a carrier.
        if all(_held_on_dev(m, history) for m in gained):
            return 'ok', 'held on dev, then removed'
        return 'fail', 'absent at target'
    pool, untouched = [], Counter(common)
    for t in candidates:
        if untouched[t.state]:
            untouched[t.state] -= 1
        else:
            pool.append(t)
    beats = {}
    for t in candidates:
        for m in gained:
            if (t.state, m.state) not in beats:
                beats[t.state, m.state] = _beats(t, m, history)
                if beats[t.state, m.state][0] is None:
                    return 'uncomparable', beats[t.state, m.state][1]

    def roles(ts: list[Record], ms: list[Record]) -> list[str]:
        return sorted({beats[t.state, m.state][1] for t in ts for m in ms if t.kind != m.kind})

    reason = ''
    for m in gained:
        carried = [t for t in candidates if beats[t.state, m.state][0]]
        if not carried or (every and not all(beats[t.state, m.state][0] for t in pool)):
            return 'fail', '; '.join([f'target has {_show(candidates)}', *roles(candidates, [m])])
        pick = next((t for t in carried if t in pool), carried[0])
        reason = reason or beats[pick.state, m.state][1]
    for t in {t.state: t for t in pool}.values():
        kept = retired.get(t.state)
        if kept is None and not gained:
            for r in {r.state: r for r in base_records if r.state in retired}.values():
                # Dev held the base state at B, so another digest or opaque value is one it moved on to.
                carries, why = _beats(t, r, history | {(r.slot, r.state)})
                if carries is None:
                    return 'uncomparable', why
                if not carries:
                    return 'fail', '; '.join(
                        [
                            f'target has {_show([t])}, not a move forward from {_show([r])}, which main removed',
                            *([why] if t.kind != r.kind else []),
                        ]
                    )
            continue
        if kept is None or (kept and not gained):
            continue
        if not gained or not all(beats[t.state, m.state][0] for m in gained):
            return 'fail', '; '.join(
                [
                    f'target still holds {_show([t])}, which main {"replaced" if gained else "removed"}',
                    *roles([t], gained),
                ]
            )
    edges = [[i for i, t in enumerate(pool) if beats[t.state, m.state][0]] for m in gained]
    if len(gained) - _matched(edges, len(pool)) > max(0, len(main) - len(candidates)):
        return 'fail', '; '.join(
            [
                f'target has {_show(candidates)}, and its changed copies do not carry {_show(gained)}',
                *roles(pool, gained),
            ]
        )
    return 'ok', reason or f'removed on main, and target moved on to {_show(candidates)}'


def moved_pins(repo: Repo, a: str, b: str, paths: list[str]) -> set[str]:
    """Identities of the Dockerfile pins whose version or digest differs from `a` to `b`."""
    moved = set()
    for path in paths:
        if surface_kind(path) != 'dockerfile':
            continue
        at_a, at_b = repo.parsed(a, path), repo.parsed(b, path)
        if at_a is None or at_b is None:
            continue
        before = {r.identity: r.state for r in at_a[0] if r.kind == 'pin'}
        moved |= {
            r.identity
            for r in at_b[0]
            if r.kind == 'pin' and r.identity in before and before[r.identity] != r.state
        }
    return moved


def dominance(
    repo: Repo, base: str, main: str, target: str, owned: list[re.Pattern], canonical: Path
) -> dict:
    changed = [
        p
        for p in repo.git('diff', '--no-renames', '--name-only', '-z', base, main).split('\0')
        if p
    ]
    pins = moved_pins(repo, base, main, changed)
    results = []
    for path in changed:
        results.append(_judge_path(repo, base, main, target, path, owned, canonical, pins))
    verdict = 'pass'
    if any(r['status'] == 'uncomparable' for r in results):
        verdict = 'uncomparable'
    elif any(r['status'] == 'fail' for r in results):
        verdict = 'fail'
    return {'verdict': verdict, 'base': base, 'main': main, 'target': target, 'paths': results}


def _by_slot(records: list[Record]) -> dict[tuple, list[Record]]:
    out: dict[tuple, list[Record]] = {}
    for r in records:
        out.setdefault(r.slot, []).append(r)
    return out


def _judged(records: list[Record]) -> dict[tuple, list[Record]]:
    """The slots dominance judges: manifest ranges are notes only, since the
    lockfile beside them holds the resolved state that ships."""
    return _by_slot([r for r in records if r.ecosystem != 'npm-range'])


def _elsewhere(repo: Repo, main: str, target: str, path: str) -> dict[tuple, list[Record]]:
    """Where a slot that left `path` (a moved or split file) can still be carried:
    the target's other surfaces of the same lane, each only for the slots it did
    not hold at main, since a surface that did is judged on its own."""
    lanes = repo.lanes(target)
    found: list[Record] = []
    for p in repo.surfaces(target):
        if p == path or lane_of(p, lanes) != lane_of(path, lanes):
            continue
        at_main = {r.slot for r in (repo.parsed(main, p) or ([], ''))[0]}
        found += [r for r in (repo.parsed(target, p) or ([], ''))[0] if r.slot not in at_main]
    return _by_slot(found)


def canonical_copy(canonical: Path, path: str) -> str | None:
    """The canonical sync copy at a consumer path, decoded like `Repo.blob` (no
    newline translation); None when it does not exist."""
    try:
        data = (canonical / path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InventoryError(f'canonical copy {canonical / path}: {exc}') from None
    return data.decode('utf-8', errors='surrogateescape')


def _judge_path(repo, base, main, target, path, owned, canonical, pins) -> dict:
    entry = {'path': path, 'status': 'ok', 'reason': '', 'records': []}
    if any(p.fullmatch(path) for p in owned):
        at_t, at_m = repo.blob(target, path), repo.blob(main, path)
        if at_t == at_m:
            entry['reason'] = 'sync-owned, target equals main'
        elif at_t is not None and at_t == canonical_copy(canonical, path):
            entry['reason'] = 'sync-owned, target equals the canonical copy'
        else:
            entry.update(
                status='fail',
                reason='sync-owned, target equals neither main nor the canonical copy',
            )
        return entry
    dep = regenerated_by(path)
    if dep:
        if repo.blob(target, path) == repo.blob(main, path):
            entry['reason'] = f'regenerated by {dep}, target equals main'
        elif dep in pins:
            entry['reason'] = f'regenerated by {dep}, follows that pin'
        else:
            entry.update(status='fail', reason=f'regenerated by {dep}, changed on main without it')
        return entry
    kind = surface_kind(path)
    if kind is None:
        entry.update(status='fail', reason='neither an inventory surface nor sync-owned')
        return entry
    if kind == 'gosum':
        entry['reason'] = 'checksum file following its go.mod'
        return entry
    try:
        at_b, at_m, at_t = (repo.parsed(rev, path) for rev in (base, main, target))
        if at_m is None:
            if at_t is None:
                entry['reason'] = 'deleted on main and on dev'
            else:
                entry.update(status='fail', reason='deleted on main, target still has it')
            return entry
        if at_b is None:
            # Main's whole skeleton is new: dev carries it when it holds it, or held it and moved on.
            skeleton = at_m[1]
            if not (at_t and at_t[1] == skeleton) and not any(
                s == skeleton for _, s in _dev_revisions(repo, base, target, path)
            ):
                if at_t is None:
                    entry.update(status='fail', reason='added on main, absent at target')
                else:
                    entry.update(
                        status='fail',
                        reason='added on main, but target never held its content outside the dependency records',
                    )
                return entry
            at_b = ([], skeleton)
        if at_b[1] != at_m[1]:
            entry.update(status='fail', reason='changed outside its dependency records')
            return entry
        entry['records'] = _judge_slots(repo, base, main, target, path, at_b, at_m, at_t)
    except InventoryError as exc:
        entry.update(status='uncomparable', reason=str(exc).removeprefix(f'{path}: '))
        return entry
    statuses = {r['status'] for r in entry['records']}
    entry['status'] = (
        'uncomparable' if 'uncomparable' in statuses else 'fail' if 'fail' in statuses else 'ok'
    )
    entry['reason'] = 'inventory surface'
    return entry


def _judge_slots(repo, base, main, target, path, at_b, at_m, at_t) -> list[dict]:
    by_b, by_m = _judged(at_b[0]), _judged(at_m[0])
    by_t = _judged(at_t[0]) if at_t else {}
    history, elsewhere, out = None, None, []
    for slot in sorted(set(by_b) | set(by_m)):
        b_states = Counter(r.state for r in by_b.get(slot, []))
        m_states = Counter(r.state for r in by_m.get(slot, []))
        if b_states == m_states:
            continue
        if history is None:
            history = _history(repo, base, target, path)
        candidates, every = by_t.get(slot, []), False
        if not candidates:
            if elsewhere is None:
                elsewhere = _elsewhere(repo, main, target, path)
            candidates = elsewhere.get(slot, [])
            every = bool(candidates)
        status, reason = judge(
            by_m.get(slot, []), by_b.get(slot, []), candidates, history, every=every
        )
        if every:
            reason += f' (in {", ".join(sorted({c.file for c in candidates}))})'
        out.append(
            {
                'identity': slot[1],
                'main': _show(by_m.get(slot, [])),
                'status': status,
                'reason': reason,
            }
        )
    return out


def render_dominance(result: dict) -> str:
    out = []
    for p in result['paths']:
        if p['status'] == 'ok' and not any(r['status'] != 'ok' for r in p['records']):
            out.append(f'ok   {p["path"]}: {p["reason"]}')
            continue
        if not p['records']:
            out.append(f'{p["status"].upper():<4} {p["path"]}: {p["reason"]}')
        for r in p['records']:
            if r['status'] != 'ok':
                shown = r['main'] or '(removed)'
                out.append(
                    f'{r["status"].upper():<4} {p["path"]}: {r["identity"]} {shown} on main; {r["reason"]}'
                )
    out.append(
        f'dominance: {result["verdict"].upper()} ({len(result["paths"])} path(s) main changed since {result["base"][:12]})'
    )
    return '\n'.join(out)


def _security_shas(path: str | None) -> set[str]:
    if not path:
        return set()
    try:
        return {
            line.strip()
            for line in Path(path).read_text(encoding='utf-8').splitlines()
            if line.strip()
        }
    except OSError as exc:
        raise InventoryError(f'security SHAs {path}: {exc}') from None


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('records', 'surfaces'):
        p = sub.add_parser(name)
        p.add_argument('--git-dir', required=True)
        p.add_argument('--rev', required=True)
        if name == 'records':
            p.add_argument('--lane', help="lane dir ('.' for the root artifact)")
    p = sub.add_parser('diff')
    p.add_argument('--git-dir', required=True)
    p.add_argument('--from', dest='from_rev', required=True)
    p.add_argument('--to', dest='to_rev', required=True)
    p.add_argument('--lane', help="lane dir ('.' for the root artifact)")
    p.add_argument('--format', choices=('json', 'markdown'), default='json')
    p.add_argument('--security-shas', help='file of commit SHAs merged from Renovate security PRs')
    p = sub.add_parser('dominance')
    p.add_argument('--git-dir', required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--main', required=True)
    p.add_argument('--target', required=True)
    p.add_argument('--sync-owned', required=True, help='file listing the sync-owned paths')
    p.add_argument(
        '--canonical-dir', required=True, help='canonical sync copies at their consumer paths'
    )
    p.add_argument('--format', choices=('text', 'json'), default='text')
    return parser.parse_args(argv)


def run(args) -> int:
    repo = Repo(args.git_dir)
    if args.cmd == 'records':
        records = repo.records(repo.commit(args.rev), args.lane)
        print(json.dumps([asdict(r) for r in records], indent=2))
        return 0
    if args.cmd == 'surfaces':
        readable, refused = repo.readable_surfaces(repo.commit(args.rev))
        print(json.dumps(readable, indent=2))
        for reason in refused:
            print(f'inventory: {reason}', file=sys.stderr)
        return 2 if refused else 0
    if args.cmd == 'diff':
        a, b = repo.commit(args.from_rev), repo.commit(args.to_rev)
        result = diff(repo, a, b, args.lane, _security_shas(args.security_shas))
        print(
            render_markdown(result) if args.format == 'markdown' else json.dumps(result, indent=2),
            end='',
        )
        if args.format == 'json':
            print()
        return 0
    owned = load_sync_owned(args.sync_owned)
    canonical = Path(args.canonical_dir)
    if not canonical.is_dir():
        raise InventoryError(f'canonical dir {canonical} does not exist')
    base, main, target = (repo.commit(r) for r in (args.base, args.main, args.target))
    result = dominance(repo, base, main, target, owned, canonical)
    print(json.dumps(result, indent=2) if args.format == 'json' else render_dominance(result))
    return {'pass': 0, 'fail': 1, 'uncomparable': 2}[result['verdict']]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except InventoryError as exc:
        print(f'inventory: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
