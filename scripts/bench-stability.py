#!/usr/bin/env python3
"""Measure per-series benchmark stability, to decide enrolment on evidence.

Why this exists
---------------
`weekly-bench.yaml` alerts at a fixed 150% threshold. That number is only
meaningful against the NOISE FLOOR of each series, and the floor is a property
of the individual benchmark, not of the repo it lives in. Measured across four
cplieger repos (38 ns/op series, `-count=10` on one host, 2026-08-21):

    allocs/op   coefficient of variation 0.00% on 38 of 38 series
    B/op        0.00%, constant to within 1-5 bytes on the two that move
    ns/op       median 8.6%, p90 24.6%, worst 43.7%

A zero here means "the same count on every sample", which is not the same as
"never allocates": testing.AllocsPerRun and BenchmarkResult.AllocsPerOp divide as
INTEGERS, so an allocation occurring less often than once per run floors to zero.
An amortised path that grows a buffer occasionally can therefore read 0.

So the allocation metrics are stable where ns/op is not, and ns/op can only
carry a trend.

TWO LIMITS ON WHAT THIS SCRIPT CAN TELL YOU, both found by adversarial review
after it shipped, and neither yet closed.

First, the statistic is not the one that decides an alert. This reports the
coefficient of variation over the RAW samples. The workflow publishes their
MEDIAN, and the action alerts on a ratio between two medians a week apart. Those
are different random variables, so the sigma figure below is a description of
within-run spread and NOT the sampling distribution of the decision. It is
useful for comparing series and for seeing benchtime work; it is not an
admission test, and the exit status is advisory rather than a gate. The durable
statistic is the empirical distribution of consecutive-median ratios over an
unchanged tree, which needs repeated runs on the real runner to obtain.

Second, a coefficient of variation assumes the spread is worth summarising by a
standard deviation. Hosted-runner noise is spiky rather than gaussian, and NIST
recommends a median absolute deviation or an interquartile range for
long-tailed data (itl.nist.gov/div898/handbook/eda/section3/eda356.htm). MAD is
reported alongside CoV for that reason; where the two disagree sharply, believe
MAD and distrust any sigma derived from the standard deviation.

The published literature says the same thing and says not to guess it: across
5 million data points in Java and Go, Laaber, Scheuner and Leitner measured
per-benchmark coefficients of variation from 0.03% to over 100%
(https://doi.org/10.1007/s10664-019-09681-1), and Laaber and Leitner found
suites containing benchmarks at 50% or higher, concluding that not all
benchmarks are useful for reliably discovering slowdowns. Bencher reports
GitHub-hosted runners exceeding 30% variance against under 2% on bare metal
(https://bencher.dev/docs/explanation/continuous-benchmarking/).

Usage
-----
    go test -run='^$' -bench=. -benchmem -count=10 ./... > bench.txt
    python3 bench-stability.py bench.txt              # one repo
    python3 bench-stability.py *.txt --threshold 150  # several

Exit status is 0 for a report and 1 when any series is too noisy for the
threshold, so this can gate an enrolment change rather than only inform one.
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
import sys

# Same shape bench-reduce.py parses, and the same GOMAXPROCS-suffix strip, so a
# series named here is the series that reaches the chart.
LINE = re.compile(r'^(Benchmark\S*)\s+(\d+)\s+(.*)$')
METRIC = re.compile(r'([0-9.]+)\s+(ns/op|B/op|allocs/op)')

# Below this many samples a coefficient of variation is not worth reporting.
MIN_SAMPLES = 3

# A threshold closer to the noise than this many standard deviations will fire
# on noise. Three sigma is the conventional floor for calling a move real.
MIN_SIGMA = 3.0


def parse(text: str) -> dict[tuple[str, str], list[float]]:
    """Collect every sample for each (benchmark, unit) pair."""
    samples: dict[tuple[str, str], list[float]] = {}
    for raw in text.splitlines():
        m = LINE.match(raw.strip())
        if not m:
            continue
        name = re.sub(r'-\d+$', '', m.group(1))
        for value, unit in METRIC.findall(m.group(3)):
            samples.setdefault((name, unit), []).append(float(value))
    return samples


def mad_pct(values: list[float]) -> float:
    """Median absolute deviation, as a percentage of the median.

    The robust companion to cov(). A standard deviation weights an outlier
    quadratically, so one scheduling spike in ten samples moves CoV a long way;
    MAD does not. NIST recommends it for long-tailed data. When MAD is small and
    CoV is large, the series is quiet with occasional spikes, which is the shape
    hosted-runner noise actually takes, and the CoV-derived sigma figure is then
    pessimistic rather than wrong.
    """
    med = statistics.median(values)
    if med == 0:
        return 0.0
    deviations = [abs(v - med) for v in values]
    return statistics.median(deviations) / med * 100


def cov(values: list[float]) -> float:
    """Coefficient of variation as a percentage.

    A series whose mean is zero is a genuine constant (an allocation-free path),
    which has no spread to express as a ratio; report 0 rather than dividing.
    """
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values) / mean * 100


def sigma_to_threshold(cov_pct: float, threshold_pct: float) -> float:
    """How many standard deviations the alert threshold sits from the mean.

    threshold_pct is the action's own alert-threshold percentage: 150 means the
    alert fires at 1.5x, so the move it must clear is 50% of the mean.
    """
    if cov_pct == 0:
        return math.inf
    return (threshold_pct - 100) / cov_pct


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('inputs', nargs='+', help='files of `go test -bench -count=N` output')
    ap.add_argument(
        '--threshold',
        type=float,
        default=150.0,
        help="the workflow's alert-threshold percent (default 150)",
    )
    ap.add_argument('--quiet', action='store_true', help='summary only, no per-series table')
    args = ap.parse_args()

    per_unit: dict[str, list[float]] = {}
    per_unit_mad: dict[str, list[float]] = {}
    rows: list[tuple[float, str, str, str, int, float]] = []
    short: dict[str, int] = {}

    for path in args.inputs:
        label = path.rsplit('/', 1)[-1].removesuffix('.txt')
        with open(path, encoding='utf-8') as fh:
            samples = parse(fh.read())
        if not samples:
            print(f'{path}: no Benchmark lines found', file=sys.stderr)
            return 1
        for (name, unit), values in sorted(samples.items()):
            if len(values) < MIN_SAMPLES:
                # Keyed by benchmark, not by (benchmark, unit), or every name
                # repeats three times in the notice below.
                short[f'{label}/{name}'] = len(values)
                continue
            c = cov(values)
            per_unit.setdefault(unit, []).append(c)
            per_unit_mad.setdefault(unit, []).append(mad_pct(values))
            rows.append((c, label, name, unit, len(values), statistics.median(values)))

    if not rows:
        n = len(short)
        print(
            f'None of the {n} benchmark(s) had at least {MIN_SAMPLES} samples, so no '
            f'spread can be computed. Re-run with -count={MIN_SAMPLES} or higher '
            f'(the tracker itself uses -count=10).',
            file=sys.stderr,
        )
        return 1

    if short and not args.quiet:
        print(
            f'Skipped {len(short)} benchmark(s) with fewer than {MIN_SAMPLES} samples '
            f'(re-run with a larger -count):'
        )
        for name, n in sorted(short.items())[:5]:
            print(f'  {name} ({n} sample(s))')
        if len(short) > 5:
            print(f'  ... and {len(short) - 5} more')
        print()

    print(
        f'{"unit":<11}{"series":>7}{"median":>9}{"p90":>9}{"worst":>9}'
        f'{"medMAD":>9}{"exact":>9}{"  verdict"}'
    )
    for unit in ('ns/op', 'B/op', 'allocs/op'):
        vals = sorted(per_unit.get(unit, []))
        if not vals:
            continue
        p90 = vals[min(int(len(vals) * 0.9), len(vals) - 1)]
        exact = sum(1 for v in vals if v == 0.0)
        med = statistics.median(vals)
        med_mad = statistics.median(per_unit_mad.get(unit, [0.0]))
        # "stable across samples" rather than "exact": a zero here means every
        # sample reported the same figure, which the integer division in
        # AllocsPerOp can produce for a path that allocates rarely. See the module
        # docstring.
        verdict = 'stable across samples' if max(vals) == 0.0 else 'trend-only'
        print(
            f'{unit:<11}{len(vals):>7}{med:>8.2f}%{p90:>8.2f}%{max(vals):>8.2f}%'
            f'{med_mad:>8.2f}%{f"{exact}/{len(vals)}":>9}  {verdict}'
        )

    ns = [c for c, _, _, u, _, _ in rows if u == 'ns/op']
    if ns:
        med = statistics.median(ns)
        s = sigma_to_threshold(med, args.threshold)
        print(
            f'\nAt the median ns/op spread of {med:.2f}%, a {args.threshold:.0f}% alert sits '
            f'{s:.1f} sigma from the WITHIN-RUN spread.'
        )
        print(
            '  Read that as a comparison between series, not as a pass mark. The alert '
            'compares\n  two medians a week apart, which is a different quantity; see the '
            'module docstring.'
        )
        print(
            f'  It can see a regression of about {med * MIN_SIGMA:.0f}% or worse '
            f'(3 sigma). Anything finer is inside the noise.'
        )

    noisy = sorted(
        (
            r
            for r in rows
            if r[3] == 'ns/op' and sigma_to_threshold(r[0], args.threshold) < MIN_SIGMA
        ),
        reverse=True,
    )
    if noisy:
        print(
            f'\nWIDEST relative to a {args.threshold:.0f}% threshold '
            f'(under {MIN_SIGMA:.0f} sigma of within-run spread). ADVISORY, not a '
            f'verdict:\nthese are the series most likely to alert without a code '
            f'change, ranked. Confirm\nagainst the MAD column above - a wide CoV '
            f'with a narrow MAD is spikes, not drift -\nand against several runs '
            f'before acting.'
        )
        for c, label, name, _unit, n, med in noisy:
            print(
                f'  {c:>6.1f}% spread, {sigma_to_threshold(c, args.threshold):.1f} sigma  '
                f'{label}/{name}  (median {med:,.0f} ns, {n} samples)'
            )
        print(
            '  Raise -benchtime before rewriting any of these. Spread falls as\n'
            '  1/sqrt(iterations per sample), so 3x the benchtime buys about a 1.7x\n'
            '  tighter series across the WHOLE suite at a linear wall-time cost.\n'
            "  Measured on two of the fleet's worst series: slogx/ParseLevel went\n"
            '  11.1% -> 4.6% -> 2.5% at 100ms/1s/3s, and xmlx oversized_token went\n'
            '  13.2% -> 7.5% at 1s/3s. Rewriting one benchmark fixes one series;\n'
            '  the flag fixes all of them.'
        )

    print(
        '\nRead this against how the samples were taken. A whole-suite run on a busy\n'
        'host inflates every number here: the two series above measured 43.7% and\n'
        '39.1% inside a loaded `-bench=.` sweep against 4.6% and 13.2% run alone at\n'
        'the same benchtime. Ambient load is not the code under test, so compare a\n'
        'series only against another run of the same shape.'
    )

    if not args.quiet:
        print('\nNoisiest ns/op series:')
        for c, label, name, _unit, _n, med in sorted(
            (r for r in rows if r[3] == 'ns/op'), reverse=True
        )[:10]:
            print(f'  {c:>6.1f}%  {label}/{name}  (median {med:,.0f} ns)')

    # Non-zero so a human running this deliberately gets a signal, and so a
    # future gate has something to key on. The workflow calls it with `|| true`
    # BY DESIGN: the statistic is not yet the one that decides an alert, so it
    # must not fail a leg that measured everything it was asked to measure.
    return 1 if noisy else 0


if __name__ == '__main__':
    sys.exit(main())
