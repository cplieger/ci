#!/usr/bin/env python3
"""File-sync engine: push canonical files from this checkout into consumer repos as PRs.

Per manifest target (a repo, or a repo and a `base:`): clone, copy the mapped files from
this checkout, commit
`chore(sync): ...`, force-push `repo-sync/ci/default` (`repo-sync/ci/<base>` with --branch
and --base), and keep one `dependencies` PR open. No diff, no PR; an open sync PR whose
diff evaporated is closed. Files are only added or updated; forks are skipped unless
--allow-forks. A failed target never stops the rest, and the exit is then non-zero.

Auth: the ambient `gh` credentials over REST; git pushes through gh's credential helper,
so no token reaches a remote URL or process output.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import ghrest
import yaml

OWNER = 'cplieger'
BRANCH = 'repo-sync/ci/default'
BASES = frozenset({'dev', 'main'})
COMMIT_SUBJECT = 'chore(sync): synced file(s) with cplieger/ci'
PR_TITLE = COMMIT_SUBJECT
PR_LABEL = 'dependencies'
CLOSE_COMMENT = "Closing: the target branch already contains this sync's content."
GIT_USER = 'github-actions[bot]'
GIT_EMAIL = '41898282+github-actions[bot]@users.noreply.github.com'
# Repo-local credential helper: git asks gh, gh uses GH_TOKEN/keyring. The
# leading ! marks a shell-out helper; scoped to each clone, never global.
CRED_HELPER = '!gh auth git-credential'


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
    """The open sync pull requests of this target, newest first. A base target takes
    only heads in this repository, because a fork's pull request can carry the same
    name; a main-default target takes a head of that name from any repository."""
    scope = '' if base is None else f'&base={base}'
    rows = ghrest.pages(f'repos/{repo}/pulls?state=open&sort=created&direction=desc{scope}')
    head = branch_for(base)
    return [
        row
        for row in rows
        if (row.get('head') or {}).get('ref') == head and (base is None or own_head(row, repo))
    ]


def open_pull(repo, base):
    rows = open_pulls(repo, base)
    return rows[0] if rows else None


def add_label(repo, number):
    """Label the PR when the repo has PR_LABEL; a missing label is never created, and a
    failure is a warning, never a failed sync."""
    try:
        if ghrest.get_or_none(f'repos/{repo}/labels/{PR_LABEL}') is not None:
            ghrest.send('POST', f'repos/{repo}/issues/{number}/labels', {'labels': [PR_LABEL]})
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
    pr = ghrest.send(
        'POST',
        f'repos/{repo}/pulls',
        {'title': PR_TITLE, 'head': branch_for(base), 'base': into, 'body': '\n'.join(body_lines)},
    )
    add_label(repo, pr['number'])
    print(f'  opened PR: {pr["html_url"]}')


def close_stale_pr(repo, base, row):
    """No diff this run: close a leftover open sync PR whose content has since landed
    on its target branch some other way. Each failed step is a warning; a fork's
    head branch is not ours to delete."""
    number, label = row['number'], target_label(repo, base)
    steps = [
        ('comment', 'POST', f'repos/{repo}/issues/{number}/comments', {'body': CLOSE_COMMENT}),
        ('close', 'PATCH', f'repos/{repo}/pulls/{number}', {'state': 'closed'}),
    ]
    if own_head(row, repo):
        steps.append(
            ('branch delete', 'DELETE', f'repos/{repo}/git/refs/heads/{branch_for(base)}', None)
        )
    for what, method, path, body in steps:
        try:
            ghrest.send(method, path, body)
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


def fork_names():
    """Names of every fork under OWNER.

    Fail closed: a target repo is only writable by this engine once it is known
    NOT to be a fork, so an unreadable repo list aborts the run rather than
    proceeding on an unverified manifest.
    """
    try:
        repos = ghrest.pages('user/repos?affiliation=owner')
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


def print_open_prs(mapping):
    """`<repo name> <number> <head> [<base>]` per open sync PR, for sync.yaml's sweep;
    a target whose lookup fails is a warning on stderr and is skipped."""
    for repo, base in sorted(mapping, key=target_key):
        try:
            rows = open_pulls(repo, base)
        except ghrest.ApiError as err:
            msg = f'::warning::{target_label(repo, base)}: the open sync PR lookup failed: {err}'
            print(msg, file=sys.stderr)
            continue
        for row in rows:
            fields = [repo.split('/')[-1], str(row['number']), branch_for(base)]
            print(' '.join([*fields, *([base] if base else [])]))


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
        '--print-open-prs',
        action='store_true',
        help='print each open sync PR as "<repo> <number> <head> [<base>]" and exit',
    )
    args = ap.parse_args()

    source_root = Path(args.source_dir).resolve()
    try:
        mapping = load_mapping(args.manifest)
    except ManifestError as err:
        sys.exit(f'::error::sync-files: {err}')
    if args.print_open_prs:
        print_open_prs(mapping)
        return
    if args.only:
        wanted = {w.strip() for w in args.only.replace(',', ' ').split() if w.strip()}
        mapping = {t: f for t, f in mapping.items() if t[0].split('/')[-1] in wanted}
    if not args.allow_forks:
        mapping = drop_forks(mapping)

    if not mapping:
        print('nothing to sync (empty mapping after filters)')
        return

    counts = {'changed': 0, 'clean': 0, 'dry': 0}
    failures = []
    for repo, base in sorted(mapping, key=target_key):
        label = target_label(repo, base)
        print(f'::group::{label}')
        try:
            outcome = sync_repo(repo, base, mapping[repo, base], source_root, args.dry_run)
            counts[outcome] += 1
        except (subprocess.CalledProcessError, ghrest.ApiError, OSError) as exc:
            detail = (
                exc.stderr.strip()
                if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
                else str(exc)
            )
            print(f'::warning::{label}: sync failed — {detail}')
            failures.append(label)
        print('::endgroup::')

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
