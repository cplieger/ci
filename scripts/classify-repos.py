#!/usr/bin/env python3
"""Auto-discover and profile every cplieger repo, printing the sync manifest.

The manifest (.github/sync.yml) goes to stdout, per-repo diagnostics to stderr.
A two-branch repo (release_channels.is_two_branch) is classified once per base
from that base's tree and its groups carry `base:`; every other repo from HEAD.

Importable: `sync_owned_patterns()` is every path the sync can write, as globs,
`canonical_sources(repo)` maps each of `repo`'s paths to its source here, and
`classify_tree(tree, releasing=)` with `synced_dests(profile)` give a local
tree's profile and the paths the sync writes into it, reading no API.

Failure model: the repo listing aborts on an error, a timeout or the page cap;
an unreadable tree drops that repo (or base) from every group; a failed
tags read of a two-branch repo aborts, because a guessed cliff tier auto-merges
the wrong cliff.toml. Every other repo gets no release pipeline, so its tags
are never read. Repos are read concurrently (fanout.py); the output keeps the
repo order.
"""

import re
import sys
from typing import NamedTuple

import fanout
import ghrest
import release_channels

OWNER = 'cplieger'
TIMEOUT = 10  # seconds per API call
# Reads only, so nothing is spaced; a rate-limit refusal holds every worker.
PACER = ghrest.Pacer()
REST = ghrest.Client(
    run=ghrest.paced(ghrest.run_process, PACER), timeout=TIMEOUT, pause=PACER.pause
)
# A two-branch repo's groups follow every other group, one block per base in this order.
BASES = ('dev', 'main')


class Group(NamedTuple):
    """One manifest group: its comment line and its files, each a path synced
    under its own name or a (source, dest) pair."""

    comment: str
    files: tuple


CI = Group(
    'Unified CI (auto-detects go/ts/web/shell surfaces)',
    (
        '.editorconfig',
        '.gitattributes',
        ('.github/workflow-templates/ci.yml', '.github/workflows/ci.yaml'),
        ('.github/workflow-templates/codeql.yml', '.github/workflows/codeql.yml'),
        ('.github/workflow-templates/security.yml', '.github/workflows/security.yml'),
    ),
)
ARTIFACT_CI = Group(
    'Unified CI for artifact repos (own publish.yaml, no central release.yaml)', CI.files
)
GOLANGCI = Group(
    'Go-tooling configs (Go-having repos)',
    ('.golangci.yaml', ('configs/gremlins.yaml', '.gremlins.yaml')),
)
SMOKE = Group(
    'Image-smoke harness (repos with a tests/image-smoke.conf opt-in)',
    (('configs/image-smoke.sh', 'tests/image-smoke.sh'),),
)
# The assert/extract library and its self-test carry no repo-specific content,
# so they sync; tests/shell/run.sh stays repo-owned because it is the opt-in
# marker, and a synced marker could only arrive once the repo already had it.
SHELL_TEST = Group(
    'Shell unit-test harness (repos with a tests/shell/run.sh opt-in)',
    (
        ('configs/shell/lib.sh', 'tests/shell/lib.sh'),
        ('configs/shell/harness_test.sh', 'tests/shell/harness_test.sh'),
    ),
)
# Keyed on a root Dockerfile rather than an opt-in marker: both scripts are
# inert until a Dockerfile calls them (repin-sha.sh acts only on `# repin:`
# lines; collect-licenses.sh only when the builder stage runs it).
REPIN = Group(
    'sha256 pin recompute helper (image repos; inert without # repin: markers)',
    (
        ('configs/repin-sha.sh', 'scripts/repin-sha.sh'),
        ('configs/collect-licenses.sh', 'scripts/collect-licenses.sh'),
    ),
)
PYTHON = Group(
    'Python repos (ruff + editorconfig; bespoke ci.yaml)',
    ('.editorconfig', '.gitattributes', ('configs/ruff.toml', 'ruff.toml')),
)
TS_CONFIG = Group(
    'TypeScript lint/format configs',
    (
        ('configs/eslint.config.base.mjs', 'eslint.config.base.mjs'),
        ('configs/prettier.json', '.prettierrc.json'),
        ('configs/stylelint.json', '.stylelintrc.json'),
        ('configs/htmlvalidate.json', '.htmlvalidate.json'),
    ),
)
# The nested frontend folders ci.yaml's web job probes, in its order (a test
# holds the two lists equal).
WEB_DIRS = ('static-src', 'web', 'internal/server/static-src', 'internal/webui/static-src')


def ts_config_group(ts_dirs):
    """TS_CONFIG for a repo whose npm projects are in `ts_dirs` ('' for the root,
    empty when its npm projects sit only in folders no group serves).

    The ESLint base is the one config a consumer imports rather than names on
    a command line, and an import resolves its `@eslint/js` and
    `typescript-eslint` imports from its own folder upward, so it lands beside
    each project's package.json or jsr.json, and nowhere when no such folder
    is one the sync writes to. The other three stay at the root, where the
    lint steps read them unless a nearer copy shadows them."""
    if ts_dirs == ('',):
        return TS_CONFIG
    base, *rest = TS_CONFIG.files
    if not ts_dirs:
        return Group(
            f'{TS_CONFIG.comment} (no ESLint base: npm projects in other folders)', tuple(rest)
        )
    where = ', '.join(d or 'the root' for d in ts_dirs)
    return Group(
        f'{TS_CONFIG.comment} (npm project{"s" if len(ts_dirs) > 1 else ""} in {where})',
        (*((base[0], f'{d}/{base[1]}' if d else base[1]) for d in ts_dirs), *rest),
    )


# Every consumer path a module half's ESLint base can take (nested_halves).
HALF_BASE = '**/web/eslint.config.base.mjs'


RELEASE = Group(
    'Release (unified auto-detect)',
    (('.github/workflow-templates/release.yml', '.github/workflows/release.yaml'),),
)
CLIFF = {
    'stable': Group(
        'Cliff config (stable — v1.x+)', (('configs/cliff-stable.toml', 'cliff.toml'),)
    ),
    'alpha': Group('Cliff config (alpha — v0.x)', (('configs/cliff-alpha.toml', 'cliff.toml'),)),
}
TWO_BRANCH_RENOVATE = Group(
    'Two-branch Renovate delta (repos whose default branch is dev)',
    (('configs/renovate-two-branch.json', 'renovate.json'),),
)
# Emission order, for the single-branch block and for each base's; the key
# names the list assign() fills.
GROUPS = (
    ('ci', CI),
    ('artifact_ci', ARTIFACT_CI),
    ('golangci', GOLANGCI),
    ('smoke', SMOKE),
    ('shell_test', SHELL_TEST),
    ('repin', REPIN),
    ('python', PYTHON),
    ('ts_config', TS_CONFIG),
    ('release', RELEASE),
    ('cliff_stable', CLIFF['stable']),
    ('cliff_alpha', CLIFF['alpha']),
    ('two_branch_renovate', TWO_BRANCH_RENOVATE),
)

HEADER = """\
# Auto-generated by scripts/classify-repos.py — DO NOT EDIT MANUALLY.
# Re-run the script (or let the daily sync workflow do it) to regenerate.

group:"""


class ClassifyError(RuntimeError):
    """A read the classification depends on failed; nothing may be guessed from it."""


def pairs(group):
    """The group's files as (source, dest) pairs."""
    return [(f, f) if isinstance(f, str) else f for f in group.files]


def render_files(group):
    lines = ['    files:']
    for entry in group.files:
        if isinstance(entry, str):
            lines.append(f'      - {entry}')
        else:
            lines += [f'      - source: {entry[0]}', f'        dest: {entry[1]}']
    return '\n'.join(lines)


def every_group():
    """GROUPS' groups plus the TS_CONFIG variant for each of WEB_DIRS and the
    one with no ESLint base."""
    variants = [ts_config_group(d) for d in (*((d,) for d in WEB_DIRS), ())]
    return [group for _key, group in GROUPS] + variants


def sync_owned_patterns():
    """Every consumer path any group can write, as globs, sorted; reads no API."""
    return sorted({dest for group in every_group() for _source, dest in pairs(group)} | {HALF_BASE})


def canonical_sources(repo):
    """{dest: source path in this repo} for every sync-owned path, with
    cliff.toml resolved by `repo`'s cliff tier and the ESLint base of each
    module half in `repo`'s default-branch tree. Raises ClassifyError when the
    tags or the tree read fails."""
    name = repo.split('/')[-1]
    tier = cliff_tier(name)
    sources = {}
    for group in every_group():
        if group in CLIFF.values() and group is not CLIFF[tier]:
            continue
        sources.update((dest, source) for source, dest in pairs(group))
    tree = tree_paths(name)
    if tree is None:
        raise ClassifyError(f'tree read failed for {name}, so its module halves are unknown')
    base_source, base_dest = TS_CONFIG.files[0]
    for half in nested_halves(tree):
        sources[f'{half}/{base_dest}'] = base_source
    return sources


def bool_str(value):
    return 'true' if value else 'false'


def api_json(path):
    """One GET with the per-call timeout, under ghrest's retry policy; parsed
    JSON, or None on a timeout, an HTTP error or an unparseable body, never an abort."""
    try:
        return REST.get(path)
    except ghrest.ApiError:
        return None


def tree_paths(repo, ref='HEAD'):
    """Every path in the repo's git tree at `ref`, recursively; None when unreadable,
    a truncated listing included (GitHub caps a recursive tree)."""
    data = api_json(f'repos/{OWNER}/{repo}/git/trees/{ref}?recursive=1')
    if not isinstance(data, dict) or data.get('truncated'):
        return None
    return [entry.get('path', '') for entry in data.get('tree') or [] if isinstance(entry, dict)]


def latest_tag(repo):
    """Name of the repo's newest STABLE tag (vX.Y.Z): '' when there is none,
    None when the read FAILED, a page that is not a list of objects with a
    string `name` included. A dev or lane tag is not a root version, so the
    listing pages past them; TagListingTruncatedError when it cannot."""

    def fetch(page):
        data = api_json(
            f'repos/{OWNER}/{repo}/tags?per_page={release_channels.TAG_PAGE_SIZE}&page={page}'
        )
        if data is None:
            return None
        shaped = isinstance(data, list) and all(
            isinstance(e, dict) and isinstance(e.get('name'), str) and e['name'] for e in data
        )
        return [e['name'] for e in data] if shaped else None

    names = release_channels.collect_tags(fetch, want_stable=1)
    if names is None:
        return None
    return release_channels.newest_stable_tag(names)


def cliff_tier(repo):
    """'alpha' when the newest stable tag is v0.x, else 'stable' (so an untagged
    repo's first release bumps to v1.0.0). ClassifyError when the tags read fails."""
    try:
        tag = latest_tag(repo)
    except release_channels.TagListingTruncatedError as err:
        msg = f'tags listing of {repo} truncated ({err}); aborting rather than guessing the cliff tier'
        raise ClassifyError(msg) from None
    if tag is None:
        msg = f'tags read failed for {repo}; aborting rather than guessing the cliff tier'
        raise ClassifyError(msg)
    return 'alpha' if tag.startswith('v0.') else 'stable'


def discover_repos():
    """Every non-archived, non-fork cplieger repo as a REST-shaped repository
    object (name, default_branch, visibility, fork, archived), byte-order sorted
    by name.

    Forks are excluded (same rule as audit.py): a fork's tree is UPSTREAM's, so
    syncing our conventions into it rewrites code we do not own.
    """
    try:
        # sync.yaml's App installation token cannot read user/repos; the App is
        # installed on every repository of the account.
        repos = REST.pages('installation/repositories', key='repositories')
    except ghrest.RequestTimeoutError:
        sys.exit(124)  # what `timeout N gh ...` exits with under set -e
    except ghrest.ApiError as err:
        sys.exit(f'classify-repos: repo listing failed: {err}')
    return sorted(
        (
            {
                'name': repo['name'],
                'default_branch': repo.get('default_branch') or '',
                'visibility': str(repo.get('visibility') or '').lower(),
                'fork': False,
                'archived': False,
            }
            for repo in repos
            if repo.get('archived') is False and repo.get('fork') is False
        ),
        key=lambda repo: repo['name'],
    )


NESTED_GO_PRUNED = re.compile(r'(^|/)(node_modules|vendor|testdata|static|dist)/')


MODULE_DIR = re.compile(r'[A-Za-z0-9._/-]+')


def nested_modules(tree):
    """Each <dir> whose <dir>/go.mod sits outside the pruned trees, whose name
    stays in [A-Za-z0-9._/-] and which has a .go file below it, sorted:
    ci.yaml's go-nested discovery, which validates exactly these and fails on
    any other name, so no other name reaches a sync.yml dest."""
    sources = [path for path in tree if path.endswith('.go')]
    return sorted(
        d
        for d in (path.removesuffix('/go.mod') for path in tree if path.endswith('/go.mod'))
        if not NESTED_GO_PRUNED.search(f'{d}/go.mod')
        and MODULE_DIR.fullmatch(d)
        and any(source.startswith(f'{d}/') for source in sources)
    )


def has_nested_go(tree):
    """True when the repo holds a nested Go module (nested_modules)."""
    return bool(nested_modules(tree))


def nested_halves(tree):
    """<dir>/web for each nested module whose web folder holds a package.json or
    jsr.json, sorted: ci.yaml's ts-nested discovery, which lints exactly these."""
    paths = set(tree)
    return [
        f'{d}/web'
        for d in nested_modules(tree)
        if f'{d}/web/package.json' in paths or f'{d}/web/jsr.json' in paths
    ]


def classify(repo, ref='HEAD', *, releasing=False):
    """Profile the repo from its tree at `ref`, read once (classify_tree)."""
    return classify_tree(tree_paths(repo, ref) or [], releasing=releasing)


def classify_tree(tree, *, releasing=False):
    """Profile a repo's surfaces and language from `tree`, the paths of its git
    tree: the recursive trees listing, or a clone's `git ls-files`, whose
    directories are derived here (a git tree holds no empty one, so both give
    one profile). Reads no API, so fleet tooling classifies a local clone the
    way the sync does. The profile records `releasing`, which synced_dests reads.

    A root check is an exact-path match against the listing; the cliff tier is
    read separately (cliff_tier). A `releasing` (two-branch) base with no root
    go.mod is Go when it holds nested modules, because the release pipeline
    ships them as lanes; elsewhere a root-less repo keeps its own CI.
    """
    tree = set(tree)
    readable = bool(tree)
    tree |= {path.rsplit('/', depth)[0] for path in tree for depth in range(1, path.count('/') + 1)}
    has_gomod = 'go.mod' in tree
    has_jsr = 'jsr.json' in tree
    has_pkg = 'package.json' in tree
    has_dockerfile = 'Dockerfile' in tree
    has_pyproject = 'pyproject.toml' in tree
    is_web = 'static-src' in tree or 'web' in tree

    if has_gomod:
        lang = 'go'
    elif has_jsr or has_pkg:
        lang = 'ts'
    elif has_dockerfile:
        lang = 'shell'
    elif has_pyproject:
        lang = 'python'
    elif releasing and has_nested_go(tree):
        lang = 'go'
    else:
        lang = 'none'

    # Deeper web indicators: internal/server/static-src, a nested */web/* tree.
    if lang == 'go' and not is_web:
        is_web = any('static-src' in path or '/web/' in path for path in tree)
    has_smoke = 'tests/image-smoke.conf' in tree
    # The same file shell-ci.yaml runs, so enrolment and execution cannot disagree.
    has_shell_tests = 'tests/shell/run.sh' in tree
    # An artifact repo publishes from its own publish.yaml and still needs the
    # meta CI. A marker, not "not releaseable": a repo that releases nothing may
    # keep its own CI, which the sync must never overwrite.
    has_publish = '.github/workflows/publish.yaml' in tree
    # Where ci.yaml lints npm projects: the root when it holds one, the first
    # web folder that does (its web job probes them whatever the root holds),
    # and each nested module's TS half. With none of those, the base goes to
    # the root, or nowhere when the only projects sit in other folders, which
    # keep their own copy.
    web = next(
        (d for d in WEB_DIRS if f'{d}/package.json' in tree or f'{d}/jsr.json' in tree), None
    )
    ts_dirs = (
        *(('',) if has_pkg or has_jsr else ()),
        *((web,) if web else ()),
        *nested_halves(tree),
    )
    if not ts_dirs and not any(
        path.endswith(('/package.json', '/jsr.json'))
        and not any(f'/{d}/' in f'/{path}' for d in ('node_modules', 'testdata', 'vendor'))
        for path in tree
    ):
        ts_dirs = ('',)

    return {
        'readable': readable,
        'releasing': releasing,
        'lang': lang,
        'has_jsr': has_jsr,
        'has_pkg': has_pkg,
        'is_web': is_web,
        'ts_dirs': ts_dirs,
        'can_release': lang == 'go' or has_jsr or has_dockerfile,
        'has_smoke': has_smoke,
        'has_shell_tests': has_shell_tests,
        'has_dockerfile': has_dockerfile,
        'has_publish': has_publish,  # own publish.yaml -> meta CI without release.yaml
    }


def synced_dests(profile):
    """Every path the sync writes into a repo of `profile` (classify_tree), sorted:
    the dests of each group assign() puts it in, the TypeScript configs placed by
    its ts_dirs, and on a releasing base the two-branch Renovate delta. An
    unreadable tree gets nothing, as main() leaves it out of every group, and so
    does the ci repo, the sync's source, which the caller excludes by name. Both
    cliff tiers write cliff.toml, so a profile with no cliff_tier reads as stable."""
    if not profile['readable']:
        return []
    profile = {'cliff_tier': 'stable', **profile}
    groups = assign(['repo'], {'repo': profile}, profile['releasing'])
    if profile['releasing']:
        groups['two_branch_renovate'] = ['repo']
    dests = set()
    for key, group in GROUPS:
        if groups[key]:
            chosen = ts_config_group(profile['ts_dirs']) if key == 'ts_config' else group
            dests.update(dest for _source, dest in pairs(chosen))
    return sorted(dests)


def log_profile(repo, profile, base=None):
    """One stderr line; only a two-branch row (`base`) can release, so only it
    reports a cliff tier and the release flag."""
    line = f'  classified: {repo:<30} lang={profile["lang"]:<5} web='
    if base:
        line += (
            f'{bool_str(profile["is_web"]):<5} cliff={profile["cliff_tier"]:<6}'
            f' release={bool_str(profile["can_release"])} base={base}'
            + ('' if profile['readable'] else ' (tree unreadable, skipped)')
        )
    else:
        line += bool_str(profile['is_web'])
    print(line, file=sys.stderr)


def assign(repo_names, profiles, releasing):
    """{group key: repos} over `repo_names` (in order) and their profiles. Only a
    `releasing` (two-branch) population gets the release and cliff groups, and
    only its profiles carry a cliff_tier."""
    groups = {key: [] for key, _group in GROUPS}
    for repo in repo_names:
        profile = profiles[repo]
        lang = profile['lang']

        # Both opt-ins below resolve BEFORE the lang gate: a repo can carry
        # tested shell or its own publish.yaml while classifying lang=none
        # or lang=python.
        if profile['has_shell_tests']:
            groups['shell_test'].append(repo)
        # can_release keeps the two CI groups disjoint.
        if profile['has_publish'] and not profile['can_release']:
            groups['artifact_ci'].append(repo)

        if lang == 'none':
            continue
        # Lint baseline only; these repos keep their own bespoke ci.yaml.
        if lang == 'python':
            groups['python'].append(repo)
            continue

        if profile['can_release']:
            groups['ci'].append(repo)
            # Its own publish.yaml owns the repo's Releases; a synced release.yaml
            # would cut code Releases marked latest over them.
            if releasing and not profile['has_publish']:
                groups['release'].append(repo)
        if lang == 'go':
            groups['golangci'].append(repo)
        if lang == 'ts':
            groups['ts_config'].append(repo)
        if profile['has_smoke']:
            groups['smoke'].append(repo)
        if profile['has_dockerfile']:
            groups['repin'].append(repo)
        if releasing:
            groups['cliff_stable' if profile['cliff_tier'] == 'stable' else 'cliff_alpha'].append(
                repo
            )

    # Go repos that also have a TS surface need the TS configs too; appended
    # after the pure-TS repos, in repo order.
    for repo in repo_names:
        profile = profiles[repo]
        if profile['lang'] == 'go' and (
            profile['has_jsr'] or profile['has_pkg'] or profile['is_web']
        ):
            groups['ts_config'].append(repo)
    return groups


def emit_block(groups, profiles, *, base=None):
    """Print every group of one block (the single-branch one, or one base's),
    the TypeScript configs as one group per distinct set of ESLint-base folders:
    the root and each WEB_DIRS folder alone, then every other set sorted, then
    the group with no base."""
    suffix = f' (base: {base})' if base else ''
    for index, (key, group) in enumerate(GROUPS):
        lead_blank = base is not None or index > 0
        if key != 'ts_config':
            print_group(
                group.comment + suffix, groups[key], group, lead_blank=lead_blank, base=base
            )
            continue
        single = [(d,) for d in ('', *WEB_DIRS)]
        others = sorted({profiles[repo]['ts_dirs'] for repo in groups[key]} - {*single, ()})
        for ts_dirs in [*single, *others, ()]:
            repos = [repo for repo in groups[key] if profiles[repo]['ts_dirs'] == ts_dirs]
            variant = ts_config_group(ts_dirs)
            print_group(variant.comment + suffix, repos, variant, lead_blank=lead_blank, base=base)


def print_group(comment, repos, group, *, lead_blank=True, base=None):
    """Emit one sync.yml group (comment, repos block, file list) if non-empty."""
    if not repos:
        return
    if lead_blank:
        print()
    print(f'  # {comment}')
    print('  - repos: |')
    for repo in repos:
        print(f'      {OWNER}/{repo}')
    if base:
        print(f'    base: {base}')
    print(render_files(group))


def profile_repo(repo):
    """[(base, profile)] of one discovered repo: one row per base, each carrying
    its cliff tier, for a two-branch repo; else one row with base None and no
    tier. ClassifyError when a two-branch repo's tier cannot be read."""
    name = repo['name']
    if not release_channels.is_two_branch(repo):
        return [(None, classify(name))]
    found = [(base, classify(name, base, releasing=True)) for base in BASES]
    tier = cliff_tier(name)
    for _base, profile in found:
        profile['cliff_tier'] = tier
    return found


def main():
    # Skip the ci repo itself (it is the sync source, never a target).
    repos = [repo for repo in discover_repos() if repo['name'] != 'ci']

    single = []
    profiles = {}
    per_base = {base: {} for base in BASES}
    try:
        for repo, found in fanout.ordered(repos, profile_repo):
            name = repo['name']
            for base, profile in found:
                log_profile(name, profile, base)
                if base is None:
                    single.append(name)
                    profiles[name] = profile
                elif profile['readable']:
                    per_base[base][name] = profile
    except ClassifyError as err:
        sys.exit(f'classify-repos: {err}')

    print(HEADER)
    emit_block(assign(single, profiles, releasing=False), profiles)
    for base in BASES:
        names = sorted(per_base[base])
        groups = assign(names, per_base[base], releasing=True)
        groups['two_branch_renovate'] = names
        emit_block(groups, per_base[base], base=base)


if __name__ == '__main__':
    main()
