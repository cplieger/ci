#!/usr/bin/env python3
"""Pin the tracker bodies the three aggregators render and the skeleton they share.

Golden bodies: each aggregator renders scripts/testdata/tracker/<x>/ twice, once
against the recorded existing issue body and once fresh, and both must equal the
committed expected files byte for byte. The expected files were rendered by the
aggregators as they were BEFORE trackerlib existed, so this is also the proof
that the port onto trackerlib changed no rendered issue. Regenerate them only
when a body change is intended, and say so in the commit.

Also pins trackerlib's own contract (history rolling, run-id replacement, the
unreadable-previous-row delta, trend and regression, notes carry-over) and the
links body renderer.

Run: python3 scripts/test-tracker-body.py     (exit 0 = pass)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trackerlib  # noqa: E402

FIXTURES = HERE / 'testdata' / 'tracker'
WEEK = '2026-09-14 22:30'
RUN_URL = 'https://github.com/cplieger/ci/actions/runs/34900000001'

HEADER = '| Run (UTC) | Score | Δ score |\n|---|---|---|'


def render(script: str, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(HERE / script), *args, '--week', WEEK, '--run-url', RUN_URL],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f'{script} exited {proc.returncode}:\n{proc.stderr}')
    return proc.stdout


class GoldenBodyTest(unittest.TestCase):
    """Each case renders with and without the existing body and diffs both."""

    def check_golden(
        self, name: str, script: str, *args: str, expected: str = 'expected'
    ) -> tuple[str, str]:
        """Render both ways and compare against `<expected>.txt` and `<expected>-fresh.txt`.

        Returns the body rendered with the existing notes and the regression
        marker, for assertions on the fixture's shape beyond byte equality.
        """
        fixture = FIXTURES / name
        with tempfile.TemporaryDirectory() as tmp:
            markers = [
                '--regression-marker-file',
                f'{tmp}/regression.txt',
            ]
            got = render(
                script,
                *args,
                *markers,
                '--existing-body-file',
                str(fixture / 'existing.txt'),
            )
            self.assertEqual(
                got, (fixture / f'{expected}.txt').read_text(), f'{name}: body with notes drifted'
            )

            fresh = render(script, *args, *markers)
            self.assertEqual(
                fresh,
                (fixture / f'{expected}-fresh.txt').read_text(),
                f'{name}: fresh body drifted',
            )
            self.assertIn(trackerlib.NOTES_DEFAULT, fresh)
            self.assertNotEqual(got, fresh, f'{name}: the fixture notes must survive the update')
            return got, Path(f'{tmp}/regression.txt').read_text()

    def test_gremlins_golden(self) -> None:
        self.check_golden(
            'gremlins',
            'gremlins-aggregate.py',
            '--repo',
            'scheduler',
            '--artifacts-dir',
            str(FIXTURES / 'gremlins'),
        )

    @unittest.skipIf(
        sys.version_info < (3, 14),
        'stryker-aggregate.py uses PEP 758 syntax; weekly-stryker.yaml pins python 3.14',
    )
    def test_stryker_golden(self) -> None:
        self.check_golden(
            'stryker',
            'stryker-aggregate.py',
            '--repo',
            'reactive',
            '--artifacts-dir',
            str(FIXTURES / 'stryker'),
        )

    def test_bench_golden(self) -> None:
        self.check_golden(
            'bench',
            'bench-aggregate.py',
            '--repo',
            'pathinside',
            '--data-file',
            str(FIXTURES / 'bench' / 'data.js'),
        )

    def test_bench_golden_regressed_only(self) -> None:
        """The week `bench-regression` fires on: regressions, nothing improved, nothing new.

        The lone Regressed block is the one findings shape that ends with a
        blank line before the closing sentinel; the all-buckets fixture above
        cannot see it.
        """
        body, marker = self.check_golden(
            'bench',
            'bench-aggregate.py',
            '--repo',
            'pathinside',
            '--data-file',
            str(FIXTURES / 'bench' / 'data-regressed.js'),
            expected='expected-regressed',
        )
        self.assertEqual(marker, 'true')
        self.assertIn('<summary>Regressed —', body)
        self.assertNotIn('<summary>Improved —', body)
        self.assertNotIn('<summary>First measurement —', body)
        self.assertIn('</details>\n\n<!-- /bench-findings -->', body)


class HistoryBlockTest(unittest.TestCase):
    def block(self, existing: str, row: str, run_id: str = '', keep: int = 12) -> str:
        return trackerlib.update_history_block(existing, 'x-data', HEADER, row, run_id, keep)

    def rows(self, block: str) -> list[str]:
        return trackerlib.history_lines(block, 'x-data')

    def test_first_row_has_no_delta_and_carries_the_run_marker(self) -> None:
        block = self.block('', '| 2026-09-14 22:30 | 80.0% |', '101')
        self.assertEqual(
            block,
            '<!-- x-data -->\n'
            + HEADER
            + '\n| 2026-09-14 22:30 | 80.0% | — | <!-- run:101 -->\n<!-- /x-data -->',
        )

    def test_delta_is_against_the_previous_newest_row(self) -> None:
        first = self.block('', '| 2026-09-07 22:30 | 80.0% |', '101')
        second = self.block(first, '| 2026-09-14 22:30 | 81.3% |', '102')
        self.assertEqual(
            self.rows(second),
            [
                '| 2026-09-14 22:30 | 81.3% | +1.3% | <!-- run:102 -->',
                '| 2026-09-07 22:30 | 80.0% | — | <!-- run:101 -->',
            ],
        )

    def test_same_run_replaces_its_own_row(self) -> None:
        first = self.block('', '| 2026-09-07 22:30 | 80.0% |', '101')
        retry = self.block(first, '| 2026-09-08 01:15 | 80.0% |', '101')
        self.assertEqual(self.rows(retry), ['| 2026-09-08 01:15 | 80.0% | — | <!-- run:101 -->'])

    def test_a_row_without_a_marker_is_always_kept(self) -> None:
        legacy = (
            '<!-- x-data -->\n' + HEADER + '\n| 2026-08-01 00:00 | 70.0% | — |\n<!-- /x-data -->'
        )
        block = self.block(legacy, '| 2026-09-14 22:30 | 75.0% |', '101')
        self.assertEqual(len(self.rows(block)), 2)
        self.assertTrue(self.rows(block)[0].startswith('| 2026-09-14 22:30 | 75.0% | +5.0% |'))

    def test_an_unreadable_previous_score_yields_no_delta(self) -> None:
        odd = '<!-- x-data -->\n' + HEADER + '\n| 2026-08-01 00:00 | n/a | — |\n<!-- /x-data -->'
        block = self.block(odd, '| 2026-09-14 22:30 | 75.0% |', '101')
        self.assertEqual(self.rows(block)[0], '| 2026-09-14 22:30 | 75.0% | — | <!-- run:101 -->')

    def test_window_trims_the_oldest_rows(self) -> None:
        block = ''
        for week in range(1, 15):
            block = self.block(
                block, f'| 2026-01-{week:02d} 00:00 | {50 + week}.0% |', str(week), 12
            )
        rows = self.rows(block)
        self.assertEqual(len(rows), 12)
        self.assertIn('2026-01-14', rows[0])
        self.assertIn('2026-01-03', rows[-1])

    def test_history_column_skips_unreadable_cells_but_rows_do_not(self) -> None:
        body = (
            '<!-- x-data -->\n'
            + HEADER
            + '\n| 2026-09-14 22:30 | 3 +28.5% | — |\n| 2026-09-07 22:30 | 80.0% | — |\n<!-- /x-data -->'
        )
        self.assertEqual(trackerlib.history_column(body, 'x-data', 1), [80.0])
        self.assertEqual(len(trackerlib.history_rows(body, 'x-data')), 2)
        self.assertEqual(trackerlib.history_rows(body, 'x-data')[0][1], '3 +28.5%')

    def test_missing_sentinel_means_no_history(self) -> None:
        self.assertEqual(trackerlib.history_rows('# body without table\n', 'x-data'), [])
        self.assertIsNone(trackerlib.sentinel_inner('', 'x-data'))


class SkeletonTest(unittest.TestCase):
    def test_run_id_of(self) -> None:
        self.assertEqual(trackerlib.run_id_of(RUN_URL), '34900000001')
        self.assertEqual(trackerlib.run_id_of('https://example.invalid/run/1'), '')
        self.assertEqual(trackerlib.run_id_of(''), '')

    def test_trend_marker(self) -> None:
        self.assertEqual(trackerlib.trend_marker(80.0, []), '')
        self.assertEqual(
            trackerlib.trend_marker(80.0, [70.0, 75.0]),
            '**Trend**: ↗ +7.5% from 12-week mean (72.5%).',
        )
        self.assertIn('↘ -2.5%', trackerlib.trend_marker(70.0, [72.5]))
        self.assertIn('→ +0.4%', trackerlib.trend_marker(72.9, [72.5]))

    def test_regression_threshold(self) -> None:
        self.assertFalse(trackerlib.regression(80.0, [], 5.0))
        self.assertFalse(trackerlib.regression(75.0, [80.0], 5.0))
        self.assertTrue(trackerlib.regression(74.9, [80.0], 5.0))

    def test_preserve_notes(self) -> None:
        self.assertEqual(trackerlib.preserve_notes(''), trackerlib.NOTES_DEFAULT)
        self.assertEqual(
            trackerlib.preserve_notes('# T\n\n## Free-form notes\n\n\n'), trackerlib.NOTES_DEFAULT
        )
        self.assertEqual(
            trackerlib.preserve_notes('# T\n\n## Free-form notes\n\nkeep me\n\n- and me\n'),
            'keep me\n\n- and me',
        )

    def test_links_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'links.md'
            report.write_bytes(b'## Errors\n\n* [404] https://example.invalid/x\n')
            proc = subprocess.run(
                [
                    sys.executable,
                    str(HERE / 'links-body.py'),
                    '--report',
                    str(report),
                    '--run-url',
                    RUN_URL,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith('Automated weekly external-link check.'))
        self.assertIn(
            '<!-- links-data -->\n## Errors\n\n* [404] https://example.invalid/x\n<!-- /links-data -->',
            proc.stdout,
        )
        self.assertTrue(proc.stdout.endswith(f'\n\n_Run: {RUN_URL}_\n'))
        self.assertNotIn('\n\n\n', proc.stdout, 'no hard-wrapped preamble, no blank-line runs')


if __name__ == '__main__':
    unittest.main(verbosity=2)
