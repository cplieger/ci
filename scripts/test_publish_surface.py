"""publish-surface.py's reading of `npm pack --json`, in the shapes npm 11 and npm 12 print."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    'publish_surface', ROOT / 'actions' / 'publish-surface' / 'publish-surface.py'
)
publish_surface = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish_surface)

ENTRY = {
    'name': '@cplieger/ui',
    'version': '1.0.0',
    'filename': 'cplieger-ui-1.0.0.tgz',
    'files': [{'path': 'package.json'}, {'path': 'src/index.ts'}],
}


class PackEntry(unittest.TestCase):
    def test_npm_11_prints_a_list_of_entries(self):
        self.assertEqual(publish_surface.pack_entry(json.dumps([ENTRY])), ENTRY)

    def test_npm_12_prints_an_object_keyed_by_package_name(self):
        self.assertEqual(publish_surface.pack_entry(json.dumps({'@cplieger/ui': ENTRY})), ENTRY)

    def test_output_with_no_entry_is_refused(self):
        for stdout in ('[]', '{}', '"cplieger-ui-1.0.0.tgz"', '[1]'):
            with self.subTest(stdout=stdout), self.assertRaises(publish_surface.SurfaceError):
                publish_surface.pack_entry(stdout)

    def test_an_entry_without_its_file_list_is_refused(self):
        entry = {k: v for k, v in ENTRY.items() if k != 'files'}
        with self.assertRaisesRegex(publish_surface.SurfaceError, 'missing `files`'):
            publish_surface.pack_entry(json.dumps({'@cplieger/ui': entry}))

    def test_non_json_output_is_refused(self):
        with self.assertRaisesRegex(publish_surface.SurfaceError, 'non-JSON'):
            publish_surface.pack_entry('npm warn something\n')


if __name__ == '__main__':
    unittest.main()
