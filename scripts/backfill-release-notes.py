#!/usr/bin/env python3
"""Regenerate existing GitHub release bodies with the current cliff.toml.

One-off maintenance tool for the release-notes noise cleanup: historical
releases carry (a) CI-pin churn that the cliff `exclude_paths` filter now
drops, and (b) off-by-one commit windows from the era when notes were
rendered with `--latest` after tagging. Re-rendering every tag's true
predecessor..tag range with the current config repairs both.

Scope guarantees:
    - Edits release BODIES only. Never touches git tags, release titles,
      draft/prerelease flags, or assets.
    - Dry-run by default, printing a unified diff per release; `--apply`
      performs the edits.
    - Two-phase apply: ALL current bodies are fetched and saved verbatim to a
      freshly created --backup-dir (exclusive create; one <tag>.md per
      release plus manifest.json recording repo, tag SHAs, body hashes, and
      the config hash) BEFORE the first edit. `--restore <dir>` replays a
      backup verbatim after checking it belongs to this repo.
    - Optimistic concurrency: each body is re-fetched immediately before its
      edit and must equal the reviewed value; a drifted body is skipped.
    - Pair windows are validated: the predecessor must be an ancestor of the
      tag (non-linear pairs are skipped with a warning), and every local tag
      must match the remote tag SHA (stale/moved tags abort before any edit).
    - The oldest release (no predecessor tag) is left untouched: bootstrap
      bodies ("Initial release") are not regenerable from a commit range.
    - A regenerated body that comes back empty (every commit in the range is
      excluded) becomes a maintenance stub plus a collapsed list of the
      policy-excluded commit subjects, rather than deleting the release.
    - Draft releases and non-vX.Y.Z tags (e.g. prereleases) are skipped with
      a notice; a skipped tag's window folds into the next stable tag's
      regenerated range. More than 1000 releases aborts (no pagination).

Run from (or point --repo-dir at) a local clone whose git remote is the
GitHub repo; `gh` resolves the repo from the remote and must be authed.
Requires Python 3.10+. Run AFTER the new cliff.toml has synced into the
repo, or pass --config pointing at cplieger/ci's configs/cliff-stable.toml
(or cliff-alpha.toml for pre-1.0 repos).

Usage:
    backfill-release-notes.py                        # dry-run, all releases
    backfill-release-notes.py --only v1.0.6 --only v1.0.7
    backfill-release-notes.py --config ../ci/configs/cliff-stable.toml
    backfill-release-notes.py --apply                # edit after reviewing
    backfill-release-notes.py --restore .release-notes-backup/1752600000
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import itertools
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

if sys.version_info < (3, 10):  # noqa: UP036 - the guard IS the feature
    sys.exit('error: this script needs Python 3.10+')

STUB_BODY = (
    '_Maintenance release: this range contains no changes eligible for release notes '
    'under the current policy (CI/dependency plumbing, docs, or test-only changes)._'
)
SEMVER_TAG = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')
RELEASE_LIST_CAP = 1000
MAX_STUB_SUBJECTS = 100


def run(
    cmd: list[str], cwd: Path, *, check: bool = True, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        print(f'error: {" ".join(cmd)} failed with rc={proc.returncode}', file=sys.stderr)
        print(proc.stderr.strip(), file=sys.stderr)
        sys.exit(2)
    return proc


def normalize(body: str) -> str:
    """Normalize for comparison only (never for storage): CRLF -> LF, strip trail."""
    lines = [ln.rstrip() for ln in body.replace('\r\n', '\n').split('\n')]
    return '\n'.join(lines).strip()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def repo_identity(repo_dir: Path) -> str:
    proc = run(['gh', 'repo', 'view', '--json', 'nameWithOwner'], repo_dir)
    return json.loads(proc.stdout)['nameWithOwner']


def list_release_tags(repo_dir: Path) -> list[str]:
    """Published, non-draft, plain-semver release tags, sorted ascending."""
    proc = run(
        [
            'gh',
            'release',
            'list',
            '--limit',
            str(RELEASE_LIST_CAP),
            '--json',
            'tagName,isDraft,isPrerelease',
        ],
        repo_dir,
    )
    entries = json.loads(proc.stdout)
    if len(entries) >= RELEASE_LIST_CAP:
        print(
            f'error: {len(entries)} releases returned (cap {RELEASE_LIST_CAP}); '
            'refusing to proceed on a possibly-truncated list',
            file=sys.stderr,
        )
        sys.exit(2)
    tags: list[tuple[int, int, int, str]] = []
    for entry in entries:
        tag = entry['tagName']
        if entry.get('isDraft'):
            print(f'  skip {tag}: draft release', file=sys.stderr)
            continue
        m = SEMVER_TAG.match(tag)
        if not m:
            kind = 'prerelease' if entry.get('isPrerelease') else 'non-semver tag'
            print(
                f'  skip {tag}: {kind} (its window folds into the next stable tag)', file=sys.stderr
            )
            continue
        tags.append((int(m[1]), int(m[2]), int(m[3]), tag))
    tags.sort()
    return [t[3] for t in tags]


def verify_tags(repo_dir: Path, tags: list[str]) -> dict[str, str]:
    """Every tag must exist locally AND resolve to the same commit as the remote."""
    proc = run(['git', 'ls-remote', '--tags', 'origin'], repo_dir, timeout=60)
    remote: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition('\t')
        name = ref.removeprefix('refs/tags/')
        if name.endswith('^{}'):  # peeled annotated tag: authoritative commit
            remote[name.removesuffix('^{}')] = sha
        else:
            remote.setdefault(name, sha)
    shas: dict[str, str] = {}
    for tag in tags:
        proc = run(['git', 'rev-parse', '--verify', f'{tag}^{{commit}}'], repo_dir, check=False)
        if proc.returncode != 0:
            print(
                f'error: tag {tag} not found locally; run `git fetch --tags` first', file=sys.stderr
            )
            sys.exit(2)
        local = proc.stdout.strip()
        if tag not in remote:
            print(f'error: tag {tag} has a GitHub release but no remote tag', file=sys.stderr)
            sys.exit(2)
        if remote[tag] != local:
            print(
                f'error: tag {tag} is {local[:12]} locally but {remote[tag][:12]} on origin; '
                'refusing (stale or moved tag)',
                file=sys.stderr,
            )
            sys.exit(2)
        shas[tag] = local
    return shas


def render_range(repo_dir: Path, cliff_bin: str, config: Path, prev: str, tag: str) -> str:
    proc = run(
        [cliff_bin, '--config', str(config), '--strip', 'header', f'{prev}..{tag}'],
        repo_dir,
    )
    return proc.stdout.strip()


def excluded_subjects(repo_dir: Path, prev: str, tag: str) -> list[str]:
    proc = run(['git', 'log', '--reverse', '--format=%s', f'{prev}..{tag}'], repo_dir)
    return [s for s in proc.stdout.splitlines() if s.strip()]


def stub_body(repo_dir: Path, prev: str, tag: str) -> str:
    subjects = excluded_subjects(repo_dir, prev, tag)
    shown = subjects[:MAX_STUB_SUBJECTS]
    lines = [
        STUB_BODY,
        '',
        '<details>',
        '<summary>Commits in this range (all policy-excluded)</summary>',
        '',
    ]
    lines += [f'- {s}' for s in shown]
    if len(subjects) > len(shown):
        lines.append(f'- (+ {len(subjects) - len(shown)} more)')
    lines += ['', '</details>']
    return '\n'.join(lines)


def fetch_body(repo_dir: Path, tag: str) -> str:
    proc = run(['gh', 'release', 'view', tag, '--json', 'body'], repo_dir)
    return json.loads(proc.stdout).get('body') or ''


def edit_body(repo_dir: Path, tag: str, body: str) -> None:
    with tempfile.NamedTemporaryFile(
        'w', suffix='.md', delete=False, encoding='utf-8', newline=''
    ) as tf:
        tf.write(body)
        notes_file = tf.name
    try:
        run(['gh', 'release', 'edit', tag, '--notes-file', notes_file], repo_dir)
    finally:
        Path(notes_file).unlink(missing_ok=True)


def restore(repo_dir: Path, backup_dir: Path) -> int:
    manifest_file = backup_dir / 'manifest.json'
    if not manifest_file.exists():
        print(f'error: {backup_dir} has no manifest.json (not a backup dir)', file=sys.stderr)
        return 2
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    repo = repo_identity(repo_dir)
    if manifest['repo'] != repo:
        print(
            f'error: backup belongs to {manifest["repo"]}, current repo is {repo}', file=sys.stderr
        )
        return 2
    for entry in manifest['entries']:
        tag = entry['tag']
        f = backup_dir / f'{tag}.md'
        body = f.read_text(encoding='utf-8', newline='')
        if sha256(body) != entry['old_sha256']:
            print(
                f'error: backup file for {tag} does not match its manifest hash; aborting',
                file=sys.stderr,
            )
            return 2
        print(f'restoring {tag}')
        edit_body(repo_dir, tag, body)
    print(f'restored {len(manifest["entries"])} release bodies')
    return 0


@dataclass
class Plan:
    tag: str
    prev: str
    old: str
    new: str
    is_stub: bool


def resolve_config(arg: str, repo_dir: Path, *, explicit: bool) -> Path:
    p = Path(arg)
    if p.is_absolute():
        candidates = [p]
    elif explicit:
        candidates = [Path.cwd() / p, repo_dir / p]
    else:
        candidates = [repo_dir / p]
    for c in candidates:
        if c.exists():
            return c.resolve()
    print(
        f'error: cliff config not found: {arg} (tried: {", ".join(map(str, candidates))})',
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--repo-dir', default='.', help='local clone of the target repo (default: .)')
    ap.add_argument(
        '--config',
        default='cliff.toml',
        help='git-cliff config; the default resolves against --repo-dir, an explicit '
        'relative path against the current directory first',
    )
    ap.add_argument('--cliff-bin', default='git-cliff', help='git-cliff binary (default: PATH)')
    ap.add_argument(
        '--only',
        action='append',
        default=[],
        help='backfill only this tag (repeatable; unknown tags are an error)',
    )
    ap.add_argument(
        '--apply', action='store_true', help='edit the releases (default: dry-run print only)'
    )
    ap.add_argument(
        '--backup-dir',
        default=None,
        help='backup location, must not exist yet (default: <repo>/.release-notes-backup/<epoch>/)',
    )
    ap.add_argument(
        '--restore',
        metavar='DIR',
        help='restore bodies from a backup dir (checks its manifest) and exit',
    )
    args = ap.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    if not (repo_dir / '.git').exists():
        print(f'error: {repo_dir} is not a git checkout', file=sys.stderr)
        return 2

    if args.restore:
        return restore(repo_dir, Path(args.restore).resolve())

    config = resolve_config(args.config, repo_dir, explicit=args.config != 'cliff.toml')
    repo = repo_identity(repo_dir)
    tags = list_release_tags(repo_dir)
    if len(tags) < 2:
        print('nothing to do: fewer than two semver releases')
        return 0
    tag_shas = verify_tags(repo_dir, tags)

    print(
        f'repo: {repo}  releases: {len(tags)}  config: {config} (sha256 {sha256(config.read_text(encoding="utf-8"))[:12]})'
    )
    print(f'oldest release {tags[0]} is skipped (no predecessor tag)\n')

    pairs = list(itertools.pairwise(tags))
    reachable_targets = {t for _, t in pairs}
    unknown = [t for t in args.only if t not in reachable_targets]
    if unknown:
        print(
            f'error: --only tag(s) not in the backfillable set: {", ".join(unknown)} '
            f'(backfillable: {", ".join(sorted(reachable_targets))})',
            file=sys.stderr,
        )
        return 2

    # Phase 1: compute and show the full plan (no writes).
    plans: list[Plan] = []
    unchanged = nonlinear = 0
    for prev, tag in pairs:
        if args.only and tag not in args.only:
            continue
        proc = run(['git', 'merge-base', '--is-ancestor', prev, tag], repo_dir, check=False)
        if proc.returncode != 0:
            print(
                f'!! {tag}: predecessor {prev} is not an ancestor (non-linear history) '
                '- skipping this pair',
                file=sys.stderr,
            )
            nonlinear += 1
            continue
        new_body = render_range(repo_dir, args.cliff_bin, config, prev, tag)
        is_stub = not new_body
        if is_stub:
            new_body = stub_body(repo_dir, prev, tag)
        old_body = fetch_body(repo_dir, tag)
        if normalize(old_body) == normalize(new_body):
            print(f'== {tag}: unchanged')
            unchanged += 1
            continue
        plans.append(Plan(tag=tag, prev=prev, old=old_body, new=new_body, is_stub=is_stub))
        marker = '  [maintenance stub]' if is_stub else ''
        print(f'== {tag}: {prev}..{tag}{marker}')
        diff = difflib.unified_diff(
            normalize(old_body).splitlines(),
            new_body.splitlines(),
            fromfile=f'{tag} (current)',
            tofile=f'{tag} (regenerated)',
            lineterm='',
        )
        for ln in diff:
            print(f'   {ln}')
        print()

    if not args.apply:
        print(
            f'DRY-RUN (use --apply to edit): {len(plans)} would change '
            f'({sum(p.is_stub for p in plans)} stubbed), {unchanged} unchanged, '
            f'{nonlinear} skipped non-linear'
        )
        return 0
    if not plans:
        print(f'nothing to apply: {unchanged} unchanged, {nonlinear} skipped non-linear')
        return 0

    # Phase 2a: backup EVERYTHING before the first edit (exclusive dir create).
    backup_dir = (
        Path(args.backup_dir)
        if args.backup_dir
        else repo_dir / '.release-notes-backup' / str(int(time.time()))
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        'repo': repo,
        'config': str(config),
        'config_sha256': sha256(config.read_text(encoding='utf-8')),
        'created': int(time.time()),
        'entries': [
            {'tag': p.tag, 'tag_sha': tag_shas[p.tag], 'old_sha256': sha256(p.old)} for p in plans
        ],
    }
    for p in plans:
        f = backup_dir / f'{p.tag}.md'
        f.write_text(p.old, encoding='utf-8', newline='')
        if sha256(f.read_text(encoding='utf-8', newline='')) != sha256(p.old):
            print(
                f'error: backup verification failed for {p.tag}; aborting before any edit',
                file=sys.stderr,
            )
            return 2
    (backup_dir / 'manifest.json').write_text(
        json.dumps(manifest, indent=2) + '\n', encoding='utf-8'
    )
    print(f'backups written and verified: {backup_dir}')

    # Phase 2b: edit, with an optimistic re-check against concurrent changes.
    applied = drifted = 0
    for p in plans:
        current = fetch_body(repo_dir, p.tag)
        if current != p.old:
            print(
                f'!! {p.tag}: body changed since review; skipping (re-run to pick it up)',
                file=sys.stderr,
            )
            drifted += 1
            continue
        edit_body(repo_dir, p.tag, p.new)
        if normalize(fetch_body(repo_dir, p.tag)) != normalize(p.new):
            print(
                f'error: post-edit verification failed for {p.tag}; STOPPING. '
                f'Restore with: --restore {backup_dir}',
                file=sys.stderr,
            )
            return 2
        print(f'applied {p.tag}')
        applied += 1

    print(
        f'\napplied: {applied} ({sum(p.is_stub for p in plans)} stubbed), '
        f'{unchanged} unchanged, {nonlinear} skipped non-linear, {drifted} drifted'
    )
    print(f'backups: {backup_dir}  (restore with --restore {backup_dir})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
