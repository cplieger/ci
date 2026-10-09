"""Tests for release_channels.py: tag shapes, lanes, ordering and paging."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import unittest
from typing import ClassVar

import release_channels as rc

# A branch-model variable assigned from a raw default-branch comparison.
RAW_MODEL = re.compile(r'two_(?:channel|branch)\w*["\']?\]?\s*=(?!=)[^\n]*[=!]=\s*["\']dev["\']')


class TagShapes(unittest.TestCase):
    def test_stable_matches_full_semver_only(self):
        self.assertTrue(rc.is_stable_tag('v1.3.0'))
        for name in ('v1', 'v1.3', 'v1.3.0-dev.7', 'sha-' + 'a' * 40, 'yamlenv/v1.3.0', 'v1.3.0 '):
            self.assertFalse(rc.is_stable_tag(name), name)

    def test_dev_matches_dev_suffix_only(self):
        self.assertTrue(rc.is_dev_tag('v1.3.0-dev.7'))
        for name in ('v1', 'v1.3', 'v1.3.0', 'sha-' + 'a' * 40, 'v1.3.0-dev', 'v1.3.0-rc.1'):
            self.assertFalse(rc.is_dev_tag(name), name)


class NewestStableTag(unittest.TestCase):
    def test_ignores_dev_tags(self):
        self.assertEqual(rc.newest_stable_tag(['v1.3.0-dev.9', 'v1.2.0', 'v1.4.0-dev.1']), 'v1.2.0')

    def test_orders_numerically_not_lexically(self):
        self.assertEqual(rc.newest_stable_tag(['v1.9.0', 'v1.10.0', 'v1.2.0']), 'v1.10.0')

    def test_does_not_trust_list_order(self):
        self.assertEqual(rc.newest_stable_tag(['v2.0.0', 'v3.1.4', 'v3.0.9']), 'v3.1.4')

    def test_empty_when_no_stable_tag(self):
        self.assertEqual(rc.newest_stable_tag(['v1.3.0-dev.1', 'yamlenv/v1.0.0']), '')

    def test_sorted_newest_first(self):
        self.assertEqual(
            rc.stable_tags_sorted(['v1.0.0', 'v1.0.1-dev.1', 'v1.0.10', 'v1.0.2']),
            ['v1.0.10', 'v1.0.2', 'v1.0.0'],
        )


class Lanes(unittest.TestCase):
    def test_lane_shapes_carry_the_module_dir_prefix(self):
        self.assertEqual(rc.tag_lane('v1.3.0'), '')
        self.assertEqual(rc.tag_lane('v1.3.0-dev.7'), '')
        self.assertEqual(rc.tag_lane('yamlenv/v1.1.0'), 'yamlenv')
        self.assertEqual(rc.tag_lane('yamlenv/v1.1.0-dev.1'), 'yamlenv')
        self.assertEqual(rc.tag_lane('pkg/sub.mod/v2.0.0'), 'pkg/sub.mod')
        for name in (
            'v1.3',
            'yamlenv/v1.3',
            'v1.3.0-rc.1',
            'yamlenv/v1.3.0-rc.1',
            'sha-' + 'a' * 40,
        ):
            self.assertIsNone(rc.tag_lane(name), name)

    def test_root_shapes_stay_root_only(self):
        self.assertFalse(rc.is_stable_tag('yamlenv/v1.1.0'))
        self.assertFalse(rc.is_dev_tag('yamlenv/v1.1.0-dev.1'))

    def test_tags_are_grouped_and_ordered_per_lane(self):
        names = [
            'yamlenv/v1.1.0-dev.2',
            'v1.3.0',
            'yamlenv/v1.1.0',
            'v1.4.0-dev.1',
            'yamlenv/v1.1.0-dev.10',
            'yamlenv/v1.0.9',
            'v1.10.0',
            'v1.4.0-dev.1-rc.1',
            'sha-' + 'a' * 40,
        ]
        self.assertEqual(
            rc.tags_by_lane(names),
            {
                '': (['v1.10.0', 'v1.3.0'], ['v1.4.0-dev.1']),
                'yamlenv': (
                    ['yamlenv/v1.1.0', 'yamlenv/v1.0.9'],
                    ['yamlenv/v1.1.0-dev.10', 'yamlenv/v1.1.0-dev.2'],
                ),
            },
        )


class DevOrdering(unittest.TestCase):
    def test_dev_tags_sorted_by_version_then_counter(self):
        self.assertEqual(
            rc.dev_tags_sorted(
                ['v1.3.0-dev.2', 'v1.3.0', 'v1.3.0-dev.10', 'v1.10.0-dev.1', 'v1.9.0-dev.7']
            ),
            ['v1.10.0-dev.1', 'v1.9.0-dev.7', 'v1.3.0-dev.10', 'v1.3.0-dev.2'],
        )

    def test_dev_key_refuses_other_shapes(self):
        with self.assertRaises(ValueError):
            rc.dev_key('v1.3.0')


def paged(names):
    """A fetch_page over `names` in GitHub's newest-first, 100-per-page shape."""
    pages = [names[i : i + rc.TAG_PAGE_SIZE] for i in range(0, len(names), rc.TAG_PAGE_SIZE)]

    def fetch(page):
        return pages[page - 1] if page <= len(pages) else []

    fetch.pages = pages
    return fetch


class CollectTags(unittest.TestCase):
    def test_stable_tags_behind_more_than_one_page_of_dev_tags_are_found(self):
        names = [f'v1.4.0-dev.{n}' for n in range(250, 0, -1)] + ['v1.3.0', 'v1.2.0']
        got = rc.collect_tags(paged(names), want_stable=1)
        self.assertEqual(rc.newest_stable_tag(got), 'v1.3.0')
        self.assertEqual(len(got), 252)

    def test_stops_at_the_first_page_that_satisfies_the_request(self):
        names = ['v1.3.0-dev.3', 'v1.3.0-dev.2', 'v1.3.0-dev.1', 'v1.2.0'] + [
            f'v1.1.0-dev.{n}' for n in range(200)
        ]
        calls = []
        fetch = paged(names)

        def counting(page):
            calls.append(page)
            return fetch(page)

        got = rc.collect_tags(counting, want_stable=1, want_dev=3)
        self.assertEqual(calls, [1])
        self.assertEqual(len(got), 100)

    def test_keeps_paging_until_both_shapes_are_satisfied(self):
        names = [f'v1.4.0-dev.{n}' for n in range(120, 0, -1)] + [
            'v1.3.0',
            'v1.2.0',
            'v1.1.0',
            'v1.0.0',
            'v0.9.0',
        ]
        got = rc.collect_tags(paged(names), want_stable=5, want_dev=5)
        self.assertEqual(len(got), 125)

    def test_exhausted_pages_return_what_exists(self):
        got = rc.collect_tags(paged(['v1.3.0-dev.1']), want_stable=1)
        self.assertEqual(got, ['v1.3.0-dev.1'])

    def test_reaching_the_page_cap_unsatisfied_is_a_visible_failure(self):
        names = [f'v1.4.0-dev.{n}' for n in range(5000, 0, -1)] + ['v1.3.0']
        with self.assertRaises(rc.TagListingTruncatedError):
            rc.collect_tags(paged(names), want_stable=1, page_cap=3)

    def test_a_request_satisfied_on_the_last_allowed_page_is_not_truncated(self):
        names = [f'v1.4.0-dev.{n}' for n in range(250, 0, -1)] + ['v1.3.0']
        got = rc.collect_tags(paged(names), want_stable=1, page_cap=3)
        self.assertEqual(rc.newest_stable_tag(got), 'v1.3.0')

    def test_a_failed_page_read_is_none_not_a_short_list(self):
        self.assertIsNone(rc.collect_tags(lambda page: None, want_stable=1))


class CollectAllTags(unittest.TestCase):
    def test_reads_past_a_page_that_satisfies_every_root_count(self):
        names = [f'v1.{n}.0' for n in range(5)] + [f'v1.5.0-dev.{n}' for n in range(1, 6)]
        names += [f'other-{n}' for n in range(90)] + ['yamlenv/v9.0.0']
        calls = []
        fetch = paged(names)

        def counting(page):
            calls.append(page)
            return fetch(page)

        got = rc.collect_all_tags(counting)
        self.assertEqual(calls, [1, 2])
        self.assertIn('yamlenv/v9.0.0', got)

    def test_stops_at_the_first_short_page(self):
        names = [f'v1.4.0-dev.{n}' for n in range(150, 0, -1)]
        calls = []
        fetch = paged(names)

        def counting(page):
            calls.append(page)
            return fetch(page)

        self.assertEqual(len(rc.collect_all_tags(counting)), 150)
        self.assertEqual(calls, [1, 2])

    def test_a_listing_longer_than_the_cap_is_a_visible_failure(self):
        names = [f'v1.4.0-dev.{n}' for n in range(400, 0, -1)]
        with self.assertRaises(rc.TagListingTruncatedError):
            rc.collect_all_tags(paged(names), page_cap=3)

    def test_a_failed_page_read_is_none(self):
        self.assertIsNone(rc.collect_all_tags(lambda page: None))


class TagReceipts(unittest.TestCase):
    def test_receipt_is_the_tags_own_context_in_state_success(self):
        statuses = [
            {'context': 'ci / validate', 'state': 'success'},
            {'context': 'release/tag/v1.3.0-dev.4', 'state': 'success'},
            {'context': 'release/tag/v1.3.0-dev.5', 'state': 'pending'},
        ]
        self.assertTrue(rc.has_tag_receipt('v1.3.0-dev.4', statuses))
        self.assertFalse(rc.has_tag_receipt('v1.3.0-dev.5', statuses))
        self.assertFalse(rc.has_tag_receipt('v1.3.0-dev.6', statuses))
        self.assertEqual(
            rc.tag_receipt_context('yamlenv/v1.1.0-dev.1'), 'release/tag/yamlenv/v1.1.0-dev.1'
        )

    def test_completion_receipt_is_per_tag_and_lane(self):
        statuses = [
            {'context': 'release/complete/v1.3.0', 'state': 'success'},
            {'context': 'release/complete/yamlenv/v1.1.0', 'state': 'pending'},
            {'context': 'release/tag/v1.4.0', 'state': 'success'},
        ]
        self.assertTrue(rc.has_completion_receipt('v1.3.0', statuses))
        self.assertFalse(rc.has_completion_receipt('yamlenv/v1.1.0', statuses))
        self.assertFalse(rc.has_completion_receipt('v1.4.0', statuses))
        self.assertEqual(
            rc.completion_receipt_context('yamlenv/v1.1.0'), 'release/complete/yamlenv/v1.1.0'
        )

    def test_the_shell_writer_uses_the_same_contexts(self):
        script = (pathlib.Path(__file__).parent / 'release-state.sh').read_text()
        self.assertIn(f"COMPLETION_PREFIX='{rc.COMPLETION_RECEIPT_PREFIX}'", script)
        self.assertIn(f"TAG_RECEIPT_PREFIX='{rc.TAG_RECEIPT_PREFIX}'", script)

    def test_release_receipt_is_the_actions_author(self):
        self.assertTrue(
            rc.release_is_pipeline_authored({'author': {'login': 'github-actions[bot]'}})
        )
        self.assertFalse(rc.release_is_pipeline_authored({'author': {'login': 'cplieger'}}))
        self.assertFalse(rc.release_is_pipeline_authored({}))


class Tables(unittest.TestCase):
    def test_single_main_repos_are_never_deployed_images(self):
        self.assertFalse(rc.SINGLE_MAIN_REPOS & rc.DEPLOYED_IMAGE_REPOS)

    def test_machine_prefixes_end_with_slash(self):
        for prefix in rc.MACHINE_HEAD_PREFIXES:
            self.assertTrue(prefix.endswith('/'), prefix)

    def test_every_main_intake_head_is_a_machine_head(self):
        for head in (*rc.MAIN_INTAKE_PREFIXES, rc.MAIN_SYNC_HEAD):
            self.assertTrue(head.startswith(rc.MACHINE_HEAD_PREFIXES), head)


class MainIntakeKind(unittest.TestCase):
    def test_heads_main_takes(self):
        cases = {
            'renovate/main-weekly-dependencies': 'renovate',
            'renovate/main-github.com-x-y-vulnerability': 'renovate',
            'repo-sync/ci/main': 'sync',
            'rebuild/main-2026-10-04': 'rebuild',
        }
        for head, kind in cases.items():
            self.assertEqual(rc.main_intake_kind(head), kind, head)

    def test_every_other_head_is_refused(self):
        for head in (
            'renovate/main-',
            'rebuild/main-',
            'renovate/dev-weekly-dependencies',
            'renovate/go-1.x',
            'repo-sync/ci/dev',
            'repo-sync/ci/main-2',
            'repo-sync/ci/default',
            'rebuild/dev-2026-10-04',
            'feat/main-thing',
            'main',
            '',
        ):
            self.assertIsNone(rc.main_intake_kind(head), head)


class TwoBranch(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parent.parent
    # Every place that decides a repository's branch model, and how it asks.
    CONSUMERS: ClassVar[dict] = {
        'scripts/classify-repos.py': 'release_channels.is_two_branch(',
        'scripts/release_maintenance.py': 'rc.is_two_branch(',
        'scripts/audit.py': 'release_channels.is_two_branch(',
        'scripts/apply-rulesets.sh': 'rc.is_two_branch(',
        'scripts/promote.py': 'rc.is_two_branch(',
        'scripts/rebuild-stale.sh': 'release_channels.py" two-branch',
        'scripts/inventory-corpus.sh': 'release_channels.py" two-branch',
        '.github/workflows/daily-security.yaml': 'release_channels.py two-branch',
    }

    @staticmethod
    def repo(name='subflux', default_branch='dev', **over) -> dict:
        return {
            'name': name,
            'default_branch': default_branch,
            'visibility': 'public',
            'fork': False,
            'archived': False,
            **over,
        }

    def test_the_predicate(self):
        self.assertTrue(rc.is_two_branch(self.repo()))
        self.assertFalse(rc.is_two_branch(self.repo(default_branch='main')))
        self.assertFalse(rc.is_two_branch(self.repo('tool-catalog')))

    def test_a_private_fork_or_archived_dev_default_repo_is_not_enrolled(self):
        for over in (
            {'visibility': 'private'},
            {'visibility': 'internal'},
            {'fork': True},
            {'archived': True},
        ):
            with self.subTest(**over):
                self.assertFalse(rc.is_two_branch(self.repo(**over)))

    def test_a_missing_or_mistyped_field_is_not_enrolled(self):
        for field in ('name', 'default_branch', 'visibility', 'fork', 'archived'):
            with self.subTest(missing=field):
                repo = self.repo()
                del repo[field]
                self.assertFalse(rc.is_two_branch(repo))
        for over in ({'fork': None}, {'archived': 0}, {'visibility': 'PUBLIC'}, {'name': 7}):
            with self.subTest(**over):
                self.assertFalse(rc.is_two_branch(self.repo(**over)))

    def test_names_from_repository_objects(self):
        lines = [
            json.dumps(self.repo()) + '\n',
            json.dumps(self.repo('docker-age', 'main')) + '\n',
            json.dumps(self.repo('tool-catalog')) + '\n',
            json.dumps(self.repo('secret', visibility='private')) + '\n',
            '\n',
        ]
        self.assertEqual(rc.two_branch_names(lines), ['subflux'])
        for bad in ('subflux dev\n', '["subflux"]\n'):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                rc.two_branch_names([bad])

    def test_the_command_line_filter(self):
        script = self.ROOT / 'scripts' / 'release_channels.py'
        listing = ''.join(
            json.dumps(r) + '\n'
            for r in (
                self.repo(),
                self.repo('knell'),
                self.repo('docker-age', 'main'),
                self.repo('tool-catalog'),
                self.repo('secret', visibility='private'),
            )
        )
        proc = subprocess.run(
            [sys.executable, str(script), 'two-branch'],
            input=listing,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, 'subflux\nknell\n'))
        for argv, stdin in (([], ''), (['two-branch'], 'subflux dev\n')):
            with self.subTest(argv=argv):
                bad = subprocess.run(
                    [sys.executable, str(script), *argv],
                    input=stdin,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(bad.returncode, 0)
                self.assertEqual(bad.stdout, '')

    def test_every_consumer_asks_this_module_and_restates_nothing(self):
        for path, call in self.CONSUMERS.items():
            with self.subTest(path):
                text = (self.ROOT / path).read_text()
                self.assertIn(call, text)
                self.assertNotIn('def is_two_branch', text)
                self.assertNotIn('single_main', text)
                self.assertNotRegex(text, RAW_MODEL)
        defined = [
            p.name
            for p in (self.ROOT / 'scripts').glob('*.py')
            if not p.name.startswith('test') and 'def is_two_branch' in p.read_text()
        ]
        self.assertEqual(defined, ['release_channels.py'])


if __name__ == '__main__':
    unittest.main()
