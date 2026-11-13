"""mutation_cost.py's attribution, history, projection, packing and caps, and mutation-history.sh against a stub gh."""

from __future__ import annotations

import ast
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import zipfile

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import mutation_cost  # noqa: E402


class Attribute(unittest.TestCase):
    def test_each_completion_is_charged_its_gap_times_the_workers(self):
        got = mutation_cost.attribute([(12.0, 'a.go'), (13.0, 'a.go'), (17.0, 'b.go')], 10.0, 4)
        self.assertEqual(
            got, {'a.go': {'secs': 12.0, 'mutants': 2}, 'b.go': {'secs': 16.0, 'mutants': 1}}
        )

    def test_a_completion_stamped_out_of_order_costs_nothing(self):
        got = mutation_cost.attribute([(14.0, 'a.go'), (12.0, 'b.go'), (15.0, 'b.go')], 10.0, 2)
        self.assertEqual(
            got, {'a.go': {'secs': 8.0, 'mutants': 1}, 'b.go': {'secs': 2.0, 'mutants': 2}}
        )

    def test_a_completion_before_the_start_costs_nothing(self):
        self.assertEqual(
            mutation_cost.attribute([(9.0, 'a.go')], 10.0, 4), {'a.go': {'secs': 0.0, 'mutants': 1}}
        )


class History(unittest.TestCase):
    def test_combine_averages_each_file_over_the_runs_that_measured_it(self):
        got = mutation_cost.combine(
            [
                {
                    'files': {'a': {'secs': 10, 'mutants': 2}, 'b': {'secs': 4, 'mutants': 1}},
                    'setup_secs': 100,
                },
                {'files': {'a': {'secs': 30, 'mutants': 2}}, 'setup_secs': 200},
                {'files': {'b': {'secs': 8, 'mutants': 1}}},
            ]
        )
        self.assertEqual(
            got,
            {
                'files': {'a': {'secs': 20.0, 'mutants': 2}, 'b': {'secs': 6.0, 'mutants': 1}},
                'setup_secs': 150.0,
            },
        )

    def test_unusable_rows_of_an_untrusted_record_are_dropped(self):
        rows = {
            'ok': {'secs': 1, 'mutants': 1},
            'neg': {'secs': -1, 'mutants': 1},
            'nan': {'secs': float('nan'), 'mutants': 1},
            'zero': {'secs': 1, 'mutants': 0},
            'bool': {'secs': True, 'mutants': 1},
            'str': {'secs': '1', 'mutants': 1},
            'list': [1],
        }
        self.assertEqual(mutation_cost.clean_files(rows), {'ok': {'secs': 1.0, 'mutants': 1}})
        self.assertEqual(mutation_cost.clean_files(['a']), {})

    def test_combine_of_nothing_has_no_setup(self):
        self.assertEqual(mutation_cost.combine([]), {'files': {}, 'setup_secs': None})


class Project(unittest.TestCase):
    def test_history_prices_a_file_per_mutant_and_the_rest_at_its_prior_times_the_error(self):
        e = mutation_cost.PRIOR_ERROR
        units = {'a': (4, 100.0), 'b': (2, 50.0), 'new': (3, 30.0)}
        hist = {'files': {'a': {'secs': 160.0, 'mutants': 4}, 'b': {'secs': 30.0, 'mutants': 3}}}
        got = mutation_cost.project(units, hist)
        # Measured files set no ratio for the rest: one file's prior error says nothing of another's.
        self.assertEqual(
            got, {'a': (160.0, 'measured'), 'b': (20.0, 'measured'), 'new': (30.0 * e, 'prior')}
        )

    def test_no_history_is_the_prior_times_the_error(self):
        want = {'a': (7.0 * mutation_cost.PRIOR_ERROR, 'prior')}
        self.assertEqual(mutation_cost.project({'a': (1, 7.0)}, None), want)
        self.assertEqual(mutation_cost.project({'a': (1, 7.0)}, {'files': 'junk'}), want)

    def test_a_sample_short_of_the_unit_s_mutants_prices_what_it_measured_and_the_rest_at_the_error(
        self,
    ):
        e = mutation_cost.PRIOR_ERROR
        # 987 of 1672 mutants measured at 72.4 s each before a deadline cut the shard.
        row = {'secs': 987 * 72.4, 'mutants': 987}
        got = mutation_cost.project({'s.ts': (1672, 1672 * 30.0)}, {'files': {'s.ts': row}})
        self.assertEqual(got, {'s.ts': (987 * 72.4 + 685 * 30.0 * e, 'partial')})
        self.assertEqual(mutation_cost.measured_part(got['s.ts'], row), 987 * 72.4)
        # With each mutant's prior, the ones it did not reach are taken as the costliest.
        each = [1.0, 9.0, 2.0, 8.0]
        cut = {'secs': 6.0, 'mutants': 2}
        self.assertEqual(mutation_cost.price(4, 20.0, cut, each), (6.0 + 17.0 * e, 'partial'))
        self.assertEqual(mutation_cost.measured_part((1.0, 'prior'), None), 0.0)
        self.assertEqual(mutation_cost.measured_part((5.0, 'measured'), None), 5.0)
        # With no prior either, its cost is unknown.
        hist = {'files': {'u.ts': {'secs': 20.0, 'mutants': 2}}}
        self.assertEqual(
            mutation_cost.project({'u.ts': (5, None)}, hist), {'u.ts': (None, 'unknown')}
        )
        hist['files']['u.ts']['mutants'] = 5
        self.assertEqual(
            mutation_cost.project({'u.ts': (5, None)}, hist), {'u.ts': (20.0, 'measured')}
        )

    def test_spread_shares_a_total_by_weight_and_keeps_its_sum(self):
        self.assertEqual(mutation_cost.spread(90.0, [1.0, 2.0]), [30.0, 60.0])
        self.assertEqual(mutation_cost.spread(10.0, [0.0, 0.0]), [5.0, 5.0])

    def test_a_row_of_another_blob_is_dropped(self):
        hist = {
            'files': {
                'a': {'secs': 1.0, 'mutants': 1},
                'b': {'secs': 2.0, 'mutants': 1},
                'c': {'secs': 3.0, 'mutants': 1},
            },
            'blobs': {'a': 'a' * 40, 'b': 'a' * 40},
        }
        got = mutation_cost.at_blobs(hist, {'a': 'a' * 40, 'b': 'b' * 40, 'c': 'a' * 40})
        self.assertEqual(sorted(got['files']), ['a'])
        self.assertIsNone(mutation_cost.at_blobs(None, {}))

    def test_a_file_with_no_mutant_left_costs_nothing(self):
        got = mutation_cost.project({'a': (0, 5.0)}, {'files': {'a': {'secs': 9.0, 'mutants': 3}}})
        self.assertEqual(got, {'a': (0.0, 'measured')})

    def test_a_price_outside_the_factor_of_its_probe_is_refused_and_the_file_takes_the_prior(self):
        f = mutation_cost.PRICE_FACTOR
        units = {
            'ok': (1, 10.0),
            'edge': (1, 10.0),
            'zero': (1, 10.0),
            'huge': (2, 10.0),
            'tiny': (1, 10.0),
            'free': (1, 0.0),
            'new': (1, 10.0),
        }
        hist = {
            'files': {
                'ok': {'secs': 20.0, 'mutants': 1},
                'edge': {'secs': 10.0 * f, 'mutants': 1},
                'zero': {'secs': 0.0, 'mutants': 1},
                'huge': {'secs': 1e300, 'mutants': 2},
                'tiny': {'secs': 10.0 / f / 1.01, 'mutants': 1},
                'free': {'secs': 1.0, 'mutants': 1},
            }
        }
        got = mutation_cost.project(units, hist)
        self.assertEqual(got['ok'], (20.0, 'measured'))
        self.assertEqual(got['edge'], (10.0 * f, 'measured'))
        for name in ('zero', 'huge', 'tiny', 'new'):
            with self.subTest(name=name):
                self.assertAlmostEqual(got[name][0], 10.0 * mutation_cost.PRIOR_ERROR)
                self.assertEqual(got[name][1], 'prior' if name == 'new' else 'refused')
        self.assertEqual(got['free'], (0.0, 'refused'))

    def test_a_probe_under_its_clock_is_unknown_never_zero(self):
        for probe, clock, want in (
            (0.0, 1.0, None),
            (0.5, 1.0, None),
            (1.0, 1.0, 1.0),
            (7.0, 1.0, 7.0),
        ):
            with self.subTest(probe=probe):
                self.assertEqual(mutation_cost.known(probe, clock), want)

    def test_history_is_judged_only_against_a_known_probe(self):
        self.assertTrue(mutation_cost.plausible(2700.0, None))
        self.assertFalse(mutation_cost.plausible(0.0, None))
        self.assertFalse(mutation_cost.plausible(2700.0, 1.0))

    def test_an_unknown_prior_takes_its_measured_history_and_is_unknown_without_one(self):
        units = {'hist': (3000, None), 'none': (5, None), 'zero': (5, None), 'empty': (0, None)}
        hist = {
            'files': {
                'hist': {'secs': 2700.0, 'mutants': 3000},
                'zero': {'secs': 0.0, 'mutants': 5},
            }
        }
        got = mutation_cost.project(units, hist)
        self.assertEqual(got['hist'], (2700.0, 'measured'))
        self.assertEqual(got['none'], (None, 'unknown'))
        self.assertEqual(got['zero'], (None, 'unknown'))
        self.assertEqual(got['empty'], (0.0, 'prior'))

    def test_an_unknown_prior_does_not_change_the_price_of_the_rest(self):
        units = {'a': (4, 100.0), 'u': (10, None), 'new': (3, 30.0)}
        hist = {'files': {'a': {'secs': 80.0, 'mutants': 2}, 'u': {'secs': 1e4, 'mutants': 10}}}
        got = mutation_cost.project(units, hist)
        self.assertEqual(got['u'], (1e4, 'measured'))
        self.assertEqual(got['new'], (30.0 * mutation_cost.PRIOR_ERROR, 'prior'))

    def test_a_measured_setup_outside_the_factor_of_its_probe_is_refused(self):
        f = mutation_cost.PRICE_FACTOR
        for measured, want in (
            (80.0, 80.0),
            (60.0 * f, 60.0 * f),
            (60.0 * f * 1.01, 60.0),
            (0.0, 60.0),
            (None, 60.0),
            (float('nan'), 60.0),
            (10**400, 60.0),
            (True, 60.0),
        ):
            with self.subTest(measured=measured):
                self.assertEqual(mutation_cost.setup_price(measured, 60.0), want)

    def test_overflowing_prices_never_raise(self):
        units = {f'f{i}': (2**53, 1e308) for i in range(4)}
        hist = {
            'files': {
                'f0': {'secs': 1e308, 'mutants': 1},
                'f1': {'secs': 10**400, 'mutants': 1},
                'f2': {'secs': 1.0, 'mutants': 10**400},
            }
        }
        got = mutation_cost.project(units, hist)
        self.assertEqual(set(got), set(units))
        self.assertTrue(all(secs >= 0 for secs, _ in got.values()))


class Numbers(unittest.TestCase):
    def test_a_number_is_finite_nonnegative_and_an_int_no_wider_than_a_float_holds_exactly(self):
        for v, ok in (
            (0, True),
            (2**53, True),
            (2**53 + 1, False),
            (10**400, False),
            (1.5, True),
            (-1, False),
            (float('inf'), False),
            (float('nan'), False),
            (True, False),
            ('1', False),
        ):
            with self.subTest(v=v):
                self.assertIs(mutation_cost.nonnegative(v), ok)

    def test_a_count_is_an_int_in_range(self):
        for v, ok in (
            (0, True),
            (2**53, True),
            (2**53 + 1, False),
            (1.0, False),
            (-1, False),
            (False, False),
        ):
            with self.subTest(v=v):
                self.assertIs(mutation_cost.count(v), ok)
        self.assertEqual(mutation_cost.clean_files({'a': {'secs': 1, 'mutants': 10**400}}), {})


class Pack(unittest.TestCase):
    def test_longest_first_and_the_fewest_shards_that_fit(self):
        items = [mutation_cost.Item('m', f'f{w}', w) for w in (1, 7, 3, 6, 2, 5, 4)]
        bins = mutation_cost.pack(items, 2, {'m': 0})
        self.assertEqual(
            [sorted(it.unit for it in b.items) for b in bins],
            [['f3', 'f4', 'f7'], ['f1', 'f2', 'f5', 'f6']],
        )
        k, _ = mutation_cost.choose(items, {'m': 0}, 17.25)
        self.assertEqual(k, 2)

    def test_two_groups_pack_as_the_exhaustive_rule_does(self):
        items = [mutation_cost.Item('x' if w % 3 else 'y', f'f{w}', float(w)) for w in range(1, 40)]
        setup = {'x': 7.0, 'y': 2.5}
        for k in (1, 2, 3, 5, 8):
            bins = [mutation_cost.Bin() for _ in range(k)]
            for it in sorted(items, key=lambda it: (-it.work, it.group, it.unit)):
                i = min(
                    range(k),
                    key=lambda i: (
                        bins[i].load
                        + it.work
                        + (0 if it.group in bins[i].groups else setup[it.group]),
                        it.group not in bins[i].groups,
                        i,
                    ),
                )
                bins[i].load += it.work + (0 if it.group in bins[i].groups else setup[it.group])
                bins[i].groups.add(it.group)
                bins[i].items.append(it)
            got = mutation_cost.pack(items, k, setup)
            self.assertEqual(
                [[it.unit for it in b.items] for b in got],
                [[it.unit for it in b.items] for b in bins],
            )


class Choose(unittest.TestCase):
    """Shards land in a band whose top is the given one: 85 to 115 under a 115-s top."""

    @staticmethod
    def items(*work: float) -> list:
        return [mutation_cost.Item('m', f'f{i}', w) for i, w in enumerate(work)]

    def test_a_run_inside_the_band_s_top_stays_one_job(self):
        k, bins = mutation_cost.choose(self.items(60, 50), {'m': 0}, 115)
        self.assertEqual((k, bins[0].load), (1, 110))

    def test_the_count_that_lands_every_shard_in_the_band_beats_more_shards_under_the_target(self):
        k, bins = mutation_cost.choose(self.items(*[10] * 22), {'m': 0}, 115)
        self.assertEqual(k, 2)
        self.assertTrue(all(85 <= b.load <= 115 for b in bins), [b.load for b in bins])

    def test_with_no_count_in_the_band_the_fewest_under_its_top(self):
        k, bins = mutation_cost.choose(self.items(*[10] * 16), {'m': 0}, 115)
        self.assertEqual((k, [b.load for b in bins]), (2, [80, 80]))

    def test_an_item_over_the_band_stops_at_the_fewest_shards_that_isolate_it(self):
        k, bins = mutation_cost.choose(self.items(500, 30, 20), {'m': 10}, 115)
        self.assertEqual((k, max(b.load for b in bins)), (2, 510))

    def test_an_item_past_the_top_runs_alone_and_the_rest_still_fit_the_top(self):
        k, bins = mutation_cost.choose(self.items(500, 300, 60, 50, 40), {'m': 0}, 115)
        self.assertEqual(k, 4)
        self.assertEqual(sorted(b.load for b in bins), [60, 90, 300, 500])
        items = self.items(500, 300, 60, 50, 40)
        self.assertFalse(mutation_cost.capped(k, bins, items, {'m': 0}, 115, 4))
        k, bins = mutation_cost.choose(items, {'m': 0}, 115, 3)
        self.assertEqual(sorted(b.load for b in bins), [150, 300, 500])
        self.assertTrue(mutation_cost.capped(k, bins, items, {'m': 0}, 115, 3))

    def test_two_thousand_items_with_one_over_the_target_choose_in_seconds(self):
        items = self.items(5000, *[1.0 + i % 7 for i in range(1999)])
        started = time.monotonic()
        _, bins = mutation_cost.choose(items, {'m': 30}, 690)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(max(b.load for b in bins), 5030)
        self.assertEqual(sum(len(b.items) for b in bins), 2000)

    def test_the_target_holds_the_slowest_runner_and_the_top_holds_the_band_under_it(self):
        mc = mutation_cost
        self.assertEqual((mc.SAFE_JOB_MINUTES, mc.SLOWDOWN, mc.BAND), (300, 1.9, 0.15))
        # gremlins' 3-min setup, marotte static-src's 31.2 min and a slow 80 min.
        for minutes in (0.0, 3.0, 31.2, 80.0):
            setup = minutes * 60
            with self.subTest(setup=minutes):
                target = mc.target_secs(setup)
                self.assertAlmostEqual(target, 300 * 60 / 1.9 - setup)
                # The runner slows the setup too (1.46x in run 43153682226), so the whole job scales.
                self.assertAlmostEqual(mc.SLOWDOWN * (setup + target), mc.SAFE_JOB_MINUTES * 60)
                # A shard at the top whose mean runs BAND over still ends in time on the slowest runner.
                self.assertLessEqual(
                    mc.SLOWDOWN * (1 + mc.BAND) * mc.TOP_SECS, mc.SAFE_JOB_MINUTES * 60 + 1e-6
                )
                self.assertAlmostEqual(mc.TOP_SECS * (1 + mc.BAND), setup + target)
        self.assertAlmostEqual(mc.target_secs(180) / 60, 154.9, places=1)
        self.assertAlmostEqual(mc.target_secs(1872) / 60, 126.7, places=1)
        self.assertAlmostEqual(mc.TOP_SECS / 60, 137.3, places=1)
        self.assertEqual(mc.target_secs(400 * 60), 0.0)

    def test_a_run_past_the_top_splits_and_none_of_its_shards_passes_it(self):
        top = mutation_cost.TOP_SECS
        k, bins = mutation_cost.choose(self.items(*[10 * 60] * 22), {'m': 180}, top)
        self.assertEqual(k, 2)
        self.assertTrue(all(b.load <= top for b in bins), [b.load for b in bins])

    def test_no_count_passes_the_limit_whatever_the_prices(self):
        k, _ = mutation_cost.choose(self.items(*[1e6] * 300), {'m': 30}, 230 * 60)
        self.assertEqual(k, mutation_cost.MAX_SHARDS)
        for work in (1e6, 1e300, 1e308, float('inf')):
            for limit in (mutation_cost.MAX_SHARDS, 3):
                with self.subTest(work=work, limit=limit):
                    items = self.items(*[work] * 300)
                    k, bins = mutation_cost.choose(items, {'m': 30}, 230 * 60, limit=limit)
                    self.assertTrue(1 <= k <= limit and len(bins) == k, k)
                    self.assertEqual(sum(len(b.items) for b in bins), 300)
        k, _ = mutation_cost.choose(
            self.items(1e300, 1e308, 1e308), {'m': 1e308}, 230 * 60, limit=1
        )
        self.assertEqual(k, 1)

    def test_capped_says_whether_more_shards_would_have_shortened_the_longest(self):
        items = self.items(*[60 * 60] * 30)
        k, bins = mutation_cost.choose(items, {'m': 0}, 230 * 60, limit=4)
        self.assertTrue(mutation_cost.capped(k, bins, items, {'m': 0}, 230 * 60, limit=4))
        k, bins = mutation_cost.choose(items, {'m': 0}, 230 * 60)
        self.assertFalse(
            mutation_cost.capped(k, bins, items, {'m': 0}, 230 * 60, limit=mutation_cost.MAX_SHARDS)
        )
        lone = self.items(500 * 60, 10, 10)
        k, bins = mutation_cost.choose(lone, {'m': 0}, 230 * 60, limit=2)
        self.assertFalse(mutation_cost.capped(k, bins, lone, {'m': 0}, 230 * 60, limit=2))


class Bound(unittest.TestCase):
    """fit: no shard projects past BOUND_SECS but one its job's deadline bounds; else the plan fails."""

    B = mutation_cost.BOUND_SECS

    @staticmethod
    def items(*work: float, known: float | None = None) -> list:
        return [mutation_cost.Item('m', f'f{i}', w, known) for i, w in enumerate(work)]

    def test_the_bound_holds_the_slowest_runner_to_the_safe_limit(self):
        mc = mutation_cost
        self.assertAlmostEqual(mc.SLOWDOWN * mc.BOUND_SECS, mc.SAFE_JOB_MINUTES * 60)
        self.assertAlmostEqual(mc.target_secs(600.0), mc.BOUND_SECS - 600.0)

    def test_a_count_past_the_limit_is_taken_where_the_bound_needs_it(self):
        items = self.items(*[self.B * 0.6] * 40)
        k, bins = mutation_cost.choose(items, {'m': 0}, mutation_cost.TOP_SECS)
        self.assertTrue(mutation_cost.breaches(bins, {'m': 0}))
        k, bins = mutation_cost.fit(items, {'m': 0}, mutation_cost.TOP_SECS)
        self.assertEqual((k, mutation_cost.breaches(bins, {'m': 0})), (40, []))
        self.assertGreater(k, mutation_cost.MAX_SHARDS)

    def test_an_estimate_past_the_bound_runs_alone_under_the_deadline(self):
        items = [*self.items(self.B * 3, known=0.0), *self.items(10.0, 20.0)]
        _, bins = mutation_cost.fit(items, {'m': 60}, mutation_cost.TOP_SECS)
        self.assertEqual(mutation_cost.breaches(bins, {'m': 60}), [])
        alone = mutation_cost.deadline_bound(bins, {'m': 60})
        self.assertEqual([[it.work for it in bins[i].items] for i in alone], [[self.B * 3]])
        # Shared with other work, it is a breach: only a unit alone is the deadline's.
        shared = mutation_cost.pack(items, 1, {'m': 60})
        self.assertEqual(mutation_cost.breaches(shared, {'m': 60}), [0])
        self.assertEqual(mutation_cost.breaches(shared, {'m': 60}, whole=True), [])

    def test_a_measure_past_the_bound_fails_the_plan_named(self):
        for items, known in ((self.items(self.B, 10.0), None), (self.items(self.B * 2), self.B)):
            with self.subTest(known=known):
                items = [mutation_cost.Item('m', 'big', self.B * 2, known), *items[1:]]
                with self.assertRaises(mutation_cost.UnsafePlanError) as ctx:
                    mutation_cost.fit(items, {'m': 1.0}, mutation_cost.TOP_SECS)
                self.assertIn('big alone projects', str(ctx.exception))

    def test_more_shards_than_most_fails_the_plan(self):
        items = self.items(*[self.B * 0.9] * 10)
        with self.assertRaises(mutation_cost.UnsafePlanError) as ctx:
            mutation_cost.fit(items, {'m': 0}, mutation_cost.TOP_SECS, most=9)
        self.assertIn('no count of up to 9 shards', str(ctx.exception))
        with self.assertRaises(mutation_cost.UnsafePlanError) as ctx:
            mutation_cost.fit(self.items(self.B, self.B), {'m': 0}, mutation_cost.TOP_SECS, 1, 1)
        self.assertIn('cannot be sharded', str(ctx.exception))

    def test_the_deadline_note_names_each_unit_or_nothing(self):
        note = mutation_cost.deadline_note('r:web', {'deadline': ['a.ts:1-9', 'b.ts']})
        self.assertIn(
            'r:web: unmeasured, running alone under the deadline: a.ts:1-9, b.ts',
            mutation_cost.render(note),
        )
        for plan in ({'deadline': []}, {}, None, {'deadline': [1]}):
            self.assertIsNone(mutation_cost.deadline_note('r', plan))

    def test_a_unit_s_source_is_partial_where_its_parts_mix(self):
        for parts, want in (
            (['measured'], 'measured'),
            (['prior', 'prior'], 'prior'),
            (['measured', 'prior'], 'partial'),
            (['partial', 'measured'], 'partial'),
            (['refused', 'measured'], 'refused'),
            (['unknown', 'prior'], 'unknown'),
            ([], 'measured'),
        ):
            self.assertEqual(mutation_cost.source_of(parts), want, parts)


class Bar(unittest.TestCase):
    def test_each_shard_s_mean_over_its_attempts_is_judged_never_one_job(self):
        # 160 and 282 min on one shard's work, 1.76x apart, average 221 against 200.
        self.assertEqual(mutation_cost.bar_faults([200.0], [[160.0], [282.0]]), [])
        self.assertEqual(
            mutation_cost.bar_faults([200.0], [[240.0], [242.0]]),
            ['the run averaged 241.0 min over its shards against 200.0 projected'],
        )
        self.assertEqual(mutation_cost.bar_faults([150.0], [[180.0]]), [])

    def test_a_shard_over_its_projection_misses_the_bar(self):
        # The run is 10% over, within the band, but shard 1 averages 20% over.
        self.assertEqual(
            mutation_cost.bar_faults([100.0, 100.0], [[120.0, 100.0], [120.0, 100.0]]),
            ['shard 1 averaged 120.0 min against 100.0 projected'],
        )

    def test_a_shard_under_its_projection_is_no_fault_while_the_run_is_within_the_band(self):
        self.assertEqual(
            mutation_cost.bar_faults([100.0, 100.0], [[75.0, 100.0], [75.0, 100.0]]), []
        )
        # Run 43328219246: shards 3 and 4 averaged 17.1% and 22.7% under, the run 9.8% under.
        attempts = [
            [120.5, 125.5, 110.2, 91.3, 131.6],
            [147.4, 85.0, 119.5, 129.4, 133.1],
            [133.5, 161.3, 113.5, 99.5, 165.9],
        ]
        self.assertEqual(mutation_cost.bar_faults([138.0] * 5, attempts), [])

    def test_a_run_under_its_projection_misses_the_bar(self):
        self.assertEqual(
            mutation_cost.bar_faults([100.0, 100.0], [[60.0, 80.0], [60.0, 80.0]]),
            ['the run averaged 140.0 min over its shards against 200.0 projected'],
        )

    def test_one_attempt_per_shard_is_judged_once_over_the_run(self):
        # Stryker's one job per shard: 180 + 100 against 150 + 150 is within the band.
        self.assertEqual(mutation_cost.bar_faults([150.0, 150.0], [[180.0, 100.0]]), [])
        self.assertEqual(
            mutation_cost.bar_faults([150.0, 150.0], [[180.0, 190.0]]),
            ['the run took 370.0 min over its shards against 300.0 projected'],
        )
        self.assertEqual(
            mutation_cost.bar_faults([150.0], [[301.0]]), ['shard 1 took 301.0 min, past 300']
        )

    def test_every_job_ends_by_the_safe_limit(self):
        self.assertEqual(
            mutation_cost.bar_faults([280.0], [[250.0], [301.0]]),
            ['shard 1 took 301.0 min, past 300'],
        )

    def test_a_shard_missing_from_an_attempt_has_no_mean(self):
        self.assertEqual(
            mutation_cost.bar_faults([100.0, 100.0], [[100.0, None], [100.0, 100.0]]),
            ['shard 2 has no measure in every attempt'],
        )
        self.assertEqual(
            mutation_cost.bar_faults([100.0], []), ['shard 1 has no measure in every attempt']
        )


class PriceOwner(unittest.TestCase):
    """mutation_cost.price is the one place a unit's price is set: nothing else scales a prior."""

    COST_SCRIPTS = ('mutation_cost.py', 'stryker-shard.py', 'gremlins-shard.py')
    # Where a prior meets a measure by right: price sets prices, prior_error measures the ratio.
    OWNERS = frozenset({'price', 'prior_error'})
    PRIOR = frozenset({'prior', 'priors', 'prior_secs', 'PRIOR_ERROR'})
    # A history row's key, or a local that names its measure.
    MEASURE = frozenset({"'secs'", 'measured'})

    def taint(self, node: ast.AST, names: dict[str, set[str]]) -> set[str]:
        """What NODE reads: 'prior' (a prior or PRIOR_ERROR) and 'measure' (a measured secs),
        through NAMES, the local names already assigned from either."""
        out: set[str] = set()
        for n in ast.walk(node):
            word = (
                n.id
                if isinstance(n, ast.Name)
                else n.attr
                if isinstance(n, ast.Attribute)
                else repr(n.value)
                if isinstance(n, ast.Constant) and n.value == 'secs'
                else n.value
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                else None
            )
            out |= {'prior'} if word in self.PRIOR else set()
            out |= {'measure'} if word in self.MEASURE else set()
            if isinstance(n, ast.Name):
                out |= names.get(n.id, set())
        return out

    def mixed(self, fn: ast.FunctionDef) -> list[int]:
        """The lines of FN where a multiply or divide reads both a prior and a measure, through any
        chain of local assignments, loops and comprehensions."""
        names: dict[str, set[str]] = {}
        flows = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                flows += [(t, n.value) for t in n.targets]
            elif isinstance(n, ast.AugAssign | ast.AnnAssign | ast.NamedExpr) and n.value:
                flows.append((n.target, n.value))
            elif isinstance(n, ast.For | ast.comprehension):
                flows.append((n.target, n.iter))
        changed = True
        while changed:
            changed = False
            for target, value in flows:
                got = self.taint(value, names)
                for t in ast.walk(target):
                    if isinstance(t, ast.Name) and not got <= names.get(t.id, set()):
                        names[t.id] = names.get(t.id, set()) | got
                        changed = True
        return sorted(
            {
                n.lineno
                for n in ast.walk(fn)
                if isinstance(n, ast.BinOp)
                and isinstance(n.op, ast.Mult | ast.Div)
                and self.taint(n, names) >= {'prior', 'measure'}
            }
        )

    @staticmethod
    def priced(node: ast.expr) -> bool:
        """NODE reads PRIOR_ERROR or a unit's prior_secs."""
        for n in ast.walk(node):
            if isinstance(n, ast.Name | ast.Attribute) and (
                getattr(n, 'id', None) == 'PRIOR_ERROR' or getattr(n, 'attr', None) == 'PRIOR_ERROR'
            ):
                return True
            if isinstance(n, ast.Constant) and n.value == 'prior_secs':
                return True
        return False

    def sites(self, source: str, name: str) -> list[str]:
        out = []

        def visit(node: ast.AST, owner: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef):
                    visit(child, child.name)
                    continue
                if (
                    isinstance(child, ast.BinOp | ast.AugAssign)
                    and isinstance(child.op, ast.Mult | ast.Div)
                    and owner != 'price'
                    and self.priced(child)
                ):
                    out.append(f'{name}:{child.lineno} in {owner}')
                visit(child, owner)

        visit(ast.parse(source), '<module>')
        for fn in ast.walk(ast.parse(source)):
            if isinstance(fn, ast.FunctionDef) and fn.name not in self.OWNERS:
                out += [f'{name}:{line} in {fn.name}' for line in self.mixed(fn)]
        return sorted(set(out), key=lambda x: int(x.split(':')[1].split()[0]))

    def test_no_cost_script_scales_a_prior_outside_price(self):
        for name in self.COST_SCRIPTS:
            with self.subTest(name):
                self.assertEqual(self.sites((SCRIPTS / name).read_text(), name), [])

    def test_the_check_names_a_scaled_prior(self):
        source = 'def assign(f, r):\n    return f["prior_secs"] * r\n\ndef g(p):\n    return p * PRIOR_ERROR\n'
        self.assertEqual(self.sites(source, 'x.py'), ['x.py:2 in assign', 'x.py:5 in g'])

    def test_the_check_follows_a_prior_and_a_measure_through_locals(self):
        # The cross-file calibration of an earlier project(), and an earlier weights()' cross-range one.
        source = """def project(units, hist):
    measured = sum(hist[p]['secs'] for p in hist)
    prior = sum(units[p][1] for p in hist)
    ratio = measured / prior
    return {p: v * ratio for p, v in units.items()}

def weights(spans, rows):
    prior = [s[2] for s in spans]
    rates = [r['secs'] / p for r, p in zip(rows, prior)]
    return [p * rates[0] for p in prior]

def fine(secs, workers):
    return secs / workers
"""
        self.assertEqual(
            self.sites(source, 'x.py'),
            ['x.py:4 in project', 'x.py:5 in project', 'x.py:9 in weights', 'x.py:10 in weights'],
        )


class PriorError(unittest.TestCase):
    def test_the_measure_is_the_largest_shard_sum_of_prior_priced_work_against_its_prior(self):
        rows = [(1, 30.0, 10.0), (1, 2.0, 10.0), (2, 9.0, 1.0), (2, 1.0, 9.0), (3, 5.0, 0.0)]
        # Shard 1 took 32 of 20, shard 2 10 of 10: a small unit's 9x moves only its shard.
        self.assertEqual(mutation_cost.prior_error(rows), 1.6)
        self.assertIsNone(mutation_cost.prior_error([]))
        self.assertIsNone(mutation_cost.prior_error([(1, 5.0, 0.0)]))

    def test_a_measure_past_the_constant_warns_and_any_measure_is_summarized(self):
        for measured, warned in (
            (None, False),
            (1.0, False),
            (mutation_cost.PRIOR_ERROR + 0.1, True),
        ):
            with (
                self.subTest(measured=measured),
                unittest.mock.patch.object(mutation_cost, 'annotate') as annotate,
                unittest.mock.patch.object(mutation_cost, 'summarize') as summarize,
                unittest.mock.patch.object(mutation_cost, 'say'),
            ):
                mutation_cost.note_prior_error('r', measured)
                self.assertEqual(annotate.called, warned)
                self.assertEqual(summarize.call_count, int(measured is not None))

    def test_a_record_s_prior_error_is_a_ratio_or_none(self):
        base = {'files': {}, 'setup_secs': 1.0}
        self.assertEqual(mutation_cost.record_fault({**base, 'prior_error': 2.5}), '')
        self.assertEqual(mutation_cost.record_fault({**base, 'prior_error': None}), '')
        self.assertIn('prior_error', mutation_cost.record_fault({**base, 'prior_error': -1}))
        self.assertIn('prior_error', mutation_cost.record_fault({**base, 'prior_error': 'x'}))


class MatrixBound(unittest.TestCase):
    def test_one_call_s_run_matrix_fits_one_github_matrix(self):
        self.assertLessEqual(
            mutation_cost.MAX_SHARDS * mutation_cost.MAX_SHARDS, mutation_cost.MATRIX_LIMIT
        )
        self.assertLessEqual(3 * mutation_cost.MAX_SHARDS, mutation_cost.MATRIX_LIMIT)


class CarryForward(unittest.TestCase):
    def test_a_file_measured_now_takes_its_new_cost_and_one_not_measured_keeps_its_last(self):
        new = {'files': {'a': {'secs': 5.0, 'mutants': 1}}, 'setup_secs': None}
        old = {
            'files': {
                'a': {'secs': 9.0, 'mutants': 1},
                'b': {'secs': 3.0, 'mutants': 2},
                'gone': {'secs': 1.0, 'mutants': 1},
            },
            'setup_secs': 80,
        }
        got = mutation_cost.carry_forward(new, old, keep={'a', 'b'})
        self.assertEqual(
            got,
            {
                'files': {'a': {'secs': 5.0, 'mutants': 1}, 'b': {'secs': 3.0, 'mutants': 2}},
                'setup_secs': 80.0,
            },
        )
        self.assertIn('gone', mutation_cost.carry_forward(new, old)['files'])

    def test_an_untrusted_old_history_adds_nothing(self):
        new = {'files': {'a': {'secs': 5.0, 'mutants': 1}}, 'setup_secs': 60.0}
        for old in (None, [], {'files': [1], 'setup_secs': -1}):
            self.assertEqual(mutation_cost.carry_forward(new, old), new)

    def test_a_sample_short_of_its_planned_mutants_replaces_only_a_measure_of_fewer(self):
        new = {
            'files': {
                'cut': {'secs': 2.0, 'mutants': 4},
                'new': {'secs': 1.0, 'mutants': 1},
                'half': {'secs': 6.0, 'mutants': 5},
                'whole': {'secs': 7.0, 'mutants': 10},
            },
            'setup_secs': 60.0,
        }
        old = {
            'files': {'cut': {'secs': 90.0, 'mutants': 10}, 'half': {'secs': 9.0, 'mutants': 10}}
        }
        planned = {'cut': 10, 'new': 10, 'half': 10, 'whole': 10}
        got = mutation_cost.carry_forward(new, old, planned=planned)['files']
        self.assertEqual(
            got,
            {
                'cut': {'secs': 90.0, 'mutants': 10},
                'half': {'secs': 9.0, 'mutants': 10},
                'new': {'secs': 1.0, 'mutants': 1},
                'whole': {'secs': 7.0, 'mutants': 10},
            },
        )
        old['files']['cut']['mutants'] = 3
        got = mutation_cost.carry_forward(new, old, planned=planned)['files']
        self.assertEqual(got['cut'], {'secs': 2.0, 'mutants': 4})


FLEET = json.loads((SCRIPTS / 'testdata' / 'mutation-cost' / 'fleet.json').read_text())
# Layouts whose keys collided before entry_key, each pair a distinct entry.
ADVERSARIAL = [
    ('a-b', 'c'),
    ('a', 'b-c'),
    ('a', 'b/c'),
    ('a', 'b_c'),
    ('a_b', 'c'),
    ('r', '.'),
    ('r', 'root'),
    ('deadset', 'ts-x'),
    ('deadset-ts', 'x'),
    ('a', 'b/c/d'),
    ('a', 'b=c/d'),
    ('a~b', 'c'),
    ('x-1', '.'),
    ('x', '1'),
    ('x', '1/2'),
    ('x-1', '2'),
    ('.hidden', '.hidden/.x'),
]


class EntryKey(unittest.TestCase):
    def entries(self) -> list[tuple[str, str | None]]:
        return (
            [(e['repo'], e['dir']) for e in FLEET['stryker']]
            + [(r, None) for r in FLEET['gremlins']]
            + ADVERSARIAL
            + [(r, None) for r, _ in ADVERSARIAL]
        )

    def test_every_pair_of_distinct_entries_has_distinct_keys_and_artifact_names(self):
        keys: dict[str, tuple] = {}
        names: dict[str, tuple] = {}
        for repo, dir_ in dict.fromkeys(self.entries()):
            try:
                key = mutation_cost.entry_key(repo, dir_)
            except ValueError:
                continue
            with self.subTest(entry=(repo, dir_)):
                self.assertNotIn(key, keys, f'{(repo, dir_)} and {keys.get(key)} share {key}')
                self.assertRegex(key, r'^[A-Za-z0-9._~=-]+$')
                keys[key] = (repo, dir_)
                own = [
                    f'{p}-{key}'
                    for p in (
                        'strykerplan',
                        'strykercosts',
                        'stryker',
                        'gremlinscosts',
                        'shard-plan',
                    )
                ]
                own += [
                    f'{p}-{key}-{n}'
                    for p in ('strykershard', 'strykersamples')
                    for n in range(1, mutation_cost.MAX_SHARDS + 1)
                ]
                for name in own:
                    self.assertNotIn(
                        name, names, f'{(repo, dir_)} and {names.get(name)} share {name}'
                    )
                    names[name] = (repo, dir_)
        self.assertGreaterEqual(len(keys), len(FLEET['stryker']) + len(FLEET['gremlins']))

    def test_a_name_outside_the_key_s_characters_is_refused(self):
        for repo, dir_ in (
            ('a~b', 'c'),
            ('a', 'b=c/d'),
            ('a', '../x'),
            ('a', 'b//c'),
            ('a', 'b/'),
            ('a', ''),
            ('a', 'b*'),
            ('a', '{x,y}'),
            ('a b', '.'),
            ('', '.'),
            ('.', None),
            ('a', 'b/./c'),
        ):
            with self.subTest(repo=repo, dir=dir_), self.assertRaises(ValueError):
                mutation_cost.entry_key(repo, dir_)

    def test_the_fleet_s_keys(self):
        self.assertEqual(mutation_cost.entry_key('go', 'keyenc/web'), 'go~keyenc=web')
        self.assertEqual(mutation_cost.entry_key('ui', '.'), 'ui~')
        self.assertEqual(mutation_cost.entry_key('deadset-go'), 'deadset-go')


GH_STUB = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
mode = os.environ.get('STUB_MODE', '')
if 'artifacts?name=' in ' '.join(args):
    if mode == 'list-fails':
        sys.exit(1)
    for a in json.loads(os.environ['STUB_ARTIFACTS']):
        sys.stdout.write(json.dumps(a) + '\n')
    sys.exit(0)
if args[-1].endswith('/zip'):
    if mode == 'zip-fails':
        sys.exit(1)
    sys.stdout.buffer.write(open(os.environ['STUB_ZIP'], 'rb').read())
    sys.exit(0)
sys.exit(2)
"""


def artifact(
    id_: int, branch: str, created: str, *, expired: bool = False, head_repo: int | None = 7
) -> dict:
    return {
        'id': id_,
        'expired': expired,
        'created_at': created,
        'workflow_run': {
            'id': id_ * 10,
            'head_branch': branch,
            'repository_id': 7,
            'head_repository_id': head_repo,
        },
    }


class HistoryFetch(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        bin_ = self.tmp / 'bin'
        bin_.mkdir()
        (bin_ / 'gh').write_text(GH_STUB)
        (bin_ / 'gh').chmod(0o755)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('costs.json', '{"modules": {}}')
        (self.tmp / 'a.zip').write_bytes(buf.getvalue())
        self.env = {
            **os.environ,
            'PATH': f'{bin_}:{os.environ["PATH"]}',
            'GITHUB_REPOSITORY': 'o/ci',
            'STUB_LOG': str(self.tmp / 'gh.log'),
            'STUB_ZIP': str(self.tmp / 'a.zip'),
            'READBACK_SLEEP': '0',
        }

    def fetch(
        self, artifacts: list[dict], mode: str = ''
    ) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        out = self.tmp / 'out' / 'costs.json'
        out.unlink(missing_ok=True)
        proc = subprocess.run(
            [
                'bash',
                str(SCRIPTS / 'mutation-history.sh'),
                'gremlinscosts-x',
                'main',
                str(out.parent),
                'costs.json',
            ],
            env={**self.env, 'STUB_ARTIFACTS': json.dumps(artifacts), 'STUB_MODE': mode},
            capture_output=True,
            text=True,
            check=False,
        )
        return proc, out

    def calls(self) -> list[list[str]]:
        log = self.tmp / 'gh.log'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_takes_the_newest_unexpired_artifact_of_the_runs_own_branch(self):
        proc, out = self.fetch(
            [
                artifact(1, 'main', '2026-11-01T00:00:00Z'),
                artifact(2, 'feat/x', '2026-11-09T00:00:00Z'),
                artifact(3, 'main', '2026-11-08T00:00:00Z'),
                artifact(4, 'main', '2026-11-10T00:00:00Z', expired=True),
            ]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out.read_text(), '{"modules": {}}')
        self.assertEqual(self.calls()[-1], ['api', 'repos/o/ci/actions/artifacts/3/zip'])
        self.assertIn('artifact 3', proc.stdout)

    def test_none_on_this_branch_leaves_no_history(self):
        proc, out = self.fetch([artifact(2, 'feat/x', '2026-11-09T00:00:00Z')])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(out.exists())
        self.assertTrue(all(not c[-1].endswith('/zip') for c in self.calls()))

    def test_a_failed_lookup_or_download_warns_and_leaves_no_history(self):
        for mode in ('list-fails', 'zip-fails'):
            with self.subTest(mode=mode):
                proc, out = self.fetch([artifact(3, 'main', '2026-11-08T00:00:00Z')], mode)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(out.exists())
                self.assertIn('::warning::gremlinscosts-x', proc.stdout)

    def test_an_artifact_a_fork_s_run_uploaded_is_not_history(self):
        proc, out = self.fetch(
            [
                artifact(3, 'main', '2026-11-08T00:00:00Z'),
                artifact(5, 'main', '2026-11-10T00:00:00Z', head_repo=99),
                artifact(6, 'main', '2026-11-11T00:00:00Z', head_repo=None),
            ]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out.read_text(), '{"modules": {}}')
        self.assertEqual(self.calls()[-1], ['api', 'repos/o/ci/actions/artifacts/3/zip'])

    def test_an_artifact_without_the_file_leaves_no_history(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('other.json', '{}')
        (self.tmp / 'a.zip').write_bytes(buf.getvalue())
        proc, out = self.fetch([artifact(3, 'main', '2026-11-08T00:00:00Z')])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(out.exists())
        self.assertIn('has no readable costs.json', proc.stdout)


if __name__ == '__main__':
    unittest.main()
