#!/usr/bin/env python3
"""Write each Go module's .gremlins.yaml: the central config plus its deeper modules.

gremlins v0.6.0 mutates every non-test .go file under the module root it runs
in, nested modules included (internal/engine/engine.go walks os.DirFS of the
module root, filtered only by unleash.exclude-files). When the outer module's
tests compile a nested module's in-tree sources (a `replace ... => ./<dir>`)
and the run uses `--coverpkg ./...` (integration mode), they execute and kill
those mutants, which then carry a verdict the nested module's own run also
records. So every copy gets one `'^<dir>/'` entry per deeper module, from the
same module list and the same `deeper_dirs` that gremlins-merge.py later uses
to refuse any such file that still appears. A nested module the workflow does
not measure (`--exclude-dir`) is excluded the same way but gets no copy, and
the merge, which never sees its dir, cannot refuse its files.

Usage
-----
    gremlins-scope.py --config configs/gremlins.yaml --target target \
        --exclude-dir=static/ui -- . mapread

writes target/./.gremlins.yaml (excluding '^mapread/' and '^static/ui/') and
target/mapread/.gremlins.yaml (the central config unchanged).
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'gremlins_merge', Path(__file__).with_name('gremlins-merge.py')
)
gremlins_merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gremlins_merge)

# The workflow's module discovery refuses any other character, which also keeps
# '.' the only regex metacharacter to escape and no quote inside a YAML entry.
SAFE_DIR = re.compile(r'[A-Za-z0-9._/-]+')
ANCHOR = re.compile(r'^  exclude-files:[ \t]*$', re.MULTILINE)


def scoped_config(central: str, dir_: str, all_dirs: list[str]) -> str:
    """The central config with dir_'s deeper module directories excluded."""
    inner = gremlins_merge.deeper_dirs(dir_, all_dirs)
    if not inner:
        return central
    anchors = list(ANCHOR.finditer(central))
    if len(anchors) != 1:
        raise SystemExit(
            f'gremlins-scope: expected one "  exclude-files:" line in the central config, '
            f'found {len(anchors)}'
        )
    entries = ''.join(f"    - '^{d.replace('.', r'\.')}/'\n" for d in sorted(inner))
    at = anchors[0].end() + 1
    return (
        central[:at]
        + "    # Nested Go modules, not this module's code (scripts/gremlins-scope.py)\n"
        + entries
        + central[at:]
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--config', required=True, type=Path, help='central gremlins config')
    p.add_argument('--target', required=True, type=Path, help='checkout holding the modules')
    p.add_argument('--exclude-dir', action='append', default=[], metavar='DIR',
                   help='nested module dir to exclude from its outer modules but not measure')
    p.add_argument('modules', nargs='+', help="module dirs relative to --target ('.' for the root)")
    args = p.parse_args()

    candidates = args.modules + args.exclude_dir
    for d in candidates:
        if not SAFE_DIR.fullmatch(d):
            raise SystemExit(
                f'gremlins-scope: module dir {d!r} has characters outside [A-Za-z0-9._/-]'
            )

    central = args.config.read_text()
    for d in args.modules:
        inner = gremlins_merge.deeper_dirs(d, candidates)
        (args.target / d / '.gremlins.yaml').write_text(scoped_config(central, d, candidates))
        print(f'[{d}] .gremlins.yaml excludes nested modules: {inner or "none"}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
