#!/usr/bin/env python3
"""The daily security scan of a two-branch repository's `main`, as one record.

Subcommands, run by security-scan.yaml on a scheduled or dispatched run of `main`:

    platforms <repo>  the platforms of ghcr.io/cplieger/<repo>:latest as GITHUB_OUTPUT
                      lines; a failed read is an `error=` line, never an empty image.
    sbom <repo> <index> --out FILE
                      the SPDX documents attested to that image index by the release
                      pipeline, signature verified with cosign; a failure is recorded
                      in FILE as `error`, never as an SBOM naming nothing.
    go-modules        one `<dir>\\t<slug>` line per tracked Go module with Go source.
    summarize         the Trivy and govulncheck reports reduced to security-main.json
                      and a step summary; exits 1 when an expected report is unreadable.

The record's fields are listed in docs/workflows.md; bump SCHEMA when they change,
because release_maintenance.py refuses any other value. Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import promote

SCHEMA = 1
SEVERITIES = ('HIGH', 'CRITICAL')
LOCKFILES = frozenset(
    {
        'package-lock.json',
        'npm-shrinkwrap.json',
        'yarn.lock',
        'pnpm-lock.yaml',
        'bun.lock',
        'uv.lock',
        'poetry.lock',
        'Pipfile.lock',
        'Cargo.lock',
    }
)
GO_STDLIB = frozenset({'stdlib', 'toolchain'})
SHAPE_ERRORS = (KeyError, IndexError, TypeError, AttributeError)

# Components an image builds from source or downloads, which the scanners see
# only through a hand-written CycloneDX fragment and match no advisory for, keyed
# by the fragment's path in the final image. A declared path whose names the
# Dockerfile does not show takes these; any other unnamed fragment is a problem.
# A component leaves this map and joins COVERED only with a positive test: a
# known-vulnerable and a known-fixed version, told apart by the scanner through
# the release's own signed SBOM.
SBOM_DIR = '/usr/share/sbom'
FROM_SOURCE: dict[str, dict[str, tuple[str, ...]]] = {
    'docker-fclones-scheduler': {f'{SBOM_DIR}/fclones-scheduler.cdx.json': ('fclones',)},
    'docker-keepalived': {f'{SBOM_DIR}/keepalived.cdx.json': ('keepalived',)},
    'docker-nut-upsd': {f'{SBOM_DIR}/nut-upsd.cdx.json': ('libmodbus', 'net-snmp', 'nut')},
    'docker-radvd': {f'{SBOM_DIR}/radvd.cdx.json': ('radvd',)},
    'docker-rsync-scheduler': {f'{SBOM_DIR}/rsync-scheduler.cdx.json': ('rsync',)},
    'docker-smtp-relay': {f'{SBOM_DIR}/postfix.cdx.json': ('postfix',)},
    'pg-autodump': {f'{SBOM_DIR}/pg-autodump.cdx.json': ('tini',)},
    'subflux': {f'{SBOM_DIR}/subflux-ffmpeg.cdx.json': ('ffmpeg', 'libx264')},
}
COVERED: frozenset[str] = frozenset()
# The signed release SBOM is what the image ships: syft's sbom-cataloger, enabled
# by docker-release.yaml, lists each fragment's components with this sourceInfo.
SBOM_SOURCE = re.compile(r'acquired package info from SBOM: (\S+)')
SPDX_PREDICATE = 'https://spdx.dev/Document'
SBOM_SIGNER = r'^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@'
NOT_COVERED_TEXT = (
    'These components are built from source or downloaded, and no scanner here has '
    'been shown to match their advisories, so the scan does not report on them:'
)
FRAGMENT_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')
FRAGMENT_PATH = re.compile(r'[^\s"\'=<>|;&()]*\.cdx\.json')
INSTRUCTION = re.compile(
    r'\s*(FROM|RUN|CMD|LABEL|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|VOLUME|USER|WORKDIR|ARG'
    r'|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL)\b',
    re.IGNORECASE,
)
HEREDOC_KEYWORDS = re.compile(r'\s*(?:ONBUILD\s+)?(?:RUN|COPY|ADD)\b', re.IGNORECASE)
# BuildKit opens a heredoc only for a whole shell word `<<NAME`, so `$((1<<n))` is not
# one: https://github.com/moby/buildkit/blob/master/frontend/dockerfile/parser/parser.go
SHELL_WORD = re.compile(r"""(?:[^\s'"]|'[^']*'|"(?:\\.|[^"\\])*")+""")
HEREDOC = re.compile(r'\d*<<(-?)([^<]+)')


def heredoc_markers(line: str) -> list[tuple[bool, str]]:
    """(tab-stripping, terminator) per heredoc a line opens, quotes removed."""
    found = [HEREDOC.fullmatch(w) for w in SHELL_WORD.findall(line)]
    marks = [(m[1] == '-', re.sub(r'[\'"\\]', '', m[2])) for m in found if m]
    return [(tabs, word) for tabs, word in marks if word]


def instructions(dockerfile: str) -> list[tuple[str, str]]:
    """(upper-case keyword, text) per instruction, comment and blank lines outside a
    heredoc dropped. Docker reads keywords in any case, so only a line that neither
    continues the previous one nor sits in a RUN, COPY or ADD heredoc starts one; a
    heredoc's body starts after the line that ends the instruction's continuation."""
    out: list[tuple[str, list[str]]] = []
    heredocs: list[tuple[bool, str]] = []
    pending: list[tuple[bool, str]] = []
    continued = False
    for line in dockerfile.splitlines():
        if heredocs:
            out[-1][1].append(line)
            tabs, word = heredocs[0]
            if (line.lstrip('\t') if tabs else line) == word:
                heredocs.pop(0)
            continue
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        m = None if continued else INSTRUCTION.match(line)
        if m:
            out.append((m[1].upper(), [line]))
        elif out:
            out[-1][1].append(line)
        else:
            continue
        continued = line.rstrip(' \t').endswith('\\')
        if HEREDOC_KEYWORDS.match(out[-1][1][0]):
            pending += heredoc_markers(line)
        if not continued:
            heredocs, pending = pending, []
    return [(kw, '\n'.join(lines)) for kw, lines in out]


def copy_args(text: str) -> tuple[str | None, list[str]]:
    """(the --from value or None, the sources then the destination) of a COPY or ADD."""
    words = text.replace('\\\n', ' ').split()[1:]
    flags = []
    while words and words[0].startswith('--'):
        flags.append(words.pop(0))
    rest = ' '.join(words)
    try:
        args = json.loads(rest) if rest.startswith('[') else words
    except ValueError:
        args = words
    froms = [f.split('=', 1)[1] for f in flags if f.startswith('--from=')]
    return (froms[-1] if froms else None), [str(a) for a in args]


def fragment_names(doc) -> set[str]:
    """Every component name of a CycloneDX document, nested components included."""
    if not isinstance(doc, dict):
        return set()
    meta = doc.get('metadata')
    stack = [meta.get('component') if isinstance(meta, dict) else None]
    stack += doc.get('components') or []
    names: set[str] = set()
    while stack:
        comp = stack.pop()
        if isinstance(comp, dict):
            if isinstance(comp.get('name'), str) and comp['name']:
                names.add(comp['name'].lower())
            stack += comp.get('components') or []
    return names


def context_fragments(root: Path, src: str, dest: str) -> tuple[dict[str, set[str]], list[str]]:
    """({image path: names}, problems) for every fragment a COPY of `src` from the
    build context puts in the image, a directory copy included."""
    if src.startswith(('http://', 'https://')):
        return {}, [f'{src} is fetched at build time'] if src.endswith('.cdx.json') else []
    rel = src.lstrip('/').removeprefix('./').rstrip('/')
    base = root.resolve()
    if '..' in Path(rel).parts:
        return {}, [f'{src} is outside the checkout']
    out: dict[str, set[str]] = {}
    problems = []
    for hit in [base] if rel in ('', '.') else sorted(base.glob(rel)):
        files = sorted(hit.rglob('*.cdx.json')) if hit.is_dir() else [hit]
        for path in files:
            if not path.name.endswith('.cdx.json') or not path.resolve().is_relative_to(base):
                continue
            shown_path = path.relative_to(base).as_posix()
            try:
                found = fragment_names(json.loads(path.read_text()))
            except (OSError, ValueError) as exc:
                problems.append(f'{shown_path} does not read as JSON: {exc}')
                continue
            if not found:
                problems.append(f'{shown_path} names no component')
                continue
            inner = path.relative_to(hit).as_posix() if hit.is_dir() else path.name
            target = dest.rstrip('/') + '/' + inner if dest.endswith('/') or hit.is_dir() else dest
            out[target] = found
    if not out and not problems and src.endswith('.cdx.json'):
        problems.append(f'{src} is not in the checkout')
    return out, problems


Files = dict[str, set[str] | None]


def stage_copy(files: Files, src: str, dest: str, into_dir: bool) -> Files:
    """The fragments a COPY --from of `src` to `dest` takes from a stage's files. A
    fragment path the stage does not name is copied as unnamed (None)."""
    base = src.rstrip('/')
    glob = any(c in base for c in '*?[')
    out: Files = {}
    for path, names in files.items():
        if path == base or (glob and fnmatch.fnmatchcase(path, base)):
            out[dest.rstrip('/') + '/' + path.rsplit('/', 1)[-1] if into_dir else dest] = names
        elif path.startswith(base + '/'):
            out[dest.rstrip('/') + '/' + path[len(base) + 1 :]] = names
    if not out and src.endswith('.cdx.json') and not glob:
        out[dest.rstrip('/') + '/' + src.rsplit('/', 1)[-1] if into_dir else dest] = None
    return out


def source_components(
    repo: str, dockerfile: str, root: Path, shipped: dict[str, set[str]] | None = None
) -> tuple[list[str], list[str]]:
    """(the components the scan does not cover, why a fragment could not be named).
    They are every name FROM_SOURCE declares for `repo`, every name `shipped` (the
    signed SBOM's fragments, by image path) lists, and what each CycloneDX fragment in
    the final image names, followed per stage through FROM inheritance, COPY --from
    and copies from the checkout. A final-image fragment with no name, no declaration
    and no SBOM entry is a problem, so it is never read as clean."""
    declared = FROM_SOURCE.get(repo, {})
    shipped = shipped or {}
    stages: list[dict] = []
    problems: list[str] = []

    def stage(ref: str) -> dict | None:
        named = [s for s in stages if s['name'] == ref.lower()]
        if named:
            return named[-1]
        return stages[int(ref)] if ref.isdigit() and int(ref) < len(stages) else None

    for keyword, text in instructions(dockerfile):
        if keyword == 'FROM':
            words = [w for w in text.split()[1:] if not w.startswith('--')]
            parent = stage(words[0]) if words else None
            stages.append(
                {
                    'name': words[2].lower()
                    if len(words) > 2 and words[1].lower() == 'as'
                    else None,
                    'files': dict(parent['files']) if parent else {},
                    'pathless': set(parent['pathless']) if parent else set(),
                }
            )
            continue
        if not stages:
            continue
        files = stages[-1]['files']
        if keyword in ('COPY', 'ADD'):
            from_ref, args = copy_args(text)
            *sources, dest = args or ['']
            source = stage(from_ref) if from_ref is not None else None
            for src in sources:
                if from_ref is not None:
                    into_dir = len(sources) > 1 or dest.endswith('/')
                    files.update(stage_copy(source['files'] if source else {}, src, dest, into_dir))
                    continue
                got, bad = context_fragments(root, src, dest)
                files.update(got)
                problems += [f'the fragment {b}' for b in bad]
            continue
        paths = {p for p in FRAGMENT_PATH.findall(text) if '*' not in p}
        found = {n.lower() for n in FRAGMENT_NAME.findall(text)}
        if found and paths:
            files.update({p: found for p in paths if not files.get(p)})
        elif found and 'CycloneDX' in text:
            stages[-1]['pathless'] |= found
        elif 'CycloneDX' in text and not paths:
            problems.append('an instruction writes a CycloneDX document naming no component')
        else:
            files.update({p: None for p in paths if p not in files})

    final = stages[-1] if stages else {'files': {}, 'pathless': set()}
    names = set().union(*declared.values(), *shipped.values(), final['pathless'])
    for path, got in sorted(final['files'].items()):
        if got is not None:
            names |= got
        elif path not in declared and path not in shipped:
            problems.append(f'no component name resolves for {path}')
    if any(s['pathless'] - final['pathless'] for s in stages):
        problems.append(
            'a stage the image may not ship writes a CycloneDX document to no named path'
        )
    return sorted(names - COVERED), problems


def sbom_fragments(documents: list) -> dict[str, set[str]]:
    """{image path: names} of the fragment components SPDX documents list;
    ValueError when one is not an SPDX document."""
    out: dict[str, set[str]] = {}
    for doc in documents:
        if (
            not isinstance(doc, dict)
            or 'spdxVersion' not in doc
            or not isinstance(doc.get('packages'), list)
        ):
            raise ValueError('not an SPDX document')
        for pkg in doc['packages']:
            info = pkg.get('sourceInfo') if isinstance(pkg, dict) else None
            m = SBOM_SOURCE.fullmatch(info) if isinstance(info, str) else None
            if m and isinstance(pkg.get('name'), str) and pkg['name']:
                out.setdefault(m[1], set()).add(pkg['name'].lower())
    return out


def signed_sboms(repo: str, index: str, run=subprocess.run, sleep=time.sleep) -> list[dict]:
    """The SPDX documents attested to `index` by the release pipeline. cosign retries
    none of its network calls, so a failed verification is retried twice."""
    ref = f'{promote.REGISTRY}/{promote.OWNER}/{repo}@{index}'
    argv = [
        'cosign',
        'verify-attestation',
        '--type',
        'spdxjson',
        '--certificate-oidc-issuer',
        'https://token.actions.githubusercontent.com',
        '--certificate-identity-regexp',
        SBOM_SIGNER,
        ref,
    ]
    for attempt in range(3):
        proc = run(argv, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            break
        if attempt < 2:
            sleep(10 * 2**attempt)
    else:
        tail = (proc.stderr.strip().splitlines() or ['no output'])[-1]
        raise ValueError(f'cosign verify-attestation {ref}: {tail}')
    docs = []
    try:
        for line in filter(str.strip, proc.stdout.splitlines()):
            statement = json.loads(base64.b64decode(json.loads(line)['payload']))
            subjects = {f'sha256:{s["digest"]["sha256"]}' for s in statement['subject']}
            if statement.get('predicateType') == SPDX_PREDICATE and index in subjects:
                docs.append(statement['predicate'])
    except (ValueError, binascii.Error, *SHAPE_ERRORS) as exc:
        raise ValueError(f'unreadable attestation of {ref}: {exc!r}') from None
    if not docs:
        raise ValueError(f'no SPDX attestation of {ref}')
    return docs


# ── Report reducers ───────────────────────────────────────────────────────────


def trivy_class(result: dict, package: str, *, image: bool) -> str:
    if package in GO_STDLIB:
        return 'stdlib'
    if image:
        return 'os' if result.get('Class') == 'os-pkgs' else 'manifest'
    return 'lockfile' if Path(result.get('Target', '')).name in LOCKFILES else 'manifest'


def trivy_findings(report, *, platform: str | None) -> list[dict]:
    """Findings of one Trivy JSON report; ValueError when it is not one."""
    if not isinstance(report, dict) or 'SchemaVersion' not in report:
        raise ValueError('not a Trivy JSON report')
    try:
        return [
            trivy_record(result, v, platform)
            for result in report.get('Results') or []
            for v in result.get('Vulnerabilities') or []
            if v['Severity'] in SEVERITIES
        ]
    except SHAPE_ERRORS as exc:
        raise ValueError(f'unexpected Trivy report shape: {exc!r}') from None


def trivy_record(result: dict, v: dict, platform: str | None) -> dict:
    klass = trivy_class(result, v['PkgName'], image=platform is not None)
    return {
        'id': v['VulnerabilityID'],
        'package': v['PkgName'],
        'installed': v.get('InstalledVersion', ''),
        'fixed': v.get('FixedVersion') or None,
        'severity': v['Severity'],
        'class': klass,
        'targets': ['image' if klass == 'os' else result['Target']],
        'platforms': [platform] if platform else [],
        'sources': ['trivy-image' if platform else 'trivy-fs'],
    }


def json_stream(text: str) -> list:
    """govulncheck -format json prints concatenated JSON objects."""
    decoder, at, out = json.JSONDecoder(), 0, []
    while True:
        while at < len(text) and text[at].isspace():
            at += 1
        if at == len(text):
            return out
        obj, at = decoder.raw_decode(text, at)
        out.append(obj)


def govulncheck_findings(text: str, module_dir: str) -> list[dict]:
    """Called vulnerabilities of one module; ValueError when the stream is not a report."""
    try:
        objs = json_stream(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'not a govulncheck JSON stream: {exc}') from None
    if not objs or not isinstance(objs[0], dict) or 'config' not in objs[0]:
        raise ValueError('not a govulncheck JSON stream: no config record')
    gomod = 'go.mod' if module_dir == '.' else f'{module_dir}/go.mod'
    try:
        called = [
            o['finding']
            for o in objs
            if 'finding' in o and o['finding']['trace'][0].get('function')
        ]
        return [govulncheck_record(f, gomod) for f in called]
    except SHAPE_ERRORS as exc:
        raise ValueError(f'unexpected govulncheck record shape: {exc!r}') from None


def govulncheck_record(finding: dict, gomod: str) -> dict:
    top = finding['trace'][0]
    stdlib = top['module'] in GO_STDLIB
    return {
        'id': finding['osv'],
        'package': 'stdlib' if stdlib else top['module'],
        'installed': top.get('version', ''),
        'fixed': finding.get('fixed_version') or None,
        'severity': None,
        'class': 'stdlib' if stdlib else 'manifest',
        'targets': [gomod],
        'platforms': [],
        'sources': ['govulncheck'],
    }


def merge(findings: list[dict]) -> list[dict]:
    """One record per (id, package, installed, fixed, class), its lists unioned, so a
    platform's record names only the fix that platform's scan reported."""
    merged: dict[tuple, dict] = {}
    for f in findings:
        key = (f['id'], f['package'], f['installed'], f['fixed'], f['class'])
        have = merged.setdefault(key, {**f, 'targets': [], 'platforms': [], 'sources': []})
        for field in ('targets', 'platforms', 'sources'):
            have[field] = sorted({*have[field], *f[field]})
    return sorted(merged.values(), key=lambda f: (f['fixed'] is None, f['id'], f['package']))


# ── Inputs from the runner ────────────────────────────────────────────────────


def go_modules(root: Path) -> list[tuple[str, str]]:
    """(dir, slug) of each tracked go.mod outside vendor/ and testdata/ that owns a
    tracked .go file, so a source-free module is skipped. A file belongs to the module
    of its nearest enclosing go.mod; files under vendor/ or testdata/ build nothing."""
    tracked = subprocess.run(
        ['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, text=True, check=True
    ).stdout.split('\0')
    bounds = [
        '/'.join(p.split('/')[:-1]) or '.'
        for p in sorted(p for p in tracked if p == 'go.mod' or p.endswith('/go.mod'))
    ]
    owners = set()
    for g in (p for p in tracked if p.endswith('.go')):
        if {'vendor', 'testdata'} & set(g.split('/')[:-1]):
            continue
        within = [m for m in bounds if m == '.' or g.startswith(f'{m}/')]
        if within:
            owners.add(max(within, key=lambda m: 0 if m == '.' else m.count('/') + 1))
    return [
        (m, 'root' if m == '.' else m.replace('/', '__'))
        for m in bounds
        if m in owners and not {'vendor', 'testdata'} & set(m.split('/'))
    ]


def read_json(path: Path, errors: list[str], what: str):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        errors.append(f'{what}: no report ({path.name})')
    except ValueError as exc:
        errors.append(f'{what}: unreadable report ({path.name}): {exc}')
    return None


def fs_findings(reports: Path, errors: list[str]) -> list[dict]:
    report = read_json(reports / 'trivy-fs.json', errors, 'Trivy filesystem scan')
    try:
        return [] if report is None else trivy_findings(report, platform=None)
    except ValueError as exc:
        errors.append(f'Trivy filesystem scan: {exc}')
        return []


def image_findings(args, reports: Path, errors: list[str]) -> tuple[dict | None, list[dict]]:
    """The published image record and its findings, every platform expected."""
    if args.image != 'true':
        return None, []
    rows = json.loads(args.platforms or '[]')
    image = None
    if args.image_error:
        errors.append(f'published image: {args.image_error}')
    elif not rows:
        errors.append(f'published image: not resolved (job {args.image_job or "not run"})')
    else:
        image = {
            'ref': f'{promote.REGISTRY}/{promote.OWNER}/{args.repo}:latest',
            'digest': args.index,
            'platforms': {r['platform']: r['ref'].rsplit('@', 1)[1] for r in rows},
        }
    found = []
    for row in rows:
        what = f'Trivy image scan of {row["platform"]}'
        report = read_json(reports / f'trivy-image-{row["slug"]}.json', errors, what)
        try:
            found += [] if report is None else trivy_findings(report, platform=row['platform'])
        except ValueError as exc:
            errors.append(f'{what}: {exc}')
    return image, found


def module_findings(root: Path, reports: Path, errors: list[str]) -> list[dict]:
    """govulncheck findings of every module go_modules lists, each one expected."""
    try:
        modules = go_modules(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f'govulncheck: could not list the Go modules: {exc}')
        return []
    tsv = reports / 'govulncheck-modules.tsv'
    if modules and not tsv.is_file():
        errors.append('govulncheck: no module list (govulncheck-modules.tsv)')
        return []
    statuses = {}
    for line in tsv.read_text().splitlines() if modules else ():
        parts = line.split('\t')
        if len(parts) == 3:
            statuses[parts[0]] = parts[2]
        else:
            errors.append(f'govulncheck: unreadable module list line {line!r}')
    found = []
    for mod_dir, slug in modules:
        what = f'govulncheck of {mod_dir}'
        if statuses.get(mod_dir) != 'ok':
            err = reports / f'govulncheck-{slug}.err'
            tail = err.read_text().strip().splitlines()[-1:] if err.is_file() else []
            why = 'not run' if mod_dir not in statuses else 'failed'
            errors.append(f'{what}: {why}{": " + tail[0] if tail else ""}')
            continue
        try:
            found += govulncheck_findings(
                (reports / f'govulncheck-{slug}.json').read_text(), mod_dir
            )
        except FileNotFoundError:
            errors.append(f'{what}: no report (govulncheck-{slug}.json)')
        except ValueError as exc:
            errors.append(f'{what}: {exc}')
    return found


def shipped_fragments(args, reports: Path, errors: list[str]) -> dict[str, set[str]]:
    """The fragments the published image's signed SBOM lists, by image path."""
    record = read_json(reports / 'sbom.json', errors, 'signed SBOM')
    if record is None:
        return {}
    if not isinstance(record, dict) or record.get('index') != args.index:
        errors.append(f'signed SBOM: not the SBOM of {args.index}')
        return {}
    if 'error' in record:
        errors.append(f'signed SBOM: {record["error"]}')
        return {}
    try:
        return sbom_fragments(record.get('documents') or [])
    except ValueError as exc:
        errors.append(f'signed SBOM: {exc}')
        return {}


def collect(args, root: Path) -> dict:
    errors: list[str] = []
    reports = Path(args.reports)
    findings = fs_findings(reports, errors)
    image, found = image_findings(args, reports, errors)
    findings += found + module_findings(root, reports, errors)
    shipped = shipped_fragments(args, reports, errors) if image else {}
    dockerfile = root / args.dockerfile
    uncovered, unnamed = source_components(
        args.repo, dockerfile.read_text() if dockerfile.is_file() else '', root, shipped
    )
    errors += [f'source components: {p}' for p in unnamed]
    return {
        'schema': SCHEMA,
        'repo': f'{promote.OWNER}/{args.repo}',
        'commit': args.commit,
        'image': image,
        'findings': merge(findings),
        'not_covered': uncovered,
        'complete': not errors,
        'errors': errors,
    }


# ── Rendering ─────────────────────────────────────────────────────────────────


def cell(value) -> str:
    return str(value if value not in (None, '') else '-').replace('|', '\\|')


def render(doc: dict) -> str:
    lines = [f'## Security scan of `main` at {doc["commit"][:12]}', '']
    image = doc['image']
    if image:
        plats = ', '.join(sorted(image['platforms']))
        lines += [f'Published image: `{image["ref"]}` (`{image["digest"]}`), {plats}.', '']
    if doc['errors']:
        lines += ['**The scan is incomplete. Do not read the findings below as the full set.**', '']
        lines += [f'- {e}' for e in doc['errors']] + ['']
    fixable = [f for f in doc['findings'] if f['fixed']]
    unfixed = len(doc['findings']) - len(fixable)
    if fixable:
        lines += [
            f'### Fixable findings ({len(fixable)})',
            '',
            '| ID | Package | Installed | Fixed | Severity | Class | Where |',
            '|---|---|---|---|---|---|---|',
        ]
        for f in fixable:
            where = ', '.join(f['platforms'] or f['targets'])
            lines.append(
                '| '
                + ' | '.join(
                    cell(x)
                    for x in (
                        f['id'],
                        f['package'],
                        f['installed'],
                        f['fixed'],
                        f['severity'],
                        f['class'],
                        where,
                    )
                )
                + ' |'
            )
        lines.append('')
    elif not doc['errors']:
        lines += ['No fixable finding.', '']
    if unfixed:
        lines += [f'{unfixed} finding(s) have no fix yet, and `security-main.json` lists them.', '']
    if doc['not_covered']:
        lines += [
            '### Not covered by the scan',
            '',
            NOT_COVERED_TEXT,
            '',
            *(f'- {n}' for n in doc['not_covered']),
            '',
        ]
    return '\n'.join(lines)


# ── Entry points ──────────────────────────────────────────────────────────────


def cmd_platforms(args) -> int:
    try:
        token = promote.registry_token(args.repo)
        digest = promote.tag_digest(args.repo, 'latest', token)
        plats = promote.platforms(args.repo, digest, token)
    except promote.GhError as exc:
        print('platforms=[]')
        print(f'error=ghcr.io/{promote.OWNER}/{args.repo}:latest: {" ".join(str(exc).split())}')
        return 0
    rows = [
        {
            'platform': plat,
            'slug': plat.replace('/', '-'),
            'ref': f'{promote.REGISTRY}/{promote.OWNER}/{args.repo}@{ref}',
        }
        for plat, ref in sorted(plats.items())
    ]
    print(f'index={digest}')
    print(f'platforms={json.dumps(rows, separators=(",", ":"))}')
    return 0


def cmd_sbom(args) -> int:
    try:
        record = {'index': args.index, 'documents': signed_sboms(args.repo, args.index)}
    except (ValueError, OSError) as exc:
        record = {'index': args.index, 'error': ' '.join(str(exc).split())}
        print(f'::warning::signed SBOM: {record["error"]}')
    Path(args.out).write_text(json.dumps(record) + '\n')
    return 0


def cmd_go_modules(_args) -> int:
    for mod_dir, slug in go_modules(Path.cwd()):
        print(f'{mod_dir}\t{slug}')
    return 0


def cmd_summarize(args) -> int:
    doc = collect(args, Path.cwd())
    Path(args.out).write_text(json.dumps(doc, indent=2) + '\n')
    with open(args.summary, 'a', encoding='utf-8') as fh:
        fh.write(render(doc) + '\n')
    for e in doc['errors']:
        print(f'::error::{e}')
    return 0 if doc['complete'] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('platforms')
    p.add_argument('repo', type=lambda s: s if promote.REPO_RE.match(s) else parser.error(s))
    b = sub.add_parser('sbom')
    b.add_argument('repo', type=lambda s: s if promote.REPO_RE.match(s) else parser.error(s))
    b.add_argument('index')
    b.add_argument('--out', required=True)
    sub.add_parser('go-modules')
    s = sub.add_parser('summarize')
    s.add_argument('--repo', required=True)
    s.add_argument('--commit', required=True)
    s.add_argument('--reports', required=True)
    s.add_argument('--dockerfile', default='Dockerfile')
    s.add_argument('--image', choices=('true', 'false'), required=True)
    s.add_argument('--platforms', default='')
    s.add_argument('--index', default='')
    s.add_argument('--image-error', default='')
    s.add_argument('--image-job', default='')
    s.add_argument('--out', required=True)
    s.add_argument('--summary', required=True)
    args = parser.parse_args(argv)
    commands = {
        'platforms': cmd_platforms,
        'sbom': cmd_sbom,
        'go-modules': cmd_go_modules,
        'summarize': cmd_summarize,
    }
    return commands[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
