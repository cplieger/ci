#!/usr/bin/env python3
"""File-sync engine: push canonical files from this checkout into consumer repos as PRs.

Per manifest target (a repo, or a repo and a `base:`): clone, copy the mapped files from
this checkout, commit `chore(sync): ...`, force-push `repo-sync/ci/default`
(`repo-sync/ci/<base>` for a base target), and keep one `dependencies` PR open. No diff,
no PR; an open sync PR whose diff evaporated is closed. Files are only added or updated;
forks are skipped unless --allow-forks. A failed target never stops the rest, and the
exit is then non-zero.

--arm-open-prs is sync.yaml's auto-merge sweep: over the same targets a sync would write
(--only, forks skipped), it re-reads each target's open sync PRs and arms or merges each
it finds, exiting non-zero when a found `base: main` PR was left open. A target whose
lookup fails is warned and skipped.

Targets run on --workers threads (fanout.py), each target's log printed whole in
manifest order; with more than one worker, writes are paced (WRITE_GAP), and a rate-limit
refusal holds every worker (ghrest.Pacer).

Auth: a GitHub App installation token in GH_TOKEN, whose installation lists the repos;
git pushes through gh's credential helper, so no token reaches a remote URL or process
output.
"""

import argparse
import functools
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import fanout
import ghrest
import yaml

OWNER = 'cplieger'
BRANCH = 'repo-sync/ci/default'
BASES = frozenset({'dev', 'main'})
COMMIT_SUBJECT = 'chore(sync): synced file(s) with cplieger/ci'
PR_TITLE = COMMIT_SUBJECT
PR_LABEL = 'dependencies'
CLOSE_COMMENT = "Closing: the target branch already contains this sync's content."
# The App whose installation token sync.yaml pushes with, so commit and push name one actor.
GIT_USER = 'tribble-trouble[bot]'
GIT_EMAIL = '338123824+tribble-trouble[bot]@users.noreply.github.com'
# Repo-local credential helper: git asks gh, gh uses GH_TOKEN/keyring. The
# leading ! marks a shell-out helper; scoped to each clone, never global.
CRED_HELPER = '!gh auth git-credential'
# Seconds between the starts of two writes once targets run concurrently (ghrest.Pacer
# cites GitHub's rule); --workers 1 is the serial engine and spaces nothing.
WRITE_GAP = 1.0
PACER = ghrest.Pacer()
RUN = ghrest.paced(ghrest.run_process, PACER)
REST = ghrest.Client(run=RUN, pause=PACER.pause)


class ManifestError(ValueError):
    """The manifest names something the engine refuses to write."""


def run(args, cwd=None, check=True):
    """subprocess.run wrapper: captured text output, optional check."""
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def branch_for(base):
    """The sync head for a base; release_channels.MAIN_SYNC_HEAD is the `main` one."""
    return BRANCH if base is None else f'repo-sync/ci/{base}'


def target_label(repo, base):
    return repo if base is None else f'{repo} (base: {base})'


def load_mapping(manifest_path):
    """Manifest -> {(repo, base or None): {dest: source}}. A target in several
    groups gets the union of their files; a duplicate dest keeps the LAST
    group's source (groups are emitted most-generic-first by classify-repos.py).
    ManifestError on an unreadable manifest, one with no target, or a base other
    than dev or main."""
    try:
        cfg = yaml.safe_load(Path(manifest_path).read_text())
    except (OSError, yaml.YAMLError) as err:
        msg = f'cannot read the manifest {manifest_path}: {err}'
        raise ManifestError(msg) from None
    groups = cfg.get('group') if isinstance(cfg, dict) else None
    if not isinstance(groups, list) or not groups:
        msg = f'{manifest_path} lists no sync group, so it is no classify-repos.py manifest'
        raise ManifestError(msg)
    mapping = {}
    for group in groups:
        if not isinstance(group, dict):
            msg = f'{manifest_path}: a sync group is not a mapping: {group!r}'
            raise ManifestError(msg)
        repos = [r.strip() for r in (group.get('repos') or '').splitlines() if r.strip()]
        base = group.get('base')
        if base is not None and base not in BASES:
            msg = f'unknown base {base!r} for {", ".join(repos)} (want one of {sorted(BASES)})'
            raise ManifestError(msg)
        files = group.get('files') or []
        for repo in repos:
            dest_map = mapping.setdefault((repo, base), {})
            for entry in files:
                if isinstance(entry, str):
                    dest_map[entry] = entry
                else:
                    dest_map[entry['dest']] = entry['source']
    return mapping


def target_key(target):
    repo, base = target
    return repo, base or ''


def clone(repo, dest_dir, base):
    """Shallow-clone the base (the default branch without one) with the gh
    credential helper wired in repo-locally (covers private targets and the
    later push)."""
    branch = [] if base is None else ['--branch', base]
    run(
        [
            'git',
            '-c',
            f'credential.helper={CRED_HELPER}',
            'clone',
            '--quiet',
            '--depth',
            '1',
            *branch,
            f'https://github.com/{repo}.git',
            str(dest_dir),
        ]
    )
    run(['git', 'config', 'credential.helper', CRED_HELPER], cwd=dest_dir)
    run(['git', 'config', 'user.name', GIT_USER], cwd=dest_dir)
    run(['git', 'config', 'user.email', GIT_EMAIL], cwd=dest_dir)


def copy_files(source_root, clone_dir, dest_map):
    """Copy sources from the checkout into the clone; return the staged dest paths
    that differ."""
    written = []
    for dest, source in sorted(dest_map.items()):
        target = clone_dir / dest
        src = source_root / source
        if not src.is_file():
            msg = f'source file missing in ci checkout: {source}'
            raise FileNotFoundError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)  # copies bytes + mode (x-bit survives)
        written.append(dest)
    if written:
        run(['git', 'add', '--', *written], cwd=clone_dir)
    diff = run(['git', 'diff', '--cached', '--name-only'], cwd=clone_dir).stdout.split()
    return sorted(diff)


def default_branch(clone_dir):
    """The branch a clone without --branch checked out: the repo's default."""
    return run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=clone_dir).stdout.strip()


def own_head(row, repo):
    return ((row.get('head') or {}).get('repo') or {}).get('full_name') == repo


def open_pulls(repo, base):
    """The open sync pull requests of this target, newest first: only heads in this
    repository. A fork's pull request can carry the same branch name, and the sweep
    arms or merges every row returned here with a write credential."""
    scope = '' if base is None else f'&base={base}'
    rows = REST.pages(f'repos/{repo}/pulls?state=open&sort=created&direction=desc{scope}')
    head = branch_for(base)
    return [
        row for row in rows if (row.get('head') or {}).get('ref') == head and own_head(row, repo)
    ]


def open_pull(repo, base):
    rows = open_pulls(repo, base)
    return rows[0] if rows else None


def add_label(repo, number):
    """Label the PR when the repo has PR_LABEL; a missing label is never created, and a
    failure is a warning, never a failed sync."""
    try:
        if REST.get_or_none(f'repos/{repo}/labels/{PR_LABEL}') is not None:
            REST.send('POST', f'repos/{repo}/issues/{number}/labels', {'labels': [PR_LABEL]})
    except ghrest.ApiError as err:
        print(f'::warning::{repo}#{number}: label {PR_LABEL} was not applied: {err}')


def ensure_pr(repo, base, into, changed):
    """Open the sync PR into `into` unless one is open."""
    if open_pull(repo, base) is not None:
        print('  PR already open; force-push refreshed it')
        return
    body_lines = [
        'Synced from [cplieger/ci](https://github.com/cplieger/ci) by',
        '`scripts/sync-files.py`. Files carrying a `Synced from cplieger/ci` header are',
        'overwritten on every sync — change the canonical copy in cplieger/ci',
        "instead. Auto-merges once this repo's required checks pass.",
        '',
        'Files updated in this run:',
        *[f'- `{path}`' for path in changed],
    ]
    pr = REST.send(
        'POST',
        f'repos/{repo}/pulls',
        {'title': PR_TITLE, 'head': branch_for(base), 'base': into, 'body': '\n'.join(body_lines)},
    )
    add_label(repo, pr['number'])
    print(f'  opened PR: {pr["html_url"]}')


def close_stale_pr(repo, base, row):
    """No diff this run: close a leftover open sync PR whose content has since landed
    on its target branch some other way. Each failed step is a warning. `row` comes
    from open_pulls, so its head branch is this repository's to delete."""
    number, label = row['number'], target_label(repo, base)
    steps = [
        ('comment', 'POST', f'repos/{repo}/issues/{number}/comments', {'body': CLOSE_COMMENT}),
        ('close', 'PATCH', f'repos/{repo}/pulls/{number}', {'state': 'closed'}),
        ('branch delete', 'DELETE', f'repos/{repo}/git/refs/heads/{branch_for(base)}', None),
    ]
    for what, method, path, body in steps:
        try:
            REST.send(method, path, body)
        except ghrest.ApiError as err:
            # 422 is GitHub's answer for a ref that no longer exists.
            if what != 'branch delete' or err.status != 422:
                print(f'::warning::{label}: stale sync PR #{number}: {what} failed: {err}')
    print(f'  closed stale sync PR #{number}')


def sync_repo(repo, base, dest_map, source_root, dry_run):
    """Sync one target. Returns 'changed', 'clean', or 'dry'."""
    branch = branch_for(base)
    with tempfile.TemporaryDirectory(prefix='sync-') as tmp:
        clone_dir = Path(tmp) / 'repo'
        clone(repo, clone_dir, base)
        into = base if base is not None else default_branch(clone_dir)
        run(['git', 'checkout', '--quiet', '-B', branch], cwd=clone_dir)
        changed = copy_files(source_root, clone_dir, dest_map)

        if not changed:
            print('  in sync (no diff)')
            if not dry_run:
                # A clean target is in sync whether or not its leftover PR can be read.
                try:
                    row = open_pull(repo, base)
                except ghrest.ApiError as err:
                    print(
                        f'::warning::{target_label(repo, base)}: the open sync PR lookup failed: {err}'
                    )
                    return 'clean'
                if row is not None:
                    close_stale_pr(repo, base, row)
            return 'clean'

        print(f'  {len(changed)} file(s) differ: {", ".join(changed)}')
        if dry_run:
            return 'dry'

        run(['git', 'commit', '--quiet', '-m', COMMIT_SUBJECT], cwd=clone_dir)
        run(
            ['git', 'push', '--quiet', '--force', 'origin', f'HEAD:refs/heads/{branch}'],
            cwd=clone_dir,
        )
        ensure_pr(repo, base, into, changed)
        return 'changed'


def sync_target(target, dest_map, source_root, dry_run):
    """sync_repo inside the target's log group; None when the target failed."""
    repo, base = target
    label = target_label(repo, base)
    print(f'::group::{label}')
    try:
        return sync_repo(repo, base, dest_map, source_root, dry_run)
    except (subprocess.CalledProcessError, ghrest.ApiError, OSError) as exc:
        detail = (
            exc.stderr.strip()
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else str(exc)
        )
        print(f'::warning::{label}: sync failed — {detail}')
        return None
    finally:
        print('::endgroup::')


def fork_names():
    """Names of every fork under OWNER.

    Fail closed: a target repo is only writable by this engine once it is known
    NOT to be a fork, so an unreadable repo list aborts the run rather than
    proceeding on an unverified manifest.
    """
    try:
        # The App token's listing (classify-repos.py discover_repos says why).
        repos = REST.pages('installation/repositories', key='repositories')
    except ghrest.ApiError as err:
        sys.exit(
            f"sync-files: cannot read {OWNER}'s repo list, so forks cannot "
            f'be excluded — refusing to sync: {err}'
        )
    return {repo['name'] for repo in repos if repo.get('fork')}


def drop_forks(mapping):
    """Remove fork targets from the mapping, naming each one dropped.

    classify-repos.py already filters forks, but --manifest is an argument, so
    the engine cannot assume the file it was handed was generated that way.
    """
    forks = fork_names()
    kept = {}
    for target, dest_map in mapping.items():
        full_name = target[0]
        if full_name.split('/')[-1] in forks:
            print(f'::notice::{target_label(*target)}: skipped, repo is a fork')
            continue
        kept[target] = dest_map
    return kept


def sweep_lookup(target):
    """The target's open sync PRs; none, after a warning, when the lookup failed."""
    repo, base = target
    try:
        return open_pulls(repo, base)
    except ghrest.ApiError as err:
        print(f'::warning::{target_label(repo, base)}: the open sync PR lookup failed: {err}')
        return []


def delete_merged_head(repo, number):
    """The branch delete `gh pr merge --delete-branch` makes; a 422 means the merge
    already deleted it. The PR came from open_pulls, so its head is this repository's."""
    try:
        REST.send('DELETE', f'repos/{repo}/git/refs/heads/{BRANCH}')
    except ghrest.ApiError as err:
        if err.status != 422:
            print(f'  WARN: merged {repo}#{number} but could not delete {BRANCH}: {err}')


def merged(repo, number):
    """Whether the pull request is merged; False when that cannot be read."""
    try:
        return bool((REST.get(f'repos/{repo}/pulls/{number}') or {}).get('merged_at'))
    except ghrest.ApiError:
        return False


def arm_default(repo, number):
    """Arm a main-default target's PR, else squash-merge it directly: GitHub refuses
    --auto on a pull request that is already merge-ready. A rate-limit refusal is no
    such answer, so it leaves the PR open unless a re-read finds it merged: `gh pr
    merge` is several requests, and the refused one can follow the merge."""
    # gh reads an OWNER/REPO#NUMBER shorthand as a branch name, hence --repo.
    args = ['pr', 'merge', '--auto', '--squash', '--delete-branch', '--repo', repo, str(number)]
    try:
        auto = RUN(args)
    except (ghrest.ApiError, OSError) as err:
        print(f'  WARN: failed to merge {repo}#{number}: auto-merge did not run ({err})')
        return
    if auto.returncode == 0:
        print(f'{repo}#{number}: armed')
        return
    refused = ' '.join(auto.stderr.decode(errors='replace').split()) or f'exit {auto.returncode}'
    if ghrest.refused_by_rate_limit(auto):
        if merged(repo, number):
            print(f'{repo}#{number}: merged')
            return
        print(f'  WARN: failed to merge {repo}#{number}: auto-merge rate limited ({refused})')
        return
    try:
        REST.send('PUT', f'repos/{repo}/pulls/{number}/merge', {'merge_method': 'squash'})
    except ghrest.ApiError as err:
        print(f'  WARN: failed to merge {repo}#{number}: auto-merge refused ({refused}) and {err}')
        return
    print(f'{repo}#{number}: merged')
    delete_merged_head(repo, number)


def arm(pr, merge_checked):
    """Arm or merge one open sync PR; False only when a `base: main` target's PR was
    left open (a main-default or `dev` refusal is a warning)."""
    repo, base, number = pr
    print(f'Enabling auto-merge on {repo}#{number}')
    if base is None:
        arm_default(repo, number)
        return True
    # A base target's PR is armed or merged only after merge_checked's fresh read of its
    # base and head; merge_checked owns why.
    name = repo.split('/')[-1]
    head = branch_for(base)
    if merge_checked(name, number, base=base, head=head):
        return True
    if base == 'dev':
        print(f'  WARN: failed to merge {repo}#{number}')
        return True
    return False


def arm_open_prs(mapping, workers):
    """Arm or merge every open sync PR a fresh read finds; 1 when a found `base: main`
    PR was left open, else 0. A target whose lookup fails is warned and skipped, and
    does not change the exit."""
    # Imported here, so an import-time break in its tracker modules stops only the
    # sweep, never file propagation.
    import release_maintenance

    api = release_maintenance.Api(run=RUN, pause=PACER.pause)
    merge_checked = functools.partial(release_maintenance.report_merge_checked, api)
    targets = sorted(mapping, key=target_key)
    prs = [
        (repo, base, row['number'])
        for (repo, base), rows in fanout.ordered(targets, sweep_lookup, workers)
        for row in rows
    ]
    left_open = [
        pr for pr, ok in fanout.ordered(prs, lambda pr: arm(pr, merge_checked), workers) if not ok
    ]
    if left_open:
        print('::error::a sync pull request into main was left open, as the lines above show')
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description='cplieger file-sync engine')
    ap.add_argument(
        '--manifest',
        default='.github/sync.yml',
        help='repo↔file mapping (generated by classify-repos.py)',
    )
    ap.add_argument(
        '--source-dir', default='.', help='root of the cplieger/ci checkout holding the sources'
    )
    ap.add_argument(
        '--dry-run', action='store_true', help='report diffs only; no push, no PR, no close'
    )
    ap.add_argument('--only', default='', help='comma/space-separated repo names to limit the run')
    ap.add_argument(
        '--allow-forks', action='store_true', help='sync forks too (default: forks are skipped)'
    )
    ap.add_argument(
        '--arm-open-prs',
        action='store_true',
        help='arm or merge the open sync PRs of the selected targets, then exit',
    )
    ap.add_argument(
        '--workers',
        type=int,
        default=fanout.WORKERS,
        help=f'targets handled at once, 1 to {fanout.MAX_WORKERS} (default {fanout.WORKERS})',
    )
    args = ap.parse_args()
    if not 1 <= args.workers <= fanout.MAX_WORKERS:
        ap.error(f'--workers must be 1 to {fanout.MAX_WORKERS}')
    PACER.gap = WRITE_GAP if args.workers > 1 else 0.0

    source_root = Path(args.source_dir).resolve()
    try:
        mapping = load_mapping(args.manifest)
    except ManifestError as err:
        sys.exit(f'::error::sync-files: {err}')
    if args.only:
        wanted = {w.strip() for w in args.only.replace(',', ' ').split() if w.strip()}
        mapping = {t: f for t, f in mapping.items() if t[0].split('/')[-1] in wanted}
    if not args.allow_forks:
        mapping = drop_forks(mapping)
    if args.arm_open_prs:
        sys.exit(arm_open_prs(mapping, args.workers))

    if not mapping:
        print('nothing to sync (empty mapping after filters)')
        return

    counts = {'changed': 0, 'clean': 0, 'dry': 0}
    failures = []
    targets = sorted(mapping, key=target_key)
    for target, outcome in fanout.ordered(
        targets,
        lambda t: sync_target(t, mapping[t], source_root, args.dry_run),
        args.workers,
    ):
        if outcome is None:
            failures.append(target_label(*target))
        else:
            counts[outcome] += 1

    total = len(mapping)
    noun = 'repo(s)' if all(base is None for _repo, base in mapping) else 'target(s)'
    print(
        f'\n{total} {noun}: {counts["changed"]} synced · '
        f'{counts["clean"]} already in sync · {counts["dry"]} with pending diffs (dry-run) · '
        f'{len(failures)} failed{" (" + ", ".join(failures) + ")" if failures else ""}'
    )
    if failures:
        sys.exit(1)


if __name__ == '__main__':
    main()
