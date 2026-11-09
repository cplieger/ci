#!/usr/bin/env python3
"""Size each repo's weekly gremlins work and split it across jobs when one job cannot hold it.

A module projects its setup plus, per package, RUNNABLE mutants x one mutant's
cost / workers; an integration module projects suite x mutants / workers, as
gremlins-run.sh's select_mode does. A mutant's cost is the larger of the wall and
CPU time of rebuilding and testing its package after a code change, which
gremlins-run.sh measures. The package's coverage-pass time undercounts it by the
rebuild and by every core its tests use, which the workers share. Over
--target-minutes, packages go to the fewest shards that fit, longest first. A
package is never split, so a mutant's verdict never depends on its shard.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MODES = ('integration', 'per-package')
SHA = re.compile(r'[0-9a-f]{40}')
# gremlins-scope.py turns each package dir into an exclude-files regexp and
# escapes only '.', so a dir outside this set cannot be scoped to a shard.
SAFE_DIR = re.compile(r'[A-Za-z0-9._/-]+')


@dataclass(frozen=True)
class Item:
    """One unit a shard runs whole: a package, or a module with no package to split."""

    module: str
    package: str | None
    work: float  # wall seconds on the module's workers


class UnsizedError(Exception):
    """A module whose measurements are missing or unusable, so it runs whole and unmeasured."""


@dataclass
class Bin:
    load: float = 0.0
    modules: set[str] = field(default_factory=set)
    items: list[Item] = field(default_factory=list)


def pack(items: list[Item], k: int, setup: dict[str, float]) -> list[Bin]:
    """Greedy longest-first packing into k shards.

    Each item goes to the shard whose projected time it raises least, counting
    its module's setup only where that module is not yet present; ties go to a
    shard that already runs the module, then to the lower index. With one module
    this is the classic longest-processing-time rule.
    """
    bins = [Bin() for _ in range(k)]
    for item in sorted(items, key=lambda it: (-it.work, it.module, it.package or '')):
        def cost(b: Bin, it: Item = item) -> float:
            return b.load + it.work + (0.0 if it.module in b.modules else setup[it.module])

        best = min(range(k), key=lambda i: (cost(bins[i]), item.module not in bins[i].modules, i))
        b = bins[best]
        b.load = cost(b)
        b.modules.add(item.module)
        b.items.append(item)
    return bins


def choose(items: list[Item], setup: dict[str, float], target: float) -> tuple[int, list[Bin]]:
    """The fewest shards whose slowest one fits target seconds.

    When no k fits, because one package is over the target or because no
    packing absorbs the per-shard setup, return the fewest shards that reach
    the lowest slowest shard.
    """
    limit = max(1, sum(1 for it in items if it.work > 0))
    best: tuple[float, int, list[Bin]] | None = None
    for k in range(1, limit + 1):
        bins = pack(items, k, setup)
        worst = max(b.load for b in bins)
        if worst <= target:
            return k, bins
        if best is None or worst < best[0]:
            best = (worst, k, bins)
    return best[1], best[2]


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            out[key] = value
    return out


def number(env: dict[str, str], key: str, *, required: bool = True) -> float | None:
    raw = env.get(key, '')
    if raw == '' and not required:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise UnsizedError(f'{key}={raw!r} is not a number') from None
    if value < 0:
        raise UnsizedError(f'{key}={raw!r} is negative')
    return value


def mutant_times(text: str) -> dict[str, float]:
    """Seconds per import path from time_package_mutants' `<path>\t<ms>` lines."""
    out = {}
    for line in text.splitlines():
        path, _, ms = line.partition('\t')
        if path and ms.isdigit():
            out[path] = int(ms) / 1000
    return out


def package_counts(dry: dict) -> dict[str, list[int]]:
    """[RUNNABLE, other] mutant counts per module-relative package dir of a dry run."""
    out: dict[str, list[int]] = {}
    for fe in dry.get('files') or []:
        name = fe.get('file_name') or ''
        pkg = name.rsplit('/', 1)[0] if '/' in name else '.'
        counts = out.setdefault(pkg, [0, 0])
        for m in fe.get('mutations') or []:
            counts[0 if (m.get('status') or '').upper() == 'RUNNABLE' else 1] += 1
    return out


def size_module(dir_: str, plan_dir: Path) -> tuple[dict, list[Item]]:
    """One module's plan entry (without shards yet) and the items it packs into.

    Raises UnsizedError when gremlins-run.sh did not finish the module's plan or left
    files the planner cannot use.
    """
    env_path = plan_dir / 'plan.env'
    env = read_env(env_path) if env_path.is_file() else {}
    if env.get('sized') != '1':
        raise UnsizedError('its sizing did not finish')
    mode = env.get('mode', '')
    if mode not in MODES:
        raise UnsizedError(f'mode={mode!r} is neither of {MODES}')
    workers = int(number(env, 'workers') or 0)
    if workers < 1:
        raise UnsizedError('workers must be at least 1')
    setup = number(env, 'setup_secs')
    if mode == 'integration':
        suite = number(env, 'suite_secs')
        mutants = number(env, 'mutants')
        work = suite * mutants / workers
        entry = {'mode': mode, 'workers': workers, 'setup_secs': setup + suite,
                 'work_secs': round(work, 1), 'packages': {}}
        return entry, [Item(dir_, None, work)]

    # The run's own coverage pass comes after its setup and takes about a suite.
    suite = number(env, 'suite_secs', required=False) or 0.0
    entry = {'mode': mode, 'workers': workers, 'setup_secs': setup + suite, 'packages': {}}
    dry_path = plan_dir / 'dry-run.json'
    if not dry_path.is_file() or dry_path.stat().st_size == 0:
        # A finished plan with no report is a module with no mutants
        # (gremlins-merge.py load_module).
        return entry, [Item(dir_, None, 0.0)]
    try:
        dry = json.loads(dry_path.read_text())
        times = mutant_times((plan_dir / 'mutant-ms.tsv').read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise UnsizedError(str(exc)) from None
    module = dry.get('go_module') if isinstance(dry, dict) else None
    if not module:
        raise UnsizedError('dry-run.json names no go_module')
    items = []
    for pkg, (runnable, other) in sorted(package_counts(dry).items()):
        if not SAFE_DIR.fullmatch(pkg):
            raise UnsizedError(f'package dir {pkg!r} has characters outside [A-Za-z0-9._/-]')
        path = module if pkg == '.' else f'{module}/{pkg}'
        # A package with no row (its tests failed, or the plan reached its
        # deadline first) costs as much as the dearest one.
        secs = times.get(path, max(times.values(), default=None))
        if secs is None and runnable:
            raise UnsizedError(f'no package has a mutant time, so {pkg} cannot be sized')
        work = runnable * (secs or 0.0) / workers
        entry['packages'][pkg] = {'runnable': runnable, 'not_covered': other, 'mutant_secs': secs,
                                  'work_secs': round(work, 1)}
        items.append(Item(dir_, pkg, work))
    return entry, items or [Item(dir_, None, 0.0)]


def plan(repo: str, sha: str, target_minutes: float,
         modules: list[tuple[str, Path]]) -> tuple[dict, list[str]]:
    """The repo's plan and the modules it could not size."""
    entries, items, unsized = {}, [], []
    for dir_, plan_dir in modules:
        try:
            entry, its = size_module(dir_, plan_dir)
        except UnsizedError as exc:
            print(f'::error::{repo}: module {dir_} could not be sized ({exc}, in {plan_dir}), '
                  'so it runs whole and unmeasured, beside no other module when the repo shards',
                  file=sys.stderr)
            entries[dir_] = {'mode': None, 'workers': None, 'setup_secs': None, 'packages': {}}
            unsized.append(dir_)
            continue
        entries[dir_] = entry
        items += its
    setup = {d: e['setup_secs'] for d, e in entries.items() if d not in unsized}
    one_job = sum(setup.values()) + sum(it.work for it in items)
    k, bins = choose(items, setup, target_minutes * 60) if one_job > target_minutes * 60 else (
        1, pack(items, 1, setup))
    projected = [round(b.load / 60, 1) for b in bins]
    if max(projected) > target_minutes:
        slowest = max(items, key=lambda it: it.work)
        print(f'::warning::{repo}: {slowest.module}:{slowest.package or "(module)"} alone projects '
              f'{slowest.work / 60:.0f} min, so its shard stays over the {target_minutes:g}-min target',
              file=sys.stderr)
    # An unsized module's cost is unknown, so a shard it shares could pass the
    # cap. In a repo that fits one job it joins that job, as with no plan at all.
    if k > 1:
        bins += [Bin(items=[Item(d, None, 0.0)]) for d in unsized]
        projected += [None] * len(unsized)
    else:
        bins[0].items += [Item(d, None, 0.0) for d in unsized]
    for e in entries.values():
        e['shards'] = []
    for n, b in enumerate(bins, start=1):
        for it in b.items:
            e = entries[it.module]
            if it.package is not None:
                e['packages'][it.package]['shard'] = n
            if n not in e['shards']:
                e['shards'].append(n)
    for e in entries.values():
        e['shards'].sort()
    doc = {'repo': repo, 'sha': sha, 'target_minutes': target_minutes, 'shards': len(bins),
           'one_job_minutes': round(one_job / 60, 1), 'projected_minutes': projected,
           'modules': entries}
    return doc, unsized


def valid_plan(doc: object) -> str:
    """Why a plan cannot be trusted to build the matrix, or '' when it can."""
    if not isinstance(doc, dict):
        return 'not a JSON object'
    if not isinstance(doc.get('sha'), str) or not SHA.fullmatch(doc['sha']):
        return f'sha {doc.get("sha")!r} is not a 40-hex commit'
    k = doc.get('shards')
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        return f'shards {k!r} is not a positive integer'
    modules = doc.get('modules')
    if not isinstance(modules, dict) or not modules:
        return 'no modules'
    for dir_, e in modules.items():
        used = e.get('shards') if isinstance(e, dict) else None
        if not isinstance(used, list) or not used or any(
                not isinstance(n, int) or not 1 <= n <= k for n in used):
            return f'module {dir_!r} has shards {used!r}, outside 1..{k}'
        # Neither is set for a module the plan could not size, which measures its own.
        mode, workers = e.get('mode'), e.get('workers')
        if (mode, workers) != (None, None):
            if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
                return f'module {dir_!r} has workers {workers!r}, not a positive integer'
            if mode not in MODES:
                return f'module {dir_!r} has mode {mode!r}, neither of {MODES}'
        # Each shard of a module mutates only the packages given to it, so the
        # packages' shards must be exactly the module's: one more drops a
        # package, one fewer runs a shard with nothing to mutate. A module with
        # no package list runs whole, so only once.
        packages = e.get('packages')
        if not isinstance(packages, dict) or not all(isinstance(p, dict) for p in packages.values()):
            return f'module {dir_!r} has no package map'
        if bad := next((p for p in packages if not SAFE_DIR.fullmatch(p)), None):
            return f'module {dir_!r} has package dir {bad!r} outside [A-Za-z0-9._/-]'
        if mode is None and packages:
            return f'module {dir_!r} has no mode but splits its packages'
        given = {p.get('shard') for p in packages.values()} if packages else None
        if (given is None and len(used) != 1) or (given is not None and given != set(used)):
            return f'module {dir_!r} runs in shards {used!r} but its packages in {given!r}'
    covered = {n for e in modules.values() for n in e['shards']}
    if covered != set(range(1, k + 1)):
        return f'shards {sorted(set(range(1, k + 1)) - covered)} run no module'
    return ''


def matrix(repos: list[str], plans_dir: Path, attempts: int) -> list[dict]:
    plans: dict[str, dict] = {}
    for path in sorted(plans_dir.rglob('plan.json')) if plans_dir.is_dir() else []:
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f'::warning::{path} is unreadable, so it is ignored ({exc})', file=sys.stderr)
            continue
        repo = doc.get('repo') if isinstance(doc, dict) else None
        if isinstance(repo, str):
            plans[repo] = doc
    out = []
    for repo in repos:
        doc = plans.get(repo)
        k = 1
        if doc is None:
            print(f'::warning::{repo}: no shard plan, so each attempt runs in one job', file=sys.stderr)
        elif why := valid_plan(doc):
            print(f'::warning::{repo}: shard plan refused ({why}), so each attempt runs in one job',
                  file=sys.stderr)
        else:
            k = doc['shards']
            print(f'{repo}: {k} shard(s), projected {doc.get("projected_minutes")} min '
                  f'(one job: {doc.get("one_job_minutes")} min)', file=sys.stderr)
        for attempt in range(1, attempts + 1):
            if k == 1:
                out.append({'repo': repo, 'attempt': attempt})
            else:
                out += [{'repo': repo, 'attempt': attempt, 'shard': n, 'shards': k}
                        for n in range(1, k + 1)]
    return out


def shard_modules(doc: dict, shard: int, modules: list[str]) -> list[str]:
    if why := valid_plan(doc):
        raise SystemExit(f'gremlins-shard: the plan cannot be used ({why})')
    if not 1 <= shard <= doc['shards']:
        raise SystemExit(f'gremlins-shard: shard {shard} is outside the plan\'s 1..{doc["shards"]}')
    planned = doc['modules']
    if sorted(planned) != sorted(modules):
        raise SystemExit(
            f'gremlins-shard: this checkout has modules {sorted(modules)} but the plan sized '
            f'{sorted(planned)}'
        )
    return [' '.join(str(v) for v in ('run', d, planned[d]['mode'], planned[d]['workers'])
                     if v is not None)
            if shard in planned[d]['shards'] else f'skip {d}' for d in modules]


def parse_module_arg(raw: str) -> tuple[str, Path]:
    dir_, sep, path = raw.partition('=')
    if not sep or not path:
        raise SystemExit(f'--module expects DIR=PLAN_DIR, got {raw!r}')
    return dir_.rstrip('/') or '.', Path(path)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    pl = sub.add_parser('plan', help="size one repo's modules and pick its shards")
    pl.add_argument('--repo', required=True)
    pl.add_argument('--sha', required=True)
    pl.add_argument('--target-minutes', required=True, type=float)
    pl.add_argument('--module', action='append', required=True, metavar='DIR=PLAN_DIR')
    pl.add_argument('--out', required=True, type=Path)
    mx = sub.add_parser('matrix', help="the run job's matrix from every repo's plan")
    mx.add_argument('--repos', required=True, help='JSON list of repo names')
    mx.add_argument('--plans', required=True, type=Path, help='downloaded plan artifacts')
    mx.add_argument('--attempts', required=True, type=int)
    md = sub.add_parser('modules', help='which modules one shard runs')
    md.add_argument('--plan', required=True, type=Path)
    md.add_argument('--shard', required=True, type=int)
    md.add_argument('--modules', required=True, help='space-separated module dirs of this checkout')
    args = p.parse_args()

    if args.command == 'plan':
        if not SHA.fullmatch(args.sha):
            raise SystemExit(f'--sha {args.sha!r} is not a 40-hex commit')
        doc, unsized = plan(args.repo, args.sha, args.target_minutes,
                            [parse_module_arg(m) for m in args.module])
        args.out.write_text(json.dumps(doc, indent=1) + '\n')
        print(f'[{args.repo}] one job {doc["one_job_minutes"]} min -> {doc["shards"]} shard(s) '
              f'{doc["projected_minutes"]} min', file=sys.stderr)
        if unsized:
            return 1
    elif args.command == 'matrix':
        repos = json.loads(args.repos)
        if not isinstance(repos, list) or not all(isinstance(r, str) and r for r in repos):
            raise SystemExit(f'--repos must be a JSON list of names, got {args.repos!r}')
        print(json.dumps(matrix(repos, args.plans, args.attempts), separators=(',', ':')))
    else:
        doc = json.loads(args.plan.read_text())
        print('\n'.join(shard_modules(doc, args.shard, args.modules.split())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
