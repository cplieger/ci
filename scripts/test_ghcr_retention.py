"""Tests for the retention selection in ghcr_retention.py."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import ghcr_retention as gr

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
