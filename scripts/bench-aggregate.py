#!/usr/bin/env python3
"""Render the weekly benchmark tracker issue body for one repo.

The third tracker in the house set, after gremlins and stryker. Same shape: one
permanent issue per repo, body rewritten in place, machine regions delimited by
HTML-comment sentinels, `## Free-form notes` preserved, decisions exported as
marker files for the workflow's shell to act on. Pure body generator: no network,
no gh CLI, no auth. Body on stdout, diagnostics on stderr.

Why this exists: weekly-bench measured benchmarks and reported a 1.5x regression
only into a job summary nobody opens. Its four sibling weekly workflows all
surface findings as a tracker issue, and that is the difference between a
tracker that is correct and one that is noticed.

WHERE THE HISTORY COMES FROM, and why this differs from its siblings

gremlins and stryker keep their rolling table in the issue body, because a
mutation report is an ephemeral artifact and the issue is the only store. That
reason does not hold here: the benchmark series already lives durably in each
consumer's orphan `benchmarks` branch, up to 100 points, and since the
attribution repair each point names the commit it measured. So this script reads
that series and REGENERATES the table every run instead of parsing its own
previous output.

That is deliberate and it removes a whole class of defect the siblings carry: the
stryker tracker had to fix a fused table column caused by rolling its own
rendered markdown forward, and a tracker that re-derives from data cannot drift
from it. Only the free-form notes are read back out of the existing body.

THRESHOLDS ARE PER METRIC, because the metrics are not alike

  * ns/op is wall clock on a shared runner, whose amplitude is documented at
    10-20%. A tight threshold there trains everyone to ignore the tracker, so it
    keeps the 1.5x the workflow already configures for the action's own alert:
    the target is an algorithmic regression, which shows up as a multiple.
  * B/op and allocs/op are COUNTED, not timed. They do not move with runner
    load, so a real increase is a real change and 1.5x would hide it entirely.
    A library whose whole claim is that it does not allocate needs the tight
    threshold, which is the claim xmlx and jsonx were enrolled to defend.
  * Crossing zero is called out on its own. Allocation-free becoming
    allocating is the regression these libraries exist to prevent, and it is
    also the case where a ratio is undefined.

Never closes the issue. The sibling sentinel query is `--state open`, so a closed
tracker is invisible next week; a clean week renders an inline note instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import statistics
import sys

PREFIX = 'window.BENCHMARK_DATA = '

# Rows in the rolling table. Matches the siblings so the three read alike.
ROLLING_ROWS = 12
# Points behind the newest that form a baseline. A median over several runs is
# steadier than the immediately previous point, which is one sample of a noisy
# quantity, and it is what makes the ns/op threshold survivable.
BASELINE_POINTS = 6
# Wall-clock regression factor. Deliberately loose; see the module docstring.
TIME_FACTOR = 1.50
# Counted-metric regression factor. Tight, because these do not drift.
COUNT_FACTOR = 1.05
# Cap on findings rendered inline, shared across buckets, as the siblings do.
MAX_INLINE = 50

TIME_UNITS = frozenset({'ns/op', 'us/op', 'ms/op', 's/op'})
COUNT_UNITS = frozenset({'B/op', 'allocs/op', 'MB/s', 'bytes/op'})

TITLE = 'Benchmark regression tracker'

HEADER_TPL = """# {title}

Weekly medians for `{repo}`, compared against the median of the previous {n} runs.
Updated {week} UTC by [this run]({run_url}).

Full series and charts: [`benchmarks` branch]({chart_url}).
"""

DATA_BLOCK_TPL = """## Trend

<!-- bench-data -->
| Run | Commit | Benchmarks | Regressed | Improved |
|---|---|---|---|---|
{rows}
<!-- /bench-data -->"""

LEGEND = """
## How to read this

Each run measures every `func Benchmark` in the repo, takes 10 samples, and
reduces them to one median per benchmark per metric. A finding compares that
median against the median of the previous {n} runs.

| Metric | Flagged when | Why this threshold |
|---|---|---|
| `ns/op` | {tf}x slower | Wall clock on a shared runner varies 10-20% by itself, so only a multiple is evidence. The target is an algorithmic regression. |
| `B/op`, `allocs/op` | {cf}x higher | Counted, not timed, so they do not move with runner load. A real increase is a real change. |
| any | zero to non-zero | An allocation-free path that starts allocating. The ratio is undefined and the change is the point. |

Nothing here gates a merge or fails a build. A flagged wall-clock finding is
evidence to investigate, not proof the code got slower; check the `±` spread
before believing a single run.
"""


class DataError(RuntimeError):
    """The series could not be read."""


def load_series(path: pathlib.Path) -> dict:
    raw = path.read_text(encoding='utf-8')
    if not raw.startswith(PREFIX):
        raise DataError(f'{path} is not the benchmark action data format')
    try:
        data = json.loads(raw[len(PREFIX) :])
    except json.JSONDecodeError as err:
        raise DataError(f'{path} payload is not valid JSON: {err}') from err
    if not isinstance(data, dict) or not isinstance(data.get('entries'), dict):
        raise DataError(f'{path} carries no entries')
    return data


def factor_for(unit: str) -> float:
    """Regression factor for a unit. Unknown units are treated as counted."""
    return TIME_FACTOR if unit in TIME_UNITS else COUNT_FACTOR


def baseline_for(points: list[dict], name: str) -> float | None:
    """Median of `name` across the given points, or None when it never appears."""
    values = []
    for point in points:
        for bench in point.get('benches') or []:
            if bench.get('name') == name and isinstance(bench.get('value'), (int, float)):
                values.append(float(bench['value']))
                break
    return statistics.median(values) if values else None


def compare(points: list[dict], index: int) -> dict:
    """Classify every benchmark at points[index] against its preceding window."""
    window = points[max(0, index - BASELINE_POINTS) : index]
    regressed, improved, new = [], [], []

    for bench in points[index].get('benches') or []:
        name, unit = bench.get('name'), bench.get('unit') or ''
        value = bench.get('value')
        if not name or not isinstance(value, (int, float)):
            continue
        value = float(value)
        base = baseline_for(window, name) if window else None

        if base is None:
            new.append({'name': name, 'unit': unit, 'value': value})
            continue

        finding = {
            'name': name,
            'unit': unit,
            'value': value,
            'base': base,
            'range': bench.get('range') or '',
        }
        factor = factor_for(unit)

        if base == 0:
            # Undefined ratio. Any move off zero is the finding; staying at zero
            # is the healthy case and not worth a line.
            if value > 0:
                finding['why'] = 'started allocating'
                regressed.append(finding)
            continue
        if value == 0:
            finding['why'] = 'reached zero'
            improved.append(finding)
            continue

        ratio = value / base
        finding['ratio'] = ratio
        if ratio >= factor:
            finding['why'] = f'{ratio:.2f}x'
            regressed.append(finding)
        elif ratio <= 1 / factor:
            finding['why'] = f'{ratio:.2f}x'
            improved.append(finding)

    return {'regressed': regressed, 'improved': improved, 'new': new}


def fmt_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return f'{int(value)}'
    return f'{value:.4g}'


def render_finding(finding: dict) -> str:
    unit = finding['unit']
    if 'base' not in finding:
        return f'- `{finding["name"]}` — {fmt_value(finding["value"])} {unit} (first measurement)'
    spread = f' {finding["range"]}' if finding.get('range') else ''
    return (
        f'- `{finding["name"]}` — {fmt_value(finding["base"])} to '
        f'{fmt_value(finding["value"])} {unit}{spread}  **{finding["why"]}**'
    )


def render_bucket(title: str, findings: list[dict], cap: int, open_attr: str) -> tuple[str, int]:
    """One <details> block, capped. Returns (markdown, overflow_count)."""
    if not findings:
        return '', 0
    shown, overflow = findings[:cap], max(len(findings) - cap, 0)
    lines = '\n'.join(render_finding(f) for f in shown)
    block = (
        f'<details{open_attr}>\n<summary>{title} — {len(findings)}</summary>\n\n'
        f'{lines}\n\n</details>\n'
    )
    return block, overflow


def render_rows(points: list[dict], repo_url: str) -> str:
    """Regenerate the trend table from the series, newest first."""
    rows = []
    for index in range(len(points) - 1, -1, -1):
        point = points[index]
        verdict = compare(points, index)
        stamp = dt.datetime.fromtimestamp((point.get('date') or 0) / 1000, tz=dt.UTC).strftime(
            '%Y-%m-%d %H:%M'
        )
        sha = (point.get('commit') or {}).get('id') or ''
        url = (point.get('commit') or {}).get('url') or f'{repo_url}/commit/{sha}'
        link = f'[`{sha[:8]}`]({url})' if sha else '—'
        rows.append(
            f'| {stamp} | {link} | {len(point.get("benches") or [])} | '
            f'{len(verdict["regressed"])} | {len(verdict["improved"])} |'
        )
        if len(rows) >= ROLLING_ROWS:
            break
    return '\n'.join(rows)


def carry_notes(existing: str) -> str:
    if existing:
        match = re.search(r'## Free-form notes\s*\n(.*?)$', existing, re.DOTALL)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return "Add anything below — won't be touched by the auto-updater."


def build_body(
    data: dict, repo: str, week: str, run_url: str, existing: str
) -> tuple[str, bool, int]:
    suite, points = next(iter(data['entries'].items()))
    if not points:
        raise DataError(f'suite {suite!r} has no data points')

    verdict = compare(points, len(points) - 1)
    entries = len(points[-1].get('benches') or [])
    repo_url = data.get('repoUrl') or f'https://github.com/cplieger/{repo}'

    reg_block, of1 = render_bucket('Regressed', verdict['regressed'], MAX_INLINE, ' open')
    remaining = max(MAX_INLINE - min(len(verdict['regressed']), MAX_INLINE), 0)
    imp_block, of2 = render_bucket('Improved', verdict['improved'], remaining, '')
    new_block, of3 = render_bucket('First measurement', verdict['new'], remaining, '')

    findings = reg_block + ('\n' if reg_block else '') + imp_block
    findings += ('\n' if imp_block and new_block else '') + new_block
    if not verdict['regressed']:
        findings = (
            'No benchmark moved past its threshold this week. 🎉\n\n' + findings
        ).rstrip() + '\n'
    overflow = of1 + of2 + of3
    if overflow:
        findings += f'\n_… and {overflow} more; full series on the `benchmarks` branch._\n'

    body = (
        HEADER_TPL.format(
            title=TITLE,
            repo=repo,
            n=BASELINE_POINTS,
            week=week,
            run_url=run_url,
            chart_url=f'{repo_url}/tree/benchmarks',
        )
        + '\n'
        + DATA_BLOCK_TPL.format(rows=render_rows(points, repo_url))
        + '\n\n## Findings\n\n<!-- bench-findings -->\n'
        + findings
        + '<!-- /bench-findings -->\n'
        + LEGEND.format(n=BASELINE_POINTS, tf=f'{TIME_FACTOR:g}', cf=f'{COUNT_FACTOR:g}')
        + '\n## Free-form notes\n\n'
        + carry_notes(existing)
        + '\n'
    )
    return body, bool(verdict['regressed']), entries


def main() -> int:
    ap = argparse.ArgumentParser(
        description='Render the weekly benchmark tracker issue body for one repo.'
    )
    ap.add_argument('--repo', required=True, help='repo name without owner')
    ap.add_argument('--data-file', required=True, type=pathlib.Path, help="the consumer's data.js")
    ap.add_argument('--week', required=True, help='YYYY-MM-DD HH:MM')
    ap.add_argument('--run-url', required=True)
    ap.add_argument('--existing-body-file', type=pathlib.Path, default=None)
    ap.add_argument('--regression-marker-file', type=pathlib.Path, default=None)
    ap.add_argument(
        '--entries-marker-file',
        type=pathlib.Path,
        default=None,
        help='Count of benchmarks in the newest point; 0 means leave the issue untouched.',
    )
    args = ap.parse_args()

    existing = ''
    if args.existing_body_file and args.existing_body_file.is_file():
        existing = args.existing_body_file.read_text(encoding='utf-8')

    # Tolerant like its siblings: a repo whose series cannot be read reports zero
    # entries so the workflow leaves its issue alone, rather than publishing a
    # misleading empty tracker. The workflow decides whether that is fatal.
    try:
        data = load_series(args.data_file)
        body, regression, entries = build_body(data, args.repo, args.week, args.run_url, existing)
    except (DataError, OSError) as err:
        print(f'[{args.repo}] bench-aggregate: {err}', file=sys.stderr)
        if args.entries_marker_file:
            args.entries_marker_file.write_text('0')
        if args.regression_marker_file:
            args.regression_marker_file.write_text('false')
        return 0

    if args.entries_marker_file:
        args.entries_marker_file.write_text(str(entries))
    if args.regression_marker_file:
        args.regression_marker_file.write_text('true' if regression else 'false')
    if entries == 0:
        print(f'[{args.repo}] newest point carries no benchmarks', file=sys.stderr)
        return 0

    print(body)
    return 0


if __name__ == '__main__':
    sys.exit(main())
