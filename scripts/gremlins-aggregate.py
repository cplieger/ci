#!/usr/bin/env python3
"""Aggregate weekly gremlins runs into the per-repo tracker issue body.

Inputs:
    repo:           e.g. "atomicfile" (without owner)
    artifacts_dir:  directory containing per-attempt subdirs like
                    gremlins-<repo>-1/, gremlins-<repo>-2/, gremlins-<repo>-3/
                    each holding gremlins-out.json (full mutant list).
    week_ending:    YYYY-MM-DD
    run_url:        URL of the current workflow run (for "see full report" link)
    existing_body:  current issue body (or empty if creating fresh)

Outputs:
    Stdout:  the new issue body (markdown).
    Stderr:  diagnostic lines.

Body format:

    # Gremlins mutation testing tracker

    Auto-updated by [weekly-gremlins.yaml](...). Last update: 2026-06-08.

    **This week**: 78.4% efficacy (±2.1% across 3 runs), 92.3% mutant coverage.
    **Trend**: ↗ +1.2% from 12-week mean (77.2%).

    ## Rolling 12-week history
    <!-- gremlins-data -->
    | Week ending | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ vs prev |
    |---|---|---|---|---|---|
    | 2026-06-08 | 78.4% | ±2.1% | 92.3% | 47 | +1.2% |
    | ... 11 more |
    <!-- /gremlins-data -->

    ## Current live mutants (47, this week)
    <!-- live-mutants -->
    <details>
    <summary>Confirmed live (LIVED in all 3 runs) — 41</summary>

    ### internal/auth/argon.go
    - L42 — CONDITIONALS_BOUNDARY: `>` → `>=`
    - L67 — ARITHMETIC_BASE: `+` → `-`

    ...

    </details>

    <details>
    <summary>Flaky (LIVED in some runs, KILLED in others) — 6</summary>
    ...
    </details>

    Full report: [run artifacts](RUN_URL#artifacts).
    <!-- /live-mutants -->

    ## How to read
    ...

    ## Free-form notes
    ...

The `<!-- gremlins-data -->` and `<!-- live-mutants -->` sentinel blocks are
the only parts replaced; anything outside is preserved across updates so users
can add notes without conflict.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAX_LIVE_MUTANTS_INLINE = 50  # cap; remainder goes to artifact link
ROLLING_WEEKS = 12
REGRESSION_THRESHOLD_PCT = 5.0  # mean drops > 5% below rolling-mean → flag

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def mutant_key(m: dict) -> tuple:
    """Stable identifier for a mutant across runs.

    Gremlins JSON layout (v0.6): a mutant entry looks like:
        {"mutator_name": "...", "type": "CONDITIONALS_BOUNDARY", "status": "LIVED",
         "position": {"file": "...", "row": ..., "column": ...},
         "current_token": ">", "new_token": ">="}

    Older versions used "relative_position" / different key names; we read both
    so the script tolerates schema drift.
    """
    pos = m.get("position") or m.get("relative_position") or {}
    return (
        m.get("file") or pos.get("file"),
        pos.get("row") or pos.get("line"),
        pos.get("column") or pos.get("col"),
        m.get("type") or m.get("mutator") or m.get("mutator_name"),
    )


def mutant_label(m: dict) -> str:
    pos = m.get("position") or m.get("relative_position") or {}
    file = m.get("file") or pos.get("file") or "?"
    row = pos.get("row") or pos.get("line") or "?"
    typ = m.get("type") or m.get("mutator") or m.get("mutator_name") or "?"
    cur = m.get("current_token") or m.get("token_old") or "?"
    new = m.get("new_token") or m.get("token_new") or "?"
    return f"L{row} — {typ}: `{cur}` → `{new}`"


def file_of(m: dict) -> str:
    pos = m.get("position") or m.get("relative_position") or {}
    return m.get("file") or pos.get("file") or "(unknown)"


def safe_div(a: float, b: float) -> float:
    return (a / b) if b else 0.0


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate(attempt_files: list[Path]) -> dict:
    """Cross-reference mutants across N runs.

    Returns a dict with:
        attempts:        N
        per_attempt:     [{killed, lived, not_covered, ...} for each run]
        efficacy_mean:   float (0..100)
        efficacy_stddev: float
        mutant_coverage_mean: float
        confirmed_live:  list of mutant detail dicts (LIVED in ALL N runs)
        flaky:           list of mutant detail dicts (LIVED in some, KILLED in others)
        live_count:      len(confirmed_live)
    """
    runs = []
    for f in attempt_files:
        try:
            with open(f) as fp:
                data = json.load(fp)
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        muts = data.get("mutants", []) or []
        runs.append(muts)

    if not runs:
        return {
            "attempts": 0,
            "per_attempt": [],
            "efficacy_mean": 0.0,
            "efficacy_stddev": 0.0,
            "mutant_coverage_mean": 0.0,
            "confirmed_live": [],
            "flaky": [],
            "live_count": 0,
        }

    # Per-run aggregate counts
    per_attempt = []
    for run in runs:
        counts = {"killed": 0, "lived": 0, "not_covered": 0, "timed_out": 0, "not_viable": 0}
        for m in run:
            s = (m.get("status") or "").upper()
            if s == "KILLED":
                counts["killed"] += 1
            elif s == "LIVED":
                counts["lived"] += 1
            elif s == "NOT_COVERED":
                counts["not_covered"] += 1
            elif s == "TIMED_OUT":
                counts["timed_out"] += 1
            elif s == "NOT_VIABLE":
                counts["not_viable"] += 1
        runnable = counts["killed"] + counts["lived"] + counts["timed_out"]
        # Treat TIMED_OUT as a "killed" outcome (mutation made the test hang
        # → effectively detected, even if not via assertion).
        eff = round(safe_div((counts["killed"] + counts["timed_out"]) * 100, runnable), 1)
        cov_denom = runnable + counts["not_covered"]
        cov = round(safe_div(runnable * 100, cov_denom), 1)
        per_attempt.append({**counts, "runnable": runnable, "efficacy": eff, "mutant_coverage": cov})

    efficacies = [a["efficacy"] for a in per_attempt]
    eff_mean = round(statistics.mean(efficacies), 1)
    eff_stddev = round(statistics.pstdev(efficacies), 1) if len(efficacies) > 1 else 0.0
    cov_mean = round(statistics.mean([a["mutant_coverage"] for a in per_attempt]), 1)

    # Cross-reference mutants: status across runs.
    by_key: dict[tuple, dict] = {}
    for run in runs:
        for m in run:
            k = mutant_key(m)
            if k not in by_key:
                by_key[k] = {"statuses": [], "detail": m}
            by_key[k]["statuses"].append((m.get("status") or "").upper())

    # Bucket every mutant that LIVED at least once by its lived-frequency.
    # Rare flakes (LIVED 1/N) are the most actionable — a test that USUALLY
    # catches a mutant but occasionally doesn't is a flaky-test signal worth
    # surfacing prominently. Solid gaps (LIVED N/N) are also reported but
    # are the "known gap" tier.
    n_runs = len(runs)
    live_buckets: dict[int, list[dict]] = {i: [] for i in range(1, n_runs + 1)}
    for entry in by_key.values():
        statuses = entry["statuses"]
        lived_count = sum(1 for s in statuses if s == "LIVED")
        if lived_count >= 1:
            entry["detail"]["_lived_count"] = lived_count
            entry["detail"]["_runs_seen"] = len(statuses)
            live_buckets[lived_count].append(entry["detail"])

    total_live = sum(len(v) for v in live_buckets.values())

    return {
        "attempts": len(runs),
        "per_attempt": per_attempt,
        "efficacy_mean": eff_mean,
        "efficacy_stddev": eff_stddev,
        "mutant_coverage_mean": cov_mean,
        "live_buckets": live_buckets,    # {lived_count: [mutant detail, ...]}
        "live_count": total_live,
        "n_runs": n_runs,
    }


# ---------------------------------------------------------------------------
# Body generation
# ---------------------------------------------------------------------------
HEADER_TPL = """# Gremlins mutation testing tracker

Auto-updated weekly by [`cplieger/ci/.github/workflows/weekly-gremlins.yaml`](https://github.com/cplieger/ci/blob/main/.github/workflows/weekly-gremlins.yaml).
Last update: {week_ending}

**This week**: {eff_mean}% efficacy (±{eff_stddev}% across {attempts} runs), {cov_mean}% mutant coverage. {live_count} confirmed live mutant{plural}.
{trend_line}

## Rolling 12-week history
"""

DATA_BLOCK_TPL = """<!-- gremlins-data -->
| Week ending | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ vs prev |
|---|---|---|---|---|---|
{rows}
<!-- /gremlins-data -->"""

LIVE_BLOCK_TPL = """## Current live mutants{header_suffix}
<!-- live-mutants -->
{confirmed_block}{flaky_block}{overflow_block}
<!-- /live-mutants -->"""

LIVE_DETAILS_TPL = """<details>
<summary>{summary} — {count}</summary>

{body}

</details>
"""

LEGEND = """## How to read

- **Mean efficacy**: % of runnable mutants killed (or timed-out, treated as caught), averaged across the N runs
- **Stddev**: variance across runs — high stddev (>3%) signals flaky tests
- **Mutant coverage**: % of mutants reached by the test suite (test depth)

The "Current live mutants" section is bucketed by **how many of the {N} runs the
mutant LIVED in**:

- **Rare flake (1/N)**: a mutant your tests usually KILL but occasionally let
  through. **Most actionable** — almost always means a flaky test that's not
  reliable. Open the section to see the file:line; expect to fix the test, not
  the production code.
- **Weakly flaky (k/N for 1<k<N)**: tests catch this mutant inconsistently.
  Less ideal than rare-flake but still worth investigating.
- **Solid gap (N/N)**: every run lets this mutant through. Either there's no
  test exercising the path, or the test asserts the wrong thing. Action: add a
  test or strengthen an existing assertion.

The `mutation-regression` label is added when this week's mean efficacy drops
>5% below the rolling 12-week mean.

## Free-form notes

Add anything below — won't be touched by the auto-updater.
"""


def render_mutant_section(name: str, mutants: list[dict], cap: int) -> tuple[str, int]:
    """Group mutants by file, render as collapsible section. Returns (markdown, overflow_count)."""
    if not mutants:
        return "", 0
    # Group by file, sort within file by line.
    by_file: dict[str, list[dict]] = defaultdict(list)
    for m in mutants:
        by_file[file_of(m)].append(m)
    for f in by_file:
        by_file[f].sort(key=lambda m: (m.get("position") or m.get("relative_position") or {}).get("row") or 0)

    rendered = 0
    overflow = 0
    parts = []
    for file in sorted(by_file):
        if rendered >= cap:
            overflow += len(by_file[file])
            continue
        parts.append(f"### `{file}`")
        for m in by_file[file]:
            if rendered >= cap:
                overflow += 1
                continue
            parts.append(f"- {mutant_label(m)}")
            rendered += 1
        parts.append("")
    body = "\n".join(parts).rstrip()
    return LIVE_DETAILS_TPL.format(summary=name, count=len(mutants), body=body), overflow


def render_bucketed_live_mutants(buckets: dict[int, list[dict]], n_runs: int, cap: int) -> tuple[str, int]:
    """Render frequency-bucketed live mutants, rare-first.

    Bucket order:
      1/N            "Rare flake — LIVED in 1 of N runs (test usually catches it)" — open
      2/N..(N-1)/N   "Weakly flaky — LIVED in K of N runs" — collapsed
      N/N            "Solid gap — LIVED in all N runs (known coverage hole)" — collapsed

    Within each bucket, group by file. Rare bucket takes priority for cap budget
    since it's the most actionable signal.
    """
    overflow_total = 0
    remaining_cap = cap
    parts = []

    for n in range(1, n_runs + 1):
        mutants = buckets.get(n) or []
        if not mutants:
            continue

        if n == n_runs:
            label = f"Solid gap — LIVED in all {n_runs} runs (known coverage hole)"
            open_attr = ""
        elif n == 1:
            label = f"Rare flake — LIVED in 1 of {n_runs} runs (test usually catches it)"
            open_attr = " open"
        else:
            label = f"Weakly flaky — LIVED in {n} of {n_runs} runs"
            open_attr = ""

        by_file: dict[str, list[dict]] = defaultdict(list)
        for m in mutants:
            by_file[file_of(m)].append(m)
        for f in by_file:
            by_file[f].sort(key=lambda m: (m.get("position") or m.get("relative_position") or {}).get("row") or 0)

        body_parts = []
        for file in sorted(by_file):
            if remaining_cap <= 0:
                overflow_total += len(by_file[file])
                continue
            body_parts.append(f"### `{file}`")
            for m in by_file[file]:
                if remaining_cap <= 0:
                    overflow_total += 1
                    continue
                body_parts.append(f"- {mutant_label(m)}")
                remaining_cap -= 1
            body_parts.append("")

        body_md = "\n".join(body_parts).rstrip()
        parts.append(
            f"<details{open_attr}>\n<summary>{label} — {len(mutants)}</summary>\n\n{body_md}\n\n</details>\n"
        )

    return "\n".join(parts), overflow_total


def update_history_block(existing: str, new_row: str, mean_for_trend: float) -> tuple[str, float]:
    """Update the rolling 12-week table block. Returns (block, prev_mean for delta)."""
    rows = []
    if existing:
        m = re.search(r"<!-- gremlins-data -->(.*?)<!-- /gremlins-data -->", existing, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if re.match(r"^\| 20\d{2}-", line):
                    rows.append(line.rstrip())

    prev_mean = 0.0
    if rows:
        # Extract previous mean (column 2, format "78.4%")
        first_row_cells = [c.strip() for c in rows[0].split("|") if c.strip()]
        if len(first_row_cells) >= 2:
            try:
                prev_mean = float(first_row_cells[1].rstrip("%"))
            except ValueError:
                pass

    # Compute delta on this row
    delta = mean_for_trend - prev_mean if rows else 0.0
    delta_str = f"{delta:+.1f}%" if rows else "—"
    new_row_with_delta = new_row.rstrip("|").rstrip() + f" {delta_str} |"

    rows.insert(0, new_row_with_delta)
    rows = rows[:ROLLING_WEEKS]

    block = DATA_BLOCK_TPL.format(rows="\n".join(rows))
    return block, prev_mean


def trend_marker(mean: float, history_means: list[float]) -> str:
    if not history_means:
        return ""
    rolling = statistics.mean(history_means)
    delta = mean - rolling
    if abs(delta) < 0.5:
        symbol = "→"
    elif delta > 0:
        symbol = "↗"
    else:
        symbol = "↘"
    return f"**Trend**: {symbol} {delta:+.1f}% from {ROLLING_WEEKS}-week mean ({rolling:.1f}%)."


def build_body(repo: str, week: str, agg: dict, run_url: str, existing: str) -> tuple[str, bool]:
    """Returns (new_body, regression_flag)."""
    # Build new rolling-history row.
    eff_mean = agg["efficacy_mean"]
    eff_stddev = agg["efficacy_stddev"]
    cov_mean = agg["mutant_coverage_mean"]
    live_count = agg["live_count"]

    new_row = f"| {week} | {eff_mean}% | ±{eff_stddev}% | {cov_mean}% | {live_count} |"
    history_block, prev_mean = update_history_block(existing, new_row, eff_mean)

    # Get all historical means for trend marker.
    history_means = []
    if existing:
        m = re.search(r"<!-- gremlins-data -->(.*?)<!-- /gremlins-data -->", existing, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if re.match(r"^\| 20\d{2}-", line):
                    cells = [c.strip() for c in line.split("|") if c.strip()]
                    if len(cells) >= 2:
                        try:
                            history_means.append(float(cells[1].rstrip("%")))
                        except ValueError:
                            continue

    trend_line = trend_marker(eff_mean, history_means)

    # Live mutants section, frequency-bucketed (rare-first, most actionable).
    live_block_inner, overflow = render_bucketed_live_mutants(
        agg["live_buckets"], agg["n_runs"], MAX_LIVE_MUTANTS_INLINE
    )
    overflow_block = (
        f"\n_… and {overflow} more in the [full report]({run_url}#artifacts)._\n"
        if overflow else ""
    )

    if not live_block_inner:
        live_block = "## Current live mutants\n<!-- live-mutants -->\nNone — every mutant killed by the test suite this week. 🎉\n<!-- /live-mutants -->"
    else:
        live_block = (
            f"## Current live mutants ({live_count}, this week)\n"
            f"<!-- live-mutants -->\n"
            f"{live_block_inner}{overflow_block}\n"
            f"<!-- /live-mutants -->"
        )

    # Header
    header = HEADER_TPL.format(
        week_ending=week,
        eff_mean=eff_mean,
        eff_stddev=eff_stddev,
        attempts=agg["attempts"],
        cov_mean=cov_mean,
        live_count=live_count,
        plural="" if live_count == 1 else "s",
        trend_line=trend_line,
    )

    # Preserve any free-form notes outside the sentinel blocks.
    notes = ""
    if existing:
        # Extract anything AFTER the legacy "## Free-form notes" section if present.
        m = re.search(r"## Free-form notes\s*\n(.*?)$", existing, re.DOTALL)
        if m:
            notes = m.group(1).strip()
    if not notes:
        notes = "Add anything below — won't be touched by the auto-updater."

    body = (
        header
        + history_block + "\n\n"
        + live_block + "\n\n"
        + LEGEND.rsplit("\n## Free-form notes", 1)[0]
        + "\n## Free-form notes\n\n" + notes + "\n"
    )

    # Regression flag.
    regression = bool(history_means) and (eff_mean < statistics.mean(history_means) - REGRESSION_THRESHOLD_PCT)

    return body, regression


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Aggregate weekly gremlins runs into the tracker issue body.")
    p.add_argument("--repo", required=True, help="repo name without owner")
    p.add_argument("--artifacts-dir", required=True, type=Path)
    p.add_argument("--week", required=True, help="YYYY-MM-DD")
    p.add_argument("--run-url", required=True, help="Workflow run URL for artifact link")
    p.add_argument("--existing-body-file", type=Path, default=None,
                   help="Path to existing issue body (or omit if creating fresh)")
    p.add_argument("--regression-marker-file", type=Path, default=None,
                   help="Write 'true' or 'false' here for the workflow to read")
    args = p.parse_args()

    attempt_files = sorted(args.artifacts_dir.glob(f"gremlins-{args.repo}-*/gremlins-out.json"))
    print(f"[{args.repo}] found {len(attempt_files)} attempt files", file=sys.stderr)

    agg = aggregate(attempt_files)
    print(f"[{args.repo}] efficacy={agg['efficacy_mean']}±{agg['efficacy_stddev']} "
          f"cov={agg['mutant_coverage_mean']} live={agg['live_count']} "
          f"buckets={ {k: len(v) for k, v in agg['live_buckets'].items()} }",
          file=sys.stderr)

    existing = ""
    if args.existing_body_file and args.existing_body_file.exists():
        existing = args.existing_body_file.read_text()

    body, regression = build_body(args.repo, args.week, agg, args.run_url, existing)
    print(body)

    if args.regression_marker_file:
        args.regression_marker_file.write_text("true" if regression else "false")

    return 0


if __name__ == "__main__":
    sys.exit(main())
