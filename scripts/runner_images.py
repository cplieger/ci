#!/usr/bin/env python3
"""The runner images, images/<name>/Dockerfile, a docker repository releases beside its app.

plan --package ghcr.io/OWNER/APP-runner --channel dev|stable --tag TAG --sha SHA --repo OWNER/NAME
    --date YYYY-MM-DD --created ISO8601 [--licenses JSON] [--default-licenses SPDX]: the images in
    build order as a JSON array, [] for none. promoted --package OWNER/APP-runner --source COMMIT
    --names JSON: {name: digest} when every image has a complete dev build of COMMIT, else {}.
    bake --platform PLATFORM --context DIR < PLAN: a bake file building PLAN with no output.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

NAME_RE = re.compile(r'^[a-z0-9]{1,32}$')
INDEX_TYPES = (
    'application/vnd.oci.image.index.v1+json, '
    'application/vnd.docker.distribution.manifest.list.v2+json'
)
TOOLS = Path(__file__).resolve().parent


class PlanError(Exception):
    pass


def declares(text: str, arg: str) -> bool:
    return re.search(rf'^[ \t]*ARG[ \t]+{arg}([ \t=]|$)', text, re.MULTILINE) is not None


def instruction_lines(text: str) -> list[str]:
    lines, cur = [], ''
    for raw in text.splitlines():
        stripped = raw.strip()
        # Docker drops comment and empty lines inside a continuation too.
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.endswith('\\'):
            cur += stripped[:-1] + ' '
            continue
        lines.append(cur + stripped)
        cur = ''
    if cur:
        lines.append(cur)
    return lines


SWAPPED_FORMS = 'a FROM image, a COPY --from= or a RUN --mount from='


def swapped_spans(line: str) -> set[tuple[int, int]]:
    """The spans of one instruction that a build context replaces, SWAPPED_FORMS and no other
    (BuildKit v0.23.2 frontend/dockerfile/dockerfile2llb convert.go and convert_runmount.go)."""
    tokens = list(re.finditer(r'\S+', line))
    keyword = tokens[0].group().upper() if tokens else ''
    spans = set()
    for t in tokens[1:]:
        word = t.group()
        if not word.startswith('--'):
            if keyword == 'FROM':
                spans.add(t.span())
            break
        if keyword == 'COPY' and word.startswith('--from='):
            spans.add((t.start() + len('--from='), t.end()))
        elif keyword == 'RUN' and word.startswith('--mount='):
            at = t.start() + len('--mount=')
            for field in word[len('--mount=') :].split(','):
                if field.startswith('from='):
                    spans.add((at + len('from='), at + len(field)))
                at += len(field) + 1
    return spans


def sibling_refs(
    dockerfile: str, text: str, package: str, name: str, names: list[str]
) -> dict[str, str]:
    """{reference as written: sibling image} for every mention of the package."""
    ref_re = re.compile(
        rf'(?<![\w./-]){re.escape(package)}'
        r'(?::(?P<tag>[\w][\w.-]{0,127}))?(?P<digest>@sha256:[0-9a-f]{64})?(?![\w./:@-])'
    )
    moving = {f'{n}{suffix}': n for n in names for suffix in ('', '-dev')}
    refs: dict[str, str] = {}
    for line in instruction_lines(text):
        spans = swapped_spans(line)
        for m in ref_re.finditer(line):
            tag = m.group('tag')
            if m.group('digest') or not tag or tag not in moving:
                raise PlanError(
                    f'{dockerfile} names {m.group(0)}, but a runner image names a sibling only by '
                    f'its moving tag ({" or ".join(sorted(f"{package}:{t}" for t in moving))}) '
                    f'as {SWAPPED_FORMS}, which the release replaces with the build of the same run'
                )
            if moving[tag] == name:
                raise PlanError(f'{dockerfile} names {m.group(0)}, which is the image itself')
            if m.span() not in spans:
                raise PlanError(
                    f'{dockerfile} names {m.group(0)} in a {line.split()[0].upper()} instruction, '
                    f'which the release does not rewrite: it swaps a sibling only as {SWAPPED_FORMS}'
                )
            refs[f'{package}:{tag}'] = moving[tag]
    return refs


def build_order(parents: dict[str, set[str]]) -> list[str]:
    order: list[str] = []
    left = {n: set(p) for n, p in parents.items()}
    while left:
        ready = sorted(n for n, p in left.items() if not p)
        if not ready:
            raise PlanError(
                'the runner images name each other in a cycle: '
                + ', '.join(f'{n} on {", ".join(sorted(p))}' for n, p in sorted(left.items()))
            )
        for n in ready:
            order.append(n)
            del left[n]
        for p in left.values():
            p.difference_update(ready)
    return order


def plan(root: Path, args: argparse.Namespace) -> list[dict]:
    dockerfiles = sorted(root.glob('images/*/Dockerfile'))
    names = [d.parent.name for d in dockerfiles]
    for n in names:
        if not NAME_RE.match(n):
            raise PlanError(
                f'images/{n}/Dockerfile: a runner image name is 1 to 32 lowercase letters and '
                f'digits, so its tags ({n}-dev, {n}-sha-<commit>) name exactly one image. '
                f'Rename images/{n}'
            )
    licenses = json.loads(args.licenses or '{}')
    if not isinstance(licenses, dict) or not all(isinstance(v, str) for v in licenses.values()):
        raise PlanError(
            f'--licenses must be a JSON object of image name to SPDX expression, got {args.licenses}'
        )
    if unknown := sorted(set(licenses) - set(names)):
        print(
            f'::warning::the release policy names runner image {", ".join(unknown)}, '
            'which has no images/<name>/Dockerfile here',
            file=sys.stderr,
        )
    texts = {n: d.read_text() for n, d in zip(names, dockerfiles, strict=True)}
    refs = {
        n: sibling_refs(f'images/{n}/Dockerfile', texts[n], args.package, n, names) for n in names
    }
    images = []
    for n in build_order({n: set(r.values()) for n, r in refs.items()}):
        version = f'{n}-{args.tag}'
        moving = f'{n}-dev' if args.channel == 'dev' else n
        build_args = []
        if declares(texts[n], 'PKG_REFRESH'):
            build_args.append(f'PKG_REFRESH={args.date}')
        if declares(texts[n], 'BUILD_VERSION'):
            build_args.append(f'BUILD_VERSION={args.tag}')
        lic = licenses.get(n, args.default_licenses)
        labels = [
            f'org.opencontainers.image.created={args.created}',
            f'org.opencontainers.image.revision={args.sha}',
            f'org.opencontainers.image.source=https://github.com/{args.repo}',
            f'org.opencontainers.image.title={args.package.rsplit("/", 1)[-1]}:{n}',
            f'org.opencontainers.image.version={version}',
        ]
        if lic:
            labels.append(f'org.opencontainers.image.licenses={lic}')
        images.append(
            {
                'name': n,
                'dockerfile': f'images/{n}/Dockerfile',
                'parents': refs[n],
                'build_args': build_args,
                'labels': labels,
                'index_tag': f'{n}-sha-{args.sha}',
                'version_tag': version,
                'moving_tag': moving,
            }
        )
    return images


def curl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['curl', '--connect-timeout', '10', '--max-time', '30', *args],
        capture_output=True,
        text=True,
        check=False,
    )


def dev_build(package: str, ref: str, token: str) -> str:
    """The index digest GHCR serves at ref, '' on a 404; PlanError when GHCR cannot tell."""
    head = curl(
        '-sSI',
        '--retry',
        '3',
        '--retry-all-errors',
        '-H',
        f'Authorization: Bearer {token}',
        '-H',
        f'Accept: {INDEX_TYPES}',
        f'https://ghcr.io/v2/{package}/manifests/{ref}',
    )
    if head.returncode != 0:
        raise PlanError(f'no answer from ghcr.io for {package}:{ref}')
    # A retried request prints each attempt's headers, so the last status line answers.
    lines = head.stdout.replace('\r', '').splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith('HTTP/')]
    status = [*lines[starts[-1]].split(), ''][1] if starts else ''
    if status == '404':
        return ''
    if status != '200':
        raise PlanError(f'ghcr.io answered HTTP {status or "nothing"} for {package}:{ref}')
    for line in lines[starts[-1] + 1 :]:
        key, _, value = line.partition(':')
        if key.strip().lower() == 'docker-content-digest':
            return value.strip()
    raise PlanError(f'ghcr.io served {package}:{ref} without its digest')


def promoted(package: str, source: str, names: list[str]) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp) / 'token.json'
        status = curl(
            '-sSL',
            '--retry',
            '3',
            '--retry-all-errors',
            '-o',
            str(body),
            '-w',
            '%{http_code}',
            f'https://ghcr.io/token?scope=repository:{package}:pull',
        )
        if status.returncode != 0:
            raise PlanError(f'no answer from the ghcr.io token endpoint for {package}')
        if status.stdout == '403':
            note(
                f'ghcr.io refuses an anonymous read of {package}, which does not exist yet or is '
                'private, so the runner images build from source. Its owner makes a private '
                'package public in the package settings'
            )
            return {}
        if status.stdout != '200':
            raise PlanError(
                f'the ghcr.io token endpoint answered HTTP {status.stdout} for {package}'
            )
        token = json.loads(body.read_text()).get('token') or ''
    found = {}
    for n in names:
        digest = dev_build(package, f'{n}-sha-{source}', token)
        if not digest:
            note(
                f'ghcr.io/{package} has no {n}-sha-{source}, so the runner images build from source'
            )
            return {}
        rc = subprocess.run(
            [
                'bash',
                str(TOOLS / 'release-state.sh'),
                'complete',
                f'ghcr.io/{package}@{digest}',
                source,
            ],
            stdout=sys.stderr,
            check=False,
        ).returncode
        if rc == 3:
            return {}
        if rc != 0:
            raise PlanError(
                f'cannot tell whether ghcr.io/{package}@{digest} was signed and attested'
            )
        found[n] = digest
    return found


def bake(plan_json: str, platform: str, context: str) -> dict:
    """Each image as a bake target with no output, a sibling it names built by its own target."""
    targets = {}
    for image in json.loads(plan_json):
        targets[image['name']] = {
            'context': context,
            'dockerfile': image['dockerfile'],
            'platforms': [platform],
            # The app image's pull-request build passes PKG_REFRESH alone.
            'args': dict(
                a.split('=', 1) for a in image['build_args'] if a.startswith('PKG_REFRESH=')
            ),
            'contexts': {ref: f'target:{parent}' for ref, parent in image['parents'].items()},
            'output': ['type=cacheonly'],
        }
    return {'group': {'default': {'targets': list(targets)}}, 'target': targets}


def note(msg: str) -> None:
    print(f'::notice::{msg}', file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    pl = sub.add_parser('plan')
    for flag in ('--package', '--tag', '--sha', '--repo', '--date', '--created'):
        pl.add_argument(flag, required=True)
    pl.add_argument('--channel', required=True, choices=('dev', 'stable'))
    pl.add_argument('--licenses', default='{}')
    pl.add_argument('--default-licenses', default='')
    pr = sub.add_parser('promoted')
    for flag in ('--package', '--source', '--names'):
        pr.add_argument(flag, required=True)
    bk = sub.add_parser('bake')
    bk.add_argument('--platform', required=True)
    bk.add_argument('--context', required=True)
    args = p.parse_args(argv)
    try:
        if args.cmd == 'plan':
            print(json.dumps(plan(Path.cwd(), args), separators=(',', ':')))
        elif args.cmd == 'bake':
            print(json.dumps(bake(sys.stdin.read(), args.platform, args.context), indent=2))
        else:
            print(
                json.dumps(
                    promoted(args.package, args.source, json.loads(args.names)),
                    separators=(',', ':'),
                )
            )
    except PlanError as e:
        print(f'::error::{e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
