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
import json
import re
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'gremlins_merge', Path(__file__).with_name('gremlins-merge.py')
)
gremlins_merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gremlins_merge)
_spec = importlib.util.spec_from_file_location(
    'gremlins_shard', Path(__file__).with_name('gremlins-shard.py')
)
gremlins_shard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gremlins_shard)

# A measured module's name: gremlins-prepare.sh refuses any other.
SAFE_DIR = re.compile(r'[A-Za-z0-9._/-]+')
ANCHOR = re.compile(r'^  exclude-files:[ \t]*$', re.MULTILINE)


def escaped(d: str) -> str:
    """D as a Go regexp literal inside a single-quoted YAML entry: an excluded dir can hold any character."""
    out = []
    for c in d:
        if c.isascii() and (c.isalnum() or c in '_/-'):
            out.append(c)
        elif c == '.':
            out.append(r'\.')
        elif '\udc80' <= c <= '\udcff':
            # A byte that is not UTF-8 (surrogateescape); Go's regexp reads it as U+FFFD.
            out.append(r'\x{fffd}')
        else:
            out.append(f'\\x{{{ord(c):x}}}')
    return ''.join(out)


def scoped_config(
    central: str, dir_: str, all_dirs: list[str], other_shards: list[str] = ()
) -> str:
    """The central config with dir_'s deeper module directories excluded.

    other_shards: files of dir_ that another shard mutates.
    """
    inner = gremlins_merge.deeper_dirs(dir_, all_dirs)
    if not inner and not other_shards:
        return central
    anchors = list(ANCHOR.finditer(central))
    if len(anchors) != 1:
        raise SystemExit(
            f'gremlins-scope: expected one "  exclude-files:" line in the central config, '
            f'found {len(anchors)}'
        )
    entries = ''
    if inner:
        entries += "    # Nested Go modules, not this module's code (scripts/gremlins-scope.py)\n"
        entries += ''.join(f"    - '^{escaped(d)}/'\n" for d in sorted(inner))
    if other_shards:
        entries += '    # Files another shard of this attempt mutates (scripts/gremlins-shard.py)\n'
        entries += ''.join(f"    - '^{escaped(f)}$'\n" for f in sorted(other_shards))
    at = anchors[0].end() + 1
    return central[:at] + entries + central[at:]


def other_shard_files(plan: dict, shard: int, dir_: str) -> list[str]:
    """The files of module dir_ that the plan gives a shard other than shard."""
    module = plan['modules'].get(dir_)
    if module is None:
        raise SystemExit(f'gremlins-scope: the shard plan has no module {dir_!r}')
    return [f for f, e in (module.get('files') or {}).items() if e.get('shard') != shard]


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--config', required=True, type=Path, help='central gremlins config')
    p.add_argument('--target', required=True, type=Path, help='checkout holding the modules')
    p.add_argument(
        '--exclude-dir',
        action='append',
        default=[],
        metavar='DIR',
        help='nested module dir to exclude from its outer modules but not measure',
    )
    p.add_argument('--plan', type=Path, help="scripts/gremlins-shard.py's plan for this repo")
    p.add_argument('--shard', type=int, help='the shard of --plan this run is')
    p.add_argument('modules', nargs='+', help="module dirs relative to --target ('.' for the root)")
    args = p.parse_args()
    if (args.plan is None) != (args.shard is None):
        raise SystemExit('gremlins-scope: --plan and --shard go together')
    plan = None
    if args.plan is not None:
        plan = json.loads(args.plan.read_text())
        if why := gremlins_shard.valid_plan(plan):
            raise SystemExit(f'gremlins-scope: the shard plan cannot be used ({why})')
        if not 1 <= args.shard <= plan['shards']:
            raise SystemExit(f'gremlins-scope: shard {args.shard} is outside 1..{plan["shards"]}')

    candidates = args.modules + args.exclude_dir
    for d in args.modules:
        if not SAFE_DIR.fullmatch(d):
            raise SystemExit(
                f'gremlins-scope: module dir {d!r} has characters outside [A-Za-z0-9._/-]'
            )

    central = args.config.read_text()
    for d in args.modules:
        inner = gremlins_merge.deeper_dirs(d, candidates)
        others = other_shard_files(plan, args.shard, d) if plan else []
        (args.target / d / '.gremlins.yaml').write_text(
            scoped_config(central, d, candidates, others)
        )
        print(f'[{d}] .gremlins.yaml excludes nested modules: {inner or "none"}', file=sys.stderr)
        if plan:
            print(
                f'[{d}] shard {args.shard} of {plan["shards"]} also excludes '
                f'{len(others)} file(s) other shards mutate',
                file=sys.stderr,
            )
    return 0


if __name__ == '__main__':
    sys.exit(main())
