#!/usr/bin/env python3
"""Maintain one automation-owned issue: the transport the scheduled writers share.

The weekly and daily workflows that file findings as issues (bench, fuzz,
gremlins, links, stryker trackers and the notify-failure run tracker) all go
through this script instead of carrying their own `gh issue` shell. One copy
means one answer to the three questions the copies used to disagree on:

- A repo with issues disabled never fails the run. Every mode reads
  `hasIssuesEnabled` first and skips with a `::notice::` when it is false. A
  FAILED read falls through to the issue ops: a loud failure beats a silently
  dropped finding.
- Labels are created on first use from the palette below, so a writer is
  self-installing in any repo (`gh issue create --label` fails on a missing
  label). `auto-generated` is always added to an issue this script creates.
- Any other `gh` failure exits 1 with gh's stderr and writes nothing further.
  Nothing is retried and nothing is swallowed; the caller decides whether the
  repo's failure ends the run.

Modes (`--mode`):

    fetch             write the open issue's body to --body-file; an empty
                      file when there is none
    upsert            edit the open issue's body from --body-file, or create it
    recur             comment --comment-file on the open issue, or create it
                      from --body-file
    close-when-clean  close the open issue with --comment-file's text; no open
                      issue is a no-op
    list              print `number<TAB>title` for each open issue with --label

`fetch`, `upsert` and `close-when-clean` find the issue by --label plus exact
--title; `recur` by exact-title search (a finding's title is its identity, so
the label is not part of the match). After `upsert` or `recur`,
`--flag-label NAME --flag on|off` adds or removes NAME on the issue.

Authentication is gh's: pass the token as GH_TOKEN in the environment. Runs on
the runner's default python3 (3.12).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ALWAYS_LABEL = 'auto-generated'
LIST_LIMIT = '100'
# (color, description) per label this transport creates. Unknown labels get
# the neutral pair, so a caller's typo creates a grey label rather than failing.
LABELS = {
    'auto-generated': ('ededed', 'Maintained by automation'),
    'bench-regression': ('b60205', 'Benchmark regression'),
    'bench-tracker': ('0e8a16', 'Weekly benchmark tracker'),
    'broken-links': ('d93f0b', 'Automated weekly external-link check'),
    'fuzz-finding': ('b60205', 'Fuzz-discovered regression'),
    'gremlins-tracker': ('5319e7', 'Gremlins mutation testing tracker'),
    'mutation-regression': ('b60205', 'Mutation efficacy regression'),
    'stryker-tracker': ('1d76db', 'Stryker mutation testing tracker'),
    'weekly-ci-failure': ('b60205', 'A scheduled CI run failed and needs triage'),
}
DEFAULT_LABEL = ('ededed', 'Maintained by automation')


class GhError(Exception):
    """A gh invocation this script cannot proceed past."""


def gh(*args: str) -> str:
    proc = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f'exit {proc.returncode}'
        raise GhError(f'gh {" ".join(args[:2])} failed: {detail}')
    return proc.stdout


def issues_enabled(repo: str) -> bool:
    proc = subprocess.run(
        ['gh', 'repo', 'view', repo, '--json', 'hasIssuesEnabled', '--jq', '.hasIssuesEnabled'],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            f'could not read the issue setting for {repo}; attempting the issue ops anyway: '
            f'{proc.stderr.strip()}',
            file=sys.stderr,
        )
        return True
    return proc.stdout.strip() == 'true'


def open_issues(repo: str, fields: str, label: str = '', search: str = '') -> list[dict]:
    args = ['issue', 'list', '-R', repo, '--state', 'open', '--limit', LIST_LIMIT, '--json', fields]
    if label:
        args += ['--label', label]
    if search:
        args += ['--search', search]
    return json.loads(gh(*args) or '[]')


def find_open(
    repo: str, title: str, label: str = '', fields: str = 'number,title,labels'
) -> dict | None:
    """The open issue with exactly `title`: within `label` when given, else by title search."""
    if label:
        candidates = open_issues(repo, fields, label=label)
    else:
        candidates = open_issues(repo, fields, search=f'{title} in:title')
    for issue in candidates:
        if issue.get('title') == title:
            return issue
    return None


def label_names(issue: dict) -> set[str]:
    return {entry['name'] for entry in issue.get('labels') or []}


def ensure_label(repo: str, name: str) -> None:
    color, description = LABELS.get(name, DEFAULT_LABEL)
    proc = subprocess.run(
        ['gh', 'label', 'create', name, '-R', repo, '--color', color, '--description', description],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 and 'already exists' not in proc.stderr:
        print(f'label {name!r} not created in {repo}: {proc.stderr.strip()}', file=sys.stderr)


def create(repo: str, title: str, labels: list[str], body_file: Path) -> int:
    for name in labels:
        ensure_label(repo, name)
    url = gh(
        'issue',
        'create',
        '-R',
        repo,
        '--title',
        title,
        '--label',
        ','.join(labels),
        '--body-file',
        str(body_file),
    ).strip()
    number = int(url.rsplit('/', 1)[-1])
    print(f'created #{number} in {repo}: {title}')
    return number


def set_flag(repo: str, number: int, current: set[str], flag_label: str, flag: str) -> None:
    if flag == 'on' and flag_label not in current:
        ensure_label(repo, flag_label)
        gh('issue', 'edit', str(number), '-R', repo, '--add-label', flag_label)
        print(f'flagged #{number} {flag_label}')
    elif flag == 'off' and flag_label in current:
        gh('issue', 'edit', str(number), '-R', repo, '--remove-label', flag_label)
        print(f'unflagged #{number} {flag_label}')


def create_labels(args: argparse.Namespace) -> list[str]:
    labels = [args.label, ALWAYS_LABEL, *args.extra_label]
    if args.flag == 'on' and args.flag_label not in labels:
        labels.append(args.flag_label)
    return list(dict.fromkeys(labels))


def mode_fetch(args: argparse.Namespace) -> int:
    issue = find_open(args.repo, args.title, label=args.label, fields='number,title,body')
    args.body_file.write_text(issue['body'] if issue else '')
    print(f'fetched #{issue["number"]}' if issue else 'no open issue to fetch')
    return 0


def mode_upsert(args: argparse.Namespace) -> int:
    issue = find_open(args.repo, args.title, label=args.label)
    if issue:
        number, current = issue['number'], label_names(issue)
        gh('issue', 'edit', str(number), '-R', args.repo, '--body-file', str(args.body_file))
        print(f'updated #{number} in {args.repo}')
    else:
        labels = create_labels(args)
        number, current = create(args.repo, args.title, labels, args.body_file), set(labels)
    if args.flag_label:
        set_flag(args.repo, number, current, args.flag_label, args.flag)
    return 0


def mode_recur(args: argparse.Namespace) -> int:
    issue = find_open(args.repo, args.title)
    if issue:
        number, current = issue['number'], label_names(issue)
        gh('issue', 'comment', str(number), '-R', args.repo, '--body-file', str(args.comment_file))
        print(f'commented on #{number} in {args.repo}')
    else:
        labels = create_labels(args)
        number, current = create(args.repo, args.title, labels, args.body_file), set(labels)
    if args.flag_label:
        set_flag(args.repo, number, current, args.flag_label, args.flag)
    return 0


def mode_close_when_clean(args: argparse.Namespace) -> int:
    issue = find_open(args.repo, args.title, label=args.label)
    if not issue:
        print(f'no open issue to close in {args.repo}: {args.title}')
        return 0
    comment = args.comment_file.read_text()
    gh(
        'issue',
        'close',
        str(issue['number']),
        '-R',
        args.repo,
        '--reason',
        'completed',
        '--comment',
        comment,
    )
    print(f'closed #{issue["number"]} in {args.repo}')
    return 0


def mode_list(args: argparse.Namespace) -> int:
    for issue in open_issues(args.repo, 'number,title', label=args.label):
        print(f'{issue["number"]}\t{issue["title"]}')
    return 0


MODES = {
    'fetch': mode_fetch,
    'upsert': mode_upsert,
    'recur': mode_recur,
    'close-when-clean': mode_close_when_clean,
    'list': mode_list,
}
NEEDS_TITLE = {'fetch', 'upsert', 'recur', 'close-when-clean'}
NEEDS_BODY = {'fetch', 'upsert', 'recur'}
NEEDS_COMMENT = {'recur', 'close-when-clean'}
TAKES_FLAG = {'upsert', 'recur'}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--repo', required=True, help='OWNER/NAME')
    p.add_argument('--label', required=True, help='the label that identifies this writer')
    p.add_argument('--title', default='', help='exact issue title (every mode but list)')
    p.add_argument('--mode', required=True, choices=sorted(MODES))
    p.add_argument(
        '--body-file', type=Path, help='issue body: read by upsert/recur, written by fetch'
    )
    p.add_argument('--comment-file', type=Path, help='comment text for recur and close-when-clean')
    p.add_argument(
        '--extra-label',
        action='append',
        default=[],
        help='additional label on a created issue (repeatable)',
    )
    p.add_argument('--flag-label', default='', help='label to add or remove after upsert/recur')
    p.add_argument('--flag', choices=['on', 'off'], help='add (on) or remove (off) --flag-label')
    args = p.parse_args(argv)

    if args.mode in NEEDS_TITLE and not args.title:
        p.error(f'--title is required for --mode {args.mode}')
    if args.mode in NEEDS_BODY and args.body_file is None:
        p.error(f'--body-file is required for --mode {args.mode}')
    if args.mode in NEEDS_COMMENT and args.comment_file is None:
        p.error(f'--comment-file is required for --mode {args.mode}')
    if bool(args.flag_label) != bool(args.flag):
        p.error('--flag-label and --flag go together')
    if args.flag_label and args.mode not in TAKES_FLAG:
        p.error(f'--flag-label applies to {", ".join(sorted(TAKES_FLAG))}, not {args.mode}')
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not issues_enabled(args.repo):
        subject = args.title or args.label
        print(
            f'::notice::{args.repo} has issues disabled; skipped {args.mode} for {subject}',
            file=sys.stderr,
        )
        if args.mode == 'fetch':
            args.body_file.write_text('')
        return 0
    try:
        return MODES[args.mode](args)
    except GhError as err:
        print(f'::error::{args.repo}: {err}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
