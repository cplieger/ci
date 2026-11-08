#!/usr/bin/env python3
"""The promotion purity rule: a first-party dependency pinned at a `-dev.` version.

A stable release that ships such a pin hands its consumers a pre-release
they never chose. promote.py refuses a promotion whose root carries one and
holds back a nested lane that does; release.yaml's stable run and
release-state.sh hold the same lanes back through `held-lanes` below. This
module owns the rule and the tree reader, so the three cannot disagree. Its
rule functions read text only; tree_files and the CLI read one commit's tree.

    purity.py held-lanes <commit>   GO_LANES_JSON: JSON array of nested lane dirs

prints a JSON object {lane: [violation, ...]} of the nested lanes the commit
holds back, sorted, `{}` for none. Exit 2 when a manifest cannot be read, so
its pins cannot be judged; exit 1 when git or GO_LANES_JSON fails.
"""

from __future__ import annotations

import json
import os
import re
import sys

import inventory


def first_party(spec: str) -> bool:
    return 'cplieger/' in spec or spec.startswith('@cplieger/')


def _go_mod_directives(text: str):
    """(directive, words) for every require and replace line, blocks unfolded."""
    block = ''
    for raw in text.splitlines():
        line = raw.split('//', 1)[0].strip()
        if not line:
            continue
        if block:
            if line == ')':
                block = ''
            else:
                yield block, line.split()
            continue
        words = line.split()
        if words[0] in ('require', 'replace'):
            if len(words) == 2 and words[1] == '(':
                block = words[0]
            else:
                yield words[0], words[1:]


def _go_mod_violations(path: str, text: str) -> list[str]:
    found = []
    for directive, words in _go_mod_directives(text):
        if directive == 'require':
            if (
                len(words) >= 2
                and words[0].startswith('github.com/cplieger/')
                and '-dev.' in words[1]
            ):
                found.append(f'{path}: {words[0]} {words[1]}')
            continue
        # replace [old [vOld]] => new [vNew]: the replacement is what Go builds.
        if '=>' not in words:
            continue
        target = words[words.index('=>') + 1 :]
        if (
            len(target) >= 2
            and target[0].startswith('github.com/cplieger/')
            and '-dev.' in target[1]
        ):
            found.append(f'{path}: replace {words[0]} => {target[0]} {target[1]}')
    return found


class PurityInputError(ValueError):
    """A purity file whose shape the check cannot read, so its pins cannot be judged."""


def _json_sections(path: str, text: str, sections: tuple[str, ...]) -> list[tuple[str, dict]]:
    """(section, mapping) for each present section of a JSON manifest, as npm and JSR
    read it: the document and every present section must be objects."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise PurityInputError(f'{path} is not valid JSON ({err})') from None
    if not isinstance(data, dict):
        raise PurityInputError(f'{path} is not a JSON object')
    found = []
    for section in sections:
        mapping = data.get(section)
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            raise PurityInputError(f'{section} in {path} is not an object')
        found.append((section, mapping))
    return found


def _package_json_violations(path: str, text: str) -> list[str]:
    found = []
    sections = ('dependencies', 'peerDependencies', 'optionalDependencies')
    for section, mapping in _json_sections(path, text, sections):
        for name, spec in mapping.items():
            # An alias spec (`npm:@cplieger/x@1.0.0-dev.1`) names the package
            # in the value, so both sides are judged.
            if first_party(f'{name} {spec}') and '-dev.' in str(spec):
                found.append(f'{path}: {section} {name} {spec}')
    return found


def _jsr_json_violations(path: str, text: str) -> list[str]:
    return [
        f'{path}: imports {name} {spec}'
        for _, mapping in _json_sections(path, text, ('imports',))
        for name, spec in mapping.items()
        if '@cplieger/' in f'{name} {spec}' and '-dev.' in str(spec)
    ]


_RENOVATE_DEP = re.compile(r'#\s*renovate:.*\bdepName=(\S+)')
_FROM_LINE = re.compile(r'^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)', re.IGNORECASE)
_ARG_LINE = re.compile(r'^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)')


def _dockerfile_violations(path: str, text: str) -> list[str]:
    """A `# renovate:` marker annotates the ARG on the next line only, as
    Renovate reads it; any other line in between disowns it."""
    found = []
    renovate_dep = ''
    for raw in text.splitlines():
        m = _RENOVATE_DEP.search(raw)
        if m:
            renovate_dep = m.group(1)
            continue
        m = _FROM_LINE.match(raw)
        if m:
            image = m.group(1)
            if image.startswith('ghcr.io/cplieger/') and '-dev.' in image:
                found.append(f'{path}: FROM {image}')
        m = _ARG_LINE.match(raw)
        if m:
            name, value = m.group(1), m.group(2).strip('"\'')
            if '-dev.' in value and (first_party(value) or first_party(renovate_dep)):
                found.append(f'{path}: ARG {name}={value}')
        renovate_dep = ''
    return found


def purity_violations(files: dict[str, str]) -> list[str]:
    """First-party pins at a `-dev.` version among {path: text} for go.mod,
    package.json, jsr.json and Dockerfile* files. Empty means pure. Raises
    PurityInputError when a JSON manifest has a shape it cannot read."""
    found: list[str] = []
    for path in sorted(files):
        text = files[path]
        base = path.rsplit('/', 1)[-1]
        if base == 'go.mod':
            found += _go_mod_violations(path, text)
        elif base == 'package.json':
            found += _package_json_violations(path, text)
        elif base == 'jsr.json':
            found += _jsr_json_violations(path, text)
        elif base.startswith('Dockerfile'):
            found += _dockerfile_violations(path, text)
    return found


def is_purity_file(path: str) -> bool:
    if '/node_modules/' in f'/{path}':
        return False
    base = path.rsplit('/', 1)[-1]
    return base in ('go.mod', 'package.json', 'jsr.json') or base.startswith('Dockerfile')


def lane_holds(files: dict[str, str], lanes: list[str]) -> dict[str, list[str]]:
    """{lane: [violation, ...]} of each nested lane whose own purity files, among
    {path: text}, pin a first-party dependency at a dev version. A root
    violation is no hold: the root cannot be held back, so promote.py refuses
    it. Raises PurityInputError for a manifest it cannot read."""
    held: dict[str, list[str]] = {}
    for path in sorted(files):
        if not is_purity_file(path):
            continue
        lane = inventory.lane_of(path, lanes)
        if lane == '.':
            continue
        found = purity_violations({path: files[path]})
        if found:
            held.setdefault(lane, []).extend(found)
    return held


def tree_files(repo: inventory.Repo, commit: str) -> dict[str, str]:
    """{path: text} of every purity file in `commit`'s tree. Raises
    inventory.InventoryError when git cannot read the tree."""
    return {p: repo.blob(commit, p) or '' for p in repo.paths(commit) if is_purity_file(p)}


def held_lanes(commit: str, lanes: list[str]) -> dict[str, list[str]]:
    """lane_holds over the purity files at `commit` in the working directory's repository."""
    return lane_holds(tree_files(inventory.Repo('.'), commit), lanes)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != 'held-lanes':
        print('usage: purity.py held-lanes <commit>', file=sys.stderr)
        return 1
    try:
        lanes = json.loads(os.environ.get('GO_LANES_JSON') or '[]')
    except json.JSONDecodeError as err:
        print(f'::error::GO_LANES_JSON is not JSON ({err})', file=sys.stderr)
        return 1
    if not isinstance(lanes, list) or not all(isinstance(lane, str) and lane for lane in lanes):
        print('::error::GO_LANES_JSON is not a JSON array of lane dirs', file=sys.stderr)
        return 1
    try:
        held = held_lanes(argv[1], lanes)
    except inventory.InventoryError as err:
        print(f'::error::{err}', file=sys.stderr)
        return 1
    except PurityInputError as err:
        print(f'::error::{err}, so its first-party pins cannot be judged', file=sys.stderr)
        return 2
    print(json.dumps(held, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
