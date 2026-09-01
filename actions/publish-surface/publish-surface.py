#!/usr/bin/env python3
"""Verify a build-less TypeScript package publishes exactly its exports' import closure.

These libraries ship raw TypeScript, so the published artifact IS source and
a consumer compiles it with none of the library's own devDependencies
(vitest, fast-check, happy-dom, @types/node) installed — a green `tsc` run in
the library proves nothing about the consumer.

The exports map already declares the public API, so the TypeScript that must
be published is exactly the transitive import closure of those entry points;
everything else under src/ is test-only by construction. Linting exclusion
patterns instead of computing the closure is why they were wrong in six
places at once for one missing file, so this script computes the set and
checks each registry's real output against it via its own tooling
(`npm pack --dry-run --json`, `jsr publish --dry-run`) rather than
reimplementing glob semantics.

Three failure classes, from one `tsc --listFiles` pass over the entry points:

  leak       published but unreachable from any export. How
             @cplieger/web-terminal-ui@5.6.1 shipped a vitest-importing test
             setup file and broke every consumer's Docker build, and how
             @cplieger/web-terminal-engine@3.10.4 shipped fast-check test
             helpers to JSR — both behind correct-looking exclusion patterns.
  missing    reachable from an export but not published, so a consumer
             resolves an export to a file the tarball does not contain.
  undeclared the closure needs a node_modules package that is neither a
             dependency nor a peerDependency.

Exit 0 when every registry's .ts set equals the closure and every external
edge is declared; 1 otherwise, with each offending path named. A
package.json publishing nothing (no `name`, or `private: true`) is skipped
with a notice, since ts-ci also runs against app-frontend build manifests.

Usage:
    publish-surface.py                     # check ./
    publish-surface.py --package-dir web   # subpackage layout (engine)
    publish-surface.py --skip-jsr          # npm only
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# `jsr publish --dry-run` prints one `file:///abs/path (size)` line per file.
JSR_FILE_LINE = re.compile(r'file://(/[^\s]*?)(?:\s+\([^)]*\))?\s*$')

# A node_modules path segment -> owning package name, `@scope/name` aware.
NODE_MODULES_PKG = re.compile(r'.*/node_modules/(?P<name>@[^/]+/[^/]+|[^/@][^/]*)/')

# Pinned to match the release workflow's JSR_VERSION, so the checked surface is the published surface.
# renovate: datasource=npm depName=jsr
JSR_VERSION = '0.14.3'


class SurfaceError(RuntimeError):
    """A condition that stops the check rather than producing a finding.

    Distinct from a finding on purpose: a finding is a defect in the package
    under test, while this is the check itself being unable to answer. Both
    fail the gate, but only one of them is the package's fault, and reporting
    "no leaks" because the closure could not be computed would be a gate that
    passes by accident.
    """


def run(
    cmd: list[str], cwd: Path, *, timeout: int = 300, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing text output, with a mandatory timeout."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise SurfaceError(f'{cmd[0]}: not found on PATH') from exc
    except subprocess.TimeoutExpired as exc:
        raise SurfaceError(f'{" ".join(cmd)}: timed out after {timeout}s') from exc
    if check and proc.returncode != 0:
        raise SurfaceError(
            f'{" ".join(cmd)}: exit {proc.returncode}\n'
            f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}'
        )
    return proc


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise SurfaceError(f'{path}: not found') from exc
    except json.JSONDecodeError as exc:
        raise SurfaceError(f'{path}: invalid JSON ({exc})') from exc


def collect_export_targets(exports: object) -> list[str]:
    """Flatten an `exports` map to its target paths.

    Handles the string form (`"./src/index.ts"`), the subpath map, and nested
    condition objects (`{"import": ..., "default": ...}`). Every leaf string is
    a target; the caller decides which are TypeScript.
    """
    out: list[str] = []
    if isinstance(exports, str):
        out.append(exports)
    elif isinstance(exports, dict):
        for value in exports.values():
            out.extend(collect_export_targets(value))
    elif isinstance(exports, list):
        for value in exports:
            out.extend(collect_export_targets(value))
    return out


def publishable(pkg: dict) -> tuple[bool, str]:
    """Decide whether this package.json describes something that gets published.

    `name` is the discriminator, not directory location or `web-lint`: a
    subdirectory library (web-terminal-engine) arrives through the same `web`
    job as an app frontend, but only a frontend's package.json omits `name`
    (it exists purely to pin build deps). Deliberately NOT "has no exports
    map" — a named package missing `exports` fails closed in entry_points
    instead of skipping quietly.
    """
    if pkg.get('private') is True:
        return False, 'package.json sets private: true, so nothing is published'
    if not pkg.get('name'):
        return False, (
            'package.json declares no name, so this is a build manifest rather '
            'than a publishable package'
        )
    return True, ''


def entry_points(pkg: dict, pkg_dir: Path) -> tuple[list[Path], list[str]]:
    """Split the exports map into TypeScript entry points and non-TS assets.

    An asset (`./wire-compatibility.json`, `./css/ui-primitives.css`) is a
    deliberate publish that no import graph reaches, so it is reported for
    context and excluded from the closure comparison, which covers .ts only.
    """
    exports = pkg.get('exports')
    if exports is None:
        raise SurfaceError(
            'package.json has no `exports` map, so the public API is not '
            'declared and the publish surface cannot be derived. Add `exports` '
            'or exclude this package from the check.'
        )
    ts: list[Path] = []
    assets: list[str] = []
    for target in collect_export_targets(exports):
        if not target.startswith('.'):
            # A bare specifier in `exports` is a re-export of a dependency,
            # not a file this package ships.
            continue
        if target.endswith('.ts'):
            resolved = (pkg_dir / target).resolve()
            if not resolved.is_file():
                raise SurfaceError(
                    f'exports target {target} does not exist at {resolved}. '
                    'The exports map names a file the repo does not have.'
                )
            ts.append(resolved)
        else:
            assets.append(target)
    if not ts:
        raise SurfaceError('exports map contains no .ts targets; nothing to close over')
    # Deduplicate while keeping a stable order for reproducible tsconfig output.
    seen: set[Path] = set()
    unique = [p for p in ts if not (p in seen or seen.add(p))]
    return unique, assets


def resolve_tsc(pkg_dir: Path) -> Path:
    """Locate the repo's OWN pinned compiler, or refuse to run.

    Not `npx --no-install tsc` and not a PATH lookup: with node_modules
    absent, npx falls through to an unrelated `tsc` stub package on npm,
    reporting a false compile defect for a package that just needs `npm ci`.
    """
    for candidate in [pkg_dir, *pkg_dir.parents]:
        binary = candidate / 'node_modules' / '.bin' / 'tsc'
        if binary.is_file():
            return binary
        if (candidate / '.git').exists():
            break
    raise SurfaceError(
        f'no node_modules/.bin/tsc found at or above {pkg_dir}. Run `npm ci` in '
        'the package directory first; the closure must be computed by the '
        'compiler this repo pins, not by whatever is on PATH.'
    )


def compute_closure(pkg_dir: Path, entries: list[Path]) -> tuple[set[Path], set[str]]:
    """Return (local .ts closure, external package names) for the public API.

    Inherits the repo's own compilerOptions via `extends`, overriding only:
    `files` to the export entry points (the program IS the public graph),
    `include` emptied (so `src/**/*.ts` can't drag in the test-only files
    this check exists to find), and `types` emptied — the load-bearing one,
    since it drops automatic @types/* inclusion so a node_modules path left
    in --listFiles is a real import edge, matching a consumer with no
    @types/node.
    """
    tsconfig = pkg_dir / 'tsconfig.json'
    if not tsconfig.is_file():
        raise SurfaceError(f'{tsconfig}: not found; cannot inherit compiler options')
    tsc = resolve_tsc(pkg_dir)

    # `extends` resolves relative paths in the inherited config against the
    # inherited config's own directory, so `rootDir: src` stays correct even
    # though this config lives in a temp dir.
    probe = {
        'extends': str(tsconfig.resolve()),
        'compilerOptions': {'noEmit': True, 'types': []},
        'files': [str(p) for p in entries],
        'include': [],
        'exclude': [],
    }

    with tempfile.TemporaryDirectory(prefix='publish-surface-') as tmp:
        probe_path = Path(tmp) / 'tsconfig.json'
        probe_path.write_text(json.dumps(probe, indent=2), encoding='utf-8')
        proc = run(
            [str(tsc), '-p', str(probe_path), '--listFiles'],
            cwd=pkg_dir,
            check=False,
        )

    listed = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not listed:
        raise SurfaceError(
            'tsc --listFiles produced no output; the compiler could not build a '
            f'program for the public API.\nstdout:\n{proc.stdout}\n'
            f'stderr:\n{proc.stderr}'
        )

    # A non-zero exit means the public graph does not compile in a
    # consumer-shaped environment. That is a finding, not a broken check, and
    # the diagnostics identify it far better than a set difference would.
    if proc.returncode != 0:
        diagnostics = '\n'.join(
            line for line in (proc.stdout + '\n' + proc.stderr).splitlines() if 'error TS' in line
        )
        raise SurfaceError(
            'the public API does not compile with only its declared types '
            '(this is what a consumer sees):\n' + (diagnostics or proc.stdout)
        )

    pkg_root = pkg_dir.resolve()
    local: set[Path] = set()
    external: set[str] = set()
    for raw in listed:
        path = Path(raw)
        text = str(path)
        if '/node_modules/' in text:
            # The compiler's own lib.*.d.ts files ship inside its package;
            # they are toolchain, not a dependency edge.
            if '/node_modules/@typescript/' in text or '/node_modules/typescript/' in text:
                continue
            match = NODE_MODULES_PKG.match(text)
            if match:
                external.add(match.group('name'))
            continue
        if path.suffix != '.ts':
            continue
        try:
            rel = path.resolve().relative_to(pkg_root)
        except ValueError:
            # Outside the package (a sibling in a monorepo, or a lib file from
            # a compiler installed outside node_modules). Not this package's
            # publish surface.
            continue
        local.add(Path(rel))
    return local, external


def npm_shipped(pkg_dir: Path) -> set[Path]:
    """The exact file set `npm publish` would upload, from npm itself."""
    proc = run(['npm', 'pack', '--dry-run', '--json'], cwd=pkg_dir)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SurfaceError(f'npm pack --json returned non-JSON: {exc}\n{proc.stdout}') from exc
    if not payload or 'files' not in payload[0]:
        raise SurfaceError(f'npm pack --json payload missing `files`: {proc.stdout[:400]}')
    return {Path(entry['path']) for entry in payload[0]['files']}


def jsr_shipped(pkg_dir: Path) -> set[Path]:
    """The exact file set `jsr publish` would upload, from the jsr CLI.

    `--dry-run` needs no credentials and no network beyond fetching the pinned
    CLI. `--allow-dirty` is required because CI checks out a working tree the
    CLI considers dirty; it does not relax any file selection.
    """
    proc = run(
        ['npx', '--yes', f'jsr@{JSR_VERSION}', 'publish', '--dry-run', '--allow-dirty'],
        cwd=pkg_dir,
        check=False,
    )
    combined = proc.stdout + '\n' + proc.stderr
    pkg_root = pkg_dir.resolve()
    shipped: set[Path] = set()
    for line in combined.splitlines():
        match = JSR_FILE_LINE.search(line.strip())
        if not match:
            continue
        try:
            shipped.add(Path(match.group(1)).resolve().relative_to(pkg_root))
        except ValueError:
            continue
    if not shipped:
        raise SurfaceError(
            'jsr publish --dry-run listed no files; cannot verify the JSR '
            f'surface.\nexit {proc.returncode}\n{combined[-2000:]}'
        )
    return shipped


def ts_under_src(paths: set[Path]) -> set[Path]:
    """Restrict a file set to the TypeScript it publishes from src/.

    Assets (css/, scaffold/, LICENSE, README, *.json) are deliberate publishes
    that no import graph reaches, so comparing them to a closure would be a
    category error.
    """
    return {p for p in paths if p.suffix == '.ts' and p.parts and p.parts[0] == 'src'}


def declared_dependencies(pkg: dict) -> set[str]:
    """Every package name a consumer will have available.

    `dependencies` and `peerDependencies` only. devDependencies are absent in a
    consumer's install by definition, which is the whole point, and
    optionalDependencies cannot be relied on to be present.
    """
    names: set[str] = set()
    for field in ('dependencies', 'peerDependencies'):
        names.update((pkg.get(field) or {}).keys())
    return names


def report(registry: str, closure: set[Path], shipped: set[Path]) -> list[str]:
    """Compare one registry's published .ts against the closure, both ways."""
    findings: list[str] = []
    for path in sorted(shipped - closure):
        findings.append(
            f'{registry}: leak: {path} is published but no declared export '
            f'reaches it. Exclude it from the {registry} publish surface, or '
            f'export it if consumers are meant to import it.'
        )
    for path in sorted(closure - shipped):
        findings.append(
            f'{registry}: missing: {path} is reachable from a declared export '
            f'but is NOT published, so a consumer resolves an export to a file '
            f'the package does not contain. Add it to the {registry} publish surface.'
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the published file set equals the public API's import closure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--package-dir',
        default='.',
        help='directory holding package.json (default: .)',
    )
    parser.add_argument(
        '--skip-jsr',
        action='store_true',
        help='check npm only, even when jsr.json is present',
    )
    parser.add_argument(
        '--github',
        action='store_true',
        help='emit ::error:: annotations for GitHub Actions',
    )
    args = parser.parse_args()

    pkg_dir = Path(args.package_dir).resolve()
    if not pkg_dir.is_dir():
        print(f'error: --package-dir {pkg_dir} is not a directory', file=sys.stderr)
        return 2

    try:
        pkg = read_json(pkg_dir / 'package.json')

        ships, reason = publishable(pkg)
        if not ships:
            print(f'skipped: {reason}')
            return 0

        entries, assets = entry_points(pkg, pkg_dir)
        closure_all, external = compute_closure(pkg_dir, entries)
        closure = ts_under_src(closure_all)

        print(f'package:      {pkg.get("name")}')
        print(f'entry points: {len(entries)} exported .ts')
        if assets:
            print(f'assets:       {", ".join(sorted(assets))} (not closure-checked)')
        print(f'closure:      {len(closure)} .ts under src/ reachable from exports')

        findings: list[str] = []

        npm_set = ts_under_src(npm_shipped(pkg_dir))
        print(f'npm ships:    {len(npm_set)} .ts under src/')
        findings.extend(report('npm', closure, npm_set))

        has_jsr = (pkg_dir / 'jsr.json').is_file()
        if has_jsr and not args.skip_jsr:
            jsr_set = ts_under_src(jsr_shipped(pkg_dir))
            print(f'jsr ships:    {len(jsr_set)} .ts under src/')
            findings.extend(report('jsr', closure, jsr_set))
        elif has_jsr:
            print('jsr ships:    skipped (--skip-jsr)')
        else:
            print('jsr ships:    no jsr.json; npm only')

        declared = declared_dependencies(pkg)
        undeclared = sorted(external - declared)
        print(
            f'external:     {len(external)} package(s) reached'
            + (f' ({", ".join(sorted(external))})' if external else '')
        )
        for name in undeclared:
            findings.append(
                f'deps: undeclared: the published type graph requires `{name}`, '
                f'which is neither a dependency nor a peerDependency, so a '
                f'consumer cannot resolve it. Declare it, or stop importing it '
                f'from published source. One undeclared import can surface its '
                f'own type dependencies here too, so fix the import you '
                f'recognise first and re-run.'
            )
    except SurfaceError as exc:
        message = f'publish-surface check could not complete: {exc}'
        if args.github:
            print(f'::error::{message.splitlines()[0]}')
        print(f'error: {message}', file=sys.stderr)
        return 1

    if not findings:
        print("\nOK: every registry publishes exactly the public API's closure.")
        return 0

    print(f'\n{len(findings)} finding(s):', file=sys.stderr)
    for finding in findings:
        if args.github:
            print(f'::error::{finding}')
        print(f'  {finding}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
