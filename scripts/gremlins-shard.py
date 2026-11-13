#!/usr/bin/env python3
"""Size each repo's weekly gremlins work and split it across jobs when one job cannot hold it.

The plan job sizes each module (`plan`): per file of a per-package module its
RUNNABLE mutants and a prior cost, RUNNABLE x the package's measured cost of one
surviving mutant (gremlins-run.sh time_package_mutants); an integration module,
suite x mutants, with each module's git blob per Go file (`blobs`). The shard
job prices each file from the repo's last measured run (`history`, built from
each shard's `costs`) where it measured the file's current blob, the prior times
PRIOR_ERROR where not (scripts/mutation_cost.py price),
and packs files into shards that each end inside the band mutation_cost.TOP_SECS
derives from runner speed, longest first (`matrix`, mutation_cost.choose). A file is the smallest unit gremlins can scope, and a mutant's
verdict does not depend on its shard: each mutant runs its own package's tests
under that package's timeout.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import mutation_cost
from mutation_cost import Bin, Item

if TYPE_CHECKING:
    from string.templatelib import Template

MODES = ('integration', 'per-package')
SHA = re.compile(r'[0-9a-f]{40}')
# gremlins-scope.py turns each file into an exclude-files regexp and escapes
# only '.', so a path outside this set cannot be scoped to a shard.
SAFE_PATH = re.compile(r'[A-Za-z0-9._/-]+')
# The run job's steps outside gremlins-run.sh (checkout, Python, clone, image
# pull, merge, upload) took 90 s to 2 min per shard in run 42584241747.
JOB_OVERHEAD_SECS = 120
# gremlins prints one of these per mutant it ran, as it finishes; NOT COVERED
# lines cost nothing.
COMPLETION = re.compile(r'^\s*(?:KILLED|LIVED|TIMED OUT|NOT VIABLE)\s+\S+ at (\S+?):\d+:\d+\s*$')
MUTATION_START = 'Gathering coverage... done'


class UnsizedError(Exception):
    """A module whose measurements are missing or unusable, so it runs whole and unmeasured."""


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
        raise UnsizedError(f'{key}={raw!r:.200} is not a number') from None
    if not math.isfinite(value) or value < 0:
        raise UnsizedError(f'{key}={raw!r:.200} is not a finite number at or above 0')
    return value


def mutant_times(text: str) -> dict[str, float]:
    """Seconds per import path from time_package_mutants' `<path>\t<ms>` lines."""
    out = {}
    for line in text.splitlines():
        path, _, ms = line.partition('\t')
        if path and ms.isdigit():
            out[path] = int(ms) / 1000
    return out


def file_counts(dry: dict) -> dict[str, int]:
    """RUNNABLE mutants per module-relative file of a dry run, 0 for a file with none."""
    out: dict[str, int] = {}
    for fe in dry.get('files') or []:
        name = fe.get('file_name') or ''
        out[name] = out.get(name, 0) + sum(
            1 for m in fe.get('mutations') or [] if (m.get('status') or '').upper() == 'RUNNABLE'
        )
    return out


def package_of(path: str) -> str:
    return path.rsplit('/', 1)[0] if '/' in path else '.'


def size_module(plan_dir: Path) -> dict:
    """One module's sizing entry.

    Raises UnsizedError when gremlins-run.sh did not finish the module's plan or left
    files the planner cannot use.
    """
    env_path = plan_dir / 'plan.env'
    env = read_env(env_path) if env_path.is_file() else {}
    if env.get('sized') != '1':
        raise UnsizedError('its sizing did not finish')
    mode = env.get('mode', '')
    if mode not in MODES:
        raise UnsizedError(f'mode={mode!r:.200} is neither of {MODES}')
    workers = int(number(env, 'workers') or 0)
    if workers < 1:
        raise UnsizedError('workers must be at least 1')
    setup = number(env, 'setup_secs')
    if mode == 'integration':
        suite = number(env, 'suite_secs')
        mutants = number(env, 'mutants')
        per_mutant = mutation_cost.known(suite, mutation_cost.SECOND_TICK)
        return {
            'mode': mode,
            'workers': workers,
            'setup_secs': setup + suite,
            'mutants': int(mutants),
            'prior_secs': None if per_mutant is None else round(per_mutant * mutants, 1),
            'packages': {},
            'files': {},
        }

    # The run's own coverage pass comes after its setup and takes about a suite.
    suite = number(env, 'suite_secs', required=False) or 0.0
    entry = {
        'mode': mode,
        'workers': workers,
        'setup_secs': setup + suite,
        'packages': {},
        'files': {},
    }
    dry_path = plan_dir / 'dry-run.json'
    if not dry_path.is_file() or dry_path.stat().st_size == 0:
        # A finished plan with no report is a module with no mutants
        # (gremlins-merge.py load_module).
        return entry
    try:
        dry = mutation_cost.load_json(dry_path)
        times = mutant_times((plan_dir / 'mutant-ms.tsv').read_text(errors='replace'))
    except Exception as exc:
        raise UnsizedError(mutation_cost.describe(exc)) from None
    module = dry.get('go_module') if isinstance(dry, dict) else None
    if not module:
        raise UnsizedError('dry-run.json names no go_module')
    for path, runnable in sorted(file_counts(dry).items()):
        if not SAFE_PATH.fullmatch(path):
            raise UnsizedError(f'file {path!r:.200} has characters outside [A-Za-z0-9._/-]')
        pkg = package_of(path)
        if pkg not in entry['packages']:
            # A package with no row (its tests failed, or the plan reached its
            # deadline first) costs as much as the dearest one.
            secs = times.get(
                module if pkg == '.' else f'{module}/{pkg}', max(times.values(), default=None)
            )
            entry['packages'][pkg] = {'mutant_secs': secs}
        p = entry['packages'][pkg]
        if p['mutant_secs'] is None and runnable:
            raise UnsizedError(f'no package has a mutant time, so {pkg} cannot be sized')
        secs = mutation_cost.known(p['mutant_secs'] or 0.0, mutation_cost.MILLISECOND_TICK)
        entry['files'][path] = {
            'runnable': runnable,
            'prior_secs': None if runnable and secs is None else round(runnable * (secs or 0.0), 3),
        }
    return entry


def blobs(target: Path, dir_: str) -> dict[str, str]:
    """Each Go source file of module DIR_ at TARGET's HEAD, module-relative, with its git blob:
    the version a file's measured cost belongs to. {} when git cannot list it."""
    proc = subprocess.run(
        ['git', '-C', str(target), 'ls-tree', '-r', '-z', 'HEAD', '--', dir_],
        capture_output=True,
        check=False,
    )
    prefix = '' if dir_ == '.' else f'{dir_}/'
    out = {}
    for row in proc.stdout.split(b'\0') if proc.returncode == 0 else []:
        meta, _, raw = row.partition(b'\t')
        fields, name = meta.split(), raw.decode(errors='replace')
        if (
            len(fields) == 3
            and fields[1] == b'blob'
            and name.startswith(prefix)
            and name.endswith('.go')
            and not name.endswith('_test.go')
            and mutation_cost.GIT_BLOB.fullmatch(fields[2].decode())
        ):
            out[name.removeprefix(prefix)] = fields[2].decode()
    return dict(sorted(out.items()))


def sizing(
    repo: str, sha: str, modules: list[tuple[str, Path]], target: Path | None = None
) -> tuple[dict, list[str]]:
    """The repo's sizing and the modules it could not size; with TARGET, the clone, each sized
    module's blobs, limited to its files when it lists them."""
    entries, unsized = {}, []
    for dir_, plan_dir in modules:
        try:
            entries[dir_] = size_module(plan_dir)
            if target is not None:
                found = blobs(target, dir_)
                files = entries[dir_]['files']
                entries[dir_]['blobs'] = {p: b for p, b in found.items() if not files or p in files}
        except Exception as exc:
            why = str(exc) if isinstance(exc, UnsizedError) else mutation_cost.describe(exc)
            mutation_cost.annotate(
                'error',
                t'{repo}: module {dir_} could not be sized ({why}, in {str(plan_dir)}), so it runs '
                t'whole and unmeasured, priced as a full shard (gremlins-shard.py with_unsized)',
            )
            entries[dir_] = {
                'mode': None,
                'workers': None,
                'setup_secs': None,
                'packages': {},
                'files': {},
            }
            unsized.append(dir_)
    return {'repo': repo, 'sha': sha, 'modules': entries}, unsized


def module_history(history: dict | None, dir_: str) -> dict:
    modules = history.get('modules') if isinstance(history, dict) else None
    h = modules.get(dir_) if isinstance(modules, dict) else None
    return h if isinstance(h, dict) else {}


def integration_price(e: dict, h: dict) -> tuple[float | None, str]:
    """An integration module's price (mutation_cost.price) from its history rows at their current
    blobs, summed into one; a module with no mutants costs 0 whatever its history."""
    if not e['mutants']:
        return 0.0, 'prior'
    hist = mutation_cost.clean_files(h.get('files'))
    done = sum(f['mutants'] for f in hist.values())
    total = {'secs': sum(f['secs'] for f in hist.values()), 'mutants': done}
    return mutation_cost.price(e['mutants'], e['prior_secs'], total if done else None)


def with_unsized(
    items: list[Item], setup: dict[str, float], unsized: int, top: float, limit: int
) -> tuple[int, int, list[Bin]]:
    """The shards UNSIZED items get, and the sized ITEMS' shard count and packing in the rest of LIMIT.

    An unsized item (a module the plan could not size, or a module or file whose
    probe measured nothing and has no history) is of unknown cost, so it is
    priced as a whole shard (TOP, the band's top) and gets one of its own
    while LIMIT leaves the sized work one. Past that, the split whose longest
    shard projects least, ties going to more unsized shards; at a LIMIT of 1
    everything is one job.
    """
    if not unsized or not items:
        return min(unsized, limit), *(
            mutation_cost.choose(items, setup, top, limit) if items else (0, [])
        )
    if limit == 1:
        return 0, 1, mutation_cost.pack(items, 1, setup)
    best: tuple[float, int, int, list[Bin]] | None = None
    for own in range(min(unsized, limit - 1), 0, -1):
        k, bins = mutation_cost.choose(items, setup, top, limit - own)
        longest = max(*(b.load for b in bins), -(-unsized // own) * top)
        if best is None or longest < best[0]:
            best = (longest, own, k, bins)
    return best[1:]


def assign(
    doc: dict, history: dict | None, limit: int = mutation_cost.MAX_SHARDS
) -> tuple[dict, list[Template]]:
    """The run plan, doc's files priced from HISTORY and packed into at most LIMIT shards inside the
    band (mutation_cost.TOP_SECS), and its warnings."""
    plan = json.loads(json.dumps(doc))
    entries, items, setup, refused = plan['modules'], [], {}, 0
    unsized: list[Item] = []
    unknown: list[str] = []
    for dir_, e in entries.items():
        e.pop('packages', None)
        if e['mode'] is None:
            unsized.append(Item(dir_, None, 0.0))
            continue
        h = mutation_cost.at_blobs(module_history(history, dir_), e.get('blobs') or {})
        setup[dir_] = mutation_cost.setup_price(h.get('setup_secs'), e['setup_secs'])
        workers = e['workers']
        if e['mode'] == 'integration':
            secs, e['cost'] = integration_price(e, h)
            refused += e['cost'] == 'refused'
            e['work_secs'] = None if secs is None else round(secs / workers, 1)
            if secs is None:
                unsized.append(Item(dir_, None, 0.0))
                unknown.append(dir_)
            else:
                items.append(Item(dir_, None, secs / workers))
            continue
        priced = mutation_cost.project(
            {p: (f['runnable'], f['prior_secs']) for p, f in e['files'].items()}, h
        )
        refused += mutation_cost.refusals(priced)
        for p, (secs, source) in priced.items():
            e['files'][p].update(
                work_secs=None if secs is None else round(secs / workers, 1), cost=source
            )
            if secs is None:
                unsized.append(Item(dir_, p, 0.0))
                unknown.append(f'{dir_}:{p}')
            else:
                items.append(Item(dir_, p, secs / workers))
        if not e['files']:
            items.append(Item(dir_, None, 0.0))
    warnings: list[Template] = []
    if unknown:
        warnings.append(
            t'{plan["repo"]}: no measured or probed cost for {len(unknown)} item(s), so each is '
            t'planned as a full shard of its own: {", ".join(unknown):.1000}'
        )
    if refused:
        warnings.append(
            t'{plan["repo"]}: {refused} measured price(s) more than {mutation_cost.PRICE_FACTOR}x '
            t"from the probe's were refused, so those files are priced from it"
        )
    one_job = sum(setup.values()) + sum(it.work for it in items)
    top_secs = mutation_cost.TOP_SECS
    own, k, bins = with_unsized(items, setup, len(unsized), top_secs, limit)
    room = limit - own
    projected: list[float | None] = [round(b.load / 60, 1) for b in bins]
    top = round(top_secs / 60, 1)
    if items and mutation_cost.capped(k, bins, items, setup, top_secs, room):
        plan['capped'] = mutation_cost.cap_reason(limit)
    elif items and max(projected) > top:
        slowest = max(items, key=lambda it: it.work)
        warnings.append(
            t'{plan["repo"]}: {slowest.group}:{slowest.unit or "(module)"} alone '
            t'projects {slowest.work / 60:.0f} min, so its shard ends past {top:.1f} min, the top of '
            t'its band'
        )
    if own:
        extra = [Bin() for _ in range(own)]
        for i, it in enumerate(unsized):
            extra[i % own].items.append(it)
        bins += extra
        projected += [None] * own
    elif unsized:
        bins[0].items += unsized
    marked = {id(it) for it in unsized}
    if shared := sorted(
        {
            it.group if it.unit is None else f'{it.group}:{it.unit}'
            for b in bins
            if len(b.items) > 1
            for it in b.items
            if id(it) in marked
        }
    ):
        plan['capped'] = mutation_cost.cap_reason(limit)
        plan['unsized_shared'] = shared
    for e in entries.values():
        e['shards'] = []
    for n, b in enumerate(bins, start=1):
        for it in b.items:
            e = entries[it.group]
            if it.unit is not None:
                e['files'][it.unit]['shard'] = n
            if n not in e['shards']:
                e['shards'].append(n)
    for e in entries.values():
        e['shards'].sort()
    plan.update(
        top_minutes=top,
        shards=len(bins),
        one_job_minutes=round(one_job / 60, 1),
        projected_minutes=projected,
    )
    if note := mutation_cost.cap_note(plan['repo'], plan):
        warnings.append(note)
    return plan, warnings


def priced_or_unknown(entry: dict) -> bool:
    """ENTRY holds a prior: a measure, or None for a probe that measured nothing."""
    return 'prior_secs' in entry and (
        entry['prior_secs'] is None or mutation_cost.nonnegative(entry['prior_secs'])
    )


def valid_module(dir_: str, e: object, *, planned: int | None) -> str:
    """Why module entry E cannot be trusted, or ''; PLANNED is the plan's shard count, None for a sizing."""
    if not isinstance(dir_, str) or (
        dir_ != '.'
        and not all(
            mutation_cost.DIR_PART.fullmatch(p) and p not in ('.', '..') for p in dir_.split('/')
        )
    ):
        return (
            f"module dir {dir_!r:.200} is neither '.' nor a relative path of [A-Za-z0-9._-] names"
        )
    if not isinstance(e, dict):
        return f'module {dir_!r:.200} is not an object'
    # Neither is set for a module the plan could not size, which measures its own.
    mode, workers = e.get('mode'), e.get('workers')
    if (mode, workers) != (None, None):
        if not mutation_cost.count(workers) or workers < 1:
            return f'module {dir_!r:.200} has workers {workers!r:.200}, not a positive integer'
        if mode not in MODES:
            return f'module {dir_!r:.200} has mode {mode!r:.200}, neither of {MODES}'
        if not mutation_cost.nonnegative(e.get('setup_secs')):
            return f'module {dir_!r:.200} has setup_secs {e.get("setup_secs")!r:.200}'
    if mode == 'integration' and (
        not mutation_cost.count(e.get('mutants')) or not priced_or_unknown(e)
    ):
        return (
            f'module {dir_!r:.200} is an integration module with no usable mutants and prior_secs'
        )
    packages = e.get('packages', {})
    if not isinstance(packages, dict) or not all(
        isinstance(p, dict)
        and set(p) == {'mutant_secs'}
        and (p['mutant_secs'] is None or mutation_cost.nonnegative(p['mutant_secs']))
        for p in packages.values()
    ):
        return f'module {dir_!r:.200} has no usable package map'
    versions = e.get('blobs', {})
    if not isinstance(versions, dict) or not all(
        isinstance(p, str) and isinstance(b, str) and mutation_cost.GIT_BLOB.fullmatch(b)
        for p, b in versions.items()
    ):
        return f'module {dir_!r:.200} has blobs that are not all git object ids'
    files = e.get('files')
    if not isinstance(files, dict) or not all(isinstance(f, dict) for f in files.values()):
        return f'module {dir_!r:.200} has no file map'
    if bad := next(
        (p for p in files if not isinstance(p, str) or not SAFE_PATH.fullmatch(p)), None
    ):
        return f'module {dir_!r:.200} has file {bad!r:.200} outside [A-Za-z0-9._/-]'
    if mode != 'per-package' and files:
        return f'module {dir_!r:.200} is not per-package but splits its files'
    for p, f in files.items():
        if not mutation_cost.count(f.get('runnable')) or not priced_or_unknown(f):
            return (
                f'module {dir_!r:.200} file {p!r:.200} has no usable runnable count and prior_secs'
            )
    if planned is None:
        return ''
    # Each shard of a module mutates only the files given to it, so the files'
    # shards must be exactly the module's: one more drops a file, one fewer runs
    # a shard with nothing to mutate. A module with no file list runs whole, so
    # only once.
    used = e.get('shards')
    if (
        not isinstance(used, list)
        or not used
        or any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= planned for n in used)
    ):
        return f'module {dir_!r:.200} has shards {used!r:.200}, outside 1..{planned}'
    given = {f.get('shard') for f in files.values()} if files else None
    if (given is None and len(used) != 1) or (given is not None and given != set(used)):
        return f'module {dir_!r:.200} runs in shards {used!r:.200} but its files in {given!r:.200}'
    return ''


def valid_sizing(doc: object) -> str:
    """Why a plan job's sizing cannot be trusted to plan from, or '' when it can."""
    if not isinstance(doc, dict):
        return 'not a JSON object'
    if not isinstance(doc.get('sha'), str) or not SHA.fullmatch(doc['sha']):
        return f'sha {doc.get("sha")!r:.200} is not a 40-hex commit'
    modules = doc.get('modules')
    if not isinstance(modules, dict) or not modules:
        return 'no modules'
    return next((why for d, e in modules.items() if (why := valid_module(d, e, planned=None))), '')


def valid_plan(doc: object) -> str:
    """Why a plan cannot be trusted to scope a shard, or '' when it can."""
    if why := valid_sizing(doc):
        return why
    k = doc.get('shards')
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        return f'shards {k!r:.200} is not a positive integer'
    modules = doc['modules']
    if why := next((why for d, e in modules.items() if (why := valid_module(d, e, planned=k))), ''):
        return why
    covered = {n for e in modules.values() for n in e['shards']}
    if covered != set(range(1, k + 1)):
        return f'shards {sorted(set(range(1, k + 1)) - covered)} run no module'
    return ''


def history_fault(doc: object, repo: str) -> str:
    """Why DOC is not REPO's whole cost history, or ''; a history is used whole or not at all."""
    if (
        not isinstance(doc, dict)
        or not set(doc) <= {'repo', 'modules', 'slowdown', 'prior_error'}
        or doc.get('repo') != repo
        or not isinstance(doc.get('modules'), dict)
    ):
        return f"it is not {repo}'s {{repo, modules, slowdown, prior_error}} object"
    for key in ('slowdown', 'prior_error'):
        if doc.get(key) is not None and not mutation_cost.nonnegative(doc[key]):
            return f'its {key} {doc[key]!r:.200} is not a ratio'
    for d, rec in doc['modules'].items():
        if why := mutation_cost.record_fault(rec):
            return f'module {d!r:.200}: {why}'
    return ''


def read_history(path: Path | None, repo: str) -> dict | None:
    """REPO's last cost history at PATH, or None when it has none; raises what mutation_cost.load_json
    raises, and ValueError for a value that is not REPO's history of cost records."""
    if path is None or not path.is_file():
        return None
    doc = mutation_cost.load_json(path)
    if why := history_fault(doc, repo):
        raise ValueError(why)
    return doc


def plan_repo(
    repo: str, plan_path: Path, history_path: Path | None
) -> tuple[dict, dict | None, list[Template]] | None:
    """REPO's run plan, the history it priced from and its warnings, or None when it runs one job per attempt.

    A refused history prices every file from the probe; a refused or unreadable
    plan runs each attempt in one job, which tests everything all the same.
    """
    try:
        history = read_history(history_path, repo)
    except Exception as exc:
        mutation_cost.annotate(
            'warning',
            t'{repo}: its cost history failed to load ({mutation_cost.describe(exc)}), so every '
            t"file is priced from the plan job's probe",
        )
        history = None
    try:
        doc = mutation_cost.load_json(plan_path) if plan_path.is_file() else None
    except Exception as exc:
        mutation_cost.annotate(
            'warning',
            t'{repo}: its shard plan failed to load ({mutation_cost.describe(exc)}), so each '
            t'attempt runs in one job',
        )
        return None
    if doc is None:
        mutation_cost.annotate('warning', t'{repo}: no shard plan, so each attempt runs in one job')
        return None
    named = doc.get('repo') if isinstance(doc, dict) else None
    why = f'it names repo {named!r:.200}' if named != repo else valid_sizing(doc)
    if why:
        mutation_cost.annotate(
            'warning', t'{repo}: shard plan refused ({why}), so each attempt runs in one job'
        )
        return None
    plan, warnings = assign(doc, history)
    return plan, history, warnings


def matrix(
    repo: str,
    plan_path: Path,
    attempts: int,
    *,
    history_path: Path | None = None,
    out_plan: Path | None = None,
) -> list[dict]:
    """REPO's run matrix, attempt x shard; a repo that shards gets its run plan in OUT_PLAN."""
    mutation_cost.entry_key(repo)
    k = 1
    if (planned := plan_repo(repo, plan_path, history_path)) is not None:
        plan, history, warnings = planned
        k = plan['shards']
        if out_plan is not None:
            out_plan.parent.mkdir(parents=True, exist_ok=True)
            out_plan.write_text(json.dumps(plan, indent=1) + '\n')
        for w in warnings:
            mutation_cost.annotate('warning', w)
        measured = sum(
            1
            for e in plan['modules'].values()
            for f in e.get('files', {}).values()
            if f.get('cost') == 'measured'
        )
        mutation_cost.say(
            t'{repo}: {k} shard(s), projected {plan["projected_minutes"]} min '
            t'(one job: {plan["one_job_minutes"]} min, {measured} file(s) priced from '
            t'{"history" if history else "no history"})'
        )
    if k == 1:
        return [{'repo': repo, 'attempt': n} for n in range(1, attempts + 1)]
    return [
        {'repo': repo, 'attempt': n, 'shard': i, 'shards': k}
        for n in range(1, attempts + 1)
        for i in range(1, k + 1)
    ]


def shard_modules(doc: dict, shard: int, modules: list[str]) -> list[str]:
    if why := valid_plan(doc):
        raise SystemExit(f'gremlins-shard: the plan cannot be used ({why})')
    if not 1 <= shard <= doc['shards']:
        raise SystemExit(f"gremlins-shard: shard {shard} is outside the plan's 1..{doc['shards']}")
    planned = doc['modules']
    if sorted(planned) != sorted(modules):
        raise SystemExit(
            f'gremlins-shard: this checkout has modules {sorted(modules)} but the plan sized '
            f'{sorted(planned)}'
        )
    return [
        ' '.join(
            str(v) for v in ('run', d, planned[d]['mode'], planned[d]['workers']) if v is not None
        )
        if shard in planned[d]['shards']
        else f'skip {d}'
        for d in modules
    ]


def module_costs(path: Path) -> dict:
    """One module's measured costs from the `<epoch>\t<line>` record gremlins-run.sh writes.

    Its `#workers` and `#launch` (the container's start) header rows give the
    pool size and the setup, which ends when gremlins' coverage pass does.
    """
    header: dict[str, float] = {}
    start, events = None, []
    for row in path.read_text(errors='replace').splitlines():
        stamp, sep, line = row.partition('\t')
        try:
            value = float(line if stamp in ('#workers', '#launch') else stamp)
        except ValueError:
            continue
        if stamp in ('#workers', '#launch'):
            header[stamp] = value
        elif not sep:
            continue
        elif start is None and MUTATION_START in line:
            start = value
        elif m := COMPLETION.match(line):
            events.append((value, m.group(1)))
    workers = int(header.get('#workers', 0))
    if workers < 1 or '#launch' not in header or start is None:
        raise ValueError(
            f'{path} has no worker count, no launch time or no end of the coverage pass'
        )
    return {
        'setup_secs': round(max(0.0, start - header['#launch']) + JOB_OVERHEAD_SECS, 1),
        'files': mutation_cost.attribute(events, start, workers),
    }


def costs(modules: list[tuple[str, Path]]) -> dict:
    out = {}
    for dir_, path in modules:
        try:
            out[dir_] = module_costs(path)
        except (OSError, ValueError) as exc:
            mutation_cost.annotate(
                'warning',
                t'module {dir_}: no cost record ({mutation_cost.describe(exc)}), so next week '
                t'prices it from its probe',
            )
    return {'modules': out}


def read_doc(path: Path | None) -> dict:
    """A JSON object from PATH, or {} when it is absent or unusable."""
    try:
        doc = mutation_cost.load_json(path) if path else None
    except Exception:
        doc = None
    return doc if isinstance(doc, dict) else {}


def slowdown(runs: dict[str, dict[str, tuple[float, int]]]) -> float | None:
    """The largest ratio of two attempts' seconds per mutant on one shard's identical work, or None.

    RUNS is, per shard, each attempt's (worker-seconds, mutants); only attempts
    that measured the shard's most mutants did its whole work.
    """
    ratios = []
    for attempts in runs.values():
        most = max((n for _, n in attempts.values()), default=0)
        rates = [secs / n for secs, n in attempts.values() if n == most and n > 0 and secs > 0]
        if len(rates) > 1:
            ratios.append(max(rates) / min(rates))
    return round(max(ratios), 2) if ratios else None


def prior_rows(e: object, measured: dict) -> list[tuple[object, float, float]]:
    """(shard, measured seconds, prior seconds) of each unit of plan module E priced from its prior alone,
    and measured whole, for mutation_cost.prior_error; MEASURED is this run's mean per file over
    its attempts."""
    if not isinstance(e, dict):
        return []
    prior_priced = ('prior', 'refused')
    if e.get('mode') == 'integration':
        shards = e.get('shards') or [None]
        if e.get('cost') not in prior_priced or not measured or not e.get('prior_secs'):
            return []
        if not mutation_cost.count(e.get('mutants')) or (
            sum(f['mutants'] for f in measured.values()) < e['mutants']
        ):
            return []
        return [(shards[0], sum(f['secs'] for f in measured.values()), e['prior_secs'])]
    files = e.get('files') if isinstance(e.get('files'), dict) else {}
    return [
        (f.get('shard'), measured[p]['secs'], f['prior_secs'])
        for p, f in files.items()
        if isinstance(f, dict)
        and f.get('cost') in prior_priced
        and p in measured
        and mutation_cost.nonnegative(f.get('prior_secs'))
        and mutation_cost.count(f.get('runnable'))
        and measured[p]['mutants'] >= f['runnable']
    ]


def history(
    repo: str, artifacts_dir: Path, previous: Path | None = None, plan: Path | None = None
) -> dict | None:
    """The repo's history: this run's cost records bound to it by meta.json, carried over PREVIOUS.

    A file this run did not measure keeps its PREVIOUS measure while its blob is
    the one PLAN gives it; with this run's PLAN, only its modules, and in a
    per-package module only its files, are kept, each with its blob. A module no
    PLAN gives blobs keeps its PREVIOUS record and records no new measure.
    """
    records: dict[str, list[dict]] = {}
    runs: dict[str, dict[str, tuple[float, int]]] = {}
    for meta_path in sorted(artifacts_dir.rglob('meta.json')):
        meta, doc = read_doc(meta_path), read_doc(meta_path.parent / 'gremlins-costs.json')
        if meta.get('repo') != repo or not isinstance(doc.get('modules'), dict):
            continue
        secs, mutants = 0.0, 0
        for dir_, rec in doc['modules'].items():
            if isinstance(dir_, str) and isinstance(rec, dict):
                records.setdefault(dir_, []).append(rec)
                for e in mutation_cost.clean_files(rec.get('files')).values():
                    secs, mutants = secs + e['secs'], mutants + e['mutants']
        runs.setdefault(str(meta.get('shard', '')), {})[str(meta.get('attempt'))] = (secs, mutants)
    try:
        old = read_history(previous, repo)
    except Exception as exc:
        mutation_cost.annotate(
            'warning',
            t'{repo}: the history this run priced from failed to load '
            t"({mutation_cost.describe(exc)}), so only this run's measures are kept",
        )
        old = None
    old = old['modules'] if old else {}
    planned = read_doc(plan).get('modules')
    dirs = set(records) | {d for d in old if isinstance(d, str)}
    if isinstance(planned, dict):
        dirs &= set(planned)
    modules = {}
    rows: list[tuple[object, float, float]] = []
    for d in sorted(dirs):
        e = planned.get(d) if isinstance(planned, dict) else None
        files = e.get('files') if isinstance(e, dict) and e.get('mode') == 'per-package' else None
        keep = set(files) if isinstance(files, dict) else None
        counts = (
            {
                p: f['runnable']
                for p, f in files.items()
                if isinstance(f, dict) and mutation_cost.count(f.get('runnable'))
            }
            if keep
            else None
        )
        new = mutation_cost.combine(records.get(d, []))
        prev = old.get(d) if isinstance(old.get(d), dict) else {}
        versions = e.get('blobs') if isinstance(e, dict) else None
        if not isinstance(versions, dict):
            # No plan names this run's versions, so its measures price nothing and the last stay as they were.
            versions = prev.get('blobs') if isinstance(prev.get('blobs'), dict) else {}
            new = {**new, 'files': {}}
        last = mutation_cost.at_blobs(prev, versions)
        if (
            isinstance(e, dict)
            and e.get('mode') == 'integration'
            and mutation_cost.count(e.get('mutants'))
            and sum(f['mutants'] for f in new['files'].values()) < e['mutants']
        ):
            # integration_price prices the module from its total, so a short sample keeps the last one whole.
            new = {**new, 'files': {}}
        rows += prior_rows(e, new['files'])
        rec = mutation_cost.carry_forward(new, last, keep, counts)
        rec['blobs'] = {p: versions[p] for p in rec['files'] if isinstance(versions.get(p), str)}
        modules[d] = rec
    if not any(m['files'] or m['setup_secs'] is not None for m in modules.values()):
        return None
    return {
        'repo': repo,
        'modules': modules,
        'slowdown': slowdown(runs),
        'prior_error': mutation_cost.prior_error(rows),
    }


def parse_module_arg(raw: str) -> tuple[str, Path]:
    dir_, sep, path = raw.partition('=')
    if not sep or not path:
        raise SystemExit(f'--module expects DIR=PATH, got {raw!r:.200}')
    return dir_.rstrip('/') or '.', Path(path)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest='command', required=True)
    pl = sub.add_parser('plan', help="size one repo's modules")
    pl.add_argument('--repo', required=True)
    pl.add_argument('--sha', required=True)
    pl.add_argument('--module', action='append', required=True, metavar='DIR=PLAN_DIR')
    pl.add_argument('--out', required=True, type=Path)
    pl.add_argument('--target', type=Path, help="the target repo's clone, for each file's git blob")
    mx = sub.add_parser('matrix', help="one repo's run plan and its run job's matrix")
    mx.add_argument('--repo', required=True)
    mx.add_argument('--plan', required=True, type=Path, help="the plan job's sizing")
    mx.add_argument('--attempts', required=True, type=int)
    mx.add_argument('--history', type=Path, help="the repo's last cost history")
    mx.add_argument(
        '--out-plan', type=Path, help='where to write the run plan when the repo shards'
    )
    md = sub.add_parser('modules', help='which modules one shard runs')
    md.add_argument('--plan', required=True, type=Path)
    md.add_argument('--shard', required=True, type=int)
    md.add_argument('--modules', required=True, help='space-separated module dirs of this checkout')
    co = sub.add_parser('costs', help="one shard's measured cost per file")
    co.add_argument('--module', action='append', required=True, metavar='DIR=TIMES')
    co.add_argument('--out', required=True, type=Path)
    hi = sub.add_parser(
        'history', help="one repo's cost history from this run's artifacts and the last one"
    )
    hi.add_argument('--repo', required=True)
    hi.add_argument('--artifacts-dir', required=True, type=Path)
    hi.add_argument('--previous', type=Path, help="the history this run's shard job planned from")
    hi.add_argument('--plan', type=Path, help="this run's plan for the repo")
    hi.add_argument('--out', required=True, type=Path)
    ca = sub.add_parser('capped', help='name the repo when its run plan was capped')
    ca.add_argument('--repo', required=True)
    ca.add_argument('--plan', required=True, type=Path, help="this run's plan for the repo")
    args = p.parse_args()

    if args.command == 'plan':
        if not SHA.fullmatch(args.sha):
            raise SystemExit(f'--sha {args.sha!r:.200} is not a 40-hex commit')
        doc, unsized = sizing(
            args.repo, args.sha, [parse_module_arg(m) for m in args.module], args.target
        )
        args.out.write_text(json.dumps(doc, indent=1) + '\n')
        files = sum(len(e['files']) for e in doc['modules'].values())
        mutation_cost.say(t'{args.repo}: sized {len(doc["modules"])} module(s), {files} file(s)')
        if unsized:
            return 1
    elif args.command == 'matrix':
        out = matrix(
            args.repo,
            args.plan,
            args.attempts,
            history_path=args.history,
            out_plan=args.out_plan,
        )
        print(json.dumps(out, separators=(',', ':')))
    elif args.command == 'modules':
        doc = json.loads(args.plan.read_text())
        print('\n'.join(shard_modules(doc, args.shard, args.modules.split())))
    elif args.command == 'costs':
        args.out.write_text(json.dumps(costs([parse_module_arg(m) for m in args.module])) + '\n')
    elif args.command == 'capped':
        if note := mutation_cost.cap_note(args.repo, read_doc(args.plan)):
            mutation_cost.annotate('warning', note, summary=True)
    else:
        doc = history(args.repo, args.artifacts_dir, args.previous, args.plan)
        if doc is None:
            mutation_cost.say(t'{args.repo}: no cost record this week')
            return 0
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, indent=1) + '\n')
        if (measured := doc['slowdown']) is not None:
            line = t'{args.repo}: runner slowdown this week {measured}x on identical work'
            mutation_cost.say(line)
            mutation_cost.summarize(line)
            if measured > mutation_cost.SLOWDOWN:
                mutation_cost.annotate(
                    'warning',
                    t'{args.repo}: attempts ran {measured}x apart on identical work, past the '
                    t'{mutation_cost.SLOWDOWN}x mutation_cost.SLOWDOWN plans for',
                    summary=True,
                )
        mutation_cost.note_prior_error(args.repo, doc['prior_error'])
    return 0


if __name__ == '__main__':
    sys.exit(mutation_cost.run_main(main))
