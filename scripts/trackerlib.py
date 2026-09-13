"""The tracker-issue body skeleton shared by the weekly aggregators.

A tracker body has machine regions delimited by HTML-comment sentinels
(`<!-- name -->` ... `<!-- /name -->`) and a free-form notes section after
`## Free-form notes` that the updater carries forward untouched. This module
owns the sentinel format, the rolling history table (one row per run, keyed by
run id, trimmed to a window), the trend and regression tests over that table,
and the notes carry-over. Rendering the per-tracker prose stays in each
aggregator.

Runs on the runner's default python3 (3.12): weekly-gremlins does not pin an
interpreter.
"""

from __future__ import annotations

import re
import statistics

ROLLING_WEEKS = 12
NOTES_DEFAULT = "Add anything below — won't be touched by the auto-updater."
RUN_MARKER_RE = re.compile(r'<!--\s*run:(\d+)\s*-->')
DATA_ROW_RE = re.compile(r'^\| 20\d{2}-')


def run_id_of(run_url: str) -> str:
    """The workflow run id from a run URL, or '' when it cannot be read.

    The row's own timestamp cannot identify the run that wrote it: it is
    `date -u` taken inside the aggregate step, so a retry stamps a different
    time for the SAME run. The id is what makes a re-aggregate idempotent.
    """
    m = re.search(r'/runs/(\d+)', run_url or '')
    return m.group(1) if m else ''


def sentinel_block(name: str, inner: str) -> str:
    return f'<!-- {name} -->\n{inner}\n<!-- /{name} -->'


def sentinel_inner(existing: str, name: str) -> str | None:
    """The text between a sentinel pair, or None when the body has none."""
    if not existing:
        return None
    m = re.search(
        rf'<!-- {re.escape(name)} -->(.*?)<!-- /{re.escape(name)} -->', existing, re.DOTALL
    )
    return m.group(1) if m else None


def history_lines(existing: str, sentinel: str) -> list[str]:
    """The data rows of the history table inside `sentinel`, newest first."""
    inner = sentinel_inner(existing, sentinel)
    if inner is None:
        return []
    return [line.rstrip() for line in inner.splitlines() if DATA_ROW_RE.match(line)]


def split_cells(row: str) -> list[str]:
    """Non-empty cells of a table row. An empty cell shifts later indexes."""
    return [c.strip() for c in row.split('|') if c.strip()]


def history_rows(existing: str, sentinel: str) -> list[list[str]]:
    return [split_cells(row) for row in history_lines(existing, sentinel)]


def percent_cell(cells: list[str], index: int) -> float | None:
    """The float behind a `78.4%` cell, or None when absent or unreadable."""
    if len(cells) <= index:
        return None
    try:
        return float(cells[index].rstrip('%'))
    except ValueError:
        return None


def history_column(existing: str, sentinel: str, index: int = 1) -> list[float]:
    """Every readable percent value in one column of the history, newest first.

    Feeds the trend mean and the regression test, where order does not matter
    and an unreadable row simply drops out. A reader that needs a SPECIFIC
    row (last week's count) must go through `history_rows` and index the row
    itself, because dropping unreadable rows here would shift what [0] means.
    """
    values = []
    for cells in history_rows(existing, sentinel):
        value = percent_cell(cells, index)
        if value is not None:
            values.append(value)
    return values


def update_history_block(
    existing: str,
    sentinel: str,
    header: str,
    new_row: str,
    run_id: str = '',
    keep: int = ROLLING_WEEKS,
) -> str:
    """Roll the history table forward by one run and render the sentinel block.

    `new_row` carries every cell but the delta, computed on column 2 against the
    previous newest row; `header` is the table's two header lines. A run
    contributes ONE row: a re-run of the aggregate job (`if: always()` on the
    `run` dependency) reads the SAME artifacts, so a second row would count one
    measurement twice in the rolling mean. A re-aggregate replaces the row that
    carries its run id; a row with no marker predates the scheme and is kept.
    """
    rows = history_lines(existing, sentinel)
    if run_id:
        rows = [
            r for r in rows if (own := RUN_MARKER_RE.search(r)) is None or own.group(1) != run_id
        ]

    prev = percent_cell(split_cells(rows[0]), 1) if rows else None
    current = percent_cell(split_cells(new_row), 1)
    delta_str = '—' if prev is None or current is None else f'{current - prev:+.1f}%'

    # The delta is its OWN cell and the run marker sits AFTER the closing pipe:
    # both must add a trailing cell, never shift an index a reader uses
    # (cells[1] for the score, cells[4] for the gremlins live count).
    row = new_row.rstrip() + f' {delta_str} |'
    if run_id:
        row += f' <!-- run:{run_id} -->'
    rows.insert(0, row)
    rows = rows[:keep]
    return sentinel_block(sentinel, header + '\n' + '\n'.join(rows))


def trend_marker(mean: float, history: list[float], weeks: int = ROLLING_WEEKS) -> str:
    if not history:
        return ''
    rolling = statistics.mean(history)
    delta = mean - rolling
    if abs(delta) < 0.5:
        symbol = '→'
    elif delta > 0:
        symbol = '↗'
    else:
        symbol = '↘'
    return f'**Trend**: {symbol} {delta:+.1f}% from {weeks}-week mean ({rolling:.1f}%).'


def regression(mean: float, history: list[float], threshold: float) -> bool:
    """True when `mean` sits more than `threshold` points under the history mean."""
    return bool(history) and mean < statistics.mean(history) - threshold


def preserve_notes(existing: str) -> str:
    """The free-form notes of an existing body, or the default invitation."""
    if existing:
        m = re.search(r'## Free-form notes\s*\n(.*?)$', existing, re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return NOTES_DEFAULT
