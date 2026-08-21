#!/usr/bin/env python3
"""Re-point a benchmark data point at the commit that was actually benchmarked.

weekly-bench is Pattern C: the benchmark action runs in cplieger/ci while
measuring a consumer, and it resolves the repo it is acting on from
`github.context.repo`. That is read from GITHUB_REPOSITORY, which GitHub
reserves and a workflow cannot overwrite, so every data point it writes names a
cplieger/ci commit and every chart links back to ci. A chart that cannot name
the change that made something slower is the one thing this tracker exists for.

So the workflow repairs the record after the action publishes it. This script
rewrites the entry the run just appended, taking the real commit metadata from
the consumer's own API response and mirroring the shape the action itself builds
in dist/src/extract.js, so the chart renders identically.

It is deliberately strict. The one real cost of editing another tool's output is
that a format change downstream turns into silent corruption, so every
assumption is asserted and a surprise is a loud non-zero exit, never a
best-effort write. Assumptions, all verified against the pinned v1.22.1 dist:

  * the file is `window.BENCHMARK_DATA = ` + JSON.stringify(data, null, 2),
    with no trailing semicolon and no trailing newline (dist/src/write.js:49,69)
  * the run's own entry is the LAST element of each suite in `entries`
  * that entry currently carries the ci commit, which is what --expect-commit
    pins; anything else means the behaviour changed and we must not touch it

Reads no network. The caller fetches the commit and passes it in, which is also
what makes this testable against a real published file.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

PREFIX = 'window.BENCHMARK_DATA = '


class FormatError(RuntimeError):
    """The published data does not match what this script is allowed to edit."""


def load(path: pathlib.Path) -> dict:
    raw = path.read_text(encoding='utf-8')
    if not raw.startswith(PREFIX):
        raise FormatError(
            f"{path} does not start with {PREFIX!r}; the action's output format "
            'changed and this repair is no longer safe'
        )
    try:
        data = json.loads(raw[len(PREFIX) :])
    except json.JSONDecodeError as err:
        raise FormatError(f'{path} payload is not valid JSON: {err}') from err
    if not isinstance(data, dict) or not isinstance(data.get('entries'), dict):
        raise FormatError(f"{path} has no 'entries' object")
    return data


def dump(path: pathlib.Path, data: dict) -> None:
    # Must reproduce JSON.stringify(data, null, 2) byte for byte, so that a run
    # which changes nothing produces no diff: 2-space indent, no space before
    # the separator colon's value beyond one, and no ASCII escaping.
    path.write_text(PREFIX + json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def commit_from_api(payload: dict) -> dict:
    """Build the action's Commit shape from a repos/{owner}/{repo}/commits/{ref} body.

    Mirrors getCommitFromGitHubAPIRequest in dist/src/extract.js so the rewritten
    entry is indistinguishable from one the action would have written itself.
    """
    commit = payload.get('commit') or {}
    author = commit.get('author') or {}
    committer = commit.get('committer') or {}
    sha = payload.get('sha')
    if not sha:
        raise FormatError("commit payload carries no 'sha'")
    return {
        'author': {
            'name': author.get('name'),
            'username': (payload.get('author') or {}).get('login'),
            'email': author.get('email'),
        },
        'committer': {
            'name': committer.get('name'),
            'username': (payload.get('committer') or {}).get('login'),
            'email': committer.get('email'),
        },
        'id': sha,
        'message': commit.get('message'),
        'timestamp': author.get('date'),
        'url': payload.get('html_url'),
    }


def repair(data: dict, expect: str, replacement: dict, repo_url: str) -> list[str]:
    """Re-point the last entry of every suite. Returns the suite names touched."""
    touched = []
    for suite, entries in data['entries'].items():
        if not isinstance(entries, list) or not entries:
            raise FormatError(f'suite {suite!r} holds no entries')
        latest = entries[-1]
        found = (latest.get('commit') or {}).get('id')
        if found != expect:
            raise FormatError(
                f'suite {suite!r}: newest entry names commit {found!r}, expected '
                f'{expect!r}. Refusing to rewrite - either this run appended '
                'nothing, or the action no longer appends to the end.'
            )
        latest['commit'] = replacement
        touched.append(suite)
    if not touched:
        raise FormatError("no suites in 'entries'; nothing was published")
    data['repoUrl'] = repo_url
    return touched


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-file', required=True, type=pathlib.Path)
    ap.add_argument(
        '--expect-commit',
        required=True,
        help="the commit the action wrongly recorded (the ci run's own SHA)",
    )
    ap.add_argument(
        '--commit-json',
        required=True,
        type=pathlib.Path,
        help='repos/{owner}/{repo}/commits/{sha} response for the benchmarked commit',
    )
    ap.add_argument('--repo-url', required=True, help='consumer repo html_url, for data.repoUrl')
    args = ap.parse_args()

    try:
        data = load(args.data_file)
        replacement = commit_from_api(json.loads(args.commit_json.read_text(encoding='utf-8')))
        touched = repair(data, args.expect_commit, replacement, args.repo_url)
    except (FormatError, json.JSONDecodeError, OSError) as err:
        print(f'bench-fix-attribution: {err}', file=sys.stderr)
        return 1

    dump(args.data_file, data)
    print(
        f're-pointed {len(touched)} suite(s) {touched} from {args.expect_commit[:12]} '
        f'to {replacement["id"][:12]}; repoUrl={args.repo_url}'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
