#!/usr/bin/env python3
"""Promote a commit on a two-branch repository's `dev` to its `main`.

promote.yaml runs one subcommand per job or step, so a repository's own
configuration and the credential that moves `main` never share a step:

    snapshot  resolves --repo and --target to T on dev's first-parent history
              and main's head M, the run's only authority-bearing values.
    preview   renders each lane's stable version and notes for a promotion of T;
              runs the repository's cliff config and holds no token.
    check     runs the five promotion checks over exactly T and M with this
              repository's code and plans R in --plan-file; reads only.
    create    creates R on GitHub, a commit no ref names yet (PROMOTE_PAT).
    tag       tags the promoted dev digest `promoted-<R>` on GHCR (PACKAGES_PAT).
    move      re-reads R and its tag, then moves main to R (force=false, PROMOTE_PAT).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import ghrest
import inventory
import release_channels as rc

OWNER = 'cplieger'
SCRIPTS = Path(__file__).resolve().parent
CI_ROOT = SCRIPTS.parent
# scripts/reconciliation.sh matches the same subject.
RECONCILIATION_SUBJECT = 'release: promote dev into main'
DIGEST_TRAILER = 'Promoted-Digest'
REGISTRY = 'ghcr.io'
# ghcr_retention.is_candidate deletes only a version whose every tag is a dev or a
# `sha-` tag, so this tag keeps a promoted digest for good.
PROMOTED_TAG_PREFIX = 'promoted-'
DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
CLONE_URL = 'https://github.com/{owner}/{repo}'
REPO_RE = re.compile(r'^[A-Za-z0-9._-]+$')
FULL_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
TARGET_RE = re.compile(r'^[0-9a-f]{7,40}$')
MAIN_RUN_POLL_SECONDS = 30
MAIN_RUN_WAIT_SECONDS = 45 * 60
# A push to main lists its release run within seconds, so a commit this old
# with no run listed has none in flight.
MAIN_RUN_GRACE_SECONDS = 10 * 60
MANIFEST_ACCEPT = (
    'application/vnd.oci.image.index.v1+json, '
    'application/vnd.docker.distribution.manifest.list.v2+json, '
    'application/vnd.oci.image.manifest.v1+json, '
    'application/vnd.docker.distribution.manifest.v2+json'
)
INDEX_TYPES = frozenset(
    {
        'application/vnd.oci.image.index.v1+json',
        'application/vnd.docker.distribution.manifest.list.v2+json',
    }
)
SINGLE_MANIFEST = 'single'


class GhError(Exception):
    """A GitHub, registry, git or tool read this script cannot proceed past."""


def gh_json(path: str):
    try:
        return ghrest.get(path)
    except ghrest.ApiError as err:
        raise GhError(str(err)) from None


def gh_send(method: str, path: str, body: dict):
    try:
        return ghrest.send(method, path, body)
    except ghrest.ApiError as err:
        raise GhError(str(err)) from None


def git(clone: Path, *args: str, stdin: str | None = None, env: dict | None = None) -> str:
    proc = subprocess.run(
        ['git', '-C', str(clone), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise GhError(f'git {" ".join(args[:2])} in {clone} failed: {proc.stderr.strip()}')
    return proc.stdout


def write_summary(text: str) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(text)
    print(text, end='')


def write_outputs(values: dict[str, str]) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    lines = ''.join(f'{k}={v}\n' for k, v in values.items())
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(lines)
    print(lines, end='')


def token_free_env(**extra: str) -> dict[str, str]:
    """The environment for a child that runs repository code or reads no API."""
    env = {k: v for k, v in os.environ.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN')}
    env.update(extra)
    return env


# ── Purity ────────────────────────────────────────────────────────────────────


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


class PurityInputError(GhError):
    """A purity file whose shape the check cannot read, so its pins cannot be judged."""


def _json_sections(path: str, text: str, sections: tuple[str, ...]) -> list[tuple[str, dict]]:
    """(section, mapping) for each present section of a JSON manifest, as npm and JSR
    read it: the document and every present section must be objects."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise PurityInputError(f'{path} is not valid JSON ({err})') from None
    if not isinstance(data, dict):
        raise PurityInputError(f'{path} is not a JSON object')
    found = []
    for section in sections:
        mapping = data.get(section)
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            raise PurityInputError(f'{section} in {path} is not an object')
        found.append((section, mapping))
    return found


def _package_json_violations(path: str, text: str) -> list[str]:
    found = []
    sections = ('dependencies', 'peerDependencies', 'optionalDependencies')
    for section, mapping in _json_sections(path, text, sections):
        for name, spec in mapping.items():
            # An alias spec (`npm:@cplieger/x@1.0.0-dev.1`) names the package
            # in the value, so both sides are judged.
            if _first_party(f'{name} {spec}') and '-dev.' in str(spec):
                found.append(f'{path}: {section} {name} {spec}')
    return found


def _jsr_json_violations(path: str, text: str) -> list[str]:
    return [
        f'{path}: imports {name} {spec}'
        for _, mapping in _json_sections(path, text, ('imports',))
        for name, spec in mapping.items()
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
    package.json, jsr.json and Dockerfile* files. Empty means pure. Raises
    PurityInputError when a JSON manifest has a shape it cannot read."""
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


# Shipped pin files only the inventory reads; lockfiles ship to no consumer.
_PIN_SURFACES = frozenset({'pins', 'bundled-tools'})


def pin_violations(repo: inventory.Repo, target: str) -> list[str]:
    found = []
    for path in repo.surfaces(target):
        if inventory.surface_kind(path) not in _PIN_SURFACES:
            continue
        records, _ = repo.parsed(target, path) or ([], '')
        found += [
            f'{path}: {r.identity} {r.value}'
            for r in records
            if _first_party(r.identity) and '-dev.' in r.value
        ]
    return sorted(found)


def check_purity(repo: inventory.Repo, target: str) -> list[str]:
    files = {p: repo.blob(target, p) or '' for p in repo.paths(target) if is_purity_file(p)}
    lanes = repo.lanes(target)
    refusals, found = [], []
    for path in sorted(files):
        try:
            found += purity_violations({path: files[path]})
        except PurityInputError as err:
            refusals.append(
                f'lane {inventory.lane_of(path, lanes)}: {err}, so the first-party pins in '
                f'{path} cannot be judged. Fix {path} on dev'
            )
    found += pin_violations(repo, target)
    return refusals + [
        f'lane {inventory.lane_of(v.split(":", 1)[0], lanes)}: a first-party dependency is '
        f'pinned at a dev version ({v}). Promote it first and stabilize this commit'
        for v in found
    ]


# ── Repository layout and path significance ───────────────────────────────────


@dataclass(frozen=True)
class Layout:
    """release.yaml's detect view of one revision: its type, TS subpackages and Go lanes."""

    type: str
    subpackages: list[str]
    lanes: list[str]

    def env(self) -> dict[str, str]:
        return {
            'REPO_TYPE': self.type,
            'SUBPACKAGES_JSON': json.dumps(self.subpackages, separators=(',', ':')),
            'GO_LANES_JSON': json.dumps(self.lanes, separators=(',', ':')),
        }


def layout_of(repo: inventory.Repo, sha: str) -> Layout:
    paths = repo.paths(sha)
    root = {p for p in paths if '/' not in p}
    kind = 'none'
    for name, value in (('Dockerfile', 'docker'), ('jsr.json', 'ts'), ('go.mod', 'go')):
        if name in root:
            kind = value
            break
    subs = sorted(
        {p.split('/', 1)[0] for p in paths if p.count('/') == 1 and p.endswith('/jsr.json')}
    )
    return Layout(kind, subs, sorted(repo.lanes(sha)))


def path_significance(clone: Path, base: str, head: str, layout: Layout) -> dict[str, str]:
    """path-significance.sh MODE=paths over the tree diff base..head."""
    proc = subprocess.run(
        ['bash', str(SCRIPTS / 'path-significance.sh')],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
        env=token_free_env(MODE='paths', FROM=base, TO=head, **layout.env()),
    )
    if proc.returncode != 0:
        raise GhError(
            f'path-significance.sh {base[:12]}..{head[:12]} failed: {proc.stderr.strip()}'
        )
    return dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)


def affected_lanes(sig: dict[str, str]) -> list[str]:
    """'.' when the root publishes (TS subpackages share its version), then each Go lane."""
    lanes = []
    if sig.get('root_changed') == 'true' or json.loads(sig.get('subpackages_to_publish') or '[]'):
        lanes.append('.')
    return lanes + json.loads(sig.get('go_modules_to_release') or '[]')


def check_affected(sig: dict[str, str]) -> list[str]:
    if affected_lanes(sig):
        return []
    return [
        (
            'nothing to publish: the tree diff from main to the target changes no path any lane '
            'ships (an empty rebuild on dev is not one, since main rebuilds its own images)'
        )
    ]


# ── Retained dev digest ───────────────────────────────────────────────────────


def check_digest(
    clone: Path, repo: str, target: str, layout: Layout, sig: dict[str, str]
) -> tuple[str, list[str]]:
    """(digest, refusals): the complete dev image promote-digest.sh resolves for T."""
    proc = subprocess.run(
        ['bash', str(SCRIPTS / 'promote-digest.sh'), target],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
        env=token_free_env(
            RELEASE_MODEL='two-branch',
            IMAGE_NAME=f'{OWNER}/{repo}',
            REGISTRY=REGISTRY,
            EXCLUDE_RE=sig.get('exclude_re', ''),
            GITHUB_REPOSITORY=f'{OWNER}/{repo}',
            **layout.env(),
        ),
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1:] or [f'exit {proc.returncode}']
        return '', [
            f'image: promote-digest.sh could not resolve a dev image for the target: {detail[0]}'
        ]
    out = dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)
    digest = out.get('promote_digest', '')
    if not digest:
        return '', [
            (
                f'image: no complete dev image to re-tag at {target[:12]} or a first-parent '
                'ancestor it carries unchanged (retention deleted it, or its build stopped before '
                'signing). Rebuild on dev and promote the new head'
            )
        ]
    print(
        f'image: re-tags {digest} built at {out.get("promote_source", "")[:12]} ({out.get("promote_via")})'
    )
    return digest, []


# ── Dominance ─────────────────────────────────────────────────────────────────


def load_classify():
    spec = importlib.util.spec_from_file_location('classify_repos', SCRIPTS / 'classify-repos.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sync_inputs(repo: str, work: Path, classify=None) -> tuple[Path, Path]:
    """The sync-owned set and the repo's canonical sync copies, from classify-repos.py's
    `sync_owned_patterns()` (globs) and `canonical_sources(repo)` ({dest: source path});
    GhError when either cannot be read."""
    module = classify or load_classify()
    patterns = getattr(module, 'sync_owned_patterns', None)
    sources = getattr(module, 'canonical_sources', None)
    failure = getattr(module, 'ClassifyError', None)
    if not callable(patterns) or not callable(sources) or not isinstance(failure, type):
        raise GhError(
            'classify-repos.py publishes no sync_owned_patterns()/canonical_sources()/'
            'ClassifyError, so dominance cannot judge a sync-owned path'
        )
    try:
        mapping = sources(repo)
    except failure as err:
        raise GhError(f'classify-repos.py: {err}') from None
    owned = work / 'sync-owned.txt'
    owned.write_text(''.join(f'{p}\n' for p in patterns()), encoding='utf-8')
    canonical = work / 'canonical'
    shutil.rmtree(canonical, ignore_errors=True)
    canonical.mkdir(parents=True)
    for dest, source in mapping.items():
        out = canonical / dest
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CI_ROOT / source, out)
    return owned, canonical


def check_dominance(
    repo: inventory.Repo, main: str, target: str, owned: Path, canonical: Path
) -> list[str]:
    base = repo.git('merge-base', target, main).strip()
    try:
        result = inventory.dominance(
            repo, base, main, target, inventory.load_sync_owned(str(owned)), canonical
        )
    except inventory.InventoryError as exc:
        return [f'dominance: uncomparable: {exc}']
    if result['verdict'] == 'pass':
        print(inventory.render_dominance(result))
        return []
    lines = inventory.render_dominance(result).splitlines()
    return [
        line if line.startswith('dominance:') else f'dominance: {line}'
        for line in lines
        if not line.startswith('ok ')
    ]


# ── Image vulnerability state ─────────────────────────────────────────────────


def registry_get(url: str, token: str, accept: str = '', method: str = 'GET'):
    """(headers, body bytes) of a registry read; GhError on any failure."""
    return registry_request(url, f'Bearer {token}' if token else '', accept, method)


def registry_request(
    url: str,
    authorization: str,
    accept: str = '',
    method: str = 'GET',
    body: bytes | None = None,
    content_type: str = '',
):
    req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 - fixed https registry URLs
    if authorization:
        req.add_header('Authorization', authorization)
    if accept:
        req.add_header('Accept', accept)
    if content_type:
        req.add_header('Content-Type', content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return dict(resp.headers.items()), resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GhError(f'{method} {url}: {exc}') from None


def registry_json(url: str, token: str, accept: str = ''):
    _, body = registry_get(url, token, accept)
    try:
        return json.loads(body)
    except ValueError:
        raise GhError(f'GET {url}: not JSON') from None


def registry_token(repo: str) -> str:
    doc = registry_json(f'https://{REGISTRY}/token?scope=repository:{OWNER}/{repo}:pull', '')
    if not isinstance(doc, dict) or not isinstance(doc.get('token'), str):
        raise GhError(f'{REGISTRY} answered no pull token for {OWNER}/{repo}')
    return doc['token']


def registry_push_token(repo: str, credential: str) -> str:
    basic = base64.b64encode(f'{OWNER}:{credential}'.encode()).decode()
    _, body = registry_request(
        f'https://{REGISTRY}/token?scope=repository:{OWNER}/{repo}:pull,push&service={REGISTRY}',
        f'Basic {basic}',
    )
    try:
        token = json.loads(body).get('token')
    except ValueError, AttributeError:
        token = None
    if not isinstance(token, str) or not token:
        raise GhError(f'{REGISTRY} answered no push token for {OWNER}/{repo}')
    return token


def tag_digest(repo: str, tag: str, token: str) -> str:
    headers, _ = registry_get(
        f'https://{REGISTRY}/v2/{OWNER}/{repo}/manifests/{tag}', token, MANIFEST_ACCEPT, 'HEAD'
    )
    digest = {k.lower(): v for k, v in headers.items()}.get('docker-content-digest', '')
    if not digest.startswith('sha256:'):
        raise GhError(f'{REGISTRY}/{OWNER}/{repo}:{tag} answered no digest')
    return digest


def platforms(repo: str, digest: str, token: str) -> dict[str, str]:
    """{os/arch[/variant]: manifest digest} of every published platform of `digest`."""
    doc = registry_json(
        f'https://{REGISTRY}/v2/{OWNER}/{repo}/manifests/{digest}', token, MANIFEST_ACCEPT
    )
    if doc.get('mediaType') not in INDEX_TYPES and 'manifests' not in doc:
        return {SINGLE_MANIFEST: digest}
    out = {}
    for m in doc.get('manifests') or []:
        p = m.get('platform') or {}
        if p.get('os') in (None, 'unknown'):
            continue  # attestation manifests
        key = '/'.join(x for x in (p.get('os'), p.get('architecture'), p.get('variant')) if x)
        out[key] = m['digest']
    if not out:
        raise GhError(f'{OWNER}/{repo}@{digest} lists no platform image')
    return out


def trivy_findings(ref: str) -> set[tuple[str, str]]:
    """Fixable HIGH and CRITICAL (vulnerability ID, package) pairs, against the one DB
    snapshot in TRIVY_CACHE_DIR (never updated here)."""
    cmd = [
        os.environ.get('TRIVY_BIN', 'trivy'),
        'image',
        '--quiet',
        '--format',
        'json',
        '--scanners',
        'vuln',
        '--severity',
        'HIGH,CRITICAL',
        '--ignore-unfixed',
        '--skip-db-update',
        '--skip-java-db-update',
        '--timeout',
        '15m',
    ]
    if os.environ.get('TRIVY_CACHE_DIR'):
        cmd += ['--cache-dir', os.environ['TRIVY_CACHE_DIR']]
    proc = subprocess.run([*cmd, ref], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GhError(f'trivy over {ref} failed: {proc.stderr.strip()[-400:]}')
    try:
        report = json.loads(proc.stdout or '{}')
    except ValueError:
        raise GhError(f'trivy over {ref} printed no JSON report') from None
    return {
        (v['VulnerabilityID'], v['PkgName'])
        for result in report.get('Results') or []
        for v in result.get('Vulnerabilities') or []
    }


def release_runs(repo: str, sha: str) -> list[dict]:
    data = gh_json(
        f'repos/{OWNER}/{repo}/actions/workflows/release.yaml/runs'
        f'?branch=main&head_sha={sha}&per_page=100'
    )
    return (data or {}).get('workflow_runs') or []


def wait_for_main_release(
    repo: str, main: str, committed: datetime, *, sleep=time.sleep, clock=time.monotonic
) -> str:
    """'' once no release run of main's head is in flight, else the reason it timed out."""
    start = clock()
    while True:
        runs = release_runs(repo, main)
        busy = [r for r in runs if r.get('status') != 'completed']
        age = (datetime.now(UTC) - committed).total_seconds()
        if not busy and (runs or age >= MAIN_RUN_GRACE_SECONDS):
            return ''
        if clock() - start >= MAIN_RUN_WAIT_SECONDS:
            what = f'{len(busy)} release run(s) still running' if busy else 'no release run listed'
            return f"image: main's head {main[:12]} has {what} after {MAIN_RUN_WAIT_SECONDS // 60} minutes"
        sleep(MAIN_RUN_POLL_SECONDS)


def main_stable_tag(clone: Path, main: str) -> str:
    tags = [t for t in git(clone, 'tag', '--merged', main).split() if rc.is_stable_tag(t)]
    return rc.newest_stable_tag(tags)


def check_vulnerabilities(clone: Path, repo: str, main: str, digest: str) -> list[str]:
    committed = datetime.fromisoformat(git(clone, 'log', '-1', '--format=%cI', main).strip())
    waited = wait_for_main_release(repo, main, committed)
    if waited:
        return [waited]
    tag = main_stable_tag(clone, main)
    if not tag:
        print('image: main has no stable image yet, so a promotion undoes no fix of it')
        return []
    token = registry_token(repo)
    main_digest = tag_digest(repo, tag, token)
    ours, theirs = platforms(repo, digest, token), platforms(repo, main_digest, token)
    if (SINGLE_MANIFEST in ours) != (SINGLE_MANIFEST in theirs):
        shape = {True: 'one manifest', False: 'a multi-platform index'}
        mismatch = (
            f"image: main's {tag} is {shape[SINGLE_MANIFEST in theirs]} and the dev image is "
            f'{shape[SINGLE_MANIFEST in ours]}, so no platform of one can be matched to the other. '
            'Rebuild on dev and promote the new head'
        )
        return [mismatch]
    found = [
        f"image {plat}: main's {tag} publishes this platform and the dev image does not. "
        'Build it on dev and promote the new head'
        for plat in sorted(theirs.keys() - ours.keys())
    ]
    for plat, ref in sorted(ours.items()):
        new = trivy_findings(f'{REGISTRY}/{OWNER}/{repo}@{ref}')
        old = (
            trivy_findings(f'{REGISTRY}/{OWNER}/{repo}@{theirs[plat]}') if plat in theirs else set()
        )
        found += [
            f"image {plat}: {vid} in {pkg} is fixable and absent from main's {tag}. "
            'Rebuild on dev and promote the new head'
            for vid, pkg in sorted(new - old)
        ]
    return found


# ── The reconciliation commit ─────────────────────────────────────────────────


def reconciliation_message(digest: str) -> str:
    message = RECONCILIATION_SUBJECT
    if digest:
        message += f'\n\n{DIGEST_TRAILER}: {digest}'
    return message + '\n'


LOCAL_IDENTITY = {
    'GIT_AUTHOR_NAME': 'promote',
    'GIT_AUTHOR_EMAIL': 'promote@invalid',
    'GIT_COMMITTER_NAME': 'promote',
    'GIT_COMMITTER_EMAIL': 'promote@invalid',
}


def build_reconciliation(clone: Path, main: str, target: str, digest: str) -> dict:
    """R (tree T's, parents [M, T]) built locally and read back by reconciliation.sh,
    so the commit written is one the stable pipeline recognises."""
    tree = git(clone, 'rev-parse', f'{target}^{{tree}}').strip()
    message = reconciliation_message(digest)
    sha = git(
        clone,
        'commit-tree',
        tree,
        '-p',
        main,
        '-p',
        target,
        '-F',
        '-',
        stdin=message,
        env=token_free_env(**LOCAL_IDENTITY),
    ).strip()
    proc = subprocess.run(
        [
            'bash',
            '-c',
            '. "$1" && is_reconciliation "$2" && { [ -z "$3" ] || [ "$(promoted_digest "$2")" = "$3" ]; }',
            '_',
            str(SCRIPTS / 'reconciliation.sh'),
            sha,
            digest,
        ],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GhError(
            f'reconciliation.sh does not recognise the commit built for R: {proc.stderr.strip()}'
        )
    return {'sha': sha, 'tree': tree, 'parents': [main, target], 'message': message}


# ── Clones ────────────────────────────────────────────────────────────────────


def clone_repo(repo: str, dest: Path, *, bare: bool) -> Path:
    """A fresh clone; bare where nothing may read a work tree."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = CLONE_URL.format(owner=OWNER, repo=repo)
    args = ['git', 'clone', '--quiet', *(['--bare'] if bare else []), url, str(dest)]
    proc = subprocess.run(args, capture_output=True, text=True, check=False, env=token_free_env())
    if proc.returncode != 0:
        raise GhError(f'clone of {repo} failed: {proc.stderr.strip()}')
    return dest


def branch_head(clone: Path, branch: str, *, bare: bool) -> str:
    ref = f'refs/heads/{branch}' if bare else f'refs/remotes/origin/{branch}'
    return git(clone, 'rev-parse', '--verify', f'{ref}^{{commit}}').strip()


def on_dev_first_parent(clone: Path, target: str, *, bare: bool) -> bool:
    ref = 'refs/heads/dev' if bare else 'refs/remotes/origin/dev'
    return target in git(clone, 'rev-list', '--first-parent', ref).split()


# ── snapshot ──────────────────────────────────────────────────────────────────


def enrolment_refusal(repo: str) -> str:
    """Why `repo` cannot be promoted, or ''; the shape checks read nothing."""
    if not REPO_RE.fullmatch(repo):
        return f'{repo!r} is not a repository name'
    if repo in rc.SINGLE_MAIN_REPOS:
        return f'{repo} publishes from main directly and has no dev branch'
    meta = gh_json(f'repos/{OWNER}/{repo}') or {}
    if meta.get('archived') or meta.get('fork'):
        return f'{repo} is archived or a fork'
    if meta.get('visibility') != 'public':
        return f'{repo} is not public, and only a public repository has a dev channel'
    if meta.get('default_branch') != 'dev':
        return f'the default branch of {repo} is {meta.get("default_branch") or "unknown"}, not dev'
    if not rc.is_two_branch(meta):
        return f'{repo} is not a two-branch repository'
    return ''


def cmd_snapshot(args) -> int:
    try:
        refusal = enrolment_refusal(args.repo)
        if refusal:
            print(f'::error::refused: {refusal}', file=sys.stderr)
            return 1
        clone = clone_repo(args.repo, Path(args.work_dir) / f'{args.repo}.git', bare=True)
        main = branch_head(clone, 'main', bare=True)
        target = branch_head(clone, 'dev', bare=True)
        if args.target:
            if not TARGET_RE.fullmatch(args.target):
                print(f'::error::refused: {args.target!r} is not a commit SHA', file=sys.stderr)
                return 1
            target = git(clone, 'rev-parse', '--verify', f'{args.target}^{{commit}}').strip()
    except GhError as err:
        print(f'::error::{err}', file=sys.stderr)
        return 1
    if not on_dev_first_parent(clone, target, bare=True):
        print(
            f'::error::refused: {target} is not on the first-parent history of dev', file=sys.stderr
        )
        return 1
    if git(clone, 'merge-base', target, main).strip() == target:
        print(f'::error::refused: {target[:12]} is already part of main', file=sys.stderr)
        return 1
    write_outputs({'repo': args.repo, 'target': target, 'main': main})
    write_summary(
        f'## Promotion snapshot\n\n**{args.repo}**: dev `{target[:12]}` over main `{main[:12]}`.\n'
    )
    return 0


# ── preview ───────────────────────────────────────────────────────────────────


def run_tool(clone: Path, script: str, *args: str, **env: str) -> dict[str, str]:
    """Run a release script the way release.yaml does; its $GITHUB_OUTPUT as a dict."""
    out = clone.parent / f'{clone.name}.output'
    out.write_text('')
    proc = subprocess.run(
        ['bash', str(SCRIPTS / script), *args],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
        env=token_free_env(GITHUB_OUTPUT=str(out), **env),
    )
    if proc.returncode != 0:
        raise GhError(f'{script} {" ".join(args)} failed: {proc.stderr.strip()[-600:]}')
    return dict(line.split('=', 1) for line in out.read_text().splitlines() if '=' in line)


def render_lane_notes(clone: Path, repo: str, key: str, lane: dict, layout: Layout, r: str) -> str:
    notes = clone.parent / f'{repo}-notes.md'
    site = ['--site', layout.type] if key == '.' else ['--site', 'lane', '--lane', key]
    render = [
        'bash',
        str(SCRIPTS / 'render-notes.sh'),
        '--release-model',
        'two-branch',
        *site,
        '--version',
        lane['version'],
        '--release-commit',
        r,
        '--kind-note',
        lane.get('kind_note', ''),
        '--repo',
        f'{OWNER}/{repo}',
        '--go-lanes',
        json.dumps(layout.lanes),
        '--out',
        str(notes),
    ]
    proc = subprocess.run(
        render, cwd=clone, capture_output=True, text=True, check=False, env=token_free_env()
    )
    if proc.returncode != 0:
        raise GhError(f'render-notes.sh for {key} failed: {proc.stderr.strip()[-600:]}')
    return notes.read_text().rstrip()


def cmd_preview(args) -> int:
    """The stable release's own pending/number/notes steps, run at a local R."""
    try:
        clone = clone_repo(args.repo, Path(args.work_dir) / args.repo, bare=False)
        git(clone, 'checkout', '--quiet', '--detach', args.target)
        r = build_reconciliation(clone, args.main, args.target, '')['sha']
        git(clone, 'checkout', '--quiet', '--detach', r)
        layout = layout_of(inventory.Repo(str(clone)), args.target)
        exclude = '\n'.join(f'{lane}/**' for lane in layout.lanes)
        pending = run_tool(clone, 'release-state.sh', 'pending', CHANNEL='stable', **layout.env())
        state = json.loads(pending.get('state') or '{}')
        if pending.get('any') == 'true':
            numbered = run_tool(
                clone,
                'release-state.sh',
                'number',
                STATE=pending['state'],
                CLIFF_SCRIPT=str(CI_ROOT / 'actions/git-cliff-version/compute.sh'),
                EXCLUDE_PATHS=exclude,
                **layout.env(),
            )
            state = json.loads(numbered['state'])
        lines = [f'## Promotion preview: {args.repo}', '']
        published = [(k, v) for k, v in sorted(state.items()) if v.get('version')]
        if not published:
            lines.append('No lane publishes: the stable release would carry nothing.')
        for key, lane in published:
            notes = render_lane_notes(clone, args.repo, key, lane, layout, r)
            name = 'root' if key == '.' else key
            lines += [f'### {name} {lane["version"]}', '', notes, '']
    except (GhError, inventory.InventoryError) as err:
        print(f'::error::preview: {err}', file=sys.stderr)
        return 1
    write_summary('\n'.join(lines) + '\n')
    return 0


# ── check ─────────────────────────────────────────────────────────────────────


def run_checks(
    clone: Path, repo: str, main: str, target: str, work: Path, classify=None
) -> tuple[list[str], str]:
    """(refusals, digest) of the five promotion checks over exactly `main` and `target`."""
    inv = inventory.Repo(str(clone))
    layout = layout_of(inv, target)
    sig = path_significance(clone, main, target, layout)
    refusals = check_purity(inv, target)
    digest = ''
    if layout.type == 'docker':
        digest, found = check_digest(clone, repo, target, layout, sig)
        refusals += found
    try:
        owned, canonical = sync_inputs(repo, work, classify)
        refusals += check_dominance(inv, main, target, owned, canonical)
    except GhError as err:
        refusals.append(f'dominance: {err}')
    refusals += check_affected(sig)
    if digest:
        refusals += check_vulnerabilities(clone, repo, main, digest)
    return refusals, digest


def cmd_check(args) -> int:
    work = Path(args.work_dir)
    if not REPO_RE.fullmatch(args.repo) or not all(
        FULL_SHA_RE.fullmatch(s) for s in (args.main, args.target)
    ):
        print('::error::check needs a repository name and two full commit SHAs', file=sys.stderr)
        return 2
    try:
        clone = clone_repo(args.repo, work / f'{args.repo}.git', bare=True)
        head = branch_head(clone, 'main', bare=True)
        if head != args.main:
            refusals, digest = (
                [f'main moved since the snapshot ({args.main[:12]} -> {head[:12]}), so rerun'],
                '',
            )
        elif not on_dev_first_parent(clone, args.target, bare=True):
            refusals, digest = (
                [f"{args.target[:12]} is no longer on dev's first-parent history"],
                '',
            )
        else:
            refusals, digest = run_checks(clone, args.repo, args.main, args.target, work)
        plan = None if refusals else build_reconciliation(clone, args.main, args.target, digest)
    except (GhError, inventory.InventoryError) as err:
        refusals, plan = [f'a read failed: {err}'], None
    lines = [f'## Promotion checks: {args.repo}', '']
    if refusals:
        lines += [f'- {r}' for r in refusals]
        write_summary('\n'.join(lines) + '\n')
        for r in refusals:
            print(f'::error::{r}', file=sys.stderr)
        return 1
    Path(args.plan_file).write_text(
        json.dumps({'repo': args.repo, 'digest': digest, **plan}, indent=2) + '\n'
    )
    lines.append(
        f'Every check passed: R `{plan["sha"][:12]}` (tree of `{args.target[:12]}`, '
        f'parents `{args.main[:12]}` and `{args.target[:12]}`).'
    )
    if args.dry_run:
        lines.append('Dry run: main is not moved.')
    write_summary('\n'.join(lines) + '\n')
    return 0


# ── write ─────────────────────────────────────────────────────────────────────


def verify_created(created: dict, plan: dict) -> None:
    got = (
        created['tree']['sha'],
        [p['sha'] for p in created['parents']],
        created['message'].rstrip('\n'),
    )
    if got != (plan['tree'], plan['parents'], plan['message'].rstrip('\n')):
        raise GhError(
            f'GitHub created {created["sha"]}, which differs from the checked commit. The main branch did not move.'
        )


def cmd_create(args) -> int:
    """Creates R as planned and outputs its SHA and digest; no ref names it yet."""
    plan = json.loads(Path(args.plan_file).read_text())
    repo, (main, target) = plan['repo'], plan['parents']
    try:
        created = gh_send(
            'POST',
            f'repos/{OWNER}/{repo}/git/commits',
            {'message': plan['message'], 'tree': plan['tree'], 'parents': [main, target]},
        )
        verify_created(created, plan)
    except GhError as err:
        print(f'::error::{repo}: {err}', file=sys.stderr)
        return 1
    write_outputs({'r': created['sha'], 'digest': plan.get('digest', '')})
    return 0


def promoted_tag(r: str) -> str:
    return f'{PROMOTED_TAG_PREFIX}{r}'


def push_tag(repo: str, digest: str, tag: str, credential: str) -> None:
    base = f'https://{REGISTRY}/v2/{OWNER}/{repo}/manifests'
    token = registry_push_token(repo, credential)
    headers, body = registry_get(f'{base}/{digest}', token, MANIFEST_ACCEPT)
    if f'sha256:{hashlib.sha256(body).hexdigest()}' != digest:
        raise GhError(f'{digest} read back as other bytes')
    media = {k.lower(): v for k, v in headers.items()}.get('content-type', '')
    registry_request(f'{base}/{tag}', f'Bearer {token}', '', 'PUT', body, media)
    tagged = tag_digest(repo, tag, token)
    if tagged != digest:
        raise GhError(f'{tag} names {tagged}, not {digest}')


def cmd_tag(args) -> int:
    """Tags the digest `promoted-<R>` in the repository's own package by pushing back the
    manifest bytes read at that digest, so the digest cannot change; then reads it back."""
    if not (
        REPO_RE.fullmatch(args.repo)
        and DIGEST_RE.fullmatch(args.digest)
        and FULL_SHA_RE.fullmatch(args.r)
    ):
        print('::error::tag needs a repository name, a sha256 digest and R', file=sys.stderr)
        return 2
    credential = os.environ.get('GHCR_TOKEN', '')
    if not credential:
        print('::error::tag needs GHCR_TOKEN', file=sys.stderr)
        return 2
    tag = promoted_tag(args.r)
    try:
        push_tag(args.repo, args.digest, tag, credential)
    except GhError as err:
        print(f'::error::{args.repo}: {err}. The main branch did not move.', file=sys.stderr)
        return 1
    write_summary(f'## Tagged\n\n`{REGISTRY}/{OWNER}/{args.repo}:{tag}` = `{args.digest}`.\n')
    return 0


def verify_move(repo: str, main: str, target: str, digest: str, r: str) -> None:
    """GhError unless R on GitHub is the commit the checks planned over `main` and
    `target` and, for an image lane, `promoted-<R>` names `digest`."""
    created = gh_json(f'repos/{OWNER}/{repo}/git/commits/{r}') or {}
    tree = (gh_json(f'repos/{OWNER}/{repo}/git/commits/{target}') or {}).get('tree') or {}
    expected = {
        'tree': tree.get('sha'),
        'parents': [main, target],
        'message': reconciliation_message(digest),
    }
    verify_created(created, expected)
    if digest:
        tagged = tag_digest(repo, promoted_tag(r), registry_token(repo))
        if tagged != digest:
            raise GhError(
                f'{promoted_tag(r)} names {tagged}, not {digest}. The main branch did not move.'
            )


def cmd_move(args) -> int:
    """Re-reads R as the checks planned it and, for an image lane, its `promoted-<R>` tag,
    then one force=false update of refs/heads/main: a main that moved since the snapshot
    is not an ancestor of R, so GitHub refuses and main is untouched."""
    shas = (args.main, args.target, args.r)
    if not (
        REPO_RE.fullmatch(args.repo)
        and all(FULL_SHA_RE.fullmatch(s) for s in shas)
        and (not args.digest or DIGEST_RE.fullmatch(args.digest))
    ):
        print(
            '::error::move needs a repository name, three full SHAs and a digest or none',
            file=sys.stderr,
        )
        return 2
    repo = args.repo
    try:
        verify_move(repo, args.main, args.target, args.digest, args.r)
        gh_send(
            'PATCH',
            f'repos/{OWNER}/{repo}/git/refs/heads/main',
            {'sha': args.r, 'force': False},
        )
    except (GhError, KeyError, TypeError) as err:
        print(f'::error::{repo}: {err}', file=sys.stderr)
        return 1
    write_summary(
        f'## Promoted\n\n**{repo}**: main -> `{args.r[:12]}` (dev `{args.target[:12]}`).\n'
    )
    return 0


def parse_args(argv: list[str] | None = None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--work-dir', default='promote-work', help='clones and plan files live here')
    sub = p.add_subparsers(dest='command', required=True)

    snap = sub.add_parser('snapshot', help='resolve the repository, T and M')
    snap.add_argument('--repo', required=True, help='repository name, without the owner')
    snap.add_argument('--target', default='', help='commit on dev (default: the head of dev)')
    snap.set_defaults(func=cmd_snapshot)

    for name, func, text in (
        ('preview', cmd_preview, 'render the versions and notes a promotion publishes'),
        ('check', cmd_check, 'run the promotion checks and plan R'),
    ):
        s = sub.add_parser(name, help=text)
        s.add_argument('--repo', required=True)
        s.add_argument('--target', required=True, help="snapshot's T")
        s.add_argument('--main', required=True, help="snapshot's M")
        s.set_defaults(func=func)
        if name == 'check':
            s.add_argument('--plan-file', required=True)
            s.add_argument('--dry-run', action='store_true', help='say so in the summary')

    create = sub.add_parser('create', help='create R, a commit no ref names yet')
    create.add_argument('--plan-file', required=True)
    create.set_defaults(func=cmd_create)

    tag = sub.add_parser('tag', help="tag the promoted digest `promoted-<R>` in the repo's package")
    tag.add_argument('--repo', required=True)
    tag.add_argument('--digest', required=True)
    tag.add_argument('--r', required=True, help="create's R")
    tag.set_defaults(func=cmd_tag)

    move = sub.add_parser('move', help='move main to R')
    move.add_argument('--repo', required=True)
    move.add_argument('--main', required=True, help="snapshot's M")
    move.add_argument('--target', required=True, help="snapshot's T")
    move.add_argument('--digest', default='', help="create's digest, empty for no image lane")
    move.add_argument('--r', required=True, help="create's R")
    move.set_defaults(func=cmd_move)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
