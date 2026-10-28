#!/usr/bin/env python3
"""Tell whether the resident Renovate still opens pull requests.

The resident Renovate container authenticates as a GitHub App and opens its
pull requests from `renovate/` branches. Whatever stops the deployment (a
broken image, a rejected App key, a stopped container), no such pull request
opens in any of the owner's repositories, so a window with none is the alarm.
The branch filter matters: this repository's sync and rebuild workflows open
pull requests as the same App, and they would keep a dead Renovate looking
alive. `renovate-heartbeat.yaml` runs this daily and keeps one issue open while
the window stays empty.

It prints `stalled` or `alive`, and writes the issue body to --body-file when
stalled or the closing comment to --comment-file when alive. A search GitHub
marks incomplete that finds nothing proves nothing, so it exits 1 instead.

Every call is REST through scripts/ghrest.py; authentication is gh's GH_TOKEN.
Stdlib only; runs on the runner's system python3 (3.12).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import ghrest


def search_prs(client: ghrest.Client, query: str) -> dict:
    """The newest pull request matching `query` and the total count."""
    path = f'search/issues?q={quote(query)}&sort=created&order=desc&per_page=1'
    return json.loads(client.request('GET', path).body)


def describe(item: dict) -> str:
    """`[owner/repo#7](url), "title", opened <time>` for one search item."""
    repo = '/'.join(item['repository_url'].rsplit('/', 2)[-2:])
    return f'[{repo}#{item["number"]}]({item["html_url"]}), "{item["title"]}", opened {item["created_at"]}'


CHECKLIST = (
    'Check, in order:\n\n'
    '1. The container is running and healthy, and its log shows `renovate run complete` within the last run interval.\n'
    '2. `renovate run failed` lines and their `likely_cause`.\n'
    "3. Renovate's `Repository finished` lines whose `result` is not `done`.\n"
    "4. The GitHub App's private key and its installation."
)


def stalled_body(author, owner, hours, since, newest, run_url) -> str:
    seen = (
        f'Its newest pull request is {describe(newest)}.'
        if newest
        else 'GitHub search finds no pull request by it at all.'
    )
    return '\n\n'.join(
        [
            f'`{author}` has opened no pull request from a `renovate/` branch in any `{owner}` repository in the last {hours} hours, since {since}. {seen}',
            'This watches the resident Renovate deployment, which runs docker-renovate-scheduler with a GitHub App token, so it notices the deployment stopping whatever the cause. It cannot see a fault that only another authentication mode meets: the image smoke test covers clone URLs that carry a token.',
            CHECKLIST,
            'This issue is checked daily and closes once the App opens a pull request again.',
            f'Run: {run_url}',
        ]
    )


def main(argv=None, client=None, now=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--author', required=True, help='search author, such as app/<slug>')
    parser.add_argument('--owner', required=True, help='the account whose repositories count')
    parser.add_argument('--hours', type=int, default=48)
    parser.add_argument('--run-url', required=True)
    parser.add_argument('--body-file', type=Path, required=True)
    parser.add_argument('--comment-file', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.hours < 1:
        parser.error('--hours must be at least 1')
    client = client or ghrest.Client()
    now = now or datetime.now(UTC)
    since = (now - timedelta(hours=args.hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    # `head:renovate` matches head branches that begin with the word renovate.
    base = f'is:pr author:{args.author} user:{args.owner} head:renovate'

    try:
        recent = search_prs(client, f'{base} created:>={since}')
        if recent['total_count'] > 0:
            newest = f', the newest {describe(recent["items"][0])}' if recent['items'] else ''
            args.comment_file.write_text(
                f'`{args.author}` opened {recent["total_count"]} pull requests in the last {args.hours} hours'
                f'{newest}. Closing.\n'
            )
            print('alive')
            return 0
        if recent.get('incomplete_results'):
            print(
                f'::error::GitHub marked the search for {base} incomplete, so this run gives no verdict',
                file=sys.stderr,
            )
            return 1
        ever = search_prs(client, base)
        if ever.get('incomplete_results') and not ever['items']:
            print(
                f'::error::GitHub marked the search for {base} incomplete, so this run gives no verdict',
                file=sys.stderr,
            )
            return 1
        newest = ever['items']
    except ghrest.ApiError as err:
        print(f'::error::pull request search failed: {err}', file=sys.stderr)
        return 1
    body = stalled_body(
        args.author, args.owner, args.hours, since, newest[0] if newest else None, args.run_url
    )
    args.body_file.write_text(body + '\n')
    print('stalled')
    return 0


if __name__ == '__main__':
    sys.exit(main())
