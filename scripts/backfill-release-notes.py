#!/usr/bin/env python3
"""Regenerate existing GitHub release bodies with the current cliff.toml.

Re-renders every tag's predecessor..tag range through render-notes.sh, as
release.yaml renders a new release.

Scope guarantees:
    - Edits release BODIES only. Never touches git tags, release titles,
      draft/prerelease flags, or assets.
    - Dry-run by default, printing a unified diff per release; `--apply`
      performs the edits.
    - Two-phase apply: ALL current bodies are fetched and saved verbatim to a
      freshly created --backup-dir (exclusive create; one <tag>.md per
      release plus a manifest recording repo, tag SHAs, body hashes, and the
      config hash) BEFORE the first edit. `--restore <dir>` replays a backup
      verbatim after checking it belongs to this repo.
    - Optimistic concurrency: each body is re-fetched immediately before its
      edit and must equal the reviewed value; a drifted body is skipped.
    - Pair windows are validated: the predecessor must be an ancestor of the
      tag (non-linear pairs skip with a warning), and every local tag must
      match the remote tag SHA (stale/moved tags abort before any edit).
    - The oldest release (no predecessor tag) is left untouched: bootstrap
      bodies ("Initial release") aren't regenerable from a commit range.
    - Nested Go module lanes are excluded from these root bodies, as
      release.yaml renders them.
    - The system-package diff is read from the two releases' SBOM assets when
      both carry one whose Sigstore bundle verifies (cosign) as signed by this
      repository's release run at the tag's commit (or, for a repaired
      Release, at a main commit up to the next stable tag), and the updates
      merged through a Renovate security PR into main or dev marked.
    - Draft releases and non-vX.Y.Z tags skip with a notice; a skipped tag's
      window folds into the next stable tag's range. More than 1000 releases
      aborts rather than plan from a truncated list.
    - A release listed in --carve-outs (default: backfill-carve-outs.yaml
      beside this script) carries hand-written text no commit contains, so it
      is skipped in plan and apply with a notice; --include-carved overrides.

Run from (or point --repo-dir at) a local clone whose `origin` remote is the
GitHub repo; `gh` must be authed, and every GitHub call is REST.
Requires Python 3.11+ and PyYAML. Run AFTER the new cliff.toml has synced into the repo,
or pass --config pointing at cplieger/ci's configs/cliff-stable.toml (or
cliff-alpha.toml for pre-1.0 repos).

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
import functools
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import yaml

if sys.version_info < (3, 11):  # noqa: UP036 - the guard IS the feature
    sys.exit('error: this script needs Python 3.11+')

# Imported after the guard: inventory needs tomllib (3.11+).
import ghrest
from inventory import Repo

SEMVER_TAG = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')
GITHUB_REMOTE = re.compile(
    r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?'
)
RELEASE_LIST_CAP = 1000
SBOM_ASSET = 'sbom.spdx.json'
SBOM_BUNDLE = f'{SBOM_ASSET}.sigstore.json'
# The identity docker-release.yaml signs the SBOM asset with (keyless).
SBOM_SIGNER = r'^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@'
SBOM_ISSUER = 'https://token.actions.githubusercontent.com'
RENDER_NOTES = Path(__file__).resolve().parent / 'render-notes.sh'
RELEASE_STATE = Path(__file__).resolve().parent / 'release-state.sh'
CARVE_OUTS = Path(__file__).resolve().with_name('backfill-carve-outs.yaml')


def run(
    cmd: list[str],
    cwd: Path,
    *,
    check: bool = True,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
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


def rest(call, *args):
    """A ghrest call; any API failure exits 2 with its message, as a failed gh run does."""
    try:
        return call(*args)
    except ghrest.ApiError as err:
        print(f'error: {err}', file=sys.stderr)
        sys.exit(2)


def repo_identity(repo_dir: Path) -> str:
    """`owner/name` of the `origin` remote, as GitHub names it (a renamed repository
    answers with its current name)."""
    url = run(['git', 'config', '--get', 'remote.origin.url'], repo_dir, check=False).stdout
    m = GITHUB_REMOTE.fullmatch(url.strip())
    if not m:
        print(
            f'error: origin is not a github.com remote: {url.strip() or "(none)"}', file=sys.stderr
        )
        sys.exit(2)
    return rest(ghrest.get, f'repos/{m[1]}')['full_name']


def release_of(repo: str, tag: str) -> dict:
    """The published release of `tag`, read fresh."""
    return rest(ghrest.get, f'repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe="")}')


def list_release_tags(repo: str) -> list[str]:
    """Published, non-draft, plain-semver release tags, sorted ascending."""
    per_page = 100
    try:
        entries = ghrest.pages(
            f'repos/{repo}/releases', per_page=per_page, cap=RELEASE_LIST_CAP // per_page
        )
    except ghrest.ApiError as err:
        print(
            f'error: {err}. Refusing to proceed on a possibly-truncated release list.',
            file=sys.stderr,
        )
        sys.exit(2)
    tags: list[tuple[int, int, int, str]] = []
    for entry in entries:
        tag = entry['tag_name']
        if entry.get('draft'):
            print(f'  skip {tag}: draft release', file=sys.stderr)
            continue
        m = SEMVER_TAG.match(tag)
        if not m:
            kind = 'prerelease' if entry.get('prerelease') else 'non-semver tag'
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


def lanes_at(repo_dir: Path, sha: str) -> list[str]:
    """Nested Go module lanes at `sha`, which release.yaml gives their own releases."""
    return Repo(str(repo_dir)).lanes(sha)


def download_asset(repo: str, tag: str, name: str, target: Path) -> bool:
    """Whether the release of `tag` has asset `name` and it was written to `target`."""
    try:
        release = ghrest.get(f'repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe="")}')
        ids = [a['id'] for a in release.get('assets') or [] if a.get('name') == name]
        if not ids:
            return False
        path = f'repos/{repo}/releases/assets/{ids[0]}'
        data = ghrest.DEFAULT.request('GET', path, headers=('Accept: application/octet-stream',))
    except ghrest.ApiError as err:
        print(f'  warning: {tag}: {name} could not be downloaded: {err}', file=sys.stderr)
        return False
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_bytes(data.body)
    return True


def repair_signers(repo_dir: Path, sha: str) -> list[str]:
    """The main commits after `sha` whose run may have repaired its Release: each first
    parent up to and including the first one carrying another stable tag, since a repair
    runs before any newer version publishes."""
    tip = 'refs/remotes/origin/main'
    if run(['git', 'rev-parse', '--verify', '-q', tip], repo_dir, check=False).returncode != 0:
        tip = 'HEAD'
    walk = run(
        ['git', 'rev-list', '--first-parent', '--ancestry-path', '--reverse', f'{sha}..{tip}'],
        repo_dir,
    )
    signers = []
    for commit in walk.stdout.split():
        signers.append(commit)
        tags = run(['git', 'tag', '--points-at', commit], repo_dir).stdout.split()
        if any(SEMVER_TAG.match(t) for t in tags):
            break
    return signers


# Once per tag: a middle release is both pairs' side.
@functools.cache
def release_sbom(
    repo_dir: Path, repo: str, tag: str, sha: str, dest: Path, cosign_bin: str
) -> Path | None:
    """The release's SBOM asset once its Sigstore bundle verifies as signed by a
    docker-release.yaml run of `repo` at `sha` or, for a repaired Release, at one of
    `repair_signers`, else None."""
    target = dest / tag.replace('/', '_')
    for asset in (SBOM_ASSET, SBOM_BUNDLE):
        if not download_asset(repo, tag, asset, target):
            if asset == SBOM_BUNDLE:
                print(
                    f'  warning: {tag} carries no {SBOM_BUNDLE}, so its SBOM is not used',
                    file=sys.stderr,
                )
            return None
    path = target / SBOM_ASSET
    # Every consumer of docker-release.yaml signs under the same identity; the
    # repository and commit extensions are what tie a bundle to this release.
    for signer in [sha, *repair_signers(repo_dir, sha)]:
        cmd = [
            cosign_bin,
            'verify-blob',
            '--bundle',
            str(target / SBOM_BUNDLE),
            '--certificate-oidc-issuer',
            SBOM_ISSUER,
            '--certificate-identity-regexp',
            SBOM_SIGNER,
            '--certificate-github-workflow-repository',
            repo,
            '--certificate-github-workflow-sha',
            signer,
            str(path),
        ]
        try:
            proc = run(cmd, repo_dir, check=False)
        except FileNotFoundError:
            print(f'error: {cosign_bin} not found. It verifies the SBOM assets.', file=sys.stderr)
            sys.exit(2)
        if proc.returncode == 0:
            return path
    print(
        f'  warning: {tag}: {SBOM_ASSET} does not verify against its bundle, so its SBOM is not used',
        file=sys.stderr,
    )
    return None


def security_shas(repo_dir: Path, repo: str, prevs: list[str], scratch: Path) -> Path:
    """A file of the merges of `repo`'s Renovate security PRs into main or dev
    that can fall inside a range starting at one of `prevs`."""
    floor = min(int(run(['git', 'log', '-1', '--format=%ct', p], repo_dir).stdout) for p in prevs)
    proc = run(
        ['bash', str(RELEASE_STATE), 'security-shas', str(floor)],
        repo_dir,
        env={**os.environ, 'GITHUB_REPOSITORY': repo},
    )
    path = scratch / 'security-shas'
    path.write_text(proc.stdout, encoding='utf-8')
    return path


def render(
    repo_dir: Path,
    cliff_bin: str,
    cosign_bin: str,
    config: Path,
    repo: str,
    prev: str,
    tag: str,
    tag_shas: dict[str, str],
    lanes: list[str],
    scratch: Path,
    security: Path,
) -> str:
    """The body render-notes.sh gives `tag`, never empty: every backfilled tag has a
    predecessor, so a compare link."""
    has_image = run(['git', 'cat-file', '-e', f'{tag}:Dockerfile'], repo_dir, check=False)
    site = 'docker' if has_image.returncode == 0 else 'go'
    out = scratch / f'{tag.replace("/", "_")}.md'
    cmd = [
        'bash',
        str(RENDER_NOTES),
        '--site',
        site,
        '--version',
        tag,
        '--release-commit',
        tag,
        '--repo',
        repo,
        '--config',
        str(config),
        '--go-lanes',
        json.dumps(lanes),
        '--security-shas',
        str(security),
        '--out',
        str(out),
    ]
    if site == 'docker':
        pair = (
            release_sbom(repo_dir, repo, prev, tag_shas[prev], scratch, cosign_bin),
            release_sbom(repo_dir, repo, tag, tag_shas[tag], scratch, cosign_bin),
        )
        if all(pair):
            cmd += ['--sbom-prev', str(pair[0]), '--sbom-new', str(pair[1])]
    # render-notes.sh refuses a token: git-cliff executes the repository's config.
    env = {k: v for k, v in os.environ.items() if k not in {'GITHUB_TOKEN', 'GH_TOKEN'}}
    env['CLIFF_BIN'] = cliff_bin
    run(cmd, repo_dir, env=env)
    return out.read_text(encoding='utf-8').strip()


def fetch_body(repo: str, tag: str) -> str:
    return release_of(repo, tag).get('body') or ''


def edit_body(repo: str, tag: str, body: str) -> None:
    rest(
        ghrest.send, 'PATCH', f'repos/{repo}/releases/{release_of(repo, tag)["id"]}', {'body': body}
    )


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
        edit_body(repo, tag, body)
    print(f'restored {len(manifest["entries"])} release bodies')
    return 0


def load_carve_outs(path: Path, repo: str) -> dict[str, str]:
    """This repo's carved-out tags and their reasons; exits 2 naming a malformed file."""
    try:
        doc = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as exc:
        print(f'error: carve-out list {path}: {exc}', file=sys.stderr)
        sys.exit(2)
    entries = doc.get('carve_outs') if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        print(f'error: carve-out list {path}: expected a `carve_outs:` list', file=sys.stderr)
        sys.exit(2)
    carved: dict[str, str] = {}
    for index, entry in enumerate(entries, 1):
        named, tag, reason = (
            entry.get(k) if isinstance(entry, dict) else None for k in ('repo', 'tag', 'reason')
        )
        texts = all(isinstance(v, str) and v.strip() for v in (named, tag, reason))
        if not texts or not SEMVER_TAG.match(tag):
            print(
                f'error: carve-out list {path}: entry {index} needs repo, a vX.Y.Z tag and a reason',
                file=sys.stderr,
            )
            sys.exit(2)
        if named == repo:
            carved[tag] = reason
    return carved


def select_pairs(
    pairs: list[tuple[str, str]], only: list[str], carved: dict[str, str], *, include_carved: bool
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The (prev, tag) pairs to plan, and the (tag, reason) pairs carved out of them."""
    selected: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for prev, tag in pairs:
        if only and tag not in only:
            continue
        if tag in carved and not include_carved:
            skipped.append((tag, carved[tag]))
            continue
        selected.append((prev, tag))
    return selected, skipped


@dataclass
class Plan:
    tag: str
    prev: str
    old: str
    new: str


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
        '--cosign-bin',
        default='cosign',
        help='cosign binary that verifies the SBOM assets (default: PATH)',
    )
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
    ap.add_argument(
        '--carve-outs',
        type=Path,
        default=CARVE_OUTS,
        help='releases whose bodies are hand-written (default: %(default)s)',
    )
    ap.add_argument(
        '--include-carved',
        action='store_true',
        help='regenerate carved-out releases too (deletes their hand-written text)',
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
    carved = load_carve_outs(args.carve_outs, repo)
    tags = list_release_tags(repo)
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
    selected, skipped = select_pairs(pairs, args.only, carved, include_carved=args.include_carved)
    for tag, reason in skipped:
        print(f'  skip {tag}: carved out ({reason})', file=sys.stderr)
    plans: list[Plan] = []
    unchanged = nonlinear = 0
    bodies: dict[tuple[str, str], str] = {}
    # security_shas needs at least one predecessor to find its floor.
    if selected:
        with tempfile.TemporaryDirectory(prefix='backfill-notes-') as tmp:
            security = security_shas(repo_dir, repo, [p for p, _ in selected], Path(tmp))
            for prev, tag in selected:
                proc = run(['git', 'merge-base', '--is-ancestor', prev, tag], repo_dir, check=False)
                if proc.returncode != 0:
                    print(
                        f'!! {tag}: predecessor {prev} is not an ancestor (non-linear history) '
                        '- skipping this pair',
                        file=sys.stderr,
                    )
                    nonlinear += 1
                    continue
                bodies[prev, tag] = render(
                    repo_dir,
                    args.cliff_bin,
                    args.cosign_bin,
                    config,
                    repo,
                    prev,
                    tag,
                    tag_shas,
                    lanes_at(repo_dir, tag),
                    Path(tmp),
                    security,
                )
    for (prev, tag), new_body in bodies.items():
        old_body = fetch_body(repo, tag)
        if normalize(old_body) == normalize(new_body):
            print(f'== {tag}: unchanged')
            unchanged += 1
            continue
        plans.append(Plan(tag=tag, prev=prev, old=old_body, new=new_body))
        print(f'== {tag}: {prev}..{tag}')
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
            f'DRY-RUN (use --apply to edit): {len(plans)} would change, {unchanged} unchanged, '
            f'{nonlinear} skipped non-linear, {len(skipped)} carved out'
        )
        return 0
    if not plans:
        print(
            f'nothing to apply: {unchanged} unchanged, {nonlinear} skipped non-linear, '
            f'{len(skipped)} carved out'
        )
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
        current = fetch_body(repo, p.tag)
        if current != p.old:
            print(
                f'!! {p.tag}: body changed since review; skipping (re-run to pick it up)',
                file=sys.stderr,
            )
            drifted += 1
            continue
        edit_body(repo, p.tag, p.new)
        if normalize(fetch_body(repo, p.tag)) != normalize(p.new):
            print(
                f'error: post-edit verification failed for {p.tag}; STOPPING. '
                f'Restore with: --restore {backup_dir}',
                file=sys.stderr,
            )
            return 2
        print(f'applied {p.tag}')
        applied += 1

    print(
        f'\napplied: {applied}, {unchanged} unchanged, {nonlinear} skipped non-linear, '
        f'{drifted} drifted, '
        f'{len(skipped)} carved out'
    )
    print(f'backups: {backup_dir}  (restore with --restore {backup_dir})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
