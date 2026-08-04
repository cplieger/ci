#!/usr/bin/env python3
"""Reduce `go test -bench -count=N` output to one median entry per metric.

Why this exists
---------------
`benchmark-action/github-action-benchmark` performs NO aggregation. Its Go
extractor emits one entry per output line, and its alert comparator resolves the
baseline with a first-match-by-name lookup. Feeding it raw `-count=10` output
therefore compares a single arbitrary sample against a single arbitrary previous
sample, ten times over, which multiplies false-alert odds by ten and writes ten
duplicate-named points per benchmark into the stored series.

So the samples are reduced HERE, and the action is handed one point per
benchmark per metric via its `customSmallerIsBetter` tool. Medians rather than
means, matching what `benchstat` reports and resisting a single outlier sample
(hosted-runner noise is spiky, not gaussian).

Input:  `go test -run='^$' -bench=. -benchmem -count=N ./...` on stdin or a path.
Output: a JSON array of {name, unit, value} on stdout.

Metrics emitted per benchmark:
    <name>                  ns/op
    <name> - B/op           B/op        (only when -benchmem was used)
    <name> - allocs/op      allocs/op   (only when -benchmem was used)

The `<name> - <metric>` shape is the action's own convention for extra metrics,
so an allocation regression alerts independently of a time regression. That is
what makes the xmlx/jsonx allocation-free-preflight contracts gateable.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys

# A Go benchmark result line, e.g.:
#   BenchmarkPreflight/small-16    1000000    1043 ns/op    512 B/op    3 allocs/op
# Field order after the iteration count is not guaranteed, so metrics are parsed
# by unit rather than by position.
#
# MB/s is deliberately ABSENT. The whole output feeds one
# `customSmallerIsBetter` series, and throughput is the one go-test metric
# where smaller is WORSE: charting it there inverts every verdict (a
# throughput collapse renders as an improvement, a gain alerts as a
# regression). ns/op carries the same information with the correct sign, so
# `-benchtime`-normalised throughput is dropped rather than published wrong.
LINE = re.compile(r'^(Benchmark\S*)\s+(\d+)\s+(.*)$')
METRIC = re.compile(r'([0-9.]+)\s+(ns/op|B/op|allocs/op)')


def parse(text: str) -> dict[tuple[str, str], list[float]]:
    """Collect every sample for each (benchmark, unit) pair."""
    samples: dict[tuple[str, str], list[float]] = {}
    for raw in text.splitlines():
        m = LINE.match(raw.strip())
        if not m:
            continue
        name, _iters, rest = m.groups()
        # Strip the trailing -N parallelism suffix so a runner with a different
        # GOMAXPROCS does not fork the series into two names.
        name = re.sub(r'-\d+$', '', name)
        for value, unit in METRIC.findall(rest):
            samples.setdefault((name, unit), []).append(float(value))
    return samples


def reduce_samples(
    samples: dict[tuple[str, str], list[float]],
) -> list[dict[str, object]]:
    """Median per (benchmark, unit), in the action's custom-tool schema."""
    out: list[dict[str, object]] = []
    for (name, unit), values in sorted(samples.items()):
        label = name if unit == 'ns/op' else f'{name} - {unit}'
        median = statistics.median(values)
        entry: dict[str, object] = {
            'name': label,
            'unit': unit,
            'value': round(median, 4),
        }
        # `range` renders as an error bar in the action's chart. Only meaningful
        # with more than one sample.
        if len(values) > 1:
            entry['range'] = f'± {round(max(values) - min(values), 4)}'
            entry['extra'] = f'{len(values)} samples, median'
        out.append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        'input',
        nargs='?',
        help='file holding `go test -bench` output; reads stdin when omitted',
    )
    ap.add_argument(
        '--allow-empty',
        action='store_true',
        help='emit [] instead of failing when no benchmark lines are found',
    )
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding='utf-8') as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    entries = reduce_samples(parse(text))

    if not entries and not args.allow_empty:
        # Fail loudly: a silent empty series would publish a chart point of
        # nothing and read as "no regression" forever. This is the vacuous-gate
        # failure mode in publisher form.
        print(
            'bench-reduce: no Benchmark lines found in input; '
            'did the run actually execute benchmarks?',
            file=sys.stderr,
        )
        return 1

    json.dump(entries, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
