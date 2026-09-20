#!/usr/bin/env python3
"""Delete aged dev-channel versions from the owner's GHCR container packages.

Candidates are the versions is_candidate accepts; the newest KEEP_NEWEST
survive at any age and the rest go once older than MAX_AGE_DAYS. Needs a
GH_TOKEN with read:packages and delete:packages; `--dry-run` deletes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta

import release_channels as rc

OWNER = 'cplieger'
KEEP_NEWEST = 10
MAX_AGE_DAYS = 30
SHA_TAG_RE = re.compile(r'^sha-[0-9a-f]{40}$')


def gh(*args: str) -> str:
    proc = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f'exit {proc.returncode}'
        raise RuntimeError(f'gh {" ".join(args[:3])} failed: {detail}')
    return proc.stdout


def gh_paginate(path: str) -> list[dict]:
    out = gh('api', '--paginate', '--slurp', path)
    pages = json.loads(out or '[]')
    return [item for page in pages for item in page]


def version_tags(version: dict) -> list[str]:
    return ((version.get('metadata') or {}).get('container') or {}).get('tags') or []


def created_at(version: dict) -> datetime:
    return datetime.fromisoformat(version['created_at'])


def is_candidate(version: dict) -> bool:
    """Tagged, every tag a dev version or a `sha-<commit>` tag, at least one a
    dev version. Untagged versions are the per-architecture manifests live
    indexes reference, and the API cannot tell one from an orphan."""
    tags = version_tags(version)
    if not tags:
        return False
    if not all(rc.is_dev_tag(t) or SHA_TAG_RE.match(t) for t in tags):
        return False
    return any(rc.is_dev_tag(t) for t in tags)


def select_deletions(
    versions: list[dict],
    now: datetime,
    keep_newest: int = KEEP_NEWEST,
    max_age_days: int = MAX_AGE_DAYS,
) -> list[dict]:
    """The versions to delete: candidates beyond the newest `keep_newest`, older
    than `max_age_days` (a version exactly that old is kept)."""
    candidates = sorted((v for v in versions if is_candidate(v)), key=created_at, reverse=True)
    cutoff = timedelta(days=max_age_days)
    return [v for v in candidates[keep_newest:] if now - created_at(v) > cutoff]


def list_packages() -> list[str]:
    return sorted(
        p['name']
        for p in gh_paginate(f'users/{OWNER}/packages?package_type=container&per_page=100')
    )


def package_path(name: str) -> str:
    return f'users/{OWNER}/packages/container/{urllib.parse.quote(name, safe="")}'


def list_versions(name: str) -> list[dict]:
    return gh_paginate(f'{package_path(name)}/versions?per_page=100')


def delete_version(name: str, version_id: int) -> None:
    gh('api', '-X', 'DELETE', f'{package_path(name)}/versions/{version_id}')


def age_days(version: dict, now: datetime) -> int:
    return (now - created_at(version)).days


def render_table(rows: list[tuple[str, int, str, int, str]]) -> str:
    lines = [
        '## GHCR dev-channel retention',
        '',
        '| Package | Version id | Tags | Age (days) | Action |',
        '|---|---|---|---|---|',
    ]
    lines += [f'| {p} | {i} | {t} | {a} | {act} |' for p, i, t, a, act in rows]
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--dry-run', action='store_true', help='list the decisions, delete nothing')
    args = p.parse_args(argv)

    now = datetime.now(UTC)
    rows: list[tuple[str, int, str, int, str]] = []
    deleted = 0
    for name in list_packages():
        versions = list_versions(name)
        doomed = select_deletions(versions, now)
        doomed_ids = {v['id'] for v in doomed}
        for v in sorted((v for v in versions if is_candidate(v)), key=created_at, reverse=True):
            action = 'keep'
            if v['id'] in doomed_ids:
                action = 'would delete' if args.dry_run else 'delete'
                if not args.dry_run:
                    delete_version(name, v['id'])
                    deleted += 1
            rows.append((name, v['id'], ', '.join(version_tags(v)), age_days(v, now), action))
    table = render_table(rows)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as fh:
            fh.write(table)
    print(table, end='')
    print(
        f'{"would delete" if args.dry_run else "deleted"}: {len([r for r in rows if r[4] != "keep"])}'
        f'{"" if args.dry_run else f" ({deleted} done)"}'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
