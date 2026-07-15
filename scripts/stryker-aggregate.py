#!/usr/bin/env python3
"""Aggregate weekly Stryker runs into the per-repo tracker issue body.

The TS counterpart of gremlins-aggregate.py. One run per package dir per week
(no attempt averaging — Stryker+vitest is deterministic enough on these small
surfaces); a repo with several enrolled package dirs gets ONE issue whose
score aggregates all dirs and whose mutant list prefixes each file with its
dir.

Inputs:
    repo:           e.g. "reactive" (without owner)
    artifacts_dir:  directory containing per-entry subdirs like
                    stryker-<repo>-<dirslug>/ each holding mutation.json
                    (the mutation-testing-elements schema report) and
                    meta.json ({"repo": ..., "dir": ...}).
    week_ending:    YYYY-MM-DD HH:MM
    run_url:        URL of the current workflow run (artifact link)
    existing_body:  current issue body (or empty if creating fresh)

Outputs:
    Stdout:  the new issue body (markdown).
    Stderr:  diagnostic lines.
    Markers: --regression-marker-file ("true"/"false"),
             --entries-marker-file (count of parsed package dirs; 0 means
             every report was missing/unparseable — the workflow then leaves
             the tracker issue untouched instead of writing a misleading 0%).

Body format mirrors the gremlins tracker (sentinel blocks, rolling 12-week
table, bucketed mutants, preserved free-form notes) so downstream tooling can
parse both with one shape. Differences: single-run line (no stddev), history
sentinels are <!-- stryker-data -->, and the buckets are Survived (tests ran
and missed it) vs No coverage (no test reaches it).

Score formula (mutation-testing-elements metrics, same as the badge step in
weekly-stryker.yaml): detected (Killed+Timeout) / valid (detected + Survived
+ NoCoverage). Mutant coverage: (valid - NoCoverage) / valid.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

MAX_MUTANTS_INLINE = 50
ROLLING_WEEKS = 12
REGRESSION_THRESHOLD_PCT = 5.0  # score drops > 5% below rolling mean → flag
DETECTED = {'Killed', 'Timeout'}
UNDETECTED = {'Survived', 'NoCoverage'}
REPLACEMENT_MAX_CHARS = 40


def load_report(path: Path):
    """Parse one mutation-testing-elements schema report.

    Returns (mutants, counts) or None when the file is missing/invalid.
    mutants: [{"file", "line", "mutator", "replacement", "status"}] for
    undetected mutants only (the actionable list). counts: dict of status
    totals across all mutants.
    """
    try:
        with open(path) as fp:
            data = json.load(fp)
    except json.JSONDecodeError, FileNotFoundError:
        return None
    if not isinstance(data, dict) or 'files' not in data:
        return None

    mutants, counts = [], {}
    for fname, fentry in (data.get('files') or {}).items():
        for m in fentry.get('mutants') or []:
            status = m.get('status') or '?'
            counts[status] = counts.get(status, 0) + 1
            if status in UNDETECTED:
                loc = (m.get('location') or {}).get('start') or {}
                mutants.append(
                    {
                        'file': fname,
                        'line': loc.get('line') or 0,
                        'mutator': m.get('mutatorName') or '?',
                        'replacement': m.get('replacement'),
                        'status': status,
                    }
                )
    return mutants, counts


def aggregate(entry_dirs: list[tuple[str, Path]]):
    """Combine per-package-dir reports into one repo-level result.

    entry_dirs: [(package_dir, report_path)]. Returns a dict with entries
    (parsed dir count), score, mutant_coverage, surviving counts, per_dir
    summaries, and the undetected mutant list (file paths prefixed with the
    package dir when it isn't the repo root).
    """
    per_dir, all_mutants = [], []
    totals = {}
    for pkg_dir, report in entry_dirs:
        loaded = load_report(report)
        if loaded is None:
            print(f'  skip unparseable report for dir {pkg_dir}', file=sys.stderr)
            continue
        mutants, counts = loaded
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        prefix = '' if pkg_dir in ('.', '') else pkg_dir.rstrip('/') + '/'
        for m in mutants:
            m['file'] = prefix + m['file']
            all_mutants.append(m)
        detected = sum(counts.get(s, 0) for s in DETECTED)
        valid = detected + sum(counts.get(s, 0) for s in UNDETECTED)
        per_dir.append(
            {
                'dir': pkg_dir,
                'score': round(detected / valid * 100, 1) if valid else 0.0,
                'valid': valid,
            }
        )

    detected = sum(totals.get(s, 0) for s in DETECTED)
    undetected = sum(totals.get(s, 0) for s in UNDETECTED)
    valid = detected + undetected
    covered = valid - totals.get('NoCoverage', 0)
    return {
        'entries': len(per_dir),
        'per_dir': per_dir,
        'score': round(detected / valid * 100, 1) if valid else 0.0,
        'mutant_coverage': round(covered / valid * 100, 1) if valid else 0.0,
        'surviving': undetected,
        'survived': totals.get('Survived', 0),
        'no_coverage': totals.get('NoCoverage', 0),
        'mutants': all_mutants,
    }


HEADER_TPL = """# Stryker mutation testing tracker

Auto-updated weekly by [`cplieger/ci/.github/workflows/weekly-stryker.yaml`](https://github.com/cplieger/ci/blob/main/.github/workflows/weekly-stryker.yaml).
Last update: {week_ending}

**This week**: {score}% mutation score, {cov}% mutant coverage. {surviving} surviving mutant{plural}.
{per_dir_line}{trend_line}

## Rolling 12-week history
"""

DATA_BLOCK_TPL = """<!-- stryker-data -->
| Run (UTC) | Score | Mutant coverage | Surviving | Δ score |
|---|---|---|---|---|
{rows}
<!-- /stryker-data -->"""

LEGEND = """## How to read

- **Score**: % of valid mutants detected (Killed + Timeout) out of detected +
  Survived + NoCoverage — the mutation-testing-elements formula, same as the
  README badge
- **Mutant coverage**: % of valid mutants reached by at least one test
- One Stryker run per package dir per week (deterministic enough on these TS
  surfaces; no attempt averaging like the Go/gremlins tracker)

The "Current surviving mutants" section is bucketed by status:

- **Survived**: a test executed the mutant and still passed — the assertion
  misses the behavior. Most actionable: strengthen the assertion or add a
  case.
- **No coverage**: no test reaches the mutated code at all. Add a test, or
  accept the gap if the file is deliberately untested glue.

The `mutation-regression` label is added when this week's score drops
>5% below the rolling 12-week mean.

## Free-form notes

Add anything below — won't be touched by the auto-updater.
"""


def esc_replacement(repl):
    if repl is None:
        return None
    flat = ' '.join(str(repl).split())
    if len(flat) > REPLACEMENT_MAX_CHARS:
        flat = flat[: REPLACEMENT_MAX_CHARS - 1] + '…'
    return flat.replace('`', "'")


def mutant_label(m: dict) -> str:
    repl = esc_replacement(m.get('replacement'))
    if repl:
        return f'- L{m["line"]} — {m["mutator"]} → `{repl}`'
    return f'- L{m["line"]} — {m["mutator"]}'


def render_bucket(title: str, mutants: list[dict], cap: int, open_attr: str) -> tuple[str, int]:
    """One <details> block, mutants grouped by file, capped. Returns
    (markdown, overflow_count)."""
    if not mutants:
        return '', 0
    by_file: dict[str, list[dict]] = {}
    for m in mutants:
        by_file.setdefault(m['file'], []).append(m)
    for f in by_file:
        by_file[f].sort(key=lambda m: m['line'])

    rendered, overflow, parts = 0, 0, []
    for file in sorted(by_file):
        if rendered >= cap:
            overflow += len(by_file[file])
            continue
        parts.append(f'### `{file}`')
        for m in by_file[file]:
            if rendered >= cap:
                overflow += 1
                continue
            parts.append(mutant_label(m))
            rendered += 1
        parts.append('')
    body = '\n'.join(parts).rstrip()
    block = f'<details{open_attr}>\n<summary>{title} — {len(mutants)}</summary>\n\n{body}\n\n</details>\n'
    return block, overflow


def update_history_block(existing: str, new_row_cells: str, score: float) -> str:
    """Roll the 12-week table forward; delta computed vs the previous top row."""
    rows = []
    if existing:
        m = re.search(r'<!-- stryker-data -->(.*?)<!-- /stryker-data -->', existing, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if re.match(r'^\| 20\d{2}-', line):
                    rows.append(line.rstrip())

    prev_score = None
    if rows:
        cells = [c.strip() for c in rows[0].split('|') if c.strip()]
        if len(cells) >= 2:
            try:
                prev_score = float(cells[1].rstrip('%'))
            except ValueError:
                prev_score = None

    delta_str = f'{score - prev_score:+.1f}%' if prev_score is not None else '—'
    # new_row_cells ends in " |"; append the delta as its OWN cell (the
    # gremlins tracker once fused it into the previous column — issue #4).
    rows.insert(0, new_row_cells.rstrip() + f' {delta_str} |')
    rows = rows[:ROLLING_WEEKS]
    return DATA_BLOCK_TPL.format(rows='\n'.join(rows))


def history_scores(existing: str) -> list[float]:
    scores = []
    if existing:
        m = re.search(r'<!-- stryker-data -->(.*?)<!-- /stryker-data -->', existing, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if re.match(r'^\| 20\d{2}-', line):
                    cells = [c.strip() for c in line.split('|') if c.strip()]
                    if len(cells) >= 2:
                        try:
                            scores.append(float(cells[1].rstrip('%')))
                        except ValueError:
                            continue
    return scores


def trend_marker(score: float, history: list[float]) -> str:
    if not history:
        return ''
    rolling = statistics.mean(history)
    delta = score - rolling
    symbol = '→' if abs(delta) < 0.5 else ('↗' if delta > 0 else '↘')
    return f'**Trend**: {symbol} {delta:+.1f}% from {ROLLING_WEEKS}-week mean ({rolling:.1f}%).'


def build_body(week: str, agg: dict, run_url: str, existing: str) -> tuple[str, bool]:
    score, cov, surviving = agg['score'], agg['mutant_coverage'], agg['surviving']

    new_row = f'| {week} | {score}% | {cov}% | {surviving} |'
    history_block = update_history_block(existing, new_row, score)
    history = history_scores(existing)
    trend_line = trend_marker(score, history)

    per_dir_line = ''
    if agg['entries'] > 1:
        pieces = ', '.join(f'`{d["dir"]}` {d["score"]}%' for d in agg['per_dir'])
        per_dir_line = f'Package dirs: {pieces}.\n'

    survived = [m for m in agg['mutants'] if m['status'] == 'Survived']
    no_cov = [m for m in agg['mutants'] if m['status'] == 'NoCoverage']
    survived_block, of1 = render_bucket(
        'Survived — a test ran and missed it', survived, MAX_MUTANTS_INLINE, ' open'
    )
    # No-coverage bucket gets whatever cap budget remains.
    remaining = max(MAX_MUTANTS_INLINE - min(len(survived), MAX_MUTANTS_INLINE), 0)
    nocov_block, of2 = render_bucket('No coverage — no test reaches it', no_cov, remaining, '')
    overflow = of1 + of2
    overflow_block = (
        f'\n_… and {overflow} more in the [full report]({run_url}#artifacts)._\n'
        if overflow
        else ''
    )

    if not (survived_block or nocov_block):
        live_block = (
            '## Current surviving mutants\n<!-- live-mutants -->\n'
            'None — every reached mutant killed this week. 🎉\n<!-- /live-mutants -->'
        )
    else:
        live_block = (
            f'## Current surviving mutants ({surviving}, this week)\n'
            f'<!-- live-mutants -->\n'
            f'{survived_block}{nocov_block}{overflow_block}\n'
            f'<!-- /live-mutants -->'
        )

    header = HEADER_TPL.format(
        week_ending=week,
        score=score,
        cov=cov,
        surviving=surviving,
        plural='' if surviving == 1 else 's',
        per_dir_line=per_dir_line,
        trend_line=trend_line,
    )

    notes = ''
    if existing:
        m = re.search(r'## Free-form notes\s*\n(.*?)$', existing, re.DOTALL)
        if m:
            notes = m.group(1).strip()
    if not notes:
        notes = "Add anything below — won't be touched by the auto-updater."

    body = (
        header
        + history_block
        + '\n\n'
        + live_block
        + '\n\n'
        + LEGEND.rsplit('\n## Free-form notes', 1)[0]
        + '\n## Free-form notes\n\n'
        + notes
        + '\n'
    )

    regression = bool(history) and (score < statistics.mean(history) - REGRESSION_THRESHOLD_PCT)
    return body, regression


def main() -> int:
    p = argparse.ArgumentParser(
        description='Aggregate weekly Stryker runs into the tracker issue body.'
    )
    p.add_argument('--repo', required=True, help='repo name without owner')
    p.add_argument('--artifacts-dir', required=True, type=Path)
    p.add_argument('--week', required=True, help='YYYY-MM-DD HH:MM')
    p.add_argument('--run-url', required=True)
    p.add_argument('--existing-body-file', type=Path, default=None)
    p.add_argument('--regression-marker-file', type=Path, default=None)
    p.add_argument(
        '--entries-marker-file',
        type=Path,
        default=None,
        help='Write the count of parsed package-dir reports here; 0 means the workflow should leave the issue untouched.',
    )
    args = p.parse_args()

    # Entries are matched by meta.json content, not artifact-name parsing
    # (repo names and dir slugs both contain hyphens).
    entry_dirs = []
    for meta_path in sorted(args.artifacts_dir.glob('stryker-*/meta.json')):
        try:
            meta = json.loads(meta_path.read_text())
        except OSError, json.JSONDecodeError:
            continue
        if meta.get('repo') != args.repo:
            continue
        entry_dirs.append((meta.get('dir') or '.', meta_path.parent / 'mutation.json'))
    print(f'[{args.repo}] found {len(entry_dirs)} report(s)', file=sys.stderr)

    agg = aggregate(entry_dirs)
    print(
        f'[{args.repo}] score={agg["score"]} cov={agg["mutant_coverage"]} '
        f'surviving={agg["surviving"]} (survived={agg["survived"]}, '
        f'no_coverage={agg["no_coverage"]}) entries={agg["entries"]}',
        file=sys.stderr,
    )

    if args.entries_marker_file:
        args.entries_marker_file.write_text(str(agg['entries']))
    if agg['entries'] == 0:
        if args.regression_marker_file:
            args.regression_marker_file.write_text('false')
        return 0

    existing = ''
    if args.existing_body_file and args.existing_body_file.exists():
        existing = args.existing_body_file.read_text()

    body, regression = build_body(args.week, agg, args.run_url, existing)
    print(body)

    if args.regression_marker_file:
        args.regression_marker_file.write_text('true' if regression else 'false')
    return 0


if __name__ == '__main__':
    sys.exit(main())
