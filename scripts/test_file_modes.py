"""Workflows, action metadata, configs and test fixtures are data, never executables."""

import os
import stat
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PREFIXES = ('.github/', 'actions/', 'configs/', 'scripts/testdata/')


def git(*args: str) -> list[str]:
    out = subprocess.run(
        ['git', '-C', str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.split('\0') if line]


def is_script(path: Path) -> bool:
    with path.open('rb') as fh:
        return fh.read(2) == b'#!'


class DataFileModes(unittest.TestCase):
    def test_no_data_file_would_land_executable(self):
        tracked = {}
        for entry in git('ls-files', '--stage', '-z'):
            meta, name = entry.split('\t', 1)
            tracked[name] = meta.split()[0]
        untracked = git('ls-files', '--others', '--exclude-standard', '-z')
        bad = []
        for name in [*tracked, *untracked]:
            path = ROOT / name
            if not name.startswith(DATA_PREFIXES) or not path.is_file() or is_script(path):
                continue
            if name in tracked:
                executable = tracked[name] == '100755'
            else:
                executable = bool(
                    os.stat(path).st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                )
            if executable:
                bad.append(name)
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
