#!/usr/bin/env python3
"""Render the `Broken external links` tracker body from a lychee markdown report.

weekly-links.yaml writes the body once per repo and hands it to tracker_issue.py;
the report itself is lychee's `--format markdown` output. Runs on the runner's
default python3 (3.12).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import trackerlib

# GitHub caps an issue body at 65536 characters; the rest of the body must fit
# beside the report.
REPORT_CAP_BYTES = 60000

PREAMBLE = (
    'Automated weekly external-link check. Report-only: this never blocks a merge.\n'
    '\n'
    'Each entry below is a link a reader cannot open. Known false positives are already '
    'filtered: 429 and 5xx are accepted, loopback URLs are skipped, and hosts that block CI '
    'checkers are excluded in [cplieger/ci .github/lychee.toml]'
    '(https://github.com/cplieger/ci/blob/main/.github/lychee.toml). So treat what remains as '
    'real. Fix the link in this repo; if the URL does open in a browser, add it to that list '
    'with the reason.\n'
    '\n'
)


def build_body(report: bytes, run_url: str) -> str:
    text = report[:REPORT_CAP_BYTES].decode('utf-8', errors='replace').rstrip('\n')
    return PREAMBLE + trackerlib.sentinel_block('links-data', text) + f'\n\n_Run: {run_url}_\n'


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', required=True, type=Path, help="lychee's markdown report")
    p.add_argument('--run-url', required=True, help='URL of the workflow run that produced it')
    args = p.parse_args()
    sys.stdout.write(build_body(args.report.read_bytes(), args.run_url))
    return 0


if __name__ == '__main__':
    sys.exit(main())
