"""publish-surface.py's reading of `npm pack --json` and its JSR dry run's dev-build imports."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

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


JSR_VERSIONS = {'@cplieger/web-terminal-engine': ['5.3.0', '6.4.1', '6.5.0', '7.0.0']}


class DevImportSwaps(unittest.TestCase):
    def swap(self, imports):
        config = {'imports': dict(imports)}
        swaps = publish_surface.dev_import_swaps(config, JSR_VERSIONS.__getitem__)
        return config['imports'], swaps

    def test_a_first_party_dev_pin_moves_to_the_newest_stable_of_its_major(self):
        old = 'jsr:@cplieger/web-terminal-engine@^6.6.0-dev.1'
        imports, swaps = self.swap({'@cplieger/web-terminal-engine': old})
        new = 'jsr:@cplieger/web-terminal-engine@^6.5.0'
        self.assertEqual(imports, {'@cplieger/web-terminal-engine': new})
        self.assertEqual(swaps, {'@cplieger/web-terminal-engine': (old, new)})

    def test_stable_and_third_party_pins_are_untouched(self):
        imports = {
            '@cplieger/web-terminal-engine': 'jsr:@cplieger/web-terminal-engine@^6.0.0',
            '@std/assert': 'jsr:@std/assert@^1.1.0-dev.1',
            'lodash': 'npm:lodash@^4.17.21',
        }
        got, swaps = self.swap(imports)
        self.assertEqual((got, swaps), (imports, {}))

    def test_a_dev_pin_with_no_stable_of_its_major_is_refused(self):
        with self.assertRaisesRegex(publish_surface.SurfaceError, 'no stable'):
            self.swap({'e': 'jsr:@cplieger/web-terminal-engine@^8.0.0-dev.3'})

    def test_no_imports_object_swaps_nothing(self):
        for config in ({}, {'imports': []}):
            with self.subTest(config=config):
                self.assertEqual(publish_surface.dev_import_swaps(config, self.fail), {})


class JsrShippedRestoresJsrJson(unittest.TestCase):
    def test_the_dry_run_sees_the_swap_and_the_file_is_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = pathlib.Path(tmp)
            jsr = pkg / 'jsr.json'
            original = '{"imports": {"e": "jsr:@cplieger/e@^6.6.0-dev.1"}}'
            jsr.write_text(original)
            seen = {}

            def fake_run(cmd, cwd, **_):
                seen['jsr'] = json.loads((cwd / 'jsr.json').read_text())
                out = f'file://{cwd.resolve()}/src/index.ts (1KB)'
                return subprocess.CompletedProcess(cmd, 0, out, '')

            with (
                mock.patch.object(publish_surface, 'run', fake_run),
                mock.patch.object(publish_surface, 'jsr_stable_versions', lambda _: ['6.5.0']),
                contextlib.redirect_stdout(io.StringIO()) as log,
            ):
                shipped = publish_surface.jsr_shipped(pkg)
            self.assertEqual(seen['jsr'], {'imports': {'e': 'jsr:@cplieger/e@^6.5.0'}})
            self.assertEqual(jsr.read_text(), original)
            self.assertEqual(shipped, {pathlib.Path('src/index.ts')})
            self.assertIn('^6.6.0-dev.1 -> jsr:@cplieger/e@^6.5.0', log.getvalue())


if __name__ == '__main__':
    unittest.main()
