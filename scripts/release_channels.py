"""Tag shapes and repository tables shared by the two-channel release scripts.

DEPLOYED_IMAGE_REPOS is owned by the homelab deployment inventory
(`apps/*/compose.yaml` in cplieger/homelab): a repo joins when it is onboarded
there and leaves when its deployment is removed; edit both together.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

STABLE_TAG_RE = re.compile(r'^v\d+\.\d+\.\d+$')
DEV_TAG_RE = re.compile(r'^v\d+\.\d+\.\d+-dev\.\d+$')
# A nested Go module releases on its own lane, whose tags carry the module dir
# as a prefix (`yamlenv/v1.1.0`); the dir charset is the one release.yaml admits.
LANE_STABLE_TAG_RE = re.compile(r'^(?:(?P<lane>[A-Za-z0-9._/-]+)/)?v\d+\.\d+\.\d+$')
LANE_DEV_TAG_RE = re.compile(r'^(?:(?P<lane>[A-Za-z0-9._/-]+)/)?v\d+\.\d+\.\d+-dev\.\d+$')

DEPLOYED_IMAGE_REPOS = frozenset(
    {
        'cert-converter',
        'docker-age',
        'docker-caddy',
        'docker-fclones-scheduler',
        'docker-keepalived',
        'docker-nut-upsd',
        'docker-radvd',
        'docker-renovate-scheduler',
        'docker-smtp-relay',
        'github-scout',
        'knell',
        'marotte',
        'pg-autodump',
        'plex-exporter',
        'plex-language-sync',
        'registry-stats',
        'seadex-scout',
        'subflux',
        'tautulli-remap',
        'web-terminal-kiro',
        'web-terminal-server',
    }
)

# Repos that publish from `main` directly and never get a `dev` branch.
SINGLE_MAIN_REPOS = frozenset({'animap', 'ci', '.github', 'tool-catalog', 'unraid-templates'})

# Two-channel repos whose own .github/workflows/publish.yaml tags and releases
# on a main push instead of the central release.yaml, so they have no dev
# release run to read. Owned by the publish.yaml marker classify-repos.py keys
# the artifact CI group on; a repo joins or leaves both together.
OWN_PUBLISH_REPOS = frozenset({'web-terminal-glyphs'})

# A commit merged from a pull request whose head branch starts with one of these
# is a machine change; every other head, and a commit with no pull request, is human.
MACHINE_HEAD_PREFIXES = ('renovate/', 'repo-sync/', 'rebuild/')

# The receipt a dev tag path leaves on its commit: a commit status in this
# context, posted right after the tag ref. Stable tags need none because their
# GitHub Release, authored by the Actions token, is the receipt.
TAG_RECEIPT_PREFIX = 'release/tag/'
RELEASE_AUTHORS = frozenset({'github-actions[bot]'})

# GitHub's tag listing pages at 100 and every dev tag is permanent, so a page
# cap bounds the walk on a repo with thousands of dev builds.
TAG_PAGE_SIZE = 100
TAG_PAGE_CAP = 20


class TagListingTruncatedError(Exception):
    """The page cap was reached before the requested tags were found; the
    listing read so far is not the repo's tag set and must not be graded."""


def tag_receipt_context(tag: str) -> str:
    return TAG_RECEIPT_PREFIX + tag


def has_tag_receipt(tag: str, statuses: Iterable[dict]) -> bool:
    """Whether `statuses` (a commit's combined statuses) carry the tag's receipt."""
    context = tag_receipt_context(tag)
    return any(s.get('context') == context and s.get('state') == 'success' for s in statuses)


def release_is_pipeline_authored(release: dict) -> bool:
    return ((release.get('author') or {}).get('login') or '') in RELEASE_AUTHORS


def is_stable_tag(name: str) -> bool:
    return bool(STABLE_TAG_RE.match(name))


def is_dev_tag(name: str) -> bool:
    return bool(DEV_TAG_RE.match(name))


def semver_key(tag: str) -> tuple[int, int, int]:
    """Numeric sort key of a stable tag; raises ValueError on any other shape."""
    if not is_stable_tag(tag):
        raise ValueError(f'not a stable tag: {tag!r}')
    major, minor, patch = tag[1:].split('.')
    return int(major), int(minor), int(patch)


def dev_key(tag: str) -> tuple[int, int, int, int]:
    """Numeric sort key of a dev tag; raises ValueError on any other shape."""
    if not is_dev_tag(tag):
        raise ValueError(f'not a dev tag: {tag!r}')
    base, number = tag.split('-dev.')
    return (*semver_key(base), int(number))


def stable_tags_sorted(names: Iterable[str]) -> list[str]:
    """The stable tags among `names`, newest first, by semver rather than list order."""
    return sorted((n for n in names if is_stable_tag(n)), key=semver_key, reverse=True)


def dev_tags_sorted(names: Iterable[str]) -> list[str]:
    """The dev tags among `names`, newest first, by semver then dev number."""
    return sorted((n for n in names if is_dev_tag(n)), key=dev_key, reverse=True)


def newest_stable_tag(names: Iterable[str]) -> str:
    """The highest stable tag among `names`, or '' when there is none."""
    ordered = stable_tags_sorted(names)
    return ordered[0] if ordered else ''


def tag_lane(name: str) -> str | None:
    """'' for a root stable or dev tag, the lane prefix for a lane one, None for
    any other shape."""
    m = LANE_STABLE_TAG_RE.match(name) or LANE_DEV_TAG_RE.match(name)
    return (m.group('lane') or '') if m else None


def tags_by_lane(names: Iterable[str]) -> dict[str, tuple[list[str], list[str]]]:
    """{lane: (stable tags, dev tags)}, each newest first, over every stable or
    dev tag of any lane among `names`; the root lane is ''."""
    lanes: dict[str, list[str]] = {}
    for name in names:
        lane = tag_lane(name)
        if lane is not None:
            lanes.setdefault(lane, []).append(name)
    out = {}
    for lane, tags in lanes.items():
        prefix = f'{lane}/' if lane else ''
        bare = [t[len(prefix) :] for t in tags]
        out[lane] = (
            [prefix + t for t in stable_tags_sorted(bare)],
            [prefix + t for t in dev_tags_sorted(bare)],
        )
    return out


def _collect_pages(
    fetch_page: Callable[[int], list[str] | None],
    satisfied: Callable[[list[str]], bool],
    page_cap: int,
    request: str,
) -> list[str] | None:
    names: list[str] = []
    for page in range(1, page_cap + 1):
        batch = fetch_page(page)
        if batch is None:
            return None
        names.extend(batch)
        if satisfied(names) or len(batch) < TAG_PAGE_SIZE:
            return names
    raise TagListingTruncatedError(
        f'{page_cap} pages of {TAG_PAGE_SIZE} tags did not reach {request}'
    )


def collect_tags(
    fetch_page: Callable[[int], list[str] | None],
    want_stable: int,
    want_dev: int = 0,
    page_cap: int = TAG_PAGE_CAP,
) -> list[str] | None:
    """Tag names from successive pages of `fetch_page(page)` (1-based, at most
    TAG_PAGE_SIZE names each) until at least `want_stable` root stable and
    `want_dev` root dev tags are present or the pages run out. None when a page
    read fails; TagListingTruncatedError when `page_cap` full pages did not
    satisfy the request."""

    def satisfied(names: list[str]) -> bool:
        stable = sum(1 for n in names if is_stable_tag(n))
        dev = sum(1 for n in names if is_dev_tag(n))
        return stable >= want_stable and dev >= want_dev

    return _collect_pages(
        fetch_page, satisfied, page_cap, f'{want_stable} stable and {want_dev} dev tags'
    )


def collect_all_tags(
    fetch_page: Callable[[int], list[str] | None],
    page_cap: int = TAG_PAGE_CAP,
) -> list[str] | None:
    """Every tag name in the listing, read to its first short page: a lane
    cannot be counted before its first tag is seen, so a reader grading every
    lane has no earlier stop. None when a page read fails;
    TagListingTruncatedError when `page_cap` full pages did not end the listing."""
    return _collect_pages(fetch_page, lambda _names: False, page_cap, 'the end of the listing')
