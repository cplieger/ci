#!/usr/bin/env python3
"""Render the Docker Hub overview page for an image repo.

Docker Hub's full-description field is capped at 25,000 bytes server-side, and
the publish action truncates anything larger, which silently cuts the end of the
document. Mirroring a full README therefore put every app README under a byte
budget that had nothing to do with what the README is for.

This script builds a SHORT overview page instead: what the image is, how to pull
and run it, and links to the real documentation on GitHub. Its length is bounded
by construction, because neither of its two variable-length inputs grows when
the README grows:

    * the marked summary region of README.md (the tagline plus the first
      section, per public-docs.md's pyramid)
    * compose.yaml, the repo's reference example

Everything else is derived: the repo name, the image name, the doc links and the
README's own License section. The GitHub description is deliberately absent,
because Docker Hub renders it as the separate short-description field above the
overview, and the marked region already opens with the README's tagline.

Nothing is committed. docker-release.yaml runs this at publish time and points
peter-evans/dockerhub-description at the output, so there is no second document
to keep current and no drift to detect.

Relative links are rewritten to absolute GitHub URLs here rather than by the
action's `enable-url-completion`, which cannot know that this page is not
README.md and would resolve every anchor against the wrong file.

Usage:
    render-hub-overview.py --repo-root . --owner cplieger --repo docker-radvd \\
        --image cplieger/docker-radvd -o hub-overview.md

Exits 1 with a diagnostic when the README carries no summary markers, so a
release publishes the previous page instead of a truncated one. audit.py checks
the markers for every image repo, which is where a missing pair is meant to
surface.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Docker Hub rejects a longer full description; the publish action truncates to
# fit. A page built from the inputs above lands near 5 KB, so reaching this is a
# defect in the summary region rather than a budget to manage.
HUB_MAX_BYTES = 25000
HUB_WARN_BYTES = 20000

MARKER_BEGIN = '<!-- hub-overview BEGIN -->'
MARKER_END = '<!-- hub-overview END -->'

# A link target already carrying a scheme (https:, mailto:) or a protocol-relative
# host is left alone; everything else is repo-relative and needs absolutizing.
ABSOLUTE_TARGET = re.compile(r'^(?:[a-z][a-z0-9+.\-]*:|//)', re.IGNORECASE)
INLINE_LINK = re.compile(r'(!?\[(?:[^\]\\]|\\.)*\])\(\s*<?([^)\s>]+)>?((?:\s+"[^"]*")?)\s*\)')
REFERENCE_LINK = re.compile(r'^(\s{0,3}\[(?:[^\]\\]|\\.)+\]:\s*)(\S+)(.*)$')
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')


def fail(msg: str) -> None:
    print(f'::error::{msg}', file=sys.stderr)
    sys.exit(1)


def absolutize(target: str, owner: str, repo: str, ref: str, readme: str, *, image: bool) -> str:
    """Resolve one repo-relative link target against the GitHub tree.

    An image needs the raw host: a `blob` URL serves GitHub's HTML viewer, which
    renders as a broken image wherever the page is mirrored.
    """
    if ABSOLUTE_TARGET.match(target):
        return target
    blob = f'https://github.com/{owner}/{repo}/blob/{ref}'
    raw = f'https://raw.githubusercontent.com/{owner}/{repo}/{ref}'
    if target.startswith('#'):
        # A bare fragment points into the README this text was extracted from,
        # not into the page being generated.
        return f'{blob}/{readme}{target}'
    path, _, fragment = target.partition('#')
    path = path.lstrip('./')
    if not path:
        return f'{blob}/{readme}' + (f'#{fragment}' if fragment else '')
    base = raw if image else blob
    return f'{base}/{path}' + (f'#{fragment}' if fragment else '')


def rewrite_links(text: str, owner: str, repo: str, ref: str, readme: str) -> str:
    """Absolutize every relative link outside fenced code blocks."""

    def one(target: str, *, image: bool) -> str:
        return absolutize(target, owner, repo, ref, readme, image=image)

    def inline(m: re.Match[str]) -> str:
        label = m.group(1)
        return f'{label}({one(m.group(2), image=label.startswith("!"))}{m.group(3)})'

    def reference(m: re.Match[str]) -> str:
        return f'{m.group(1)}{one(m.group(2), image=False)}{m.group(3)}'

    out: list[str] = []
    fence: str | None = None
    for line in text.split('\n'):
        marker = FENCE.match(line)
        if fence is not None:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            out.append(line)
            continue
        if marker:
            fence = marker.group(1)
            out.append(line)
            continue
        line = INLINE_LINK.sub(inline, line)
        line = REFERENCE_LINK.sub(reference, line)
        out.append(line)
    return '\n'.join(out)


def marked_region(readme_text: str) -> str | None:
    """Return the text between the summary markers, or None when absent."""
    start = readme_text.find(MARKER_BEGIN)
    end = readme_text.find(MARKER_END)
    if start < 0 or end < 0 or end < start:
        return None
    return readme_text[start + len(MARKER_BEGIN) : end].strip()


def named_section(readme_text: str, heading: str) -> str | None:
    """Return the body of a top-level `## <heading>` section, without its heading."""
    pattern = re.compile(
        rf'^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(readme_text)
    return match.group(1).strip() if match else None


def page_title(page: Path) -> str:
    """A docs page's own H1, so the link text is the page's title not its filename."""
    for line in page.read_text(encoding='utf-8').split('\n'):
        if line.startswith('# '):
            return line[2:].strip()
    stem = page.stem.replace('-', ' ').replace('_', ' ')
    return stem[:1].upper() + stem[1:]


def doc_links(root: Path, owner: str, repo: str, ref: str) -> list[str]:
    """Build the documentation link list from the files the repo actually has."""
    blob = f'https://github.com/{owner}/{repo}/blob/{ref}'
    links = [f'**[Full documentation]({blob}/README.md)** — configuration, behavior, security']
    docs = root / 'docs'
    for page in sorted(docs.glob('*.md')) if docs.is_dir() else []:
        links.append(f'[{page_title(page)}]({blob}/docs/{page.name})')
    if (root / 'CONTRIBUTING.md').is_file():
        links.append(f'[Contributing]({blob}/CONTRIBUTING.md)')
    links.append(f'[Releases and changelog](https://github.com/{owner}/{repo}/releases)')
    return links


def render(args: argparse.Namespace) -> str:
    root = Path(args.repo_root)
    readme_path = root / args.readme
    if not readme_path.is_file():
        fail(f'{readme_path} not found')
    readme_text = readme_path.read_text(encoding='utf-8')

    summary = marked_region(readme_text)
    if summary is None:
        fail(
            f'{args.readme} carries no `{MARKER_BEGIN}` / `{MARKER_END}` pair. '
            'Wrap the tagline and the first section in them so the Docker Hub '
            'overview can be built (public-docs.md "Docker Hub overview").'
        )
    if not summary:
        fail(f'the {MARKER_BEGIN} region in {args.readme} is empty')

    def links(text: str) -> str:
        return rewrite_links(text, args.owner, args.repo, args.ref, args.readme)

    parts = [f'# {args.repo}', '', links(summary), '', '## Pull', '']
    parts += ['```bash', f'docker pull {args.image}:latest', '```', '']
    parts += [
        (
            f'Also published to `ghcr.io/{args.owner}/{args.repo}` with identical images '
            'and tags. Release versions are tagged `vX.Y.Z` alongside `latest`.'
        ),
        '',
    ]

    compose = root / args.compose
    if compose.is_file():
        parts += ['## Quick start', '']
        parts += ['```yaml', compose.read_text(encoding='utf-8').strip(), '```', '']

    parts += ['## Documentation', '']
    parts += [f'- {line}' for line in doc_links(root, args.owner, args.repo, args.ref)]
    parts += ['']

    license_body = named_section(readme_text, 'License')
    if license_body:
        parts += ['## License', '', links(license_body), '']

    return '\n'.join(parts).rstrip() + '\n'


def main() -> int:
    env_repo = os.environ.get('GITHUB_REPOSITORY', '')
    env_owner, _, env_name = env_repo.partition('/')

    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--repo-root', default='.', help='repo working tree (default: .)')
    p.add_argument('--owner', default=env_owner or 'cplieger', help='GitHub owner')
    p.add_argument('--repo', default=env_name, help='GitHub repo name')
    p.add_argument('--image', default='', help='Docker Hub image, e.g. cplieger/docker-radvd')
    p.add_argument('--ref', default=os.environ.get('GITHUB_REF_NAME') or 'main')
    p.add_argument('--readme', default='README.md')
    p.add_argument('--compose', default='compose.yaml')
    p.add_argument('-o', '--output', default='', help='write here instead of stdout')
    args = p.parse_args()

    if not args.repo:
        fail('--repo is required (or set GITHUB_REPOSITORY)')
    if not args.image:
        args.image = f'{args.owner}/{args.repo}'

    page = render(args)
    size = len(page.encode('utf-8'))
    if size > HUB_MAX_BYTES:
        fail(
            f"rendered overview is {size} bytes, over Docker Hub's {HUB_MAX_BYTES}-byte "
            'description limit. The summary region or compose example has grown far past '
            'what this page is for; shorten it rather than raising the limit.'
        )
    if size > HUB_WARN_BYTES:
        print(
            f'::warning::rendered overview is {size} bytes, near the '
            f'{HUB_MAX_BYTES}-byte Docker Hub limit',
            file=sys.stderr,
        )

    if args.output:
        Path(args.output).write_text(page, encoding='utf-8')
        print(f'wrote {args.output} ({size} bytes)', file=sys.stderr)
    else:
        sys.stdout.write(page)
    return 0


if __name__ == '__main__':
    sys.exit(main())
