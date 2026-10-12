#!/usr/bin/env python3
"""Push the generated Docker Hub overview page to every image repo, out of band.

The stable release writes each repo's Hub page; this renders it from the local
working tree and PATCHes it directly, so a renderer change need not wait on 22
release cadences. It also sets the Hub short description from the GitHub About
line (read with an authenticated `gh api`), cut to Hub's 100-byte limit and left
alone when the line is empty or unreadable. Dry-run by default; `--apply` writes.
Credentials: DOCKERHUB_TOKEN with DOCKERHUB_USERNAME, else the `docker login`
entry in ~/.docker/config.json; the token is never printed.

Usage:
    sync-hub-overviews.py --workspace /workspace              # dry run, all repos
    sync-hub-overviews.py --only docker-radvd --apply         # one repo, for real
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
# Hub answers 400 to a longer short description, so it is cut here the way the
# release's peter-evans/dockerhub-description step cuts it: at 100 UTF-8 bytes.
SHORT_DESCRIPTION_MAX_BYTES = 100


def short_description(about: str) -> str:
    """The About line cut to Hub's byte limit without splitting a character."""
    raw = about.encode()[:SHORT_DESCRIPTION_MAX_BYTES]
    return raw.decode('utf-8', 'ignore')


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


def hub_get_repo(jwt: str, image: str) -> dict | None:
    """The Hub repository record, or None when it is unreadable."""
    try:
        return _hub_call(f'/repositories/{image}/', jwt=jwt)
    except urllib.error.HTTPError as e:
        print(f'    read failed: HTTP {e.code}', file=sys.stderr)
        return None


def hub_patch(jwt: str, image: str, fields: dict[str, str]) -> bool:
    try:
        _hub_call(f'/repositories/{image}/', jwt=jwt, data=fields, method='PATCH')
        return True
    except urllib.error.HTTPError as e:
        print(f'    write failed: HTTP {e.code} {e.read()[:200]!r}', file=sys.stderr)
        return False


def github_about(owner: str, repo: str) -> str | None:
    """The repo's GitHub About line, '' when unset, None when unreadable."""
    proc = subprocess.run(
        ['gh', 'api', f'repos/{owner}/{repo}', '--jq', '.description // ""'],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        print(f'    About line unreadable: {lines[-1] if lines else "gh failed"}', file=sys.stderr)
        return None
    return proc.stdout.strip()


def pending_fields(
    page: str, about: str | None, current: dict[str, str | None]
) -> tuple[dict[str, str], list[str]]:
    """The Hub fields that differ from the rendered state, and notes on skipped ones."""
    fields: dict[str, str] = {}
    notes: list[str] = []
    if (current.get('full_description') or '').strip() != page.strip():
        fields['full_description'] = page
    if about is None:
        return fields, notes
    if not about:
        notes.append('GitHub About line is empty; short description left alone')
        return fields, notes
    short = short_description(about)
    if short != about:
        notes.append(
            f'GitHub About line is {len(about.encode())} bytes, cut to '
            f'{len(short.encode())} for the {SHORT_DESCRIPTION_MAX_BYTES}-byte limit'
        )
    if short != (current.get('description') or ''):
        fields['description'] = short
    return fields, notes


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

    changed = same = skipped = failed = unreadable = 0
    for repo in repos:
        image = f'{args.owner}/{repo.name}'
        print(f'{repo.name}')
        page = render(repo, args.owner, args.ref)
        if page is None:
            skipped += 1
            continue
        current = hub_get_repo(jwt, image)
        if current is None:
            failed += 1
            continue
        about = github_about(args.owner, repo.name)
        unreadable += about is None
        fields, notes = pending_fields(page, about, current)
        for note in notes:
            print(f'    {note}')
        if not fields:
            print(f'    already current ({len(page.encode())} bytes)')
            same += 1
            continue
        if 'full_description' in fields:
            on_hub = len((current.get('full_description') or '').encode())
            print(f'    {on_hub} bytes on Hub -> {len(page.encode())} bytes rendered')
        if 'description' in fields:
            print(f'    short description -> {fields["description"]!r}')
        if not args.apply:
            changed += 1
            continue
        if hub_patch(jwt, image, fields):
            print('    pushed')
            changed += 1
        else:
            failed += 1

    verb = 'pushed' if args.apply else 'would change'
    print(
        f'\n{verb}: {changed}   already current: {same}   '
        f'render-skipped: {skipped}   failed: {failed}   About unreadable: {unreadable}'
    )
    if not args.apply and changed:
        print('dry run, re-run with --apply to write')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
