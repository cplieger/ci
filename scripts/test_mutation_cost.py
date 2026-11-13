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
        got = mutation_cost.attribute(
            [(12.0, 'a.go', '1:1 X'), (13.0, 'a.go', '2:1 X'), (17.0, 'b.go', '1:1 X')], 10.0, 4
        )
        self.assertEqual(got, {'a.go': {'1:1 X': [8.0], '2:1 X': [4.0]}, 'b.go': {'1:1 X': [16.0]}})

    def test_a_completion_stamped_out_of_order_costs_nothing(self):
        got = mutation_cost.attribute(
            [(14.0, 'a.go', 'k'), (12.0, 'b.go', 'k'), (15.0, 'b.go', 'k')], 10.0, 2
        )
        self.assertEqual(got, {'a.go': {'k': [8.0]}, 'b.go': {'k': [0.0, 2.0]}})

    def test_a_completion_before_the_start_costs_nothing(self):
        self.assertEqual(
            mutation_cost.attribute([(9.0, 'a.go', 'k')], 10.0, 4), {'a.go': {'k': [0.0]}}
        )


class History(unittest.TestCase):
    BLOB = 'a' * 40

    def test_only_a_value_that_is_no_measure_is_dropped(self):
        secs = {
            'ok': [1, 2.5],
            'mixed': [1.0, -1, float('nan'), True, '1', 10**400, 1e300],
            'none': [-1],
            'list': 'x',
            7: [1.0],
        }
        self.assertEqual(mutation_cost.clean_secs(secs), {'ok': [1.0, 2.5], 'mixed': [1.0, 1e300]})
        self.assertEqual(mutation_cost.clean_secs(['a']), {})

    def test_an_implausibly_high_measure_is_kept(self):
        # A measure is never priced below itself, so only garbage is refused.
        files = {'a': {'blob': self.BLOB, 'secs': {'k': [1e9]}}}
        self.assertEqual(mutation_cost.at_blobs(files, {'a': self.BLOB}), {'a': {'k': [1e9]}})

    def test_a_file_s_measures_count_only_at_its_current_blob(self):
        files = {
            'a': {'blob': self.BLOB, 'secs': {'k': [1.0]}},
            'b': {'blob': self.BLOB, 'secs': {'k': [2.0]}},
        }
        got = mutation_cost.at_blobs(files, {'a': self.BLOB, 'b': 'b' * 40})
        self.assertEqual(got, {'a': {'k': [1.0]}})
        self.assertEqual(mutation_cost.at_blobs(None, {}), {})

    def test_a_record_is_refused_only_for_its_shape(self):
        ok = {'files': {'a': {'blob': self.BLOB, 'secs': {'k': [1.0, -5]}}}, 'setup_secs': 1.0}
        self.assertEqual(mutation_cost.record_fault(ok), '')
        for bad in (
            [],
            {'files': {}, 'extra': 1},
            {'files': {'a': {'secs': 1.0, 'mutants': 1}}},
            {'files': {'a': {'blob': 'x', 'secs': {}}}},
            {'files': {'a': {'blob': self.BLOB, 'secs': {'k': 1.0}}}},
            {'files': {}, 'setup_secs': -1},
        ):
            with self.subTest(bad=bad):
                self.assertNotEqual(mutation_cost.record_fault(bad), '')

    def test_merged_takes_new_measures_first_and_keeps_the_last_of_the_rest(self):
        new = {'a': [5.0], 'b': [1.0, 2.0]}
        old = {'a': [9.0, 8.0], 'c': [3.0]}
        self.assertEqual(
            mutation_cost.merged(new, old), {'a': [5.0, 8.0], 'b': [1.0, 2.0], 'c': [3.0]}
        )

    def test_attempts_average_per_mutant_over_those_that_measured_it(self):
        # Two whole attempts and one a deadline cut after one mutant.
        whole = {'m1': [10.0], 'm2': [20.0]}
        got = mutation_cost.attempt_mean([whole, {'m1': [30.0], 'm2': [40.0]}, {'m1': [5.0]}])
        self.assertEqual(got, {'m1': [15.0], 'm2': [30.0]})
        self.assertEqual(
            mutation_cost.attempt_mean([{'k': [1.0, 3.0]}, {'k': [3.0]}]), {'k': [2.0, 3.0]}
        )

    def test_setup_is_a_measure_or_the_probe(self):
        for measured, want in (
            (80.0, 80.0),
            (6000.0, 6000.0),
            (0.0, 0.0),
            (None, 60.0),
            (float('nan'), 60.0),
            (10**400, 60.0),
            (True, 60.0),
        ):
            with self.subTest(measured=measured):
                self.assertEqual(mutation_cost.setup_price(measured, 60.0), want)
        self.assertEqual(mutation_cost.mean_of([10, None, 20.0, -1]), 15.0)
        self.assertIsNone(mutation_cost.mean_of([None, 'x']))


class Price(unittest.TestCase):
    E = mutation_cost.PRIOR_ERROR

    def test_a_unit_costs_its_measured_mutants_and_the_rest_at_the_error(self):
        got = mutation_cost.price([(10.0, 4.0), (10.0, None), (20.0, 9.0)])
        self.assertEqual(got, (13.0 + 10.0 * self.E, 13.0, 'partial', 2))
        self.assertEqual(mutation_cost.price([(10.0, 4.0)]).source, 'measured')
        self.assertEqual(mutation_cost.price([(10.0, None)]), (10.0 * self.E, 0.0, 'prior', None))
        self.assertEqual(mutation_cost.price([]), (0.0, 0.0, 'measured', None))

    def test_a_cut_sample_is_never_priced_below_what_it_measured(self):
        # 10 of 500 mutants measured 48000 s against a 2-s prior each.
        mutants = [(2.0, 4800.0)] * 10 + [(2.0, None)] * 490
        got = mutation_cost.price(mutants)
        self.assertEqual(
            (got.secs, got.measured, got.source), (48000 + 490 * 2.0 * self.E, 48000, 'partial')
        )
        # A cheap measure prices what it took, as does a dear one.
        self.assertEqual(mutation_cost.price([(1000.0, 0.5)]).secs, 0.5)

    def test_an_unmeasured_mutant_of_no_prior_is_unknown_never_zero(self):
        self.assertEqual(mutation_cost.price([(None, None), (1.0, 2.0)]).source, 'unknown')
        self.assertIsNone(mutation_cost.price([(None, None)]).secs)
        self.assertEqual(mutation_cost.price([(None, 2700.0)]), (2700.0, 2700.0, 'measured', 0))

    def test_pair_gives_each_planned_mutant_its_own_measure(self):
        got = mutation_cost.pair(
            ['k', 'k', 'j', 'gone'], [1.0, 1.0, 2.0, 3.0], {'k': [5.0], 'j': [6.0, 7.0], 'x': [9.0]}
        )
        self.assertEqual(got, [(1.0, 5.0), (1.0, None), (2.0, 6.0), (3.0, None)])

    def test_a_probe_under_its_clock_is_unknown_never_zero(self):
        for probe, clock, want in (
            (0.0, 1.0, None),
            (0.5, 1.0, None),
            (1.0, 1.0, 1.0),
            (7.0, 1.0, 7.0),
        ):
            with self.subTest(probe=probe):
                self.assertEqual(mutation_cost.known(probe, clock), want)

    def test_overflowing_prices_never_raise(self):
        got = mutation_cost.price([(1e308, 1e308), (1e308, None), (1e308, None)])
        self.assertEqual(got.secs, float('inf'))
        self.assertEqual(
            mutation_cost.totals({'a': {'k': [1.0, 2.0]}, 'b': {'j': [3.0]}}), (6.0, 3)
        )


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
        self.assertFalse(mutation_cost.capped(k, bins, items, 115, 4))
        k, bins = mutation_cost.choose(items, {'m': 0}, 115, 3)
        self.assertEqual(sorted(b.load for b in bins), [150, 300, 500])
        self.assertTrue(mutation_cost.capped(k, bins, items, 115, 3))

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
                target = mc.BOUND_SECS - setup
                self.assertAlmostEqual(target, 300 * 60 / 1.9 - setup)
                # The runner slows the setup too (1.46x in run 43153682226), so the whole job scales.
                self.assertAlmostEqual(mc.SLOWDOWN * (setup + target), mc.SAFE_JOB_MINUTES * 60)
                # A shard at the top whose mean runs BAND over still ends in time on the slowest runner.
                self.assertLessEqual(
                    mc.SLOWDOWN * (1 + mc.BAND) * mc.TOP_SECS, mc.SAFE_JOB_MINUTES * 60 + 1e-6
                )
                self.assertAlmostEqual(mc.TOP_SECS * (1 + mc.BAND), setup + target)
        self.assertAlmostEqual((mc.BOUND_SECS - 180) / 60, 154.9, places=1)
        self.assertAlmostEqual((mc.BOUND_SECS - 1872) / 60, 126.7, places=1)
        self.assertAlmostEqual(mc.TOP_SECS / 60, 137.3, places=1)

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
        self.assertTrue(mutation_cost.capped(k, bins, items, 230 * 60, limit=4))
        k, bins = mutation_cost.choose(items, {'m': 0}, 230 * 60)
        self.assertFalse(
            mutation_cost.capped(k, bins, items, 230 * 60, limit=mutation_cost.MAX_SHARDS)
        )
        lone = self.items(500 * 60, 10, 10)
        k, bins = mutation_cost.choose(lone, {'m': 0}, 230 * 60, limit=2)
        self.assertFalse(mutation_cost.capped(k, bins, lone, 230 * 60, limit=2))


class Bound(unittest.TestCase):
    """fit: no shard projects past BOUND_SECS but one its job's deadline bounds; else the plan fails."""

    B = mutation_cost.BOUND_SECS

    @staticmethod
    def items(*work: float, known: float | None = None) -> list:
        return [mutation_cost.Item('m', f'f{i}', w, known) for i, w in enumerate(work)]

    def test_the_bound_holds_the_slowest_runner_to_the_safe_limit(self):
        mc = mutation_cost
        self.assertAlmostEqual(mc.SLOWDOWN * mc.BOUND_SECS, mc.SAFE_JOB_MINUTES * 60)

    def test_a_count_past_the_limit_is_taken_where_the_bound_needs_it(self):
        items = self.items(*[self.B * 0.6] * 40)
        k, bins = mutation_cost.choose(items, {'m': 0}, mutation_cost.TOP_SECS)
        self.assertTrue(mutation_cost.breaches(bins, {'m': 0}))
        k, bins = mutation_cost.fit(items, {'m': 0}, mutation_cost.TOP_SECS)
        self.assertEqual((k, mutation_cost.breaches(bins, {'m': 0})), (40, []))
        self.assertEqual([len(b.items) for b in bins], [1] * 40)
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

    def test_fit_never_plans_more_shards_than_units(self):
        # Prior-only units past the bound, each one the deadline's alone.
        for n, size, want in ((18, 1.2, 18), (20, 4.0, 20), (100, 3.0, 100)):
            with self.subTest(n=n, size=size):
                items = self.items(*[self.B * size] * n, known=0.0)
                k, bins = mutation_cost.fit(items, {'m': 60}, mutation_cost.TOP_SECS)
                self.assertEqual(k, want)
                self.assertTrue(all(b.items for b in bins))
                self.assertEqual(mutation_cost.breaches(bins, {'m': 60}), [])

    def test_exempt_units_are_counted_out_of_the_lower_bound(self):
        items = [*self.items(*[self.B * 2] * 3, known=0.0), *self.items(*[self.B * 0.5] * 4)]
        # With one shard allowed in the band the search starts at 3 alone + 2 for the rest.
        k, bins = mutation_cost.fit(items, {'m': 0}, mutation_cost.TOP_SECS, limit=1)
        self.assertEqual(k, 5)
        self.assertTrue(all(b.items for b in bins))
        self.assertEqual(mutation_cost.breaches(bins, {'m': 0}), [])

    def test_a_measured_mutant_past_the_bound_fails_the_plan_naming_it(self):
        # A cut sample measured 48000 s, so its unit cannot share a shard or fit one.
        priced = mutation_cost.price([(2.0, 4800.0)] * 10 + [(2.0, None)] * 490)
        item = mutation_cost.Item(
            'm', 'a.ts', priced.secs / 4, priced.measured / 4, 'a.ts 3:1-3:9 X'
        )
        with self.assertRaises(mutation_cost.UnsafePlanError) as ctx:
            mutation_cost.fit([item, *self.items(10.0)], {'m': 60}, mutation_cost.TOP_SECS)
        self.assertIn('a.ts alone projects', str(ctx.exception))
        self.assertIn('its costliest measured mutant a.ts 3:1-3:9 X', str(ctx.exception))


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
    """mutation_cost.price is the one place a unit's price is set: nothing else scales a prior or
    does arithmetic on a cost history's measures."""

    COST_SCRIPTS = ('mutation_cost.py', 'stryker-shard.py', 'gremlins-shard.py')
    # The functions that read measures by right: price and pair set prices, the rest record,
    # count or ratio them.
    OWNERS = frozenset(
        {'price', 'pair', 'attribute', 'attempt_mean', 'merged', 'totals', 'mean_of', 'prior_error'}
    )
    # Calls that hand out a cost history's measures, and parameters that hold them.
    SOURCES = frozenset(
        {
            'at_blobs',
            'clean_secs',
            'clean_files',
            'unit_measures',
            'attempt_mean',
            'merged',
            'pair',
            'span_pairs',
        }
    )
    HELD = frozenset({'measured', 'paired', 'history'})
    # Calls whose value is a price or a count, no longer a measure.
    PRICES = frozenset(
        {
            'price',
            'priced_unit',
            'integration_price',
            'setup_price',
            'totals',
            'prior_rows',
            'prior_error',
            'mean_of',
            'len',
        }
    )
    ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)

    @staticmethod
    def called(node: ast.AST) -> str | None:
        f = node.func if isinstance(node, ast.Call) else None
        return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None

    def reads(self, node: ast.AST, names: set[str]) -> bool:
        """NODE reads a measure: a source call, or a tainted name, outside a price call."""
        if isinstance(node, ast.Call) and self.called(node) in self.PRICES:
            return False
        if isinstance(node, ast.Call) and self.called(node) in self.SOURCES:
            return True
        if isinstance(node, ast.Name) and node.id in names:
            return True
        # A comprehension's value is its element; its loops only bind the names tainted() follows.
        if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
            return self.reads(node.elt, names)
        if isinstance(node, ast.DictComp):
            return self.reads(node.key, names) or self.reads(node.value, names)
        return any(self.reads(c, names) for c in ast.iter_child_nodes(node))

    def tainted(self, fn: ast.FunctionDef) -> set[str]:
        """FN's local names that hold a measure, through assignments, loops and comprehensions."""
        names = {a.arg for a in fn.args.args if a.arg in self.HELD}
        flows = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                flows += [(t, n.value) for t in n.targets]
            elif isinstance(n, ast.AnnAssign | ast.NamedExpr) and n.value:
                flows.append((n.target, n.value))
            elif isinstance(n, ast.For | ast.comprehension):
                flows.append((n.target, n.iter))
        changed = True
        while changed:
            changed = False
            for target, value in flows:
                if not self.reads(value, names):
                    continue
                # Only the names a value is bound to, never a subscript's index.
                bound = (
                    [target]
                    if isinstance(target, ast.Name)
                    else (
                        [e for e in ast.walk(target) if isinstance(e, ast.Name)]
                        if isinstance(target, ast.Tuple | ast.List)
                        else []
                    )
                )
                for t in bound:
                    if t.id not in names:
                        names.add(t.id)
                        changed = True
        return names

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

        tree = ast.parse(source)
        visit(tree, '<module>')
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name in self.OWNERS:
                continue
            names = self.tainted(fn)
            for n in ast.walk(fn):
                if isinstance(n, ast.BinOp) and isinstance(n.op, self.ARITHMETIC):
                    hit = self.reads(n.left, names) or self.reads(n.right, names)
                elif isinstance(n, ast.AugAssign) and isinstance(n.op, self.ARITHMETIC):
                    hit = self.reads(n.value, names) or self.reads(n.target, names)
                elif self.called(n) in ('sum', 'max', 'min', 'round'):
                    hit = any(self.reads(a, names) for a in n.args)
                else:
                    continue
                if hit:
                    out.append(f'{name}:{n.lineno} in {fn.name}')
        return sorted(set(out), key=lambda x: int(x.split(':')[1].split()[0]))

    def test_no_cost_script_prices_outside_price(self):
        for name in self.COST_SCRIPTS:
            with self.subTest(name):
                self.assertEqual(self.sites((SCRIPTS / name).read_text(), name), [])

    def test_the_check_names_a_scaled_prior(self):
        source = 'def assign(f, r):\n    return f["prior_secs"] * r\n\ndef g(p):\n    return p * PRIOR_ERROR\n'
        self.assertEqual(self.sites(source, 'x.py'), ['x.py:2 in assign', 'x.py:5 in g'])

    def test_the_check_follows_a_history_measure_through_locals(self):
        # A per-unit row scaled, smeared or combined into a price, and a function that prices right.
        source = """def span_prices(f, history):
    rows = mutation_cost.at_blobs(history['files'], {})
    total = sum(v for vs in rows['a'].values() for v in vs)
    return [total * w for w in f]

def combine(records):
    acc = [mutation_cost.clean_secs(r) for r in records]
    return {k: acc[0][k][0] / len(acc) for k in acc[0]}

def fine(measured, workers):
    priced = mutation_cost.price(mutation_cost.pair(['k'], [1.0], measured))
    n = len(measured)
    return priced.secs / workers + n
"""
        self.assertEqual(
            self.sites(source, 'x.py'),
            ['x.py:3 in span_prices', 'x.py:4 in span_prices', 'x.py:8 in combine'],
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
