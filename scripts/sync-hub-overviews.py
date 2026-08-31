#!/usr/bin/env python3
"""Push the generated Docker Hub overview page to every image repo, out of band.

A release is the normal way each repo's Hub page gets refreshed, and it is not
the only way: the page is a plain API field, so this script renders and PATCHes
it directly. Two situations need that.

    * Adopting the overview page at all. Until a repo cuts its next genuine
      release its Hub listing still shows the old full README, and a stable repo
      can go months without one.
    * Changing the renderer. A fleet-wide re-sync otherwise waits on 22
      independent release cadences.

The daily stale-rebuild fan-out does NOT help: every Hub step in
docker-release.yaml is gated on `nochange.skip != 'true'`, so a rebuild that
finds nothing changed leaves the Hub page alone by design.

Dry-run by default, like backfill-release-notes.py: it renders every page and
reports what would change, and only `--apply` writes. Rendering happens from the
local working tree, so run it on the tree you intend to publish.

Credentials, in order: DOCKERHUB_TOKEN (with DOCKERHUB_USERNAME), else the
`docker login` entry in ~/.docker/config.json. The token is never printed.

Usage:
    sync-hub-overviews.py --workspace /workspace              # dry run, all repos
    sync-hub-overviews.py --only docker-radvd --apply         # one repo, for real
    sync-hub-overviews.py --workspace /workspace --apply      # the whole fleet
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HUB_API = 'https://hub.docker.com/v2'
DOCKER_INDEX = 'https://index.docker.io/v1/'
RENDERER = (
    Path(__file__).resolve().parent.parent / 'actions/render-hub-overview/render-hub-overview.py'
)
TIMEOUT = 30


def hub_credentials() -> tuple[str, str]:
    """Return (username, secret) from the environment or the docker config."""
    user = os.environ.get('DOCKERHUB_USERNAME', '')
    token = os.environ.get('DOCKERHUB_TOKEN', '')
    if user and token:
        return user, token
    cfg = Path(os.environ.get('DOCKER_CONFIG', Path.home() / '.docker')) / 'config.json'
    if not cfg.is_file():
        raise SystemExit(
            'no credentials: set DOCKERHUB_USERNAME + DOCKERHUB_TOKEN, '
            f'or run `docker login` (looked for {cfg})'
        )
    auth = json.loads(cfg.read_text()).get('auths', {}).get(DOCKER_INDEX, {}).get('auth')
    if not auth:
        raise SystemExit(f'{cfg} holds no {DOCKER_INDEX} entry; run `docker login`')
    decoded = base64.b64decode(auth).decode()
    user, _, token = decoded.partition(':')
    if not user or not token:
        raise SystemExit(f'{cfg}: {DOCKER_INDEX} entry is not user:secret')
    return user, token


def _hub_call(path: str, jwt: str = '', data: dict | None = None, method: str = '') -> dict:
    """One authenticated Hub API call. Every URL here is built from the HUB_API
    constant, so the scheme is always https and never caller-controlled."""
    headers = {}
    if jwt:
        headers['Authorization'] = f'JWT {jwt}'
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(  # noqa: S310 - fixed https base, see docstring
        f'{HUB_API}{path}', data=body, headers=headers, method=method or None
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
        raw = resp.read()
        return json.loads(raw) if raw else {}


def hub_login(user: str, secret: str) -> str:
    try:
        return _hub_call('/users/login/', data={'username': user, 'password': secret})['token']
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit(
                f'Docker Hub rejected the credentials for {user} (HTTP {e.code}). '
                'The token is wrong, expired or revoked. A PAT that no longer '
                'authenticates here fails `docker login` too, so check it against '
                'the registry before assuming this endpoint is the problem.'
            ) from e
        raise SystemExit(f'Docker Hub login failed: HTTP {e.code}') from e
    except urllib.error.URLError as e:
        raise SystemExit(f'Docker Hub unreachable: {e.reason}') from e


def hub_get_description(jwt: str, image: str) -> str | None:
    """Current full_description, or None when the repo is unreadable."""
    try:
        return _hub_call(f'/repositories/{image}/', jwt=jwt).get('full_description') or ''
    except urllib.error.HTTPError as e:
        print(f'    read failed: HTTP {e.code}', file=sys.stderr)
        return None


def hub_put_description(jwt: str, image: str, body: str) -> bool:
    try:
        _hub_call(
            f'/repositories/{image}/',
            jwt=jwt,
            data={'full_description': body},
            method='PATCH',
        )
        return True
    except urllib.error.HTTPError as e:
        print(f'    write failed: HTTP {e.code} {e.read()[:200]!r}', file=sys.stderr)
        return False


def image_repos(workspace: Path, only: list[str]) -> list[Path]:
    """Local clones that publish an image: a Dockerfile plus a README."""
    found = []
    for d in sorted(p for p in workspace.iterdir() if p.is_dir()):
        if only and d.name not in only:
            continue
        if (d / 'Dockerfile').is_file() and (d / 'README.md').is_file():
            found.append(d)
    return found


def render(repo: Path, owner: str, ref: str) -> str | None:
    proc = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            '--repo-root',
            str(repo),
            '--owner',
            owner,
            '--repo',
            repo.name,
            '--image',
            f'{owner}/{repo.name}',
            '--ref',
            ref,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f'    render failed: {proc.stderr.strip().splitlines()[-1]}', file=sys.stderr)
        return None
    return proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--workspace', default='.', help='directory holding the repo clones')
    ap.add_argument('--only', action='append', default=[], help='limit to this repo (repeatable)')
    ap.add_argument('--owner', default='cplieger', help='GitHub owner (default: cplieger)')
    ap.add_argument('--ref', default='main', help='git ref the links point at (default: main)')
    ap.add_argument('--apply', action='store_true', help='write to Docker Hub (default: dry run)')
    args = ap.parse_args()

    if not RENDERER.is_file():
        raise SystemExit(f'renderer not found at {RENDERER}')

    repos = image_repos(Path(args.workspace), args.only)
    if not repos:
        raise SystemExit(f'no image repos found under {args.workspace}')

    user, secret = hub_credentials()
    jwt = hub_login(user, secret)
    print(f'authenticated to Docker Hub as {user}\n')

    changed = same = skipped = failed = 0
    for repo in repos:
        image = f'{args.owner}/{repo.name}'
        print(f'{repo.name}')
        page = render(repo, args.owner, args.ref)
        if page is None:
            skipped += 1
            continue
        current = hub_get_description(jwt, image)
        if current is None:
            failed += 1
            continue
        if current.strip() == page.strip():
            print(f'    already current ({len(page.encode())} bytes)')
            same += 1
            continue
        print(f'    {len(current.encode())} bytes on Hub -> {len(page.encode())} bytes rendered')
        if not args.apply:
            changed += 1
            continue
        if hub_put_description(jwt, image, page):
            print('    pushed')
            changed += 1
        else:
            failed += 1

    verb = 'pushed' if args.apply else 'would change'
    print(
        f'\n{verb}: {changed}   already current: {same}   '
        f'render-skipped: {skipped}   failed: {failed}'
    )
    if not args.apply and changed:
        print('dry run — re-run with --apply to write')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
