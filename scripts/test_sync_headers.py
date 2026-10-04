"""Every comment-capable source classify-repos.py syncs opens with a provenance header.

The header is how a reader of a consumer repo learns that a file is overwritten
by the next sync and must be changed in cplieger/ci instead.
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    'classify_repos', ROOT / 'scripts' / 'classify-repos.py'
)
classify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(classify)

# Keyed on the suffix, or on the whole name for dotfiles with none. A synced
# format missing from both maps fails the test, so a new one gets a decision.
COMMENT_MARKERS = {
    '.yml': '#',
    '.yaml': '#',
    '.toml': '#',
    '.sh': '#',
    '.mjs': '//',
    '.editorconfig': '#',
    '.gitattributes': '#',
}
NO_COMMENT = {'.json'}


def synced_sources() -> list[str]:
    """Every source path named in a `*_FILES` sync block, bare or `source:` form."""
    sources = []
    for name in dir(classify):
        block = getattr(classify, name)
        if not (name.endswith('_FILES') and isinstance(block, str)):
            continue
        for line in block.splitlines():
            entry = line.strip()
            if entry.startswith('- source:'):
                sources.append(entry.removeprefix('- source:').strip())
            elif entry.startswith('- ') and ':' not in entry:
                sources.append(entry.removeprefix('- ').strip())
    return sorted(set(sources))


def kind(source: str) -> str:
    path = Path(source)
    return path.suffix or path.name


class SyncHeaders(unittest.TestCase):
    def test_sync_lists_parse_to_existing_files(self):
        sources = synced_sources()
        self.assertIn('.editorconfig', sources)
        self.assertIn('configs/prettier.json', sources)
        for source in sources:
            self.assertTrue((ROOT / source).is_file(), source)

    def test_every_synced_format_is_classified(self):
        for source in synced_sources():
            self.assertIn(kind(source), COMMENT_MARKERS.keys() | NO_COMMENT, source)

    def test_comment_capable_sources_carry_header(self):
        for source in synced_sources():
            marker = COMMENT_MARKERS.get(kind(source))
            if marker is None:
                continue
            with self.subTest(source=source):
                lines = (ROOT / source).read_text(encoding='utf-8').splitlines()
                # A shebang must stay on line 1, so the header follows it.
                first = lines[1] if lines and lines[0].startswith('#!') else lines[0]
                pattern = rf'{re.escape(marker)} Synced from cplieger/ci/{re.escape(source)}[ .]'
                self.assertRegex(first, f'^{pattern}')


if __name__ == '__main__':
    unittest.main()
