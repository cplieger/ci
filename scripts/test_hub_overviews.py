"""Tests for the Docker Hub overview renderer and the out-of-band sync script."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load(
    'render_hub_overview', HERE.parent / 'actions/render-hub-overview/render-hub-overview.py'
)
sync = load('sync_hub_overviews', HERE / 'sync-hub-overviews.py')

BLOB = 'https://github.com/cplieger/demo/blob/main'


class DocLinks(unittest.TestCase):
    def test_readme_link_is_plain_text_with_its_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = render.doc_links(Path(tmp), 'cplieger', 'demo', 'main')
        self.assertEqual(
            links[0],
            f'[Full documentation]({BLOB}/README.md): setup, settings, troubleshooting and security',
        )

    def test_docs_pages_follow_the_readme_link_by_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / 'docs'
            docs.mkdir()
            (docs / 'monitoring.md').write_text('# Monitoring and alerts\n')
            links = render.doc_links(Path(tmp), 'cplieger', 'demo', 'main')
        self.assertEqual(links[1], f'[Monitoring and alerts]({BLOB}/docs/monitoring.md)')


PAGE = '# demo\n\nbody\n'
ABOUT = 'Keeps a library on the best releases'


class PendingFields(unittest.TestCase):
    def test_nothing_pending_when_page_and_short_description_match(self):
        current = {'full_description': PAGE, 'description': ABOUT}
        self.assertEqual(sync.pending_fields(PAGE, ABOUT, current), ({}, []))

    def test_changed_page_is_pending(self):
        current = {'full_description': 'old', 'description': ABOUT}
        fields, _ = sync.pending_fields(PAGE, ABOUT, current)
        self.assertEqual(fields, {'full_description': PAGE})

    def test_changed_about_line_becomes_the_short_description(self):
        current = {'full_description': PAGE, 'description': 'stale'}
        fields, _ = sync.pending_fields(PAGE, ABOUT, current)
        self.assertEqual(fields, {'description': ABOUT})

    def test_unset_hub_short_description_is_filled(self):
        current = {'full_description': PAGE, 'description': None}
        fields, _ = sync.pending_fields(PAGE, ABOUT, current)
        self.assertEqual(fields, {'description': ABOUT})

    def test_empty_about_line_leaves_the_short_description_alone(self):
        current = {'full_description': PAGE, 'description': 'kept'}
        fields, notes = sync.pending_fields(PAGE, '', current)
        self.assertEqual(fields, {})
        self.assertEqual(len(notes), 1)

    def test_unreadable_about_line_leaves_the_short_description_alone(self):
        current = {'full_description': PAGE, 'description': 'kept'}
        self.assertEqual(sync.pending_fields(PAGE, None, current), ({}, []))

    def test_about_line_over_the_hub_limit_is_cut_on_a_character_boundary(self):
        current = {'full_description': PAGE, 'description': 'kept'}
        long_about = 'x' * 99 + '\u2014'
        fields, notes = sync.pending_fields(PAGE, long_about, current)
        self.assertEqual(fields, {'description': 'x' * 99})
        self.assertIn('102 bytes, cut to 99', notes[0])

    def test_multibyte_character_ending_at_the_limit_is_kept(self):
        current = {'full_description': PAGE, 'description': 'kept'}
        about = 'x' * 97 + '\u2014' + 'tail'
        fields, _ = sync.pending_fields(PAGE, about, current)
        self.assertEqual(fields, {'description': 'x' * 97 + '\u2014'})
        self.assertEqual(len(fields['description'].encode()), 100)

    def test_cut_line_already_on_hub_is_not_rewritten(self):
        current = {'full_description': PAGE, 'description': 'x' * 99}
        fields, notes = sync.pending_fields(PAGE, 'x' * 99 + '\u2014', current)
        self.assertEqual(fields, {})
        self.assertEqual(len(notes), 1)

    def test_about_line_at_the_hub_limit_is_written(self):
        current = {'full_description': PAGE, 'description': 'kept'}
        about = 'x' * 100
        fields, _ = sync.pending_fields(PAGE, about, current)
        self.assertEqual(fields, {'description': about})


if __name__ == '__main__':
    unittest.main()
