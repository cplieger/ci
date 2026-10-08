#!/usr/bin/env python3
"""Hourly maintenance of `main` in every repository whose default branch is `dev`.

Subcommands, run in this order by release-maintenance.yaml, each with its own token:

    plan    reads every repository and writes the plan; it writes nothing else.
    merge   (SYNC_PAT) merges the planned pull requests at the head the plan read,
            and opens the planned rebuild pull requests.
    report  (CI_SCHEDULE) ticks the dashboards, syncs each `Release blocked` issue,
            and exits 1 when any read or write failed.

`merge-checked` and `rebuild-refreshes` serve other workflows; see their --help.
Decisions come from metadata only, never pull-request content, and a failed read or
planned action never closes an issue. Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import re
import shlex
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import ghrest
import release_channels as rc
import scan_coverage
import tracker_issue
import trackerlib

OWNER = 'cplieger'
VALIDATE_CHECK = 'ci / validate'
ACTIONS_APP_ID = 15368
SCAN_ARTIFACT = 'security-main'
SCAN_WORKFLOW = '.github/workflows/security.yml'
SCAN_EVENTS = frozenset({'schedule', 'workflow_dispatch'})
SCAN_MAX_AGE = dt.timedelta(hours=36)
SHIP_WINDOW = dt.timedelta(hours=3)
REBUILD_RETRY_WINDOW = dt.timedelta(hours=24)
# Must match the `main` group rule's `schedule: ["* 0-5 * * 6"]` and timezone in
# cplieger/.github two-branch.json: a group PR opened outside it was expedited.
SCHEDULE_TZ = ZoneInfo('Europe/Paris')
SCHEDULE_WEEKDAY, SCHEDULE_HOURS = 5, range(6)
DEADLINE_WEEKDAY, DEADLINE_HOUR = 5, 12
ISSUE_TITLE = 'Release blocked'
ISSUE_LABEL = 'release-blocked'
NOTES_DEFAULT = 'Anything written below this heading is kept when the issue is updated.'
PER_PAGE = 100
PAGE_CAP = 10
SECTION_CAP = 30
INSTALL_SUBCOMMANDS = {
    'apk': frozenset({'add', 'upgrade'}),
    'apt-get': frozenset({'install', 'upgrade', 'dist-upgrade'}),
}
# Options that take their value as the next word, read from each tool's getopt string
# (GNU coreutils, findutils and time, sudo, bash builtins), so the value is not taken
# for the command or the subcommand.
VALUE_OPTIONS = {
    'apk': frozenset(
        {'-X', '--repository', '-p', '--root', '--arch', '--cache-dir', '--keys-dir'}
        | {'--repositories-file', '--timeout', '--wait', '--progress-fd', '--cache-max-age'}
    ),
    'apt-get': frozenset(
        {'-o', '--option', '-c', '--config-file', '-t', '--target-release', '--default-release'}
        | {'-a', '--host-architecture'}
    ),
    'command': frozenset(),
    'env': frozenset({'-u', '--unset', '-C', '--chdir', '-a', '--argv0', '-S', '--split-string'}),
    'exec': frozenset({'-a'}),
    'nice': frozenset({'-n', '--adjustment'}),
    'nohup': frozenset(),
    'sudo': frozenset(
        {'-a', '--auth-type', '-C', '--close-from', '-c', '--login-class', '-D', '--chdir'}
        | {'-g', '--group', '-p', '--prompt', '-R', '--chroot', '-r', '--role', '-T'}
        | {'--command-timeout', '-t', '--type', '-U', '--other-user', '-u', '--user'}
    ),
    'time': frozenset({'-f', '--format', '-o', '--output'}),
    'xargs': frozenset(
        {'-a', '--arg-file', '-d', '--delimiter', '-E', '-I', '-L', '-n', '--max-args'}
        | {'-P', '--max-procs', '-s', '--max-chars', '--process-slot-var'}
    ),
}
WRAPPERS = frozenset(VALUE_OPTIONS) - frozenset({'apk', 'apt-get'})
# A single-dash word is a getopt cluster: at its first letter that takes a value, the
# rest of the word, or the next word when nothing is left, is that value.
SHORT_VALUE_LETTERS = {
    tool: frozenset(o[1] for o in opts if len(o) == 2 and o[1] != '-')
    for tool, opts in VALUE_OPTIONS.items()
}
# docker-release.yaml's `declares` grep, which decides whether a build gets PKG_REFRESH.
RELEASE_DECLARES = re.compile(
    r'^[ \t\r\f\v]*ARG[ \t\r\f\v]+PKG_REFRESH(?:[ \t\r\f\v=]|$)', re.MULTILINE
)
SHELLS = frozenset({'sh', 'bash', 'ash', 'dash'})
SHELL_KEYWORDS = frozenset({'!', '{', 'if', 'then', 'else', 'elif', 'do', 'while', 'until'})
ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
REDIRECT = re.compile(r'\d*[<>]')
ARG_REF = re.compile(r'\$(?:\{([A-Za-z_]\w*)(?:(:[-+])([^}]*))?\}|([A-Za-z_]\w*))')
CONTINUATION = re.compile(r'\\[ \t]*\n')
FROM_LINE = re.compile(r'\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$', re.IGNORECASE)
CHECKBOX = re.compile(r'^ - \[( |x)\] <!-- ([a-zA-Z]+)-branch=(\S+) -->(.*)$')
PACKAGE_LIST = re.compile(r'\((`[^`]+`(?:, `[^`]+`)*)\)\s*$')
REPO_NAME = re.compile(r'[A-Za-z0-9._-]+')
VERSION = re.compile(r'(\d+)(?:\.(\d+))?(?:\.(\d+))?')
IN_FLIGHT_HEADINGS = frozenset({'Pending Status Checks', 'Other Branches', 'Open'})
SHAPE_ERRORS = (KeyError, IndexError, TypeError, AttributeError, ValueError)
SECTIONS = (
    ('weekly', 'The Saturday update did not ship by 12:00 UTC'),
    ('late', 'A security, expedited or rebuild pull request did not ship within three hours'),
    ('unrouted', 'A fixable vulnerability on `main` has no automatic route'),
)


class GhError(Exception):
    """A gh call that failed; the message carries the API's or gh's error."""


def gh_command(run, args: list[str]) -> bytes:
    """A non-API `gh` command through `run` (ghrest's process contract); GhError
    on a non-zero exit."""
    proc = run(args, None)
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors='replace').strip() or f'exit {proc.returncode}'
        raise GhError(f'gh {" ".join(args[:3])}: {" ".join(detail.split())}')
    return proc.stdout


def run_gh(args: list[str]) -> bytes:
    return gh_command(ghrest.run_process, args)


class Api:
    """REST through scripts/ghrest.py, so the shared GraphQL budget is not spent
    on reads; every API failure is a GhError. `run` also carries the non-API
    commands (`command`), so one fake answers both."""

    def __init__(self, run=ghrest.run_process):
        self.run = run
        self.rest = ghrest.Client(run=run)

    @staticmethod
    def _call(fn, *args):
        try:
            return fn(*args)
        except ghrest.ApiError as err:
            raise GhError(str(err)) from None

    def get(self, path: str):
        return self._call(self.rest.get, path)

    def get_or_none(self, path: str):
        return self._call(self.rest.get_or_none, path)

    def raw(self, path: str) -> bytes:
        return self._call(self.rest.request, 'GET', path).body

    def patch(self, path: str, body: dict) -> None:
        self._call(self.rest.send, 'PATCH', path, body)

    def put(self, path: str, body: dict) -> None:
        self._call(self.rest.send, 'PUT', path, body)

    def delete_branch(self, repo: str, ref: str) -> None:
        """Deletes `ref` of OWNER/`repo`; a branch already gone (deleted on merge) is
        no error, which GitHub answers with 422."""
        try:
            self.rest.request('DELETE', f'repos/{OWNER}/{repo}/git/refs/heads/{ref}')
        except ghrest.ApiError as err:
            if err.status != 422:
                raise GhError(str(err)) from None

    def pages(self, path: str, key: str | None = None, stop=None) -> list:
        """Every item of a paged listing, read until a short page or until `stop(item)`
        holds for an item; GhError when PAGE_CAP full pages did not reach the end."""
        return self._call(self.rest.pages, path, key, PER_PAGE, PAGE_CAP, stop)

    def command(self, args: list[str]) -> bytes:
        return gh_command(self.run, args)


def ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def shown(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime('%Y-%m-%d %H:%M UTC')


# ── Schedule ──────────────────────────────────────────────────────────────────


def in_group_window(created: dt.datetime) -> bool:
    local = created.astimezone(SCHEDULE_TZ)
    return local.weekday() == SCHEDULE_WEEKDAY and local.hour in SCHEDULE_HOURS


def saturday_deadline(after: dt.datetime) -> dt.datetime:
    """The first Saturday 12:00 UTC strictly after `after`."""
    day = after.astimezone(dt.UTC).date()
    day += dt.timedelta(days=(DEADLINE_WEEKDAY - day.weekday()) % 7)
    deadline = dt.datetime(day.year, day.month, day.day, DEADLINE_HOUR, tzinfo=dt.UTC)
    return deadline if deadline > after else deadline + dt.timedelta(days=7)


# ── Discovery ─────────────────────────────────────────────────────────────────


def discover(api: Api, only: set[str]) -> list[str]:
    repos = api.pages('user/repos?affiliation=owner')
    names = sorted(r['name'] for r in repos if rc.is_two_branch(r))
    return [n for n in names if not only or n in only]


# ── Pull requests ─────────────────────────────────────────────────────────────


def own_head(pr: dict, repo: str) -> bool:
    return ((pr.get('head') or {}).get('repo') or {}).get('full_name') == f'{OWNER}/{repo}'


def labels_of(pr: dict) -> set[str]:
    return {label['name'] for label in pr.get('labels') or []}


def validate_green(api: Api, repo: str, sha: str) -> bool:
    """Whether every latest `ci / validate` check run from GitHub Actions at `sha`
    concluded success; a run from any other app never counts."""
    runs = api.get(
        f'repos/{OWNER}/{repo}/commits/{sha}/check-runs'
        f'?check_name={quote(VALIDATE_CHECK, safe="")}&filter=latest&per_page={PER_PAGE}'
    )['check_runs']
    ours = [r for r in runs if (r.get('app') or {}).get('id') == ACTIONS_APP_ID]
    return bool(ours) and all(
        r['status'] == 'completed' and r['conclusion'] == 'success' for r in ours
    )


def plan_security(api: Api, repo: str, open_prs: list[dict]) -> tuple[list[dict], list[str]]:
    """(the pull requests to merge, summary notes) from labels and check runs only."""
    merge, notes = [], []
    for pr in open_prs:
        base, head = pr['base']['ref'], pr['head']['ref']
        allowed = labels_of(pr) & rc.SECURITY_AUTOMERGE_LABELS
        if base not in ('dev', 'main') or not head.startswith('renovate/') or not allowed:
            continue
        if not own_head(pr, repo) or pr.get('draft') or 'security-major' in labels_of(pr):
            continue
        if pr.get('auto_merge'):
            notes.append(f'#{pr["number"]} into {base}: auto-merge already armed')
            continue
        sha = pr['head']['sha']
        if not validate_green(api, repo, sha):
            notes.append(
                f'#{pr["number"]} into {base}: `{VALIDATE_CHECK}` is not green at {sha[:12]}'
            )
            continue
        merge.append({'number': pr['number'], 'base': base, 'sha': sha, 'labels': sorted(allowed)})
    return merge, notes


def main_kind(pr: dict, repo: str) -> str | None:
    """`group`, `security` or `rebuild` for a machine pull request into `main`."""
    if pr['base']['ref'] != 'main' or not own_head(pr, repo):
        return None
    head = pr['head']['ref']
    if head == rc.MAIN_GROUP_BRANCH:
        return 'group'
    if rc.main_intake_kind(head) == 'rebuild':
        return 'rebuild'
    if head.startswith('renovate/') and 'security' in labels_of(pr):
        return 'security'
    return None


class Shipping:
    """Whether a merge into `main` has shipped: the newest successful release run on
    `main` was created after it and its head contains the merge commit."""

    def __init__(self, api: Api, repo: str):
        self.api, self.repo = api, repo
        workflow = 'publish.yaml' if repo in rc.OWN_PUBLISH_REPOS else 'release.yaml'
        runs = api.get(
            f'repos/{OWNER}/{repo}/actions/workflows/{workflow}/runs'
            '?branch=main&status=success&per_page=1'
        )['workflow_runs']
        self.latest = runs[0] if runs else None

    @property
    def since(self) -> dt.datetime | None:
        return ts(self.latest['created_at']) if self.latest else None

    @property
    def finished(self) -> dt.datetime | None:
        return ts(self.latest['updated_at']) if self.latest else None

    def shipped(self, pr: dict) -> bool:
        if not self.latest or ts(pr['merged_at']) > self.since:
            return False
        compare = self.api.get(
            f'repos/{OWNER}/{self.repo}/compare/{pr["merge_commit_sha"]}...{self.latest["head_sha"]}'
        )
        return compare['status'] in ('ahead', 'identical')


def merged_main_prs(api: Api, repo: str, since: dt.datetime | None) -> list[dict]:
    """Merged machine pull requests into `main` that the newest successful release
    run may not contain, newest first. With no successful run every one may be
    unshipped, so the whole listing is read; GhError when it exceeds PAGE_CAP."""
    cutoff = since - dt.timedelta(days=1) if since else None
    closed = api.pages(
        f'repos/{OWNER}/{repo}/pulls?state=closed&base=main&sort=updated&direction=desc',
        stop=(lambda pr: ts(pr['updated_at']) < cutoff) if cutoff else None,
    )
    merged = [pr for pr in closed if pr.get('merged_at') and main_kind(pr, repo)]
    return sorted(merged, key=lambda pr: pr['merged_at'], reverse=True)


def due_of(pr: dict, kind: str) -> tuple[str, dt.datetime]:
    """(section, deadline) of a machine pull request into `main`."""
    created = ts(pr['created_at'])
    if kind == 'group' and in_group_window(created):
        return 'weekly', saturday_deadline(created)
    return 'late', created + SHIP_WINDOW


def describe(pr: dict, kind: str, section: str) -> str:
    if kind == 'group':
        kind = 'Saturday group' if section == 'weekly' else 'expedited group'
    return f'#{pr["number"]} ({kind}, `{pr["head"]["ref"]}`)'


class PullState:
    """The machine pull requests into `main` and what has shipped."""

    def __init__(self, api: Api, repo: str, open_prs: list[dict]):
        self.shipping = Shipping(api, repo)
        self.open = [pr for pr in open_prs if main_kind(pr, repo)]
        self.merged = merged_main_prs(api, repo, self.shipping.since)
        self.unshipped = [pr for pr in self.merged if not self.shipping.shipped(pr)]
        self.repo = repo

    def newest(self, prs: list[dict], kind: str) -> dict | None:
        return next((pr for pr in prs if main_kind(pr, self.repo) == kind), None)

    def blockers(self, now: dt.datetime) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {'weekly': [], 'late': []}
        for pr in self.open:
            kind = main_kind(pr, self.repo)
            section, due = due_of(pr, kind)
            if now < due:
                continue
            held = ''
            if kind == 'security' and not labels_of(pr) & rc.SECURITY_AUTOMERGE_LABELS:
                held = ' Its labels allow no automatic merge, so it waits for you.'
            out[section].append(
                f'{describe(pr, kind, section)} is still open. It was due by {shown(due)}.{held}'
            )
        for pr in self.unshipped:
            kind = main_kind(pr, self.repo)
            section, due = due_of(pr, kind)
            if now < due:
                continue
            out[section].append(
                f'{describe(pr, kind, section)} merged at {shown(ts(pr["merged_at"]))}, and no release '
                f'run on `main` has succeeded since. It was due by {shown(due)}.'
            )
        return out


# ── The scan of main and its routes ──────────────────────────────────────────


def read_scan(api: Api, repo: str) -> tuple[dict, dict] | None:
    """(the newest scan record of `main` from a scheduled or dispatched security.yml
    run of this repository, that run), or None when there is none."""
    listing = api.get(
        f'repos/{OWNER}/{repo}/actions/artifacts?name={SCAN_ARTIFACT}&per_page={PER_PAGE}'
    )['artifacts']
    for art in listing:
        origin = art.get('workflow_run') or {}
        if art.get('expired') or origin.get('head_branch') != 'main':
            continue
        run = api.get(f'repos/{OWNER}/{repo}/actions/runs/{origin["id"]}')
        if (
            run.get('path') != SCAN_WORKFLOW
            or run.get('event') not in SCAN_EVENTS
            or (run.get('head_repository') or {}).get('full_name') != f'{OWNER}/{repo}'
        ):
            continue
        blob = api.raw(f'repos/{OWNER}/{repo}/actions/artifacts/{art["id"]}/zip')
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                doc = json.loads(zf.read('security-main.json'))
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise GhError(f'artifact {art["id"]}: not a {SCAN_ARTIFACT} record: {exc}') from None
        if (
            not isinstance(doc, dict)
            or doc.get('schema') != 1
            or doc.get('repo') != f'{OWNER}/{repo}'
            or doc.get('commit') != run.get('head_sha')
            or not isinstance(doc.get('findings'), list)
        ):
            raise GhError(f'artifact {art["id"]}: the record does not describe run {run["id"]}')
        return doc, run
    return None


def version_tuple(text: str) -> tuple[int, int, int] | None:
    m = VERSION.search(text or '')
    return tuple(int(g or 0) for g in m.groups()) if m else None


def fix_level(finding: dict) -> str:
    """`ok` when a listed fixed version at or above the installed one stays on its
    line (the same major, and for the Go standard library the same minor), else
    `go-minor` or `major`; `unknown` when no listed version compares."""
    installed = version_tuple(finding.get('installed') or '')
    fixes = [version_tuple(v) for v in re.split(r'[,\s]+', finding.get('fixed') or '') if v]
    if installed is None or not fixes or None in fixes:
        return 'unknown'
    width = 2 if finding['class'] == 'stdlib' else 1
    newer = [f for f in fixes if f >= installed]
    if any(f[:width] == installed[:width] for f in newer):
        return 'ok'
    if width == 2 and any(f[:1] == installed[:1] for f in newer):
        return 'go-minor'
    return 'major' if newer else 'unknown'


def parse_dashboard(body: str) -> dict[str, dict]:
    """{branch: {checked, kind, heading, line, packages}} for every checkbox Renovate
    writes as ` - [ ] <!-- <kind>-branch=<branch> -->`; GhError when a branch is
    listed twice, so an ambiguous body is never edited."""
    entries: dict[str, dict] = {}
    heading = ''
    for line in body.replace('\r\n', '\n').split('\n'):
        if line.startswith('## '):
            heading = line[3:].strip()
            continue
        m = CHECKBOX.match(line)
        if not m:
            continue
        mark, kind, branch, tail = m.groups()
        if branch in entries:
            raise GhError(f'the Dependency Dashboard lists {branch} twice')
        names = PACKAGE_LIST.search(tail)
        entries[branch] = {
            'checked': mark == 'x',
            'kind': kind,
            'heading': heading,
            'line': line,
            'packages': [n.strip('`') for n in names.group(1).split(', ')] if names else None,
        }
    return entries


def read_dashboard(api: Api, repo: str) -> dict | None:
    """The open Dependency Dashboard Renovate authored; GhError when there are two,
    since ticking either could be the one Renovate no longer reads."""
    issues = api.pages(f'repos/{OWNER}/{repo}/issues?state=open&labels=renovate')
    boards = [
        issue
        for issue in issues
        if issue.get('title') == 'Dependency Dashboard'
        and 'pull_request' not in issue
        and ((issue.get('user') or {}).get('login') or '') in rc.RENOVATE_AUTHORS
    ]
    if len(boards) > 1:
        numbers = ', '.join(f'#{issue.get("number")}' for issue in boards)
        raise GhError(f'more than one open Dependency Dashboard: {numbers}')
    return boards[0] if boards else None


def finding_text(f: dict) -> str:
    where = ', '.join(f.get('platforms') or f.get('targets') or [])
    return f'`{f["id"]}` in `{f["package"]}` {f.get("installed") or "?"}, fixed in {f["fixed"]}' + (
        f' ({where})' if where else ''
    )


UNROUTED = {
    'major': 'the fix is a major update, which reaches `main` only through a promotion',
    'go-minor': 'the fix needs a new Go minor release, which reaches `main` only through a promotion',
    'unknown': 'the fixed version cannot be compared with the installed one, so nothing was expedited',
    'not-in-group': 'the `main` group does not update this package',
    'no-package-list': (
        'the Dependency Dashboard names no packages for the `main` group, which Renovate '
        'does when the group holds fewer than two, so nothing was expedited'
    ),
    'no-group': 'the Dependency Dashboard lists no waiting `main` group to expedite',
    'no-rebase': 'the Dependency Dashboard offers no rebase checkbox for the open `main` group',
    'no-dashboard': 'the repository has no Dependency Dashboard to expedite through',
    'unknown-base': (
        "the final image's base is named by an ARG with no default, "
        'so no `main` group update is known to reach this package'
    ),
    'rebuild-shipped': (
        'a rebuild of `main` shipped before this scan and the scan still finds it, '
        'so the base packages carry no fix yet'
    ),
    'no-refresh': (
        'the image installs packages, but no install runs after an `ARG PKG_REFRESH` '
        'line of its stage or of a stage it is built from, so a rebuild would reuse the '
        'cached package layer'
    ),
}


def build_stages(
    dockerfile: str,
) -> list[tuple[str, str, list[tuple[str, bool]], bool, list[str]]]:
    """(base, alias, [(the shell script of a RUN, whether PKG_REFRESH reaches it)],
    whether the stage declares PKG_REFRESH, the stages or images it copies or mounts
    from) per stage, read from the instructions Docker runs; a base names a global ARG's
    default in place of its reference. BuildKit puts a stage ARG's value in the
    environment, and so the cache key, of every later RUN of that stage and of the
    stages built FROM it; a global ARG reaches FROM lines only. The release build
    passes PKG_REFRESH only when RELEASE_DECLARES matches."""
    stages: list[tuple[str, str, list[tuple[str, bool]], bool, list[str]]] = []
    args: dict[str, str | None] = {}
    passed = RELEASE_DECLARES.search(dockerfile) is not None
    for keyword, text in scan_coverage.instructions(dockerfile):
        joined = CONTINUATION.sub(' ', text)
        if keyword == 'ARG' and not stages:
            args.update(arg_defaults(joined))
        elif keyword == 'ARG':
            base, alias, runs, declares, sources = stages[-1]
            declares = declares or (passed and 'PKG_REFRESH' in arg_defaults(joined))
            stages[-1] = (base, alias, runs, declares, sources)
        elif keyword == 'FROM':
            m = FROM_LINE.fullmatch(' '.join(joined.split()))
            if m:
                base = substitute_args(m[1], args).lower()
                stages.append((base, (m[2] or '').lower(), [], False, []))
        elif keyword in ('COPY', 'RUN') and stages:
            stages[-1][4].extend(substitute_args(s, args).lower() for s in from_flags(joined))
            if keyword == 'RUN':
                stages[-1][2].append((run_script(joined), stages[-1][3]))
    return stages


def from_flags(instruction: str) -> list[str]:
    """The `--from=` of a COPY and the `from=` of each RUN `--mount`."""
    out = []
    for word in instruction.split('\n', 1)[0].split()[1:]:
        if not word.startswith('--'):
            break
        flag, _, value = word.partition('=')
        if flag == '--from':
            out.append(value)
        elif flag == '--mount':
            out += [v for k, _, v in (f.partition('=') for f in value.split(',')) if k == 'from']
    return out


def arg_defaults(instruction: str) -> dict[str, str | None]:
    try:
        words = shlex.split(instruction)[1:]
    except ValueError:
        return {}
    pairs = (w.partition('=') for w in words)
    return {name: value if eq else None for name, eq, value in pairs}


def substitute_args(ref: str, args: dict[str, str | None]) -> str:
    """`ref` with each `$NAME`, `${NAME}`, `${NAME:-word}` and `${NAME:+word}` a global
    ARG resolves replaced; a reference to an ARG with no default is left as written."""

    def resolve(m: re.Match) -> str:
        value = args.get(m[1] or m[4])
        if m[2] == ':-':
            return value or m[3]
        if m[2] == ':+':
            return m[3] if value else ''
        return m[0] if value is None else value

    return ARG_REF.sub(resolve, ref)


def run_script(instruction: str) -> str:
    """The shell script a RUN runs: flags dropped, an exec form re-quoted, and a heredoc
    body kept only when a shell executes it rather than reading it as data."""
    line, _, body = instruction.partition('\n')
    words = line.split(None, 1)[1].split() if len(line.split(None, 1)) > 1 else []
    while words and words[0].startswith('--'):
        words.pop(0)
    rest = ' '.join(words)
    if rest.startswith('['):
        try:
            argv = json.loads(rest)
        except ValueError:
            return rest
        return shlex.join(str(a) for a in argv) if isinstance(argv, list) else rest
    if not body:
        return rest
    command = [w for w in rest.split() if not REDIRECT.match(w)]
    name = command[0].rsplit('/', 1)[-1] if command else ''
    shell_reads_body = not command or (name in SHELLS and shell_script(command) is None)
    return f'{rest}\n{body}' if shell_reads_body else rest


def simple_commands(script: str) -> list[list[str]]:
    """The words of each simple command of a shell script, quotes removed. A command
    ends at a newline, `;`, `&`, `|`, `(` or `)` outside quotes; `#` starts a comment."""
    out: list[list[str]] = [[]]
    word: list[str] | None = None
    i = 0
    while i < len(script):
        c = script[i]
        if c == "'":
            end = script.find("'", i + 1)
            end = len(script) if end < 0 else end
            word = [*(word or []), script[i + 1 : end]]
            i = end + 1
        elif c == '"':
            i += 1
            word = word or []
            while i < len(script) and script[i] != '"':
                escaped = script[i] == '\\' and i + 1 < len(script)
                word.append(script[i + 1] if escaped else script[i])
                i += 2 if escaped else 1
            i += 1
        elif c == '\\' and i + 1 < len(script):
            word = [*(word or []), script[i + 1]]
            i += 2
        elif c == '#' and word is None:
            end = script.find('\n', i)
            i = len(script) if end < 0 else end
        elif c.isspace() or c in ';&|()':
            if word is not None:
                out[-1].append(''.join(word))
                word = None
            if (c == '\n' or c in ';&|()') and out[-1]:
                out.append([])
            i += 1
        else:
            word = [*(word or []), c]
            i += 1
    if word is not None:
        out[-1].append(''.join(word))
    return [words for words in out if words]


def shell_script(words: list[str]) -> str | None:
    """The script a shell command's `-c` option runs, or None when it reads one."""
    for at, w in enumerate(words[1:-1], 1):
        if w.startswith('-') and not w.startswith('--') and 'c' in w:
            return words[at + 1]
    return None


def installs_packages(script: str, depth: int = 0) -> bool:
    """Whether a command of the script runs `apk add|upgrade` or `apt-get install|upgrade|
    dist-upgrade`: the package manager in command position, after any prefix keyword,
    wrapper or assignment, or inside a shell's `-c` script."""
    for words in simple_commands(script):
        words = command_words(words)
        if not words:
            continue
        name = words[0].rsplit('/', 1)[-1]
        if name in SHELLS:
            inner = shell_script(words)
            if inner is not None and depth < 3 and installs_packages(inner, depth + 1):
                return True
        elif name in INSTALL_SUBCOMMANDS and (
            subcommand(name, words[1:]) in INSTALL_SUBCOMMANDS[name]
        ):
            return True
    return False


def command_words(words: list[str]) -> list[str]:
    """The words from the command a simple command runs: keywords, assignments,
    redirections and each wrapper with its options skipped, env's split string read
    as words; empty when `command -v` only names it."""
    i = 0
    while i < len(words):
        w = words[i]
        wrapper = w.rsplit('/', 1)[-1]
        if w in SHELL_KEYWORDS or ASSIGNMENT.match(w) or REDIRECT.match(w):
            i += 1
            continue
        if wrapper not in WRAPPERS:
            break
        i += 1
        while i < len(words) and words[i].startswith('-'):
            short = '' if words[i].startswith('--') else words[i][1:]
            if wrapper == 'command' and set(short) & {'v', 'V'}:
                return []
            i, name, value = read_option(wrapper, words, i)
            if wrapper == 'env' and name in ('-S', '--split-string'):
                try:
                    inserted = shlex.split(value or '')
                except ValueError:
                    inserted = (value or '').split()
                words = [*words[:i], *inserted, *words[i:]]
    return words[i:]


def read_option(tool: str, words: list[str], i: int) -> tuple[int, str, str | None]:
    """The option at `words[i]`, read as the tool's getopt does: the index after it
    and its value, the name of the option that takes the value (or the word), and the
    value, or None when it takes none."""
    w = words[i]
    following = words[i + 1] if i + 1 < len(words) else ''
    if w.startswith('--'):
        name, eq, value = w.partition('=')
        if eq or name not in VALUE_OPTIONS[tool]:
            return i + 1, name, value if eq else None
        return i + 2, name, following
    for at, letter in enumerate(w[1:], 2):
        if letter in SHORT_VALUE_LETTERS[tool]:
            return (i + 1, f'-{letter}', w[at:]) if w[at:] else (i + 2, f'-{letter}', following)
    return i + 1, w, None


def subcommand(manager: str, args: list[str]) -> str | None:
    """The package manager's first operand: its options, and the value of each one
    VALUE_OPTIONS lists, skipped."""
    i = 0
    while i < len(args):
        if not args[i].startswith('-'):
            return args[i]
        i = read_option(manager, args, i)[0]
    return None


def final_base_images(dockerfile: str) -> set[str] | None:
    """The image, without tag or digest, as Renovate lists it, at the root of the FROM
    chain the default build's stage descends from: the only base whose update moves the
    final image's package database. Empty for scratch; None when an ARG with no default
    names it."""
    stages = build_stages(dockerfile)
    index = len(stages) - 1
    while index >= 0:
        ref = stages[index][0]
        parent = next((i for i in range(index) if stages[i][1] == ref), -1)
        if parent < 0:
            if '$' in ref:
                return None
            name = ref.split('@', 1)[0]
            if ':' in name.rsplit('/', 1)[-1]:
                name = name.rsplit(':', 1)[0]
            return set() if name == 'scratch' else {name}
        index = parent
    return set()


def stage_chain(stages: list, index: int) -> list[int]:
    """The stage at `index` and the stages it is built FROM, root first."""
    chain = []
    while index >= 0:
        chain.append(index)
        base = stages[index][0]
        index = next((i for i in range(index) if stages[i][1] == base), -1)
    return chain[::-1]


def chain_installs(stages: list, index: int) -> bool:
    return any(installs_packages(s) for i in stage_chain(stages, index) for s, _ in stages[i][2])


def chain_refreshes(stages: list, index: int) -> bool:
    """Whether a rebuild with a new PKG_REFRESH re-runs a package install of the chain:
    one PKG_REFRESH reaches, in a stage that declares it or a stage built FROM one."""
    inherited = False
    for i in stage_chain(stages, index):
        for script, reached in stages[i][2]:
            if (inherited or reached) and installs_packages(script):
                return True
        inherited = inherited or stages[i][3]
    return False


def final_stage_installs(dockerfile: str) -> bool:
    """Whether the stage a default build produces, or a stage it is built FROM, runs a
    package install. An install in a builder stage alone leaves the final image's
    package database to its base, which only a base-digest update moves."""
    stages = build_stages(dockerfile)
    return bool(stages) and chain_installs(stages, len(stages) - 1)


def final_stage_refreshes(dockerfile: str) -> bool:
    stages = build_stages(dockerfile)
    return bool(stages) and chain_refreshes(stages, len(stages) - 1)


def needed_stages(stages: list, index: int) -> set[int]:
    """The stages a build of the stage at `index` runs: it, the stages it is built FROM,
    and the stages any of them copies or mounts from, by name or index."""
    seen: set[int] = set()
    todo = [index]
    while todo:
        i = todo.pop()
        if i in seen:
            continue
        seen.add(i)
        todo += stage_chain(stages, i)
        for ref in stages[i][4]:
            j = (
                int(ref)
                if ref.isdigit()
                else next((k for k in range(i) if stages[k][1] == ref), -1)
            )
            if 0 <= j < i:
                todo.append(j)
    return seen


def rebuild_refreshes(dockerfile: str) -> str:
    """`refreshes` when a rebuild of the default target re-runs a package install of a
    stage it needs, `stale` when such a stage installs packages but none of them
    re-runs, else `none`."""
    stages = build_stages(dockerfile)
    needed = needed_stages(stages, len(stages) - 1) if stages else set()
    if any(chain_refreshes(stages, i) for i in needed):
        return 'refreshes'
    return 'stale' if any(chain_installs(stages, i) for i in needed) else 'none'


class Router:
    """Routes the fixable findings of one scan record; collects at most one tick, at
    most one rebuild, the findings with no automatic route and summary notes."""

    def __init__(self, api: Api, repo: str, pulls: PullState, doc: dict, run: dict):
        self.api, self.repo, self.pulls, self.doc = api, repo, pulls, doc
        self.started = ts(run['run_started_at'])
        self.tick: dict | None = None
        self.rebuild: list[str] = []
        self.unrouted: list[str] = []
        self.notes: list[str] = []
        self._dashboard: tuple[dict, dict] | bool | None = False
        self._dockerfile: str | None = None

    def dashboard(self) -> tuple[dict, dict] | None:
        if self._dashboard is False:
            issue = read_dashboard(self.api, self.repo)
            self._dashboard = (issue, parse_dashboard(issue.get('body') or '')) if issue else None
        return self._dashboard

    def dockerfile(self) -> str:
        if self._dockerfile is None:
            file = self.api.get_or_none(f'repos/{OWNER}/{self.repo}/contents/Dockerfile?ref=main')
            self._dockerfile = (
                base64.b64decode(''.join(file['content'].split()), validate=True).decode()
                if file
                else ''
            )
        return self._dockerfile

    def installs_packages(self) -> bool:
        return final_stage_installs(self.dockerfile())

    def route(self) -> None:
        fixable = [f for f in self.doc['findings'] if f.get('fixed')]
        lockfile = [f['id'] for f in fixable if f['class'] == 'lockfile']
        if lockfile:
            self.notes.append(
                f'{len(lockfile)} fixable finding(s) only in lockfile entries, which ship '
                f'nothing: {", ".join(sorted(set(lockfile)))}'
            )
        for f in fixable:
            if f['class'] == 'os' and self.installs_packages():
                if final_stage_refreshes(self.dockerfile()):
                    self.route_rebuild(f)
                else:
                    self.unroute(f, 'no-refresh')
            elif f['class'] in ('os', 'manifest', 'stdlib'):
                level = 'ok' if f['class'] == 'os' else fix_level(f)
                if level == 'ok':
                    self.route_group(f)
                else:
                    self.unroute(f, level)
        if self.doc.get('not_covered'):
            self.notes.append(f'not covered by the scan: {", ".join(self.doc["not_covered"])}')

    def unroute(self, f: dict, reason: str) -> None:
        line = f'{finding_text(f)}: {UNROUTED[reason]}.'
        if line not in self.unrouted:
            self.unrouted.append(line)

    def route_rebuild(self, f: dict) -> None:
        if self.pulls.newest(self.pulls.open, 'rebuild'):
            return
        merged = self.pulls.newest(self.pulls.merged, 'rebuild')
        if merged and ts(merged['merged_at']) >= self.started - REBUILD_RETRY_WINDOW:
            finished = self.pulls.shipping.finished
            if merged not in self.pulls.unshipped and finished and finished < self.started:
                self.unroute(f, 'rebuild-shipped')
            return
        if f['id'] not in self.rebuild:
            self.rebuild.append(f['id'])

    def route_group(self, f: dict) -> None:
        if f['class'] == 'os' and final_base_images(self.dockerfile()) is None:
            self.unroute(f, 'unknown-base')
            return
        group = self.pulls.newest(self.pulls.open, 'group')
        if not group and self.pulls.newest(self.pulls.unshipped, 'group'):
            return
        board = self.dashboard()
        if board is None:
            self.unroute(f, 'no-dashboard')
            return
        issue, entries = board
        entry = entries.get(rc.MAIN_GROUP_BRANCH)
        unreached = self.unreached(f, entry) if entry is not None else None
        if group:
            if entry is None or entry['kind'] != 'rebase':
                self.unroute(f, 'no-rebase')
            elif unreached:
                self.unroute(f, unreached)
            elif not entry['checked'] and self.started > ts(group['updated_at']):
                self.want_tick(issue, entry)
            return
        if entry is None:
            self.unroute(f, 'no-group')
        elif entry['heading'] == 'Awaiting Schedule' and entry['kind'] == 'unschedule':
            if unreached:
                self.unroute(f, unreached)
            elif not entry['checked']:
                self.want_tick(issue, entry)
        elif entry['heading'] not in IN_FLIGHT_HEADINGS:
            line = f'{finding_text(f)}: the `main` group is listed under "{entry["heading"]}", which this job does not act on.'
            if line not in self.unrouted:
                self.unrouted.append(line)

    def unreached(self, f: dict, entry: dict) -> str | None:
        """Why the group entry gives no evidence that it can update the finding's package,
        or None. The standard library is not a Renovate package name, so it is not looked up."""
        if f['class'] == 'stdlib':
            return None
        # Renovate lists a branch's packages only when it has two or more:
        # https://github.com/renovatebot/renovate/blob/main/lib/workers/repository/dependency-dashboard.ts
        if entry['packages'] is None:
            return 'no-package-list'
        # The list is what Renovate would update now, not what an open branch
        # carries. An OS package moves only with a base image the group updates.
        if f['class'] == 'manifest':
            reached = f['package'] in entry['packages']
        else:
            reached = bool(
                final_base_images(self.dockerfile()) & {p.lower() for p in entry['packages']}
            )
        return None if reached else 'not-in-group'

    def want_tick(self, issue: dict, entry: dict) -> None:
        self.tick = {'issue': issue['number'], 'line': entry['line'], 'kind': entry['kind']}


def scan_blockers(doc_run: tuple[dict, dict] | None, now: dt.datetime, repo: str) -> list[str]:
    """Why the newest scan record must not be routed; empty only for a complete, fresh one."""
    if doc_run is None:
        return [
            (
                'No scan of `main` was found. Run '
                f'`gh workflow run security.yml -R {OWNER}/{repo} --ref main`.'
            )
        ]
    doc, run = doc_run
    started = ts(run['run_started_at'])
    out = []
    if now - started > SCAN_MAX_AGE:
        out.append(
            f'The newest scan of `main` started at {shown(started)}, more than 36 hours ago.'
        )
    if not doc.get('complete'):
        errors = '; '.join((doc.get('errors') or ['no reason recorded'])[:3])
        out.append(f'The scan of `main` at {doc["commit"][:12]} is incomplete: {errors}.')
    return out


# ── Plan ──────────────────────────────────────────────────────────────────────


def plan_repo(api: Api, repo: str, now: dt.datetime) -> dict:
    row = {
        'repo': repo,
        'errors': [],
        'merge': [],
        'rebuild': None,
        'tick': None,
        'blockers': {key: [] for key, _ in SECTIONS},
        # Sections a failed read left unknown; the issue keeps their earlier text.
        'unread': [],
        'notes': [],
    }
    try:
        open_prs = api.pages(f'repos/{OWNER}/{repo}/pulls?state=open')
        row['merge'], notes = plan_security(api, repo, open_prs)
        row['notes'] += notes
    except (GhError, *SHAPE_ERRORS) as exc:
        row['errors'].append(f'security pull requests: {exc}')
        row['unread'] = [key for key, _ in SECTIONS]
        return row
    try:
        pulls = PullState(api, repo, open_prs)
        row['blockers'].update(pulls.blockers(now))
    except (GhError, *SHAPE_ERRORS) as exc:
        row['errors'].append(f'pull requests into main: {exc}')
        row['unread'] = [key for key, _ in SECTIONS]
        return row
    try:
        doc_run = read_scan(api, repo)
        scan_problems = scan_blockers(doc_run, now, repo)
        row['blockers']['unrouted'] += scan_problems
        if not scan_problems:
            router = Router(api, repo, pulls, *doc_run)
            router.route()
            row['blockers']['unrouted'] += router.unrouted
            row['notes'] += router.notes
            row['tick'] = router.tick
            if router.rebuild:
                ids = ', '.join(f'`{i}`' for i in router.rebuild[:5])
                more = f' and {len(router.rebuild) - 5} more' if len(router.rebuild) > 5 else ''
                row['rebuild'] = {
                    'reason': (
                        f'The daily scan of `main` at {doc_run[0]["commit"][:12]} found fixable '
                        f'vulnerabilities in the OS packages of the published image: {ids}{more}.'
                    )
                }
    except (GhError, *SHAPE_ERRORS) as exc:
        row['errors'].append(f'scan of main: {exc}')
        row['unread'] = ['unrouted']
    return row


def cmd_plan(args, api: Api) -> int:
    now = ts(args.now) if args.now else dt.datetime.now(dt.UTC)
    only = set(args.only.replace(',', ' ').split())
    repos = discover(api, only)
    doc = {'now': iso(now), 'repos': [plan_repo(api, r, now) for r in repos]}
    Path(args.out).write_text(json.dumps(doc, indent=2) + '\n')
    print(f'planned {len(repos)} repositories whose default branch is dev')
    return 0


# ── Merge ─────────────────────────────────────────────────────────────────────


def merge_pr(api: Api, repo: str, pr: dict, run) -> str:
    """`merged`, or a failure line, after merge_checked reads the base, the head, the
    labels and the check again rather than trusting Plan's read. Never armed: an armed
    pull request would merge later on labels nobody re-read."""
    try:
        result = merge_checked(
            api,
            repo,
            pr['number'],
            run,
            base=pr['base'],
            head_prefix='renovate/',
            sha=pr['sha'],
            labels=rc.SECURITY_AUTOMERGE_LABELS,
            arm=False,
        )
    except (GhError, *SHAPE_ERRORS) as exc:
        return f'failed: {exc}'
    return result if result == 'merged' else f'failed: {result}'


def merge_checked(
    api: Api,
    repo: str,
    number: int,
    run=run_gh,
    *,
    base: str,
    head: str = '',
    head_prefix: str = '',
    sha: str = '',
    labels: frozenset[str] = frozenset(),
    arm: bool = True,
) -> str:
    """`armed`, `merged`, or why pull request `number` was left open. Acts only while it
    still targets `base` from this repository's own `head` (or a head starting with
    `head_prefix`), its head is `sha` when given, and it carries one of `labels` and not
    `security-major` when `labels` is given; then, with `arm`, arms auto-merge pinned to
    that head. GitHub refuses to arm a pull request that is already mergeable, and a
    direct merge (a squash pinned to that head, then the head branch deleted) also needs
    every latest `ci / validate` from GitHub Actions green there: `main`'s ruleset lets
    the owner credential bypass the check."""
    if base not in ('dev', 'main'):
        raise ValueError(f'base must be dev or main, got {base!r}')
    pr = api.get(f'repos/{OWNER}/{repo}/pulls/{number}')
    at = pr['head']['sha']
    if pr.get('state') != 'open':
        return f'not merged: the pull request is {pr.get("state")}'
    ref = pr['head'].get('ref') or ''
    if (
        pr['base'].get('ref') != base
        or not own_head(pr, repo)
        or (head and ref != head)
        or not ref.startswith(head_prefix)
    ):
        return f'not merged: the pull request is not {head or head_prefix or "a head"} of {OWNER}/{repo} into {base}'
    if sha and at != sha:
        return f'not merged: its head moved from {sha[:12]} to {at[:12]}'
    if labels and (not labels_of(pr) & labels or 'security-major' in labels_of(pr)):
        return 'not merged: its labels no longer allow an unattended merge'
    refused = ''
    if arm:
        args = ['pr', 'merge', str(number), '-R', f'{OWNER}/{repo}', '--squash', '--delete-branch']
        try:
            run([*args, '--auto', '--match-head-commit', at])
            return 'armed'
        except GhError as exc:
            refused = f'auto-merge refused ({exc}) and '
    if not validate_green(api, repo, at):
        return f'not merged: {refused}`{VALIDATE_CHECK}` is not green at {at[:12]}'
    api.put(f'repos/{OWNER}/{repo}/pulls/{number}/merge', {'merge_method': 'squash', 'sha': at})
    try:
        api.delete_branch(repo, ref)
    except GhError as exc:
        print(
            f'::warning::{OWNER}/{repo}#{number} merged, but its head branch was not deleted: {exc}'
        )
    return 'merged'


def cmd_merge_checked(args, api: Api) -> int:
    try:
        result = merge_checked(
            api,
            args.repo,
            args.number,
            api.command,
            base=args.base,
            head=args.head,
            head_prefix=args.head_prefix,
        )
    except (GhError, *SHAPE_ERRORS) as exc:
        result = f'failed: {exc}'
    print(f'cplieger/{args.repo}#{args.number}: {result}')
    return 0 if result in ('armed', 'merged') else 1


def open_rebuild(repo: str, reason: str, run_script) -> str:
    script = Path(__file__).resolve().parent / 'rebuild-stale.sh'
    proc = run_script(['bash', str(script), 'open-pr', repo, 'main', reason])
    if proc.returncode != 0:
        return f'failed: {" ".join((proc.stderr or proc.stdout or "").split())[-300:]}'
    return 'opened'


def run_script_default(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def cmd_merge(args, run=ghrest.run_process, run_script=run_script_default) -> int:
    plan = json.loads(Path(args.plan).read_text())
    api = Api(run)
    out = {}
    for row in plan['repos']:
        done = {'merge': {}, 'rebuild': None}
        for pr in row['merge']:
            done['merge'][str(pr['number'])] = (
                'dry run' if args.dry_run else merge_pr(api, row['repo'], pr, api.command)
            )
        if row['rebuild']:
            done['rebuild'] = (
                'dry run'
                if args.dry_run
                else open_rebuild(row['repo'], row['rebuild']['reason'], run_script)
            )
        out[row['repo']] = done
    Path(args.out).write_text(json.dumps(out, indent=2) + '\n')
    return 0


# ── Report ────────────────────────────────────────────────────────────────────


def section_text(existing: str, title: str) -> str | None:
    """The section `title` exactly as an earlier run wrote it, or None."""
    inner = trackerlib.sentinel_inner(existing, ISSUE_LABEL) or ''
    m = re.search(rf'^### {re.escape(title)}\n\n(?:- [^\n]*(?:\n|$))+', inner, re.MULTILINE)
    return m.group(0).rstrip('\n') if m else None


def issue_body(blockers: dict[str, list[str]], existing: str, unread=()) -> str:
    parts = [
        (
            'Something that reached `main` has not shipped, or a vulnerability on `main` has '
            'no automatic fix. This issue is checked every hour and closes when every '
            'section below is clear.'
        )
    ]
    for key, title in SECTIONS:
        if key in unread:
            kept = section_text(existing, title)
            if kept:
                parts.append(kept)
            continue
        items = blockers[key]
        if not items:
            continue
        lines = [f'- {item}' for item in items[:SECTION_CAP]]
        if len(items) > SECTION_CAP:
            lines.append(f'- and {len(items) - SECTION_CAP} more, listed in the run summary.')
        parts.append(f'### {title}\n\n' + '\n'.join(lines))
    notes = trackerlib.preserve_notes(existing)
    if notes == trackerlib.NOTES_DEFAULT:
        notes = NOTES_DEFAULT
    return (
        trackerlib.sentinel_block(ISSUE_LABEL, '\n\n'.join(parts))
        + f'\n\n## Free-form notes\n\n{notes}\n'
    )


def tick(api: Api, repo: str, want: dict) -> str:
    issue = api.get(f'repos/{OWNER}/{repo}/issues/{want["issue"]}')
    body = issue.get('body') or ''
    lines = body.split('\n')
    hits = [i for i, line in enumerate(lines) if line.rstrip('\r') == want['line']]
    if len(hits) != 1:
        return 'skipped: the dashboard changed since the plan read it'
    lines[hits[0]] = lines[hits[0]].replace(' - [ ] ', ' - [x] ', 1)
    api.patch(f'repos/{OWNER}/{repo}/issues/{want["issue"]}', {'body': '\n'.join(lines)})
    return f'ticked {want["kind"]}'


def tracker_main(argv: list[str]) -> int:
    return tracker_issue.main(argv, allow_reserved=True)


def sync_issue(repo: str, row: dict, run_url: str, tracker, work: Path) -> str:
    """Open, update or close the repository's issue; returns what was done. A row with
    errors, from a read or from a planned action this run did not complete, never
    closes it."""
    common = ['--repo', f'{OWNER}/{repo}', '--label', ISSUE_LABEL, '--title', ISSUE_TITLE]
    unread = set(row['unread'])
    if not any(items for key, items in row['blockers'].items() if key not in unread):
        if row['errors']:
            return 'left as is: a read or a planned action failed'
        comment = work / f'{repo}.comment.md'
        comment.write_text(f'Every blocker is clear as of {run_url}\n')
        rc_ = tracker([*common, '--mode', 'close-when-clean', '--comment-file', str(comment)])
        return 'closed if open' if rc_ == 0 else 'failed: close'
    current = work / f'{repo}.current.md'
    if tracker([*common, '--mode', 'fetch', '--body-file', str(current)]) != 0:
        return 'failed: fetch'
    existing = current.read_text()
    body = issue_body(row['blockers'], existing, unread)
    if body == existing:
        return 'unchanged'
    new = work / f'{repo}.body.md'
    new.write_text(body)
    if tracker([*common, '--mode', 'upsert', '--body-file', str(new)]) != 0:
        return 'failed: upsert'
    return 'opened or updated'


def cell(text: str) -> str:
    return (text or '-').replace('|', '\\|').replace('\n', ' ')


def cmd_report(args, api: Api, tracker=tracker_main) -> int:
    plan = json.loads(Path(args.plan).read_text())
    merged = json.loads(Path(args.merged).read_text()) if Path(args.merged).is_file() else None
    failed = False
    lines = [
        '## Release maintenance',
        '',
        '| Repo | Merges | Rebuild | Tick | Issue | Notes | Errors |',
        '|---|---|---|---|---|---|---|',
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for row in plan['repos']:
            repo = row['repo']
            errors = list(row['errors'])
            done = (merged or {}).get(repo)
            if done is None and (row['merge'] or row['rebuild']):
                errors.append('the merge step did not record this repository')
                done = {'merge': {}, 'rebuild': None}
            done = done or {'merge': {}, 'rebuild': None}
            errors += [f'#{n}: {r}' for n, r in done['merge'].items() if r.startswith('failed')]
            if (done['rebuild'] or '').startswith('failed'):
                errors.append(f'rebuild: {done["rebuild"]}')
            ticked = '-'
            if row['tick']:
                try:
                    ticked = 'dry run' if args.dry_run else tick(api, repo, row['tick'])
                except GhError as exc:
                    errors.append(f'dashboard tick: {exc}')
                if ticked.startswith('skipped'):
                    errors.append(f'dashboard tick {ticked}')
            if args.dry_run:
                issue = 'dry run'
            else:
                issue = sync_issue(
                    repo, {**row, 'errors': errors}, args.run_url, tracker, Path(tmp)
                )
                if issue.startswith('failed'):
                    errors.append(f'issue: {issue}')
            failed |= bool(errors)
            merges = ', '.join(f'#{n} {r}' for n, r in done['merge'].items())
            blockers = [b for key, _ in SECTIONS for b in row['blockers'][key]]
            notes = row['notes'] + [f'blocked: {b}' for b in blockers]
            lines.append(
                '| '
                + ' | '.join(
                    cell(x)
                    for x in (
                        repo,
                        merges,
                        done['rebuild'] or '',
                        ticked,
                        issue,
                        '<br>'.join(notes),
                        '<br>'.join(errors),
                    )
                )
                + ' |'
            )
            for e in errors:
                print(f'::error::{repo}: {e}')
    if not plan['repos']:
        lines.append('| (no repository has `dev` as its default branch) | | | | | | |')
    with open(args.summary, 'a', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--out', required=True)
    p.add_argument('--only', default='', help='comma- or space-separated repository names')
    p.add_argument('--now', default='', help='ISO time to plan at (tests)')
    m = sub.add_parser('merge')
    m.add_argument('--plan', required=True)
    m.add_argument('--out', required=True)
    m.add_argument('--dry-run', action='store_true')
    r = sub.add_parser('report')
    r.add_argument('--plan', required=True)
    r.add_argument('--merged', required=True)
    r.add_argument('--run-url', required=True)
    r.add_argument('--summary', required=True)
    r.add_argument('--dry-run', action='store_true')
    c = sub.add_parser(
        'merge-checked',
        help=(
            'after a fresh read of its base and head, arm auto-merge on one machine pull '
            'request into --base, or merge it once `ci / validate` is green, or exit 1'
        ),
    )
    c.add_argument('repo', type=lambda s: s if REPO_NAME.fullmatch(s) else parser.error(s))
    c.add_argument('number', type=int)
    c.add_argument('--base', required=True, choices=('dev', 'main'))
    want = c.add_mutually_exclusive_group(required=True)
    want.add_argument('--head', default='', help='the exact head branch')
    want.add_argument('--head-prefix', default='', help='the head branch prefix')
    sub.add_parser(
        'rebuild-refreshes',
        help=(
            'exit 0 when a rebuild of the Dockerfile on stdin re-runs a package install of a '
            'stage its default target needs, 4 when those stages install packages in no layer '
            'PKG_REFRESH reaches, else 3'
        ),
    )
    args = parser.parse_args(argv)
    if args.cmd == 'rebuild-refreshes':
        return {'refreshes': 0, 'none': 3, 'stale': 4}[rebuild_refreshes(sys.stdin.read())]
    if args.cmd == 'merge-checked':
        return cmd_merge_checked(args, Api())
    if args.cmd == 'plan':
        return cmd_plan(args, Api())
    if args.cmd == 'merge':
        return cmd_merge(args)
    return cmd_report(args, Api())


if __name__ == '__main__':
    sys.exit(main())
