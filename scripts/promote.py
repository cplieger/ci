#!/usr/bin/env python3
"""Fast-forward a repository's `main` to a commit on its `dev` branch.

The push then runs the stable channel of that repository's release pipeline.
Three subcommands share one plan file under --work-dir so that the token able
to move `main` (GH_TOKEN of `apply`) is held by exactly one step:

    plan    runs every check for --repo (or every two-channel repo with
            --scheduled) and writes plan.json plus the job summary; reads only.
    apply   re-reads the soak status, records the promotion and any --skip-soak
            reason as a commit status the stable release notes pick up, and
            fast-forwards main (PATCH, force=false). Exits 1 on a refusal.
    report  opens or updates one `promotion-blocked` issue per repo a scheduled
            run refused, and closes them where a promotion went through.

Stdlib only; runs on the runner's default python3.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import release_channels as rc
import tracker_issue

OWNER = 'cplieger'
SOAK_CONTEXT = 'homelab/soak'
SOAK_HOURS = 24
# Ancestors below the target the soak walk reads a release run for;
# docker-release.yaml's digest walk checks the target and then this many
# ancestors (its `--skip=1 --max-count`), so a build the promotion cannot
# reuse is never the one the soak is read from.
SOAK_WALK_COMMITS = 200
ISSUE_LABEL = 'promotion-blocked'
# The GitHub API caps a status description at 140 characters.
OVERRIDE_CONTEXT = 'promotion/soak-override'
OVERRIDE_DESCRIPTION_LIMIT = 140
FINALIZE_JOB_SUFFIX = ' / docker / finalize'
# The runs API's largest page; a commit whose runs fill RUN_PAGES pages has
# runs the walk never saw, so it cannot be called docs-only.
RUN_PAGE_SIZE = 100
RUN_PAGES = 5
# One issue title per failure class, so a scheduled target that moves from day
# to day updates the same issue instead of opening a new one.
BLOCKED_TITLES = {
    'soak': 'Promotion blocked: unhealthy in the deployment soak',
    'evidence': 'Promotion blocked: the dev release run has not succeeded',
    'purity': 'Promotion blocked: a first-party dependency is pinned at a dev version',
    'ancestry': 'Promotion blocked: main is not an ancestor of dev',
    'read': 'Promotion blocked: a GitHub or git read failed',
}
SOAK_BLOCKED_TITLE = BLOCKED_TITLES['soak']


class GhError(Exception):
    """A gh invocation this script cannot proceed past."""


def gh(*args: str) -> str:
    proc = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f'exit {proc.returncode}'
        raise GhError(f'gh {" ".join(args[:3])} failed: {detail}')
    return proc.stdout


def gh_json(path: str):
    return json.loads(gh('api', path) or 'null')


def git(clone: Path, *args: str) -> str:
    proc = subprocess.run(
        ['git', '-C', str(clone), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GhError(f'git {" ".join(args[:2])} in {clone} failed: {proc.stderr.strip()}')
    return proc.stdout


# ── Pure decision functions ───────────────────────────────────────────────────


def on_first_parent(history: list[str], target: str) -> bool:
    """`history` is dev's first-parent chain (newest first); `target` must be on it."""
    return target in history


def main_is_ancestor(compare: dict) -> bool:
    """From `compare/main...<target>`: main is an ancestor when target is ahead or identical."""
    return compare.get('status') in ('ahead', 'identical')


def nothing_to_promote(compare: dict) -> bool:
    return compare.get('status') == 'identical' or compare.get('ahead_by') == 0


def delta_count_matches(local_count: int, compare: dict) -> bool:
    """The clone's main..target commit count must equal what GitHub reports as ahead_by."""
    return compare.get('ahead_by') == local_count


def _first_party(spec: str) -> bool:
    return 'cplieger/' in spec or spec.startswith('@cplieger/')


def _go_mod_directives(text: str):
    """(directive, words) for every require and replace line, blocks unfolded."""
    block = ''
    for raw in text.splitlines():
        line = raw.split('//', 1)[0].strip()
        if not line:
            continue
        if block:
            if line == ')':
                block = ''
            else:
                yield block, line.split()
            continue
        words = line.split()
        if words[0] in ('require', 'replace'):
            if len(words) == 2 and words[1] == '(':
                block = words[0]
            else:
                yield words[0], words[1:]


def _go_mod_violations(path: str, text: str) -> list[str]:
    found = []
    for directive, words in _go_mod_directives(text):
        if directive == 'require':
            if (
                len(words) >= 2
                and words[0].startswith('github.com/cplieger/')
                and '-dev.' in words[1]
            ):
                found.append(f'{path}: {words[0]} {words[1]}')
            continue
        # replace [old [vOld]] => new [vNew]: the replacement is what Go builds.
        if '=>' not in words:
            continue
        target = words[words.index('=>') + 1 :]
        if (
            len(target) >= 2
            and target[0].startswith('github.com/cplieger/')
            and '-dev.' in target[1]
        ):
            found.append(f'{path}: replace {words[0]} => {target[0]} {target[1]}')
    return found


def _package_json_violations(path: str, text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [f'{path}: not valid JSON']
    found = []
    for section in ('dependencies', 'peerDependencies', 'optionalDependencies'):
        for name, spec in (data.get(section) or {}).items():
            # An alias spec (`npm:@cplieger/x@1.0.0-dev.1`) names the package
            # in the value, so both sides are judged.
            if _first_party(f'{name} {spec}') and '-dev.' in str(spec):
                found.append(f'{path}: {section} {name} {spec}')
    return found


def _jsr_json_violations(path: str, text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [f'{path}: not valid JSON']
    return [
        f'{path}: imports {name} {spec}'
        for name, spec in (data.get('imports') or {}).items()
        if '@cplieger/' in f'{name} {spec}' and '-dev.' in str(spec)
    ]


_RENOVATE_DEP = re.compile(r'#\s*renovate:.*\bdepName=(\S+)')
_FROM_LINE = re.compile(r'^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)', re.IGNORECASE)
_ARG_LINE = re.compile(r'^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)')


def _dockerfile_violations(path: str, text: str) -> list[str]:
    """A `# renovate:` marker annotates the ARG on the next line only, as
    Renovate reads it; any other line in between disowns it."""
    found = []
    renovate_dep = ''
    for raw in text.splitlines():
        m = _RENOVATE_DEP.search(raw)
        if m:
            renovate_dep = m.group(1)
            continue
        m = _FROM_LINE.match(raw)
        if m:
            image = m.group(1)
            if image.startswith('ghcr.io/cplieger/') and '-dev.' in image:
                found.append(f'{path}: FROM {image}')
        m = _ARG_LINE.match(raw)
        if m:
            name, value = m.group(1), m.group(2).strip('"\'')
            if '-dev.' in value and (_first_party(value) or _first_party(renovate_dep)):
                found.append(f'{path}: ARG {name}={value}')
        renovate_dep = ''
    return found


def purity_violations(files: dict[str, str]) -> list[str]:
    """First-party pins at a `-dev.` version among {path: text} for go.mod,
    package.json, jsr.json and Dockerfile* files. Empty means pure."""
    found: list[str] = []
    for path in sorted(files):
        text = files[path]
        base = path.rsplit('/', 1)[-1]
        if base == 'go.mod':
            found += _go_mod_violations(path, text)
        elif base == 'package.json':
            found += _package_json_violations(path, text)
        elif base == 'jsr.json':
            found += _jsr_json_violations(path, text)
        elif base.startswith('Dockerfile'):
            found += _dockerfile_violations(path, text)
    return found


def is_purity_file(path: str) -> bool:
    if '/node_modules/' in f'/{path}':
        return False
    base = path.rsplit('/', 1)[-1]
    return base in ('go.mod', 'package.json', 'jsr.json') or base.startswith('Dockerfile')


def classify_commit(pulls: list[dict]) -> str:
    """'machine' when a linked pull request's head branch carries a machine
    prefix, 'human' otherwise, 'human' with no pull request at all."""
    for pull in pulls:
        head = ((pull.get('head') or {}).get('ref')) or ''
        if head.startswith(rc.MACHINE_HEAD_PREFIXES):
            return 'machine'
    return 'human'


def first_human_commit(shas: list[str], pulls_for) -> str | None:
    """The first commit of `shas` not merged from a machine pull request, or
    None when every one was. `pulls_for(sha)` returns that commit's pull
    requests; the walk stops at the first human commit."""
    for sha in shas:
        if classify_commit(pulls_for(sha)) != 'machine':
            return sha
    return None


def newest_is_older_than(dates: list[str], now: datetime, hours: int = SOAK_HOURS) -> bool:
    """`dates` are ISO-8601 committer dates; the newest must be at least `hours` old."""
    if not dates:
        return False
    newest = max(datetime.fromisoformat(d) for d in dates)
    return now - newest >= timedelta(hours=hours)


def soak_chain(chain: list[str], target: str, main_sha: str) -> list[str] | None:
    """`target` and at most SOAK_WALK_COMMITS first-parent ancestors below it,
    or None when `main_sha` is not below it on `chain` (no fast-forward exists
    then). The walk is not cut at main: a docs-only main inherits an older
    build, and so does a docs-only target above it."""
    if target not in chain or main_sha not in chain:
        return None
    start = chain.index(target)
    if chain.index(main_sha) < start:
        return None
    return chain[start : start + SOAK_WALK_COMMITS + 1]


def soak_bearing_commit(target: str, chain: list[str], runs_by_sha) -> str | None:
    """The commit whose dev build the target runs: the nearest first-parent
    ancestor (target included) whose dev release run succeeded with a docker
    finalize job. A commit with no run of its own was pushed together with the
    next newer commit that has one, so it inherits that run's verdict; a failed
    run anywhere on the way ends the search. `runs_by_sha.get(sha)` returns
    {'conclusion': ..., 'finalize': bool} or None."""
    if not chain or chain[0] != target:
        return None
    covered = False
    for sha in chain:
        run = runs_by_sha.get(sha)
        if run is None:
            if not covered:
                return None
            continue
        if run.get('conclusion') != 'success':
            return None
        covered = True
        if run.get('finalize'):
            return sha
    return None


def evidence_ok(runs: list[dict]) -> bool:
    """The dev release run at the target succeeded (any successful run counts)."""
    return any(run.get('conclusion') == 'success' for run in runs)


def soak_ok(statuses: list[dict]) -> bool:
    return any(s.get('context') == SOAK_CONTEXT and s.get('state') == 'success' for s in statuses)


def soak_state(statuses: list[dict]) -> str:
    for s in statuses:
        if s.get('context') == SOAK_CONTEXT:
            return s.get('state') or 'missing'
    return 'missing'


def soak_override_status(state: str, description: str, run_url: str) -> dict:
    """The commit status every promotion writes before it moves main, which the
    stable release notes read in state success only: a --skip-soak reason as
    the description, an empty description for a promotion that had its soak
    (this retires a stale reason on the commit), failure with the error when
    the ref update did not move main."""
    text = ' '.join(description.split())
    if len(text) > OVERRIDE_DESCRIPTION_LIMIT:
        text = text[: OVERRIDE_DESCRIPTION_LIMIT - 3] + '...'
    return {
        'state': state,
        'context': OVERRIDE_CONTEXT,
        'description': text,
        'target_url': run_url,
    }


def render_summary(rows: list[dict]) -> str:
    lines = ['## Promotion plan', '']
    if not rows:
        lines.append('No repository was considered.')
    for row in rows:
        target = row.get('target') or '(none)'
        head = f'**{row["repo"]}** at `{target[:12]}`: {row["decision"]}'
        if row.get('last_stable'):
            head += f' (last stable {row["last_stable"]})'
        lines.append(f'- {head}.')
        for reason in row.get('reasons') or []:
            lines.append(f'  - {reason}')
    return '\n'.join(lines) + '\n'


def blocked_title(row: dict) -> str:
    return BLOCKED_TITLES.get(row.get('blocked_by') or '', 'Promotion blocked: a check failed')


# ── GitHub-backed helpers ─────────────────────────────────────────────────────


class LazyRuns:
    """`runs_by_sha` for soak_bearing_commit, fetching each commit's dev release
    runs on first use. A commit is built when any successful run at it has a
    successful Docker finalize job: a later no-op dispatch at an already tagged
    commit is a successful run without one and must not hide the build. Short
    of a build, one run that did not succeed makes the commit a failure whatever
    a later no-op run says: the failed attempt may have pushed sha-<commit>, and
    that image cannot borrow an ancestor's soak. A listing cut at the page bound
    with neither is a failure too: what it hides could be either."""

    def __init__(self, repo: str):
        self.repo = repo
        self.cache: dict[str, dict | None] = {}

    def get(self, sha: str) -> dict | None:
        if sha in self.cache:
            return self.cache[sha]
        runs, complete = release_runs(self.repo, sha)
        success = [r for r in runs if r.get('conclusion') == 'success']
        other = [r for r in runs if r.get('conclusion') != 'success']
        result: dict | None = None
        if success and any(self.finalized(run['id']) for run in success):
            result = {'conclusion': 'success', 'finalize': True}
        elif other:
            result = {'conclusion': other[0].get('conclusion'), 'finalize': False}
        elif not complete:
            result = {'conclusion': 'failure', 'finalize': False}
        elif success:
            result = {'conclusion': 'success', 'finalize': False}
        self.cache[sha] = result
        return result

    def finalized(self, run_id) -> bool:
        jobs = gh_json(f'repos/{OWNER}/{self.repo}/actions/runs/{run_id}/jobs?per_page=100')
        return any(
            (j.get('name') or '').endswith(FINALIZE_JOB_SUFFIX) and j.get('conclusion') == 'success'
            for j in (jobs or {}).get('jobs') or []
        )


class RunListing(NamedTuple):
    runs: list[dict]
    complete: bool


def release_runs(repo: str, sha: str) -> RunListing:
    """The dev release runs at `sha`, newest first; `complete` is false when
    RUN_PAGES full pages were read and the listing may go on."""
    runs: list[dict] = []
    for page in range(1, RUN_PAGES + 1):
        data = gh_json(
            f'repos/{OWNER}/{repo}/actions/workflows/release.yaml/runs'
            f'?branch=dev&head_sha={sha}&per_page={RUN_PAGE_SIZE}&page={page}'
        )
        batch = (data or {}).get('workflow_runs') or []
        runs.extend(batch)
        if len(batch) < RUN_PAGE_SIZE:
            return RunListing(runs, complete=True)
    return RunListing(runs, complete=False)


def commit_statuses(repo: str, sha: str) -> list[dict]:
    data = gh_json(f'repos/{OWNER}/{repo}/commits/{sha}/status')
    return (data or {}).get('statuses') or []


def commit_pulls(repo: str, sha: str) -> list[dict]:
    return gh_json(f'repos/{OWNER}/{repo}/commits/{sha}/pulls') or []


def compare(repo: str, base: str, head: str) -> dict:
    return gh_json(f'repos/{OWNER}/{repo}/compare/{base}...{head}') or {}


def post_status(repo: str, sha: str, status: dict) -> None:
    gh(
        'api',
        '-X',
        'POST',
        f'repos/{OWNER}/{repo}/statuses/{sha}',
        *[arg for key, value in status.items() for arg in ('-f', f'{key}={value}')],
    )


def clone_repo(repo: str, work_dir: Path) -> Path:
    clone = work_dir / repo
    if not clone.exists():
        proc = subprocess.run(
            ['git', 'clone', '--quiet', f'https://github.com/{OWNER}/{repo}', str(clone)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GhError(f'clone of {repo} failed: {proc.stderr.strip()}')
    return clone


def files_at(clone: Path, target: str) -> dict[str, str]:
    paths = [p for p in git(clone, 'ls-tree', '-r', '--name-only', target).split('\n') if p]
    return {p: git(clone, 'show', f'{target}:{p}') for p in paths if is_purity_file(p)}


def delta_commits(clone: Path, target: str) -> tuple[list[str], list[str]]:
    """Every commit of origin/main..target from the clone, newest first, with
    its committer date; complete where one Compare API page is not."""
    out = git(clone, 'log', '--format=%H %cI', f'origin/main..{target}')
    shas, dates = [], []
    for line in out.split('\n'):
        if line:
            sha, date = line.split(' ', 1)
            shas.append(sha)
            dates.append(date)
    return shas, dates


def repo_default_branch(repo: str) -> str:
    return (gh_json(f'repos/{OWNER}/{repo}') or {}).get('default_branch') or ''


def new_row(repo: str, target: str, opts) -> dict:
    """A plan row before any check ran. `soak_commit` is the commit whose
    homelab/soak status carried the evidence; apply re-reads it before moving main."""
    return {
        'repo': repo,
        'target': target,
        'decision': 'blocked',
        'blocked_by': '',
        'reasons': [],
        'machine_only': None,
        'last_stable': '',
        'clone_dir': '',
        'soak_commit': '',
        'soak_skipped_reason': '',
        'mode': 'scheduled' if opts.scheduled else 'manual',
    }


def _blocked(row: dict, kind: str, reason: str) -> dict:
    row['decision'] = 'blocked'
    row['blocked_by'] = kind
    row['reasons'].append(reason)
    return row


def _skipped(row: dict, reason: str) -> dict:
    row['decision'] = 'skip'
    row['reasons'].append(reason)
    return row


def plan_repo(repo: str, target: str | None, opts, work_dir: Path, now: datetime) -> dict:
    """Run every check for one repo and return its plan row."""
    row = new_row(repo, target or '', opts)
    if repo in rc.SINGLE_MAIN_REPOS:
        return _blocked(
            row, 'ancestry', f'{repo} publishes from main directly and has no dev channel.'
        )
    default_branch = repo_default_branch(repo)
    if default_branch != 'dev':
        return _blocked(
            row,
            'ancestry',
            f'The default branch is {default_branch or "unknown"}, not dev; the repo has not adopted the dev channel.',
        )

    clone = clone_repo(repo, work_dir)
    row['clone_dir'] = str(clone)
    dev_head = git(clone, 'rev-parse', 'origin/dev').strip()
    main_sha = git(clone, 'rev-parse', 'origin/main').strip()
    target = target or dev_head
    row['target'] = target
    tags = [t for t in git(clone, 'tag', '--list').split('\n') if t]
    row['last_stable'] = rc.newest_stable_tag(tags)

    chain = [c for c in git(clone, 'rev-list', '--first-parent', 'origin/dev').split('\n') if c]
    if not on_first_parent(chain, target):
        return _blocked(row, 'ancestry', f'{target} is not on the first-parent history of dev.')
    cmp_main = compare(repo, 'main', target)
    if not main_is_ancestor(cmp_main):
        return _blocked(
            row,
            'ancestry',
            f'main is not an ancestor of {target} (compare status {cmp_main.get("status")}); '
            'a fast-forward is impossible.',
        )
    if nothing_to_promote(cmp_main):
        return _skipped(row, 'main already points at the target; nothing to promote.')
    shas, dates = delta_commits(clone, target)
    if not delta_count_matches(len(shas), cmp_main):
        return _blocked(
            row,
            'ancestry',
            f'The clone counts {len(shas)} commits in main..{target[:12]} but GitHub reports '
            f'{cmp_main.get("ahead_by")}; refusing to classify an incomplete delta.',
        )

    if opts.scheduled:
        human = first_human_commit(shas, lambda sha: commit_pulls(repo, sha))
        row['machine_only'] = human is None
        if human is not None:
            return _skipped(
                row,
                f'{human[:12]} was not merged from a machine pull request; '
                f'{len(shas)} commits wait for a manual promotion.',
            )
        if not newest_is_older_than(dates, now):
            return _skipped(row, f'The newest pending commit is younger than {SOAK_HOURS} hours.')

    violations = purity_violations(files_at(clone, target))
    if violations:
        return _blocked(
            row,
            'purity',
            'A first-party dependency is pinned at a dev version; promote it first: '
            + '; '.join(violations),
        )

    if repo in rc.OWN_PUBLISH_REPOS:
        row['reasons'].append(
            f'{repo} publishes through its own workflow on main; there is no dev release run to check.'
        )
    elif not evidence_ok(release_runs(repo, target).runs):
        return _blocked(row, 'evidence', f'The dev release run at {target[:12]} has not succeeded.')

    if opts.skip_soak:
        row['reasons'].append(f'Soak check skipped by request: {opts.reason}')
        if repo in rc.DEPLOYED_IMAGE_REPOS:
            row['soak_skipped_reason'] = opts.reason
        else:
            row['reasons'].append(
                'No soak is required for this repo; the reason is recorded here only.'
            )
    elif repo in rc.DEPLOYED_IMAGE_REPOS:
        statuses = commit_statuses(repo, target)
        bearing = target
        if not soak_ok(statuses):
            walk = soak_chain(chain, target, main_sha)
            if walk is None:
                return _blocked(
                    row,
                    'ancestry',
                    f'main ({main_sha[:12]}) is not on the first-parent history of dev.',
                )
            bearing = soak_bearing_commit(target, walk, LazyRuns(repo))
            if bearing is None:
                return _blocked(
                    row,
                    'soak',
                    f'No dev build within {len(walk)} first-parent commits of {target[:12]} '
                    f'to read the {SOAK_CONTEXT} status from; the walk stops at a failed run, '
                    f'at a commit with {RUN_PAGES * RUN_PAGE_SIZE} or more release runs, '
                    f'and after the target plus {SOAK_WALK_COMMITS} ancestors.',
                )
            if bearing != target:
                statuses = commit_statuses(repo, bearing)
        if not soak_ok(statuses):
            return _blocked(
                row,
                'soak',
                f'The {SOAK_CONTEXT} status is {soak_state(statuses)} on '
                f'{bearing[:12]}; the image has not completed a healthy soak window.',
            )
        row['soak_commit'] = bearing

    row['decision'] = 'promote'
    row['reasons'].append('Every check passed.')
    return row


def scheduled_candidates() -> list[str]:
    out = gh(
        'repo',
        'list',
        OWNER,
        '--limit',
        '300',
        '--json',
        'name,isArchived,isFork,visibility,defaultBranchRef',
    )
    names = []
    for repo in json.loads(out or '[]'):
        if repo.get('isArchived') or repo.get('isFork'):
            continue
        if (repo.get('visibility') or '').lower() != 'public':
            continue
        if ((repo.get('defaultBranchRef') or {}).get('name')) != 'dev':
            continue
        if repo['name'] in rc.SINGLE_MAIN_REPOS:
            continue
        names.append(repo['name'])
    return sorted(names)


def write_summary(text: str) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(text)
    print(text, end='')


# ── Subcommands ───────────────────────────────────────────────────────────────


def cmd_plan(args) -> int:
    if args.skip_soak and not args.reason:
        print('::error::--skip-soak needs a --reason', file=sys.stderr)
        return 2
    if bool(args.scheduled) == bool(args.repo):
        print('::error::pass exactly one of --repo or --scheduled', file=sys.stderr)
        return 2
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    repos = scheduled_candidates() if args.scheduled else [args.repo]
    rows = []
    for repo in repos:
        try:
            rows.append(
                plan_repo(repo, args.target if not args.scheduled else None, args, work_dir, now)
            )
        except GhError as err:
            rows.append(
                _blocked(
                    new_row(repo, args.target or '', args),
                    'read',
                    f'A GitHub or git read failed: {err}',
                )
            )
    Path(args.plan_file).write_text(json.dumps(rows, indent=2) + '\n')
    write_summary(render_summary(rows))
    if args.dry_run:
        write_summary('Dry run: main is not moved.\n')
    return 0


def promote_row(row: dict, run_url: str) -> None:
    """Re-read the soak evidence, record the promotion, then fast-forward main
    to the target. The soak reporter turns a success into a failure when a
    restart or an alert breaks the window, so the status planned on is read
    again here, right before the ref moves. The record is written before the
    ref update because the push starts the stable release that reads it, and a
    write that fails stops the promotion; a failed ref update turns the record
    into a failure, so its reason never reaches a release."""
    if row.get('soak_commit') and not row.get('soak_skipped_reason'):
        statuses = commit_statuses(row['repo'], row['soak_commit'])
        if not soak_ok(statuses):
            raise GhError(
                f'{SOAK_CONTEXT} on {row["soak_commit"][:12]} is {soak_state(statuses)} now; '
                'it was success when the plan was made'
            )
    reason = row.get('soak_skipped_reason') or ''
    post_status(row['repo'], row['target'], soak_override_status('success', reason, run_url))
    try:
        fast_forward(row['repo'], row['target'])
    except GhError as err:
        post_status(row['repo'], row['target'], soak_override_status('failure', str(err), run_url))
        raise


def cmd_apply(args) -> int:
    rows = json.loads(Path(args.plan_file).read_text())
    results = []
    failed = False
    for row in rows:
        result = {'repo': row['repo'], 'target': row['target'], 'promoted': False, 'error': ''}
        results.append(result)
        if row['decision'] != 'promote':
            if row.get('mode') == 'manual' and row['decision'] == 'blocked':
                failed = True
                print(
                    f'::error::{row["repo"]}: refused: {" ".join(row["reasons"])}', file=sys.stderr
                )
            continue
        try:
            promote_row(row, args.run_url)
            result['promoted'] = True
            print(f'{row["repo"]}: main -> {row["target"]}')
        except GhError as err:
            result['error'] = str(err)
            failed = True
            print(f'::error::{row["repo"]}: {err}', file=sys.stderr)
    Path(args.result_file).write_text(json.dumps(results, indent=2) + '\n')
    lines = ['## Promotions', '']
    for r in results:
        if r['promoted']:
            lines.append(f'- **{r["repo"]}**: main fast-forwarded to `{r["target"][:12]}`.')
        elif r['error']:
            lines.append(f'- **{r["repo"]}**: not promoted: {r["error"]}')
    write_summary('\n'.join(lines) + '\n')
    return 1 if failed else 0


def fast_forward(repo: str, target: str) -> None:
    """PATCH refs/heads/main to `target`, refusing anything but a fast-forward."""
    cmp_main = compare(repo, 'main', target)
    if not main_is_ancestor(cmp_main):
        raise GhError(f'main moved since planning (compare status {cmp_main.get("status")})')
    gh(
        'api',
        '-X',
        'PATCH',
        f'repos/{OWNER}/{repo}/git/refs/heads/main',
        '-f',
        f'sha={target}',
        '-F',
        'force=false',
    )


def open_blocked_titles(repo: str) -> list[str]:
    out = gh(
        'issue',
        'list',
        '-R',
        repo,
        '--state',
        'open',
        '--label',
        ISSUE_LABEL,
        '--json',
        'title',
        '--jq',
        '.[].title',
    )
    return [t for t in out.split('\n') if t]


def cmd_report(args) -> int:
    if not Path(args.plan_file).exists():
        print(
            f'::error::{args.plan_file} does not exist: plan did not finish, nothing to report',
            file=sys.stderr,
        )
        return 1
    rows = json.loads(Path(args.plan_file).read_text())
    results = {}
    if Path(args.result_file).exists():
        results = {r['repo']: r for r in json.loads(Path(args.result_file).read_text())}
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    run_url = args.run_url or '(no run url)'
    rc_total = 0
    for row in rows:
        repo = f'{OWNER}/{row["repo"]}'
        promoted = (results.get(row['repo']) or {}).get('promoted')
        if promoted:
            try:
                titles = open_blocked_titles(repo)
            except GhError as err:
                print(
                    f'::error::{repo}: could not list open {ISSUE_LABEL} issues: {err}',
                    file=sys.stderr,
                )
                rc_total |= 1
                continue
            comment = work_dir / f'{row["repo"]}-close.md'
            comment.write_text(f'main was promoted to {row["target"]} ({run_url}); closing.\n')
            for title in titles:
                rc_total |= tracker_issue.main(
                    [
                        '--repo',
                        repo,
                        '--label',
                        ISSUE_LABEL,
                        '--title',
                        title,
                        '--mode',
                        'close-when-clean',
                        '--comment-file',
                        str(comment),
                    ]
                )
            continue
        if row.get('mode') != 'scheduled' or row['decision'] != 'blocked':
            continue
        body = work_dir / f'{row["repo"]}-body.md'
        body.write_text(
            f'The daily promotion of `{row["target"]}` to main is blocked.\n\n'
            + '\n'.join(f'- {r}' for r in row['reasons'])
            + f'\n\nThe next scheduled run retries; this issue closes when a promotion goes through. Run: {run_url}\n'
        )
        comment = work_dir / f'{row["repo"]}-comment.md'
        comment.write_text(
            f'Still blocked, now at `{row["target"]}`.\n\n'
            + '\n'.join(f'- {r}' for r in row['reasons'])
            + f'\n\nRun: {run_url}\n'
        )
        rc_total |= tracker_issue.main(
            [
                '--repo',
                repo,
                '--label',
                ISSUE_LABEL,
                '--title',
                blocked_title(row),
                '--mode',
                'recur',
                '--body-file',
                str(body),
                '--comment-file',
                str(comment),
            ]
        )
    return rc_total


def parse_args(argv: list[str] | None = None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--work-dir', default='promote-work', help='clones and plan files live here')
    p.add_argument('--plan-file', default='', help='defaults to <work-dir>/plan.json')
    p.add_argument('--result-file', default='', help='defaults to <work-dir>/result.json')
    sub = p.add_subparsers(dest='command', required=True)

    plan = sub.add_parser('plan', help='run every check and write the plan')
    plan.add_argument('--repo', default='', help='repository name (manual mode)')
    plan.add_argument('--target', default='', help='commit on dev to promote; default: head of dev')
    plan.add_argument('--scheduled', action='store_true', help='consider every two-channel repo')
    plan.add_argument('--skip-soak', action='store_true', help='skip the homelab/soak check')
    plan.add_argument(
        '--reason', default='', help='why the soak check is skipped (required with --skip-soak)'
    )
    plan.add_argument('--dry-run', action='store_true', help='say so in the summary')
    plan.set_defaults(func=cmd_plan)

    apply = sub.add_parser('apply', help='fast-forward main for every planned promotion')
    apply.add_argument('--run-url', default='', help='link carried by the soak-override status')
    apply.set_defaults(func=cmd_apply)

    report = sub.add_parser('report', help='open, update or close the promotion-blocked issues')
    report.add_argument('--run-url', default='', help='link written into the issue')
    report.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    args.plan_file = args.plan_file or str(Path(args.work_dir) / 'plan.json')
    args.result_file = args.result_file or str(Path(args.work_dir) / 'result.json')
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
