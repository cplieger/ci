"""Tests for the retention selection in ghcr_retention.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

import ghcr_retention as gr
import promote

NOW = datetime(2026, 9, 20, 5, 30, tzinfo=UTC)
SHA = 'sha-' + 'f' * 40


def version(vid: int, tags: list[str], days_old: float) -> dict:
    created = NOW - timedelta(days=days_old)
    return {
        'id': vid,
        'created_at': created.isoformat().replace('+00:00', 'Z'),
        'metadata': {'container': {'tags': tags}},
    }


class Candidates(unittest.TestCase):
    def test_dev_plus_sha_is_a_candidate(self):
        self.assertTrue(gr.is_candidate(version(1, ['v1.3.0-dev.7', SHA], 40)))

    def test_dashboard_only_dev_tag_is_a_candidate(self):
        self.assertTrue(gr.is_candidate(version(1, ['v1.3.0-dev.7'], 40)))

    def test_protected_tags_are_never_candidates(self):
        for tags in (
            ['v1.3.0-dev.7', 'dev', SHA],
            ['v1.3.0', SHA],
            ['latest'],
            ['v1', 'v1.3'],
            ['v1.3.0-dev.7', 'v1.3.0'],
        ):
            self.assertFalse(gr.is_candidate(version(1, tags, 40)), tags)

    def test_sha_only_is_not_a_candidate(self):
        self.assertFalse(gr.is_candidate(version(1, [SHA], 40)))

    def test_untagged_is_never_touched(self):
        self.assertFalse(gr.is_candidate(version(1, [], 400)))
        self.assertFalse(
            gr.is_candidate({'id': 2, 'created_at': '2020-01-01T00:00:00Z', 'metadata': {}})
        )


class Selection(unittest.TestCase):
    def test_newest_ten_survive_at_any_age(self):
        versions = [version(i, [f'v1.0.0-dev.{i}'], 100 + i) for i in range(1, 13)]
        doomed = {v['id'] for v in gr.select_deletions(versions, NOW)}
        # ids 1..10 are the newest (smallest days_old), 11 and 12 fall out.
        self.assertEqual(doomed, {11, 12})

    def test_young_candidates_survive_beyond_the_ten(self):
        versions = [version(i, [f'v1.0.0-dev.{i}'], i) for i in range(1, 15)]
        self.assertEqual(gr.select_deletions(versions, NOW), [])

    def test_protected_versions_do_not_count_toward_the_ten(self):
        versions = [version(100, ['dev', 'v2.0.0-dev.1', SHA], 0.5)]
        versions += [version(i, [f'v1.0.0-dev.{i}'], 60 + i) for i in range(1, 12)]
        doomed = {v['id'] for v in gr.select_deletions(versions, NOW)}
        self.assertEqual(doomed, {11})

    def test_boundary_exactly_max_age_is_kept(self):
        versions = [version(i, [f'v1.0.0-dev.{i}'], 1) for i in range(1, 11)]
        versions.append(version(30, ['v0.9.0-dev.1'], 30))
        versions.append(version(31, ['v0.9.0-dev.2'], 30.001))
        doomed = {v['id'] for v in gr.select_deletions(versions, NOW)}
        self.assertEqual(doomed, {31})

    def test_sorted_newest_first_not_by_id(self):
        versions = [version(1, ['v1.0.0-dev.1'], 5), version(2, ['v1.0.0-dev.2'], 90)]
        versions += [version(10 + i, [f'v1.1.0-dev.{i}'], 1) for i in range(9)]
        doomed = {v['id'] for v in gr.select_deletions(versions, NOW)}
        self.assertEqual(doomed, {2})

    def test_untagged_versions_are_ignored_by_selection(self):
        versions = [version(i, [], 400) for i in range(20)]
        self.assertEqual(gr.select_deletions(versions, NOW), [])


class PromotedImages(unittest.TestCase):
    """promote.yaml tags a promoted dev digest before main can name it."""

    TAG = promote.promoted_tag('c' * 40)

    def test_a_promoted_tag_takes_a_version_out_of_the_candidates(self):
        for tags in (['v1.3.0-dev.7', SHA, self.TAG], [self.TAG]):
            self.assertFalse(gr.is_candidate(version(1, tags, 400)), tags)

    def test_a_run_keeps_an_aged_promoted_version_with_no_read_beyond_the_listings(self):
        now = datetime.now(UTC)

        def aged(vid, tags, days):
            return {
                'id': vid,
                'created_at': (now - timedelta(days=days)).isoformat(),
                'metadata': {'container': {'tags': tags}},
            }

        young = [aged(i, [f'v1.0.0-dev.{i}'], 1) for i in range(10)]
        versions = [
            *young,
            aged(50, ['v0.9.0-dev.1', SHA, self.TAG], 60),
            aged(51, ['v0.9.0-dev.2'], 61),
        ]
        calls = []

        def gh(*args):
            calls.append(args)
            path = args[-1]
            if path.startswith('users/cplieger/packages?'):
                return json.dumps([[{'name': 'demo'}]])
            if path.endswith('/demo/versions?per_page=100'):
                return json.dumps([versions])
            if args[:3] == ('api', '-X', 'DELETE'):
                return ''
            raise RuntimeError(f'gh {path} failed: HTTP 502')

        with (
            mock.patch.object(gr, 'gh', side_effect=gh),
            mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(gr.main([]), 0)
        deletes = [c[-1].rsplit('/', 1)[-1] for c in calls if c[:3] == ('api', '-X', 'DELETE')]
        self.assertEqual(deletes, ['51'])
        self.assertEqual(len(calls), 3, calls)
        self.assertNotIn('| 50 |', out.getvalue())


class Rendering(unittest.TestCase):
    def test_table(self):
        text = gr.render_table([('knell', 7, 'v1.0.0-dev.1', 45, 'delete')])
        self.assertIn('| knell | 7 | v1.0.0-dev.1 | 45 | delete |', text)

    def test_package_path_encodes_slashes(self):
        self.assertEqual(
            gr.package_path('knell/dashboard'),
            'users/cplieger/packages/container/knell%2Fdashboard',
        )


if __name__ == '__main__':
    unittest.main()
